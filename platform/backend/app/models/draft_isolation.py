from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel
from app.models.domain import utc_now


DraftIsolationStatus = Literal["created", "ready", "dirty", "gated", "applied", "discarded", "blocked"]
DraftGateStatus = Literal["passed", "failed", "blocked"]
DraftApplyDecisionStatus = Literal["allowed", "blocked", "applied"]


class DraftIsolationManifest(StrictModel):
    schema_: str = Field(default="grounded.draft_isolation.v1", alias="schema")
    workspace_id: str
    run_id: str
    isolation_id: str
    kind: Literal["filesystem_draft"] = "filesystem_draft"
    source_ref: str
    draft_source_dir: str
    base_revision_id: str | None = None
    base_commit_sha: str | None = None
    status: DraftIsolationStatus = "ready"
    parent_run_id: str | None = None
    parent_isolation_ref: str | None = None
    diff_sha256: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    gate_ref: str | None = None
    apply_decision_ref: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DraftGateReport(StrictModel):
    schema_: str = Field(default="grounded.draft_gate.v1", alias="schema")
    workspace_id: str
    run_id: str
    gate_ref: str
    isolation_ref: str
    status: DraftGateStatus
    diff_sha256: str
    changed_files: list[str] = Field(default_factory=list)
    checks_ref: str | None = None
    lsp_ref: str | None = None
    readiness_ref: str | None = None
    approval_required: bool = True
    apply_token: str | None = None
    blocking_reasons: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    next_sequence: int = 0


class DraftApplyDecision(StrictModel):
    schema_: str = Field(default="grounded.draft_apply_decision.v1", alias="schema")
    workspace_id: str
    run_id: str
    decision: DraftApplyDecisionStatus
    apply_token: str | None = None
    selected_files: list[str] = Field(default_factory=list)
    gate_ref: str | None = None
    revision_id: str | None = None
    blocked_reasons: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class DraftVariantReport(StrictModel):
    schema_: str = Field(default="grounded.draft_variant.v1", alias="schema")
    workspace_id: str
    source_run_id: str
    variant_run_id: str
    parent_isolation_ref: str
    isolation_ref: str
    status: str = "created"
    created_at: datetime = Field(default_factory=utc_now)
