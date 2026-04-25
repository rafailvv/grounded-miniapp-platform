from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
)
from app.services.generation_runtime_config import ACTIVE_GENERATION_QUERY_CONFIG, ACTIVE_GENERATION_TURN_STATE
from app.services.generation_tool_orchestrator import GenerationToolOrchestrator

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class GenerationRepairToolRuntime:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    @staticmethod
    def is_missing_file_context(content: str | None) -> bool:
        return str(content or "").startswith("FILE_MISSING:")

    @staticmethod
    def _missing_file_context(path: str) -> str:
        return (
            f"FILE_MISSING: {path}\n"
            "This approved target does not exist in the current draft workspace yet.\n"
            "Treat it as a create-from-scratch target and return a complete file body instead of requesting it again."
        )

    def approve_requested_targets(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        targets: list[str],
    ) -> list[str]:
        approved: list[str] = []
        for raw_target in targets:
            normalized = str(raw_target or "").strip().lstrip("./")
            if not normalized or normalized in approved:
                continue
            content = self.service.workspace_service.try_read_text_file(workspace_id, normalized, run_id=draft_run_id)
            if (
                content is not None
                or self.service._is_canonical_target_path(normalized)
                or normalized.startswith(("miniapp/tests/", "miniapp/app/generated/", "artifacts/"))
            ):
                approved.append(normalized)
        return approved

    def execute_tool_requests(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        workspace_tree: list[dict[str, str]],
        draft_source: Path,
        tool_requests: list[dict[str, Any]],
        fallback_targets: list[str],
        execute_checks: Callable[[list[str], str], tuple[Any, dict[str, Any]]],
        command_timeout_seconds: int,
    ) -> tuple[list[str], list[dict[str, object]], dict[str, str]]:
        additional_targets: list[str] = []
        tool_results: list[dict[str, object]] = []
        extra_contexts: dict[str, str] = {}

        def _execute_single_request(request_item: dict[str, Any]) -> tuple[list[str], dict[str, object] | None, dict[str, str]]:
            tool_name = str(request_item.get("tool") or "").strip().lower()
            targets = [
                str(item or "").strip().lstrip("./")
                for item in list(request_item.get("targets") or [])
                if str(item or "").strip()
            ]
            reason = str(request_item.get("reason") or "").strip()
            if tool_name == "read_files":
                approved = self.approve_requested_targets(
                    workspace_id=workspace_id,
                    draft_run_id=draft_run_id,
                    targets=targets,
                )
                missing_targets: list[str] = []
                loaded_contents: dict[str, str] = {}
                for path in approved:
                    content = self.service.workspace_service.try_read_text_file(workspace_id, path, run_id=draft_run_id)
                    if content is not None:
                        loaded_contents[path] = content
                        continue
                    missing_targets.append(path)
                read_contexts = dict(loaded_contents)
                for path in missing_targets:
                    read_contexts[path] = self._missing_file_context(path)
                return (
                    list(approved),
                    {
                        "tool": "read_files",
                        "targets": list(targets),
                        "approved_targets": list(approved),
                        "missing_targets": missing_targets,
                        "files": summarize_read_file_payloads(file_contents=loaded_contents),
                        "reason": reason,
                    },
                    read_contexts,
                )
            if tool_name == "list_files":
                return (
                    [],
                    {
                        **list_workspace_files(workspace_tree=workspace_tree, targets=targets),
                        "reason": reason,
                    },
                    {},
                )
            if tool_name == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                return (
                    [],
                    {
                        **search_workspace_files(
                            workspace_tree=workspace_tree,
                            read_text_file=lambda relative_path: self.service.workspace_service.try_read_text_file(
                                workspace_id,
                                relative_path,
                                run_id=draft_run_id,
                            ),
                            pattern=pattern,
                            targets=targets,
                        ),
                        "reason": reason,
                    },
                    {},
                )
            if tool_name == "run_command":
                return (
                    [],
                    {
                        **run_workspace_command(
                            draft_source=draft_source,
                            command=str(request_item.get("command") or "").strip(),
                            timeout_seconds=command_timeout_seconds,
                        ),
                        "reason": reason,
                    },
                    {},
                )
            if tool_name == "run_checks":
                requested_changed_files = list(targets or fallback_targets or ["miniapp"])
                mode = str(request_item.get("mode") or "exact").strip().lower() or "exact"
                execution, preview_details = execute_checks(requested_changed_files, mode)
                failed_checks = [
                    {
                        "name": result.name,
                        "details": result.details,
                        "command": result.command,
                        "logs": result.logs[-8:],
                    }
                    for result in execution.results
                    if result.status == "failed"
                ]
                tool_results.append(
                    {
                        "tool": "run_checks",
                        "mode": mode,
                        "targets": requested_changed_files,
                        "reason": reason,
                        "failed_checks": failed_checks,
                        "preview_logs": list((preview_details.get("logs") or [])[-8:]),
                    }
                )
            return ([], None, {})

        query_config = ACTIVE_GENERATION_QUERY_CONFIG.get()
        orchestrator = GenerationToolOrchestrator(
            max_concurrency=getattr(query_config, "max_tool_concurrency", 8),
        )
        executed_batches, orchestration_ms = orchestrator.execute(
            tool_requests=tool_requests,
            run_request=_execute_single_request,
        )
        turn_state = ACTIVE_GENERATION_TURN_STATE.get()
        if turn_state is not None:
            turn_state.tool_orchestration_ms += orchestration_ms
        for batch_targets, batch_result, batch_contexts in executed_batches:
            for path in batch_targets:
                if path not in additional_targets:
                    additional_targets.append(path)
            if batch_result is not None:
                tool_results.append(batch_result)
            extra_contexts.update(batch_contexts)
        return additional_targets, tool_results, extra_contexts
