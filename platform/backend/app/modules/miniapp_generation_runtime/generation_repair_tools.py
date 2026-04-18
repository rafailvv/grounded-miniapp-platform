from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
)

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class GenerationRepairToolRuntime:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

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
        execute_checks: Callable[[list[str]], tuple[Any, dict[str, Any]]],
        command_timeout_seconds: int,
    ) -> tuple[list[str], list[dict[str, object]], dict[str, str]]:
        additional_targets: list[str] = []
        tool_results: list[dict[str, object]] = []
        extra_contexts: dict[str, str] = {}
        for request_item in tool_requests:
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
                for path in approved:
                    if path not in additional_targets:
                        additional_targets.append(path)
                    content = self.service.workspace_service.try_read_text_file(workspace_id, path, run_id=draft_run_id)
                    if content is not None:
                        extra_contexts[path] = content
                tool_results.append(
                    {
                        "tool": "read_files",
                        "targets": list(targets),
                        "approved_targets": list(approved),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "list_files":
                tool_results.append(
                    {
                        **list_workspace_files(workspace_tree=workspace_tree, targets=targets),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                tool_results.append(
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
                    }
                )
                continue
            if tool_name == "run_command":
                tool_results.append(
                    {
                        **run_workspace_command(
                            draft_source=draft_source,
                            command=str(request_item.get("command") or "").strip(),
                            timeout_seconds=command_timeout_seconds,
                        ),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "run_checks":
                requested_changed_files = list(targets or fallback_targets or ["miniapp"])
                execution, preview_details = execute_checks(requested_changed_files)
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
                        "mode": str(request_item.get("mode") or "exact").strip().lower() or "exact",
                        "targets": requested_changed_files,
                        "reason": reason,
                        "failed_checks": failed_checks,
                        "preview_logs": list((preview_details.get("logs") or [])[-8:]),
                    }
                )
        return additional_targets, tool_results, extra_contexts
