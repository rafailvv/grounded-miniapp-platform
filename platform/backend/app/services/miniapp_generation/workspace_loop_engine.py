from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    JobRecord,
    RepairIterationRecord,
    RunIterationOperation,
    RunIterationRecord,
    ValidationSnapshot,
    utc_now,
)
from app.repositories.state_store import StateStore
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService


LoopOutcome = Literal["patch_ready", "no_op", "needs_context", "fatal_invalid_response"]
LoopContextMode = Literal["minimal", "expanded", "full_bundle"]


@dataclass
class WorkspaceLoopTurnPlan:
    outcome: LoopOutcome
    assistant_message: str = ""
    diagnosis: str | None = None
    operations: list[DraftFileOperation] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    fix_targets: list[str] = field(default_factory=list)
    expected_verification: str | None = None
    rationale_by_file: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceLoopResult:
    status: Literal["completed", "failed", "blocked"]
    outcome_kind: str | None
    summary: str
    failure_reason: str | None
    failure_class: str | None
    failure_signature: str | None
    root_cause_summary: str | None
    current_phase: str
    remaining_issues: list[dict[str, Any]]
    latest_execution: CheckExecutionRecord | None
    latest_preview_details: dict[str, Any]
    latest_apply_result: dict[str, Any] | None
    iterations: list[RunIterationRecord]
    repair_iterations: list[RepairIterationRecord]
    all_operations: list[DraftFileOperation]
    last_assistant_message: str
    turn_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WorkspaceLoopCallbacks:
    execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]]
    build_validation_snapshot: Callable[[CheckExecutionRecord], ValidationSnapshot]
    completion_state: Callable[[list[Any], dict[str, Any], ValidationSnapshot | None], dict[str, Any]]
    has_tooling_failure: Callable[[list[Any]], bool]
    plan_turn: Callable[..., WorkspaceLoopTurnPlan]
    apply_contract_sync: Callable[[list[DraftFileOperation]], list[DraftFileOperation]]
    append_event: Callable[[JobRecord, str, str, dict[str, Any] | None], None]
    append_trace: Callable[[str, str, str, dict[str, Any] | None], None]
    store_report: Callable[[str, dict[str, Any]], None]
    stop_if_requested: Callable[[], bool] | None = None


class WorkspaceLoopEngine:
    MAX_FULL_BUNDLE_NO_PROGRESS = 2

    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.workspace_log_service = workspace_log_service

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
        initial_operations: list[DraftFileOperation],
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: WorkspaceLoopCallbacks,
    ) -> WorkspaceLoopResult:
        del draft_source
        del generation_mode
        latest_execution: CheckExecutionRecord | None = None
        latest_preview_details: dict[str, Any] = {}
        latest_apply_result: dict[str, Any] | None = None
        latest_operations = list(initial_operations)
        all_operations = list(initial_operations)
        latest_files_read = list(initial_files_read)
        latest_assistant_message = str(initial_assistant_message or "").strip()
        changed_files = list(initial_changed_files)
        iterations: list[RunIterationRecord] = []
        repair_iterations: list[RepairIterationRecord] = []
        turn_history: list[dict[str, Any]] = []
        previous_snapshot: dict[str, Any] | None = None
        repeated_no_progress = 0
        context_mode: LoopContextMode = "minimal"
        last_turn_summary: str | None = None

        for attempt in range(max_attempts + 1):
            if callbacks.stop_if_requested and callbacks.stop_if_requested():
                return WorkspaceLoopResult(
                    status="blocked",
                    outcome_kind="blocked_generation",
                    summary="Run stopped before completion.",
                    failure_reason="Run stopped before completion.",
                    failure_class="stopped_by_user",
                    failure_signature="stopped_by_user",
                    root_cause_summary="Run stopped by user.",
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

            callbacks.append_event(job, "running_checks", "Running validation and generated app checks.", {"attempt": attempt})
            callbacks.append_event(job, "build_started", "Build validation started.", {"attempt": attempt})
            latest_execution, latest_preview_details = callbacks.execute_checks(changed_files)
            validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
            completion_state = callbacks.completion_state(
                latest_execution.results,
                latest_preview_details,
                validation_snapshot=validation_snapshot,
            )
            progress_snapshot = self._progress_snapshot(
                latest_execution.results,
                latest_preview_details,
                validation_snapshot,
            )
            made_progress = previous_snapshot is None or self._is_progress(previous_snapshot, progress_snapshot)
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
                    diff_summary=self._current_diff_summary(workspace_id, run_id),
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
            self._store_loop_reports(
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

            if completion_state.get("strict_green"):
                return WorkspaceLoopResult(
                    status="completed",
                    outcome_kind="applied",
                    summary="Workspace loop completed successfully.",
                    failure_reason=None,
                    failure_class=None,
                    failure_signature=None,
                    root_cause_summary=None,
                    current_phase="completed",
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

            if completion_state.get("optimistic_complete"):
                remaining_issues = list(completion_state.get("remaining_issues") or [])
                return WorkspaceLoopResult(
                    status="completed",
                    outcome_kind="warnings" if remaining_issues else "applied",
                    summary=(
                        "Workspace loop completed with a usable draft. Remaining non-blocking issues were recorded."
                        if remaining_issues
                        else "Workspace loop completed successfully."
                    ),
                    failure_reason=None,
                    failure_class=None,
                    failure_signature=None,
                    root_cause_summary=None,
                    current_phase="completed",
                    remaining_issues=remaining_issues,
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
                return WorkspaceLoopResult(
                    status="failed",
                    outcome_kind="blocked_preview_infra",
                    summary="Workspace loop stopped because required validation tooling is unavailable.",
                    failure_reason="Platform runtime cannot execute the required validation steps.",
                    failure_class=progress_snapshot.get("failure_class") or "tooling/runtime_misconfiguration",
                    failure_signature=self._progress_signature(progress_snapshot),
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

            if attempt > 0 and not made_progress:
                if context_mode == "minimal":
                    context_mode = "expanded"
                elif context_mode == "expanded":
                    context_mode = "full_bundle"
                elif repeated_no_progress >= self.MAX_FULL_BUNDLE_NO_PROGRESS:
                    return WorkspaceLoopResult(
                        status="failed",
                        outcome_kind="blocked_generation",
                        summary="Workspace loop stopped after repeated failure signatures despite expanded-context and full-bundle retries.",
                        failure_reason="Workspace loop stopped after repeated failure signatures despite expanded-context and full-bundle retries.",
                        failure_class=job.failure_class or progress_snapshot.get("failure_class"),
                        failure_signature=job.failure_signature or self._progress_signature(progress_snapshot),
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

            if attempt >= max_attempts:
                return WorkspaceLoopResult(
                    status="failed",
                    outcome_kind="blocked_generation",
                    summary="Workspace loop exhausted its retry budget without reaching a usable state.",
                    failure_reason="Workspace loop exhausted its retry budget without reaching a usable state.",
                    failure_class=progress_snapshot.get("failure_class"),
                    failure_signature=self._progress_signature(progress_snapshot),
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

            plan = callbacks.plan_turn(
                attempt=attempt + 1,
                latest_execution=latest_execution,
                latest_preview_details=latest_preview_details,
                validation_snapshot=validation_snapshot,
                context_mode=context_mode,
                repeated_no_progress=repeated_no_progress,
                last_turn_summary=last_turn_summary,
                latest_diff_summary=self._current_diff_summary(workspace_id, run_id),
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
                return WorkspaceLoopResult(
                    status="failed",
                    outcome_kind="blocked_generation",
                    summary="Workspace loop stopped because the edit model returned an invalid response.",
                    failure_reason=plan.diagnosis or "Edit model returned an invalid response.",
                    failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                    failure_signature=plan.failure_signature or self._progress_signature(progress_snapshot),
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
                elif repeated_no_progress >= self.MAX_FULL_BUNDLE_NO_PROGRESS:
                    return WorkspaceLoopResult(
                        status="failed",
                        outcome_kind="blocked_generation",
                        summary="Workspace loop stopped after repeated no-progress turns with full context.",
                        failure_reason=plan.diagnosis or "Workspace loop stopped after repeated no-progress turns with full context.",
                        failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                        failure_signature=plan.failure_signature or self._progress_signature(progress_snapshot),
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

            synced_operations = callbacks.apply_contract_sync(list(plan.operations))
            callbacks.append_event(
                job,
                "patch_apply_started",
                "Applying workspace loop edits to the draft.",
                {"attempt": attempt + 1, "files": [operation.file_path for operation in synced_operations]},
            )
            envelope = self.workspace_service.build_patch_envelope_for_draft(workspace_id, run_id, synced_operations)
            apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
            latest_apply_result = apply_result.model_dump(mode="json")
            callbacks.store_report(
                f"patch:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "envelope": envelope.model_dump(mode="json"),
                    "apply_result": latest_apply_result,
                },
            )
            if apply_result.status != "applied":
                return WorkspaceLoopResult(
                    status="failed",
                    outcome_kind="blocked_generation",
                    summary="Workspace loop stopped because the draft patch could not be applied safely.",
                    failure_reason=apply_result.conflict_reason or "Draft patch could not be applied safely.",
                    failure_class=plan.failure_class or progress_snapshot.get("failure_class"),
                    failure_signature=plan.failure_signature or self._progress_signature(progress_snapshot),
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

        return WorkspaceLoopResult(
            status="failed",
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

    def _store_loop_reports(
        self,
        *,
        callbacks: WorkspaceLoopCallbacks,
        workspace_id: str,
        run_id: str,
        iterations: list[RunIterationRecord],
        latest_execution: CheckExecutionRecord,
    ) -> None:
        callbacks.store_report(
            f"iterations:{workspace_id}",
            {"run_id": run_id, "items": [item.model_dump(mode="json") for item in iterations]},
        )
        callbacks.store_report(
            f"check_results:{workspace_id}",
            {
                "run_id": run_id,
                "items": [item.model_dump(mode="json") for item in latest_execution.results],
                "execution": latest_execution.model_dump(mode="json"),
            },
        )
        callbacks.store_report(
            f"candidate_diff:{workspace_id}",
            {
                "run_id": run_id,
                "diff": self.workspace_service.diff(workspace_id, run_id=run_id),
            },
        )

    def _current_diff_summary(self, workspace_id: str, run_id: str) -> str | None:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        if not diff_text.strip():
            return None
        paths: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            if " b/" not in line:
                continue
            paths.append(line.split(" b/", 1)[1].strip())
        if not paths:
            return "Draft diff exists."
        unique_paths = list(dict.fromkeys(paths))
        return f"Changed files: {', '.join(unique_paths[:6])}"

    @staticmethod
    def _progress_snapshot(
        results: list[Any],
        preview_details: dict[str, Any],
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, Any]:
        failed_results = [result for result in results if getattr(result, "status", None) == "failed"]
        failed_checks = sorted(str(getattr(result, "name", "")) for result in failed_results)
        details_markers = sorted(
            {
                f"{getattr(result, 'name', '')}:{str(getattr(result, 'details', '') or '').strip()[:180]}"
                for result in failed_results
            }
        )
        preview_status = str(preview_details.get("status") or "")
        blocking_validation = bool(validation_snapshot.blocking) if validation_snapshot is not None else False
        failure_summary = " | ".join(marker for marker in details_markers[:4] if marker)
        failure_class = failed_checks[0] if failed_checks else None
        return {
            "failed_checks": failed_checks,
            "details_markers": details_markers,
            "preview_status": preview_status,
            "blocking_validation": blocking_validation,
            "failed_count": len(failed_checks),
            "failure_summary": failure_summary or None,
            "failure_class": failure_class,
        }

    @staticmethod
    def _is_progress(previous: dict[str, Any], current: dict[str, Any]) -> bool:
        if current["failed_count"] < previous["failed_count"]:
            return True
        if len(current["details_markers"]) < len(previous["details_markers"]):
            return True
        if previous["preview_status"] != "running" and current["preview_status"] == "running":
            return True
        if previous["blocking_validation"] and not current["blocking_validation"]:
            return True
        return False

    @staticmethod
    def _progress_signature(snapshot: dict[str, Any]) -> str:
        signature = "|".join(snapshot.get("details_markers") or snapshot.get("failed_checks") or [])
        return signature or "workspace_loop_failure"
