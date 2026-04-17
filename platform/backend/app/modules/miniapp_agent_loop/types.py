from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    JobRecord,
    RepairIterationRecord,
    RunIterationRecord,
    ValidationSnapshot,
)


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


__all__ = [
    "LoopContextMode",
    "LoopOutcome",
    "WorkspaceLoopCallbacks",
    "WorkspaceLoopResult",
    "WorkspaceLoopTurnPlan",
    "GenerationMode",
]

