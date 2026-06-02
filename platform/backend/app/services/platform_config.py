from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any

from app.models.platform_config import PlatformConfig


PLATFORM_CONFIG_SCHEMA_ID = "https://grounded.local/schemas/platform.config.schema.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_platform_config_path() -> Path:
    return repo_root() / "runtime" / "platform.config.json"


def platform_config_schema() -> dict[str, Any]:
    schema = PlatformConfig.model_json_schema(
        by_alias=True,
        ref_template="#/$defs/{model}",
    )
    schema["$id"] = PLATFORM_CONFIG_SCHEMA_ID
    schema["title"] = "Grounded Platform Product Configuration"
    return schema


def platform_config_path() -> Path:
    configured = str(os.getenv("PLATFORM_CONFIG_PATH") or "").strip()
    return Path(configured).expanduser() if configured else default_platform_config_path()


@lru_cache(maxsize=4)
def _load_platform_config(path_text: str) -> PlatformConfig:
    path = Path(path_text)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PlatformConfig.model_validate(payload)


def platform_config(*, reload: bool = False) -> PlatformConfig:
    if reload:
        _load_platform_config.cache_clear()
    return _load_platform_config(str(platform_config_path()))


def platform_config_manifest() -> dict[str, Any]:
    config = platform_config()
    path = platform_config_path()
    return {
        "schema": "grounded.platform_config_manifest.v1",
        "config_schema": config.schema_,
        "version": config.version,
        "source_path": str(path),
        "json_schema_id": PLATFORM_CONFIG_SCHEMA_ID,
        "schema_fixture": "platform/backend/app/schemas/platform.config.schema.json",
        "default_mode": config.sla.default_mode,
        "generation_modes": sorted(config.generation_modes),
        "checks": sorted(config.checks),
        "model_profiles": sorted(config.model_profiles),
        "skill_activation": config.skill_activation.model_dump(mode="json"),
        "browser_proof": config.browser_proof.model_dump(mode="json"),
    }
