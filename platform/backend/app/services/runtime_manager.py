from __future__ import annotations

import json
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
    PREVIEW_SERVICES = ("preview-backend", "preview-frontend", "preview-proxy", "preview-db")

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
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"]
        started_at = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        logs = [
            f"[runtime] starting docker preview for workspace {workspace_id} on port {proxy_port}",
            f"[runtime] command: {' '.join(command)}",
            *self._command_output_logs(result.stdout, result.stderr),
        ]
        if result.returncode != 0:
            raise RuntimeError("\n".join(filter(None, logs)) or "Docker compose up failed.")
        wait_logs = self.wait_until_ready(proxy_port)
        logs.extend(wait_logs)
        logs.append(f"[runtime] docker preview started in {int((time.perf_counter() - started_at) * 1000)}ms")
        return project_name, [item for item in logs if item]

    def rebuild(self, workspace_id: str, source_dir: Path, proxy_port: int) -> list[str]:
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"]
        started_at = time.perf_counter()
        result = subprocess.run(
            command,
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Docker compose rebuild failed.")
        logs = [
            f"[runtime] rebuilding docker preview for workspace {workspace_id} on port {proxy_port}",
            f"[runtime] command: {' '.join(command)}",
            *self._command_output_logs(result.stdout, result.stderr),
        ]
        logs.extend(self.wait_until_ready(proxy_port))
        logs.append(f"[runtime] docker preview rebuild completed in {int((time.perf_counter() - started_at) * 1000)}ms")
        return [item for item in logs if item]

    def reset(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        if proxy_port is None:
            return []
        project_name = self.project_name(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "down", "-v", "--remove-orphans"]
        result = subprocess.run(
            command,
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Docker compose down failed.")
        return [
            f"[runtime] stopping docker preview on port {proxy_port}",
            f"[runtime] command: {' '.join(command)}",
            *self._command_output_logs(result.stdout, result.stderr),
        ]

    def project_name(self, workspace_id: str) -> str:
        return f"grounded_preview_{workspace_id[:18]}"

    def collect_logs(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        if proxy_port is None:
            return []
        compose_cmd, compose_file, project_name, env = self._compose_parts(workspace_id, source_dir, proxy_port)
        if compose_cmd is None:
            return ["Docker Compose is not available inside the platform backend container."]
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "logs", "--tail", "200"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        return [line for line in output.splitlines() if line.strip()]

    def collect_container_logs(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> dict[str, list[str]]:
        if proxy_port is None:
            return {}
        compose_cmd, compose_file, project_name, env = self._compose_parts(workspace_id, source_dir, proxy_port)
        if compose_cmd is None:
            return {"platform": ["Docker Compose is not available inside the platform backend container."]}
        logs_by_service: dict[str, list[str]] = {}
        for service in self.PREVIEW_SERVICES:
            result = subprocess.run(
                [*compose_cmd, "-f", str(compose_file), "-p", project_name, "logs", "--tail", "120", service],
                cwd=source_dir,
                capture_output=True,
                text=True,
                env=env,
            )
            output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
            lines = [line for line in output.splitlines() if line.strip()]
            logs_by_service[service] = lines or [f"No logs for {service} yet."]
        return logs_by_service

    def inspect_containers(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[dict[str, str | None]]:
        if proxy_port is None:
            return [
                {
                    "service": service,
                    "name": None,
                    "state": "missing",
                    "status": "not started",
                    "health": None,
                    "exit_code": None,
                }
                for service in self.PREVIEW_SERVICES
            ]
        compose_cmd, compose_file, project_name, env = self._compose_parts(workspace_id, source_dir, proxy_port)
        if compose_cmd is None:
            return []
        result = subprocess.run(
            [*compose_cmd, "-f", str(compose_file), "-p", project_name, "ps", "-a", "--format", "json"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        service_map: dict[str, dict[str, str | None]] = {}
        payload = result.stdout.strip()
        if payload:
            parsed_rows: list[dict[str, object]] = []
            try:
                decoded = json.loads(payload)
                if isinstance(decoded, list):
                    parsed_rows = [item for item in decoded if isinstance(item, dict)]
            except json.JSONDecodeError:
                for line in payload.splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        parsed_rows.append(item)
            for row in parsed_rows:
                service = str(row.get("Service") or row.get("service") or "").strip()
                if not service:
                    continue
                service_map[service] = {
                    "service": service,
                    "name": str(row.get("Name") or row.get("name") or "") or None,
                    "state": str(row.get("State") or row.get("state") or "") or None,
                    "status": str(row.get("Status") or row.get("status") or "") or None,
                    "health": str(row.get("Health") or row.get("health") or "") or None,
                    "exit_code": str(row.get("ExitCode") or row.get("exitCode") or "") or None,
                }
        containers: list[dict[str, str | None]] = []
        for service in self.PREVIEW_SERVICES:
            containers.append(
                service_map.get(
                    service,
                    {
                        "service": service,
                        "name": None,
                        "state": "missing",
                        "status": "not started",
                        "health": None,
                        "exit_code": None,
                    },
                )
            )
        return containers

    def preview_url(self, proxy_port: int) -> str:
        return f"http://localhost:{proxy_port}"

    def backend_url(self, proxy_port: int) -> str:
        return f"http://localhost:{proxy_port}/api"

    def wait_until_ready(self, proxy_port: int) -> list[str]:
        deadline = time.time() + self.settings.preview_start_timeout_sec
        health_urls = [
            f"http://host.docker.internal:{proxy_port}/health",
            f"http://127.0.0.1:{proxy_port}/health",
            f"http://localhost:{proxy_port}/health",
        ]
        attempts = 0
        logs = [f"[runtime] waiting for preview health on port {proxy_port}"]
        while time.time() < deadline:
            attempts += 1
            for health_url in health_urls:
                try:
                    with urlopen(health_url, timeout=2) as response:
                        if response.status == 200:
                            logs.append(f"[runtime] health probe #{attempts} passed at {health_url}")
                            return logs
                except (URLError, OSError):
                    continue
            if attempts <= 5 or attempts % 5 == 0:
                logs.append(f"[runtime] health probe #{attempts} pending")
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

    def _compose_parts(
        self,
        workspace_id: str,
        source_dir: Path,
        proxy_port: int,
    ) -> tuple[list[str] | None, Path, str, dict[str, str]]:
        compose_file = source_dir / "docker" / "docker-compose.yml"
        project_name = self.project_name(workspace_id)
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        return compose_cmd, compose_file, project_name, env

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

    @staticmethod
    def _command_output_logs(stdout: str, stderr: str, *, tail_lines: int = 40) -> list[str]:
        merged = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
        if not merged:
            return []
        lines = [line.rstrip() for line in merged.splitlines() if line.strip()]
        return lines[-tail_lines:]
