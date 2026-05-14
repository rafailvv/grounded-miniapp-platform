from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
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
    parse_tree: dict[str, Any] = field(default_factory=dict)
    blocked_syntax: dict[str, Any] = field(default_factory=dict)
    network_policy: dict[str, Any] = field(default_factory=dict)
    resolved_argv: tuple[str, ...] = ()
    matched_amendments: tuple[dict[str, Any], ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class AgentCommandPolicy:
    """Prefix-rule shell policy for agent diagnostic commands."""

    _SHELL_WRAPPERS = {"bash", "sh"}
    _FORBIDDEN_EXECUTABLES = {
        "zsh",
        "pwsh",
        "powershell",
        "powershell.exe",
        "cmd",
        "cmd.exe",
        "env",
        "eval",
        "source",
        ".",
        "exec",
        "alias",
    }
    _NETWORK_EXECUTABLES = {
        "curl",
        "wget",
        "ssh",
        "scp",
        "rsync",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ftp",
        "sftp",
    }
    _PACKAGE_NETWORK_SUBCOMMANDS = {"install", "i", "add", "update", "upgrade", "download", "publish", "audit", "ci"}
    _PIP_NETWORK_SUBCOMMANDS = {"install", "download", "wheel"}
    _GIT_NETWORK_SUBCOMMANDS = {"clone", "fetch", "pull", "push", "ls-remote", "remote"}
    _GIT_NETWORK_COMPOUNDS = {("submodule", "update"), ("submodule", "add"), ("submodule", "sync")}
    _NODE_NETWORK_FLAGS = {"--experimental-network-imports"}
    _NETWORK_CONFIG_MARKERS = ("proxy=", "http.proxy", "https.proxy", "http_proxy", "https_proxy", "all_proxy")
    _CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    _BLOCKED_META_CHARS = re.compile(r"[`$<>|;]")
    _BLOCKED_EXPANSION = re.compile(r"\$\(|\${|%[A-Za-z_][A-Za-z0-9_]*%")
    _BLOCKED_HEREDOC = re.compile(r"<<-?")
    _BLOCKED_LINE_CONTINUATION = re.compile(r"\\\s*(?:\r?\n)")
    _BLOCKED_ENV_ASSIGNMENT = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=")
    _BLOCKED_BRACE_EXPANSION = re.compile(r"(?:^|[^$])\{[^{}\n]*,[^{}\n]*\}")
    _BLOCKED_PROCESS_SUBSTITUTION = re.compile(r"[<>]\(")
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
        source = str(payload.get("source") or "json") if isinstance(payload, dict) else "json"

        def parse_rules(raw_rules: Any, *, amendment: bool) -> list[CommandPolicyRule]:
            parsed_rules: list[CommandPolicyRule] = []
            for index, raw_rule in enumerate(raw_rules if isinstance(raw_rules, list) else []):
                if not isinstance(raw_rule, dict):
                    continue
                prefixes: list[tuple[str, ...]] = []
                for raw_prefix in raw_rule.get("prefixes", raw_rule.get("pattern", [])):
                    if isinstance(raw_prefix, str):
                        try:
                            prefixes.append(tuple(item.lower() for item in shlex.split(raw_prefix)))
                        except ValueError:
                            continue
                    elif isinstance(raw_prefix, list):
                        prefixes.append(tuple(str(item).lower() for item in raw_prefix))
                action = str(raw_rule.get("action") or raw_rule.get("decision") or "allow")
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
                    parsed_rules.append(
                        CommandPolicyRule(
                            prefixes=tuple(prefixes),
                            action=action,  # type: ignore[arg-type]
                            reason=str(raw_rule.get("reason") or raw_rule.get("justification") or "Rule-file command policy decision."),
                            rule_id=str(raw_rule.get("rule_id") or f"{'json_amendment' if amendment else 'json_rule'}_{index + 1}"),
                            source=f"{source}:amendment" if amendment else source,
                            examples=tuple(examples),
                            not_match_examples=tuple(not_match_examples),
                        )
                    )
            return parsed_rules

        rules = parse_rules(payload.get("rules", []) if isinstance(payload, dict) else [], amendment=False)
        amendments = parse_rules(payload.get("amendments", []) if isinstance(payload, dict) else [], amendment=True)
        if amendments and not rules:
            rules = [*cls.default_rules(), *amendments]
        else:
            rules.extend(amendments)
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
        amendment_rules: list[CommandPolicyRule] = []
        for index, node in enumerate(module.body):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            else:
                raise ValueError(f"{source}:{getattr(node, 'lineno', '?')}: only prefix_rule(...) or amendment_rule(...) calls are allowed")
            if not isinstance(call.func, ast.Name) or call.func.id not in {"prefix_rule", "amendment_rule"}:
                raise ValueError(f"{source}:{getattr(call, 'lineno', '?')}: only prefix_rule(...) or amendment_rule(...) calls are allowed")
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
            target = amendment_rules if call.func.id == "amendment_rule" else rules
            target.append(
                CommandPolicyRule(
                    prefixes=(tuple(item.lower() for item in pattern),),
                    action=decision,  # type: ignore[arg-type]
                    reason=justification,
                    rule_id=f"{'dsl_amendment' if call.func.id == 'amendment_rule' else 'dsl_rule'}_{index + 1}",
                    source=f"{source}:amendment" if call.func.id == "amendment_rule" else source,
                    examples=tuple(examples),
                    not_match_examples=tuple(not_match),
                )
            )
        if amendment_rules and not rules:
            rules = [*cls.default_rules(), *amendment_rules]
        else:
            rules.extend(amendment_rules)
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
            if not first and name == "python":
                first = shutil.which("python3")
            if first:
                paths.append(str(Path(first).resolve()))
            resolved[name] = paths
        return resolved

    def decide(self, command: str) -> CommandPolicyDecision:
        stripped = str(command or "").strip()
        if not stripped:
            return CommandPolicyDecision("forbidden", "Empty command.", command, "")
        parsed = self._parse_shell_subset(stripped)
        if parsed.get("blocked"):
            return self._blocked_decision(
                command=command,
                normalized=str(parsed.get("normalized_command") or stripped),
                reason=str(parsed.get("reason") or "Command is blocked by shell policy."),
                code=str(parsed.get("code") or "blocked_shell_syntax"),
                args=tuple(parsed.get("argv") or ()),
                cwd_policy=str(parsed.get("cwd_policy") or "draft_workspace"),
                parse_tree=parsed.get("parse_tree") if isinstance(parsed.get("parse_tree"), dict) else {},
            )
        normalized = str(parsed.get("normalized_command") or stripped)
        cwd_policy = str(parsed.get("cwd_policy") or "draft_workspace")
        args = [str(item) for item in parsed.get("argv") or []]
        parse_tree = parsed.get("parse_tree") if isinstance(parsed.get("parse_tree"), dict) else {}
        if not args:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Empty command.",
                code="empty_command",
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
            )
        executable_raw = str(args[0] or "")
        executable_basename = Path(executable_raw).name.lower()
        if executable_basename in self._FORBIDDEN_EXECUTABLES:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason=f"{executable_basename} is blocked for agent diagnostics.",
                code="forbidden_executable",
                args=tuple(args),
                matched_prefix=(executable_basename,),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
            )
        if "/" in executable_raw.replace("\\", "/") and not Path(executable_raw).is_absolute():
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Relative executable paths are blocked; use a trusted host executable name.",
                code="relative_executable",
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
            )
        executable_resolution = self._resolve_executable(args[0])
        executable_name = str(executable_resolution.get("name") or Path(args[0]).name).lower()
        normalized_args = [executable_name, *[str(arg).lower() for arg in args[1:]]]
        if normalized_args[0] in {"python3.10", "python3.11", "python3.12"}:
            normalized_args[0] = "python3"
        network_policy = self._network_policy(normalized_args)
        if network_policy.get("blocked"):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason=str(network_policy.get("reason") or "Network-capable command is blocked."),
                code=str(network_policy.get("code") or "network_blocked"),
                args=tuple(args),
                matched_prefix=tuple(network_policy.get("matched_prefix") or (normalized_args[0],)),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                network_policy=network_policy,
            )
        if executable_resolution.get("status") in {"untrusted_absolute", "untrusted_basename", "relative_executable"}:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Executable is not in the trusted host executable map.",
                code=str(executable_resolution.get("status") or "untrusted_executable"),
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
            )
        resolved_argv = self._resolved_argv(args, executable_resolution)
        wrapper_decision = self._shell_wrapper_decision(
            command,
            normalized,
            args,
            cwd_policy=cwd_policy,
            parse_tree=parse_tree,
            executable_resolution=executable_resolution,
            resolved_argv=resolved_argv,
        )
        if wrapper_decision is not None:
            return wrapper_decision
        if any(arg == ".." or arg.startswith("../") or "/../" in arg for arg in args):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Parent-directory paths are blocked.",
                code="path_traversal",
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        if any(str(arg).startswith(("/", "~")) for arg in args[1:]) or str(args[0]).startswith("~"):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Absolute and home-relative paths are blocked.",
                code="absolute_path_argument",
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        if any(re.search(r"[*?\\[]", str(arg)) for arg in args[1:]):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Shell glob patterns are blocked; use explicit paths from list_files/search results.",
                code="glob_pattern",
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        if any(arg == ".git" or arg.startswith(".git/") or "/.git/" in arg for arg in args[1:]):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Git internals are blocked.",
                code="git_internal_path",
                args=tuple(args),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        structured_decision = self._structured_command_decision(command, normalized, args, normalized_args, cwd_policy=cwd_policy)
        if structured_decision is not None:
            return self._enrich_decision(
                structured_decision,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                network_policy=network_policy,
                resolved_argv=resolved_argv,
            )
        if normalized_args[0] == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in normalized_args[1:]):
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="In-place sed edits are blocked.",
                code="sed_in_place",
                args=tuple(args),
                matched_prefix=("sed",),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                network_policy=network_policy,
                resolved_argv=resolved_argv,
            )
        if normalized_args[0] in {"rm", "mv", "cp", "chmod", "chown"}:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Mutating filesystem commands are blocked.",
                code="mutating_filesystem",
                args=tuple(args),
                matched_prefix=(normalized_args[0],),
                cwd_policy=cwd_policy,
                parse_tree=parse_tree,
                executable_resolution=executable_resolution,
                network_policy=network_policy,
                resolved_argv=resolved_argv,
            )
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
                parse_tree=parse_tree,
                network_policy=network_policy,
                resolved_argv=resolved_argv,
                matched_amendments=tuple(
                    rule.match_payload(normalized_args)
                    for rule in matched_rules
                    if rule.source != "builtin"
                ),
            )
        return self._blocked_decision(
            command=command,
            normalized=normalized,
            reason="Only diagnostic commands are allowed: python -m unittest, python -m py_compile, node --test, node --check, rg, sed, ls, find, and read-only git.",
            code="no_matching_allow_rule",
            args=tuple(args),
            cwd_policy=cwd_policy,
            parse_tree=parse_tree,
            executable_resolution=executable_resolution,
            network_policy=network_policy,
            resolved_argv=resolved_argv,
        )

    def _parse_shell_subset(self, command: str) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", str(command or "").strip())
        parse_tree: dict[str, Any] = {
            "schema": "grounded.shell_subset_ast.v1",
            "kind": "simple_command",
            "original": command,
            "normalized": normalized,
            "cwd_policy": "draft_workspace",
            "argv": [],
        }
        if self._CONTROL_CHARS.search(command):
            return {**parse_tree, "blocked": True, "code": "control_character", "reason": "Control characters are blocked in shell commands.", "parse_tree": parse_tree}
        if "\n" in command or "\r" in command:
            return {**parse_tree, "blocked": True, "code": "multiline_command", "reason": "Multiline shell commands are blocked.", "parse_tree": parse_tree}
        checks = [
            (self._BLOCKED_LINE_CONTINUATION, "line_continuation", "Line-continuation shell syntax is blocked."),
            (self._BLOCKED_HEREDOC, "heredoc", "Here-doc and here-string shell syntax is blocked."),
            (self._BLOCKED_PROCESS_SUBSTITUTION, "process_substitution", "Process substitution is blocked."),
            (self._BLOCKED_ENV_ASSIGNMENT, "env_assignment", "Inline environment assignment is blocked for agent diagnostics."),
            (self._BLOCKED_BRACE_EXPANSION, "brace_expansion", "Brace expansion is blocked for agent diagnostics."),
            (self._BLOCKED_EXPANSION, "shell_expansion", "Shell expansion syntax is blocked."),
            (self._BLOCKED_META_CHARS, "shell_metacharacter", "Shell redirection, pipes, variable expansion, command substitution, and command separators are blocked."),
        ]
        for pattern, code, reason in checks:
            if pattern.search(command):
                return {**parse_tree, "blocked": True, "code": code, "reason": reason, "parse_tree": parse_tree}
        if "||" in normalized:
            return {**parse_tree, "blocked": True, "code": "or_chain", "reason": "Shell OR chains are blocked.", "parse_tree": parse_tree}
        cwd_policy = "draft_workspace"
        command_to_parse = normalized
        if "&&" in normalized:
            match = re.match(r"^cd\s+miniapp\s+&&\s+(.+)$", normalized, flags=re.I)
            if not match:
                return {**parse_tree, "blocked": True, "code": "command_chain", "reason": "Command chaining is blocked except for 'cd miniapp && <diagnostic>'.", "parse_tree": parse_tree}
            command_to_parse = match.group(1).strip()
            cwd_policy = "miniapp"
            if "&&" in command_to_parse:
                return {**parse_tree, "blocked": True, "code": "nested_command_chain", "reason": "Only one safe 'cd miniapp &&' prefix is allowed.", "parse_tree": parse_tree}
        if "&" in command_to_parse:
            return {**parse_tree, "blocked": True, "code": "background_execution", "reason": "Background shell execution is blocked.", "parse_tree": parse_tree}
        try:
            args = shlex.split(command_to_parse)
        except ValueError as exc:
            return {**parse_tree, "blocked": True, "code": "parse_error", "reason": f"Command could not be parsed safely: {exc}.", "parse_tree": parse_tree}
        parse_tree = {
            **parse_tree,
            "kind": "simple_command",
            "normalized": command_to_parse,
            "cwd_policy": cwd_policy,
            "argv": list(args),
            "wrapper": Path(args[0]).name.lower() in self._SHELL_WRAPPERS if args else False,
        }
        return {
            "blocked": False,
            "normalized_command": command_to_parse,
            "argv": list(args),
            "cwd_policy": cwd_policy,
            "parse_tree": parse_tree,
        }

    @staticmethod
    def _blocked_syntax_payload(code: str, reason: str) -> dict[str, Any]:
        return {"code": code, "reason": reason, "blocked": True}

    @staticmethod
    def _default_network_policy() -> dict[str, Any]:
        return {"mode": "blocked_by_default", "blocked": False, "reason": "No direct network behavior detected."}

    def _blocked_decision(
        self,
        *,
        command: str,
        normalized: str,
        reason: str,
        code: str,
        args: tuple[str, ...] = (),
        matched_prefix: tuple[str, ...] = (),
        cwd_policy: str = "draft_workspace",
        parse_tree: dict[str, Any] | None = None,
        executable_resolution: dict[str, Any] | None = None,
        network_policy: dict[str, Any] | None = None,
        resolved_argv: tuple[str, ...] = (),
    ) -> CommandPolicyDecision:
        matched_rules: tuple[dict[str, Any], ...] = ()
        if matched_prefix:
            matched_rules = (
                {
                    "rule_id": f"hard_{code}",
                    "source": "hard_policy",
                    "pattern": list(matched_prefix),
                    "decision": "forbidden",
                    "justification": reason,
                },
            )
        return CommandPolicyDecision(
            "forbidden",
            reason,
            command,
            normalized,
            args,
            matched_prefix,
            cwd_policy,
            matched_rules,
            executable_resolution=executable_resolution or {},
            parse_tree=parse_tree or {},
            blocked_syntax=self._blocked_syntax_payload(code, reason),
            network_policy=network_policy or self._default_network_policy(),
            resolved_argv=resolved_argv,
        )

    @staticmethod
    def _enrich_decision(
        decision: CommandPolicyDecision,
        *,
        parse_tree: dict[str, Any],
        executable_resolution: dict[str, Any],
        network_policy: dict[str, Any],
        resolved_argv: tuple[str, ...],
    ) -> CommandPolicyDecision:
        return replace(
            decision,
            parse_tree=parse_tree,
            executable_resolution=executable_resolution,
            network_policy=network_policy,
            resolved_argv=resolved_argv,
        )

    def _resolve_executable(self, raw: str) -> dict[str, Any]:
        raw_text = str(raw or "")
        path = Path(raw_text)
        basename = path.name.lower()
        if basename in {"python3.10", "python3.11", "python3.12"}:
            basename = "python3"
        trusted = [str(Path(item).resolve()) for item in self.host_executables_by_name.get(basename, [])]
        if "/" in raw_text.replace("\\", "/") and not path.is_absolute():
            return {"input": raw_text, "name": basename, "resolved_path": None, "status": "relative_executable", "trusted_paths": trusted}
        if not path.is_absolute():
            if trusted:
                return {"input": raw_text, "name": basename, "resolved_path": trusted[0], "status": "trusted_basename", "trusted_paths": trusted}
            return {"input": raw_text, "name": basename, "resolved_path": None, "status": "untrusted_basename", "trusted_paths": trusted}
        resolved = str(path.resolve())
        if resolved in trusted:
            return {"input": raw_text, "name": basename, "resolved_path": resolved, "status": "trusted_absolute"}
        return {"input": raw_text, "name": basename, "resolved_path": resolved, "status": "untrusted_absolute", "trusted_paths": trusted}

    @staticmethod
    def _resolved_argv(args: list[str], executable_resolution: dict[str, Any]) -> tuple[str, ...]:
        resolved = str(executable_resolution.get("resolved_path") or "")
        if not resolved:
            return tuple(args)
        return (resolved, *tuple(args[1:]))

    def _network_policy(self, normalized_args: list[str]) -> dict[str, Any]:
        if not normalized_args:
            return self._default_network_policy()
        executable = normalized_args[0]
        if executable in self._NETWORK_EXECUTABLES:
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "direct_network_tool",
                "reason": "Direct network tools are blocked for agent diagnostics.",
                "matched_prefix": [executable],
            }
        if executable in {"npm", "pnpm", "yarn"} and len(normalized_args) >= 2 and normalized_args[1] in self._PACKAGE_NETWORK_SUBCOMMANDS:
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "package_network_operation",
                "reason": "Package manager install/update/network operations are blocked.",
                "matched_prefix": [executable, normalized_args[1]],
            }
        if executable in {"pip", "pip3"} and len(normalized_args) >= 2 and normalized_args[1] in self._PIP_NETWORK_SUBCOMMANDS:
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "package_network_operation",
                "reason": "Pip install/download operations are blocked.",
                "matched_prefix": [executable, normalized_args[1]],
            }
        if executable in {"python", "python3"} and normalized_args[1:3] == ["-m", "pip"] and len(normalized_args) >= 4 and normalized_args[3] in self._PIP_NETWORK_SUBCOMMANDS:
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "package_network_operation",
                "reason": "Pip install/download operations are blocked.",
                "matched_prefix": [executable, "-m", "pip", normalized_args[3]],
            }
        if executable == "node" and any(arg in self._NODE_NETWORK_FLAGS for arg in normalized_args[1:]):
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "node_network_imports",
                "reason": "Node network import flags are blocked.",
                "matched_prefix": ["node"],
            }
        if executable == "git" and len(normalized_args) >= 2:
            subcommand = normalized_args[1]
            compound = tuple(normalized_args[1:3])
            if subcommand in self._GIT_NETWORK_SUBCOMMANDS or compound in self._GIT_NETWORK_COMPOUNDS:
                return {
                    "mode": "blocked_by_default",
                    "blocked": True,
                    "code": "git_network_operation",
                    "reason": "Git network operations are blocked.",
                    "matched_prefix": ["git", *list(compound if compound in self._GIT_NETWORK_COMPOUNDS else (subcommand,))],
                }
        if any(any(marker in arg for marker in self._NETWORK_CONFIG_MARKERS) for arg in normalized_args[1:]):
            return {
                "mode": "blocked_by_default",
                "blocked": True,
                "code": "network_proxy_config",
                "reason": "Proxy/network configuration flags are blocked.",
                "matched_prefix": [executable],
            }
        return self._default_network_policy()

    def _shell_wrapper_decision(
        self,
        command: str,
        normalized: str,
        args: list[str],
        *,
        cwd_policy: str,
        parse_tree: dict[str, Any],
        executable_resolution: dict[str, Any],
        resolved_argv: tuple[str, ...],
    ) -> CommandPolicyDecision | None:
        executable = Path(args[0]).name.lower() if args else ""
        if executable not in self._SHELL_WRAPPERS:
            return None
        if len(args) != 3 or args[1] != "-lc":
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Shell wrappers are allowed only as bash/sh -lc with a single inner diagnostic command.",
                code="shell_wrapper_shape",
                args=tuple(args),
                matched_prefix=(executable,),
                cwd_policy=cwd_policy,
                parse_tree={**parse_tree, "kind": "shell_wrapper", "wrapper": executable},
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        try:
            inner_args = shlex.split(args[2])
        except ValueError as exc:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason=f"Shell wrapper inner command could not be parsed safely: {exc}.",
                code="shell_wrapper_inner_parse_error",
                args=tuple(args),
                matched_prefix=(executable,),
                cwd_policy=cwd_policy,
                parse_tree={**parse_tree, "kind": "shell_wrapper", "wrapper": executable},
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        inner_executable = Path(inner_args[0]).name.lower() if inner_args else ""
        if inner_executable in self._SHELL_WRAPPERS or inner_executable in self._FORBIDDEN_EXECUTABLES:
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason="Nested shell wrappers and shell bypass executables are blocked.",
                code="nested_shell_wrapper",
                args=tuple(args),
                matched_prefix=(executable,),
                cwd_policy=cwd_policy,
                parse_tree={**parse_tree, "kind": "shell_wrapper", "wrapper": executable, "inner_argv": inner_args},
                executable_resolution=executable_resolution,
                resolved_argv=resolved_argv,
            )
        inner = self.decide(args[2])
        if inner.action == "forbidden":
            return self._blocked_decision(
                command=command,
                normalized=normalized,
                reason=f"Shell wrapper inner command is blocked: {inner.reason}",
                code="shell_wrapper_inner_blocked",
                args=tuple(args),
                matched_prefix=(executable,),
                cwd_policy=inner.cwd_policy,
                parse_tree={**parse_tree, "kind": "shell_wrapper", "wrapper": executable, "inner": inner.parse_tree},
                executable_resolution=executable_resolution,
                network_policy=inner.network_policy,
                resolved_argv=resolved_argv,
            )
        return CommandPolicyDecision(
            inner.action,
            f"Shell wrapper accepted after inner command policy: {inner.reason}",
            command,
            normalized,
            tuple(args),
            inner.matched_prefix or (executable,),
            inner.cwd_policy,
            inner.matched_rules,
            executable_resolution,
            parse_tree={**parse_tree, "kind": "shell_wrapper", "wrapper": executable, "inner": inner.parse_tree},
            network_policy=inner.network_policy,
            resolved_argv=resolved_argv,
            matched_amendments=inner.matched_amendments,
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
            "policy": "shell_subset_prefix_rule",
            "shell_parser": {
                "schema": "grounded.shell_subset_ast.v1",
                "allowed_forms": ["SimpleCommand", "cd miniapp && SimpleCommand", "bash|sh -lc SimpleCommand"],
                "forbidden_executables": sorted(self._FORBIDDEN_EXECUTABLES),
                "network_mode": "blocked_by_default",
            },
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
            "amendment_count": sum(1 for rule in self.rules if rule.source != "builtin"),
        }


DEFAULT_COMMAND_POLICY = AgentCommandPolicy()


def configure_default_command_policy(policy: AgentCommandPolicy) -> None:
    global DEFAULT_COMMAND_POLICY
    DEFAULT_COMMAND_POLICY = policy


def decide_workspace_command(command: str) -> CommandPolicyDecision:
    return DEFAULT_COMMAND_POLICY.decide(command)


def command_policy_snapshot() -> dict[str, object]:
    return DEFAULT_COMMAND_POLICY.snapshot()
