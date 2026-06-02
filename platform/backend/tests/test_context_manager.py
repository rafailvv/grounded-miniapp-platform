from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.context_manager import ContextManagerReport
from app.models.domain import RunRecord
from app.repositories.state_store import StateStore
from app.services.context_manager import ContextManagerService
from app.services.engine.context_budget_manager import ContextBudgetManager


def test_context_manager_empty_context_produces_manifest(tmp_path: Path) -> None:
    service = ContextManagerService(StateStore(tmp_path / "state.json"))

    result = service.prepare_turn_context(
        workspace_id="ws_1",
        run_id="run_1",
        prompt="Build the app",
        prompt_payload={},
    )

    report = ContextManagerReport.model_validate(result["report"])
    assert report.schema_ == "grounded.context_manager.v1"
    assert report.manifest.status == "ready"
    assert report.manifest.target_prompt_tokens > 0
    assert report.next_sequence == 2


def test_context_manager_summarizes_large_tool_results_and_stale_refs(tmp_path: Path) -> None:
    service = ContextManagerService(StateStore(tmp_path / "state.json"), budget_manager=ContextBudgetManager())

    result = service.prepare_turn_context(
        workspace_id="ws_1",
        run_id="run_1",
        prompt="Fix failure",
        prompt_payload={
            "task": "Fix failure",
            "tool_results": [
                {
                    "tool": "run_command",
                    "output": "x" * 80_000,
                    "microcompact_ref": "microcompact:ws_1:run_1:abc",
                }
            ],
        },
        context_pressure={
            "latest": {
                "compact_recommended": True,
                "stale_path_refs": [
                    {
                        "path": "miniapp/app/missing.py",
                        "source": "transcript.read_files",
                        "reason": "missing_in_workspace",
                        "suggested_path": "miniapp/app/main.py",
                    }
                ],
            }
        },
        transcript_snapshot={"normalization": {"status": "missing_tool_results", "missing_tool_result_ids": ["call_1"]}},
    )

    report = ContextManagerReport.model_validate(result["report"])
    actions = {decision.action for decision in report.decisions}
    assert "microcompact" in actions
    assert "refresh" in actions
    assert "defer" in actions
    assert report.stale_refs[0].suggested_path == "miniapp/app/main.py"
    assert "context_manifest" in result["payload"]


def test_context_manager_endpoint_and_manual_compact_are_compatible(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Context Manager Workspace",
            "description": "Context manager endpoint test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        implementation_plan={"primary_entities": ["workflow"]},
        acceptance_contract={"roles": ["client", "specialist", "manager"]},
    )
    run.context_pressure_ref = f"context_pressure:{workspace['workspace_id']}:{run.run_id}"
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        run.context_pressure_ref,
        {
            "schema": "grounded.context_pressure.v2",
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "status": "ready",
            "latest": {"compact_recommended": True, "total_tokens_estimate": 120000},
            "items": [{"compact_recommended": True, "total_tokens_estimate": 120000}],
        },
    )

    compact = client.post(f"/runs/{run.run_id}/context-manager/compact").json()
    report = client.get(f"/runs/{run.run_id}/context-manager").json()

    assert compact["schema"] == "grounded.context_manager.v1"
    assert report["schema"] == "grounded.context_manager.v1"
    assert report["manifest"]["metadata"]["compact_recommended"] is True
