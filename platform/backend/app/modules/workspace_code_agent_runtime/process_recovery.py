from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class AgentProcessRecovery:
    """Typed checkpoint view for process state across runtime restarts."""

    @staticmethod
    def checkpoint(process_snapshot: dict[str, Any]) -> dict[str, Any]:
        active = [dict(item) for item in process_snapshot.get("active_processes") or [] if isinstance(item, dict)]
        processes = [dict(item) for item in process_snapshot.get("processes") or [] if isinstance(item, dict)]
        return {
            "active_processes": active,
            "processes": processes,
            "active_count": len(active),
            "restart_policy": "running processes are marked stale on runtime restore; the agent reruns the diagnostic tool if that output is still needed",
        }

    @staticmethod
    def restore_view(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(checkpoint, dict):
            return {"restored": False, "active_processes": [], "stale_processes": []}
        process_summary = checkpoint.get("process_summary") if isinstance(checkpoint.get("process_summary"), dict) else {}
        active = [dict(item) for item in process_summary.get("active_processes") or [] if isinstance(item, dict)]
        stale = [
            {
                **item,
                "status": "stale_after_restart",
                "restored_at": datetime.now(timezone.utc).isoformat(),
                "next_action": "rerun the command if the next agent step still needs this diagnostic output",
            }
            for item in active
        ]
        return {"restored": bool(checkpoint), "active_processes": [], "stale_processes": stale}
