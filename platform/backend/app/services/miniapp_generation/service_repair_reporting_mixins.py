from __future__ import annotations

import re
from typing import Any

from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation, RunCheckResult
from app.modules.miniapp_agent_loop.tool_agent_runtime import tool_patch_schema
from app.services.miniapp_generation.constants import ROLE_ORDER
from app.services.workspace.service import json_dumps
from app.modules.miniapp_generation_runtime import MiniappGenerationReporting, MiniappGenerationReportingCompaction, MiniappGenerationReportingRepair


class ServiceRepairReportingMixins:
    @staticmethod
    def _prioritize_repair_file_contexts(
        file_contexts: dict[str, str],
        tool_results: list[dict[str, Any]] | None,
    ) -> dict[str, str]:
        if not file_contexts:
            return {}
        prioritized_paths: list[str] = []
        for item in reversed(list(tool_results or [])):
            if str(item.get("tool") or "").strip().lower() != "read_files":
                continue
            for target in list(item.get("approved_targets") or item.get("targets") or []):
                normalized = str(target or "").strip().lstrip("./")
                if not normalized or normalized in prioritized_paths:
                    continue
                if normalized in file_contexts:
                    prioritized_paths.append(normalized)
        if not prioritized_paths:
            return dict(file_contexts)
        ordered: dict[str, str] = {}
        for path in prioritized_paths:
            ordered[path] = file_contexts[path]
        for path, content in file_contexts.items():
            if path not in ordered:
                ordered[path] = content
        return ordered

    def _collect_existing_file_contexts(self, workspace_id: str, run_id: str, target_files: list[str]) -> dict[str, str]:
        file_contexts: dict[str, str] = {}
        for file_path in target_files:
            try:
                content = self.workspace_service.try_read_text_file(workspace_id, file_path, run_id=run_id)
            except FileNotFoundError:
                continue
            if content is None:
                continue
            file_contexts[file_path] = content
        return file_contexts

    @staticmethod
    def _repair_full_context_paths(
        *,
        target_files: list[str],
        build_issues: list[ValidationIssue],
        file_contexts: dict[str, str],
    ) -> list[str]:
        preferred: list[str] = []
        for path in [*target_files, *[str(issue.location or "").strip() for issue in build_issues]]:
            normalized = str(path or "").strip().lstrip("./")
            if not normalized or normalized in preferred or normalized not in file_contexts:
                continue
            preferred.append(normalized)
        return preferred[:8]

    def _compact_repair_contexts(
        self,
        *,
        file_contexts: dict[str, str],
        full_paths: list[str],
        max_file_chars: int,
        max_total_chars: int,
        max_full_file_chars: int,
    ) -> dict[str, str]:
        compact: dict[str, str] = {}
        total = 0
        for path in full_paths:
            content = str(file_contexts.get(path) or "")
            if not content:
                continue
            bounded = self._limit_text(content, max_full_file_chars)
            next_total = total + len(bounded)
            if compact and next_total > max_total_chars:
                break
            compact[path] = bounded
            total = next_total
        remaining_contexts = {path: content for path, content in file_contexts.items() if path not in compact}
        remaining_budget = max(0, max_total_chars - total)
        if remaining_contexts and remaining_budget > 0:
            compact.update(
                self._compact_file_contexts_for_repair(
                    remaining_contexts,
                    max_file_chars=max_file_chars,
                    max_total_chars=remaining_budget,
                )
            )
        return compact

    @staticmethod
    def _preview_failure_issue(preview: Any) -> ValidationIssue:
        message = next((str(line).strip() for line in reversed(preview.logs or []) if str(line).strip()), "Preview runtime failed to rebuild.")
        return ValidationIssue(code="preview.rebuild_failed", message=message, severity="high", location="preview", blocking=True)

    @staticmethod
    def _is_non_blocking_preview_issue(issue: ValidationIssue) -> bool:
        message = issue.message.lower()
        infra_markers = ("docker daemon socket", "operation not permitted", "permission denied", "connect to the docker daemon", "dial unix")
        return any(marker in message for marker in infra_markers)

    @staticmethod
    def _should_retry_repair_with_expanded_context(message: str) -> bool:
        lowered = message.lower()
        return any(marker in lowered for marker in ("no file operations for the requested target_files", "can't access", "cannot access", "unable to inspect", "unable to access", "did not return operations"))

    @classmethod
    def _expand_repair_targets_for_safe_companions(cls, *, target_files: list[str], invalid_paths: list[str], build_issues: list[ValidationIssue]) -> list[str] | None:
        if not invalid_paths:
            return list(dict.fromkeys(target_files))
        issue_implicated_paths: set[str] = set()
        for issue in build_issues:
            location = str(issue.location or "").strip().lstrip("./")
            if cls._is_canonical_target_path(location):
                issue_implicated_paths.add(location)
            issue_implicated_paths.update(cls._safe_static_companion_paths(location))
            issue_implicated_paths.update(cls._safe_repair_companion_paths_for_issue(issue))
        additions: list[str] = []
        for path in invalid_paths:
            if not isinstance(path, str):
                return None
            if not cls._is_canonical_target_path(path):
                return None
            normalized_path = path.strip().lstrip("./")
            if normalized_path in issue_implicated_paths:
                additions.append(normalized_path)
                continue
            if not normalized_path.startswith("miniapp/app/static/shared/"):
                return None
            if not normalized_path.endswith((".js", ".css")):
                return None
            additions.append(normalized_path)
        if not additions:
            return None
        return list(dict.fromkeys([*target_files, *additions]))

    @classmethod
    def _safe_repair_companion_paths_for_issue(cls, issue: ValidationIssue) -> set[str]:
        code = str(issue.code or "").strip().lower()
        message = str(issue.message or "").strip().lower()
        location = str(issue.location or "").strip().lower()
        markers = (
            "build.missing_role_profile_page",
            "route_manifest.missing_profile_route",
            "runtime.missing_role_pages",
        )
        is_profile_surface_issue = code in {
            "build.missing_role_profile_page",
            "route_manifest.missing_profile_route",
        } or any(marker in message for marker in markers) or any(marker in location for marker in markers)
        if not is_profile_surface_issue:
            return set()
        roles = cls._roles_implicated_by_issue(issue)
        paths: set[str] = {
            "miniapp/app/routes/profiles.py",
            "miniapp/app/routes/role_pages.py",
            "miniapp/app/routes/runtime.py",
        }
        for role in roles:
            profile_base = f"miniapp/app/static/{role}/profile/"
            paths.update(
                {
                    f"{profile_base}index.html",
                    f"{profile_base}styles.css",
                    f"{profile_base}app.js",
                    f"miniapp/app/routes/{role}.py",
                }
            )
        return paths

    @staticmethod
    def _roles_implicated_by_issue(issue: ValidationIssue) -> tuple[str, ...]:
        code = str(issue.code or "").strip().lower()
        location = str(issue.location or "").strip().lower()
        if code in {"build.missing_role_profile_page", "route_manifest.missing_profile_route"}:
            return tuple(ROLE_ORDER)
        if location in {"artifacts/generated_app_graph.json", "miniapp/app/generated/route_manifest.json"}:
            return tuple(ROLE_ORDER)
        haystacks = (
            code,
            str(issue.message or "").strip().lower(),
            location,
        )
        matched = tuple(role for role in ROLE_ORDER if any(role in haystack for haystack in haystacks))
        return matched or tuple(ROLE_ORDER)

    @staticmethod
    def _safe_static_companion_paths(path: str) -> set[str]:
        normalized = str(path or "").strip().lstrip("./")
        if not normalized.startswith("miniapp/app/static/"):
            return set()
        if normalized.endswith("/index.html"):
            base = normalized[: -len("index.html")]
            return {normalized, f"{base}styles.css", f"{base}app.js"}
        if normalized.endswith("/styles.css"):
            base = normalized[: -len("styles.css")]
            return {f"{base}index.html", normalized, f"{base}app.js"}
        if normalized.endswith("/app.js"):
            base = normalized[: -len("app.js")]
            return {f"{base}index.html", f"{base}styles.css", normalized}
        return {normalized}

    @staticmethod
    def _validate_targeted_operations(*, stage_name: str, target_files: list[str], operations: list[DraftFileOperation]) -> None:
        if not target_files:
            return
        targeted_hits = [operation for operation in operations if operation.file_path in set(target_files)]
        if not targeted_hits:
            raise RuntimeError(f"{stage_name.capitalize()} returned no file operations for the requested target_files.")
        for operation in targeted_hits:
            ServiceRepairReportingMixins._validate_backend_python_operation(operation)

    @staticmethod
    def _is_backend_framework_contract_error(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in ("fastapi apirouter", "fastapi/apirouter", "flask/blueprint", "must define router = apirouter", "must stay on fastapi"))

    @staticmethod
    def _validate_backend_python_operation(operation: DraftFileOperation) -> None:
        file_path = str(operation.file_path or "").replace("\\", "/")
        if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
            return
        content = str(operation.content or "")
        lowered = content.lower()
        if "from flask import" in lowered or "blueprint(" in lowered:
            raise RuntimeError(f"{file_path} must stay on FastAPI APIRouter, not Flask/Blueprint.")
        if file_path == "miniapp/app/routes/role_pages.py":
            if "def resolve_role_page" not in content:
                raise RuntimeError(f"{file_path} must keep the shared role page resolution helpers.")
            return
        if "apirouter" not in lowered:
            raise RuntimeError(f"{file_path} must stay on FastAPI APIRouter.")
        if re.search(r"(?m)^\s*router\s*=\s*APIRouter\(", content) is None:
            raise RuntimeError(f"{file_path} must define router = APIRouter(...).")

    @staticmethod
    def _repair_schema() -> dict[str, Any]:
        return tool_patch_schema()

    @staticmethod
    def _repair_system_prompt() -> str:
        prompt = (
            "You repair an existing generated draft workspace after build or preview failure. "
            "Work like a tool-using coding agent inside the existing miniapp template: inspect evidence first, then patch only the smallest safe set of files needed to restore a working three-role DB-backed app. "
            "Preserve the current template shell, keep the 76px top safe-area contract, keep preview bridge wiring, and keep the canonical FastAPI/runtime structure unless the evidence explicitly shows it is broken."
        )
        from app.services.miniapp_generation.service import GenerationService
        GenerationService._assert_english_control_text(prompt)
        return prompt

    def _repair_user_prompt(self, **kwargs: Any) -> str:
        build_issues = kwargs["build_issues"]
        structural_failure = self._is_structural_contract_failure(build_issues)
        compact_target_files = list(kwargs["target_files"]) if kwargs.get("expanded_context") else (kwargs["target_files"][:28] if structural_failure else kwargs["target_files"][:12])
        prioritized_file_contexts = self._prioritize_repair_file_contexts(
            kwargs["file_contexts"],
            kwargs.get("tool_results") or [],
        )
        full_context_paths = self._repair_full_context_paths(
            target_files=compact_target_files,
            build_issues=build_issues,
            file_contexts=prioritized_file_contexts,
        )
        compact_file_contexts = self._compact_repair_contexts(
            file_contexts=prioritized_file_contexts,
            full_paths=full_context_paths,
            max_file_chars=5600 if kwargs.get("expanded_context") else (4200 if structural_failure else 2200),
            max_total_chars=42000 if kwargs.get("expanded_context") else (32000 if structural_failure else 24000),
            max_full_file_chars=14000 if kwargs.get("expanded_context") else 12000,
        )
        grounded_spec = kwargs["grounded_spec"]
        generation_mode = kwargs.get("generation_mode") or GenerationMode.BALANCED
        generation_mode_value = generation_mode.value if isinstance(generation_mode, GenerationMode) else str(generation_mode)
        return json_dumps({
            "task": "Repair build or preview failures in the generated draft",
            "attempt": kwargs["attempt"],
            "generation_mode": generation_mode_value,
            "repair_mode": "structural_bundle" if structural_failure else "targeted_patch",
            "prompt": kwargs["prompt"],
            "role_scope": kwargs["role_scope"],
            "scope_mode": kwargs["scope_mode"],
            "target_files": compact_target_files,
            "grounded_spec": {"product_goal": grounded_spec.product_goal, "api_requirements": [item.model_dump(mode="json") for item in grounded_spec.api_requirements[:6]], "assumptions": [item.model_dump(mode="json") for item in grounded_spec.assumptions[:4]]},
            "role_contract": self._compact_role_contract_for_codegen(kwargs["role_contract"], kwargs["role_scope"]),
            "page_graph": self._compact_page_graph_for_codegen(kwargs["page_graph"], kwargs["role_scope"]),
            "file_contexts": compact_file_contexts,
            "build_issues": [issue.model_dump(mode="json") for issue in build_issues],
            "preview_issue": kwargs["preview_issue"].model_dump(mode="json") if kwargs["preview_issue"] is not None else None,
            "preview_logs": kwargs["preview_logs"][-6:],
            "previous_turn_summary": kwargs.get("previous_turn_summary"),
            "previous_diff_summary": kwargs.get("previous_diff_summary"),
            "tool_results": kwargs.get("tool_results") or [],
            "read_only_surfaces": [
                "miniapp/tests/",
                "miniapp/app/generated/route_manifest.json",
                "miniapp/app/generated/runtime_manifest.json",
                "artifacts/generated_app_graph.json",
            ],
            "rules": [
                "Use the tool-owned repair loop when evidence is insufficient: request list_files, read_files, search_files, run_command, or run_checks before editing.",
                "Treat build_issues, preview_issue, target_files, file_contexts, and hard invariants as the source of truth.",
                "Preserve the three-role DB-backed runtime and linked flows for client, specialist, and manager.",
                "Preserve the shared shell contract, including the current 76px top safe-area baseline and preview bridge references.",
                "Every create or replace operation must include the full resulting file content. Never return import-only snippets, diff fragments, or partial blocks.",
                "Generated tests and generated manifests are read-only; repair application code instead.",
                "Do not redesign the app or synthesize a new business flow. Fix the current failing cluster only.",
                "If you return outcome=tool_request, return tool_requests and no operations.",
                "Use run_checks mode=exact for focused verification and mode=final when you need the full final snapshot before patching.",
                "run_command is diagnostic-only: destructive shell, git reset/discard, network fetches, package installs, and docker rebuild commands are blocked.",
            ],
        })

    @staticmethod
    def _diff_summary(diff_text: str) -> str:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE):
            candidate = match.group(1).strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            paths.append(candidate)
        if not paths:
            return "No draft diff was produced."
        unique_paths = list(dict.fromkeys(paths))
        return f"Changed files: {', '.join(unique_paths[:6])}"

    def _build_agent_traceability_report(self, workspace_id: str, grounded_spec: Any, operations: list[DraftFileOperation]):
        return self.generation_reporting._build_agent_traceability_report(workspace_id, grounded_spec, operations)

    @staticmethod
    def _build_agent_summary(*, grounded_spec: Any, role_scope: list[str], operations: list[DraftFileOperation], generation_mode: GenerationMode, assistant_message: str) -> str:
        return MiniappGenerationReporting._build_agent_summary(grounded_spec=grounded_spec, role_scope=role_scope, operations=operations, generation_mode=generation_mode, assistant_message=assistant_message)

    @staticmethod
    def _compile_code_summary(operations: list[DraftFileOperation], role_scope: list[str]) -> dict[str, int | str]:
        return MiniappGenerationReporting._compile_code_summary(operations, role_scope)

    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        return MiniappGenerationReportingCompaction._limit_text(text, max_chars)

    def _bounded_file_contexts(self, file_contexts: dict[str, str], *, max_file_chars: int, max_total_chars: int) -> dict[str, str]:
        return self.generation_reporting_compaction._bounded_file_contexts(file_contexts, max_file_chars=max_file_chars, max_total_chars=max_total_chars)

    @staticmethod
    def _compact_grounded_spec_for_codegen(grounded_spec: Any) -> dict[str, Any]:
        return MiniappGenerationReportingCompaction._compact_grounded_spec_for_codegen(grounded_spec)

    @staticmethod
    def _compact_role_contract_for_codegen(role_contract: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        return MiniappGenerationReportingCompaction._compact_role_contract_for_codegen(role_contract, role_scope)

    @staticmethod
    def _compact_page_graph_for_codegen(page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        return MiniappGenerationReportingCompaction._compact_page_graph_for_codegen(page_graph, role_scope)

    @staticmethod
    def _stateful_page_contracts(page_graph: dict[str, Any], role_scope: list[str]) -> list[dict[str, Any]]:
        return MiniappGenerationReportingRepair._stateful_page_contracts(page_graph, role_scope)

    def _build_page_graph_verification_report(self, page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        from app.modules.miniapp_validation import PageGraphValidation
        from app.services.miniapp_generation.service import GenerationService
        return PageGraphValidation.build_page_graph_verification_report(page_graph, role_scope, normalize_role_route_path=lambda role, route_path, index: GenerationService._normalize_role_route_path(role, route_path, index=index), is_business_page=GenerationService._is_business_page, is_canonical_target_path=GenerationService._is_canonical_target_path)

    @staticmethod
    def _failure_signature_for_issues(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None) -> str:
        return MiniappGenerationReportingRepair._failure_signature_for_issues(build_issues, preview_issue)

    @staticmethod
    def _is_structural_contract_failure(build_issues: list[ValidationIssue]) -> bool:
        return MiniappGenerationReportingRepair._is_structural_contract_failure(build_issues)

    @staticmethod
    def _extract_failure_file_hints(check_results: list[RunCheckResult], allowed_targets: list[str]) -> list[str]:
        return MiniappGenerationReportingRepair._extract_failure_file_hints(check_results, allowed_targets)

    def _causal_surface_for_issues(self, *, build_issues: list[ValidationIssue], check_results: list[RunCheckResult], active_targets: list[str]) -> set[str]:
        return self.generation_reporting_repair._causal_surface_for_issues(build_issues=build_issues, check_results=check_results, active_targets=active_targets)

    def _expand_structural_repair_targets(self, *, active_targets: list[str], build_issues: list[ValidationIssue]) -> tuple[list[str], list[str]]:
        return self.generation_reporting_repair._expand_structural_repair_targets(active_targets=active_targets, build_issues=build_issues)

    def _repair_targets_for_attempt(self, *, active_targets: list[str], check_results: list[RunCheckResult], attempt: int, causal_surface: set[str], scope_mode: str, structural_failure: bool) -> list[str]:
        return self.generation_reporting_repair._repair_targets_for_attempt(active_targets=active_targets, check_results=check_results, attempt=attempt, causal_surface=causal_surface, scope_mode=scope_mode, structural_failure=structural_failure)

    def _compact_file_contexts_for_repair(self, file_contexts: dict[str, str], *, max_file_chars: int, max_total_chars: int) -> dict[str, str]:
        return self.generation_reporting_compaction._bounded_file_contexts(file_contexts, max_file_chars=max_file_chars, max_total_chars=max_total_chars)
