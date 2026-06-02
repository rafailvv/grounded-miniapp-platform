from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord


def _workspace(client: TestClient) -> dict:
    return client.post(
        "/workspaces",
        json={
            "name": "Guardian Gate Workspace",
            "description": "guardian gate protocol",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()


def _run(app, workspace_id: str, *, prompt: str = "Update product surface", intent: str = "edit") -> RunRecord:
    run = RunRecord(workspace_id=workspace_id, prompt=prompt, intent=intent)
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    return run


def test_guardian_gate_blocks_deterministic_mock_data_even_if_semantic_allows(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"], prompt="Create production client UI", intent="create")
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    target = draft / "miniapp" / "app" / "static" / "client" / "app.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("const mockData = [{ name: 'John Doe' }];\n", encoding="utf-8")

    report = app.state.container.guardian_gate_service.run_gate(run=run, semantic_override="allow")

    assert report.status == "blocked"
    assert report.apply_decision == "block"
    assert any(item.code.startswith("guardian.seeded_mock_data.") for item in report.findings)


def test_guardian_gate_uncertain_semantic_verdict_blocks_apply(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("uncertain guardian change\n", encoding="utf-8")

    report = app.state.container.guardian_gate_service.run_gate(run=run, semantic_override="uncertain")

    assert report.status == "blocked"
    assert report.semantic_verdict == "uncertain"
    assert any(item.code == "guardian.semantic_uncertain" for item in report.findings)


def test_guardian_gate_prompt_contract_mismatch_creates_repair_packet(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"], prompt="Add backend API endpoint for requests")
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("not an API implementation\n", encoding="utf-8")

    report = app.state.container.guardian_gate_service.run_gate(run=run)

    assert report.status == "blocked"
    assert any(item.code == "guardian.semantic_contract_mismatch" for item in report.findings)
    assert any(packet["failure_class"] == "guardian.semantic_gate_blocked" for packet in report.repair_packets)


def test_guardian_gate_api_and_rpc_return_typed_payload(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("guardian api change\n", encoding="utf-8")

    api_report = client.post(f"/runs/{run.run_id}/guardian-gate").json()
    rpc_methods = client.get("/system/rpc-protocol").json()["methods"]
    gate = client.get(f"/runs/{run.run_id}/gate").json()

    assert api_report["schema"] == "grounded.guardian_gate.v1"
    assert {"guardian/gate", "guardian/review"}.issubset({item["method"] for item in rpc_methods})
    assert gate["guardian_gate_ref"] == api_report["guardian_gate_ref"]
    assert gate["guardian_gate"]["semantic_verdict"] in {"allow", "block", "uncertain"}


def test_staged_apply_blocks_on_semantic_guardian_before_source_mutation(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"], prompt="Add backend API endpoint for requests")
    source_readme = app.state.container.workspace_service.source_dir(workspace["workspace_id"]) / "README.md"
    original = source_readme.read_text(encoding="utf-8")
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("not an API implementation\n", encoding="utf-8")

    client.post(f"/runs/{run.run_id}/stage/files", json={"files": ["README.md"]})
    applied = client.post(f"/runs/{run.run_id}/apply/staged").json()
    protocol = client.get(f"/runs/{run.run_id}/protocol-v2").json()

    assert applied["apply_status"] == "blocked"
    assert source_readme.read_text(encoding="utf-8") == original
    assert any(item.get("event_type") == "guardian.apply.blocked" for item in protocol["timeline"])
