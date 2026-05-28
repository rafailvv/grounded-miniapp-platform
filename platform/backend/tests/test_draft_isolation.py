from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord


def _workspace(client: TestClient) -> dict:
    return client.post(
        "/workspaces",
        json={
            "name": "Draft Isolation Workspace",
            "description": "draft isolation protocol",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()


def _run(app, workspace_id: str) -> RunRecord:
    run = RunRecord(workspace_id=workspace_id, prompt="Update README", intent="edit")
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    return run


def test_draft_manifest_creation_is_idempotent(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])

    app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    first = app.state.container.draft_isolation_service.ensure_manifest(workspace_id=workspace["workspace_id"], run_id=run.run_id)
    second = app.state.container.draft_isolation_service.ensure_manifest(workspace_id=workspace["workspace_id"], run_id=run.run_id)

    assert first.isolation_id == second.isolation_id
    assert first.source_ref == f"workspace_source:{workspace['workspace_id']}"
    assert second.schema_ == "grounded.draft_isolation.v1"


def test_draft_diff_hash_changes_after_mutation(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)

    before = app.state.container.draft_isolation_service.ensure_manifest(workspace_id=workspace["workspace_id"], run_id=run.run_id)
    (draft / "README.md").write_text("changed by draft isolation\n", encoding="utf-8")
    after = app.state.container.draft_isolation_service.ensure_manifest(workspace_id=workspace["workspace_id"], run_id=run.run_id)

    assert before.diff_sha256 != after.diff_sha256
    assert "README.md" in after.changed_files


def test_apply_is_blocked_without_fresh_passing_gate(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("first gated draft\n", encoding="utf-8")

    decision = app.state.container.draft_isolation_service.validate_apply_gate(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        selected_files=["README.md"],
    )

    assert decision.decision == "blocked"
    assert decision.blocked_reasons[0]["kind"] == "missing_gate"


def test_apply_is_blocked_when_diff_changes_after_gate(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("first gated draft\n", encoding="utf-8")
    gate = app.state.container.draft_isolation_service.create_gate(workspace_id=workspace["workspace_id"], run_id=run.run_id)

    (draft / "README.md").write_text("second gated draft\n", encoding="utf-8")
    decision = app.state.container.draft_isolation_service.validate_apply_gate(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        apply_token=gate.apply_token,
        selected_files=["README.md"],
    )

    assert decision.decision == "blocked"
    assert any(item["kind"] == "stale_gate" for item in decision.blocked_reasons)


def test_staged_apply_blocks_files_outside_gated_diff(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("first gated draft\n", encoding="utf-8")
    gate = app.state.container.draft_isolation_service.create_gate(workspace_id=workspace["workspace_id"], run_id=run.run_id)

    decision = app.state.container.draft_isolation_service.validate_apply_gate(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        apply_token=gate.apply_token,
        selected_files=["README.md", "missing.txt"],
    )

    assert decision.decision == "blocked"
    assert any(item["kind"] == "outside_gated_diff" for item in decision.blocked_reasons)


def test_variant_draft_clone_preserves_parent_manifest_ref(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("variant source draft\n", encoding="utf-8")

    report = app.state.container.draft_isolation_service.create_variant(
        workspace_id=workspace["workspace_id"],
        source_run_id=run.run_id,
        variant_run_id=f"{run.run_id}_alt",
    )

    assert report.parent_isolation_ref == f"draft_isolation:{workspace['workspace_id']}:{run.run_id}"
    assert (app.state.container.workspace_service.draft_source_dir(workspace["workspace_id"], report.variant_run_id) / "README.md").read_text(encoding="utf-8") == "variant source draft\n"


def test_draft_isolation_api_and_legacy_staged_apply(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = _run(app, workspace["workspace_id"])
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    (draft / "README.md").write_text("api draft apply\n", encoding="utf-8")

    isolation = client.get(f"/runs/{run.run_id}/draft-isolation").json()
    gate = client.post(f"/runs/{run.run_id}/draft-gate").json()
    client.post(f"/runs/{run.run_id}/stage/files", json={"files": ["README.md"]})
    applied = client.post(f"/runs/{run.run_id}/apply/staged").json()
    protocol = client.get(f"/runs/{run.run_id}/protocol-v2").json()

    assert isolation["schema"] == "grounded.draft_isolation.v1"
    assert gate["status"] == "passed"
    assert applied["apply_status"] == "applied"
    assert applied["draft_isolation_ref"] == f"draft_isolation:{workspace['workspace_id']}:{run.run_id}"
    assert (app.state.container.workspace_service.source_dir(workspace["workspace_id"]) / "README.md").read_text(encoding="utf-8") == "api draft apply\n"
    assert any(item.get("event_type") == "draft.apply.completed" for item in protocol["timeline"])
