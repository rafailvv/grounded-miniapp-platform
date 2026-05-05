from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.modules.miniapp_agent_loop.agent_command_policy import DEFAULT_COMMAND_POLICY, decide_workspace_command
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry


MAX_TOOL_OUTPUT_CHARS = 6000


def normalize_tool_calls(raw_tool_calls: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_tool_calls, list):
        return normalized
    for item in raw_tool_calls:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip().lower()
        if tool not in AgentToolRegistry.names():
            continue
        raw_targets = item.get("targets") or []
        if not isinstance(raw_targets, list):
            raw_targets = []
        targets: list[str] = []
        for target in raw_targets:
            value = _strip_leading_dot_slash(target)
            if not value or value in targets:
                continue
            targets.append(value)
        mode = str(item.get("mode") or ("exact" if tool == "run_checks" else "")).strip().lower()
        if tool == "run_checks" and mode not in {"exact", "final"}:
            mode = "exact"
        normalized.append(
            {
                "tool": tool,
                "tool_use_id": str(item.get("tool_use_id") or "").strip(),
                "mode": mode,
                "targets": targets[:12],
                "file_path": _strip_leading_dot_slash(item.get("file_path") or ""),
                "pattern": str(item.get("pattern") or "").strip(),
                "command": str(item.get("command") or "").strip(),
                "artifact_ref": str(item.get("artifact_ref") or "").strip(),
                "content": str(item.get("content") or ""),
                "diff": str(item.get("diff") or ""),
                "old_string": str(item.get("old_string") or ""),
                "new_string": str(item.get("new_string") or ""),
                "replace_all": bool(item.get("replace_all") or False),
                "worker_id": str(item.get("worker_id") or "").strip(),
                "owner_scope": str(item.get("owner_scope") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return normalized


def _strip_leading_dot_slash(raw_path: object) -> str:
    path = str(raw_path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def truncate_tool_text(value: str, *, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2) :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n...[truncated {omitted} chars]...\n{tail}"


def summarize_read_file_payloads(
    *,
    file_contents: dict[str, str],
    max_files: int = 8,
    max_chars_per_file: int = 2200,
) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    for path, content in list(file_contents.items())[:max_files]:
        payloads.append(
            {
                "file_path": path,
                "content_excerpt": truncate_tool_text(content, max_chars=max_chars_per_file),
            }
        )
    return payloads


def list_workspace_files(*, workspace_tree: list[dict[str, str]], targets: list[str], max_paths: int = 200) -> dict[str, object]:
    selected_paths = [
        item["path"]
        for item in workspace_tree
        if (
            not targets
            or any(
                str(item["path"]).startswith(target.rstrip("/") + "/") or str(item["path"]) == target.rstrip("/")
                for target in targets
            )
        )
    ]
    return {
        "tool": "list_files",
        "targets": list(targets),
        "paths": selected_paths[:max_paths],
    }


def search_workspace_files(
    *,
    workspace_tree: list[dict[str, str]],
    read_text_file,
    pattern: str,
    targets: list[str],
    max_candidate_files: int = 200,
    max_matches: int = 20,
) -> dict[str, object]:
    candidate_files = [
        item["path"]
        for item in workspace_tree
        if item.get("type") == "file"
        and (
            not targets
            or any(
                str(item["path"]).startswith(target.rstrip("/") + "/") or str(item["path"]) == target.rstrip("/")
                for target in targets
            )
        )
    ]
    matches: list[dict[str, object]] = []
    if not pattern:
        return {"tool": "search_files", "pattern": pattern, "targets": list(targets), "matches": matches}
    for relative_path in candidate_files[:max_candidate_files]:
        content = read_text_file(relative_path)
        if not content or pattern not in content:
            continue
        line_hits = []
        for line_no, line in enumerate(content.splitlines(), start=1):
            if pattern in line:
                line_hits.append({"line": line_no, "text": line[:240]})
            if len(line_hits) >= 5:
                break
        matches.append({"file_path": relative_path, "hits": line_hits})
        if len(matches) >= max_matches:
            break
    return {
        "tool": "search_files",
        "pattern": pattern,
        "targets": list(targets),
        "matches": matches,
    }


def validate_workspace_command(command: str) -> str | None:
    decision = decide_workspace_command(command)
    return None if decision.allowed else decision.reason


def run_workspace_command(
    *,
    draft_source: Path,
    command: str,
    timeout_seconds: int,
    max_output_chars: int = MAX_TOOL_OUTPUT_CHARS,
    progress_callback=None,
    process_manager: AgentProcessManager | None = None,
    process_id: str | None = None,
) -> dict[str, object]:
    started_at = time.perf_counter()
    decision = decide_workspace_command(command)
    manager = process_manager or AgentProcessManager()
    result = manager.run(
        draft_source=draft_source,
        command=command,
        decision=decision,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        progress_callback=progress_callback,
        process_id=process_id,
    ).as_dict()
    result.setdefault("duration_ms", int((time.perf_counter() - started_at) * 1000))
    return result


def command_policy_snapshot() -> dict[str, object]:
    return DEFAULT_COMMAND_POLICY.snapshot()
