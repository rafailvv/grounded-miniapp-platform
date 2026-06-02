from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.openapi_export import export_openapi
from app.models.platform_config import PlatformConfig
from app.models.protocol import ProtocolEnvelopeV1, ProtocolEnvelopeV2
from app.services.app_protocol import APP_PROTOCOL_SCHEMA_ROOT, app_protocol_json_schemas, app_protocol_manifest
from app.services.platform_config import default_platform_config_path, platform_config, platform_config_schema
from app.services.rpc_protocol import RPC_PARAM_MODELS, rpc_protocol_manifest


def _response_schema_refs(openapi: dict[str, Any], path: str, method: str) -> set[str]:
    schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
    refs: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                refs.add(ref.rsplit("/", 1)[-1])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(schema)
    return refs


def test_openapi_keeps_typed_workbench_response_models(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    openapi = TestClient(app).get("/openapi.json").json()

    expected = {
        ("/system/app-protocol", "get"): "AppProtocolManifest",
        ("/system/app-protocol/schemas", "get"): "ProtocolSchemaCatalog",
        ("/runs/{run_id}/events-v2", "get"): "EventJournalPage",
        ("/runs/{run_id}/journal/state", "get"): "RunJournalState",
        ("/threads/{thread_id}/events-v2", "get"): "EventJournalPage",
        ("/threads/{thread_id}/journal/state", "get"): "ThreadJournalState",
        ("/event-payloads/{payload_ref}", "get"): "EventJournalPayload",
        ("/runs/{run_id}/context-manager", "get"): "ContextManagerReport",
        ("/runs/{run_id}/context-pressure", "get"): "ContextPressureReport",
        ("/runs/{run_id}/output-artifacts", "get"): "OutputArtifactIndex",
        ("/runs/{run_id}/prompt-suggestions", "get"): "PromptSuggestionsReport",
        ("/runs/{run_id}/completion-audit", "get"): "PromptCompletionAuditReport",
        ("/runs/{run_id}/visual-regression", "get"): "VisualRegressionReport",
        ("/system/generation-modes", "get"): "GenerationModeSlaManifest",
        ("/system/platform-config", "get"): "PlatformConfig",
        ("/workspaces/{workspace_id}/memory/retrieve", "post"): "MemoryRetrievalResult",
        ("/workspaces/{workspace_id}/memory/summary", "get"): "MemorySummaryReport",
        ("/system/schema", "get"): "SystemSchemaManifest",
    }

    for route, model_name in expected.items():
        assert model_name in _response_schema_refs(openapi, *route)


def test_schema_manifest_and_generated_types_contract_stay_in_sync(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    manifest = client.get("/system/schema").json()
    exported = export_openapi()
    component_names = set(exported["components"]["schemas"])
    manifest_names = {item["name"] for item in manifest["models"]}

    assert manifest["schema"] == "grounded.system_schema.v1"
    assert manifest["openapi_url"] == "/openapi.json"
    assert manifest["app_protocol_url"] == "/system/app-protocol"
    assert manifest["app_protocol_schema_url"] == "/system/app-protocol/schemas"
    assert manifest["app_protocol_fixture_root"] == "platform/backend/app/schemas/app_protocol"
    assert manifest["generated_types_path"] == "platform/frontend/src/lib/generated/openapi-types.ts"
    for model_name in {
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
        "RpcProtocolReport",
        "RpcMethodSpecV2",
        "RpcIdempotency",
        "RpcCursorPage",
        "ToolEnvelope",
        "RunEventsReport",
        "GateReport",
        "RepairCase",
        "TraceState",
        "ThreadSnapshot",
        "EventJournalPage",
        "RunJournalState",
        "ThreadJournalState",
        "ContextManagerReport",
        "ContextPressureReport",
            "PromptSuggestionsReport",
            "PromptCompletionAuditReport",
            "VisualRegressionReport",
            "GenerationModeSlaManifest",
            "PlatformConfig",
            "MemorySummaryReport",
        }:
        assert model_name in manifest_names
        assert model_name in component_names


def test_platform_config_schema_fixture_matches_generated_model_and_default_config() -> None:
    fixture = Path("platform/backend/app/schemas/platform.config.schema.json")
    default_config = default_platform_config_path()

    assert fixture.exists()
    assert default_config.exists()
    assert json.loads(fixture.read_text(encoding="utf-8")) == platform_config_schema()

    config = PlatformConfig.model_validate_json(default_config.read_text(encoding="utf-8"))
    assert config.schema_ == "grounded.platform_config.v1"
    assert {"fast", "balanced", "quality", "production", "basic"} <= set(config.generation_modes)
    assert {"api_workflow_smoke", "browser_flow_smoke"} <= set(config.checks)
    assert {"openai_code_fast", "research_balanced", "openai_code_quality"} <= set(config.model_profiles)
    assert "browser_flow_smoke" in config.generation_modes["fast"].required_checks
    assert "fast" in config.browser_proof.required_modes


def test_platform_config_path_override_loads_without_python_changes(tmp_path: Path, monkeypatch) -> None:
    payload = json.loads(default_platform_config_path().read_text(encoding="utf-8"))
    payload["generation_modes"]["balanced"]["label"] = "Balanced from override"
    override = tmp_path / "platform.config.json"
    override.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("PLATFORM_CONFIG_PATH", str(override))
    loaded = platform_config(reload=True)

    assert loaded.generation_modes["balanced"].label == "Balanced from override"


def test_platform_config_endpoint_exposes_product_contract(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    config = client.get("/system/platform-config").json()
    manifest = client.get("/system/platform-config/manifest").json()
    schema = client.get("/system/platform-config/schema").json()

    assert config["schema"] == "grounded.platform_config.v1"
    assert config["generation_modes"]["balanced"]["audit_level"] == "standard"
    assert config["checks"]["api_workflow_smoke"]["proof_kind"] == "persistence"
    assert config["default_profile_by_mode"]["quality"] == "openai_code_quality"
    assert manifest["schema"] == "grounded.platform_config_manifest.v1"
    assert "generation_modes" in manifest
    assert schema["$id"] == "https://grounded.local/schemas/platform.config.schema.json"
    assert schema["properties"]["browser_proof"]["$ref"] == "#/$defs/BrowserProofConfig"


def test_app_protocol_manifest_covers_core_subjects_and_legacy_endpoints(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    protocol = TestClient(app).get("/system/app-protocol").json()

    assert protocol["schema"] == "grounded.app_protocol.manifest.v1"
    assert protocol["current_version"] == "v2"
    assert protocol["supported_versions"] == ["v1", "v2"]
    assert protocol["endpoint_refs"]["openapi"] == "/openapi.json"

    subjects = {item["subject"]: item for item in protocol["subjects"]}
    assert set(subjects) == {"workbench", "run", "turn", "tool_call", "approval", "event", "worker_update", "browser_proof"}
    assert subjects["workbench"]["v2_payload_model"] == "ProtocolWorkbenchState"
    assert subjects["browser_proof"]["v2_payload_model"] == "ProtocolBrowserProofState"
    assert "/runs/{run_id}/browser-proof" in subjects["browser_proof"]["legacy_endpoints"]
    assert "/runs/{run_id}/protocol" in subjects["run"]["legacy_endpoints"]
    assert "/threads/{thread_id}" in subjects["turn"]["legacy_endpoints"]
    assert "/runs/{run_id}/tool-events" in subjects["tool_call"]["legacy_endpoints"]
    assert "/runs/{run_id}/approvals" in subjects["approval"]["legacy_endpoints"]
    assert "/runs/{run_id}/events-v2" in subjects["event"]["legacy_endpoints"]
    assert "/runs/{run_id}/workers" in subjects["worker_update"]["legacy_endpoints"]
    assert {
        "WorkbenchEventResponse",
        "RunEventsResponse",
        "WorkerUpdateResponse",
        "BrowserProofResponse",
        "WorkbenchNotification",
        "RunNotification",
        "WorkerUpdateNotification",
        "BrowserProofNotification",
    }.issubset(set(protocol["rpc_method_models"]))


def test_app_protocol_schema_fixtures_match_generated_models() -> None:
    generated = app_protocol_json_schemas()
    assert generated

    for name, schema in generated.items():
        fixture_name = ""
        for char_index, char in enumerate(name):
            if char.isupper() and char_index > 0:
                fixture_name += "_"
            fixture_name += char.lower()
        fixture = APP_PROTOCOL_SCHEMA_ROOT / f"{fixture_name}.schema.json"
        assert fixture.exists(), f"Missing protocol fixture for {name}"
        assert json.loads(fixture.read_text(encoding="utf-8")) == schema


def test_protocol_v2_envelope_is_additive_over_v1() -> None:
    v1_fields = {field.alias or name for name, field in ProtocolEnvelopeV1.model_fields.items()}
    v2_fields = {field.alias or name for name, field in ProtocolEnvelopeV2.model_fields.items()}
    assert v1_fields <= v2_fields

    manifest = app_protocol_manifest().model_dump(mode="json", by_alias=True)
    version_specs = {item["version"]: item for item in manifest["versions"]}
    assert version_specs["v1"]["status"] == "supported"
    assert version_specs["v2"]["status"] == "current"
    assert "additive" in manifest["compatibility_policy"]


def test_typed_rpc_protocol_declares_models_idempotency_and_cursors(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    protocol = TestClient(app).get("/system/rpc-protocol").json()

    assert protocol["schema"] == "grounded.rpc_protocol.v2"
    assert protocol["request_envelope_model"] == "RpcRequestEnvelopeV2"
    assert protocol["response_envelope_model"] == "RpcResponseEnvelopeV2"
    assert protocol["notification_envelope_model"] == "RpcNotificationEnvelopeV2"

    methods = {item["method"]: item for item in protocol["methods"]}
    assert set(RPC_PARAM_MODELS) <= set(methods)
    assert methods["thread/list"]["cursor"]["cursor_kind"] == "opaque_cursor"
    assert methods["run/replay"]["cursor"]["cursor_kind"] == "sequence_cursor"
    assert methods["run/events"]["result_model"] == "RunEventsResponse"
    assert methods["worker/updates"]["result_model"] == "WorkerUpdateResponse"
    assert methods["browser/proof"]["result_model"] == "BrowserProofResponse"
    assert methods["workbench/events"]["result_model"] == "WorkbenchEventResponse"
    assert methods["thread/list"]["idempotent"] is True
    assert methods["thread/start"]["idempotency"]["mode"] == "recommended"
    assert methods["command/exec"]["stability"] == "experimental"
    assert methods["command/exec"]["experimental"][0]["name"] == "preset"

    compatibility_rules = {item["rule_id"] for item in protocol["compatibility_rules"]}
    assert {"rpc.v2.additive", "rpc.params.aliases", "rpc.breaking_changes"} <= compatibility_rules


def test_app_protocol_links_typed_rpc_catalog() -> None:
    manifest = app_protocol_manifest().model_dump(mode="json", by_alias=True)
    rpc = rpc_protocol_manifest()

    assert manifest["endpoint_refs"]["rpc_protocol"] == "/system/rpc-protocol"
    assert manifest["endpoint_refs"]["sandbox_runtime"] == "/system/sandbox-runtime"
    assert manifest["rpc_protocol_model"] == "RpcProtocolReport"
    assert "RpcRequestEnvelopeV2" in manifest["rpc_method_models"]
    assert "SandboxRuntimeBoundary" in manifest["payload_models"]
    assert {item["rule_id"] for item in manifest["compatibility_rules"]}
    assert rpc["schema"] == "grounded.rpc_protocol.v2"


def test_legacy_doctor_shape_remains_additive(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    doctor = TestClient(app).get("/doctor").json()

    assert set(doctor) >= {"status", "checks", "created_at"}
    assert isinstance(doctor["checks"], list)
    for check in doctor["checks"]:
        assert set(check) >= {"name", "status", "details", "required"}
    assert {"python", "node", "npm", "template_hash", "disk_space", "exec_policy"}.issubset({item["name"] for item in doctor["checks"]})
