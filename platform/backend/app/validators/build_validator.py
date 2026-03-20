from __future__ import annotations

import json
from pathlib import Path
import re

from app.models.artifacts import ValidationIssue


class BuildValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        required_files = [
            workspace_path / "miniapp" / "app" / "main.py",
            workspace_path / "miniapp" / "requirements.txt",
            workspace_path / "miniapp" / "app" / "static" / "client" / "index.html",
            workspace_path / "miniapp" / "app" / "static" / "client" / "profile.html",
            workspace_path / "miniapp" / "app" / "static" / "specialist" / "index.html",
            workspace_path / "miniapp" / "app" / "static" / "manager" / "index.html",
            workspace_path / "docker" / "docker-compose.yml",
            workspace_path / "artifacts" / "grounded_spec.json",
        ]
        for file_path in required_files:
            if not file_path.exists():
                issues.append(
                    ValidationIssue(
                        code="build.missing_entrypoint",
                        message=f"Required scaffold or entrypoint is missing: {file_path.name}",
                        severity="high",
                        location=str(file_path.relative_to(workspace_path)),
                    )
                )
        issues.extend(self._validate_generated_app_shape(workspace_path))
        issues.extend(self._validate_contract_drift(workspace_path))
        return issues

    def _validate_generated_app_shape(self, workspace_path: Path) -> list[ValidationIssue]:
        graph_path = workspace_path / "artifacts" / "generated_app_graph.json"
        if not graph_path.exists():
            return []

        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return [
                ValidationIssue(
                    code="build.invalid_generated_app_graph",
                    message="generated_app_graph.json is invalid JSON.",
                    severity="high",
                    location="artifacts/generated_app_graph.json",
                )
            ]

        if graph.get("scope_mode") == "minimal_patch":
            return []

        if graph.get("flow_mode") != "multi_page":
            return []

        issues: list[ValidationIssue] = []
        roles = graph.get("roles") or {}
        normalized_root_pages: list[str] = []

        for role, role_payload in roles.items():
            pages = role_payload.get("pages") or []
            routes_file_raw = role_payload.get("routes_file")
            if isinstance(routes_file_raw, str) and routes_file_raw:
                routes_file = workspace_path / routes_file_raw
                if not routes_file.exists():
                    issues.append(
                        ValidationIssue(
                            code="build.missing_role_routes",
                            message=f"{role} entry file was not generated.",
                            severity="high",
                            location=routes_file_raw,
                        )
                    )
                else:
                    routes_content = routes_file.read_text(encoding="utf-8")
                    if "RoleCabinetHomePage" in routes_content:
                        issues.append(
                            ValidationIssue(
                                code="build.placeholder_role_surface",
                                message=f"{role} entry file still uses placeholder surfaces.",
                                severity="high",
                                location=routes_file_raw,
                            )
                        )

            if len(pages) < 2:
                issues.append(
                    ValidationIssue(
                        code="build.insufficient_pages",
                        message=f"{role} did not receive enough generated pages.",
                        severity="high",
                        location="artifacts/generated_app_graph.json",
                    )
                )

            for page in pages:
                file_path_raw = page.get("file_path")
                if not isinstance(file_path_raw, str):
                    continue
                file_path = workspace_path / file_path_raw
                if not file_path.exists():
                    issues.append(
                        ValidationIssue(
                            code="build.missing_generated_page",
                            message=f"Generated page is missing: {Path(file_path_raw).name}",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                    continue

                content = file_path.read_text(encoding="utf-8")
                if "RoleCabinetHomePage" in content:
                    issues.append(
                        ValidationIssue(
                            code="build.placeholder_page",
                            message=f"{Path(file_path_raw).name} still renders a homepage placeholder wrapper.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if page.get("route_path") in {"/", f"/{role}"}:
                    normalized_root_pages.append(self._normalize_role_page(content))

        if len(normalized_root_pages) > 1 and len(set(normalized_root_pages)) == 1:
            issues.append(
                ValidationIssue(
                    code="build.identical_role_pages",
                    message="Generated role root pages are effectively identical apart from role labels.",
                    severity="high",
                    location="artifacts/generated_app_graph.json",
                )
            )
        return issues

    def _validate_contract_drift(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        static_root = workspace_path / "miniapp" / "app" / "static"
        backend_routes = workspace_path / "miniapp" / "app" / "routes"
        legacy_dirs = [
            workspace_path / "frontend",
            workspace_path / "miniapp" / "app" / "api",
            workspace_path / "miniapp" / "app" / "application",
            workspace_path / "miniapp" / "app" / "domain",
            workspace_path / "miniapp" / "app" / "infrastructure",
        ]

        for legacy_dir in legacy_dirs:
            if legacy_dir.exists() and any(item.is_file() for item in legacy_dir.rglob("*")):
                issues.append(
                    ValidationIssue(
                        code="build.legacy_architecture_root",
                        message=f"Legacy architecture root is still present: {legacy_dir.relative_to(workspace_path)}",
                        severity="high",
                        location=str(legacy_dir.relative_to(workspace_path)),
                    )
                )

        for file_path in static_root.rglob("*"):
            if file_path.suffix not in {".html", ".css", ".js"}:
                continue
            content = file_path.read_text(encoding="utf-8")
            relative = str(file_path.relative_to(workspace_path))

            if re.search(r"""from\s+["']next/""", content) or "react-router-dom" in content:
                issues.append(
                    ValidationIssue(
                        code="build.unsupported_static_dependency",
                        message="Generated static UI still imports framework-specific frontend modules.",
                        severity="high",
                        location=relative,
                    )
                )

        if static_root.exists():
            categories_usage = any(
                "/api/categories" in path.read_text(encoding="utf-8")
                for path in static_root.rglob("*")
                if path.suffix in {".html", ".js"}
            )
            if categories_usage and not (backend_routes / "categories.py").exists():
                issues.append(
                    ValidationIssue(
                        code="build.missing_categories_route",
                        message="Frontend expects /api/categories but the miniapp route is missing.",
                        severity="high",
                        location="miniapp/app/routes/categories.py",
                    )
                )

        return issues

    @staticmethod
    def _normalize_role_page(content: str) -> str:
        lowered = content.lower()
        for marker in ("client", "specialist", "manager", "shopper", "operations", "management"):
            lowered = lowered.replace(marker, "role")
        lowered = re.sub(r"\s+", "", lowered)
        return lowered
