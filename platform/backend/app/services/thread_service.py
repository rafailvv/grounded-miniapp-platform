from __future__ import annotations

from datetime import datetime, timezone
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from app.models.domain import CreateRunRequest, new_id
from app.models.threads import ItemRecord, RolloutEventRecord, ThreadRecord, ThreadSnapshot, TurnRecord
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.exec_policy_service import ExecPolicyService
from app.services.rpc_event_hub import RpcEventHub
from app.services.tool_protocol import tool_envelope
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService


TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked"}


class ThreadService:
    def __init__(
        self,
        db: PlatformDb,
        run_service: RunService,
        workspace_service: WorkspaceService,
        event_hub: RpcEventHub,
        *,
        store: StateStore | None = None,
        exec_policy_service: ExecPolicyService | None = None,
    ) -> None:
        self.db = db
        self.run_service = run_service
        self.workspace_service = workspace_service
        self.event_hub = event_hub
        self.store = store or run_service.store
        self.exec_policy_service = exec_policy_service or ExecPolicyService()
        self._monitors: dict[str, threading.Thread] = {}

    def start_thread(self, *, workspace_id: str, title: str | None = None, metadata: dict[str, Any] | None = None) -> ThreadRecord:
        self.workspace_service.get_workspace(workspace_id)
        thread = ThreadRecord(workspace_id=workspace_id, title=(title or "Mini-app session").strip() or "Mini-app session", metadata=metadata or {})
        self.db.upsert_thread(thread)
        self._append_event(thread.thread_id, None, "thread.started", {"thread": thread.model_dump(mode="json")})
        self.event_hub.publish("thread/started", {"thread": thread.model_dump(mode="json")})
        return thread

    def list_threads(self, *, workspace_id: str | None = None, include_archived: bool = False, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        threads, next_cursor = self.db.list_threads(workspace_id=workspace_id, include_archived=include_archived, limit=limit, cursor=cursor)
        return {"items": [thread.model_dump(mode="json") for thread in threads], "next_cursor": next_cursor}

    def read_thread(self, thread_id: str, *, include_events: bool = True) -> ThreadSnapshot:
        thread = self._get_thread(thread_id)
        turns = self.db.list_turns(thread_id)
        items = self.db.list_items(thread_id, limit=1000)
        events = self.db.list_events(thread_id, limit=1000) if include_events else []
        return ThreadSnapshot(thread=thread, turns=turns, items=items, events=events)

    def resume_thread(self, thread_id: str) -> ThreadRecord:
        thread = self._get_thread(thread_id)
        if thread.archived:
            thread.archived = False
            thread.status = "idle"
            thread.updated_at = self._now()
            self.db.upsert_thread(thread)
        self.event_hub.publish("thread/status/changed", {"thread_id": thread.thread_id, "status": thread.status})
        return thread

    def fork_thread(self, thread_id: str, *, title: str | None = None) -> ThreadRecord:
        source = self._get_thread(thread_id)
        fork = ThreadRecord(
            workspace_id=source.workspace_id,
            title=title or f"{source.title} fork",
            forked_from_thread_id=source.thread_id,
            metadata={"forked_from_thread_id": source.thread_id},
        )
        self.db.upsert_thread(fork)
        self._append_item(fork.thread_id, None, "thread.forked", {"source_thread_id": source.thread_id})
        self._append_event(fork.thread_id, None, "thread.forked", {"source_thread_id": source.thread_id})
        self.event_hub.publish("thread/started", {"thread": fork.model_dump(mode="json")})
        return fork

    def archive_thread(self, thread_id: str) -> ThreadRecord:
        thread = self._get_thread(thread_id)
        thread.archived = True
        thread.status = "archived"
        thread.updated_at = self._now()
        self.db.upsert_thread(thread)
        self._append_event(thread_id, None, "thread.archived", {})
        self.event_hub.publish("thread/status/changed", {"thread_id": thread_id, "status": "archived"})
        return thread

    def rollback_thread(self, thread_id: str) -> ThreadRecord:
        thread = self._get_thread(thread_id)
        turns = self.db.list_turns(thread_id)
        completed_with_run = [turn for turn in turns if turn.status == "completed" and turn.linked_run_id]
        if not completed_with_run:
            raise ValueError("No completed applied turn is available for rollback.")
        latest = completed_with_run[-1]
        run = self.run_service.rollback_run(str(latest.linked_run_id))
        self._append_item(thread_id, latest.turn_id, "thread.rollback", {"run": run.model_dump(mode="json")})
        self._append_event(thread_id, latest.turn_id, "thread.rollback", {"run_id": run.run_id})
        return thread

    def start_turn(self, thread_id: str, params: dict[str, Any]) -> TurnRecord:
        thread = self._get_thread(thread_id)
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("turn/start requires a non-empty prompt.")
        turn = TurnRecord(
            thread_id=thread.thread_id,
            workspace_id=thread.workspace_id,
            kind=str(params.get("kind") or "agent"),  # type: ignore[arg-type]
            status="running",
            prompt=prompt,
            started_at=self._now(),
            metadata={"request": dict(params)},
        )
        self.db.insert_turn(turn)
        thread.status = "running"
        thread.current_turn_id = turn.turn_id
        thread.updated_at = self._now()
        self.db.upsert_thread(thread)
        self._append_item(thread.thread_id, turn.turn_id, "user.message", {"content": prompt})
        self._append_event(thread.thread_id, turn.turn_id, "turn.started", {"turn": turn.model_dump(mode="json")})
        self.event_hub.publish("turn/started", {"thread_id": thread.thread_id, "turn": turn.model_dump(mode="json")})

        run_request = CreateRunRequest(
            prompt=prompt,
            mode=params.get("mode") or "generate",
            intent=params.get("intent") or "auto",
            apply_strategy=params.get("apply_strategy") or "staged_auto_apply",
            target_role_scope=list(params.get("target_role_scope") or []),
            model_profile=str(params.get("model_profile") or ""),
            generation_mode=params.get("generation_mode") or "balanced",
            target_platform=params.get("target_platform") or "telegram_mini_app",
            preview_profile=params.get("preview_profile") or "telegram_mock",
            error_context=params.get("error_context"),
        )
        run = self.run_service.create_run(thread.workspace_id, run_request)
        turn.linked_run_id = run.run_id
        turn.metadata["run"] = run.model_dump(mode="json")
        self.db.insert_turn(turn)
        self._append_item(thread.thread_id, turn.turn_id, "run.linked", {"run": run.model_dump(mode="json")})
        self._ensure_monitor(turn.thread_id, turn.turn_id, run.run_id)
        return turn

    def interrupt_turn(self, thread_id: str, turn_id: str) -> TurnRecord:
        turn = self._get_turn(turn_id)
        if turn.thread_id != thread_id:
            raise KeyError(f"Turn {turn_id} does not belong to thread {thread_id}.")
        if turn.status == "running" and turn.linked_run_id:
            self.run_service.stop_run(turn.linked_run_id)
        turn.status = "interrupted"
        turn.completed_at = self._now()
        turn.updated_at = self._now()
        self.db.insert_turn(turn)
        self._append_item(thread_id, turn_id, "turn.interrupted", {})
        self._append_event(thread_id, turn_id, "turn.interrupted", {})
        self.event_hub.publish("turn/interrupted", {"thread_id": thread_id, "turn": turn.model_dump(mode="json")})
        return turn

    def steer_turn(self, thread_id: str, turn_id: str, message: str) -> ItemRecord:
        turn = self._get_turn(turn_id)
        if turn.thread_id != thread_id:
            raise KeyError(f"Turn {turn_id} does not belong to thread {thread_id}.")
        return self._append_item(thread_id, turn_id, "user.steer", {"content": message})

    def compact_thread(self, thread_id: str) -> TurnRecord:
        thread = self._get_thread(thread_id)
        turns = self.db.list_turns(thread_id, limit=500)
        items = self.db.list_items(thread_id, limit=1000)
        linked_runs = []
        known_failures = []
        active_files: list[str] = []
        accepted_decisions: list[str] = []
        active_constraints: list[str] = []
        unresolved_approvals: list[dict[str, Any]] = []
        for turn in turns:
            if not turn.linked_run_id:
                continue
            try:
                run = self.run_service.get_run(turn.linked_run_id)
            except Exception:
                continue
            linked_runs.append(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "apply_status": run.apply_status,
                    "summary": run.summary,
                    "created_at": run.created_at.isoformat(),
                }
            )
            if run.failure_reason:
                known_failures.append(run.failure_reason)
            active_files.extend(run.touched_files[:20])
            active_constraints.extend(str(item) for item in (run.acceptance_contract or {}).get("constraints") or [])
            accepted_decisions.extend(str(item) for item in (run.implementation_plan or {}).get("decisions") or [])
            approvals = self.run_service.store.get("reports", f"approvals:{run.run_id}") or {}
            unresolved_approvals.extend(
                item
                for item in approvals.get("items") or []
                if isinstance(item, dict) and item.get("status") == "pending"
            )
        summary = {
            "item_count": len(items),
            "turn_count": len(turns),
            "short_summary": f"{len(turns)} turns, {len(linked_runs)} linked runs, {len(set(active_files))} active files.",
            "latest_items": [item.payload for item in items[-8:]],
            "linked_runs": linked_runs[-12:],
            "active_constraints": list(dict.fromkeys(active_constraints))[:20],
            "accepted_decisions": list(dict.fromkeys(accepted_decisions))[:20],
            "known_failures": list(dict.fromkeys(known_failures))[:12],
            "current_file_focus": list(dict.fromkeys(active_files))[:24],
            "unresolved_approvals": unresolved_approvals[:20],
        }
        turn = TurnRecord(thread_id=thread_id, workspace_id=thread.workspace_id, kind="compaction", status="completed", prompt="Compact thread history.", completed_at=self._now())
        turn.metadata["compaction"] = summary
        self.db.insert_turn(turn)
        self._append_item(thread_id, turn.turn_id, "thread.compaction_summary", summary)
        self._append_event(thread_id, turn.turn_id, "thread.compacted", summary)
        return turn

    def review_thread(self, thread_id: str) -> TurnRecord:
        thread = self._get_thread(thread_id)
        turn = TurnRecord(thread_id=thread_id, workspace_id=thread.workspace_id, kind="review", status="completed", prompt="Review current thread changes.", completed_at=self._now())
        self.db.insert_turn(turn)
        self._append_item(thread_id, turn.turn_id, "review.summary", {"summary": "Review mode scaffold is ready; wire detailed diff review in the next iteration."})
        self._append_event(thread_id, turn.turn_id, "review.completed", {})
        return turn

    def fs_read_file(self, *, workspace_id: str, path: str, run_id: str | None = None) -> dict[str, str]:
        return {"path": path, "content": self.workspace_service.read_file(workspace_id, path, run_id=run_id)}

    def fs_write_file(self, *, workspace_id: str, path: str, content: str, run_id: str | None = None) -> dict[str, str]:
        revision = self.workspace_service.save_file(workspace_id, type("SaveFileRequestAdapter", (), {"relative_path": path, "content": content, "run_id": run_id})())
        return {"revision_id": revision.revision_id if revision else "", "commit_sha": revision.commit_sha if revision else ""}

    def fs_read_directory(self, *, workspace_id: str, path: str = "", run_id: str | None = None) -> dict[str, Any]:
        entries = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        prefix = path.strip("/")
        if prefix:
            entries = [entry for entry in entries if str(entry.get("path") or "").startswith(prefix)]
        return {"entries": entries}

    def exec_command(self, *, workspace_id: str, command: str, thread_id: str | None = None, turn_id: str | None = None, timeout: int = 30, approval_id: str | None = None, preset: str = "safe_auto") -> dict[str, Any]:
        linked_run_id = self._get_turn(turn_id).linked_run_id if turn_id else None
        evaluation = self.exec_policy_service.evaluate_command(command, preset=preset)
        decision = evaluation.get("decision") if isinstance(evaluation.get("decision"), dict) else {}
        approval = evaluation.get("approval") if isinstance(evaluation.get("approval"), dict) else {}
        if linked_run_id:
            self._record_run_tool_event(
                linked_run_id,
                tool_envelope(
                    tool="policy.evaluate",
                    input_payload={"command": self.exec_policy_service.redact(command), "preset": preset},
                    result=evaluation,
                    risk=decision.get("risk") or "unknown",
                    approval=approval,
                ),
            )
            if approval.get("required") and approval.get("approval_id"):
                self._upsert_run_approval(
                    linked_run_id,
                    {
                        "approval_id": str(approval["approval_id"]),
                        "status": "pending",
                        "kind": "command",
                        "risk": decision.get("risk"),
                        "summary": self.exec_policy_service.redact(command),
                        "input": {"command": self.exec_policy_service.redact(command), "workspace_id": workspace_id},
                        "policy_decision": decision,
                        "created_at": self._now().isoformat(),
                    },
                )
        if decision.get("action") != "allow":
            raise ValueError(f"Command rejected by policy: {decision.get('reason')}")
        if approval.get("required"):
            if not approval_id or not linked_run_id or not self._run_approval_status(linked_run_id, approval_id) == "approved":
                raise ValueError(f"Command requires approval: {approval.get('approval_id')}")
        process_id = new_id("proc")
        cwd = self.workspace_service.source_dir(workspace_id)
        started = time.perf_counter()
        result = subprocess.run(list(decision.get("argv") or []), cwd=cwd, shell=False, text=True, capture_output=True, timeout=max(1, min(timeout, 120)))
        payload = {
            "process_id": process_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "workspace_id": workspace_id,
            "command": command,
            "status": "completed",
            "exit_code": result.returncode,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "policy_decision": decision,
        }
        self.db.record_exec_process(process_id, payload)
        if linked_run_id:
            self._record_run_tool_event(
                linked_run_id,
                tool_envelope(
                    tool="shell.exec",
                    input_payload={"command": self.exec_policy_service.redact(command)},
                    result={key: payload[key] for key in ("status", "exit_code", "duration_ms")},
                    risk=decision.get("risk") or "read_only",
                    approval={"required": False, "status": "approved" if approval_id else "not_required", "approval_id": approval_id},
                    timing={"duration_ms": payload["duration_ms"]},
                ),
            )
        if thread_id:
            self._append_item(thread_id, turn_id, "command.exec.completed", payload)
        return payload

    def _record_run_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": self._now().isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)

    def _upsert_run_approval(self, run_id: str, item: dict[str, Any]) -> None:
        key = f"approvals:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        items = [entry for entry in payload.get("items") or [] if isinstance(entry, dict)]
        if not any(entry.get("approval_id") == item.get("approval_id") for entry in items):
            items.append(item)
        payload["items"] = items
        self.store.upsert("reports", key, payload)

    def _run_approval_status(self, run_id: str, approval_id: str) -> str | None:
        payload = self.store.get("reports", f"approvals:{run_id}") or {}
        for item in payload.get("items") or []:
            if isinstance(item, dict) and item.get("approval_id") == approval_id:
                return str(item.get("status") or "")
        return None

    def _ensure_monitor(self, thread_id: str, turn_id: str, run_id: str) -> None:
        if turn_id in self._monitors:
            return
        worker = threading.Thread(target=self._monitor_run, args=(thread_id, turn_id, run_id), daemon=True)
        self._monitors[turn_id] = worker
        worker.start()

    def _monitor_run(self, thread_id: str, turn_id: str, run_id: str) -> None:
        seen_activity = 0
        last_stage = ""
        while True:
            try:
                run = self.run_service.get_run(run_id)
            except Exception as exc:
                self._finish_turn(thread_id, turn_id, "failed", {"error": str(exc)})
                return
            if run.current_stage != last_stage:
                last_stage = run.current_stage
                self._append_item(
                    thread_id,
                    turn_id,
                    "item.tool.progress",
                    {"stage": run.current_stage, "progress_percent": run.progress_percent, "run_id": run.run_id},
                    notify_method="item/tool/progress",
                )
            activity = list(run.agent_activity_events or [])
            for event in activity[seen_activity:]:
                self._append_item(thread_id, turn_id, "agent.activity", {"run_id": run.run_id, **dict(event)}, notify_method="item/tool/progress")
            seen_activity = len(activity)
            if run.status in TERMINAL_RUN_STATUSES:
                status = "completed" if run.status == "completed" else "failed"
                self._finish_turn(thread_id, turn_id, status, {"run": run.model_dump(mode="json")})
                return
            time.sleep(1.0)

    def _finish_turn(self, thread_id: str, turn_id: str, status: str, payload: dict[str, Any]) -> None:
        turn = self._get_turn(turn_id)
        turn.status = status  # type: ignore[assignment]
        turn.completed_at = self._now()
        turn.updated_at = self._now()
        self.db.insert_turn(turn)
        thread = self._get_thread(thread_id)
        thread.status = "idle" if status == "completed" else "failed"
        thread.updated_at = self._now()
        self.db.upsert_thread(thread)
        self._append_item(thread_id, turn_id, f"turn.{status}", payload)
        self._append_event(thread_id, turn_id, f"turn.{status}", payload)
        self.event_hub.publish(f"turn/{status}", {"thread_id": thread_id, "turn": turn.model_dump(mode="json"), **payload})

    def _append_item(self, thread_id: str, turn_id: str | None, item_type: str, payload: dict[str, Any], *, notify_method: str | None = None) -> ItemRecord:
        item = self.db.append_item(ItemRecord(thread_id=thread_id, turn_id=turn_id, item_type=item_type, payload=payload))
        self.event_hub.publish(notify_method or "item/completed", {"thread_id": thread_id, "turn_id": turn_id, "item": item.model_dump(mode="json")})
        return item

    def _append_event(self, thread_id: str, turn_id: str | None, event_type: str, payload: dict[str, Any]) -> RolloutEventRecord:
        event = self.db.append_event(RolloutEventRecord(thread_id=thread_id, turn_id=turn_id, event_type=event_type, payload=payload))
        return event

    def _get_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.db.get_thread(thread_id)
        if thread is None:
            raise KeyError(f"Thread not found: {thread_id}")
        return thread

    def _get_turn(self, turn_id: str) -> TurnRecord:
        turn = self.db.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"Turn not found: {turn_id}")
        return turn

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
