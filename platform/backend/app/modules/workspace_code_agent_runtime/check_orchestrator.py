from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord


@dataclass(frozen=True)
class AgentCheckPlan:
    scope_mode: str
    check_profile: str
    check_attempt: int


class WorkspaceAgentCheckOrchestrator:
    """Selects check scope without embedding that branching in the runtime facade."""

    @staticmethod
    def plan(
        *,
        focused_visual_edit: bool,
        create_intent: bool,
        acceptance_required: bool,
        generation_mode: GenerationMode,
        has_draft_diff: bool,
    ) -> AgentCheckPlan:
        check_profile = (
            "focused_edit"
            if focused_visual_edit
            else "full"
            if create_intent or acceptance_required or generation_mode == GenerationMode.QUALITY
            else "fast_gate"
        )
        return AgentCheckPlan(
            scope_mode="focused_edit" if focused_visual_edit else "agentic",
            check_profile=check_profile,
            check_attempt=1 if has_draft_diff else 0,
        )

    @staticmethod
    def fast_gate_passed(execution: CheckExecutionRecord) -> bool:
        return not any(result.status == "failed" for result in execution.results)

    @classmethod
    def should_run_final_gate(
        cls,
        *,
        check_profile: str,
        execution: CheckExecutionRecord,
        has_draft_diff: bool,
        create_intent: bool,
        acceptance_required: bool,
    ) -> bool:
        return (
            check_profile == "fast_gate"
            and cls.fast_gate_passed(execution)
            and has_draft_diff
            and not (create_intent or acceptance_required)
        )

    @staticmethod
    def preview_details(preview: Any) -> dict[str, Any]:
        return {
            "status": "skipped",
            "stage": getattr(preview, "stage", "idle"),
            "progress_percent": getattr(preview, "progress_percent", 0),
            "logs": list(getattr(preview, "logs", [])),
            "last_error": getattr(preview, "last_error", None),
            "containers": [],
            "container_logs": {},
        }
