from __future__ import annotations


class MiniappGenerationContractApiRoutesCrud:
    @staticmethod
    def _deterministic_requests_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api", tags=["requests"])


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
            "equipment_type TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS comments ("
            "id TEXT PRIMARY KEY, "
            "request_id TEXT, "
            "comment TEXT, "
            "author_role TEXT, "
            "created_at TEXT)"
        ))


def _serialize_request(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    start_date = mapping.get("start_date") or ""
    end_date = mapping.get("end_date") or ""
    return {
        "request_id": mapping["id"],
        "id": mapping["id"],
        "submission_id": mapping["id"],
        "title": mapping["title"] or "Request",
        "description": mapping["description"] or "",
        "client_name": mapping["client_name"] or "",
        "phone": mapping["phone"] or "",
        "preferred_time": mapping["preferred_time"] or "",
        "comment": mapping["comment"] or "",
        "status": mapping["status"] or "pending_review",
        "assigned_specialist": mapping["assigned_specialist"],
        "specialist": mapping["assigned_specialist"] or "",
        "equipment_type": mapping.get("equipment_type") or "",
        "equipment": mapping.get("equipment_type") or mapping["title"] or "Equipment request",
        "item_type": mapping.get("equipment_type") or mapping["title"] or "Equipment request",
        "start_date": start_date,
        "end_date": end_date,
        "date_range": f"{start_date} → {end_date}" if start_date and end_date else start_date or end_date or "Dates to be confirmed",
        "reason": mapping.get("reason") or mapping["description"] or mapping["comment"] or "",
        "specialist_notes": mapping.get("specialist_notes") or "",
        "availability": "Availability is based on active bookings in the shared queue.",
        "conflict": "No conflicts reported yet.",
        "created_at": mapping["created_at"],
        "updated_at": mapping["updated_at"],
    }


def _fetch_request(conn: Any, request_id: str) -> Any | None:
    return conn.execute(text("SELECT * FROM requests WHERE id = :id"), {"id": request_id}).first()


def _fetch_comments(conn: Any, request_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT id, request_id, comment, author_role, created_at FROM comments WHERE request_id = :request_id ORDER BY created_at ASC"),
        {"request_id": request_id},
    ).fetchall()
    return [
        {
            "comment_id": row._mapping["id"],
            "request_id": row._mapping["request_id"],
            "comment": row._mapping["comment"] or "",
            "author_role": row._mapping["author_role"] or "",
            "created_at": row._mapping["created_at"],
        }
        for row in rows
    ]


def _timeline_for_request(request: dict[str, Any], comments: list[dict[str, Any]]) -> list[dict[str, str]]:
    timeline = [
        {"title": "Submitted", "note": request.get("created_at") or "Created"},
        {"title": "Current status", "note": request.get("status") or "pending_review"},
    ]
    for item in comments[-3:]:
        note = (item.get("comment") or "").strip()
        if not note:
            continue
        timeline.append({"title": f"{item.get('author_role') or 'team'} note", "note": note})
    return timeline


@router.get("/requests")
@router.get("/submissions")
def list_requests() -> dict[str, list[dict[str, Any]]]:
    _ensure_tables()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT * FROM requests ORDER BY created_at DESC")).fetchall()
    return {"items": [_serialize_request(row) for row in rows]}


@router.post("/requests")
@router.post("/submissions")
def create_request(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _ensure_tables()
    request_id = str(payload.get("request_id") or payload.get("id") or uuid4().hex[:12])
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": request_id,
        "title": str(payload.get("title") or payload.get("equipment_type") or payload.get("item_type") or payload.get("task") or payload.get("subject") or "Request"),
        "description": str(payload.get("description") or payload.get("details") or payload.get("reason") or ""),
        "client_name": str(payload.get("requested_by") or payload.get("name") or payload.get("client_name") or ""),
        "phone": str(payload.get("phone") or ""),
        "preferred_time": str(payload.get("preferred_time") or payload.get("date") or payload.get("slot") or payload.get("start_date") or ""),
        "comment": str(payload.get("comment") or payload.get("notes") or ""),
        "status": str(payload.get("status") or "pending_review"),
        "assigned_specialist": payload.get("assigned_specialist"),
        "equipment_type": str(payload.get("equipment_type") or payload.get("item_type") or payload.get("title") or ""),
        "start_date": str(payload.get("start_date") or ""),
        "end_date": str(payload.get("end_date") or ""),
        "reason": str(payload.get("reason") or payload.get("purpose") or payload.get("description") or ""),
        "specialist_notes": str(payload.get("specialist_notes") or ""),
        "created_at": now,
        "updated_at": now,
    }
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT OR REPLACE INTO requests "
            "(id, title, description, client_name, phone, preferred_time, comment, status, assigned_specialist, equipment_type, start_date, end_date, reason, specialist_notes, created_at, updated_at) "
            "VALUES (:id, :title, :description, :client_name, :phone, :preferred_time, :comment, :status, :assigned_specialist, :equipment_type, :start_date, :end_date, :reason, :specialist_notes, :created_at, :updated_at)"
        ), record)
        row = _fetch_request(conn, request_id)
    return _serialize_request(row)


@router.get("/requests/{request_id}")
@router.get("/submissions/{request_id}")
def get_request(request_id: str) -> dict[str, Any]:
    _ensure_tables()
    with engine.begin() as conn:
        row = _fetch_request(conn, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        payload = _serialize_request(row)
        comments = _fetch_comments(conn, request_id)
    payload["comments"] = comments
    payload["timeline"] = _timeline_for_request(payload, comments)
    return payload


@router.patch("/requests/{request_id}")
def update_request(request_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        row = _fetch_request(conn, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        current = _serialize_request(row)
        record = {
            "id": request_id,
            "title": str(payload.get("title") or current.get("title") or "Request"),
            "description": str(payload.get("description") or current.get("description") or ""),
            "client_name": str(payload.get("client_name") or current.get("client_name") or ""),
            "phone": str(payload.get("phone") or current.get("phone") or ""),
            "preferred_time": str(payload.get("preferred_time") or current.get("preferred_time") or ""),
            "comment": str(payload.get("comment") or current.get("comment") or ""),
            "status": str(payload.get("status") or current.get("status") or "pending_review"),
            "assigned_specialist": payload.get("assigned_specialist") if payload.get("assigned_specialist") is not None else current.get("assigned_specialist"),
            "equipment_type": str(payload.get("equipment_type") or current.get("equipment_type") or ""),
            "start_date": str(payload.get("start_date") or current.get("start_date") or ""),
            "end_date": str(payload.get("end_date") or current.get("end_date") or ""),
            "reason": str(payload.get("reason") or current.get("reason") or ""),
            "specialist_notes": str(payload.get("specialist_notes") or current.get("specialist_notes") or ""),
            "created_at": current.get("created_at") or now,
            "updated_at": now,
        }
        conn.execute(text(
            "INSERT OR REPLACE INTO requests "
            "(id, title, description, client_name, phone, preferred_time, comment, status, assigned_specialist, equipment_type, start_date, end_date, reason, specialist_notes, created_at, updated_at) "
            "VALUES (:id, :title, :description, :client_name, :phone, :preferred_time, :comment, :status, :assigned_specialist, :equipment_type, :start_date, :end_date, :reason, :specialist_notes, :created_at, :updated_at)"
        ), record)
        updated = _fetch_request(conn, request_id)
    return _serialize_request(updated)


@router.patch("/requests/{request_id}/status")
def update_request_status(request_id: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _ensure_tables()
    status = str(payload.get("status") or "approved")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        row = _fetch_request(conn, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        conn.execute(text("UPDATE requests SET status = :status, updated_at = :updated_at WHERE id = :id"), {"id": request_id, "status": status, "updated_at": now})
        row = _fetch_request(conn, request_id)
    return _serialize_request(row)
"""

    @staticmethod
    def _deterministic_comments_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/comments", tags=["comments"])


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
            "equipment_type TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))
        conn.execute(text("CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, request_id TEXT, comment TEXT, author_role TEXT, created_at TEXT)"))


@router.get("")
@router.get("/")
def list_comments(request_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    _ensure_tables()
    query = "SELECT id, request_id, comment, author_role, created_at FROM comments"
    params: dict[str, Any] = {}
    if request_id:
        query += " WHERE request_id = :request_id"
        params["request_id"] = request_id
    query += " ORDER BY created_at ASC"
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).fetchall()
    return {
        "items": [
            {
                "comment_id": row._mapping["id"],
                "request_id": row._mapping["request_id"],
                "comment": row._mapping["comment"] or "",
                "author_role": row._mapping["author_role"] or "",
                "created_at": row._mapping["created_at"],
            }
            for row in rows
        ]
    }


@router.post("")
@router.post("/")
def create_comment(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    _ensure_tables()
    comment_id = uuid4().hex[:12]
    request_id = str(payload.get("request_id") or payload.get("id") or "")
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id required")
    record = {
        "id": comment_id,
        "request_id": request_id,
        "comment": str(payload.get("comment") or payload.get("note") or ""),
        "author_role": str(payload.get("author_role") or "specialist"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as conn:
        request_row = conn.execute(text("SELECT id FROM requests WHERE id = :id"), {"id": request_id}).first()
        if request_row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        conn.execute(text("INSERT INTO comments (id, request_id, comment, author_role, created_at) VALUES (:id, :request_id, :comment, :author_role, :created_at)"), record)
    return {"comment_id": comment_id, **record}
"""

    @staticmethod
    def _deterministic_assignments_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


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
            "equipment_type TEXT, "
            "start_date TEXT, "
            "end_date TEXT, "
            "reason TEXT, "
            "specialist_notes TEXT, "
            "created_at TEXT, "
            "updated_at TEXT)"
        ))


@router.post("")
@router.patch("/{request_id}")
def assign_request(request_id: str | None = None, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    assignment_request_id = str(request_id or payload.get("request_id") or payload.get("id") or "")
    specialist = str(payload.get("specialist_id") or payload.get("assignee_id") or payload.get("assigned_specialist") or "")
    now = datetime.now(timezone.utc).isoformat()
    _ensure_tables()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id FROM requests WHERE id = :id"), {"id": assignment_request_id}).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        conn.execute(text("UPDATE requests SET assigned_specialist = :specialist, updated_at = :updated_at WHERE id = :id"), {"id": assignment_request_id, "specialist": specialist, "updated_at": now})
    return {"request_id": assignment_request_id, "assigned_specialist": specialist, "updated_at": now}
"""
