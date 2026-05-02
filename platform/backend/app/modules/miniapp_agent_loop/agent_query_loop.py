from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.artifacts import ApplyPatchResult
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    RunIterationAction,
    RunIterationRecord,
    utc_now,
)
from app.modules.miniapp_agent_loop.agent_kernel import compact_agent_memory
from app.modules.miniapp_agent_loop.check_feedback import AgentCheckFeedback
from app.modules.miniapp_agent_loop.context_builder import AgentContextBuilder
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.result_classifier import AgentLoopResultFactory
from app.modules.miniapp_agent_loop.types import AgentLoopCallbacks, AgentLoopResult


class AgentQueryLoop:
    """Claude/Codex-style agent loop for draft miniapp edits.

    The loop does not generate a full application as one deterministic platform
    patch. It repeatedly asks the model for the next tool/patch action, applies
    valid mutations to the draft, runs validation, and feeds compact failure
    packets back until strict green completion or an external budget/provider
    blocker.
    """

    def __init__(self, *, context_builder: AgentContextBuilder) -> None:
        self.context_builder = context_builder
        self.feedback = AgentCheckFeedback()
        self.edit_validator = AgentEditValidator()
        self.results = AgentLoopResultFactory()

    @staticmethod
    def _budget_state(callbacks: AgentLoopCallbacks, turn: int) -> dict[str, object]:
        if callbacks.budget_status is None:
            return {}
        try:
            state = callbacks.budget_status(turn)
        except Exception as exc:
            return {"exhausted": False, "budget_status_error": str(exc)}
        return dict(state or {}) if isinstance(state, dict) else {}

    @staticmethod
    def _budget_failure_reason(state: dict[str, object]) -> str:
        reason = str(state.get("reason") or "budget_exhausted").strip()
        elapsed_ms = int(state.get("elapsed_ms") or 0)
        token_total = int(state.get("total_tokens") or 0)
        token_limit = int(state.get("token_limit") or 0)
        time_limit_ms = int(state.get("time_limit_ms") or 0)
        if reason == "token_budget_exhausted":
            return f"Generation token budget exhausted: {token_total}/{token_limit} tokens."
        if reason == "time_budget_exhausted":
            return f"Generation time budget exhausted: {elapsed_ms}/{time_limit_ms} ms."
        return "Generation budget exhausted before strict-green completion."

    @staticmethod
    def _initial_pending_execution(workspace_id: str, run_id: str) -> CheckExecutionRecord:
        return CheckExecutionRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            changed_files=[],
            results=[
                RunCheckResult(
                    name="initial_patch_pending",
                    status="failed",
                    details="Initial checks are waiting for the first agent patch.",
                    command="agent query loop",
                    logs=[],
                )
            ],
            started_at=utc_now(),
            completed_at=utc_now(),
            duration_ms=0,
        )

    @staticmethod
    def _phase_for_budget_state(state: dict[str, object]) -> str:
        return str(state.get("current_phase") or "blocked_budget_exhausted")

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        initial_draft_actions,
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: AgentLoopCallbacks,
    ) -> AgentLoopResult:
        del draft_source, role_scope, generation_mode

        latest_execution: CheckExecutionRecord | None = None
        latest_preview_details: dict[str, Any] = {}
        latest_apply_result: dict[str, Any] | None = None
        latest_draft_actions = list(initial_draft_actions or [])
        all_draft_actions = list(initial_draft_actions or [])
        latest_files_read = list(initial_files_read or [])
        latest_assistant_message = str(initial_assistant_message or "").strip()
        changed_files = list(initial_changed_files or [])
        iterations: list[RunIterationRecord] = []
        repair_iterations: list[RepairIterationRecord] = []
        turn_history: list[dict[str, Any]] = []
        last_turn_summary: str | None = None
        context_mode = "minimal"
        previous_snapshot: dict[str, Any] | None = None
        repeated_failure_signatures: dict[str, int] = {}

        turn = 0
        while True:
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
                    all_draft_actions=all_draft_actions,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            budget_state = self._budget_state(callbacks, turn)
            if budget_state.get("exhausted"):
                failure_reason = self._budget_failure_reason(budget_state)
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "completed",
                        "Agent stopped at generation budget.",
                        {"turn": turn, "status": "blocked", "budget_status": budget_state},
                    )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    failure_reason,
                    {"turn": turn, "budget_status": budget_state},
                )
                return self.results.blocked(
                    summary=failure_reason,
                    failure_reason=failure_reason,
                    failure_class=str(budget_state.get("failure_class") or "generation.budget_exhausted"),
                    failure_signature=str(budget_state.get("failure_signature") or budget_state.get("reason") or "generation.budget_exhausted"),
                    root_cause_summary=failure_reason,
                    current_phase=self._phase_for_budget_state(budget_state),
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_draft_actions=all_draft_actions,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            has_draft_diff = bool(
                all_draft_actions
                or self.context_builder.workspace_service.diff(workspace_id, run_id=run_id).strip()
            )
            skip_checks = bool(callbacks.skip_initial_checks and turn == 0 and not has_draft_diff)
            if skip_checks:
                latest_execution = self._initial_pending_execution(workspace_id, run_id)
                latest_preview_details = {}
                validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
                progress_snapshot = self.feedback.progress_snapshot(latest_execution.results, latest_preview_details, validation_snapshot)
                completion_state = {
                    "strict_green": False,
                    "optimistic_complete": False,
                    "remaining_issues": [
                        {
                            "kind": "initial_patch_pending",
                            "check": "initial_patch_pending",
                            "details": "The agent must create the first patch before validation can pass.",
                            "blocking": True,
                        }
                    ],
                }
                callbacks.append_event(
                    job,
                    "spec_extract_started",
                    "Agent query loop planned the first patch before baseline validation.",
                    {"turn": turn, "has_draft_diff": False},
                )
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "planning",
                        "Planning first draft patch",
                        {"turn": turn, "has_draft_diff": False},
                    )
            else:
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "checking",
                        "Running validation checks",
                        {"turn": turn, "has_draft_diff": has_draft_diff},
                    )
                callbacks.append_event(
                    job,
                    "running_checks",
                    "Running validation and generated app checks.",
                    {"turn": turn, "has_file_edits": has_draft_diff},
                )
                latest_execution, latest_preview_details = callbacks.execute_checks(changed_files)
                validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
                completion_state = callbacks.completion_state(
                    latest_execution.results,
                    latest_preview_details,
                    validation_snapshot=validation_snapshot,
                )
                progress_snapshot = self.feedback.progress_snapshot(latest_execution.results, latest_preview_details, validation_snapshot)
                iterations.append(
                    RunIterationRecord(
                        run_id=run_id,
                        assistant_message=latest_assistant_message or f"agent query turn {turn}",
                        files_read=list(latest_files_read),
                        draft_actions=[
                            RunIterationAction(
                                file_path=operation.file_path,
                                operation=operation.operation,
                                reason=operation.reason,
                            )
                            for operation in latest_draft_actions
                        ],
                        check_results=latest_execution.results,
                        diff_summary=self.context_builder.current_diff_summary(workspace_id, run_id),
                        role_scope=[],
                        latency_breakdown={"checks_ms": latest_execution.duration_ms or 0},
                        failure_class=progress_snapshot.get("failure_class"),
                    )
                )
                self.context_builder.store_agent_reports(
                    callbacks=callbacks,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    iterations=iterations,
                    latest_execution=latest_execution,
                )

            if completion_state.get("strict_green") or (
                callbacks.allow_optimistic_completion and completion_state.get("optimistic_complete")
            ):
                verification_report = (
                    callbacks.verify_completion(latest_execution, latest_preview_details)
                    if callbacks.verify_completion is not None
                    else {"status": "passed", "issues": []}
                )
                if str(verification_report.get("status") or "").lower() != "passed":
                    issues = list(verification_report.get("issues") or [])
                    if latest_execution is not None:
                        latest_execution.results.append(
                            RunCheckResult(
                                name="verification_worker",
                                status="failed",
                                details=str(verification_report.get("summary") or "Verification worker found unresolved issues."),
                                command="agent verification worker",
                                logs=[str(issue)[:500] for issue in issues[:8]],
                                diagnostics={"issues": issues},
                            )
                        )
                    if callbacks.append_activity:
                        callbacks.append_activity(
                            job,
                            "repairing",
                            "Verification worker requested targeted repair",
                            {"turn": turn, "status": "failed", "issue_count": len(issues)},
                        )
                    callbacks.append_event(
                        job,
                        "repair_iteration",
                        "Verification worker found unresolved workflow proof issues.",
                        {"turn": turn, "verification_report": verification_report},
                    )
                    completion_state = {
                        "strict_green": False,
                        "optimistic_complete": False,
                        "remaining_issues": issues,
                    }
                    if latest_execution is not None:
                        validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
                        progress_snapshot = self.feedback.progress_snapshot(
                            latest_execution.results,
                            latest_preview_details,
                            validation_snapshot,
                        )
                    else:
                        progress_snapshot = {
                            **progress_snapshot,
                            "failure_class": "verification_worker",
                            "signature": "verification_worker_failed",
                        }
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "completed",
                        "Strict green completion reached",
                        {"turn": turn, "status": "completed"},
                    )
                return self.results.completed(
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_draft_actions=all_draft_actions,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            if callbacks.has_tooling_failure(latest_execution.results if latest_execution else []):
                return self.results.failed(
                    outcome_kind="blocked_preview_infra",
                    summary="Required validation tooling is unavailable.",
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
                    all_draft_actions=all_draft_actions,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            current_signature = self.feedback.progress_signature(progress_snapshot)
            repeated_failure_signatures[current_signature] = repeated_failure_signatures.get(current_signature, 0) + 1
            made_progress = previous_snapshot is None or self.feedback.is_progress(previous_snapshot, progress_snapshot)
            if made_progress:
                context_mode = "minimal"
            elif repeated_failure_signatures[current_signature] >= 2:
                context_mode = "expanded"

            memory = compact_agent_memory(
                turn_history=turn_history,
                draft_action_count=len(all_draft_actions),
                last_assistant_message=latest_assistant_message,
            )
            job.agent_memory = dict(memory)
            compact_payload = {
                "turn": turn,
                "memory": memory,
                "latest_failure_signature": current_signature,
                "latest_diff_summary": self.context_builder.current_diff_summary(workspace_id, run_id),
            }
            if callbacks.record_compact_boundary is not None:
                callbacks.record_compact_boundary(compact_payload)
            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "compacting",
                    "Compacted agent repair memory",
                    {
                        "turn": turn,
                        "failed_signature_count": len(memory.get("failed_signatures") or []),
                        "draft_action_count": len(all_draft_actions),
                    },
                )
            callbacks.store_report(
                f"agent_memory:{workspace_id}:{run_id}",
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "memory": memory,
                    "latest_failure_signature": current_signature,
                },
            )
            callbacks.append_event(
                job,
                "compact_boundary",
                "Agent repair context was compacted for the next turn.",
                {
                    "turn": turn,
                    "failure_signature": current_signature,
                    "failed_signature_count": len(memory.get("failed_signatures") or []),
                },
            )

            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "planning",
                    "Planning next agent step",
                    {
                        "turn": turn + 1,
                        "context_mode": context_mode,
                        "failure_signature": current_signature,
                    },
                )
            plan = self.edit_validator.normalize_plan(
                callbacks.plan_turn(
                    attempt=turn + 1,
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    validation_snapshot=validation_snapshot,
                    context_mode=context_mode,
                    repeated_no_progress=max(0, repeated_failure_signatures[current_signature] - 1),
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=self.context_builder.current_diff_summary(workspace_id, run_id),
                    agent_memory=memory,
                )
            )
            latest_files_read = list(plan.files_read)
            latest_assistant_message = plan.assistant_message or plan.diagnosis or latest_assistant_message
            last_turn_summary = plan.diagnosis or latest_assistant_message or last_turn_summary
            turn_history.append(
                {
                    "turn": turn + 1,
                    "outcome": plan.outcome,
                    "diagnosis": plan.diagnosis,
                    "assistant_message": plan.assistant_message,
                    "files_read": list(plan.files_read),
                    "failure_class": plan.failure_class,
                    "failure_signature": plan.failure_signature,
                    "metadata": dict(plan.metadata),
                    "created_at": utc_now().isoformat(),
                }
            )
            job.agent_turns = list(turn_history)
            if plan.failure_class:
                job.failure_class = plan.failure_class
            if plan.failure_signature:
                job.failure_signature = plan.failure_signature
            if plan.root_cause_summary:
                job.root_cause_summary = plan.root_cause_summary
            if plan.fix_targets:
                job.fix_targets = list(plan.fix_targets)

            if plan.outcome == "fatal_invalid_response":
                if plan.failure_class in {"provider.insufficient_quota", "generation.budget_exhausted"}:
                    is_quota = plan.failure_class == "provider.insufficient_quota"
                    summary = (
                        "OpenAI provider quota is exhausted for the selected code generation model."
                        if is_quota
                        else "Generation budget exhausted before strict-green completion."
                    )
                    return self.results.blocked(
                        summary=summary,
                        failure_reason=plan.diagnosis or summary,
                        failure_class=plan.failure_class,
                        failure_signature=plan.failure_signature or plan.failure_class,
                        root_cause_summary=plan.root_cause_summary or summary,
                        current_phase="blocked_provider_quota" if is_quota else "blocked_budget_exhausted",
                        latest_execution=latest_execution,
                        latest_preview_details=latest_preview_details,
                        latest_apply_result=latest_apply_result,
                        iterations=iterations,
                        repair_iterations=repair_iterations,
                        all_draft_actions=all_draft_actions,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Agent returned an invalid response; continuing with compact failure memory.",
                    {"turn": turn + 1, "diagnosis": plan.diagnosis, "failure_signature": plan.failure_signature},
                )
                previous_snapshot = progress_snapshot
                turn += 1
                continue

            if plan.outcome in {"no_op", "needs_context"}:
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "repairing",
                        "Continuing after no-op/context step",
                        {"turn": turn + 1, "outcome": plan.outcome, "context_mode": context_mode},
                    )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Agent requested more context or produced no edit; continuing the tool loop.",
                    {"turn": turn + 1, "outcome": plan.outcome, "context_mode": context_mode},
                )
                previous_snapshot = progress_snapshot
                turn += 1
                continue

            synced_draft_actions = (
                list(plan.draft_actions)
                if bool(plan.metadata.get("skip_contract_sync"))
                else callbacks.apply_contract_sync(list(plan.draft_actions))
            )
            callbacks.append_event(
                job,
                "patch_apply_started",
                "Applying agent patch to draft.",
                {"turn": turn + 1, "files": [operation.file_path for operation in synced_draft_actions]},
            )
            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "applying_patch",
                    "Applying agent draft actions",
                    {
                        "turn": turn + 1,
                        "draft_action_count": len(synced_draft_actions),
                        "file_count": len({operation.file_path for operation in synced_draft_actions}),
                    },
                )
            if callbacks.before_apply is not None:
                callbacks.before_apply(turn + 1, synced_draft_actions)
            try:
                envelope = self.context_builder.workspace_service.build_patch_envelope_for_draft_actions(workspace_id, run_id, synced_draft_actions)
                apply_result = self.context_builder.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
                latest_apply_result = apply_result.model_dump(mode="json")
            except ValueError as exc:
                apply_result = ApplyPatchResult(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status="conflict",
                    conflict_reason=str(exc),
                )
                latest_apply_result = apply_result.model_dump(mode="json")
            if callbacks.after_apply is not None:
                callbacks.after_apply(
                    turn + 1,
                    synced_draft_actions,
                    apply_result,
                    [operation.file_path for operation in synced_draft_actions],
                )

            callbacks.store_report(
                f"patch:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "turn": turn + 1,
                    "draft_actions": [
                        {
                            "file_path": operation.file_path,
                            "operation": operation.operation,
                            "reason": operation.reason,
                        }
                        for operation in synced_draft_actions
                    ],
                    "apply_result": latest_apply_result,
                },
            )
            latest_draft_actions = list(synced_draft_actions)
            if apply_result.status != "applied":
                turn_history[-1]["result"] = "apply_conflict"
                turn_history[-1]["apply_error"] = apply_result.conflict_reason or apply_result.status
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Agent patch did not apply; continuing with conflict packet.",
                    {"turn": turn + 1, "conflict_reason": apply_result.conflict_reason or apply_result.status},
                )
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "repairing",
                        "Patch did not apply; preparing conflict repair",
                        {"turn": turn + 1, "status": apply_result.status, "conflict_reason": apply_result.conflict_reason},
                    )
                previous_snapshot = progress_snapshot
                turn += 1
                continue

            changed_files = callbacks.post_apply_stabilize(workspace_id, run_id, apply_result, [operation.file_path for operation in synced_draft_actions]) if callbacks.post_apply_stabilize else [operation.file_path for operation in synced_draft_actions]
            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "editing",
                    "Draft patch applied",
                    {"turn": turn + 1, "changed_file_count": len(set(changed_files)), "status": "applied"},
                )
            all_draft_actions.extend(synced_draft_actions)
            turn_history[-1]["result"] = "applied"
            turn_history[-1]["files_changed"] = list(changed_files)
            if turn > 0:
                repair_iterations.append(
                    RepairIterationRecord(
                        run_id=run_id,
                        attempt=turn,
                        files_read=list(latest_files_read),
                        files_changed=list(changed_files),
                        failure_class=progress_snapshot.get("failure_class"),
                        check_results=latest_execution.results if latest_execution else [],
                        latency_breakdown={},
                        token_usage={},
                    )
                )
            previous_snapshot = progress_snapshot
            turn += 1
