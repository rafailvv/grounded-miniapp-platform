from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftAction,
    JobRecord,
    RepairIterationRecord,
    RunIterationRecord,
    ValidationSnapshot,
)
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookOutcome


LoopOutcome = Literal["changes_ready", "no_op", "needs_context", "fatal_invalid_response"]
LoopContextMode = Literal["minimal", "expanded"]


@dataclass
class AgentTurnPlan:
    outcome: LoopOutcome
    assistant_message: str = ""
    diagnosis: str | None = None
    file_changes: list[DraftAction] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    fix_targets: list[str] = field(default_factory=list)
    expected_verification: str | None = None
    rationale_by_file: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairTurnContext:
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    failing_checks: list[Any] = field(default_factory=list)
    implicated_files: list[str] = field(default_factory=list)
    file_contexts: dict[str, str] = field(default_factory=dict)
    previous_turn_summary: str | None = None
    previous_diff_summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentLoopResult:
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
    all_file_changes: list[DraftAction]
    last_assistant_message: str
    turn_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentLoopCallbacks:
    execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]]
    build_validation_snapshot: Callable[[CheckExecutionRecord], ValidationSnapshot]
    completion_state: Callable[[list[Any], dict[str, Any], ValidationSnapshot | None], dict[str, Any]]
    has_tooling_failure: Callable[[list[Any]], bool]
    plan_turn: Callable[..., AgentTurnPlan]
    apply_change_sync: Callable[[list[DraftAction]], list[DraftAction]]
    verify_completion: Callable[[CheckExecutionRecord | None, dict[str, Any]], dict[str, Any]] | None
    before_apply: Callable[[int, list[DraftAction]], AgentHookOutcome | None] | None
    after_apply: Callable[[int, list[DraftAction], Any, list[str]], None] | None
    post_apply_stabilize: Callable[[str, str, Any, list[str]], list[str]] | None
    append_event: Callable[[JobRecord, str, str, dict[str, Any] | None], None]
    append_activity: Callable[[JobRecord, str, str, dict[str, Any] | None], None] | None
    append_trace: Callable[[str, str, str, dict[str, Any] | None], None]
    store_report: Callable[[str, dict[str, Any]], None]
    record_compact_boundary: Callable[[dict[str, Any]], None] | None = None
    allow_optimistic_completion: bool = False
    skip_initial_checks: bool = False
    stop_if_requested: Callable[[], bool] | None = None
    budget_status: Callable[[int], dict[str, Any]] | None = None


__all__ = [
    "LoopContextMode",
    "LoopOutcome",
    "RepairTurnContext",
    "AgentLoopCallbacks",
    "AgentLoopResult",
    "AgentTurnPlan",
    "GenerationMode",
]
