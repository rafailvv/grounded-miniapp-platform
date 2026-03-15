from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.schemas import (
    AppRole,
    AuthResponse,
    RoleDashboardResponse,
    RoleProfile,
    RuntimeActionRequest,
    RuntimeActionResponse,
    RuntimeManifestResponse,
    TelegramAuthRequest,
)
from app.store import (
    ensure_state,
    execute_action,
    get_role_dashboard,
    get_role_manifest,
    get_role_profile,
    register_submission,
    save_role_profile,
)

ROLE_ORDER: tuple[AppRole, ...] = ("client", "specialist", "manager")

app = FastAPI(title="Base Mini-App Backend", version="2.0.0")


def normalize_role(raw_role: str | None) -> AppRole:
    normalized = (raw_role or "").strip().lower()
    if normalized == "expert":
        normalized = "specialist"
    if normalized not in ROLE_ORDER:
        return "client"
    return cast(AppRole, normalized)


def resolve_role(payload: TelegramAuthRequest) -> AppRole:
    query_role = None
    start_param = payload.init_data_unsafe.get("start_param")
    if isinstance(start_param, str) and "role=" in start_param:
        query_role = start_param.split("role=", 1)[-1].split("&", 1)[0]
    elif isinstance(start_param, str):
        query_role = start_param

    if query_role:
        return normalize_role(query_role)

    user = payload.init_data_unsafe.get("user")
    if isinstance(user, dict) and isinstance(user.get("role"), str):
        return normalize_role(user.get("role"))

    if payload.user_id is not None:
        return ROLE_ORDER[payload.user_id % len(ROLE_ORDER)]

    return "client"


@app.on_event("startup")
def startup() -> None:
    ensure_state()


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/telegram", response_model=AuthResponse)
def auth_telegram(payload: TelegramAuthRequest) -> AuthResponse:
    role = resolve_role(payload)
    user = payload.init_data_unsafe.get("user") if isinstance(payload.init_data_unsafe, dict) else None
    now = datetime.now(timezone.utc)
    access_suffix = str(int(now.timestamp()))
    return AuthResponse(
        access_token=f"runtime-access-{role}-{access_suffix}",
        refresh_token=f"runtime-refresh-{role}-{access_suffix}",
        token_type="Bearer",
        expires_at=(now + timedelta(hours=12)).isoformat(),
        role=role,
        user={
            "id": payload.user_id,
            "role": role,
            "username": user.get("username") if isinstance(user, dict) else None,
        },
    )


@app.get("/api/roles")
def get_roles() -> dict[str, list[dict[str, str]]]:
    return {
        "roles": [
            {"role": "client", "label": "Client"},
            {"role": "specialist", "label": "Specialist"},
            {"role": "manager", "label": "Manager"},
        ]
    }


@app.get("/api/dashboard/{role}", response_model=RoleDashboardResponse)
def dashboard(role: AppRole) -> RoleDashboardResponse:
    return get_role_dashboard(role)


@app.get("/api/runtime/{role}/manifest", response_model=RuntimeManifestResponse)
def runtime_manifest(role: AppRole) -> RuntimeManifestResponse:
    return get_role_manifest(role)


@app.post("/api/runtime/{role}/actions/{action_id}", response_model=RuntimeActionResponse)
def runtime_action(role: AppRole, action_id: str, payload: RuntimeActionRequest) -> RuntimeActionResponse:
    return execute_action(role, action_id, payload=payload.payload, item_id=payload.item_id)


@app.get("/api/profiles/{role}", response_model=RoleProfile)
def load_profile(role: AppRole) -> RoleProfile:
    return get_role_profile(role)


@app.put("/api/profiles/{role}", response_model=RoleProfile)
def update_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:
    return save_role_profile(role, profile)


@app.post("/api/submissions")
def create_submission(payload: dict) -> dict:
    return register_submission(payload)


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})
