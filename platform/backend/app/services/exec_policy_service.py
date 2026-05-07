from __future__ import annotations

import re
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

from app.modules.miniapp_agent_loop.agent_command_policy import (
    DEFAULT_COMMAND_POLICY,
    AgentCommandPolicy,
    CommandPolicyDecision,
    configure_default_command_policy,
)
from app.services.sandbox_service import SandboxService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, ToolRisk


APPROVAL_PRESETS: dict[str, dict[str, Any]] = {
    "strict_manual": {
        "description": "Prompt for every permitted operation; forbidden commands remain blocked.",
        "auto_approve_risks": [],
    },
    "safe_auto": {
        "description": "Auto-accept permitted local diagnostics and draft mutations; forbidden commands remain blocked.",
        "auto_approve_risks": ["safe", "read_only", "mutating"],
    },
    "workspace_trusted": {
        "description": "Trust workspace-scoped diagnostics and draft writes; still block destructive and external network operations.",
        "auto_approve_risks": ["safe", "read_only", "mutating"],
    },
    "developer_bypass": {
        "description": "Developer-only bypass for local experimentation; forbidden commands remain blocked.",
        "auto_approve_risks": ["safe", "read_only", "mutating", "network", "destructive", "unknown"],
    },
}

SANDBOX_PROFILES: dict[str, dict[str, Any]] = {
    "analysis_readonly": {
        "description": "Read-only diagnostics and workspace inspection.",
        "writes": "none",
        "network": False,
    },
    "agent_draft_write": {
        "description": "Default agent mode; writes are restricted to the draft workspace.",
        "writes": "draft_workspace",
        "network": False,
    },
    "source_apply_gate": {
        "description": "Apply reviewed draft after strict-green checks or manual approval.",
        "writes": "source_workspace",
        "network": False,
    },
    "developer_bypass": {
        "description": "Local development bypass; forbidden commands still remain blocked.",
        "writes": "workspace",
        "network": True,
    },
}


SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|token|secret|password)=([^\s]+)", re.I),
    re.compile(r"(sk-[A-Za-z0-9_-]{12,})"),
)


class ExecPolicyService:
    """Central policy facade for command, path, and approval decisions."""

    def __init__(self, policy_path: Path | None = None, *, sandbox_service: SandboxService | None = None) -> None:
        self.policy_source = str(policy_path) if policy_path else "builtin"
        self.policy_status = "builtin"
        self.policy_errors: list[str] = []
        self.policy_validation: list[dict[str, Any]] = []
        self.policy = DEFAULT_COMMAND_POLICY
        self.sandbox_service = sandbox_service or SandboxService()
        selected_path = self._select_policy_path(policy_path)
        if selected_path is not None and selected_path.exists():
            self.policy_source = str(selected_path)
            try:
                loaded = AgentCommandPolicy.from_dsl_file(selected_path) if selected_path.suffix == ".codexpolicy" else AgentCommandPolicy.from_rule_file(selected_path)
                examples = loaded.validation_examples()
                self.policy_validation = examples
                failed = [item for item in examples if item.get("status") != "passed"]
                if failed:
                    raise ValueError(f"Policy examples failed: {failed[:3]}")
                self.policy = loaded
                self.policy_status = "loaded"
            except Exception as exc:
                self.policy_errors.append(str(exc))
                self.policy_status = "fallback_builtin"
                self.policy = DEFAULT_COMMAND_POLICY
        configure_default_command_policy(self.policy)

    @staticmethod
    def _select_policy_path(policy_path: Path | None) -> Path | None:
        if policy_path is None:
            return None
        if policy_path.suffix == ".codexpolicy":
            return policy_path if policy_path.exists() else None
        dsl_path = policy_path.with_suffix(".codexpolicy")
        if dsl_path.exists():
            return dsl_path
        return policy_path if policy_path.exists() else None

    def snapshot(self) -> dict[str, Any]:
        payload = self.policy.snapshot()
        payload.update(
            {
                "tool_protocol_version": TOOL_PROTOCOL_VERSION,
                "risk_model": ["safe", "read_only", "draft_write", "workspace_write", "network_limited", "dangerous_requires_approval", "forbidden", "unknown"],
                "network_policy": {
                    "mode": "blocked_by_default",
                    "allowed": False,
                    "hard_blocked": ["direct network tools", "package installs/updates", "git network subcommands", "proxy/network config flags"],
                },
                "approval_presets": APPROVAL_PRESETS,
                "sandbox_profiles": SANDBOX_PROFILES,
                "sandbox": {
                    **self.sandbox_service.manifest(),
                    "cwd": "draft workspace or miniapp subdirectory",
                    "network": "blocked by OS sandbox for model-facing and thread/API exec when provider is available",
                    "writes": "restricted to draft workspace paths; generated caches/build outputs are ignored by apply",
                    "path_traversal": "parent-directory traversal, symlink writes, and hardlink writes are denied",
                },
                "write_grants": self.write_grants(),
                "policy_file": {
                    "source": self.policy_source,
                    "status": self.policy_status,
                    "errors": list(self.policy_errors),
                    "validation": list(self.policy_validation),
                },
            }
        )
        return payload

    def evaluate_command(self, command: str, *, preset: str = "safe_auto", root: Path | None = None) -> dict[str, Any]:
        decision = self.policy.decide(command)
        risk = self._risk_for_decision(decision)
        approval = self._approval_for_risk(risk, decision=decision, preset=preset)
        return {
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "command": self.redact(command),
            "argv": [self.redact(item) for item in decision.argv],
            "resolved_argv": [self.redact(item) for item in decision.resolved_argv],
            "resolved_executable": decision.executable_resolution,
            "matched_rules": [self._redact_rule(item) for item in decision.matched_rules],
            "matched_amendments": [self._redact_rule(item) for item in decision.matched_amendments],
            "selected_decision": decision.action,
            "shell_parse": decision.parse_tree,
            "blocked_syntax": decision.blocked_syntax,
            "network_policy": decision.network_policy,
            "decision": self._decision_payload(decision, risk=risk),
            "approval": approval,
            "sandbox_summary": self.sandbox_summary(decision, risk=risk, preset=preset, root=root),
            "policy_file": {
                "source": self.policy_source,
                "status": self.policy_status,
                "errors": list(self.policy_errors),
            },
        }

    def doctor_check(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        examples = [item for item in snapshot.get("examples") or [] if isinstance(item, dict)]
        failed = [item for item in examples if item.get("status") != "passed"]
        policy_file = snapshot.get("policy_file") if isinstance(snapshot.get("policy_file"), dict) else {}
        status = "failed" if policy_file.get("status") == "fallback_builtin" or failed else "passed"
        if policy_file.get("status") == "builtin":
            status = "warning"
        return {
            "name": "exec_policy",
            "status": status,
            "details": str(policy_file.get("source") or "builtin"),
            "required": True,
            "policy_file": policy_file,
            "failed_examples": failed[:12],
            "matched_rule_count": len(snapshot.get("rules") or []),
        }

    def validate_workspace_path(self, path: str, *, operation: str) -> dict[str, Any]:
        normalized = self._normalize_path(path)
        allowed = bool(normalized) and not self._has_path_traversal(normalized)
        if allowed and operation in {"write", "edit", "patch"}:
            allowed = not self._is_read_only_path(normalized)
        risk: ToolRisk = "mutating" if operation in {"write", "edit", "patch"} else "read_only"
        if not allowed:
            risk = "forbidden"
        return {
            "path": normalized,
            "operation": operation,
            "allowed": allowed,
            "risk": risk,
            "reason": "Path is allowed by workspace grants." if allowed else "Path escapes or targets a read-only workspace area.",
        }

    def write_grants(self) -> dict[str, Any]:
        return {
            "allow": [
                "miniapp/app/**",
                "miniapp/tests/**",
                "miniapp/package*.json",
                "miniapp/requirements.txt",
                "docs/**",
                "README.md",
            ],
            "deny": [
                ".git/**",
                "node_modules/**",
                "dist/**",
                "build/**",
                ".cache/**",
                ".pytest_cache/**",
                ".mypy_cache/**",
                ".ruff_cache/**",
                ".next/**",
                ".vite/**",
            ],
        }

    def sandbox_summary(self, decision: CommandPolicyDecision | None = None, *, risk: ToolRisk | None = None, preset: str = "safe_auto", root: Path | None = None) -> dict[str, Any]:
        profile = self._sandbox_profile_for(risk or "unknown", preset=preset)
        execution_plan: dict[str, Any] = {}
        if root is not None and decision is not None:
            cwd = root / "miniapp" if decision.cwd_policy == "miniapp" else root
            plan = self.sandbox_service.build_execution_plan(
                root=root,
                cwd=cwd,
                argv=list(decision.resolved_argv or decision.argv),
                profile=profile,  # type: ignore[arg-type]
                network_mode="allowed" if profile == "developer_bypass" else "blocked",
                write_roots=[cwd / ".sandbox" / "tmp", cwd / ".sandbox" / "home"],
            )
            execution_plan = plan.model_dump(mode="json")
        return {
            "profile": profile,
            "profile_description": SANDBOX_PROFILES[profile]["description"],
            "cwd_policy": decision.cwd_policy if decision else "draft_workspace",
            "argv": list(decision.argv) if decision else [],
            "resolved_argv": list(decision.resolved_argv) if decision else [],
            "matched_prefix": list(decision.matched_prefix) if decision else [],
            "network_allowed": bool(SANDBOX_PROFILES[profile]["network"]),
            "network_policy": decision.network_policy if decision else {},
            "provider": self.sandbox_service.network_provider(),
            "enforcement": execution_plan.get("enforcement") or ("hard" if self.sandbox_service.network_provider() in {"sandbox-exec", "unshare"} else "unavailable"),
            "execution_plan": execution_plan,
            "path_traversal_blocked": True,
            "symlink_writes_blocked": True,
            "hardlink_writes_blocked": True,
            "shell_metacharacters_blocked": True,
            "host_executable_resolution": decision.executable_resolution if decision else {},
        }

    @staticmethod
    def redact(text: str) -> str:
        redacted = str(text or "")
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>" if match.lastindex and match.lastindex >= 2 else "<redacted-secret>", redacted)
        return redacted

    def _approval_for_risk(self, risk: ToolRisk, *, decision: CommandPolicyDecision, preset: str) -> dict[str, Any]:
        resolved_preset = preset if preset in APPROVAL_PRESETS else "safe_auto"
        if decision.action == "forbidden" or risk == "forbidden":
            return {"required": False, "status": "blocked", "preset": resolved_preset, "approval_id": None}
        required = False
        return {
            "required": required,
            "status": "not_required",
            "preset": resolved_preset,
            "approval_id": None,
            "actions": [],
        }

    def _decision_payload(self, decision: CommandPolicyDecision, *, risk: ToolRisk) -> dict[str, Any]:
        return {
            "action": decision.action,
            "risk": risk,
            "reason": decision.reason,
            "normalized_command": self.redact(decision.normalized_command),
            "argv": [self.redact(item) for item in decision.argv],
            "resolved_argv": [self.redact(item) for item in decision.resolved_argv],
            "matched_prefix": list(decision.matched_prefix),
            "cwd_policy": decision.cwd_policy,
            "matched_rules": [self._redact_rule(item) for item in decision.matched_rules],
            "matched_amendments": [self._redact_rule(item) for item in decision.matched_amendments],
            "executable_resolution": decision.executable_resolution,
            "shell_parse": decision.parse_tree,
            "blocked_syntax": decision.blocked_syntax,
            "network_policy": decision.network_policy,
        }

    def _redact_rule(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: self.redact(value) if isinstance(value, str) else value
            for key, value in item.items()
        }

    @staticmethod
    def _risk_for_decision(decision: CommandPolicyDecision) -> ToolRisk:
        if decision.action == "forbidden":
            return "forbidden"
        executable = PurePosixPath(decision.argv[0]).name.lower() if decision.argv else ""
        args = [str(arg).lower() for arg in decision.argv]
        matched_executable = str(decision.matched_prefix[0]).lower() if decision.matched_prefix else executable
        if matched_executable in {"rg", "sed", "ls", "python", "python3", "node", "find"}:
            return "read_only"
        if matched_executable == "git" and decision.action == "allow":
            return "read_only"
        if executable in {"curl", "wget", "git", "npm", "pnpm", "yarn", "pip", "pip3"}:
            return "network"
        if executable in {"rm", "mv", "cp", "docker"} or any(arg in {"--force", "-rf", "-fr"} for arg in args):
            return "destructive"
        return "unknown"

    @staticmethod
    def _sandbox_profile_for(risk: ToolRisk | str, *, preset: str) -> str:
        if preset == "developer_bypass":
            return "developer_bypass"
        if risk in {"safe", "read_only"}:
            return "analysis_readonly"
        if risk == "mutating":
            return "agent_draft_write"
        return "analysis_readonly"

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.strip("/")

    @staticmethod
    def _has_path_traversal(path: str) -> bool:
        parts = PurePosixPath(path).parts
        return any(part == ".." for part in parts)

    @staticmethod
    def _is_read_only_path(path: str) -> bool:
        return path.startswith(
            (
                ".git/",
                "node_modules/",
                "dist/",
                "build/",
                ".cache/",
                ".pytest_cache/",
                ".mypy_cache/",
                ".ruff_cache/",
                ".next/",
                ".vite/",
            )
        )
