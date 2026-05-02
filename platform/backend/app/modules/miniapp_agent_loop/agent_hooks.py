from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


AgentHookName = Literal[
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "pre_apply_patch",
    "post_apply_patch",
    "post_browser_verify",
]


@dataclass(frozen=True)
class AgentHookEvent:
    hook: AgentHookName
    status: Literal["started", "completed", "failed"]
    payload: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "hook": self.hook,
            "status": self.status,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class AgentHookManager:
    """Small lifecycle hook recorder for agent tools, patches, and proof steps."""

    def __init__(self) -> None:
        self._events: dict[str, list[AgentHookEvent]] = {}

    def record(
        self,
        run_id: str,
        hook: AgentHookName,
        *,
        status: Literal["started", "completed", "failed"] = "completed",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = AgentHookEvent(
            hook=hook,
            status=status,
            payload=dict(payload or {}),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._events.setdefault(run_id, []).append(event)
        return event.as_dict()

    def snapshot(self, run_id: str) -> dict[str, Any]:
        events = [item.as_dict() for item in self._events.get(run_id, [])]
        counts: dict[str, int] = {}
        for item in events:
            key = f"{item.get('hook')}:{item.get('status')}"
            counts[key] = counts.get(key, 0) + 1
        return {"run_id": run_id, "event_count": len(events), "counts": counts, "events": events[-500:]}
