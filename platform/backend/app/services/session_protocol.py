from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.domain import RunRecord
from app.models.threads import ThreadRecord, TurnRecord
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.run_protocol import RunProtocolService


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionProtocolReducer:
    """Canonical protocol view over existing thread, run, journal, and artifact stores."""

    def __init__(
        self,
        *,
        db: PlatformDb,
        store: StateStore,
        event_journal_service: EventJournalService,
        run_protocol_service: RunProtocolService | None = None,
    ) -> None:
        self.db = db
        self.store = store
        self.event_journal_service = event_journal_service
        self.run_protocol_service = run_protocol_service

    def session_protocol(self, session_id: str) -> dict[str, Any]:
        thread = self._thread(session_id)
        turns = self.db.list_turns(thread.thread_id, limit=500)
        runs = self._runs_for_session(thread.thread_id, turns)
        timeline = self._session_timeline(thread=thread, turns=turns, runs=runs)
        bookmarks = self._bookmarks_for_runs(runs)
        artifacts = self._artifact_refs(runs=runs, timeline=timeline)
        proofs = self._proof_refs(runs=runs, timeline=timeline)
        resume = self._resume(candidates=self._resume_candidates(thread=thread, turns=turns, runs=runs, bookmarks=bookmarks))
        return {
            "schema": "grounded.session_protocol.v1",
            "status": "ok",
            "session_id": thread.thread_id,
            "thread_id": thread.thread_id,
            "workspace_id": thread.workspace_id,
            "session": self._thread_payload(thread),
            "turns": [self._turn_payload(turn) for turn in turns],
            "linked_runs": [self._run_payload(run) for run in runs],
            "timeline": timeline,
            "artifacts": artifacts,
            "proofs": proofs,
            "bookmarks": bookmarks,
            "latest_bookmark": bookmarks[0] if bookmarks else None,
            "resume": resume,
            "failure_point": self._failure_point(runs=runs, timeline=timeline),
            "next_sequence": self._next_sequence(timeline),
        }

    def turn_protocol(self, turn_id: str) -> dict[str, Any]:
        turn = self._turn(turn_id)
        session = self.session_protocol(turn.thread_id)
        timeline = [item for item in session["timeline"] if item.get("turn_id") == turn.turn_id or item.get("run_id") == turn.linked_run_id]
        bookmarks = [item for item in session["bookmarks"] if item.get("turn_id") == turn.turn_id or item.get("run_id") == turn.linked_run_id]
        run_ids = {str(turn.linked_run_id or "")}
        return {
            "schema": "grounded.turn_protocol.v1",
            "status": "ok",
            "session_id": turn.thread_id,
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "workspace_id": turn.workspace_id,
            "turns": [self._turn_payload(turn)],
            "timeline": timeline,
            "artifacts": [item for item in session["artifacts"] if not item.get("run_id") or item.get("run_id") in run_ids],
            "proofs": [item for item in session["proofs"] if not item.get("run_id") or item.get("run_id") in run_ids],
            "bookmarks": bookmarks,
            "resume": self._resume(candidates=[item for item in session["resume"].get("candidates", []) if item.get("turn_id") == turn.turn_id or item.get("run_id") == turn.linked_run_id]),
            "next_sequence": self._next_sequence(timeline),
        }

    def run_protocol(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        turns = self._turns_for_run(run)
        timeline = self._run_timeline(run=run, turns=turns)
        bookmarks = self._bookmarks_for_runs([run])
        return {
            "schema": "grounded.run_protocol.v2",
            "status": "ok",
            "session_id": run.session_id,
            "thread_id": run.session_id,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "turns": [self._turn_payload(turn) for turn in turns],
            "timeline": timeline,
            "artifacts": self._artifact_refs(runs=[run], timeline=timeline),
            "proofs": self._proof_refs(runs=[run], timeline=timeline),
            "bookmarks": bookmarks,
            "latest_bookmark": bookmarks[0] if bookmarks else None,
            "resume": self._resume(candidates=self._resume_candidates(thread=None, turns=turns, runs=[run], bookmarks=bookmarks)),
            "next_sequence": self._next_sequence(timeline),
            "items": self._legacy_protocol_items(run.run_id),
        }

    def backfill_session_view(self, session_id: str) -> dict[str, Any]:
        payload = self.session_protocol(session_id)
        payload["backfill"] = {"mode": "view_only", "writes": False}
        return payload

    def _session_timeline(self, *, thread: ThreadRecord, turns: list[TurnRecord], runs: list[RunRecord]) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        for event in self.event_journal_service.list_thread(thread.thread_id, limit=5000):
            timeline.append(self._timeline_from_thread_event(event))
        if not timeline:
            for event in self.db.list_events(thread.thread_id, limit=5000):
                payload = event.model_dump(mode="json")
                timeline.append(
                    {
                        "sequence": event.sequence,
                        "source": "legacy_thread_event",
                        "subject": "turn" if event.turn_id else "event",
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "status": self._status_from_event(event.event_type, payload.get("payload") or {}),
                        "actor": "legacy",
                        "summary": event.event_type,
                        "session_id": thread.thread_id,
                        "thread_id": thread.thread_id,
                        "turn_id": event.turn_id,
                        "run_id": self._payload_run_id(payload.get("payload") or {}),
                        "payload": payload.get("payload") or {},
                        "refs": {},
                        "created_at": event.created_at.isoformat(),
                    }
                )
        for run in runs:
            timeline.extend(self._run_timeline(run=run, turns=[turn for turn in turns if turn.linked_run_id == run.run_id]))
        return self._sort_timeline(timeline)

    def _run_timeline(self, *, run: RunRecord, turns: list[TurnRecord]) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        for event in self.event_journal_service.list_run(run.run_id, limit=5000):
            timeline.append(self._timeline_from_run_event(event, run=run))
        if self.run_protocol_service is not None:
            for event in self._legacy_protocol_items(run.run_id):
                timeline.append(self._timeline_from_protocol_event(event, run=run))
        if not timeline:
            timeline.append(
                {
                    "sequence": 0,
                    "source": "run_record",
                    "subject": "run",
                    "event_id": None,
                    "event_type": "run.snapshot",
                    "status": run.status,
                    "actor": "system",
                    "summary": run.summary or run.failure_reason or run.current_stage,
                    "session_id": run.session_id,
                    "thread_id": run.session_id,
                    "turn_id": turns[0].turn_id if turns else None,
                    "run_id": run.run_id,
                    "payload": self._run_payload(run),
                    "refs": self._run_refs(run),
                    "created_at": run.updated_at.isoformat(),
                }
            )
        return self._sort_timeline(timeline)

    def _timeline_from_thread_event(self, event: Any) -> dict[str, Any]:
        payload = self._payload(event.payload_ref)
        return {
            "sequence": event.sequence,
            "source": "thread_events_v2",
            "subject": self._subject(event.event_type),
            "event_id": event.event_id,
            "event_type": event.event_type,
            "status": self._status_from_event(event.event_type, payload),
            "actor": event.actor,
            "summary": event.summary,
            "session_id": event.thread_id,
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "run_id": event.run_id or self._payload_run_id(payload),
            "tool_call_id": self._payload_tool_call_id(payload),
            "artifact_ref": self._payload_artifact_ref(payload),
            "proof_ref": self._payload_proof_ref(payload),
            "payload_ref": event.payload_ref,
            "payload_sha256": event.payload_sha256,
            "refs": self._payload_refs(payload),
            "payload": payload,
            "created_at": event.created_at,
        }

    def _timeline_from_run_event(self, event: Any, *, run: RunRecord) -> dict[str, Any]:
        payload = self._payload(event.payload_ref)
        return {
            "sequence": event.sequence,
            "source": "run_events_v2",
            "subject": self._subject(event.event_type),
            "event_id": event.event_id,
            "event_type": event.event_type,
            "status": self._status_from_event(event.event_type, payload),
            "actor": event.actor,
            "summary": event.summary,
            "session_id": run.session_id,
            "thread_id": run.session_id,
            "turn_id": self._payload_turn_id(payload),
            "run_id": run.run_id,
            "tool_call_id": self._payload_tool_call_id(payload),
            "artifact_ref": self._payload_artifact_ref(payload),
            "proof_ref": self._payload_proof_ref(payload),
            "payload_ref": event.payload_ref,
            "payload_sha256": event.payload_sha256,
            "refs": {**self._run_refs(run), **self._payload_refs(payload)},
            "payload": payload,
            "created_at": event.created_at,
        }

    def _timeline_from_protocol_event(self, event: dict[str, Any], *, run: RunRecord) -> dict[str, Any]:
        return {
            "sequence": event.get("sequence"),
            "source": "legacy_run_protocol",
            "subject": self._subject(str(event.get("type") or "")),
            "event_id": event.get("event_id"),
            "event_type": f"protocol.{event.get('type')}",
            "status": str(event.get("status") or "completed"),
            "actor": "system",
            "summary": str(event.get("message") or event.get("type") or ""),
            "session_id": event.get("session_id") or run.session_id,
            "thread_id": event.get("session_id") or run.session_id,
            "turn_id": event.get("turn_id"),
            "run_id": run.run_id,
            "refs": event.get("refs") if isinstance(event.get("refs"), dict) else {},
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            "created_at": str(event.get("created_at") or run.updated_at.isoformat()),
        }

    def _thread(self, session_id: str) -> ThreadRecord:
        thread = self.db.get_thread(session_id)
        if thread is None:
            raise KeyError(f"Session not found: {session_id}")
        return thread

    def _turn(self, turn_id: str) -> TurnRecord:
        turn = self.db.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"Turn not found: {turn_id}")
        return turn

    def _run(self, run_id: str) -> RunRecord:
        payload = self.store.get("runs", run_id)
        if not isinstance(payload, dict):
            raise KeyError(f"Run not found: {run_id}")
        return RunRecord.model_validate(payload)

    def _runs_for_session(self, session_id: str, turns: list[TurnRecord]) -> list[RunRecord]:
        run_ids = [turn.linked_run_id for turn in turns if turn.linked_run_id]
        runs: list[RunRecord] = []
        seen: set[str] = set()
        for payload in self.store.list("runs"):
            if not isinstance(payload, dict):
                continue
            if payload.get("session_id") != session_id and payload.get("run_id") not in run_ids:
                continue
            run = RunRecord.model_validate(payload)
            if run.run_id not in seen:
                seen.add(run.run_id)
                runs.append(run)
        runs.sort(key=lambda item: item.created_at)
        return runs

    def _turns_for_run(self, run: RunRecord) -> list[TurnRecord]:
        if not run.session_id:
            return []
        return [turn for turn in self.db.list_turns(run.session_id, limit=500) if turn.linked_run_id == run.run_id]

    def _payload(self, payload_ref: str | None) -> dict[str, Any]:
        if not payload_ref:
            return {}
        record = self.event_journal_service.read_payload(payload_ref)
        return dict(record.payload) if record is not None else {}

    def _bookmarks_for_runs(self, runs: list[RunRecord]) -> list[dict[str, Any]]:
        if self.run_protocol_service is None:
            return []
        items: list[dict[str, Any]] = []
        for run in runs:
            for bookmark in self.run_protocol_service.bookmarks(run.run_id).get("items") or []:
                if not isinstance(bookmark, dict):
                    continue
                item = dict(bookmark)
                item.setdefault("session_id", run.session_id)
                item.setdefault("run_id", run.run_id)
                items.append(item)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def _legacy_protocol_items(self, run_id: str) -> list[dict[str, Any]]:
        if self.run_protocol_service is None:
            return []
        return [item for item in self.run_protocol_service.protocol_events(run_id, limit=5000).get("items") or [] if isinstance(item, dict)]

    def _artifact_refs(self, *, runs: list[RunRecord], timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for run in runs:
            for key, value in self._run_refs(run).items():
                if value and key not in {"browser_proof", "latest_check", "memory_stage1"}:
                    refs.append({"kind": key, "ref": value, "run_id": run.run_id, "session_id": run.session_id})
        for item in timeline:
            ref = item.get("artifact_ref")
            if ref:
                refs.append({"kind": "event_artifact", "ref": ref, "run_id": item.get("run_id"), "session_id": item.get("session_id"), "event_id": item.get("event_id")})
        return self._dedupe_refs(refs)

    def _proof_refs(self, *, runs: list[RunRecord], timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for run in runs:
            for kind, ref in {
                "latest_check": f"latest_check_execution:{run.run_id}",
                "browser_proof": run.browser_proof_ref,
                "browser_replay_proof": getattr(run, "browser_replay_proof_ref", None),
                "verification_report": run.verification_report_ref,
                "verifier_review": run.verifier_review_ref,
                "trace_bundle": run.trace_bundle_ref,
                "run_artifacts": f"run_artifacts:{run.run_id}",
            }.items():
                if ref:
                    refs.append({"kind": kind, "ref": ref, "run_id": run.run_id, "session_id": run.session_id})
        for item in timeline:
            ref = item.get("proof_ref")
            if ref:
                refs.append({"kind": "event_proof", "ref": ref, "run_id": item.get("run_id"), "session_id": item.get("session_id"), "event_id": item.get("event_id")})
        return self._dedupe_refs(refs)

    def _resume_candidates(
        self,
        *,
        thread: ThreadRecord | None,
        turns: list[TurnRecord],
        runs: list[RunRecord],
        bookmarks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for bookmark in bookmarks:
            candidates.append(
                {
                    "candidate_id": str(bookmark.get("bookmark_id") or ""),
                    "kind": "bookmark",
                    "status": "available",
                    "session_id": bookmark.get("session_id") or (thread.thread_id if thread else None),
                    "turn_id": bookmark.get("turn_id"),
                    "run_id": bookmark.get("run_id"),
                    "bookmark_id": bookmark.get("bookmark_id"),
                    "reason": "Resume from protocol bookmark.",
                    "refs": {
                        "checkpoint_ref": bookmark.get("checkpoint_ref"),
                        "trace_bundle_ref": bookmark.get("trace_bundle_ref"),
                        "latest_check_ref": bookmark.get("latest_check_ref"),
                    },
                    "created_at": bookmark.get("created_at"),
                }
            )
        for run in runs:
            if run.status in {"failed", "blocked"} or run.resume_checkpoint_ref:
                candidates.append(
                    {
                        "candidate_id": f"run:{run.run_id}",
                        "kind": "failed_run" if run.status in {"failed", "blocked"} else "checkpoint",
                        "status": "available",
                        "session_id": run.session_id or (thread.thread_id if thread else None),
                        "turn_id": next((turn.turn_id for turn in turns if turn.linked_run_id == run.run_id), None),
                        "run_id": run.run_id,
                        "reason": run.failure_reason or run.summary or "Resume from latest run checkpoint.",
                        "refs": self._run_refs(run),
                        "created_at": run.updated_at.isoformat(),
                    }
                )
        return candidates

    @staticmethod
    def _resume(*, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "status": "available" if candidates else "none",
            "candidates": candidates,
            "latest": candidates[0] if candidates else None,
        }

    @staticmethod
    def _failure_point(*, runs: list[RunRecord], timeline: list[dict[str, Any]]) -> dict[str, Any]:
        failed = next((run for run in reversed(runs) if run.status in {"failed", "blocked"}), None)
        if failed is None:
            failed_event = next((item for item in reversed(timeline) if item.get("status") in {"failed", "blocked"}), None)
            return failed_event or {}
        return {
            "run_id": failed.run_id,
            "status": failed.status,
            "stage": failed.current_stage,
            "failure_class": failed.failure_class,
            "failure_signature": failed.failure_signature,
            "reason": failed.failure_reason,
            "refs": {
                "resume_checkpoint_ref": failed.resume_checkpoint_ref,
                "trace_bundle_ref": failed.trace_bundle_ref,
                "latest_check_ref": f"latest_check_execution:{failed.run_id}",
            },
        }

    @staticmethod
    def _thread_payload(thread: ThreadRecord) -> dict[str, Any]:
        payload = thread.model_dump(mode="json")
        payload["session_id"] = thread.thread_id
        return payload

    @staticmethod
    def _turn_payload(turn: TurnRecord) -> dict[str, Any]:
        payload = turn.model_dump(mode="json")
        payload["session_id"] = turn.thread_id
        return payload

    @staticmethod
    def _run_payload(run: RunRecord) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "session_id": run.session_id,
            "status": run.status,
            "apply_status": run.apply_status,
            "draft_status": run.draft_status,
            "current_stage": run.current_stage,
            "progress_percent": run.progress_percent,
            "failure_class": run.failure_class,
            "failure_signature": run.failure_signature,
            "context_manager_ref": getattr(run, "context_manager_ref", None),
            "lsp_context_ref": getattr(run, "lsp_context_ref", None),
            "worker_sessions_ref": getattr(run, "worker_sessions_ref", None),
            "worker_ownership_ref": getattr(run, "worker_ownership_ref", None),
            "draft_isolation_ref": getattr(run, "draft_isolation_ref", None),
            "draft_gate_ref": getattr(run, "draft_gate_ref", None),
            "draft_apply_decision_ref": getattr(run, "draft_apply_decision_ref", None),
            "guardian_gate_ref": getattr(run, "guardian_gate_ref", None),
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
        }

    @staticmethod
    def _run_refs(run: RunRecord) -> dict[str, Any]:
        return {
            "run_artifacts": f"run_artifacts:{run.run_id}",
            "resume_checkpoint": run.resume_checkpoint_ref,
            "trace_bundle": run.trace_bundle_ref,
            "trace_reducer": run.trace_reducer_ref,
            "tool_trace": run.tool_trace_ref,
            "prompt_contract": getattr(run, "prompt_contract_ref", None),
            "context_manager": getattr(run, "context_manager_ref", None),
            "lsp_context": getattr(run, "lsp_context_ref", None),
            "worker_sessions": getattr(run, "worker_sessions_ref", None),
            "worker_mailbox": getattr(run, "worker_mailbox_ref", None),
            "worker_ownership": getattr(run, "worker_ownership_ref", None),
            "draft_isolation": getattr(run, "draft_isolation_ref", None),
            "draft_gate": getattr(run, "draft_gate_ref", None),
            "draft_apply_decision": getattr(run, "draft_apply_decision_ref", None),
            "guardian_gate": getattr(run, "guardian_gate_ref", None),
            "context_pressure": run.context_pressure_ref,
            "browser_proof": run.browser_proof_ref,
            "browser_replay_proof": getattr(run, "browser_replay_proof_ref", None),
            "verification_report": run.verification_report_ref,
            "memory_stage1": f"memory_stage1:{run.workspace_id}:{run.run_id}",
        }

    @staticmethod
    def _subject(event_type: str) -> str:
        value = str(event_type or "")
        if "tool." in value or "tool_" in value:
            return "tool_call"
        if "proof" in value or "check." in value or "visual" in value:
            return "proof"
        if "artifact" in value or "bookmark" in value:
            return "artifact"
        if value.startswith("turn.") or "turn_" in value:
            return "turn"
        if value.startswith("run.") or "run_" in value:
            return "run"
        if value.startswith("session.") or value.startswith("thread."):
            return "session"
        if value.startswith("memory."):
            return "memory_update"
        return "event"

    @staticmethod
    def _status_from_event(event_type: str, payload: dict[str, Any]) -> str:
        direct = str(payload.get("status") or "").strip()
        if direct:
            return direct
        text = str(event_type or "").lower()
        if "failed" in text or "failure" in text:
            return "failed"
        if "blocked" in text:
            return "blocked"
        if "started" in text or "requested" in text:
            return "started"
        if "completed" in text or "finished" in text or "created" in text:
            return "completed"
        return "completed"

    @staticmethod
    def _payload_run_id(payload: dict[str, Any]) -> str | None:
        if isinstance(payload.get("run_id"), str):
            return payload["run_id"]
        nested = payload.get("run") if isinstance(payload.get("run"), dict) else {}
        return nested.get("run_id") if isinstance(nested.get("run_id"), str) else None

    @staticmethod
    def _payload_turn_id(payload: dict[str, Any]) -> str | None:
        return payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None

    @staticmethod
    def _payload_tool_call_id(payload: dict[str, Any]) -> str | None:
        return payload.get("tool_call_id") if isinstance(payload.get("tool_call_id"), str) else None

    @staticmethod
    def _payload_artifact_ref(payload: dict[str, Any]) -> str | None:
        for key in ("artifact_ref", "output_ref", "storage_ref"):
            if isinstance(payload.get(key), str):
                return payload[key]
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        return refs.get("artifact_ref") if isinstance(refs.get("artifact_ref"), str) else None

    @staticmethod
    def _payload_proof_ref(payload: dict[str, Any]) -> str | None:
        for key in ("proof_ref", "latest_check_ref", "browser_proof_ref"):
            if isinstance(payload.get(key), str):
                return payload[key]
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        for key in ("proof_ref", "latest_check_ref", "browser_proof_ref"):
            if isinstance(refs.get(key), str):
                return refs[key]
        return None

    @staticmethod
    def _payload_refs(payload: dict[str, Any]) -> dict[str, Any]:
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        return dict(refs)

    @staticmethod
    def _sort_timeline(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: (str(item.get("created_at") or ""), int(item.get("sequence") or 0), str(item.get("event_id") or "")))

    @staticmethod
    def _next_sequence(items: list[dict[str, Any]]) -> int:
        return max([int(item.get("sequence") or 0) for item in items], default=0)

    @staticmethod
    def _dedupe_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            ref = str(item.get("ref") or "")
            if not ref:
                continue
            key = (str(item.get("kind") or ""), ref, str(item.get("run_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result
