from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.common import PreviewProfile, TargetPlatform
from app.models.domain import CodeChangePlan, JobRecord, RunRecord


def test_draft_diff_normalizes_source_and_draft_paths_without_git_noise(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Draft Diff Workspace",
            "description": "Exercise draft diff normalization.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    workspace_service = app.state.container.workspace_service
    workspace_service.clone_template(workspace_id)

    run_id = "run_diff_normalization"
    draft_source = workspace_service.prepare_draft(workspace_id, run_id)
    target = draft_source / "README.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nvisual-patch-smoke\n", encoding="utf-8")

    diff_text = workspace_service.diff(workspace_id, run_id=run_id)

    assert "diff --git a/source/README.md b/draft/README.md" in diff_text
    assert "asource/" not in diff_text
    assert "bdraft/" not in diff_text
    assert ".git/" not in diff_text


def test_meaningful_paths_fall_back_to_completed_job_apply_result(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service

    run = RunRecord(
        workspace_id="ws_apply_result_fallback",
        prompt="Remove a stray visual indicator.",
        intent="edit",
    )
    plan = CodeChangePlan(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        intent="edit",
        summary="No agent targets were emitted for this patch.",
    )
    job = JobRecord(
        workspace_id=run.workspace_id,
        prompt=run.prompt,
        status="completed",
        mode="generate",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        apply_result={"changed_files": ["miniapp/app/static/manager/styles.css"]},
    )

    assert run_service._meaningful_paths_for_run(
        workspace_id=run.workspace_id,
        run=run,
        change_plan=plan,
        job=job,
    ) == ["miniapp/app/static/manager/styles.css"]
