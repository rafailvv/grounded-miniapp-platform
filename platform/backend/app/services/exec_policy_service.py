from __future__ import annotations

import hashlib
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
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, ToolRisk, tool_registry_contract


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

COMMAND_CLASS_MODEL: dict[str, dict[str, Any]] = {
    "read_only": {
        "description": "Workspace inspection that should not change files, network, or process state.",
        "default_approval": "auto",
        "examples": ["rg api miniapp/app", "cat miniapp/app/main.py", "git status --short"],
    },
    "build_test": {
        "description": "Local compile/test diagnostics that may write caches but not product source.",
        "default_approval": "auto",
        "examples": ["python3 -m py_compile miniapp/app/main.py", "node --check miniapp/app/static/client/app.js"],
    },
    "mutation": {
        "description": "Workspace or draft writes that require policy or human approval depending on scope.",
        "default_approval": "prompt",
        "examples": ["git diff --output out.patch", "sed -i s/a/b/ miniapp/app/main.py"],
    },
    "network": {
        "description": "Commands that can fetch, publish, install, or contact external hosts.",
        "default_approval": "blocked",
        "examples": ["curl https://example.com", "npm install", "git pull"],
    },
    "destructive": {
        "description": "Commands that can delete, reset, clean, overwrite broadly, or mutate host tooling.",
        "default_approval": "blocked",
        "examples": ["rm -rf miniapp", "git reset --hard", "docker run image"],
    },
    "unknown": {
        "description": "Anything outside the allowlisted command grammar and command prefixes.",
        "default_approval": "blocked",
        "examples": ["./tool", "zsh -lc 'ls'"],
    },
}

COMMAND_AUDIT_SCHEMA = "grounded.command_audit.v1"


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
                "command_class_model": COMMAND_CLASS_MODEL,
                "safety_model": ["read_only", "build_test", "workspace_write", "destructive", "network", "unknown"],
                "network_policy": {
                    "mode": "blocked_by_default",
                    "allowed": False,
                    "hard_blocked": ["direct network tools", "package installs/updates", "git network subcommands", "proxy/network config flags"],
                },
                "approval_presets": APPROVAL_PRESETS,
                "approval_gates": self.approval_gates(),
                "per_tool_policy": self.per_tool_policy(),
                "generated_command_default": {
                    "action": "forbidden",
                    "reason": "Generated commands are denied unless the shell parser, trusted executable resolver, network policy, and allowlist all accept them.",
                    "blocked_code": "no_matching_allow_rule",
                },
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

    def evaluate_command(
        self,
        command: str,
        *,
        preset: str = "safe_auto",
        root: Path | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        decision = self.policy.decide(command)
        risk = self._risk_for_decision(decision)
        command_class = self.command_class(decision)
        command_fingerprint = self.command_fingerprint(command, workspace_id=workspace_id)
        approval = self._approval_for_risk(
            risk,
            decision=decision,
            preset=preset,
            workspace_id=workspace_id,
            command_fingerprint=command_fingerprint,
        )
        safety = self._safety_payload(decision)
        approval_gate = self._approval_gate_payload(
            decision=decision,
            risk=risk,
            command_class=command_class,
            approval=approval,
            preset=preset,
        )
        block_explanation = self._block_explanation(decision, command_class=command_class)
        return {
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "command": self.redact(command),
            "command_fingerprint": command_fingerprint,
            "command_class": command_class,
            "argv": [self.redact(item) for item in decision.argv],
            "resolved_argv": [self.redact(item) for item in decision.resolved_argv],
            "resolved_executable": decision.executable_resolution,
            "matched_rules": [self._redact_rule(item) for item in decision.matched_rules],
            "matched_amendments": [self._redact_rule(item) for item in decision.matched_amendments],
            "selected_decision": decision.action,
            "shell_parse": decision.parse_tree,
            "blocked_syntax": decision.blocked_syntax,
            "network_policy": decision.network_policy,
            "safety": safety,
            "decision": self._decision_payload(decision, risk=risk),
            "approval": approval,
            "approval_gate": approval_gate,
            "block_explanation": block_explanation,
            "per_command_policy": {
                "command_fingerprint": command_fingerprint,
                "command_class": command_class,
                "preset": preset if preset in APPROVAL_PRESETS else "safe_auto",
                "matched_rule_ids": [str(item.get("rule_id") or "") for item in [*decision.matched_rules, *decision.matched_amendments] if isinstance(item, dict)],
                "generated_default": "deny_unmatched",
            },
            "sandbox_summary": self.sandbox_summary(decision, risk=risk, preset=preset, root=root),
            "policy_file": {
                "source": self.policy_source,
                "status": self.policy_status,
                "errors": list(self.policy_errors),
            },
        }

    def append_audit_record(
        self,
        store: Any,
        *,
        workspace_id: str,
        command: str,
        evaluation: dict[str, Any],
        source: str,
        run_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        process_id: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        from datetime import datetime, timezone
        from uuid import uuid4

        decision = evaluation.get("decision") if isinstance(evaluation.get("decision"), dict) else {}
        approval = evaluation.get("approval") if isinstance(evaluation.get("approval"), dict) else {}
        block_explanation = evaluation.get("block_explanation") if isinstance(evaluation.get("block_explanation"), dict) else {}
        item = {
            "audit_id": f"cmd_audit_{uuid4().hex}",
            "schema": "grounded.command_audit_record.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "process_id": process_id,
            "source": source,
            "outcome": outcome or ("blocked" if decision.get("action") == "forbidden" else "approval_required" if approval.get("required") else "allowed"),
            "command": self.redact(command),
            "command_fingerprint": evaluation.get("command_fingerprint"),
            "command_class": evaluation.get("command_class"),
            "risk": decision.get("risk"),
            "action": decision.get("action"),
            "approval": approval,
            "approval_gate": evaluation.get("approval_gate") if isinstance(evaluation.get("approval_gate"), dict) else {},
            "blocked_code": block_explanation.get("code"),
            "blocked_reason": block_explanation.get("reason"),
            "matched_rule_ids": (evaluation.get("per_command_policy") or {}).get("matched_rule_ids") if isinstance(evaluation.get("per_command_policy"), dict) else [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        key = f"command_audit:{workspace_id}"
        payload = store.get("reports", key) or {"schema": COMMAND_AUDIT_SCHEMA, "workspace_id": workspace_id, "items": []}
        items = [entry for entry in payload.get("items") or [] if isinstance(entry, dict)]
        items.append(item)
        payload["items"] = items[-500:]
        payload["updated_at"] = item["created_at"]
        store.upsert("reports", key, payload)
        return item

    def command_audit(self, store: Any, *, workspace_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for key, payload in store.items("reports"):
            if not key.startswith("command_audit:") or not isinstance(payload, dict):
                continue
            if workspace_id and payload.get("workspace_id") != workspace_id:
                continue
            for item in payload.get("items") or []:
                if isinstance(item, dict):
                    items.append(dict(item))
        return {
            "schema": COMMAND_AUDIT_SCHEMA,
            "workspace_id": workspace_id,
            "items": sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)[: max(1, min(int(limit or 100), 500))],
        }

    @staticmethod
    def approval_gates() -> dict[str, Any]:
        return {
            "auto": {"status": "not_required", "classes": ["read_only", "build_test"], "description": "Allowed diagnostics can run without manual approval."},
            "prompt": {"status": "pending", "classes": ["mutation"], "description": "Workspace mutations require an approval unless a scoped grant exists."},
            "block": {"status": "blocked", "classes": ["network", "destructive", "unknown"], "description": "Dangerous generated commands are denied by default."},
        }

    @staticmethod
    def per_tool_policy() -> dict[str, Any]:
        registry = tool_registry_contract()
        raw_tools = registry.get("tools")
        tools = {str(item.get("canonical")): item for item in raw_tools if isinstance(item, dict)} if isinstance(raw_tools, list) else {}
        shell = tools.get("shell.exec") if isinstance(tools.get("shell.exec"), dict) else {}
        return {
            "shell.exec": {
                "approval_class": shell.get("approval_class") or "policy",
                "command_policy": "shell_subset_prefix_rule",
                "audit": COMMAND_AUDIT_SCHEMA,
                "dangerous_generated_default": "forbidden",
            }
        }

    @staticmethod
    def command_class(decision: CommandPolicyDecision) -> str:
        executable = PurePosixPath(decision.argv[0]).name.lower() if decision.argv else ""
        args = [str(arg).lower() for arg in decision.argv]
        blocked_code = str((decision.blocked_syntax or {}).get("code") or "")
        if decision.safety_class == "network":
            return "network"
        if decision.safety_class == "destructive":
            return "destructive"
        if decision.safety_class == "workspace_write":
            return "mutation"
        if executable in {"python", "python3", "node"} and (
            args[1:3] in [["-m", "unittest"], ["-m", "py_compile"]]
            or (len(args) > 1 and args[1] in {"--test", "--check"})
        ):
            return "build_test"
        if blocked_code in {"package_network_operation", "direct_network_tool", "git_network_operation", "node_network_imports", "network_proxy_config"}:
            return "network"
        if blocked_code in {"mutating_filesystem", "sed_in_place"}:
            return "mutation"
        if blocked_code in {"no_matching_allow_rule", "forbidden_executable", "relative_executable", "untrusted_basename", "untrusted_absolute"}:
            return "unknown"
        if decision.safety_class == "read_only":
            return "read_only"
        return "unknown"

    def command_fingerprint(self, command: str, *, workspace_id: str | None = None) -> str:
        normalized = self.policy.decide(command).normalized_command
        scope = str(workspace_id or "global")
        return hashlib.sha256(f"{scope}\n{normalized}".encode("utf-8", errors="replace")).hexdigest()

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

    def _approval_for_risk(
        self,
        risk: ToolRisk,
        *,
        decision: CommandPolicyDecision,
        preset: str,
        workspace_id: str | None,
        command_fingerprint: str,
    ) -> dict[str, Any]:
        resolved_preset = preset if preset in APPROVAL_PRESETS else "safe_auto"
        if decision.action == "forbidden" or risk == "forbidden":
            return {
                "required": False,
                "status": "blocked",
                "preset": resolved_preset,
                "approval_id": None,
                "scope": "workspace" if workspace_id else "global",
                "workspace_id": workspace_id,
                "command_fingerprint": command_fingerprint,
            }
        auto_approve = set(APPROVAL_PRESETS[resolved_preset]["auto_approve_risks"])
        required = decision.action == "prompt" or (risk not in {"safe", "read_only"} and risk not in auto_approve)
        approval_id = f"appr_ws_{command_fingerprint[:20]}" if required else None
        return {
            "required": required,
            "status": "pending" if required else "not_required",
            "preset": resolved_preset,
            "approval_id": approval_id,
            "scope": "workspace" if workspace_id else "global",
            "workspace_id": workspace_id,
            "command_fingerprint": command_fingerprint,
            "actions": ["workspace_scoped_command"] if required else [],
        }

    def _decision_payload(self, decision: CommandPolicyDecision, *, risk: ToolRisk) -> dict[str, Any]:
        return {
            "action": decision.action,
            "risk": risk,
            "command_class": self.command_class(decision),
            "safety_class": decision.safety_class,
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

    @staticmethod
    def _safety_payload(decision: CommandPolicyDecision) -> dict[str, Any]:
        safety_class = decision.safety_class
        command_class = ExecPolicyService.command_class(decision)
        return {
            "class": safety_class,
            "command_class": command_class,
            "read_only": safety_class == "read_only",
            "build_test": command_class == "build_test",
            "writes_workspace": safety_class == "workspace_write",
            "destructive": safety_class == "destructive",
            "network": safety_class == "network",
            "requires_approval": decision.action == "prompt",
            "denied": decision.action == "forbidden",
            "reason": decision.reason,
        }

    @staticmethod
    def _approval_gate_payload(
        *,
        decision: CommandPolicyDecision,
        risk: ToolRisk,
        command_class: str,
        approval: dict[str, Any],
        preset: str,
    ) -> dict[str, Any]:
        if decision.action == "forbidden" or approval.get("status") == "blocked":
            gate = "block"
        elif approval.get("required"):
            gate = "prompt"
        else:
            gate = "auto"
        return {
            "gate": gate,
            "preset": preset if preset in APPROVAL_PRESETS else "safe_auto",
            "risk": risk,
            "command_class": command_class,
            "required": bool(approval.get("required")),
            "status": approval.get("status") or ("blocked" if gate == "block" else "not_required"),
            "reason": decision.reason,
        }

    @staticmethod
    def _block_explanation(decision: CommandPolicyDecision, *, command_class: str) -> dict[str, Any]:
        if decision.action != "forbidden":
            return {"blocked": False}
        blocked = decision.blocked_syntax if isinstance(decision.blocked_syntax, dict) else {}
        code = str(blocked.get("code") or "policy_forbidden")
        reason = str(blocked.get("reason") or decision.reason or "Command blocked by policy.")
        remediation = {
            "network": "Use already vendored dependencies, existing project files, or a platform-approved integration instead of direct network/package commands.",
            "destructive": "Use draft patch operations or targeted file edits; never reset, clean, delete, or overwrite broad workspace paths.",
            "mutation": "Use draft-scoped file tools or request an explicit workspace approval for the exact command.",
            "unknown": "Use allowlisted diagnostics: rg, sed -n, ls, cat, find, read-only git, python compile/unittest, or node check/test.",
            "read_only": "Use a simpler read-only command shape without shell metacharacters or path escapes.",
            "build_test": "Run the diagnostic directly without shell wrappers, env assignments, network flags, or path escapes.",
        }.get(command_class, "Use an allowlisted diagnostic command or request a narrower policy rule.")
        return {
            "blocked": True,
            "code": code,
            "reason": reason,
            "command_class": command_class,
            "remediation": remediation,
        }

    def _redact_rule(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: self.redact(value) if isinstance(value, str) else value
            for key, value in item.items()
        }

    @staticmethod
    def _risk_for_decision(decision: CommandPolicyDecision) -> ToolRisk:
        if decision.action == "forbidden":
            if decision.safety_class == "network":
                return "network"
            if decision.safety_class == "destructive":
                return "destructive"
            if decision.safety_class == "workspace_write":
                return "mutating"
            return "forbidden"
        executable = PurePosixPath(decision.argv[0]).name.lower() if decision.argv else ""
        args = [str(arg).lower() for arg in decision.argv]
        matched_executable = str(decision.matched_prefix[0]).lower() if decision.matched_prefix else executable
        if decision.safety_class == "workspace_write":
            return "mutating"
        if decision.safety_class == "destructive":
            return "destructive"
        if decision.safety_class == "network":
            return "network"
        if matched_executable in {"rg", "sed", "ls", "cat", "python", "python3", "node", "find"}:
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
