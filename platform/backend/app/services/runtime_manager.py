from __future__ import annotations

import os
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings


class PreviewRuntimeManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preferred_mode(self) -> str:
        if os.getenv("PYTEST_CURRENT_TEST"):
            return "inline"
        return "docker"

    def allocate_port(self, workspace_id: str) -> int:
        start = self.settings.preview_port_base + (sum(ord(char) for char in workspace_id) % 1000)
        for port in range(start, start + 400):
            if self._port_free(port):
                return port
        raise RuntimeError("No free preview port available.")

    def port_free(self, port: int) -> bool:
        return self._port_free(port)

    def start(self, workspace_id: str, source_dir: Path, proxy_port: int) -> tuple[str, list[str]]:
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        logs = [result.stdout.strip(), result.stderr.strip()]
        if result.returncode != 0:
            raise RuntimeError("\n".join(filter(None, logs)) or "Docker compose up failed.")
        self.wait_until_ready(proxy_port)
        return project_name, [item for item in logs if item]

    def rebuild(self, workspace_id: str, source_dir: Path, proxy_port: int) -> list[str]:
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Docker compose rebuild failed.")
        self.wait_until_ready(proxy_port)
        return [item for item in [result.stdout.strip(), result.stderr.strip()] if item]

    def reset(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        if proxy_port is None:
            return []
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "down", "-v", "--remove-orphans"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Docker compose down failed.")
        return [item for item in [result.stdout.strip(), result.stderr.strip()] if item]

    def project_name(self, workspace_id: str) -> str:
        return f"grounded_preview_{workspace_id[:18]}"

    def collect_logs(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        if proxy_port is None:
            return []
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            return ["Docker Compose is not available inside the platform backend container."]
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "logs", "--tail", "80"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        return [line for line in output.splitlines() if line.strip()]

    def preview_url(self, proxy_port: int) -> str:
        return f"http://localhost:{proxy_port}"

    def backend_url(self, proxy_port: int) -> str:
        return f"http://localhost:{proxy_port}/api"

    def wait_until_ready(self, proxy_port: int) -> None:
        deadline = time.time() + self.settings.preview_start_timeout_sec
        health_urls = [
            f"http://host.docker.internal:{proxy_port}/health",
            f"http://127.0.0.1:{proxy_port}/health",
            f"http://localhost:{proxy_port}/health",
        ]
        while time.time() < deadline:
            for health_url in health_urls:
                try:
                    with urlopen(health_url, timeout=2) as response:
                        if response.status == 200:
                            return
                except (URLError, OSError):
                    continue
            time.sleep(1)
        raise RuntimeError(
            "Preview runtime did not become healthy at any of: "
            + ", ".join(health_urls)
            + "."
        )

    def _compose_env(self, proxy_port: int) -> dict[str, str]:
        env = os.environ.copy()
        env["PREVIEW_PROXY_PORT"] = str(proxy_port)
        env["PREVIEW_AUTH_ENDPOINT"] = "/api/auth/telegram"
        env["PREVIEW_API_BASE_URL"] = ""
        env["PREVIEW_DEFAULT_ROLE"] = "client"
        env["PREVIEW_POSTGRES_DB"] = "miniapp"
        env["PREVIEW_POSTGRES_USER"] = "miniapp"
        env["PREVIEW_POSTGRES_PASSWORD"] = "miniapp"
        return env

    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            return sock.connect_ex(("127.0.0.1", port)) != 0

    @staticmethod
    def _compose_command() -> list[str] | None:
        try:
            result = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        except FileNotFoundError:
            result = None
        if result is not None and result.returncode == 0:
            return ["docker", "compose"]
        try:
            legacy = subprocess.run(["docker-compose", "version"], capture_output=True, text=True)
        except FileNotFoundError:
            legacy = None
        if legacy is not None and legacy.returncode == 0:
            return ["docker-compose"]
        return None
