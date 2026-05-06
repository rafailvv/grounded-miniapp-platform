from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable


MICROCOMPACT_THRESHOLD_CHARS = 6000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_payload(value: Any, *, max_chars: int = 4500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars // 2] + f"\n...[omitted {len(text) - max_chars} chars]...\n" + text[-max_chars // 2 :]


def _payload_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


class AgentTranscriptStore:
    """Append-only model/tool transcript for a code-agent run.

    This is the runtime equivalent of a Claude/Codex conversation trace: model
    turns, tool calls, tool results, patches, checks, browser proof and repair
    packets are recorded with stable ids. The store also exposes pending tool
    results for the next model step so the model can continue from actual
    tool_result messages instead of a prompt-only summary.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._last_response_id: dict[str, str] = {}
        self._last_tool_call_ids: dict[str, list[str]] = {}
        self._pending_tool_results: dict[str, list[dict[str, Any]]] = {}
        self._pending_post_compact_messages: dict[str, list[dict[str, Any]]] = {}
        self._writers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._microcompact_writers: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {}

    def configure_persistence(
        self,
        run_key: str,
        *,
        writer: Callable[[dict[str, Any]], None],
        existing: dict[str, Any] | None = None,
    ) -> None:
        self._writers[run_key] = writer
        if existing:
            self.restore(run_key, existing)
        self._persist(run_key)

    def is_configured(self, run_key: str) -> bool:
        return run_key in self._writers

    def configure_microcompact(
        self,
        run_key: str,
        *,
        writer: Callable[[dict[str, Any], str], dict[str, Any]] | None,
    ) -> None:
        if writer is not None:
            self._microcompact_writers[run_key] = writer

    def restore(self, run_key: str, snapshot: dict[str, Any]) -> None:
        events = snapshot.get("events") if isinstance(snapshot, dict) else None
        if isinstance(events, list):
            self._events[run_key] = [event for event in events if isinstance(event, dict)]
        response_id = snapshot.get("last_response_id") if isinstance(snapshot, dict) else None
        if response_id:
            self._last_response_id[run_key] = str(response_id)
        tool_call_ids = snapshot.get("last_tool_call_ids") if isinstance(snapshot, dict) else None
        if isinstance(tool_call_ids, list):
            self._last_tool_call_ids[run_key] = [str(item) for item in tool_call_ids if str(item or "").strip()]
        pending = snapshot.get("tool_result_messages") if isinstance(snapshot, dict) else None
        if isinstance(pending, list):
            self._pending_tool_results[run_key] = [item for item in pending if isinstance(item, dict)]
        compact_messages = snapshot.get("post_compact_messages") if isinstance(snapshot, dict) else None
        if isinstance(compact_messages, list):
            self._pending_post_compact_messages[run_key] = [item for item in compact_messages if isinstance(item, dict)]

    def _persist(self, run_key: str) -> None:
        writer = self._writers.get(run_key)
        if writer is None:
            return
        writer(self.snapshot(run_key))

    def append(
        self,
        run_key: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self._events.setdefault(run_key, [])
        event = {
            "sequence": len(events) + 1,
            "event_type": event_type,
            "payload": payload or {},
            "created_at": _now(),
        }
        events.append(event)
        self._persist(run_key)
        return event

    def next_model_context(self, run_key: str) -> dict[str, Any]:
        previous_response_id = self._last_response_id.get(run_key) or None
        pending = list(self._pending_tool_results.get(run_key, []))
        last_tool_call_ids = list(self._last_tool_call_ids.get(run_key, []))
        if previous_response_id and last_tool_call_ids:
            pending_by_id = {
                str(item.get("tool_use_id") or item.get("call_id") or "").strip(): item
                for item in pending
                if isinstance(item, dict)
            }
            missing = [call_id for call_id in last_tool_call_ids if call_id not in pending_by_id]
            if missing:
                self._last_response_id.pop(run_key, None)
                self._last_tool_call_ids[run_key] = []
                self._pending_tool_results[run_key] = []
                self.append(
                    run_key,
                    "tool_result_context_incomplete",
                    {
                        "previous_response_id": previous_response_id,
                        "missing_tool_call_ids": missing[:12],
                        "pending_tool_result_count": len(pending),
                    },
                )
                previous_response_id = None
                pending = []
            else:
                pending = [pending_by_id[call_id] for call_id in last_tool_call_ids]
        elif pending and not previous_response_id:
            self._pending_tool_results[run_key] = []
            self.append(
                run_key,
                "tool_result_context_incomplete",
                {
                    "previous_response_id": None,
                    "missing_tool_call_ids": [],
                    "pending_tool_result_count": len(pending),
                },
            )
            pending = []
        return {
            "previous_response_id": previous_response_id,
            "tool_result_messages": pending,
            "post_compact_messages": list(self._pending_post_compact_messages.get(run_key, [])),
        }

    def clear_model_context(self, run_key: str) -> None:
        """Start the next model step from compact prompt context only."""
        self._last_response_id.pop(run_key, None)
        self._last_tool_call_ids[run_key] = []
        self._pending_tool_results[run_key] = []
        self._pending_post_compact_messages[run_key] = []
        self.append(run_key, "compact_model_context_reset", {"reason": "context_window_pressure"})

    def append_model_turn(
        self,
        run_key: str,
        *,
        attempt: int,
        tool_round: int,
        response_id: str,
        assistant_message: str,
        tool_calls: list[dict[str, Any]],
        model: str,
        usage: dict[str, Any] | None = None,
        consumed_tool_result_count: int = 0,
        consumed_post_compact_count: int = 0,
        consumed_post_compact_refs: list[str] | None = None,
    ) -> None:
        if response_id:
            self._last_response_id[run_key] = response_id
        self._last_tool_call_ids[run_key] = [
            str(item.get("tool_use_id") or item.get("call_id") or item.get("id") or "").strip()
            for item in tool_calls
            if isinstance(item, dict) and str(item.get("tool_use_id") or item.get("call_id") or item.get("id") or "").strip()
        ]
        if consumed_tool_result_count:
            self._pending_tool_results[run_key] = []
        if consumed_post_compact_count:
            consumed = list(self._pending_post_compact_messages.get(run_key, []))[:consumed_post_compact_count]
            self._pending_post_compact_messages[run_key] = list(self._pending_post_compact_messages.get(run_key, []))[consumed_post_compact_count:]
            self.append(
                run_key,
                "post_compact_message_consumed",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "response_id": response_id,
                    "count": consumed_post_compact_count,
                    "refs": consumed_post_compact_refs
                    or [str(item.get("post_compact_message_ref") or item.get("ref") or "") for item in consumed],
                },
            )
        self.append(
            run_key,
            "model_turn",
            {
                "attempt": attempt,
                "tool_round": tool_round,
                "response_id": response_id,
                "assistant_message": assistant_message[:2000],
                "tool_calls": [
                    {
                        "tool_use_id": str(item.get("tool_use_id") or ""),
                        "tool": str(item.get("tool") or ""),
                        "targets": list(item.get("targets") or [])[:12] if isinstance(item.get("targets"), list) else [],
                    }
                    for item in tool_calls
                    if isinstance(item, dict)
                ],
                "model": model,
                "usage": dict(usage or {}),
                "consumed_tool_result_count": consumed_tool_result_count,
                "consumed_post_compact_count": consumed_post_compact_count,
            },
        )

    def append_tool_calls(self, run_key: str, tool_calls: list[dict[str, Any]]) -> None:
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            self.append(
                run_key,
                "tool_call",
                {
                    "tool_use_id": str(item.get("tool_use_id") or ""),
                    "tool": str(item.get("tool") or ""),
                    "arguments": {
                        key: value
                        for key, value in item.items()
                        if key not in {"content", "diff"} and key != "tool_use_id"
                    },
                    "has_content": bool(item.get("content")),
                    "has_diff": bool(item.get("diff")),
                },
            )

    def append_tool_results(self, run_key: str, tool_results: list[dict[str, Any]]) -> None:
        pending = self._pending_tool_results.setdefault(run_key, [])
        for result in tool_results:
            if not isinstance(result, dict):
                continue
            tool_use_id = str(result.get("tool_use_id") or result.get("call_id") or result.get("id") or "").strip()
            serialized = _payload_text(result)
            microcompact = self._microcompact(run_key, result, serialized)
            output = str(microcompact.get("output") or "") if microcompact else _compact_payload(result)
            if not tool_use_id:
                self.append(
                    run_key,
                    "tool_result_unlinked",
                    {
                        "tool": str(result.get("tool") or ""),
                        "output": output,
                        "pending": False,
                        **({key: value for key, value in microcompact.items() if key != "output"} if microcompact else {}),
                    },
                )
                continue
            message = {
                "tool_use_id": tool_use_id,
                "tool": str(result.get("tool") or ""),
                "output": output,
                **({key: value for key, value in microcompact.items() if key != "output"} if microcompact else {}),
            }
            pending.append(message)
            self.append(run_key, "tool_result", message)
            if microcompact:
                self.append(
                    run_key,
                    "tool_result_microcompact",
                    {
                        "tool_use_id": tool_use_id,
                        "tool": str(result.get("tool") or ""),
                        "microcompact_ref": microcompact.get("microcompact_ref"),
                        "digest": microcompact.get("digest"),
                        "original_chars": microcompact.get("original_chars"),
                    },
                )

    def compact_model_context(self, run_key: str, *, reason: str, preserve_response_id: bool = True) -> dict[str, Any]:
        previous_response_id = self._last_response_id.get(run_key)
        pending_count = len(self._pending_tool_results.get(run_key, []))
        last_tool_call_count = len(self._last_tool_call_ids.get(run_key, []))
        self._pending_tool_results[run_key] = []
        if not preserve_response_id or pending_count or last_tool_call_count:
            self._last_response_id.pop(run_key, None)
            self._last_tool_call_ids[run_key] = []
        event = self.append(
            run_key,
            "compact_model_context_reset",
            {
                "reason": reason,
                "previous_response_id": previous_response_id,
                "pending_tool_result_count": pending_count,
                "last_tool_call_count": last_tool_call_count,
                "preserve_response_id": preserve_response_id,
            },
        )
        return dict(event.get("payload") or {})

    def append_post_compact_message(self, run_key: str, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        ref = str(message.get("post_compact_message_ref") or message.get("ref") or "").strip()
        pending = self._pending_post_compact_messages.setdefault(run_key, [])
        if ref and any(str(item.get("post_compact_message_ref") or item.get("ref") or "") == ref for item in pending):
            return
        compact = {
            "boundary_id": message.get("boundary_id"),
            "post_compact_message_ref": ref or message.get("post_compact_message_ref"),
            "status": message.get("status") or "pending",
            "message": str(message.get("message") or "")[:12000],
            "sections": message.get("sections") if isinstance(message.get("sections"), dict) else {},
            "refs": message.get("refs") if isinstance(message.get("refs"), dict) else {},
            "created_at": message.get("created_at"),
        }
        pending.append(compact)
        self.append(run_key, "post_compact_message", compact)

    def _microcompact(self, run_key: str, result: dict[str, Any], serialized: str) -> dict[str, Any] | None:
        writer = self._microcompact_writers.get(run_key)
        if writer is None or len(serialized) <= MICROCOMPACT_THRESHOLD_CHARS:
            return None
        try:
            return writer(result, serialized)
        except Exception:
            digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            return {
                "digest": digest,
                "original_chars": len(serialized),
                "output": _compact_payload(result),
                "microcompact_error": "writer_failed",
            }

    def append_file_changes(self, run_key: str, *, turn: int, file_changes: list[Any]) -> None:
        self.append(
            run_key,
            "file_change",
            {
                "turn": turn,
                "changes": [
                    {
                        "file_path": getattr(item, "file_path", ""),
                        "change_type": getattr(item, "operation", ""),
                        "reason": getattr(item, "reason", ""),
                        "has_content": bool(getattr(item, "content", None)),
                        "has_diff": bool(getattr(item, "diff", None)),
                    }
                    for item in file_changes
                ],
            },
        )

    def append_check_snapshot(self, run_key: str, *, failed_count: int, result_names: list[str]) -> None:
        self.append(run_key, "check_snapshot", {"failed_count": failed_count, "result_names": result_names[:24]})

    def append_browser_proof(self, run_key: str, proof: dict[str, Any]) -> None:
        self.append(run_key, "browser_proof", proof)

    def append_repair(self, run_key: str, payload: dict[str, Any]) -> None:
        self.append(run_key, "repair", payload)

    def snapshot(self, run_key: str) -> dict[str, Any]:
        events = list(self._events.get(run_key, []))
        counts: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            counts[event_type] = counts.get(event_type, 0) + 1
        return {
            "event_count": len(events),
            "counts": counts,
            "last_response_id": self._last_response_id.get(run_key),
            "last_tool_call_ids": list(self._last_tool_call_ids.get(run_key, [])),
            "pending_tool_result_count": len(self._pending_tool_results.get(run_key, [])),
            "pending_post_compact_count": len(self._pending_post_compact_messages.get(run_key, [])),
            "events": events,
            "tool_result_messages": list(self._pending_tool_results.get(run_key, [])),
            "post_compact_messages": list(self._pending_post_compact_messages.get(run_key, [])),
            "all_tool_result_messages": [
                event.get("payload")
                for event in events
                if event.get("event_type") == "tool_result" and isinstance(event.get("payload"), dict)
            ],
            "reduced_graph": [self._reduce(event) for event in events],
        }

    @staticmethod
    def _reduce(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return {
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "tool": payload.get("tool"),
            "tool_use_id": payload.get("tool_use_id"),
            "response_id": payload.get("response_id"),
            "summary": payload.get("assistant_message") or payload.get("reason") or payload.get("tool"),
            "created_at": event.get("created_at"),
        }
