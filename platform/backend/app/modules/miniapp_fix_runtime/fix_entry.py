from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.models.common import GenerationMode
from app.models.domain import GenerateRequest, JobRecord, ValidationSnapshot
from app.modules.miniapp_agent_loop.types import WorkspaceLoopCallbacks, WorkspaceLoopTurnPlan
from app.services.check_runner import CheckRunner

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixEntryRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def generate_with_workspace_loop(
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
        del role_scope
        scope_entries = []
        scope_expansions: list[dict[str, object]] = []

        def _execute_checks(changed_files: list[str]):
            return self.service._execute_exact_checks(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
                changed_files=changed_files or ["miniapp"],
            )

        def _plan_turn(
            *,
            attempt: int,
            latest_execution,
            latest_preview_details: dict[str, object],
            validation_snapshot: ValidationSnapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            fix_turn = self.service._build_fix_case(
                workspace_id=workspace_id,
                run_id=run_id,
                attempt=attempt,
                request=request,
                check_execution=latest_execution,
                preview_details=latest_preview_details,
                prior_attempts=[],
                existing_scope=scope_entries,
                memory_context=memory_context,
            )
            next_scope = self.service._build_write_scope(
                workspace_id,
                run_id,
                fix_turn.implicated_files,
                fix_turn.failure_class or "build/runtime",
                scope_entries,
            )
            scope_entries[:] = self.service._merge_scope(scope_entries, next_scope, scope_expansions)
            job.failure_class = self.service._prefer_failure_class(job.failure_class, fix_turn.failure_class)
            job.failure_signature = fix_turn.failure_signature
            job.root_cause_summary = fix_turn.root_cause_summary
            job.fix_targets = list(fix_turn.implicated_files)
            job.validation_snapshot = self.service._validation_snapshot_from_execution(latest_execution)
            self.service._store_report(
                f"fix_case:{workspace_id}",
                {
                    "workspace_id": fix_turn.workspace_id,
                    "run_id": fix_turn.run_id,
                    "attempt": fix_turn.attempt,
                    "failure_class": fix_turn.failure_class,
                    "failure_signature": fix_turn.failure_signature,
                    "root_cause_summary": fix_turn.root_cause_summary,
                    "implicated_files": list(fix_turn.implicated_files),
                    "write_scope": [entry.model_dump(mode="json") for entry in fix_turn.write_scope],
                },
            )
            self.service._append_event(
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
            deterministic_operations = self.service._deterministic_contract_repair_operations(
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
            prompt_context = self.service._build_repair_packet(
                workspace_id=workspace_id,
                run_id=run_id,
                fix_turn=fix_turn,
                scope_entries=scope_entries,
                context_mode=context_mode,
            )
            if last_turn_summary or latest_diff_summary:
                prompt_context.previous_attempt_summary = last_turn_summary or prompt_context.previous_attempt_summary
                prompt_context.previous_diff_summary = latest_diff_summary or prompt_context.previous_diff_summary
            self.service._append_event(
                job,
                "repair_planned",
                "Prepared repair packet for the current failure bundle.",
                {"attempt": attempt, "scope": [entry.file_path for entry in scope_entries], "context_mode": prompt_context.context_mode},
            )
            llm_result = self.service._plan_patch(job=job, prompt_context=prompt_context)
            repair_outcome = self.service._repair_outcome_from_response(
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
            build_validation_snapshot=self.service._validation_snapshot_from_execution,
            completion_state=self.service._completion_state_from_results,
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=lambda operations: self.service.generation_service._run_pre_apply_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=run_id,
                page_graph=self.service._page_graph_for_deterministic_repair(workspace_id, run_id),
                role_scope=self.service._role_scope_for_fix_request(workspace_id, run_id, request),
                generation_mode=effective_mode,
                operations=list(operations),
            )
            if self.service.generation_service is not None
            else list(operations),
            append_event=self.service._append_event,
            append_trace=self.service._append_trace,
            store_report=self.service._store_report,
            stop_if_requested=should_stop,
        )
        loop_result = self.service.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=self.service._role_scope_for_fix_request(workspace_id, run_id, request),
            generation_mode=effective_mode,
            max_attempts=self.service.MAX_ATTEMPTS,
            initial_operations=[],
            initial_assistant_message="Fix workspace loop initialized.",
            initial_files_read=[],
            initial_changed_files=["miniapp"],
            callbacks=callbacks,
        )
        return self.service._finalize_loop_job(
            job=job,
            loop_result=loop_result,
            scope_expansions=scope_expansions,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
