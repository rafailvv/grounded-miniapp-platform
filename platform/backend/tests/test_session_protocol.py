from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.models.threads import ThreadRecord, TurnRecord
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.run_protocol import RunProtocolService
from app.services.session_protocol import SessionProtocolReducer


def _reducer(tmp_path: Path) -> tuple[SessionProtocolReducer, PlatformDb, StateStore, EventJournalService, RunProtocolService]:
    db = PlatformDb(tmp_path / "platform.db")
    store = StateStore(tmp_path / "state.json")
    journal = EventJournalService(db)
    run_protocol = RunProtocolService(db, store, event_journal_service=journal)
    reducer = SessionProtocolReducer(db=db, store=store, event_journal_service=journal, run_protocol_service=run_protocol)
    return reducer, db, store, journal, run_protocol


def test_session_protocol_reduces_empty_session(tmp_path: Path) -> None:
    reducer, db, _store, journal, _run_protocol = _reducer(tmp_path)
    thread = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Empty"))
    journal.append_thread(
        workspace_id="ws_1",
        thread_id=thread.thread_id,
        event_type="session.started",
        payload={"session_id": thread.thread_id},
        idempotency_key=f"session.started:{thread.thread_id}",
    )

    protocol = reducer.session_protocol(thread.thread_id)

    assert protocol["schema"] == "grounded.session_protocol.v1"
    assert protocol["session_id"] == thread.thread_id
    assert protocol["turns"] == []
    assert protocol["linked_runs"] == []
    assert protocol["timeline"][0]["event_type"] == "session.started"
    assert protocol["resume"]["status"] == "none"


def test_session_protocol_reduces_completed_turn_run_tool_proof_and_bookmark(tmp_path: Path) -> None:
    reducer, db, store, journal, run_protocol = _reducer(tmp_path)
    thread = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Build"))
    turn = db.insert_turn(TurnRecord(thread_id=thread.thread_id, workspace_id="ws_1", status="completed", prompt="Build app"))
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Build app",
        intent="create",
        session_id=thread.thread_id,
        status="completed",
        apply_status="applied",
        current_stage="completed",
        model_profile="test",
    )
    turn.linked_run_id = run.run_id
    db.insert_turn(turn)
    store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    store.upsert("reports", "resume_checkpoint:ok", {"status": "ready"})
    journal.append_thread(workspace_id="ws_1", thread_id=thread.thread_id, turn_id=turn.turn_id, run_id=run.run_id, event_type="turn.completed", payload={"status": "completed", "run_id": run.run_id})
    journal.append_run(workspace_id="ws_1", run_id=run.run_id, event_type="tool.requested", payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "tool_call_id": "tool_1", "tool": "shell.exec"})
    journal.append_run(workspace_id="ws_1", run_id=run.run_id, event_type="tool.completed", payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "tool_call_id": "tool_1", "tool": "shell.exec", "status": "completed"})
    journal.append_run(workspace_id="ws_1", run_id=run.run_id, event_type="proof.completed", payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "proof_ref": "latest_check_execution:run"})
    bookmark = run_protocol.create_bookmark(
        run_id=run.run_id,
        workspace_id="ws_1",
        turn_id=turn.turn_id,
        response_id="resp_1",
        checkpoint_ref="resume_checkpoint:ok",
        trace_bundle_ref="trace_bundle:ok",
        diff_sha256_value=None,
        latest_check_ref="latest_check_execution:run",
    )

    session = reducer.session_protocol(thread.thread_id)
    run_view = reducer.run_protocol(run.run_id)

    assert session["linked_runs"][0]["run_id"] == run.run_id
    assert {item["event_type"] for item in session["timeline"]} >= {"tool.requested", "tool.completed", "proof.completed"}
    assert session["latest_bookmark"]["bookmark_id"] == bookmark["bookmark_id"]
    assert session["resume"]["latest"]["kind"] == "bookmark"
    assert run_view["session_id"] == thread.thread_id
    assert run_view["bookmarks"][0]["turn_id"] == turn.turn_id


def test_session_protocol_reduces_failed_run_failure_point(tmp_path: Path) -> None:
    reducer, db, store, journal, _run_protocol = _reducer(tmp_path)
    thread = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Repair"))
    turn = db.insert_turn(TurnRecord(thread_id=thread.thread_id, workspace_id="ws_1", status="failed", prompt="Fix"))
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Fix",
        intent="edit",
        session_id=thread.thread_id,
        status="failed",
        current_stage="browser proof",
        failure_reason="Browser proof failed.",
        failure_class="browser_flow_smoke",
        failure_signature="missing_selector",
        model_profile="test",
    )
    turn.linked_run_id = run.run_id
    db.insert_turn(turn)
    store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    journal.append_run(workspace_id="ws_1", run_id=run.run_id, event_type="tool.failed", payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "tool_call_id": "tool_bad", "status": "failed"})
    journal.append_run(workspace_id="ws_1", run_id=run.run_id, event_type="proof.completed", payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "status": "failed", "proof_ref": "browser_proof:bad"})

    protocol = reducer.session_protocol(thread.thread_id)

    assert protocol["failure_point"]["run_id"] == run.run_id
    assert protocol["failure_point"]["failure_signature"] == "missing_selector"
    assert protocol["resume"]["latest"]["kind"] == "failed_run"
    assert any(item["status"] == "failed" for item in protocol["timeline"])


def test_session_protocol_reduces_interrupted_compacted_and_forked_threads(tmp_path: Path) -> None:
    reducer, db, _store, journal, _run_protocol = _reducer(tmp_path)
    source = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Source"))
    interrupted = db.insert_turn(TurnRecord(thread_id=source.thread_id, workspace_id="ws_1", status="interrupted", prompt="Stop"))
    compacted = db.insert_turn(TurnRecord(thread_id=source.thread_id, workspace_id="ws_1", kind="compaction", status="completed", prompt="Compact"))
    fork = db.upsert_thread(
        ThreadRecord(
            workspace_id="ws_1",
            title="Fork",
            forked_from_thread_id=source.thread_id,
            metadata={"fork": {"source_thread_id": source.thread_id}},
        )
    )
    journal.append_thread(workspace_id="ws_1", thread_id=source.thread_id, turn_id=interrupted.turn_id, event_type="turn.interrupted", payload={"status": "interrupted"})
    journal.append_thread(workspace_id="ws_1", thread_id=source.thread_id, turn_id=compacted.turn_id, event_type="turn.compacted", payload={"status": "completed"})
    journal.append_thread(workspace_id="ws_1", thread_id=fork.thread_id, event_type="session.started", payload={"session_id": fork.thread_id, "forked_from_thread_id": source.thread_id})

    source_protocol = reducer.session_protocol(source.thread_id)
    fork_protocol = reducer.session_protocol(fork.thread_id)

    assert {item["event_type"] for item in source_protocol["timeline"]} >= {"turn.interrupted", "turn.compacted"}
    assert any(turn["status"] == "interrupted" for turn in source_protocol["turns"])
    assert any(turn["kind"] == "compaction" for turn in source_protocol["turns"])
    assert fork_protocol["session"]["forked_from_thread_id"] == source.thread_id


def test_session_protocol_rest_and_rpc_endpoints_are_available(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "Protocol API", "target_platform": "telegram_mini_app", "preview_profile": "telegram_mock"},
    ).json()
    thread = app.state.container.thread_service.start_thread(workspace_id=workspace["workspace_id"], title="Protocol")
    turn = TurnRecord(thread_id=thread.thread_id, workspace_id=workspace["workspace_id"], status="completed", prompt="Build")
    app.state.container.platform_db.insert_turn(turn)
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build",
        intent="create",
        session_id=thread.thread_id,
        status="completed",
        apply_status="applied",
        model_profile="test",
    )
    turn.linked_run_id = run.run_id
    app.state.container.platform_db.insert_turn(turn)
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.event_journal_service.append_run(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        event_type="run.completed",
        payload={"session_id": thread.thread_id, "turn_id": turn.turn_id, "status": "completed"},
    )

    session_protocol = client.get(f"/sessions/{thread.thread_id}/protocol").json()
    turn_protocol = client.get(f"/turns/{turn.turn_id}/protocol").json()
    run_protocol = client.get(f"/runs/{run.run_id}/protocol-v2").json()
    legacy_thread = client.get(f"/threads/{thread.thread_id}").json()
    legacy_run = client.get(f"/runs/{run.run_id}").json()

    assert session_protocol["schema"] == "grounded.session_protocol.v1"
    assert turn_protocol["turn_id"] == turn.turn_id
    assert run_protocol["schema"] == "grounded.run_protocol.v2"
    assert legacy_thread["thread"]["thread_id"] == thread.thread_id
    assert legacy_run["run_id"] == run.run_id

    with client.websocket_connect("/rpc") as ws:
        ws.send_json({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pytest"}}})
        _receive_response(ws, 1)
        ws.send_json({"id": 2, "method": "session/protocol", "params": {"session_id": thread.thread_id}})
        assert _receive_response(ws, 2)["result"]["session_id"] == thread.thread_id
        ws.send_json({"id": 3, "method": "turn/protocol", "params": {"turn_id": turn.turn_id}})
        assert _receive_response(ws, 3)["result"]["turn_id"] == turn.turn_id
        ws.send_json({"id": 4, "method": "run/protocol", "params": {"run_id": run.run_id}})
        assert _receive_response(ws, 4)["result"]["run_id"] == run.run_id


def _receive_response(ws, request_id: int) -> dict:
    for _ in range(10):
        message = ws.receive_json()
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"response {request_id} not received")
