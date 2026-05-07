from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.models.domain import CheckExecutionRecord
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.tool_router import ToolRouter, ToolRouterContext
from app.services.workspace.service import WorkspaceService


class AgentToolExecutor:
    """Compatibility facade for model-facing tools.

    Runtime execution lives in ToolRouter; this class preserves the older
    WorkspaceCodeAgentRuntime dependency shape while keeping the router as the
    single model-facing boundary.
    """

    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        file_state_cache: AgentFileStateCache | None = None,
        process_manager: AgentProcessManager | None = None,
        read_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.workspace_service = workspace_service
        self.file_state_cache = file_state_cache or AgentFileStateCache()
        self.process_manager = process_manager or AgentProcessManager(sandbox_service=workspace_service.sandbox_service)
        self.read_artifact = read_artifact

    def execute(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_calls: list[dict[str, Any]],
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
        append_activity: Callable[[str, str, dict[str, Any] | None], None] | None = None,
        append_batch_summary: Callable[[dict[str, object]], None] | None = None,
        hook_manager: AgentHookManager | None = None,
        mode: str = "default",
        forced_allowed_tools: set[str] | None = None,
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        router = ToolRouter(
            ToolRouterContext(
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
                workspace_service=self.workspace_service,
                execute_checks=execute_checks,
                file_state_cache=self.file_state_cache,
                process_manager=self.process_manager,
                read_artifact=self.read_artifact,
                append_activity=append_activity,
                append_batch_summary=append_batch_summary,
                hook_manager=hook_manager,
                mode=mode,
                forced_allowed_tools=forced_allowed_tools,
            )
        )
        result = router.route_batch(tool_calls)
        return result.loaded_context, result.model_results
