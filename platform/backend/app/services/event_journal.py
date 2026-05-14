from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.models.event_journal import (
    EventJournalPayload,
    RunEventV2,
    RunJournalState,
    ThreadEventV2,
    ThreadJournalState,
)
from app.repositories.platform_db import PlatformDb


class EventJournalSecretError(ValueError):
    pass


SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "private_key",
    "openai_api_key",
}
SAFE_TOKEN_KEYS = {
    "token_usage",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "budget_status",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
)


class EventJournalService:
    """Typed append-only event journal facade for run/thread v2 events."""

    def __init__(self, db: PlatformDb) -> None:
        self.db = db

    def append_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunEventV2:
        safe_payload = self._safe_payload(payload or {})
        return self.db.append_run_event_v2(
            workspace_id=workspace_id,
            run_id=run_id,
            event_type=event_type,
            payload=safe_payload,
            actor=actor,
            summary=self._summary(event_type, safe_payload, summary),
            source_ref=source_ref,
            idempotency_key=idempotency_key,
        )

    def append_thread(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> ThreadEventV2:
        safe_payload = self._safe_payload(payload or {})
        linked_run_id = run_id or self._run_id_from_payload(safe_payload)
        return self.db.append_thread_event_v2(
            workspace_id=workspace_id,
            thread_id=thread_id,
            event_type=event_type,
            payload=safe_payload,
            turn_id=turn_id,
            run_id=linked_run_id,
            actor=actor,
            summary=self._summary(event_type, safe_payload, summary),
            source_ref=source_ref,
            idempotency_key=idempotency_key,
        )

    def list_run(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[RunEventV2]:
        return self.db.list_run_events_v2(run_id, after_sequence=after_sequence, limit=limit)

    def list_thread(self, thread_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[ThreadEventV2]:
        return self.db.list_thread_events_v2(thread_id, after_sequence=after_sequence, limit=limit)

    def read_payload(self, payload_ref: str) -> EventJournalPayload | None:
        record = self.db.get_event_payload(payload_ref)
        if record is None:
            return None
        return EventJournalPayload.model_validate(record.model_dump(mode="json"))

    def reduce_run(self, run_id: str) -> RunJournalState:
        return RunEventJournalReducer(self).reduce(run_id)

    def reduce_thread(self, thread_id: str) -> ThreadJournalState:
        return ThreadEventJournalReducer(self).reduce(thread_id)

    def backfill_run(self, *, workspace_id: str, run_id: str, limit: int = 5000) -> list[RunEventV2]:
        created: list[RunEventV2] = []
        for wrapper in self.db.list_run_events(run_id, limit=limit):
            event_id = str(wrapper.get("event_id") or "")
            if not event_id:
                continue
            payload = wrapper.get("payload") if isinstance(wrapper.get("payload"), dict) else {}
            created.append(
                self.append_run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    event_type=str(wrapper.get("event_type") or "legacy.run_event"),
                    payload={"legacy_event": wrapper, "payload": payload},
                    actor="legacy",
                    summary=str(payload.get("message") or wrapper.get("event_type") or "legacy run event"),
                    source_ref=event_id,
                    idempotency_key=f"legacy.run_event:{event_id}",
                )
            )
        return created

    def backfill_thread(self, *, workspace_id: str, thread_id: str, limit: int = 5000) -> list[ThreadEventV2]:
        created: list[ThreadEventV2] = []
        for event in self.db.list_events(thread_id, limit=limit):
            payload = event.model_dump(mode="json")
            created.append(
                self.append_thread(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    turn_id=event.turn_id,
                    event_type=event.event_type,
                    payload={"legacy_event": payload, "payload": payload.get("payload") or {}},
                    actor="legacy",
                    summary=event.event_type,
                    source_ref=event.event_id,
                    idempotency_key=f"legacy.thread_event:{event.event_id}",
                )
            )
        return created

    def _safe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._jsonable(payload)
        if not isinstance(normalized, dict):
            normalized = {"value": normalized}
        self._assert_no_secrets(normalized)
        return self._compact(normalized)

    def _summary(self, event_type: str, payload: dict[str, Any], explicit: str = "") -> str:
        if explicit:
            return explicit[:500]
        for key in ("summary", "message", "reason", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        for key in ("summary", "message", "reason", "status"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return event_type[:500]

    def _jsonable(self, value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return {"value": str(value)}

    def _assert_no_secrets(self, value: Any, *, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key)
                if self._secret_like_key(key_text):
                    raise EventJournalSecretError(f"Secret-like journal payload key rejected at {path}.{key_text}")
                self._assert_no_secrets(nested, path=f"{path}.{key_text}")
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                self._assert_no_secrets(nested, path=f"{path}[{index}]")
            return
        if isinstance(value, str):
            for pattern in SECRET_VALUE_PATTERNS:
                if pattern.search(value):
                    raise EventJournalSecretError(f"Secret-like journal payload value rejected at {path}")

    def _secret_like_key(self, key: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized in SAFE_TOKEN_KEYS:
            return False
        if normalized in SECRET_KEY_NAMES:
            return True
        return bool(re.search(r"(^|_)(api_key|access_token|refresh_token|private_key|client_secret|password|secret)($|_)", normalized))

    def _compact(self, value: Any, *, max_chars: int = 16000, max_items: int = 80) -> Any:
        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            for index, (key, nested) in enumerate(value.items()):
                if index >= max_items:
                    compacted["_truncated_items"] = len(value) - max_items
                    break
                compacted[str(key)] = self._compact(nested, max_chars=max_chars, max_items=max_items)
            return compacted
        if isinstance(value, list):
            items = [self._compact(item, max_chars=max_chars, max_items=max_items) for item in value[:max_items]]
            if len(value) > max_items:
                items.append({"_truncated_items": len(value) - max_items})
            return items
        if isinstance(value, str) and len(value) > max_chars:
            return {
                "truncated": True,
                "chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "excerpt": value[:max_chars],
            }
        return value

    def _run_id_from_payload(self, payload: dict[str, Any]) -> str | None:
        direct = payload.get("run_id")
        if isinstance(direct, str) and direct.strip():
            return direct
        nested = payload.get("run") if isinstance(payload.get("run"), dict) else None
        if nested:
            run_id = nested.get("run_id")
            if isinstance(run_id, str) and run_id.strip():
                return run_id
        return None


class RunEventJournalReducer:
    def __init__(self, journal: EventJournalService) -> None:
        self.journal = journal

    def reduce(self, run_id: str) -> RunJournalState:
        events = self.journal.list_run(run_id, limit=5000)
        workspace_id = events[0].workspace_id if events else None
        latest_status: str | None = None
        latest_stage: str | None = None
        blocking = False
        timeline: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        apply_events: list[dict[str, Any]] = []
        repair_events: list[dict[str, Any]] = []
        protocol_refs: list[dict[str, Any]] = []
        for event in events:
            payload = self._payload(event.payload_ref)
            latest_status = self._first_text(payload, "status", "run_status") or latest_status
            latest_stage = self._first_text(payload, "current_stage", "stage") or latest_stage
            if event.event_type in {"run.blocked", "run.failed"} or latest_status in {"blocked", "failed"}:
                blocking = True
            if bool(payload.get("blocking")):
                blocking = True
            item = {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "summary": event.summary,
                "status": self._first_text(payload, "status", "run_status"),
                "current_stage": self._first_text(payload, "current_stage", "stage"),
                "payload_ref": event.payload_ref,
                "source_ref": event.source_ref,
                "created_at": event.created_at,
            }
            timeline.append(item)
            if event.event_type.startswith("tool."):
                tool_events.append({**item, "tool": payload.get("tool"), "result": payload.get("result") or {}})
            elif event.event_type.startswith("check."):
                checks.append({**item, "check": payload.get("name") or payload.get("check"), "result": payload.get("result") or payload})
            elif event.event_type.startswith("apply.") or event.event_type.startswith("approval."):
                apply_events.append({**item, "payload": self._compact_payload_view(payload)})
            elif event.event_type.startswith("repair."):
                repair_events.append({**item, "case_id": payload.get("case_id"), "status": payload.get("status")})
            elif event.event_type.startswith("protocol."):
                protocol_refs.append({**item, "protocol_type": payload.get("type"), "bookmark_id": payload.get("bookmark_id")})
        next_sequence = max([event.sequence for event in events], default=0)
        return RunJournalState(
            run_id=run_id,
            workspace_id=workspace_id,
            status="available" if events else "empty",
            event_count=len(events),
            next_sequence=next_sequence,
            latest_stage=latest_stage,
            latest_status=latest_status,
            blocking=blocking,
            timeline=timeline,
            tool_events=tool_events,
            checks=checks,
            apply_events=apply_events,
            repair_events=repair_events,
            protocol_refs=protocol_refs,
            replay_cursor=next_sequence,
        )

    def _payload(self, payload_ref: str) -> dict[str, Any]:
        record = self.journal.read_payload(payload_ref)
        return record.payload if record is not None else {}

    def _first_text(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        for key in keys:
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _compact_payload_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in {"large_output", "stdout", "stderr"}}


class ThreadEventJournalReducer:
    def __init__(self, journal: EventJournalService) -> None:
        self.journal = journal

    def reduce(self, thread_id: str) -> ThreadJournalState:
        events = self.journal.list_thread(thread_id, limit=5000)
        workspace_id = events[0].workspace_id if events else None
        compact_events: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        linked_runs: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = self._payload(event.payload_ref)
            item = {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "turn_id": event.turn_id,
                "run_id": event.run_id,
                "summary": event.summary,
                "payload_ref": event.payload_ref,
                "source_ref": event.source_ref,
                "created_at": event.created_at,
            }
            compact_events.append(item)
            if event.event_type.startswith("turn."):
                turns.append({**item, "status": event.event_type.removeprefix("turn.")})
            if event.event_type.startswith("item.") or event.event_type.startswith("user.") or ".message" in event.event_type:
                items.append({**item, "item_type": event.event_type, "payload": payload})
            run_id = event.run_id or self._run_id_from_payload(payload)
            if run_id:
                linked_runs[run_id] = {
                    "run_id": run_id,
                    "last_sequence": event.sequence,
                    "last_event_type": event.event_type,
                    "payload_ref": event.payload_ref,
                }
        next_sequence = max([event.sequence for event in events], default=0)
        return ThreadJournalState(
            thread_id=thread_id,
            workspace_id=workspace_id,
            status="available" if events else "empty",
            event_count=len(events),
            next_sequence=next_sequence,
            turns=turns,
            items=items,
            events=compact_events,
            linked_runs=list(linked_runs.values()),
            replay_cursor=next_sequence,
        )

    def _payload(self, payload_ref: str) -> dict[str, Any]:
        record = self.journal.read_payload(payload_ref)
        return record.payload if record is not None else {}

    def _run_id_from_payload(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("run_id")
        if isinstance(value, str) and value.strip():
            return value
        nested = payload.get("run") if isinstance(payload.get("run"), dict) else None
        if nested and isinstance(nested.get("run_id"), str):
            return str(nested["run_id"])
        return None
