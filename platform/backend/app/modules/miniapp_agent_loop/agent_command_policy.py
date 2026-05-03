from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
from typing import Any, Literal


CommandPolicyAction = Literal["allow", "prompt", "forbidden"]


@dataclass(frozen=True)
class CommandPolicyExample:
    command: str
    action: CommandPolicyAction


@dataclass(frozen=True)
class CommandPolicyRule:
    prefixes: tuple[tuple[str, ...], ...]
    action: CommandPolicyAction
    reason: str
    examples: tuple[CommandPolicyExample, ...] = field(default_factory=tuple)

    def matches(self, args: list[str]) -> bool:
        lowered = [item.lower() for item in args]
        for prefix in self.prefixes:
            if len(lowered) >= len(prefix) and tuple(lowered[: len(prefix)]) == prefix:
                return True
        return False


@dataclass(frozen=True)
class CommandPolicyDecision:
    action: CommandPolicyAction
    reason: str
    command: str
    normalized_command: str
    argv: tuple[str, ...] = ()
    matched_prefix: tuple[str, ...] = ()
    cwd_policy: str = "draft_workspace"

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class AgentCommandPolicy:
    """Prefix-rule shell policy for agent diagnostic commands."""

    _BLOCKED_META_CHARS = re.compile(r"[`$<>|;]")

    def __init__(self, rules: list[CommandPolicyRule] | None = None) -> None:
        self.rules = list(rules or self.default_rules())

    @classmethod
    def from_rule_payload(cls, payload: dict[str, Any]) -> "AgentCommandPolicy":
        rules: list[CommandPolicyRule] = []
        for raw_rule in payload.get("rules", []) if isinstance(payload, dict) else []:
            if not isinstance(raw_rule, dict):
                continue
            prefixes: list[tuple[str, ...]] = []
            for raw_prefix in raw_rule.get("prefixes", []):
                if isinstance(raw_prefix, str):
                    try:
                        prefixes.append(tuple(item.lower() for item in shlex.split(raw_prefix)))
                    except ValueError:
                        continue
                elif isinstance(raw_prefix, list):
                    prefixes.append(tuple(str(item).lower() for item in raw_prefix))
            action = str(raw_rule.get("action") or "allow")
            if action not in {"allow", "prompt", "forbidden"}:
                continue
            examples: list[CommandPolicyExample] = []
            for raw_example in raw_rule.get("examples", []):
                if not isinstance(raw_example, dict):
                    continue
                expected = str(raw_example.get("action") or action)
                if expected in {"allow", "prompt", "forbidden"}:
                    examples.append(CommandPolicyExample(str(raw_example.get("command") or ""), expected))  # type: ignore[arg-type]
            if prefixes:
                rules.append(
                    CommandPolicyRule(
                        prefixes=tuple(prefixes),
                        action=action,  # type: ignore[arg-type]
                        reason=str(raw_rule.get("reason") or "Rule-file command policy decision."),
                        examples=tuple(examples),
                    )
                )
        return cls(rules or None)

    @classmethod
    def from_rule_file(cls, path: Path) -> "AgentCommandPolicy":
        import json

        return cls.from_rule_payload(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def default_rules() -> list[CommandPolicyRule]:
        return [
            CommandPolicyRule(
                prefixes=(("python", "-m", "unittest"), ("python3", "-m", "unittest")),
                action="allow",
                reason="Python unit diagnostics are allowed inside the draft workspace.",
                examples=(CommandPolicyExample("python -m unittest discover", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("python", "-m", "py_compile"), ("python3", "-m", "py_compile")),
                action="allow",
                reason="Python compile diagnostics are allowed inside the draft workspace.",
                examples=(CommandPolicyExample("python -m py_compile miniapp/app/main.py", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("node", "--test"), ("node", "--check")),
                action="allow",
                reason="Node diagnostics are allowed inside the draft workspace.",
                examples=(CommandPolicyExample("node --check miniapp/app/static/client/app.js", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("rg",), ("sed",), ("ls",)),
                action="allow",
                reason="Read-only workspace inspection commands are allowed.",
                examples=(CommandPolicyExample("rg api miniapp/app", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(
                    ("pip", "install"),
                    ("pip3", "install"),
                    ("python", "-m", "pip", "install"),
                    ("python3", "-m", "pip", "install"),
                    ("npm", "install"),
                    ("npm", "i"),
                    ("pnpm", "install"),
                    ("pnpm", "add"),
                    ("yarn", "install"),
                    ("yarn", "add"),
                ),
                action="forbidden",
                reason="Package installation is not an agent diagnostic command.",
                examples=(CommandPolicyExample("npm install", "forbidden"),),
            ),
            CommandPolicyRule(
                prefixes=(
                    ("rm",),
                    ("git", "reset"),
                    ("git", "clean"),
                    ("git", "checkout", "--"),
                    ("curl",),
                    ("wget",),
                    ("docker", "build"),
                    ("docker", "run"),
                    ("docker", "pull"),
                    ("apt",),
                    ("apt-get",),
                    ("brew",),
                ),
                action="forbidden",
                reason="The command can mutate or fetch outside the draft diagnostic boundary.",
                examples=(CommandPolicyExample("rm -rf miniapp", "forbidden"),),
            ),
        ]

    def decide(self, command: str) -> CommandPolicyDecision:
        stripped = str(command or "").strip()
        if not stripped:
            return CommandPolicyDecision("forbidden", "Empty command.", command, "")
        if self._BLOCKED_META_CHARS.search(stripped):
            return CommandPolicyDecision(
                "forbidden",
                "Shell metacharacters are blocked except for a single safe 'cd miniapp && ...' prefix.",
                command,
                stripped,
            )
        normalized = re.sub(r"\s+", " ", stripped)
        normalized_lower = normalized.lower()
        cwd_policy = "draft_workspace"
        if "&&" in normalized_lower:
            if not normalized_lower.startswith("cd miniapp && "):
                return CommandPolicyDecision("forbidden", "Command chaining is blocked except for 'cd miniapp && ...'.", command, normalized)
            normalized = normalized.split("&&", 1)[1].strip()
            cwd_policy = "miniapp"
            if "&&" in normalized.lower():
                return CommandPolicyDecision("forbidden", "Only one safe 'cd miniapp && ...' prefix is allowed.", command, normalized)
        if "&" in normalized:
            return CommandPolicyDecision("forbidden", "Background shell execution is blocked.", command, normalized)
        try:
            args = shlex.split(normalized)
        except ValueError as exc:
            return CommandPolicyDecision("forbidden", f"Command could not be parsed safely: {exc}.", command, normalized)
        if not args:
            return CommandPolicyDecision("forbidden", "Empty command.", command, normalized)
        if any(arg == ".." or arg.startswith("../") or "/../" in arg for arg in args):
            return CommandPolicyDecision("forbidden", "Parent-directory paths are blocked.", command, normalized, tuple(args))
        normalized_args = [Path(args[0]).name.lower(), *[str(arg).lower() for arg in args[1:]]]
        if normalized_args[0] in {"python3.10", "python3.11", "python3.12"}:
            normalized_args[0] = "python3"
        if normalized_args[0] == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in normalized_args[1:]):
            return CommandPolicyDecision("forbidden", "In-place sed edits are blocked.", command, normalized, tuple(args), ("sed",), cwd_policy)
        matched_rules: list[CommandPolicyRule] = []
        for rule in self.rules:
            if rule.matches(normalized_args):
                matched_rules.append(rule)
        if matched_rules:
            severity = {"allow": 0, "prompt": 1, "forbidden": 2}
            selected = max(matched_rules, key=lambda rule: severity[rule.action])
            matched = next(
                (prefix for prefix in selected.prefixes if tuple(normalized_args[: len(prefix)]) == prefix),
                (),
            )
            return CommandPolicyDecision(selected.action, selected.reason, command, normalized, tuple(args), matched, cwd_policy)
        return CommandPolicyDecision(
            "forbidden",
            "Only diagnostic commands are allowed: python -m unittest, python -m py_compile, node --test, node --check, rg, sed, and ls.",
            command,
            normalized,
            tuple(args),
            cwd_policy=cwd_policy,
        )

    def validation_examples(self) -> list[dict[str, str]]:
        examples: list[dict[str, str]] = []
        for rule in self.rules:
            for example in rule.examples:
                decision = self.decide(example.command)
                examples.append(
                    {
                        "command": example.command,
                        "expected": example.action,
                        "actual": decision.action,
                        "status": "passed" if decision.action == example.action else "failed",
                    }
                )
        return examples

    def snapshot(self) -> dict[str, object]:
        return {
            "policy": "prefix_rule",
            "rules": [
                {
                    "prefixes": [" ".join(prefix) for prefix in rule.prefixes],
                    "action": rule.action,
                    "reason": rule.reason,
                }
                for rule in self.rules
            ],
            "examples": self.validation_examples(),
        }


DEFAULT_COMMAND_POLICY = AgentCommandPolicy()


def decide_workspace_command(command: str) -> CommandPolicyDecision:
    return DEFAULT_COMMAND_POLICY.decide(command)


def command_policy_snapshot() -> dict[str, object]:
    return DEFAULT_COMMAND_POLICY.snapshot()
