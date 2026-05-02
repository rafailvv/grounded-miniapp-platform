from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from threading import Lock
from pathlib import Path
import time
from typing import Any, Callable

from app.models.domain import CheckExecutionRecord
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_kernel import plan_agent_tool_batches
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
)
from app.modules.miniapp_agent_loop.tool_batch_summary import summarize_tool_batch
from app.services.workspace.service import WorkspaceService


class AgentToolExecutor:
    """Ordered Claude-style tool executor for one draft workspace.

    Consecutive safe read-only tools are run concurrently. Validation tools
    are serialized in the original order because they snapshot/rebuild draft
    state and can be expensive or stateful.
    """

    def __init__(self, *, workspace_service: WorkspaceService, file_state_cache: AgentFileStateCache | None = None) -> None:
        self.workspace_service = workspace_service
        self.file_state_cache = file_state_cache or AgentFileStateCache()

    def execute(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_requests: list[dict[str, Any]],
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
        append_activity: Callable[[str, str, dict[str, Any] | None], None] | None = None,
        append_batch_summary: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        loaded_context: dict[str, str] = {}
        tool_results: list[dict[str, object]] = []
        workspace_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        tool_batch_plan = plan_agent_tool_batches(tool_requests)
        tool_results.append(
            {
                "tool": "agent_tool_batch",
                "contract": "read-only diagnostic tools are batched concurrently when safe; validation snapshots and all draft mutations are serialized.",
                "read_only_count": len(tool_batch_plan.read_only_requests),
                "mutating_count": len(tool_batch_plan.mutating_requests),
                "verification_count": len(tool_batch_plan.verification_requests),
                "unknown_count": len(tool_batch_plan.unknown_requests),
                "ordered_batches": [
                    {"concurrency_safe": batch.concurrency_safe, "tools": batch.tools}
                    for batch in tool_batch_plan.ordered_batches
                ],
            }
        )
        activity_lock = Lock()

        def emit_activity(kind: str, label: str, details: dict[str, Any] | None = None) -> None:
            if append_activity is not None:
                with activity_lock:
                    append_activity(kind, label, details or {})

        def tool_activity(tool_name: str) -> tuple[str, str]:
            spec = AgentToolRegistry.spec(tool_name)
            if spec is None:
                return "reading", "Reading workspace context"
            return spec.activity, spec.progress_label

        def tool_use_id(tool_name: str, ordinal: int | None = None) -> str:
            suffix = len(tool_results) + 1 if ordinal is None else ordinal
            return f"{str(tool_name or 'tool').strip().lower()}_{suffix}"

        def targets(request_item: dict[str, Any]) -> list[str]:
            return [
                self._strip_leading_dot_slash(item)
                for item in request_item.get("targets") or []
                if str(item or "").strip()
            ]

        def execute_read_only(request_item: dict[str, Any]) -> tuple[dict[str, str], dict[str, object]]:
            local_context: dict[str, str] = {}
            tool_name = str(request_item.get("tool") or "").strip().lower()
            use_id = str(request_item.get("tool_use_id") or tool_use_id(tool_name))
            request_targets = targets(request_item)
            reason = str(request_item.get("reason") or "").strip()
            activity, label = tool_activity(tool_name)
            emit_activity(
                "hook_started",
                "Pre-tool hook",
                {"hook": "pre_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "started"},
            )
            emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "started"})
            if tool_name == "list_files":
                result = {**list_workspace_files(workspace_tree=workspace_tree, targets=request_targets), "reason": reason, "tool_use_id": use_id}
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "completed", "path_count": len(result.get("paths") or [])})
                emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "completed"})
                return local_context, result
            if tool_name == "read_files":
                for target in request_targets[:16]:
                    content = self.file_state_cache.read(
                        run_id=run_id,
                        root=draft_source,
                        path=target,
                        read_text=lambda relative_path: self.workspace_service.try_read_text_file(
                            workspace_id,
                            relative_path,
                            run_id=run_id,
                        ),
                    )
                    if content is not None:
                        local_context[target] = content
                result = {
                    "tool": "read_files",
                    "tool_use_id": use_id,
                    "targets": request_targets,
                    "files": summarize_read_file_payloads(file_contents=local_context),
                    "reason": reason,
                }
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "completed", "file_count": len(local_context)})
                emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "completed"})
                return local_context, result
            if tool_name == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                result = {
                    **search_workspace_files(
                        workspace_tree=workspace_tree,
                        read_text_file=lambda relative_path: self.file_state_cache.read(
                            run_id=run_id,
                            root=draft_source,
                            path=relative_path,
                            read_text=lambda cached_path: self.workspace_service.try_read_text_file(
                                workspace_id,
                                cached_path,
                                run_id=run_id,
                            ),
                        ),
                        pattern=pattern,
                        targets=request_targets,
                    ),
                    "reason": reason,
                    "tool_use_id": use_id,
                }
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "completed", "match_count": len(result.get("matches") or [])})
                emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "completed"})
                return local_context, result
            if tool_name == "semantic_scan":
                result = {**semantic_scan(root=draft_source, targets=request_targets), "reason": reason, "tool_use_id": use_id}
                total_items = (
                    len(result.get("python") or [])
                    + len(result.get("html") or [])
                    + len(result.get("javascript") or [])
                    + len(result.get("css") or [])
                    + len(result.get("generated_tests") or [])
                )
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "completed", "item_count": total_items})
                emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "completed"})
                return local_context, result
            if tool_name == "inspect_diff":
                result = {**self._inspect_diff(workspace_id=workspace_id, run_id=run_id, targets=request_targets), "reason": reason, "tool_use_id": use_id}
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets, "status": "completed", "path_count": len(result.get("paths") or [])})
                emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name, "status": "completed"})
                return local_context, result
            if tool_name == "run_command":
                command = str(request_item.get("command") or "").strip()
                command_started = time.perf_counter()
                delta_counter = {"count": 0}

                def command_progress(payload: dict[str, Any]) -> None:
                    status = str(payload.get("status") or "")
                    if status == "started":
                        emit_activity(
                            "process_started",
                            "Diagnostic process started",
                            {"tool_use_id": use_id, "tool": tool_name, "command": command, **payload},
                        )
                    elif status == "output_delta":
                        delta_counter["count"] += 1
                        if delta_counter["count"] in {1, 2, 3} or delta_counter["count"] % 20 == 0:
                            emit_activity(
                                "command_output_delta",
                                "Diagnostic command emitted output",
                                {"tool_use_id": use_id, "tool": tool_name, "command": command, **payload},
                            )
                    elif status == "heartbeat":
                        emit_activity(
                            "tool_progress",
                            "Diagnostic command still running",
                            {"tool_use_id": use_id, "tool": tool_name, "command": command, **payload},
                        )
                    elif status == "completed":
                        emit_activity(
                            "process_completed",
                            "Diagnostic process completed",
                            {"tool_use_id": use_id, "tool": tool_name, "command": command, **payload},
                        )

                result = {
                    **run_workspace_command(
                        draft_source=draft_source,
                        command=command,
                        timeout_seconds=25,
                        max_output_chars=AgentToolRegistry.spec("run_command").output_cap_chars if AgentToolRegistry.spec("run_command") else 6000,
                        progress_callback=command_progress,
                    ),
                    "reason": reason,
                    "tool_use_id": use_id,
                }
                elapsed_ms = int((time.perf_counter() - command_started) * 1000)
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "command": command, "status": "completed", "elapsed_ms": elapsed_ms, "exit_code": result.get("exit_code"), "error": result.get("error")})
                hook_status = (
                    "failed"
                    if result.get("error") or (result.get("success") is False and result.get("semantic_status") != "no_matches")
                    else "completed"
                )
                emit_activity(
                    "hook_completed",
                    "Post-tool hook" if hook_status == "completed" else "Tool failure hook",
                    {
                        "hook": "post_tool_use" if hook_status == "completed" else "post_tool_use_failure",
                        "tool_use_id": use_id,
                        "tool": tool_name,
                        "status": hook_status,
                        "semantic_status": result.get("semantic_status"),
                    },
                )
                return local_context, result
            emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name or "unknown", "targets": request_targets, "status": "ignored"})
            emit_activity("hook_completed", "Post-tool hook", {"hook": "post_tool_use", "tool_use_id": use_id, "tool": tool_name or "unknown", "status": "ignored"})
            return local_context, {
                "tool": tool_name or "unknown",
                "tool_use_id": use_id,
                "status": "ignored",
                "reason": reason or "Unsupported read-only tool request.",
            }

        def flush_concurrent_batch(indexed_requests: list[tuple[int, dict[str, Any]]]) -> None:
            if not indexed_requests:
                return
            concurrent_outputs: list[tuple[int, dict[str, str], dict[str, object]]] = []
            started_at = time.perf_counter()
            batch_id = f"batch_{len(tool_results) + 1}"
            emit_activity(
                "reading",
                "Running read-only tool batch",
                {
                    "batch_id": batch_id,
                    "tool_use_id": batch_id,
                    "status": "started",
                    "tool_count": len(indexed_requests),
                    "tools": [str(item.get("tool") or "") for _, item in indexed_requests],
                },
            )
            max_workers = min(6, len(indexed_requests))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-read-tool") as executor:
                future_to_request = {
                    executor.submit(execute_read_only, request_item): (index, request_item)
                    for index, request_item in indexed_requests
                }
                for future in as_completed(future_to_request):
                    index, request_item = future_to_request[future]
                    try:
                        context, result = future.result()
                    except Exception as exc:
                        context = {}
                        result = {
                            "tool": str(request_item.get("tool") or "read_only_tool"),
                            "status": "error",
                            "error": str(exc),
                            "error_class": exc.__class__.__name__,
                        }
                    concurrent_outputs.append((index, context, result))
            for _, context, result in sorted(concurrent_outputs, key=lambda item: item[0]):
                loaded_context.update(context)
                tool_results.append(result)
            summary = summarize_tool_batch(
                batch_id=batch_id,
                requests=[request_item for _, request_item in indexed_requests],
                results=[result for _, _, result in concurrent_outputs],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                status="completed",
            )
            if append_batch_summary is not None:
                append_batch_summary(summary)
            emit_activity(
                "tool_use_summary",
                summary["summary"],
                {**summary, "status": "completed"},
            )

        def execute_serial_tool(request_item: dict[str, Any]) -> None:
            tool_name = str(request_item.get("tool") or "").strip().lower()
            use_id = str(request_item.get("tool_use_id") or tool_use_id(tool_name))
            request_targets = targets(request_item)
            reason = str(request_item.get("reason") or "").strip()
            activity, label = tool_activity(tool_name)
            if tool_name == "run_checks":
                serial_started = time.perf_counter()
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets or ["miniapp"], "status": "started"})
                mode = str(request_item.get("mode") or "exact").strip().lower()
                execution, preview_details = execute_checks(request_targets or ["miniapp"])
                elapsed_ms = int((time.perf_counter() - serial_started) * 1000)
                if elapsed_ms >= 5000:
                    emit_activity("tool_progress", "Validation checks are still running", {"tool_use_id": use_id, "tool": tool_name, "elapsed_ms": elapsed_ms, "status": "heartbeat"})
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
                        "tool_use_id": use_id,
                        "contract": "serialized_read_only_validation_snapshot",
                        "writes_files": False,
                        "executes_arbitrary_commands": False,
                        "mode": mode,
                        "targets": request_targets or ["miniapp"],
                        "failed_checks": failed_checks,
                        "preview": preview_details,
                        "reason": reason,
                    }
                )
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets or ["miniapp"], "status": "completed", "elapsed_ms": elapsed_ms, "failed_count": len(failed_checks)})
                return
            if tool_name == "browser_verify":
                serial_started = time.perf_counter()
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets or ["miniapp"], "status": "started"})
                execution, preview_details = execute_checks(request_targets or ["miniapp"])
                elapsed_ms = int((time.perf_counter() - serial_started) * 1000)
                if elapsed_ms >= 5000:
                    emit_activity("tool_progress", "Browser proof is still running", {"tool_use_id": use_id, "tool": tool_name, "elapsed_ms": elapsed_ms, "status": "heartbeat"})
                workflow_results = [
                    {
                        "name": result.name,
                        "status": result.status,
                        "details": result.details,
                        "logs": result.logs[-8:],
                        "diagnostics": result.diagnostics,
                    }
                    for result in execution.results
                    if result.name in {"api_workflow_smoke", "browser_flow_smoke"}
                ]
                tool_results.append(
                    {
                        "tool": "browser_verify",
                        "tool_use_id": use_id,
                        "contract": "serialized_read_only_browser_and_api_workflow_snapshot",
                        "writes_files": False,
                        "targets": request_targets or ["miniapp"],
                        "workflow_results": workflow_results,
                        "preview": preview_details,
                        "reason": reason,
                    }
                )
                emit_activity(activity, label, {"tool_use_id": use_id, "tool": tool_name, "targets": request_targets or ["miniapp"], "status": "completed", "elapsed_ms": elapsed_ms, "workflow_count": len(workflow_results)})
                return
            if AgentToolRegistry.kind(tool_name) == "read_only":
                context, result = execute_read_only(request_item)
                loaded_context.update(context)
                tool_results.append(result)
                return
            tool_results.append(
                {
                    "tool": tool_name or "unknown",
                    "tool_use_id": use_id,
                    "status": "ignored",
                    "reason": str(request_item.get("reason") or "Unsupported tool request."),
                }
            )

        for batch_index, batch in enumerate(tool_batch_plan.ordered_batches):
            if batch.concurrency_safe:
                flush_concurrent_batch([(batch_index * 1000 + index, request_item) for index, request_item in enumerate(batch.requests)])
                continue
            for request_item in batch.requests:
                started_at = time.perf_counter()
                before_count = len(tool_results)
                execute_serial_tool(request_item)
                batch_id = f"batch_{len(tool_results) + 1}"
                result_slice = [item for item in tool_results[before_count:] if isinstance(item, dict)]
                summary = summarize_tool_batch(
                    batch_id=batch_id,
                    requests=[request_item],
                    results=result_slice,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    status="completed",
                )
                if append_batch_summary is not None:
                    append_batch_summary(summary)
                emit_activity(
                    "tool_use_summary",
                    str(summary["summary"]),
                    summary,
                )
        return loaded_context, tool_results

    def _inspect_diff(self, *, workspace_id: str, run_id: str, targets: list[str]) -> dict[str, object]:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        paths = self._paths_from_diff(diff_text)
        normalized_targets = [target.rstrip("/") for target in targets if target.strip()]
        if normalized_targets:
            selected_chunks: list[str] = []
            current_chunk: list[str] = []
            current_path = ""
            for line in diff_text.splitlines():
                if line.startswith("diff --git "):
                    if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                        selected_chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_path = ""
                    match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
                    if match:
                        current_path = self._strip_leading_dot_slash(match.group(2))
                    continue
                current_chunk.append(line)
            if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                selected_chunks.append("\n".join(current_chunk))
            diff_text = "\n".join(selected_chunks)
        return {
            "tool": "inspect_diff",
            "paths": paths,
            "diff": diff_text[:12000],
            "truncated": len(diff_text) > 12000,
        }

    @staticmethod
    def _path_matches_targets(path: str, targets: list[str]) -> bool:
        if not targets:
            return True
        normalized = AgentToolExecutor._strip_leading_dot_slash(path)
        return any(normalized == target or normalized.startswith(f"{target.rstrip('/')}/") for target in targets)

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff_text or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
            if match:
                path = AgentToolExecutor._strip_leading_dot_slash(match.group(2))
                if path and path not in paths:
                    paths.append(path)
        return paths

    @staticmethod
    def _strip_leading_dot_slash(raw_path: object) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path
