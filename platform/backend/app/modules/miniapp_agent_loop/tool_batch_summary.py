from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def summarize_tool_batch(
    *,
    batch_id: str,
    requests: list[dict[str, Any]],
    results: list[dict[str, Any]],
    duration_ms: int,
    status: str,
    worker: str | None = None,
    owner_scope: str | None = None,
) -> dict[str, object]:
    tools = [str(item.get("tool") or "").strip() for item in requests if str(item.get("tool") or "").strip()]
    tool_set = set(tools)
    if tool_set <= {"read_files", "list_files"}:
        summary = "Read workspace context"
    elif "search_files" in tool_set:
        summary = "Searched workspace"
    elif "semantic_scan" in tool_set:
        summary = "Scanned source semantics"
    elif "run_command" in tool_set:
        summary = "Ran diagnostic command"
    elif "run_checks" in tool_set:
        summary = "Ran validation checks"
    elif "browser_verify" in tool_set:
        summary = "Ran browser proof"
    elif tools:
        summary = f"Executed {len(tools)} tool request{'s' if len(tools) != 1 else ''}"
    else:
        summary = "Executed tool batch"

    failed = [item for item in results if str(item.get("status") or "").lower() in {"error", "failed"}]
    return {
        "batch_id": batch_id,
        "summary": summary,
        "tools": tools,
        "tool_count": len(tools),
        "result_count": len(results),
        "failed_count": len(failed),
        "status": "failed" if failed and status == "completed" else status,
        "duration_ms": duration_ms,
        "worker": worker,
        "owner_scope": owner_scope,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
