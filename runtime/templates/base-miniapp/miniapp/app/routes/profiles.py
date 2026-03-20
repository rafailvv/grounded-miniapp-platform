from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.db import RoleProfileRecord, SessionLocal
from app.schemas import AppRole, RoleProfile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])
DEFAULT_PROFILES: dict[AppRole, dict[str, str | None]] = {
    "client": {"first_name": "Ivan", "last_name": "Ivanov", "email": "", "phone": "", "photo_url": None},
    "specialist": {"first_name": "Ivan", "last_name": "Ivanov", "email": "", "phone": "", "photo_url": None},
    "manager": {"first_name": "Ivan", "last_name": "Ivanov", "email": "", "phone": "", "photo_url": None},
}

def _to_schema(record: RoleProfileRecord) -> RoleProfile:
    return RoleProfile(
        first_name=record.first_name,
        last_name=record.last_name,
        email=record.email,
        phone=record.phone,
        photo_url=record.photo_url,
        updated_at=record.updated_at,
    )


def _get_or_create(role: AppRole) -> RoleProfileRecord:
    with SessionLocal() as session:
        record = session.get(RoleProfileRecord, role)
        if record is None:
            record = RoleProfileRecord(role=role, **DEFAULT_PROFILES[role])
            session.add(record)
            session.commit()
            session.refresh(record)
        return record


def load_role_profile(role: AppRole) -> RoleProfile:
    return _to_schema(_get_or_create(role))


def save_role_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    with SessionLocal() as session:
        record = session.get(RoleProfileRecord, role)
        if record is None:
            record = RoleProfileRecord(role=role, **DEFAULT_PROFILES[role])
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
