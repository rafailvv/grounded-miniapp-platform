from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.domain import ContainerStatusRecord, FixAttemptRecord, FixScopeEntry, RunCheckResult


@dataclass
class FixTurnContext:
    workspace_id: str
    run_id: str
    attempt: int = 1
    failure_class: str | None = None
    failure_signature: str | None = None
    failing_command: str | None = None
    root_cause_summary: str | None = None
    exact_error_excerpt: str | None = None
    implicated_files: list[str] = field(default_factory=list)
    container_statuses: list[ContainerStatusRecord] = field(default_factory=list)
    container_logs: dict[str, list[str]] = field(default_factory=dict)
    write_scope: list[FixScopeEntry] = field(default_factory=list)
    attempt_history: list[FixAttemptRecord | dict[str, Any]] = field(default_factory=list)
    executed_checks: list[RunCheckResult] = field(default_factory=list)
    api_failure_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    memory_context: str | None = None


@dataclass
class FixPromptContext:
    workspace_id: str
    run_id: str
    attempt: int
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    exact_error_excerpt: str | None = None
    context_mode: Literal["minimal", "expanded", "full_bundle"] = "minimal"
    failing_checks: list[dict[str, Any]] = field(default_factory=list)
    api_failure_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    normalized_critical_issues: list[dict[str, Any]] = field(default_factory=list)
    failing_file_paths: list[str] = field(default_factory=list)
    expected_contract: dict[str, Any] = field(default_factory=dict)
    file_contexts: dict[str, str] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    read_only_surfaces: list[str] = field(default_factory=list)
    previous_attempt_summary: str | None = None
    previous_diff_summary: str | None = None
    repair_base: str | None = None
