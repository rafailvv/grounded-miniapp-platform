from __future__ import annotations

from typing import Any

from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager


MUTATING_AGENT_TOOLS = {"apply_patch_to_draft", "write_file"}


def is_mutating_agent_tool_call(request_item: dict[str, Any]) -> bool:
    return str(request_item.get("tool") or "").strip().lower() in MUTATING_AGENT_TOOLS


def file_changes_from_mutating_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    default_worker_id: str | None = None,
    default_owner_scope: str | None = None,
) -> tuple[list[DraftAction], list[dict[str, object]]]:
    file_changes: list[DraftAction] = []
    trace: list[dict[str, object]] = []
    for request_item in tool_calls:
        tool = str(request_item.get("tool") or "").strip().lower()
        if tool not in MUTATING_AGENT_TOOLS:
            continue
        targets = [
            _strip_leading_dot_slash(target)
            for target in request_item.get("targets") or []
            if str(target or "").strip()
        ]
        raw_file_path = request_item.get("file_path") or (targets[0] if targets else "")
        raw_worker_id = str(request_item.get("worker_id") or default_worker_id or "").strip()
        file_path = _normalize_agent_file_path(raw_file_path, worker_id=raw_worker_id)
        reason = str(request_item.get("reason") or f"{tool} requested by agent").strip()
        worker_id = str(raw_worker_id or AgentWorkerManager.owner_for_path(file_path)).strip()
        owner_scope = str(request_item.get("owner_scope") or default_owner_scope or "").strip()
        if worker_id:
            reason = f"[{worker_id}] {reason}"
        if tool == "write_file":
            file_changes.append(
                DraftAction(
                    file_path=file_path,
                    operation="replace",
                    content=str(request_item.get("content") or ""),
                    reason=reason,
                )
            )
        else:
            file_changes.append(
                DraftAction(
                    file_path=file_path,
                    operation="patch",
                    diff=str(request_item.get("diff") or request_item.get("content") or ""),
                    reason=reason,
                )
            )
        trace.append(
            {
                "tool": tool,
                "tool_use_id": str(request_item.get("tool_use_id") or ""),
                "contract": "mutating tool call converted to an internal file change for serialized draft apply",
                "file_path": file_path,
                "worker_id": worker_id,
                "owner_scope": owner_scope,
                "reason": reason,
            }
        )
    return file_changes, trace


def _strip_leading_dot_slash(raw_path: object) -> str:
    path = str(raw_path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _normalize_agent_file_path(raw_path: object, *, worker_id: str | None = None) -> str:
    path = _strip_leading_dot_slash(raw_path)
    if not path:
        return path
    if path.startswith("source/"):
        path = path[len("source/") :]
    if path.startswith("miniapp/"):
        return path
    worker = str(worker_id or "").strip()
    if worker in {"client_ui", "specialist_ui", "manager_ui"}:
        role = worker.removesuffix("_ui")
        if path in {"index.html", "app.js", "styles.css"}:
            return f"miniapp/app/static/{role}/{path}"
    if worker == "generated_tests":
        if path in {"test_generated_app.py", "generated_app.test.mjs"}:
            return f"miniapp/tests/{path}"
    if worker == "backend_api":
        if path in {"main.py", "db.py", "schemas.py"}:
            return f"miniapp/app/{path}"
        if path.endswith(".py") and "/" not in path:
            return f"miniapp/app/routes/{path}"
    if path.startswith(("app/", "tests/")) or path in {"Dockerfile", "requirements.txt"}:
        return f"miniapp/{path}"
    if path.startswith("static/"):
        return f"miniapp/app/{path}"
    if path.startswith("routes/"):
        return f"miniapp/app/{path}"
    if path.startswith("generated/"):
        return f"miniapp/app/{path}"
    return path
