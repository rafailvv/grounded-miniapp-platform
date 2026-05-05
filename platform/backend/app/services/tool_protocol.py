from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


TOOL_PROTOCOL_VERSION = "grounded.tool.v2"

ToolRisk = Literal["safe", "read_only", "mutating", "network", "destructive", "forbidden", "unknown"]
SandboxProfile = Literal["analysis_only", "agent_draft", "apply_gate", "developer_bypass"]


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
    "edit_file_exact": "file.edit",
    "file.edit": "file.edit",
    "file.read": "file.read",
    "file.write": "file.write",
    "search.grep": "search.grep",
    "search.glob": "file.list",
    "shell.exec": "shell.exec",
    "browser.verify": "browser.verify",
    "patch.apply": "patch.apply",
    "contract.compile": "contract.compile",
    "registry.sync": "registry.sync",
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
    "contract.compile": "safe",
    "registry.sync": "mutating",
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
                "input_schema": TOOL_INPUT_SCHEMAS.get(canonical, {}),
                "output_schema": TOOL_OUTPUT_SCHEMAS.get(canonical, {}),
            }
        )
    return {
        "tool_protocol_version": TOOL_PROTOCOL_VERSION,
        "envelope_fields": [
            "tool_call_id",
            "tool",
            "version",
            "status",
            "input",
            "risk",
            "approval",
            "sandbox_profile",
            "progress",
            "result",
            "artifacts",
            "timing",
            "started_at",
            "completed_at",
            "duration_ms",
            "stdout_ref",
            "stderr_ref",
            "changed_files",
            "failure_class",
            "failure_signature",
            "repair_recipe_ids",
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


TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "contract.compile": {
        "type": "object",
        "required": ["workspace_id", "run_id", "prompt", "generation_mode"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "prompt": {"type": "string"},
            "generation_mode": {"type": "string", "enum": ["fast", "balanced", "quality", "basic"]},
        },
    },
    "registry.sync": {
        "type": "object",
        "required": ["workspace_id", "run_id", "contract_id"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "contract_id": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "checks.run": {
        "type": "object",
        "required": ["workspace_id", "run_id"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
        },
    },
    "patch.apply": {
        "type": "object",
        "required": ["file_path"],
        "properties": {
            "file_path": {"type": "string"},
            "diff": {"type": "string"},
            "content": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "file.write": {
        "type": "object",
        "required": ["file_path", "content"],
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "file.edit": {
        "type": "object",
        "required": ["file_path", "old_string", "new_string"],
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "allowed_file_graph": {"type": "object"},
        },
    },
}

TOOL_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "contract.compile": {"type": "object", "required": ["contract", "allowed_file_graph"]},
    "registry.sync": {"type": "object", "required": ["snapshot", "regenerated_files", "repair_recipes"]},
    "checks.run": {"type": "object", "required": ["status", "results"]},
    "patch.apply": {"type": "object", "required": ["status", "changed_files"]},
    "file.write": {"type": "object", "required": ["status", "file_path"]},
    "file.edit": {"type": "object", "required": ["status", "file_path"]},
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
    status: str | None = None,
    sandbox_profile: SandboxProfile | str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: int | None = None,
    stdout_ref: str | None = None,
    stderr_ref: str | None = None,
    changed_files: list[str] | None = None,
    failure_class: str | None = None,
    failure_signature: str | None = None,
    repair_recipe_ids: list[str] | None = None,
) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    created_at = datetime.now(timezone.utc).isoformat()
    resolved_status = status or ("failed" if error else "completed" if result is not None else "started")
    resolved_timing = dict(timing or {})
    if duration_ms is not None:
        resolved_timing["duration_ms"] = duration_ms
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
        "status": resolved_status,
        "input": input_payload or {},
        "risk": risk or default_tool_risk(canonical),
        "approval": approval or {"required": False, "status": "not_required"},
        "approval_id": (approval or {}).get("approval_id") if isinstance(approval, dict) else None,
        "sandbox_profile": sandbox_profile or ("agent_draft" if default_tool_risk(canonical) == "mutating" else "analysis_only"),
        "progress": progress or [],
        "result": result or {},
        "artifacts": artifacts or [],
        "timing": resolved_timing,
        "started_at": started_at or created_at,
        "completed_at": completed_at if completed_at is not None else (created_at if resolved_status in {"completed", "failed"} else None),
        "duration_ms": duration_ms if duration_ms is not None else resolved_timing.get("duration_ms"),
        "stdout_ref": stdout_ref,
        "stderr_ref": stderr_ref,
        "changed_files": changed_files or [],
        "failure_class": failure_class,
        "failure_signature": failure_signature,
        "repair_recipe_ids": repair_recipe_ids or [],
        "retry": retry or {"retryable": bool(normalized_error and normalized_error.get("retryable")), "attempt": 1},
        "truncation": truncation or {"truncated": False},
        "error": normalized_error,
        "created_at": created_at,
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
