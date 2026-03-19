from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, get_settings
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.export_service import ExportService
from app.services.fix_orchestrator import FixOrchestrator
from app.services.generation_service import GenerationService
from app.services.patch_service import PatchService
from app.services.preview_service import PreviewService
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.run_service import RunService
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService
from app.validators.suite import ValidationSuite
from app.ai.openrouter_client import OpenRouterClient


class ServiceContainer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = StateStore(self.settings.data_dir / "platform-state.json")
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
        self.validation_suite = ValidationSuite()
        self.check_runner = CheckRunner(self.validation_suite, self.preview_service)
        self.openrouter_client = OpenRouterClient(self.settings)
        self.context_pack_builder = ContextPackBuilder(self.code_index_service, self.workspace_service)
        self.generation_service = GenerationService(
            self.store,
            self.workspace_service,
            self.document_service,
            self.code_index_service,
            self.context_pack_builder,
            self.patch_service,
            self.preview_service,
            self.check_runner,
            self.validation_suite,
            self.openrouter_client,
            self.workspace_log_service,
        )
        self.fix_orchestrator = FixOrchestrator(
            self.store,
            self.workspace_service,
            self.check_runner,
            self.preview_service,
            self.runtime_manager,
            self.openrouter_client,
            self.workspace_log_service,
        )
        self.run_service = RunService(
            self.store,
            self.workspace_service,
            self.generation_service,
            self.fix_orchestrator,
            self.preview_service,
            self.openrouter_client,
            self.workspace_log_service,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    return ServiceContainer(settings)
