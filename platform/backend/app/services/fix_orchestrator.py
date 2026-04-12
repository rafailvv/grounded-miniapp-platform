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
    ContainerStatusRecord,
    DraftFileOperation,
    FixAttemptOutcome,
    FixAttemptRecord,
    FixCase,
    FixScopeEntry,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairPacket,
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
from app.services.preview_service import PreviewService
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.generation_service import GenerationService


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
            current_fix_phase="triaging",
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

        scope_entries: list[FixScopeEntry] = []
        scope_expansions: list[dict[str, Any]] = []
        fix_attempts: list[FixAttemptRecord] = []
        repair_iterations: list[RepairIterationRecord] = []
        previous_progress_snapshot: dict[str, Any] | None = None
        previous_failure_signature: str | None = None
        repeated_signature_without_progress = 0
        latest_check_execution: CheckExecutionRecord | None = None
        latest_preview_details: dict[str, Any] = {}
        latest_apply_result: dict[str, Any] | None = None
        memory_context = (self.store.get("reports", f"project_memory_context:{workspace_id}") or {}).get("summary")
        active_strategy = "exact_fix"

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            if should_stop and should_stop():
                job.status = "blocked"
                job.failure_reason = "Fix run stopped before completion."
                job.summary = "Fix run stopped before completion."
                job.current_fix_phase = "stopped"
                self._append_event(job, "job_failed", "Fix run was stopped before completion.")
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

            self._append_event(job, "triage_started", f"Triage attempt {attempt} started.", {"attempt": attempt})
            self._append_trace(workspace_id, "triage", "Running exact fix verification checks.", {"attempt": attempt})
            latest_check_execution, latest_preview_details = self._execute_exact_checks(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
                changed_files=[entry.file_path for entry in scope_entries] or ["frontend", "miniapp"],
            )
            job.executed_checks = [result.model_dump(mode="json") for result in latest_check_execution.results]
            job.container_statuses = latest_preview_details.get("containers", [])
            job.current_failing_command = self._first_failing_command(latest_check_execution.results)
            job.current_exit_code = self._first_failing_exit_code(latest_check_execution.results)
            job.validation_snapshot = self._validation_snapshot_from_execution(latest_check_execution)
            self._store_report(
                f"check_results:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "items": [item.model_dump(mode="json") for item in latest_check_execution.results],
                    "duration_ms": latest_check_execution.duration_ms,
                },
            )

            fix_case = self._build_fix_case(
                workspace_id=workspace_id,
                run_id=run_id,
                attempt=attempt,
                request=request,
                check_execution=latest_check_execution,
                preview_details=latest_preview_details,
                prior_attempts=fix_attempts,
                existing_scope=scope_entries,
                memory_context=memory_context,
            )
            job.failure_class = fix_case.failure_class
            job.failure_signature = fix_case.failure_signature
            active_strategy = fix_case.fix_strategy or active_strategy
            job.root_cause_summary = fix_case.root_cause_summary
            job.fix_targets = list(fix_case.implicated_files)
            job.current_fix_phase = "triaging"
            self._store_report(f"fix_case:{workspace_id}", fix_case.model_dump(mode="json"))
            self._append_event(
                job,
                "triage_completed",
                fix_case.root_cause_summary or "Fix evidence packet prepared.",
                {
                    "attempt": attempt,
                    "failure_class": fix_case.failure_class,
                    "fix_strategy": fix_case.fix_strategy,
                    "failure_signature": fix_case.failure_signature,
                    "implicated_files": fix_case.implicated_files,
                },
            )

            if self._is_fix_success(latest_check_execution.results, latest_preview_details):
                latest_check_execution, latest_preview_details = self._execute_final_checks(
                    job=job,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    draft_source=draft_source,
                    changed_files=self._final_check_changed_files(latest_apply_result, fix_case, scope_entries),
                )
                if not self._is_fix_success(latest_check_execution.results, latest_preview_details):
                    continue
                success_attempt = FixAttemptRecord(
                    run_id=run_id,
                    attempt=attempt,
                    diagnosis=fix_case.root_cause_summary or "Fix verification passed.",
                    commands=[result.command for result in latest_check_execution.results if result.command],
                    exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                    files_changed=[],
                    implicated_files=fix_case.implicated_files,
                    failure_signature=fix_case.failure_signature,
                    result="green",
                    expected_verification="Draft compiles and preview runtime is healthy.",
                )
                fix_attempts.append(success_attempt)
                job.status = "completed"
                job.summary = "Fix completed successfully after exact verification."
                job.failure_reason = None
                job.current_fix_phase = "completed"
                job.validation_snapshot = ValidationSnapshot(
                    grounded_spec_valid=True,
                    app_ir_valid=True,
                    build_valid=True,
                    blocking=False,
                    issues=[],
                )
                self._append_event(job, "checks_completed", "Fix checks passed and preview is healthy.", {"attempt": attempt})
                self._append_event(job, "job_completed", "Fix completed successfully.")
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

            if CheckRunner.has_tooling_failure(latest_check_execution.results):
                job.status = "failed"
                job.failure_reason = fix_case.root_cause_summary or "Platform/tooling misconfiguration prevents exact verification."
                job.summary = "Fix stopped because the platform runtime cannot execute required checks."
                job.current_fix_phase = "stopped"
                self._append_event(job, "job_failed", job.failure_reason)
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

            signature = fix_case.failure_signature or ""
            current_progress_snapshot = self._repair_progress_snapshot(
                latest_check_execution.results,
                latest_preview_details,
                fix_case,
            )
            made_progress = self._repair_snapshot_improved(previous_progress_snapshot, current_progress_snapshot)
            if signature and previous_failure_signature == signature and not made_progress:
                repeated_signature_without_progress += 1
            else:
                repeated_signature_without_progress = 0
            if signature and repeated_signature_without_progress >= 3:
                job.status = "failed"
                job.failure_reason = (
                    "Fix loop stopped after expanded-context and full-bundle retries failed to improve the same failure signature."
                )
                job.summary = "Fix loop stopped because repeated repair attempts did not improve the same root-cause cluster."
                job.current_fix_phase = "stopped"
                self._append_event(
                    job,
                    "job_failed",
                    job.failure_reason,
                    {
                        "failure_signature": signature,
                        "repeated_without_progress": repeated_signature_without_progress,
                        "progress_snapshot": current_progress_snapshot,
                    },
                )
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )
            previous_failure_signature = signature or previous_failure_signature
            previous_progress_snapshot = current_progress_snapshot

            strategy_budget = self.MAX_ATTEMPTS
            if attempt >= strategy_budget:
                job.status = "failed"
                job.failure_reason = "Fix loop reached the repair attempt budget without reaching a green build and preview."
                job.summary = "Fix loop reached the repair attempt budget."
                job.current_fix_phase = "stopped"
                self._append_event(
                    job,
                    "job_failed",
                    job.failure_reason,
                    {"fix_strategy": active_strategy, "attempt_budget": strategy_budget},
                )
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

            expanded_scope = self._merge_scope(scope_entries, fix_case.write_scope, scope_expansions)
            if expanded_scope != scope_entries:
                added_paths = [entry.file_path for entry in expanded_scope if entry.file_path not in {item.file_path for item in scope_entries}]
                if added_paths:
                    scope_expansions.append(
                        {
                            "attempt": attempt,
                            "files": added_paths,
                            "reason": "Evidence-based scope expansion from fix case.",
                        }
                    )
                    self._append_event(job, "scope_expanded", "Expanded fix scope based on new evidence.", {"attempt": attempt, "files": added_paths})
                scope_entries = expanded_scope
            elif not scope_entries:
                scope_entries = fix_case.write_scope

            deterministic_operations = self._deterministic_contract_repair_operations(
                workspace_id=workspace_id,
                run_id=run_id,
                fix_case=fix_case,
                scope_entries=scope_entries,
                generation_mode=effective_mode,
            )
            if deterministic_operations:
                self._append_event(
                    job,
                    "repair_planned",
                    "Applying deterministic contract repair before model patching.",
                    {"attempt": attempt, "scope": [operation.file_path for operation in deterministic_operations]},
                )
                envelope = self.workspace_service.build_patch_envelope_for_draft(workspace_id, run_id, deterministic_operations)
                apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
                latest_apply_result = apply_result.model_dump(mode="json")
                if apply_result.status != "applied":
                    job.status = "failed"
                    job.failure_reason = apply_result.conflict_reason or "Deterministic contract repair could not be applied."
                    job.summary = "Fix failed while applying deterministic contract repair."
                    job.current_fix_phase = "patching"
                    self._append_event(job, "job_failed", job.failure_reason)
                    return self._finalize_job(
                        job,
                        fix_attempts=fix_attempts,
                        repair_iterations=repair_iterations,
                        scope_expansions=scope_expansions,
                        latest_execution=latest_check_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                    )
                diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
                self._store_report(f"candidate_diff:{workspace_id}", {"workspace_id": workspace_id, "diff": diff_text})
                self._append_event(
                    job,
                    "patch_apply_completed",
                    "Deterministic contract repair applied to the draft.",
                    {"attempt": attempt, "changed_files": apply_result.changed_files},
                )
                self._append_trace(
                    workspace_id,
                    "patch_apply",
                    "Deterministic contract repair applied to the draft.",
                    {"attempt": attempt, "changed_files": apply_result.changed_files},
                )
                fix_attempts.append(
                    FixAttemptRecord(
                        run_id=run_id,
                        attempt=attempt,
                        diagnosis="Applied deterministic contract repair before model patching.",
                        commands=[result.command for result in latest_check_execution.results if result.command],
                        exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                        files_changed=list(apply_result.changed_files),
                        implicated_files=fix_case.implicated_files,
                        failure_signature=fix_case.failure_signature,
                        result="patched",
                        expected_verification="Deterministic contract repair should reduce route/runtime drift before the next verification pass.",
                    )
                )
                repair_iterations.append(
                    RepairIterationRecord(
                        run_id=run_id,
                        attempt=attempt,
                        files_read=[entry.file_path for entry in scope_entries],
                        files_changed=list(apply_result.changed_files),
                        failure_class=fix_case.failure_class,
                        check_results=latest_check_execution.results,
                        latency_breakdown={"attempt_ms": 0},
                        token_usage={},
                    )
                )
                continue

            repair_context_mode = self._repair_context_mode(fix_case, repeated_signature_without_progress)
            repair_packet = self._build_repair_packet(
                workspace_id=workspace_id,
                run_id=run_id,
                fix_case=fix_case,
                scope_entries=scope_entries,
                context_mode=repair_context_mode,
            )
            self._append_event(
                job,
                "repair_planned",
                "Prepared repair packet for the current failure bundle.",
                {
                    "attempt": attempt,
                    "scope": [entry.file_path for entry in scope_entries],
                    "context_mode": repair_packet.context_mode,
                },
            )
            self._append_trace(
                workspace_id,
                "repair_planned",
                "Prepared repair packet.",
                {
                    "attempt": attempt,
                    "scope": [entry.file_path for entry in scope_entries],
                    "context_mode": repair_packet.context_mode,
                },
            )
            llm_result = self._plan_patch(job=job, repair_packet=repair_packet)
            repair_outcome = self._repair_outcome_from_response(
                llm_result=llm_result,
                repair_packet=repair_packet,
                fix_case=fix_case,
                scope_expansions=scope_expansions,
            )
            if repair_outcome.outcome == "needs_more_context":
                next_context_mode = "full_bundle" if repair_packet.context_mode == "expanded" else "expanded"
                if repair_packet.context_mode == "full_bundle":
                    repair_outcome = FixAttemptOutcome(
                        outcome="fatal_invalid_response",
                        diagnosis=repair_outcome.diagnosis,
                        planned_targets=repair_outcome.planned_targets,
                        validation_error=repair_outcome.validation_error or "Repair requested more context after receiving the full bundle.",
                        expected_verification=repair_outcome.expected_verification,
                        rationale_by_file=repair_outcome.rationale_by_file,
                        raw_response=repair_outcome.raw_response,
                    )
                else:
                    retry_feedback = (
                        "The previous repair response did not return a valid executable patch. "
                        "The full current contents for the requested files are now included. "
                        "Return only concrete create/replace/delete operations for generated app code."
                    )
                    expanded_packet = self._build_repair_packet(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        fix_case=fix_case,
                        scope_entries=scope_entries,
                        context_mode=next_context_mode,
                        additional_paths=repair_outcome.planned_targets,
                    )
                    self._append_event(
                        job,
                        "repair_scope_expanded",
                        "Retrying repair planning with a larger repair packet.",
                        {
                            "attempt": attempt,
                            "from_context_mode": repair_packet.context_mode,
                            "to_context_mode": expanded_packet.context_mode,
                            "planned_targets": repair_outcome.planned_targets,
                            "validation_error": repair_outcome.validation_error,
                        },
                    )
                    llm_result = self._plan_patch(
                        job=job,
                        repair_packet=expanded_packet,
                        repair_feedback=retry_feedback,
                    )
                    repair_outcome = self._repair_outcome_from_response(
                        llm_result=llm_result,
                        repair_packet=expanded_packet,
                        fix_case=fix_case,
                        scope_expansions=scope_expansions,
                    )
                    repair_packet = expanded_packet
            if repair_outcome.outcome != "patch_ready":
                job.status = "failed"
                job.failure_reason = repair_outcome.validation_error or "Repair model did not return a valid executable patch."
                job.summary = "Fix failed because the repair response was incomplete, invalid, or out of scope."
                job.current_fix_phase = "patching"
                self._append_event(
                    job,
                    "job_failed",
                    job.failure_reason,
                    {
                        "repair_outcome": repair_outcome.outcome,
                        "planned_targets": repair_outcome.planned_targets,
                    },
                )
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )
            operations = list(repair_outcome.operations)

            self._append_event(job, "patch_apply_started", "Applying minimal repair patch.", {"attempt": attempt, "files": [operation.file_path for operation in operations]})
            envelope = self.workspace_service.build_patch_envelope_for_draft(workspace_id, run_id, operations)
            apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
            latest_apply_result = apply_result.model_dump(mode="json")
            self._store_report(
                f"patch:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "envelope": envelope.model_dump(mode="json"),
                    "apply_result": latest_apply_result,
                },
            )
            if apply_result.status != "applied":
                attempt_record = FixAttemptRecord(
                    run_id=run_id,
                    attempt=attempt,
                    diagnosis=str(repair_outcome.diagnosis or "Patch conflict while applying the repair."),
                    commands=[result.command for result in latest_check_execution.results if result.command],
                    exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                    files_changed=[],
                    implicated_files=fix_case.implicated_files,
                    failure_signature=fix_case.failure_signature,
                    result="conflict",
                    rationale_by_file=dict(repair_outcome.rationale_by_file),
                    expected_verification=repair_outcome.expected_verification,
                )
                fix_attempts.append(attempt_record)
                job.status = "failed"
                job.failure_reason = apply_result.conflict_reason or "Repair patch conflicted with the current draft."
                job.summary = "Fix stopped because the repair patch could not be applied safely."
                job.current_fix_phase = "patching"
                self._append_event(job, "job_failed", job.failure_reason)
                return self._finalize_job(
                    job,
                    fix_attempts=fix_attempts,
                    repair_iterations=repair_iterations,
                    scope_expansions=scope_expansions,
                    latest_execution=latest_check_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                )

            diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
            self._store_report(f"candidate_diff:{workspace_id}", {"workspace_id": workspace_id, "diff": diff_text})
            self._append_event(job, "patch_apply_completed", "Repair patch applied to the draft.", {"attempt": attempt, "changed_files": apply_result.changed_files})
            self._append_event(job, "repair_iteration", f"Repair attempt {attempt} applied. Re-running verification.", {"attempt": attempt})
            self._append_trace(
                workspace_id,
                "patch_apply",
                "Repair patch applied to the draft.",
                {"attempt": attempt, "changed_files": apply_result.changed_files},
            )

            attempt_record = FixAttemptRecord(
                run_id=run_id,
                attempt=attempt,
                diagnosis=str(repair_outcome.diagnosis or "Applied a minimal repair patch."),
                commands=[result.command for result in latest_check_execution.results if result.command],
                exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                files_changed=list(apply_result.changed_files),
                implicated_files=fix_case.implicated_files,
                failure_signature=fix_case.failure_signature,
                result="patched",
                rationale_by_file=dict(repair_outcome.rationale_by_file),
                expected_verification=repair_outcome.expected_verification,
            )
            fix_attempts.append(attempt_record)

            repair_iterations.append(
                RepairIterationRecord(
                    run_id=run_id,
                    attempt=attempt,
                    files_read=[entry.file_path for entry in scope_entries],
                    files_changed=list(apply_result.changed_files),
                    failure_class=fix_case.failure_class,
                    check_results=latest_check_execution.results,
                    latency_breakdown={"attempt_ms": 0},
                    token_usage={},
                )
            )
            if self.session_engine is not None:
                diminishing = self.session_engine.should_stop_for_diminishing_returns(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    phase="fix_repair",
                    generation_mode=effective_mode.value,
                    metrics={
                        "attempt": attempt,
                        "changed_files_count": len(apply_result.changed_files),
                        "diff_chars": len(diff_text),
                        "failure_signature": fix_case.failure_signature,
                        "total_tokens": int(job.cache_stats.get("total_tokens", 0) or 0),
                    },
                )
                if diminishing.get("should_stop"):
                    job.status = "failed"
                    job.failure_reason = str(diminishing.get("reason") or "Fix stopped due to diminishing returns.")
                    job.summary = "Fix loop stopped because additional iterations were no longer producing meaningful changes."
                    job.current_fix_phase = "stopped"
                    self._append_event(job, "job_failed", job.failure_reason)
                    return self._finalize_job(
                        job,
                        fix_attempts=fix_attempts,
                        repair_iterations=repair_iterations,
                        scope_expansions=scope_expansions,
                        latest_execution=latest_check_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                    )
            run_iteration = RunIterationRecord(
                run_id=run_id,
                assistant_message=attempt_record.diagnosis,
                files_read=[entry.file_path for entry in scope_entries],
                operations=[
                    RunIterationOperation(
                        file_path=operation.file_path,
                        operation=operation.operation,
                        reason=operation.reason,
                    )
                    for operation in operations
                ],
                check_results=latest_check_execution.results,
                diff_summary=self._diff_summary(diff_text),
                role_scope=request.target_role_scope,
                latency_breakdown={},
                token_usage={},
                failure_class=fix_case.failure_class,
            )
            self._append_iteration_report(workspace_id, run_iteration)
            self._store_report(f"fix_attempts:{workspace_id}", {"workspace_id": workspace_id, "items": [item.model_dump(mode="json") for item in fix_attempts]})
            self._store_report(f"scope_expansions:{workspace_id}", {"workspace_id": workspace_id, "items": scope_expansions})

        job.status = "failed"
        job.failure_reason = "Fix loop reached the repair attempt budget without reaching a green build and preview."
        job.summary = "Fix stopped after exhausting the repair budget."
        job.current_fix_phase = "stopped"
        self._append_event(job, "job_failed", job.failure_reason)
        return self._finalize_job(
            job,
            fix_attempts=fix_attempts,
            repair_iterations=repair_iterations,
            scope_expansions=scope_expansions,
            latest_execution=latest_check_execution,
            latest_preview_details=latest_preview_details,
            latest_apply_result=latest_apply_result,
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
        fix_case: FixCase,
        scope_entries: list[FixScopeEntry],
    ) -> list[str]:
        if latest_apply_result and latest_apply_result.get("changed_files"):
            return [str(path) for path in latest_apply_result.get("changed_files") or []]
        if fix_case.implicated_files:
            return list(fix_case.implicated_files)
        return [entry.file_path for entry in scope_entries]

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
    ) -> FixCase:
        raw_error = request.error_context.raw_error if request.error_context else request.prompt
        combined_text = "\n".join(
            [
                raw_error,
                *[item.details or "" for item in check_execution.results],
                *[line for result in check_execution.results for line in result.logs],
                *(preview_details.get("logs") or []),
            ]
        )
        combined_text = self._augment_failure_evidence_from_test_results(combined_text, check_execution.results)
        implicated_files = self._implicated_files(workspace_id, run_id, combined_text, existing_scope)
        failure_class = self._specialized_failure_class(
            workspace_id=workspace_id,
            run_id=run_id,
            results=check_execution.results,
            combined_text=combined_text,
            implicated_files=implicated_files,
        )
        if not failure_class:
            failure_class = self._classify_failure_text(combined_text) or CheckRunner.classify_failure(check_execution.results) or "build/runtime"
        root_cause = self._root_cause_summary(check_execution.results, preview_details, raw_error)
        failure_signature = self._failure_signature(failure_class, root_cause)
        fix_strategy = "exact_fix"
        write_scope = self._build_write_scope(workspace_id, run_id, implicated_files, failure_class, existing_scope, fix_strategy)
        excerpt = self._error_excerpt(check_execution.results, preview_details, raw_error)
        container_statuses = [
            ContainerStatusRecord.model_validate(item)
            for item in preview_details.get("containers", [])
            if isinstance(item, dict)
        ]
        return FixCase(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt=attempt,
            failure_class=failure_class,
            failure_signature=failure_signature,
            fix_strategy=fix_strategy,
            failing_command=self._first_failing_command(check_execution.results),
            root_cause_summary=root_cause,
            exact_error_excerpt=excerpt,
            implicated_files=implicated_files,
            container_statuses=container_statuses,
            container_logs=preview_details.get("container_logs", {}),
            write_scope=write_scope,
            attempt_history=[item.model_dump(mode="json") for item in prior_attempts[-4:]],
            executed_checks=check_execution.results,
            memory_context=memory_context,
        )

    def _build_repair_packet(
        self,
        *,
        workspace_id: str,
        run_id: str,
        fix_case: FixCase,
        scope_entries: list[FixScopeEntry],
        context_mode: str,
        additional_paths: list[str] | None = None,
    ) -> RepairPacket:
        full_files = context_mode in {"expanded", "full_bundle"} or self._needs_full_context_first(fix_case)
        budget = self.MAX_CONTEXT_CHARS_EXPANDED if full_files else self.MAX_CONTEXT_CHARS
        file_contexts = self._collect_file_contexts(
            workspace_id,
            run_id,
            scope_entries,
            fix_case=fix_case,
            budget_override=budget,
            full_files=full_files,
        )
        extra_paths = list(additional_paths or [])
        if context_mode == "full_bundle":
            extra_paths.extend(self._deterministic_contract_seed_paths(workspace_id, run_id, fix_case, scope_entries))
        if extra_paths:
            file_contexts = self._merge_additional_context_paths(
                workspace_id,
                run_id,
                file_contexts,
                extra_paths,
                budget_override=budget,
            )
        return RepairPacket(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt=fix_case.attempt,
            failure_class=fix_case.failure_class,
            failure_signature=fix_case.failure_signature,
            root_cause_summary=fix_case.root_cause_summary,
            exact_error_excerpt=fix_case.exact_error_excerpt,
            context_mode=context_mode,
            failing_checks=[
                {
                    "name": item.name,
                    "status": item.status,
                    "details": item.details,
                    "logs": item.logs[-12:],
                }
                for item in fix_case.executed_checks
                if item.status == "failed"
            ],
            failing_file_paths=list(fix_case.implicated_files),
            deterministic_companions=[entry.file_path for entry in scope_entries],
            expected_contract=self._expected_contract_snapshot(fix_case),
            file_contexts=file_contexts,
            read_only_surfaces=self._read_only_surfaces(),
        )

    @staticmethod
    def _read_only_surfaces() -> list[str]:
        return [
            "miniapp/tests/",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/app/generated/static_runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        ]

    @classmethod
    def _expected_contract_snapshot(cls, fix_case: FixCase) -> dict[str, Any]:
        evidence = "\n".join(
            [
                str(fix_case.root_cause_summary or ""),
                str(fix_case.exact_error_excerpt or ""),
                *[item.details or "" for item in fix_case.executed_checks],
                *[line for item in fix_case.executed_checks for line in item.logs],
            ]
        )
        required_api_routes = sorted(
            {
                endpoint
                for endpoint in re.findall(r"/api/([a-zA-Z0-9_-]+)", evidence)
                if endpoint
            }
        )
        required_role_routes = sorted(
            {
                route
                for route in re.findall(r"(/(?:client|specialist|manager)(?:/[A-Za-z0-9_{}:-]+)*/?)", evidence)
                if route
            }
        )
        required_exports: list[str] = []
        if "get_db" in evidence:
            required_exports.append("get_db")
        return {
            "strict_green": True,
            "runtime_manifest_aliases": {"sample": "client"},
            "canonical_api_aliases": {
                "submission": "requests",
                "submissions": "requests",
                "booking": "requests",
                "bookings": "requests",
                "specialist": "users",
                "specialists": "users",
            },
            "required_api_routes": required_api_routes,
            "required_role_routes": required_role_routes,
            "required_exports": required_exports,
            "read_only_surfaces": cls._read_only_surfaces(),
        }

    @staticmethod
    def _repair_context_mode(fix_case: FixCase, repeated_signature_without_progress: int) -> str:
        route_runtime_failure = str(fix_case.failure_class or "") in {
            "runtime_manifest_route_missing",
            "router_not_registered",
            "api_endpoint_missing",
            "frontend_link_route_mismatch",
            "db_dependency_export_missing",
        }
        if repeated_signature_without_progress >= 2:
            return "full_bundle"
        if repeated_signature_without_progress >= 1 or route_runtime_failure:
            return "expanded"
        return "minimal"

    @staticmethod
    def _needs_full_context_first(fix_case: FixCase) -> bool:
        return str(fix_case.failure_class or "") in {
            "runtime_manifest_route_missing",
            "router_not_registered",
            "api_endpoint_missing",
            "frontend_link_route_mismatch",
            "db_dependency_export_missing",
        }

    @staticmethod
    def _repair_progress_snapshot(
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        fix_case: FixCase,
    ) -> dict[str, Any]:
        issues = CheckRunner.failing_issues(results)
        evidence = "\n".join(
            [
                str(fix_case.root_cause_summary or ""),
                str(fix_case.exact_error_excerpt or ""),
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
        repair_packet: RepairPacket,
        fix_case: FixCase,
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
                [FixScopeEntry(file_path=path, reason="Repair packet companion scope.") for path in repair_packet.deterministic_companions],
                fix_case,
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
                outcome="fatal_invalid_response",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                validation_error=str(exc),
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        if not operations:
            return FixAttemptOutcome(
                outcome="fatal_invalid_response",
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
        repair_packet: RepairPacket,
        repair_feedback: str | None = None,
    ) -> dict[str, Any]:
        if not self.openrouter_client.enabled:
            return {"error": "Fix mode requires an enabled LLM provider or a deterministic local repair path."}
        job.current_fix_phase = "patching"
        self._save_job(job)
        prompt_cache_key = self._prompt_cache_key(repair_packet)
        try:
            payload = self.openrouter_client.generate_repair(
                schema_name="fix_patch_v1",
                schema=self._repair_schema(),
                system_prompt=self._repair_system_prompt(),
                user_prompt=self._repair_user_prompt(repair_packet, repair_feedback=repair_feedback),
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
                repair_packet.workspace_id,
                repair_packet.run_id,
            )
            return {"error": f"Repair patch generation failed: {exc}"}

    def _coerce_operations(
        self,
        raw_operations: list[Any],
        scope_entries: list[FixScopeEntry],
        fix_case: FixCase,
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
            if operation.file_path.startswith("miniapp/tests/") and not self._allow_test_file_writes(fix_case):
                raise ValueError(f"Repair attempted to edit generated tests instead of the app surface: {operation.file_path}")
            if self._is_read_only_generated_surface(operation.file_path):
                raise ValueError(f"Repair attempted to edit a generated manifest surface instead of the app bundle: {operation.file_path}")
            if operation.file_path not in scope_paths:
                if len(scope_expansions) >= self.MAX_SCOPE_EXPANSIONS or not self._can_expand_for_file(operation.file_path, fix_case.implicated_files):
                    raise ValueError(f"Repair touched files outside the allowed evidence-based scope: {operation.file_path}")
                scope_expansions.append(
                    {
                        "attempt": fix_case.attempt,
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
            "miniapp/app/generated/static_runtime_manifest.json",
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
        fix_strategy: str,
    ) -> list[FixScopeEntry]:
        entries = {entry.file_path: entry for entry in existing_scope}
        for file_path in implicated_files:
            entries.setdefault(file_path, FixScopeEntry(file_path=file_path, reason="Directly implicated by the current failure evidence."))
            for companion in self._deterministic_companion_scope(file_path):
                if self._file_exists(workspace_id, run_id, companion) or self._allow_missing_scope_path(companion):
                    entries.setdefault(companion, FixScopeEntry(file_path=companion, reason="Included as a deterministic companion of the failing bundle."))
        for candidate in self._structural_scope_bundle(workspace_id, run_id, implicated_files, failure_class):
            entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Included as part of the deterministic contract bundle."))
        if failure_class.startswith("preview_runtime") or failure_class.startswith("runtime") or failure_class.startswith("tooling"):
            for candidate in ("docker/docker-compose.yml", "miniapp/requirements.txt", "miniapp/app/main.py"):
                if self._file_exists(workspace_id, run_id, candidate) or self._allow_missing_scope_path(candidate):
                    entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Runtime or preview glue may be involved in the current failure."))
        if not self._allow_test_file_writes_for_failure(failure_class):
            entries = {path: entry for path, entry in entries.items() if not path.startswith("miniapp/tests/")}
        if not entries:
            for fallback in ("miniapp/app/static", "miniapp/app"):
                entries.setdefault(fallback, FixScopeEntry(file_path=fallback, reason="Fallback repair surface for the current failure cluster."))
        return list(entries.values())

    @staticmethod
    def _deterministic_companion_scope(file_path: str) -> list[str]:
        normalized = str(file_path or "").strip().replace("\\", "/")
        companions: list[str] = []
        if normalized.startswith("miniapp/app/static/"):
            path_obj = Path(normalized)
            if normalized.endswith("/index.html") or normalized.endswith("/styles.css") or normalized.endswith("/app.js"):
                base_dir = path_obj.parent
                companions.extend(
                    [
                        str(base_dir / "index.html").replace("\\", "/"),
                        str(base_dir / "styles.css").replace("\\", "/"),
                        str(base_dir / "app.js").replace("\\", "/"),
                    ]
                )
            elif normalized.endswith("/index.html") is False and path_obj.suffix in {".html", ".css", ".js"}:
                base = path_obj.with_suffix("")
                companions.extend(
                    [
                        f"{base}.html",
                        f"{base}.css",
                        f"{base}.js",
                    ]
                )
        elif normalized in {"miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py", "miniapp/app/routes/profiles.py"}:
            companions.extend(
                [
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                    "miniapp/app/routes/profiles.py",
                ]
            )
        elif normalized.startswith("miniapp/app/routes/") and normalized.endswith(".py"):
            companions.extend(
                [
                    normalized,
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                ]
            )
        return list(dict.fromkeys(path for path in companions if path))

    def _structural_scope_bundle(
        self,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
    ) -> list[str]:
        bundle: list[str] = []
        for candidate in (
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/main.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/app/generated/static_runtime_manifest.json",
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
            "artifacts/generated_app_graph.json",
        ):
            if self._file_exists(workspace_id, run_id, candidate) or self._allow_missing_scope_path(candidate):
                bundle.append(candidate)
        for file_path in implicated_files:
            if file_path.startswith("miniapp/app/routes/"):
                bundle.append(file_path)
            if file_path.startswith("miniapp/app/static/") and file_path.endswith((".html", ".css", ".js")):
                parent = str(Path(file_path).parent)
                if self._file_exists(workspace_id, run_id, parent) or self._allow_missing_scope_path(file_path):
                    bundle.append(parent)
        if "route" in failure_class or "contract" in failure_class:
            routes_dir = "miniapp/app/routes"
            if self._file_exists(workspace_id, run_id, routes_dir):
                bundle.append(routes_dir)
        return list(dict.fromkeys(bundle))

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
        merged = {entry.file_path: entry for entry in current_scope}
        for entry in next_scope:
            merged.setdefault(entry.file_path, entry)
        if len(scope_expansions) > FixOrchestrator.MAX_SCOPE_EXPANSIONS:
            return current_scope
        return list(merged.values())

    def _collect_file_contexts(
        self,
        workspace_id: str,
        run_id: str,
        scope_entries: list[FixScopeEntry],
        *,
        fix_case: FixCase | None = None,
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
        for support_path in self._repair_support_files(fix_case):
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
        fix_case: FixCase,
        scope_entries: list[FixScopeEntry],
        generation_mode: GenerationMode,
    ) -> list[DraftFileOperation]:
        if self.generation_service is None:
            return []
        page_graph = self._page_graph_for_deterministic_repair(workspace_id, run_id)
        role_scope = [role for role in ((page_graph.get("roles") or {}).keys()) if role in {"client", "specialist", "manager"}]
        if not role_scope:
            role_scope = ["client", "specialist", "manager"]
        seed_paths = self._deterministic_contract_seed_paths(workspace_id, run_id, fix_case, scope_entries)
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
        fix_case: FixCase,
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
        route_root = self.workspace_service.draft_source_dir(fix_case.workspace_id, run_id) / "miniapp/app/routes"
        if route_root.exists():
            for route_file in sorted(route_root.glob("*.py")):
                relative = f"miniapp/app/routes/{route_file.name}"
                if relative not in candidates:
                    candidates.append(relative)
        for entry in scope_entries:
            if entry.file_path not in candidates:
                candidates.append(entry.file_path)
        for file_path in fix_case.implicated_files:
            if file_path not in candidates:
                candidates.append(file_path)
        unique: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip().lstrip("./")
            if not normalized or normalized in unique:
                continue
            if self._file_exists(fix_case.workspace_id, run_id, normalized):
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
            )
        )

    @staticmethod
    def _repair_support_files(fix_case: FixCase | None) -> list[str]:
        if fix_case is None:
            return []
        connectivity_codes = {
            "connectivity.missing_ui_loading_state",
            "connectivity.missing_ui_error_state",
            "connectivity.missing_backend_route",
        }
        evidence = "\n".join(
            [
                str(fix_case.failure_class or ""),
                str(fix_case.root_cause_summary or ""),
                str(fix_case.exact_error_excerpt or ""),
                *[result.details or "" for result in fix_case.executed_checks],
                *[line for result in fix_case.executed_checks for line in result.logs],
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
                    "enum": ["patch_ready", "needs_more_context", "fatal_invalid_response"],
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

    @staticmethod
    def _repair_system_prompt() -> str:
        return (
            "You are a focused software repair agent. "
            "Diagnose the current failure packet, patch only the generated app files justified by the evidence, "
            "keep the diff minimal, and aim for strict-green validation: validators, generated tests, and preview runtime all passing. "
            "Do not redesign the app. Fix the current root-cause cluster only. "
            "Preserve the existing backend architecture, routers, and static mounting unless the evidence explicitly implicates them. "
            "Never replace a functioning FastAPI backend or route module with placeholder HTML handlers, stub pages, or a simplified demo app. "
            "Do not rewrite generated tests or generated manifests to make the app pass; repair the application code and runtime contract instead."
        )

    @staticmethod
    def _repair_user_prompt(
        repair_packet: RepairPacket,
        *,
        repair_feedback: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "task": "Patch the draft workspace to resolve the current failing bundle and get the checks green.",
                "repair_packet": repair_packet.model_dump(mode="json"),
                "repair_feedback": repair_feedback,
                "rules": [
                    "Fix only the current root-cause cluster before moving on.",
                    "Return the smallest safe patch.",
                    "Prefer editing the failing file and its deterministic bundle companions over broad refactors.",
                    "Treat repair_packet.expected_contract and deterministic_companions as the source of truth for repair scope.",
                    "Only change generated app code. Generated tests, generated manifests, and platform runtime assets are read-only.",
                    "Do not modify miniapp/tests/*; default to repairing app code instead of test code.",
                    "Do not modify generated manifests such as route_manifest.json or generated_app_graph.json; repair the application bundle so the deterministic manifest builder stays correct.",
                    "Do not replace route modules with placeholder text/html responses to satisfy navigation tests; repair real route wiring and page surfaces.",
                    "The fix is considered successful only if validators, generated tests, and preview runtime are all green.",
                    "Preserve existing endpoints, router wiring, and static file serving unless the evidence shows they are broken.",
                    "Do not replace main.py, route modules, or backend services with placeholder HTML stubs or hard-coded pages.",
                    "Every create or replace operation must include the full resulting file content.",
                    "Always return outcome=patch_ready, outcome=needs_more_context, or outcome=fatal_invalid_response.",
                    "If you need more context, return outcome=needs_more_context with planned_targets and no operations.",
                    "Do not return diagnosis-only responses without an explicit outcome and executable patch state.",
                ],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _allow_test_file_writes_for_failure(failure_class: str | None) -> bool:
        del failure_class
        return False

    @classmethod
    def _allow_test_file_writes(cls, fix_case: FixCase) -> bool:
        del fix_case
        return False

    @staticmethod
    def _prompt_cache_key(repair_packet: RepairPacket) -> str:
        digest = hashlib.sha1(
            "|".join(
                [
                    repair_packet.failure_class or "unknown",
                    repair_packet.failure_signature or "unknown",
                    repair_packet.context_mode,
                    ",".join(sorted(repair_packet.deterministic_companions)),
                ]
            ).encode("utf-8")
        ).hexdigest()
        return f"fix:{digest}"

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
