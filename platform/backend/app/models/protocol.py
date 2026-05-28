from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field

from app.models.common import StrictModel


ProtocolVersion = Literal["v1", "v2"]
ProtocolSubject = Literal["run", "turn", "tool_call", "approval", "event", "worker_update"]
ProtocolStatus = Literal["queued", "started", "running", "waiting", "completed", "failed", "blocked", "cancelled", "skipped"]
RpcStability = Literal["stable", "experimental", "deprecated"]
RpcCursorKind = Literal["none", "opaque_cursor", "sequence_cursor", "offset_cursor"]
RpcIdempotencyMode = Literal["none", "optional", "recommended", "required"]


class RpcErrorObject(StrictModel):
    schema_: str = Field(default="grounded.rpc.error.v2", alias="schema")
    code: int
    message: str
    data: dict[str, Any] | list[Any] | str | int | float | bool | None = None


class RpcRequestEnvelopeV2(StrictModel):
    schema_: str = Field(default="grounded.rpc.request.v2", alias="schema")
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, validation_alias=AliasChoices("idempotency_key", "idempotencyKey"))
    metadata: dict[str, Any] = Field(default_factory=dict)


class RpcResponseEnvelopeV2(StrictModel):
    schema_: str = Field(default="grounded.rpc.response.v2", alias="schema")
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any | None = None
    error: RpcErrorObject | None = None
    idempotency_key: str | None = Field(default=None, validation_alias=AliasChoices("idempotency_key", "idempotencyKey"))


class RpcNotificationEnvelopeV2(StrictModel):
    schema_: str = Field(default="grounded.rpc.notification.v2", alias="schema")
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    sequence: int | None = None


class RpcCursorPage(StrictModel):
    cursor_kind: RpcCursorKind = "none"
    cursor_param: str | None = None
    next_cursor_field: str | None = None
    limit_param: str = "limit"


class RpcIdempotency(StrictModel):
    mode: RpcIdempotencyMode = "none"
    key_field: str = "idempotency_key"
    header: str = "Idempotency-Key"
    scope: Literal["request", "workspace", "thread", "run", "global"] = "request"


class ExperimentalFieldSpec(StrictModel):
    name: str
    reason: str = ""
    since: str | None = None
    replacement: str | None = None


class CompatibilityRule(StrictModel):
    rule_id: str
    description: str
    since: str = "v2"
    enforcement: Literal["documented", "tested", "enforced"] = "tested"


class RpcMethodSpecV2(StrictModel):
    method: str
    version: Literal["v2"] = "v2"
    transport: Literal["websocket"] = "websocket"
    stability: RpcStability = "stable"
    idempotent: bool = False
    description: str = ""
    params_model: str
    result_model: str
    notification_models: list[str] = Field(default_factory=list)
    params_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: str | None = None
    cursor: RpcCursorPage = Field(default_factory=RpcCursorPage)
    idempotency: RpcIdempotency = Field(default_factory=RpcIdempotency)
    experimental: list[ExperimentalFieldSpec] = Field(
        default_factory=list,
        json_schema_extra={"x-grounded-experimental": True},
    )


class RpcProtocolReport(StrictModel):
    schema_: str = Field(default="grounded.rpc_protocol.v2", alias="schema")
    status: str = "ok"
    jsonrpc: Literal["2.0"] = "2.0"
    endpoint: str = "/rpc"
    current_version: Literal["v2"] = "v2"
    supported_versions: list[ProtocolVersion] = Field(default_factory=lambda: ["v1", "v2"])
    capabilities: dict[str, Any] = Field(default_factory=dict)
    compatibility_rules: list[CompatibilityRule] = Field(default_factory=list)
    request_envelope_model: str = "RpcRequestEnvelopeV2"
    response_envelope_model: str = "RpcResponseEnvelopeV2"
    notification_envelope_model: str = "RpcNotificationEnvelopeV2"
    methods: list[RpcMethodSpecV2] = Field(default_factory=list)


class EmptyParams(StrictModel):
    pass


class InitializeParams(StrictModel):
    client_info: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("client_info", "clientInfo"))


class InitializeResult(StrictModel):
    server: dict[str, Any]
    capabilities: dict[str, Any]
    protocol: dict[str, Any]


class ThreadListParams(StrictModel):
    workspace_id: str | None = Field(default=None, validation_alias=AliasChoices("workspace_id", "workspaceId"))
    include_archived: bool = Field(default=False, validation_alias=AliasChoices("include_archived", "includeArchived"))
    limit: int = 50
    cursor: str | None = None


class ThreadListResult(StrictModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None


class ThreadStartParams(StrictModel):
    workspace_id: str = Field(validation_alias=AliasChoices("workspace_id", "workspaceId"))
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadIdParams(StrictModel):
    thread_id: str = Field(validation_alias=AliasChoices("thread_id", "threadId"))


class SessionIdParams(StrictModel):
    session_id: str = Field(validation_alias=AliasChoices("session_id", "sessionId", "thread_id", "threadId"))


class DoctorParams(StrictModel):
    scope: Literal["quick", "full"] = "quick"
    workspace_id: str | None = Field(default=None, validation_alias=AliasChoices("workspace_id", "workspaceId"))
    run_id: str | None = Field(default=None, validation_alias=AliasChoices("run_id", "runId"))


class DoctorWorkspaceParams(StrictModel):
    workspace_id: str = Field(validation_alias=AliasChoices("workspace_id", "workspaceId"))
    scope: Literal["quick", "full"] = "quick"
    run_id: str | None = Field(default=None, validation_alias=AliasChoices("run_id", "runId"))


class ThreadForkParams(ThreadIdParams):
    title: str | None = None


class TurnStartParams(StrictModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, use_enum_values=True)

    thread_id: str = Field(validation_alias=AliasChoices("thread_id", "threadId"))
    prompt: str = ""
    mode: str = "generate"
    edit_mode: str = Field(default="default", validation_alias=AliasChoices("edit_mode", "editMode"))
    generation_mode: str = Field(default="balanced", validation_alias=AliasChoices("generation_mode", "generationMode"))
    intent: str = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str | None = Field(default=None, validation_alias=AliasChoices("workspace_id", "workspaceId"))


class TurnInterruptParams(ThreadIdParams):
    turn_id: str = Field(validation_alias=AliasChoices("turn_id", "turnId"))


class TurnIdParams(StrictModel):
    turn_id: str = Field(validation_alias=AliasChoices("turn_id", "turnId"))


class RunReplayParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    after_sequence: int = Field(default=0, validation_alias=AliasChoices("after_sequence", "afterSequence"))
    limit: int = 500


class ImproveRunParams(StrictModel):
    workspace_id: str = Field(validation_alias=AliasChoices("workspace_id", "workspaceId"))
    prompt: str
    run_id: str | None = Field(default=None, validation_alias=AliasChoices("run_id", "runId"))
    resume_from_run_id: str | None = Field(default=None, validation_alias=AliasChoices("resume_from_run_id", "resumeFromRunId"))
    target_role_scope: list[str] = Field(default_factory=list, validation_alias=AliasChoices("target_role_scope", "targetRoleScope"))
    model_profile: str | None = Field(default=None, validation_alias=AliasChoices("model_profile", "modelProfile"))
    generation_mode: str | None = Field(default=None, validation_alias=AliasChoices("generation_mode", "generationMode"))


class DraftApplyParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    files: list[str] = Field(default_factory=list)
    apply_token: str | None = Field(default=None, validation_alias=AliasChoices("apply_token", "applyToken"))


class DraftVariantParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    variant_run_id: str | None = Field(default=None, validation_alias=AliasChoices("variant_run_id", "variantRunId"))


class GuardianGateParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    semantic_override: str | None = Field(default=None, validation_alias=AliasChoices("semantic_override", "semanticOverride"))


class BrowserReplayScenarioParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    scenario_id: str = Field(validation_alias=AliasChoices("scenario_id", "scenarioId"))


class WorkerSessionParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    worker_session_id: str = Field(validation_alias=AliasChoices("worker_session_id", "workerSessionId"))


class WorkerSessionMessageParams(WorkerSessionParams):
    kind: str = "manual"
    from_worker: str = Field(default="coordinator", validation_alias=AliasChoices("from_worker", "fromWorker", "from"))
    to_worker: str | None = Field(default=None, validation_alias=AliasChoices("to_worker", "toWorker", "to"))
    payload: dict[str, Any] = Field(default_factory=dict)


class RunCompareParams(StrictModel):
    base_run_id: str = Field(validation_alias=AliasChoices("base_run_id", "baseRunId"))
    target_run_id: str = Field(validation_alias=AliasChoices("target_run_id", "targetRunId"))


class RunBookmarkParams(StrictModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"))
    bookmark_id: str = Field(validation_alias=AliasChoices("bookmark_id", "bookmarkId"))
    prompt: str | None = None


class SlashCommandExecuteParams(StrictModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, use_enum_values=True)

    command_id: str = Field(validation_alias=AliasChoices("command_id", "commandId", "id"))


class CommandExecParams(StrictModel):
    workspace_id: str = Field(validation_alias=AliasChoices("workspace_id", "workspaceId"))
    command: str
    thread_id: str | None = Field(default=None, validation_alias=AliasChoices("thread_id", "threadId"))
    turn_id: str | None = Field(default=None, validation_alias=AliasChoices("turn_id", "turnId"))
    timeout: int = 30
    managed: bool = False
    approval_id: str | None = Field(default=None, validation_alias=AliasChoices("approval_id", "approvalId"))
    preset: str = "safe_auto"
    idempotency_key: str | None = Field(default=None, validation_alias=AliasChoices("idempotency_key", "idempotencyKey"))


class FsReadFileParams(StrictModel):
    workspace_id: str = Field(validation_alias=AliasChoices("workspace_id", "workspaceId"))
    path: str
    run_id: str | None = Field(default=None, validation_alias=AliasChoices("run_id", "runId"))


class FsWriteFileParams(FsReadFileParams):
    content: str


class ProtocolRunState(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.run_state.v2", alias="schema")
    run_id: str
    workspace_id: str
    status: str
    apply_status: str | None = None
    draft_status: str | None = None
    current_stage: str | None = None
    progress_percent: int = 0
    turn_id: str | None = None
    thread_id: str | None = None
    generation_mode: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None


class ProtocolTurnState(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.turn_state.v2", alias="schema")
    turn_id: str
    thread_id: str
    workspace_id: str
    status: str
    kind: str = "agent"
    linked_run_id: str | None = None
    parent_turn_id: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None


class ProtocolToolCallState(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.tool_call_state.v2", alias="schema")
    tool_call_id: str
    run_id: str
    workspace_id: str
    tool: str
    canonical_tool: str
    status: str
    risk: str = "unknown"
    approval_id: str | None = None
    sandbox_profile: str | None = None
    input_schema_id: str | None = None
    output_ref: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    failure_signature: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None


class ProtocolApprovalState(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.approval_state.v2", alias="schema")
    approval_id: str
    workspace_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    subject: Literal["tool_call", "command", "file_change", "apply", "permission"] = "tool_call"
    status: Literal["pending", "approved", "rejected", "expired", "cancelled"] = "pending"
    risk: str = "unknown"
    requested_action: str
    reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    resolved_at: str | None = None


class ProtocolEventState(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.event_state.v2", alias="schema")
    event_id: str
    sequence: int
    event_type: str
    workspace_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    actor: str = "system"
    summary: str = ""
    payload_ref: str | None = None
    payload_sha256: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProtocolWorkerUpdate(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.worker_update.v2", alias="schema")
    worker_id: str
    run_id: str
    workspace_id: str
    status: Literal["planned", "running", "completed", "failed", "blocked", "merged", "rejected"] = "planned"
    phase: str | None = None
    owner_scope: str | None = None
    path_prefixes: list[str] = Field(default_factory=list)
    branch_run_id: str | None = None
    branch_policy: str | None = None
    write_scope: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    proof_refs: list[dict[str, Any]] = Field(default_factory=list)
    merge_decision: dict[str, Any] = Field(default_factory=dict)
    conflict_report: dict[str, Any] = Field(default_factory=dict)
    post_merge_verifier: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ProtocolEnvelopeV1(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.envelope.v1", alias="schema")
    protocol_version: Literal["v1"] = "v1"
    message_id: str
    sequence: int
    subject: ProtocolSubject
    type: str
    status: ProtocolStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProtocolEnvelopeV2(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.envelope.v2", alias="schema")
    protocol_version: Literal["v2"] = "v2"
    message_id: str
    sequence: int
    subject: ProtocolSubject
    type: str
    status: ProtocolStatus
    actor: str = "system"
    workspace_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    tool_call_id: str | None = None
    approval_id: str | None = None
    worker_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_ref: str | None = None
    payload_sha256: str | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    previous_message_id: str | None = None
    compatibility: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProtocolVersionSpec(StrictModel):
    version: ProtocolVersion
    envelope_model: str
    status: Literal["current", "supported", "deprecated"] = "supported"
    compatibility: str


class ProtocolSubjectSpec(StrictModel):
    subject: ProtocolSubject
    purpose: str
    v1_payload_model: str
    v2_payload_model: str
    source_models: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    legacy_endpoints: list[str] = Field(default_factory=list)


class ProtocolSchemaFixture(StrictModel):
    name: str
    model_name: str
    version: ProtocolVersion | Literal["manifest"]
    schema_id: str
    path: str
    json_schema: dict[str, Any] | None = None


class ProtocolSchemaCatalog(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.schema_catalog.v1", alias="schema")
    status: str = "ok"
    json_schema_draft: str = "https://json-schema.org/draft/2020-12/schema"
    fixture_root: str = "platform/backend/app/schemas/app_protocol"
    generated_types_path: str = "platform/frontend/src/lib/generated/openapi-types.ts"
    fixtures: list[ProtocolSchemaFixture] = Field(default_factory=list)


class AppProtocolManifest(StrictModel):
    schema_: str = Field(default="grounded.app_protocol.manifest.v1", alias="schema")
    status: str = "ok"
    current_version: Literal["v2"] = "v2"
    supported_versions: list[ProtocolVersion] = Field(default_factory=lambda: ["v1", "v2"])
    compatibility_policy: str = "v2 is additive over v1; v1 envelope fields remain stable and are never renamed."
    endpoint_refs: dict[str, str] = Field(default_factory=dict)
    versions: list[ProtocolVersionSpec] = Field(default_factory=list)
    subjects: list[ProtocolSubjectSpec] = Field(default_factory=list)
    envelope_models: list[str] = Field(default_factory=list)
    payload_models: list[str] = Field(default_factory=list)
    rpc_protocol_model: str = "RpcProtocolReport"
    rpc_method_models: list[str] = Field(default_factory=list)
    compatibility_rules: list[CompatibilityRule] = Field(default_factory=list)
    fixture_root: str = "platform/backend/app/schemas/app_protocol"
    generated_types_path: str = "platform/frontend/src/lib/generated/openapi-types.ts"
