from __future__ import annotations

from pathlib import Path
import re

from app.models.artifacts import ValidationIssue


class AgentStaticValidation:
    @staticmethod
    def _normalized_imported_schema_names(content: str) -> set[str]:
        imported_names: set[str] = set()
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+\((.*?)\)", content, flags=re.DOTALL):
            parts = [part.strip() for part in match.group(1).replace("\n", " ").split(",") if part.strip()]
            imported_names.update(part.split(" as ", 1)[0].strip() for part in parts)
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+([A-Za-z0-9_, ]+)", content):
            parts = [part.strip() for part in match.group(1).split(",") if part.strip()]
            imported_names.update(part.split(" as ", 1)[0].strip() for part in parts)
        return imported_names

    @classmethod
    def route_schema_issues(cls, draft_root: Path) -> list[ValidationIssue]:
        routes_dir = draft_root / "miniapp/app/routes"
        schemas_path = draft_root / "miniapp/app/schemas.py"
        if not routes_dir.exists() or not schemas_path.exists():
            return []
        schemas_content = schemas_path.read_text(encoding="utf-8")
        issues: list[ValidationIssue] = []
        for route_file in routes_dir.glob("*.py"):
            content = route_file.read_text(encoding="utf-8")
            imported_names = cls._normalized_imported_schema_names(content)
            missing = sorted(name for name in imported_names if f"class {name}" not in schemas_content and f"{name} =" not in schemas_content)
            if missing:
                issues.append(
                    ValidationIssue(
                        code="agent_static.route_schema_contract",
                        message=f"{route_file.name} imports schemas that do not exist in schemas.py: {', '.join(missing)}.",
                        severity="high",
                        location="miniapp/app/schemas.py",
                        blocking=True,
                    )
                )
        return issues
