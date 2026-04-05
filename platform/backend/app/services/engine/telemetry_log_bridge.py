from __future__ import annotations

from typing import Any

from app.services.workspace_log_service import WorkspaceLogService


class TelemetryLogBridge:
    def __init__(self, workspace_log_service: WorkspaceLogService) -> None:
        self.workspace_log_service = workspace_log_service

    def phase(self, workspace_id: str, phase: str, message: str, *, payload: dict[str, Any] | None = None) -> None:
        self.workspace_log_service.append(
            workspace_id,
            source=f"engine.{phase}",
            message=message,
            payload=payload or {},
        )
