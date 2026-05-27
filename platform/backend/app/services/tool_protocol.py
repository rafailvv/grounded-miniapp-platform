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
ToolSideEffectClass = Literal["none", "read_workspace", "write_draft", "execute_process", "verification", "external_browser", "approval_request", "unknown"]
ToolKind = Literal["read_only", "mutating", "verification", "unknown"]
SandboxProfile = Literal["analysis_readonly", "agent_draft_write", "source_apply_gate", "developer_bypass", "analysis_only", "agent_draft", "apply_gate"]

DEFAULT_MODE_VISIBILITY = ("default", "read_only", "worker_branch")
MUTATION_MODE_VISIBILITY = ("mutation_required", "mutating", "worker_branch")
VERIFICATION_MODE_VISIBILITY = ("default", "verification")


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
    description: str = ""
    capability_tags: tuple[str, ...] = ()
    allowed_paths: dict[str, Any] | None = None
    side_effects: tuple[ToolSideEffectClass, ...] = ()
    parallel_safe: bool | None = None
    result_summarization: dict[str, Any] | None = None
    retry_policy: dict[str, Any] | None = None
    failure_signatures: dict[str, Any] | None = None
    kind: ToolKind = "unknown"
    model_name: str | None = None
    model_visible: bool = False
    internal_only: bool = False
    mode_visibility: tuple[str, ...] = ()
    activity: str = "reading"
    progress_label: str = "Reading workspace context"
    argument_progress_fields: tuple[str, ...] = ()

    def as_contract(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "version": self.version,
            "description": self.description,
            "aliases": list(self.aliases),
            "capabilities": list(self.capability_tags),
            "risk": self.risk,
            "approval_class": self.approval_class,
            "sandbox_profile": self.sandbox_profile,
            "concurrency_safe": self.concurrency_safe,
            "parallel_safe": bool(self.parallel_safe if self.parallel_safe is not None else self.concurrency_safe),
            "timeout_seconds": self.timeout_seconds,
            "output_cap_chars": self.output_cap_chars,
            "artifact_spill_policy": self.artifact_spill_policy,
            "allowed_paths": self.allowed_paths or default_tool_allowed_paths(self.canonical),
            "side_effects": list(self.side_effects or default_tool_side_effects(self.canonical)),
            "side_effect_class": tool_side_effect_class(self.side_effects or default_tool_side_effects(self.canonical)),
            "result_summarization": self.result_summarization or default_tool_result_summarization(self.canonical),
            "retry_policy": self.retry_policy or default_tool_retry_policy(self.canonical),
            "failure_signatures": self.failure_signatures or default_tool_failure_signatures(self.canonical),
            "kind": self.kind,
            "model_name": self.model_name,
            "model_visible": self.model_visible,
            "internal_only": self.internal_only,
            "mode_visibility": list(self.mode_visibility),
            "activity": self.activity,
            "progress_label": self.progress_label,
            "argument_progress_fields": list(self.argument_progress_fields),
            "deferred": self.deferred,
            "dynamic": self.dynamic,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


@dataclass(frozen=True)
class ToolDefinition:
    canonical: str
    aliases: tuple[str, ...]
    risk: ToolRisk
    kind: ToolKind
    approval_class: ToolApprovalClass
    sandbox_profile: str
    concurrency_safe: bool
    parallel_safe: bool
    timeout_seconds: int
    output_cap_chars: int
    artifact_spill_policy: ToolArtifactSpillPolicy
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    description: str
    capability_tags: tuple[str, ...]
    allowed_paths: dict[str, Any]
    side_effects: tuple[ToolSideEffectClass, ...]
    side_effect_class: ToolSideEffectClass
    result_summarization: dict[str, Any]
    retry_policy: dict[str, Any]
    failure_signatures: dict[str, Any]
    deferred: bool
    dynamic: bool
    model_name: str | None
    model_visible: bool
    internal_only: bool
    mode_visibility: tuple[str, ...]
    activity: str
    progress_label: str
    argument_progress_fields: tuple[str, ...]


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
    "lsp.diagnostics": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000, "dynamic": True},
    "lsp.symbol_context": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 10000, "dynamic": True},
    "lsp.definition": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 8000, "dynamic": True},
    "lsp.find_references": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 10000, "dynamic": True},
    "lsp.route_graph": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 14000, "dynamic": True},
    "lsp.route_static_context": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000, "dynamic": True},
    "diff.inspect": {"concurrency_safe": True, "timeout_seconds": 25, "output_cap_chars": 12000},
    "checks.run": {"concurrency_safe": False, "timeout_seconds": 120, "output_cap_chars": 10000, "approval_class": "policy", "dynamic": True},
    "browser.verify": {
        "concurrency_safe": False,
        "timeout_seconds": 180,
        "output_cap_chars": 14000,
        "approval_class": "policy",
        "dynamic": True,
    },
    "shell.exec": {"concurrency_safe": True, "timeout_seconds": 30, "output_cap_chars": 6000, "approval_class": "policy", "dynamic": True},
    "file.write": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "file.edit": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "patch.apply": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "contract.compile": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "registry.sync": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000, "approval_class": "human", "deferred": True},
    "todo.write": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "user.ask": {"concurrency_safe": False, "timeout_seconds": 25, "output_cap_chars": 6000},
    "review.start": {"concurrency_safe": False, "timeout_seconds": 120, "output_cap_chars": 10000, "approval_class": "policy"},
}

TOOL_CAPABILITY_METADATA: dict[str, dict[str, Any]] = {
    "file.list": {"description": "List visible workspace files.", "capabilities": ["filesystem.list", "workspace.context", "read_only"]},
    "file.read": {"description": "Read selected visible workspace files.", "capabilities": ["filesystem.read", "workspace.context", "read_only"]},
    "artifact.read": {"description": "Read stored run/tool artifacts by ref.", "capabilities": ["artifact.read", "trace.context", "read_only"]},
    "search.grep": {"description": "Search visible workspace files by text pattern.", "capabilities": ["filesystem.search", "workspace.context", "read_only"]},
    "semantic.scan": {"description": "Build a semantic source graph for app code.", "capabilities": ["semantic.index", "code_graph", "read_only"]},
    "tool.search": {"description": "Discover optional deferred tools without enabling them by default.", "capabilities": ["tool.discovery", "capability.routing", "read_only"]},
    "lsp.diagnostics": {"description": "Collect targeted language diagnostics.", "capabilities": ["lsp.diagnostics", "code_health", "read_only"]},
    "lsp.symbol_context": {"description": "Read symbol-adjacent context.", "capabilities": ["lsp.symbols", "code_context", "read_only"]},
    "lsp.definition": {"description": "Find symbol definitions.", "capabilities": ["lsp.definition", "code_navigation", "read_only"]},
    "lsp.find_references": {"description": "Find symbol references.", "capabilities": ["lsp.references", "code_navigation", "read_only"]},
    "lsp.route_graph": {"description": "Build route graph context.", "capabilities": ["route_graph", "backend_frontend_contract", "read_only"]},
    "lsp.route_static_context": {"description": "Read route and static UI cross-links.", "capabilities": ["route_static_context", "backend_frontend_contract", "read_only"]},
    "diff.inspect": {"description": "Inspect draft/source diff.", "capabilities": ["diff.read", "change_review", "read_only"]},
    "checks.run": {"description": "Run serialized validation checks.", "capabilities": ["validation", "checks", "verification"]},
    "browser.verify": {"description": "Run browser/API workflow verification against preview.", "capabilities": ["browser", "preview", "verification"]},
    "shell.exec": {"description": "Run governed diagnostic shell commands.", "capabilities": ["shell.exec", "diagnostics", "process"]},
    "file.write": {"description": "Propose one draft file replacement.", "capabilities": ["filesystem.write", "draft_mutation", "deferred"]},
    "file.edit": {"description": "Propose one exact-string draft file edit.", "capabilities": ["filesystem.edit", "draft_mutation", "deferred"]},
    "patch.apply": {"description": "Propose one strict patch to a draft file.", "capabilities": ["patch.apply", "draft_mutation", "deferred"]},
    "contract.compile": {"description": "Compile a typed mini-app contract.", "capabilities": ["contract.compile", "planning"]},
    "registry.sync": {"description": "Synchronize generated registry metadata.", "capabilities": ["registry.sync", "draft_mutation", "deferred"]},
    "todo.write": {"description": "Update agent task state.", "capabilities": ["todo", "planning"]},
    "user.ask": {"description": "Request user input.", "capabilities": ["user_input", "approval_request"]},
    "review.start": {"description": "Start automated review.", "capabilities": ["review", "verification"]},
}

TOOL_SIDE_EFFECTS: dict[str, tuple[ToolSideEffectClass, ...]] = {
    "file.list": ("read_workspace",),
    "file.read": ("read_workspace",),
    "artifact.read": ("read_workspace",),
    "search.grep": ("read_workspace",),
    "semantic.scan": ("read_workspace",),
    "tool.search": ("none",),
    "lsp.diagnostics": ("read_workspace",),
    "lsp.symbol_context": ("read_workspace",),
    "lsp.definition": ("read_workspace",),
    "lsp.find_references": ("read_workspace",),
    "lsp.route_graph": ("read_workspace",),
    "lsp.route_static_context": ("read_workspace",),
    "diff.inspect": ("read_workspace",),
    "checks.run": ("verification", "execute_process"),
    "browser.verify": ("verification", "external_browser"),
    "shell.exec": ("execute_process",),
    "file.write": ("write_draft", "approval_request"),
    "file.edit": ("write_draft", "approval_request"),
    "patch.apply": ("write_draft", "approval_request"),
    "contract.compile": ("none",),
    "registry.sync": ("write_draft", "approval_request"),
    "todo.write": ("none",),
    "user.ask": ("approval_request",),
    "review.start": ("verification",),
}

DEFAULT_READ_PATH_POLICY = {
    "read": ["miniapp/**", "README.md", "docs/**"],
    "write": [],
    "deny": [
        ".git/**",
        "node_modules/**",
        "dist/**",
        "build/**",
        ".sandbox/**",
        "miniapp/app/generated/**",
        "miniapp/app/main.py",
        "miniapp/app/routes/role_pages.py",
        "miniapp/app/routes/role_routes.py",
    ],
}
DEFAULT_WRITE_PATH_POLICY = {
    "read": DEFAULT_READ_PATH_POLICY["read"],
    "write": ["miniapp/app/static/**", "miniapp/app/routes/**", "miniapp/app/services/**", "miniapp/tests/**", "miniapp/requirements.txt", "miniapp/package*.json", "README.md", "docs/**"],
    "deny": DEFAULT_READ_PATH_POLICY["deny"],
    "write_mode": "deferred_draft_action_only",
}

TOOL_PATH_POLICIES: dict[str, dict[str, Any]] = {
    "file.write": DEFAULT_WRITE_PATH_POLICY,
    "file.edit": DEFAULT_WRITE_PATH_POLICY,
    "patch.apply": DEFAULT_WRITE_PATH_POLICY,
    "registry.sync": {
        **DEFAULT_WRITE_PATH_POLICY,
        "write": ["miniapp/app/generated/**"],
        "deny": [item for item in DEFAULT_READ_PATH_POLICY["deny"] if item != "miniapp/app/generated/**"],
    },
    "shell.exec": {
        "read": DEFAULT_READ_PATH_POLICY["read"],
        "write": [".sandbox/tmp/**", ".sandbox/home/**"],
        "deny": DEFAULT_READ_PATH_POLICY["deny"],
        "command_policy": "exec_policy_service_prefix_rules",
    },
    "checks.run": {**DEFAULT_READ_PATH_POLICY, "write": [".sandbox/**", "miniapp/.pytest_cache/**"]},
    "browser.verify": {**DEFAULT_READ_PATH_POLICY, "write": [".sandbox/**"]},
}

TOOL_RESULT_SUMMARIZATION: dict[str, dict[str, Any]] = {
    "default": {
        "mode": "structured_summary",
        "include": ["status", "counts", "targets", "changed_files", "artifacts", "failure_signature"],
        "max_inline_chars": 1200,
        "spill_full_result": "on_truncation",
    },
    "file.read": {"mode": "file_excerpt_summary", "include": ["file_count", "paths", "omitted_chars"], "max_inline_chars": 1800, "spill_full_result": "on_truncation"},
    "search.grep": {"mode": "match_summary", "include": ["match_count", "paths"], "max_inline_chars": 1400, "spill_full_result": "on_truncation"},
    "shell.exec": {"mode": "process_summary", "include": ["semantic_status", "exit_code", "stdout_ref", "stderr_ref", "killed_diagnostics"], "max_inline_chars": 1600, "spill_full_result": "always_for_large_output"},
    "checks.run": {"mode": "validation_summary", "include": ["failed_checks", "preview", "failure_signature"], "max_inline_chars": 1800, "spill_full_result": "on_truncation"},
    "browser.verify": {"mode": "workflow_summary", "include": ["workflow_results", "preview", "failure_signature"], "max_inline_chars": 1800, "spill_full_result": "on_truncation"},
}

TOOL_RETRY_POLICIES: dict[str, dict[str, Any]] = {
    "default": {"retryable": True, "max_attempts": 2, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
    "tool.search": {"retryable": False, "max_attempts": 1, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
    "shell.exec": {"retryable": True, "max_attempts": 1, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
    "file.write": {"retryable": True, "max_attempts": 2, "backoff": "none", "requires_fresh_context": True, "first_retry_tool": "read_files", "stop_on_same_failure_signature": True},
    "file.edit": {"retryable": True, "max_attempts": 2, "backoff": "none", "requires_fresh_context": True, "first_retry_tool": "read_files", "stop_on_same_failure_signature": True},
    "patch.apply": {"retryable": True, "max_attempts": 2, "backoff": "none", "requires_fresh_context": True, "first_retry_tool": "read_files", "stop_on_same_failure_signature": True},
    "checks.run": {"retryable": True, "max_attempts": 1, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
    "browser.verify": {"retryable": True, "max_attempts": 1, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
    "user.ask": {"retryable": False, "max_attempts": 1, "backoff": "none", "requires_fresh_context": False, "stop_on_same_failure_signature": True},
}

TOOL_FAILURE_SIGNATURES: dict[str, dict[str, Any]] = {
    "default": {"format": "{tool}:{error_code}:{stable_detail_hash}", "fields": ["tool", "error.code", "details"]},
    "file.edit": {"format": "file.edit:{error_code}:{file_path}", "fields": ["tool", "error.code", "input.file_path"]},
    "file.write": {"format": "file.write:{error_code}:{file_path}", "fields": ["tool", "error.code", "input.file_path"]},
    "patch.apply": {"format": "patch.apply:{error_code}:{file_path}", "fields": ["tool", "error.code", "input.file_path"]},
    "shell.exec": {"format": "shell.exec:{semantic_status}:{command_hash}", "fields": ["tool", "result.semantic_status", "input.command"]},
    "checks.run": {"format": "checks.run:{first_failed_check}", "fields": ["tool", "result.failed_checks.0.name"]},
    "browser.verify": {"format": "browser.verify:{first_failed_workflow}", "fields": ["tool", "result.workflow_results.0.name"]},
}

TOOL_MODEL_METADATA: dict[str, dict[str, Any]] = {
    "file.list": {
        "kind": "read_only",
        "model_name": "list_files",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "reading",
        "progress_label": "Reading workspace file list",
        "argument_progress_fields": ("targets",),
    },
    "file.read": {
        "kind": "read_only",
        "model_name": "read_files",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "reading",
        "progress_label": "Reading selected files",
        "argument_progress_fields": ("targets", "files"),
    },
    "search.grep": {
        "kind": "read_only",
        "model_name": "search_files",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Searching workspace",
        "argument_progress_fields": ("pattern", "targets"),
    },
    "diff.inspect": {
        "kind": "read_only",
        "model_name": "inspect_diff",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "reading",
        "progress_label": "Inspecting draft diff",
        "argument_progress_fields": ("targets",),
    },
    "artifact.read": {
        "kind": "read_only",
        "model_name": "read_artifact_ref",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "reading",
        "progress_label": "Reading stored tool artifact",
        "argument_progress_fields": ("artifact_ref", "targets"),
    },
    "semantic.scan": {
        "kind": "read_only",
        "model_name": "semantic_scan",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Scanning source semantics",
        "argument_progress_fields": ("targets",),
    },
    "tool.search": {
        "kind": "read_only",
        "model_name": "tool_search",
        "model_visible": True,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Discovering optional deferred tools",
        "argument_progress_fields": ("query", "domain", "intent"),
    },
    "lsp.diagnostics": {
        "kind": "read_only",
        "model_name": "lsp_diagnostics",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "checking",
        "progress_label": "Running targeted file diagnostics",
        "argument_progress_fields": ("targets", "files"),
    },
    "lsp.symbol_context": {
        "kind": "read_only",
        "model_name": "lsp_symbol_context",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Reading symbol context",
        "argument_progress_fields": ("query", "pattern", "targets"),
    },
    "lsp.definition": {
        "kind": "read_only",
        "model_name": "lsp_definition",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Finding symbol definition",
        "argument_progress_fields": ("symbol", "query", "pattern", "targets"),
    },
    "lsp.find_references": {
        "kind": "read_only",
        "model_name": "lsp_find_references",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Finding symbol references",
        "argument_progress_fields": ("symbol", "query", "pattern", "targets"),
    },
    "lsp.route_graph": {
        "kind": "read_only",
        "model_name": "lsp_route_graph",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Building route graph",
        "argument_progress_fields": ("targets",),
    },
    "lsp.route_static_context": {
        "kind": "read_only",
        "model_name": "lsp_route_static_context",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "searching",
        "progress_label": "Reading route and static UI context",
        "argument_progress_fields": ("targets",),
    },
    "shell.exec": {
        "kind": "read_only",
        "model_name": "run_command",
        "model_visible": False,
        "mode_visibility": DEFAULT_MODE_VISIBILITY,
        "activity": "running_command",
        "progress_label": "Running diagnostic command",
        "argument_progress_fields": ("command", "process_id"),
    },
    "checks.run": {
        "kind": "verification",
        "model_name": "run_checks",
        "model_visible": False,
        "mode_visibility": VERIFICATION_MODE_VISIBILITY,
        "activity": "checking",
        "progress_label": "Running validation checks",
        "argument_progress_fields": ("targets", "mode"),
    },
    "browser.verify": {
        "kind": "verification",
        "model_name": "browser_verify",
        "model_visible": False,
        "mode_visibility": VERIFICATION_MODE_VISIBILITY,
        "activity": "browser_verifying",
        "progress_label": "Running browser workflow proof",
        "argument_progress_fields": ("targets",),
    },
    "patch.apply": {
        "kind": "mutating",
        "model_name": "apply_patch_to_draft",
        "model_visible": True,
        "mode_visibility": MUTATION_MODE_VISIBILITY,
        "activity": "applying_patch",
        "progress_label": "Applying one draft file patch",
        "argument_progress_fields": ("file_path", "diff"),
    },
    "file.write": {
        "kind": "mutating",
        "model_name": "write_file",
        "model_visible": True,
        "mode_visibility": MUTATION_MODE_VISIBILITY,
        "activity": "editing",
        "progress_label": "Writing one draft file",
        "argument_progress_fields": ("file_path", "content"),
    },
    "file.edit": {
        "kind": "mutating",
        "model_name": "edit_file_exact",
        "model_visible": True,
        "mode_visibility": MUTATION_MODE_VISIBILITY,
        "activity": "editing",
        "progress_label": "Applying an exact old/new string edit to one draft file",
        "argument_progress_fields": ("file_path", "old_string", "new_string"),
    },
    "contract.compile": {
        "kind": "read_only",
        "model_name": None,
        "model_visible": False,
        "internal_only": True,
        "mode_visibility": (),
        "activity": "planning",
        "progress_label": "Compiling the typed mini-app contract",
        "argument_progress_fields": ("prompt", "generation_mode"),
    },
    "registry.sync": {
        "kind": "mutating",
        "model_name": None,
        "model_visible": False,
        "internal_only": True,
        "mode_visibility": (),
        "activity": "checking",
        "progress_label": "Synchronizing prompt-contract metadata",
        "argument_progress_fields": ("contract_id",),
    },
    "todo.write": {
        "kind": "read_only",
        "model_name": None,
        "model_visible": False,
        "internal_only": True,
        "mode_visibility": (),
        "activity": "planning",
        "progress_label": "Updating the agent task plan",
        "argument_progress_fields": ("items",),
    },
    "user.ask": {
        "kind": "read_only",
        "model_name": None,
        "model_visible": False,
        "internal_only": True,
        "mode_visibility": (),
        "activity": "tool_progress",
        "progress_label": "Requesting user input",
        "argument_progress_fields": ("question", "choices"),
    },
    "review.start": {
        "kind": "verification",
        "model_name": None,
        "model_visible": False,
        "internal_only": True,
        "mode_visibility": (),
        "activity": "checking",
        "progress_label": "Starting an automated review",
        "argument_progress_fields": ("targets",),
    },
}


def _all_canonical_tool_names() -> list[str]:
    return sorted(
        set(CANONICAL_TOOL_ALIASES.values())
        | set(TOOL_RISK_DEFAULTS)
        | set(TOOL_INPUT_SCHEMAS)
        | set(TOOL_OUTPUT_SCHEMAS)
        | set(TOOL_EXECUTION_DEFAULTS)
        | set(TOOL_MODEL_METADATA)
    )


def tool_aliases(canonical: object) -> tuple[str, ...]:
    resolved = canonical_tool_name(canonical)
    return tuple(sorted(alias for alias, target in CANONICAL_TOOL_ALIASES.items() if target == resolved and alias != resolved))


def tool_definition(tool: object) -> ToolDefinition:
    canonical = canonical_tool_name(tool)
    configured = TOOL_EXECUTION_DEFAULTS.get(canonical, {})
    risk = default_tool_risk(canonical)
    metadata = default_tool_capability_metadata(canonical)
    model_metadata = TOOL_MODEL_METADATA.get(canonical, {})
    side_effects = default_tool_side_effects(canonical)
    side_effect_class = tool_side_effect_class(side_effects)
    spill_policy = configured.get("artifact_spill_policy") or ("never" if risk in {"safe", "forbidden"} else "on_truncation")
    if spill_policy not in {"never", "on_truncation", "always"}:
        spill_policy = "on_truncation"
    kind = str(model_metadata.get("kind") or ("mutating" if risk in {"mutating", "destructive"} else "read_only" if risk in {"safe", "read_only"} else "unknown"))
    if kind not in {"read_only", "mutating", "verification", "unknown"}:
        kind = "unknown"
    concurrency_safe = bool(configured.get("concurrency_safe", False))
    parallel_safe = bool(configured.get("parallel_safe", concurrency_safe and side_effect_class in {"none", "read_workspace"}))
    model_name = model_metadata.get("model_name")
    return ToolDefinition(
        canonical=canonical,
        aliases=tool_aliases(canonical),
        risk=risk,
        kind=kind,  # type: ignore[arg-type]
        approval_class=default_tool_approval_class(canonical),
        sandbox_profile=default_tool_sandbox_profile(canonical),
        concurrency_safe=concurrency_safe,
        parallel_safe=parallel_safe,
        timeout_seconds=int(configured.get("timeout_seconds") or 25),
        output_cap_chars=int(configured.get("output_cap_chars") or 6000),
        artifact_spill_policy=spill_policy,  # type: ignore[arg-type]
        input_schema=TOOL_INPUT_SCHEMAS.get(canonical, {}),
        output_schema=TOOL_OUTPUT_SCHEMAS.get(canonical, {}),
        description=str(metadata.get("description") or canonical),
        capability_tags=tuple(str(item) for item in metadata.get("capabilities") or []),
        allowed_paths=default_tool_allowed_paths(canonical),
        side_effects=side_effects,
        side_effect_class=side_effect_class,
        result_summarization=default_tool_result_summarization(canonical),
        retry_policy=default_tool_retry_policy(canonical),
        failure_signatures=default_tool_failure_signatures(canonical),
        deferred=bool(configured.get("deferred") or risk in {"mutating", "destructive"}),
        dynamic=bool(configured.get("dynamic")),
        model_name=str(model_name) if model_name else None,
        model_visible=bool(model_metadata.get("model_visible", False)),
        internal_only=bool(model_metadata.get("internal_only", False)),
        mode_visibility=tuple(str(item) for item in model_metadata.get("mode_visibility") or ()),
        activity=str(model_metadata.get("activity") or "reading"),
        progress_label=str(model_metadata.get("progress_label") or "Reading workspace context"),
        argument_progress_fields=tuple(str(item) for item in model_metadata.get("argument_progress_fields") or ()),
    )


def tool_definitions() -> dict[str, ToolDefinition]:
    return {canonical: tool_definition(canonical) for canonical in _all_canonical_tool_names()}


def tool_model_name(tool: object) -> str | None:
    return tool_definition(tool).model_name


def model_tool_for_canonical(tool: object) -> str:
    definition = tool_definition(tool)
    return definition.model_name or definition.canonical


def canonical_tool_for_model(tool: object) -> str:
    return canonical_tool_name(tool)


def model_visible_tool_names(*, include_dynamic: bool = False, include_internal: bool = False) -> set[str]:
    names: set[str] = set()
    for definition in tool_definitions().values():
        if not definition.model_name:
            continue
        if definition.internal_only and not include_internal:
            continue
        if not definition.model_visible and not include_dynamic:
            continue
        if definition.dynamic and not include_dynamic and not definition.model_visible:
            continue
        names.add(definition.model_name)
    return names


def registry_tool_names() -> set[str]:
    names = set(_all_canonical_tool_names())
    for definition in tool_definitions().values():
        if definition.model_name:
            names.add(definition.model_name)
        names.update(definition.aliases)
    return names


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


def default_tool_capability_metadata(tool: object) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    metadata = TOOL_CAPABILITY_METADATA.get(canonical)
    if metadata is not None:
        return {"description": str(metadata.get("description") or canonical), "capabilities": list(metadata.get("capabilities") or [])}
    return {"description": canonical, "capabilities": [canonical.replace(".", "_")]}


def default_tool_side_effects(tool: object) -> tuple[ToolSideEffectClass, ...]:
    canonical = canonical_tool_name(tool)
    configured = TOOL_SIDE_EFFECTS.get(canonical)
    if configured:
        return configured
    risk = default_tool_risk(canonical)
    if risk == "mutating":
        return ("write_draft", "approval_request")
    if risk in {"read_only", "safe"}:
        return ("read_workspace",) if risk == "read_only" else ("none",)
    return ("unknown",)


def tool_side_effect_class(side_effects: tuple[ToolSideEffectClass, ...] | list[str]) -> ToolSideEffectClass:
    ordered: list[ToolSideEffectClass] = [
        "write_draft",
        "external_browser",
        "verification",
        "execute_process",
        "approval_request",
        "read_workspace",
        "none",
        "unknown",
    ]
    values = {str(item) for item in side_effects}
    for candidate in ordered:
        if candidate in values:
            return candidate
    return "unknown"


def default_tool_allowed_paths(tool: object) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    configured = TOOL_PATH_POLICIES.get(canonical)
    if configured is not None:
        return dict(configured)
    if default_tool_risk(canonical) == "mutating":
        return dict(DEFAULT_WRITE_PATH_POLICY)
    return dict(DEFAULT_READ_PATH_POLICY)


def default_tool_result_summarization(tool: object) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    return dict(TOOL_RESULT_SUMMARIZATION.get(canonical) or TOOL_RESULT_SUMMARIZATION["default"])


def default_tool_retry_policy(tool: object) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    return dict(TOOL_RETRY_POLICIES.get(canonical) or TOOL_RETRY_POLICIES["default"])


def default_tool_failure_signatures(tool: object) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    return dict(TOOL_FAILURE_SIGNATURES.get(canonical) or TOOL_FAILURE_SIGNATURES["default"])


def tool_protocol_spec(tool: object) -> ToolProtocolSpec:
    definition = tool_definition(tool)
    return ToolProtocolSpec(
        canonical=definition.canonical,
        version=TOOL_PROTOCOL_VERSION,
        aliases=definition.aliases,
        risk=definition.risk,
        approval_class=definition.approval_class,
        sandbox_profile=definition.sandbox_profile,
        concurrency_safe=definition.concurrency_safe,
        timeout_seconds=definition.timeout_seconds,
        output_cap_chars=definition.output_cap_chars,
        artifact_spill_policy=definition.artifact_spill_policy,
        input_schema=definition.input_schema,
        output_schema=definition.output_schema,
        deferred=definition.deferred,
        dynamic=definition.dynamic,
        description=definition.description,
        capability_tags=definition.capability_tags,
        allowed_paths=definition.allowed_paths,
        side_effects=definition.side_effects,
        parallel_safe=definition.parallel_safe,
        result_summarization=definition.result_summarization,
        retry_policy=definition.retry_policy,
        failure_signatures=definition.failure_signatures,
        kind=definition.kind,
        model_name=definition.model_name,
        model_visible=definition.model_visible,
        internal_only=definition.internal_only,
        mode_visibility=definition.mode_visibility,
        activity=definition.activity,
        progress_label=definition.progress_label,
        argument_progress_fields=definition.argument_progress_fields,
    )


def tool_registry_contract() -> dict[str, Any]:
    tools = [tool_protocol_spec(canonical).as_contract() for canonical in _all_canonical_tool_names()]
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
            "capabilities",
            "risk",
            "approval_class",
            "sandbox_profile",
            "allowed_paths",
            "side_effects",
            "side_effect_class",
            "concurrency_safe",
            "parallel_safe",
            "timeout_seconds",
            "output_cap_chars",
            "artifact_spill_policy",
            "result_summarization",
            "retry_policy",
            "failure_signatures",
            "kind",
            "model_name",
            "model_visible",
            "internal_only",
            "mode_visibility",
            "activity",
            "progress_label",
            "argument_progress_fields",
        ],
        "json_schema_tools": {
            "input_schema_field": "input_schema",
            "output_schema_field": "output_schema",
            "additional_properties_default": False,
        },
        "parallel_execution_policy": {
            "parallel_safe_field": "parallel_safe",
            "router_rule": "Only read-workspace/none side-effect tools marked parallel_safe may run in concurrent batches.",
        },
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
            "domain": {"type": "string", "enum": ["", "deploy", "browser", "lsp", "verification", "shell", "image", "docs", "database", "payments", "cms", "github", "vercel"]},
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
    result_summary: dict[str, Any] | None = None,
    capabilities: list[str] | None = None,
    allowed_paths: dict[str, Any] | None = None,
    side_effects: list[str] | None = None,
    side_effect_class: str | None = None,
    parallel_safe: bool | None = None,
    retry_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical = canonical_tool_name(tool)
    spec = tool_protocol_spec(canonical)
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
    resolved_retry = dict(retry or retry_policy or spec.retry_policy or {})
    if normalized_error is not None:
        resolved_retry["retryable"] = bool(normalized_error.get("retryable"))
    else:
        resolved_retry.setdefault("retryable", bool(normalized_error and normalized_error.get("retryable")))
    resolved_retry.setdefault("attempt", 1)
    resolved_retry.setdefault("max_attempts", (retry_policy or spec.retry_policy or {}).get("max_attempts", 1))
    payload = {
        "tool_call_id": tool_call_id or f"tool_{uuid4().hex}",
        "tool": canonical,
        "version": TOOL_PROTOCOL_VERSION,
        "status": resolved_status,
        "input": input_payload or {},
        "capabilities": capabilities if capabilities is not None else list(spec.capability_tags),
        "risk": risk or default_tool_risk(canonical),
        "approval": approval_payload,
        "approval_id": approval_payload.get("approval_id"),
        "sandbox_profile": sandbox_profile or default_tool_sandbox_profile(canonical),
        "allowed_paths": allowed_paths if allowed_paths is not None else (spec.allowed_paths or default_tool_allowed_paths(canonical)),
        "side_effects": side_effects if side_effects is not None else list(spec.side_effects or default_tool_side_effects(canonical)),
        "side_effect_class": side_effect_class or tool_side_effect_class(spec.side_effects or default_tool_side_effects(canonical)),
        "parallel_safe": bool(parallel_safe if parallel_safe is not None else spec.parallel_safe),
        "progress": progress or [],
        "result": result or {},
        "result_summary": result_summary or {},
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
        "retry": resolved_retry,
        "retry_policy": retry_policy or spec.retry_policy or default_tool_retry_policy(canonical),
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
