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
        issues.extend(self._validate_route_module_import_safety(workspace_path))
        issues.extend(self._validate_persistent_storage_contract(workspace_path))
        return issues

    @staticmethod
    def _is_role_root_page(role: str, page: dict) -> bool:
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip()
        page_kind = str(page.get("page_kind") or "").strip().lower()
        return bool(
            page.get("is_entry")
            or route_path in {"/", f"/{role}"}
            or file_path == f"miniapp/app/static/{role}/index.html"
            or page_kind in {"home", "dashboard", "landing"}
        )

    @staticmethod
    def _has_visible_loading_surface(content: str) -> bool:
        lowered = str(content or "").lower()
        scrubbed = re.sub(
            r"<(?P<tag>div|section|article|aside|p|span)[^>]*(?:hidden|aria-hidden=['\"]true['\"])[^>]*>.*?</(?P=tag)>",
            "",
            lowered,
            flags=re.IGNORECASE | re.DOTALL,
        )
        markers = (
            "loading your workspace",
            "loading queue",
            "loading dashboard",
            "syncing",
            "pulling",
            "please wait",
            "loading...",
        )
        return any(marker in scrubbed for marker in markers)

    @staticmethod
    def _has_business_surface(content: str) -> bool:
        lowered = str(content or "").lower()
        markers = (
            "metric-card",
            "summary-card",
            "stat-card",
            "request-list",
            "queue-list",
            "workload-list",
            "approval-list",
            "availability-list",
            "conflict-list",
            "empty-state",
            "empty-panel",
            "empty-card",
            "primary-actions",
            "quick request",
            "recent requests",
            "next requests",
            "live overview",
        )
        structural_hits = sum(1 for marker in markers if marker in lowered)
        semantic_hits = sum(lowered.count(tag) for tag in ("<section", "<article", "<button", "href="))
        return structural_hits >= 2 or (structural_hits >= 1 and semantic_hits >= 4)

    @staticmethod
    def _empty_business_container_count(content: str) -> int:
        pattern = re.compile(
            r"<(?:div|section|article)[^>]+(?:id|class)=['\"][^'\"]*(?:metrics|summary|requests|queue|availability|conflict|approval|workload)[^'\"]*['\"][^>]*>\s*</(?:div|section|article)>",
            flags=re.IGNORECASE,
        )
        return len(pattern.findall(str(content or "")))

    def _validate_root_page_first_paint(
        self,
        *,
        role: str,
        page: dict,
        content: str,
        location: str,
    ) -> list[ValidationIssue]:
        if not self._is_role_root_page(role, page):
            return []
        dependency_count = len(page.get("data_dependencies") or [])
        if dependency_count <= 0:
            return []
        issues: list[ValidationIssue] = []
        if self._has_visible_loading_surface(content):
            issues.append(
                ValidationIssue(
                    code="build.loading_first_root_surface",
                    message=f"{Path(location).name} still renders loading-first copy as the primary role root surface.",
                    severity="high",
                    location=location,
                )
            )
        if not self._has_business_surface(content) and self._empty_business_container_count(content) > 0:
            issues.append(
                ValidationIssue(
                    code="build.root_page_missing_business_surface",
                    message=f"{Path(location).name} does not render a real first-paint business surface for the role root page.",
                    severity="high",
                    location=location,
                )
            )
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
            if not isinstance(role_payload, dict):
                issues.append(
                    ValidationIssue(
                        code="build.invalid_generated_role_entry",
                        message=f"{role} has an invalid role payload in generated_app_graph.json.",
                        severity="high",
                        location="artifacts/generated_app_graph.json",
                    )
                )
                continue
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
                if not isinstance(page, dict):
                    issues.append(
                        ValidationIssue(
                            code="build.invalid_generated_page_entry",
                            message=f"{role} contains a non-object page entry in generated_app_graph.json.",
                            severity="high",
                            location="artifacts/generated_app_graph.json",
                        )
                    )
                    continue
                file_path_raw = page.get("file_path")
                if not isinstance(file_path_raw, str):
                    continue
                if not str(file_path_raw).startswith(f"miniapp/app/static/{role}/"):
                    issues.append(
                        ValidationIssue(
                            code="build.page_not_role_local",
                            message=f"{role} page must live under its own role-local static directory.",
                            severity="high",
                            location="artifacts/generated_app_graph.json",
                        )
                    )
                    continue
                style_path_raw = str(page.get("style_path") or "")
                script_path_raw = str(page.get("script_path") or "")
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
                for asset_kind, asset_path_raw in (("style", style_path_raw), ("script", script_path_raw)):
                    if not asset_path_raw:
                        issues.append(
                            ValidationIssue(
                                code=f"build.missing_page_{asset_kind}_reference",
                                message=f"Generated page is missing its {asset_kind} path in the route manifest: {Path(file_path_raw).name}",
                                severity="high",
                                location=file_path_raw,
                            )
                        )
                        continue
                    asset_path = workspace_path / asset_path_raw
                    if not asset_path.exists():
                        issues.append(
                            ValidationIssue(
                                code=f"build.missing_page_{asset_kind}_asset",
                                message=f"Generated page asset is missing: {Path(asset_path_raw).name}",
                                severity="high",
                                location=asset_path_raw,
                            )
                        )
                    elif not str(asset_path_raw).startswith(f"miniapp/app/static/{role}/"):
                        issues.append(
                            ValidationIssue(
                                code=f"build.page_{asset_kind}_not_role_local",
                                message=f"{Path(file_path_raw).name} must use its own role-local {asset_kind} asset, not a shared file.",
                                severity="high",
                                location=asset_path_raw,
                            )
                        )

                content = file_path.read_text(encoding="utf-8")
                issues.extend(
                    self._validate_root_page_first_paint(
                        role=role,
                        page=page,
                        content=content,
                        location=file_path_raw,
                    )
                )
                if "RoleCabinetHomePage" in content:
                    issues.append(
                        ValidationIssue(
                            code="build.placeholder_page",
                            message=f"{Path(file_path_raw).name} still renders a homepage placeholder wrapper.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if "/static/shared/base.css" not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_shell_style_link",
                            message=f"{Path(file_path_raw).name} does not reference the shared shell stylesheet.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if "/static/preview_bridge.js" not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_preview_bridge",
                            message=f"{Path(file_path_raw).name} does not reference the preview bridge runtime.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if "page-shell" not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_shell_root",
                            message=f"{Path(file_path_raw).name} does not include a page-shell root container with top safe-area spacing.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                expected_style_href = self._static_asset_href(style_path_raw)
                expected_script_src = self._static_asset_href(script_path_raw)
                if style_path_raw and expected_style_href and expected_style_href not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_style_link",
                            message=f"{Path(file_path_raw).name} does not reference its page CSS asset.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if script_path_raw and expected_script_src and expected_script_src not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_script_link",
                            message=f"{Path(file_path_raw).name} does not reference its page JS asset.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if re.search(r">\s*Refresh\s*<", content, flags=re.IGNORECASE):
                    issues.append(
                        ValidationIssue(
                            code="build.unnecessary_manual_refresh_action",
                            message=f"{Path(file_path_raw).name} renders a manual refresh action instead of relying on normal page loading.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if script_path_raw:
                    script_abs_path = workspace_path / script_path_raw
                    if script_abs_path.exists():
                        html_ids = self._extract_html_ids(content)
                        script_ids = self._extract_js_dom_ids(script_abs_path.read_text(encoding="utf-8"))
                        missing_ids = sorted(script_ids - html_ids)
                        if missing_ids:
                            issues.append(
                                ValidationIssue(
                                    code="build.page_script_dom_contract",
                                    message=f"{Path(file_path_raw).name} is missing DOM ids required by {Path(script_path_raw).name}: {', '.join(missing_ids[:6])}",
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
                style_path_raw = str(page.get("style_path") or "")
                script_path_raw = str(page.get("script_path") or "")
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
                for asset_kind, asset_path_raw in (("style", style_path_raw), ("script", script_path_raw)):
                    if not asset_path_raw:
                        issues.append(
                            ValidationIssue(
                                code=f"build.missing_page_{asset_kind}_reference",
                                message=f"Generated page is missing its {asset_kind} path in the route manifest: {Path(file_path_raw).name}",
                                severity="high",
                                location=file_path_raw,
                            )
                        )
                        continue
                    asset_path = workspace_path / asset_path_raw
                    if not asset_path.exists():
                        issues.append(
                            ValidationIssue(
                                code=f"build.missing_page_{asset_kind}_asset",
                                message=f"Generated page asset is missing: {Path(asset_path_raw).name}",
                                severity="high",
                                location=asset_path_raw,
                            )
                        )
                    elif not str(asset_path_raw).startswith(f"miniapp/app/static/{role}/"):
                        issues.append(
                            ValidationIssue(
                                code=f"build.page_{asset_kind}_not_role_local",
                                message=f"{Path(file_path_raw).name} must use its own role-local {asset_kind} asset, not a shared file.",
                                severity="high",
                                location=asset_path_raw,
                            )
                        )
                content = file_path.read_text(encoding="utf-8")
                issues.extend(
                    self._validate_root_page_first_paint(
                        role=role,
                        page=page,
                        content=content,
                        location=file_path_raw,
                    )
                )
                if "/static/shared/base.css" not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_shell_style_link",
                            message=f"{Path(file_path_raw).name} does not reference the shared shell stylesheet.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                expected_style_href = self._static_asset_href(style_path_raw)
                expected_script_src = self._static_asset_href(script_path_raw)
                if style_path_raw and expected_style_href and expected_style_href not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_style_link",
                            message=f"{Path(file_path_raw).name} does not reference its page CSS asset.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if script_path_raw and expected_script_src and expected_script_src not in content:
                    issues.append(
                        ValidationIssue(
                            code="build.page_missing_script_link",
                            message=f"{Path(file_path_raw).name} does not reference its page JS asset.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if re.search(r">\s*Refresh\s*<", content, flags=re.IGNORECASE):
                    issues.append(
                        ValidationIssue(
                            code="build.unnecessary_manual_refresh_action",
                            message=f"{Path(file_path_raw).name} renders a manual refresh action instead of relying on normal page loading.",
                            severity="high",
                            location=file_path_raw,
                        )
                    )
                if script_path_raw:
                    script_abs_path = workspace_path / script_path_raw
                    if script_abs_path.exists():
                        html_ids = self._extract_html_ids(content)
                        script_ids = self._extract_js_dom_ids(script_abs_path.read_text(encoding="utf-8"))
                        missing_ids = sorted(script_ids - html_ids)
                        if missing_ids:
                            issues.append(
                                ValidationIssue(
                                    code="build.page_script_dom_contract",
                                    message=f"{Path(file_path_raw).name} is missing DOM ids required by {Path(script_path_raw).name}: {', '.join(missing_ids[:6])}",
                                    severity="high",
                                    location=file_path_raw,
                                )
                            )
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
    def _static_asset_href(asset_path_raw: str) -> str:
        normalized = str(asset_path_raw or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        prefix = "miniapp/app/"
        if normalized.startswith(prefix):
            return "/" + normalized[len(prefix):]
        if normalized.startswith("app/"):
            return "/" + normalized
        if normalized.startswith("/"):
            return normalized
        return "/" + normalized

    def _validate_route_module_import_safety(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        routes_dir = workspace_path / "miniapp" / "app" / "routes"
        if not routes_dir.exists():
            return issues
        for route_file in routes_dir.glob("*.py"):
            module_name = route_file.stem
            try:
                content = route_file.read_text(encoding="utf-8")
            except OSError:
                continue
            self_import_markers = (
                f"from app.routes.{module_name} import ",
                f"import app.routes.{module_name}",
            )
            if any(marker in content for marker in self_import_markers):
                issues.append(
                    ValidationIssue(
                        code="build.route_self_import",
                        message=f"Route module {route_file.name} imports itself and will fail at runtime.",
                        severity="high",
                        location=str(route_file.relative_to(workspace_path)),
                    )
                )
            if "from miniapp.app import " in content or "import miniapp.app." in content:
                issues.append(
                    ValidationIssue(
                        code="build.invalid_route_import_root",
                        message=f"Route module {route_file.name} imports from miniapp.app; generated runtime modules must import from app.* inside the workspace.",
                        severity="high",
                        location=str(route_file.relative_to(workspace_path)),
                    )
                )
            if re.search(r"Depends\(\s*lambda:\s*get_actor_context\(\)\s*\)", content):
                issues.append(
                    ValidationIssue(
                        code="build.invalid_actor_dependency",
                        message=f"Route module {route_file.name} wraps get_actor_context in lambda, which breaks FastAPI header injection.",
                        severity="high",
                        location=str(route_file.relative_to(workspace_path)),
                    )
                )
        return issues

    def _validate_persistent_storage_contract(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        grounded_spec = self._read_grounded_spec(workspace_path)
        raw_spec = self._read_json(workspace_path / "artifacts" / "grounded_spec.json")
        persistence_count = 0
        api_count = 0
        if isinstance(raw_spec, dict):
            persistence_count = len(raw_spec.get("persistence_requirements") or [])
            api_count = len(raw_spec.get("api_requirements") or [])
        execution_class = self._execution_class_for_spec(grounded_spec)
        requires_persistence_contract = execution_class != "shell_app" or persistence_count > 0 or api_count > 0
        if not requires_persistence_contract:
            return issues

        db_path = workspace_path / "miniapp" / "app" / "db.py"
        schemas_path = workspace_path / "miniapp" / "app" / "schemas.py"
        if not db_path.exists():
            issues.append(
                ValidationIssue(
                    code="build.missing_db_module",
                    message="Workflow apps must persist data through miniapp/app/db.py.",
                    severity="high",
                    location="miniapp/app/db.py",
                )
            )
        if not schemas_path.exists():
            issues.append(
                ValidationIssue(
                    code="build.missing_schemas_module",
                    message="Workflow apps must define request/response schemas in miniapp/app/schemas.py.",
                    severity="high",
                    location="miniapp/app/schemas.py",
                )
            )
        if issues:
            return issues

        try:
            db_content = db_path.read_text(encoding="utf-8")
        except OSError:
            db_content = ""
        if "create_engine(" not in db_content or "sessionmaker(" not in db_content or "DeclarativeBase" not in db_content:
            issues.append(
                ValidationIssue(
                    code="build.invalid_db_module",
                    message="miniapp/app/db.py must define a real SQLAlchemy engine, sessionmaker, and DeclarativeBase for persisted app data.",
                    severity="high",
                    location="miniapp/app/db.py",
                )
            )
        profiles_path = workspace_path / "miniapp" / "app" / "routes" / "profiles.py"
        if profiles_path.exists():
            try:
                profiles_content = profiles_path.read_text(encoding="utf-8")
            except OSError:
                profiles_content = ""
            if "RoleProfileRecord" in profiles_content and "class RoleProfileRecord" not in db_content:
                issues.append(
                    ValidationIssue(
                        code="build.profile_contract_db_drift",
                        message="db.py no longer defines RoleProfileRecord even though routes/profiles.py still imports it.",
                        severity="high",
                        location="miniapp/app/db.py",
                    )
                )
        db_store_pattern = re.compile(
            r"^(?P<name>(?:REQUESTS|COMMENTS|ASSIGNMENTS|TIME_SLOTS|SPECIALISTS|USERS|PROFILE_STORE|[A-Z][A-Z0-9_]*(?:_STORE|_CACHE|_TABLE|_ITEMS)))\s*(?::[^=]+)?=\s*(?:\{|\[)",
            flags=re.MULTILINE,
        )
        db_store_match = db_store_pattern.search(db_content)
        if db_store_match:
            issues.append(
                ValidationIssue(
                    code="build.in_memory_db_store",
                    message=f"db.py still keeps mutable app data in {db_store_match.group('name')} instead of SQLAlchemy models and persisted rows.",
                    severity="high",
                    location="miniapp/app/db.py",
                )
            )

        routes_dir = workspace_path / "miniapp" / "app" / "routes"
        if not routes_dir.exists():
            return issues

        mutable_store_pattern = re.compile(
            r"^(?P<name>(?:REQUESTS|COMMENTS|ASSIGNMENTS|TIME_SLOTS|SPECIALISTS|USERS|PROFILE_STORE|[A-Z][A-Z0-9_]*(?:_STORE|_CACHE|_TABLE|_ITEMS)))\s*(?::[^=]+)?=\s*(?:\{|\[)",
            flags=re.MULTILINE,
        )
        for route_file in routes_dir.glob("*.py"):
            try:
                content = route_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in mutable_store_pattern.finditer(content):
                store_name = match.group("name")
                issues.append(
                    ValidationIssue(
                        code="build.in_memory_route_store",
                        message=f"Route module {route_file.name} keeps mutable app data in {store_name} instead of persisting through db.py.",
                        severity="high",
                        location=str(route_file.relative_to(workspace_path)),
                    )
                )
                break
            if "from pydantic import BaseModel" in content or "from pydantic import BaseModel," in content:
                issues.append(
                    ValidationIssue(
                        code="build.inline_route_schema_model",
                        message=f"Route module {route_file.name} defines Pydantic models inline; move shared schemas into miniapp/app/schemas.py.",
                        severity="high",
                        location=str(route_file.relative_to(workspace_path)),
                    )
                )
                continue
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
    def _extract_html_ids(content: str) -> set[str]:
        return {match.group(1) for match in re.finditer(r'id=["\']([^"\']+)["\']', content)}

    @staticmethod
    def _extract_js_dom_ids(content: str) -> set[str]:
        refs: set[str] = set()
        patterns = (
            r'getElementById\(["\']([^"\']+)["\']\)',
            r'querySelector\(["\']#([^"\']+)["\']\)',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                refs.add(match.group(1))
        return refs

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
            if "/api/auth" in content or "/auth/login" in content or "/auth/me" in content:
                issues.append(
                    ValidationIssue(
                        code="build.unexpected_auth_reference",
                        message="Generated app code must not introduce auth/login bootstrap endpoints.",
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
