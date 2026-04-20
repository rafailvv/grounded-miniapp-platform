from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.models.artifacts import MaterializationReport, ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation, RunCheckResult
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_generation_runtime import MiniappGenerationPlanRuntime
from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.modules.miniapp_validation import PageGraphValidation


class ServiceContractMaterializationMixins:
    @staticmethod
    def _detect_missing_backend_contract_targets(*, generated_page_sources: dict[str, str], current_target_files: list[str], backend_targets: list[str], entity_contract: dict[str, Any] | None = None) -> list[str]:
        endpoint_names: set[str] = set()
        for source in generated_page_sources.values():
            if not isinstance(source, str):
                continue
            for match in re.finditer(r"[\"'`](/api/([a-zA-Z0-9_-]+))([/'\"`?]|$)", source):
                endpoint_names.add(match.group(2))
        if not endpoint_names:
            return []
        existing_targets = set(current_target_files) | set(backend_targets)
        inferred: list[str] = []
        router_path = "miniapp/app/main.py"
        for contract_path in ("miniapp/app/db.py", "miniapp/app/schemas.py"):
            if contract_path not in existing_targets:
                inferred.append(contract_path)
        for endpoint_name in sorted(endpoint_names):
            normalized_endpoint_name = MiniappGenerationPlanRuntime.normalize_endpoint_name_for_entity_contract(
                endpoint_name,
                entity_contract=entity_contract,
            )
            if ServiceContractMaterializationMixins._is_forbidden_endpoint_name(normalized_endpoint_name):
                continue
            inferred_path = ServiceContractMaterializationMixins._route_module_path_for_endpoint_name(normalized_endpoint_name)
            if inferred_path not in existing_targets:
                inferred.append(inferred_path)
            if router_path not in existing_targets:
                inferred.append(router_path)
        entity_route_file = MiniappGenerationPlanRuntime.entity_route_file(entity_contract)
        if entity_route_file and entity_route_file not in existing_targets and endpoint_names:
            inferred.append(entity_route_file)
        return list(dict.fromkeys(inferred))

    @staticmethod
    def _detect_missing_static_asset_targets(*, generated_page_sources: dict[str, str], current_target_files: list[str], page_graph: dict[str, Any] | None = None) -> list[str]:
        from app.modules.miniapp_generation_runtime import MiniappGenerationTargeting
        return MiniappGenerationTargeting._detect_missing_static_asset_targets(generated_page_sources=generated_page_sources, current_target_files=current_target_files, page_graph=page_graph)

    @staticmethod
    def _extract_static_asset_targets(content: str, *, source_path: str) -> list[str]:
        from app.modules.miniapp_generation_runtime import MiniappGenerationTargeting
        return MiniappGenerationTargeting._extract_static_asset_targets(content, source_path=source_path)

    @staticmethod
    def _resolve_static_asset_target(raw_ref: str, *, source_path: str) -> str | None:
        from app.modules.miniapp_generation_runtime import MiniappGenerationTargeting
        return MiniappGenerationTargeting._resolve_static_asset_target(raw_ref, source_path=source_path)

    @staticmethod
    def _detect_missing_backend_contract_targets_from_page_graph(*, page_graph: dict[str, Any], current_target_files: list[str], backend_targets: list[str], entity_contract: dict[str, Any] | None = None) -> list[str]:
        return MiniappGenerationPlanRuntime.detect_missing_backend_contract_targets_from_page_graph(page_graph=page_graph, current_target_files=current_target_files, backend_targets=backend_targets, entity_contract=entity_contract)

    @staticmethod
    def _detect_missing_backend_contract_targets_from_spec(*, grounded_spec: GroundedSpecModel, page_graph: dict[str, Any], current_target_files: list[str], backend_targets: list[str], entity_contract: dict[str, Any] | None = None) -> list[str]:
        return MiniappGenerationPlanRuntime.detect_missing_backend_contract_targets_from_spec(grounded_spec=grounded_spec, page_graph=page_graph, current_target_files=current_target_files, backend_targets=backend_targets, entity_contract=entity_contract)

    @staticmethod
    def _endpoint_names_from_dependency_text(dependency: str) -> set[str]:
        return MiniappGenerationPlanRuntime.endpoint_names_from_dependency_text(dependency)

    @staticmethod
    def _dedupe_operations(operations: list[DraftFileOperation]) -> list[DraftFileOperation]:
        deduped: dict[str, DraftFileOperation] = {}
        for operation in operations:
            deduped[operation.file_path] = operation
        return list(deduped.values())

    @classmethod
    def _route_module_path_for_endpoint_name(cls, endpoint_name: str) -> str:
        return MiniappGenerationPlanRuntime.route_module_path_for_endpoint_name(endpoint_name)

    @classmethod
    def _is_forbidden_endpoint_name(cls, endpoint_name: str) -> bool:
        return MiniappGenerationPlanRuntime.is_forbidden_endpoint_name(endpoint_name)

    @classmethod
    def _canonical_endpoint_name(cls, endpoint_name: str) -> str:
        return MiniappGenerationPlanRuntime.canonical_endpoint_name(endpoint_name)

    @staticmethod
    def _page_graph_gate_issues(page_graph: dict[str, Any], role_scope: list[str], *, scope_mode: str, require_multi_page: bool) -> list[str]:
        from app.services.miniapp_generation.service import GenerationService
        return PageGraphValidation.page_graph_gate_issues(page_graph, role_scope, scope_mode=scope_mode, require_multi_page=require_multi_page, normalize_role_route_path=lambda role, route_path, index: GenerationService._normalize_role_route_path(role, route_path, index=index), is_business_page=GenerationService._is_business_page, is_canonical_target_path=GenerationService._is_canonical_target_path)

    @classmethod
    def _edit_gate_issues(
        cls,
        page_graph: dict[str, Any],
        operations: list[DraftFileOperation],
        role_scope: list[str],
        *,
        scope_mode: str,
        target_files: list[str],
    ) -> list[str]:
        from app.modules.miniapp_validation import GenerationEditGate
        from app.services.miniapp_generation.service import GenerationService

        return GenerationEditGate.edit_gate_issues(
            page_graph,
            operations,
            role_scope,
            scope_mode=scope_mode,
            target_files=target_files,
            is_canonical_target_path=GenerationService._is_canonical_target_path,
            is_business_page=GenerationService._is_business_page,
            is_role_root_page=GenerationService._is_role_root_page,
        )

    def _preflight_generation_issues(self, *, draft_root: Path, changed_files: list[str], page_graph: dict[str, Any], role_scope: list[str], **_: Any) -> list[ValidationIssue]:
        return self.generation_preflight_validation.preflight_generation_issues(draft_root=draft_root, changed_files=changed_files, page_graph=page_graph, role_scope=role_scope, normalize_local_route_ref=self._normalize_local_route_ref)

    @classmethod
    def _python_app_level_test_content(cls, *, page_graph: dict[str, Any], role_scope: list[str], entity_contract: dict[str, Any] | None = None) -> str:
        return cls._artifact_builder().python_app_level_test_content(page_graph=page_graph, role_scope=role_scope, entity_contract=entity_contract)

    @classmethod
    def _js_app_level_test_content(cls, *, page_graph: dict[str, Any], role_scope: list[str]) -> str:
        return cls._artifact_builder().js_app_level_test_content(page_graph=page_graph, role_scope=role_scope)

    @staticmethod
    def _route_manifest_from_page_graph(page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        from app.services.miniapp_generation.service import GenerationService
        return GenerationService._artifact_builder().route_manifest_from_page_graph(page_graph, role_scope)

    @staticmethod
    def _runtime_manifest_from_page_graph(route_manifest: dict[str, Any], grounded_spec: GroundedSpecModel, generation_mode: GenerationMode) -> dict[str, Any]:
        from app.services.miniapp_generation.service import GenerationService
        return GenerationService._artifact_builder().runtime_manifest_from_page_graph(route_manifest, grounded_spec, generation_mode)

    @staticmethod
    def _normalize_runtime_python_path(path: str) -> str:
        return MiniappMaterializationService.normalize_runtime_python_path(path)

    @classmethod
    def _normalize_runtime_python_paths_in_plan(cls, plan_result: dict[str, Any]) -> None:
        MiniappMaterializationService.normalize_runtime_python_paths_in_plan(plan_result)

    @classmethod
    def _expand_page_asset_targets_in_plan(cls, plan_result: dict[str, Any]) -> None:
        cls._materialization_helpers().expand_page_asset_targets_in_plan(plan_result)

    @classmethod
    def _normalize_runtime_python_paths_in_structure(cls, value: Any) -> Any:
        return MiniappMaterializationService.normalize_runtime_python_paths_in_structure(value)

    def _realized_draft_file_paths(self, workspace_id: str, run_id: str) -> set[str]:
        return self.materialization_service.realized_draft_file_paths(workspace_id, run_id)

    @staticmethod
    def _missing_required_cluster_targets(*, cluster_targets: list[str], operations: list[DraftFileOperation], file_contexts: dict[str, str]) -> list[str]:
        return MiniappMaterializationService.missing_required_cluster_targets(cluster_targets=cluster_targets, operations=operations, file_contexts=file_contexts)

    @staticmethod
    def _materialization_gate_result(report: MaterializationReport, *, require_multi_page: bool, scope_mode: str, generation_mode: GenerationMode) -> tuple[str, list[str]] | None:
        return MiniappMaterializationService.materialization_gate_result(report, require_multi_page=require_multi_page, scope_mode=scope_mode, generation_mode=generation_mode)

    @staticmethod
    def _build_check_results(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None = None) -> list[RunCheckResult]:
        return MiniappMaterializationService.build_check_results(build_issues, preview_issue)

    @staticmethod
    def _filter_non_blocking_build_issues(build_issues: list[ValidationIssue], *, scope_mode: str) -> list[ValidationIssue]:
        return MiniappMaterializationService.filter_non_blocking_build_issues(build_issues, scope_mode=scope_mode)

    @staticmethod
    def _repair_attempt_limit(generation_mode: GenerationMode, intent: str) -> int:
        return MiniappMaterializationService.repair_attempt_limit(generation_mode, intent)
