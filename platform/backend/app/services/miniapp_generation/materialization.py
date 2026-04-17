from __future__ import annotations

from collections import Counter
import os
from typing import Any, Callable

from app.models.artifacts import MaterializationReport, ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation, RunCheckResult


class MiniappMaterializationService:
    def __init__(
        self,
        *,
        default_page_asset_path: Callable[[str, str], str],
        workspace_file_tree: Callable[[str, str], list[dict[str, Any]]],
        build_stage_reports: Callable[[dict[str, Any], list[str], set[str]], list[dict[str, Any]]],
    ) -> None:
        self._default_page_asset_path = default_page_asset_path
        self._workspace_file_tree = workspace_file_tree
        self._build_stage_reports = build_stage_reports

    @staticmethod
    def normalize_runtime_python_path(path: str) -> str:
        if path.startswith("miniapp/app/routes/") and path.endswith(".py"):
            head, tail = path.rsplit("/", 1)
            if "-" in tail:
                return f"{head}/{tail.replace('-', '_')}"
        return path

    @classmethod
    def normalize_runtime_python_paths_in_plan(cls, plan_result: dict[str, Any]) -> None:
        for key in ("target_files", "backend_targets", "files_to_read", "shared_files"):
            values = plan_result.get(key)
            if isinstance(values, list):
                plan_result[key] = [
                    cls.normalize_runtime_python_path(str(value))
                    for value in values
                    if isinstance(value, str)
                ]
        cls.normalize_runtime_python_paths_in_structure(plan_result.get("page_graph"))
        cls.normalize_runtime_python_paths_in_structure(plan_result.get("execution_plan"))
        cls.normalize_runtime_python_paths_in_structure(plan_result.get("planner_contract_enrichment"))
        cls.normalize_runtime_python_paths_in_structure(plan_result.get("generation_clusters"))

    def expand_page_asset_targets_in_plan(self, plan_result: dict[str, Any]) -> None:
        page_graph = plan_result.get("page_graph")
        if not isinstance(page_graph, dict):
            return
        target_files = list(plan_result.get("target_files") or [])
        files_to_read = list(plan_result.get("files_to_read") or [])
        expanded_paths: list[str] = []
        for role_payload in (page_graph.get("roles") or {}).values():
            if not isinstance(role_payload, dict):
                continue
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                file_path = str(page.get("file_path") or "").strip()
                if not file_path:
                    continue
                style_path = str(page.get("style_path") or self._default_page_asset_path(file_path, "css")).strip()
                script_path = str(page.get("script_path") or self._default_page_asset_path(file_path, "js")).strip()
                page["style_path"] = style_path
                page["script_path"] = script_path
                expanded_paths.extend([file_path, style_path, script_path])
        plan_result["target_files"] = list(dict.fromkeys([*target_files, *expanded_paths]))
        plan_result["files_to_read"] = list(dict.fromkeys([*files_to_read, *expanded_paths]))

    @classmethod
    def normalize_runtime_python_paths_in_structure(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.normalize_runtime_python_path(value)
        if isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = cls.normalize_runtime_python_paths_in_structure(item)
            if all(isinstance(item, str) for item in value):
                value[:] = list(dict.fromkeys(str(item) for item in value))
            return value
        if isinstance(value, dict):
            for key, item in list(value.items()):
                value[key] = cls.normalize_runtime_python_paths_in_structure(item)
            return value
        return value

    def realized_draft_file_paths(self, workspace_id: str, run_id: str) -> set[str]:
        return {
            str(item.get("path"))
            for item in self._workspace_file_tree(workspace_id, run_id)
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        }

    @staticmethod
    def missing_required_cluster_targets(
        *,
        cluster_targets: list[str],
        operations: list[DraftFileOperation],
        file_contexts: dict[str, str],
    ) -> list[str]:
        operation_paths = {
            operation.file_path
            for operation in operations
            if operation.operation in {"create", "replace"} and operation.content is not None
        }
        required_targets: list[str] = []
        for path in cluster_targets:
            existing_content = str(file_contexts.get(path) or "")
            if existing_content.strip():
                continue
            if path.endswith((".css", ".js")) and "generated/" not in path and "/routes/" not in path:
                continue
            required_targets.append(path)
        return [path for path in required_targets if path not in operation_paths]

    def build_materialization_report(
        self,
        *,
        execution_class: str,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> MaterializationReport:
        normalized_realized_paths = {
            self.normalize_runtime_python_path(str(path))
            for path in realized_paths
            if isinstance(path, str)
        }
        planned_pages = [
            self.normalize_runtime_python_path(str(page.get("file_path")))
            for role in role_scope
            for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("file_path"), str)
        ]
        expected_backend_files = [
            self.normalize_runtime_python_path(str(path))
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        ]
        expected_manifests = [
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/app/generated/static_runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        ]
        missing_files = [path for path in planned_pages if path not in normalized_realized_paths]
        missing_backend_files = [path for path in expected_backend_files if path not in normalized_realized_paths]
        role_unique_page_counts: dict[str, int] = {}
        duplicate_page_file_roles: dict[str, list[str]] = {}
        role_page_counts = {
            role: sum(
                1
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict)
                and self.normalize_runtime_python_path(str(page.get("file_path") or "")) in normalized_realized_paths
            )
            for role in role_scope
        }
        for role in role_scope:
            role_pages = [
                self.normalize_runtime_python_path(str(page.get("file_path") or ""))
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict) and isinstance(page.get("file_path"), str)
            ]
            role_unique_page_counts[role] = len(set(role_pages))
            duplicates = sorted(path for path, count in Counter(role_pages).items() if count > 1 and path)
            if duplicates:
                duplicate_page_file_roles[role] = duplicates
        backend_surface_ok = not missing_backend_files if expected_backend_files else execution_class == "shell_app"
        page_surface_ok = (
            not missing_files
            and not duplicate_page_file_roles
            and all(count >= 2 for count in role_page_counts.values())
        ) if execution_class != "shell_app" else all(count >= 1 for count in role_page_counts.values())
        manifest_surface_ok = all(path in normalized_realized_paths for path in expected_manifests)
        fell_back_to_template = execution_class != "shell_app" and not page_surface_ok and all(count <= 2 for count in role_page_counts.values())
        return MaterializationReport(
            execution_class=execution_class,  # type: ignore[arg-type]
            planned_files=sorted(dict.fromkeys(planned_pages)),
            created_files=sorted(normalized_realized_paths),
            missing_files=sorted(dict.fromkeys(missing_files)),
            expected_backend_files=sorted(dict.fromkeys(expected_backend_files)),
            missing_backend_files=sorted(dict.fromkeys(missing_backend_files)),
            backend_surface_ok=backend_surface_ok,
            page_surface_ok=page_surface_ok,
            manifest_surface_ok=manifest_surface_ok,
            fell_back_to_template=fell_back_to_template,
            role_page_counts=role_page_counts,
            role_unique_page_counts=role_unique_page_counts,
            duplicate_page_file_roles=duplicate_page_file_roles,
            stage_reports=self._build_stage_reports(page_graph, role_scope, normalized_realized_paths),
        )

    @staticmethod
    def materialization_gate_result(
        report: MaterializationReport,
        *,
        require_multi_page: bool,
        scope_mode: str,
        generation_mode: GenerationMode,
    ) -> tuple[str, list[str]] | None:
        if scope_mode == "minimal_patch":
            return None
        fast_mode = generation_mode == GenerationMode.FAST
        if report.execution_class == "shell_app":
            if report.missing_files:
                return ("generation.edit.missing_planned_files", [f"Planned pages were not materialized: {', '.join(report.missing_files[:5])}"])
            return None
        if report.fell_back_to_template:
            return (
                "generation.edit.fell_back_to_template",
                ["Draft collapsed back to the shell template instead of materializing the planned workflow pages."],
            )
        if report.missing_backend_files:
            return (
                "generation.edit.missing_backend_surface",
                [f"Required backend workflow files were not materialized: {', '.join(report.missing_backend_files[:5])}"],
            )
        if report.duplicate_page_file_roles:
            role, duplicates = next(iter(report.duplicate_page_file_roles.items()))
            return (
                "generation.edit.duplicate_page_surface",
                [f"{role} reuses the same page file for multiple planned routes: {', '.join(duplicates[:5])}"],
            )
        if not report.manifest_surface_ok and generation_mode == GenerationMode.QUALITY:
            return (
                "generation.edit.plan_not_materialized",
                ["Generated runtime manifests were not materialized for the planned workflow app."],
            )
        if (report.missing_files and not fast_mode) or (require_multi_page and not report.page_surface_ok):
            return (
                "generation.edit.missing_planned_files",
                [f"Planned workflow pages were not materialized: {', '.join(report.missing_files[:5])}"],
            )
        return None

    @staticmethod
    def build_check_results(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None = None) -> list[RunCheckResult]:
        results: list[RunCheckResult] = []
        if build_issues:
            results.append(
                RunCheckResult(
                    name="draft-build",
                    status="failed",
                    details="; ".join(issue.message for issue in build_issues[:5]),
                )
            )
        else:
            results.append(RunCheckResult(name="draft-build", status="passed", details="Scaffold entrypoints are present in the draft."))
        if preview_issue is not None:
            results.append(RunCheckResult(name="draft-preview", status="failed", details=preview_issue.message))
        elif build_issues:
            results.append(
                RunCheckResult(
                    name="draft-preview",
                    status="skipped",
                    details="Preview rebuild was skipped because build validation failed.",
                )
            )
        elif not build_issues:
            results.append(RunCheckResult(name="draft-preview", status="passed", details="Preview runtime rebuilt successfully."))
        return results

    @staticmethod
    def filter_non_blocking_build_issues(build_issues: list[ValidationIssue], *, scope_mode: str) -> list[ValidationIssue]:
        if scope_mode != "minimal_patch":
            return build_issues
        ignored_codes = {
            "build.invalid_generated_app_graph",
            "build.missing_role_routes",
            "build.placeholder_role_surface",
            "build.insufficient_routes",
            "build.insufficient_pages",
        }
        return [issue for issue in build_issues if issue.code not in ignored_codes]

    @staticmethod
    def repair_attempt_limit(generation_mode: GenerationMode, intent: str) -> int:
        if intent in {"edit", "refine", "role_only_change"}:
            return max(1, int(os.getenv("EDIT_AUTO_REPAIR_ATTEMPTS", "4")))
        if generation_mode == GenerationMode.FAST:
            return max(1, int(os.getenv("FAST_AUTO_REPAIR_ATTEMPTS", "3")))
        if generation_mode == GenerationMode.QUALITY:
            return max(1, int(os.getenv("QUALITY_AUTO_REPAIR_ATTEMPTS", "6")))
        if generation_mode == GenerationMode.BALANCED:
            return max(1, int(os.getenv("BALANCED_AUTO_REPAIR_ATTEMPTS", "5")))
        return max(0, int(os.getenv("BASIC_AUTO_REPAIR_ATTEMPTS", "1")))
