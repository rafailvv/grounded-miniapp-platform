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
            "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, title TEXT, description TEXT, client_name TEXT, phone TEXT, preferred_time TEXT, comment TEXT, status TEXT, assigned_specialist TEXT, item_type TEXT, item_label TEXT, start_date TEXT, end_date TEXT, reason TEXT, specialist_notes TEXT, created_at TEXT, updated_at TEXT)"
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
