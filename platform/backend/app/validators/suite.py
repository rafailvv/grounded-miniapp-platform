from __future__ import annotations

from pathlib import Path

from app.models.app_ir import AppIRModel
from app.models.artifacts import AppIRValidatorResult, GroundedSpecValidatorResult
from app.models.grounded_spec import GroundedSpecModel
from app.validators.app_ir_validator import AppIRValidator
from app.validators.build_validator import BuildValidator
from app.validators.grounded_spec_validator import GroundedSpecValidator
from app.validators.platform_validator import PlatformValidator


class ValidationSuite:
    def __init__(self) -> None:
        self.grounded_spec_validator = GroundedSpecValidator()
        self.app_ir_validator = AppIRValidator()
        self.platform_validator = PlatformValidator()
        self.build_validator = BuildValidator()

    def validate_grounded_spec(self, spec: GroundedSpecModel) -> GroundedSpecValidatorResult:
        return self.grounded_spec_validator.validate(spec)

    def validate_app_ir(self, ir: AppIRModel) -> AppIRValidatorResult:
        result = self.app_ir_validator.validate(ir)
        issues = list(result.issues)
        issues.extend(self.platform_validator.validate(ir))
        blocking = any(issue.blocking for issue in issues)
        return AppIRValidatorResult(valid=not issues, blocking=blocking, issues=issues)

    def validate_build(self, workspace_path: Path):
        return self.build_validator.validate(workspace_path)
