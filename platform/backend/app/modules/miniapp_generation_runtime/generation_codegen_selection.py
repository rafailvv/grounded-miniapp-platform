from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

from app.ai.model_registry import task_model_overrides
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
    _HELPER_DISCOVERY_TARGETS = {
        "miniapp/app/static/shared/runtime.js",
        "miniapp/app/static/shared/api.js",
        "miniapp/app/static/preview_bridge.js",
    }
    _HELPER_DISCOVERY_PATTERNS = {"api", "runtime", "miniappapifetch", "preview_bridge"}

    @staticmethod
    def _tool_request_signature(tool_requests: list[dict[str, Any]]) -> str:
        if not tool_requests:
            return ""
        normalized_items = []
        for item in tool_requests:
            normalized_items.append(
                {
                    "tool": str(item.get("tool") or "").strip().lower(),
                    "mode": str(item.get("mode") or "").strip().lower(),
                    "targets": sorted(
                        {
                            str(target or "").strip().lstrip("./")
                            for target in list(item.get("targets") or [])
                            if str(target or "").strip()
                        }
                    ),
                    "pattern": str(item.get("pattern") or "").strip(),
                    "command": str(item.get("command") or "").strip(),
                }
            )
        return json.dumps(normalized_items, sort_keys=True, ensure_ascii=True)

    @staticmethod
    def _duplicate_tool_request_feedback(tool_requests: list[dict[str, Any]]) -> dict[str, object]:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        return {
            "tool": "tool_request_feedback",
            "targets": list(dict.fromkeys(requested_targets)),
            "error": (
                "The same tool request was already executed in this attempt. "
                "These files were already read and are available in current context. "
                "Use the existing file_contexts and prior tool_results to return operations "
                "or outcome=no_progress instead of requesting the same files again."
            ),
        }

    @staticmethod
    def _already_available_context_note(tool_requests: list[dict[str, Any]]) -> str:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        distinct_targets = list(dict.fromkeys(requested_targets))
        if not distinct_targets:
            return (
                "Context reuse recovery mode:\n"
                "- The files you asked to read are already present in file_contexts.\n"
                "- Do not request read_files again for already provided files.\n"
                "- Use the current context to return the page HTML/CSS/JS operations now.\n"
                "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
            )
        rendered_targets = "\n".join(f"- {target}" for target in distinct_targets[:12])
        return (
            "Context reuse recovery mode:\n"
            "- The files you asked to read are already present in file_contexts.\n"
            "- These already-available files are:\n"
            f"{rendered_targets}\n"
            "- Do not request read_files again for these files.\n"
            "- Use the current context to return the page HTML/CSS/JS operations now.\n"
            "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
        )

    @classmethod
    def _is_runtime_helper_discovery_request(cls, *, page: dict[str, Any], tool_requests: list[dict[str, Any]]) -> bool:
        page_kind = str(page.get("page_kind") or "").strip().lower()
        if page_kind not in {"profile", "dashboard", "feature"} or not tool_requests:
            return False
        file_path = str(page.get("file_path") or "")
        if not (
            file_path.endswith("/index.html")
            and any(segment in file_path for segment in ("/client/", "/specialist/", "/manager/"))
        ):
            return False
        for item in tool_requests:
            tool_name = str(item.get("tool") or "").strip().lower()
            targets = [
                str(target or "").strip().lstrip("./")
                for target in list(item.get("targets") or [])
                if str(target or "").strip()
            ]
            if tool_name == "search_files":
                pattern = str(item.get("pattern") or "").strip().lower()
                if pattern not in cls._HELPER_DISCOVERY_PATTERNS:
                    return False
                if targets and not all(target.startswith("miniapp/app/static") for target in targets):
                    return False
                continue
            if tool_name == "read_files":
                if not targets or any(target not in cls._HELPER_DISCOVERY_TARGETS for target in targets):
                    return False
                continue
            return False
        return True

    @staticmethod
    def _runtime_helper_discovery_feedback(tool_requests: list[dict[str, Any]]) -> dict[str, object]:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        return {
            "tool": "tool_request_feedback",
            "targets": list(dict.fromkeys(requested_targets)),
            "error": (
                "Do not search for generic static/shared runtime or api helpers here. "
                "The canonical runtime bridge is /static/preview_bridge.js, which already exposes "
                "window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role). "
                "Existing template role pages may also call same-origin /api/... endpoints directly with fetch(...). "
                "Use the current file_contexts and supporting context to emit operations now."
            ),
        }

    @staticmethod
    def _read_request_already_satisfied(
        tool_requests: list[dict[str, Any]],
        file_contexts: dict[str, str],
    ) -> bool:
        if not tool_requests:
            return False
        saw_read = False
        for item in tool_requests:
            tool_name = str(item.get("tool") or "").strip().lower()
            if tool_name != "read_files":
                return False
            saw_read = True
            targets = [
                str(target or "").strip().lstrip("./")
                for target in list(item.get("targets") or [])
                if str(target or "").strip()
            ]
            if not targets:
                return False
            if any(
                not str(file_contexts.get(target) or "").strip()
                or GenerationRepairToolRuntime.is_missing_file_context(file_contexts.get(target))
                for target in targets
            ):
                return False
        return saw_read

    def _preload_existing_page_contexts(
        self,
        *,
        workspace_id: str | None,
        draft_run_id: str | None,
        targets: list[str],
        file_contexts: dict[str, str],
    ) -> dict[str, str]:
        current = dict(file_contexts)
        if not (workspace_id and draft_run_id):
            return current
        for raw_target in targets:
            target = str(raw_target or "").strip().lstrip("./")
            if not target or str(current.get(target) or "").strip():
                continue
            content = self.service.workspace_service.try_read_text_file(workspace_id, target, run_id=draft_run_id)
            if content is not None:
                current[target] = content
                continue
            if self.service._is_canonical_target_path(target):
                current[target] = GenerationRepairToolRuntime._missing_file_context(target)
        return current

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
        ordered = [
            path
            for path in target_files
            if path.startswith("miniapp/")
            and path not in page_paths
            and path.strip().replace("\\", "/") != "miniapp/app/routes/__init__.py"
        ]
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
        entity_contract: dict[str, Any],
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
            page_targets = [
                page["file_path"],
                str(page.get("style_path") or self._default_page_asset_path(page["file_path"], asset_kind="css")),
                str(page.get("script_path") or self._default_page_asset_path(page["file_path"], asset_kind="js")),
            ]
            context_reuse_recovery_note = ""
            current_file_contexts = self._preload_existing_page_contexts(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                targets=page_targets,
                file_contexts=file_contexts,
            )
            tool_results_for_attempt: list[dict[str, object]] = []
            seen_tool_request_signatures: set[str] = set()
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._page_edit_system_prompt()
                    user_prompt = self._page_edit_user_prompt(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        entity_contract=entity_contract,
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
                    if context_reuse_recovery_note:
                        system_prompt = f"{system_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
                    model_override, fallback_model_override = task_model_overrides(
                        role="code_edit",
                        generation_mode=prompt_mode,
                        scope_mode=scope_mode,
                        visual_only_patch=scope_mode == "minimal_patch",
                        target_file_count=3,
                        backend_target_count=0,
                    )
                    payload = self._generate_structured_with_retry(
                        role="code_edit",
                        schema_name=f"page_file_v1_{page['page_id']}",
                        schema=self._code_edit_schema(),
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        model_override=model_override,
                        fallback_model_override=fallback_model_override,
                    )
                    normalized = self._normalize_model_payload(payload["payload"])
                    tool_requests = normalize_tool_requests(normalized.get("tool_requests") or [])
                    raw_operations = normalized.get("operations")
                    outcome_hint = str(normalized.get("outcome") or "").strip().lower()
                    if outcome_hint == "tool_request" or tool_requests:
                        request_signature = self._tool_request_signature(tool_requests)
                        if self._is_runtime_helper_discovery_request(page=page, tool_requests=tool_requests):
                            context_reuse_recovery_note = (
                                "Runtime helper recovery mode:\n"
                                "- Do not search for static/shared/runtime.js or static/shared/api.js.\n"
                                "- The canonical bridge is /static/preview_bridge.js and it already exposes window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role).\n"
                                "- If existing template examples use same-origin fetch('/api/...'), that is also valid for this page.\n"
                                "- Use the current context and emit operations now.\n"
                            )
                            tool_results_for_attempt.append(self._runtime_helper_discovery_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(f"{page['file_path']} kept searching for nonexistent runtime/api helper files instead of using current context.")
                        if self._read_request_already_satisfied(tool_requests, current_file_contexts):
                            if request_signature and request_signature in seen_tool_request_signatures:
                                tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                                raise ValueError(f"{page['file_path']} repeated identical tool requests without returning operations.")
                            context_reuse_recovery_note = self._already_available_context_note(tool_requests)
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if request_signature:
                                seen_tool_request_signatures.add(request_signature)
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(f"{page['file_path']} requested files that were already present in the current context.")
                        if request_signature and request_signature in seen_tool_request_signatures:
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(f"{page['file_path']} repeated identical tool requests without returning operations.")
                        if not (workspace_id and draft_run_id and draft_source is not None):
                            raise ValueError(f"{page['file_path']} requested tools without a draft workspace runtime.")
                        requested_targets, executed_tool_results, extra_contexts = tool_runtime.execute_tool_requests(
                            workspace_id=workspace_id,
                            draft_run_id=draft_run_id,
                            workspace_tree=list(workspace_tree or []),
                            draft_source=draft_source,
                            tool_requests=tool_requests,
                            fallback_targets=[page["file_path"]],
                            execute_checks=lambda requested_changed_files, mode: self._execute_tool_requested_checks(
                                workspace_id=workspace_id,
                                draft_run_id=draft_run_id,
                                draft_source=draft_source,
                                changed_files=requested_changed_files,
                                scope_mode=scope_mode,
                                mode=mode,
                            ),
                            command_timeout_seconds=int(getattr(self.service.timeout_profile, "tool_command_sec", 180)),
                        )
                        if request_signature:
                            seen_tool_request_signatures.add(request_signature)
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
                    existing_primary_content = current_file_contexts.get(page["file_path"])
                    if existing_primary_content is None and workspace_id and draft_run_id:
                        existing_primary_content = self.service.workspace_service.try_read_text_file(
                            workspace_id,
                            page["file_path"],
                            run_id=draft_run_id,
                        )
                    if primary_operation is None:
                        companion_operations = [
                            valid_operations[path]
                            for path in sorted(path for path in allowed_paths if path != page["file_path"])
                            if path in valid_operations
                        ]
                        if companion_operations and existing_primary_content is not None:
                            return {
                                "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                                "operations": companion_operations,
                                "model": payload["model"],
                                "tool_results": list(tool_results_for_attempt),
                            }
                        if not valid_operations and outcome_hint in {"no_progress", "done", "completed", "patch_ready"} and existing_primary_content is not None:
                            return {
                                "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                                "operations": [],
                                "model": payload["model"],
                                "tool_results": list(tool_results_for_attempt),
                            }
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
        mode: str = "exact",
    ) -> tuple[Any, dict[str, Any]]:
        execution = self.service.check_runner.run(
            workspace_id=workspace_id,
            run_id=draft_run_id,
            source_dir=draft_source,
            changed_files=sorted(set(changed_files or ["miniapp"])),
            preview_run_id=draft_run_id,
            scope_mode="whole_file_build" if mode == "final" else scope_mode,
        )
        preview = self.service.preview_service.get(workspace_id)
        return execution, {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "logs": list(preview.logs),
            "last_error": preview.last_error,
        }
