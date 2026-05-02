from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AgentEnvironmentSnapshot:
    cwd: str
    captured_at: str
    python_version: str | None
    node_version: str | None
    npm_version: str | None
    manifests: dict[str, object]
    available_test_commands: list[str]
    shell_policy: dict[str, object]

    @staticmethod
    def _run_version(cwd: Path, command: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = (completed.stdout or completed.stderr or "").strip()
        return output.splitlines()[0][:160] if output else None

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def capture(cls, *, draft_source: Path, command_policy: Callable[[str], str | None]) -> dict[str, object]:
        manifests: dict[str, object] = {}
        for relative_path in (
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
        ):
            path = draft_source / relative_path
            if not path.exists():
                continue
            if relative_path == "package.json":
                package = cls._read_json(path)
                manifests[relative_path] = {
                    "scripts": package.get("scripts", {}),
                    "dependencies_count": len(package.get("dependencies", {}) or {}),
                    "dev_dependencies_count": len(package.get("devDependencies", {}) or {}),
                }
            else:
                manifests[relative_path] = {"exists": True, "size": path.stat().st_size}

        candidate_commands = [
            "python -m py_compile miniapp/app/main.py",
            "python -m unittest discover miniapp/tests",
            "node --check miniapp/tests/generated_app.test.mjs",
            "node --test miniapp/tests/generated_app.test.mjs",
            "rg fetch miniapp/app",
            "ls miniapp",
        ]
        available = [command for command in candidate_commands if command_policy(command) is None]
        snapshot = cls(
            cwd=str(draft_source),
            captured_at=datetime.now(timezone.utc).isoformat(),
            python_version=cls._run_version(draft_source, ["python3", "--version"])
            or cls._run_version(draft_source, ["python", "--version"]),
            node_version=cls._run_version(draft_source, ["node", "--version"]),
            npm_version=cls._run_version(draft_source, ["npm", "--version"]),
            manifests=manifests,
            available_test_commands=available,
            shell_policy={
                "allowed_diagnostics": available,
                "blocked_examples": {
                    "install": command_policy("npm install"),
                    "network": command_policy("curl https://example.com"),
                    "destructive": command_policy("rm -rf miniapp"),
                },
            },
        )
        return {
            "cwd": snapshot.cwd,
            "captured_at": snapshot.captured_at,
            "python_version": snapshot.python_version,
            "node_version": snapshot.node_version,
            "npm_version": snapshot.npm_version,
            "manifests": snapshot.manifests,
            "available_test_commands": snapshot.available_test_commands,
            "shell_policy": snapshot.shell_policy,
        }
