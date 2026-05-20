from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


ProtocolVersion = Literal["v1", "v2"]
ProtocolSubject = Literal["run", "turn", "tool_call", "approval", "event", "worker_update"]
ProtocolStatus = Literal["queued", "started", "running", "waiting", "completed", "failed", "blocked", "cancelled", "skipped"]


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
    fixture_root: str = "platform/backend/app/schemas/app_protocol"
    generated_types_path: str = "platform/frontend/src/lib/generated/openapi-types.ts"
