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
    assert ir_payload["entry_screen_id"] == "screen_form"
    assert validation_payload["blocking"] is False
    assert preview_payload["url"].endswith(f"/preview/{workspace_id}")
    assert preview_payload["role_urls"]["client"].endswith(f"/preview/{workspace_id}?role=client")
    assert any(item["path"] == "artifacts/grounded_spec.json" for item in file_tree)

    preview_response = client.get(f"/preview/{workspace_id}?role=manager")
    assert preview_response.status_code == 200
    assert "Manager role" in preview_response.text

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
