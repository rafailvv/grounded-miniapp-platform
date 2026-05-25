from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.protocol import (
    AppProtocolManifest,
    InitializeResult,
    ProtocolApprovalState,
    ProtocolEnvelopeV1,
    ProtocolEnvelopeV2,
    ProtocolEventState,
    ProtocolRunState,
    ProtocolSchemaCatalog,
    ProtocolSchemaFixture,
    ProtocolSubjectSpec,
    ProtocolToolCallState,
    ProtocolTurnState,
    ProtocolVersionSpec,
    ProtocolWorkerUpdate,
    RpcCursorPage,
    RpcErrorObject,
    RpcIdempotency,
    RpcMethodSpecV2,
    RpcNotificationEnvelopeV2,
    RpcProtocolReport,
    RpcRequestEnvelopeV2,
    RpcResponseEnvelopeV2,
    ThreadListResult,
)
from app.models.sandbox import (
    SandboxEnvironmentSnapshot,
    SandboxFilesystemAllowlist,
    SandboxKillDiagnostics,
    SandboxLogCapture,
    SandboxPreviewLifecycle,
    SandboxRuntimeBoundary,
    SandboxRuntimeManifest,
)
from app.services.rpc_protocol import RPC_PARAM_MODELS


APP_PROTOCOL_SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas" / "app_protocol"
APP_PROTOCOL_FIXTURE_ROOT = "platform/backend/app/schemas/app_protocol"

APP_PROTOCOL_MODELS: dict[str, type] = {
    "AppProtocolManifest": AppProtocolManifest,
    "ProtocolSchemaCatalog": ProtocolSchemaCatalog,
    "ProtocolEnvelopeV1": ProtocolEnvelopeV1,
    "ProtocolEnvelopeV2": ProtocolEnvelopeV2,
    "ProtocolRunState": ProtocolRunState,
    "ProtocolTurnState": ProtocolTurnState,
    "ProtocolToolCallState": ProtocolToolCallState,
    "ProtocolApprovalState": ProtocolApprovalState,
    "ProtocolEventState": ProtocolEventState,
    "ProtocolWorkerUpdate": ProtocolWorkerUpdate,
    "RpcErrorObject": RpcErrorObject,
    "RpcRequestEnvelopeV2": RpcRequestEnvelopeV2,
    "RpcResponseEnvelopeV2": RpcResponseEnvelopeV2,
    "RpcNotificationEnvelopeV2": RpcNotificationEnvelopeV2,
    "RpcProtocolReport": RpcProtocolReport,
    "RpcMethodSpecV2": RpcMethodSpecV2,
    "RpcCursorPage": RpcCursorPage,
    "RpcIdempotency": RpcIdempotency,
    "InitializeResult": InitializeResult,
    "ThreadListResult": ThreadListResult,
    "SandboxRuntimeManifest": SandboxRuntimeManifest,
    "SandboxRuntimeBoundary": SandboxRuntimeBoundary,
    "SandboxFilesystemAllowlist": SandboxFilesystemAllowlist,
    "SandboxEnvironmentSnapshot": SandboxEnvironmentSnapshot,
    "SandboxLogCapture": SandboxLogCapture,
    "SandboxKillDiagnostics": SandboxKillDiagnostics,
    "SandboxPreviewLifecycle": SandboxPreviewLifecycle,
    **{model.__name__: model for model in RPC_PARAM_MODELS.values()},
}

APP_PROTOCOL_FIXTURE_NAMES: tuple[str, ...] = (
    "AppProtocolManifest",
    "ProtocolSchemaCatalog",
    "ProtocolEnvelopeV1",
    "ProtocolEnvelopeV2",
    "ProtocolRunState",
    "ProtocolTurnState",
    "ProtocolToolCallState",
    "ProtocolApprovalState",
    "ProtocolEventState",
    "ProtocolWorkerUpdate",
    "RpcErrorObject",
    "RpcRequestEnvelopeV2",
    "RpcResponseEnvelopeV2",
    "RpcNotificationEnvelopeV2",
    "RpcProtocolReport",
    "RpcMethodSpecV2",
    "RpcCursorPage",
    "RpcIdempotency",
    "InitializeResult",
    "ThreadListResult",
    "SandboxRuntimeManifest",
    "SandboxRuntimeBoundary",
    "SandboxFilesystemAllowlist",
    "SandboxEnvironmentSnapshot",
    "SandboxLogCapture",
    "SandboxKillDiagnostics",
    "SandboxPreviewLifecycle",
    *tuple(dict.fromkeys(model.__name__ for model in RPC_PARAM_MODELS.values())),
)


def app_protocol_manifest() -> AppProtocolManifest:
    return AppProtocolManifest(
        endpoint_refs={
            "manifest": "/system/app-protocol",
            "schema_catalog": "/system/app-protocol/schemas",
            "openapi": "/openapi.json",
            "rpc_protocol": "/system/rpc-protocol",
            "sandbox_runtime": "/system/sandbox-runtime",
            "preview_runtime_boundary": "/workspaces/{workspace_id}/preview/runtime-boundary",
            "system_schema": "/system/schema",
            "run_protocol": "/runs/{run_id}/protocol",
            "run_events_v2": "/runs/{run_id}/events-v2",
            "thread_snapshot": "/threads/{thread_id}",
            "tool_events": "/runs/{run_id}/tool-events",
            "workers": "/runs/{run_id}/workers",
        },
        versions=[
            ProtocolVersionSpec(
                version="v1",
                envelope_model="ProtocolEnvelopeV1",
                status="supported",
                compatibility="Frozen compatibility envelope for current Workbench clients.",
            ),
            ProtocolVersionSpec(
                version="v2",
                envelope_model="ProtocolEnvelopeV2",
                status="current",
                compatibility="Adds actor, ids, payload refs, idempotency, and worker/approval correlation without removing v1 fields.",
            ),
        ],
        subjects=[
            ProtocolSubjectSpec(
                subject="run",
                purpose="Generation lifecycle, resume/fork lineage, apply state, checks, and artifact refs.",
                v1_payload_model="RunRecord",
                v2_payload_model="ProtocolRunState",
                source_models=["RunRecord", "RunProtocolReport", "RunJournalState", "RunEventReplayReport"],
                event_types=["run.started", "run.stage_changed", "run.completed", "run.failed", "run.blocked"],
                legacy_endpoints=["/workspaces/{workspace_id}/runs", "/runs/{run_id}", "/runs/{run_id}/protocol"],
            ),
            ProtocolSubjectSpec(
                subject="turn",
                purpose="Resumable user/agent turns inside Workbench threads.",
                v1_payload_model="TurnRecord",
                v2_payload_model="ProtocolTurnState",
                source_models=["ThreadSnapshot", "TurnRecord", "ThreadJournalState"],
                event_types=["turn.started", "turn.interrupted", "turn.completed", "turn.failed"],
                legacy_endpoints=["/threads/{thread_id}/turns", "/threads/{thread_id}", "/threads/{thread_id}/events-v2"],
            ),
            ProtocolSubjectSpec(
                subject="tool_call",
                purpose="Canonical model-facing tool request/result envelopes with risk, sandbox, artifacts, and repair metadata.",
                v1_payload_model="ToolEnvelope",
                v2_payload_model="ProtocolToolCallState",
                source_models=["ToolEnvelope", "ToolEventsReport", "SandboxRuntimeBoundary", "SandboxEnvironmentSnapshot", "SandboxLogCapture", "SandboxKillDiagnostics"],
                event_types=["tool.requested", "tool.progress", "tool.completed", "tool.failed"],
                legacy_endpoints=["/runs/{run_id}/tool-events", "/system/tools/dynamic", "/system/policies/exec"],
            ),
            ProtocolSubjectSpec(
                subject="approval",
                purpose="Human or policy approval requests for commands, file changes, tool calls, and apply gates.",
                v1_payload_model="dict",
                v2_payload_model="ProtocolApprovalState",
                source_models=["RunRecord", "ToolEnvelope"],
                event_types=["approval.requested", "approval.approved", "approval.rejected", "approval.expired"],
                legacy_endpoints=["/runs/{run_id}/approvals", "/runs/{run_id}/approvals/{approval_id}/approve", "/runs/{run_id}/approvals/{approval_id}/reject"],
            ),
            ProtocolSubjectSpec(
                subject="event",
                purpose="Append-only run/thread event journal records with payload refs and replay cursors.",
                v1_payload_model="RunEvent",
                v2_payload_model="ProtocolEventState",
                source_models=["RunEvent", "RunEventV2", "ThreadEventV2", "EventJournalPage"],
                event_types=["event.appended", "event.payload_written", "event.replayed"],
                legacy_endpoints=["/runs/{run_id}/events", "/runs/{run_id}/events-v2", "/event-payloads/{payload_ref}"],
            ),
            ProtocolSubjectSpec(
                subject="worker_update",
                purpose="Worker branch lifecycle, ownership, scoped diffs, proof refs, and merge decisions.",
                v1_payload_model="dict",
                v2_payload_model="ProtocolWorkerUpdate",
                source_models=["RunRecord", "BackgroundTaskRecord"],
                event_types=["worker.planned", "worker.started", "worker.completed", "worker.failed", "worker.merged"],
                legacy_endpoints=[
                    "/runs/{run_id}/workers",
                    "/runs/{run_id}/workers/orchestration",
                    "/runs/{run_id}/workers/{worker_id}/diff",
                    "/runs/{run_id}/workers/merge-decision",
                ],
            ),
        ],
        envelope_models=["ProtocolEnvelopeV1", "ProtocolEnvelopeV2"],
        payload_models=[
            "ProtocolRunState",
            "ProtocolTurnState",
            "ProtocolToolCallState",
            "ProtocolApprovalState",
            "ProtocolEventState",
            "ProtocolWorkerUpdate",
            "SandboxRuntimeBoundary",
            "SandboxEnvironmentSnapshot",
            "SandboxLogCapture",
            "SandboxKillDiagnostics",
            "SandboxPreviewLifecycle",
        ],
        rpc_protocol_model="RpcProtocolReport",
        rpc_method_models=list(dict.fromkeys(["RpcRequestEnvelopeV2", "RpcResponseEnvelopeV2", "RpcNotificationEnvelopeV2", *[model.__name__ for model in RPC_PARAM_MODELS.values()]])),
        compatibility_rules=[
            {
                "rule_id": "app_protocol.v2.additive",
                "description": "v2 is additive over v1; existing fields remain stable and readable.",
                "since": "v2",
                "enforcement": "tested",
            },
            {
                "rule_id": "app_protocol.v3_for_breaking_changes",
                "description": "Breaking payload, envelope, or RPC method changes require a future v3 protocol.",
                "since": "v2",
                "enforcement": "documented",
            },
        ],
    )


def app_protocol_json_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for name, model in APP_PROTOCOL_MODELS.items():
        schema = model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
        schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
        schema.setdefault("$id", f"https://grounded.local/schemas/app_protocol/{_fixture_file_name(name)}")
        schemas[name] = schema
    return schemas


def app_protocol_schema_catalog(*, include_json_schema: bool = False) -> ProtocolSchemaCatalog:
    generated = app_protocol_json_schemas()
    fixtures = []
    for name in APP_PROTOCOL_FIXTURE_NAMES:
        schema = generated[name]
        fixtures.append(
            ProtocolSchemaFixture(
                name=_fixture_file_name(name),
                model_name=name,
                version=_fixture_version(name),
                schema_id=str(schema.get("$id") or schema.get("title") or name),
                path=f"{APP_PROTOCOL_FIXTURE_ROOT}/{_fixture_file_name(name)}",
                json_schema=schema if include_json_schema else None,
            )
        )
    return ProtocolSchemaCatalog(fixtures=fixtures)


def write_app_protocol_schema_fixtures(root: Path | None = None) -> list[Path]:
    target_root = root or APP_PROTOCOL_SCHEMA_ROOT
    target_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in app_protocol_json_schemas().items():
        path = target_root / _fixture_file_name(name)
        path.write_text(f"{json.dumps(schema, indent=2, sort_keys=True)}\n", encoding="utf-8")
        written.append(path)
    return written


def _fixture_file_name(model_name: str) -> str:
    chars: list[str] = []
    for index, char in enumerate(model_name):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    return f"{''.join(chars)}.schema.json"


def _fixture_version(model_name: str) -> str:
    if model_name == "AppProtocolManifest" or model_name == "ProtocolSchemaCatalog":
        return "manifest"
    if model_name.endswith("V1"):
        return "v1"
    return "v2"
