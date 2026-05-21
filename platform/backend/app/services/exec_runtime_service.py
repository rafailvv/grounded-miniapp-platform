from __future__ import annotations

from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any

from app.models.domain import new_id
from app.models.threads import ItemRecord
from app.modules.miniapp_agent_loop.agent_command_policy import CommandPolicyDecision
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.output_artifact_service import OutputArtifactService
from app.services.sandbox_service import SandboxService
from app.services.rpc_event_hub import RpcEventHub
from app.services.tool_protocol import tool_envelope
from app.services.workspace.service import WorkspaceService


class ExecRuntimeService:
    """Long-running workspace command executor with sandboxed process lifecycle controls."""

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        platform_db: PlatformDb,
        event_hub: RpcEventHub,
        store: StateStore,
        event_journal_service: EventJournalService | None = None,
        sandbox_service: SandboxService | None = None,
        output_artifact_service: OutputArtifactService | None = None,
    ) -> None:
        self.workspace_service = workspace_service
        self.platform_db = platform_db
        self.event_hub = event_hub
        self.store = store
        self.event_journal_service = event_journal_service
        self.output_artifact_service = output_artifact_service
        self.sandbox_service = sandbox_service or workspace_service.sandbox_service
        self.process_manager = AgentProcessManager(sandbox_service=self.sandbox_service)
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def start(
        self,
        *,
        workspace_id: str,
        command: str,
        decision: CommandPolicyDecision,
        policy_evaluation: dict[str, Any],
        thread_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)
        process_id = new_id("proc")
        started_at = self._now()
        session = {
            "process_id": process_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "run_id": run_id,
            "command": command,
            "status": "starting",
            "exit_code": None,
            "started_at": started_at.isoformat(),
            "updated_at": started_at.isoformat(),
            "timeout_seconds": timeout_seconds,
            "policy_decision": policy_evaluation.get("decision") or {},
            "sandbox_summary": policy_evaluation.get("sandbox_summary") or {},
            "sandbox_boundary": None,
            "environment_snapshot": None,
            "log_capture": None,
            "killed_diagnostics": None,
        }
        with self._lock:
            self._sessions[process_id] = session
        self.platform_db.record_exec_process(process_id, session)
        if run_id and self.event_journal_service is not None:
            try:
                self.event_journal_service.append_run(
                    workspace_id=workspace_id,
                    run_id=str(run_id),
                    event_type="tool.requested",
                    actor="tool:shell.exec",
                    payload={"tool": "shell.exec", "process_id": process_id, "command": command, "status": "requested", "policy_decision": session.get("policy_decision") or {}},
                    summary="Shell command requested.",
                    idempotency_key=f"exec.requested:{process_id}",
                )
            except Exception:
                pass
        self._publish("command/exec/started", session)
        worker = threading.Thread(
            target=self._run_worker,
            args=(process_id, workspace.path, source_dir, command, decision, timeout_seconds),
            daemon=True,
        )
        worker.start()
        return dict(session)

    def write(self, process_id: str, data: str) -> dict[str, Any]:
        ok = self.process_manager.write_stdin(process_id, data)
        payload = {"process_id": process_id, "ok": ok, "bytes": len(data.encode("utf-8")), "status": "stdin_written" if ok else "not_running"}
        self._publish("command/exec/stdin", payload)
        return payload

    def resize(self, process_id: str, *, cols: int | None = None, rows: int | None = None) -> dict[str, Any]:
        ok = self.process_manager.resize(process_id, cols=cols, rows=rows)
        payload = {"process_id": process_id, "ok": ok, "cols": cols or 80, "rows": rows or 24, "status": "resized" if ok else "not_found"}
        self._publish("command/exec/resize", payload)
        return payload

    def terminate(self, process_id: str) -> dict[str, Any]:
        ok = self.process_manager.terminate(process_id)
        payload = {"process_id": process_id, "ok": ok, "status": "terminating" if ok else "not_running"}
        self._publish("command/exec/terminate", payload)
        return payload

    def read_output(self, process_id: str, *, stream: str = "stdout", start: int | None = None, end: int | None = None) -> dict[str, Any]:
        return self.process_manager.read_output(process_id, stream=stream, start=start, end=end)

    def snapshot(self) -> dict[str, Any]:
        process_snapshot = self.process_manager.snapshot()
        with self._lock:
            sessions = list(self._sessions.values())
        return {
            "sessions": sessions,
            "active_processes": process_snapshot.get("active_processes") or [],
            "processes": process_snapshot.get("processes") or [],
        }

    def _run_worker(
        self,
        process_id: str,
        workspace_path: str,
        source_dir: Path,
        command: str,
        decision: CommandPolicyDecision,
        timeout_seconds: int,
    ) -> None:
        del workspace_path

        def progress(payload: dict[str, Any]) -> None:
            event = {**payload, "process_id": process_id}
            if payload.get("status") == "started":
                self._update_session(
                    process_id,
                    {
                        "status": "running",
                        "sandbox_summary": payload.get("sandbox") or {},
                        "sandbox_boundary": payload.get("sandbox_boundary"),
                        "environment_snapshot": payload.get("environment_snapshot"),
                        "log_capture": payload.get("log_capture"),
                    },
                )
            elif payload.get("status") == "output_delta":
                self._publish("command/exec/output_delta", event)
            elif payload.get("status") == "heartbeat":
                self._publish("command/exec/heartbeat", event)
            elif payload.get("status") == "completed":
                self._publish("command/exec/completed", event)

        result = self.process_manager.run(
            draft_source=source_dir,
            command=command,
            decision=decision,
            timeout_seconds=max(1, min(timeout_seconds, 300)),
            max_output_chars=24000,
            progress_callback=progress,
            process_id=process_id,
            output_artifact_writer=self._output_artifact_writer(process_id),
        )
        result_payload = result.as_dict()
        completed_at = self._now().isoformat()
        session = self._update_session(
            process_id,
            {
                "status": "completed" if result_payload.get("exit_code") == 0 else "failed",
                "exit_code": result_payload.get("exit_code"),
                "duration_ms": result_payload.get("duration_ms"),
                "semantic_status": result_payload.get("semantic_status"),
                "success": result_payload.get("success"),
                "stdout": result_payload.get("stdout"),
                "stderr": result_payload.get("stderr"),
                "stdout_ref": result_payload.get("stdout_ref"),
                "stderr_ref": result_payload.get("stderr_ref"),
                "output_artifacts": result_payload.get("output_artifacts") or [],
                "sandbox_boundary": result_payload.get("sandbox_boundary"),
                "environment_snapshot": result_payload.get("environment_snapshot"),
                "log_capture": result_payload.get("log_capture"),
                "killed_diagnostics": result_payload.get("killed_diagnostics"),
                "completed_at": completed_at,
                "updated_at": completed_at,
                "result": result_payload,
            },
        )
        self.platform_db.record_exec_process(process_id, session)
        thread_id = session.get("thread_id")
        if thread_id:
            self.platform_db.append_item(
                ItemRecord(
                    thread_id=str(thread_id),
                    turn_id=session.get("turn_id"),
                    item_type="command.exec.completed",
                    status="completed" if session["status"] == "completed" else "failed",
                    payload=session,
                )
            )
            if self.event_journal_service is not None:
                try:
                    self.event_journal_service.append_thread(
                        workspace_id=str(session.get("workspace_id") or ""),
                        thread_id=str(thread_id),
                        turn_id=session.get("turn_id"),
                        run_id=str(session.get("run_id") or "") or None,
                        event_type="item.command.exec.completed",
                        actor="tool:shell.exec",
                        payload=session,
                        summary=f"Shell command {session['status']}.",
                        idempotency_key=f"exec.thread.completed:{process_id}",
                    )
                except Exception:
                    pass
        self._publish("command/exec/completed", session)
        run_id = session.get("run_id")
        if run_id and result_payload.get("semantic_status") == "blocked_by_sandbox" and self.event_journal_service is not None:
            try:
                self.event_journal_service.append_run(
                    workspace_id=str(session.get("workspace_id") or ""),
                    run_id=str(run_id),
                    event_type="sandbox.exec_blocked",
                    actor="system",
                    payload={"process_id": process_id, "command": command, "result": result_payload},
                    summary="Shell command blocked by sandbox.",
                    idempotency_key=f"sandbox.exec_blocked:{process_id}",
                )
            except Exception:
                pass
        if run_id:
            self._record_run_tool_event(
                str(run_id),
                tool_envelope(
                    tool="shell.exec",
                    input_payload={"command": command},
                    result={
                        "process_id": process_id,
                        "status": session["status"],
                        "exit_code": session.get("exit_code"),
                        "duration_ms": session.get("duration_ms"),
                        "sandbox_boundary": session.get("sandbox_boundary"),
                        "log_capture": session.get("log_capture"),
                        "killed_diagnostics": session.get("killed_diagnostics"),
                    },
                    risk=(session.get("policy_decision") or {}).get("risk") or "read_only",
                    timing={"duration_ms": session.get("duration_ms")},
                    artifacts=[
                        {"kind": "stdout", "ref": result_payload.get("stdout_ref") or f"exec:{process_id}:stdout"},
                        {"kind": "stderr", "ref": result_payload.get("stderr_ref") or f"exec:{process_id}:stderr"},
                    ],
                    stdout_ref=result_payload.get("stdout_ref"),
                    stderr_ref=result_payload.get("stderr_ref"),
                ),
            )
            if self.event_journal_service is not None:
                try:
                    self.event_journal_service.append_run(
                        workspace_id=str(session.get("workspace_id") or ""),
                        run_id=str(run_id),
                        event_type="tool.completed" if session["status"] == "completed" else "tool.failed",
                        actor="tool:shell.exec",
                        payload={
                            "tool": "shell.exec",
                            "process_id": process_id,
                            "command": command,
                            "status": session["status"],
                            "exit_code": session.get("exit_code"),
                            "duration_ms": session.get("duration_ms"),
                            "result": result_payload,
                        },
                        summary=f"Shell command {session['status']}.",
                        idempotency_key=f"exec.completed:{process_id}",
                    )
                except Exception:
                    pass

    def _output_artifact_writer(self, process_id: str):
        if self.output_artifact_service is None:
            return None

        def write(payload: dict[str, Any]) -> dict[str, Any] | None:
            session = dict(self._sessions.get(process_id) or {})
            run_id = str(session.get("run_id") or "").strip()
            workspace_id = str(session.get("workspace_id") or "").strip()
            if not run_id or not workspace_id:
                return None
            return self.output_artifact_service.store_command_output(
                workspace_id=workspace_id,
                run_id=run_id,
                process_id=process_id,
                stream=str(payload.get("stream") or "stdout"),
                command=str(payload.get("command") or session.get("command") or ""),
                content=str(payload.get("content") or ""),
                head_tail=payload.get("head_tail") if isinstance(payload.get("head_tail"), dict) else {},
                exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
                semantic_status=str(payload.get("semantic_status") or "") or None,
                metadata={"source": "exec_runtime", "thread_id": session.get("thread_id"), "turn_id": session.get("turn_id")},
            )

        return write

    def _update_session(self, process_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = dict(self._sessions.get(process_id) or {"process_id": process_id})
            current.update(patch)
            current["updated_at"] = self._now().isoformat()
            self._sessions[process_id] = current
            return dict(current)

    def _publish(self, method: str, payload: dict[str, Any]) -> None:
        self.event_hub.publish(method, payload)

    def _record_run_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": self._now().isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
