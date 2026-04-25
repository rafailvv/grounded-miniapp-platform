from __future__ import annotations

from pathlib import Path

from app.main import create_app
from app.models.common import PreviewProfile, TargetPlatform
from app.models.domain import GenerateRequest, JobRecord


def test_container_exposes_single_code_agent_runtime(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    container = app.state.container

    assert hasattr(container, "workspace_code_agent_runtime")
    assert not hasattr(container, "generation_" + "service")
    assert not hasattr(container, "fix_" + "orchestrator")
    assert container.run_service.code_agent_runtime is container.workspace_code_agent_runtime


def test_latest_job_and_current_report_use_agent_store(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime
    store = app.state.container.store

    job = JobRecord(
        workspace_id="ws_agent_store",
        prompt="Build a product catalog",
        status="completed",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
    )
    store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
    store.upsert("reports", "trace:ws_agent_store", {"workspace_id": "ws_agent_store", "entries": [{"stage": "agent_turn"}]})

    assert runtime.get_job(job.job_id).job_id == job.job_id
    assert runtime.latest_job_for_workspace("ws_agent_store").job_id == job.job_id
    assert runtime.current_report("ws_agent_store", "trace") == {
        "workspace_id": "ws_agent_store",
        "entries": [{"stage": "agent_turn"}],
    }


def test_retry_from_job_runs_same_agent_runtime(tmp_path: Path, monkeypatch) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime
    store = app.state.container.store

    job = JobRecord(
        workspace_id="ws_retry",
        prompt="Build a calculator",
        status="failed",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
    )
    store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
    captured: dict[str, GenerateRequest] = {}

    def fake_generate(workspace_id: str, request: GenerateRequest, **_kwargs):
        captured["workspace_id"] = workspace_id
        captured["request"] = request
        return job.model_copy(update={"status": "completed"})

    monkeypatch.setattr(runtime, "generate", fake_generate)

    result = runtime.retry_from_job(job.job_id)

    assert result.status == "completed"
    assert captured["workspace_id"] == "ws_retry"
    assert captured["request"].prompt == "Build a calculator"
