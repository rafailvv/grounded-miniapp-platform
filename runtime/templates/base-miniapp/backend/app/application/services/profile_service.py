from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.domain.models.profile import RoleProfileRecord
from app.domain.schemas.profile import AppRole, RoleProfile
from app.infrastructure.database import Base, SessionLocal, engine
from app.infrastructure.repositories.profile_repository import ProfileRepository


DEFAULT_PROFILES: dict[AppRole, dict[str, str | None]] = {
    "client": {"first_name": "", "last_name": "", "email": "", "phone": "", "photo_url": None},
    "specialist": {"first_name": "", "last_name": "", "email": "", "phone": "", "photo_url": None},
    "manager": {"first_name": "", "last_name": "", "email": "", "phone": "", "photo_url": None},
}


class ProfileService:
    def __init__(self, repository: ProfileRepository) -> None:
        self._repository = repository

    def init_storage(self) -> None:
        Base.metadata.create_all(bind=engine)

        with SessionLocal() as session:
            existing_roles = set(session.scalars(select(RoleProfileRecord.role)).all())
            missing_roles = [role for role in DEFAULT_PROFILES if role not in existing_roles]
            if not missing_roles:
                return

            for role in missing_roles:
                self._repository.create(session, role, DEFAULT_PROFILES[role])
            session.commit()

    def get_role_profile(self, role: AppRole) -> RoleProfile:
        with SessionLocal() as session:
            record = self._repository.get_by_role(session, role)
            if record is None:
                record = self._repository.create(session, role, DEFAULT_PROFILES[role])
                session.commit()
                session.refresh(record)
            return self._to_schema(record)

    def save_role_profile(self, role: AppRole, profile: RoleProfile) -> RoleProfile:
        with SessionLocal() as session:
            record = self._repository.get_by_role(session, role)
            if record is None:
                record = self._repository.create(session, role, DEFAULT_PROFILES[role])

            record.first_name = profile.first_name
            record.last_name = profile.last_name
            record.email = profile.email
            record.phone = profile.phone
            record.photo_url = profile.photo_url
            record.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(record)
            return self._to_schema(record)

    @staticmethod
    def _to_schema(record: RoleProfileRecord) -> RoleProfile:
        return RoleProfile(
            first_name=record.first_name,
            last_name=record.last_name,
            email=record.email,
            phone=record.phone,
            photo_url=record.photo_url,
            updated_at=record.updated_at,
        )
