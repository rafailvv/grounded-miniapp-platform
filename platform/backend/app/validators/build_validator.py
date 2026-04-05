from __future__ import annotations

import json
from pathlib import Path
import re

from app.models.grounded_spec import GroundedSpecModel
from app.models.artifacts import ValidationIssue


class BuildValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        required_files = [
            workspace_path / "miniapp" / "app" / "main.py",
            workspace_path / "miniapp" / "requirements.txt",
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
        route_manifest_path = workspace_path / "miniapp" / "app" / "generated" / "route_manifest.json"
        runtime_manifest_path = workspace_path / "miniapp" / "app" / "generated" / "runtime_manifest.json"
        route_manifest = self._read_json(route_manifest_path)
        grounded_spec = self._read_grounded_spec(workspace_path)
        execution_class = self._execution_class_for_spec(grounded_spec)
        if not graph_path.exists():
            if not route_manifest_path.exists():
                return [
                    ValidationIssue(
                        code="build.missing_entrypoint",
                        message="Required scaffold or entrypoint is missing: route_manifest.json",
                        severity="high",
                        location="miniapp/app/generated/route_manifest.json",
                    )
                ]
            return self._validate_route_manifest_only(workspace_path, route_manifest)

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

        issues: list[ValidationIssue] = []
        if graph.get("scope_mode") == "minimal_patch":
            return issues
        roles = graph.get("roles") or {}
        backend_targets = [str(path) for path in (graph.get("backend_targets") or []) if isinstance(path, str)]
        normalized_root_pages: list[str] = []
        if graph.get("flow_mode") == "multi_page" and not route_manifest_path.exists():
            issues.append(
                ValidationIssue(
                    code="build.missing_route_manifest",
                    message="Multi-page apps must persist the route manifest.",
                    severity="high",
                    location="miniapp/app/generated/route_manifest.json",
                )
            )
        if execution_class != "shell_app" and not runtime_manifest_path.exists():
            issues.append(
                ValidationIssue(
                    code="build.missing_runtime_manifest",
                    message="Workflow apps must persist the runtime manifest.",
                    severity="high",
                    location="miniapp/app/generated/runtime_manifest.json",
                )
            )
        if execution_class != "shell_app" and backend_targets:
            missing_backend_targets = [path for path in backend_targets if not (workspace_path / path).exists()]
            if missing_backend_targets:
                issues.append(
                    ValidationIssue(
                        code="build.missing_backend_surface",
                        message=f"Workflow backend surface is missing: {Path(missing_backend_targets[0]).name}",
                        severity="high",
                        location=missing_backend_targets[0],
                    )
                )

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

            root_pages = [
                page
                for page in pages
                if isinstance(page, dict) and (page.get("route_path") in {"/", f"/{role}"} or page.get("is_entry"))
            ]
            profile_pages = [
                page
                for page in pages
                if isinstance(page, dict)
                and (
                    str(page.get("route_path") or "").rstrip("/") == "/profile"
                    or str(page.get("page_kind") or "").lower() == "profile"
                )
            ]
            if not root_pages:
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_entry_page",
                        message=f"{role} is missing a usable root page.",
                        severity="high",
                        location="artifacts/generated_app_graph.json",
                    )
                )
            if not profile_pages:
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_profile_page",
                        message=f"{role} is missing the required profile page.",
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
        if execution_class != "shell_app":
            default_only = []
            for role, role_payload in roles.items():
                pages = role_payload.get("pages") or []
                route_paths = {str(page.get("route_path") or "") for page in pages if isinstance(page, dict)}
                if route_paths and route_paths.issubset({"/", "/profile", f"/{role}", f"/{role}/profile"}):
                    default_only.append(role)
            if default_only:
                issues.append(
                    ValidationIssue(
                        code="build.workflow_shell_collapse",
                        message=f"Workflow app collapsed back to root/profile shell for: {', '.join(default_only)}.",
                        severity="high",
                        location="artifacts/generated_app_graph.json",
                    )
                )
        return issues

    def _validate_route_manifest_only(self, workspace_path: Path, route_manifest: dict | list | None) -> list[ValidationIssue]:
        if not isinstance(route_manifest, dict):
            return []
        issues: list[ValidationIssue] = []
        normalized_root_pages: list[str] = []
        for role, role_payload in (route_manifest.get("roles") or {}).items():
            if not isinstance(role_payload, dict):
                continue
            pages = role_payload.get("pages") or []
            root_pages = [
                page
                for page in pages
                if isinstance(page, dict) and (page.get("route_path") in {"/", f"/{role}"} or page.get("is_entry"))
            ]
            profile_pages = [
                page
                for page in pages
                if isinstance(page, dict)
                and (
                    str(page.get("route_path") or "").rstrip("/") == "/profile"
                    or str(page.get("page_kind") or "").lower() == "profile"
                )
            ]
            if not root_pages:
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_entry_page",
                        message=f"{role} is missing a usable root page.",
                        severity="high",
                        location="miniapp/app/generated/route_manifest.json",
                    )
                )
            if not profile_pages:
                issues.append(
                    ValidationIssue(
                        code="build.missing_role_profile_page",
                        message=f"{role} is missing the required profile page.",
                        severity="high",
                        location="miniapp/app/generated/route_manifest.json",
                    )
                )
            for page in pages:
                if not isinstance(page, dict):
                    continue
                file_path_raw = str(page.get("file_path") or "")
                if not file_path_raw:
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
                if str(page.get("route_path") or "/") in {"/", f"/{role}"}:
                    normalized_root_pages.append(self._normalize_role_page(content))
        if len(normalized_root_pages) > 1 and len(set(normalized_root_pages)) == 1:
            issues.append(
                ValidationIssue(
                    code="build.identical_role_pages",
                    message="Generated role root pages are effectively identical apart from role labels.",
                    severity="high",
                    location="miniapp/app/generated/route_manifest.json",
                )
            )
        return issues

    @staticmethod
    def _read_json(path: Path) -> dict | list | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _read_grounded_spec(workspace_path: Path) -> GroundedSpecModel | None:
        spec_path = workspace_path / "artifacts" / "grounded_spec.json"
        if not spec_path.exists():
            return None
        try:
            return GroundedSpecModel.model_validate(json.loads(spec_path.read_text(encoding="utf-8")))
        except Exception:
            return None

    @staticmethod
    def _execution_class_for_spec(spec: GroundedSpecModel | None) -> str:
        if spec is None:
            return "shell_app"
        entity_count = len(spec.domain_entities)
        flow_count = len(spec.user_flows)
        api_count = len(spec.api_requirements)
        persistence_count = len(spec.persistence_requirements)
        if persistence_count >= 3 or (entity_count >= 4 and api_count >= 4):
            return "data_crud_app"
        if flow_count > 1 and api_count >= 3:
            return "workflow_dashboard_app"
        if flow_count > 1 or entity_count > 1 or api_count > 0 or persistence_count > 0:
            return "entity_workflow_app"
        return "shell_app"

    def _validate_contract_drift(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        static_root = workspace_path / "miniapp" / "app" / "static"
        frontend_root = workspace_path / "frontend"
        nginx_conf = workspace_path / "docker" / "nginx.conf"
        nginx_content = nginx_conf.read_text(encoding="utf-8") if nginx_conf.exists() else ""
        frontend_only_proxy = "proxy_pass http://frontend" in nginx_content and "location /api" not in nginx_content
        legacy_dirs = [
            frontend_root,
            workspace_path / "miniapp" / "app" / "api",
            workspace_path / "miniapp" / "app" / "application",
            workspace_path / "miniapp" / "app" / "domain",
            workspace_path / "miniapp" / "app" / "infrastructure",
        ]

        for legacy_dir in legacy_dirs:
            if legacy_dir == frontend_root and (frontend_root / ".grounded-compat-scaffold").exists():
                continue
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

        for file_path in workspace_path.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix not in {".js", ".jsx", ".ts", ".tsx", ".html"}:
                continue
            relative = str(file_path.relative_to(workspace_path))
            if relative.startswith("frontend/") and (frontend_root / ".grounded-compat-scaffold").exists():
                continue
            content = file_path.read_text(encoding="utf-8")
            if re.search(r"""from\s+["']next/""", content):
                issues.append(
                    ValidationIssue(
                        code="build.unsupported_next_import",
                        message="Generated app still imports Next.js modules, which are not supported in the miniapp runtime.",
                        severity="high",
                        location=relative,
                    )
                )
            for match in re.finditer(r"""import\s+([A-Z][A-Za-z0-9_]*)\s+from\s+['"](\.[^'"]+)['"]""", content):
                import_name = match.group(1)
                import_target = match.group(2)
                target_path = (file_path.parent / f"{import_target}.tsx").resolve()
                if not target_path.exists():
                    target_path = (file_path.parent / f"{import_target}.ts").resolve()
                if not target_path.exists():
                    continue
                target_content = target_path.read_text(encoding="utf-8")
                if "export default" not in target_content:
                    issues.append(
                        ValidationIssue(
                            code="build.route_export_mismatch",
                            message=f"{import_name} is imported as a default export but the target file does not export default.",
                            severity="high",
                            location=relative,
                        )
                    )
            if "fetch('/api/" in content or 'fetch("/api/' in content or "fetch('/builds/" in content or 'fetch("/builds/' in content:
                issues.append(
                    ValidationIssue(
                        code="build.authless_api_fetch",
                        message="Generated frontend still performs direct authless fetch calls to platform APIs.",
                        severity="high",
                        location=relative,
                    )
                )
                if frontend_only_proxy:
                    issues.append(
                        ValidationIssue(
                            code="build.unproxied_backend_route",
                            message="Frontend calls backend routes that are not proxied through the runtime gateway.",
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
