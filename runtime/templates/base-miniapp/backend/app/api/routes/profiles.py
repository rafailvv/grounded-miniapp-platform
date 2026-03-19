from __future__ import annotations

from fastapi import APIRouter, Depends

from app.domain.schemas.profile import AppRole, RoleProfile
from app.api.dependencies import get_profile_service
from app.application.services.profile_service import ProfileService

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("/{role}", response_model=RoleProfile)
def load_profile(role: AppRole, profile_service: ProfileService = Depends(get_profile_service)) -> RoleProfile:
    return profile_service.get_role_profile(role)


@router.put("/{role}", response_model=RoleProfile)
def update_profile(
    role: AppRole,
    profile: RoleProfile,
    profile_service: ProfileService = Depends(get_profile_service),
) -> RoleProfile:
    return profile_service.save_role_profile(role, profile)
