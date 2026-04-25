from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


MAX_TOOL_OUTPUT_CHARS = 6000

_BLOCKED_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(^|\s)rm\s+-rf(\s|$)", "Destructive file deletion is blocked."),
    (r"(^|\s)git\s+reset(\s|$)", "Git reset is blocked."),
    (r"(^|\s)git\s+checkout\s+--(\s|$)", "Discarding tracked changes is blocked."),
    (r"(^|\s)git\s+clean(\s|$)", "Git clean is blocked."),
    (r"(^|\s)curl(\s|$)", "Network fetch commands are blocked."),
    (r"(^|\s)wget(\s|$)", "Network fetch commands are blocked."),
    (r"\|\s*sh(\s|$)", "Piping remote or generated shell into sh is blocked."),
    (r"\|\s*bash(\s|$)", "Piping remote or generated shell into bash is blocked."),
    (r"(^|\s)pip(\d+)?\s+install(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)python(\d+(\.\d+)?)?\s+-m\s+pip\s+install(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)npm\s+(install|i)(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)pnpm\s+(install|add)(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)yarn\s+(add|install)(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)uv\s+pip\s+install(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)poetry\s+add(\s|$)", "Package installation commands are blocked."),
    (r"(^|\s)docker\s+(build|pull|run)(\s|$)", "Docker image or container mutation is blocked."),
    (r"(^|\s)docker\s+compose\s+(build|pull|up|run)(\s|$)", "Docker rebuild commands are blocked."),
    (r"(^|\s)docker-compose\s+(build|pull|up|run)(\s|$)", "Docker rebuild commands are blocked."),
    (r"(^|\s)(apt|apt-get|brew)\s+(install|update|upgrade)(\s|$)", "System package management commands are blocked."),
)


def tool_patch_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["patch_ready", "tool_request", "no_progress", "fatal_invalid_response"],
            },
            "diagnosis": {"type": "string"},
            "tool_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tool": {"type": "string", "enum": ["list_files", "read_files", "run_checks", "search_files", "inspect_diff", "run_command"]},
                        "mode": {"type": "string", "enum": ["exact", "final"]},
                        "targets": {"type": "array", "items": {"type": "string"}},
                        "pattern": {"type": "string"},
                        "command": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["tool", "targets", "reason"],
                },
            },
            "expected_verification": {"type": "string"},
            "rationale_by_file": {"type": "object", "additionalProperties": {"type": "string"}},
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "file_path": {"type": "string"},
                        "operation": {"type": "string", "enum": ["create", "replace", "delete", "patch"]},
                        "content": {"type": ["string", "null"]},
                        "diff": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["file_path", "operation", "reason"],
                },
            },
        },
        "required": ["diagnosis", "tool_requests", "expected_verification", "rationale_by_file", "operations"],
    }


def normalize_tool_requests(raw_tool_requests: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_tool_requests, list):
        return normalized
    for item in raw_tool_requests:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip().lower()
        if tool not in {"list_files", "read_files", "run_checks", "search_files", "inspect_diff", "run_command"}:
            continue
        raw_targets = item.get("targets") or []
        if not isinstance(raw_targets, list):
            raw_targets = []
        targets: list[str] = []
        for target in raw_targets:
            value = str(target or "").strip().lstrip("./")
            if not value or value in targets:
                continue
            targets.append(value)
        mode = str(item.get("mode") or ("exact" if tool == "run_checks" else "")).strip().lower()
        if tool == "run_checks" and mode not in {"exact", "final"}:
            mode = "exact"
        normalized.append(
            {
                "tool": tool,
                "mode": mode,
                "targets": targets[:12],
                "pattern": str(item.get("pattern") or "").strip(),
                "command": str(item.get("command") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return normalized


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
    stripped = str(command or "").strip()
    if not stripped:
        return "Empty command."
    lowered = stripped.lower()
    for pattern, message in _BLOCKED_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return message
    return None


def run_workspace_command(
    *,
    draft_source: Path,
    command: str,
    timeout_seconds: int,
    max_output_chars: int = MAX_TOOL_OUTPUT_CHARS,
) -> dict[str, object]:
    policy_error = validate_workspace_command(command)
    if policy_error:
        return {
            "tool": "run_command",
            "command": command,
            "error": policy_error,
            "cwd": str(draft_source),
        }
    shell_candidates = ["/bin/zsh", shutil.which("zsh"), "/bin/sh", shutil.which("sh")]
    shell_path = next(
        (
            candidate
            for candidate in shell_candidates
            if candidate and Path(candidate).exists()
        ),
        None,
    )
    if not shell_path:
        return {
            "tool": "run_command",
            "command": command,
            "error": "No compatible shell was found for diagnostic command execution.",
            "cwd": str(draft_source),
        }
    try:
        completed = subprocess.run(
            [shell_path, "-lc", command],
            cwd=draft_source,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "tool": "run_command",
            "command": command,
            "shell": shell_path,
            "exit_code": completed.returncode,
            "stdout": truncate_tool_text(completed.stdout, max_chars=max_output_chars),
            "stderr": truncate_tool_text(completed.stderr, max_chars=max_output_chars),
            "cwd": str(draft_source),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "tool": "run_command",
            "command": command,
            "shell": shell_path,
            "error": f"Command timed out after {timeout_seconds}s.",
            "stdout": truncate_tool_text(exc.stdout or "", max_chars=max_output_chars),
            "stderr": truncate_tool_text(exc.stderr or "", max_chars=max_output_chars),
            "cwd": str(draft_source),
        }
