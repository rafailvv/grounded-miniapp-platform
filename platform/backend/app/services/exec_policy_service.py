from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from app.modules.miniapp_agent_loop.agent_command_policy import DEFAULT_COMMAND_POLICY, CommandPolicyDecision
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, ToolRisk


APPROVAL_PRESETS: dict[str, dict[str, Any]] = {
    "strict_manual": {
        "description": "Prompt for every permitted operation; forbidden commands remain blocked.",
        "auto_approve_risks": [],
    },
    "safe_auto": {
        "description": "Auto-approve safe read-only diagnostics; require approval for mutations and network.",
        "auto_approve_risks": ["safe", "read_only"],
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
    "analysis_only": {
        "description": "Read-only diagnostics and workspace inspection.",
        "writes": "none",
        "network": False,
    },
    "agent_draft": {
        "description": "Default agent mode; writes are restricted to the draft workspace.",
        "writes": "draft_workspace",
        "network": False,
    },
    "apply_gate": {
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

    def __init__(self) -> None:
        self.policy = DEFAULT_COMMAND_POLICY

    def snapshot(self) -> dict[str, Any]:
        payload = self.policy.snapshot()
        payload.update(
            {
                "tool_protocol_version": TOOL_PROTOCOL_VERSION,
                "risk_model": ["safe", "read_only", "draft_write", "workspace_write", "network_limited", "dangerous_requires_approval", "forbidden", "unknown"],
                "approval_presets": APPROVAL_PRESETS,
                "sandbox_profiles": SANDBOX_PROFILES,
                "sandbox": {
                    "cwd": "draft workspace or miniapp subdirectory",
                    "network": "blocked unless a future connector policy grants it",
                    "writes": "restricted to draft workspace paths; generated caches/build outputs are ignored by apply",
                    "path_traversal": "parent-directory traversal is denied",
                },
                "write_grants": self.write_grants(),
            }
        )
        return payload

    def evaluate_command(self, command: str, *, preset: str = "safe_auto") -> dict[str, Any]:
        decision = self.policy.decide(command)
        risk = self._risk_for_decision(decision)
        approval = self._approval_for_risk(risk, decision=decision, preset=preset)
        return {
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "command": self.redact(command),
            "decision": self._decision_payload(decision, risk=risk),
            "approval": approval,
            "sandbox_summary": self.sandbox_summary(decision, risk=risk, preset=preset),
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

    def sandbox_summary(self, decision: CommandPolicyDecision | None = None, *, risk: ToolRisk | None = None, preset: str = "safe_auto") -> dict[str, Any]:
        profile = self._sandbox_profile_for(risk or "unknown", preset=preset)
        return {
            "profile": profile,
            "profile_description": SANDBOX_PROFILES[profile]["description"],
            "cwd_policy": decision.cwd_policy if decision else "draft_workspace",
            "argv": list(decision.argv) if decision else [],
            "matched_prefix": list(decision.matched_prefix) if decision else [],
            "network_allowed": bool(SANDBOX_PROFILES[profile]["network"]),
            "path_traversal_blocked": True,
            "shell_metacharacters_blocked": True,
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
        required = risk not in APPROVAL_PRESETS[resolved_preset]["auto_approve_risks"] or decision.action == "prompt"
        return {
            "required": required,
            "status": "pending" if required else "not_required",
            "preset": resolved_preset,
            "approval_id": f"approval_{uuid4().hex}" if required else None,
            "actions": ["approve_once", "approve_prefix", "reject"] if required else [],
        }

    def _decision_payload(self, decision: CommandPolicyDecision, *, risk: ToolRisk) -> dict[str, Any]:
        return {
            "action": decision.action,
            "risk": risk,
            "reason": decision.reason,
            "normalized_command": self.redact(decision.normalized_command),
            "argv": [self.redact(item) for item in decision.argv],
            "matched_prefix": list(decision.matched_prefix),
            "cwd_policy": decision.cwd_policy,
        }

    @staticmethod
    def _risk_for_decision(decision: CommandPolicyDecision) -> ToolRisk:
        if decision.action == "forbidden":
            return "forbidden"
        executable = PurePosixPath(decision.argv[0]).name.lower() if decision.argv else ""
        args = [str(arg).lower() for arg in decision.argv]
        if executable in {"rg", "sed", "ls", "python", "python3", "node", "find"}:
            return "read_only"
        if executable == "git" and decision.action == "allow" and decision.matched_prefix and decision.matched_prefix[0] == "git":
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
            return "analysis_only"
        if risk == "mutating":
            return "agent_draft"
        return "analysis_only"

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
