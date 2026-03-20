from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import Settings


class WorkspaceLogService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()

    def _logs_dir(self, workspace_id: str) -> Path:
        logs_dir = self.settings.workspaces_dir / workspace_id / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir

    def log_path(self, workspace_id: str) -> Path:
        return self._logs_dir(workspace_id) / "platform.log"

    def api_log_path(self, workspace_id: str) -> Path:
        return self._logs_dir(workspace_id) / "api.log"

    def append(
        self,
        workspace_id: str,
        *,
        source: str,
        message: str,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "source": source,
            "message": message,
            "payload": payload or {},
        }
        line = json.dumps(entry, ensure_ascii=True)
        log_path = self.log_path(workspace_id)
        with self._lock:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")

    def append_api(
        self,
        workspace_id: str,
        *,
        source: str,
        message: str,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "source": source,
            "message": message,
            "payload": payload or {},
        }
        line = json.dumps(entry, ensure_ascii=True)
        log_path = self.api_log_path(workspace_id)
        with self._lock:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")

    def read_lines(self, workspace_id: str, *, kind: str = "platform", limit: int = 400) -> list[str]:
        log_path = self.log_path(workspace_id) if kind == "platform" else self.api_log_path(workspace_id)
        if not log_path.exists():
            return []
        with self._lock:
            with log_path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
        return [line.rstrip("\n") for line in lines[-limit:]]
