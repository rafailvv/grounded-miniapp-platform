from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    FixAttemptOutcome,
    FixAttemptRecord,
    FixScopeEntry,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    RunIterationOperation,
    RunIterationRecord,
    ValidationSnapshot,
    new_id,
    utc_now,
)
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
from app.modules.miniapp_agent_loop.types import (
    WorkspaceLoopCallbacks,
    WorkspaceLoopResult,
    WorkspaceLoopTurnPlan,
)
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class FixOrchestrator:
    MAX_ATTEMPTS = 12
    MAX_SCOPE_EXPANSIONS = 4
    MAX_CONTEXT_CHARS = 12000
    MAX_CONTEXT_CHARS_EXPANDED = 32000

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
        if source_run_id and source_run_id != run_id and self.workspace_service.draft_exists(workspace_id, source_run_id):
            self.workspace_service.clone_draft(workspace_id, source_run_id, run_id)
            cloned_source_draft = True
        reuse_existing_draft = cloned_source_draft or bool(
            request.linked_run_id and self.workspace_service.draft_exists(workspace_id, run_id)
        )
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
                details={
                    "failure_class": job.failure_class,
                    "resume_from_run_id": request.resume_from_run_id,
                },
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
            },
        )
        if cloned_source_draft:
            self._append_trace(
                workspace_id,
                "draft_reused",
                "Fix cloned the previous failed generation draft and continued from it.",
                {"run_id": run_id, "source_run_id": source_run_id},
            )
        elif reuse_existing_draft:
            self._append_trace(
                workspace_id,
                "draft_reused",
                "Fix reused the existing generation draft instead of resetting it to the current source revision.",
                {"run_id": run_id},
            )

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

    def _generate_with_workspace_loop(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        draft_source: Path,
        started_at: float,
        role_scope: list[str],
        effective_mode: GenerationMode,
        memory_context: str | None,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord:
        scope_entries: list[FixScopeEntry] = []
        scope_expansions: list[dict[str, Any]] = []

        def _execute_checks(changed_files: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            return self._execute_exact_checks(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
                changed_files=changed_files or ["miniapp"],
            )

        def _plan_turn(
            *,
            attempt: int,
            latest_execution: CheckExecutionRecord,
            latest_preview_details: dict[str, Any],
            validation_snapshot: ValidationSnapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            fix_turn = self.fix_turn_builder.build_turn_context(
                workspace_id=workspace_id,
                run_id=run_id,
                attempt=attempt,
                request=request,
                check_execution=latest_execution,
                preview_details=latest_preview_details,
                prior_attempts=[],
                existing_scope=scope_entries,
                memory_context=memory_context,
                augment_failure_evidence=self._augment_failure_evidence_from_test_results,
                implicated_files=self._implicated_files,
                specialized_failure_class=self._specialized_failure_class,
                classify_failure_text=lambda text: self._classify_failure_text(text) or CheckRunner.classify_failure(latest_execution.results) or "build/runtime",
                root_cause_summary=self._root_cause_summary,
                failure_signature=self._failure_signature,
                error_excerpt=self._error_excerpt,
                first_failing_command=self._first_failing_command,
                write_scope=lambda ws_id, current_run_id, implicated, failure_class, existing: self._build_write_scope(
                    ws_id,
                    current_run_id,
                    implicated,
                    failure_class,
                    existing,
                ),
            )
            next_scope = self._build_write_scope(
                workspace_id,
                run_id,
                fix_turn.implicated_files,
                fix_turn.failure_class or "build/runtime",
                scope_entries,
            )
            scope_entries[:] = self.fix_scope_builder.merge_scope(
                scope_entries,
                next_scope,
                scope_expansions,
                max_scope_expansions=self.MAX_SCOPE_EXPANSIONS,
            )
            job.failure_class = self._prefer_failure_class(job.failure_class, fix_turn.failure_class)
            job.failure_signature = fix_turn.failure_signature
            job.root_cause_summary = fix_turn.root_cause_summary
            job.fix_targets = list(fix_turn.implicated_files)
            job.validation_snapshot = self._validation_snapshot_from_execution(latest_execution)
            self._store_report(f"fix_case:{workspace_id}", {
                "workspace_id": fix_turn.workspace_id,
                "run_id": fix_turn.run_id,
                "attempt": fix_turn.attempt,
                "failure_class": fix_turn.failure_class,
                "failure_signature": fix_turn.failure_signature,
                "root_cause_summary": fix_turn.root_cause_summary,
                "implicated_files": list(fix_turn.implicated_files),
                "write_scope": [entry.model_dump(mode="json") for entry in fix_turn.write_scope],
            })
            self._append_event(
                job,
                "triage_completed",
                fix_turn.root_cause_summary or "Fix evidence packet prepared.",
                {
                    "attempt": attempt,
                    "failure_class": fix_turn.failure_class,
                    "failure_signature": fix_turn.failure_signature,
                    "implicated_files": fix_turn.implicated_files,
                    "context_mode": context_mode,
                    "repeated_no_progress": repeated_no_progress,
                },
            )
            deterministic_operations = self._deterministic_contract_repair_operations(
                workspace_id=workspace_id,
                run_id=run_id,
                fix_turn=fix_turn,
                scope_entries=scope_entries,
                generation_mode=effective_mode,
            )
            if deterministic_operations:
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message="Applied deterministic contract repair before model editing.",
                    diagnosis="Applied deterministic contract repair before model editing.",
                    operations=deterministic_operations,
                    files_read=[entry.file_path for entry in scope_entries],
                    failure_class=fix_turn.failure_class,
                    failure_signature=fix_turn.failure_signature,
                    root_cause_summary=fix_turn.root_cause_summary,
                    fix_targets=list(fix_turn.implicated_files),
                    metadata={"source": "deterministic_contract_repair"},
                )

            prompt_context = self.fix_turn_builder.build_prompt_context(
                workspace_id=workspace_id,
                run_id=run_id,
                fix_turn=fix_turn,
                scope_entries=scope_entries,
                context_mode=str(context_mode),
                collect_file_contexts=self._collect_file_contexts,
                merge_additional_context_paths=self._merge_additional_context_paths,
                deterministic_contract_seed_paths=self._deterministic_contract_seed_paths,
                current_diff_summary=self._current_diff_summary,
            )
            if last_turn_summary or latest_diff_summary:
                prompt_context.previous_attempt_summary = last_turn_summary or prompt_context.previous_attempt_summary
                prompt_context.previous_diff_summary = latest_diff_summary or prompt_context.previous_diff_summary
            self._append_event(
                job,
                "repair_planned",
                "Prepared repair packet for the current failure bundle.",
                {
                    "attempt": attempt,
                    "scope": [entry.file_path for entry in scope_entries],
                    "context_mode": prompt_context.context_mode,
                },
            )
            llm_result = self._plan_patch(job=job, prompt_context=prompt_context)
            repair_outcome = self._repair_outcome_from_response(
                llm_result=llm_result,
                prompt_context=prompt_context,
                fix_turn=fix_turn,
                scope_expansions=scope_expansions,
            )
            mapped_outcome = repair_outcome.outcome
            if mapped_outcome == "fatal_invalid_response":
                return WorkspaceLoopTurnPlan(
                    outcome="fatal_invalid_response",
                    assistant_message=str(repair_outcome.diagnosis or ""),
                    diagnosis=repair_outcome.validation_error or repair_outcome.diagnosis,
                    files_read=list(prompt_context.file_contexts.keys()),
                    failure_class=fix_turn.failure_class,
                    failure_signature=fix_turn.failure_signature,
                    root_cause_summary=fix_turn.root_cause_summary,
                    fix_targets=list(fix_turn.implicated_files),
                    metadata={"raw_response": repair_outcome.raw_response},
                )
            return WorkspaceLoopTurnPlan(
                outcome="patch_ready" if mapped_outcome == "patch_ready" else ("needs_context" if mapped_outcome == "needs_more_context" else "no_op"),
                assistant_message=str(repair_outcome.diagnosis or ""),
                diagnosis=repair_outcome.validation_error or repair_outcome.diagnosis,
                operations=list(repair_outcome.operations),
                files_read=list(prompt_context.file_contexts.keys()),
                failure_class=fix_turn.failure_class,
                failure_signature=fix_turn.failure_signature,
                root_cause_summary=fix_turn.root_cause_summary,
                fix_targets=list(fix_turn.implicated_files),
                expected_verification=repair_outcome.expected_verification,
                rationale_by_file=dict(repair_outcome.rationale_by_file),
                metadata={"planned_targets": list(repair_outcome.planned_targets)},
            )

        callbacks = WorkspaceLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self._validation_snapshot_from_execution,
            completion_state=self._completion_state_from_results,
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=lambda operations: self.generation_service._run_pre_apply_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=run_id,
                page_graph=self._page_graph_for_deterministic_repair(workspace_id, run_id),
                role_scope=self._role_scope_for_fix_request(workspace_id, run_id, request),
                generation_mode=effective_mode,
                operations=list(operations),
            )
            if self.generation_service is not None
            else list(operations),
            append_event=self._append_event,
            append_trace=self._append_trace,
            store_report=self._store_report,
            stop_if_requested=should_stop,
        )
        loop_result = self.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=self._role_scope_for_fix_request(workspace_id, run_id, request),
            generation_mode=effective_mode,
            max_attempts=self.MAX_ATTEMPTS,
            initial_operations=[],
            initial_assistant_message="Fix workspace loop initialized.",
            initial_files_read=[],
            initial_changed_files=["miniapp"],
            callbacks=callbacks,
        )
        return self._finalize_loop_job(
            job=job,
            loop_result=loop_result,
            scope_expansions=scope_expansions,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )

    def _execute_exact_checks(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        changed_files: list[str],
    ) -> tuple[CheckExecutionRecord, dict[str, Any]]:
        self._append_event(job, "frontend_build_started", "Running exact frontend/build verification.")
        self._append_event(job, "backend_compile_started", "Running exact miniapp compile verification.")
        execution = self.check_runner.run(
            workspace_id=workspace_id,
            run_id=run_id,
            source_dir=draft_source,
            changed_files=changed_files,
            preview_run_id=run_id,
            scope_mode="fix_agentic",
        )
        results = [item for item in execution.results if item.name not in {"preview_boot_smoke", "preview_connectivity_smoke"}]
        preview_details: dict[str, Any] = {"status": "skipped", "containers": [], "container_logs": {}, "logs": [], "last_error": None}
        static_failure = any(item.status == "failed" for item in results if item.name in {"schema_validators", "connectivity_validators", "changed_files_static"})
        if not static_failure:
            preview_result = RunCheckResult(
                name="preview_boot_smoke",
                status="skipped",
                details="Preview rebuild is deferred during fix verification after static/build checks passed.",
                command="preview deferred during fix",
                exit_code=0,
                logs=[],
            )
            results.append(preview_result)
            results.append(
                RunCheckResult(
                    name="preview_connectivity_smoke",
                    status="skipped",
                    details="Preview connectivity smoke is deferred during fix verification.",
                    command="preview deferred during fix",
                    exit_code=0,
                    logs=[],
                )
            )
        else:
            preview = self.preview_service.get(workspace_id)
            container_logs = {}
            containers: list[dict[str, Any]] = []
            if preview.proxy_port is not None:
                log_source = (
                    self.workspace_service.draft_source_dir(workspace_id, preview.draft_run_id)
                    if preview.draft_run_id and self.workspace_service.draft_exists(workspace_id, preview.draft_run_id)
                    else self.workspace_service.source_dir(workspace_id)
                )
                container_logs = self.runtime_manager.collect_container_logs(workspace_id, log_source, preview.proxy_port)
                containers = self.runtime_manager.inspect_containers(workspace_id, log_source, preview.proxy_port)
            results.append(
                RunCheckResult(
                    name="preview_boot_smoke",
                    status="skipped",
                    details="Preview rebuild was skipped because compile/build checks are still failing.",
                    command="docker compose up -d --build",
                    logs=["Preview rebuild was skipped because compile/build checks are still failing."],
                )
            )
            results.append(
                RunCheckResult(
                    name="preview_connectivity_smoke",
                    status="skipped",
                    details="Preview route smoke was skipped because compile/build checks are still failing.",
                    command="preview route smoke (current session)",
                    logs=["Preview route smoke was skipped because compile/build checks are still failing."],
                )
            )
            preview_details = {
                "status": preview.status,
                "stage": preview.stage,
                "progress_percent": preview.progress_percent,
                "logs": list(preview.logs),
                "last_error": preview.last_error,
                "containers": containers,
                "container_logs": container_logs,
            }
        execution.results = results
        execution.completed_at = utc_now()
        return execution, preview_details

    def _execute_final_checks(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        changed_files: list[str],
    ) -> tuple[CheckExecutionRecord, dict[str, Any]]:
        self._append_event(job, "final_checks_started", "Running final full verification before completing fix.")
        execution = self.check_runner.run(
            workspace_id=workspace_id,
            run_id=run_id,
            source_dir=draft_source,
            changed_files=changed_files,
            preview_run_id=run_id,
            scope_mode="whole_file_build",
        )
        preview = self.preview_service.get(workspace_id)
        container_logs = {}
        containers: list[dict[str, Any]] = []
        if preview.proxy_port is not None:
            log_source = (
                self.workspace_service.draft_source_dir(workspace_id, preview.draft_run_id)
                if preview.draft_run_id and self.workspace_service.draft_exists(workspace_id, preview.draft_run_id)
                else self.workspace_service.source_dir(workspace_id)
            )
            container_logs = self.runtime_manager.collect_container_logs(workspace_id, log_source, preview.proxy_port)
            containers = self.runtime_manager.inspect_containers(workspace_id, log_source, preview.proxy_port)
        preview_details = {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "logs": list(preview.logs),
            "last_error": preview.last_error,
            "containers": containers,
            "container_logs": container_logs,
        }
        execution.completed_at = utc_now()
        return execution, preview_details

    @staticmethod
    def _final_check_changed_files(
        latest_apply_result: dict[str, Any] | None,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
    ) -> list[str]:
        if latest_apply_result and latest_apply_result.get("changed_files"):
            return [str(path) for path in latest_apply_result.get("changed_files") or []]
        if fix_case.implicated_files:
            return list(fix_case.implicated_files)
        return [entry.file_path for entry in scope_entries]

    def _role_scope_for_fix_request(
        self,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
    ) -> list[str]:
        explicit_scope = [role for role in request.target_role_scope if role in {"client", "specialist", "manager"}]
        if explicit_scope:
            return explicit_scope
        page_graph = self._page_graph_for_deterministic_repair(workspace_id, run_id)
        graph_scope = [
            str(role)
            for role in (page_graph.get("roles") or {}).keys()
            if role in {"client", "specialist", "manager"}
        ]
        return graph_scope or ["client", "specialist", "manager"]

    def _finalize_loop_job(
        self,
        *,
        job: JobRecord,
        loop_result: WorkspaceLoopResult,
        scope_expansions: list[dict[str, Any]],
        elapsed_ms: int,
    ) -> JobRecord:
        job.status = loop_result.status
        job.outcome_kind = loop_result.outcome_kind
        job.summary = loop_result.summary
        job.failure_reason = loop_result.failure_reason
        if loop_result.failure_class is not None:
            job.failure_class = loop_result.failure_class
        if loop_result.failure_signature is not None:
            job.failure_signature = loop_result.failure_signature
        if loop_result.root_cause_summary is not None:
            job.root_cause_summary = loop_result.root_cause_summary
        baseline_failure_class = (
            self._classify_failure_text(job.error_context.raw_error)
            if job.error_context and job.error_context.raw_error
            else None
        )
        job.failure_class = self._prefer_failure_class(job.failure_class, baseline_failure_class)
        job.current_fix_phase = loop_result.current_phase
        job.remaining_issues = [] if loop_result.status == "completed" else list(loop_result.remaining_issues)
        if loop_result.latest_execution is not None:
            job.validation_snapshot = self._validation_snapshot_from_execution(loop_result.latest_execution)
        if loop_result.status == "completed":
            job.outcome_kind = "applied"
            job.validation_snapshot = ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        fix_attempts: list[FixAttemptRecord] = []
        for turn in loop_result.turn_history:
            fix_attempts.append(
                FixAttemptRecord(
                    run_id=job.linked_run_id or "",
                    attempt=int(turn.get("attempt") or 0),
                    diagnosis=str(turn.get("diagnosis") or turn.get("assistant_message") or ""),
                    commands=[result.command for result in (loop_result.latest_execution.results if loop_result.latest_execution else []) if result.command],
                    exit_codes={
                        result.name: result.exit_code
                        for result in (loop_result.latest_execution.results if loop_result.latest_execution else [])
                    },
                    files_changed=[str(path) for path in turn.get("files_changed") or []],
                    implicated_files=[str(path) for path in turn.get("fix_targets") or []],
                    failure_signature=str(turn.get("failure_signature") or "") or None,
                    result="patched" if str(turn.get("result")) == "patched" else "failed",
                    rationale_by_file={str(k): str(v) for k, v in dict(turn.get("metadata") or {}).items() if isinstance(v, str)},
                    expected_verification=None,
                )
            )
        return self._finalize_job(
            job,
            fix_attempts=fix_attempts,
            repair_iterations=loop_result.repair_iterations,
            scope_expansions=scope_expansions,
            latest_execution=loop_result.latest_execution,
            latest_preview_details=loop_result.latest_preview_details,
            latest_apply_result=loop_result.latest_apply_result,
            elapsed_ms=elapsed_ms,
        )

    def _build_fix_case(
        self,
        *,
        workspace_id: str,
        run_id: str,
        attempt: int,
        request: GenerateRequest,
        check_execution: CheckExecutionRecord,
        preview_details: dict[str, Any],
        prior_attempts: list[FixAttemptRecord],
        existing_scope: list[FixScopeEntry],
        memory_context: str | None = None,
    ) -> FixTurnContext:
        return self.fix_turn_builder.build_turn_context(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt=attempt,
            request=request,
            check_execution=check_execution,
            preview_details=preview_details,
            prior_attempts=prior_attempts,
            existing_scope=existing_scope,
            memory_context=memory_context,
            augment_failure_evidence=self._augment_failure_evidence_from_test_results,
            implicated_files=self._implicated_files,
            specialized_failure_class=self._specialized_failure_class,
            classify_failure_text=lambda text: self._classify_failure_text(text) or CheckRunner.classify_failure(check_execution.results) or "build/runtime",
            root_cause_summary=self._root_cause_summary,
            failure_signature=self._failure_signature,
            error_excerpt=self._error_excerpt,
            first_failing_command=self._first_failing_command,
            write_scope=lambda ws_id, current_run_id, implicated, failure_class, existing: self._build_write_scope(
                ws_id,
                current_run_id,
                implicated,
                failure_class,
                existing,
            ),
        )

    @staticmethod
    def _prefer_failure_class(existing: str | None, candidate: str | None) -> str | None:
        if not candidate:
            return existing
        if not existing:
            return candidate
        priority = {
            "runtime_manifest_route_missing": 100,
            "db_dependency_export_missing": 95,
            "backend_framework_mismatch": 94,
            "loading_first_root_surface": 92,
            "frontend_link_route_mismatch": 90,
            "router_not_registered": 88,
            "api_endpoint_missing": 86,
            "frontend_compile/type/import": 80,
            "backend_startup/import/schema": 78,
            "preview_runtime/docker_orchestration": 72,
            "route_api_contract_mismatch": 68,
            "runtime_preview_boot": 40,
            "build/runtime": 10,
        }
        existing_rank = priority.get(existing, 50)
        candidate_rank = priority.get(candidate, 50)
        return candidate if candidate_rank >= existing_rank else existing

    def _build_repair_packet(
        self,
        *,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
        context_mode: str,
        additional_paths: list[str] | None = None,
    ) -> FixPromptContext:
        return self.fix_turn_builder.build_prompt_context(
            workspace_id=workspace_id,
            run_id=run_id,
            fix_turn=fix_turn,
            scope_entries=scope_entries,
            context_mode=context_mode,
            collect_file_contexts=self._collect_file_contexts,
            merge_additional_context_paths=self._merge_additional_context_paths,
            deterministic_contract_seed_paths=self._deterministic_contract_seed_paths,
            current_diff_summary=self._current_diff_summary,
            additional_paths=additional_paths,
        )

    def _read_only_surfaces(self) -> list[str]:
        return self.fix_prompt_builder.read_only_surfaces()

    def _expected_contract_snapshot(self, fix_turn: FixTurnContext) -> dict[str, Any]:
        return self.fix_prompt_builder.expected_contract_snapshot(fix_turn)

    @staticmethod
    def _repair_context_mode(fix_turn: FixTurnContext, repeated_signature_without_progress: int) -> str:
        return FixPromptBuilder.repair_context_mode(fix_turn, repeated_signature_without_progress)

    @staticmethod
    def _needs_full_context_first(fix_turn: FixTurnContext) -> bool:
        return FixPromptBuilder.needs_full_context_first(fix_turn)

    @staticmethod
    def _previous_attempt_summary(fix_turn: FixTurnContext) -> str | None:
        return FixPromptBuilder.previous_attempt_summary(fix_turn)

    def _current_diff_summary(self, workspace_id: str, run_id: str) -> str | None:
        diff_report = self.store.get("reports", f"candidate_diff:{workspace_id}") or {}
        diff_text = str(diff_report.get("diff") or "")
        if not diff_text and self.workspace_service.draft_exists(workspace_id, run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        summary = self._diff_summary(diff_text)
        return None if summary == "No diff recorded." else summary

    @staticmethod
    def _normalized_critical_issues(results: list[RunCheckResult], fix_turn: FixTurnContext | None = None) -> list[dict[str, Any]]:
        return FixPromptBuilder.normalized_critical_issues(results, failure_class=fix_turn.failure_class if fix_turn is not None else None)

    @staticmethod
    def _repair_progress_snapshot(
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        fix_turn: FixTurnContext,
    ) -> dict[str, Any]:
        issues = CheckRunner.failing_issues(results)
        evidence = "\n".join(
            [
                str(fix_turn.root_cause_summary or ""),
                str(fix_turn.exact_error_excerpt or ""),
                *[item.details or "" for item in results],
                *[line for item in results for line in item.logs],
                *(preview_details.get("logs") or []),
            ]
        )
        route_markers = sorted(
            {
                marker
                for marker in re.findall(r"(/(?:api/)?[A-Za-z0-9_./:-]+)", evidence)
                if marker.startswith(("/api/", "/client", "/specialist", "/manager"))
            }
        )
        return {
            "failed_checks": sorted(result.name for result in results if result.status == "failed"),
            "blocking_issue_codes": sorted(issue.code for issue in issues if issue.blocking),
            "route_markers": route_markers[:20],
            "preview_status": str(preview_details.get("status") or ""),
            "preview_stage": str(preview_details.get("stage") or ""),
        }

    @staticmethod
    def _repair_snapshot_improved(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
        if previous is None:
            return True
        previous_checks = set(previous.get("failed_checks") or [])
        current_checks = set(current.get("failed_checks") or [])
        previous_codes = set(previous.get("blocking_issue_codes") or [])
        current_codes = set(current.get("blocking_issue_codes") or [])
        previous_routes = set(previous.get("route_markers") or [])
        current_routes = set(current.get("route_markers") or [])
        return (
            len(current_checks) < len(previous_checks)
            or len(current_codes) < len(previous_codes)
            or len(current_routes) < len(previous_routes)
            or current_checks < previous_checks
            or current_codes < previous_codes
            or current_routes < previous_routes
        )

    def _specialized_failure_class(
        self,
        *,
        workspace_id: str,
        run_id: str,
        results: list[RunCheckResult],
        combined_text: str,
        implicated_files: list[str],
    ) -> str | None:
        lowered = combined_text.lower()
        issue_codes = {issue.code for issue in CheckRunner.failing_issues(results)}
        if {"build.loading_first_root_surface", "build.root_page_missing_business_surface"} & issue_codes:
            return "loading_first_root_surface"
        if (
            ("no module named 'flask'" in lowered or 'no module named "flask"' in lowered or "from flask import" in lowered)
            and any(path.startswith("miniapp/app/routes/") and path.endswith(".py") for path in implicated_files)
        ):
            return "backend_framework_mismatch"
        if "/api/runtime/" in lowered and "manifest" in lowered and ("404" in lowered or "not found" in lowered):
            return "runtime_manifest_route_missing"
        if ("cannot import name 'get_db'" in lowered or 'cannot import name "get_db"' in lowered or "import get_db" in lowered) and any(
            path.endswith(("/db.py", "/schemas.py", "/main.py")) for path in implicated_files
        ):
            return "db_dependency_export_missing"
        if "not declared in route_manifest.json" in lowered or ("/specialist/" in lowered and "404" in lowered):
            return "frontend_link_route_mismatch"
        missing_backend_routes = [
            issue
            for issue in CheckRunner.failing_issues(results)
            if issue.code == "connectivity.missing_backend_route"
        ]
        if missing_backend_routes:
            route_root = self.workspace_service.draft_source_dir(workspace_id, run_id) / "miniapp/app/routes"
            for issue in missing_backend_routes:
                location = str(issue.location or "")
                if location.startswith("miniapp/app/routes/") and (self.workspace_service.draft_source_dir(workspace_id, run_id) / location).exists():
                    return "router_not_registered"
            return "api_endpoint_missing"
        return None

    def _repair_outcome_from_response(
        self,
        *,
        llm_result: dict[str, Any],
        prompt_context: FixPromptContext,
        fix_turn: FixTurnContext,
        scope_expansions: list[dict[str, Any]],
    ) -> FixAttemptOutcome:
        if "error" in llm_result:
            return FixAttemptOutcome(
                outcome="fatal_invalid_response",
                validation_error=str(llm_result["error"]),
                raw_response=llm_result,
            )
        raw_operations = llm_result.get("operations") or []
        diagnosis_text = str(llm_result.get("diagnosis") or "")
        planned_targets = self._planned_target_paths(llm_result)
        outcome_hint = str(llm_result.get("outcome") or "").strip().lower()
        if outcome_hint == "needs_more_context":
            return FixAttemptOutcome(
                outcome="needs_more_context",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        if not raw_operations and ((self._looks_like_context_refusal(diagnosis_text)) or planned_targets):
            return FixAttemptOutcome(
                outcome="needs_more_context",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        try:
            operations = self._coerce_operations(
                raw_operations,
                [FixScopeEntry(file_path=path, reason="Repair packet companion scope.") for path in prompt_context.deterministic_companions],
                fix_turn,
                scope_expansions,
            )
        except Exception as exc:
            if self._should_retry_patch_validation(str(exc)):
                return FixAttemptOutcome(
                    outcome="needs_more_context",
                    diagnosis=diagnosis_text,
                    planned_targets=planned_targets,
                    validation_error=str(exc),
                    expected_verification=str(llm_result.get("expected_verification") or ""),
                    rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                    raw_response=llm_result,
                )
            return FixAttemptOutcome(
                outcome="no_progress",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                validation_error=str(exc),
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        if not operations:
            return FixAttemptOutcome(
                outcome="no_progress",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                validation_error="Repair model did not return any patch operations.",
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        return FixAttemptOutcome(
            outcome="patch_ready",
            diagnosis=diagnosis_text,
            operations=operations,
            planned_targets=planned_targets,
            expected_verification=str(llm_result.get("expected_verification") or ""),
            rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
            raw_response=llm_result,
        )

    def _plan_patch(
        self,
        *,
        job: JobRecord,
        prompt_context: FixPromptContext,
        repair_feedback: str | None = None,
    ) -> dict[str, Any]:
        if not self.openrouter_client.enabled:
            return {"error": "Fix mode requires an enabled LLM provider or a deterministic local repair path."}
        job.current_fix_phase = "patching"
        self._save_job(job)
        prompt_cache_key = self._prompt_cache_key(prompt_context)
        try:
            payload = self.openrouter_client.generate_workspace_edits(
                schema_name="fix_patch_v1",
                schema=self._repair_schema(),
                system_prompt=self._repair_system_prompt(),
                user_prompt=self._repair_user_prompt(prompt_context, repair_feedback=repair_feedback),
                prompt_cache_key=prompt_cache_key,
                stable_prefix=self._repair_system_prompt(),
            )
            job.llm_model = str(payload["model"])
            job.cache_stats = self._merge_cache_stats(job.cache_stats, payload.get("cache_stats") or {})
            self._save_job(job)
            normalized = payload["payload"]
            if isinstance(normalized, str):
                normalized = json.loads(normalized)
            return normalized if isinstance(normalized, dict) else {"error": "Repair model returned an invalid payload."}
        except Exception as exc:
            logger.exception(
                "fix_patch_generation_failed workspace_id=%s run_id=%s",
                prompt_context.workspace_id,
                prompt_context.run_id,
            )
            return {"error": f"Repair patch generation failed: {exc}"}

    def _coerce_operations(
        self,
        raw_operations: list[Any],
        scope_entries: list[FixScopeEntry],
        fix_turn: FixTurnContext,
        scope_expansions: list[dict[str, Any]],
    ) -> list[DraftFileOperation]:
        scope_paths = {entry.file_path for entry in scope_entries}
        operations: list[DraftFileOperation] = []
        for index, item in enumerate(raw_operations):
            operation = DraftFileOperation.model_validate(item)
            if operation.operation in {"create", "replace"} and operation.content is None:
                raise ValueError(
                    f"Repair returned {operation.operation} for {operation.file_path} without content."
                )
            framework_validation_error = self._backend_framework_validation_error(operation)
            if framework_validation_error:
                raise ValueError(framework_validation_error)
            if operation.file_path.startswith("miniapp/tests/") and not self._allow_test_file_writes(fix_turn):
                raise ValueError(f"Repair attempted to edit generated tests instead of the app surface: {operation.file_path}")
            if self._is_read_only_generated_surface(operation.file_path):
                raise ValueError(f"Repair attempted to edit a generated manifest surface instead of the app bundle: {operation.file_path}")
            if operation.file_path not in scope_paths:
                if len(scope_expansions) >= self.MAX_SCOPE_EXPANSIONS or not self._can_expand_for_file(operation.file_path, fix_turn.implicated_files):
                    raise ValueError(f"Repair touched files outside the allowed evidence-based scope: {operation.file_path}")
                scope_expansions.append(
                    {
                        "attempt": fix_turn.attempt,
                        "files": [operation.file_path],
                        "reason": "Repair model requested an adjacent evidence-based file.",
                    }
                )
                scope_paths.add(operation.file_path)
            operations.append(
                DraftFileOperation(
                    operation_id=operation.operation_id or f"fix_op_{index}",
                    file_path=operation.file_path,
                    operation=operation.operation,
                    content=operation.content,
                    reason=operation.reason,
                )
            )
        return operations

    @staticmethod
    def _is_read_only_generated_surface(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        return normalized in {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        }

    @staticmethod
    def _can_expand_for_file(candidate: str, implicated_files: list[str]) -> bool:
        if not implicated_files:
            return candidate.startswith(("miniapp/", "docker/"))
        for file_path in implicated_files:
            if candidate.startswith(file_path.rsplit("/", 1)[0] + "/"):
                return True
            if candidate.split("/", 1)[0] == file_path.split("/", 1)[0]:
                return True
        return candidate.startswith(("docker/", "miniapp/app/", "miniapp/app/static/"))

    def _build_write_scope(
        self,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
        existing_scope: list[FixScopeEntry],
    ) -> list[FixScopeEntry]:
        entries = self.fix_scope_builder.build_write_scope(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            existing_scope=existing_scope,
            allow_missing_scope_path=self._allow_missing_scope_path,
        )
        if not self._allow_test_file_writes_for_failure(failure_class):
            entries = [entry for entry in entries if not entry.file_path.startswith("miniapp/tests/")]
        return entries

    @staticmethod
    def _deterministic_companion_scope(file_path: str) -> list[str]:
        return FixScopeBuilder.deterministic_companion_scope(file_path)

    def _structural_scope_bundle(
        self,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
    ) -> list[str]:
        return self.fix_scope_builder.structural_scope_bundle(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            allow_missing_scope_path=self._allow_missing_scope_path,
        )

    def _feature_scope_bundle(self, workspace_id: str, run_id: str, implicated_files: list[str]) -> list[str]:
        bundle: list[str] = []
        for file_path in implicated_files:
            if file_path.startswith("miniapp/app/static/") and file_path.endswith((".html", ".css", ".js")):
                parent = Path(file_path).parent
                for sibling in (parent / "index.html", parent / "styles.css", parent / "app.js"):
                    normalized = sibling.as_posix()
                    if self._file_exists(workspace_id, run_id, normalized) or self._allow_missing_scope_path(normalized):
                        bundle.append(normalized)
        return list(dict.fromkeys(bundle))

    @staticmethod
    def _merge_scope(
        current_scope: list[FixScopeEntry],
        next_scope: list[FixScopeEntry],
        scope_expansions: list[dict[str, Any]],
    ) -> list[FixScopeEntry]:
        return FixScopeBuilder.merge_scope(
            current_scope,
            next_scope,
            scope_expansions,
            max_scope_expansions=FixOrchestrator.MAX_SCOPE_EXPANSIONS,
        )

    def _collect_file_contexts(
        self,
        workspace_id: str,
        run_id: str,
        scope_entries: list[FixScopeEntry],
        *,
        fix_turn: FixTurnContext | None = None,
        budget_override: int | None = None,
        full_files: bool = False,
    ) -> dict[str, str]:
        contexts: dict[str, str] = {}
        budget = budget_override or self.MAX_CONTEXT_CHARS
        for entry in scope_entries:
            if budget <= 0:
                break
            if not self._file_exists(workspace_id, run_id, entry.file_path):
                continue
            target_path = self.workspace_service.draft_source_dir(workspace_id, run_id) / entry.file_path
            if target_path.is_dir():
                continue
            content = self.workspace_service.read_file(workspace_id, entry.file_path, run_id=run_id)
            excerpt = content if full_files else content[: min(len(content), min(4000, budget))]
            if len(excerpt) > budget:
                excerpt = excerpt[:budget]
            contexts[entry.file_path] = excerpt
            budget -= len(excerpt)
        for support_path in self._repair_support_files(fix_turn):
            if budget <= 0 or support_path in contexts or not self._file_exists(workspace_id, run_id, support_path):
                continue
            content = self.workspace_service.read_file(workspace_id, support_path, run_id=run_id)
            excerpt = content if full_files else content[: min(len(content), min(2500, budget))]
            if len(excerpt) > budget:
                excerpt = excerpt[:budget]
            contexts[support_path] = excerpt
            budget -= len(excerpt)
        return contexts

    def _deterministic_contract_repair_operations(
        self,
        *,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
        generation_mode: GenerationMode,
    ) -> list[DraftFileOperation]:
        if self.generation_service is None:
            return []
        page_graph = self._page_graph_for_deterministic_repair(workspace_id, run_id)
        role_scope = [role for role in ((page_graph.get("roles") or {}).keys()) if role in {"client", "specialist", "manager"}]
        if not role_scope:
            role_scope = ["client", "specialist", "manager"]
        seed_paths = self._deterministic_contract_seed_paths(workspace_id, run_id, fix_turn, scope_entries)
        if not seed_paths:
            return []
        seed_operations: list[DraftFileOperation] = []
        for file_path in seed_paths:
            if not self._file_exists(workspace_id, run_id, file_path):
                continue
            absolute_path = self.workspace_service.draft_source_dir(workspace_id, run_id) / file_path
            if absolute_path.is_dir():
                continue
            seed_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=self.workspace_service.read_file(workspace_id, file_path, run_id=run_id),
                    reason="Deterministic contract repair seed.",
                )
            )
        if not seed_operations:
            return []
        repaired = self.generation_service._run_pre_apply_contract_pass(
            workspace_id=workspace_id,
            draft_run_id=run_id,
            page_graph=page_graph,
            role_scope=role_scope,
            generation_mode=generation_mode,
            operations=seed_operations,
        )
        changed: list[DraftFileOperation] = []
        for operation in repaired:
            if operation.file_path.startswith(("artifacts/", "miniapp/app/generated/", "miniapp/tests/")):
                continue
            if operation.operation not in {"replace", "create", "delete"}:
                continue
            exists = self._file_exists(workspace_id, run_id, operation.file_path)
            if operation.operation == "delete":
                if exists:
                    changed.append(operation)
                continue
            current_content = self.workspace_service.read_file(workspace_id, operation.file_path, run_id=run_id) if exists else ""
            if current_content != (operation.content or ""):
                changed.append(operation)
        return changed

    def _page_graph_for_deterministic_repair(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        if self._file_exists(workspace_id, run_id, "artifacts/generated_app_graph.json"):
            graph_content = self.workspace_service.try_read_text_file(
                workspace_id,
                "artifacts/generated_app_graph.json",
                run_id=run_id,
            )
        else:
            graph_content = None
        if graph_content:
            try:
                graph = json.loads(graph_content)
                if isinstance(graph, dict):
                    return graph
            except Exception:
                pass
        report_payload = self.store.get("reports", f"page_graph:{workspace_id}") or {}
        report_graph = report_payload.get("page_graph")
        if isinstance(report_graph, dict):
            return report_graph
        if self._file_exists(workspace_id, run_id, "miniapp/app/generated/route_manifest.json"):
            route_manifest_content = self.workspace_service.try_read_text_file(
                workspace_id,
                "miniapp/app/generated/route_manifest.json",
                run_id=run_id,
            )
        else:
            route_manifest_content = None
        if route_manifest_content:
            try:
                route_manifest = json.loads(route_manifest_content)
                if isinstance(route_manifest, dict):
                    roles_payload = route_manifest.get("roles") or {}
                    if isinstance(roles_payload, dict):
                        return {"roles": roles_payload}
            except Exception:
                pass
        return {"roles": {}}

    def _deterministic_contract_seed_paths(
        self,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
    ) -> list[str]:
        del workspace_id
        base_paths = [
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/profiles.py",
        ]
        candidates = list(base_paths)
        route_root = self.workspace_service.draft_source_dir(fix_turn.workspace_id, run_id) / "miniapp/app/routes"
        if route_root.exists():
            for route_file in sorted(route_root.glob("*.py")):
                relative = f"miniapp/app/routes/{route_file.name}"
                if relative not in candidates:
                    candidates.append(relative)
        for entry in scope_entries:
            if entry.file_path not in candidates:
                candidates.append(entry.file_path)
        for file_path in fix_turn.implicated_files:
            if file_path not in candidates:
                candidates.append(file_path)
        unique: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip().lstrip("./")
            if not normalized or normalized in unique:
                continue
            if self._file_exists(fix_turn.workspace_id, run_id, normalized):
                unique.append(normalized)
        return unique[:48]

    @staticmethod
    def _looks_like_context_refusal(diagnosis: str) -> bool:
        lowered = diagnosis.lower().replace("’", "'").replace("‘", "'")
        markers = (
            "can't inspect",
            "cannot inspect",
            "can't access",
            "cannot access",
            "can't edit the workspace files",
            "cannot edit the workspace files",
            "without access to the actual file contents",
            "unable to inspect",
            "unable to access the file",
            "need to inspect",
            "need the current contents",
            "need current contents",
            "need the current route wiring",
            "need current route wiring",
            "need to review the current",
            "need the full file",
            "need full file",
            "current file excerpts were insufficient",
            "insufficient file excerpts",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _planned_target_paths(llm_result: dict[str, Any]) -> list[str]:
        raw_targets = llm_result.get("planned_targets") or []
        if not isinstance(raw_targets, list):
            return []
        normalized: list[str] = []
        for item in raw_targets:
            target = str(item or "").strip().lstrip("./")
            if not target or target in normalized:
                continue
            normalized.append(target)
        return normalized[:12]

    def _merge_additional_context_paths(
        self,
        workspace_id: str,
        run_id: str,
        contexts: dict[str, str],
        additional_paths: list[str],
        *,
        budget_override: int | None = None,
    ) -> dict[str, str]:
        if not additional_paths:
            return contexts
        merged = dict(contexts)
        budget = budget_override or self.MAX_CONTEXT_CHARS_EXPANDED
        used = sum(len(content) for content in merged.values())
        remaining = max(0, budget - used)
        for path in additional_paths:
            if remaining <= 0 or path in merged or not self._file_exists(workspace_id, run_id, path):
                continue
            target_path = self.workspace_service.draft_source_dir(workspace_id, run_id) / path
            if target_path.is_dir():
                continue
            content = self.workspace_service.read_file(workspace_id, path, run_id=run_id)
            excerpt = content[:remaining]
            if not excerpt:
                continue
            merged[path] = excerpt
            remaining -= len(excerpt)
        return merged

    @staticmethod
    def _operations_missing_content(raw_operations: list[Any]) -> list[str]:
        missing: list[str] = []
        for item in raw_operations:
            if not isinstance(item, dict):
                continue
            operation = str(item.get("operation") or "").strip().lower()
            if operation not in {"create", "replace"}:
                continue
            file_path = str(item.get("file_path") or "").strip()
            if not file_path:
                continue
            if item.get("content") is None:
                missing.append(file_path)
        return list(dict.fromkeys(missing))

    @staticmethod
    def _should_retry_patch_validation(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(
            marker in lowered
            for marker in (
                "without content",
                "did not return any patch operations",
                "did not return any file operations",
                "flask/blueprint",
                "must stay on fastapi",
                "must define router = apirouter",
            )
        )

    @staticmethod
    def _backend_framework_validation_error(operation: DraftFileOperation) -> str | None:
        file_path = str(operation.file_path or "").replace("\\", "/")
        if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
            return None
        content = str(operation.content or "")
        lowered = content.lower()
        if "from flask import" in lowered or "blueprint(" in lowered:
            return f"{file_path} must stay on FastAPI APIRouter, not Flask/Blueprint."
        if "apirouter" not in lowered:
            return f"{file_path} must stay on FastAPI APIRouter."
        if re.search(r"(?m)^\s*router\s*=\s*APIRouter\(", content) is None:
            return f"{file_path} must define router = APIRouter(...)."
        return None

    @staticmethod
    def _repair_support_files(fix_turn: FixTurnContext | None) -> list[str]:
        if fix_turn is None:
            return []
        connectivity_codes = {
            "connectivity.missing_ui_loading_state",
            "connectivity.missing_ui_error_state",
            "connectivity.missing_backend_route",
        }
        evidence = "\n".join(
            [
                str(fix_turn.failure_class or ""),
                str(fix_turn.root_cause_summary or ""),
                str(fix_turn.exact_error_excerpt or ""),
                *[result.details or "" for result in fix_turn.executed_checks],
                *[line for result in fix_turn.executed_checks for line in result.logs],
            ]
        ).lower()
        if any(code in evidence for code in connectivity_codes):
            return ["artifacts/generated_app_graph.json"]
        return []

    def _implicated_files(
        self,
        workspace_id: str,
        run_id: str,
        text: str,
        existing_scope: list[FixScopeEntry],
    ) -> list[str]:
        candidates: list[str] = []
        for match in re.findall(r"((?:miniapp|docker)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text):
            candidates.append(match)
        for match in re.findall(r"(static/[A-Za-z0-9_./-]+\.(?:html|css|js))", text):
            candidates.append(f"miniapp/app/{match}")
        for module in re.findall(r"\"(@/[A-Za-z0-9_./-]+)\"", text):
            resolved = self._resolve_frontend_module(workspace_id, run_id, module)
            if resolved:
                candidates.append(resolved)
        for module in re.findall(r"'(app(?:\.[A-Za-z0-9_]+)+)'", text):
            resolved = self._resolve_backend_module(workspace_id, run_id, module)
            if resolved:
                candidates.append(resolved)
        for line in text.splitlines():
            if "cannot import name" in line.lower():
                backend_match = re.search(r"from '([^']+)'", line)
                if backend_match:
                    resolved = self._resolve_backend_module(workspace_id, run_id, backend_match.group(1))
                    if resolved:
                        candidates.append(resolved)
        candidates.extend(self._test_failure_implicated_paths(text))
        for entry in existing_scope:
            candidates.append(entry.file_path)
        unique: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip().lstrip("./")
            if not normalized or normalized in unique:
                continue
            if self._file_exists(workspace_id, run_id, normalized) or self._allow_missing_scope_path(normalized):
                unique.append(normalized)
        return unique[:24]

    def _root_cause_summary(self, results: list[RunCheckResult], preview_details: dict[str, Any], raw_error: str) -> str:
        for result in results:
            if result.status == "failed":
                line = next((item.strip() for item in result.logs if item.strip()), result.details or "")
                if line:
                    return line
        preview_error = str(preview_details.get("last_error") or "").strip()
        if preview_error:
            return preview_error
        raw = raw_error.strip()
        return raw.splitlines()[0] if raw else "Fix mode detected an unresolved build or runtime failure."

    @staticmethod
    def _allow_missing_scope_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().lstrip("./")
        if not normalized:
            return False
        return normalized.startswith(
            (
                "miniapp/app/static/",
                "miniapp/app/routes/",
                "miniapp/app/generated/",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/main.py",
                "miniapp/tests/",
                "artifacts/",
            )
        )

    @staticmethod
    def _scope_can_still_expand(existing_scope: list[FixScopeEntry], next_scope: list[FixScopeEntry]) -> bool:
        current = {entry.file_path for entry in existing_scope}
        upcoming = {entry.file_path for entry in next_scope}
        return bool(upcoming - current)

    @staticmethod
    def _augment_failure_evidence_from_test_results(base_text: str, results: list[RunCheckResult]) -> str:
        markers: list[str] = []
        for result in results:
            if result.status != "failed":
                continue
            haystack = "\n".join([result.details or "", *result.logs]).lower()
            if result.name == "generated_app_python_tests":
                if "sessionlocal" in haystack:
                    markers.append("runtime.startup.missing_sessionlocal")
                if "roleprofilerecord" in haystack:
                    markers.append("runtime.startup.missing_role_profile_record")
                if "cannot import name" in haystack or "importerror" in haystack:
                    markers.append("runtime.startup.import_drift")
            if result.name == "generated_app_js_tests":
                if "not declared in route_manifest.json" in haystack:
                    markers.append("route_manifest.missing_declared_route")
                if "/profile" in haystack:
                    markers.append("route_manifest.missing_profile_route")
        if not markers:
            return base_text
        return "\n".join([base_text, *markers])

    def _test_failure_implicated_paths(self, text: str) -> list[str]:
        lowered = text.lower()
        candidates: list[str] = []
        if "sessionlocal" in lowered:
            candidates.extend(
                [
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/tests/test_generated_app.py",
                ]
            )
        if "roleprofilerecord" in lowered:
            candidates.extend(
                [
                    "miniapp/app/db.py",
                    "miniapp/app/routes/profiles.py",
                    "miniapp/app/schemas.py",
                    "miniapp/tests/test_generated_app.py",
                ]
            )
        if "route_manifest.json" in lowered or "not declared in route_manifest.json" in lowered:
            candidates.extend(
                [
                    "miniapp/app/generated/route_manifest.json",
                    "miniapp/tests/generated_app.test.mjs",
                ]
            )
        route_refs = re.findall(r"Route\s+([/A-Za-z0-9_{}:-]+)\s+referenced", text)
        route_refs.extend(match for _, match in re.findall(r"(['\"])(/[^'\"]+)\1", text))
        normalized_routes: list[str] = []
        for route in route_refs:
            route = str(route).strip()
            if not route.startswith("/") or route.startswith("/api/") or route in normalized_routes:
                continue
            normalized_routes.append(route)
        for route in normalized_routes:
            candidates.extend(self._page_triplet_candidates_for_route(route))
            role = route.strip("/").split("/", 1)[0]
            if role in {"client", "specialist", "manager"}:
                candidates.append(f"miniapp/app/routes/{role}.py")
        return candidates

    @staticmethod
    def _page_triplet_candidates_for_route(route_path: str) -> list[str]:
        route = str(route_path or "").strip()
        if not route.startswith("/"):
            return []
        segments = [segment for segment in route.strip("/").split("/") if segment]
        if not segments:
            return []
        role = segments[0]
        if role not in {"client", "specialist", "manager"}:
            return []
        page_segments = segments[1:]
        if not page_segments:
            base = f"miniapp/app/static/{role}"
            return [f"{base}/index.html", f"{base}/styles.css", f"{base}/app.js"]
        folder = "_".join(segment.replace("-", "_") for segment in page_segments)
        base = f"miniapp/app/static/{role}/{folder}"
        return [f"{base}/index.html", f"{base}/styles.css", f"{base}/app.js"]

    @staticmethod
    def _failure_signature(failure_class: str, root_cause_summary: str) -> str:
        normalized = re.sub(r"\s+", " ", f"{failure_class}:{root_cause_summary}".strip().lower())
        normalized = re.sub(r"\bline \d+\b", "line", normalized)
        normalized = re.sub(r"\(\d+,\d+\)", "(loc)", normalized)
        return normalized[:220]

    @staticmethod
    def _error_excerpt(results: list[RunCheckResult], preview_details: dict[str, Any], raw_error: str) -> str:
        excerpt_lines: list[str] = []
        for result in results:
            if result.status == "failed":
                excerpt_lines.extend(result.logs[:12])
        if not excerpt_lines and preview_details.get("logs"):
            excerpt_lines.extend(preview_details.get("logs", [])[-12:])
        if not excerpt_lines and raw_error.strip():
            excerpt_lines = raw_error.strip().splitlines()[:12]
        return "\n".join(excerpt_lines[:12])

    @staticmethod
    def _is_fix_success(results: list[RunCheckResult], preview_details: dict[str, Any]) -> bool:
        validators_ok = all(result.status != "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"})
        build_ok = all(result.status != "failed" for result in results if result.name == "changed_files_static")
        app_tests_ok = all(
            result.status == "passed"
            for result in results
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}
        )
        preview_result = next((result for result in results if result.name == "preview_boot_smoke"), None)
        preview_connectivity_result = next((result for result in results if result.name == "preview_connectivity_smoke"), None)
        preview_deferred = (
            preview_result is not None
            and preview_result.status == "skipped"
            and preview_connectivity_result is not None
            and preview_connectivity_result.status == "skipped"
            and preview_details.get("status") == "skipped"
        )
        preview_ok = (
            preview_result is not None
            and preview_result.status == "passed"
            and preview_connectivity_result is not None
            and preview_connectivity_result.status == "passed"
            and preview_details.get("status") == "running"
        )
        return validators_ok and build_ok and app_tests_ok and (preview_ok or preview_deferred)

    @classmethod
    def _completion_state_from_results(
        cls,
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        *,
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, Any]:
        strict_green = cls._is_fix_success(results, preview_details)
        preview_result = next((result for result in results if result.name == "preview_boot_smoke"), None)
        preview_connectivity_result = next((result for result in results if result.name == "preview_connectivity_smoke"), None)
        preview_ok = (
            preview_result is not None
            and preview_connectivity_result is not None
            and preview_result.status != "failed"
            and preview_connectivity_result.status != "failed"
        )
        non_blocking_validation_codes = {"build.identical_role_pages"}
        validation_failures = [
            issue
            for issue in (validation_snapshot.issues if validation_snapshot is not None else [])
            if isinstance(issue, dict) and issue.get("blocking", False)
        ]
        only_non_blocking_validator_tail = bool(validation_failures) and all(
            str(issue.get("code") or "") in non_blocking_validation_codes for issue in validation_failures
        )
        validators_ok = all(result.status != "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"})
        if only_non_blocking_validator_tail:
            validators_ok = True
        build_ok = all(result.status != "failed" for result in results if result.name == "changed_files_static")
        app_test_failures = [
            result
            for result in results
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"} and result.status == "failed"
        ]
        non_test_failures = [
            result
            for result in results
            if result.status == "failed"
            and result.name not in {"generated_app_python_tests", "generated_app_js_tests"}
            and not (
                only_non_blocking_validator_tail
                and result.name == "schema_validators"
            )
        ]
        remaining_issues = cls._remaining_issues_from_results(
            app_test_failures=app_test_failures,
            validation_snapshot=validation_snapshot,
            preview_details=preview_details,
        )
        if only_non_blocking_validator_tail:
            remaining_issues.extend(
                {
                    "kind": "validation_issue",
                    "code": issue.get("code"),
                    "message": issue.get("message"),
                    "location": issue.get("location"),
                    "blocking": False,
                }
                for issue in validation_failures
            )
        return {
            "strict_green": strict_green,
            "optimistic_complete": False,
            "remaining_issues": remaining_issues,
        }

    @staticmethod
    def _remaining_issues_from_results(
        *,
        app_test_failures: list[RunCheckResult],
        validation_snapshot: ValidationSnapshot | None,
        preview_details: dict[str, Any],
    ) -> list[dict[str, Any]]:
        remaining: list[dict[str, Any]] = []
        for result in app_test_failures:
            remaining.append(
                {
                    "kind": "generated_test_failure",
                    "location": "tests",
                    "blocking": False,
                    "check": result.name,
                    "details": result.details,
                    "logs": result.logs[-8:],
                }
            )
        if validation_snapshot is not None:
            for issue in validation_snapshot.issues:
                if not isinstance(issue, dict) or issue.get("blocking", False):
                    continue
                remaining.append(
                    {
                        "kind": "validation_issue",
                        "code": issue.get("code"),
                        "message": issue.get("message"),
                        "location": issue.get("location"),
                    }
                )
        if preview_details.get("status") == "error" and preview_details.get("last_error"):
            remaining.append(
                {
                    "kind": "preview_warning",
                    "message": preview_details.get("last_error"),
                }
            )
        return remaining

    @staticmethod
    def _first_failing_command(results: list[RunCheckResult]) -> str | None:
        for result in results:
            if result.status == "failed" and result.command:
                return result.command
        return None

    @staticmethod
    def _first_failing_exit_code(results: list[RunCheckResult]) -> int | None:
        for result in results:
            if result.status == "failed" and result.exit_code is not None:
                return result.exit_code
        return None

    @staticmethod
    def _classify_failure_text(text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ("npm is not available", "docker compose is not available", "tooling is unavailable", "was not found on path")):
            return "tooling/platform_misconfiguration"
        if any(
            marker in lowered
            for marker in (
                "could not be opened in preview",
                "returned unusable preview content",
                "preview route smoke",
                "connection refused",
            )
        ):
            return "runtime_preview_boot"
        if any(
            marker in lowered
            for marker in (
                "has no exported member",
                "typescript",
                "argument of type",
                "cannot find module",
                "vite build",
                "static miniapp validation failed",
                "is not defined",
                "undefined leaves invalid state",
                "ts230",
                ".ts:",
                ".tsx:",
                ".js:",
            )
        ):
            return "frontend_compile/type/import"
        if any(marker in lowered for marker in ("traceback", "importerror", "modulenotfounderror", "cannot import name", "py_compile failed", "pydantic")):
            return "backend_startup/import/schema"
        if any(marker in lowered for marker in ("docker preview", "container ", "dependency failed to start", "health probe", "preview rebuild failed")):
            return "preview_runtime/docker_orchestration"
        if any(marker in lowered for marker in ("401", "403", "permission denied")):
            return "runtime_permission_mismatch"
        if any(marker in lowered for marker in ("fetch(", "/api/", "response status", "payload", "contract")):
            return "route_api_contract_mismatch"
        return "build/runtime"

    def _validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        return ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=not build_failed,
            blocking=bool(issues),
            issues=issues,
        )

    def _finalize_job(
        self,
        job: JobRecord,
        *,
        fix_attempts: list[FixAttemptRecord],
        repair_iterations: list[RepairIterationRecord],
        scope_expansions: list[dict[str, Any]],
        latest_execution: CheckExecutionRecord | None,
        latest_preview_details: dict[str, Any],
        latest_apply_result: dict[str, Any] | None,
        elapsed_ms: int,
    ) -> JobRecord:
        job.fix_attempts = [item.model_dump(mode="json") for item in fix_attempts]
        job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
        job.scope_expansions = list(scope_expansions)
        job.apply_result = latest_apply_result
        if latest_execution is not None:
            job.executed_checks = [item.model_dump(mode="json") for item in latest_execution.results]
        job.container_statuses = latest_preview_details.get("containers", job.container_statuses)
        job.updated_at = datetime.now(timezone.utc)
        job.latency_breakdown["fix_total_ms"] = elapsed_ms
        self._save_job(job)
        if self.artifact_recorder is not None:
            self.artifact_recorder.store_workspace_report(
                job.workspace_id,
                "cache_diagnostics",
                {
                    "workspace_id": job.workspace_id,
                    "cache_stats": job.cache_stats,
                    "repair_iterations": len(job.repair_iterations),
                    "fix_attempts": len(job.fix_attempts),
                },
            )
        if self.session_engine is not None:
            self.session_engine.record_phase(
                workspace_id=job.workspace_id,
                phase="repair",
                generation_mode=str(job.generation_mode),
                model_profile=job.model_profile or "openai_code_fast",
                run_mode="fix",
                details={
                    "fix_attempts": len(fix_attempts),
                    "scope_expansions": len(scope_expansions),
                    "status": job.status,
                },
            )
        self._store_report(f"fix_attempts:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.fix_attempts})
        self._store_report(f"scope_expansions:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": scope_expansions})
        self._store_report(
            f"remaining_issues:{job.workspace_id}",
            {"workspace_id": job.workspace_id, "items": list(job.remaining_issues)},
        )
        if job.validation_snapshot is not None:
            self._store_report(
                f"validation:{job.workspace_id}",
                job.validation_snapshot.model_dump(mode="json"),
            )
        if latest_preview_details:
            self._store_report(
                f"fix_runtime:{job.workspace_id}",
                {
                    "workspace_id": job.workspace_id,
                    "containers": latest_preview_details.get("containers", []),
                    "container_logs": latest_preview_details.get("container_logs", {}),
                    "status": latest_preview_details.get("status"),
                    "stage": latest_preview_details.get("stage"),
                    "last_error": latest_preview_details.get("last_error"),
                },
            )
        return job

    @staticmethod
    def _prompt_cache_key_seed(*, workspace_id: str, run_id: str, prompt: str) -> str:
        return hashlib.sha256(f"{workspace_id}:{run_id}:{prompt}".encode("utf-8")).hexdigest()

    def _repair_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["patch_ready", "needs_more_context", "no_progress", "fatal_invalid_response"],
                },
                "diagnosis": {"type": "string"},
                "planned_targets": {"type": "array", "items": {"type": "string"}},
                "expected_verification": {"type": "string"},
                "rationale_by_file": {"type": "object", "additionalProperties": {"type": "string"}},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "file_path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["create", "replace", "delete"]},
                            "content": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["file_path", "operation", "reason"],
                    },
                },
            },
            "required": ["diagnosis", "planned_targets", "expected_verification", "rationale_by_file", "operations"],
        }

    def _repair_system_prompt(self) -> str:
        return self.fix_prompt_builder.repair_system_prompt()

    def _repair_user_prompt(
        self,
        repair_packet: FixPromptContext,
        *,
        repair_feedback: str | None = None,
    ) -> str:
        return self.fix_prompt_builder.repair_user_prompt(repair_packet, repair_feedback=repair_feedback)

    @staticmethod
    def _allow_test_file_writes_for_failure(failure_class: str | None) -> bool:
        del failure_class
        return False

    @classmethod
    def _allow_test_file_writes(cls, fix_turn: FixTurnContext) -> bool:
        del fix_turn
        return False

    @staticmethod
    def _prompt_cache_key(repair_packet: FixPromptContext) -> str:
        return FixPromptBuilder.prompt_cache_key(repair_packet)

    @staticmethod
    def _merge_cache_stats(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current or {})
        for key, value in dict(incoming or {}).items():
            if isinstance(value, (int, float)):
                merged[key] = (merged.get(key, 0) or 0) + value
            else:
                merged[key] = value
        return merged

    def _resolve_frontend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        normalized = module_path.replace("@/", "miniapp/app/static/")
        candidates = [normalized]
        if "." not in Path(normalized).name:
            candidates.extend([f"{normalized}.html", f"{normalized}.css", f"{normalized}.js"])
        for candidate in candidates:
            if self._file_exists(workspace_id, run_id, candidate):
                return candidate
        return None

    def _resolve_backend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        normalized = f"miniapp/{module_path.replace('.', '/')}.py"
        return normalized if self._file_exists(workspace_id, run_id, normalized) else None

    def _file_exists(self, workspace_id: str, run_id: str, relative_path: str) -> bool:
        return (self.workspace_service.draft_source_dir(workspace_id, run_id) / relative_path).exists()

    @staticmethod
    def _diff_summary(diff_text: str) -> str:
        files = re.findall(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE)
        if not files:
            return "No diff recorded."
        return f"Updated {len(files)} file(s): {', '.join(files[:5])}"

    def _append_iteration_report(self, workspace_id: str, iteration: RunIterationRecord) -> None:
        report_key = f"iterations:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "items": []}
        items = list(current.get("items", []))
        items.append(iteration.model_dump(mode="json"))
        current["items"] = items
        self._store_report(report_key, current)

    def _clear_reports(self, workspace_id: str, *, preserve_generation_state: bool = False) -> None:
        keys = [
            "validation",
            "check_results",
            "fix_case",
            "fix_attempts",
            "scope_expansions",
            "fix_runtime",
        ]
        if not preserve_generation_state:
            keys.extend(["iterations", "candidate_diff", "patch"])
        for key in keys:
            self.store.delete("reports", f"{key}:{workspace_id}")

    def _save_job(self, job: JobRecord) -> None:
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def _store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.store.upsert("reports", key, payload)

    def _clear_trace(self, workspace_id: str) -> None:
        self._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append(
            {
                "stage": stage,
                "message": message,
                "payload": payload or {},
                "created_at": utc_now().isoformat(),
            }
        )
        current["entries"] = entries
        self._store_report(report_key, current)
        self.workspace_log_service.append(workspace_id, source=f"fix.trace.{stage}", message=message, payload=payload or {})
        logger.info("trace workspace_id=%s stage=%s message=%s", workspace_id, stage, message)

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = utc_now()
        self._sync_run_progress(job, event_type, message, details or {})
        self.workspace_log_service.append(job.workspace_id, source=f"fix.{event_type}", message=message, payload=details or {})
        self._save_job(job)

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any]) -> None:
        if not job.linked_run_id:
            return
        payload = self.store.get("runs", job.linked_run_id)
        if not payload:
            return
        stage, progress = self._run_progress_for_event(event_type)
        payload["linked_job_id"] = job.job_id
        payload["current_stage"] = stage
        payload["progress_percent"] = max(int(payload.get("progress_percent", 0)), progress)
        payload["summary"] = job.summary
        payload["failure_reason"] = job.failure_reason
        payload["failure_class"] = job.failure_class
        payload["failure_signature"] = job.failure_signature
        payload["root_cause_summary"] = job.root_cause_summary
        payload["current_fix_phase"] = job.current_fix_phase
        payload["current_failing_command"] = job.current_failing_command
        payload["current_exit_code"] = job.current_exit_code
        payload["fix_targets"] = list(job.fix_targets)
        payload["remaining_issues"] = list(job.remaining_issues)
        payload["repair_iterations"] = list(job.repair_iterations)
        payload["fix_attempts"] = list(job.fix_attempts)
        payload["scope_expansions"] = list(job.scope_expansions)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", job.linked_run_id, payload)
        logger.info("fix_progress run_id=%s stage=%s progress=%s message=%s", job.linked_run_id, stage, progress, message)

    @staticmethod
    def _run_progress_for_event(event_type: str) -> tuple[str, int]:
        progress_map = {
            "job_started": ("starting fix", 6),
            "triage_started": ("triaging failure", 12),
            "frontend_build_started": ("compiling frontend", 22),
            "backend_compile_started": ("compiling miniapp", 30),
            "preview_validation_started": ("rebuilding preview", 40),
            "triage_completed": ("evidence ready", 48),
            "repair_planned": ("planning repair patch", 58),
            "patch_apply_started": ("applying repair patch", 68),
            "patch_apply_completed": ("repair patch applied", 76),
            "scope_expanded": ("expanding fix scope", 80),
            "failure_reanalyzed": ("reading new failure", 84),
            "repair_iteration": ("retrying repair", 88),
            "checks_completed": ("checks complete", 94),
            "draft_ready": ("awaiting review", 99),
            "job_completed": ("almost complete", 99),
            "job_failed": ("failed", 100),
        }
        return progress_map.get(event_type, ("processing", 12))
