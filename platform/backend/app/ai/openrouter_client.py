from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.ai.model_registry import MODEL_REGISTRY
from app.core.config import Settings


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def configuration(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "models": MODEL_REGISTRY,
            "routing": {
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
