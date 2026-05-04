from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.models.threads import ThreadSnapshot
from app.services.container import ServiceContainer

router = APIRouter(tags=["rpc"])


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@router.websocket("/rpc")
async def rpc_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    container: ServiceContainer = websocket.app.state.container
    subscription = container.rpc_event_hub.subscribe()
    initialized = False

    async def send_notifications() -> None:
        while True:
            message = await subscription.queue.get()
            await websocket.send_json(message)

    notification_task = asyncio.create_task(send_notifications())
    try:
        while True:
            raw = await websocket.receive_json()
            request_id = raw.get("id")
            method = str(raw.get("method") or "")
            params = raw.get("params") or {}
            try:
                if method != "initialize" and not initialized:
                    raise RpcError(-32000, "Not initialized.")
                if method == "initialize":
                    initialized = True
                    result = {
                        "server": {"name": "grounded-miniapp-platform", "version": "0.1.0"},
                        "capabilities": {
                            "threads": True,
                            "turns": True,
                            "items": True,
                            "jsonRpcWebSocket": True,
                            "fs": True,
                            "commandExec": True,
                        },
                    }
                else:
                    result = await _dispatch(container, method, params)
                await websocket.send_json({"id": request_id, "result": result})
            except RpcError as exc:
                await websocket.send_json({"id": request_id, "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
            except (KeyError, ValueError) as exc:
                await websocket.send_json({"id": request_id, "error": {"code": -32602, "message": str(exc)}})
            except Exception as exc:
                await websocket.send_json({"id": request_id, "error": {"code": -32603, "message": str(exc)}})
    except WebSocketDisconnect:
        pass
    finally:
        notification_task.cancel()
        container.rpc_event_hub.unsubscribe(subscription)


async def _dispatch(container: ServiceContainer, method: str, params: dict[str, Any]) -> Any:
    service = container.thread_service
    if method == "thread/start":
        return service.start_thread(
            workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""),
            title=params.get("title"),
            metadata=params.get("metadata") or {},
        ).model_dump(mode="json")
    if method == "thread/list":
        return service.list_threads(
            workspace_id=params.get("workspace_id") or params.get("workspaceId"),
            include_archived=bool(params.get("include_archived") or params.get("includeArchived") or False),
            limit=int(params.get("limit") or 50),
            cursor=params.get("cursor"),
        )
    if method == "thread/read":
        snapshot = service.read_thread(str(params.get("thread_id") or params.get("threadId") or ""))
        return _snapshot_payload(snapshot)
    if method == "thread/snapshot":
        return service.create_snapshot(
            str(params.get("thread_id") or params.get("threadId") or ""),
            reason=str(params.get("reason") or "manual"),
            turn_id=params.get("turn_id") or params.get("turnId"),
        )
    if method == "thread/snapshot/list":
        return service.list_snapshots(
            str(params.get("thread_id") or params.get("threadId") or ""),
            limit=int(params.get("limit") or 50),
        )
    if method == "thread/resume":
        return service.resume_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "thread/fork":
        return service.fork_thread(str(params.get("thread_id") or params.get("threadId") or ""), title=params.get("title")).model_dump(mode="json")
    if method == "thread/archive":
        return service.archive_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "thread/rollback":
        return service.rollback_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "turn/start":
        return service.start_turn(str(params.get("thread_id") or params.get("threadId") or ""), params).model_dump(mode="json")
    if method == "turn/interrupt":
        return service.interrupt_turn(str(params.get("thread_id") or params.get("threadId") or ""), str(params.get("turn_id") or params.get("turnId") or "")).model_dump(mode="json")
    if method == "turn/steer":
        return service.steer_turn(
            str(params.get("thread_id") or params.get("threadId") or ""),
            str(params.get("turn_id") or params.get("turnId") or ""),
            str(params.get("message") or ""),
        ).model_dump(mode="json")
    if method == "turn/compact/start":
        return service.compact_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "review/start":
        return service.review_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "fs/readFile":
        return service.fs_read_file(workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""), path=str(params.get("path") or ""), run_id=params.get("run_id") or params.get("runId"))
    if method == "fs/writeFile":
        return service.fs_write_file(workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""), path=str(params.get("path") or ""), content=str(params.get("content") or ""), run_id=params.get("run_id") or params.get("runId"))
    if method == "fs/readDirectory":
        return service.fs_read_directory(workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""), path=str(params.get("path") or ""), run_id=params.get("run_id") or params.get("runId"))
    if method in {"fs/watch", "fs/unwatch"}:
        return {"status": "registered" if method == "fs/watch" else "removed"}
    if method == "command/exec":
        return service.exec_command(
            workspace_id=str(params.get("workspace_id") or params.get("workspaceId") or ""),
            command=str(params.get("command") or ""),
            thread_id=params.get("thread_id") or params.get("threadId"),
            turn_id=params.get("turn_id") or params.get("turnId"),
            timeout=int(params.get("timeout") or 30),
            approval_id=params.get("approval_id") or params.get("approvalId"),
            preset=str(params.get("preset") or "safe_auto"),
        )
    if method == "command/exec/write":
        return service.write_exec(
            str(params.get("process_id") or params.get("processId") or ""),
            str(params.get("data") or params.get("chars") or ""),
        )
    if method == "command/exec/resize":
        return service.resize_exec(
            str(params.get("process_id") or params.get("processId") or ""),
            cols=int(params.get("cols") or 80),
            rows=int(params.get("rows") or 24),
        )
    if method == "command/exec/terminate":
        return service.terminate_exec(str(params.get("process_id") or params.get("processId") or ""))
    if method == "command/exec/read":
        start = params.get("start")
        end = params.get("end")
        return service.read_exec_output(
            str(params.get("process_id") or params.get("processId") or ""),
            stream=str(params.get("stream") or "stdout"),
            start=int(start) if start is not None else None,
            end=int(end) if end is not None else None,
        )
    if method == "model/list":
        return container.openai_client.configuration()
    if method == "skills/list":
        return {"items": []}
    if method == "plugin/list":
        return {"items": []}
    raise RpcError(-32601, f"Unknown method: {method}")


def _snapshot_payload(snapshot: ThreadSnapshot) -> dict[str, Any]:
    return {
        "thread": snapshot.thread.model_dump(mode="json"),
        "turns": [turn.model_dump(mode="json") for turn in snapshot.turns],
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "events": [event.model_dump(mode="json") for event in snapshot.events],
    }
