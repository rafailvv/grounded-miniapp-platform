from __future__ import annotations

from fastapi import APIRouter

from app.schemas import AppRole, RoleProfile
from app.store import get_role_profile, save_role_profile

router = APIRouter()


@router.get("/api/profiles/{role}", response_model=RoleProfile)
def load_profile(role: AppRole) -> RoleProfile:
    return get_role_profile(role)


@router.put("/api/profiles/{role}", response_model=RoleProfile)
def update_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    return save_role_profile(role, profile)
