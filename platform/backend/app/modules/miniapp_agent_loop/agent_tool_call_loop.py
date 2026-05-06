from __future__ import annotations

from pathlib import Path
import re
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
from app.modules.miniapp_agent_loop.diagnostics_delta import AgentDiagnosticsDelta
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.agent_mutation_guard import AgentMutationGuard
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.modules.miniapp_agent_loop.repair_packets import RepairTransitionPolicy
from app.modules.miniapp_agent_loop.result_classifier import AgentLoopResultFactory
from app.modules.miniapp_agent_loop.types import AgentLoopCallbacks, AgentLoopResult
from app.services.repair_cases import RepairCaseService, patch_sha256
from app.services.repair_catalog import RepairCatalog


class AgentToolCallLoop:
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
        self.mutation_guard = AgentMutationGuard()
        self.repair_cases = RepairCaseService(context_builder.store)

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
                    command="agent tool-call loop",
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

    @staticmethod
    def _repair_packets_from_plan(plan: Any) -> list[dict[str, Any]]:
        metadata = getattr(plan, "metadata", {}) if plan is not None else {}
        packets = metadata.get("repair_packets") if isinstance(metadata, dict) else None
        return [dict(item) for item in packets if isinstance(item, dict)] if isinstance(packets, list) else []

    @staticmethod
    def _repair_packets_from_execution(execution: CheckExecutionRecord | None) -> list[dict[str, Any]]:
        if execution is None:
            return []
        issues: list[dict[str, Any]] = []
        for result in execution.results:
            if result.status not in {"failed", "blocked"}:
                continue
            issues.append(
                {
                    "check": result.name,
                    "details": result.details or "",
                    "logs": list(result.logs or [])[-8:],
                    "diagnostics": dict(result.diagnostics or {}),
                    "failure_class": result.name,
                    "failure_signature": f"{result.name}:{str(result.details or '')[:160]}",
                    "paths": AgentToolCallLoop._paths_from_check_result(result),
                }
            )
        return RepairCatalog.classify_many(issues)

    @staticmethod
    def _paths_from_check_result(result: RunCheckResult) -> list[str]:
        paths: list[str] = []
        diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
        for key in ("paths", "target_files", "changed_files", "files"):
            value = diagnostics.get(key)
            if isinstance(value, list):
                for item in value:
                    path = str(item or "").strip().replace("\\", "/")
                    if path.startswith("./"):
                        path = path[2:]
                    if path.startswith("miniapp/") and path not in paths:
                        paths.append(path)
        for text in [result.details or "", *list(result.logs or [])[-8:]]:
            for match in re.finditer(r"(?:^|[/\\])(?P<path>miniapp[/\\][A-Za-z0-9_./\\-]+\.(?:py|js|mjs|html|css|json))", str(text or "")):
                path = match.group("path").replace("\\", "/")
                if path not in paths:
                    paths.append(path)
            for match in re.finditer(r"(?P<path>miniapp/app/static/(?P<role>client|specialist|manager))(?![A-Za-z0-9_./-])", str(text or "")):
                role = match.group("role")
                for path in (
                    f"miniapp/app/static/{role}/index.html",
                    f"miniapp/app/static/{role}/app.js",
                    f"miniapp/app/static/{role}/styles.css",
                ):
                    if path not in paths:
                        paths.append(path)
        return paths[:8]

    @staticmethod
    def _with_repeated_counts(
        packets: list[dict[str, Any]],
        counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        for packet in packets:
            signature = str(packet.get("failure_signature") or packet.get("signature") or packet.get("code") or "repair_packet")
            counts[signature] = counts.get(signature, 0) + 1
            updated.append({**packet, "repeated_count": counts[signature]})
        return updated

    @staticmethod
    def _should_escalate_repeated_no_progress(
        *,
        repeated_count: int,
        has_draft_diff: bool,
        active_case: dict[str, Any] | None,
        transition: dict[str, Any],
    ) -> bool:
        if not has_draft_diff or repeated_count < 2 or not isinstance(active_case, dict):
            return False
        attempts = active_case.get("attempts") if isinstance(active_case.get("attempts"), list) else []
        next_action = active_case.get("next_action") if isinstance(active_case.get("next_action"), dict) else {}
        attempt_count = max(len(attempts), int(next_action.get("attempt_count") or 0))
        forced = transition.get("next_forced_action") if isinstance(transition.get("next_forced_action"), dict) else {}
        action = str(next_action.get("action") or forced.get("action") or "").strip()
        if action == "fresh_context_repair" and repeated_count >= 2:
            return True
        if attempt_count >= 3 and repeated_count >= 2:
            return True
        return repeated_count >= 5

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        initial_file_changes,
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: AgentLoopCallbacks,
    ) -> AgentLoopResult:
        del role_scope, generation_mode

        latest_execution: CheckExecutionRecord | None = None
        latest_preview_details: dict[str, Any] = {}
        latest_apply_result: dict[str, Any] | None = None
        latest_file_changes = list(initial_file_changes or [])
        all_file_changes = list(initial_file_changes or [])
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
        repair_packet_counts: dict[str, int] = {}
        latest_repair_packets: list[dict[str, Any]] = []
        latest_repair_transition: dict[str, Any] = {}
        latest_repair_cases: dict[str, Any] = {"schema": "grounded.repair_cases.v1", "run_id": run_id, "status": "empty", "items": []}
        active_repair_case_id = ""
        previous_diagnostics_snapshot: dict[str, dict[str, Any]] | None = None
        latest_diagnostics_delta: dict[str, Any] = {"status": "unchanged", "added": [], "changed": [], "resolved": []}
        pending_validation_after_patch = bool(initial_file_changes or initial_changed_files)

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
                    all_file_changes=all_file_changes,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            budget_state = self._budget_state(callbacks, turn)
            if budget_state.get("exhausted") and not pending_validation_after_patch:
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
                    all_file_changes=all_file_changes,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
                )

            has_draft_diff = bool(
                all_file_changes
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
                    "Agent tool-call loop planned the first patch before baseline validation.",
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
                pending_validation_after_patch = False
                validation_snapshot = callbacks.build_validation_snapshot(latest_execution)
                completion_state = callbacks.completion_state(
                    latest_execution.results,
                    latest_preview_details,
                    validation_snapshot=validation_snapshot,
                )
                progress_snapshot = self.feedback.progress_snapshot(latest_execution.results, latest_preview_details, validation_snapshot)
                current_diagnostics_snapshot = AgentDiagnosticsDelta.snapshot(latest_execution.results)
                latest_diagnostics_delta = AgentDiagnosticsDelta.delta(previous_diagnostics_snapshot, current_diagnostics_snapshot)
                previous_diagnostics_snapshot = current_diagnostics_snapshot
                check_packets = self._repair_packets_from_execution(latest_execution)
                if check_packets:
                    latest_repair_packets = check_packets
                    latest_repair_cases = self.repair_cases.sync_from_packets(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        packets=latest_repair_packets,
                        source="agent_loop_checks",
                        trace_state=self.context_builder.store.get("reports", f"trace_reducer:{workspace_id}:{run_id}") or {},
                    )
                    latest_repair_packets = RepairCaseService.enrich_packets(latest_repair_packets, latest_repair_cases)
                    active_repair_case_id = RepairCaseService.active_case_id(latest_repair_cases)
                    callbacks.store_report(
                        f"repair_state:{workspace_id}:{run_id}",
                        {
                            "workspace_id": workspace_id,
                            "run_id": run_id,
                            "latest_repair_packets": latest_repair_packets,
                            "repair_cases_ref": RepairCaseService.index_ref(run_id),
                            "active_repair_case": latest_repair_cases.get("active_case"),
                            "diagnostics_delta": latest_diagnostics_delta,
                            "updated_at": utc_now().isoformat(),
                        },
                    )
                callbacks.store_report(
                    f"diagnostics_delta:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "delta": latest_diagnostics_delta,
                        "current_snapshot": current_diagnostics_snapshot,
                        "updated_at": utc_now().isoformat(),
                    },
                )
                iterations.append(
                    RunIterationRecord(
                        run_id=run_id,
                        assistant_message=latest_assistant_message or f"agent tool-call step {turn}",
                        files_read=list(latest_files_read),
                        file_changes=[
                            RunIterationAction(
                                file_path=operation.file_path,
                                operation=operation.operation,
                                reason=operation.reason,
                            )
                            for operation in latest_file_changes
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
                else:
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
                        all_file_changes=all_file_changes,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )

            if self._has_browser_infra_failure(latest_execution.results if latest_execution else []):
                return self.results.failed(
                    outcome_kind="blocked_preview_infra",
                    summary="Required browser verification infrastructure is unavailable.",
                    failure_reason="Playwright browser proof could not run; this is platform infrastructure, not generated app code.",
                    failure_class="blocked_preview_infra",
                    failure_signature=self.feedback.progress_signature(progress_snapshot),
                    root_cause_summary="Browser proof infrastructure is unavailable; install/configure Playwright before retrying create/workflow completion.",
                    current_phase="blocked_preview_infra",
                    remaining_issues=[],
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_file_changes=all_file_changes,
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
                    all_file_changes=all_file_changes,
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
                file_change_count=len(all_file_changes),
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
                        "file_change_count": len(all_file_changes),
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

            transition = RepairTransitionPolicy.decide(
                repair_packets=latest_repair_packets,
                repeated_failure_signatures={**repeated_failure_signatures, **repair_packet_counts},
                latest_files_read=latest_files_read,
            )
            latest_repair_transition = transition.as_dict()
            if transition.active:
                context_mode = transition.context_mode
                callbacks.store_report(
                    f"repair_state:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "latest_repair_packets": latest_repair_packets,
                        "next_forced_action": transition.next_forced_action,
                        "repair_transition": latest_repair_transition,
                        "repair_cases_ref": RepairCaseService.index_ref(run_id),
                        "active_repair_case": latest_repair_cases.get("active_case") if isinstance(latest_repair_cases, dict) else None,
                        "diagnostics_delta": latest_diagnostics_delta,
                        "updated_at": utc_now().isoformat(),
                    },
                )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Repair transition policy forced the next tool strategy.",
                    {
                        "turn": turn + 1,
                        "forced_tool_names": transition.forced_tool_names,
                        "forced_targets": transition.forced_targets,
                        "reason": transition.reason,
                    },
                )

            active_case = latest_repair_cases.get("active_case") if isinstance(latest_repair_cases, dict) else None
            if self._should_escalate_repeated_no_progress(
                repeated_count=repeated_failure_signatures[current_signature],
                has_draft_diff=has_draft_diff,
                active_case=active_case if isinstance(active_case, dict) else None,
                transition=latest_repair_transition,
            ):
                summary = "Repeated repair no-progress reached; focused repair continuation is required."
                failure_reason = (
                    f"{summary} failure_signature={current_signature[:240]} "
                    f"repeat_count={repeated_failure_signatures[current_signature]}"
                )
                callbacks.store_report(
                    f"repair_state:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "latest_repair_packets": latest_repair_packets,
                        "repair_cases_ref": RepairCaseService.index_ref(run_id),
                        "active_repair_case": active_case,
                        "next_forced_action": latest_repair_transition.get("next_forced_action") if isinstance(latest_repair_transition, dict) else {},
                        "diagnostics_delta": latest_diagnostics_delta,
                        "continuation_recommended": True,
                        "continuation_reason": "repeated_no_progress",
                        "repeat_count": repeated_failure_signatures[current_signature],
                        "updated_at": utc_now().isoformat(),
                    },
                )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Repeated repair no-progress reached; stopping this run for focused continuation.",
                    {
                        "turn": turn,
                        "failure_signature": current_signature,
                        "repeat_count": repeated_failure_signatures[current_signature],
                        "active_repair_case_id": active_repair_case_id,
                    },
                )
                if callbacks.append_activity:
                    callbacks.append_activity(
                        job,
                        "blocked",
                        "Focused repair continuation required",
                        {
                            "turn": turn,
                            "failure_signature": current_signature,
                            "repeat_count": repeated_failure_signatures[current_signature],
                            "active_repair_case_id": active_repair_case_id,
                        },
                    )
                return self.results.blocked(
                    summary=summary,
                    failure_reason=failure_reason,
                    failure_class="generation.repeated_no_progress",
                    failure_signature=f"repair.no_progress:{current_signature[:180]}",
                    root_cause_summary="The same evidence-backed repair case repeated without strict-green progress; continue in a fresh focused repair context.",
                    current_phase="blocked_repair_continuation_needed",
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    latest_apply_result=latest_apply_result,
                    iterations=iterations,
                    repair_iterations=repair_iterations,
                    all_file_changes=all_file_changes,
                    last_assistant_message=latest_assistant_message,
                    turn_history=turn_history,
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
                    repair_packets=latest_repair_packets,
                    next_forced_action=latest_repair_transition,
                    diagnostics_delta=latest_diagnostics_delta,
                )
            )
            plan_repair_packets = self._repair_packets_from_plan(plan)
            if plan_repair_packets:
                latest_repair_packets = self._with_repeated_counts(plan_repair_packets, repair_packet_counts)
                latest_repair_cases = self.repair_cases.sync_from_packets(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    packets=latest_repair_packets,
                    source="agent_loop_plan",
                    trace_state=self.context_builder.store.get("reports", f"trace_reducer:{workspace_id}:{run_id}") or {},
                )
                latest_repair_packets = RepairCaseService.enrich_packets(latest_repair_packets, latest_repair_cases)
                active_repair_case_id = RepairCaseService.active_case_id(latest_repair_cases)
                plan.metadata = {
                    **dict(plan.metadata or {}),
                    "repair_packets": latest_repair_packets,
                    "active_repair_case_id": active_repair_case_id,
                }
                callbacks.store_report(
                    f"repair_state:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "latest_repair_packets": latest_repair_packets,
                        "repair_cases_ref": RepairCaseService.index_ref(run_id),
                        "active_repair_case": latest_repair_cases.get("active_case"),
                        "next_forced_action": latest_repair_transition.get("next_forced_action") if isinstance(latest_repair_transition, dict) else {},
                        "diagnostics_delta": latest_diagnostics_delta,
                        "updated_at": utc_now().isoformat(),
                    },
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
                    "repair_packets": latest_repair_packets if plan_repair_packets else [],
                    "active_repair_case_id": active_repair_case_id,
                    "next_forced_action": latest_repair_transition.get("next_forced_action") if isinstance(latest_repair_transition, dict) else {},
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
                        all_file_changes=all_file_changes,
                        last_assistant_message=latest_assistant_message,
                        turn_history=turn_history,
                    )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Agent returned an invalid response; continuing with compact failure memory.",
                    {
                        "turn": turn + 1,
                        "diagnosis": plan.diagnosis,
                        "failure_signature": plan.failure_signature,
                        "repair_packets": latest_repair_packets,
                    },
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

            synced_file_changes = (
                list(plan.file_changes)
                if bool(plan.metadata.get("skip_contract_sync"))
                else callbacks.apply_change_sync(list(plan.file_changes))
            )
            current_patch_hash = patch_sha256(synced_file_changes)
            changed_targets = [operation.file_path for operation in synced_file_changes if operation.file_path]
            diagnostics_before = (
                LspToolService.diagnostics(root=draft_source, targets=changed_targets, include_optional_tools=False)
                if changed_targets
                else {}
            )
            if active_repair_case_id and self.repair_cases.repeated_patch(run_id=run_id, case_id=active_repair_case_id, patch_hash=current_patch_hash):
                repeated_packet = {
                    "signature": f"repair.repeated_patch:{active_repair_case_id}",
                    "issue_code": "repeated_identical_patch",
                    "code": "repeated_identical_patch",
                    "severity": "high",
                    "likely_root_cause": "The repair loop attempted the same patch hash again for the active repair case.",
                    "target_files": changed_targets,
                    "verification_check": "repair_case_attempt_ledger",
                    "instruction": "Do not repeat this patch. Read fresh evidence or choose a different constrained repair strategy.",
                    "required_next_tool": "read_files",
                    "suggested_tool_after_read": "apply_patch_to_draft_or_write_file",
                    "retry_policy": "do_not_retry_same_patch",
                    "failure_class": "repair.repeated_patch",
                    "failure_signature": f"repair.repeated_patch:{active_repair_case_id}",
                    "expected_proof": "next attempt uses different evidence or a different patch hash",
                    "evidence": {"patch_sha256": current_patch_hash, "changed_files": changed_targets},
                }
                latest_repair_packets = self._with_repeated_counts([repeated_packet], repair_packet_counts)
                self.repair_cases.record_attempt(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    case_id=active_repair_case_id,
                    attempt={
                        "status": "blocked",
                        "files_read": list(latest_files_read),
                        "changed_files": changed_targets,
                        "patch_sha256": current_patch_hash,
                        "diagnostics_before": diagnostics_before,
                        "failure_reason": "repeated_identical_patch",
                    },
                )
                callbacks.store_report(
                    f"repair_state:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "latest_repair_packets": latest_repair_packets,
                        "repair_cases_ref": RepairCaseService.index_ref(run_id),
                        "active_repair_case": self.repair_cases.get_case(run_id, active_repair_case_id),
                        "diagnostics_delta": latest_diagnostics_delta,
                        "updated_at": utc_now().isoformat(),
                    },
                )
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Repair attempt repeated an identical patch and was blocked by the attempt ledger.",
                    {"turn": turn + 1, "repair_case_id": active_repair_case_id, "patch_sha256": current_patch_hash, "changed_files": changed_targets},
                )
                previous_snapshot = progress_snapshot
                turn += 1
                continue
            callbacks.append_event(
                job,
                "patch_apply_started",
                "Applying agent patch to draft.",
                {"turn": turn + 1, "files": changed_targets, "active_repair_case_id": active_repair_case_id, "patch_sha256": current_patch_hash},
            )
            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "applying_patch",
                    "Applying agent draft changes",
                    {
                        "turn": turn + 1,
                        "file_change_count": len(synced_file_changes),
                        "file_count": len({operation.file_path for operation in synced_file_changes}),
                    },
                )
            if callbacks.before_apply is not None:
                callbacks.before_apply(turn + 1, synced_file_changes)
            try:
                with self.mutation_guard.lock(run_id):
                    envelope = self.context_builder.workspace_service.build_patch_envelope_for_file_changes(workspace_id, run_id, synced_file_changes)
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
                    synced_file_changes,
                    apply_result,
                    [operation.file_path for operation in synced_file_changes],
                )

            callbacks.store_report(
                f"patch:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "turn": turn + 1,
                    "file_changes": [
                        {
                            "file_path": operation.file_path,
                            "change_type": operation.operation,
                            "reason": operation.reason,
                        }
                        for operation in synced_file_changes
                    ],
                    "apply_result": latest_apply_result,
                },
            )
            latest_file_changes = list(synced_file_changes)
            if apply_result.status != "applied":
                conflict_packet = AgentEditValidator.repair_packet_for_apply_conflict(
                    conflict_reason=apply_result.conflict_reason or apply_result.status,
                    file_changes=synced_file_changes,
                    attempt=turn + 1,
                    repeated_count=0,
                )
                latest_repair_packets = self._with_repeated_counts([conflict_packet], repair_packet_counts)
                if active_repair_case_id:
                    self.repair_cases.record_attempt(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        case_id=active_repair_case_id,
                        attempt={
                            "status": "conflict",
                            "files_read": list(latest_files_read),
                            "changed_files": changed_targets,
                            "patch_sha256": current_patch_hash,
                            "diagnostics_before": diagnostics_before,
                            "failure_reason": apply_result.conflict_reason or apply_result.status,
                        },
                    )
                callbacks.store_report(
                    f"repair_state:{workspace_id}:{run_id}",
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "latest_repair_packets": latest_repair_packets,
                        "repair_cases_ref": RepairCaseService.index_ref(run_id),
                        "active_repair_case": self.repair_cases.get_case(run_id, active_repair_case_id) if active_repair_case_id else None,
                        "diagnostics_delta": latest_diagnostics_delta,
                        "updated_at": utc_now().isoformat(),
                    },
                )
                turn_history[-1]["result"] = "apply_conflict"
                turn_history[-1]["apply_error"] = apply_result.conflict_reason or apply_result.status
                turn_history[-1]["repair_packets"] = latest_repair_packets
                callbacks.append_event(
                    job,
                    "repair_iteration",
                    "Agent patch did not apply; continuing with conflict packet.",
                    {
                        "turn": turn + 1,
                        "conflict_reason": apply_result.conflict_reason or apply_result.status,
                        "repair_packets": latest_repair_packets,
                    },
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

            changed_files = callbacks.post_apply_stabilize(workspace_id, run_id, apply_result, [operation.file_path for operation in synced_file_changes]) if callbacks.post_apply_stabilize else [operation.file_path for operation in synced_file_changes]
            diagnostics_after = (
                LspToolService.diagnostics(root=draft_source, changed_files=list(changed_files), changed_only=True, include_optional_tools=False)
                if changed_files
                else {}
            )
            if active_repair_case_id:
                before_errors = int(diagnostics_before.get("error_count") or 0) if isinstance(diagnostics_before, dict) else 0
                after_errors = int(diagnostics_after.get("error_count") or 0) if isinstance(diagnostics_after, dict) else 0
                introduced_errors = after_errors > before_errors
                self.repair_cases.record_attempt(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    case_id=active_repair_case_id,
                    attempt={
                        "status": "failed" if introduced_errors else "applied",
                        "files_read": list(latest_files_read),
                        "changed_files": list(changed_files),
                        "patch_sha256": current_patch_hash,
                        "diagnostics_before": diagnostics_before,
                        "diagnostics_after": diagnostics_after,
                        "proof_result": {"status": "failed" if introduced_errors else "pending", "reason": "changed-file diagnostics introduced new errors" if introduced_errors else "checks run on next loop iteration"},
                        "failure_reason": "changed_file_diagnostics_regressed" if introduced_errors else "",
                    },
                )
            pending_validation_after_patch = True
            if callbacks.append_activity:
                callbacks.append_activity(
                    job,
                    "editing",
                    "Draft patch applied",
                    {"turn": turn + 1, "changed_file_count": len(set(changed_files)), "status": "applied"},
                )
            all_file_changes.extend(synced_file_changes)
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
                        repair_packets=latest_repair_packets,
                        repair_packet_ids=[
                            str(item.get("failure_signature") or item.get("signature") or item.get("code") or "")
                            for item in latest_repair_packets
                            if isinstance(item, dict)
                        ],
                        diagnostics_delta_ref=f"diagnostics_delta:{workspace_id}:{run_id}",
                        check_results=latest_execution.results if latest_execution else [],
                        latency_breakdown={},
                        token_usage={},
                    )
                )
            previous_snapshot = progress_snapshot
            turn += 1

    @staticmethod
    def _has_browser_infra_failure(results: list[RunCheckResult]) -> bool:
        repairable_failures = {
            "schema_validators",
            "connectivity_validators",
            "changed_files_static",
            "lsp_static_diagnostics",
            "platform_invariants",
            "frontend_interaction_static_smoke",
            "generated_app_python_tests",
            "generated_app_js_tests",
            "api_workflow_smoke",
            "preview_boot_smoke",
            "preview_connectivity_smoke",
        }
        if any(result.status in {"failed", "blocked"} and result.name in repairable_failures for result in results):
            return False
        for result in results:
            if result.name != "browser_flow_smoke" or result.status != "failed":
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            if diagnostics.get("infra_unavailable"):
                return True
        return False
