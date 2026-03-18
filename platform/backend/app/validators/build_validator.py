from __future__ import annotations

import json
from pathlib import Path
import re

from app.models.artifacts import ValidationIssue


class BuildValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        required_files = [
            workspace_path / "backend" / "app" / "main.py",
            workspace_path / "backend" / "requirements.txt",
            workspace_path / "frontend" / "package.json",
            workspace_path / "frontend" / "src" / "main.tsx",
            workspace_path / "frontend" / "src" / "app" / "App.tsx",
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
            if not isinstance(routes_file_raw, str):
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_routes",
                        message=f"{role} is missing a routes file in the generated page graph.",
                        severity="high",
                        location="artifacts/generated_app_graph.json",
                    )
                )
                continue

            routes_file = workspace_path / routes_file_raw
            if not routes_file.exists():
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_routes",
                        message=f"{role} routes file was not generated.",
                        severity="high",
                        location=routes_file_raw,
                    )
                )
                continue

            routes_content = routes_file.read_text(encoding="utf-8")
            if "RoleCabinetHomePage" in routes_content:
                issues.append(
                    ValidationIssue(
                        code="build.placeholder_role_surface",
                        message=f"{role} routes still use RoleCabinetHomePage placeholder surfaces.",
                        severity="high",
                        location=routes_file_raw,
                    )
                )

            route_count = len(re.findall(r"<Route\b", routes_content))
            if route_count < max(3, len(pages)):
                issues.append(
                    ValidationIssue(
                        code="build.insufficient_routes",
                        message=f"{role} routes do not expose enough separate pages for a multi-flow app.",
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
                if page.get("route_path") == "/":
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
        frontend_src = workspace_path / "frontend" / "src"
        backend_routes = workspace_path / "backend" / "app" / "routes"
        nginx_conf = workspace_path / "docker" / "nginx.conf"
        nginx_content = nginx_conf.read_text(encoding="utf-8") if nginx_conf.exists() else ""

        for file_path in frontend_src.rglob("*"):
            if file_path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            content = file_path.read_text(encoding="utf-8")
            relative = str(file_path.relative_to(workspace_path))

            if re.search(r"""from\s+["']next/""", content):
                issues.append(
                    ValidationIssue(
                        code="build.unsupported_next_import",
                        message="Generated frontend imports Next.js modules inside a Vite template.",
                        severity="high",
                        location=relative,
                    )
                )

            if "fetch(" in content and "/api/" in content and "Authorization" not in content and "httpClient." not in content:
                issues.append(
                    ValidationIssue(
                        code="build.authless_api_fetch",
                        message="Generated frontend uses raw /api fetch instead of the template auth-aware HTTP client.",
                        severity="high",
                        location=relative,
                    )
                )

            if any(marker in content for marker in ('"/builds/', "'/builds/", '"/repo/', "'/repo/", '"/sandbox/', "'/sandbox/", '"/telemetry/', "'/telemetry/")):
                if not re.search(r"location\s+~\s+\^/\((builds|repo|sandbox|telemetry)\)/", nginx_content):
                    issues.append(
                        ValidationIssue(
                            code="build.unproxied_backend_route",
                            message="Generated frontend calls non-API backend routes that are not proxied by nginx.",
                            severity="high",
                            location=relative,
                        )
                    )

            issues.extend(self._detect_route_export_drift(workspace_path, relative, content))

        if frontend_src.exists():
            categories_usage = any(
                "/api/categories" in path.read_text(encoding="utf-8")
                for path in frontend_src.rglob("*")
                if path.suffix in {".ts", ".tsx", ".js", ".jsx"}
            )
            if categories_usage and not (backend_routes / "categories.py").exists():
                issues.append(
                    ValidationIssue(
                        code="build.missing_categories_route",
                        message="Frontend expects /api/categories but the backend route is missing.",
                        severity="high",
                        location="backend/app/routes/categories.py",
                    )
                )

        return issues

    def _detect_route_export_drift(self, workspace_path: Path, relative: str, content: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if not relative.endswith("Routes.tsx"):
            return issues
        import_pattern = re.compile(r"""import\s+([A-Za-z0-9_{}\s,]+)\s+from\s+["'](.+?)["'];?""")
        for match in import_pattern.finditer(content):
            imported_name = match.group(1).strip()
            module_ref = match.group(2).strip()
            if imported_name.startswith("{") or not module_ref.startswith("."):
                continue
            module_path = (workspace_path / relative).parent / f"{module_ref}.tsx"
            if not module_path.exists():
                module_path = (workspace_path / relative).parent / module_ref / "index.tsx"
            if not module_path.exists():
                continue
            module_content = module_path.read_text(encoding="utf-8")
            has_default_export = "export default" in module_content
            has_named_export = re.search(rf"""export\s+(const|function|class)\s+{re.escape(imported_name)}\b""", module_content) is not None
            if not has_default_export and has_named_export:
                issues.append(
                    ValidationIssue(
                        code="build.route_export_mismatch",
                        message=f"Routes file imports {imported_name} as default, but the page only has a named export.",
                        severity="high",
                        location=relative,
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
