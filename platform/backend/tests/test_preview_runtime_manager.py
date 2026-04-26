from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.services.workspace.runtime_manager import PreviewRuntimeManager


ORIGINAL_RESET = PreviewRuntimeManager.reset


def test_direct_preview_cleanup_removes_project_containers_networks_and_volumes(monkeypatch) -> None:
    runtime = object.__new__(PreviewRuntimeManager)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[:4] == ["docker", "ps", "-aq", "--filter"] and "label=" in command[4]:
            return subprocess.CompletedProcess(command, 0, stdout="labelled-container\n", stderr="")
        if command[:4] == ["docker", "ps", "-aq", "--filter"] and "name=" in command[4]:
            return subprocess.CompletedProcess(command, 0, stdout="named-container\nlabelled-container\n", stderr="")
        if command[:3] == ["docker", "network", "ls"]:
            return subprocess.CompletedProcess(command, 0, stdout="network-id\n", stderr="")
        if command[:3] == ["docker", "volume", "ls"]:
            return subprocess.CompletedProcess(command, 0, stdout="volume-id\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    logs = runtime._remove_project_resources("grounded_preview_ws_example")

    assert ["docker", "rm", "-f", "labelled-container", "named-container"] in calls
    assert ["docker", "network", "rm", "network-id"] in calls
    assert ["docker", "volume", "rm", "-f", "volume-id"] in calls
    assert any("removed 2 stale preview container" in line for line in logs)


def test_reset_keeps_recovery_going_when_compose_down_fails_but_direct_cleanup_succeeds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(preview_port_base=16000)
    runtime = PreviewRuntimeManager(settings)  # type: ignore[arg-type]
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(runtime, "_compose_command", lambda: ["docker", "compose"])
    monkeypatch.setattr(runtime, "_render_host_compose_file", lambda _source_dir: compose_file)
    monkeypatch.setattr(runtime, "_compose_workdir", lambda _source_dir: tmp_path)
    monkeypatch.setattr(runtime, "_remove_project_resources", lambda _project_name: ["[runtime] direct cleanup succeeded."])

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="compose down failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    logs = ORIGINAL_RESET(runtime, "ws_example", tmp_path, 16042)

    assert "[runtime] direct cleanup succeeded." in logs
    assert "[runtime] compose down failed, but direct stale-resource cleanup completed." in logs
