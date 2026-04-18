from __future__ import annotations

import re
from typing import Any

from app.models.artifacts import ValidationIssue
from app.models.domain import RunCheckResult

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationReportingRepair(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _stateful_page_contracts(page_graph: dict[str, Any], role_scope: list[str]) -> list[dict[str, Any]]:
        roles = page_graph.get("roles") or {}
        contracts: list[dict[str, Any]] = []
        for role in role_scope:
            role_payload = roles.get(role) or {}
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                data_dependencies = list(page.get("data_dependencies") or [])
                if not data_dependencies:
                    continue
                contracts.append(
                    {
                        "role": role,
                        "page_id": page.get("page_id"),
                        "file_path": page.get("file_path"),
                        "data_dependencies": data_dependencies[:6],
                        "loading_state": page.get("loading_state"),
                        "empty_state": page.get("empty_state"),
                        "error_state": page.get("error_state"),
                    }
                )
        return contracts[:12]

    @staticmethod
    def _failure_signature_for_issues(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None) -> str:
        signature_parts = [f"{issue.code}:{issue.message.strip().lower()}" for issue in build_issues]
        if preview_issue is not None:
            signature_parts.append(f"{preview_issue.code}:{preview_issue.message.strip().lower()}")
        return " | ".join(sorted(dict.fromkeys(signature_parts))) or "no_failure_signature"

    @staticmethod
    def _is_structural_contract_failure(build_issues: list[ValidationIssue]) -> bool:
        markers = ("miniapp route is missing", "missing endpoint", "expects /api/")
        for issue in build_issues:
            if issue.code in {
                "check.schema_validators",
                "check.connectivity_validators",
                "connectivity.missing_backend_route",
                "connectivity.unwired_page_dependency",
                "build.missing_role_profile_page",
                "build.missing_role_routes",
                "build.insufficient_routes",
                "build.insufficient_pages",
                "build.page_missing_script_link",
                "build.page_script_dom_contract",
                "connectivity.missing_ui_loading_state",
                "connectivity.missing_ui_error_state",
                "tests.python_generated_app",
                "tests.js_generated_app",
            }:
                return True
            lowered = issue.message.lower()
            if any(marker in lowered for marker in markers):
                return True
        return False

    @staticmethod
    def _extract_failure_file_hints(check_results: list[RunCheckResult], allowed_targets: list[str]) -> list[str]:
        allowed_set = set(allowed_targets)
        resolved: list[str] = []
        pattern = re.compile(r"([A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|py))\(")
        for result in check_results:
            for line in result.logs:
                for match in pattern.finditer(line):
                    candidate = match.group(1)
                    if candidate in allowed_set:
                        resolved.append(candidate)
                        continue
                    prefixed_candidates = [
                        path for path in allowed_targets if path.endswith(f"/{candidate}") or path == candidate
                    ]
                    if prefixed_candidates:
                        resolved.append(prefixed_candidates[0])
        return list(dict.fromkeys(resolved))

    def _causal_surface_for_issues(
        self,
        *,
        build_issues: list[ValidationIssue],
        check_results: list[RunCheckResult],
        active_targets: list[str],
    ) -> set[str]:
        causal = set(self._extract_failure_file_hints(check_results, active_targets))
        active_target_set = set(active_targets)
        for issue in build_issues:
            if issue.location in active_target_set:
                causal.add(issue.location)
            if issue.code in {"build.placeholder_role_surface", "build.placeholder_page"}:
                for candidate in active_targets:
                    normalized = candidate.replace("\\", "/")
                    if normalized.startswith("miniapp/app/static/") and any(
                        normalized.endswith(suffix)
                        for suffix in ("/index.html", "/app.js", "/styles.css")
                    ):
                        role_segment = normalized.split("/")[3] if len(normalized.split("/")) > 3 else ""
                        if role_segment in {"client", "specialist", "manager"}:
                            causal.add(candidate)
            for match in re.finditer(r"/api/([a-zA-Z0-9_-]+)", issue.message):
                endpoint_name = match.group(1)
                causal.add(self._route_module_path_for_endpoint_name(endpoint_name))
                causal.add("miniapp/app/main.py")
        return causal or active_target_set

    def _expand_structural_repair_targets(
        self,
        *,
        active_targets: list[str],
        build_issues: list[ValidationIssue],
    ) -> tuple[list[str], list[str]]:
        expanded = list(active_targets)
        added: list[str] = []
        for issue in build_issues:
            for match in re.finditer(r"/api/([a-zA-Z0-9_-]+)", issue.message):
                endpoint_name = match.group(1)
                for candidate in (self._route_module_path_for_endpoint_name(endpoint_name), "miniapp/app/main.py"):
                    if candidate not in expanded:
                        expanded.append(candidate)
                        added.append(candidate)
            message = str(issue.message or "").lower()
            if issue.code in {
                "build.invalid_generated_app_graph",
                "build.missing_role_routes",
                "build.insufficient_routes",
                "build.insufficient_pages",
                "build.placeholder_role_surface",
                "build.placeholder_page",
                "build.missing_role_profile_page",
                "build.page_missing_script_link",
                "build.page_script_dom_contract",
                "connectivity.missing_ui_loading_state",
                "connectivity.missing_ui_error_state",
                "tests.python_generated_app",
                "tests.js_generated_app",
            } or any(marker in message for marker in ("route", "navigation", "manifest", "shared", "profile", "workspace", "workbench", "roleprofilerecord", "db.py", "schemas.py")):
                for candidate in (
                    "artifacts/generated_app_graph.json",
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                    "miniapp/app/routes/profiles.py",
                    "miniapp/app/static/shared/common.js",
                    "miniapp/app/static/shared/base.css",
                    "miniapp/app/generated/route_manifest.json",
                    "miniapp/app/generated/runtime_manifest.json",
                ):
                    if candidate not in expanded:
                        expanded.append(candidate)
                        added.append(candidate)
                for candidate in active_targets:
                    if candidate.startswith("miniapp/app/static/") and candidate.endswith((".html", ".js", ".css")) and candidate not in expanded:
                        expanded.append(candidate)
                        added.append(candidate)
        return list(dict.fromkeys(expanded)), list(dict.fromkeys(added))

    def _repair_targets_for_attempt(
        self,
        *,
        active_targets: list[str],
        check_results: list[RunCheckResult],
        attempt: int,
        causal_surface: set[str],
        scope_mode: str,
        structural_failure: bool,
    ) -> list[str]:
        hinted_files = self._extract_failure_file_hints(check_results, active_targets)
        if structural_failure:
            prioritized = [path for path in active_targets if path in causal_surface]
            if prioritized:
                return list(dict.fromkeys([*prioritized, *active_targets]))[:20]
            return list(dict.fromkeys(active_targets))[:20]
        if scope_mode == "whole_file_build":
            narrowed = [path for path in hinted_files if path in set(active_targets)]
            if narrowed:
                return narrowed[:8]
            dependent = [path for path in active_targets if path in causal_surface]
            if dependent:
                return dependent[:10]
        if attempt <= 3:
            narrowed = [path for path in hinted_files if path in set(active_targets)]
            if narrowed:
                return narrowed[:6]
            narrowed = [path for path in active_targets if path in causal_surface]
            if narrowed:
                return narrowed[:8]
        if attempt <= 5:
            widened = [path for path in active_targets if path in causal_surface]
            if widened:
                return widened[:12]
        return list(active_targets)
