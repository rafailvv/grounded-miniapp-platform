from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class ContextPressureSection(StrictModel):
    key: str
    label: str
    tokens: int = 0
    ratio: float = 0.0
    budget_tokens: int | None = None
    top_contributors: list[dict[str, Any]] = Field(default_factory=list)


class ContextPressureRecommendation(StrictModel):
    code: str
    message: str
    section: str
    severity: str = "info"
    tokens: int = 0
    action: str = ""
    artifact_ref: str | None = None
    microcompact_ref: str | None = None
    paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MicrocompactCandidate(StrictModel):
    tool: str | None = None
    status: str | None = None
    original_chars: int = 0
    tokens_estimate: int = 0
    microcompact_ref: str | None = None
    artifact_ref: str | None = None
    digest: str | None = None
    reason: str = "large_tool_output"


class FileReadHint(StrictModel):
    path: str
    read_count: int = 0
    duplicate_token_estimate: int = 0
    recommendation: str = "Use cached file context/current diff; re-read only after mutation or for a precise missing range."


class StalePathReference(StrictModel):
    path: str
    source: str = "transcript"
    reason: str = "missing_in_workspace"
    read_count: int = 0
    last_sequence: int | None = None
    suggested_path: str | None = None
    action: str = "refresh_path_reference"


class ContextPhaseBudget(StrictModel):
    phase: str
    status: str = "pending"
    token_budget: int = 0
    tokens_used: int = 0
    token_ratio: float = 0.0
    cost_budget_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    action: str = "stay_within_phase_budget"


class CompactBoundaryWarning(StrictModel):
    recommended: bool = False
    pressure_ratio: float = 0.0
    threshold: float = 0.8
    message: str = ""
    boundary_ref: str | None = None
    reason: str | None = None


class ContextPressureSnapshot(StrictModel):
    schema_: str = Field(default="grounded.context_pressure_snapshot.v2", alias="schema")
    total_tokens_estimate: int = 0
    context_window_tokens: int = 128_000
    pressure_ratio: float = 0.0
    sections: dict[str, ContextPressureSection] = Field(default_factory=dict)
    section_tokens: dict[str, int] = Field(default_factory=dict)
    recommendations: list[ContextPressureRecommendation] = Field(default_factory=list)
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    microcompact_candidates: list[MicrocompactCandidate] = Field(default_factory=list)
    avoid_reread_files: list[FileReadHint] = Field(default_factory=list)
    stale_path_refs: list[StalePathReference] = Field(default_factory=list)
    phase_budgets: list[ContextPhaseBudget] = Field(default_factory=list)
    token_cost_budget: dict[str, Any] = Field(default_factory=dict)
    duplicate_file_reads: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_read_token_estimate: int = 0
    compact_boundary: CompactBoundaryWarning = Field(default_factory=CompactBoundaryWarning)
    compact_recommended: bool = False
    attempt: int | None = None
    tool_round: int | None = None
    created_at: str | None = None


class ContextPressureReport(StrictModel):
    schema_: str = Field(default="grounded.context_pressure.v2", alias="schema")
    workspace_id: str
    run_id: str
    status: str = "empty"
    latest: ContextPressureSnapshot | None = None
    items: list[ContextPressureSnapshot] = Field(default_factory=list)
    sections: dict[str, ContextPressureSection] = Field(default_factory=dict)
    recommendations: list[ContextPressureRecommendation] = Field(default_factory=list)
    microcompact_candidates: list[MicrocompactCandidate] = Field(default_factory=list)
    avoid_reread_files: list[FileReadHint] = Field(default_factory=list)
    stale_path_refs: list[StalePathReference] = Field(default_factory=list)
    phase_budgets: list[ContextPhaseBudget] = Field(default_factory=list)
    token_cost_budget: dict[str, Any] = Field(default_factory=dict)
    compact_boundary: CompactBoundaryWarning = Field(default_factory=CompactBoundaryWarning)
    updated_at: str | None = None
