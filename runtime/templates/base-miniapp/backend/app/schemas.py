from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AppRole = Literal["client", "specialist", "manager"]


class TelegramAuthRequest(StrictModel):
    init_data: str = ""
    user_id: int | None = None
    init_data_unsafe: dict = {}


class AuthTokens(StrictModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_at: str


class AuthResponse(StrictModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_at: str
    role: AppRole
    user: dict


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

