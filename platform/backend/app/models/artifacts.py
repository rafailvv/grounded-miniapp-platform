from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models.common import StrictModel


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
    explanation: str
    trace_refs: list[str] = Field(default_factory=list)


class ArtifactPlanModel(StrictModel):
    plan_id: str
    workspace_id: str
    summary: str
    operations: list[PatchOperationModel]


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
