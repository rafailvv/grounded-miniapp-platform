from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


SandboxProfile = Literal["analysis_readonly", "agent_draft_write", "source_apply_gate", "developer_bypass"]
SandboxOperation = Literal["read", "write", "delete", "copy", "apply", "exec"]
SandboxNetworkMode = Literal["blocked", "allowed"]
SandboxEnforcement = Literal["hard", "policy_only", "unavailable"]


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


class ApplySafetyItem(StrictModel):
    operation_id: str | None = None
    op: str
    path: str
    decision: SandboxPathDecision
    snapshot: dict[str, Any] = Field(default_factory=dict)


class ApplySafetyReport(StrictModel):
    schema: str = "grounded.apply_safety_report.v1"
    status: Literal["passed", "blocked"] = "passed"
    profile: SandboxProfile
    root: str
    items: list[ApplySafetyItem] = Field(default_factory=list)
    violations: list[SandboxViolation] = Field(default_factory=list)
