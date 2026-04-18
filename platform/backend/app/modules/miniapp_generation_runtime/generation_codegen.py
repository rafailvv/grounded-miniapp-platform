from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class MiniappGenerationCodegen(MiniappGenerationRuntimeOwner):
    def _resolve_code_edits(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        file_contexts: dict[str, str],
        target_files: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        intent: str,
        scope_mode: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if scope_mode == "whole_file_build":
            return self._resolve_whole_file_code_edits(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                prompt=prompt,
                grounded_spec=grounded_spec,
                role_scope=role_scope,
                file_contexts=file_contexts,
                target_files=target_files,
                role_contract=role_contract,
                page_graph=page_graph,
                intent=intent,
                scope_mode=scope_mode,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
        target_set = set(target_files)
        page_operations: list[DraftFileOperation] = []
        page_messages: list[str] = []
        generated_page_sources: dict[str, str] = {}
        generated_backend_sources: dict[str, str] = {}
        trace_payloads: dict[str, dict[str, Any]] = {}
        latency_breakdown: dict[str, int] = {}
        selected_pages = self._selected_pages_for_edit(page_graph, target_set)
        if len(selected_pages) <= 1:
            ordered_page_results = [
                self._resolve_page_file_edit(
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    role=role,
                    page=page,
                    page_graph=page_graph,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    intent=intent,
                    file_contexts=file_contexts,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                )
                for role, page in selected_pages
            ]
        else:
            ordered_page_results = asyncio.run(
                self._resolve_page_file_edits_async(
                    selected_pages=selected_pages,
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    page_graph=page_graph,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    intent=intent,
                    file_contexts=file_contexts,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                )
            )
            if any("error" in result and (result.get("retryable") or self._is_recoverable_page_error_message(str(result.get("error") or ""))) for result in ordered_page_results):
                for index, page_result in enumerate(ordered_page_results):
                    if "error" not in page_result:
                        continue
                    if not (page_result.get("retryable") or self._is_recoverable_page_error_message(str(page_result.get("error") or ""))):
                        continue
                    role, page = selected_pages[index]
                    ordered_page_results[index] = self._resolve_page_file_edit(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        role=role,
                        page=page,
                        page_graph=page_graph,
                        role_contract=role_contract,
                        scope_mode=scope_mode,
                        intent=intent,
                        file_contexts=file_contexts,
                        generation_mode=GenerationMode.FAST,
                        creative_direction=creative_direction,
                        recovery_mode="serial_compact_retry",
                    )
        for page_result in ordered_page_results:
            if "error" in page_result:
                return page_result
            raw_operations = list(page_result.get("operations") or [])
            if not raw_operations and page_result.get("operation") is not None:
                raw_operations = [page_result["operation"]]
            for raw_operation in raw_operations:
                operation = raw_operation if isinstance(raw_operation, DraftFileOperation) else DraftFileOperation.model_validate(raw_operation)
                page_operations.append(operation)
                if operation.content is not None:
                    generated_page_sources[operation.file_path] = operation.content
            page_messages.append(str(page_result.get("assistant_message") or "").strip())
        effective_target_files = list(target_files)
        backend_targets = self._backend_composition_targets(target_files, selected_pages)
        backend_contract_gap_targets = self._detect_missing_backend_contract_targets(
            generated_page_sources=generated_page_sources,
            current_target_files=effective_target_files,
            backend_targets=backend_targets,
        )
        static_contract_gap_targets = self._detect_missing_static_asset_targets(
            generated_page_sources=generated_page_sources,
            current_target_files=effective_target_files,
            page_graph=page_graph,
        )
        contract_gap_targets = list(dict.fromkeys([*backend_contract_gap_targets, *static_contract_gap_targets]))
        if contract_gap_targets:
            effective_target_files = list(dict.fromkeys([*effective_target_files, *contract_gap_targets]))
            backend_targets = list(dict.fromkeys([*backend_targets, *backend_contract_gap_targets]))
            for file_path in contract_gap_targets:
                if file_path in file_contexts:
                    continue
                try:
                    content = self.workspace_service.try_read_text_file(workspace_id, file_path, run_id=draft_run_id)
                except FileNotFoundError:
                    continue
                if content is not None:
                    file_contexts[file_path] = content
        composition_clusters: list[tuple[str, str, list[str]]] = []
        if backend_targets:
            composition_clusters.append(("composition_backend", "miniapp", backend_targets))
        if composition_clusters:
            async def resolve_clusters() -> list[dict[str, Any]]:
                tasks = [
                    asyncio.to_thread(
                        self._timed_composition_cluster,
                        cluster_name=cluster_name,
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        role_scope=role_scope,
                        role_contract=role_contract,
                        page_graph=page_graph,
                        scope_mode=scope_mode,
                        intent=intent,
                        stage_name=stage_name,
                        target_files=cluster_targets,
                        file_contexts=file_contexts,
                        generated_page_sources=generated_page_sources,
                        generated_support_sources={},
                        generation_mode=generation_mode,
                        creative_direction=creative_direction,
                    )
                    for cluster_name, stage_name, cluster_targets in composition_clusters
                ]
                return await asyncio.gather(*tasks)
            composition_results = asyncio.run(resolve_clusters())
        else:
            composition_results = []
        for result in composition_results:
            if "error" in result:
                return result
            cluster_name = str(result["cluster_name"])
            duration_ms = int(result["duration_ms"])
            latency_breakdown[cluster_name] = duration_ms
            trace_payloads[cluster_name] = {
                "message": f"{cluster_name.replace('_', ' ').capitalize()} completed.",
                "payload": {
                    "duration_ms": duration_ms,
                    "target_files": result["target_files"],
                    "operation_count": len(result["operations"]),
                },
            }
            if cluster_name == "composition_backend":
                for operation in result["operations"]:
                    if operation.content is not None:
                        generated_backend_sources[operation.file_path] = operation.content
            if str(result.get("assistant_message") or "").strip():
                page_messages.append(str(result["assistant_message"]).strip())
        operations = self._dedupe_operations(
            [
                DraftFileOperation(file_path="artifacts/generated_app_graph.json", operation="replace", content=json_dumps(page_graph), reason="Persist the LLM-generated page graph for validation, preview, and run artifacts."),
                DraftFileOperation(file_path="artifacts/page_graph_verification.json", operation="replace", content=json_dumps(self._build_page_graph_verification_report(page_graph, role_scope)), reason="Persist structural verification for the planned page graph and route tree."),
                *page_operations,
                *[operation for result in composition_results for operation in result["operations"]],
            ]
        )
        assistant_parts = [message for message in page_messages if message]
        assistant_message = " ".join(assistant_parts).strip() or f"Generated {len(page_operations)} page files and composed the miniapp for a {scope_mode} run."
        if "composition_backend" in latency_breakdown:
            latency_breakdown["composition_backend_ms"] = latency_breakdown["composition_backend"]
        return {
            "assistant_message": assistant_message,
            "operations": operations,
            "planner_contract_gap_targets": contract_gap_targets,
            "effective_target_files": effective_target_files,
            "effective_backend_targets": backend_targets,
            "latency_breakdown": latency_breakdown,
            "trace_payloads": trace_payloads,
        }

    def _resolve_whole_file_code_edits(self, **kwargs: Any) -> dict[str, Any]:
        target_files = kwargs["target_files"]
        clusters = self._build_generation_clusters(target_files)
        if not clusters:
            return {"error": "Whole-file generation requires at least one canonical target file."}
        total_target_files = max(1, sum(len(list(cluster["target_files"])) for cluster in clusters))
        completed_target_files = 0
        results: list[dict[str, Any]] = []
        for batch in self._group_generation_clusters_for_execution(clusters):
            self._sync_generation_batch_started(
                linked_run_id=kwargs["draft_run_id"],
                completed_target_files=completed_target_files,
                total_target_files=total_target_files,
                batch=batch,
            )
            executor = ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix=f"whole-file-batch-{self._whole_file_parallel_group(str(batch[0]['cluster_name']))}")
            future_map: dict[Any, tuple[str, int]] = {}
            for cluster in batch:
                cluster_name = str(cluster["cluster_name"])
                cluster_targets = list(cluster["target_files"])
                cluster_target_count = len(cluster_targets)
                logger.info("whole_file_cluster_started workspace_id=%s draft_run_id=%s cluster=%s targets=%s", kwargs["workspace_id"], kwargs["draft_run_id"], cluster_name, cluster_target_count)
                future = self._submit_with_context(
                    executor,
                    self._timed_whole_file_cluster,
                    cluster_name=cluster_name,
                    cluster_targets=cluster_targets,
                    prompt=kwargs["prompt"],
                    grounded_spec=kwargs["grounded_spec"],
                    role_scope=kwargs["role_scope"],
                    role_contract=kwargs["role_contract"],
                    page_graph=kwargs["page_graph"],
                    scope_mode=kwargs["scope_mode"],
                    intent=kwargs["intent"],
                    file_contexts=kwargs["file_contexts"],
                    generation_mode=kwargs["generation_mode"],
                    creative_direction=kwargs["creative_direction"],
                )
                future_map[future] = (cluster_name, cluster_target_count)
            done, not_done = wait(future_map.keys(), timeout=self.WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS, return_when=ALL_COMPLETED)
            if not_done:
                executor.shutdown(wait=False, cancel_futures=True)
                pending_names = ", ".join(future_map[future][0] for future in not_done)
                return {"error": f"Whole-file generation timed out while waiting for cluster result: {pending_names}"}
            try:
                for future in done:
                    cluster_name, cluster_target_count = future_map[future]
                    cluster_result = future.result()
                    if "error" in cluster_result:
                        return {"error": str(cluster_result["error"])}
                    cluster_operations = [DraftFileOperation.model_validate(item) for item in cluster_result.get("operations", [])]
                    if cluster_operations:
                        self.workspace_service.apply_draft_operations(kwargs["workspace_id"], kwargs["draft_run_id"], self._dedupe_operations(cluster_operations))
                        self.workspace_log_service.append(
                            kwargs["workspace_id"],
                            source="generation.cluster_persisted",
                            message="Persisted completed code cluster into the draft workspace.",
                            payload={"draft_run_id": kwargs["draft_run_id"], "cluster_name": cluster_name, "file_paths": [operation.file_path for operation in cluster_operations]},
                        )
                    logger.info("whole_file_cluster_completed workspace_id=%s draft_run_id=%s cluster=%s duration_ms=%s", kwargs["workspace_id"], kwargs["draft_run_id"], cluster_name, cluster_result.get("duration_ms"))
                    results.append(cluster_result)
                    completed_target_files += cluster_target_count
                    self._sync_generation_cluster_progress(
                        linked_run_id=kwargs["draft_run_id"],
                        completed_target_files=completed_target_files,
                        total_target_files=total_target_files,
                        cluster_name=cluster_name,
                    )
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                return {"error": f"Whole-file cluster failed: {exc}"}
            else:
                executor.shutdown(wait=False, cancel_futures=False)
        operations: list[DraftFileOperation] = [
            DraftFileOperation(file_path="artifacts/generated_app_graph.json", operation="replace", content=json_dumps(kwargs["page_graph"]), reason="Persist the planned page graph for validation, preview, and run artifacts."),
            DraftFileOperation(file_path="artifacts/page_graph_verification.json", operation="replace", content=json_dumps(self._build_page_graph_verification_report(kwargs["page_graph"], kwargs["role_scope"])), reason="Persist structural verification for the planned page graph and route tree."),
        ]
        messages: list[str] = []
        latency_breakdown: dict[str, int] = {}
        trace_payloads: dict[str, dict[str, Any]] = {}
        for result in results:
            if "error" in result:
                return result
            operations.extend(result["operations"])
            if str(result.get("assistant_message") or "").strip():
                messages.append(str(result["assistant_message"]).strip())
            latency_breakdown[result["cluster_name"]] = int(result["duration_ms"])
            trace_payloads[result["cluster_name"]] = {
                "message": f"{result['cluster_name'].replace('_', ' ').capitalize()} completed.",
                "payload": {"duration_ms": result["duration_ms"], "target_files": result["target_files"], "operation_count": len(result["operations"]), "write_strategy": "whole_file_build"},
            }
        if any(key.startswith("frontend_") for key in latency_breakdown):
            latency_breakdown["whole_file_frontend_ms"] = sum(value for key, value in latency_breakdown.items() if key.startswith("frontend_"))
        if "backend_core" in latency_breakdown:
            latency_breakdown["whole_file_backend_ms"] = latency_breakdown["backend_core"]
        return {
            "assistant_message": " ".join(messages).strip() or f"Generated {len(target_files)} files using whole-file bundle generation.",
            "operations": self._dedupe_operations(operations),
            "planner_contract_gap_targets": [],
            "effective_target_files": list(target_files),
            "latency_breakdown": latency_breakdown,
            "trace_payloads": trace_payloads,
        }

    async def _resolve_page_file_edits_async(self, **kwargs: Any) -> list[dict[str, Any]]:
        selected_pages = kwargs["selected_pages"]
        semaphore = asyncio.Semaphore(min(self._page_edit_parallelism(scope_mode=kwargs["scope_mode"], generation_mode=kwargs["generation_mode"]), len(selected_pages)))

        async def run_one(role: str, page: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await asyncio.to_thread(
                    self._resolve_page_file_edit,
                    prompt=kwargs["prompt"],
                    grounded_spec=kwargs["grounded_spec"],
                    role=role,
                    page=page,
                    page_graph=kwargs["page_graph"],
                    role_contract=kwargs["role_contract"],
                    scope_mode=kwargs["scope_mode"],
                    intent=kwargs["intent"],
                    file_contexts=kwargs["file_contexts"],
                    generation_mode=kwargs["generation_mode"],
                    creative_direction=kwargs["creative_direction"],
                )
        return list(await asyncio.gather(*[run_one(role, page) for role, page in selected_pages]))
