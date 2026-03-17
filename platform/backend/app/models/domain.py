from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from app.models.common import GenerationMode, PreviewProfile, StrictModel, TargetPlatform


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class RevisionRecord(StrictModel):
    revision_id: str = Field(default_factory=lambda: new_id("rev"))
    commit_sha: str
    message: str
    source: Literal["template_clone", "manual_edit", "ai_patch", "reset"]
    created_at: datetime = Field(default_factory=utc_now)


class WorkspaceRecord(StrictModel):
    workspace_id: str = Field(default_factory=lambda: new_id("ws"))
    name: str
    description: str | None = None
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK
    path: str
    template_cloned: bool = False
    current_revision_id: str | None = None
    revisions: list[RevisionRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DocumentChunkRecord(StrictModel):
    chunk_id: str = Field(default_factory=lambda: new_id("chunk"))
    section_title: str | None = None
    content: str
    semantic_role: str = "general"


class DocumentRecord(StrictModel):
    document_id: str = Field(default_factory=lambda: new_id("doc"))
    workspace_id: str
    file_name: str
    file_path: str
    source_type: Literal[
        "project_doc",
        "openapi",
        "codebase",
        "platform_doc",
        "user_prompt",
        "assumption",
    ]
    content: str
    indexed: bool = False
    chunks: list[DocumentChunkRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ChatTurnRecord(StrictModel):
    turn_id: str = Field(default_factory=lambda: new_id("turn"))
    workspace_id: str
    role: Literal["user", "assistant"]
    content: str
    summary: str | None = None
    linked_job_id: str | None = None
    linked_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class JobEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    event_type: Literal[
        "retrieval_started",
        "spec_ready",
        "spec_blocked",
        "ir_ready",
        "validation_failed",
        "artifact_plan_ready",
        "patch_applied",
        "build_started",
        "repair_iteration",
        "preview_ready",
        "job_failed",
        "job_completed",
    ]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ValidationSnapshot(StrictModel):
    grounded_spec_valid: bool = False
    app_ir_valid: bool = False
    build_valid: bool = False
    blocking: bool = True
    issues: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class JobRecord(StrictModel):
    job_id: str = Field(default_factory=lambda: new_id("job"))
    workspace_id: str
    prompt: str
    status: Literal["pending", "running", "blocked", "completed", "failed"] = "pending"
    generation_mode: GenerationMode = GenerationMode.QUALITY
    target_platform: TargetPlatform
    preview_profile: PreviewProfile
    current_revision_id: str | None = None
    fidelity: Literal["quality_app", "balanced_app", "basic_scaffold", "blocked"] = "blocked"
    llm_enabled: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    model_profile: str | None = None
    linked_run_id: str | None = None
    failure_reason: str | None = None
    compile_summary: dict[str, int | str] = Field(default_factory=dict)
    events: list[JobEvent] = Field(default_factory=list)
    summary: str | None = None
    assumptions_report: list[dict] = Field(default_factory=list)
    traceability_report_id: str | None = None
    validation_snapshot: ValidationSnapshot | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PreviewRecord(StrictModel):
    preview_id: str = Field(default_factory=lambda: new_id("preview"))
    workspace_id: str
    status: Literal["stopped", "running", "error"] = "stopped"
    url: str | None = None
    frontend_url: str | None = None
    backend_url: str | None = None
    proxy_port: int | None = None
    runtime_mode: Literal["inline", "docker"] = "docker"
    project_name: str | None = None
    logs: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ExportRecord(StrictModel):
    export_id: str = Field(default_factory=lambda: new_id("export"))
    workspace_id: str
    export_type: Literal["zip", "git_patch"]
    file_path: str
    created_at: datetime = Field(default_factory=utc_now)


class CreateWorkspaceRequest(StrictModel):
    name: str
    description: str | None = None
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK


class SaveDocumentRequest(StrictModel):
    file_name: str
    file_path: str
    source_type: Literal[
        "project_doc",
        "openapi",
        "codebase",
        "platform_doc",
        "user_prompt",
        "assumption",
    ]
    content: str


class CreateChatTurnRequest(StrictModel):
    role: Literal["user", "assistant"] = "user"
    content: str
    summary: str | None = None
    linked_job_id: str | None = None
    linked_run_id: str | None = None


class GenerateRequest(StrictModel):
    prompt: str
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK
    generation_mode: GenerationMode = GenerationMode.QUALITY
    intent: Literal["auto", "create", "edit", "refine", "role_only_change"] = "auto"
    model_profile: str = "openai_code_fast"
    linked_run_id: str | None = None


class SaveFileRequest(StrictModel):
    relative_path: str
    content: str


class CodeChangeTarget(StrictModel):
    file_path: str
    operation: Literal["create", "update", "delete"]
    reason: str
    risk: Literal["low", "medium", "high"] = "medium"


class CodeChangePlan(StrictModel):
    plan_id: str = Field(default_factory=lambda: new_id("change_plan"))
    workspace_id: str
    run_id: str | None = None
    intent: Literal["create", "edit", "refine", "role_only_change"]
    summary: str
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    targets: list[CodeChangeTarget] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    acceptance_checks: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class RunChecksSummary(StrictModel):
    validators: Literal["pending", "passed", "failed", "blocked"] = "pending"
    build: Literal["pending", "passed", "failed", "blocked"] = "pending"
    preview: Literal["pending", "passed", "failed", "blocked"] = "pending"
    issues: list[dict[str, Any]] = Field(default_factory=list)


class RunRecord(StrictModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    workspace_id: str
    prompt: str
    intent: Literal["create", "edit", "refine", "role_only_change"]
    apply_strategy: Literal["staged_auto_apply", "manual_approve"] = "staged_auto_apply"
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    model_profile: str = "openai_code_fast"
    llm_provider: str | None = None
    llm_model: str | None = None
    linked_job_id: str | None = None
    source_revision_id: str | None = None
    result_revision_id: str | None = None
    status: Literal["pending", "running", "awaiting_approval", "completed", "blocked", "failed"] = "pending"
    apply_status: Literal["pending", "applied", "awaiting_approval", "blocked", "failed"] = "pending"
    summary: str | None = None
    failure_reason: str | None = None
    checks_summary: RunChecksSummary = Field(default_factory=RunChecksSummary)
    touched_files: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CreateRunRequest(StrictModel):
    prompt: str
    intent: Literal["auto", "create", "edit", "refine", "role_only_change"] = "auto"
    apply_strategy: Literal["staged_auto_apply", "manual_approve"] = "staged_auto_apply"
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    model_profile: str = "openai_code_fast"
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK
    generation_mode: GenerationMode = GenerationMode.QUALITY
