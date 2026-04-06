from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    ContainerStatusRecord,
    DraftFileOperation,
    FixAttemptRecord,
    FixCase,
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
from app.services.preview_service import PreviewService
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService

logger = logging.getLogger(__name__)


class FixOrchestrator:
    MAX_ATTEMPTS = 12
    MAX_SCOPE_EXPANSIONS = 4
    MAX_CONTEXT_CHARS = 12000
    MAX_CONTEXT_CHARS_EXPANDED = 32000
    STRATEGY_ATTEMPTS = {
        "targeted_fix": 8,
        "feature_adjustment": 10,
        "structural_fix": 12,
    }

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
        prior_signatures: list[str] = []
        latest_check_execution: CheckExecutionRecord | None = None
        latest_preview_details: dict[str, Any] = {}
        latest_apply_result: dict[str, Any] | None = None
        memory_context = (self.store.get("reports", f"project_memory_context:{workspace_id}") or {}).get("summary")
        active_strategy = "targeted_fix"

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
            repeated_signature_budget = 2 if active_strategy == "structural_fix" else 1
            if signature and len(prior_signatures) >= repeated_signature_budget and all(
                previous == signature for previous in prior_signatures[-repeated_signature_budget:]
            ):
                if active_strategy == "structural_fix" and self._scope_can_still_expand(scope_entries, fix_case.write_scope):
                    prior_signatures.append(signature)
                    continue
                job.status = "failed"
                job.failure_reason = (
                    "Fix loop stopped after the same failure repeated without evidence of progress."
                    if active_strategy == "structural_fix"
                    else "Fix loop stopped after the same failure repeated twice in a row."
                )
                job.summary = "Fix loop stopped because the root cause signature did not change."
                job.current_fix_phase = "stopped"
                self._append_event(job, "job_failed", job.failure_reason, {"failure_signature": signature})
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
            if signature:
                prior_signatures.append(signature)

            strategy_budget = self._attempt_budget_for_strategy(active_strategy)
            if attempt >= strategy_budget:
                job.status = "failed"
                job.failure_reason = f"Fix loop reached the {active_strategy} repair attempt budget without reaching a green build and preview."
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

            file_contexts = self._collect_file_contexts(workspace_id, run_id, scope_entries, fix_case=fix_case)
            self._append_event(job, "repair_planned", "Prepared minimal repair packet.", {"attempt": attempt, "scope": [entry.file_path for entry in scope_entries]})
            self._append_trace(
                workspace_id,
                "repair_planned",
                "Prepared minimal repair packet.",
                {"attempt": attempt, "scope": [entry.file_path for entry in scope_entries]},
            )
            llm_result = self._plan_patch(job=job, fix_case=fix_case, file_contexts=file_contexts)
            if "error" in llm_result:
                job.status = "failed"
                job.failure_reason = str(llm_result["error"])
                job.summary = "Fix failed while generating the next repair patch."
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

            raw_operations = llm_result.get("operations") or []
            diagnosis_text = str(llm_result.get("diagnosis") or "")
            if not raw_operations and self._looks_like_context_refusal(diagnosis_text):
                expanded_contexts = self._collect_file_contexts(
                    workspace_id,
                    run_id,
                    scope_entries,
                    fix_case=fix_case,
                    budget_override=self.MAX_CONTEXT_CHARS_EXPANDED,
                    full_files=True,
                )
                if expanded_contexts and expanded_contexts != file_contexts:
                    self._append_event(
                        job,
                        "repair_planned",
                        "Repair model requested more file context; retrying patch planning with expanded excerpts.",
                        {"attempt": attempt, "scope": [entry.file_path for entry in scope_entries], "expanded_context": True},
                    )
                    llm_result = self._plan_patch(job=job, fix_case=fix_case, file_contexts=expanded_contexts)
                    raw_operations = llm_result.get("operations") or []
                    diagnosis_text = str(llm_result.get("diagnosis") or "")

            operations = self._coerce_operations(raw_operations, scope_entries, fix_case, scope_expansions)
            if not operations:
                job.status = "failed"
                job.failure_reason = "Repair model did not return any patch operations."
                job.summary = "Fix failed because no concrete patch was proposed."
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
                    diagnosis=str(llm_result.get("diagnosis") or "Patch conflict while applying the repair."),
                    commands=[result.command for result in latest_check_execution.results if result.command],
                    exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                    files_changed=[],
                    implicated_files=fix_case.implicated_files,
                    failure_signature=fix_case.failure_signature,
                    result="conflict",
                    rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                    expected_verification=str(llm_result.get("expected_verification") or ""),
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
                diagnosis=str(llm_result.get("diagnosis") or "Applied a minimal repair patch."),
                commands=[result.command for result in latest_check_execution.results if result.command],
                exit_codes={result.name: result.exit_code for result in latest_check_execution.results},
                files_changed=list(apply_result.changed_files),
                implicated_files=fix_case.implicated_files,
                failure_signature=fix_case.failure_signature,
                result="patched",
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                expected_verification=str(llm_result.get("expected_verification") or ""),
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

    @classmethod
    def _attempt_budget_for_strategy(cls, strategy: str | None) -> int:
        return cls.STRATEGY_ATTEMPTS.get(str(strategy or "targeted_fix"), cls.MAX_ATTEMPTS)

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
        failure_class = self._classify_failure_text(combined_text) or CheckRunner.classify_failure(check_execution.results) or "build/runtime"
        root_cause = self._root_cause_summary(check_execution.results, preview_details, raw_error)
        failure_signature = self._failure_signature(failure_class, root_cause)
        implicated_files = self._implicated_files(workspace_id, run_id, combined_text, existing_scope)
        fix_strategy = self._classify_fix_strategy(
            raw_error=raw_error,
            failure_class=failure_class,
            root_cause=root_cause,
            check_results=check_execution.results,
            implicated_files=implicated_files,
        )
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

    def _plan_patch(self, *, job: JobRecord, fix_case: FixCase, file_contexts: dict[str, str]) -> dict[str, Any]:
        if not self.openrouter_client.enabled:
            return {"error": "Fix mode requires an enabled LLM provider or a deterministic local repair path."}
        job.current_fix_phase = "patching"
        self._save_job(job)
        prompt_cache_key = self._prompt_cache_key(fix_case)
        try:
            payload = self.openrouter_client.generate_repair(
                schema_name="fix_patch_v1",
                schema=self._repair_schema(),
                system_prompt=self._repair_system_prompt(),
                user_prompt=self._repair_user_prompt(fix_case, file_contexts),
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
            logger.exception("fix_patch_generation_failed workspace_id=%s run_id=%s", fix_case.workspace_id, fix_case.run_id)
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
            if operation.file_path.startswith("miniapp/tests/") and not self._allow_test_file_writes(fix_case):
                raise ValueError(f"Repair attempted to edit generated tests instead of the app surface: {operation.file_path}")
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
        if fix_strategy == "structural_fix":
            for candidate in self._structural_scope_bundle(workspace_id, run_id, implicated_files, failure_class):
                entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Included as part of a structural fix bundle."))
        elif fix_strategy == "feature_adjustment":
            for candidate in self._feature_scope_bundle(workspace_id, run_id, implicated_files):
                entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Included as part of a feature-local fix bundle."))
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
        )
        return any(marker in lowered for marker in markers)

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

    def _classify_fix_strategy(
        self,
        *,
        raw_error: str,
        failure_class: str,
        root_cause: str,
        check_results: list[RunCheckResult],
        implicated_files: list[str],
    ) -> str:
        evidence = "\n".join(
            [
                raw_error,
                failure_class,
                root_cause,
                *[result.details or "" for result in check_results],
                *[line for result in check_results for line in result.logs],
            ]
        ).lower()
        structural_markers = (
            "app/runtime_test",
            "build.in_memory_route_store",
            "build.inline_route_schema_model",
            "build.missing_db_module",
            "build.missing_schemas_module",
            "build.invalid_db_module",
            "build.missing_backend_surface",
            "build.page_script_dom_contract",
            "build.page_missing_shell_style_link",
            "connectivity.missing_backend_route",
            "route manifest",
            "runtime manifest",
            "sqlalchemy",
            "schemas.py",
            "db.py",
            "sessionlocal",
            "roleprofilerecord",
            "not declared in route_manifest.json",
            "missing the required profile page",
            "generated_app_python_tests",
            "generated_app_js_tests",
            "navigation mismatch",
            "contract mismatch",
        )
        feature_markers = (
            "feature",
            "adjust",
            "tweak",
            "change behavior",
            "change the page",
            "update the page",
        )
        if any(marker in evidence for marker in structural_markers):
            return "structural_fix"
        failed_names = {result.name for result in check_results if result.status == "failed"}
        if "generated_app_python_tests" in failed_names and any(
            marker in evidence
            for marker in (
                "nameerror",
                "sessionlocal",
                "cannot import name",
                "importerror",
                "roleprofilerecord",
                "main.py",
                "db.py",
                "schemas.py",
            )
        ):
            return "structural_fix"
        if "generated_app_js_tests" in failed_names and any(
            marker in evidence
            for marker in (
                "route ",
                "route_manifest.json",
                "/profile",
                "page js asset",
                "dom ids required by app.js",
            )
        ):
            return "structural_fix"
        if any(marker in evidence for marker in feature_markers):
            return "feature_adjustment"
        if len(implicated_files) >= 5:
            return "structural_fix"
        return "targeted_fix"

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
            "Diagnose the current failure packet, patch only the files justified by the evidence, "
            "keep the diff minimal, and aim for a green compile plus healthy preview runtime. "
            "Do not redesign the app. Fix the current root-cause cluster only. "
            "Preserve the existing backend architecture, routers, and static mounting unless the evidence explicitly implicates them. "
            "Never replace a functioning FastAPI backend or route module with placeholder HTML handlers, stub pages, or a simplified demo app. "
            "Do not rewrite generated tests to make the app pass; repair the application code and runtime contract instead."
        )

    @staticmethod
    def _repair_user_prompt(fix_case: FixCase, file_contexts: dict[str, str]) -> str:
        return json.dumps(
            {
                "task": "Patch the draft workspace to resolve the current root-cause cluster.",
                "fix_case": fix_case.model_dump(mode="json"),
                "fix_strategy": fix_case.fix_strategy or "targeted_fix",
                "memory_context": fix_case.memory_context,
                "file_contexts": file_contexts,
                "rules": [
                    "Fix only the current root-cause cluster before moving on.",
                    "Return the smallest safe patch.",
                    "Prefer editing implicated files over broad refactors.",
                    "If fix_strategy is structural_fix, prefer a coordinated multi-file repair across the implicated contract bundle instead of a one-file patch that leaves the architecture inconsistent.",
                    "If fix_strategy is targeted_fix, keep the patch narrow and avoid touching unrelated architecture files.",
                    "If fix_strategy is feature_adjustment, keep the change local to the implicated feature surface and its immediate page/backend companions.",
                    "Respect the provided write scope unless a directly adjacent dependency is required.",
                    "Do not modify miniapp/tests/* unless the failure packet explicitly shows the generated tests themselves are wrong; default to repairing app code instead of test code.",
                    "Do not replace route modules with placeholder text/html responses to satisfy navigation tests; repair real route wiring and page surfaces.",
                    "Keep miniapp/app/generated/route_manifest.json in its canonical object shape with per-role pages; do not rewrite it into an ad hoc list format.",
                    "The fix is considered successful only if the app compiles and the preview runtime becomes healthy.",
                    "Preserve existing endpoints, router wiring, and static file serving unless the evidence shows they are broken.",
                    "Do not replace main.py, route modules, or backend services with placeholder HTML stubs or hard-coded pages.",
                ],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _allow_test_file_writes_for_failure(failure_class: str | None) -> bool:
        lowered = str(failure_class or "").lower()
        return lowered.startswith("tooling/") or lowered.startswith("test_harness/")

    @classmethod
    def _allow_test_file_writes(cls, fix_case: FixCase) -> bool:
        return cls._allow_test_file_writes_for_failure(fix_case.failure_class)

    @staticmethod
    def _prompt_cache_key(fix_case: FixCase) -> str:
        digest = hashlib.sha1(
            "|".join(
                [
                    fix_case.failure_class or "unknown",
                    fix_case.failure_signature or "unknown",
                    ",".join(sorted(item.file_path for item in fix_case.write_scope)),
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
