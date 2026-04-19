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

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from app.db import BookingRequestRecord, SessionLocal
from app.schemas import BookingRequestCreate, BookingRequestListResponse, BookingRequestRead, BookingRequestUpdate

router = APIRouter(prefix="/api/bookingrequests", tags=["bookingrequests"])


def _normalize_create_payload(payload: dict[str, Any]) -> BookingRequestCreate:
    normalized = {
        "item_type": payload.get("item_type") or payload.get("equipment_type") or payload.get("equipment") or payload.get("item"),
        "item_label": payload.get("item_label") or payload.get("equipment_details") or payload.get("item_name") or payload.get("preferred_item"),
        "start_date": payload.get("start_date") or payload.get("preferred_date") or payload.get("date"),
        "end_date": payload.get("end_date") or payload.get("preferred_date") or payload.get("date"),
        "reason": payload.get("reason") or payload.get("comment") or payload.get("description") or payload.get("details") or "",
    }
    return BookingRequestCreate.model_validate(normalized)


def _normalize_update_payload(payload: dict[str, Any]) -> BookingRequestUpdate:
    normalized_status = payload.get("status") or payload.get("state")
    if normalized_status == "canceled":
        normalized_status = "cancelled"
    normalized = {
        "status": normalized_status,
        "owner_role": payload.get("owner_role") or payload.get("owner") or payload.get("owner_specialist_id") or payload.get("owner_specialist") or payload.get("specialist_id"),
        "item_label": payload.get("item_label") or payload.get("assigned_item") or payload.get("equipment_details"),
        "issued_at": payload.get("issued_at"),
        "returned_at": payload.get("returned_at"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "reason": payload.get("reason") or payload.get("comment") or payload.get("description") or payload.get("details"),
    }
    filtered = {key: value for key, value in normalized.items() if value is not None}
    return BookingRequestUpdate.model_validate(filtered)


def _to_schema(record: BookingRequestRecord, conflict: bool = False) -> BookingRequestRead:
    return BookingRequestRead(
        bookingrequest_id=record.id,
        item_type=record.item_type,
        item_label=record.item_label,
        start_date=record.start_date,
        end_date=record.end_date,
        reason=record.reason,
        status=record.status,
        owner_role=record.owner_role,
        issued_at=record.issued_at,
        returned_at=record.returned_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        conflict=conflict,
    )


def _overlaps(record: BookingRequestRecord, other: BookingRequestRecord) -> bool:
    return max(record.start_date, other.start_date) <= min(record.end_date, other.end_date)


def _conflict_count(record: BookingRequestRecord, records: list[BookingRequestRecord]) -> int:
    count = 0
    for other in records:
        if other.id == record.id:
            continue
        if other.item_type != record.item_type:
            continue
        if other.status in {"cancelled", "closed", "returned"}:
            continue
        if _overlaps(record, other):
            count += 1
    return count


@router.post("", response_model=BookingRequestRead)
def create_booking_request(payload: dict[str, Any]) -> BookingRequestRead:
    try:
        normalized = _normalize_create_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    now = datetime.now(timezone.utc)
    record = BookingRequestRecord(
        id=str(uuid4()),
        item_type=normalized.item_type,
        item_label=normalized.item_label,
        start_date=normalized.start_date,
        end_date=normalized.end_date,
        reason=normalized.reason,
        status="submitted",
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as session:
        session.add(record)
        session.commit()
        session.refresh(record)
        records = session.query(BookingRequestRecord).order_by(BookingRequestRecord.created_at.desc()).all()
        return _to_schema(record, _conflict_count(record, records) > 0)


@router.get("", response_model=BookingRequestListResponse)
def list_booking_requests() -> BookingRequestListResponse:
    with SessionLocal() as session:
        records = session.query(BookingRequestRecord).order_by(BookingRequestRecord.created_at.desc()).all()
        items = [_to_schema(record, _conflict_count(record, records) > 0) for record in records]
    return BookingRequestListResponse(items=items)


@router.put("/{item_id}", response_model=BookingRequestRead)
def update_booking_request(item_id: str, payload: dict[str, Any]) -> BookingRequestRead:
    try:
        normalized = _normalize_update_payload(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    with SessionLocal() as session:
        record = session.get(BookingRequestRecord, item_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Booking request not found.")
        if normalized.status is not None:
            record.status = normalized.status
        if normalized.owner_role is not None:
            record.owner_role = normalized.owner_role
        if normalized.item_label is not None:
            record.item_label = normalized.item_label
        if normalized.issued_at is not None:
            record.issued_at = normalized.issued_at
        if normalized.returned_at is not None:
            record.returned_at = normalized.returned_at
        if normalized.start_date is not None:
            record.start_date = normalized.start_date
        if normalized.end_date is not None:
            record.end_date = normalized.end_date
        if normalized.reason is not None:
            record.reason = normalized.reason
        record.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(record)
        records = session.query(BookingRequestRecord).order_by(BookingRequestRecord.created_at.desc()).all()
        return _to_schema(record, _conflict_count(record, records) > 0)
"""
