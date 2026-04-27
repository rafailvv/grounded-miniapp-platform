from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, get_settings
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.export_service import ExportService
from app.services.engine import (
    ContextBudgetManager,
    PromptStateManager,
)
from app.modules.miniapp_agent_loop.engine import WorkspaceLoopEngine
from app.modules.workspace_code_agent_runtime import WorkspaceCodeAgentRuntime
from app.services.patch_service import PatchService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.run_service import RunService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService
from app.validators.suite import ValidationSuite
from app.ai.openai_client import OpenAIClient


class ServiceContainer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = StateStore(self.settings.data_dir / "platform-state.json")
        self.store.migrate_persisted_runtime_state()
        self.workspace_log_service = WorkspaceLogService(self.settings)
        self.workspace_service = WorkspaceService(self.settings, self.store, self.workspace_log_service)
        self.code_index_service = CodeIndexService(self.settings, self.store)
        self.workspace_service.attach_code_index_service(self.code_index_service)
        self.document_service = DocumentIntelligenceService(self.settings, self.store, self.code_index_service)
        self.patch_service = PatchService(self.workspace_service)
        self.runtime_manager = PreviewRuntimeManager(self.settings)
        self.preview_service = PreviewService(
            self.settings,
            self.store,
            self.workspace_service,
            self.runtime_manager,
            self.workspace_log_service,
        )
        self.workspace_loop_engine = WorkspaceLoopEngine(
            self.store,
            self.workspace_service,
            self.workspace_log_service,
        )
        self.validation_suite = ValidationSuite()
        self.check_runner = CheckRunner(self.validation_suite, self.preview_service)
        self.openai_client = OpenAIClient(self.settings, self.workspace_log_service)
        self.context_budget_manager = ContextBudgetManager()
        self.prompt_state_manager = PromptStateManager()
        self.context_pack_builder = ContextPackBuilder(
            self.code_index_service,
            self.workspace_service,
            self.context_budget_manager,
            self.prompt_state_manager,
        )
        self.workspace_code_agent_runtime = WorkspaceCodeAgentRuntime(
            store=self.store,
            workspace_service=self.workspace_service,
            check_runner=self.check_runner,
            preview_service=self.preview_service,
            runtime_manager=self.runtime_manager,
            openai_client=self.openai_client,
            workspace_log_service=self.workspace_log_service,
            workspace_loop_engine=self.workspace_loop_engine,
            context_pack_builder=self.context_pack_builder,
        )
        self.run_service = RunService(
            self.store,
            self.workspace_service,
            self.workspace_code_agent_runtime,
            self.preview_service,
            self.check_runner,
            self.openai_client,
            self.workspace_log_service,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    return ServiceContainer(settings)
