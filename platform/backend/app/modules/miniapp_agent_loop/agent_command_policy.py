from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import shutil
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
    rule_id: str = ""
    source: str = "builtin"
    examples: tuple[CommandPolicyExample, ...] = field(default_factory=tuple)
    not_match_examples: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, args: list[str]) -> bool:
        lowered = [item.lower() for item in args]
        for prefix in self.prefixes:
            if len(lowered) >= len(prefix) and tuple(lowered[: len(prefix)]) == prefix:
                return True
        return False

    def matched_prefix(self, args: list[str]) -> tuple[str, ...]:
        lowered = [item.lower() for item in args]
        for prefix in self.prefixes:
            if len(lowered) >= len(prefix) and tuple(lowered[: len(prefix)]) == prefix:
                return prefix
        return ()

    def match_payload(self, args: list[str]) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "source": self.source,
            "pattern": list(self.matched_prefix(args)),
            "decision": self.action,
            "justification": self.reason,
        }


@dataclass(frozen=True)
class CommandPolicyDecision:
    action: CommandPolicyAction
    reason: str
    command: str
    normalized_command: str
    argv: tuple[str, ...] = ()
    matched_prefix: tuple[str, ...] = ()
    cwd_policy: str = "draft_workspace"
    matched_rules: tuple[dict[str, Any], ...] = ()
    executable_resolution: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class AgentCommandPolicy:
    """Prefix-rule shell policy for agent diagnostic commands."""

    _SHELL_WRAPPERS = {"bash", "sh", "zsh"}
    _BLOCKED_META_CHARS = re.compile(r"[`$<>|;]")
    _BLOCKED_EXPANSION = re.compile(r"\$\(|\${|%[A-Za-z_][A-Za-z0-9_]*%")
    _BLOCKED_HEREDOC = re.compile(r"<<-?")
    _BLOCKED_LINE_CONTINUATION = re.compile(r"\\\s*(?:\r?\n)")
    _BLOCKED_ENV_ASSIGNMENT = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=")
    _BLOCKED_BRACE_EXPANSION = re.compile(r"(?:^|[^$])\{[^{}\n]*,[^{}\n]*\}")
    _UNSAFE_GIT_GLOBAL_OPTIONS = {
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--paginate",
    }
    _SAFE_GIT_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "rev-parse"}
    _UNSAFE_GIT_FLAGS = {"--output", "--ext-diff", "--textconv", "--external-diff", "--paginate", "-p"}
    _UNSAFE_RG_OPTIONS = {"--pre", "--pre-glob", "--hostname-bin", "-z", "--search-zip"}
    _UNSAFE_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"}

    _DEFAULT_HOST_EXECUTABLES = {
        "python",
        "python3",
        "node",
        "npm",
        "pnpm",
        "yarn",
        "rg",
        "sed",
        "ls",
        "find",
        "git",
        "bash",
        "sh",
        "zsh",
    }

    def __init__(
        self,
        rules: list[CommandPolicyRule] | None = None,
        *,
        host_executables_by_name: dict[str, list[str]] | None = None,
        source: str = "builtin",
    ) -> None:
        self.rules = list(rules or self.default_rules())
        self.source = source
        self.host_executables_by_name = host_executables_by_name or self._discover_host_executables()

    @classmethod
    def from_rule_payload(cls, payload: dict[str, Any]) -> "AgentCommandPolicy":
        rules: list[CommandPolicyRule] = []
        source = str(payload.get("source") or "json") if isinstance(payload, dict) else "json"
        for index, raw_rule in enumerate(payload.get("rules", []) if isinstance(payload, dict) else []):
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
                if isinstance(raw_example, dict):
                    expected = str(raw_example.get("action") or action)
                    if expected in {"allow", "prompt", "forbidden"}:
                        examples.append(CommandPolicyExample(str(raw_example.get("command") or ""), expected))  # type: ignore[arg-type]
            for raw_example in raw_rule.get("match", []):
                command = " ".join(str(item) for item in raw_example) if isinstance(raw_example, list) else str(raw_example or "")
                if command.strip():
                    examples.append(CommandPolicyExample(command, action))  # type: ignore[arg-type]
            not_match_examples: list[str] = []
            for raw_example in raw_rule.get("not_match", []):
                command = " ".join(str(item) for item in raw_example) if isinstance(raw_example, list) else str(raw_example or "")
                if command.strip():
                    not_match_examples.append(command)
            if prefixes:
                rules.append(
                    CommandPolicyRule(
                        prefixes=tuple(prefixes),
                        action=action,  # type: ignore[arg-type]
                        reason=str(raw_rule.get("reason") or "Rule-file command policy decision."),
                        rule_id=str(raw_rule.get("rule_id") or f"json_rule_{index + 1}"),
                        source=source,
                        examples=tuple(examples),
                        not_match_examples=tuple(not_match_examples),
                    )
                )
        return cls(rules or None, source=source)

    @classmethod
    def from_rule_file(cls, path: Path) -> "AgentCommandPolicy":
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = {**payload, "source": str(path)}
        return cls.from_rule_payload(payload)

    @classmethod
    def from_dsl_file(cls, path: Path) -> "AgentCommandPolicy":
        return cls.from_dsl_text(path.read_text(encoding="utf-8"), source=str(path))

    @classmethod
    def from_dsl_text(cls, text: str, *, source: str = "dsl") -> "AgentCommandPolicy":
        module = ast.parse(text, filename=source, mode="exec")
        rules: list[CommandPolicyRule] = []
        for index, node in enumerate(module.body):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            else:
                raise ValueError(f"{source}:{getattr(node, 'lineno', '?')}: only prefix_rule(...) calls are allowed")
            if not isinstance(call.func, ast.Name) or call.func.id != "prefix_rule":
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: only prefix_rule(...) calls are allowed")
            if call.args:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: positional arguments are not allowed")
            raw = cls._dsl_keywords(call, source=source)
            unknown = set(raw) - {"pattern", "decision", "justification", "match", "not_match"}
            if unknown:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: unknown fields: {', '.join(sorted(unknown))}")
            pattern = cls._dsl_string_list(raw.get("pattern"), field="pattern", source=source, lineno=getattr(call, "lineno", 0))
            if not pattern:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: pattern cannot be empty")
            decision = str(raw.get("decision") or "allow")
            if decision not in {"allow", "prompt", "forbidden"}:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: invalid decision {decision!r}")
            justification = str(raw.get("justification") or raw.get("reason") or "Rule-file command policy decision.")
            if not justification.strip():
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: justification cannot be empty")
            examples = [
                CommandPolicyExample(command, decision)  # type: ignore[arg-type]
                for command in cls._dsl_command_examples(raw.get("match"), source=source, lineno=getattr(call, "lineno", 0))
            ]
            not_match = cls._dsl_command_examples(raw.get("not_match"), source=source, lineno=getattr(call, "lineno", 0))
            rules.append(
                CommandPolicyRule(
                    prefixes=(tuple(item.lower() for item in pattern),),
                    action=decision,  # type: ignore[arg-type]
                    reason=justification,
                    rule_id=f"dsl_rule_{index + 1}",
                    source=source,
                    examples=tuple(examples),
                    not_match_examples=tuple(not_match),
                )
            )
        return cls(rules or None, source=source)

    @staticmethod
    def _dsl_keywords(call: ast.Call, *, source: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: **kwargs are not allowed")
            values[keyword.arg] = ast.literal_eval(keyword.value)
        return values

    @staticmethod
    def _dsl_string_list(value: Any, *, field: str, source: str, lineno: int) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"{source}:{lineno}: {field} must be a non-empty list of strings")
        return [item.strip() for item in value]

    @classmethod
    def _dsl_command_examples(cls, value: Any, *, source: str, lineno: int) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{source}:{lineno}: match/not_match must be a list")
        commands: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                commands.append(item.strip())
            elif isinstance(item, list) and all(isinstance(part, str) and part.strip() for part in item):
                commands.append(" ".join(part.strip() for part in item))
            else:
                raise ValueError(f"{source}:{lineno}: examples must be command strings or string arrays")
        return commands

    @staticmethod
    def default_rules() -> list[CommandPolicyRule]:
        return [
            CommandPolicyRule(
                prefixes=(("python", "-m", "unittest"), ("python3", "-m", "unittest")),
                action="allow",
                reason="Python unit diagnostics are allowed inside the draft workspace.",
                rule_id="builtin_python_unittest",
                examples=(CommandPolicyExample("python -m unittest discover", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("python", "-m", "py_compile"), ("python3", "-m", "py_compile")),
                action="allow",
                reason="Python compile diagnostics are allowed inside the draft workspace.",
                rule_id="builtin_python_compile",
                examples=(CommandPolicyExample("python -m py_compile miniapp/app/main.py", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("node", "--test"), ("node", "--check")),
                action="allow",
                reason="Node diagnostics are allowed inside the draft workspace.",
                rule_id="builtin_node_diagnostics",
                examples=(CommandPolicyExample("node --check miniapp/app/static/client/app.js", "allow"),),
            ),
            CommandPolicyRule(
                prefixes=(("rg",), ("sed",), ("ls",)),
                action="allow",
                reason="Read-only workspace inspection commands are allowed.",
                rule_id="builtin_read_only_inspection",
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
                rule_id="builtin_package_install_block",
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
                rule_id="builtin_dangerous_block",
                examples=(CommandPolicyExample("rm -rf miniapp", "forbidden"),),
            ),
        ]

    @classmethod
    def _discover_host_executables(cls) -> dict[str, list[str]]:
        resolved: dict[str, list[str]] = {}
        for name in sorted(cls._DEFAULT_HOST_EXECUTABLES):
            paths = []
            first = shutil.which(name)
            if first:
                paths.append(str(Path(first).resolve()))
            resolved[name] = paths
        return resolved

    def decide(self, command: str) -> CommandPolicyDecision:
        stripped = str(command or "").strip()
        if not stripped:
            return CommandPolicyDecision("forbidden", "Empty command.", command, "")
        if self._BLOCKED_LINE_CONTINUATION.search(stripped):
            return CommandPolicyDecision("forbidden", "Line-continuation shell syntax is blocked.", command, stripped)
        if self._BLOCKED_HEREDOC.search(stripped):
            return CommandPolicyDecision("forbidden", "Here-doc and here-string shell syntax is blocked.", command, stripped)
        if self._BLOCKED_ENV_ASSIGNMENT.search(stripped):
            return CommandPolicyDecision("forbidden", "Inline environment assignment is blocked for agent diagnostics.", command, stripped)
        if self._BLOCKED_BRACE_EXPANSION.search(stripped):
            return CommandPolicyDecision("forbidden", "Brace expansion is blocked for agent diagnostics.", command, stripped)
        if self._BLOCKED_META_CHARS.search(stripped):
            return CommandPolicyDecision(
                "forbidden",
                "Shell redirection, pipes, variable expansion, command substitution, and command separators are blocked.",
                command,
                stripped,
            )
        if self._BLOCKED_EXPANSION.search(stripped):
            return CommandPolicyDecision("forbidden", "Shell expansion syntax is blocked.", command, stripped)
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
        wrapper_decision = self._shell_wrapper_decision(command, normalized, args, cwd_policy=cwd_policy)
        if wrapper_decision is not None:
            return wrapper_decision
        if any(arg == ".." or arg.startswith("../") or "/../" in arg for arg in args):
            return CommandPolicyDecision("forbidden", "Parent-directory paths are blocked.", command, normalized, tuple(args))
        executable_resolution = self._resolve_executable(args[0])
        if executable_resolution.get("status") == "untrusted_absolute":
            return CommandPolicyDecision(
                "forbidden",
                "Absolute executable path is not in the trusted host executable map.",
                command,
                normalized,
                tuple(args),
                executable_resolution=executable_resolution,
            )
        if any(str(arg).startswith(("/", "~")) for arg in args[1:]) or str(args[0]).startswith("~"):
            return CommandPolicyDecision("forbidden", "Absolute and home-relative paths are blocked.", command, normalized, tuple(args), executable_resolution=executable_resolution)
        if any(re.search(r"[*?\\[]", str(arg)) for arg in args[1:]):
            return CommandPolicyDecision("forbidden", "Shell glob patterns are blocked; use explicit paths from list_files/search results.", command, normalized, tuple(args))
        if any(arg == ".git" or arg.startswith(".git/") or "/.git/" in arg for arg in args[1:]):
            return CommandPolicyDecision("forbidden", "Git internals are blocked.", command, normalized, tuple(args))
        executable_name = str(executable_resolution.get("name") or Path(args[0]).name).lower()
        normalized_args = [executable_name, *[str(arg).lower() for arg in args[1:]]]
        if normalized_args[0] in {"python3.10", "python3.11", "python3.12"}:
            normalized_args[0] = "python3"
        structured_decision = self._structured_command_decision(command, normalized, args, normalized_args, cwd_policy=cwd_policy)
        if structured_decision is not None:
            return structured_decision
        if normalized_args[0] == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in normalized_args[1:]):
            return CommandPolicyDecision("forbidden", "In-place sed edits are blocked.", command, normalized, tuple(args), ("sed",), cwd_policy)
        if normalized_args[0] in {"rm", "mv", "cp", "chmod", "chown", "curl", "wget"}:
            return CommandPolicyDecision("forbidden", "Mutating filesystem and direct network commands are blocked.", command, normalized, tuple(args), (normalized_args[0],), cwd_policy)
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
            return CommandPolicyDecision(
                selected.action,
                selected.reason,
                command,
                normalized,
                tuple(args),
                matched,
                cwd_policy,
                tuple(rule.match_payload(normalized_args) for rule in matched_rules),
                executable_resolution,
            )
        return CommandPolicyDecision(
            "forbidden",
            "Only diagnostic commands are allowed: python -m unittest, python -m py_compile, node --test, node --check, rg, sed, and ls.",
            command,
            normalized,
            tuple(args),
            cwd_policy=cwd_policy,
            executable_resolution=executable_resolution,
        )

    def _resolve_executable(self, raw: str) -> dict[str, Any]:
        raw_text = str(raw or "")
        path = Path(raw_text)
        basename = path.name.lower()
        if basename in {"python3.10", "python3.11", "python3.12"}:
            basename = "python3"
        if not path.is_absolute():
            return {"input": raw_text, "name": basename, "resolved_path": None, "status": "basename"}
        resolved = str(path.resolve())
        trusted = [str(Path(item).resolve()) for item in self.host_executables_by_name.get(basename, [])]
        if resolved in trusted:
            return {"input": raw_text, "name": basename, "resolved_path": resolved, "status": "trusted_absolute"}
        return {"input": raw_text, "name": basename, "resolved_path": resolved, "status": "untrusted_absolute", "trusted_paths": trusted}

    def _shell_wrapper_decision(
        self,
        command: str,
        normalized: str,
        args: list[str],
        *,
        cwd_policy: str,
    ) -> CommandPolicyDecision | None:
        executable = Path(args[0]).name.lower() if args else ""
        if executable not in self._SHELL_WRAPPERS:
            return None
        if len(args) != 3 or args[1] not in {"-c", "-lc"}:
            return CommandPolicyDecision(
                "forbidden",
                "Shell wrappers are allowed only as bash/sh/zsh -lc with a single inner diagnostic command.",
                command,
                normalized,
                tuple(args),
                (executable,),
                cwd_policy,
            )
        inner = self.decide(args[2])
        if inner.action == "forbidden":
            return CommandPolicyDecision(
                "forbidden",
                f"Shell wrapper inner command is blocked: {inner.reason}",
                command,
                normalized,
                tuple(args),
                (executable,),
                inner.cwd_policy,
            )
        return CommandPolicyDecision(
            inner.action,
            f"Shell wrapper accepted after inner command policy: {inner.reason}",
            command,
            normalized,
            tuple(args),
            inner.matched_prefix or (executable,),
            inner.cwd_policy,
        )

    def _structured_command_decision(
        self,
        command: str,
        normalized: str,
        args: list[str],
        normalized_args: list[str],
        *,
        cwd_policy: str,
    ) -> CommandPolicyDecision | None:
        executable = normalized_args[0] if normalized_args else ""
        if executable == "git":
            return self._git_decision(command, normalized, args, normalized_args, cwd_policy=cwd_policy)
        if executable == "rg":
            for arg in normalized_args[1:]:
                if arg in self._UNSAFE_RG_OPTIONS or any(arg.startswith(f"{option}=") for option in self._UNSAFE_RG_OPTIONS):
                    return CommandPolicyDecision(
                        "forbidden",
                        "ripgrep preprocessing, archive search, and binary helper options are blocked.",
                        command,
                        normalized,
                        tuple(args),
                        ("rg",),
                        cwd_policy,
                    )
        if executable == "find":
            for arg in normalized_args[1:]:
                if arg in self._UNSAFE_FIND_ACTIONS:
                    return CommandPolicyDecision(
                        "forbidden",
                        "find actions that execute commands, delete, or write files are blocked.",
                        command,
                        normalized,
                        tuple(args),
                        ("find",),
                        cwd_policy,
                    )
            return CommandPolicyDecision(
                "allow",
                "Read-only find diagnostics are allowed inside the draft workspace.",
                command,
                normalized,
                tuple(args),
                ("find",),
                cwd_policy,
            )
        return None

    def _git_decision(
        self,
        command: str,
        normalized: str,
        args: list[str],
        normalized_args: list[str],
        *,
        cwd_policy: str,
    ) -> CommandPolicyDecision:
        if len(normalized_args) < 2:
            return CommandPolicyDecision("forbidden", "Git command must specify a read-only subcommand.", command, normalized, tuple(args), ("git",), cwd_policy)
        first = normalized_args[1]
        if first.startswith("-"):
            return CommandPolicyDecision(
                "forbidden",
                "Git global options are blocked; use a direct read-only subcommand.",
                command,
                normalized,
                tuple(args),
                ("git",),
                cwd_policy,
            )
        for arg in normalized_args[1:]:
            if arg in self._UNSAFE_GIT_GLOBAL_OPTIONS or any(arg.startswith(f"{option}=") for option in self._UNSAFE_GIT_GLOBAL_OPTIONS):
                return CommandPolicyDecision(
                    "forbidden",
                    "Git global options that can escape the workspace or run helpers are blocked.",
                    command,
                    normalized,
                    tuple(args),
                    ("git",),
                    cwd_policy,
                )
            if arg in self._UNSAFE_GIT_FLAGS or any(arg.startswith(f"{option}=") for option in self._UNSAFE_GIT_FLAGS):
                return CommandPolicyDecision(
                    "forbidden",
                    "Git output/helper flags that can write files or execute external diff tools are blocked.",
                    command,
                    normalized,
                    tuple(args),
                    ("git", first),
                    cwd_policy,
                )
        if first not in self._SAFE_GIT_SUBCOMMANDS:
            return CommandPolicyDecision(
                "forbidden",
                "Only read-only git diagnostics are allowed: status, log, diff, show, branch, and rev-parse.",
                command,
                normalized,
                tuple(args),
                ("git", first),
                cwd_policy,
            )
        return CommandPolicyDecision(
            "allow",
            "Read-only git diagnostics are allowed inside the draft workspace.",
            command,
            normalized,
            tuple(args),
            ("git", first),
            cwd_policy,
        )

    def validation_examples(self) -> list[dict[str, str]]:
        examples: list[dict[str, str]] = []
        for rule in self.rules:
            for example in rule.examples:
                decision = self.decide(example.command)
                examples.append(
                    {
                        "rule_id": rule.rule_id,
                        "command": example.command,
                        "expected": example.action,
                        "actual": decision.action,
                        "status": "passed" if decision.action == example.action else "failed",
                    }
                )
            for command in rule.not_match_examples:
                try:
                    args = shlex.split(command)
                except ValueError:
                    args = []
                matched_this_rule = bool(args and rule.matches(args))
                examples.append(
                    {
                        "rule_id": rule.rule_id,
                        "command": command,
                        "expected": "not_match",
                        "actual": "matched" if matched_this_rule else "not_match",
                        "status": "failed" if matched_this_rule else "passed",
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
                    "rule_id": rule.rule_id,
                    "source": rule.source,
                    "match": [example.command for example in rule.examples],
                    "not_match": list(rule.not_match_examples),
                }
                for rule in self.rules
            ],
            "examples": self.validation_examples(),
            "host_executables": self.host_executables_by_name,
        }


DEFAULT_COMMAND_POLICY = AgentCommandPolicy()


def configure_default_command_policy(policy: AgentCommandPolicy) -> None:
    global DEFAULT_COMMAND_POLICY
    DEFAULT_COMMAND_POLICY = policy


def decide_workspace_command(command: str) -> CommandPolicyDecision:
    return DEFAULT_COMMAND_POLICY.decide(command)


def command_policy_snapshot() -> dict[str, object]:
    return DEFAULT_COMMAND_POLICY.snapshot()
