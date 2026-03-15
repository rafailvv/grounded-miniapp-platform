from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AppRole = Literal["client", "specialist", "manager"]


class TelegramAuthRequest(StrictModel):
    init_data: str = ""
    user_id: int | None = None
    init_data_unsafe: dict = Field(default_factory=dict)


class AuthResponse(StrictModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_at: str
    role: AppRole
    user: dict[str, Any]


class RoleProfile(StrictModel):
    first_name: str
    last_name: str = ""
    email: str = ""
    phone: str = ""
    photo_url: str | None = None
    updated_at: datetime | None = None


class RoleDashboardMetric(StrictModel):
    metric_id: str
    label: str
    value: str


class RoleDashboardResponse(StrictModel):
    role: AppRole
    title: str
    description: str
    feature_text: str
    metrics: list[RoleDashboardMetric]
    primary_action_label: str
    secondary_action_label: str | None = None


class RuntimeActionRequest(StrictModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    item_id: str | None = None
    current_path: str | None = None
    screen_id: str | None = None


class RuntimeActionResponse(StrictModel):
    status: Literal["ok", "error"] = "ok"
    message: str | None = None
    next_path: str | None = None
    record_id: str | None = None
    refresh_manifest: bool = True


class RuntimeManifestResponse(StrictModel):
    role: AppRole
    entry_path: str
    routes: list[dict[str, Any]]
    navigation: list[dict[str, str]]
    screens: dict[str, dict[str, Any]]
    metrics: list[RoleDashboardMetric]
    profile: RoleProfile
    alerts: list[str] = Field(default_factory=list)
    activity: list[dict[str, Any]] = Field(default_factory=list)
    app: dict[str, Any]
