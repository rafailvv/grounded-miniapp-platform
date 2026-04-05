from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from app.services.runtime_manager import PreviewRuntimeManager


_PREVIEW_PROJECT_PREFIX = "grounded_preview_"
_PREVIEW_COMPOSE_SUFFIX = "-preview-compose.yml"


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None


def _docker_available() -> bool:
    result = _run_command(["docker", "version", "--format", "{{.Server.Version}}"])
    return bool(result and result.returncode == 0)


def _inspect_labelled_resource_ids(resource: str) -> list[str]:
    result = _run_command(
        ["docker", resource, "ls", "-q", "--filter", "label=com.docker.compose.project"]
    )
    if not result or result.returncode != 0:
        return []
    resource_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    matched: list[str] = []
    for resource_id in resource_ids:
        inspect_result = _run_command(
            ["docker", resource, "inspect", resource_id, "--format", "{{json .Labels}}"]
        )
        if not inspect_result or inspect_result.returncode != 0:
            continue
        labels = inspect_result.stdout
        if _PREVIEW_PROJECT_PREFIX in labels:
            matched.append(resource_id)
    return matched


def _remove_preview_containers() -> None:
    result = _run_command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project"])
    if not result or result.returncode != 0:
        return
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    stale_ids: list[str] = []
    for container_id in container_ids:
        inspect_result = _run_command(
            ["docker", "inspect", container_id, "--format", "{{json .Config.Labels}}"]
        )
        if not inspect_result or inspect_result.returncode != 0:
            continue
        if _PREVIEW_PROJECT_PREFIX in inspect_result.stdout:
            stale_ids.append(container_id)
    if stale_ids:
        _run_command(["docker", "rm", "-f", *stale_ids])


def _remove_preview_networks() -> None:
    network_ids = _inspect_labelled_resource_ids("network")
    if network_ids:
        _run_command(["docker", "network", "rm", *network_ids])


def _remove_preview_volumes() -> None:
    volume_ids = _inspect_labelled_resource_ids("volume")
    if volume_ids:
        _run_command(["docker", "volume", "rm", "-f", *volume_ids])


def _remove_preview_compose_files() -> None:
    temp_dir = Path(tempfile.gettempdir())
    for path in temp_dir.glob(f"*{_PREVIEW_COMPOSE_SUFFIX}"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def _cleanup_test_preview_runtime() -> None:
    _remove_preview_compose_files()
    if not _docker_available():
        return
    _remove_preview_containers()
    _remove_preview_networks()
    _remove_preview_volumes()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_preview_runtime_session() -> None:
    _cleanup_test_preview_runtime()
    yield
    _cleanup_test_preview_runtime()


@pytest.fixture(autouse=True)
def _stub_preview_runtime_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    def _start(self: PreviewRuntimeManager, workspace_id: str, source_dir: Path, proxy_port: int) -> tuple[str, list[str]]:
        del source_dir
        return (
            self.project_name(workspace_id),
            [f"[runtime:test] stubbed preview start on port {proxy_port}"],
        )

    def _rebuild(self: PreviewRuntimeManager, workspace_id: str, source_dir: Path, proxy_port: int) -> list[str]:
        del source_dir
        return [f"[runtime:test] stubbed preview rebuild for {workspace_id} on port {proxy_port}"]

    def _reset(self: PreviewRuntimeManager, workspace_id: str, source_dir: Path, proxy_port: int | None) -> list[str]:
        del workspace_id, source_dir, proxy_port
        return ["[runtime:test] stubbed preview reset."]

    monkeypatch.setattr(PreviewRuntimeManager, "start", _start)
    monkeypatch.setattr(PreviewRuntimeManager, "rebuild", _rebuild)
    monkeypatch.setattr(PreviewRuntimeManager, "reset", _reset)
    yield


@pytest.fixture(autouse=True)
def _cleanup_preview_runtime_per_test() -> None:
    _cleanup_test_preview_runtime()
    yield
    _cleanup_test_preview_runtime()
