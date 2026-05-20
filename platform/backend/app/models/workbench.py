from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from app.models.common import StrictModel
from app.models.event_journal import EventJournalPage, EventJournalPayload, RunEventV2, RunJournalState, ThreadEventV2, ThreadJournalState
from app.models.observability import ObservabilityReport
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
    risk: str
    approval: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None
    sandbox_profile: str | None = None
    progress: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
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
    failure_class: str | None = None
    failure_signature: str | None = None
    issue_code: str | None = None
    severity: str | None = None
    likely_cause: str | None = None
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


class SchemaModelRef(WorkbenchApiModel):
    name: str
    purpose: str


class SystemSchemaShapes(WorkbenchApiModel):
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
    repair_case: RepairCase | None = None
    trace_state: TraceState | None = None


class SystemSchemaManifest(WorkbenchApiModel):
    schema_: str = Field(default="grounded.system_schema.v1", alias="schema")
    status: str = "ok"
    openapi_url: str = "/openapi.json"
    generated_types_path: str = "platform/frontend/src/lib/generated/openapi-types.ts"
    models: list[SchemaModelRef] = Field(default_factory=list)
    model_shapes: SystemSchemaShapes | None = None


SYSTEM_SCHEMA_MODEL_REFS: tuple[SchemaModelRef, ...] = (
    SchemaModelRef(name="RunEvent", purpose="Append-only run event wrapper."),
    SchemaModelRef(name="RunEventV2", purpose="Append-only run journal event with payload ref."),
    SchemaModelRef(name="ThreadEventV2", purpose="Append-only thread journal event with payload ref."),
    SchemaModelRef(name="EventJournalPage", purpose="Paged run/thread journal response."),
    SchemaModelRef(name="EventJournalPayload", purpose="Payload document addressed by event payload ref."),
    SchemaModelRef(name="RunJournalState", purpose="Reduced run state reconstructed from v2 journal."),
    SchemaModelRef(name="ThreadJournalState", purpose="Reduced thread state reconstructed from v2 journal."),
    SchemaModelRef(name="ToolEnvelope", purpose="Canonical workbench tool-call envelope."),
    SchemaModelRef(name="ToolEventsReport", purpose="Run tool event report."),
    SchemaModelRef(name="RunEventsReport", purpose="Run event report."),
    SchemaModelRef(name="RunProtocolReport", purpose="Run protocol event report."),
    SchemaModelRef(name="RunTimelineReport", purpose="Run timeline report."),
    SchemaModelRef(name="RunTraceViewReport", purpose="Run trace view report."),
    SchemaModelRef(name="TraceBundleReport", purpose="Trace bundle report."),
    SchemaModelRef(name="CheckResult", purpose="Workbench check execution result."),
    SchemaModelRef(name="ArtifactRef", purpose="Stable reference to persisted run artifacts."),
    SchemaModelRef(name="GateReport", purpose="Generation acceptance gate report."),
    SchemaModelRef(name="RepairCase", purpose="Evidence-driven repair case."),
    SchemaModelRef(name="TraceState", purpose="Reduced trace bundle state."),
    SchemaModelRef(name="ThreadSnapshot", purpose="Typed thread read snapshot."),
    SchemaModelRef(name="ContextPressureReport", purpose="Context budget pressure report and recommendations."),
    SchemaModelRef(name="OutputArtifactIndex", purpose="Indexed head/tail command output artifacts."),
    SchemaModelRef(name="PromptSuggestionsReport", purpose="Product follow-up prompts generated after a run."),
    SchemaModelRef(name="MemoryRetrievalResult", purpose="Top-k workspace memory retrieval response."),
    SchemaModelRef(name="ObservabilityReport", purpose="Cost, latency, failure class, green-rate, and repair success dashboard."),
    SchemaModelRef(name="WebhookSubscription", purpose="SDK-managed webhook subscription."),
    SchemaModelRef(name="WebhookListReport", purpose="Paged webhook subscription list."),
    SchemaModelRef(name="WebhookDeliveryReport", purpose="Webhook test/delivery status report."),
)


def system_schema_manifest() -> SystemSchemaManifest:
    return SystemSchemaManifest(models=list(SYSTEM_SCHEMA_MODEL_REFS))
