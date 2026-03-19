from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models.profile import RoleProfileRecord
from app.domain.schemas.profile import AppRole


class ProfileRepository:
    def get_by_role(self, session: Session, role: AppRole) -> RoleProfileRecord | None:
        return session.get(RoleProfileRecord, role)

    def create(self, session: Session, role: AppRole, payload: dict[str, str | None]) -> RoleProfileRecord:
        record = RoleProfileRecord(role=role, **payload)
        session.add(record)
        session.flush()
        return record
