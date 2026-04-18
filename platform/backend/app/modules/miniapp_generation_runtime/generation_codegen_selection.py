from __future__ import annotations

import logging
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class MiniappGenerationCodegenSelection(MiniappGenerationRuntimeOwner):
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
    ) -> dict[str, Any]:
        retry_modes = [generation_mode]
        if generation_mode != GenerationMode.FAST:
            retry_modes.append(GenerationMode.FAST)
        last_error: Exception | None = None
        for mode_attempt, prompt_mode in enumerate(retry_modes):
            try:
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
                    file_contexts=file_contexts,
                    generation_mode=prompt_mode,
                    creative_direction=creative_direction,
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
                raw_operations = normalized.get("operations")
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
                    "assistant_message": str(normalized.get("assistant_message") or "").strip(),
                    "operation": primary_operation,
                    "operations": ordered_operations,
                    "model": payload["model"],
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
