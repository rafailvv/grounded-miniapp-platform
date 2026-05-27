from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


ContextAction = Literal["include", "summarize", "microcompact", "artifact_ref", "discard", "refresh", "defer"]


class ContextBudgetSection(StrictModel):
    key: str
    priority: int = 50
    budget_tokens: int = 0
    always_load: bool = False
    stale_ttl_seconds: int | None = None
    overflow_action: ContextAction = "summarize"


class ContextBudgetPolicy(StrictModel):
    schema_: str = Field(default="grounded.context_budget_policy.v1", alias="schema")
    generation_mode: str = "balanced"
    run_mode: str = "generate"
    context_window_tokens: int = 128_000
    target_prompt_tokens: int = 96_000
    compact_threshold_ratio: float = 0.80
    tool_result_tail: int = 12
    sections: dict[str, ContextBudgetSection] = Field(default_factory=dict)
    actions: list[ContextAction] = Field(
        default_factory=lambda: ["include", "summarize", "microcompact", "artifact_ref", "discard", "refresh", "defer"]
    )


class ContextFragmentDecision(StrictModel):
    fragment_id: str
    section: str
    action: ContextAction
    reason: str = ""
    priority: int = 50
    estimated_tokens: int = 0
    budget_tokens: int = 0
    source: str | None = None
    ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextStaleRef(StrictModel):
    path: str
    source: str = "context"
    reason: str = "stale_reference"
    suggested_path: str | None = None
    action: ContextAction = "refresh"


class ContextManifest(StrictModel):
    schema_: str = Field(default="grounded.context_manifest.v1", alias="schema")
    manifest_id: str
    workspace_id: str
    run_id: str
    session_id: str | None = None
    status: str = "ready"
    total_tokens_estimate: int = 0
    included_tokens_estimate: int = 0
    target_prompt_tokens: int = 0
    included_sections: list[str] = Field(default_factory=list)
    summarized_sections: list[str] = Field(default_factory=list)
    ref_sections: list[str] = Field(default_factory=list)
    dropped_sections: list[str] = Field(default_factory=list)
    included_refs: list[str] = Field(default_factory=list)
    dropped_refs: list[str] = Field(default_factory=list)
    decisions: list[ContextFragmentDecision] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextManagerReport(StrictModel):
    schema_: str = Field(default="grounded.context_manager.v1", alias="schema")
    status: str = "ready"
    workspace_id: str
    session_id: str | None = None
    run_id: str
    report_ref: str
    manifest_ref: str | None = None
    policy: ContextBudgetPolicy
    manifest: ContextManifest
    decisions: list[ContextFragmentDecision] = Field(default_factory=list)
    pressure: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    proofs: list[dict[str, Any]] = Field(default_factory=list)
    bookmarks: list[dict[str, Any]] = Field(default_factory=list)
    included_refs: list[str] = Field(default_factory=list)
    dropped_refs: list[str] = Field(default_factory=list)
    stale_refs: list[ContextStaleRef] = Field(default_factory=list)
    history_normalization: dict[str, Any] = Field(default_factory=dict)
    next_sequence: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
