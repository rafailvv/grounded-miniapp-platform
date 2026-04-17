from __future__ import annotations

from app.modules.miniapp_agent_loop.types import WorkspaceLoopResult


class WorkspaceLoopResultFactory:
    @staticmethod
    def failed(
        *,
        outcome_kind: str,
        summary: str,
        failure_reason: str,
        failure_class: str | None,
        failure_signature: str | None,
        root_cause_summary: str | None,
        current_phase: str,
        remaining_issues: list[dict],
        latest_execution,
        latest_preview_details,
        latest_apply_result,
        iterations,
        repair_iterations,
        all_operations,
        last_assistant_message: str,
        turn_history,
    ) -> WorkspaceLoopResult:
        return WorkspaceLoopResult(
            status="failed",
            outcome_kind=outcome_kind,
            summary=summary,
            failure_reason=failure_reason,
            failure_class=failure_class,
            failure_signature=failure_signature,
            root_cause_summary=root_cause_summary,
            current_phase=current_phase,
            remaining_issues=remaining_issues,
            latest_execution=latest_execution,
            latest_preview_details=latest_preview_details,
            latest_apply_result=latest_apply_result,
            iterations=iterations,
            repair_iterations=repair_iterations,
            all_operations=all_operations,
            last_assistant_message=last_assistant_message,
            turn_history=turn_history,
        )

    @staticmethod
    def blocked(
        *,
        summary: str,
        failure_reason: str,
        failure_class: str | None,
        failure_signature: str | None,
        root_cause_summary: str | None,
        current_phase: str,
        latest_execution,
        latest_preview_details,
        latest_apply_result,
        iterations,
        repair_iterations,
        all_operations,
        last_assistant_message: str,
        turn_history,
    ) -> WorkspaceLoopResult:
        return WorkspaceLoopResult(
            status="blocked",
            outcome_kind="blocked_generation",
            summary=summary,
            failure_reason=failure_reason,
            failure_class=failure_class,
            failure_signature=failure_signature,
            root_cause_summary=root_cause_summary,
            current_phase=current_phase,
            remaining_issues=[],
            latest_execution=latest_execution,
            latest_preview_details=latest_preview_details,
            latest_apply_result=latest_apply_result,
            iterations=iterations,
            repair_iterations=repair_iterations,
            all_operations=all_operations,
            last_assistant_message=last_assistant_message,
            turn_history=turn_history,
        )

    @staticmethod
    def completed(
        *,
        latest_execution,
        latest_preview_details,
        latest_apply_result,
        iterations,
        repair_iterations,
        all_operations,
        last_assistant_message: str,
        turn_history,
    ) -> WorkspaceLoopResult:
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
            last_assistant_message=last_assistant_message,
            turn_history=turn_history,
        )

