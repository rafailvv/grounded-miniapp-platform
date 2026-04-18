from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationRuntimeOwner:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def __getattr__(self, name: str) -> Any:
        return getattr(self.service, name)
