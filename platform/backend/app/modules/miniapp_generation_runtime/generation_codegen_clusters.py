from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_agent_loop.tool_agent_runtime import normalize_tool_requests
from app.modules.miniapp_generation_runtime.generation_repair_tools import GenerationRepairToolRuntime

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class MiniappGenerationCodegenClusters(MiniappGenerationRuntimeOwner):
    MAX_TOOL_ROUNDS = 5
    COMMAND_TIMEOUT_SECONDS = 20

    def _resolve_whole_file_cluster(
        self,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        workspace_id: str | None = None,
        draft_run_id: str | None = None,
        workspace_tree: list[dict[str, str]] | None = None,
        draft_source: Path | None = None,
    ) -> dict[str, Any]:
        completeness_recovery_used = False
        scope_recovery_used = False
        framework_recovery_used = False
        last_error: Exception | None = None
        tool_runtime = GenerationRepairToolRuntime(self.service)
        for _ in range(3):
            current_file_contexts = dict(file_contexts)
            tool_results_for_attempt: list[dict[str, object]] = []
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._whole_file_cluster_system_prompt(cluster_name)
                    user_prompt = self._whole_file_cluster_user_prompt(
                        cluster_name=cluster_name,
                        cluster_targets=cluster_targets,
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        role_scope=role_scope,
                        role_contract=role_contract,
                        page_graph=page_graph,
                        scope_mode=scope_mode,
                        intent=intent,
                        file_contexts=current_file_contexts,
                        generation_mode=generation_mode,
                        creative_direction=creative_direction,
                        tool_results=list(tool_results_for_attempt),
                    )
                    if completeness_recovery_used:
                        recovery_note = (
                            "Cluster completeness recovery mode:\n"
                            "- The previous attempt omitted required target files from this cluster.\n"
                            "- You must emit create/replace operations for every new required target file in cluster_targets.\n"
                            "- Do not stop after index.html or app.js if the cluster also contains detail, workload, form, route, or manifest files.\n"
                            "- Return one operation per required file path with the complete final file body.\n"
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{recovery_note}".strip()
                    if scope_recovery_used:
                        scope_note = (
                            "Cluster scope recovery mode:\n"
                            "- The previous attempt introduced a canonical role-local support file outside cluster_targets.\n"
                            "- Keep one page triplet per planned page: HTML, CSS, and JS with the same stem.\n"
                            "- Do not collapse page behavior into role-level app.js/styles.css files.\n"
                            "- Do not use inline style or script blocks when page CSS/JS targets exist.\n"
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{scope_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{scope_note}".strip()
                    if framework_recovery_used:
                        framework_note = (
                            "Backend framework recovery mode:\n"
                            "- The previous attempt drifted away from the FastAPI contract.\n"
                            "- Every Python file in miniapp/app/routes must use `from fastapi import APIRouter` and a top-level `router = APIRouter(...)`.\n"
                            "- Do not emit Flask imports, Blueprint objects, current_app, send_from_directory, or Flask decorators.\n"
                            "- Keep page-serving route modules on FastAPI using FileResponse, HTMLResponse, or Jinja2Templates only.\n"
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{framework_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{framework_note}".strip()
                    payload = self._generate_structured_with_retry(
                        role="code_edit",
                        schema_name=f"whole_file_bundle_v1_{cluster_name}",
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
                            raise ValueError(f"Whole-file cluster {cluster_name} requested tools without a draft workspace runtime.")
                        requested_targets, executed_tool_results, extra_contexts = tool_runtime.execute_tool_requests(
                            workspace_id=workspace_id,
                            draft_run_id=draft_run_id,
                            workspace_tree=list(workspace_tree or []),
                            draft_source=draft_source,
                            tool_requests=tool_requests,
                            fallback_targets=cluster_targets,
                            execute_checks=lambda requested_changed_files, mode: self._execute_tool_requested_checks(
                                workspace_id=workspace_id,
                                draft_run_id=draft_run_id,
                                draft_source=draft_source,
                                changed_files=requested_changed_files,
                                scope_mode=scope_mode,
                                mode=mode,
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
                        raise ValueError(f"Whole-file cluster {cluster_name} exhausted the tool-request budget without returning operations.")
                    if not isinstance(raw_operations, list):
                        raise ValueError("Whole-file cluster did not return operations.")
                    operations = self._sanitize_draft_operations([DraftFileOperation.model_validate(item) for item in raw_operations])
                    if not operations:
                        if intent != "create" and all(str(current_file_contexts.get(path) or "").strip() for path in cluster_targets):
                            return {
                                "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip() or f"{cluster_name} already matches the requested workspace state.",
                                "operations": [],
                                "model": payload["model"],
                                "outcome": "no_op",
                                "tool_results": list(tool_results_for_attempt),
                            }
                        raise ValueError("Whole-file cluster returned no file operations for the requested cluster_targets.")
                    allowed_targets = set(cluster_targets)
                    invalid = [
                        operation.file_path
                        for operation in operations
                        if operation.file_path not in allowed_targets or operation.operation not in {"create", "replace"} or operation.content is None
                    ]
                    if invalid:
                        expanded_targets = self._expand_cluster_targets_for_safe_companions(
                            cluster_name=cluster_name,
                            cluster_targets=cluster_targets,
                            invalid_paths=invalid,
                        )
                        if expanded_targets is not None and expanded_targets != cluster_targets:
                            cluster_targets = expanded_targets
                            scope_recovery_used = True
                            continue
                        raise ValueError(f"Whole-file cluster touched files outside its scope: {', '.join(invalid[:5])}")
                    self._validate_targeted_operations(stage_name=cluster_name, target_files=cluster_targets, operations=operations)
                    missing_required_targets = self._missing_required_cluster_targets(
                        cluster_targets=cluster_targets,
                        operations=operations,
                        file_contexts=current_file_contexts,
                    )
                    if missing_required_targets:
                        raise ValueError(f"Whole-file cluster omitted required target files: {', '.join(missing_required_targets[:5])}")
                    return {
                        "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                        "operations": operations,
                        "model": payload["model"],
                        "tool_results": list(tool_results_for_attempt),
                    }
            except Exception as exc:
                last_error = exc
                if not completeness_recovery_used and "omitted required target files" in str(exc).lower():
                    completeness_recovery_used = True
                    continue
                if not framework_recovery_used and self._is_backend_framework_contract_error(str(exc)):
                    framework_recovery_used = True
                    continue
                break
        assert last_error is not None
        raise last_error

    def _timed_whole_file_cluster(self, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            result = self._resolve_whole_file_cluster(**kwargs)
        except Exception as exc:
            return {"error": f"Whole-file cluster failed for {kwargs['cluster_name']}: {exc}"}
        return {**result, "cluster_name": kwargs["cluster_name"], "target_files": kwargs["cluster_targets"], "duration_ms": int((time.perf_counter() - started) * 1000)}

    def _resolve_composition_edit(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        intent: str,
        stage_name: str,
        target_files: list[str],
        file_contexts: dict[str, str],
        generated_page_sources: dict[str, str],
        generated_support_sources: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        workspace_id: str | None = None,
        draft_run_id: str | None = None,
        workspace_tree: list[dict[str, str]] | None = None,
        draft_source: Path | None = None,
    ) -> dict[str, Any]:
        if not target_files:
            return {"assistant_message": f"{stage_name.capitalize()} stage complete: no changes were required.", "operations": []}
        allowed_targets = set(target_files)
        retry_modes = [generation_mode]
        if generation_mode != GenerationMode.FAST:
            retry_modes.append(GenerationMode.FAST)
        last_error: Exception | None = None
        scope_recovery_used = False
        tool_runtime = GenerationRepairToolRuntime(self.service)
        for mode_attempt, prompt_mode in enumerate(retry_modes):
            current_file_contexts = dict(file_contexts)
            tool_results_for_attempt: list[dict[str, object]] = []
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._composition_system_prompt(stage_name)
                    user_prompt = self._composition_user_prompt(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        role_scope=role_scope,
                        role_contract=role_contract,
                        page_graph=page_graph,
                        scope_mode=scope_mode,
                        intent=intent,
                        stage_name=stage_name,
                        target_files=target_files,
                        file_contexts=current_file_contexts,
                        generated_page_sources=generated_page_sources,
                        generated_support_sources=generated_support_sources,
                        generation_mode=prompt_mode,
                        creative_direction=creative_direction,
                        tool_results=list(tool_results_for_attempt),
                    )
                    if scope_recovery_used:
                        scope_recovery_note = (
                            "Scope recovery mode:\n"
                            "- The previous attempt used file paths outside the real workspace.\n"
                            "- You must only emit operations for the exact repo-relative files listed in target_files.\n"
                            "- Do not invent miniapp/src, src/server.ts, or any new architecture root unless that exact path is present in target_files.\n"
                            "- If a miniapp surface is requested but the target_files are frontend-only, leave miniapp untouched.\n"
                            "- If a target file does not exist yet, create only that exact path.\n"
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{scope_recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{scope_recovery_note}".strip()
                    if mode_attempt > 0:
                        recovery_note = (
                            "Provider recovery mode:\n"
                            "- Previous attempt failed with a transient provider or transport issue.\n"
                            "- Keep the composition patch concise.\n"
                            "- Only return operations for target_files.\n"
                            "- Prefer stable wiring over extra polish."
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{recovery_note}".strip()
                    payload = self._generate_structured_with_retry(
                        role="code_edit",
                        schema_name="composition_bundle_v1",
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
                            raise ValueError(f"Composition stage {stage_name} requested tools without a draft workspace runtime.")
                        requested_targets, executed_tool_results, extra_contexts = tool_runtime.execute_tool_requests(
                            workspace_id=workspace_id,
                            draft_run_id=draft_run_id,
                            workspace_tree=list(workspace_tree or []),
                            draft_source=draft_source,
                            tool_requests=tool_requests,
                            fallback_targets=target_files,
                            execute_checks=lambda requested_changed_files, mode: self._execute_tool_requested_checks(
                                workspace_id=workspace_id,
                                draft_run_id=draft_run_id,
                                draft_source=draft_source,
                                changed_files=requested_changed_files,
                                scope_mode=scope_mode,
                                mode=mode,
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
                        raise ValueError(f"Composition stage {stage_name} exhausted the tool-request budget without returning operations.")
                    if not isinstance(raw_operations, list):
                        raise ValueError("Composition did not return operations.")
                    operations = self._sanitize_draft_operations([DraftFileOperation.model_validate(item) for item in raw_operations])
                    invalid = [
                        operation.file_path
                        for operation in operations
                        if operation.file_path not in allowed_targets or (operation.operation in {"create", "replace"} and operation.content is None)
                    ]
                    if invalid:
                        raise ValueError(f"Composition touched files outside the planned scope: {', '.join(invalid[:5])}")
                    self._validate_targeted_operations(stage_name=stage_name, target_files=target_files, operations=operations)
                    return {
                        "assistant_message": str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip(),
                        "operations": operations,
                        "model": payload["model"],
                        "tool_results": list(tool_results_for_attempt),
                    }
            except Exception as exc:
                last_error = exc
                error_text = str(exc)
                if not scope_recovery_used and "outside the planned scope" in error_text.lower():
                    scope_recovery_used = True
                    logger.warning("Retrying %s composition after scope mismatch: %s", stage_name, exc)
                    continue
                if mode_attempt + 1 < len(retry_modes) and self._is_retryable_llm_error(exc):
                    logger.warning("Retrying %s composition with compact recovery context after transient provider failure: %s", stage_name, exc)
                    continue
                break
        assert last_error is not None
        return {"error": f"Composition step failed: {last_error}"}

    def _timed_composition_cluster(self, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        result = self._resolve_composition_edit(**{k: v for k, v in kwargs.items() if k != "cluster_name"})
        if "error" in result:
            return result
        return {**result, "cluster_name": kwargs["cluster_name"], "target_files": kwargs["target_files"], "duration_ms": int((time.perf_counter() - started) * 1000)}

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
