from __future__ import annotations

from app.application.services.profile_service import ProfileService
from app.infrastructure.repositories.profile_repository import ProfileRepository


def get_profile_service() -> ProfileService:
    return ProfileService(ProfileRepository())
