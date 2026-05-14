from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


AgentHookName = Literal[
    "session_start",
    "before_run",
    "after_run",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "before_apply",
    "after_apply",
    "before_checks",
    "on_check_failed",
    "pre_apply_patch",
    "post_apply_patch",
    "post_browser_verify",
    "after_gate",
    "on_memory_update",
    "on_export",
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
            "schema": "grounded.hook_event.v1",
            "side_effects_allowed": False,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AgentHookOutcome:
    hook: AgentHookName
    should_block: bool
    block_reason: str | None
    additional_contexts: list[str]
    event: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "grounded.hook_outcome.v1",
            "hook": self.hook,
            "should_block": self.should_block,
            "block_reason": self.block_reason,
            "additional_contexts": list(self.additional_contexts),
            "event": self.event,
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
        safe_payload = dict(payload or {})
        safe_payload.setdefault("side_effects_allowed", False)
        safe_payload.setdefault("permission", "record_only")
        event = AgentHookEvent(
            hook=hook,
            status=status,
            payload=safe_payload,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._events.setdefault(run_id, []).append(event)
        return event.as_dict()

    def run(
        self,
        run_id: str,
        hook: AgentHookName,
        *,
        payload: dict[str, Any] | None = None,
    ) -> AgentHookOutcome:
        safe_payload = dict(payload or {})
        additional_contexts: list[str] = []
        should_block = False
        block_reason = None
        if safe_payload.get("block_reason"):
            should_block = True
            block_reason = str(safe_payload.get("block_reason"))
        if hook == "on_check_failed":
            failed = safe_payload.get("failed_checks")
            if isinstance(failed, list) and failed:
                additional_contexts.append(
                    "Repair must start from the failing check evidence and named repair packet before broad edits."
                )
        if hook == "pre_tool_use" and safe_payload.get("risk") == "forbidden":
            should_block = True
            block_reason = "Tool risk is forbidden by internal hook policy."
        event = self.record(
            run_id,
            hook,
            status="failed" if should_block else "completed",
            payload={
                **safe_payload,
                "additional_contexts": additional_contexts,
                "block_reason": block_reason,
            },
        )
        return AgentHookOutcome(
            hook=hook,
            should_block=should_block,
            block_reason=block_reason,
            additional_contexts=additional_contexts,
            event=event,
        )

    def snapshot(self, run_id: str) -> dict[str, Any]:
        events = [item.as_dict() for item in self._events.get(run_id, [])]
        counts: dict[str, int] = {}
        for item in events:
            key = f"{item.get('hook')}:{item.get('status')}"
            counts[key] = counts.get(key, 0) + 1
        return {
            "schema": "grounded.hook_trace.v1",
            "run_id": run_id,
            "side_effects_allowed": False,
            "event_count": len(events),
            "counts": counts,
            "events": events[-500:],
        }
