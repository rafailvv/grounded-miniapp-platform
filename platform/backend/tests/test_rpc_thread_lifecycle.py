from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.threads import ItemRecord, ThreadRecord, TurnRecord
from app.repositories.platform_db import PlatformDb


def test_platform_db_migrates_and_paginates_threads(tmp_path: Path) -> None:
    db = PlatformDb(tmp_path / "platform.db")
    first = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="First"))
    second = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Second"))

    page_one, cursor = db.list_threads(workspace_id="ws_1", limit=1)
    page_two, next_cursor = db.list_threads(workspace_id="ws_1", limit=1, cursor=cursor)

    assert [thread.thread_id for thread in page_one] == [first.thread_id]
    assert [thread.thread_id for thread in page_two] == [second.thread_id]
    assert next_cursor is None


def test_platform_db_appends_items_in_sequence(tmp_path: Path) -> None:
    db = PlatformDb(tmp_path / "platform.db")
    thread = db.upsert_thread(ThreadRecord(workspace_id="ws_1", title="Thread"))
    turn = db.insert_turn(TurnRecord(thread_id=thread.thread_id, workspace_id="ws_1", prompt="Build"))

    first = db.append_item(ItemRecord(thread_id=thread.thread_id, turn_id=turn.turn_id, item_type="user.message"))
    second = db.append_item(ItemRecord(thread_id=thread.thread_id, turn_id=turn.turn_id, item_type="item.tool.progress"))

    assert first.sequence == 1
    assert second.sequence == 2
    assert [item.item_type for item in db.list_items(thread.thread_id)] == ["user.message", "item.tool.progress"]


def test_rpc_requires_initialize_and_serves_thread_lifecycle(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "RPC Workspace",
            "description": "RPC lifecycle test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    with client.websocket_connect("/rpc") as ws:
        ws.send_json({"id": 1, "method": "thread/list", "params": {}})
        assert _receive_response(ws, 1)["error"]["message"] == "Not initialized."

        ws.send_json({"id": 2, "method": "initialize", "params": {"clientInfo": {"name": "pytest"}}})
        assert _receive_response(ws, 2)["result"]["capabilities"]["threads"] is True

        ws.send_json(
            {
                "id": 3,
                "method": "thread/start",
                "params": {"workspace_id": workspace["workspace_id"], "title": "Thread A"},
            }
        )
        thread = _receive_response(ws, 3)["result"]
        assert thread["workspace_id"] == workspace["workspace_id"]

        ws.send_json({"id": 4, "method": "thread/read", "params": {"thread_id": thread["thread_id"]}})
        snapshot = _receive_response(ws, 4)["result"]
        assert snapshot["thread"]["thread_id"] == thread["thread_id"]
        assert snapshot["events"][0]["event_type"] == "thread.started"


def test_rpc_validates_unknown_and_invalid_params(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    with client.websocket_connect("/rpc") as ws:
        ws.send_json({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pytest"}}})
        assert _receive_response(ws, 1)["result"]["protocol"]["schema"] == "grounded.rpc_protocol.v2"

        ws.send_json({"id": 2, "method": "missing/method", "params": {}})
        missing = _receive_response(ws, 2)
        assert missing["error"]["code"] == -32601

        ws.send_json({"id": 3, "method": "thread/start", "params": {"title": "Missing workspace"}})
        invalid = _receive_response(ws, 3)
        assert invalid["error"]["code"] == -32602


def test_rpc_accepts_legacy_param_aliases_paginates_and_dedupes_idempotency(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "RPC Compatibility",
            "description": "RPC alias and idempotency test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    with client.websocket_connect("/rpc") as ws:
        ws.send_json({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pytest"}}})
        _receive_response(ws, 1)

        create = {
            "method": "thread/start",
            "params": {"workspaceId": workspace["workspace_id"], "title": "Thread Alias"},
            "idempotency_key": "thread-start-1",
        }
        ws.send_json({"id": 2, **create})
        first = _receive_response(ws, 2)["result"]
        ws.send_json({"id": 3, **create})
        duplicate = _receive_response(ws, 3)["result"]
        assert duplicate["thread_id"] == first["thread_id"]

        ws.send_json({"id": 4, "method": "thread/start", "params": {"workspace_id": workspace["workspace_id"], "title": "Thread B"}})
        _receive_response(ws, 4)

        ws.send_json({"id": 5, "method": "thread/list", "params": {"workspaceId": workspace["workspace_id"], "limit": 1}})
        page_one = _receive_response(ws, 5)["result"]
        assert len(page_one["items"]) == 1
        assert page_one["next_cursor"]

        ws.send_json({"id": 6, "method": "thread/list", "params": {"workspace_id": workspace["workspace_id"], "limit": 1, "cursor": page_one["next_cursor"]}})
        page_two = _receive_response(ws, 6)["result"]
        assert len(page_two["items"]) == 1
        assert page_two["items"][0]["thread_id"] != page_one["items"][0]["thread_id"]


def _receive_response(ws, request_id: int) -> dict:
    for _ in range(10):
        message = ws.receive_json()
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"response {request_id} not received")
