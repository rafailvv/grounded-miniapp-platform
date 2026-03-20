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
    source: Literal["template_clone", "manual_edit", "ai_patch", "reset", "rollback"]
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
    start_line: int | None = None
    end_line: int | None = None


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
        "job_started",
        "resume_started",
        "indexing_started",
        "retrieval_started",
        "retrieval_completed",
        "spec_started",
        "spec_ready",
        "spec_blocked",
        "draft_prepared",
        "role_contract_started",
        "role_contract_ready",
        "planning_started",
        "planning_ready",
        "context_pack_started",
        "context_pack_ready",
        "editing_started",
        "iteration_ready",
        "validation_failed",
        "build_started",
        "checks_completed",
        "preview_skipped_due_to_build_failure",
        "repair_started",
        "repair_iteration",
        "repair_scope_expanded",
        "repair_repeated_signature_aborted",
        "triage_started",
        "triage_completed",
        "repair_planned",
        "patch_apply_started",
        "patch_apply_completed",
        "frontend_build_started",
        "backend_compile_started",
        "preview_validation_started",
        "failure_reanalyzed",
        "scope_expanded",
        "planner_contract_gap_detected",
        "apply_started",
        "apply_completed",
        "preview_rebuild_started",
        "preview_rebuild_completed",
        "preview_rebuild_failed",
        "preview_ready",
        "draft_ready",
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


RunMode = Literal["generate", "fix"]


class ErrorContext(StrictModel):
    raw_error: str
    source: Literal["build", "preview", "miniapp", "frontend", "runtime"] | None = None
    failing_target: str | None = None


class JobRecord(StrictModel):
    job_id: str = Field(default_factory=lambda: new_id("job"))
    workspace_id: str
    prompt: str
    status: Literal["pending", "running", "blocked", "completed", "failed"] = "pending"
    mode: RunMode = "generate"
    generation_mode: GenerationMode = GenerationMode.BALANCED
    target_platform: TargetPlatform
    preview_profile: PreviewProfile
    current_revision_id: str | None = None
    fidelity: Literal["fast_app", "quality_app", "balanced_app", "basic_scaffold", "blocked"] = "blocked"
    llm_enabled: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    model_profile: str | None = None
    linked_run_id: str | None = None
    error_context: ErrorContext | None = None
    failure_reason: str | None = None
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    current_fix_phase: str | None = None
    current_failing_command: str | None = None
    current_exit_code: int | None = None
    fix_targets: list[str] = Field(default_factory=list)
    handoff_from_failed_generate: dict[str, Any] | None = None
    executed_checks: list[dict[str, Any]] = Field(default_factory=list)
    fix_attempts: list[dict[str, Any]] = Field(default_factory=list)
    scope_expansions: list[dict[str, Any]] = Field(default_factory=list)
    container_statuses: list[dict[str, Any]] = Field(default_factory=list)
    compile_summary: dict[str, int | str] = Field(default_factory=dict)
    events: list[JobEvent] = Field(default_factory=list)
    summary: str | None = None
    assumptions_report: list[dict] = Field(default_factory=list)
    traceability_report_id: str | None = None
    validation_snapshot: ValidationSnapshot | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    latency_breakdown: dict[str, float | int] = Field(default_factory=dict)
    retrieval_stats: dict[str, Any] = Field(default_factory=dict)
    cache_stats: dict[str, Any] = Field(default_factory=dict)
    repair_iterations: list[dict[str, Any]] = Field(default_factory=list)
    apply_result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PreviewRecord(StrictModel):
    preview_id: str = Field(default_factory=lambda: new_id("preview"))
    workspace_id: str
    status: Literal["stopped", "starting", "running", "error"] = "stopped"
    stage: Literal["idle", "starting", "rebuilding", "health_check", "running", "error"] = "idle"
    progress_percent: int = 0
    url: str | None = None
    frontend_url: str | None = None
    backend_url: str | None = None
    proxy_port: int | None = None
    runtime_mode: Literal["inline", "docker"] = "docker"
    project_name: str | None = None
    draft_run_id: str | None = None
    logs: list[str] = Field(default_factory=list)
    last_error: str | None = None
    latency_breakdown: dict[str, float | int] = Field(default_factory=dict)
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
    mode: RunMode = "generate"
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK
    generation_mode: GenerationMode = GenerationMode.BALANCED
    intent: Literal["auto", "create", "edit", "refine", "role_only_change"] = "auto"
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    model_profile: str = "openai_code_fast"
    linked_run_id: str | None = None
    resume_from_run_id: str | None = None
    error_context: ErrorContext | None = None


class SaveFileRequest(StrictModel):
    relative_path: str
    content: str
    run_id: str | None = None


class DraftFileOperation(StrictModel):
    operation_id: str = Field(default_factory=lambda: new_id("draft_op"))
    file_path: str
    operation: Literal["create", "replace", "delete"]
    content: str | None = None
    reason: str


class CodeChunkRecord(StrictModel):
    chunk_id: str = Field(default_factory=lambda: new_id("code_chunk"))
    workspace_id: str
    revision_id: str
    path: str
    language: str
    kind: Literal["code", "doc"] = "code"
    start_line: int
    end_line: int
    text: str
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    chunk_hash: str
    summary: str | None = None
    embedding: list[float] = Field(default_factory=list)
    source_type: str | None = None
    score: float | None = None


class CodeEmbeddingRecord(StrictModel):
    embedding_id: str = Field(default_factory=lambda: new_id("embedding"))
    chunk_id: str
    model: str = "local-hash-v1"
    vector: list[float] = Field(default_factory=list)


class IndexStatusRecord(StrictModel):
    workspace_id: str
    revision_id: str | None = None
    status: Literal["missing", "indexing", "ready", "error"] = "missing"
    chunk_count: int = 0
    indexed_at: datetime | None = None
    error: str | None = None


class ContextPack(StrictModel):
    workspace_id: str
    revision_id: str | None = None
    prompt: str
    system_prefix: str
    workspace_summary: str
    current_task: str
    recent_diff: str = ""
    code_chunks: list[CodeChunkRecord] = Field(default_factory=list)
    doc_chunks: list[CodeChunkRecord] = Field(default_factory=list)
    targeted_files: dict[str, str] = Field(default_factory=dict)
    prompt_cache_key: str
    retrieval_stats: dict[str, Any] = Field(default_factory=dict)


class RunCheckResult(StrictModel):
    check_id: str = Field(default_factory=lambda: new_id("check"))
    name: str
    status: Literal["pending", "passed", "failed", "blocked", "skipped"] = "pending"
    details: str | None = None
    duration_ms: int | None = None
    command: str | None = None
    exit_code: int | None = None
    logs: list[str] = Field(default_factory=list)


class CheckExecutionRecord(StrictModel):
    execution_id: str = Field(default_factory=lambda: new_id("check_exec"))
    workspace_id: str
    run_id: str
    revision_id: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    results: list[RunCheckResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None


class RunIterationOperation(StrictModel):
    file_path: str
    operation: Literal["create", "replace", "delete"]
    reason: str


class RunIterationRecord(StrictModel):
    iteration_id: str = Field(default_factory=lambda: new_id("iter"))
    run_id: str
    assistant_message: str
    files_read: list[str] = Field(default_factory=list)
    operations: list[RunIterationOperation] = Field(default_factory=list)
    check_results: list[RunCheckResult] = Field(default_factory=list)
    diff_summary: str | None = None
    role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    latency_breakdown: dict[str, float | int] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    failure_class: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RepairIterationRecord(StrictModel):
    repair_iteration_id: str = Field(default_factory=lambda: new_id("repair_iter"))
    run_id: str
    attempt: int
    files_read: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    check_results: list[RunCheckResult] = Field(default_factory=list)
    latency_breakdown: dict[str, float | int] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ContainerStatusRecord(StrictModel):
    service: str
    name: str | None = None
    state: str | None = None
    status: str | None = None
    health: str | None = None
    exit_code: str | None = None
    published_port: str | None = None


class FixScopeEntry(StrictModel):
    file_path: str
    reason: str


class FixCase(StrictModel):
    fix_case_id: str = Field(default_factory=lambda: new_id("fix_case"))
    workspace_id: str
    run_id: str
    attempt: int = 1
    failure_class: str | None = None
    failure_signature: str | None = None
    failing_command: str | None = None
    root_cause_summary: str | None = None
    exact_error_excerpt: str | None = None
    implicated_files: list[str] = Field(default_factory=list)
    container_statuses: list[ContainerStatusRecord] = Field(default_factory=list)
    container_logs: dict[str, list[str]] = Field(default_factory=dict)
    write_scope: list[FixScopeEntry] = Field(default_factory=list)
    attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    executed_checks: list[RunCheckResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class FixAttemptRecord(StrictModel):
    fix_attempt_id: str = Field(default_factory=lambda: new_id("fix_attempt"))
    run_id: str
    attempt: int
    diagnosis: str
    commands: list[str] = Field(default_factory=list)
    exit_codes: dict[str, int | None] = Field(default_factory=dict)
    files_changed: list[str] = Field(default_factory=list)
    implicated_files: list[str] = Field(default_factory=list)
    failure_signature: str | None = None
    result: Literal["patched", "green", "conflict", "failed", "stopped"] = "patched"
    rationale_by_file: dict[str, str] = Field(default_factory=dict)
    expected_verification: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CodeChangeTarget(StrictModel):
    file_path: str
    operation: Literal["create", "replace", "delete"]
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
    validators: Literal["pending", "passed", "failed", "blocked", "skipped"] = "pending"
    build: Literal["pending", "passed", "failed", "blocked", "skipped"] = "pending"
    preview: Literal["pending", "passed", "failed", "blocked", "skipped"] = "pending"
    issues: list[dict[str, Any]] = Field(default_factory=list)


class RunRecord(StrictModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    workspace_id: str
    prompt: str
    mode: RunMode = "generate"
    intent: Literal["create", "edit", "refine", "role_only_change"]
    apply_strategy: Literal["staged_auto_apply", "manual_approve"] = "staged_auto_apply"
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    model_profile: str = "openai_code_fast"
    generation_mode: GenerationMode = GenerationMode.BALANCED
    llm_provider: str | None = None
    llm_model: str | None = None
    linked_job_id: str | None = None
    resume_from_run_id: str | None = None
    source_revision_id: str | None = None
    result_revision_id: str | None = None
    candidate_revision_id: str | None = None
    status: Literal["pending", "running", "awaiting_approval", "completed", "blocked", "failed"] = "pending"
    apply_status: Literal["pending", "applied", "awaiting_approval", "blocked", "failed", "rolled_back", "noop"] = "pending"
    draft_status: Literal["none", "ready", "approved", "discarded", "failed"] = "none"
    draft_ready: bool = False
    approval_required: bool = False
    iteration_count: int = 0
    current_stage: str = "queued"
    progress_percent: int = 0
    summary: str | None = None
    failure_reason: str | None = None
    failure_class: str | None = None
    failure_signature: str | None = None
    root_cause_summary: str | None = None
    current_fix_phase: str | None = None
    current_failing_command: str | None = None
    current_exit_code: int | None = None
    fix_targets: list[str] = Field(default_factory=list)
    handoff_from_failed_generate: dict[str, Any] | None = None
    error_context: ErrorContext | None = None
    checks_summary: RunChecksSummary = Field(default_factory=RunChecksSummary)
    touched_files: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    latency_breakdown: dict[str, float | int] = Field(default_factory=dict)
    repair_iterations: list[dict[str, Any]] = Field(default_factory=list)
    fix_attempts: list[dict[str, Any]] = Field(default_factory=list)
    scope_expansions: list[dict[str, Any]] = Field(default_factory=list)
    apply_result: dict[str, Any] | None = None
    retrieval_stats: dict[str, Any] = Field(default_factory=dict)
    cache_stats: dict[str, Any] = Field(default_factory=dict)
    rolled_back: bool = False
    rolled_back_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CreateRunRequest(StrictModel):
    prompt: str
    mode: RunMode = "generate"
    intent: Literal["auto", "create", "edit", "refine", "role_only_change"] = "auto"
    apply_strategy: Literal["staged_auto_apply", "manual_approve"] = "staged_auto_apply"
    target_role_scope: list[Literal["client", "specialist", "manager"]] = Field(default_factory=list)
    model_profile: str = "openai_code_fast"
    target_platform: TargetPlatform = TargetPlatform.TELEGRAM
    preview_profile: PreviewProfile = PreviewProfile.TELEGRAM_MOCK
    generation_mode: GenerationMode = GenerationMode.BALANCED
    resume_from_run_id: str | None = None
    error_context: ErrorContext | None = None
