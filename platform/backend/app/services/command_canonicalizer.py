from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath
import re
import shlex
from typing import Any


TEST_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
NODE_BUILD_SCRIPTS = {"build", "compile"}
DEPENDENCY_MARKERS = (
    "module not found",
    "cannot find module",
    "no module named",
    "modulenotfounderror",
    "importerror",
    "command not found",
    "no such file or directory",
    "enoent",
)


@dataclass(frozen=True)
class CommandCanonicalForm:
    schema: str = "grounded.command_canonical_form.v1"
    raw_command: str = ""
    command_family: str = "generic.shell"
    runner: str = ""
    subcommand: str = ""
    package_manager: str | None = None
    target_args: list[str] = field(default_factory=list)
    normalized_family_command: str = ""
    command_intent: str = "diagnostic"
    retry_recipe_id: str = "command.generic_shell"
    status_taxonomy: str = "unknown"
    fingerprint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "raw_command": self.raw_command,
            "command_family": self.command_family,
            "runner": self.runner,
            "subcommand": self.subcommand,
            "package_manager": self.package_manager,
            "target_args": list(self.target_args),
            "normalized_family_command": self.normalized_family_command,
            "command_intent": self.command_intent,
            "retry_recipe_id": self.retry_recipe_id,
            "status_taxonomy": self.status_taxonomy,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CommandExecutionClassification:
    schema: str = "grounded.command_execution_classification.v1"
    command_family: str = "generic.shell"
    status_taxonomy: str = "unknown_failure"
    semantic_status: str = "unknown_failure"
    success: bool = False
    retry_recipe_id: str = "command.generic_shell"
    failure_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_family": self.command_family,
            "status_taxonomy": self.status_taxonomy,
            "semantic_status": self.semantic_status,
            "success": self.success,
            "retry_recipe_id": self.retry_recipe_id,
            "failure_hint": self.failure_hint,
        }


class CommandCanonicalizer:
    """Deterministic command family/status classifier for shell and check runs."""

    @classmethod
    def canonicalize(
        cls,
        command: str,
        *,
        argv: list[str] | tuple[str, ...] | None = None,
        workspace_id: str | None = None,
        status_taxonomy: str | None = None,
    ) -> dict[str, Any]:
        tokens = [str(item) for item in (argv or []) if str(item or "").strip()]
        if not tokens:
            tokens = cls._split(command)
        family, runner, subcommand, package_manager, targets, intent = cls._family(tokens)
        normalized = cls._normalized_family_command(family=family, runner=runner, subcommand=subcommand, package_manager=package_manager, targets=targets)
        retry_recipe_id = cls.retry_recipe_for(command_family=family, status_taxonomy=status_taxonomy)
        fingerprint = cls.family_fingerprint(
            workspace_id=workspace_id,
            command_family=family,
            normalized_family_command=normalized,
        )
        return CommandCanonicalForm(
            raw_command=str(command or ""),
            command_family=family,
            runner=runner,
            subcommand=subcommand,
            package_manager=package_manager,
            target_args=targets,
            normalized_family_command=normalized,
            command_intent=intent,
            retry_recipe_id=retry_recipe_id,
            status_taxonomy=str(status_taxonomy or "unknown"),
            fingerprint=fingerprint,
        ).as_dict()

    @classmethod
    def classify_execution(
        cls,
        *,
        command: str,
        argv: list[str] | tuple[str, ...] | None = None,
        workspace_id: str | None = None,
        exit_code: int | None,
        timed_out: bool = False,
        stdout: str = "",
        stderr: str = "",
        semantic_status: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        base = cls.canonicalize(command, argv=argv, workspace_id=workspace_id)
        status, success, hint = cls._status_for(
            command_family=str(base.get("command_family") or "generic.shell"),
            runner=str(base.get("runner") or ""),
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            semantic_status=semantic_status,
        )
        canonical = {
            **base,
            "status_taxonomy": status,
            "retry_recipe_id": cls.retry_recipe_for(command_family=str(base.get("command_family") or ""), status_taxonomy=status),
        }
        classification = CommandExecutionClassification(
            command_family=str(canonical.get("command_family") or "generic.shell"),
            status_taxonomy=status,
            semantic_status=status,
            success=success,
            retry_recipe_id=str(canonical.get("retry_recipe_id") or "command.generic_shell"),
            failure_hint=hint,
        ).as_dict()
        return canonical, classification

    @staticmethod
    def family_fingerprint(*, workspace_id: str | None, command_family: str, normalized_family_command: str) -> str:
        scope = str(workspace_id or "global")
        payload = f"{scope}\n{command_family}\n{normalized_family_command}"
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def retry_recipe_for(*, command_family: str, status_taxonomy: str | None = None) -> str:
        family = str(command_family or "generic.shell")
        status = str(status_taxonomy or "").strip()
        if family in {"python.pytest", "python.unittest"}:
            return "command.python_test_repair"
        if family == "node.test":
            return "command.node_test_repair"
        if family == "python.uvicorn":
            return "command.uvicorn_boot_repair"
        if family == "docker.compose":
            return "command.docker_compose_repair"
        if family == "playwright.test":
            return "command.playwright_replay_repair"
        if family == "node.build":
            return "command.node_build_repair"
        if status in {"policy_blocked", "sandbox_blocked"}:
            return "command.policy_or_sandbox_repair"
        return "command.generic_shell"

    @staticmethod
    def _split(command: str) -> list[str]:
        text = str(command or "").strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except ValueError:
            return [part for part in re.split(r"\s+", text) if part]

    @classmethod
    def _family(cls, tokens: list[str]) -> tuple[str, str, str, str | None, list[str], str]:
        if not tokens:
            return "generic.shell", "", "", None, [], "diagnostic"
        names = [PurePosixPath(item).name.lower() for item in tokens]
        first = names[0]
        raw = [str(item) for item in tokens]
        if first == "uv" and len(names) >= 3 and names[1] == "run":
            nested = raw[2:]
            family, runner, subcommand, package_manager, targets, intent = cls._family(nested)
            return family, f"uv run {runner}".strip(), subcommand, package_manager, targets, intent
        if first in {"python", "python3"} and len(names) >= 3 and names[1] == "-m":
            module = names[2]
            rest = raw[3:]
            if module == "pytest":
                return "python.pytest", first, "pytest", None, cls._target_args(rest), "test"
            if module == "unittest":
                return "python.unittest", first, "unittest", None, cls._target_args(rest), "test"
            if module == "uvicorn":
                return "python.uvicorn", first, "uvicorn", None, cls._target_args(rest), "server"
            if module == "py_compile":
                return "python.compile", first, "py_compile", None, cls._target_args(rest), "build"
        if first == "pytest":
            return "python.pytest", first, "pytest", None, cls._target_args(raw[1:]), "test"
        if first == "uvicorn":
            return "python.uvicorn", first, "uvicorn", None, cls._target_args(raw[1:]), "server"
        if first in TEST_PACKAGE_MANAGERS:
            script = cls._node_script(names, raw)
            if script == "test":
                return "node.test", first, script, first, cls._target_args(raw[2:] if len(names) > 1 and names[1] == "run" else raw[1:]), "test"
            if script in NODE_BUILD_SCRIPTS:
                return "node.build", first, script, first, cls._target_args(raw[2:] if len(names) > 1 and names[1] == "run" else raw[1:]), "build"
        if first == "node":
            if len(names) > 1 and names[1] == "--test":
                return "node.test", first, "--test", None, cls._target_args(raw[2:]), "test"
            if len(names) > 1 and names[1] == "--check":
                return "node.check", first, "--check", None, cls._target_args(raw[2:]), "build"
        if first == "npx" and len(names) >= 3 and names[1] == "playwright" and names[2] == "test":
            return "playwright.test", first, "playwright test", "npx", cls._target_args(raw[3:]), "browser_test"
        if first == "playwright" and len(names) >= 2 and names[1] == "test":
            return "playwright.test", first, "test", None, cls._target_args(raw[2:]), "browser_test"
        if first == "docker-compose" or (first == "docker" and len(names) >= 2 and names[1] == "compose"):
            offset = 1 if first == "docker-compose" else 2
            subcommand = names[offset] if len(names) > offset else "compose"
            return "docker.compose", first, subcommand, None, cls._target_args(raw[offset + 1 :]), "orchestration"
        return "generic.shell", first, names[1] if len(names) > 1 else "", None, cls._target_args(raw[2:] if len(raw) > 1 else []), "diagnostic"

    @staticmethod
    def _node_script(names: list[str], raw: list[str]) -> str:
        if len(names) < 2:
            return ""
        if names[1] == "run" and len(names) >= 3:
            return names[2]
        if names[1] in {"test", "build", "compile"}:
            return names[1]
        if names[0] == "yarn" and names[1] not in {"add", "install"}:
            return names[1]
        return ""

    @staticmethod
    def _target_args(args: list[str]) -> list[str]:
        targets: list[str] = []
        skip_next = False
        for raw in args:
            value = str(raw or "").strip()
            if not value:
                continue
            if skip_next:
                skip_next = False
                continue
            if value in {"--port", "-p", "--host", "--config", "-c"}:
                skip_next = True
                continue
            if value.startswith("-"):
                continue
            targets.append(value)
        return targets[:16]

    @staticmethod
    def _normalized_family_command(
        *,
        family: str,
        runner: str,
        subcommand: str,
        package_manager: str | None,
        targets: list[str],
    ) -> str:
        prefix = family
        if family == "node.test" and package_manager:
            prefix = "node.test"
        elif family == "docker.compose":
            prefix = "docker.compose"
        elif family == "generic.shell":
            prefix = " ".join(item for item in (runner, subcommand) if item).strip() or "generic.shell"
        target_text = " ".join(sorted(str(item) for item in targets if str(item).strip()))
        return " ".join(part for part in (prefix, target_text) if part).strip()

    @classmethod
    def _status_for(
        cls,
        *,
        command_family: str,
        runner: str,
        exit_code: int | None,
        timed_out: bool,
        stdout: str,
        stderr: str,
        semantic_status: str | None,
    ) -> tuple[str, bool, str]:
        if timed_out:
            return "timeout", False, "command timed out"
        explicit = str(semantic_status or "").strip()
        if explicit == "passed":
            return "passed", True, ""
        if explicit in {"blocked_by_policy", "policy_blocked"}:
            return "policy_blocked", False, explicit
        if explicit in {"blocked_by_sandbox", "sandbox_blocked"}:
            return "sandbox_blocked", False, explicit
        if exit_code is None:
            return explicit or "not_started", False, explicit or "command did not start"
        if exit_code == 0:
            return "passed", True, ""
        executable = PurePosixPath(runner.split()[-1]).name.lower() if runner else ""
        if executable == "rg" and exit_code == 1:
            return "no_matches", True, "ripgrep found no matches"
        if executable == "diff" and exit_code == 1:
            return "differences_found", True, "diff found differences"
        text = f"{stdout}\n{stderr}".lower()
        if command_family == "docker.compose" and any(marker in text for marker in ("cannot connect to the docker daemon", "docker daemon", "is the docker daemon running")):
            return "docker_unavailable", False, "docker daemon unavailable"
        if command_family == "playwright.test" and any(marker in text for marker in ("browser executable doesn't exist", "please run playwright install", "no chromium-based browser found")):
            return "browser_missing", False, "playwright browser runtime missing"
        if "address already in use" in text or "eaddrinuse" in text or "port is already allocated" in text:
            return "port_in_use", False, "port already in use"
        if any(marker in text for marker in DEPENDENCY_MARKERS):
            return "dependency_missing", False, "dependency or executable missing"
        if "traceback" in text or "syntaxerror" in text or "nameerror" in text:
            if command_family == "python.uvicorn":
                return "server_boot_failed", False, "server startup traceback"
            return "import_error", False, "python import/runtime traceback"
        if command_family in {"python.pytest", "python.unittest", "node.test", "playwright.test"} or "failed" in text or "assertionerror" in text:
            return "failed_tests", False, "test command failed"
        if command_family == "python.uvicorn":
            return "server_boot_failed", False, "server command failed"
        return "unknown_failure", False, "command failed without a known signature"
