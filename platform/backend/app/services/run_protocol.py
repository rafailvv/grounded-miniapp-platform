from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal
from uuid import uuid4

from app.models.domain import RunRecord
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore


RUN_PROTOCOL_EVENT_SCHEMA = "grounded.run_protocol_event.v1"
RUN_BOOKMARK_SCHEMA = "grounded.run_bookmark.v1"

ProtocolEventType = Literal[
    "session_configured",
    "run_started",
    "turn_started",
    "model_delta",
    "tool_requested",
    "tool_completed",
    "approval_requested",
    "check_started",
    "repair_started",
    "compact_boundary",
    "turn_completed",
    "run_completed",
]

ProtocolStatus = Literal["started", "running", "completed", "failed", "blocked", "skipped"]


class RunProtocolConflict(ValueError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("message") or payload.get("reason") or "Run protocol conflict"))


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def diff_sha256(diff_text: str | None) -> str | None:
    normalized = str(diff_text or "")
    if not normalized.strip():
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RunProtocolService:
    """Canonical run protocol overlay stored in existing run_events and reports."""

    def __init__(self, db: PlatformDb, store: StateStore) -> None:
        self.db = db
        self.store = store

    def append_event(
        self,
        *,
        run_id: str,
        workspace_id: str,
        event_type: ProtocolEventType,
        status: ProtocolStatus = "running",
        session_id: str | None = None,
        task_id: str | None = None,
        turn_id: str | None = None,
        message: str = "",
        payload: dict[str, Any] | None = None,
        refs: dict[str, Any] | None = None,
        bookmark_id: str | None = None,
        source_event_type: str | None = None,
    ) -> dict[str, Any]:
        protocol_payload = {
            "schema": RUN_PROTOCOL_EVENT_SCHEMA,
            "event_id": f"proto_evt_{uuid4().hex}",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "session_id": session_id or workspace_id,
            "task_id": task_id or run_id,
            "turn_id": turn_id,
            "sequence": None,
            "type": event_type,
            "status": status,
            "message": message,
            "payload": payload or {},
            "refs": refs or {},
            "bookmark_id": bookmark_id,
            "source_event_type": source_event_type,
            "created_at": utc_iso(),
        }
        wrapper = self.db.append_run_event(run_id, f"run_protocol.{event_type}", protocol_payload)
        sequence = int(wrapper.get("sequence") or 0)
        event_id = str(wrapper.get("event_id") or protocol_payload["event_id"])
        protocol_payload["sequence"] = sequence
        protocol_payload["event_id"] = event_id
        wrapper["payload"] = protocol_payload
        return wrapper

    def append_once_terminal(self, run: RunRecord, *, source_event_type: str | None = None) -> dict[str, Any] | None:
        for item in self.protocol_events(run.run_id, limit=5000)["items"]:
            if item.get("type") == "run_completed":
                return None
        status: ProtocolStatus = "completed" if run.status in {"completed", "awaiting_approval"} else "blocked" if run.status == "blocked" else "failed"
        return self.append_event(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            session_id=run.session_id,
            event_type="run_completed",
            status=status,
            message=run.summary or run.failure_reason or f"Run finished with status {run.status}.",
            payload={
                "run_status": run.status,
                "apply_status": run.apply_status,
                "outcome_kind": run.outcome_kind,
                "failure_class": run.failure_class,
                "failure_signature": run.failure_signature,
            },
            refs={
                "trace_bundle_ref": run.trace_bundle_ref,
                "resume_checkpoint_ref": run.resume_checkpoint_ref,
                "latest_check_ref": f"latest_check_execution:{run.run_id}",
            },
            source_event_type=source_event_type,
        )

    def record_from_source_event(
        self,
        *,
        run_id: str,
        workspace_id: str,
        session_id: str | None,
        source_event_type: str,
        message: str,
        details: dict[str, Any],
        job_status: str | None = None,
    ) -> dict[str, Any] | None:
        event_type: ProtocolEventType | None = None
        status: ProtocolStatus = "running"
        if source_event_type == "tool_use_summary":
            event_type = "tool_completed"
            status = "completed"
        elif source_event_type in {"running_checks", "build_started", "frontend_build_started", "backend_compile_started", "final_checks_started", "preview_validation_started"} or "check_step" in details:
            event_type = "check_started"
            check_status = str(details.get("check_status") or details.get("status") or "").lower()
            status = "failed" if check_status == "failed" else "started"
        elif source_event_type in {"repair_started", "repair_iteration"}:
            event_type = "repair_started"
            status = "started"
        elif source_event_type == "compact_boundary":
            event_type = "compact_boundary"
            status = "completed"
        elif source_event_type == "job_started":
            event_type = "run_started"
            status = "started"
        elif source_event_type in {"job_completed", "job_failed"}:
            event_type = "run_completed"
            status = "completed" if source_event_type == "job_completed" else "failed"
        if event_type is None:
            return None
        if event_type in {"run_completed", "compact_boundary"}:
            for item in self.protocol_events(run_id, limit=5000)["items"]:
                if item.get("type") == event_type:
                    return None
        return self.append_event(
            run_id=run_id,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type=event_type,
            status=status,
            turn_id=self._turn_id_from_details(details),
            message=message,
            payload={"details": details, "job_status": job_status},
            refs=self._refs_from_details(details),
            source_event_type=source_event_type,
        )

    def protocol_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 1000) -> dict[str, Any]:
        raw = self.db.list_run_events(run_id, after_sequence=after_sequence, limit=limit)
        items: list[dict[str, Any]] = []
        for wrapper in raw:
            payload = wrapper.get("payload") if isinstance(wrapper.get("payload"), dict) else {}
            if payload.get("schema") != RUN_PROTOCOL_EVENT_SCHEMA and not str(wrapper.get("event_type") or "").startswith("run_protocol."):
                continue
            item = dict(payload)
            item.setdefault("event_id", wrapper.get("event_id"))
            item.setdefault("run_id", wrapper.get("run_id"))
            item["sequence"] = int(wrapper.get("sequence") or item.get("sequence") or 0)
            item.setdefault("created_at", wrapper.get("created_at"))
            items.append(item)
        return {
            "schema": "grounded.run_protocol.v1",
            "run_id": run_id,
            "status": "ok",
            "items": items,
            "next_sequence": max([int(item.get("sequence") or 0) for item in items], default=int(after_sequence or 0)),
        }

    def create_bookmark(
        self,
        *,
        run_id: str,
        workspace_id: str,
        turn_id: str,
        response_id: str | None,
        checkpoint_ref: str | None,
        trace_bundle_ref: str | None,
        diff_sha256_value: str | None,
        tool_result_count: int = 0,
        latest_check_ref: str | None = None,
        todo_state_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bookmark = {
            "schema": RUN_BOOKMARK_SCHEMA,
            "bookmark_id": f"bookmark_{uuid4().hex}",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "turn_id": turn_id,
            "response_id": response_id or None,
            "checkpoint_ref": checkpoint_ref or None,
            "trace_bundle_ref": trace_bundle_ref or None,
            "diff_sha256": diff_sha256_value,
            "tool_result_count": int(tool_result_count or 0),
            "latest_check_ref": latest_check_ref or None,
            "todo_state_ref": todo_state_ref or None,
            "metadata": metadata or {},
            "created_at": utc_iso(),
        }
        index = self._bookmark_index(run_id)
        items = [item for item in index.get("items") or [] if isinstance(item, dict)]
        items.append(bookmark)
        index["items"] = items
        index["updated_at"] = utc_iso()
        self.store.upsert("reports", self._bookmark_key(run_id), index)
        return bookmark

    def bookmarks(self, run_id: str) -> dict[str, Any]:
        index = self._bookmark_index(run_id)
        items = [item for item in index.get("items") or [] if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"schema": "grounded.run_bookmarks.v1", "run_id": run_id, "status": "ok", "items": items}

    def get_bookmark(self, run_id: str, bookmark_id: str) -> dict[str, Any]:
        for bookmark in self.bookmarks(run_id)["items"]:
            if str(bookmark.get("bookmark_id") or "") == bookmark_id:
                return dict(bookmark)
        raise KeyError(f"Bookmark not found: {bookmark_id}")

    def validate_bookmark(self, run: RunRecord, bookmark: dict[str, Any], *, current_diff_sha256: str | None) -> None:
        checkpoint_ref = str(bookmark.get("checkpoint_ref") or "").strip()
        if checkpoint_ref and not isinstance(self.store.get("reports", checkpoint_ref), dict):
            raise RunProtocolConflict(
                {
                    "schema": "grounded.run_bookmark_conflict.v1",
                    "reason": "missing_checkpoint",
                    "message": "Bookmark checkpoint is missing.",
                    "run_id": run.run_id,
                    "bookmark_id": bookmark.get("bookmark_id"),
                    "checkpoint_ref": checkpoint_ref,
                }
            )
        expected_diff = str(bookmark.get("diff_sha256") or "").strip()
        actual_diff = str(current_diff_sha256 or "").strip()
        if expected_diff and expected_diff != actual_diff:
            raise RunProtocolConflict(
                {
                    "schema": "grounded.run_bookmark_conflict.v1",
                    "reason": "stale_diff",
                    "message": "Bookmark diff does not match the current draft diff.",
                    "run_id": run.run_id,
                    "bookmark_id": bookmark.get("bookmark_id"),
                    "expected_diff_sha256": expected_diff,
                    "actual_diff_sha256": actual_diff or None,
                }
            )

    def _bookmark_key(self, run_id: str) -> str:
        return f"run_bookmarks:{run_id}"

    def _bookmark_index(self, run_id: str) -> dict[str, Any]:
        existing = self.store.get("reports", self._bookmark_key(run_id))
        if isinstance(existing, dict):
            return dict(existing)
        return {"schema": "grounded.run_bookmarks.v1", "run_id": run_id, "items": [], "created_at": utc_iso()}

    @staticmethod
    def _turn_id_from_details(details: dict[str, Any]) -> str | None:
        attempt = details.get("attempt")
        tool_round = details.get("tool_round")
        if attempt is None and tool_round is None:
            return None
        return f"turn_{int(attempt or 0)}_{int(tool_round or 0)}"

    @staticmethod
    def _refs_from_details(details: dict[str, Any]) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        for key in ("artifact_ref", "checkpoint_ref", "trace_bundle_ref", "latest_check_ref"):
            if details.get(key):
                refs[key] = details.get(key)
        return refs
