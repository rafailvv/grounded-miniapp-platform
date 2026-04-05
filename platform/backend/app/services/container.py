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
    ArtifactRecorder,
    CompactionService,
    ContextBudgetManager,
    DiminishingReturnsService,
    PromptStateManager,
    ProjectMemoryService,
    SessionCostLedger,
    SessionEngine,
    TaskRouter,
    TelemetryLogBridge,
)
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
        self.openrouter_client = OpenRouterClient(self.settings, self.workspace_log_service)
        self.context_budget_manager = ContextBudgetManager()
        self.prompt_state_manager = PromptStateManager()
        self.task_router = TaskRouter()
        self.compaction_service = CompactionService()
        self.artifact_recorder = ArtifactRecorder(self.store)
        self.telemetry_log_bridge = TelemetryLogBridge(self.workspace_log_service)
        self.session_cost_ledger = SessionCostLedger(self.store)
        self.project_memory_service = ProjectMemoryService(self.store)
        self.diminishing_returns_service = DiminishingReturnsService(self.store)
        self.session_engine = SessionEngine(
            self.artifact_recorder,
            self.task_router,
            self.context_budget_manager,
            self.prompt_state_manager,
            self.compaction_service,
            self.telemetry_log_bridge,
            self.session_cost_ledger,
            self.project_memory_service,
            self.diminishing_returns_service,
        )
        self.context_pack_builder = ContextPackBuilder(
            self.code_index_service,
            self.workspace_service,
            self.context_budget_manager,
            self.prompt_state_manager,
        )
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
            self.session_engine,
            self.task_router,
            self.context_budget_manager,
            self.prompt_state_manager,
            self.compaction_service,
            self.artifact_recorder,
        )
        self.fix_orchestrator = FixOrchestrator(
            self.store,
            self.workspace_service,
            self.check_runner,
            self.preview_service,
            self.runtime_manager,
            self.openrouter_client,
            self.workspace_log_service,
            self.session_engine,
            self.task_router,
            self.context_budget_manager,
            self.prompt_state_manager,
            self.compaction_service,
            self.artifact_recorder,
        )
        self.run_service = RunService(
            self.store,
            self.workspace_service,
            self.generation_service,
            self.fix_orchestrator,
            self.preview_service,
            self.openrouter_client,
            self.workspace_log_service,
            self.session_engine,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    return ServiceContainer(settings)
