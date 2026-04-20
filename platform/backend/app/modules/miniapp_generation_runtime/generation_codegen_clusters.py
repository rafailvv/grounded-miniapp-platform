from __future__ import annotations

import logging
import json
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
    _HELPER_DISCOVERY_TARGETS = {
        "miniapp/app/static/shared/runtime.js",
        "miniapp/app/static/shared/api.js",
        "miniapp/app/static/preview_bridge.js",
    }
    _HELPER_DISCOVERY_PATTERNS = {"api", "runtime", "miniappapifetch", "preview_bridge"}

    @staticmethod
    def _is_retryable_empty_cluster_diagnosis(diagnosis: str) -> bool:
        normalized = str(diagnosis or "").strip().lower()
        if not normalized:
            return False
        return any(
            marker in normalized
            for marker in (
                "tool use was blocked",
                "unable to comply",
                "cannot comply",
                "could not comply",
                "request was blocked",
            )
        )

    @staticmethod
    def _is_existing_content_noop_diagnosis(diagnosis: str, outcome_hint: str) -> bool:
        normalized = str(diagnosis or "").strip().lower()
        normalized_outcome = str(outcome_hint or "").strip().lower()
        if not normalized:
            return False
        if normalized_outcome not in {"fatal_invalid_response", "no_progress", "patch_ready"}:
            return False
        return any(
            marker in normalized
            for marker in (
                "existing file contents",
                "no new modifications made",
                "already matches the requested workspace state",
                "already matches the requested state",
                "no modifications made",
                "current files already satisfy",
            )
        )

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
                "These files were already read and are available in file_contexts or supporting_file_contexts. "
                "Use the existing context to return operations or outcome=no_progress instead of requesting the same files again."
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
                "- The files you asked to read are already present in file_contexts or supporting_file_contexts.\n"
                "- Do not request read_files again for already provided files.\n"
                "- Use the current context to return create/replace operations for the allowed targets now.\n"
                "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
            )
        rendered_targets = "\n".join(f"- {target}" for target in distinct_targets[:12])
        return (
            "Context reuse recovery mode:\n"
            "- The files you asked to read are already present in file_contexts or supporting_file_contexts.\n"
            "- These already-available files are:\n"
            f"{rendered_targets}\n"
            "- Do not request read_files again for these files.\n"
            "- Use the current context to return create/replace operations for the allowed targets now.\n"
            "- If the current context still is not enough, return outcome=no_progress with a short diagnosis instead of repeating the same tool request.\n"
        )

    @staticmethod
    def _should_fail_fast_on_satisfied_request(
        *,
        cluster_name: str,
        tool_round: int,
        tool_requests: list[dict[str, Any]],
    ) -> bool:
        if tool_round >= 1:
            return True
        if cluster_name.startswith("role_") and "_ui_" in cluster_name:
            return False
        if len(tool_requests) != 1:
            return False
        request = tool_requests[0]
        tool_name = str(request.get("tool") or "").strip().lower()
        targets = [
            str(target or "").strip().lstrip("./")
            for target in list(request.get("targets") or [])
            if str(target or "").strip()
        ]
        return tool_name == "read_files" and len(targets) <= 1

    @classmethod
    def _is_runtime_helper_discovery_request(cls, *, cluster_name: str, tool_requests: list[dict[str, Any]]) -> bool:
        if not tool_requests or not (cluster_name.startswith("role_") and "_ui_" in cluster_name):
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
                "Use the current file_contexts and supporting_file_contexts to emit operations now."
            ),
        }

    @staticmethod
    def _nonessential_backend_support_feedback(tool_requests: list[dict[str, Any]]) -> dict[str, object]:
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
                "Do not use list_files or pre-patch run_checks for backend_support. "
                "This cluster already has enough template/runtime context to emit db.py, schemas.py, profiles.py, "
                "and runtime manifest operations directly. Generated manifests are derived artifacts, not prerequisites."
            ),
        }

    @staticmethod
    def _is_nonessential_backend_support_request(
        *,
        cluster_name: str,
        tool_requests: list[dict[str, Any]],
    ) -> bool:
        if cluster_name != "backend_support" or not tool_requests:
            return False
        for item in tool_requests:
            tool_name = str(item.get("tool") or "").strip().lower()
            targets = [
                str(target or "").strip().lstrip("./")
                for target in list(item.get("targets") or [])
                if str(target or "").strip()
            ]
            if tool_name == "list_files":
                continue
            if tool_name == "run_checks":
                if not targets:
                    return True
                if all(
                    target.startswith("miniapp/app/generated/")
                    or target.startswith("miniapp/app/routes/")
                    or target in {"miniapp/app/db.py", "miniapp/app/schemas.py"}
                    for target in targets
                ):
                    continue
            return False
        return True

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

    def _preload_existing_target_contexts(
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

    def _preload_existing_supporting_contexts(
        self,
        *,
        cluster_name: str,
        workspace_id: str | None,
        draft_run_id: str | None,
        file_contexts: dict[str, str],
    ) -> dict[str, str]:
        current = dict(file_contexts)
        if not (workspace_id and draft_run_id):
            return current
        support_targets: list[str] = []
        if cluster_name == "backend_support":
            support_targets = [
                "miniapp/app/routes/runtime.py",
                "miniapp/app/routes/client.py",
                "miniapp/app/routes/specialist.py",
                "miniapp/app/routes/manager.py",
                "miniapp/app/generated/route_manifest.json",
            ]
        elif cluster_name.startswith("backend_route_"):
            support_targets = [
                "miniapp/app/main.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/routes/runtime.py",
                "miniapp/app/routes/profiles.py",
                "miniapp/app/routes/client.py",
                "miniapp/app/routes/specialist.py",
                "miniapp/app/routes/manager.py",
                "miniapp/app/generated/route_manifest.json",
            ]
        if not support_targets:
            return current
        for target in support_targets:
            if str(current.get(target) or "").strip():
                continue
            content = self.service.workspace_service.try_read_text_file(workspace_id, target, run_id=draft_run_id)
            if content is not None:
                current[target] = content
                continue
            if self.service._is_canonical_target_path(target):
                current[target] = GenerationRepairToolRuntime._missing_file_context(target)
        return current

    def _resolve_whole_file_cluster(
        self,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        entity_contract: dict[str, Any],
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
        compact_recovery_used = False
        context_reuse_recovery_note = ""
        last_error: Exception | None = None
        tool_runtime = GenerationRepairToolRuntime(self.service)
        for _ in range(3):
            current_file_contexts = self._preload_existing_target_contexts(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                targets=cluster_targets,
                file_contexts=file_contexts,
            )
            current_file_contexts = self._preload_existing_supporting_contexts(
                cluster_name=cluster_name,
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                file_contexts=current_file_contexts,
            )
            tool_results_for_attempt: list[dict[str, object]] = []
            seen_tool_request_signatures: set[str] = set()
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._whole_file_cluster_system_prompt(cluster_name)
                    user_prompt = self._whole_file_cluster_user_prompt(
                        cluster_name=cluster_name,
                        cluster_targets=cluster_targets,
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        entity_contract=entity_contract,
                        role_scope=role_scope,
                        role_contract=role_contract,
                        page_graph=page_graph,
                        scope_mode=scope_mode,
                        intent=intent,
                        file_contexts=current_file_contexts,
                        generation_mode=GenerationMode.FAST if compact_recovery_used else generation_mode,
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
                    if compact_recovery_used:
                        compact_note = (
                            "Compact recovery mode:\n"
                            "- The previous attempt returned no operations because the response drifted into an empty blocked/no_progress state.\n"
                            "- Keep the answer concise and emit create/replace operations for the allowed cluster_targets directly.\n"
                            "- Do not request tools unless a concrete missing file path is truly required for correctness.\n"
                            "- Prefer the simplest self-contained role page or route implementation that satisfies the current cluster scope."
                        )
                        system_prompt = f"{system_prompt.rstrip()}\n\n{compact_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{compact_note}".strip()
                    if context_reuse_recovery_note:
                        system_prompt = f"{system_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
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
                    diagnosis = str(normalized.get("diagnosis") or normalized.get("assistant_message") or "").strip()
                    if outcome_hint == "tool_request" or tool_requests:
                        if self._is_nonessential_backend_support_request(
                            cluster_name=cluster_name,
                            tool_requests=tool_requests,
                        ):
                            context_reuse_recovery_note = (
                                "Backend support recovery mode:\n"
                                "- Do not use list_files or pre-patch run_checks for backend_support.\n"
                                "- db.py, schemas.py, profiles.py, and generated manifests are owned by this cluster.\n"
                                "- Route files and generated manifests are reference-only supporting context.\n"
                                "- Emit operations for the allowed backend_support targets now.\n"
                            )
                            tool_results_for_attempt.append(
                                self._nonessential_backend_support_feedback(tool_requests)
                            )
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Whole-file cluster {cluster_name} kept requesting nonessential backend-support tool actions instead of emitting operations."
                            )
                        if self._is_runtime_helper_discovery_request(cluster_name=cluster_name, tool_requests=tool_requests):
                            context_reuse_recovery_note = (
                                "Runtime helper recovery mode:\n"
                                "- Do not search for static/shared/runtime.js or static/shared/api.js.\n"
                                "- The canonical bridge is /static/preview_bridge.js and it already exposes window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role).\n"
                                "- If existing supporting examples use same-origin fetch('/api/...'), that is also valid for this cluster.\n"
                                "- Use the current supporting_file_contexts and emit operations now.\n"
                            )
                            tool_results_for_attempt.append(self._runtime_helper_discovery_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Whole-file cluster {cluster_name} kept searching for nonexistent runtime/api helper files instead of using the current context."
                            )
                        if self._read_request_already_satisfied(tool_requests, current_file_contexts):
                            context_reuse_recovery_note = self._already_available_context_note(tool_requests)
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if not self._should_fail_fast_on_satisfied_request(
                                cluster_name=cluster_name,
                                tool_round=tool_round,
                                tool_requests=tool_requests,
                            ) and tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Whole-file cluster {cluster_name} requested files that were already present in the current context."
                            )
                        request_signature = self._tool_request_signature(tool_requests)
                        if request_signature and request_signature in seen_tool_request_signatures:
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Whole-file cluster {cluster_name} repeated identical tool requests without returning operations."
                            )
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
                        raise ValueError(f"Whole-file cluster {cluster_name} exhausted the tool-request budget without returning operations.")
                    if (
                        outcome_hint == "no_progress"
                        and not tool_requests
                        and not raw_operations
                        and self._is_retryable_empty_cluster_diagnosis(diagnosis)
                    ):
                        if not compact_recovery_used:
                            compact_recovery_used = True
                            tool_results_for_attempt.append(
                                {
                                    "tool": "cluster_recovery_feedback",
                                    "targets": list(cluster_targets),
                                    "error": (
                                        "The previous response returned no operations with a blocked/no_progress diagnosis. "
                                        "Retry in compact recovery mode and emit operations for the current cluster targets directly."
                                    ),
                                }
                            )
                            continue
                        raise ValueError(
                            f"Whole-file cluster {cluster_name} returned repeated blocked/no_progress responses without file operations."
                        )
                    if not isinstance(raw_operations, list):
                        raise ValueError("Whole-file cluster did not return operations.")
                    operations = self._sanitize_draft_operations([DraftFileOperation.model_validate(item) for item in raw_operations])
                    if not operations:
                        if all(str(current_file_contexts.get(path) or "").strip() for path in cluster_targets) and (
                            intent != "create" or self._is_existing_content_noop_diagnosis(diagnosis, outcome_hint)
                        ):
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
        entity_contract: dict[str, Any],
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
        context_reuse_recovery_note = ""
        tool_runtime = GenerationRepairToolRuntime(self.service)
        for mode_attempt, prompt_mode in enumerate(retry_modes):
            current_file_contexts = self._preload_existing_target_contexts(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                targets=target_files,
                file_contexts=file_contexts,
            )
            tool_results_for_attempt: list[dict[str, object]] = []
            seen_tool_request_signatures: set[str] = set()
            try:
                for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                    system_prompt = self._composition_system_prompt(stage_name)
                    user_prompt = self._composition_user_prompt(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        entity_contract=entity_contract,
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
                    if context_reuse_recovery_note:
                        system_prompt = f"{system_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
                        user_prompt = f"{user_prompt.rstrip()}\n\n{context_reuse_recovery_note}".strip()
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
                        if self._read_request_already_satisfied(tool_requests, current_file_contexts):
                            context_reuse_recovery_note = self._already_available_context_note(tool_requests)
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if not self._should_fail_fast_on_satisfied_request(
                                cluster_name=stage_name,
                                tool_round=tool_round,
                                tool_requests=tool_requests,
                            ) and tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Composition stage {stage_name} requested files that were already present in the current context."
                            )
                        request_signature = self._tool_request_signature(tool_requests)
                        if request_signature and request_signature in seen_tool_request_signatures:
                            tool_results_for_attempt.append(self._duplicate_tool_request_feedback(tool_requests))
                            if tool_round < self.MAX_TOOL_ROUNDS:
                                continue
                            raise ValueError(
                                f"Composition stage {stage_name} repeated identical tool requests without returning operations."
                            )
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
