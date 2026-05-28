from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.common import GenerationMode
from app.models.domain import RunRecord
from app.modules.miniapp_agent_loop.agent_worker_tasks import AgentWorkerTaskPlanner
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.worker_sessions import WorkerSessionService


def _implementation_plan() -> dict[str, object]:
    return {
        "primary_entities": ["request"],
        "prompt_contract_v1": {"product": "service request"},
        "product_task_ledger": [
            {"id": "backend.request", "kind": "backend", "title": "Request API"},
            {"id": "client.request", "kind": "role_ui", "role": "client", "title": "Client request flow"},
            {"id": "proof.request", "kind": "proof", "title": "Request smoke"},
        ],
    }


def _worker_tasks() -> list[dict[str, object]]:
    return AgentWorkerTaskPlanner.worker_tasks(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan=_implementation_plan(),
    )


def test_worker_session_service_creates_sessions_mailbox_and_ownership(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    journal = EventJournalService(PlatformDb(tmp_path / "platform.db"))
    service = WorkerSessionService(store, event_journal_service=journal)

    report = service.create_sessions(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        worker_tasks=_worker_tasks(),
        mailbox={"enabled": True},
        implementation_plan=_implementation_plan(),
        acceptance_contract={"required": True},
    )

    assert report["schema"] == "grounded.worker_sessions.v1"
    assert report["mailbox"]["schema"] == "grounded.worker_mailbox.v2"
    assert report["ownership"]["schema"] == "grounded.worker_ownership.v1"
    assert any(item["worker_id"] == "backend_api_worker" and item["status"] == "ready" for item in report["items"])
    assert any(item["kind"] == "product_contract" and item["to"] == "backend_api_worker" for item in report["mailbox"]["items"])
    assert report["ownership"]["status"] == "passed"
    assert journal.list_run("run_1")


def test_worker_mailbox_append_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = WorkerSessionService(store)
    service.create_sessions(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        worker_tasks=_worker_tasks()[:1],
        mailbox={"enabled": True},
    )

    first = service.append_message(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        from_worker="planner",
        to_worker="backend_api_worker",
        kind="manual",
        payload={"note": "same"},
        message_id="wmsg_same",
    )
    second = service.append_message(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        from_worker="planner",
        to_worker="backend_api_worker",
        kind="manual",
        payload={"note": "same"},
        message_id="wmsg_same",
    )

    assert len(first["items"]) == len(second["items"])
    assert sum(1 for item in second["items"] if item["message_id"] == "wmsg_same") == 1


def test_worker_session_status_transition_rejects_terminal_regression(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = WorkerSessionService(store)
    service.create_sessions(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        worker_tasks=_worker_tasks()[:1],
        mailbox={"enabled": True},
    )

    service.update_status(workspace_id="ws_1", parent_run_id="run_1", artifact_run_id="run_1", worker_id="backend_api_worker", status="merged")
    with pytest.raises(ValueError):
        service.update_status(workspace_id="ws_1", parent_run_id="run_1", artifact_run_id="run_1", worker_id="backend_api_worker", status="running")


def test_worker_ownership_report_detects_conflicting_paths(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = WorkerSessionService(store)
    report = service.create_sessions(
        workspace_id="ws_1",
        parent_run_id="run_1",
        artifact_run_id="run_1",
        worker_tasks=[
            {
                "worker_id": "client_surface_worker",
                "branch_role": "writer",
                "ownership": {"allowed_paths": ["miniapp/app/static/client"], "forbidden_paths": [], "exclusive_write": True},
            },
            {
                "worker_id": "client_child_worker",
                "branch_role": "writer",
                "ownership": {"allowed_paths": ["miniapp/app/static/client/details"], "forbidden_paths": [], "exclusive_write": True},
            },
        ],
        mailbox={"enabled": True},
    )

    assert report["ownership"]["status"] == "conflict"
    assert report["ownership"]["merge_eligible"] is False
    assert report["ownership"]["conflicts"]


def test_worker_session_endpoints_extend_existing_workers_payload(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Worker Sessions Workspace",
            "description": "worker sessions",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a service request app",
        intent="create",
        generation_mode=GenerationMode.QUALITY,
        implementation_plan=_implementation_plan(),
        acceptance_contract={"required": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    report = app.state.container.worker_session_service.create_sessions(
        workspace_id=workspace["workspace_id"],
        parent_run_id=run.run_id,
        artifact_run_id=run.run_id,
        worker_tasks=_worker_tasks(),
        mailbox={"enabled": True},
        implementation_plan=_implementation_plan(),
        acceptance_contract={"required": True},
    )
    run.worker_sessions_ref = report["sessions_ref"]
    run.worker_ownership_ref = report["ownership_ref"]
    run.worker_mailbox_ref = f"worker_mailbox:{workspace['workspace_id']}:{run.run_id}"
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    session_id = report["items"][0]["worker_session_id"]

    workers = client.get(f"/runs/{run.run_id}/workers").json()
    sessions = client.get(f"/runs/{run.run_id}/worker-sessions").json()
    session = client.get(f"/runs/{run.run_id}/worker-sessions/{session_id}").json()
    mailbox = client.get(f"/runs/{run.run_id}/worker-mailbox").json()
    resume = client.post(f"/runs/{run.run_id}/worker-sessions/{session_id}/resume").json()

    assert workers["workers"][0]["worker_session_id"]
    assert workers["worker_sessions_ref"] == report["sessions_ref"]
    assert sessions["schema"] == "grounded.worker_sessions.v1"
    assert session["session"]["worker_session_id"] == session_id
    assert mailbox["schema"] == "grounded.worker_mailbox.v2"
    assert resume["schema"] == "grounded.worker_session_resume.v1"
