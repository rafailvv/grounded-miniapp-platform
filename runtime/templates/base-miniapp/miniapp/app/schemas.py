from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AppRole = Literal["client", "specialist", "manager"]


class RoleProfileInput(StrictModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    photo_url: str | None = None


class RoleProfile(RoleProfileInput):
    updated_at: datetime | None = None
