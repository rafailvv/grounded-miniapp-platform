from __future__ import annotations

from pathlib import Path

from app.core.config import Settings, get_settings
from app.repositories.state_store import StateStore
from app.repositories.platform_db import PlatformDb
from app.services.rpc_event_hub import RpcEventHub
from app.services.thread_service import ThreadService
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.export_service import ExportService
from app.services.engine import (
    ContextBudgetManager,
    PromptStateManager,
)
from app.modules.miniapp_agent_loop.agent_tool_call_loop import AgentToolCallLoop
from app.modules.miniapp_agent_loop.context_builder import AgentContextBuilder
from app.modules.workspace_code_agent_runtime import WorkspaceCodeAgentRuntime
from app.services.patch_service import PatchService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.run_service import RunService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.service import WorkspaceService
from app.validators.suite import ValidationSuite
from app.ai.openai_client import OpenAIClient
from app.services.exec_policy_service import ExecPolicyService
from app.services.exec_runtime_service import ExecRuntimeService
from app.services.workbench_service import WorkbenchService


class ServiceContainer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = StateStore(self.settings.data_dir / "platform-state.json")
        self.platform_db = PlatformDb(self.settings.data_dir / "platform.db")
        self.rpc_event_hub = RpcEventHub()
        self.store.shard_large_runtime_payloads()
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
        self.agent_context_builder = AgentContextBuilder(store=self.store, workspace_service=self.workspace_service)
        self.agent_tool_call_loop = AgentToolCallLoop(context_builder=self.agent_context_builder)
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
            agent_tool_call_loop=self.agent_tool_call_loop,
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
        self.exec_policy_service = ExecPolicyService()
        self.exec_runtime_service = ExecRuntimeService(
            workspace_service=self.workspace_service,
            platform_db=self.platform_db,
            event_hub=self.rpc_event_hub,
            store=self.store,
        )
        self.workbench_service = WorkbenchService(
            settings=self.settings,
            store=self.store,
            workspace_service=self.workspace_service,
            run_service=self.run_service,
            openai_client=self.openai_client,
            exec_policy_service=self.exec_policy_service,
        )
        self.thread_service = ThreadService(
            self.platform_db,
            self.run_service,
            self.workspace_service,
            self.rpc_event_hub,
            store=self.store,
            exec_policy_service=self.exec_policy_service,
            exec_runtime_service=self.exec_runtime_service,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    return ServiceContainer(settings)
