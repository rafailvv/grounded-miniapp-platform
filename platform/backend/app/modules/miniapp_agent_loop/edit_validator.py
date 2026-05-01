from __future__ import annotations

from app.modules.miniapp_agent_loop.types import WorkspaceLoopTurnPlan


class WorkspaceLoopEditValidator:
    PATCH_ENVELOPE_MARKERS = ("*** Begin Patch", "*** End Patch", "*** Add File:", "*** Delete File:", "*** Update File:")

    @staticmethod
    def normalize_plan(plan: WorkspaceLoopTurnPlan) -> WorkspaceLoopTurnPlan:
        if plan.outcome == "patch_ready" and not plan.operations:
            plan.outcome = "no_op"
        if plan.outcome == "patch_ready":
            for operation in plan.operations:
                content = str(getattr(operation, "content", None) or "")
                if getattr(operation, "operation", "") in {"create", "replace"} and any(
                    marker in content for marker in WorkspaceLoopEditValidator.PATCH_ENVELOPE_MARKERS
                ):
                    plan.outcome = "fatal_invalid_response"
                    plan.diagnosis = (
                        f"{operation.file_path} was returned as {operation.operation} content containing patch envelope markers. "
                        "Return structured DraftFileOperation content only, or use operation='patch' with a diff."
                    )
                    plan.failure_class = "generation.invalid_patch_operation"
                    plan.failure_signature = "generation.invalid_patch_operation:patch_envelope_in_content"
                    plan.root_cause_summary = "The model returned an apply_patch envelope as file content."
                    plan.operations = []
                    break
        return plan
