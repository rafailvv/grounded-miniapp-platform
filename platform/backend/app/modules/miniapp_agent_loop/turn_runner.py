from __future__ import annotations

from pathlib import Path

from app.models.common import GenerationMode
from app.models.domain import (
    JobRecord,
    RepairIterationRecord,
    RunIterationOperation,
    RunIterationRecord,
    utc_now,
)
from app.modules.miniapp_agent_loop.check_feedback import WorkspaceLoopCheckFeedback
from app.modules.miniapp_agent_loop.context_builder import WorkspaceLoopContextBuilder
from app.modules.miniapp_agent_loop.edit_validator import WorkspaceLoopEditValidator
from app.modules.miniapp_agent_loop.result_classifier import WorkspaceLoopResultFactory
from app.modules.miniapp_agent_loop.types import (
    LoopContextMode,
    WorkspaceLoopCallbacks,
    WorkspaceLoopResult,
)


class WorkspaceLoopTurnRunner:
    MAX_FULL_BUNDLE_NO_PROGRESS = 2

    def __init__(self, *, context_builder: WorkspaceLoopContextBuilder) -> None:
        self.context_builder = context_builder
        self.feedback = WorkspaceLoopCheckFeedback()
        self.edit_validator = WorkspaceLoopEditValidator()
        self.results = WorkspaceLoopResultFactory()

    @staticmethod
    def compact_patch_report_envelope(envelope) -> dict[str, object]:
        payload = envelope.model_dump(mode="json")
        compact_ops: list[dict[str, object]] = []
        for raw_operation in payload.get("ops") or []:
            operation = dict(raw_operation)
            content = operation.pop("content", None)
            diff = operation.pop("diff", None)
            if content is not None:
                operation["content_chars"] = len(str(content))
                operation["content_omitted"] = True
            if diff is not None:
                operation["diff_chars"] = len(str(diff))
                operation["diff_omitted"] = True
            compact_ops.append(operation)
        payload["ops"] = compact_ops
        return payload

    @staticmethod
    def _next_fast_context_mode(
        *,
        next_attempt: int,
        made_progress: bool,
        signature_changed: bool,
    ) -> LoopContextMode:
        if next_attempt <= 2:
            return "minimal"
        if next_attempt <= 4:
            return "expanded"
        if made_progress or signature_changed:
            return "full_bundle"
        return "expanded"

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        max_attempts: int,
        initial_operations,
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: WorkspaceLoopCallbacks,
    ) -> WorkspaceLoopResult:
        latest_execution = None
        latest_preview_details: dict[str, object] = {}
        latest_apply_result = None
        latest_operations = list(initial_operations)
        all_operations = list(initial_operations)
        latest_files_read = list(initial_files_read)
        latest_assistant_message = str(initial_assistant_message or "").strip()
        changed_files = list(initial_changed_files)
        iterations: list[RunIterationRecord] = []
        repair_iterations: list[RepairIterationRecord] = []
        turn_history: list[dict[str, object]] = []
        previous_snapshot: dict[str, object] | None = None
        repeated_no_progress = 0
        context_mode: LoopContextMode = "minimal"
        last_turn_summary: str | None = None
        full_bundle_no_progress_limit = 1 if generation_mode == GenerationMode.FAST else 3 if generation_mode == GenerationMode.QUALITY else self.MAX_FULL_BUNDLE_NO_PROGRESS

        for attempt in range(max_attempts + 1):
            if callbacks.stop_if_requested and callbacks.stop_if_requested():
                return self.results.blocked(
                    summary="Run stopped before completion.",
                    failure_reason="Run stopped before completion.",
                    failure_class="stopped_by_user",
                    failure_signature="stopped_by_user",
                    root_cause_summary="Run stopped by user.",
                    current_phase="failed",
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            callbacks.append_event(job, "running_checks", "Running validation and generated app checks.", {"attempt": attempt})
            callbacks.append_event(job, "build_started", "Build validation started.", {"attempt": attempt})
            latest_execution, latest_preview_details = callbacks.execute_checks(changed_files)
            validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
            completion_state = callbacks.completion_state(
                latest_execution.results,
                latest_preview_details,
                validation_snapshot=validation_snapshot,
            )
            progress_snapshot = self.feedback.progress_snapshot(
                latest_execution.results,
                latest_preview_details,
                validation_snapshot,
            )
            previous_signature = self.feedback.progress_signature(previous_snapshot) if previous_snapshot is not None else None
            current_signature = self.feedback.progress_signature(progress_snapshot)
            signature_changed = previous_signature is not None and current_signature != previous_signature
            made_progress = (
                previous_snapshot is None
                or signature_changed
                or self.feedback.is_progress(previous_snapshot, progress_snapshot)
            )
            repeated_no_progress = 0 if made_progress else repeated_no_progress + 1

            iteration_message = latest_assistant_message or f"workspace loop attempt {attempt}"
            iterations.append(
                RunIterationRecord(
                    run_id=run_id,
                    assistant_message=iteration_message,
                    files_read=list(latest_files_read),
                    operations=[
                        RunIterationOperation(
                            file_path=operation.file_path,
                            operation=operation.operation,
                            reason=operation.reason,
                        )
                        for operation in latest_operations
                    ],
                    check_results=latest_execution.results,
                    diff_summary=self.context_builder.current_diff_summary(workspace_id, run_id),
                    role_scope=role_scope,
                    latency_breakdown={"checks_ms": latest_execution.duration_ms or 0},
                    failure_class=progress_snapshot.get("failure_class"),
                )
            )
            if attempt > 0:
                repair_iterations.append(
                    RepairIterationRecord(
                        run_id=run_id,
                        attempt=attempt,
                        files_read=list(latest_files_read),
                        files_changed=sorted({operation.file_path for operation in latest_operations}),
                        failure_class=progress_snapshot.get("failure_class"),
                        check_results=latest_execution.results,
                        latency_breakdown={"checks_ms": latest_execution.duration_ms or 0},
                        token_usage={},
                    )
                )
            self.context_builder.store_loop_reports(
                callbacks=callbacks,
                workspace_id=workspace_id,
                run_id=run_id,
                iterations=iterations,
                latest_execution=latest_execution,
            )
            callbacks.append_event(
                job,
                "checks_completed",
                "Checks completed." if not progress_snapshot["failed_checks"] else f"Checks completed with failures: {', '.join(progress_snapshot['failed_checks'])}.",
                {
                    "attempt": attempt,
                    "failed_checks": progress_snapshot["failed_checks"],
                    "remaining_issue_count": len(completion_state.get("remaining_issues") or []),
                },
            )

            if completion_state.get("strict_green") or (
                callbacks.allow_optimistic_completion and completion_state.get("optimistic_complete")
            ):
                return self.results.completed(
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            if callbacks.has_tooling_failure(latest_execution.results):
                return self.results.failed(
                    outcome_kind="blocked_preview_infra",
                    summary="Workspace loop stopped because required validation tooling is unavailable.",
                    failure_reason="Platform runtime cannot execute the required validation steps.",
                    failure_class=progress_snapshot.get("failure_class") or "tooling/runtime_misconfiguration",
                    failure_signature=self.feedback.progress_signature(progress_snapshot),
                    root_cause_summary="Platform runtime cannot execute the required validation steps.",
                    current_phase="failed",
                    remaining_issues=[],
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            if attempt >= max_attempts:
                return self.results.failed(
                    outcome_kind="blocked_generation",
                    summary="Workspace loop exhausted its retry budget without reaching a usable state.",
                    failure_reason="Workspace loop exhausted its retry budget without reaching a usable state.",
                    failure_class=progress_snapshot.get("failure_class"),
                    failure_signature=self.feedback.progress_signature(progress_snapshot),
                    root_cause_summary=progress_snapshot.get("failure_summary"),
                    current_phase="failed",
                    remaining_issues=list(completion_state.get("remaining_issues") or []),
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            next_attempt = attempt + 1
            if generation_mode == GenerationMode.FAST:
                context_mode = self._next_fast_context_mode(
                    next_attempt=next_attempt,
                    made_progress=made_progress,
                    signature_changed=signature_changed,
                )
                if (
                    next_attempt >= 5
                    and context_mode != "full_bundle"
                    and repeated_no_progress >= 2
                    and not signature_changed
                ):
                    return self.results.failed(
                        outcome_kind="blocked_generation",
                        summary="Workspace loop stopped after repeated failure signatures in FAST mode without meaningful progress.",
                        failure_reason="Workspace loop stopped after repeated failure signatures in FAST mode without meaningful progress.",
                        failure_class=job.failure_class or progress_snapshot.get("failure_class"),
                        failure_signature=job.failure_signature or current_signature,
                        root_cause_summary=job.root_cause_summary or progress_snapshot.get("failure_summary"),
                        current_phase="failed",
                        remaining_issues=list(completion_state.get("remaining_issues") or []),
                        latest_execution=latest_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        iterations=iterations,
                        repair_iterations=repair_iterations,
                        all_operations=all_operations,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )
            elif attempt > 0 and not made_progress:
                if context_mode == "minimal":
                    context_mode = "expanded"
                elif context_mode == "expanded":
                    context_mode = "full_bundle"
                elif repeated_no_progress >= full_bundle_no_progress_limit:
                    return self.results.failed(
                        outcome_kind="blocked_generation",
                        summary="Workspace loop stopped after repeated failure signatures despite expanded-context and full-bundle retries.",
                        failure_reason="Workspace loop stopped after repeated failure signatures despite expanded-context and full-bundle retries.",
                        failure_class=job.failure_class or progress_snapshot.get("failure_class"),
                        failure_signature=job.failure_signature or current_signature,
                        root_cause_summary=job.root_cause_summary or progress_snapshot.get("failure_summary"),
                        current_phase="failed",
                        remaining_issues=list(completion_state.get("remaining_issues") or []),
                        latest_execution=latest_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        iterations=iterations,
                        repair_iterations=repair_iterations,
                        all_operations=all_operations,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )

            plan = self.edit_validator.normalize_plan(
                callbacks.plan_turn(
                    attempt=attempt + 1,
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    validation_snapshot=validation_snapshot,
                    context_mode=context_mode,
                    repeated_no_progress=repeated_no_progress,
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=self.context_builder.current_diff_summary(workspace_id, run_id),
                )
            )
            if plan.failure_class:
                job.failure_class = plan.failure_class
            if plan.failure_signature:
                job.failure_signature = plan.failure_signature
            if plan.root_cause_summary:
                job.root_cause_summary = plan.root_cause_summary
            if plan.fix_targets:
                job.fix_targets = list(plan.fix_targets)

            turn_history.append(
                {
                    "attempt": attempt + 1,
                    "outcome": plan.outcome,
                    "result": "planned",
                    "diagnosis": plan.diagnosis,
                    "assistant_message": plan.assistant_message,
                    "files_read": list(plan.files_read),
                    "files_changed": [],
                    "fix_targets": list(plan.fix_targets),
                    "failure_class": plan.failure_class,
                    "failure_signature": plan.failure_signature,
                    "metadata": dict(plan.metadata),
                    "created_at": utc_now().isoformat(),
                }
            )
            last_turn_summary = plan.diagnosis or plan.assistant_message or last_turn_summary
            latest_files_read = list(plan.files_read)

            if plan.outcome == "fatal_invalid_response":
                return self.results.failed(
                    outcome_kind="blocked_generation",
                    summary="Workspace loop stopped because the edit model returned an invalid response.",
                    failure_reason=plan.diagnosis or "Edit model returned an invalid response.",
                    failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                    failure_signature=plan.failure_signature or self.feedback.progress_signature(progress_snapshot),
                    root_cause_summary=plan.root_cause_summary or progress_snapshot.get("failure_summary"),
                    current_phase="failed",
                    remaining_issues=[],
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            if plan.outcome in {"no_op", "needs_context"}:
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Loop turn produced no executable edits. Expanding context and retrying.",
                    {
                        "attempt": attempt + 1,
                        "outcome": plan.outcome,
                        "context_mode": context_mode,
                    },
                )
                callbacks.append_trace(
                    workspace_id,
                    "workspace_loop",
                    "Loop turn produced no executable edits.",
                    {
                        "attempt": attempt + 1,
                        "outcome": plan.outcome,
                        "context_mode": context_mode,
                    },
                )
                turn_history[-1]["result"] = "no_progress"
                if context_mode == "minimal":
                    context_mode = "expanded"
                elif context_mode == "expanded":
                    context_mode = "full_bundle"
                elif repeated_no_progress >= full_bundle_no_progress_limit:
                    return self.results.failed(
                        outcome_kind="blocked_generation",
                        summary="Workspace loop stopped after repeated no-progress turns with full context.",
                        failure_reason=plan.diagnosis or "Workspace loop stopped after repeated no-progress turns with full context.",
                        failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                        failure_signature=plan.failure_signature or self.feedback.progress_signature(progress_snapshot),
                        root_cause_summary=plan.root_cause_summary or progress_snapshot.get("failure_summary"),
                        current_phase="failed",
                        remaining_issues=list(completion_state.get("remaining_issues") or []),
                        latest_execution=latest_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        iterations=iterations,
                        repair_iterations=repair_iterations,
                        all_operations=all_operations,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )
                latest_assistant_message = plan.assistant_message or plan.diagnosis or latest_assistant_message
                previous_snapshot = progress_snapshot
                continue

            if bool(plan.metadata.get("skip_contract_sync")):
                synced_operations = list(plan.operations)
            else:
                synced_operations = callbacks.apply_contract_sync(list(plan.operations))
            callbacks.append_event(
                job,
                "patch_apply_started",
                "Applying workspace loop edits to the draft.",
                {"attempt": attempt + 1, "files": [operation.file_path for operation in synced_operations]},
            )
            envelope = self.context_builder.workspace_service.build_patch_envelope_for_draft(workspace_id, run_id, synced_operations)
            apply_result = self.context_builder.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
            latest_apply_result = apply_result.model_dump(mode="json")
            callbacks.store_report(
                f"patch:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "envelope": self.compact_patch_report_envelope(envelope),
                    "apply_result": latest_apply_result,
                },
            )
            if apply_result.status != "applied":
                return self.results.failed(
                    outcome_kind="blocked_generation",
                    summary="Workspace loop stopped because the draft patch could not be applied safely.",
                    failure_reason=apply_result.conflict_reason or "Draft patch could not be applied safely.",
                    failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                    failure_signature=plan.failure_signature or self.feedback.progress_signature(progress_snapshot),
                    root_cause_summary=plan.root_cause_summary or progress_snapshot.get("failure_summary"),
                    current_phase="failed",
                    remaining_issues=[],
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_operations=all_operations,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            latest_operations = list(synced_operations)
            all_operations.extend(latest_operations)
            changed_files = list(apply_result.changed_files or [operation.file_path for operation in latest_operations])
            if callbacks.post_apply_stabilize is not None:
                stabilized_files = callbacks.post_apply_stabilize(
                    workspace_id,
                    run_id,
                    draft_source,
                    list(changed_files),
                )
                if stabilized_files:
                    changed_files = sorted(set(changed_files) | {str(path) for path in stabilized_files if str(path).strip()})
            latest_assistant_message = plan.assistant_message or plan.diagnosis or latest_assistant_message
            turn_history[-1]["result"] = "patched"
            turn_history[-1]["files_changed"] = list(changed_files)
            callbacks.append_event(
                job,
                "patch_apply_completed",
                "Workspace loop edits were applied to the draft.",
                {"attempt": attempt + 1, "changed_files": list(changed_files)},
            )
            callbacks.append_trace(
                workspace_id,
                "workspace_loop",
                "Workspace loop edits were applied to the draft.",
                {"attempt": attempt + 1, "changed_files": list(changed_files)},
            )
            previous_snapshot = progress_snapshot

        return self.results.failed(
            outcome_kind="blocked_generation",
            summary="Workspace loop exited unexpectedly.",
            failure_reason="Workspace loop exited unexpectedly.",
            failure_class=None,
            failure_signature=None,
            root_cause_summary=None,
            current_phase="failed",
            remaining_issues=[],
            latest_execution=latest_execution,
            latest_preview_details=latest_preview_details,
            latest_apply_result=latest_apply_result,
            iterations=iterations,
            repair_iterations=repair_iterations,
            all_operations=all_operations,
            last_assistant_message=latest_assistant_message,
            turn_history=turn_history,
        )
