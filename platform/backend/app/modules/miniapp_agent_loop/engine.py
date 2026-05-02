from __future__ import annotations

from pathlib import Path

from app.models.common import GenerationMode
from app.models.domain import JobRecord
from app.repositories.state_store import StateStore
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService
from app.modules.miniapp_agent_loop.agent_query_loop import AgentQueryLoop
from app.modules.miniapp_agent_loop.context_builder import AgentContextBuilder
from app.modules.miniapp_agent_loop.types import AgentLoopCallbacks, AgentLoopResult


class AgentLoopEngine:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.workspace_log_service = workspace_log_service
        self.context_builder = AgentContextBuilder(store=store, workspace_service=workspace_service)
        self.query_loop = AgentQueryLoop(context_builder=self.context_builder)

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        initial_draft_actions,
        initial_assistant_message: str,
        initial_files_read: list[str],
        initial_changed_files: list[str],
        callbacks: AgentLoopCallbacks,
    ) -> AgentLoopResult:
        return self.query_loop.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            initial_draft_actions=initial_draft_actions,
            initial_assistant_message=initial_assistant_message,
            initial_files_read=initial_files_read,
            initial_changed_files=initial_changed_files,
            callbacks=callbacks,
        )
