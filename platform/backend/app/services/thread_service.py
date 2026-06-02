from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import threading
import time
from typing import Any

from app.models.domain import CreateRunRequest, new_id
from app.models.threads import ItemRecord, RolloutEventRecord, ThreadRecord, ThreadSnapshot, TurnRecord
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.exec_policy_service import ExecPolicyService
from app.services.event_journal import EventJournalService
from app.services.exec_runtime_service import ExecRuntimeService
from app.services.rpc_event_hub import RpcEventHub
from app.services.tool_protocol import tool_envelope
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService


TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked"}
LIVE_WRITER_INTERVAL_SECONDS = 5.0


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
        exec_runtime_service: ExecRuntimeService | None = None,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.db = db
        self.run_service = run_service
        self.workspace_service = workspace_service
        self.event_hub = event_hub
        self.store = store or run_service.store
        self.exec_policy_service = exec_policy_service or ExecPolicyService()
        self.event_journal_service = event_journal_service
        self.exec_runtime_service = exec_runtime_service or ExecRuntimeService(
            workspace_service=workspace_service,
            platform_db=db,
            event_hub=event_hub,
            store=self.store,
            event_journal_service=event_journal_service,
        )
        self._monitors: dict[str, threading.Thread] = {}

    def start_thread(self, *, workspace_id: str, title: str | None = None, metadata: dict[str, Any] | None = None) -> ThreadRecord:
        self.workspace_service.get_workspace(workspace_id)
        initial_metadata = dict(metadata or {})
        initial_metadata.setdefault("protocol", {"protocol_version": "grounded.session_protocol.v1", "session_id_alias": "thread_id"})
        thread = ThreadRecord(workspace_id=workspace_id, title=(title or "Mini-app session").strip() or "Mini-app session", metadata=initial_metadata)
        self._refresh_stable_metadata(thread, reason="thread.start")
        self.db.upsert_thread(thread)
        self._append_event(thread.thread_id, None, "thread.started", {"thread": thread.model_dump(mode="json")})
        self._journal_thread_event(
            thread.thread_id,
            None,
            "session.started",
            {"session_id": thread.thread_id, "thread": thread.model_dump(mode="json")},
            actor="system",
            source_ref=thread.thread_id,
            idempotency_key=f"session.started:{thread.thread_id}",
        )
        self._refresh_stable_metadata(thread, reason="thread.started")
        self.db.upsert_thread(thread)
        self.event_hub.publish("thread/started", {"thread": thread.model_dump(mode="json")})
        return thread

    def list_threads(self, *, workspace_id: str | None = None, include_archived: bool = False, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        threads, next_cursor = self.db.list_threads(workspace_id=workspace_id, include_archived=include_archived, limit=limit, cursor=cursor)
        threads = [self.recover_thread_state(thread.thread_id, emit_event=False) if thread.status == "running" else thread for thread in threads]
        return {"items": [thread.model_dump(mode="json") for thread in threads], "next_cursor": next_cursor}

    def read_thread(self, thread_id: str, *, include_events: bool = True) -> ThreadSnapshot:
        thread = self.recover_thread_state(thread_id, emit_event=False)
        turns = self.db.list_turns(thread_id)
        items = self.db.list_items(thread_id, limit=1000)
        events = self.db.list_events(thread_id, limit=1000) if include_events else []
        return ThreadSnapshot(thread=thread, turns=turns, items=items, events=events)

    def create_snapshot(self, thread_id: str, *, reason: str = "manual", turn_id: str | None = None) -> dict[str, Any]:
        snapshot = self.read_thread(thread_id)
        payload = {
            "thread": snapshot.thread.model_dump(mode="json"),
            "turns": [turn.model_dump(mode="json") for turn in snapshot.turns],
            "items": [item.model_dump(mode="json") for item in snapshot.items],
            "events": [event.model_dump(mode="json") for event in snapshot.events],
            "compact_boundary": reason == "compaction",
            "stable_metadata": snapshot.thread.metadata.get("stable_thread") if isinstance(snapshot.thread.metadata, dict) else None,
        }
        record = self.db.insert_thread_snapshot(
            snapshot_id=new_id("snapshot"),
            thread_id=thread_id,
            turn_id=turn_id,
            reason=reason,
            payload=payload,
        )
        self._append_event(thread_id, turn_id, "thread.snapshot", {"snapshot_id": record["snapshot_id"], "reason": reason})
        return record

    def list_snapshots(self, thread_id: str, *, limit: int = 50) -> dict[str, Any]:
        self._get_thread(thread_id)
        return {"items": self.db.list_thread_snapshots(thread_id, limit=limit)}

    def resume_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.recover_thread_state(thread_id)
        if thread.archived:
            thread.archived = False
            thread.status = "idle"
            thread.updated_at = self._now()
            self._refresh_stable_metadata(thread, reason="thread.unarchive")
            self.db.upsert_thread(thread)
            self._append_event(thread.thread_id, None, "thread.unarchived", {"stable_metadata": thread.metadata.get("stable_thread")})
        self._resume_live_writer(thread.thread_id)
        self.event_hub.publish("thread/status/changed", {"thread_id": thread.thread_id, "status": thread.status})
        return thread

    def fork_thread(self, thread_id: str, *, title: str | None = None) -> ThreadRecord:
        source = self.recover_thread_state(thread_id, emit_event=False)
        source_snapshot = self.create_snapshot(source.thread_id, reason="fork_source")
        fork = ThreadRecord(
            workspace_id=source.workspace_id,
            title=title or f"{source.title} fork",
            forked_from_thread_id=source.thread_id,
            metadata={
                "forked_from_thread_id": source.thread_id,
                "fork": {
                    "schema": "grounded.thread_fork.v1",
                    "source_thread_id": source.thread_id,
                    "source_snapshot_id": source_snapshot["snapshot_id"],
                    "source_stable_metadata": source.metadata.get("stable_thread"),
                    "created_at": self._now().isoformat(),
                },
            },
        )
        self._refresh_stable_metadata(fork, reason="thread.fork")
        self.db.upsert_thread(fork)
        payload = {"source_thread_id": source.thread_id, "source_snapshot_id": source_snapshot["snapshot_id"], "stable_metadata": fork.metadata.get("stable_thread")}
        self._append_item(fork.thread_id, None, "thread.forked", payload)
        self._append_event(fork.thread_id, None, "thread.forked", payload)
        self._refresh_stable_metadata(fork, reason="thread.forked")
        self.db.upsert_thread(fork)
        self.event_hub.publish("thread/started", {"thread": fork.model_dump(mode="json")})
        return fork

    def archive_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.recover_thread_state(thread_id, emit_event=False)
        thread.archived = True
        thread.status = "archived"
        thread.updated_at = self._now()
        self._refresh_stable_metadata(thread, reason="thread.archive")
        self.db.upsert_thread(thread)
        self._append_event(thread_id, None, "thread.archived", {"stable_metadata": thread.metadata.get("stable_thread")})
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
            parent_turn_id=params.get("parent_turn_id") or params.get("parentTurnId"),
            started_at=self._now(),
            metadata={
                "request": dict(params),
                "protocol_version": "grounded.session_protocol.v1",
                "parent_turn_id": params.get("parent_turn_id") or params.get("parentTurnId"),
                "resume_from_turn_id": params.get("resume_from_turn_id") or params.get("resumeFromTurnId"),
                "linked_run_id": None,
                "started_by": str(params.get("started_by") or params.get("startedBy") or "user"),
                "artifact_refs": [],
                "proof_refs": [],
                "memory_update_refs": [],
            },
        )
        self.db.insert_turn(turn)
        thread.status = "running"
        thread.current_turn_id = turn.turn_id
        thread.updated_at = self._now()
        self._refresh_stable_metadata(thread, reason="turn.start")
        self.db.upsert_thread(thread)
        self._append_item(thread.thread_id, turn.turn_id, "user.message", {"content": prompt})
        self._append_event(thread.thread_id, turn.turn_id, "turn.started", {"turn": turn.model_dump(mode="json")})
        self._journal_thread_event(
            thread.thread_id,
            turn.turn_id,
            "turn.started",
            {"session_id": thread.thread_id, "turn": turn.model_dump(mode="json")},
            actor="user",
            source_ref=turn.turn_id,
            idempotency_key=f"turn.started:{turn.turn_id}",
        )
        self.event_hub.publish("turn/started", {"thread_id": thread.thread_id, "turn": turn.model_dump(mode="json")})

        run_request = CreateRunRequest(
            prompt=prompt,
            mode=params.get("mode") or "generate",
            edit_mode=params.get("edit_mode") or "default",
            intent=params.get("intent") or "auto",
            apply_strategy=params.get("apply_strategy") or "staged_auto_apply",
            target_role_scope=list(params.get("target_role_scope") or []),
            model_profile=str(params.get("model_profile") or ""),
            generation_mode=params.get("generation_mode") or "balanced",
            target_platform=params.get("target_platform") or "telegram_mini_app",
            preview_profile=params.get("preview_profile") or "telegram_mock",
            session_id=thread.thread_id,
            error_context=params.get("error_context"),
        )
        run = self.run_service.create_run(thread.workspace_id, run_request)
        turn.linked_run_id = run.run_id
        turn.metadata["linked_run_id"] = run.run_id
        turn.metadata["artifact_refs"] = [{"kind": "run_artifacts", "ref": f"run_artifacts:{run.run_id}"}]
        turn.metadata["proof_refs"] = [{"kind": "latest_check", "ref": f"latest_check_execution:{run.run_id}"}]
        turn.metadata["memory_update_refs"] = [{"kind": "memory_stage1", "ref": f"memory_stage1:{run.workspace_id}:{run.run_id}"}]
        turn.metadata["run"] = run.model_dump(mode="json")
        self.db.insert_turn(turn)
        self._append_item(thread.thread_id, turn.turn_id, "run.linked", {"run": run.model_dump(mode="json")})
        self._journal_thread_event(
            thread.thread_id,
            turn.turn_id,
            "run.linked",
            {"session_id": thread.thread_id, "turn_id": turn.turn_id, "run_id": run.run_id, "run": run.model_dump(mode="json")},
            actor="system",
            source_ref=run.run_id,
            idempotency_key=f"run.linked:{turn.turn_id}:{run.run_id}",
        )
        self._journal_run_protocol(
            run.run_id,
            "run.started",
            {"session_id": thread.thread_id, "turn_id": turn.turn_id, "run_id": run.run_id, "status": run.status, "stage": run.current_stage},
            summary="Run started for session turn.",
            idempotency_key=f"run.started:{run.run_id}",
        )
        self._write_live_snapshot(thread.thread_id, turn.turn_id, run, reason="turn.start")
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
        self._journal_thread_event(
            thread_id,
            turn_id,
            "turn.interrupted",
            {"session_id": thread_id, "turn_id": turn_id, "run_id": turn.linked_run_id, "status": "interrupted"},
            actor="system",
            source_ref=turn_id,
            idempotency_key=f"turn.interrupted:{turn_id}",
        )
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
            raw_run = self.run_service.store.get("runs", turn.linked_run_id)
            raw_failure_reason = (
                str(raw_run.get("failure_reason") or "").strip()
                if isinstance(raw_run, dict)
                else ""
            )
            raw_touched_files = (
                [str(item) for item in raw_run.get("touched_files") or [] if str(item).strip()]
                if isinstance(raw_run, dict) and isinstance(raw_run.get("touched_files"), list)
                else []
            )
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
            failure_reason = raw_failure_reason or str(run.failure_reason or "").strip()
            if failure_reason:
                known_failures.append(failure_reason)
            active_files.extend((raw_touched_files or run.touched_files)[:20])
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
        self._journal_thread_event(
            thread_id,
            turn.turn_id,
            "turn.compacted",
            {"session_id": thread_id, "turn_id": turn.turn_id, "summary": summary, "status": "completed"},
            actor="system",
            source_ref=turn.turn_id,
            idempotency_key=f"turn.compacted:{turn.turn_id}",
        )
        self.create_snapshot(thread_id, reason="compaction", turn_id=turn.turn_id)
        return turn

    def review_thread(self, thread_id: str) -> TurnRecord:
        thread = self._get_thread(thread_id)
        turns = self.db.list_turns(thread_id, limit=500)
        linked_turns = [turn for turn in turns if turn.linked_run_id]
        latest_run = None
        latest_turn = linked_turns[-1] if linked_turns else None
        if latest_turn and latest_turn.linked_run_id:
            try:
                latest_run = self.run_service.get_run(latest_turn.linked_run_id)
            except Exception:
                latest_run = None

        changed_files: list[str] = []
        check_issues: list[dict[str, Any]] = []
        artifact_refs: dict[str, Any] = {}
        if latest_run is not None:
            changed_files = list(latest_run.touched_files or [])[:30]
            latest_execution = self.store.get("reports", f"latest_check_execution:{latest_run.run_id}") or {}
            if isinstance(latest_execution, dict):
                for result in latest_execution.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    status = str(result.get("status") or "")
                    if status not in {"failed", "blocked"}:
                        continue
                    check_issues.append(
                        {
                            "check": result.get("name"),
                            "status": status,
                            "details": result.get("details"),
                            "logs": list(result.get("logs") or [])[-5:],
                        }
                    )
            for key in (
                f"trace_bundle:{thread.workspace_id}:{latest_run.run_id}",
                f"acceptance_contract:{thread.workspace_id}:{latest_run.run_id}",
                f"run_artifacts:{latest_run.run_id}",
            ):
                value = self.store.get("reports", key)
                if value:
                    artifact_refs[key.split(":", 1)[0]] = key

        status = "passed" if latest_run is not None and latest_run.status == "completed" and not check_issues else "needs_attention"
        summary = {
            "schema": "grounded.thread_review.v1",
            "status": status,
            "thread_id": thread_id,
            "latest_run": latest_run.model_dump(mode="json") if latest_run is not None else None,
            "changed_files": changed_files,
            "issues": check_issues,
            "artifact_refs": artifact_refs,
            "summary": (
                "Latest linked run completed without failed checks."
                if status == "passed"
                else "Latest linked run is missing, incomplete, or has failed checks."
            ),
        }
        turn = TurnRecord(thread_id=thread_id, workspace_id=thread.workspace_id, kind="review", status="completed", prompt="Review current thread changes.", completed_at=self._now())
        turn.metadata["review"] = summary
        self.db.insert_turn(turn)
        self._append_item(thread_id, turn.turn_id, "review.summary", summary)
        self._append_event(thread_id, turn.turn_id, "review.completed", summary)
        return turn

    def stable_metadata(self, thread_id: str) -> dict[str, Any]:
        thread = self._get_thread(thread_id)
        self._refresh_stable_metadata(thread, reason="metadata.read")
        self.db.upsert_thread(thread)
        return dict(thread.metadata.get("stable_thread") or {})

    def write_live_snapshot(self, thread_id: str, *, reason: str = "manual") -> dict[str, Any]:
        thread = self._get_thread(thread_id)
        if not thread.current_turn_id:
            raise ValueError("Thread has no current turn to snapshot.")
        turn = self._get_turn(thread.current_turn_id)
        if not turn.linked_run_id:
            raise ValueError("Current turn is not linked to a run.")
        run = self.run_service.get_run(turn.linked_run_id)
        return self._write_live_snapshot(thread.thread_id, turn.turn_id, run, reason=reason)

    def recover_thread_state(self, thread_id: str, *, emit_event: bool = True) -> ThreadRecord:
        thread = self._get_thread(thread_id)
        changed = False
        recovery_payload: dict[str, Any] = {"schema": "grounded.thread_recovery.v1", "thread_id": thread_id, "repairs": []}
        current_turn = self.db.get_turn(thread.current_turn_id) if thread.current_turn_id else None
        if thread.status == "running" and current_turn is None:
            thread.status = "idle"
            thread.current_turn_id = None
            changed = True
            recovery_payload["repairs"].append({"kind": "missing_current_turn", "status": "idle"})
        if current_turn is not None and current_turn.linked_run_id:
            try:
                run = self.run_service.get_run(current_turn.linked_run_id)
            except Exception as exc:
                if current_turn.status == "running":
                    current_turn.status = "failed"
                    current_turn.completed_at = self._now()
                    current_turn.updated_at = self._now()
                    self.db.insert_turn(current_turn)
                    thread.status = "failed"
                    changed = True
                    recovery_payload["repairs"].append({"kind": "missing_run", "run_id": current_turn.linked_run_id, "error": str(exc)})
            else:
                if current_turn.status == "running" and run.status in TERMINAL_RUN_STATUSES:
                    status = "completed" if run.status == "completed" else "failed"
                    current_turn.status = status  # type: ignore[assignment]
                    current_turn.completed_at = self._now()
                    current_turn.updated_at = self._now()
                    self.db.insert_turn(current_turn)
                    thread.status = "idle" if status == "completed" else "failed"
                    thread.current_turn_id = current_turn.turn_id
                    changed = True
                    recovery_payload["repairs"].append({"kind": "terminal_run", "run_id": run.run_id, "run_status": run.status, "turn_status": status})
                    self._append_item(thread_id, current_turn.turn_id, f"turn.{status}", {"run": run.model_dump(mode="json"), "recovered": True})
                elif current_turn.status == "running":
                    self._write_live_snapshot(thread_id, current_turn.turn_id, run, reason="recovery")
                    self._ensure_monitor(thread_id, current_turn.turn_id, run.run_id)
        elif current_turn is not None and current_turn.status in {"completed", "failed", "interrupted"} and thread.status == "running":
            thread.status = "idle" if current_turn.status == "completed" else "failed"
            changed = True
            recovery_payload["repairs"].append({"kind": "terminal_turn", "turn_id": current_turn.turn_id, "turn_status": current_turn.status})
        self._refresh_stable_metadata(thread, reason="thread.recovery" if changed else "thread.read")
        if changed:
            thread.updated_at = self._now()
        self.db.upsert_thread(thread)
        if emit_event and changed:
            recovery_payload["stable_metadata"] = thread.metadata.get("stable_thread")
            self._append_event(thread_id, current_turn.turn_id if current_turn else None, "thread.recovered", recovery_payload)
        return thread

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

    def exec_command(
        self,
        *,
        workspace_id: str,
        command: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        timeout: int = 30,
        managed: bool = False,
        approval_id: str | None = None,
        preset: str = "safe_auto",
    ) -> dict[str, Any]:
        linked_run_id = self._get_turn(turn_id).linked_run_id if turn_id else None
        source_dir = self.workspace_service.source_dir(workspace_id)
        evaluation = self.exec_policy_service.evaluate_command(command, preset=preset, root=source_dir, workspace_id=workspace_id)
        decision = evaluation.get("decision") if isinstance(evaluation.get("decision"), dict) else {}
        approval = evaluation.get("approval") if isinstance(evaluation.get("approval"), dict) else {}
        self.exec_policy_service.append_audit_record(
            self.store,
            workspace_id=workspace_id,
            command=command,
            evaluation=evaluation,
            run_id=linked_run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source="command.exec.evaluate",
        )
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
        if decision.get("action") == "forbidden":
            raise ValueError(f"Command rejected by policy: {decision.get('reason')}")
        if approval.get("required"):
            if not approval_id or not linked_run_id or not self._run_approval_status(linked_run_id, approval_id) == "approved":
                raise ValueError(f"Command requires approval: {approval.get('approval_id')}")
        runtime_decision = self.exec_policy_service.policy.decide(command)
        if decision.get("action") == "prompt" and approval_id:
            runtime_decision = replace(
                runtime_decision,
                action="allow",
                reason=f"Approved command permission {approval_id}: {runtime_decision.reason}",
            )
        payload = self.exec_runtime_service.start(
            workspace_id=workspace_id,
            command=command,
            decision=runtime_decision,
            policy_evaluation=evaluation,
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=linked_run_id,
            timeout_seconds=timeout,
            managed=managed,
        )
        self.exec_policy_service.append_audit_record(
            self.store,
            workspace_id=workspace_id,
            command=command,
            evaluation=evaluation,
            run_id=linked_run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            process_id=payload.get("process_id"),
            source="command.exec.start",
            outcome=str(payload.get("status") or "started"),
        )
        if linked_run_id:
            self._record_run_tool_event(
                linked_run_id,
                tool_envelope(
                    tool="shell.exec",
                    input_payload={"command": self.exec_policy_service.redact(command)},
                    result={key: payload.get(key) for key in ("status", "process_id", "timeout_seconds", "managed", "lifecycle")},
                    risk=decision.get("risk") or "read_only",
                    approval={"required": False, "status": "approved" if approval_id else "not_required", "approval_id": approval_id},
                ),
            )
        if thread_id:
            self._append_item(thread_id, turn_id, "command.exec.started", payload)
        return payload

    def write_exec(self, process_id: str, data: str) -> dict[str, Any]:
        return self.exec_runtime_service.write(process_id, data)

    def resize_exec(self, process_id: str, *, cols: int | None = None, rows: int | None = None) -> dict[str, Any]:
        return self.exec_runtime_service.resize(process_id, cols=cols, rows=rows)

    def terminate_exec(self, process_id: str) -> dict[str, Any]:
        return self.exec_runtime_service.terminate(process_id)

    def read_exec_output(self, process_id: str, *, stream: str = "stdout", start: int | None = None, end: int | None = None) -> dict[str, Any]:
        return self.exec_runtime_service.read_output(process_id, stream=stream, start=start, end=end)

    def _record_run_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": self._now().isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)
        if self.event_journal_service is not None:
            try:
                run = self.run_service.get_run(run_id)
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                status = str(result.get("status") or item.get("status") or "completed").lower()
                event_type = "tool.failed" if status in {"failed", "blocked", "error"} else "tool.completed"
                base_payload = {
                    **item,
                    "session_id": run.session_id,
                    "run_id": run_id,
                    "turn_id": self._turn_id_for_run(run),
                    "tool_call_id": item.get("tool_call_id"),
                    "refs": {
                        "output_ref": item.get("stdout_ref") or item.get("output_ref"),
                        "stderr_ref": item.get("stderr_ref"),
                        "artifact_refs": item.get("artifacts") or [],
                    },
                }
                self.event_journal_service.append_run(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    event_type="tool.requested",
                    actor=f"tool:{item.get('tool') or 'unknown'}",
                    payload={k: v for k, v in base_payload.items() if k != "result"},
                    summary=str(item.get("tool") or "Tool requested"),
                    idempotency_key=f"thread_tool_requested:{run_id}:{item.get('tool_call_id') or item.get('sequence')}",
                )
                self.event_journal_service.append_run(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    event_type=event_type,
                    actor=f"tool:{item.get('tool') or 'unknown'}",
                    payload=base_payload,
                    summary=str(item.get("tool") or "Tool event"),
                    idempotency_key=f"thread_tool:{run_id}:{item.get('tool_call_id') or item.get('sequence')}",
                )
            except Exception:
                pass

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

    def _turn_id_for_run(self, run: Any) -> str | None:
        session_id = str(getattr(run, "session_id", "") or "")
        if not session_id:
            return None
        for turn in self.db.list_turns(session_id, limit=500):
            if turn.linked_run_id == getattr(run, "run_id", None):
                return turn.turn_id
        return None

    def _ensure_monitor(self, thread_id: str, turn_id: str, run_id: str) -> None:
        existing = self._monitors.get(turn_id)
        if existing is not None and existing.is_alive():
            return
        worker = threading.Thread(target=self._monitor_run, args=(thread_id, turn_id, run_id), daemon=True)
        self._monitors[turn_id] = worker
        worker.start()

    def _monitor_run(self, thread_id: str, turn_id: str, run_id: str) -> None:
        seen_activity = 0
        last_stage = ""
        last_live_write = 0.0
        while True:
            try:
                run = self.run_service.get_run(run_id)
            except Exception as exc:
                self._finish_turn(thread_id, turn_id, "failed", {"error": str(exc)})
                return
            stage_changed = run.current_stage != last_stage
            if stage_changed:
                last_stage = run.current_stage
                self._append_item(
                    thread_id,
                    turn_id,
                    "item.tool.progress",
                    {"stage": run.current_stage, "progress_percent": run.progress_percent, "run_id": run.run_id},
                    notify_method="item/tool/progress",
                )
                self._journal_run_protocol(
                    run.run_id,
                    "run.stage_changed",
                    {"session_id": thread_id, "turn_id": turn_id, "run_id": run.run_id, "status": run.status, "stage": run.current_stage, "progress_percent": run.progress_percent},
                    summary=run.current_stage,
                    idempotency_key=f"run.stage:{run.run_id}:{run.current_stage}:{run.progress_percent}",
                )
            activity = list(run.agent_activity_events or [])
            for offset, event in enumerate(activity[seen_activity:], start=seen_activity + 1):
                self._append_item(thread_id, turn_id, "agent.activity", {"run_id": run.run_id, **dict(event)}, notify_method="item/tool/progress")
                self._journal_run_protocol(
                    run.run_id,
                    "artifact.created" if dict(event).get("artifact_ref") else "tool.completed",
                    {"session_id": thread_id, "turn_id": turn_id, "run_id": run.run_id, **dict(event)},
                    summary=str(dict(event).get("message") or dict(event).get("event_type") or "Agent activity"),
                    idempotency_key=f"agent.activity:{run.run_id}:{offset}",
                )
            seen_activity = len(activity)
            now = time.monotonic()
            if run.status not in TERMINAL_RUN_STATUSES and (stage_changed or now - last_live_write >= LIVE_WRITER_INTERVAL_SECONDS):
                self._write_live_snapshot(thread_id, turn_id, run, reason="monitor")
                last_live_write = now
            if run.status in TERMINAL_RUN_STATUSES:
                status = "completed" if run.status == "completed" else "failed"
                self._journal_run_terminal(thread_id=thread_id, turn_id=turn_id, run=run)
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
        self._refresh_stable_metadata(thread, reason=f"turn.{status}")
        self.db.upsert_thread(thread)
        self._append_item(thread_id, turn_id, f"turn.{status}", payload)
        self._append_event(thread_id, turn_id, f"turn.{status}", payload)
        self._journal_thread_event(
            thread_id,
            turn_id,
            f"turn.{status}",
            {"session_id": thread_id, "turn_id": turn_id, "status": status, **payload},
            actor="system",
            source_ref=turn_id,
            idempotency_key=f"turn.{status}:{turn_id}",
        )
        self.event_hub.publish(f"turn/{status}", {"thread_id": thread_id, "turn": turn.model_dump(mode="json"), **payload})

    def _resume_live_writer(self, thread_id: str) -> None:
        thread = self._get_thread(thread_id)
        if not thread.current_turn_id:
            return
        turn = self.db.get_turn(thread.current_turn_id)
        if turn is None or turn.status != "running" or not turn.linked_run_id:
            return
        try:
            run = self.run_service.get_run(turn.linked_run_id)
        except Exception:
            return
        if run.status not in TERMINAL_RUN_STATUSES:
            self._write_live_snapshot(thread_id, turn.turn_id, run, reason="resume")
            self._ensure_monitor(thread_id, turn.turn_id, run.run_id)

    def _write_live_snapshot(self, thread_id: str, turn_id: str | None, run: Any, *, reason: str) -> dict[str, Any]:
        snapshot = {
            "schema": "grounded.thread_live_writer.v1",
            "reason": reason,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "run": self._run_live_payload(run),
            "created_at": self._now().isoformat(),
        }
        if not self._should_write_live_snapshot(thread_id, turn_id, reason=reason):
            return snapshot
        self._append_item(thread_id, turn_id, "run.live_snapshot", snapshot, notify_method="item/tool/progress")
        self._append_event(thread_id, turn_id, "run.live_snapshot", snapshot)
        self._journal_run_protocol(
            str(run.run_id),
            "run.snapshot",
            {"session_id": thread_id, "turn_id": turn_id, "run_id": str(run.run_id), "snapshot": snapshot, "status": str(run.status), "stage": str(getattr(run, "current_stage", ""))},
            summary=f"Run snapshot: {getattr(run, 'current_stage', '')}",
            idempotency_key=f"run.snapshot:{run.run_id}:{reason}:{snapshot['created_at']}",
        )
        try:
            self.db.insert_run_state_snapshot(run_id=str(run.run_id), reason=f"thread_live:{reason}", payload=snapshot)
        except Exception:
            pass
        return snapshot

    def _should_write_live_snapshot(self, thread_id: str, turn_id: str | None, *, reason: str) -> bool:
        if reason in {"manual", "test", "turn.start", "resume"}:
            return True
        events = self.db.list_events(thread_id, limit=50)
        for event in reversed(events):
            if event.event_type != "run.live_snapshot" or event.turn_id != turn_id:
                continue
            elapsed = (self._now() - event.created_at).total_seconds()
            return elapsed >= LIVE_WRITER_INTERVAL_SECONDS
        return True

    def _refresh_stable_metadata(self, thread: ThreadRecord, *, reason: str) -> dict[str, Any]:
        persisted = self.db.get_thread(thread.thread_id)
        turns = self.db.list_turns(thread.thread_id, limit=500) if persisted else []
        items = self.db.list_items(thread.thread_id, limit=1000) if persisted else []
        events = self.db.list_events(thread.thread_id, limit=1000) if persisted else []
        latest_turn = turns[-1] if turns else None
        latest_run_id = latest_turn.linked_run_id if latest_turn else None
        metadata = dict(thread.metadata or {})
        metadata["stable_thread"] = {
            "schema": "grounded.thread_metadata.v1",
            "thread_id": thread.thread_id,
            "workspace_id": thread.workspace_id,
            "title": thread.title,
            "status": thread.status,
            "archived": thread.archived,
            "current_turn_id": thread.current_turn_id,
            "forked_from_thread_id": thread.forked_from_thread_id,
            "latest_turn_id": latest_turn.turn_id if latest_turn else None,
            "latest_run_id": latest_run_id,
            "turn_count": len(turns),
            "item_count": len(items),
            "event_count": len(events),
            "last_event_sequence": events[-1].sequence if events else 0,
            "last_item_sequence": items[-1].sequence if items else 0,
            "updated_reason": reason,
            "updated_at": self._now().isoformat(),
        }
        thread.metadata = metadata
        return metadata["stable_thread"]

    @staticmethod
    def _run_live_payload(run: Any) -> dict[str, Any]:
        return {
            "run_id": str(run.run_id),
            "status": str(run.status),
            "apply_status": str(getattr(run, "apply_status", "")),
            "current_stage": str(getattr(run, "current_stage", "")),
            "progress_percent": int(getattr(run, "progress_percent", 0) or 0),
            "iteration_count": int(getattr(run, "iteration_count", 0) or 0),
            "summary": getattr(run, "summary", None),
            "failure_reason": getattr(run, "failure_reason", None),
            "touched_files": list(getattr(run, "touched_files", []) or [])[:50],
            "active_tool_uses": list(getattr(run, "active_tool_uses", []) or [])[:20],
        }

    def _append_item(self, thread_id: str, turn_id: str | None, item_type: str, payload: dict[str, Any], *, notify_method: str | None = None) -> ItemRecord:
        item = self.db.append_item(ItemRecord(thread_id=thread_id, turn_id=turn_id, item_type=item_type, payload=payload))
        self._journal_thread_event(
            thread_id,
            turn_id,
            f"item.{item_type}",
            item.model_dump(mode="json"),
            actor=self._actor_for_item(item_type),
            source_ref=item.item_id,
            idempotency_key=f"item:{item.item_id}",
        )
        self.event_hub.publish(notify_method or "item/completed", {"thread_id": thread_id, "turn_id": turn_id, "item": item.model_dump(mode="json")})
        return item

    def _append_event(self, thread_id: str, turn_id: str | None, event_type: str, payload: dict[str, Any]) -> RolloutEventRecord:
        event = self.db.append_event(RolloutEventRecord(thread_id=thread_id, turn_id=turn_id, event_type=event_type, payload=payload))
        self._journal_thread_event(
            thread_id,
            turn_id,
            event_type,
            event.model_dump(mode="json"),
            actor="system",
            source_ref=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )
        return event

    def _journal_thread_event(
        self,
        thread_id: str,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if self.event_journal_service is None:
            return
        try:
            thread = self._get_thread(thread_id)
            self.event_journal_service.append_thread(
                workspace_id=thread.workspace_id,
                thread_id=thread_id,
                turn_id=turn_id,
                run_id=self._run_id_from_payload(payload),
                event_type=event_type,
                actor=actor,
                payload=payload,
                summary=event_type,
                source_ref=source_ref,
                idempotency_key=idempotency_key,
            )
        except Exception:
            pass

    def _journal_run_protocol(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if self.event_journal_service is None:
            return
        try:
            run = self.run_service.get_run(run_id)
            self.event_journal_service.append_run(
                workspace_id=run.workspace_id,
                run_id=run_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                summary=summary or event_type,
                source_ref=source_ref,
                idempotency_key=idempotency_key,
            )
        except Exception:
            pass

    def _journal_run_terminal(self, *, thread_id: str, turn_id: str, run: Any) -> None:
        refs = {
            "run_artifacts": f"run_artifacts:{run.run_id}",
            "latest_check_ref": f"latest_check_execution:{run.run_id}",
            "browser_proof_ref": getattr(run, "browser_proof_ref", None),
            "trace_bundle_ref": getattr(run, "trace_bundle_ref", None),
            "memory_ref": f"memory_stage1:{run.workspace_id}:{run.run_id}",
        }
        self._journal_run_protocol(
            run.run_id,
            "proof.completed",
            {"session_id": thread_id, "turn_id": turn_id, "run_id": run.run_id, "status": run.status, "refs": refs},
            summary="Run proof completed.",
            idempotency_key=f"proof.completed:{run.run_id}",
        )
        self._journal_run_protocol(
            run.run_id,
            "run.completed",
            {"session_id": thread_id, "turn_id": turn_id, "run_id": run.run_id, "status": run.status, "apply_status": run.apply_status, "stage": run.current_stage, "refs": refs},
            summary=getattr(run, "summary", None) or getattr(run, "failure_reason", None) or f"Run {run.status}.",
            idempotency_key=f"run.completed:{run.run_id}",
        )
        self._journal_thread_event(
            thread_id,
            turn_id,
            "memory.updated",
            {"session_id": thread_id, "turn_id": turn_id, "run_id": run.run_id, "status": "recorded", "refs": {"memory_ref": refs["memory_ref"]}},
            actor="system",
            source_ref=refs["memory_ref"],
            idempotency_key=f"memory.updated:{turn_id}:{run.run_id}",
        )

    @staticmethod
    def _actor_for_item(item_type: str) -> str:
        if item_type.startswith("user."):
            return "user"
        if item_type.startswith("agent."):
            return "agent"
        if item_type.startswith("command.") or item_type.startswith("tool."):
            return "tool:thread"
        return "system"

    @staticmethod
    def _run_id_from_payload(payload: dict[str, Any]) -> str | None:
        value = payload.get("run_id")
        if isinstance(value, str) and value.strip():
            return value
        nested_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        value = nested_payload.get("run_id")
        if isinstance(value, str) and value.strip():
            return value
        run = nested_payload.get("run") if isinstance(nested_payload.get("run"), dict) else payload.get("run") if isinstance(payload.get("run"), dict) else None
        if run and isinstance(run.get("run_id"), str):
            return str(run["run_id"])
        return None

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
