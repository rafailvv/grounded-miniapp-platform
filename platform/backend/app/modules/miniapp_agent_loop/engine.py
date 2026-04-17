from __future__ import annotations

from pathlib import Path

from app.models.common import GenerationMode
from app.models.domain import JobRecord
from app.repositories.state_store import StateStore
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService
from app.modules.miniapp_agent_loop.context_builder import WorkspaceLoopContextBuilder
from app.modules.miniapp_agent_loop.turn_runner import WorkspaceLoopTurnRunner
from app.modules.miniapp_agent_loop.types import WorkspaceLoopCallbacks, WorkspaceLoopResult


class WorkspaceLoopEngine:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.workspace_log_service = workspace_log_service
        self.context_builder = WorkspaceLoopContextBuilder(store=store, workspace_service=workspace_service)
        self.turn_runner = WorkspaceLoopTurnRunner(context_builder=self.context_builder)

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        max_attempts: int,
        initial_operations,
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: WorkspaceLoopCallbacks,
    ) -> WorkspaceLoopResult:
        return self.turn_runner.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            max_attempts=max_attempts,
            initial_operations=initial_operations,
            initial_assistant_message=initial_assistant_message,
            initial_files_read=initial_files_read,
            initial_changed_files=initial_changed_files,
            callbacks=callbacks,
        )

