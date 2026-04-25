from __future__ import annotations

from dataclasses import dataclass

from app.models.common import TargetPlatform


@dataclass(frozen=True)
class BasePlatformAdapter:
    platform_name: str
    doc_dir_name: str


class TelegramPlatformAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(platform_name=TargetPlatform.TELEGRAM.value, doc_dir_name="telegram")


class MaxPlatformAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(platform_name=TargetPlatform.MAX.value, doc_dir_name="max")


def get_platform_adapter(target_platform: TargetPlatform | str) -> BasePlatformAdapter:
    platform_value = target_platform.value if isinstance(target_platform, TargetPlatform) else target_platform
    if platform_value == TargetPlatform.MAX.value:
        return MaxPlatformAdapter()
    return TelegramPlatformAdapter()
