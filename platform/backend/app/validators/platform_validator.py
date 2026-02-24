from __future__ import annotations

from app.models.app_ir import AppIRModel
from app.models.artifacts import ValidationIssue
from app.models.common import TargetPlatform


class PlatformValidator:
    def validate(self, ir: AppIRModel) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        platform = TargetPlatform(ir.platform)
        trusted_sources = set(ir.security.trusted_sources)
        if platform == TargetPlatform.TELEGRAM and "validated_init_data" not in trusted_sources:
            issues.append(
                ValidationIssue(
                    code="platform.telegram.validated_init_data",
                    message="Telegram runtime must trust only server-validated init data.",
                    severity="critical",
                    location="security.trusted_sources",
                )
            )
        if platform == TargetPlatform.MAX and "validated_host_session" not in trusted_sources:
            issues.append(
                ValidationIssue(
                    code="platform.max.validated_session",
                    message="MAX runtime must trust only server-validated host session payloads.",
                    severity="critical",
                    location="security.trusted_sources",
                )
            )
        return issues
