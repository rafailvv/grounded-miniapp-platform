from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.models.protocol import RpcErrorObject, RpcResponseEnvelopeV2
from app.models.threads import ThreadSnapshot
from app.services.container import ServiceContainer
from app.services.rpc_protocol import JSON_RPC_VERSION, RPC_PARAM_MODELS, rpc_protocol_manifest

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
    idempotency_cache: dict[tuple[str, str], Any] = {}

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
            idempotency_key = str(raw.get("idempotency_key") or raw.get("idempotencyKey") or params.get("idempotency_key") or params.get("idempotencyKey") or "").strip() or None
            try:
                if method != "initialize" and not initialized:
                    raise RpcError(-32000, "Not initialized.")
                params = _validate_params(method, params)
                cache_key = (method, idempotency_key) if idempotency_key else None
                if cache_key is not None and cache_key in idempotency_cache:
                    await websocket.send_json(_response(request_id, result=idempotency_cache[cache_key], idempotency_key=idempotency_key))
                    continue
                if method == "initialize":
                    initialized = True
                    result = {
                        "server": {"name": "grounded-miniapp-platform", "version": "0.1.0"},
                        "capabilities": {
                            "threads": True,
                            "turns": True,
                            "items": True,
                            "jsonRpcWebSocket": True,
                            "typedProtocol": True,
                            "eventReplay": True,
                            "runCompare": True,
                            "runBookmarks": True,
                            "fs": True,
                            "commandExec": True,
                        },
                        "protocol": rpc_protocol_manifest(),
                    }
                else:
                    result = await _dispatch(container, method, params)
                if cache_key is not None:
                    idempotency_cache[cache_key] = result
                await websocket.send_json(_response(request_id, result=result, idempotency_key=idempotency_key))
            except RpcError as exc:
                await websocket.send_json(_response(request_id, error=RpcErrorObject(code=exc.code, message=exc.message, data=exc.data), idempotency_key=idempotency_key))
            except (KeyError, ValueError, ValidationError) as exc:
                await websocket.send_json(_response(request_id, error=RpcErrorObject(code=-32602, message=str(exc)), idempotency_key=idempotency_key))
            except Exception as exc:
                await websocket.send_json(_response(request_id, error=RpcErrorObject(code=-32603, message=str(exc)), idempotency_key=idempotency_key))
    except WebSocketDisconnect:
        pass
    finally:
        notification_task.cancel()
        container.rpc_event_hub.unsubscribe(subscription)


async def _dispatch(container: ServiceContainer, method: str, params: dict[str, Any]) -> Any:
    service = container.thread_service
    if method == "rpc/protocol":
        return rpc_protocol_manifest()
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
        return container.workbench_service.skills()
    if method == "slash_commands/list":
        return container.workbench_service.slash_commands()
    if method == "slash_commands/execute":
        return container.workbench_service.execute_slash_command(
            str(params.get("command_id") or params.get("commandId") or params.get("id") or ""),
            params,
        )
    if method == "run/replay":
        return container.workbench_service.event_replay(
            str(params.get("run_id") or params.get("runId") or ""),
            after_sequence=int(params.get("after_sequence") or params.get("afterSequence") or 0),
            limit=int(params.get("limit") or 500),
        )
    if method == "run/compare":
        return container.workbench_service.compare_runs(
            str(params.get("base_run_id") or params.get("baseRunId") or ""),
            str(params.get("target_run_id") or params.get("targetRunId") or ""),
        )
    if method == "run/resume_from_bookmark":
        return container.workbench_service.resume_from_bookmark(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("bookmark_id") or params.get("bookmarkId") or ""),
            prompt=params.get("prompt"),
            fork=False,
        )
    if method == "run/fork_from_bookmark":
        return container.workbench_service.resume_from_bookmark(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("bookmark_id") or params.get("bookmarkId") or ""),
            prompt=params.get("prompt"),
            fork=True,
        )
    if method == "plugin/list":
        return container.workbench_service.plugins()
    raise RpcError(-32601, f"Unknown method: {method}")


def _validate_params(method: str, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("JSON-RPC params must be an object.")
    model = RPC_PARAM_MODELS.get(method)
    if model is None:
        if method in {"thread/snapshot", "thread/snapshot/list", "thread/archive", "thread/rollback", "turn/steer", "turn/compact/start", "review/start", "fs/readDirectory", "fs/watch", "fs/unwatch", "command/exec/write", "command/exec/resize", "command/exec/terminate", "command/exec/read", "model/list"}:
            return params
        raise RpcError(-32601, f"Unknown method: {method}")
    return model.model_validate(params).model_dump(mode="json", by_alias=False)


def _response(
    request_id: Any,
    *,
    result: Any | None = None,
    error: RpcErrorObject | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return RpcResponseEnvelopeV2(
        jsonrpc=JSON_RPC_VERSION,
        id=request_id,
        result=result,
        error=error,
        idempotency_key=idempotency_key,
    ).model_dump(mode="json", by_alias=True, exclude_none=True)


def _snapshot_payload(snapshot: ThreadSnapshot) -> dict[str, Any]:
    return {
        "thread": snapshot.thread.model_dump(mode="json"),
        "turns": [turn.model_dump(mode="json") for turn in snapshot.turns],
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "events": [event.model_dump(mode="json") for event in snapshot.events],
    }
