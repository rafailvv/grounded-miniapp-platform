from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


SandboxProfile = Literal["analysis_readonly", "agent_draft_write", "source_apply_gate", "developer_bypass"]
SandboxOperation = Literal["read", "write", "delete", "copy", "apply", "exec"]
SandboxNetworkMode = Literal["blocked", "allowed"]
SandboxEnforcement = Literal["hard", "policy_only", "unavailable"]
SandboxKillReason = Literal["none", "timeout", "manual_terminate", "signal", "startup_failed", "sandbox_blocked", "policy_blocked", "not_started"]


class SandboxViolation(StrictModel):
    code: str
    message: str
    path: str | None = None
    blocking: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class SandboxPathDecision(StrictModel):
    profile: SandboxProfile
    operation: SandboxOperation
    path: str
    normalized_path: str
    root: str
    absolute_path: str
    resolved_path: str
    allowed: bool
    reason: str
    exists: bool = False
    is_file: bool = False
    is_dir: bool = False
    is_symlink: bool = False
    is_hardlink: bool = False
    hardlink_count: int = 0
    sha256: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    violations: list[SandboxViolation] = Field(default_factory=list)


class SandboxNetworkDecision(StrictModel):
    mode: SandboxNetworkMode = "blocked"
    allowed: bool = False
    provider: str = "none"
    enforcement: SandboxEnforcement = "unavailable"
    reason: str = ""


class SandboxExecutionPlan(StrictModel):
    profile: SandboxProfile
    provider: str
    enforcement: SandboxEnforcement
    cwd: str
    argv: list[str] = Field(default_factory=list)
    wrapped_argv: list[str] = Field(default_factory=list)
    read_roots: list[str] = Field(default_factory=list)
    write_roots: list[str] = Field(default_factory=list)
    network: SandboxNetworkDecision = Field(default_factory=SandboxNetworkDecision)
    allowed: bool = True
    reason: str = ""
    violations: list[SandboxViolation] = Field(default_factory=list)


class SandboxFilesystemAllowlist(StrictModel):
    schema_: str = Field(default="grounded.sandbox.filesystem_allowlist.v1", alias="schema")
    root: str
    cwd: str
    read_roots: list[str] = Field(default_factory=list)
    write_roots: list[str] = Field(default_factory=list)
    denied_parts: list[str] = Field(default_factory=list)
    denied_names: list[str] = Field(default_factory=list)
    denied_suffixes: list[str] = Field(default_factory=list)
    generated_prefix: str = ""
    path_safety: dict[str, Any] = Field(default_factory=dict)
    allowed_operations: list[SandboxOperation] = Field(default_factory=list)


class SandboxEnvironmentSnapshot(StrictModel):
    schema_: str = Field(default="grounded.sandbox.environment_snapshot.v1", alias="schema")
    process_id: str
    host_pid: int | None = None
    created_at: str
    workspace_root: str
    isolated_workspace: str
    cwd: str
    argv: list[str] = Field(default_factory=list)
    resolved_argv: list[str] = Field(default_factory=list)
    wrapped_argv: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    env_sha256: str
    tmp_dir: str
    home_dir: str
    resource_limits: dict[str, int] = Field(default_factory=dict)
    os_name: str
    profile: SandboxProfile
    provider: str
    enforcement: SandboxEnforcement
    network_mode: SandboxNetworkMode
    snapshot_sha256: str


class SandboxLogStreamCapture(StrictModel):
    stream: Literal["stdout", "stderr"]
    head_chars: int = 0
    tail_chars: int = 0
    total_chars: int = 0
    omitted_chars: int = 0
    chunk_count: int = 0
    sha256: str | None = None
    artifact_ref: str | None = None
    truncated_excerpt: bool = False
    truncated_full: bool = False


class SandboxLogCapture(StrictModel):
    schema_: str = Field(default="grounded.sandbox.log_capture.v1", alias="schema")
    max_excerpt_chars: int
    full_spool_max_chars: int
    output_delta_count: int = 0
    stdout: SandboxLogStreamCapture
    stderr: SandboxLogStreamCapture


class SandboxKillDiagnostics(StrictModel):
    schema_: str = Field(default="grounded.sandbox.kill_diagnostics.v1", alias="schema")
    killed: bool = False
    reason: SandboxKillReason = "none"
    timed_out: bool = False
    timeout_seconds: int | None = None
    grace_seconds: float | None = None
    process_group: bool = False
    requested_signal: str | None = None
    final_signal: str | None = None
    exit_code: int | None = None
    return_signal: str | None = None
    detail: str | None = None
    terminated_at: str | None = None


class SandboxPreviewLifecycle(StrictModel):
    schema_: str = Field(default="grounded.sandbox.preview_lifecycle.v1", alias="schema")
    workspace_id: str
    runtime_mode: Literal["inline", "docker", "local"]
    status: str
    stage: str
    project_name: str | None = None
    proxy_port: int | None = None
    url: str | None = None
    draft_run_id: str | None = None
    cleanup_attempted: bool = False
    reused_existing_runtime: bool = False
    failure_kind: str | None = None
    cooldown_until: str | None = None
    lifecycle_events: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class SandboxRuntimeBoundary(StrictModel):
    schema_: str = Field(default="grounded.sandbox.runtime_boundary.v1", alias="schema")
    process_id: str
    mode: str = "workspace_process_group"
    profile: SandboxProfile
    provider: str
    enforcement: SandboxEnforcement
    workspace_root: str
    isolated_workspace: str
    cwd: str
    filesystem: SandboxFilesystemAllowlist
    network: SandboxNetworkDecision
    timeout_seconds: int
    resource_limits: dict[str, int] = Field(default_factory=dict)
    process_group_kill: bool = False
    environment: SandboxEnvironmentSnapshot
    log_capture_policy: dict[str, Any] = Field(default_factory=dict)
    preview_lifecycle: SandboxPreviewLifecycle | None = None
    violations: list[SandboxViolation] = Field(default_factory=list)


class SandboxRuntimeManifest(StrictModel):
    schema_: str = Field(default="grounded.sandbox_runtime.manifest.v1", alias="schema")
    profiles: dict[str, Any] = Field(default_factory=dict)
    provider: str
    enforcement: SandboxEnforcement
    execution_boundary: dict[str, Any] = Field(default_factory=dict)
    path_safety: dict[str, Any] = Field(default_factory=dict)
    network_policy: dict[str, Any] = Field(default_factory=dict)
    process_timeout: dict[str, Any] = Field(default_factory=dict)
    log_capture: dict[str, Any] = Field(default_factory=dict)
    killed_process_diagnostics: dict[str, Any] = Field(default_factory=dict)
    preview_lifecycle: dict[str, Any] = Field(default_factory=dict)
    reproducibility: dict[str, Any] = Field(default_factory=dict)


class ApplySafetyItem(StrictModel):
    operation_id: str | None = None
    op: str
    path: str
    decision: SandboxPathDecision
    snapshot: dict[str, Any] = Field(default_factory=dict)


class ApplySafetyReport(StrictModel):
    schema_: str = Field(default="grounded.apply_safety_report.v1", alias="schema")
    status: Literal["passed", "blocked"] = "passed"
    profile: SandboxProfile
    root: str
    items: list[ApplySafetyItem] = Field(default_factory=list)
    violations: list[SandboxViolation] = Field(default_factory=list)
