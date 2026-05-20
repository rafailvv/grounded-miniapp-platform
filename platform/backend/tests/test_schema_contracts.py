from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.openapi_export import export_openapi


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
        ("/runs/{run_id}/events-v2", "get"): "EventJournalPage",
        ("/runs/{run_id}/journal/state", "get"): "RunJournalState",
        ("/threads/{thread_id}/events-v2", "get"): "EventJournalPage",
        ("/threads/{thread_id}/journal/state", "get"): "ThreadJournalState",
        ("/event-payloads/{payload_ref}", "get"): "EventJournalPayload",
        ("/runs/{run_id}/context-pressure", "get"): "ContextPressureReport",
        ("/runs/{run_id}/output-artifacts", "get"): "OutputArtifactIndex",
        ("/runs/{run_id}/prompt-suggestions", "get"): "PromptSuggestionsReport",
        ("/workspaces/{workspace_id}/memory/retrieve", "post"): "MemoryRetrievalResult",
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
    assert manifest["generated_types_path"] == "platform/frontend/src/lib/generated/openapi-types.ts"
    for model_name in {
        "ToolEnvelope",
        "RunEventsReport",
        "GateReport",
        "RepairCase",
        "TraceState",
        "ThreadSnapshot",
        "EventJournalPage",
        "RunJournalState",
        "ThreadJournalState",
        "ContextPressureReport",
        "PromptSuggestionsReport",
    }:
        assert model_name in manifest_names
        assert model_name in component_names


def test_legacy_doctor_shape_remains_additive(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    doctor = TestClient(app).get("/doctor").json()

    assert set(doctor) >= {"status", "checks", "created_at"}
    assert isinstance(doctor["checks"], list)
    for check in doctor["checks"]:
        assert set(check) >= {"name", "status", "details", "required"}
    assert {"python", "node", "npm", "template_hash", "disk_space", "exec_policy"}.issubset({item["name"] for item in doctor["checks"]})
