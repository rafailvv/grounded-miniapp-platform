from __future__ import annotations

from pathlib import Path

from app.modules.miniapp_validation.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator


class ValidationSuite:
    def __init__(self) -> None:
        self.build_validator = BuildValidator()
        self.connectivity_validator = ConnectivityValidator()

    def validate_build(self, workspace_path: Path):
        return self.build_validator.validate(workspace_path)

    def validate_connectivity(self, workspace_path: Path):
        return self.connectivity_validator.validate(workspace_path)
