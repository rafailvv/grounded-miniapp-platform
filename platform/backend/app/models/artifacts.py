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


ExecutionClass = Literal["shell_app", "entity_workflow_app", "workflow_dashboard_app", "data_crud_app"]
PreviewFailureKind = Literal["address_pool_exhausted", "container_name_conflict", "network_conflict", "compose_start_failure", "unknown"]
RunOutcomeKind = Literal["applied", "warnings", "blocked_generation", "blocked_preview_infra", "noop_materialization_failure"]


class MaterializationReport(StrictModel):
    execution_class: ExecutionClass
    planned_files: list[str] = Field(default_factory=list)
    created_files: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)
    expected_backend_files: list[str] = Field(default_factory=list)
    missing_backend_files: list[str] = Field(default_factory=list)
    backend_surface_ok: bool = False
    page_surface_ok: bool = False
    manifest_surface_ok: bool = False
    fell_back_to_template: bool = False
    role_page_counts: dict[str, int] = Field(default_factory=dict)
    stage_reports: list[dict[str, Any]] = Field(default_factory=list)


class PreviewInfraDiagnostics(StrictModel):
    failure_kind: PreviewFailureKind = "unknown"
    retry_count: int = 0
    cleanup_attempted: bool = False
    reused_existing_runtime: bool = False
    cooldown_until: datetime | None = None
    last_error: str | None = None
