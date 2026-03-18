from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel
from app.models.domain import new_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: Literal["low", "medium", "high", "critical"]
    location: str
    blocking: bool = True


class GroundedSpecValidatorResult(StrictModel):
    valid: bool
    blocking: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class AppIRValidatorResult(StrictModel):
    valid: bool
    blocking: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class PatchOperationModel(StrictModel):
    operation_id: str
    op: Literal["create", "update", "delete"]
    file_path: str
    content: str | None = None
    diff: str | None = None
    diff_format: Literal["unified_diff"] = "unified_diff"
    explanation: str
    trace_refs: list[str] = Field(default_factory=list)
    precondition: dict[str, Any] | None = None


class PatchOpPrecondition(StrictModel):
    file_hash: str | None = None
    max_fuzz: int = 0


class PatchEnvelope(StrictModel):
    patch_id: str = Field(default_factory=lambda: new_id("patch"))
    workspace_id: str
    base_revision_id: str | None = None
    summary: str
    risk_level: Literal["low", "medium", "high"] = "medium"
    prompt_cache_key: str | None = None
    ops: list[PatchOperationModel] = Field(default_factory=list)
    post_actions: dict[str, Any] = Field(default_factory=dict)
    ui: dict[str, Any] = Field(default_factory=dict)


class ApplyPatchResult(StrictModel):
    apply_id: str = Field(default_factory=lambda: new_id("patch_apply"))
    workspace_id: str
    run_id: str | None = None
    base_revision_id: str | None = None
    status: Literal["applied", "conflict", "blocked", "failed"] = "blocked"
    conflict_reason: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    revision_id: str | None = None
    applied_at: datetime = Field(default_factory=utc_now)


class ArtifactPlanModel(StrictModel):
    plan_id: str
    workspace_id: str
    summary: str
    operations: list[PatchOperationModel]
    patch_envelope: PatchEnvelope | None = None


class TraceabilityReportEntry(StrictModel):
    trace_id: str
    source_ref: str
    source_kind: str
    target_id: str
    target_type: str
    mapping_note: str | None = None


class TraceabilityReportModel(StrictModel):
    report_id: str
    workspace_id: str
    entries: list[TraceabilityReportEntry]
