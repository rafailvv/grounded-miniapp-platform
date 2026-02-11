from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, get_settings
from app.repositories.state_store import StateStore
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.export_service import ExportService
from app.services.generation_service import GenerationService
from app.services.patch_service import PatchService
from app.services.preview_service import PreviewService
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.workspace_service import WorkspaceService
from app.validators.suite import ValidationSuite
from app.ai.openrouter_client import OpenRouterClient


class ServiceContainer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = StateStore(self.settings.data_dir / "platform-state.json")
        self.workspace_service = WorkspaceService(self.settings, self.store)
        self.document_service = DocumentIntelligenceService(self.settings, self.store)
        self.patch_service = PatchService(self.workspace_service)
        self.runtime_manager = PreviewRuntimeManager(self.settings)
        self.preview_service = PreviewService(self.settings, self.store, self.workspace_service, self.runtime_manager)
        self.validation_suite = ValidationSuite()
        self.openrouter_client = OpenRouterClient(self.settings)
        self.generation_service = GenerationService(
            self.store,
            self.workspace_service,
            self.document_service,
            self.patch_service,
            self.preview_service,
            self.validation_suite,
            self.openrouter_client,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    return ServiceContainer(settings)
