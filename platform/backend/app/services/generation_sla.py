from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.models.common import GenerationMode
from app.models.platform_config import GenerationModeConfig
from app.services.platform_config import platform_config


@dataclass(frozen=True)
class GenerationSlaProfile:
    mode: str
    label: str
    objective: str
    required_checks: tuple[str, ...]
    optional_checks: tuple[str, ...] = field(default_factory=tuple)
    proof_requirements: tuple[str, ...] = field(default_factory=tuple)
    final_gate: tuple[str, ...] = field(default_factory=tuple)
    context_policy: str = "standard"
    worker_policy: str = "serial"
    max_repair_attempts: int = 1
    audit_level: str = "light"
    output_style: str = "concise"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("required_checks", "optional_checks", "proof_requirements", "final_gate"):
            payload[key] = list(payload[key])
        return payload


def _profile_from_config(mode: str, config: GenerationModeConfig) -> GenerationSlaProfile:
    return GenerationSlaProfile(
        mode=mode,
        label=config.label,
        objective=config.objective,
        required_checks=tuple(config.required_checks),
        optional_checks=tuple(config.optional_checks),
        proof_requirements=tuple(config.proof_requirements),
        final_gate=tuple(config.final_gate),
        context_policy=config.context_policy,
        worker_policy=config.worker_policy,
        max_repair_attempts=config.max_repair_attempts,
        audit_level=config.audit_level,
        output_style=config.output_style,
    )


def sla_profiles() -> dict[GenerationMode, GenerationSlaProfile]:
    config = platform_config()
    profiles: dict[GenerationMode, GenerationSlaProfile] = {}
    for mode in GenerationMode:
        mode_config = config.generation_modes.get(mode.value)
        if mode_config is not None and mode_config.enabled:
            profiles[mode] = _profile_from_config(mode.value, mode_config)
    return profiles


def normalize_generation_mode(value: GenerationMode | str | None) -> GenerationMode:
    if isinstance(value, GenerationMode):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return GenerationMode(text)
        except ValueError:
            pass
    return GenerationMode.BALANCED


class GenerationSla:
    @staticmethod
    def profile(mode: GenerationMode | str | None) -> GenerationSlaProfile:
        profiles = sla_profiles()
        normalized = normalize_generation_mode(mode)
        return profiles.get(normalized) or profiles[normalize_generation_mode(platform_config().sla.default_mode)]

    @staticmethod
    def required_checks(mode: GenerationMode | str | None) -> tuple[str, ...]:
        return GenerationSla.profile(mode).required_checks

    @staticmethod
    def requires_full_audit(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode).value in set(platform_config().sla.full_audit_modes)

    @staticmethod
    def requires_visual_snapshots(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode).value in set(platform_config().sla.visual_snapshot_modes)

    @staticmethod
    def treats_as_quality(mode: GenerationMode | str | None) -> bool:
        return normalize_generation_mode(mode).value in set(platform_config().sla.quality_like_modes)

    @staticmethod
    def manifest() -> dict[str, Any]:
        config = platform_config()
        profiles = sla_profiles()
        return {
            "schema": "grounded.generation_sla.v1",
            "default_mode": config.sla.default_mode,
            "modes": [profiles[mode].to_dict() for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY, GenerationMode.PRODUCTION, GenerationMode.BASIC) if mode in profiles],
            "second_queue": list(config.sla.second_queue),
            "compatibility": dict(config.sla.compatibility),
            "platform_config_ref": "runtime/platform.config.json",
            "platform_config_schema": "platform/backend/app/schemas/platform.config.schema.json",
        }
