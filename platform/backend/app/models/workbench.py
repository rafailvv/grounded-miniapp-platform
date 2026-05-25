from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from app.models.common import StrictModel
from app.models.event_journal import EventJournalPage, EventJournalPayload, RunEventV2, RunJournalState, ThreadEventV2, ThreadJournalState
from app.models.observability import ObservabilityReport
from app.models.protocol import (
    AppProtocolManifest,
    ProtocolApprovalState,
    ProtocolEnvelopeV1,
    ProtocolEnvelopeV2,
    ProtocolEventState,
    ProtocolRunState,
    ProtocolSchemaCatalog,
    ProtocolToolCallState,
    ProtocolTurnState,
    ProtocolWorkerUpdate,
    RpcMethodSpecV2,
    RpcProtocolReport,
)
from app.models.webhooks import WebhookDeliveryReport, WebhookListReport, WebhookSubscription


class WorkbenchApiModel(StrictModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, use_enum_values=True)


class ArtifactRef(WorkbenchApiModel):
    ref: str | None = None
    kind: str | None = None
    path: str | None = None
    label: str | None = None
    mime_type: str | None = None


class CheckResult(WorkbenchApiModel):
    check_id: str | None = None
    name: str
    status: str = "pending"
    details: str | None = None
    duration_ms: int | None = None
    command: str | None = None
    exit_code: int | None = None
    logs: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class RunEvent(WorkbenchApiModel):
    event_id: str
    run_id: str
    event_type: str
    sequence: int
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunStateSnapshot(WorkbenchApiModel):
    snapshot_id: str
    run_id: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ToolEnvelope(WorkbenchApiModel):
    tool_call_id: str
    tool: str
    version: str
    status: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    risk: str
    approval: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None
    sandbox_profile: str | None = None
    allowed_paths: dict[str, Any] = Field(default_factory=dict)
    side_effects: list[str] = Field(default_factory=list)
    side_effect_class: str | None = None
    parallel_safe: bool = False
    progress: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    timing: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    failure_signature: str | None = None
    repair_recipe_ids: list[str] = Field(default_factory=list)
    retry: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    truncation: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: str | None = None


class ToolEventsReport(WorkbenchApiModel):
    run_id: str
    tool_protocol_version: str
    events: list[ToolEnvelope] = Field(default_factory=list)


class RunProtocolEvent(WorkbenchApiModel):
    schema_: str | None = Field(default=None, alias="schema")
    event_id: str
    run_id: str
    workspace_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    turn_id: str | None = None
    sequence: int
    type: str
    status: str
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)
    bookmark_id: str | None = None
    source_event_type: str | None = None
    created_at: str


class RunBookmark(WorkbenchApiModel):
    schema_: str | None = Field(default=None, alias="schema")
    bookmark_id: str
    run_id: str
    workspace_id: str
    turn_id: str | None = None
    response_id: str | None = None
    checkpoint_ref: str | None = None
    trace_bundle_ref: str | None = None
    diff_sha256: str | None = None
    tool_result_count: int = 0
    latest_check_ref: str | None = None
    todo_state_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunProtocolReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_protocol.v1", alias="schema")
    run_id: str
    workspace_id: str | None = None
    status: str
    items: list[RunProtocolEvent] = Field(default_factory=list)
    next_sequence: int | None = None
    bookmarks: list[RunBookmark] = Field(default_factory=list)
    latest_bookmark: RunBookmark | None = None


class RunBookmarksReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_bookmarks.v1", alias="schema")
    run_id: str
    status: str
    items: list[RunBookmark] = Field(default_factory=list)


class RunEventReplayReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_event_replay.v1", alias="schema")
    run_id: str
    workspace_id: str | None = None
    status: str = "ok"
    replay_cursor: int = 0
    event_count: int = 0
    latest_status: str | None = None
    latest_stage: str | None = None
    blocking: bool = False
    run: dict[str, Any] = Field(default_factory=dict)
    journal_state: dict[str, Any] = Field(default_factory=dict)
    event_page: dict[str, Any] = Field(default_factory=dict)
    protocol: dict[str, Any] = Field(default_factory=dict)
    bookmarks: list[RunBookmark] = Field(default_factory=list)
    failure_point: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)
    replay_refs: dict[str, Any] = Field(default_factory=dict)


class RunCompareReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_compare.v1", alias="schema")
    status: str = "ok"
    base_run_id: str
    target_run_id: str
    workspace_id: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    field_changes: list[dict[str, Any]] = Field(default_factory=list)
    file_delta: dict[str, Any] = Field(default_factory=dict)
    check_delta: dict[str, Any] = Field(default_factory=dict)
    readiness_delta: dict[str, Any] = Field(default_factory=dict)
    failure_delta: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)


class DiffReviewFile(WorkbenchApiModel):
    path: str
    product_area: str
    file_class: str
    status: str = "modified"
    risk: str = "medium"
    additions: int = 0
    deletions: int = 0
    why_changed: str | None = None
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class DiffReviewGroup(WorkbenchApiModel):
    key: str
    title: str
    product_area: str
    file_class: str
    risk: str
    files: list[DiffReviewFile] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class RunDiffReviewReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_diff_review.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: str = "ok"
    base: str = "source"
    target: str = "draft"
    files: list[DiffReviewFile] = Field(default_factory=list)
    groups: list[DiffReviewGroup] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    refs: dict[str, Any] = Field(default_factory=dict)


class RunSessionCheckpoint(WorkbenchApiModel):
    schema_: str | None = Field(default=None, alias="schema")
    checkpoint_id: str
    run_id: str
    workspace_id: str
    kind: str
    status: str
    source: str | None = None
    summary: str | None = None
    created_at: str | None = None
    run_status: str | None = None
    apply_status: str | None = None
    current_stage: str | None = None
    revision_id: str | None = None
    failure_class: str | None = None
    failure_signature: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class RunSessionCheckpointsReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_session_checkpoints.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: str = "ok"
    items: list[RunSessionCheckpoint] = Field(default_factory=list)
    latest_good_run_id: str | None = None
    latest_good_revision_id: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)


class RunEventsReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.run_events.v1", alias="schema")
    run_id: str
    status: str
    blocking: bool = False
    items: list[RunEvent] = Field(default_factory=list)
    protocol_events: list[RunProtocolEvent] = Field(default_factory=list)
    compaction_events: list[RunProtocolEvent] = Field(default_factory=list)
    state_snapshots: list[RunStateSnapshot] = Field(default_factory=list)
    next_sequence: int


class RunTimelineItem(WorkbenchApiModel):
    sequence: int | None = None
    kind: str
    status: str
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunTimelineReport(WorkbenchApiModel):
    run_id: str
    tool_protocol_version: str
    items: list[RunTimelineItem] = Field(default_factory=list)


class RunTraceReducerSummary(WorkbenchApiModel):
    why: str | None = None
    failed_checks: list[RunTimelineItem] = Field(default_factory=list)
    patches: list[RunTimelineItem] = Field(default_factory=list)
    browser_proofs: list[RunTimelineItem] = Field(default_factory=list)
    failures: list[RunTimelineItem] = Field(default_factory=list)
    fixes: list[RunTimelineItem] = Field(default_factory=list)


class RunTraceViewReport(WorkbenchApiModel):
    run_id: str
    trace_id: str
    status: str
    apply_status: str
    timeline: list[RunTimelineItem] = Field(default_factory=list)
    reducer: RunTraceReducerSummary
    artifact_refs: dict[str, str | None] = Field(default_factory=dict)
    reduced_trace: dict[str, Any] = Field(default_factory=dict)


class TraceState(WorkbenchApiModel):
    schema_: str | None = Field(default=None, alias="schema")
    trace_id: str | None = None
    run_id: str | None = None
    workspace_id: str | None = None
    event_count: int = 0
    turns: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    prompt_contexts: list[dict[str, Any]] = Field(default_factory=list)
    skill_edges: list[dict[str, Any]] = Field(default_factory=list)
    memory_edges: list[dict[str, Any]] = Field(default_factory=list)
    diff_edges: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_gate: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    proof_edges: list[dict[str, Any]] = Field(default_factory=list)
    compact_boundaries: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] = Field(default_factory=dict)
    payload_refs: list[dict[str, Any]] = Field(default_factory=list)
    protocol_events: list[RunProtocolEvent] = Field(default_factory=list)
    model_response_bookmarks: list[RunBookmark] = Field(default_factory=list)
    final_terminal_event: dict[str, Any] | None = None


class TraceBundleReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.trace_bundle.v1", alias="schema")
    trace_id: str | None = None
    run_id: str
    workspace_id: str
    status: str
    bundle_dir: str | None = None
    manifest_path: str | None = None
    trace_path: str | None = None
    state_path: str | None = None
    payload_dir: str | None = None
    event_count: int = 0
    payload_count: int = 0
    state: TraceState = Field(default_factory=TraceState)


class RepairAttempt(WorkbenchApiModel):
    attempt_id: str | None = None
    status: str | None = None
    summary: str | None = None
    run_id: str | None = None
    created_at: str | None = None


class RepairCase(WorkbenchApiModel):
    schema_: str | None = Field(default=None, alias="schema")
    case_id: str
    workspace_id: str
    run_id: str
    status: str
    source: str | None = None
    repair_catalog_version: str | None = None
    failure_class: str | None = None
    failure_signature: str | None = None
    normalized_signature: str | None = None
    signature_normalization: dict[str, Any] = Field(default_factory=dict)
    issue_code: str | None = None
    severity: str | None = None
    likely_cause: str | None = None
    failed_check: str | None = None
    likely_files: list[str] = Field(default_factory=list)
    probable_files: list[dict[str, Any]] = Field(default_factory=list)
    broken_surface: dict[str, Any] = Field(default_factory=dict)
    post_fix_proof: dict[str, Any] = Field(default_factory=dict)
    post_repair_proof: dict[str, Any] = Field(default_factory=dict)
    known_fix_recipe: dict[str, Any] = Field(default_factory=dict)
    known_fix_recipes: list[dict[str, Any]] = Field(default_factory=list)
    repair_class: str | None = None
    repair_classification: dict[str, Any] = Field(default_factory=dict)
    focused_patch_plan: dict[str, Any] = Field(default_factory=dict)
    relevant_checks: list[dict[str, Any]] = Field(default_factory=list)
    check_profile: str | None = None
    escalation: dict[str, Any] = Field(default_factory=dict)
    product_guardrails: dict[str, Any] = Field(default_factory=dict)
    repair_confidence: dict[str, Any] = Field(default_factory=dict)
    browser_replay: dict[str, Any] = Field(default_factory=dict)
    api_replay: dict[str, Any] = Field(default_factory=dict)
    repair_packet: dict[str, Any] = Field(default_factory=dict)
    target_files: list[str] = Field(default_factory=list)
    forbidden_files: list[str] = Field(default_factory=list)
    required_next_tool: str | None = None
    allowed_edit_slice: list[str] = Field(default_factory=list)
    expected_proof: list[dict[str, Any]] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    attempts: list[RepairAttempt] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    forbidden_repeat_action: str | None = None
    current: bool | None = None
    last_seen_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    next_action: dict[str, Any] = Field(default_factory=dict)
    repair_prompt: dict[str, Any] | str | None = None


class RepairCasesReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.repair_cases.v1", alias="schema")
    run_id: str
    status: str
    items: list[RepairCase] = Field(default_factory=list)
    active_case: RepairCase | None = None
    case_refs: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class RepairAttemptsReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.repair_attempts.v1", alias="schema")
    run_id: str
    case_id: str
    status: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class GateIssue(WorkbenchApiModel):
    kind: str
    check: str
    details: str
    blocking: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProductReadinessCheck(WorkbenchApiModel):
    key: str
    label: str
    status: str
    required: bool = True
    check: str | None = None
    details: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProductReadinessResult(WorkbenchApiModel):
    schema_: str = Field(default="grounded.product_readiness.v1", alias="schema")
    status: str
    acceptance_required: bool = True
    required_checks: list[ProductReadinessCheck] = Field(default_factory=list)
    checklist: list[ProductReadinessCheck] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    blocking_reasons: list[GateIssue] = Field(default_factory=list)
    repair_case_ids: list[str] = Field(default_factory=list)
    next_forced_action: dict[str, Any] = Field(default_factory=dict)


class GenerationModeSlaProfile(WorkbenchApiModel):
    mode: str
    label: str
    objective: str
    required_checks: list[str] = Field(default_factory=list)
    optional_checks: list[str] = Field(default_factory=list)
    proof_requirements: list[str] = Field(default_factory=list)
    final_gate: list[str] = Field(default_factory=list)
    context_policy: str = "standard"
    worker_policy: str = "serial"
    max_repair_attempts: int = 1
    audit_level: str = "light"
    output_style: str = "concise"


class GenerationModeSlaManifest(WorkbenchApiModel):
    schema_: str = Field(default="grounded.generation_sla.v1", alias="schema")
    default_mode: str = "balanced"
    modes: list[GenerationModeSlaProfile] = Field(default_factory=list)
    second_queue: list[dict[str, Any]] = Field(default_factory=list)
    compatibility: dict[str, Any] = Field(default_factory=dict)


class PromptCompletionAuditReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.prompt_completion_audit.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: str
    required: bool = True
    prompt: str = ""
    requirement_count: int = 0
    covered_count: int = 0
    uncovered_count: int = 0
    rows: list[dict[str, Any]] = Field(default_factory=list)
    uncovered: list[dict[str, Any]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    proof_summary: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str | None] = Field(default_factory=dict)
    created_at: str


class VisualRegressionReport(WorkbenchApiModel):
    schema_: str = Field(default="grounded.visual_regression.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: str
    blocking: bool = False
    mobile_viewports: list[dict[str, Any]] = Field(default_factory=list)
    mobile_viewport_screenshots: list[dict[str, Any]] = Field(default_factory=list)
    role_page_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    dom_state_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    overflow_overlap: dict[str, Any] = Field(default_factory=dict)
    visual_diffs: list[dict[str, Any]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: dict[str, str | None] = Field(default_factory=dict)
    created_at: str


class GateReport(WorkbenchApiModel):
    run_id: str
    workspace_id: str
    status: str
    blocking: bool
    issues: list[GateIssue] = Field(default_factory=list)
    repair_packets: list[dict[str, Any]] = Field(default_factory=list)
    repair_cases: RepairCasesReport
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    next_forced_action: dict[str, Any] = Field(default_factory=dict)
    blocking_repair_packet: dict[str, Any] = Field(default_factory=dict)
    requirements: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str | None] = Field(default_factory=dict)
    run_state: dict[str, Any] = Field(default_factory=dict)
    product_readiness: ProductReadinessResult | None = None
    requirement_traceability: dict[str, Any] = Field(default_factory=dict)
    prompt_completion_audit: dict[str, Any] = Field(default_factory=dict)
    visual_regression: dict[str, Any] = Field(default_factory=dict)


class SchemaModelRef(WorkbenchApiModel):
    name: str
    purpose: str


class SystemSchemaShapes(WorkbenchApiModel):
    app_protocol_manifest: AppProtocolManifest | None = None
    protocol_schema_catalog: ProtocolSchemaCatalog | None = None
    protocol_envelope_v1: ProtocolEnvelopeV1 | None = None
    protocol_envelope_v2: ProtocolEnvelopeV2 | None = None
    protocol_run_state: ProtocolRunState | None = None
    protocol_turn_state: ProtocolTurnState | None = None
    protocol_tool_call_state: ProtocolToolCallState | None = None
    protocol_approval_state: ProtocolApprovalState | None = None
    protocol_event_state: ProtocolEventState | None = None
    protocol_worker_update: ProtocolWorkerUpdate | None = None
    artifact_ref: ArtifactRef | None = None
    check_result: CheckResult | None = None
    run_event: RunEvent | None = None
    run_event_v2: RunEventV2 | None = None
    thread_event_v2: ThreadEventV2 | None = None
    event_journal_page: EventJournalPage | None = None
    event_journal_payload: EventJournalPayload | None = None
    observability_report: ObservabilityReport | None = None
    webhook_subscription: WebhookSubscription | None = None
    webhook_list_report: WebhookListReport | None = None
    webhook_delivery_report: WebhookDeliveryReport | None = None
    run_journal_state: RunJournalState | None = None
    thread_journal_state: ThreadJournalState | None = None
    tool_envelope: ToolEnvelope | None = None
    gate_report: GateReport | None = None
    product_readiness_result: ProductReadinessResult | None = None
    prompt_completion_audit_report: PromptCompletionAuditReport | None = None
    generation_mode_sla_manifest: GenerationModeSlaManifest | None = None
    visual_regression_report: VisualRegressionReport | None = None
    repair_case: RepairCase | None = None
    trace_state: TraceState | None = None


class SystemSchemaManifest(WorkbenchApiModel):
    schema_: str = Field(default="grounded.system_schema.v1", alias="schema")
    status: str = "ok"
    openapi_url: str = "/openapi.json"
    app_protocol_url: str = "/system/app-protocol"
    app_protocol_schema_url: str = "/system/app-protocol/schemas"
    app_protocol_fixture_root: str = "platform/backend/app/schemas/app_protocol"
    generated_types_path: str = "platform/frontend/src/lib/generated/openapi-types.ts"
    models: list[SchemaModelRef] = Field(default_factory=list)
    model_shapes: SystemSchemaShapes | None = None


SYSTEM_SCHEMA_MODEL_REFS: tuple[SchemaModelRef, ...] = (
    SchemaModelRef(name="AppProtocolManifest", purpose="Versioned app protocol manifest for runs, turns, tools, approvals, events, and workers."),
    SchemaModelRef(name="ProtocolSchemaCatalog", purpose="JSON Schema fixture catalog for the app protocol."),
    SchemaModelRef(name="ProtocolEnvelopeV1", purpose="Stable v1 compatibility envelope."),
    SchemaModelRef(name="ProtocolEnvelopeV2", purpose="Current additive protocol envelope."),
    SchemaModelRef(name="ProtocolRunState", purpose="Versioned run lifecycle payload."),
    SchemaModelRef(name="ProtocolTurnState", purpose="Versioned turn lifecycle payload."),
    SchemaModelRef(name="ProtocolToolCallState", purpose="Versioned tool-call lifecycle payload."),
    SchemaModelRef(name="ProtocolApprovalState", purpose="Versioned approval request payload."),
    SchemaModelRef(name="ProtocolEventState", purpose="Versioned event journal payload."),
    SchemaModelRef(name="ProtocolWorkerUpdate", purpose="Versioned worker update payload."),
    SchemaModelRef(name="RunEvent", purpose="Append-only run event wrapper."),
    SchemaModelRef(name="RunEventV2", purpose="Append-only run journal event with payload ref."),
    SchemaModelRef(name="ThreadEventV2", purpose="Append-only thread journal event with payload ref."),
    SchemaModelRef(name="EventJournalPage", purpose="Paged run/thread journal response."),
    SchemaModelRef(name="EventJournalPayload", purpose="Payload document addressed by event payload ref."),
    SchemaModelRef(name="RunJournalState", purpose="Reduced run state reconstructed from v2 journal."),
    SchemaModelRef(name="ThreadJournalState", purpose="Reduced thread state reconstructed from v2 journal."),
    SchemaModelRef(name="ToolEnvelope", purpose="Canonical workbench tool-call envelope."),
    SchemaModelRef(name="ToolEventsReport", purpose="Run tool event report."),
    SchemaModelRef(name="ProductReadinessResult", purpose="Strict production acceptance proof result."),
    SchemaModelRef(name="RunEventsReport", purpose="Run event report."),
    SchemaModelRef(name="RunProtocolReport", purpose="Run protocol event report."),
    SchemaModelRef(name="RunEventReplayReport", purpose="Replay artifact reconstructed from typed journal, protocol events, and bookmarks."),
    SchemaModelRef(name="RunCompareReport", purpose="Run-to-run diff for fork/resume/version comparison."),
    SchemaModelRef(name="RpcProtocolReport", purpose="Typed JSON-RPC method and capability manifest."),
    SchemaModelRef(name="RpcMethodSpecV2", purpose="Typed JSON-RPC method contract."),
    SchemaModelRef(name="RpcIdempotency", purpose="RPC idempotency-key policy."),
    SchemaModelRef(name="RpcCursorPage", purpose="RPC cursor pagination contract."),
    SchemaModelRef(name="RunTimelineReport", purpose="Run timeline report."),
    SchemaModelRef(name="RunTraceViewReport", purpose="Run trace view report."),
    SchemaModelRef(name="TraceBundleReport", purpose="Trace bundle report."),
    SchemaModelRef(name="CheckResult", purpose="Workbench check execution result."),
    SchemaModelRef(name="ArtifactRef", purpose="Stable reference to persisted run artifacts."),
    SchemaModelRef(name="GateReport", purpose="Generation acceptance gate report."),
    SchemaModelRef(name="GenerationModeSlaManifest", purpose="Product SLA profiles for generation modes and second-priority platform capabilities."),
    SchemaModelRef(name="PromptCompletionAuditReport", purpose="Prompt-to-artifact completion audit for final readiness gates."),
    SchemaModelRef(name="VisualRegressionReport", purpose="Generated-app snapshot and visual regression proof report."),
    SchemaModelRef(name="RepairCase", purpose="Evidence-driven repair case."),
    SchemaModelRef(name="TraceState", purpose="Reduced trace bundle state."),
    SchemaModelRef(name="ThreadSnapshot", purpose="Typed thread read snapshot."),
    SchemaModelRef(name="ContextPressureReport", purpose="Context budget pressure report and recommendations."),
    SchemaModelRef(name="OutputArtifactIndex", purpose="Indexed head/tail command output artifacts."),
    SchemaModelRef(name="PromptSuggestionsReport", purpose="Product follow-up prompts generated after a run."),
    SchemaModelRef(name="MemoryRetrievalResult", purpose="Top-k workspace memory retrieval response."),
    SchemaModelRef(name="MemorySummaryReport", purpose="Always-loaded compact workspace memory summary with details available on demand."),
    SchemaModelRef(name="ObservabilityReport", purpose="Cost, latency, failure class, green-rate, and repair success dashboard."),
    SchemaModelRef(name="WebhookSubscription", purpose="SDK-managed webhook subscription."),
    SchemaModelRef(name="WebhookListReport", purpose="Paged webhook subscription list."),
    SchemaModelRef(name="WebhookDeliveryReport", purpose="Webhook test/delivery status report."),
)


def system_schema_manifest() -> SystemSchemaManifest:
    return SystemSchemaManifest(models=list(SYSTEM_SCHEMA_MODEL_REFS))
