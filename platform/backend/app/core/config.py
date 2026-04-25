from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.services.agent_runtime_config import TimeoutProfile


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
    preview_runtime_mode: str = "auto"
    preview_port_base: int = 16000
    preview_start_timeout_sec: int = 120
    openrouter_app_name: str = "Grounded Mini-App Platform"
    openrouter_site_url: str = "http://localhost:5173"


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


def get_settings(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    preview_base_url: str = "http://localhost:8000",
) -> Settings:
    root = repo_root or Path(__file__).resolve().parents[4]
    _load_repo_env(root / ".env")
    timeout_profile = TimeoutProfile.from_env()
    preview_base_url = os.getenv("PREVIEW_BASE_URL", preview_base_url)
    resolved_data_dir = data_dir or Path(os.getenv("PLATFORM_DATA_DIR", str(root / "data")))
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
        preview_runtime_mode=os.getenv("PREVIEW_RUNTIME_MODE", "auto"),
        preview_port_base=int(os.getenv("PREVIEW_PORT_BASE", "16000")),
        preview_start_timeout_sec=timeout_profile.preview_start_sec,
        openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "Grounded Mini-App Platform"),
        openrouter_site_url=os.getenv("OPENROUTER_SITE_URL", "http://localhost:5173"),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    return settings
