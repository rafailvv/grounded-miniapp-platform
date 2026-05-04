from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


TOOL_PROTOCOL_VERSION = "grounded.tool.v2"

ToolRisk = Literal["safe", "read_only", "mutating", "network", "destructive", "forbidden", "unknown"]


CANONICAL_TOOL_ALIASES: dict[str, str] = {
    "list_files": "file.list",
    "read_files": "file.read",
    "search_files": "search.grep",
    "inspect_diff": "diff.inspect",
    "read_artifact_ref": "artifact.read",
    "semantic_scan": "semantic.scan",
    "run_command": "shell.exec",
    "run_checks": "checks.run",
    "browser_verify": "browser.verify",
    "apply_patch_to_draft": "patch.apply",
    "write_file": "file.write",
    "file.edit": "file.edit",
    "file.read": "file.read",
    "file.write": "file.write",
    "search.grep": "search.grep",
    "search.glob": "file.list",
    "shell.exec": "shell.exec",
    "browser.verify": "browser.verify",
    "patch.apply": "patch.apply",
    "todo.write": "todo.write",
    "ask_user": "user.ask",
    "review.start": "review.start",
}

TOOL_RISK_DEFAULTS: dict[str, ToolRisk] = {
    "file.list": "read_only",
    "file.read": "read_only",
    "artifact.read": "read_only",
    "search.grep": "read_only",
    "semantic.scan": "read_only",
    "diff.inspect": "read_only",
    "checks.run": "read_only",
    "browser.verify": "read_only",
    "shell.exec": "read_only",
    "file.write": "mutating",
    "file.edit": "mutating",
    "patch.apply": "mutating",
    "todo.write": "safe",
    "user.ask": "safe",
    "review.start": "read_only",
}


def canonical_tool_name(tool: object) -> str:
    raw = str(tool or "").strip().lower()
    return CANONICAL_TOOL_ALIASES.get(raw, raw or "unknown")


def default_tool_risk(tool: object) -> ToolRisk:
    return TOOL_RISK_DEFAULTS.get(canonical_tool_name(tool), "unknown")


def tool_registry_contract() -> dict[str, Any]:
    tools = []
    for alias, canonical in sorted(CANONICAL_TOOL_ALIASES.items()):
        tools.append(
            {
                "alias": alias,
                "canonical": canonical,
                "version": TOOL_PROTOCOL_VERSION,
                "risk": TOOL_RISK_DEFAULTS.get(canonical, "unknown"),
            }
        )
    return {
        "tool_protocol_version": TOOL_PROTOCOL_VERSION,
        "envelope_fields": [
            "tool_call_id",
            "tool",
            "version",
            "input",
            "risk",
            "approval",
            "progress",
            "result",
            "artifacts",
            "timing",
            "retry",
            "truncation",
            "error",
        ],
        "error_shape": {
            "code": "stable_machine_code",
            "message": "human-readable message",
            "retryable": False,
            "details": {},
        },
        "artifact_shape": {
            "artifact_id": "optional stable id",
            "ref": "run-scoped artifact reference",
            "kind": "trace|diff|stdout|stderr|screenshot|json",
            "mime_type": "application/json",
            "size_bytes": 0,
        },
        "tools": tools,
    }


def tool_envelope(
    *,
    tool: object,
    input_payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    risk: ToolRisk | None = None,
    approval: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    timing: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    progress: list[dict[str, Any]] | None = None,
    retry: dict[str, Any] | None = None,
    truncation: dict[str, Any] | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    normalized_error = None
    if error:
        normalized_error = {
            "code": str(error.get("code") or "tool_error"),
            "message": str(error.get("message") or error.get("detail") or "Tool failed."),
            "retryable": bool(error.get("retryable") or False),
            "details": error.get("details") or {},
        }
    return {
        "tool_call_id": tool_call_id or f"tool_{uuid4().hex}",
        "tool": canonical,
        "version": TOOL_PROTOCOL_VERSION,
        "input": input_payload or {},
        "risk": risk or default_tool_risk(canonical),
        "approval": approval or {"required": False, "status": "not_required"},
        "progress": progress or [],
        "result": result or {},
        "artifacts": artifacts or [],
        "timing": timing or {},
        "retry": retry or {"retryable": bool(normalized_error and normalized_error.get("retryable")), "attempt": 1},
        "truncation": truncation or {"truncated": False},
        "error": normalized_error,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def structured_tool_error(
    *,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details or {},
    }
