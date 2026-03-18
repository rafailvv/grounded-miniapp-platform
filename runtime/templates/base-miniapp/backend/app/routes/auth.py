from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

from fastapi import APIRouter

from app.schemas import AppRole, AuthResponse, TelegramAuthRequest

router = APIRouter()

ROLE_ORDER: tuple[AppRole, ...] = ("client", "specialist", "manager")


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


@router.post("/api/auth/telegram", response_model=AuthResponse)
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


@router.get("/api/roles")
def get_roles() -> dict[str, list[dict[str, str]]]:
    return {
        "roles": [
            {"role": "client", "label": "Client"},
            {"role": "specialist", "label": "Specialist"},
            {"role": "manager", "label": "Manager"},
        ]
    }
