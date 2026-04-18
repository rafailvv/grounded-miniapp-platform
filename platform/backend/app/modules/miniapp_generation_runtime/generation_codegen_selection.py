from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_agent_loop.tool_agent_runtime import normalize_tool_requests
from app.modules.miniapp_generation_runtime.generation_paths import MiniappGenerationPaths
from app.modules.miniapp_generation_runtime.generation_repair_tools import GenerationRepairToolRuntime

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class MiniappGenerationCodegenSelection(MiniappGenerationRuntimeOwner):
    MAX_TOOL_ROUNDS = 5
    COMMAND_TIMEOUT_SECONDS = 20

    @staticmethod
    def _selected_pages_for_edit(page_graph: dict[str, Any], target_files: set[str]) -> list[tuple[str, dict[str, Any]]]:
        selected: list[tuple[str, dict[str, Any]]] = []
        for role, role_payload in (page_graph.get("roles") or {}).items():
            for page in role_payload.get("pages") or []:
                file_path = page.get("file_path")
                if not isinstance(file_path, str):
                    continue
                if target_files and file_path not in target_files:
                    continue
                selected.append((role, page))
        if selected:
            return selected
        for file_path in sorted(target_files):
            match = re.fullmatch(r"miniapp/app/static/(client|specialist|manager)(?:/([^/]+))?/index\.html", file_path)
            if not match:
                continue
            role = match.group(1)
            slug = str(match.group(2) or "").strip()
            route_path = "/" if not slug else f"/{slug.replace('_', '-')}"
            page_kind = "profile" if slug == "profile" else ("dashboard" if not slug else "feature")
            selected.append(
                (
                    role,
                    {
                        "page_id": f"{role}_{slug or 'index'}",
                        "route_path": route_path,
                        "file_path": file_path,
                        "style_path": MiniappGenerationPaths._default_page_asset_path(file_path, asset_kind="css"),
                        "script_path": MiniappGenerationPaths._default_page_asset_path(file_path, asset_kind="js"),
                        "page_kind": page_kind,
                        "title": slug.replace("_", " ").title() if slug else role.title(),
                    },
                )
            )
        return selected

    @staticmethod
    def _backend_composition_targets(target_files: list[str], selected_pages: list[tuple[str, dict[str, Any]]]) -> list[str]:
        page_paths = {str(page.get("file_path")) for _, page in selected_pages if isinstance(page.get("file_path"), str)}
        ordered = [path for path in target_files if path.startswith("miniapp/") and path not in page_paths]
        return list(dict.fromkeys(ordered))

    @staticmethod
    def _frontend_composition_targets(target_files: list[str], selected_pages: list[tuple[str, dict[str, Any]]]) -> list[str]:
        page_paths = {str(page.get("file_path")) for _, page in selected_pages if isinstance(page.get("file_path"), str)}
        ordered = [path for path in target_files if path.startswith("miniapp/app/static/") and path not in page_paths]
        return list(dict.fromkeys(ordered))

    @staticmethod
    def _partition_frontend_composition_targets(
        target_files: list[str],
        page_graph: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        route_like = {
            *{
                str(role_payload.get("routes_file"))
                for role_payload in (page_graph.get("roles") or {}).values()
                if isinstance(role_payload, dict) and isinstance(role_payload.get("routes_file"), str)
            },
        }
        bootstrap_markers = ("/styles.css", "/app.js", "/profile.html", "/workbench.html", "/workspace.html")
        routing_targets: list[str] = []
        bootstrap_targets: list[str] = []
        for path in target_files:
            if path in route_like or path.endswith("Routes.tsx"):
                routing_targets.append(path)
                continue
            if any(path.endswith(marker) for marker in bootstrap_markers):
                bootstrap_targets.append(path)
                continue
            bootstrap_targets.append(path)
        return list(dict.fromkeys(bootstrap_targets)), list(dict.fromkeys(routing_targets))

    def _resolve_page_file_edit(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role: str,
        page: dict[str, Any],
        page_graph: dict[str, Any],
        role_contract: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        recovery_mode: str = "default",
        workspace_id: str | None = None,
        draft_run_id: str | None = None,
        workspace_tree: list[dict[str, str]] | None = None,
        draft_source: Path | None = None,
    ) -> dict[str, Any]:
        retry_modes = [generation_mode]
        if generation_mode != GenerationMode.FAST:
            retry_modes.append(GenerationMode.FAST)
        last_error: Exception | None = None
        tool_runtime = GenerationRepairToolRuntime(self.service)
        for mode_attempt, prompt_mode in enumerate(retry_modes):
            current_file_contexts = dict(file_contexts)
            tool_results_for_attempt: list[dict[str, object]] = []
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._page_edit_system_prompt()
                    user_prompt = self._page_edit_user_prompt(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        role=role,
                        page=page,
                        page_graph=page_graph,
                        role_contract=role_contract,
                        scope_mode=scope_mode,
                        intent=intent,
                        file_contexts=current_file_contexts,
                        generation_mode=prompt_mode,
                        creative_direction=creative_direction,
                        tool_results=list(tool_results_for_attempt),
                    )
                    if mode_attempt > 0 or recovery_mode != "default":
                        recovery_note = (
                            "Provider recovery mode:\n"
                            "- Previous attempt failed with a transient provider or transport issue.\n"
                            "- Keep the page implementation concise and stable.\n"
                            "- Return operations for the requested page HTML plus its CSS and JS companion files.\n"
                            "- Prefer the smallest valid page implementation over extra polish."
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{recovery_note}".strip()
                    payload = self._generate_structured_with_retry(
                        role="code_edit",
                        schema_name=f"page_file_v1_{page['page_id']}",
                        schema=self._code_edit_schema(),
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                    )
                    normalized = self._normalize_model_payload(payload["payload"])
                    tool_requests = normalize_tool_requests(normalized.get("tool_requests") or [])
                    raw_operations = normalized.get("operations")
                    outcome_hint = str(normalized.get("outcome") or "").strip().lower()
                    if outcome_hint == "tool_request" or tool_requests:
                        if not (workspace_id and draft_run_id and draft_source is not None):
                            raise ValueError(f"{page['file_path']} requested tools without a draft workspace runtime.")
                        requested_targets, executed_tool_results, extra_contexts = tool_runtime.execute_tool_requests(
                            workspace_id=workspace_id,
                            draft_run_id=draft_run_id,
                            workspace_tree=list(workspace_tree or []),
                            draft_source=draft_source,
                            tool_requests=tool_requests,
                            fallback_targets=[page["file_path"]],
                            execute_checks=lambda requested_changed_files: self._execute_tool_requested_checks(
                                workspace_id=workspace_id,
                                draft_run_id=draft_run_id,
                                draft_source=draft_source,
                                changed_files=requested_changed_files,
                                scope_mode=scope_mode,
                            ),
                            command_timeout_seconds=self.COMMAND_TIMEOUT_SECONDS,
                        )
                        current_file_contexts.update(extra_contexts)
                        tool_results_for_attempt.extend(executed_tool_results)
                        for requested_target in requested_targets:
                            content = self.service.workspace_service.try_read_text_file(workspace_id, requested_target, run_id=draft_run_id)
                            if content is not None:
                                current_file_contexts[requested_target] = content
                        if tool_round < self.MAX_TOOL_ROUNDS and (requested_targets or executed_tool_results):
                            continue
                        raise ValueError(f"{page['file_path']} exhausted the tool-request budget without returning operations.")
                    if not isinstance(raw_operations, list):
                        raise ValueError(f"{page['file_path']} did not return operations.")
                    operations = self._sanitize_draft_operations([DraftFileOperation.model_validate(item) for item in raw_operations])
                    allowed_paths = {
                        page["file_path"],
                        str(page.get("style_path") or self._default_page_asset_path(page["file_path"], asset_kind="css")),
                        str(page.get("script_path") or self._default_page_asset_path(page["file_path"], asset_kind="js")),
                    }
                    foreign_operations = [operation.file_path for operation in operations if operation.file_path not in allowed_paths]
                    if foreign_operations:
                        raise ValueError(f"{page['file_path']} returned operations for other files: {', '.join(sorted(set(foreign_operations))[:5])}.")
                    valid_operations = {
                        operation.file_path: operation
                        for operation in operations
                        if operation.file_path in allowed_paths and operation.operation in {"create", "replace"} and operation.content is not None
                    }
                    primary_operation = valid_operations.get(page["file_path"])
                    if primary_operation is None:
                        raise ValueError(f"{page['file_path']} did not produce the required page HTML operation.")
                    ordered_operations = [primary_operation]
                    for companion_path in sorted(path for path in allowed_paths if path != page["file_path"]):
                        operation = valid_operations.get(companion_path)
                        if operation is not None:
                            ordered_operations.append(operation)
                    return {
                        "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                        "operation": primary_operation,
                        "operations": ordered_operations,
                        "model": payload["model"],
                        "tool_results": list(tool_results_for_attempt),
                    }
            except Exception as exc:
                last_error = exc
                if mode_attempt + 1 < len(retry_modes) and (self._is_retryable_llm_error(exc) or self._is_recoverable_page_error(exc)):
                    logger.warning("Retrying page generation for %s with compact recovery context after recoverable failure: %s", page["file_path"], exc)
                    continue
                break
        assert last_error is not None
        return {
            "error": f"Page generation failed for {page['file_path']}: {last_error}",
            "retryable": self._is_retryable_llm_error(last_error),
            "file_path": page["file_path"],
        }

    def _execute_tool_requested_checks(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        draft_source: Path,
        changed_files: list[str],
        scope_mode: str,
    ) -> tuple[Any, dict[str, Any]]:
        execution = self.service.check_runner.run(
            workspace_id=workspace_id,
            run_id=draft_run_id,
            source_dir=draft_source,
            changed_files=sorted(set(changed_files or ["miniapp"])),
            preview_run_id=draft_run_id,
            scope_mode=scope_mode,
        )
        preview = self.service.preview_service.get(workspace_id)
        return execution, {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "logs": list(preview.logs),
            "last_error": preview.last_error,
        }
