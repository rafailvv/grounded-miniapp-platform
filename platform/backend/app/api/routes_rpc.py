from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.models.protocol import (
    BrowserProofResponse,
    ProtocolBrowserProofState,
    ProtocolEventState,
    ProtocolWorkerUpdate,
    RpcErrorObject,
    RpcResponseEnvelopeV2,
    RunEventsResponse,
    WorkbenchEventResponse,
    WorkerUpdateResponse,
)
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
    if method == "workbench/events":
        return WorkbenchEventResponse(
            status="ok",
            items=[],
            next_sequence=int(params.get("after_sequence") or params.get("afterSequence") or 0),
            compatibility={"legacy": "Workbench-wide event streams are additive; use run/events or thread events for persisted journals."},
        ).model_dump(mode="json", by_alias=True)
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
    if method == "session/protocol":
        return container.session_protocol_reducer.session_protocol(str(params.get("thread_id") or params.get("threadId") or params.get("session_id") or params.get("sessionId") or ""))
    if method == "session/context_manager":
        return container.workbench_service.session_context_manager(str(params.get("thread_id") or params.get("threadId") or params.get("session_id") or params.get("sessionId") or ""))
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
    if method == "session/resume":
        session_id = str(params.get("thread_id") or params.get("threadId") or params.get("session_id") or params.get("sessionId") or "")
        service.resume_thread(session_id)
        return container.session_protocol_reducer.session_protocol(session_id)
    if method == "thread/fork":
        return service.fork_thread(str(params.get("thread_id") or params.get("threadId") or ""), title=params.get("title")).model_dump(mode="json")
    if method == "thread/archive":
        return service.archive_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "thread/rollback":
        return service.rollback_thread(str(params.get("thread_id") or params.get("threadId") or "")).model_dump(mode="json")
    if method == "turn/start":
        return service.start_turn(str(params.get("thread_id") or params.get("threadId") or ""), params).model_dump(mode="json")
    if method == "turn/protocol":
        return container.session_protocol_reducer.turn_protocol(str(params.get("turn_id") or params.get("turnId") or ""))
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
    if method == "run/protocol":
        return container.session_protocol_reducer.run_protocol(str(params.get("run_id") or params.get("runId") or ""))
    if method == "run/events":
        run_id = str(params.get("run_id") or params.get("runId") or "")
        run = container.run_service.get_run(run_id)
        after_sequence = int(params.get("after_sequence") or params.get("afterSequence") or 0)
        events = container.event_journal_service.list_run(run_id, after_sequence=after_sequence, limit=int(params.get("limit") or 500))
        return RunEventsResponse(
            run_id=run_id,
            workspace_id=run.workspace_id,
            items=[_protocol_event_state(event.model_dump(mode="json")) for event in events],
            next_sequence=max([event.sequence for event in events], default=after_sequence),
            legacy_event_page_ref=f"/runs/{run_id}/events-v2",
            compatibility={"legacy_endpoint": f"/runs/{run_id}/events-v2", "additive": True},
        ).model_dump(mode="json", by_alias=True)
    if method == "run/context_manager":
        return container.workbench_service.context_manager(str(params.get("run_id") or params.get("runId") or ""))
    if method == "draft/isolation":
        return container.workbench_service.draft_isolation(str(params.get("run_id") or params.get("runId") or ""))
    if method == "draft/gate":
        return container.workbench_service.draft_gate(str(params.get("run_id") or params.get("runId") or ""), create=True)
    if method == "draft/apply":
        return container.workbench_service.draft_apply(str(params.get("run_id") or params.get("runId") or ""), params)
    if method == "draft/variant/create":
        return container.workbench_service.draft_variants(str(params.get("run_id") or params.get("runId") or ""), params)
    if method == "guardian/gate":
        return container.workbench_service.guardian_gate(
            str(params.get("run_id") or params.get("runId") or ""),
            create=False,
            semantic_override=params.get("semantic_override") or params.get("semanticOverride"),
        )
    if method == "guardian/review":
        return container.workbench_service.guardian_gate(
            str(params.get("run_id") or params.get("runId") or ""),
            create=True,
            semantic_override=params.get("semantic_override") or params.get("semanticOverride"),
        )
    if method == "browser/proof":
        run_id = str(params.get("run_id") or params.get("runId") or "")
        payload = container.workbench_service.browser_proof(run_id)
        return _browser_proof_response(run_id=run_id, payload=payload, container=container)
    if method == "browser/replay_proof":
        return container.workbench_service.browser_replay_proof(str(params.get("run_id") or params.get("runId") or ""), build=bool(params.get("build") or False))
    if method == "browser/replay_scenario":
        return container.workbench_service.browser_replay_scenario(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("scenario_id") or params.get("scenarioId") or ""),
        )
    if method == "browser/replay_build":
        return container.workbench_service.browser_replay_proof(str(params.get("run_id") or params.get("runId") or ""), build=True)
    if method == "worker/sessions":
        return container.workbench_service.worker_sessions(str(params.get("run_id") or params.get("runId") or ""))
    if method == "worker/updates":
        run_id = str(params.get("run_id") or params.get("runId") or "")
        worker_id = str(params.get("worker_id") or params.get("workerId") or "").strip()
        workers_payload = container.workbench_service.workers(run_id)
        workers = [
            _protocol_worker_update(item, workers_payload)
            for item in workers_payload.get("workers") or []
            if isinstance(item, dict) and (not worker_id or str(item.get("worker_id") or "") == worker_id)
        ]
        return WorkerUpdateResponse(
            run_id=run_id,
            workspace_id=workers_payload.get("workspace_id"),
            workers=workers,
            next_sequence=0,
            compatibility={"legacy_endpoint": f"/runs/{run_id}/workers", "additive": True},
        ).model_dump(mode="json", by_alias=True)
    if method == "worker/mailbox":
        return container.workbench_service.worker_mailbox(str(params.get("run_id") or params.get("runId") or ""))
    if method == "worker/session":
        return container.workbench_service.worker_session(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("worker_session_id") or params.get("workerSessionId") or ""),
        )
    if method == "worker/resume":
        return container.workbench_service.resume_worker_session(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("worker_session_id") or params.get("workerSessionId") or ""),
        )
    if method == "worker/message":
        return container.workbench_service.message_worker_session(
            str(params.get("run_id") or params.get("runId") or ""),
            str(params.get("worker_session_id") or params.get("workerSessionId") or ""),
            params,
        )
    if method == "doctor/global":
        return container.workbench_service.doctor(
            scope=str(params.get("scope") or "quick"),
            workspace_id=params.get("workspace_id") or params.get("workspaceId"),
            run_id=params.get("run_id") or params.get("runId"),
        )
    if method == "doctor/workspace":
        return container.workbench_service.doctor_workspace(
            str(params.get("workspace_id") or params.get("workspaceId") or ""),
            scope=str(params.get("scope") or "quick"),
            run_id=params.get("run_id") or params.get("runId"),
        )
    if method == "doctor/run":
        run_id = str(params.get("run_id") or params.get("runId") or "")
        workspace_id = params.get("workspace_id") or params.get("workspaceId")
        if not workspace_id and run_id:
            try:
                workspace_id = container.run_service.get_run(run_id).workspace_id
            except KeyError:
                workspace_id = None
        return container.workbench_service.doctor(
            scope=str(params.get("scope") or "quick"),
            workspace_id=workspace_id,
            run_id=run_id or None,
        )
    if method == "prompt_contract/read":
        return container.workbench_service.prompt_contract(str(params.get("run_id") or params.get("runId") or ""))
    if method == "prompt_contract/compile":
        return container.workbench_service.compile_prompt_contract(str(params.get("run_id") or params.get("runId") or ""))
    if method == "prompt_contract/list":
        return container.workbench_service.workspace_prompt_contracts(str(params.get("workspace_id") or params.get("workspaceId") or ""))
    if method == "improve/map":
        return container.workbench_service.existing_app_map(str(params.get("run_id") or params.get("runId") or ""))
    if method == "improve/report":
        return container.workbench_service.improve_mode(str(params.get("run_id") or params.get("runId") or ""))
    if method == "improve/run":
        return container.workbench_service.improve_workspace(
            str(params.get("workspace_id") or params.get("workspaceId") or ""),
            params,
        ).model_dump(mode="json")
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
            managed=bool(params.get("managed") or False),
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


def _protocol_event_state(event: dict[str, Any]) -> ProtocolEventState:
    return ProtocolEventState(
        event_id=str(event.get("event_id") or ""),
        sequence=int(event.get("sequence") or 0),
        event_type=str(event.get("event_type") or ""),
        workspace_id=event.get("workspace_id"),
        run_id=event.get("run_id"),
        thread_id=event.get("thread_id"),
        turn_id=event.get("turn_id"),
        actor=str(event.get("actor") or "system"),
        summary=str(event.get("summary") or ""),
        payload_ref=event.get("payload_ref"),
        payload_sha256=event.get("payload_sha256"),
        refs={"source_ref": event.get("source_ref"), "idempotency_key": event.get("idempotency_key")},
        created_at=str(event.get("created_at") or ""),
    )


def _protocol_worker_update(worker: dict[str, Any], parent: dict[str, Any]) -> ProtocolWorkerUpdate:
    status = str(worker.get("status") or "planned")
    if status not in {"planned", "running", "completed", "failed", "blocked", "merged", "rejected"}:
        status = "blocked" if "disabled" in status else "planned"
    return ProtocolWorkerUpdate(
        worker_id=str(worker.get("worker_id") or ""),
        run_id=str(parent.get("run_id") or ""),
        workspace_id=str(parent.get("workspace_id") or ""),
        status=status,  # type: ignore[arg-type]
        phase=worker.get("branch_stage") or worker.get("lane_id"),
        owner_scope=worker.get("owner_scope"),
        path_prefixes=list((worker.get("ownership") or {}).get("allowed_paths") or []),
        branch_run_id=worker.get("branch_run_id"),
        branch_policy=worker.get("branch_policy"),
        write_scope=dict(worker.get("ownership") or {}),
        changed_files=list(worker.get("changed_files") or []),
        artifact_refs=[{"kind": key, "ref": value} for key, value in {
            "context": worker.get("context_ref"),
            "memory": worker.get("memory_snapshot_ref"),
            "output": worker.get("output_ref"),
            "mailbox": worker.get("mailbox_ref"),
            "ownership": worker.get("ownership_ref"),
        }.items() if value],
        proof_refs=[{"ref": ref} if isinstance(ref, str) else dict(ref) for ref in list(worker.get("proof_refs") or [])],
        merge_decision=dict(worker.get("merge_decision") or {}),
        refs={
            "worker_session_id": worker.get("worker_session_id"),
            "latest_turn_id": worker.get("latest_turn_id"),
            "merge_decision_ref": worker.get("merge_decision_ref") or parent.get("merge_decision_ref"),
        },
    )


def _browser_proof_response(*, run_id: str, payload: dict[str, Any], container: ServiceContainer) -> dict[str, Any]:
    run = container.run_service.get_run(run_id)
    proof = ProtocolBrowserProofState(
        run_id=run_id,
        workspace_id=run.workspace_id,
        status=str(payload.get("status") or "unknown"),  # type: ignore[arg-type]
        replay_proof_ref=payload.get("replay_proof_ref"),
        scenario_refs=[str(item.get("scenario_id") or item.get("id") or "") for item in payload.get("replay_scenarios") or [] if isinstance(item, dict)],
        scenarios=[dict(item) for item in payload.get("replay_scenarios") or [] if isinstance(item, dict)],
        playwright_spec_refs=list(payload.get("playwright_spec_refs") or []),
        screenshot_refs=list(payload.get("screenshots") or payload.get("screenshot_refs") or []),
        console_errors=[str(item) for item in payload.get("console_errors") or []],
        network_errors=[str(item) for item in payload.get("network_errors") or []],
        mobile_viewport=dict(payload.get("mobile_viewport") or {}),
        artifact_refs=dict(payload.get("artifact_refs") or {}),
        refs={"legacy_endpoint": f"/runs/{run_id}/browser-proof"},
        updated_at=str(payload.get("created_at") or payload.get("updated_at") or ""),
    )
    return BrowserProofResponse(
        run_id=run_id,
        workspace_id=run.workspace_id,
        proof=proof,
        compatibility={"legacy_endpoint": f"/runs/{run_id}/browser-proof", "additive": True},
    ).model_dump(mode="json", by_alias=True)


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
