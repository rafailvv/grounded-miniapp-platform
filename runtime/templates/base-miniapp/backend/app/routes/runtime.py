from __future__ import annotations

from fastapi import APIRouter

from app.schemas import AppRole, RoleDashboardResponse, RuntimeActionRequest, RuntimeActionResponse, RuntimeManifestResponse
from app.store import execute_action, get_role_dashboard, get_role_manifest

router = APIRouter()


@router.get("/api/dashboard/{role}", response_model=RoleDashboardResponse)
def dashboard(role: AppRole) -> RoleDashboardResponse:
    return get_role_dashboard(role)


@router.get("/api/runtime/{role}/manifest", response_model=RuntimeManifestResponse)
def runtime_manifest(role: AppRole) -> RuntimeManifestResponse:
    return get_role_manifest(role)


@router.post("/api/runtime/{role}/actions/{action_id}", response_model=RuntimeActionResponse)
def runtime_action(role: AppRole, action_id: str, payload: RuntimeActionRequest) -> RuntimeActionResponse:
    return execute_action(role, action_id, payload=payload.payload, item_id=payload.item_id)
