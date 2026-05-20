from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from app.models.workbench import ToolEnvelope


TOOL_PROTOCOL_VERSION = "grounded.tool.v2"

ToolRisk = Literal["safe", "read_only", "mutating", "network", "destructive", "forbidden", "unknown"]
ToolApprovalClass = Literal["none", "policy", "human", "forbidden"]
ToolArtifactSpillPolicy = Literal["never", "on_truncation", "always"]
SandboxProfile = Literal["analysis_readonly", "agent_draft_write", "source_apply_gate", "developer_bypass", "analysis_only", "agent_draft", "apply_gate"]


@dataclass(frozen=True)
class ToolProtocolSpec:
    canonical: str
    version: str
    aliases: tuple[str, ...]
    risk: ToolRisk
    approval_class: ToolApprovalClass
    sandbox_profile: str
    concurrency_safe: bool
    timeout_seconds: int
    output_cap_chars: int
    artifact_spill_policy: ToolArtifactSpillPolicy
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    deferred: bool = False
    dynamic: bool = False

    def as_contract(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "version": self.version,
            "aliases": list(self.aliases),
            "risk": self.risk,
            "approval_class": self.approval_class,
            "sandbox_profile": self.sandbox_profile,
            "concurrency_safe": self.concurrency_safe,
            "timeout_seconds": self.timeout_seconds,
            "output_cap_chars": self.output_cap_chars,
            "artifact_spill_policy": self.artifact_spill_policy,
            "deferred": self.deferred,
            "dynamic": self.dynamic,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


CANONICAL_TOOL_ALIASES: dict[str, str] = {
    "list_files": "file.list",
    "read_files": "file.read",
    "search_files": "search.grep",
    "inspect_diff": "diff.inspect",
    "read_artifact_ref": "artifact.read",
    "semantic_scan": "semantic.scan",
    "tool_search": "tool.search",
    "lsp_diagnostics": "lsp.diagnostics",
    "lsp_symbol_context": "lsp.symbol_context",
    "lsp_definition": "lsp.definition",
    "lsp_find_references": "lsp.find_references",
    "lsp_route_graph": "lsp.route_graph",
    "lsp_route_static_context": "lsp.route_static_context",
    "lsp.diagnostics": "lsp.diagnostics",
    "lsp.symbol_context": "lsp.symbol_context",
    "lsp.definition": "lsp.definition",
    "lsp.find_references": "lsp.find_references",
    "lsp.route_graph": "lsp.route_graph",
    "lsp.route_static_context": "lsp.route_static_context",
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
    "tool.search": "tool.search",
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
    "tool.search": "read_only",
    "lsp.diagnostics": "read_only",
    "lsp.symbol_context": "read_only",
    "lsp.definition": "read_only",
    "lsp.find_references": "read_only",
    "lsp.route_graph": "read_only",
    "lsp.route_static_context": "read_only",
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

TOOL_EXECUTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "file.list": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 6000},
    "file.read": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 9000},
    "artifact.read": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "search.grep": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 6000},
    "semantic.scan": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "tool.search": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 9000},
    "lsp.diagnostics": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "lsp.symbol_context": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 10000},
    "lsp.definition": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 8000},
    "lsp.find_references": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 10000},
    "lsp.route_graph": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 14000},
    "lsp.route_static_context": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "diff.inspect": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "checks.run": {"concurrency_safe": False, "timeout_seconds": 120, "output_cap_chars": 10000, "approval_class": "policy"},
    "browser.verify": {
        "concurrency_safe": False,
        "timeout_seconds": 180,
        "output_cap_chars": 14000,
        "approval_class": "policy",
        "dynamic": True,
    },
    "shell.exec": {"concurrency_safe": True, "timeout_seconds": 30, "output_cap_chars": 6000, "approval_class": "policy"},
    "file.write": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "file.edit": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "patch.apply": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "contract.compile": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "registry.sync": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "todo.write": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "user.ask": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "review.start": {"concurrency_safe": False, "timeout_seconds": 120, "output_cap_chars": 10000, "approval_class": "policy"},
}


def canonical_tool_name(tool: object) -> str:
    raw = str(tool or "").strip().lower()
    return CANONICAL_TOOL_ALIASES.get(raw, raw or "unknown")


def default_tool_risk(tool: object) -> ToolRisk:
    return TOOL_RISK_DEFAULTS.get(canonical_tool_name(tool), "unknown")


def default_tool_approval_class(tool: object) -> ToolApprovalClass:
    canonical = canonical_tool_name(tool)
    risk = default_tool_risk(canonical)
    if risk == "forbidden":
        return "forbidden"
    configured = TOOL_EXECUTION_DEFAULTS.get(canonical, {}).get("approval_class")
    if configured in {"none", "policy", "human", "forbidden"}:
        return configured  # type: ignore[return-value]
    if risk in {"mutating", "destructive"}:
        return "human"
    if risk == "network":
        return "policy"
    return "none"


def default_tool_sandbox_profile(tool: object) -> str:
    canonical = canonical_tool_name(tool)
    risk = default_tool_risk(canonical)
    configured = TOOL_EXECUTION_DEFAULTS.get(canonical, {}).get("sandbox_profile")
    if isinstance(configured, str) and configured:
        return configured
    if risk in {"mutating", "destructive"}:
        return "agent_draft_write"
    if risk == "forbidden":
        return "analysis_only"
    return "analysis_readonly"


def tool_protocol_spec(tool: object) -> ToolProtocolSpec:
    canonical = canonical_tool_name(tool)
    configured = TOOL_EXECUTION_DEFAULTS.get(canonical, {})
    risk = default_tool_risk(canonical)
    spill_policy = configured.get("artifact_spill_policy") or ("never" if risk in {"safe", "forbidden"} else "on_truncation")
    if spill_policy not in {"never", "on_truncation", "always"}:
        spill_policy = "on_truncation"
    return ToolProtocolSpec(
        canonical=canonical,
        version=TOOL_PROTOCOL_VERSION,
        aliases=tuple(sorted(alias for alias, target in CANONICAL_TOOL_ALIASES.items() if target == canonical and alias != canonical)),
        risk=risk,
        approval_class=default_tool_approval_class(canonical),
        sandbox_profile=default_tool_sandbox_profile(canonical),
        concurrency_safe=bool(configured.get("concurrency_safe", False)),
        timeout_seconds=int(configured.get("timeout_seconds") or 25),
        output_cap_chars=int(configured.get("output_cap_chars") or 6000),
        artifact_spill_policy=spill_policy,  # type: ignore[arg-type]
        input_schema=TOOL_INPUT_SCHEMAS.get(canonical, {}),
        output_schema=TOOL_OUTPUT_SCHEMAS.get(canonical, {}),
        deferred=bool(configured.get("deferred") or risk in {"mutating", "destructive"}),
        dynamic=bool(configured.get("dynamic")),
    )


def tool_registry_contract() -> dict[str, Any]:
    canonical_tools = sorted(
        set(CANONICAL_TOOL_ALIASES.values())
        | set(TOOL_RISK_DEFAULTS)
        | set(TOOL_INPUT_SCHEMAS)
        | set(TOOL_OUTPUT_SCHEMAS)
        | set(TOOL_EXECUTION_DEFAULTS)
    )
    tools = [tool_protocol_spec(canonical).as_contract() for canonical in canonical_tools]
    return {
        "tool_protocol_version": TOOL_PROTOCOL_VERSION,
        "schema": "grounded.tool_registry_contract.v2",
        "alias_index": dict(sorted(CANONICAL_TOOL_ALIASES.items())),
        "compatibility_policy": "Canonical tool contracts are additive within grounded.tool.v2; legacy aliases must resolve to a canonical tool without changing input/output schemas.",
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
        "governance_fields": [
            "risk",
            "approval_class",
            "sandbox_profile",
            "concurrency_safe",
            "timeout_seconds",
            "output_cap_chars",
            "artifact_spill_policy",
        ],
        "tools": tools,
    }


TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "file.list": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "file.read": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "artifact.read": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "artifact_ref": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "search.grep": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "pattern": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "semantic.scan": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "diff.inspect": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "shell.exec": {
        "type": "object",
        "additionalProperties": False,
        "required": ["command"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "command": {"type": "string"},
            "process_id": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    "browser.verify": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "contract.compile": {
        "type": "object",
        "additionalProperties": False,
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
        "additionalProperties": False,
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
        "additionalProperties": False,
        "required": ["workspace_id", "run_id"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["exact", "final", ""]},
            "reason": {"type": "string"},
        },
    },
    "tool.search": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id", "query"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "query": {"type": "string"},
            "domain": {"type": "string", "enum": ["", "deploy", "browser", "database", "payments", "cms", "github", "vercel"]},
            "intent": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    "lsp.diagnostics": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "files": {"type": "array", "items": {"type": "string"}},
            "changed_only": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
    "lsp.symbol_context": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {"workspace_id": {"type": "string"}, "run_id": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    },
    "lsp.find_references": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {"workspace_id": {"type": "string"}, "run_id": {"type": "string"}, "symbol": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    },
    "lsp.definition": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {"workspace_id": {"type": "string"}, "run_id": {"type": "string"}, "symbol": {"type": "string"}, "query": {"type": "string"}, "pattern": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    },
    "lsp.route_graph": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {"workspace_id": {"type": "string"}, "run_id": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    },
    "lsp.route_static_context": {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_id"],
        "properties": {"workspace_id": {"type": "string"}, "run_id": {"type": "string"}, "targets": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    },
    "patch.apply": {
        "type": "object",
        "additionalProperties": False,
        "required": ["file_path"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "file_path": {"type": "string"},
            "diff": {"type": "string"},
            "content": {"type": "string"},
            "worker_id": {"type": "string"},
            "owner_scope": {"type": "string"},
            "reason": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "file.write": {
        "type": "object",
        "additionalProperties": False,
        "required": ["file_path", "content"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "file_path": {"type": "string"},
            "content": {"type": "string"},
            "worker_id": {"type": "string"},
            "owner_scope": {"type": "string"},
            "reason": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "file.edit": {
        "type": "object",
        "additionalProperties": False,
        "required": ["file_path", "old_string", "new_string"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "worker_id": {"type": "string"},
            "owner_scope": {"type": "string"},
            "reason": {"type": "string"},
            "allowed_file_graph": {"type": "object"},
        },
    },
    "todo.write": {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "items": {"type": "array", "items": {"type": "object"}},
            "reason": {"type": "string"},
        },
    },
    "user.ask": {
        "type": "object",
        "additionalProperties": False,
        "required": ["question"],
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
    "review.start": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_id": {"type": "string"},
            "run_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
    },
}

TOOL_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "file.list": {"type": "object", "required": ["paths"]},
    "file.read": {"type": "object", "required": ["files"]},
    "artifact.read": {"type": "object", "required": ["artifact_ref", "found"]},
    "search.grep": {"type": "object", "required": ["pattern", "matches"]},
    "semantic.scan": {"type": "object", "required": ["graph"]},
    "diff.inspect": {"type": "object", "required": ["paths", "diff"]},
    "shell.exec": {"type": "object", "required": ["process_id", "success"]},
    "browser.verify": {"type": "object", "required": ["workflow_results", "preview"]},
    "contract.compile": {"type": "object", "required": ["contract", "allowed_file_graph"]},
    "registry.sync": {"type": "object", "required": ["snapshot", "regenerated_files", "repair_recipes"]},
    "checks.run": {"type": "object", "required": ["preview", "failed_checks"]},
    "tool.search": {"type": "object", "required": ["items", "summary"]},
    "lsp.diagnostics": {"type": "object", "required": ["status", "items"]},
    "lsp.symbol_context": {"type": "object", "required": ["items"]},
    "lsp.definition": {"type": "object", "required": ["items"]},
    "lsp.find_references": {"type": "object", "required": ["items"]},
    "lsp.route_graph": {"type": "object", "required": ["nodes", "edges"]},
    "lsp.route_static_context": {"type": "object", "required": ["routes", "frontend_api_refs"]},
    "patch.apply": {"type": "object", "required": ["status", "deferred_changes"]},
    "file.write": {"type": "object", "required": ["status", "deferred_changes"]},
    "file.edit": {"type": "object", "required": ["status", "deferred_changes"]},
    "todo.write": {"type": "object", "required": ["status", "items"]},
    "user.ask": {"type": "object", "required": ["status", "question"]},
    "review.start": {"type": "object", "required": ["status", "findings"]},
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
    approval_payload = dict(approval or {"required": False, "status": "not_required"})
    approval_payload.setdefault("class", default_tool_approval_class(canonical))
    payload = {
        "tool_call_id": tool_call_id or f"tool_{uuid4().hex}",
        "tool": canonical,
        "version": TOOL_PROTOCOL_VERSION,
        "status": resolved_status,
        "input": input_payload or {},
        "risk": risk or default_tool_risk(canonical),
        "approval": approval_payload,
        "approval_id": approval_payload.get("approval_id"),
        "sandbox_profile": sandbox_profile or default_tool_sandbox_profile(canonical),
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
    return ToolEnvelope.model_validate(payload).model_dump(mode="json", by_alias=True)


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
