from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    GenerateRequest,
    JobRecord,
    utc_now,
)
from app.modules.miniapp_agent_loop.tool_agent_runtime import normalize_tool_requests
from app.modules.miniapp_agent_loop.types import (
    RepairTurnContext,
    WorkspaceLoopCallbacks,
    WorkspaceLoopResult,
    WorkspaceLoopTurnPlan,
)
from app.modules.miniapp_generation_runtime.generation_repair_tools import GenerationRepairToolRuntime
from app.services.check_runner import CheckRunner

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationRepair:
    MAX_TOOL_ROUNDS = 5
    COMMAND_TIMEOUT_SECONDS = 20
    _READ_ONLY_REPAIR_SURFACE_PREFIXES = (
        "miniapp/tests/",
        "miniapp/app/generated/",
        "artifacts/",
    )

    def __init__(self, service: "GenerationService") -> None:
        self.service = service
        self.tool_runtime = GenerationRepairToolRuntime(service)

    @classmethod
    def _is_read_only_repair_surface(cls, file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        return normalized.startswith(cls._READ_ONLY_REPAIR_SURFACE_PREFIXES)

    @staticmethod
    def _tool_request_signature(tool_requests: list[dict[str, Any]]) -> str:
        if not tool_requests:
            return ""
        normalized_items = []
        for item in tool_requests:
            normalized_items.append(
                {
                    "tool": str(item.get("tool") or "").strip().lower(),
                    "mode": str(item.get("mode") or "").strip().lower(),
                    "targets": sorted(
                        {
                            str(target or "").strip().lstrip("./")
                            for target in list(item.get("targets") or [])
                            if str(target or "").strip()
                        }
                    ),
                    "pattern": str(item.get("pattern") or "").strip(),
                    "command": str(item.get("command") or "").strip(),
                }
            )
        return json.dumps(normalized_items, sort_keys=True, ensure_ascii=True)

    @staticmethod
    def _duplicate_tool_request_feedback(tool_requests: list[dict[str, Any]]) -> dict[str, object]:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        return {
            "tool": "tool_request_feedback",
            "targets": list(dict.fromkeys(requested_targets)),
            "error": (
                "The same tool request was already executed in this repair attempt. "
                "These files were already read and are available in file_contexts or prior tool_results. "
                "Use the existing context to return operations or outcome=no_progress instead of requesting the same files again."
            ),
        }

    @staticmethod
    def _already_available_context_note(tool_requests: list[dict[str, Any]]) -> str:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        distinct_targets = list(dict.fromkeys(requested_targets))
        if not distinct_targets:
            return (
                "Context reuse recovery mode:\n"
                "- The files you asked to read are already present in file_contexts.\n"
                "- Do not request read_files again for already provided files.\n"
                "- Use the current context to return create/replace operations for the allowed targets now.\n"
                "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
            )
        rendered_targets = "\n".join(f"- {target}" for target in distinct_targets[:12])
        return (
            "Context reuse recovery mode:\n"
            "- The files you asked to read are already present in file_contexts.\n"
            "- These already-available files are:\n"
            f"{rendered_targets}\n"
            "- Do not request read_files again for these files.\n"
            "- Use the current context to return create/replace operations for the allowed targets now.\n"
            "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
        )

    @staticmethod
    def _read_request_already_satisfied(
        tool_requests: list[dict[str, Any]],
        file_contexts: dict[str, str],
    ) -> bool:
        if not tool_requests:
            return False
        saw_read = False
        for item in tool_requests:
            tool_name = str(item.get("tool") or "").strip().lower()
            if tool_name != "read_files":
                return False
            saw_read = True
            targets = [
                str(target or "").strip().lstrip("./")
                for target in list(item.get("targets") or [])
                if str(target or "").strip()
            ]
            if not targets:
                return False
            if any(
                not str(file_contexts.get(target) or "").strip()
                or GenerationRepairToolRuntime.is_missing_file_context(file_contexts.get(target))
                for target in targets
            ):
                return False
        return saw_read

    @staticmethod
    def _is_retryable_patch_validation_error(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(
            marker in lowered
            for marker in (
                "must stay on fastapi apirouter",
                "must define router = apirouter",
                "must keep the shared role page resolution helpers",
                "full resulting file content",
                "partial snippet",
                "import block",
            )
        )

    @staticmethod
    def _invalid_patch_feedback(message: str) -> str:
        return (
            "Previous patch response was invalid.\n"
            f"- Validation error: {message}\n"
            "- For every create or replace operation, content must be the full resulting file body, not a snippet, diff fragment, or import-only block.\n"
            "- For Python route files under miniapp/app/routes, keep the full FastAPI module with `from fastapi import APIRouter` and a top-level `router = APIRouter(...)` unless the target is helper-only role_pages.py.\n"
            "- Reuse the existing file_contexts and return corrected operations now.\n"
        )

    def _execute_generation_checks(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        draft_source,
        changed_files: list[str],
        fallback_changed_files: list[str],
        page_graph: dict[str, Any],
        role_scope: list[str],
        scope_mode: str,
        mode: str = "exact",
    ) -> tuple[CheckExecutionRecord, dict[str, Any]]:
        changed = sorted(set(changed_files or fallback_changed_files))
        preflight_issues = self.service._preflight_generation_issues(
            draft_root=draft_source,
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            changed_files=changed,
            page_graph=page_graph,
            role_scope=role_scope,
        )
        if preflight_issues:
            preview = self.service.preview_service.get(workspace_id)
            execution = CheckExecutionRecord(
                workspace_id=workspace_id,
                run_id=draft_run_id,
                results=self.service._preflight_check_results(preflight_issues),
                duration_ms=0,
                started_at=utc_now(),
                completed_at=utc_now(),
            )
            preview_details = {
                "status": preview.status,
                "stage": preview.stage,
                "progress_percent": preview.progress_percent,
                "logs": list(preview.logs),
                "last_error": preview.last_error,
                "containers": [],
                "container_logs": {},
            }
            return execution, preview_details
        execution = self.service.check_runner.run(
            workspace_id=workspace_id,
            run_id=draft_run_id,
            source_dir=draft_source,
            changed_files=changed,
            preview_run_id=draft_run_id,
            scope_mode="whole_file_build" if mode == "final" else scope_mode,
        )
        preview = self.service.preview_service.get(workspace_id)
        preview_details = {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "logs": list(preview.logs),
            "last_error": preview.last_error,
            "containers": [],
            "container_logs": {},
        }
        return execution, preview_details

    def _build_generation_repair_turn_context(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, Any],
        active_repair_targets: list[str],
        scope_mode: str,
        attempt: int,
        repeated_no_progress: int,
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> tuple[RepairTurnContext, ValidationIssue | None]:
        check_issues = CheckRunner.failing_issues(latest_execution.results)
        build_issues = [issue for issue in check_issues if issue.location != "preview"]
        preview_issue = next((issue for issue in check_issues if issue.location == "preview"), None)
        failure_class = self.service.check_runner.classify_failure(latest_execution.results)
        failure_signature = self.service._failure_signature_for_issues(build_issues, preview_issue)
        structural_failure = self.service._is_structural_contract_failure(build_issues)
        causal_surface = self.service._causal_surface_for_issues(
            build_issues=build_issues,
            check_results=latest_execution.results,
            active_targets=active_repair_targets,
        )
        if structural_failure and repeated_no_progress >= 1:
            expanded_targets, _added_targets = self.service._expand_structural_repair_targets(
                active_targets=active_repair_targets,
                build_issues=build_issues,
            )
            active_repair_targets[:] = expanded_targets
        repair_target_files = self.service._repair_targets_for_attempt(
            active_targets=active_repair_targets,
            check_results=latest_execution.results,
            attempt=attempt,
            causal_surface=causal_surface,
            scope_mode=scope_mode,
            structural_failure=structural_failure,
        )
        file_contexts = self.service._collect_existing_file_contexts(
            workspace_id,
            draft_run_id,
            repair_target_files,
        )
        return (
            RepairTurnContext(
                failure_class=failure_class,
                failure_signature=failure_signature,
                root_cause_summary=self.service._summarize_failed_checks(build_issues, preview_issue),
                failing_checks=build_issues,
                implicated_files=list(repair_target_files),
                file_contexts=file_contexts,
                previous_turn_summary=last_turn_summary,
                previous_diff_summary=latest_diff_summary,
                metadata={
                    "preview_issue": preview_issue,
                    "preview_logs": list(latest_preview_details.get("logs") or []),
                },
            ),
            preview_issue,
        )

    def _plan_generation_repair_turn(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        prompt: str,
        grounded_spec,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        plan_result: dict[str, Any],
        generation_mode: GenerationMode,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, Any],
        attempt: int,
        context_mode: str,
        repeated_no_progress: int,
        active_repair_targets: list[str],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> WorkspaceLoopTurnPlan:
        visual_only_patch = bool(plan_result.get("visual_only_patch"))
        refresh_runtime_artifacts = bool(plan_result.get("refresh_runtime_artifacts"))
        turn_context, preview_issue = self._build_generation_repair_turn_context(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            latest_execution=latest_execution,
            latest_preview_details=latest_preview_details,
            active_repair_targets=active_repair_targets,
            scope_mode=plan_result["scope_mode"],
            attempt=attempt,
            repeated_no_progress=repeated_no_progress,
            last_turn_summary=last_turn_summary,
            latest_diff_summary=latest_diff_summary,
        )
        repair_result = self.repair_draft_after_failure(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            prompt=prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            role_contract=role_contract,
            page_graph=page_graph,
            scope_mode=plan_result["scope_mode"],
            target_files=turn_context.implicated_files,
            file_contexts=turn_context.file_contexts,
            build_issues=list(turn_context.failing_checks),
            preview_issue=preview_issue,
            preview_logs=list(turn_context.metadata.get("preview_logs") or []),
            attempt=attempt,
            previous_turn_summary=turn_context.previous_turn_summary,
            previous_diff_summary=turn_context.previous_diff_summary,
        )
        if "error" in repair_result:
            message = str(repair_result["error"])
            lowered = message.lower()
            if any(
                marker in lowered
                for marker in (
                    "no file operations",
                    "did not return operations",
                    "unable to inspect",
                    "cannot access",
                    "can't access",
                )
            ):
                return WorkspaceLoopTurnPlan(
                    outcome="needs_context" if context_mode != "full_bundle" else "no_op",
                    assistant_message=message,
                    diagnosis=message,
                    files_read=sorted(turn_context.file_contexts.keys()),
                    failure_class=turn_context.failure_class,
                    failure_signature=turn_context.failure_signature,
                    root_cause_summary=turn_context.root_cause_summary,
                    fix_targets=list(turn_context.implicated_files),
                )
            return WorkspaceLoopTurnPlan(
                outcome="fatal_invalid_response",
                assistant_message=message,
                diagnosis=message,
                files_read=sorted(turn_context.file_contexts.keys()),
                failure_class=turn_context.failure_class,
                failure_signature=turn_context.failure_signature,
                root_cause_summary=turn_context.root_cause_summary,
                fix_targets=list(turn_context.implicated_files),
            )
        operations = list(repair_result["operations"])
        if (not visual_only_patch) or refresh_runtime_artifacts:
            operations = self.service._ensure_runtime_artifact_operations(
                grounded_spec=grounded_spec,
                page_graph=page_graph,
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=operations,
            )
        if not visual_only_patch:
            operations = self.service._ensure_app_level_test_operations(
                page_graph=page_graph,
                role_scope=role_scope,
                entity_contract=None,
                operations=operations,
            )
            operations = self.service._run_pre_apply_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                page_graph=page_graph,
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=operations,
                contract_sync_mode="repair_invariants",
            )
        return WorkspaceLoopTurnPlan(
            outcome="patch_ready",
            assistant_message=str(repair_result.get("assistant_message") or ""),
            diagnosis=str(repair_result.get("assistant_message") or ""),
            operations=operations,
            files_read=sorted(turn_context.file_contexts.keys()),
            failure_class=turn_context.failure_class,
            failure_signature=turn_context.failure_signature,
            root_cause_summary=turn_context.root_cause_summary,
            fix_targets=list(turn_context.implicated_files),
            metadata={"tool_results": list(repair_result.get("tool_results") or [])},
        )

    def run_generation_workspace_loop(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        job: JobRecord,
        request: GenerateRequest,
        draft_source,
        prompt: str,
        grounded_spec,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        plan_result: dict[str, Any],
        entity_contract: dict[str, Any],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        files_read: list[str],
        initial_operations: list[DraftFileOperation],
        initial_assistant_message: str,
        should_stop: Callable[[], bool] | None,
    ) -> WorkspaceLoopResult:
        del creative_direction
        if self.service.workspace_loop_engine is None:
            raise RuntimeError("Workspace loop engine is required for generation mode.")
        visual_only_patch = bool(plan_result.get("visual_only_patch"))
        refresh_runtime_artifacts = bool(plan_result.get("refresh_runtime_artifacts"))
        active_repair_targets = list(plan_result["target_files"])
        fallback_changed_files = [operation.file_path for operation in initial_operations]

        def _execute_checks(changed_files: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            return self._execute_generation_checks(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                draft_source=draft_source,
                changed_files=changed_files,
                fallback_changed_files=fallback_changed_files,
                page_graph=page_graph,
                role_scope=role_scope,
                scope_mode=plan_result["scope_mode"],
            )

        def _plan_turn(
            *,
            attempt: int,
            latest_execution: CheckExecutionRecord,
            latest_preview_details: dict[str, Any],
            validation_snapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            return self._plan_generation_repair_turn(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                prompt=prompt,
                grounded_spec=grounded_spec,
                role_scope=role_scope,
                role_contract=role_contract,
                page_graph=page_graph,
                plan_result=plan_result,
                generation_mode=generation_mode,
                latest_execution=latest_execution,
                latest_preview_details=latest_preview_details,
                attempt=attempt,
                context_mode=context_mode,
                repeated_no_progress=repeated_no_progress,
                active_repair_targets=active_repair_targets,
                last_turn_summary=last_turn_summary,
                latest_diff_summary=latest_diff_summary,
            )

        callbacks = WorkspaceLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self.service.generation_completion.validation_snapshot_from_execution,
            completion_state=self.service.generation_completion.workspace_loop_completion_state,
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=(
                (lambda operations: list(operations))
                if visual_only_patch and not refresh_runtime_artifacts
                else (
                    lambda operations: self.service._run_pre_apply_contract_pass(
                        workspace_id=workspace_id,
                        draft_run_id=draft_run_id,
                        page_graph=page_graph,
                        role_scope=role_scope,
                        generation_mode=generation_mode,
                        operations=list(operations),
                        entity_contract=entity_contract,
                        contract_sync_mode="repair_invariants",
                    )
                )
            ),
            post_apply_stabilize=(
                (lambda _current_workspace_id, _run_id, _current_draft_source, _changed_files: [])
                if visual_only_patch and not refresh_runtime_artifacts
                else (
                    lambda current_workspace_id, _run_id, current_draft_source, _changed_files: self.service.generation_normal_loop.stabilize_draft_contract_from_source(
                        workspace_id=current_workspace_id,
                        draft_source=current_draft_source,
                    )
                )
            ),
            append_event=self.service._append_event,
            append_trace=self.service._append_trace,
            store_report=self.service._store_report,
            stop_if_requested=should_stop,
        )
        return self.service.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=draft_run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            max_attempts=self.service._repair_attempt_limit(generation_mode, request.intent),
            initial_operations=initial_operations,
            initial_assistant_message=initial_assistant_message,
            initial_files_read=files_read,
            initial_changed_files=sorted({operation.file_path for operation in initial_operations}),
            callbacks=callbacks,
        )

    def repair_draft_after_failure(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        prompt: str,
        grounded_spec,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        target_files: list[str],
        file_contexts: dict[str, str],
        build_issues: list[ValidationIssue],
        preview_issue: ValidationIssue | None,
        preview_logs: list[str],
        attempt: int,
        previous_turn_summary: str | None = None,
        previous_diff_summary: str | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        current_target_files = list(target_files)
        draft_source = self.service.workspace_service.draft_source_dir(workspace_id, draft_run_id)
        workspace_tree = (
            self.service.workspace_service.file_tree(workspace_id, run_id=draft_run_id)
            if self.service.workspace_service.draft_exists(workspace_id, draft_run_id)
            else []
        )

        for expanded_context in (False, True):
            allowed_targets = set(current_target_files)
            current_file_contexts = dict(file_contexts)
            tool_results_for_attempt: list[dict[str, object]] = []
            seen_tool_request_signatures: set[str] = set()
            context_reuse_recovery_note: str | None = None
            invalid_patch_feedback_note: str | None = None
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    payload = self.service._generate_structured_with_retry(
                        role="code_edit",
                        schema_name="generation_repair_patch_v1",
                        schema=self.service._repair_schema(),
                        system_prompt=self.service._repair_system_prompt(),
                        user_prompt=(
                            self.service._repair_user_prompt(
                                prompt=prompt,
                                grounded_spec=grounded_spec,
                                role_scope=role_scope,
                                role_contract=role_contract,
                                page_graph=page_graph,
                                scope_mode=scope_mode,
                                target_files=current_target_files,
                                file_contexts=current_file_contexts,
                                build_issues=build_issues,
                                preview_issue=preview_issue,
                                preview_logs=preview_logs,
                                attempt=attempt,
                                expanded_context=expanded_context,
                                previous_turn_summary=previous_turn_summary,
                                previous_diff_summary=previous_diff_summary,
                                tool_results=tool_results_for_attempt,
                            )
                            + (
                                f"\n\n{context_reuse_recovery_note}"
                                if context_reuse_recovery_note
                                else ""
                            )
                            + (
                                f"\n\n{invalid_patch_feedback_note}"
                                if invalid_patch_feedback_note
                                else ""
                            )
                        ),
                    )
                    normalized = self.service._normalize_model_payload(payload["payload"])
                    tool_requests = normalize_tool_requests(normalized.get("tool_requests") or [])
                    outcome_hint = str(normalized.get("outcome") or "").strip().lower()
                    raw_operations = normalized.get("operations")
                    if outcome_hint == "tool_request" or tool_requests:
                        if self._read_request_already_satisfied(tool_requests, current_file_contexts):
                            context_reuse_recovery_note = self._already_available_context_note(tool_requests)
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError("Repair step exhausted the tool-request budget without returning operations.")
                        request_signature = self._tool_request_signature(tool_requests)
                        if request_signature and request_signature in seen_tool_request_signatures:
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError("Repair step exhausted the tool-request budget without returning operations.")
                        requested_targets, executed_tool_results, extra_contexts = self.tool_runtime.execute_tool_requests(
                            workspace_id=workspace_id,
                            draft_run_id=draft_run_id,
                            workspace_tree=workspace_tree,
                            draft_source=draft_source,
                            tool_requests=tool_requests,
                            fallback_targets=current_target_files,
                            execute_checks=lambda requested_changed_files, mode: self._execute_generation_checks(
                                workspace_id=workspace_id,
                                draft_run_id=draft_run_id,
                                draft_source=draft_source,
                                changed_files=requested_changed_files,
                                fallback_changed_files=current_target_files,
                                page_graph=page_graph,
                                role_scope=role_scope,
                                scope_mode=scope_mode,
                                mode=mode,
                            ),
                            command_timeout_seconds=self.COMMAND_TIMEOUT_SECONDS,
                        )
                        for path in requested_targets:
                            if path not in current_target_files:
                                current_target_files.append(path)
                        current_file_contexts.update(extra_contexts)
                        allowed_targets = set(current_target_files)
                        tool_results_for_attempt.extend(executed_tool_results)
                        if request_signature:
                            seen_tool_request_signatures.add(request_signature)
                        if tool_round < self.MAX_TOOL_ROUNDS and (requested_targets or executed_tool_results):
                            continue
                        raise ValueError("Repair step exhausted the tool-request budget without returning operations.")
                    if not isinstance(raw_operations, list):
                        raise ValueError("Repair step did not return operations.")
                    operations = self.service._sanitize_draft_operations(
                        [DraftFileOperation.model_validate(item) for item in raw_operations]
                    )
                    operations = [
                        operation
                        for operation in operations
                        if not self._is_read_only_repair_surface(operation.file_path)
                    ]
                    if not operations and self.service._should_retry_repair_with_expanded_context(
                        str(normalized.get("diagnosis") or normalized.get("assistant_message") or "")
                    ):
                        raise ValueError(
                            str(normalized.get("diagnosis") or normalized.get("assistant_message") or "Repair step did not return operations.")
                        )
                    invalid = [
                        operation.file_path
                        for operation in operations
                        if operation.file_path not in allowed_targets
                        or (operation.operation in {"create", "replace"} and operation.content is None)
                    ]
                    if invalid:
                        expanded_targets = self.service._expand_repair_targets_for_safe_companions(
                            target_files=current_target_files,
                            invalid_paths=invalid,
                            build_issues=build_issues,
                        )
                        if expanded_targets is None:
                            raise ValueError(f"Repair touched files outside the planned scope: {', '.join(invalid[:5])}")
                        allowed_targets = set(expanded_targets)
                        residual_invalid = [
                            operation.file_path
                            for operation in operations
                            if operation.file_path not in allowed_targets
                            or (operation.operation in {"create", "replace"} and operation.content is None)
                        ]
                        if residual_invalid:
                            raise ValueError(
                                f"Repair touched files outside the planned scope: {', '.join(residual_invalid[:5])}"
                            )
                        current_target_files = expanded_targets
                    try:
                        self.service._validate_targeted_operations(
                            stage_name="repair",
                            target_files=current_target_files,
                            operations=operations,
                        )
                    except Exception as exc:
                        if tool_round < self.MAX_TOOL_ROUNDS and self._is_retryable_patch_validation_error(str(exc)):
                            invalid_patch_feedback_note = self._invalid_patch_feedback(str(exc))
                            continue
                        raise
                    return {
                        "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                        "operations": operations,
                        "model": payload["model"],
                        "tool_results": list(tool_results_for_attempt),
                    }
            except Exception as exc:
                last_error = exc
                if expanded_context or not self.service._should_retry_repair_with_expanded_context(str(exc)):
                    break
        return {"error": f"Automatic repair step failed: {last_error}"}
