from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class RolloutTraceRecorder:
    """Append-only run trace with a small reducer summary."""

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}

    def append(self, run_key: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "sequence": len(self._events.get(run_key, [])) + 1,
            "event_type": event_type,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._events.setdefault(run_key, []).append(event)
        return event

    def snapshot(self, run_key: str) -> dict[str, object]:
        events = list(self._events.get(run_key, []))
        counts: dict[str, int] = {}
        tool_counts: dict[str, int] = {}
        graph: list[dict[str, Any]] = []
        for event in events:
            event_type = str(event.get("event_type") or "")
            counts[event_type] = counts.get(event_type, 0) + 1
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            tool = str(payload.get("tool") or "").strip() if isinstance(payload, dict) else ""
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            graph.append(self._reduce_event(event))
        return {
            "event_count": len(events),
            "counts": counts,
            "tool_counts": tool_counts,
            "graph": graph,
            "reducer": {
                "model_turns": counts.get("agent_turn_started", 0),
                "tool_batches": counts.get("tool_batch", 0) + counts.get("tool_use_summary", 0),
                "patches": counts.get("patch_apply_completed", 0),
                "checks": counts.get("running_checks", 0) + counts.get("checks_completed", 0),
                "browser_proofs": counts.get("preview_validation_started", 0),
                "repairs": counts.get("repair_iteration", 0),
            },
            "events": events,
        }

    @staticmethod
    def _reduce_event(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        return {
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "tool": payload.get("tool") or details.get("tool"),
            "phase": details.get("phase"),
            "status": details.get("status"),
            "summary": payload.get("message") or details.get("summary") or details.get("reason"),
            "artifact_ref": details.get("artifact_ref"),
            "created_at": event.get("created_at"),
        }
