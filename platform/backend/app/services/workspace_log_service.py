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

    def log_path(self, workspace_id: str) -> Path:
        logs_dir = self.settings.workspaces_dir / workspace_id / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / "platform.log"

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
