from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.services.agent_runtime_config import TimeoutProfile

_DOTENV_LOADED_VALUES: dict[str, str] = {}


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    data_dir: Path
    host_data_dir: Path
    workspaces_dir: Path
    exports_dir: Path
    runtime_dir: Path
    template_dir: Path
    preview_base_url: str = "http://localhost:8000"
    preview_runtime_mode: str = "docker"
    preview_port_base: int = 16000
    preview_start_timeout_sec: int = 120


def _load_repo_env(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    try:
        raw_lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        parsed = value.strip()
        if parsed and parsed[0] == parsed[-1] and parsed[0] in {'"', "'"}:
            parsed = parsed[1:-1]
        os.environ[key] = parsed
        _DOTENV_LOADED_VALUES[key] = parsed


def _env_was_loaded_from_dotenv(key: str) -> bool:
    return key in _DOTENV_LOADED_VALUES and os.environ.get(key) == _DOTENV_LOADED_VALUES[key]


def get_settings(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    preview_base_url: str = "http://localhost:8000",
) -> Settings:
    root = repo_root or Path(__file__).resolve().parents[4]
    _load_repo_env(root / ".env")
    explicit_host_data_dir = os.environ.get("PLATFORM_HOST_DATA_DIR")
    host_data_dir_from_process_env = explicit_host_data_dir is not None and not _env_was_loaded_from_dotenv("PLATFORM_HOST_DATA_DIR")
    timeout_profile = TimeoutProfile.from_env()
    preview_base_url = os.getenv("PREVIEW_BASE_URL", preview_base_url)
    resolved_data_dir = data_dir or Path(os.getenv("PLATFORM_DATA_DIR", str(root / "data")))
    if data_dir is not None and not host_data_dir_from_process_env:
        resolved_host_data_dir = resolved_data_dir
    else:
        resolved_host_data_dir = Path(os.getenv("PLATFORM_HOST_DATA_DIR", str(resolved_data_dir)))
    settings = Settings(
        repo_root=root,
        data_dir=resolved_data_dir,
        host_data_dir=resolved_host_data_dir,
        workspaces_dir=resolved_data_dir / "workspaces",
        exports_dir=resolved_data_dir / "exports",
        runtime_dir=root / "runtime",
        template_dir=root / "runtime" / "templates" / "base-miniapp",
        preview_base_url=preview_base_url,
        preview_runtime_mode=os.getenv("PREVIEW_RUNTIME_MODE", "docker"),
        preview_port_base=int(os.getenv("PREVIEW_PORT_BASE", "16000")),
        preview_start_timeout_sec=timeout_profile.preview_start_sec,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    return settings
