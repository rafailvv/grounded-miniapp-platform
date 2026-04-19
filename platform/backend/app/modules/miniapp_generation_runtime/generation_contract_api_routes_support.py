from __future__ import annotations


class MiniappGenerationContractApiRoutesSupport:
    @staticmethod
    def _deterministic_profiles_route_source() -> str:
        return """from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.db import RoleProfileRecord, SessionLocal
from app.schemas import AppRole, RoleProfile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _empty_profile() -> RoleProfile:
    return RoleProfile(first_name="", last_name="", email="", phone="", photo_url=None, updated_at=None)


def _to_schema(record: RoleProfileRecord) -> RoleProfile:
    return RoleProfile(
        first_name=record.first_name,
        last_name=record.last_name,
        email=record.email,
        phone=record.phone,
        photo_url=record.photo_url,
        updated_at=record.updated_at,
    )


def load_role_profile(role: AppRole) -> RoleProfile:
    with SessionLocal() as session:
        record = session.get(RoleProfileRecord, role)
        if record is None:
            return _empty_profile()
        return _to_schema(record)


def save_role_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    with SessionLocal() as session:
        record = session.get(RoleProfileRecord, role)
        if record is None:
            record = RoleProfileRecord(role=role)
            session.add(record)
        record.first_name = profile.first_name
        record.last_name = profile.last_name
        record.email = profile.email
        record.phone = profile.phone
        record.photo_url = profile.photo_url
        record.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(record)
        return _to_schema(record)


@router.get("/{role}", response_model=RoleProfile)
def get_profile(role: AppRole) -> RoleProfile:
    return load_role_profile(role)


@router.put("/{role}", response_model=RoleProfile)
def update_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    return save_role_profile(role, profile)
"""

    @staticmethod
    def _deterministic_users_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users")
@router.get("/users/")
@router.get("/specialists")
@router.get("/specialists/")
def list_users() -> dict[str, list[dict[str, str]]]:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS role_profiles (role TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, phone TEXT, photo_url TEXT, updated_at TEXT)"
        ))
        rows = conn.execute(text(
            "SELECT role, first_name, last_name FROM role_profiles WHERE role IN ('specialist', 'manager') ORDER BY role ASC"
        )).fetchall()
    return {
        "items": [
            {
                "user_id": row._mapping["role"],
                "role": row._mapping["role"],
                "name": " ".join(part for part in [row._mapping["first_name"], row._mapping["last_name"]] if part).strip(),
            }
            for row in rows
        ]
    }
"""

    @staticmethod
    def _deterministic_workload_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db import engine

router = APIRouter(prefix="/api/workload", tags=["workload"])


@router.get("")
@router.get("/")
def get_workload() -> dict[str, list[dict[str, object]]]:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, title TEXT, description TEXT, client_name TEXT, phone TEXT, preferred_time TEXT, comment TEXT, status TEXT, assigned_specialist TEXT, equipment_type TEXT, start_date TEXT, end_date TEXT, reason TEXT, specialist_notes TEXT, created_at TEXT, updated_at TEXT)"
        ))
        rows = conn.execute(text(
            "SELECT COALESCE(assigned_specialist, 'unassigned') AS assignee, COUNT(*) AS total FROM requests GROUP BY COALESCE(assigned_specialist, 'unassigned') ORDER BY total DESC"
        )).fetchall()
    return {"items": [{"assignee": row._mapping["assignee"], "total": row._mapping["total"]} for row in rows]}
"""

    @staticmethod
    def _deterministic_time_slots_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/time-slots", tags=["time_slots"])


@router.get("")
@router.get("/")
def list_time_slots() -> dict[str, list[dict[str, str]]]:
    return {
        "items": [
            {"slot_id": "slot-0900", "label": "09:00"},
            {"slot_id": "slot-1100", "label": "11:00"},
            {"slot_id": "slot-1400", "label": "14:00"},
        ]
    }
"""

    @staticmethod
    def _deterministic_bookingrequests_route_source() -> str:
        return """from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, get_args

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from app.db import BookingRequestRecord, SessionLocal
from app.schemas import BookingRequestCreate, BookingRequestListResponse, BookingRequestRead, BookingRequestUpdate

router = APIRouter(prefix="/api/bookingrequests", tags=["bookingrequests"])


def _allowed_statuses() -> list[str]:
    status_field = getattr(BookingRequestRead, "model_fields", {}).get("status")
    annotation = getattr(status_field, "annotation", None)
    values = [str(value) for value in get_args(annotation) if isinstance(value, str)]
    return values or ["submitted", "in_review", "issued", "returned", "cancelled", "conflict"]


def _default_status() -> str:
    allowed = _allowed_statuses()
    for candidate in ("submitted", "pending", "in_review", "claimed", "in_progress", "issued"):
        if candidate in allowed:
            return candidate
    return allowed[0]


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    aliases = {
        "canceled": "cancelled",
        "approved": "in_review",
        "pending_review": "in_review",
        "review": "in_review",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = set(_allowed_statuses())
    if normalized in allowed:
        return normalized
    compatibility = {
        "pending": "submitted" if "submitted" in allowed else ("pending" if "pending" in allowed else None),
        "claimed": "in_review" if "in_review" in allowed else ("claimed" if "claimed" in allowed else None),
        "in_progress": (
            "claimed"
            if "claimed" in allowed
            else ("issued" if "issued" in allowed else ("in_review" if "in_review" in allowed else ("in_progress" if "in_progress" in allowed else None)))
        ),
        "submitted": "pending" if "submitted" not in allowed and "pending" in allowed else None,
        "in_review": "claimed" if "in_review" not in allowed and "claimed" in allowed else ("in_progress" if "in_progress" in allowed else None),
        "cancelled": "closed" if "cancelled" not in allowed and "closed" in allowed else None,
    }
    remapped = compatibility.get(normalized)
    return remapped if remapped in allowed else None


def _filter_for_schema(model_cls: type, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = set(getattr(model_cls, "model_fields", {}).keys())
    return {key: value for key, value in payload.items() if value is not None and key in allowed}


def _json_safe_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    try:
        return json.loads(exc.json())
    except Exception:
        return [{"msg": str(exc), "type": "value_error"}]


def _normalize_create_payload(payload: dict[str, Any]) -> BookingRequestCreate:
    normalized = _filter_for_schema(BookingRequestCreate, {
        "item_type": payload.get("item_type") or payload.get("equipment_type") or payload.get("equipment") or payload.get("item"),
        "item_label": payload.get("item_label") or payload.get("equipment_details") or payload.get("item_name") or payload.get("preferred_item"),
        "start_date": payload.get("start_date") or payload.get("preferred_date") or payload.get("date"),
        "end_date": payload.get("end_date") or payload.get("preferred_date") or payload.get("date"),
        "reason": payload.get("reason") or payload.get("comment") or payload.get("description") or payload.get("details") or "",
    })
    return BookingRequestCreate.model_validate(normalized)


def _normalize_update_payload(payload: dict[str, Any]) -> BookingRequestUpdate:
    update_fields = getattr(BookingRequestUpdate, "model_fields", {})
    owner_value = payload.get("specialist_owner") or payload.get("owner_role") or payload.get("owner")
    normalized_payload: dict[str, Any] = {
        "status": _normalize_status(payload.get("status") or payload.get("state")),
        "returned_at": payload.get("returned_at"),
        "item_label": payload.get("item_label") or payload.get("assigned_item") or payload.get("equipment_details"),
        "issued_at": payload.get("issued_at"),
    }
    if "specialist_owner" in update_fields:
        normalized_payload["specialist_owner"] = owner_value
    elif "owner_role" in update_fields:
        normalized_payload["owner_role"] = owner_value
    elif "owner" in update_fields:
        normalized_payload["owner"] = owner_value
    normalized = _filter_for_schema(BookingRequestUpdate, normalized_payload)
    return BookingRequestUpdate.model_validate(normalized)


def _record_identifier(record: BookingRequestRecord) -> Any:
    for field_name in ("bookingrequest_id", "request_id", "id"):
        value = getattr(record, field_name, None)
        if value not in (None, ""):
            return value
    return None


def _record_order_column():
    for field_name in ("requested_at", "created_at", "updated_at", "bookingrequest_id", "id"):
        column = getattr(BookingRequestRecord, field_name, None)
        if column is not None:
            return column
    raise AttributeError("BookingRequestRecord has no sortable column.")


def _list_records(session) -> list[BookingRequestRecord]:
    return session.query(BookingRequestRecord).order_by(_record_order_column().desc()).all()


def _record_payload_from_create(normalized: BookingRequestCreate, now: datetime) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name in ("item_type", "item_label", "start_date", "end_date", "reason"):
        if hasattr(BookingRequestRecord, field_name):
            payload[field_name] = getattr(normalized, field_name)
    if hasattr(BookingRequestRecord, "status"):
        payload["status"] = _default_status()
    if hasattr(BookingRequestRecord, "requested_by"):
        payload["requested_by"] = "client"
    if hasattr(BookingRequestRecord, "requested_at"):
        payload["requested_at"] = now
    if hasattr(BookingRequestRecord, "status_updated_at"):
        payload["status_updated_at"] = now
    if hasattr(BookingRequestRecord, "created_at"):
        payload["created_at"] = now
    if hasattr(BookingRequestRecord, "updated_at"):
        payload["updated_at"] = now
    return payload


def _to_schema(record: BookingRequestRecord, conflict: bool = False) -> BookingRequestRead:
    record_id = _record_identifier(record)
    read_fields = getattr(BookingRequestRead, "model_fields", {})
    owner_value = getattr(record, "specialist_owner", getattr(record, "owner", getattr(record, "owner_role", None)))
    payload_data: dict[str, Any] = {
        "id": record_id,
        "bookingrequest_id": record_id,
        "request_id": str(record_id) if record_id is not None else None,
        "item_type": record.item_type,
        "item_label": record.item_label,
        "start_date": record.start_date,
        "end_date": record.end_date,
        "reason": record.reason,
        "status": record.status,
        "requested_by": getattr(record, "requested_by", None),
        "owner_role": getattr(record, "owner_role", None),
        "owner_assigned_at": getattr(record, "owner_assigned_at", None),
        "issued_at": getattr(record, "issued_at", None),
        "status_notes": getattr(record, "status_notes", None),
        "conflict_warning": getattr(record, "conflict_warning", False),
        "returned_at": getattr(record, "returned_at", None),
        "requested_at": getattr(record, "requested_at", None),
        "status_updated_at": getattr(record, "status_updated_at", None),
        "created_at": getattr(record, "created_at", getattr(record, "requested_at", None)),
        "updated_at": record.updated_at,
        "conflict_note": getattr(record, "status_notes", None),
        "conflict": conflict,
    }
    if "specialist_owner" in read_fields:
        payload_data["specialist_owner"] = owner_value
    elif "owner" in read_fields:
        payload_data["owner"] = owner_value
    elif "owner_role" in read_fields and payload_data.get("owner_role") is None:
        payload_data["owner_role"] = owner_value
    payload = _filter_for_schema(BookingRequestRead, payload_data)
    return BookingRequestRead.model_validate(payload)


def _overlaps(record: BookingRequestRecord, other: BookingRequestRecord) -> bool:
    return max(record.start_date, other.start_date) <= min(record.end_date, other.end_date)


def _conflict_count(record: BookingRequestRecord, records: list[BookingRequestRecord]) -> int:
    count = 0
    record_id = _record_identifier(record)
    for other in records:
        if _record_identifier(other) == record_id:
            continue
        if other.item_type != record.item_type:
            continue
        if str(other.status).lower() in {"cancelled", "closed", "returned"}:
            continue
        if _overlaps(record, other):
            count += 1
    return count


def _coerce_request_id(item_id: str) -> Any:
    try:
        return int(str(item_id))
    except Exception:
        return item_id


@router.post("", response_model=BookingRequestRead)
def create_booking_request(payload: dict[str, Any]) -> BookingRequestRead:
    try:
        normalized = _normalize_create_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_json_safe_validation_errors(exc)) from exc
    now = datetime.now(timezone.utc)
    record = BookingRequestRecord(**_record_payload_from_create(normalized, now))
    with SessionLocal() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        records = _list_records(session)
        return _to_schema(record, _conflict_count(record, records) > 0)


@router.get("", response_model=BookingRequestListResponse)
def list_booking_requests() -> BookingRequestListResponse:
    with SessionLocal() as session:
        records = _list_records(session)
        items = [_to_schema(record, _conflict_count(record, records) > 0) for record in records]
    return BookingRequestListResponse(items=items)


@router.put("/{item_id}", response_model=BookingRequestRead)
def update_booking_request(item_id: str, payload: dict[str, Any]) -> BookingRequestRead:
    try:
        normalized = _normalize_update_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_json_safe_validation_errors(exc)) from exc
    with SessionLocal() as session:
        record = session.get(BookingRequestRecord, _coerce_request_id(item_id))
        if record is None:
            raise HTTPException(status_code=404, detail="Booking request not found.")
        if normalized.status is not None:
            record.status = normalized.status
            if hasattr(record, "status_updated_at"):
                record.status_updated_at = datetime.now(timezone.utc)
        if hasattr(normalized, "specialist_owner") and normalized.specialist_owner is not None and hasattr(record, "specialist_owner"):
            record.specialist_owner = normalized.specialist_owner
        if hasattr(normalized, "owner_role") and normalized.owner_role is not None and hasattr(record, "owner_role"):
            record.owner_role = normalized.owner_role
            if hasattr(record, "owner_assigned_at"):
                record.owner_assigned_at = datetime.now(timezone.utc)
        if hasattr(normalized, "item_label") and normalized.item_label is not None:
            record.item_label = normalized.item_label
        if hasattr(normalized, "issued_at") and normalized.issued_at is not None and hasattr(record, "issued_at"):
            record.issued_at = normalized.issued_at
        if hasattr(normalized, "returned_at") and normalized.returned_at is not None and hasattr(record, "returned_at"):
            record.returned_at = normalized.returned_at
        if hasattr(record, "updated_at"):
            record.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(record)
        records = _list_records(session)
        return _to_schema(record, _conflict_count(record, records) > 0)
"""
