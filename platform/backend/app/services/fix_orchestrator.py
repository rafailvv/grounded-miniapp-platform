from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.models.common import GenerationMode
from app.models.domain import FixScopeEntry, GenerateRequest, JobRecord, new_id
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.engine import (
    ArtifactRecorder,
    CompactionService,
    ContextBudgetManager,
    PromptStateManager,
    SessionEngine,
    TaskRouter,
)
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.modules.miniapp_agent_loop.engine import WorkspaceLoopEngine
from app.modules.miniapp_agent_loop.fix_prompt_builder import FixPromptBuilder
from app.modules.miniapp_agent_loop.fix_scope_builder import FixScopeBuilder
from app.modules.miniapp_agent_loop.fix_turn_builder import FixTurnBuilder
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext
from app.modules.miniapp_fix_runtime import (
    FixClassificationRuntime,
    FixContextRuntime,
    FixEntryRuntime,
    FixExecutionRuntime,
    FixPatchingRuntime,
    FixPromptRuntime,
    FixReportingRuntime,
    FixScopeRuntime,
)
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class FixOrchestrator:
    MAX_ATTEMPTS = 12
    MAX_SCOPE_EXPANSIONS = 4
    MAX_CONTEXT_CHARS = 20000
    MAX_CONTEXT_CHARS_EXPANDED = 48000

    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        check_runner: CheckRunner,
        preview_service: PreviewService,
        runtime_manager: PreviewRuntimeManager,
        openrouter_client: OpenRouterClient,
        workspace_log_service: WorkspaceLogService,
        session_engine: SessionEngine | None = None,
        task_router: TaskRouter | None = None,
        context_budget_manager: ContextBudgetManager | None = None,
        prompt_state_manager: PromptStateManager | None = None,
        compaction_service: CompactionService | None = None,
        artifact_recorder: ArtifactRecorder | None = None,
        generation_service: "GenerationService | None" = None,
        workspace_loop_engine: WorkspaceLoopEngine | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.check_runner = check_runner
        self.preview_service = preview_service
        self.runtime_manager = runtime_manager
        self.openrouter_client = openrouter_client
        self.workspace_log_service = workspace_log_service
        self.session_engine = session_engine
        self.task_router = task_router
        self.context_budget_manager = context_budget_manager
        self.prompt_state_manager = prompt_state_manager
        self.compaction_service = compaction_service
        self.artifact_recorder = artifact_recorder
        self.generation_service = generation_service
        self.workspace_loop_engine = workspace_loop_engine
        self.fix_prompt_builder = FixPromptBuilder()
        self.fix_scope_builder = FixScopeBuilder(file_exists=self._file_exists)
        self.fix_turn_builder = FixTurnBuilder(prompt_builder=self.fix_prompt_builder)
        self.fix_entry = FixEntryRuntime(self)
        self.fix_execution = FixExecutionRuntime(self)
        self.fix_classification = FixClassificationRuntime(self)
        self.fix_context = FixContextRuntime(self)
        self.fix_scope = FixScopeRuntime(self)
        self.fix_patching = FixPatchingRuntime(self)
        self.fix_prompts = FixPromptRuntime(self)
        self.fix_reporting = FixReportingRuntime(self)

    def generate(
        self,
        workspace_id: str,
        request: GenerateRequest,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> JobRecord:
        started_at = time.perf_counter()
        workspace = self.workspace_service.get_workspace(workspace_id)
        run_id = request.linked_run_id or new_id("run")
        effective_mode = request.generation_mode if request.generation_mode == GenerationMode.QUALITY else GenerationMode.BALANCED
        stable_prefix = (
            "You are repairing a grounded mini-app workspace. "
            "Use a bounded failure packet, keep scope narrow, and avoid rereading unchanged files."
        )
        cache_key = self._prompt_cache_key_seed(workspace_id=workspace_id, run_id=run_id, prompt=request.prompt)
        if self.session_engine is not None:
            self.session_engine.bootstrap(
                workspace_id=workspace_id,
                prompt=request.prompt,
                generation_mode=effective_mode.value,
                model_profile=request.model_profile,
                run_mode="fix",
                stable_prefix=stable_prefix,
                cache_key=cache_key,
            )
            project_memory = self.session_engine.select_project_memory(
                workspace_id=workspace_id,
                prompt=request.prompt,
                generation_mode=effective_mode.value,
                run_mode="fix",
            )
            self.store.upsert("reports", f"project_memory_context:{workspace_id}", project_memory)
        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode="fix",
            status="running",
            generation_mode=effective_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace.current_revision_id,
            fidelity="balanced_app",
            llm_enabled=self.openrouter_client.enabled,
            llm_provider="openai" if self.openrouter_client.enabled else None,
            model_profile=request.model_profile,
            linked_run_id=run_id,
            error_context=request.error_context,
            failure_class=self._classify_failure_text(request.error_context.raw_error if request.error_context else request.prompt),
            root_cause_summary=(request.error_context.raw_error.strip() if request.error_context and request.error_context.raw_error.strip() else None),
            current_fix_phase="collecting_state",
        )
        source_run_id = str(request.resume_from_run_id or "").strip()
        cloned_source_draft = False
        repair_base = "current_source"
        safe_source_repair = False
        if source_run_id and source_run_id != run_id and self.workspace_service.draft_exists(workspace_id, source_run_id):
            if self._should_resume_failed_draft(workspace_id, source_run_id, request):
                self.workspace_service.clone_draft(workspace_id, source_run_id, run_id)
                cloned_source_draft = True
                repair_base = f"draft:{source_run_id}"
            else:
                safe_source_repair = True
                repair_base = "current_source_safe_reset"
        reuse_existing_draft = cloned_source_draft or bool(request.linked_run_id and self.workspace_service.draft_exists(workspace_id, run_id))
        self._clear_reports(workspace_id, preserve_generation_state=reuse_existing_draft)
        if not reuse_existing_draft:
            self._clear_trace(workspace_id)
        self._save_job(job)
        draft_source = self.workspace_service.ensure_draft(workspace_id, run_id)
        self._append_event(job, "job_started", "Fix run started.")
        if self.session_engine is not None:
            self.session_engine.record_phase(
                workspace_id=workspace_id,
                phase="intent",
                generation_mode=effective_mode.value,
                model_profile=request.model_profile,
                run_mode="fix",
                details={"failure_class": job.failure_class, "resume_from_run_id": request.resume_from_run_id},
            )
        self._append_trace(
            workspace_id,
            "fix",
            "Fix orchestrator initialized.",
            {
                "run_id": run_id,
                "reused_existing_draft": reuse_existing_draft,
                "source_run_id": source_run_id or None,
                "cloned_source_draft": cloned_source_draft,
                "repair_base": repair_base,
            },
        )
        if cloned_source_draft:
            self._append_trace(workspace_id, "draft_reused", "Fix cloned the previous failed generation draft and continued from it.", {"run_id": run_id, "source_run_id": source_run_id})
        elif safe_source_repair:
            self._append_trace(
                workspace_id,
                "draft_reused",
                "Fix detected a regressed failed draft and restarted repair from the current source snapshot.",
                {"run_id": run_id, "source_run_id": source_run_id, "repair_base": repair_base},
            )
        elif reuse_existing_draft:
            self._append_trace(workspace_id, "draft_reused", "Fix reused the existing generation draft instead of resetting it to the current source revision.", {"run_id": run_id})
        job.repair_base = repair_base
        if self.workspace_loop_engine is None:
            raise RuntimeError("Workspace loop engine is required for fix mode.")
        return self._generate_with_workspace_loop(
            workspace_id=workspace_id,
            run_id=run_id,
            request=request,
            job=job,
            draft_source=draft_source,
            started_at=started_at,
            role_scope=list(request.target_role_scope),
            effective_mode=effective_mode,
            memory_context=(self.store.get("reports", f"project_memory_context:{workspace_id}") or {}).get("summary"),
            should_stop=should_stop,
        )

    def _generate_with_workspace_loop(self, **kwargs: Any) -> JobRecord:
        return self.fix_entry.generate_with_workspace_loop(**kwargs)

    def _execute_exact_checks(self, **kwargs: Any):
        return self.fix_execution.execute_exact_checks(**kwargs)

    def _execute_final_checks(self, **kwargs: Any):
        return self.fix_execution.execute_final_checks(**kwargs)

    @staticmethod
    def _final_check_changed_files(latest_apply_result: dict[str, Any] | None, fix_turn: FixTurnContext, scope_entries):
        return FixExecutionRuntime.final_check_changed_files(latest_apply_result, fix_turn, scope_entries)

    def _role_scope_for_fix_request(self, workspace_id: str, run_id: str, request: GenerateRequest) -> list[str]:
        return self.fix_execution.role_scope_for_fix_request(workspace_id, run_id, request)

    def _finalize_loop_job(self, **kwargs: Any) -> JobRecord:
        return self.fix_execution.finalize_loop_job(**kwargs)

    def _build_fix_case(self, **kwargs: Any) -> FixTurnContext:
        workspace_id = kwargs["workspace_id"]
        memory_context = kwargs.pop(
            "memory_context",
            (self.store.get("reports", f"project_memory_context:{workspace_id}") or {}).get("summary"),
        )
        return self.fix_turn_builder.build_turn_context(
            memory_context=memory_context,
            augment_failure_evidence=self._augment_failure_evidence_from_test_results,
            implicated_files=self._implicated_files,
            specialized_failure_class=self._specialized_failure_class,
            classify_failure_text=lambda text: self._classify_failure_text(text) or CheckRunner.classify_failure(kwargs["check_execution"].results) or "build/runtime",
            root_cause_summary=self._root_cause_summary,
            failure_signature=self._failure_signature,
            error_excerpt=self._error_excerpt,
            first_failing_command=self._first_failing_command,
            write_scope=lambda ws_id, current_run_id, implicated, failure_class, existing: self._build_write_scope(ws_id, current_run_id, implicated, failure_class, existing),
            **kwargs,
        )

    @staticmethod
    def _prefer_failure_class(existing: str | None, candidate: str | None) -> str | None:
        return FixClassificationRuntime.prefer_failure_class(existing, candidate)

    def _build_repair_packet(self, **kwargs: Any) -> FixPromptContext:
        return self.fix_turn_builder.build_prompt_context(
            collect_file_contexts=self._collect_file_contexts,
            merge_additional_context_paths=self._merge_additional_context_paths,
            current_diff_summary=self._current_diff_summary,
            **kwargs,
        )

    def _should_resume_failed_draft(self, workspace_id: str, source_run_id: str, request: GenerateRequest) -> bool:
        if not self.workspace_service.draft_exists(workspace_id, source_run_id):
            return False
        return not self._draft_has_contract_regression(workspace_id, source_run_id, request)

    def _draft_has_contract_regression(self, workspace_id: str, run_id: str, request: GenerateRequest) -> bool:
        raw_error = str(request.error_context.raw_error if request.error_context else request.prompt or "")
        if "/api/" not in raw_error and "generated_app_python_tests" not in raw_error.lower():
            return False
        route_dir = self.workspace_service.draft_source_dir(workspace_id, run_id) / "miniapp/app/routes"
        if not route_dir.exists():
            return False
        for route_file in sorted(route_dir.glob("*.py")):
            try:
                content = route_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if "/api/submissions/{table}" in content.lower():
                return True
            if route_file.stem == "bookingrequests" and self.generation_service is not None:
                if self.generation_service.generation_contract_schema._needs_canonical_bookingrequests_route_repair(content):
                    return True
        return False

    def _read_only_surfaces(self) -> list[str]:
        return self.fix_prompts.read_only_surfaces()

    def _expected_contract_snapshot(self, fix_turn: FixTurnContext) -> dict[str, Any]:
        return self.fix_prompts.expected_contract_snapshot(fix_turn)

    @staticmethod
    def _repair_context_mode(fix_turn: FixTurnContext, repeated_signature_without_progress: int) -> str:
        return FixPromptRuntime.repair_context_mode(fix_turn, repeated_signature_without_progress)

    @staticmethod
    def _needs_full_context_first(fix_turn: FixTurnContext) -> bool:
        return FixPromptRuntime.needs_full_context_first(fix_turn)

    @staticmethod
    def _previous_attempt_summary(fix_turn: FixTurnContext) -> str | None:
        return FixPromptRuntime.previous_attempt_summary(fix_turn)

    def _current_diff_summary(self, workspace_id: str, run_id: str) -> str | None:
        return self.fix_context.current_diff_summary(workspace_id, run_id)

    @staticmethod
    def _normalized_critical_issues(results, fix_turn: FixTurnContext | None = None) -> list[dict[str, Any]]:
        return FixPromptRuntime.normalized_critical_issues(results, fix_turn=fix_turn)

    def _specialized_failure_class(self, **kwargs: Any) -> str | None:
        return self.fix_classification.specialized_failure_class(**kwargs)

    def _repair_outcome_from_response(self, **kwargs: Any):
        return self.fix_patching.repair_outcome_from_response(**kwargs)

    def _plan_patch(self, **kwargs: Any) -> dict[str, Any]:
        return self.fix_patching.plan_patch(**kwargs)

    def _coerce_operations(self, raw_operations, scope_entries, fix_turn, scope_expansions):
        return self.fix_patching.coerce_operations(raw_operations, scope_entries, fix_turn, scope_expansions)

    @staticmethod
    def _is_read_only_generated_surface(file_path: str) -> bool:
        return FixPatchingRuntime.is_read_only_generated_surface(file_path)

    @staticmethod
    def _can_expand_for_file(candidate: str, implicated_files: list[str]) -> bool:
        return FixPatchingRuntime.can_expand_for_file(candidate, implicated_files)

    def _build_write_scope(self, workspace_id: str, run_id: str, implicated_files: list[str], failure_class: str, existing_scope: list[FixScopeEntry]):
        return self.fix_scope.build_write_scope(workspace_id, run_id, implicated_files, failure_class, existing_scope)

    def _structural_scope_bundle(self, workspace_id: str, run_id: str, implicated_files: list[str], failure_class: str) -> list[str]:
        return self.fix_scope.structural_scope_bundle(workspace_id, run_id, implicated_files, failure_class)

    def _feature_scope_bundle(self, workspace_id: str, run_id: str, implicated_files: list[str]) -> list[str]:
        return self.fix_scope.feature_scope_bundle(workspace_id, run_id, implicated_files)

    @staticmethod
    def _merge_scope(current_scope, next_scope, scope_expansions):
        return FixScopeRuntime.merge_scope(current_scope, next_scope, scope_expansions)

    def _collect_file_contexts(self, workspace_id: str, run_id: str, scope_entries, *, fix_turn: FixTurnContext | None = None, budget_override: int | None = None, full_files: bool = False):
        return self.fix_context.collect_file_contexts(workspace_id, run_id, scope_entries, fix_turn=fix_turn, budget_override=budget_override, full_files=full_files)

    def _page_graph_for_run(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        return self.fix_scope.page_graph_for_run(workspace_id, run_id)

    @staticmethod
    def _looks_like_context_refusal(diagnosis: str) -> bool:
        return FixContextRuntime.looks_like_context_refusal(diagnosis)

    @staticmethod
    def _planned_target_paths(llm_result: dict[str, Any]) -> list[str]:
        return FixContextRuntime.planned_target_paths(llm_result)

    def _merge_additional_context_paths(self, workspace_id: str, run_id: str, contexts: dict[str, str], additional_paths: list[str], *, budget_override: int | None = None):
        return self.fix_context.merge_additional_context_paths(workspace_id, run_id, contexts, additional_paths, budget_override=budget_override)

    @staticmethod
    def _operations_missing_content(raw_operations: list[Any]) -> list[str]:
        return FixPatchingRuntime.operations_missing_content(raw_operations)

    @staticmethod
    def _should_retry_patch_validation(message: str) -> bool:
        return FixPatchingRuntime.should_retry_patch_validation(message)

    @staticmethod
    def _backend_framework_validation_error(operation) -> str | None:
        return FixPatchingRuntime.backend_framework_validation_error(operation)

    @staticmethod
    def _repair_support_files(fix_turn: FixTurnContext | None) -> list[str]:
        if fix_turn is None:
            return []
        connectivity_codes = {"connectivity.missing_ui_loading_state", "connectivity.missing_ui_error_state", "connectivity.missing_backend_route"}
        evidence = "\n".join([str(fix_turn.failure_class or ""), str(fix_turn.root_cause_summary or ""), str(fix_turn.exact_error_excerpt or ""), *[result.details or "" for result in fix_turn.executed_checks], *[line for result in fix_turn.executed_checks for line in result.logs]]).lower()
        if any(code in evidence for code in connectivity_codes):
            return ["artifacts/generated_app_graph.json"]
        return []

    def _implicated_files(self, workspace_id: str, run_id: str, text: str, existing_scope):
        return self.fix_classification.implicated_files(workspace_id, run_id, text, existing_scope)

    def _root_cause_summary(self, results, preview_details, raw_error: str) -> str:
        return self.fix_classification.root_cause_summary(results, preview_details, raw_error)

    @staticmethod
    def _allow_missing_scope_path(file_path: str) -> bool:
        return FixScopeRuntime.allow_missing_scope_path(file_path)

    @staticmethod
    def _scope_can_still_expand(existing_scope, next_scope) -> bool:
        return FixScopeRuntime.scope_can_still_expand(existing_scope, next_scope)

    @staticmethod
    def _augment_failure_evidence_from_test_results(base_text: str, results) -> str:
        return FixClassificationRuntime.augment_failure_evidence_from_test_results(base_text, results)

    def _test_failure_implicated_paths(self, text: str) -> list[str]:
        return self.fix_classification.test_failure_implicated_paths(text)

    @staticmethod
    def _page_triplet_candidates_for_route(route_path: str) -> list[str]:
        return FixClassificationRuntime.page_triplet_candidates_for_route(route_path)

    @staticmethod
    def _failure_signature(failure_class: str, root_cause_summary: str) -> str:
        return FixClassificationRuntime.failure_signature(failure_class, root_cause_summary)

    @staticmethod
    def _error_excerpt(results, preview_details, raw_error: str) -> str:
        return FixClassificationRuntime.error_excerpt(results, preview_details, raw_error)

    @staticmethod
    def _is_fix_success(results, preview_details) -> bool:
        return FixExecutionRuntime.is_fix_success(results, preview_details)

    @classmethod
    def _completion_state_from_results(cls, results, preview_details, *, validation_snapshot):
        return FixExecutionRuntime.completion_state_from_results(results, preview_details, validation_snapshot=validation_snapshot)

    @staticmethod
    def _remaining_issues_from_results(*, app_test_failures, validation_snapshot, preview_details):
        return FixExecutionRuntime.remaining_issues_from_results(
            app_test_failures=app_test_failures,
            validation_snapshot=validation_snapshot,
            preview_details=preview_details,
        )

    @staticmethod
    def _first_failing_command(results) -> str | None:
        return FixClassificationRuntime.first_failing_command(results)

    @staticmethod
    def _first_failing_exit_code(results) -> int | None:
        return FixClassificationRuntime.first_failing_exit_code(results)

    @staticmethod
    def _classify_failure_text(text: str) -> str:
        return FixClassificationRuntime.classify_failure_text(text)

    def _validation_snapshot_from_execution(self, execution) -> Any:
        return self.fix_execution.validation_snapshot_from_execution(execution)

    def _finalize_job(self, job: JobRecord, **kwargs: Any) -> JobRecord:
        return self.fix_execution.finalize_job(job, **kwargs)

    @staticmethod
    def _prompt_cache_key_seed(*, workspace_id: str, run_id: str, prompt: str) -> str:
        return hashlib.sha256(f"{workspace_id}:{run_id}:{prompt}".encode("utf-8")).hexdigest()

    def _repair_schema(self) -> dict[str, Any]:
        return self.fix_prompts.repair_schema()

    def _repair_system_prompt(self) -> str:
        return self.fix_prompts.repair_system_prompt()

    def _repair_user_prompt(self, repair_packet: FixPromptContext, *, repair_feedback: str | None = None) -> str:
        return self.fix_prompts.repair_user_prompt(repair_packet, repair_feedback=repair_feedback)

    @staticmethod
    def _allow_test_file_writes_for_failure(failure_class: str | None) -> bool:
        return FixPatchingRuntime.allow_test_file_writes_for_failure(failure_class)

    @classmethod
    def _allow_test_file_writes(cls, fix_turn: FixTurnContext) -> bool:
        return FixPatchingRuntime.allow_test_file_writes(fix_turn)

    @staticmethod
    def _prompt_cache_key(repair_packet: FixPromptContext) -> str:
        return FixPromptRuntime.prompt_cache_key(repair_packet)

    @staticmethod
    def _merge_cache_stats(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        return FixPatchingRuntime.merge_cache_stats(current, incoming)

    def _resolve_frontend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        return self.fix_context.resolve_frontend_module(workspace_id, run_id, module_path)

    def _resolve_backend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        return self.fix_context.resolve_backend_module(workspace_id, run_id, module_path)

    def _file_exists(self, workspace_id: str, run_id: str, relative_path: str) -> bool:
        return self.fix_context.file_exists(workspace_id, run_id, relative_path)

    @staticmethod
    def _diff_summary(diff_text: str) -> str:
        return FixContextRuntime.diff_summary(diff_text)

    def _append_iteration_report(self, workspace_id: str, iteration) -> None:
        self.fix_reporting.append_iteration_report(workspace_id, iteration)

    def _clear_reports(self, workspace_id: str, *, preserve_generation_state: bool = False) -> None:
        self.fix_reporting.clear_reports(workspace_id, preserve_generation_state=preserve_generation_state)

    def _save_job(self, job: JobRecord) -> None:
        self.fix_reporting.save_job(job)

    def _store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.fix_reporting.store_report(key, payload)

    def _clear_trace(self, workspace_id: str) -> None:
        self.fix_reporting.clear_trace(workspace_id)

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        self.fix_reporting.append_trace(workspace_id, stage, message, payload)

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.fix_reporting.append_event(job, event_type, message, details)

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any]) -> None:
        self.fix_reporting.sync_run_progress(job, event_type, message, details)

    @staticmethod
    def _run_progress_for_event(event_type: str) -> tuple[str, int]:
        return FixReportingRuntime.run_progress_for_event(event_type)
