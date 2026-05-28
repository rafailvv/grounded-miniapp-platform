from __future__ import annotations

import os
from pathlib import Path

from app.core.config import Settings, get_settings
from app.repositories.state_store import StateStore
from app.repositories.platform_db import PlatformDb
from app.services.rpc_event_hub import RpcEventHub
from app.services.thread_service import ThreadService
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.context_manager import ContextManagerService
from app.services.lsp_context import LspContextService
from app.services.lsp_server_manager import LspServerManager
from app.services.worker_sessions import WorkerSessionService
from app.services.draft_isolation import DraftIsolationService
from app.services.guardian_gate import GuardianGateService
from app.services.browser_replay_proof import BrowserReplayProofService
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
from app.services.doctor_service import DoctorService
from app.services.exec_runtime_service import ExecRuntimeService
from app.services.sandbox_service import SandboxService
from app.services.hook_policy_service import HookPolicyService
from app.services.event_journal import EventJournalService
from app.services.output_artifact_service import OutputArtifactService
from app.services.model_manager import ModelManagerService
from app.services.background_task_service import BackgroundTaskService
from app.services.pr_babysitter import PrBabysitterService
from app.services.run_compaction import RunCompactionService
from app.services.run_protocol import RunProtocolService
from app.services.session_protocol import SessionProtocolReducer
from app.services.repair_cases import RepairCaseService
from app.services.prompt_contract_compiler import PromptContractCompilerService
from app.services.workbench_service import WorkbenchService
from app.services.starter_workspace_service import StarterWorkspaceService


class ServiceContainer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = StateStore(self.settings.data_dir / "platform-state.json")
        self.platform_db = PlatformDb(self.settings.data_dir / "platform.db")
        self.event_journal_service = EventJournalService(self.platform_db)
        self.output_artifact_service = OutputArtifactService(self.store, event_journal_service=self.event_journal_service)
        self.run_protocol_service = RunProtocolService(self.platform_db, self.store, event_journal_service=self.event_journal_service)
        self.session_protocol_reducer = SessionProtocolReducer(
            db=self.platform_db,
            store=self.store,
            event_journal_service=self.event_journal_service,
            run_protocol_service=self.run_protocol_service,
        )
        self.run_compaction_service = RunCompactionService(self.store, self.run_protocol_service)
        self.repair_case_service = RepairCaseService(self.store, event_journal_service=self.event_journal_service)
        self.rpc_event_hub = RpcEventHub()
        self.store.shard_large_runtime_payloads()
        self.workspace_log_service = WorkspaceLogService(self.settings)
        self.model_manager_service = ModelManagerService(settings=self.settings, store=self.store)
        self.sandbox_service = SandboxService()
        self.hook_policy_service = HookPolicyService(self.store, self.settings.runtime_dir / "policies" / "agent_hooks.json")
        self.workspace_service = WorkspaceService(self.settings, self.store, self.workspace_log_service, sandbox_service=self.sandbox_service)
        self.draft_isolation_service = DraftIsolationService(
            store=self.store,
            workspace_service=self.workspace_service,
            event_journal_service=self.event_journal_service,
        )
        self.guardian_gate_service = GuardianGateService(
            store=self.store,
            workspace_service=self.workspace_service,
            event_journal_service=self.event_journal_service,
        )
        self.browser_replay_proof_service = BrowserReplayProofService(self.store, event_journal_service=self.event_journal_service)
        self.workspace_service.attach_draft_isolation_service(self.draft_isolation_service)
        self.pr_babysitter_service = PrBabysitterService(store=self.store, workspace_service=self.workspace_service)
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
        self.lsp_server_manager = LspServerManager()
        self.lsp_context_service = LspContextService(
            store=self.store,
            workspace_service=self.workspace_service,
            server_manager=self.lsp_server_manager,
            event_journal_service=self.event_journal_service,
        )
        self.agent_context_builder = AgentContextBuilder(store=self.store, workspace_service=self.workspace_service)
        self.agent_tool_call_loop = AgentToolCallLoop(context_builder=self.agent_context_builder, lsp_context_service=self.lsp_context_service)
        self.validation_suite = ValidationSuite()
        self.check_runner = CheckRunner(self.validation_suite, self.preview_service)
        self.openai_client = OpenAIClient(self.settings, self.workspace_log_service, model_manager=self.model_manager_service)
        self.context_budget_manager = ContextBudgetManager()
        self.prompt_state_manager = PromptStateManager()
        self.context_manager_service = ContextManagerService(
            self.store,
            budget_manager=self.context_budget_manager,
            event_journal_service=self.event_journal_service,
        )
        self.worker_session_service = WorkerSessionService(self.store, event_journal_service=self.event_journal_service)
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
            platform_db=self.platform_db,
            run_protocol_service=self.run_protocol_service,
            run_compaction_service=self.run_compaction_service,
            context_manager_service=self.context_manager_service,
            lsp_context_service=self.lsp_context_service,
            worker_session_service=self.worker_session_service,
            event_journal_service=self.event_journal_service,
            hook_policy_service=self.hook_policy_service,
            output_artifact_service=self.output_artifact_service,
        )
        self.run_service = RunService(
            self.store,
            self.workspace_service,
            self.workspace_code_agent_runtime,
            self.preview_service,
            self.check_runner,
            self.openai_client,
            self.workspace_log_service,
            run_protocol_service=self.run_protocol_service,
            event_journal_service=self.event_journal_service,
            prompt_contract_compiler_service=PromptContractCompilerService(
                store=self.store,
                openai_client=self.openai_client,
                event_journal_service=self.event_journal_service,
            ),
        )
        self.run_service.attach_guardian_gate_service(self.guardian_gate_service)
        self.exec_policy_service = ExecPolicyService(self.settings.runtime_dir / "policies" / "agent_exec_policy.json", sandbox_service=self.sandbox_service)
        self.doctor_service = DoctorService(
            settings=self.settings,
            store=self.store,
            openai_client=self.openai_client,
            exec_policy_service=self.exec_policy_service,
            event_journal_service=self.event_journal_service,
            run_protocol_service=self.run_protocol_service,
        )
        self.exec_runtime_service = ExecRuntimeService(
            workspace_service=self.workspace_service,
            platform_db=self.platform_db,
            event_hub=self.rpc_event_hub,
            store=self.store,
            event_journal_service=self.event_journal_service,
            sandbox_service=self.sandbox_service,
            output_artifact_service=self.output_artifact_service,
        )
        self.background_task_service = BackgroundTaskService(
            store=self.store,
            workspace_service=self.workspace_service,
            run_service=self.run_service,
            preview_service=self.preview_service,
            check_runner=self.check_runner,
            pr_babysitter_service=self.pr_babysitter_service,
            lsp_context_service=self.lsp_context_service,
        )
        self.run_service.attach_background_task_service(self.background_task_service)
        self.workbench_service = WorkbenchService(
            settings=self.settings,
            store=self.store,
            workspace_service=self.workspace_service,
            run_service=self.run_service,
            openai_client=self.openai_client,
            exec_policy_service=self.exec_policy_service,
            platform_db=self.platform_db,
            run_protocol_service=self.run_protocol_service,
            run_compaction_service=self.run_compaction_service,
            background_task_service=self.background_task_service,
            repair_case_service=self.repair_case_service,
            context_manager_service=self.context_manager_service,
            worker_session_service=self.worker_session_service,
            draft_isolation_service=self.draft_isolation_service,
            guardian_gate_service=self.guardian_gate_service,
            lsp_context_service=self.lsp_context_service,
            event_journal_service=self.event_journal_service,
            output_artifact_service=self.output_artifact_service,
            pr_babysitter_service=self.pr_babysitter_service,
            browser_replay_proof_service=self.browser_replay_proof_service,
            doctor_service=self.doctor_service,
        )
        self.thread_service = ThreadService(
            self.platform_db,
            self.run_service,
            self.workspace_service,
            self.rpc_event_hub,
            store=self.store,
            exec_policy_service=self.exec_policy_service,
            exec_runtime_service=self.exec_runtime_service,
            event_journal_service=self.event_journal_service,
        )
        self.export_service = ExportService(self.settings, self.store, self.workspace_service)
        self.starter_workspace_service = StarterWorkspaceService(
            settings=self.settings,
            store=self.store,
            workspace_service=self.workspace_service,
            workspace_log_service=self.workspace_log_service,
        )


def build_container(*, repo_root: Path | None = None, data_dir: Path | None = None) -> ServiceContainer:
    settings = get_settings(repo_root=repo_root, data_dir=data_dir)
    container = ServiceContainer(settings)
    bootstrap_flag = os.getenv("PLATFORM_BOOTSTRAP_STARTER_WORKSPACE", "")
    disabled = bootstrap_flag.lower() in {"0", "false", "no", "off"}
    bootstrap_starter = bootstrap_flag == "1" or (data_dir is None and not disabled)
    if bootstrap_starter:
        container.starter_workspace_service.ensure_default_workspace()
    return container
