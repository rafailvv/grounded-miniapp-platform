from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings


class PreviewRuntimeManager:
    PREVIEW_SERVICES = ("preview-app",)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preferred_mode(self) -> str:
        if os.getenv("PYTEST_CURRENT_TEST"):
            return "inline"
        return "docker"

    def allocate_port(self, workspace_id: str, reserved_ports: set[int] | None = None) -> int:
        reserved_ports = reserved_ports or set()
        start = self.settings.preview_port_base + (sum(ord(char) for char in workspace_id) % 1000)
        for port in range(start, start + 400):
            if port in reserved_ports:
                continue
            if self._port_free(port):
                return port
        raise RuntimeError("No free preview port available.")

    def port_free(self, port: int) -> bool:
        return self._port_free(port)

    def start(self, workspace_id: str, source_dir: Path, proxy_port: int) -> tuple[str, list[str]]:
        project_name = self.project_name(workspace_id)
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        compose_file = self._render_host_compose_file(source_dir)
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"]
        started_at = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                cwd=self._compose_workdir(source_dir),
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            compose_file.unlink(missing_ok=True)
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
        env = self._compose_env(proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        compose_file = self._render_host_compose_file(source_dir)
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "up", "-d", "--build"]
        started_at = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                cwd=self._compose_workdir(source_dir),
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            compose_file.unlink(missing_ok=True)
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
        project_name = self.project_name(workspace_id)
        effective_proxy_port = proxy_port or self.settings.preview_port_base
        env = self._compose_env(effective_proxy_port)
        compose_cmd = self._compose_command()
        if compose_cmd is None:
            raise RuntimeError("Docker Compose is not available inside the platform backend container.")
        try:
            compose_file = self._render_host_compose_file(source_dir)
        except FileNotFoundError as exc:
            return [f"[runtime] {exc}; nothing to stop."]
        command = [*compose_cmd, "-f", str(compose_file), "-p", project_name, "down", "-v", "--remove-orphans"]
        try:
            result = subprocess.run(
                command,
                cwd=self._compose_workdir(source_dir),
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            compose_file.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Docker compose down failed.")
        return [
            f"[runtime] stopping docker preview on port {effective_proxy_port}",
            f"[runtime] command: {' '.join(command)}",
            *self._command_output_logs(result.stdout, result.stderr),
        ]

    def project_name(self, workspace_id: str) -> str:
        return f"grounded_preview_{workspace_id[:18]}"

    def collect_logs(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        try:
            compose_file = self._render_host_compose_file(source_dir)
        except FileNotFoundError:
            return []
        compose_cmd = self._compose_command()
        project_name = self.project_name(workspace_id)
        env = self._compose_env(proxy_port or self.settings.preview_port_base)
        if compose_cmd is None:
            return ["Docker Compose is not available inside the platform backend container."]
        try:
            result = subprocess.run(
                [*compose_cmd, "-f", str(compose_file), "-p", project_name, "logs", "--tail", "200"],
                cwd=self._compose_workdir(source_dir),
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            compose_file.unlink(missing_ok=True)
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        return [line for line in output.splitlines() if line.strip()]

    def collect_container_logs(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> dict[str, list[str]]:
        try:
            compose_file = self._render_host_compose_file(source_dir)
        except FileNotFoundError:
            return {}
        compose_cmd = self._compose_command()
        project_name = self.project_name(workspace_id)
        env = self._compose_env(proxy_port or self.settings.preview_port_base)
        if compose_cmd is None:
            return {"platform": ["Docker Compose is not available inside the platform backend container."]}
        logs_by_service: dict[str, list[str]] = {}
        try:
            services = self._compose_services(compose_cmd, compose_file, source_dir, project_name, env)
            if not services:
                services = list(self._inspect_containers_via_docker(project_name).keys())
            if not services:
                services = ["preview-app"]
            for service in services:
                result = subprocess.run(
                    [*compose_cmd, "-f", str(compose_file), "-p", project_name, "logs", "--tail", "120", service],
                    cwd=self._compose_workdir(source_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                )
                output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
                lines = [line for line in output.splitlines() if line.strip()]
                if lines:
                    logs_by_service[service] = lines
        finally:
            compose_file.unlink(missing_ok=True)
        return logs_by_service

    def inspect_containers(self, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[dict[str, str | None]]:
        try:
            compose_file = self._render_host_compose_file(source_dir)
        except FileNotFoundError:
            compose_file = None
        compose_cmd = self._compose_command()
        project_name = self.project_name(workspace_id)
        env = self._compose_env(proxy_port or self.settings.preview_port_base)
        if compose_cmd is None:
            return []
        service_map: dict[str, dict[str, str | None]] = {}
        try:
            if compose_file is not None:
                result = subprocess.run(
                    [*compose_cmd, "-f", str(compose_file), "-p", project_name, "ps", "-a", "--format", "json"],
                    cwd=self._compose_workdir(source_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                )
                service_map.update(self._parse_container_rows(result.stdout))
        finally:
            if compose_file is not None:
                compose_file.unlink(missing_ok=True)
        if not service_map:
            service_map.update(self._inspect_containers_via_docker(project_name))
        services = self._compose_services(compose_cmd, None, source_dir, project_name, env)
        if not services:
            services = list(service_map.keys()) or list(self.PREVIEW_SERVICES)
        containers: list[dict[str, str | None]] = []
        for service in services:
            containers.append(
                service_map.get(
                    service,
                    self._missing_container(service),
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
        env["PREVIEW_APP_PORT"] = str(proxy_port)
        env["PREVIEW_AUTH_ENDPOINT"] = "/api/auth/telegram"
        env["PREVIEW_API_BASE_URL"] = ""
        env["PREVIEW_DEFAULT_ROLE"] = "client"
        env["PREVIEW_POSTGRES_DB"] = "miniapp"
        env["PREVIEW_POSTGRES_USER"] = "miniapp"
        env["PREVIEW_POSTGRES_PASSWORD"] = "miniapp"
        return env

    def _host_source_dir(self, source_dir: Path) -> Path:
        try:
            relative = source_dir.relative_to(self.settings.data_dir)
        except ValueError:
            return source_dir
        return self.settings.host_data_dir / relative

    def _compose_workdir(self, source_dir: Path) -> Path:
        return source_dir if source_dir.exists() else self.settings.data_dir

    def _render_host_compose_file(self, source_dir: Path) -> Path:
        source_compose_file = source_dir / "docker" / "docker-compose.yml"
        if not source_dir.exists():
            raise FileNotFoundError(f"Preview source directory is missing: {source_dir}")
        if not source_compose_file.exists():
            raise FileNotFoundError(f"Preview compose file is missing: {source_compose_file}")

        host_source_dir = self._host_source_dir(source_dir)
        rendered = source_compose_file.read_text(encoding="utf-8")
        replacements = {
            "../miniapp:/app": f"{(host_source_dir / 'miniapp').as_posix()}:/app",
            "./nginx.conf:/etc/nginx/conf.d/default.conf:ro": (
                f"{(host_source_dir / 'docker' / 'nginx.conf').as_posix()}:/etc/nginx/conf.d/default.conf:ro"
            ),
        }
        for pattern, replacement in replacements.items():
            rendered = rendered.replace(pattern, replacement)

        with tempfile.NamedTemporaryFile("w", suffix="-preview-compose.yml", delete=False, encoding="utf-8") as handle:
            handle.write(rendered)
            return Path(handle.name)

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

    @classmethod
    def _missing_container(cls, service: str) -> dict[str, str | None]:
        return {
            "service": service,
            "name": None,
            "state": "missing",
            "status": "not started",
            "health": None,
            "exit_code": None,
            "published_port": None,
        }

    @classmethod
    def _missing_containers(cls) -> list[dict[str, str | None]]:
        return [cls._missing_container(service) for service in cls.PREVIEW_SERVICES]

    @staticmethod
    def _command_output_logs(stdout: str, stderr: str, *, tail_lines: int = 40) -> list[str]:
        merged = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part.strip())
        if not merged:
            return []
        lines = [line.rstrip() for line in merged.splitlines() if line.strip()]
        return lines[-tail_lines:]

    @classmethod
    def _parse_container_rows(cls, payload: str) -> dict[str, dict[str, str | None]]:
        service_map: dict[str, dict[str, str | None]] = {}
        raw = payload.strip()
        if not raw:
            return service_map
        parsed_rows: list[dict[str, object]] = []
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                parsed_rows = [item for item in decoded if isinstance(item, dict)]
            elif isinstance(decoded, dict):
                parsed_rows = [decoded]
        except json.JSONDecodeError:
            for line in raw.splitlines():
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
                "name": str(row.get("Name") or row.get("name") or row.get("Names") or "") or None,
                "state": str(row.get("State") or row.get("state") or "") or None,
                "status": str(row.get("Status") or row.get("status") or "") or None,
                "health": str(row.get("Health") or row.get("health") or "") or None,
                "exit_code": str(row.get("ExitCode") or row.get("exitCode") or "") or None,
                "published_port": cls._extract_published_port(row),
            }
        return service_map

    def _inspect_containers_via_docker(self, project_name: str) -> dict[str, dict[str, str | None]]:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=com.docker.compose.project={project_name}",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return {}
        if result.returncode != 0:
            return {}
        return self._parse_container_rows(result.stdout)

    def _compose_services(
        self,
        compose_cmd: list[str],
        compose_file: Path | None,
        source_dir: Path,
        project_name: str,
        env: dict[str, str],
    ) -> list[str]:
        command = [*compose_cmd]
        if compose_file is not None:
            command.extend(["-f", str(compose_file)])
        command.extend(["-p", project_name, "config", "--services"])
        result = subprocess.run(
            command,
            cwd=self._compose_workdir(source_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _extract_published_port(row: dict[str, object]) -> str | None:
        publishers = row.get("Publishers") or row.get("publishers")
        if isinstance(publishers, list):
            for publisher in publishers:
                if isinstance(publisher, dict):
                    published = publisher.get("PublishedPort") or publisher.get("publishedPort")
                    if published is not None:
                        return str(published)
        ports = row.get("Ports") or row.get("ports")
        if isinstance(ports, str):
            for token in ports.split(","):
                token = token.strip()
                if "->" not in token:
                    continue
                host_part = token.split("->", 1)[0].strip()
                if ":" in host_part:
                    candidate = host_part.rsplit(":", 1)[-1]
                else:
                    candidate = host_part
                digits = "".join(char for char in candidate if char.isdigit())
                if digits:
                    return digits
        return None
