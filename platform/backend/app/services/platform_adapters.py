from __future__ import annotations

from dataclasses import dataclass

from app.models.common import TargetPlatform
from app.models.grounded_spec import EvidenceLink, PlatformConstraint, SecurityRequirement


@dataclass(frozen=True)
class BasePlatformAdapter:
    platform_name: str
    doc_dir_name: str

    def build_platform_constraints(self) -> list[PlatformConstraint]:
        raise NotImplementedError

    def build_security_requirements(self) -> list[SecurityRequirement]:
        raise NotImplementedError


class TelegramPlatformAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(platform_name=TargetPlatform.TELEGRAM.value, doc_dir_name="telegram")

    def build_platform_constraints(self) -> list[PlatformConstraint]:
        evidence = [EvidenceLink(doc_ref_id="platform-telegram-sdk", evidence_type="explicit")]
        return [
            PlatformConstraint(
                constraint_id="telegram_sdk",
                category="sdk",
                rule="Load telegram-web-app.js and interact through window.Telegram.WebApp.",
                severity="critical",
                evidence=evidence,
            ),
            PlatformConstraint(
                constraint_id="telegram_theme",
                category="theme",
                rule="Respect Telegram theme parameters and color scheme in preview and generated UI.",
                severity="high",
                evidence=evidence,
            ),
            PlatformConstraint(
                constraint_id="telegram_viewport",
                category="viewport",
                rule="Use Telegram viewport metrics and avoid browser-only layout assumptions.",
                severity="high",
                evidence=evidence,
            ),
        ]

    def build_security_requirements(self) -> list[SecurityRequirement]:
        evidence = [EvidenceLink(doc_ref_id="platform-telegram-initdata", evidence_type="explicit")]
        return [
            SecurityRequirement(
                security_req_id="telegram_init_data",
                category="telegram_initdata",
                rule="Validate initData on the server and never trust initDataUnsafe as an authenticated source.",
                severity="critical",
                evidence=evidence,
            )
        ]


class MaxPlatformAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(platform_name=TargetPlatform.MAX.value, doc_dir_name="max")

    def build_platform_constraints(self) -> list[PlatformConstraint]:
        evidence = [EvidenceLink(doc_ref_id="platform-max-sdk", evidence_type="explicit")]
        return [
            PlatformConstraint(
                constraint_id="max_sdk",
                category="sdk",
                rule="Load the MAX bridge and route host interactions through the platform bridge adapter.",
                severity="critical",
                evidence=evidence,
            ),
            PlatformConstraint(
                constraint_id="max_theme",
                category="theme",
                rule="Respect host-provided theme and viewport values in generated screens.",
                severity="high",
                evidence=evidence,
            ),
        ]

    def build_security_requirements(self) -> list[SecurityRequirement]:
        evidence = [EvidenceLink(doc_ref_id="platform-max-initdata", evidence_type="explicit")]
        return [
            SecurityRequirement(
                security_req_id="max_bridge_validation",
                category="auth",
                rule="Validate MAX bridge session payloads on the server before using them as trusted identity.",
                severity="critical",
                evidence=evidence,
            )
        ]


def get_platform_adapter(target_platform: TargetPlatform | str) -> BasePlatformAdapter:
    platform_value = target_platform.value if isinstance(target_platform, TargetPlatform) else target_platform
    if platform_value == TargetPlatform.MAX.value:
        return MaxPlatformAdapter()
    return TelegramPlatformAdapter()
