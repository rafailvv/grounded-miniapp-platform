from __future__ import annotations

from app.modules.miniapp_agent_loop.types import WorkspaceLoopTurnPlan


class WorkspaceLoopEditValidator:
    @staticmethod
    def normalize_plan(plan: WorkspaceLoopTurnPlan) -> WorkspaceLoopTurnPlan:
        if plan.outcome == "patch_ready" and not plan.operations:
            plan.outcome = "no_op"
        return plan

