from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)


class TargetPlatform(str, Enum):
    TELEGRAM = "telegram_mini_app"
    MAX = "max_mini_app"


class PreviewProfile(str, Enum):
    TELEGRAM_MOCK = "telegram_mock"
    MAX_MOCK = "max_mock"
    WEB_PREVIEW = "web_preview"


Severity = Literal["low", "medium", "high", "critical"]
Priority = Literal["must", "should", "could"]
Impact = Literal["low", "medium", "high"]

