from __future__ import annotations

from pathlib import Path

from app.repositories.platform_db import PlatformDb
from app.services.event_journal import EventJournalService


def test_run_journal_reducer_reconstructs_quality_surface(tmp_path: Path) -> None:
    journal = EventJournalService(PlatformDb(tmp_path / "platform.db"))

    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="run.created", payload={"status": "pending", "stage": "created"})
    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="run.status_changed", payload={"status": "running", "stage": "implementation"})
    journal.append_run(
        workspace_id="ws_1",
        run_id="run_1",
        event_type="tool.completed",
        payload={"tool": "shell.exec", "result": {"semantic_status": "passed"}},
        actor="tool:shell.exec",
    )
    journal.append_run(
        workspace_id="ws_1",
        run_id="run_1",
        event_type="check.result",
        payload={"name": "browser_flow_smoke", "status": "failed", "blocking": True},
    )
    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="apply.staged", payload={"status": "staged", "paths": ["miniapp/app/main.py"]})
    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="repair.case_opened", payload={"case_id": "case_1", "status": "open"})
    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="protocol.bookmark", payload={"type": "bookmark", "bookmark_id": "bm_1"})
    journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="run.completed", payload={"status": "completed", "stage": "done"})

    state = journal.reduce_run("run_1")

    assert state.status == "available"
    assert state.workspace_id == "ws_1"
    assert state.event_count == 8
    assert state.latest_status == "completed"
    assert state.latest_stage == "done"
    assert state.blocking is True
    assert state.replay_cursor == 8
    assert state.tool_events[0]["tool"] == "shell.exec"
    assert state.checks[0]["check"] == "browser_flow_smoke"
    assert state.apply_events[0]["payload"]["paths"] == ["miniapp/app/main.py"]
    assert state.repair_events[0]["case_id"] == "case_1"
    assert state.protocol_refs[0]["bookmark_id"] == "bm_1"


def test_thread_journal_reducer_reconstructs_turns_items_and_linked_runs(tmp_path: Path) -> None:
    journal = EventJournalService(PlatformDb(tmp_path / "platform.db"))

    journal.append_thread(workspace_id="ws_1", thread_id="thread_1", event_type="thread.started", payload={"title": "Build"}, actor="user")
    journal.append_thread(workspace_id="ws_1", thread_id="thread_1", event_type="turn.started", turn_id="turn_1", payload={"run_id": "run_1"})
    journal.append_thread(workspace_id="ws_1", thread_id="thread_1", event_type="user.message", turn_id="turn_1", payload={"text": "Add export"})
    journal.append_thread(workspace_id="ws_1", thread_id="thread_1", event_type="turn.finished", turn_id="turn_1", run_id="run_1", payload={"status": "completed"})

    page = journal.list_thread("thread_1", after_sequence=1, limit=2)
    state = journal.reduce_thread("thread_1")

    assert [event.sequence for event in page] == [2, 3]
    assert state.status == "available"
    assert state.workspace_id == "ws_1"
    assert state.event_count == 4
    assert [turn["status"] for turn in state.turns] == ["started", "finished"]
    assert state.items[0]["payload"]["text"] == "Add export"
    assert state.linked_runs[0]["run_id"] == "run_1"
    assert state.replay_cursor == 4
