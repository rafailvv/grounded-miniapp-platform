from __future__ import annotations

from pathlib import Path

from app.models.artifacts import ValidationIssue


class BuildValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        required_files = [
            workspace_path / "backend" / "app" / "generated" / "app_ir.json",
            workspace_path / "backend" / "app" / "generated" / "runtime_manifest.json",
            workspace_path / "backend" / "app" / "generated" / "role_seed.json",
            workspace_path / "backend" / "app" / "generated" / "runtime_state.json",
            workspace_path / "frontend" / "src" / "shared" / "generated" / "runtime-manifest.json",
            workspace_path / "frontend" / "src" / "shared" / "generated" / "role-experience.json",
            workspace_path / "artifacts" / "grounded_spec.json",
        ]
        for file_path in required_files:
            if not file_path.exists():
                issues.append(
                    ValidationIssue(
                        code="build.missing_artifact",
                        message=f"Required compiled artifact is missing: {file_path.name}",
                        severity="high",
                        location=str(file_path.relative_to(workspace_path)),
                    )
                )
        return issues
