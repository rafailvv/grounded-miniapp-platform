from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def test_generation_pipeline_smoke(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Smoke Workspace",
            "description": "End-to-end smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    document = client.post(
        f"/workspaces/{workspace_id}/documents",
        json={
            "file_name": "requirements.md",
            "file_path": "docs/requirements.md",
            "source_type": "project_doc",
            "content": "Booking form with name, phone, date and comment fields.",
        },
    ).json()
    index_response = client.post(f"/documents/{document['document_id']}/index")
    assert index_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Create a consultation booking mini-app with name phone date and comment fields.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "basic",
        },
    )
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "completed"

    spec_payload = client.get(f"/workspaces/{workspace_id}/spec/current").json()
    ir_payload = client.get(f"/workspaces/{workspace_id}/ir/current").json()
    validation_payload = client.get(f"/workspaces/{workspace_id}/validation/current").json()
    preview_payload = client.get(f"/workspaces/{workspace_id}/preview/url").json()
    file_tree = client.get(f"/workspaces/{workspace_id}/files/tree").json()

    assert spec_payload["product_goal"].startswith("Create a consultation booking")
    assert ir_payload["entry_screen_id"] == "client_home"
    assert len(ir_payload["screens"]) >= 10
    assert len(ir_payload["route_groups"]) == 3
    assert validation_payload["blocking"] is False
    assert preview_payload["url"] is None or preview_payload["url"].startswith("http://localhost:")
    if preview_payload["url"] is None:
        assert preview_payload["role_urls"] == {}
    else:
        assert preview_payload["role_urls"]["client"].startswith(preview_payload["url"])
    assert any(item["path"] == "backend/app/generated/runtime_manifest.json" for item in file_tree)
    assert any(item["path"] == "artifacts/grounded_spec.json" for item in file_tree)

    preview_response = client.get(f"/preview/{workspace_id}?role=manager")
    assert preview_response.status_code == 200
    assert "booking" in preview_response.text.lower()

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "docs/manual-note.md",
            "content": "# Manual edit\n\nThis file was added in the smoke test.\n",
        },
    )
    assert save_response.status_code == 200

    diff_response = client.get(f"/workspaces/{workspace_id}/diff")
    assert diff_response.status_code == 200
    assert "manual-note.md" in diff_response.json()["diff"]

    zip_export = client.post(f"/workspaces/{workspace_id}/export/zip").json()
    patch_export = client.post(f"/workspaces/{workspace_id}/export/git-patch").json()
    assert Path(zip_export["file_path"]).exists()
    assert Path(patch_export["file_path"]).exists()

    events_response = client.get(f"/jobs/{job['job_id']}/events")
    assert events_response.status_code == 200
    assert "job_completed" in events_response.text


def test_quality_mode_blocks_without_openrouter(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Quality Workspace",
            "description": "Quality mode should block without OpenRouter",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Build a high-quality role-aware consultation workflow mini-app.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "quality",
        },
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "blocked"
    assert payload["failure_reason"]


def test_run_api_exposes_artifacts_and_links_job(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Run Workspace",
            "description": "Run API smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Refine the existing mini-app with a stronger AI workspace framing and preserved role previews.",
            "intent": "auto",
            "apply_strategy": "staged_auto_apply",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
            "generation_mode": "basic",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["linked_job_id"]
    assert run_payload["status"] == "completed"
    assert run_payload["apply_status"] == "applied"

    list_response = client.get(f"/workspaces/{workspace_id}/runs")
    assert list_response.status_code == 200
    listed_runs = list_response.json()
    assert listed_runs[0]["run_id"] == run_payload["run_id"]

    artifact_response = client.get(f"/runs/{run_payload['run_id']}/artifacts")
    assert artifact_response.status_code == 200
    artifacts = artifact_response.json()
    assert artifacts["run"]["run_id"] == run_payload["run_id"]
    assert artifacts["job"]["job_id"] == run_payload["linked_job_id"]
    assert artifacts["code_change_plan"]["summary"]
    assert isinstance(artifacts["code_change_plan"]["targets"], list)
    assert "validation" in artifacts
    assert "trace" in artifacts
