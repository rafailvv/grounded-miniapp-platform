from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import GenerateRequest, JobRecord, ValidationSnapshot


def test_cold_workspace_create_clones_template_and_queues_preview(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    client = TestClient(app)

    response = client.post(
        "/workspaces",
        json={
            "name": "Cold Workspace",
            "description": "Preview bootstrap test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["template_cloned"] is True
    preview = {}
    for _ in range(20):
        preview = client.get(f"/workspaces/{workspace['workspace_id']}/preview/url").json()
        if preview["status"] in {"starting", "running"}:
            break
        time.sleep(0.05)
    assert preview["status"] in {"starting", "running"}


def test_generate_endpoint_uses_agent_runtime_and_auto_applies(tmp_path: Path, monkeypatch) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Agent Generate Workspace",
            "description": "Agent endpoint test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    runtime = app.state.container.workspace_code_agent_runtime
    captured_request: dict[str, GenerateRequest] = {}

    def fake_generate(workspace_id: str, request: GenerateRequest, **_kwargs) -> JobRecord:
        captured_request["request"] = request
        draft = app.state.container.workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_fake")
        readme = draft / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nAgent generated product catalog.\n", encoding="utf-8")
        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode=request.mode,
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=app.state.container.workspace_service.get_workspace(workspace_id).current_revision_id,
            llm_enabled=True,
            llm_model="test-agent-model",
            model_profile=request.model_profile,
            linked_run_id=request.linked_run_id,
            summary="Agent test run completed.",
            validation_snapshot=ValidationSnapshot(
                platform_valid=True,
                prompt_alignment_valid=True,
                checks_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )
        runtime._save_job(job)
        runtime._store_report(f"iterations:{workspace_id}", {"workspace_id": workspace_id, "items": [{"assistant_message": "patched"}]})
        runtime._store_report(f"check_results:{workspace_id}", {"workspace_id": workspace_id, "items": []})
        runtime._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": [{"stage": "agent_turn"}]})
        return job

    monkeypatch.setattr(runtime, "generate", fake_generate)

    response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Create an online store with product catalog and cart",
            "mode": "generate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "fast",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "completed"
    runs = client.get(f"/workspaces/{workspace_id}/runs").json()
    assert runs[0]["status"] == "completed"
    assert runs[0]["apply_status"] == "applied"
    assert captured_request["request"].target_role_scope == ["client", "specialist", "manager"]
    artifacts = client.get(f"/runs/{runs[0]['run_id']}/artifacts").json()
    forbidden_keys = {"role" + "_contract", "page" + "_graph", "entity" + "_contract", "mater" + "ialization" + "_report"}
    assert forbidden_keys.isdisjoint(artifacts.keys())
    assert "Agent generated product catalog" in app.state.container.workspace_service.read_file(workspace_id, "README.md")
