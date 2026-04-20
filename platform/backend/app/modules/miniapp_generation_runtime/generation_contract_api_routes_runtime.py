from __future__ import annotations


class MiniappGenerationContractApiRoutesRuntime:
    @staticmethod
    def _deterministic_runtime_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/runtime", tags=["runtime"])
ALLOWED_ROLES = {"client", "specialist", "manager"}


def _normalize_runtime_role(role: str) -> str:
    normalized = str(role or "").strip().strip("/")
    if normalized == "sample":
        return "client"
    return normalized


def _validate_role(role: str) -> str:
    normalized = _normalize_runtime_role(role)
    if normalized not in ALLOWED_ROLES:
        raise HTTPException(status_code=404, detail="Role not supported")
    return normalized


def _ensure_tables() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS requests ("
            "id TEXT PRIMARY KEY, "
            "title TEXT, "
            "description TEXT, "
            "client_name TEXT, "
            "phone TEXT, "
            "preferred_time TEXT, "
            "comment TEXT, "
            "status TEXT, "
            "assigned_specialist TEXT, "
            "item_type TEXT, "
            "item_label TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, request_id TEXT, comment TEXT, author_role TEXT, created_at TEXT)"
        ))


def _serialize_request(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    start_date = mapping.get("start_date") or ""
    end_date = mapping.get("end_date") or ""
    return {
        "request_id": mapping["id"],
        "id": mapping["id"],
        "title": mapping["title"] or "Request",
        "item_type": mapping.get("item_type") or "",
        "item_label": mapping.get("item_label") or "",
        "employee_name": mapping.get("client_name") or "",
        "client_name": mapping.get("client_name") or "",
        "start_date": start_date,
        "end_date": end_date,
        "date_range": f"{start_date} → {end_date}" if start_date and end_date else start_date or end_date or "Dates to be confirmed",
        "reason": mapping.get("reason") or mapping.get("description") or mapping.get("comment") or "",
        "status": mapping.get("status") or "submitted",
        "assigned_specialist": mapping.get("assigned_specialist") or "",
        "specialist_notes": mapping.get("specialist_notes") or "",
    }


def _fetch_requests() -> list[dict[str, Any]]:
    _ensure_tables()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT * FROM requests ORDER BY created_at DESC")).fetchall()
    return [_serialize_request(row) for row in rows]


def _availability(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    active_statuses = {"submitted", "in_review", "issued", "in_progress", "claimed"}
    for item in items:
        label = str(item.get("item_label") or item.get("item_type") or item.get("title") or "Unlabeled item").strip()
        entry = buckets.setdefault(
            label,
            {"name": label, "item": label, "item_type": str(item.get("item_type") or ""), "active": 0, "total": 0},
        )
        entry["total"] += 1
        if str(item.get("status") or "").lower() in active_statuses:
            entry["active"] += 1
    results: list[dict[str, Any]] = []
    for label in sorted(buckets):
        entry = buckets[label]
        results.append(
            {
                "name": entry["name"],
                "item": entry["item"],
                "item_type": entry["item_type"],
                "active": entry["active"],
                "total": entry["total"],
                "available": max(entry["total"] - entry["active"], 0),
                "status": "Active" if entry["active"] else "Idle",
                "detail": f"{entry['active']} active of {entry['total']} tracked",
                "note": f"{entry['active']} active of {entry['total']} tracked",
            }
        )
    return results


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    pending = sum(1 for item in items if item.get("status") == "submitted")
    approved = sum(1 for item in items if item.get("status") == "in_review")
    issued = sum(1 for item in items if item.get("status") == "issued")
    returns_due = sum(1 for item in items if item.get("status") == "issued")
    return {
        "pending": pending,
        "issued_today": issued,
        "returns_due": returns_due,
        "in_review": pending,
        "active": approved + issued,
        "conflicts": 0,
    }


def _manager_conflicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_item: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        label = str(item.get("item_label") or item.get("item_type") or item.get("title") or "Shared item")
        by_item.setdefault(label, []).append(item)
    for label, grouped_items in by_item.items():
        if len(grouped_items) < 2:
            continue
        conflicts.append(
            {
                "title": f"{label} overlap risk",
                "detail": f"{len(grouped_items)} requests currently reference the same shared item.",
            }
        )
    return conflicts


@router.get("/{role}/manifest")
async def runtime_manifest(role: str) -> dict[str, Any]:
    role = _validate_role(role)
    requests = _fetch_requests()
    availability = _availability(requests)
    summary = _summary(requests)
    if role == "client":
        return {
            "role": role,
            "metrics": {"total": len(requests), "active": summary["active"]},
            "requests": requests,
            "availability": availability,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
    if role == "specialist":
        queue = [item for item in requests if item.get("status") in {"submitted", "in_review", "issued"}]
        return {
            "role": role,
            "summary": summary,
            "queue": queue,
            "availability": availability,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
    return {
        "role": role,
        "metrics": {
            "in_review": summary["in_review"],
            "active": summary["active"],
            "conflicts": len(_manager_conflicts(requests)),
        },
        "conflicts": _manager_conflicts(requests),
        "approvals": [item for item in requests if item.get("status") == "submitted"],
        "availability": availability,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
"""
