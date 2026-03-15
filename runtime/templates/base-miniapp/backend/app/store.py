from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.schemas import AppRole, RoleDashboardResponse, RoleProfile, RuntimeActionResponse, RuntimeManifestResponse


BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = GENERATED_DIR / "runtime_state.json"
MANIFEST_PATH = GENERATED_DIR / "runtime_manifest.json"


DEFAULT_RUNTIME_MANIFEST = {
    "app": {
        "title": "",
        "goal": "",
        "generation_mode": "basic",
        "platform": "telegram_mini_app",
        "preview_profile": "telegram_mock",
        "route_count": 0,
        "screen_count": 0,
    },
    "roles": {
        "client": {
            "entry_path": "/",
            "navigation": [],
            "routes": [],
            "screens": {},
        },
        "specialist": {
            "entry_path": "/",
            "navigation": [],
            "routes": [],
            "screens": {},
        },
        "manager": {
            "entry_path": "/",
            "navigation": [],
            "routes": [],
            "screens": {},
        },
    },
}

DEFAULT_RUNTIME_STATE = {
    "metadata": {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generation_mode": "basic",
        "goal": "",
    },
    "records": [],
    "roles": {
        "client": {
            "profile": {
                "first_name": "",
                "last_name": "",
                "email": "",
                "phone": "",
                "photo_url": None,
            },
            "metrics": [],
        },
        "specialist": {
            "profile": {
                "first_name": "",
                "last_name": "",
                "email": "",
                "phone": "",
                "photo_url": None,
            },
            "metrics": [],
        },
        "manager": {
            "profile": {
                "first_name": "",
                "last_name": "",
                "email": "",
                "phone": "",
                "photo_url": None,
            },
            "metrics": [],
            "alerts": [],
        },
    },
    "activity": [],
}

def ensure_state() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        MANIFEST_PATH.write_text(json.dumps(DEFAULT_RUNTIME_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps(DEFAULT_RUNTIME_STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_state()


def load_manifest() -> dict[str, Any]:
    ensure_state()
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    ensure_state()
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def get_role_dashboard(role: AppRole) -> RoleDashboardResponse:
    manifest = get_role_manifest(role)
    first_screen = next(iter(manifest.screens.values()), None)
    return RoleDashboardResponse(
        role=role,
        title=first_screen["title"] if first_screen else "",
        description=(first_screen.get("subtitle") if first_screen else "") or manifest.app.get("goal", ""),
        feature_text=manifest.app.get("goal", ""),
        metrics=manifest.metrics,
        primary_action_label=(first_screen.get("actions", [{}])[0].get("label", "") if first_screen else ""),
        secondary_action_label="Profile",
    )


def get_role_profile(role: AppRole) -> RoleProfile:
    state = load_state()
    return RoleProfile.model_validate(state["roles"][role]["profile"])


def save_role_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    state = load_state()
    payload = profile.model_dump(mode="json")
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["roles"][role]["profile"] = payload
    save_state(state)
    return RoleProfile.model_validate(payload)


def register_submission(payload: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    now = datetime.now(timezone.utc)
    record_id = f"req_{int(now.timestamp())}"
    title = payload.get("title") or payload.get("name") or f"Request {record_id}"
    state["records"].insert(
        0,
        {
            "record_id": record_id,
            "title": title,
            "status": "new",
            "priority": "medium",
            "owner": "unassigned",
            "summary": payload.get("comment") or "New request submitted through generated runtime.",
            "payload": payload,
            "timeline": [{"label": "Created", "value": now.strftime("%Y-%m-%d %H:%M")}],
        },
    )
    state["activity"].insert(0, {"event_id": f"evt_{int(now.timestamp())}", "label": f"Client submitted {title}", "role": "client"})
    save_state(state)
    return {
        "submission_id": record_id,
        "status": "stored",
        "payload": payload,
    }


def get_role_manifest(role: AppRole) -> RuntimeManifestResponse:
    manifest = deepcopy(load_manifest())
    state = load_state()
    metrics = _compute_metrics(state)
    role_manifest = manifest["roles"][role]
    hydrated_screens = {
        screen_id: _hydrate_screen(role, screen, state, metrics)
        for screen_id, screen in role_manifest["screens"].items()
    }
    alerts = state["roles"].get("manager", {}).get("alerts", []) if role == "manager" else []
    return RuntimeManifestResponse(
        role=role,
        entry_path=role_manifest["entry_path"],
        routes=role_manifest["routes"],
        navigation=role_manifest["navigation"],
        screens=hydrated_screens,
        metrics=[item for item in metrics[role]],
        profile=RoleProfile.model_validate(state["roles"][role]["profile"]),
        alerts=alerts,
        activity=state.get("activity", []),
        app=manifest["app"],
    )


def execute_action(role: AppRole, action_id: str, *, payload: dict[str, Any] | None = None, item_id: str | None = None) -> RuntimeActionResponse:
    payload = payload or {}
    state = load_state()
    if action_id == "client_submit_request":
        response = register_submission(payload)
        return RuntimeActionResponse(message="Request submitted.", next_path="/requests", record_id=response["submission_id"])

    if action_id == "specialist_claim_next":
        record = next((item for item in state["records"] if item["status"] == "new"), None)
        if not record:
            return RuntimeActionResponse(message="Queue is already empty.", next_path="/queue")
        record["status"] = "in_progress"
        record["owner"] = "specialist"
        record.setdefault("timeline", []).append({"label": "Claimed", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")})
        state["activity"].insert(0, {"event_id": f"evt_{int(datetime.now(timezone.utc).timestamp())}", "label": f"Specialist claimed {record['title']}", "role": "specialist"})
        save_state(state)
        return RuntimeActionResponse(message="Next request claimed.", next_path="/queue/detail", record_id=record["record_id"])

    if action_id == "specialist_mark_in_progress":
        record = _resolve_record(state, item_id)
        if record:
            record["status"] = "in_progress"
            record.setdefault("timeline", []).append({"label": "In progress", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")})
            save_state(state)
        return RuntimeActionResponse(message="Request marked in progress.", next_path="/queue/detail", record_id=record["record_id"] if record else None)

    if action_id == "specialist_complete_request":
        record = _resolve_record(state, item_id)
        if record:
            record["status"] = "completed"
            record.setdefault("timeline", []).append({"label": "Completed", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")})
            save_state(state)
        return RuntimeActionResponse(message="Request completed.", next_path="/queue", record_id=record["record_id"] if record else None)

    if action_id == "manager_rebalance":
        state["roles"]["manager"]["alerts"] = [
            "Load was rebalanced across specialists.",
            "No overloaded assignee remains in the current queue.",
        ]
        state["activity"].insert(0, {"event_id": f"evt_{int(datetime.now(timezone.utc).timestamp())}", "label": "Manager rebalanced workload", "role": "manager"})
        save_state(state)
        return RuntimeActionResponse(message="Load rebalance simulated.", next_path="/dashboard")

    if action_id == "manager_refresh_records":
        return RuntimeActionResponse(message="Records refreshed.", next_path="/records")

    next_path = _find_action_target(role, action_id)
    return RuntimeActionResponse(message="Action executed.", next_path=next_path)


def _find_action_target(role: AppRole, action_id: str) -> str | None:
    manifest = load_manifest()
    role_manifest = manifest["roles"][role]
    for screen in role_manifest["screens"].values():
        for action in screen.get("actions", []):
            if action.get("action_id") == action_id:
                return cast(str | None, action.get("target_path"))
    return None


def _resolve_record(state: dict[str, Any], item_id: str | None) -> dict[str, Any] | None:
    if item_id:
        for record in state["records"]:
            if record["record_id"] == item_id:
                return record
    return state["records"][0] if state["records"] else None


def _compute_metrics(state: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    records = state.get("records", [])
    new_count = sum(1 for record in records if record["status"] == "new")
    in_progress_count = sum(1 for record in records if record["status"] == "in_progress")
    completed_count = sum(1 for record in records if record["status"] == "completed")
    return {
        "client": [
            {"metric_id": "client_total", "label": "Requests", "value": str(len(records))},
            {"metric_id": "client_active", "label": "Active", "value": str(new_count + in_progress_count)},
        ],
        "specialist": [
            {"metric_id": "specialist_queue", "label": "Queue", "value": str(new_count)},
            {"metric_id": "specialist_progress", "label": "In progress", "value": str(in_progress_count)},
        ],
        "manager": [
            {"metric_id": "manager_completed", "label": "Completed", "value": str(completed_count)},
            {"metric_id": "manager_sla", "label": "SLA", "value": "96%"},
        ],
    }


def _records_for_role(role: AppRole, state: dict[str, Any]) -> list[dict[str, Any]]:
    records = deepcopy(state.get("records", []))
    if role == "client":
        return records[:3]
    return records


def _hydrate_screen(role: AppRole, screen: dict[str, Any], state: dict[str, Any], metrics: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    hydrated = deepcopy(screen)
    records = _records_for_role(role, state)
    kind = hydrated.get("kind")
    screen_id = hydrated.get("screen_id", "")
    if screen_id.endswith("home"):
        hydrated["sections"] = [
            {"section_id": f"{screen_id}_hero", "type": "hero", "title": hydrated["title"], "body": hydrated.get("subtitle", "")},
            {"section_id": f"{screen_id}_stats", "type": "stats", "items": metrics[role]},
        ]
    elif kind == "form":
        sample_payload = records[0]["payload"] if records else {}
        hydrated["sections"] = [
            {"section_id": f"{screen_id}_hero", "type": "hero", "title": hydrated["title"], "body": hydrated.get("subtitle", "")},
            {
                "section_id": f"{screen_id}_form",
                "type": "form",
                "fields": [
                    {
                        "field_id": key,
                        "name": key,
                        "label": key.replace("_", " ").title(),
                        "field_type": "textarea" if key == "comment" else ("phone" if key == "phone" else ("date" if key == "date" else "string")),
                        "required": key in {"name", "phone"},
                        "placeholder": value if isinstance(value, str) else "",
                    }
                    for key, value in sample_payload.items()
                ] or [
                    {"field_id": "title", "name": "title", "label": "Title", "field_type": "text", "required": True, "placeholder": "Describe the request"},
                    {"field_id": "details", "name": "details", "label": "Details", "field_type": "text", "required": False, "placeholder": "Add details"},
                ],
            },
        ]
    elif kind == "list":
        hydrated["sections"] = [
            {
                "section_id": f"{screen_id}_list",
                "type": "list",
                "items": [
                    {
                        "item_id": record["record_id"],
                        "title": record["title"],
                        "subtitle": record["summary"],
                        "status": record["status"],
                        "meta": record["priority"],
                    }
                    for record in records
                ],
            }
        ]
    elif kind == "details":
        record = records[0] if records else None
        hydrated["sections"] = [
            {
                "section_id": f"{screen_id}_detail",
                "type": "detail",
                "title": record["title"] if record else hydrated["title"],
                "body": record["summary"] if record else hydrated.get("subtitle", ""),
                "fields": [{"label": key.replace("_", " ").title(), "value": value} for key, value in (record["payload"] if record else {}).items()],
            },
            {
                "section_id": f"{screen_id}_timeline",
                "type": "timeline",
                "items": record["timeline"] if record else [],
            },
        ]
    elif screen_id.endswith("profile"):
        profile = state["roles"][role]["profile"]
        hydrated["sections"] = [
            {
                "section_id": f"{screen_id}_profile",
                "type": "profile",
                "fields": [
                    {"name": "first_name", "label": "First name", "value": profile.get("first_name", "")},
                    {"name": "last_name", "label": "Last name", "value": profile.get("last_name", "")},
                    {"name": "email", "label": "Email", "value": profile.get("email", "")},
                    {"name": "phone", "label": "Phone", "value": profile.get("phone", "")},
                ],
            }
        ]
    return hydrated
