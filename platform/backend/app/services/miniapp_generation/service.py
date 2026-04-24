from __future__ import annotations

from contextvars import ContextVar
import os
import time
from pathlib import Path
from typing import Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.ai.model_registry import resolve_model_profile
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import ChatTurnRecord, GenerateRequest, JobRecord
from app.models.grounded_spec import GroundedSpecModel
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.miniapp_generation.artifact_builder import MiniappArtifactBuilder
from app.services.miniapp_generation.runtime_dispatch import RuntimeDispatchMixin, RuntimeOwnerMeta
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES, ROLE_ORDER
from app.services.miniapp_generation.service_contract_materialization_mixins import ServiceContractMaterializationMixins
from app.services.miniapp_generation.service_llm_grounded_misc_mixins import ServiceLlmGroundedMiscMixins
from app.services.miniapp_generation.service_page_defaults_mixins import ServicePageDefaultsMixins
from app.services.miniapp_generation.service_prompt_codegen_mixins import ServicePromptCodegenMixins
from app.services.miniapp_generation.service_repair_reporting_mixins import ServiceRepairReportingMixins
from app.services.miniapp_generation.service_strategy_mixins import ServiceStrategyMixins
from app.services.patch_service import PatchService
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.service import WorkspaceService
from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync
from app.modules.miniapp_generation_runtime import (
    GenerationProgressReportingRuntime,
    GroundedSpecOrchestrationRuntime,
    GroundedSpecPayloadsRuntime,
    GroundedSpecPromptsRuntime,
    MiniappGenerationCompletion,
    MiniappGenerationContractCritic,
    MiniappGenerationContractFrontend,
    MiniappGenerationContractPass,
    MiniappGenerationContractRoutes,
    MiniappGenerationContractSchema,
    MiniappGenerationCodePlan,
    MiniappGenerationCodePlanNormalization,
    MiniappGenerationCodePlanPrompts,
    MiniappGenerationCodegen,
    MiniappGenerationCodegenClusters,
    MiniappGenerationCodegenPrompts,
    MiniappGenerationCodegenSelection,
    MiniappGenerationEntityContract,
    MiniappGenerationEntry,
    MiniappGenerationNormalLoop,
    MiniappGenerationPageGraphRuntime,
    MiniappGenerationPaths,
    MiniappGenerationPlanRuntime,
    MiniappGenerationReporting,
    MiniappGenerationReportingCompaction,
    MiniappGenerationReportingRepair,
    MiniappGenerationResume,
    MiniappGenerationRoleContract,
    MiniappGenerationTargeting,
    MiniappGenerationRepair,
    MiniappGenerationShellContract,
    MiniappGroundedSpecBuilder,
    compile_prompt_to_scaffold,
    mentions_schedule_or_time,
    scaffold_backend_targets_from_spec,
    scaffold_page_slug_for_route,
    scaffold_role_pages_for_role,
    scaffold_role_responsibility,
    select_creative_direction,
)
from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.modules.miniapp_validation import GenerationEditGate, GenerationPreflightValidation, PageGraphValidation
from app.modules.miniapp_agent_loop.engine import WorkspaceLoopEngine
from app.validators.suite import ValidationSuite

ACTIVE_LLM_CACHE_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("active_llm_cache_context", default=None)
ACTIVE_LLM_CACHE_STATS: ContextVar[dict[str, Any] | None] = ContextVar("active_llm_cache_stats", default=None)
QUALITY_FIDELITY = {
    GenerationMode.FAST: "fast_app",
    GenerationMode.QUALITY: "quality_app",
    GenerationMode.BALANCED: "balanced_app",
    GenerationMode.BASIC: "basic_scaffold",
}


class GenerationService(
    ServiceLlmGroundedMiscMixins,
    ServiceRepairReportingMixins,
    ServiceContractMaterializationMixins,
    ServicePromptCodegenMixins,
    ServiceStrategyMixins,
    ServicePageDefaultsMixins,
    RuntimeDispatchMixin,
    metaclass=RuntimeOwnerMeta,
):
    GROUNDED_SPEC_SECTION_TIMEOUT_SECONDS = int(os.getenv("GROUNDED_SPEC_SECTION_TIMEOUT_SEC", "300"))
    GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS = int(os.getenv("GROUNDED_SPEC_TOTAL_TIMEOUT_SEC", "600"))
    CODE_PLAN_SECTION_TIMEOUT_SECONDS = int(os.getenv("CODE_PLAN_SECTION_TIMEOUT_SEC", "300"))
    CODE_PLAN_TOTAL_TIMEOUT_SECONDS = int(os.getenv("CODE_PLAN_TOTAL_TIMEOUT_SEC", "600"))
    WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS = int(os.getenv("WHOLE_FILE_CLUSTER_TIMEOUT_SEC", "900"))
    WHOLE_FILE_UI_CLUSTER_TIMEOUT_SECONDS = int(os.getenv("WHOLE_FILE_UI_CLUSTER_TIMEOUT_SEC", "1800"))
    STRUCTURED_LLM_TIMEOUT_SECONDS = int(os.getenv("STRUCTURED_LLM_TIMEOUT_SEC", "900"))
    JSON_OBJECT_LLM_TIMEOUT_SECONDS = int(os.getenv("JSON_OBJECT_LLM_TIMEOUT_SEC", "600"))

    @classmethod
    def _runtime_owner_factories(cls) -> dict[str, Any]:
        return {
            "grounded_spec_orchestration": GroundedSpecOrchestrationRuntime,
            "generation_completion": MiniappGenerationCompletion,
            "generation_repair": MiniappGenerationRepair,
            "generation_entry": MiniappGenerationEntry,
            "generation_entity_contract": MiniappGenerationEntityContract,
            "generation_contract_critic": MiniappGenerationContractCritic,
            "generation_normal_loop": MiniappGenerationNormalLoop,
            "generation_resume": MiniappGenerationResume,
            "generation_plan_runtime": MiniappGenerationPlanRuntime,
            "generation_role_contract": MiniappGenerationRoleContract,
            "generation_code_plan": MiniappGenerationCodePlan,
            "generation_code_plan_prompts": MiniappGenerationCodePlanPrompts,
            "generation_code_plan_normalization": MiniappGenerationCodePlanNormalization,
            "generation_codegen": MiniappGenerationCodegen,
            "generation_codegen_clusters": MiniappGenerationCodegenClusters,
            "generation_codegen_prompts": MiniappGenerationCodegenPrompts,
            "generation_codegen_selection": MiniappGenerationCodegenSelection,
            "generation_targeting": MiniappGenerationTargeting,
            "generation_page_graph_runtime": MiniappGenerationPageGraphRuntime,
            "generation_progress_reporting": GenerationProgressReportingRuntime,
            "generation_reporting": MiniappGenerationReporting,
            "generation_reporting_compaction": MiniappGenerationReportingCompaction,
            "generation_reporting_repair": MiniappGenerationReportingRepair,
            "generation_edit_gate": lambda _service: GenerationEditGate(),
            "generation_preflight_validation": lambda _service: GenerationPreflightValidation(),
            "generation_contract_pass": MiniappGenerationContractPass,
            "generation_contract_schema": MiniappGenerationContractSchema,
            "generation_contract_routes": MiniappGenerationContractRoutes,
            "generation_contract_frontend": MiniappGenerationContractFrontend,
            "generation_shell_contract": MiniappGenerationShellContract,
        }

    @classmethod
    def _runtime_class_owner_map(cls) -> dict[str, Any]:
        return {
            "_grounded_spec_outline_schema": GroundedSpecPromptsRuntime,
            "_grounded_spec_outline_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_section_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_outline_user_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_user_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_partial_schema": GroundedSpecPromptsRuntime,
            "_grounded_spec_section_user_prompt": GroundedSpecPromptsRuntime,
            "_compact_doc_refs": GroundedSpecPromptsRuntime,
            "_compact_creative_direction": GroundedSpecPromptsRuntime,
            "_normalize_model_payload": GroundedSpecPayloadsRuntime,
            "_normalize_assumption_item": GroundedSpecPayloadsRuntime,
            "_normalize_contradiction_item": GroundedSpecPayloadsRuntime,
            "_normalize_non_functional_requirement_item": GroundedSpecPayloadsRuntime,
            "_contains_placeholder_surface": GenerationEditGate,
            "_has_real_interactive_surface": GenerationEditGate,
            "_has_visible_loading_surface": GenerationEditGate,
            "_has_business_surface": GenerationEditGate,
            "_empty_business_container_count": GenerationEditGate,
            "_edit_gate_issues": GenerationEditGate,
            "_preflight_backend_syntax_issues": GenerationPreflightValidation,
            "_preflight_frontend_syntax_issues": GenerationPreflightValidation,
            "_preflight_profile_schema_issues": GenerationPreflightValidation,
            "_preflight_route_schema_issues": GenerationPreflightValidation,
            "_preflight_check_results": GenerationPreflightValidation,
            "_normalize_local_route_ref": GenerationPreflightValidation,
            "_page_graph_gate_issues": PageGraphValidation,
            "_build_page_graph_verification_report": PageGraphValidation,
            "_preflight_route_manifest_link_issues": GenerationPreflightValidation,
            "_workspace_loop_completion_state": MiniappGenerationCompletion,
            "_build_stage_reports": MiniappGenerationReporting,
            "_build_materialization_report": MiniappGenerationReporting,
            "_build_agent_traceability_report": MiniappGenerationReporting,
            "_build_agent_summary": MiniappGenerationReporting,
            "_compile_code_summary": MiniappGenerationReporting,
            "_limit_text": MiniappGenerationReportingCompaction,
            "_bounded_file_contexts": MiniappGenerationReportingCompaction,
            "_compact_grounded_spec_for_codegen": MiniappGenerationReportingCompaction,
            "_compact_role_contract_for_codegen": MiniappGenerationReportingCompaction,
            "_compact_page_graph_for_codegen": MiniappGenerationReportingCompaction,
            "_stateful_page_contracts": MiniappGenerationReportingRepair,
            "_failure_signature_for_issues": MiniappGenerationReportingRepair,
            "_is_structural_contract_failure": MiniappGenerationReportingRepair,
            "_extract_failure_file_hints": MiniappGenerationReportingRepair,
            "_causal_surface_for_issues": MiniappGenerationReportingRepair,
            "_expand_structural_repair_targets": MiniappGenerationReportingRepair,
            "_repair_targets_for_attempt": MiniappGenerationReportingRepair,
            "_compact_file_contexts_for_repair": MiniappGenerationReportingCompaction,
            "_whole_file_parallel_group": GenerationProgressReportingRuntime,
            "_group_generation_clusters_for_execution": GenerationProgressReportingRuntime,
            "_run_progress_for_event": GenerationProgressReportingRuntime,
            "_grounded_spec_from_operations": MiniappGenerationContractPass,
            "_canonicalize_local_role_links_in_text": MiniappGenerationContractFrontend,
            "_route_module_needs_stub": MiniappGenerationContractRoutes,
            "_route_module_requires_db_backed_repair": MiniappGenerationContractRoutes,
            "_deterministic_client_page_route_source": MiniappGenerationContractRoutes,
            "_deterministic_specialist_page_route_source": MiniappGenerationContractRoutes,
            "_deterministic_manager_page_route_source": MiniappGenerationContractRoutes,
            "_strip_noncanonical_runtime_route_handlers": MiniappGenerationContractRoutes,
            "_normalize_runtime_route_module_source": MiniappGenerationContractRoutes,
            "_deterministic_main_runtime_source": MiniappGenerationContractRoutes,
            "_normalize_api_aliases_in_text": MiniappGenerationContractFrontend,
            "_deterministic_profiles_route_source": MiniappGenerationContractRoutes,
            "_deterministic_runtime_route_source": MiniappGenerationContractRoutes,
            "_ensure_fastapi_import_symbol": MiniappGenerationContractFrontend,
            "_inject_head_asset_link": MiniappGenerationContractFrontend,
            "_ensure_head_asset_link": MiniappGenerationContractFrontend,
            "_ensure_body_script_ref": MiniappGenerationContractFrontend,
            "_ensure_preview_bridge_ref": MiniappGenerationContractFrontend,
            "_ensure_page_shell_contract": MiniappGenerationContractFrontend,
            "_ensure_html_dom_ids_for_script": MiniappGenerationContractFrontend,
            "_static_asset_href": MiniappGenerationContractFrontend,
            "_normalize_role_local_links": MiniappGenerationContractFrontend,
            "_snake_case_filename": MiniappGenerationPaths,
            "_normalize_generated_file_path": MiniappGenerationPaths,
            "_normalize_path_list": MiniappGenerationPaths,
            "_normalize_string_list": MiniappGenerationPaths,
            "_normalize_role_route_path": MiniappGenerationPaths,
            "_absolute_role_route_path": MiniappGenerationPaths,
            "_default_page_file": MiniappGenerationPaths,
            "_default_page_asset_path": MiniappGenerationPaths,
            "_build_execution_plan": MiniappGenerationPageGraphRuntime,
            "_is_canonical_target_path": MiniappGenerationTargeting,
            "_canonicalize_static_target_path": MiniappGenerationTargeting,
            "_planned_page_static_targets": MiniappGenerationTargeting,
            "_prune_non_page_static_targets": MiniappGenerationTargeting,
            "_sanitize_backend_targets": MiniappGenerationTargeting,
            "_sanitize_planner_target_files": MiniappGenerationTargeting,
            "_expand_page_triplet_targets": MiniappGenerationTargeting,
            "_build_generation_clusters": MiniappGenerationPageGraphRuntime,
            "_backend_cluster_name_for_path": MiniappGenerationPageGraphRuntime,
            "_static_cluster_suffix_for_path": MiniappGenerationPageGraphRuntime,
            "_expand_cluster_targets_for_safe_companions": MiniappGenerationTargeting,
            "_detect_missing_static_asset_targets": MiniappGenerationTargeting,
            "_extract_static_asset_targets": MiniappGenerationTargeting,
            "_resolve_static_asset_target": MiniappGenerationTargeting,
            "_whole_file_cluster_system_prompt": MiniappGenerationCodegenPrompts,
            "_page_edit_system_prompt": MiniappGenerationCodegenPrompts,
            "_composition_system_prompt": MiniappGenerationCodegenPrompts,
            "_code_plan_schema": MiniappGenerationCodePlanPrompts,
            "_code_plan_system_prompt": MiniappGenerationCodePlanPrompts,
            "_code_plan_section_system_prompt": MiniappGenerationCodePlanPrompts,
            "_code_plan_partial_schema": MiniappGenerationCodePlanPrompts,
            "_role_contract_schema": MiniappGenerationRoleContract,
        }

    @classmethod
    def _runtime_instance_owner_map(cls) -> dict[str, Any]:
        return {
            "_resolve_grounded_spec": "grounded_spec_orchestration",
            "_generate_grounded_spec_pair_with_timeout": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast_with_timeout": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast_inner": "grounded_spec_orchestration",
            "_generate_grounded_spec_pair": "grounded_spec_orchestration",
            "_generate_grounded_spec_section": "grounded_spec_orchestration",
            "_preflight_generation_issues": "generation_preflight_validation",
            "_workspace_loop_validation_snapshot_from_execution": "generation_completion",
            "_run_generation_workspace_loop": "generation_repair",
            "_repair_draft_after_failure": "generation_repair",
            "_append_event": "generation_progress_reporting",
            "_save_job": "generation_progress_reporting",
            "_store_report": "generation_progress_reporting",
            "_clear_trace": "generation_progress_reporting",
            "_append_trace": "generation_progress_reporting",
            "_sync_run_progress": "generation_progress_reporting",
            "_sync_generation_cluster_progress": "generation_progress_reporting",
            "_sync_generation_cluster_started": "generation_progress_reporting",
            "_sync_generation_batch_started": "generation_progress_reporting",
            "_ensure_runtime_artifact_operations": "generation_contract_pass",
            "_ensure_app_level_test_operations": "generation_contract_pass",
            "_run_pre_apply_contract_pass": "generation_contract_pass",
            "_resolve_grounded_spec_for_contract_pass": "generation_contract_pass",
            "_synchronize_profile_schema_contract": "generation_contract_schema",
            "_synchronize_db_session_contract": "generation_contract_schema",
            "_synchronize_runtime_route_contract": "generation_contract_schema",
            "_synchronize_backend_dependency_contract": "generation_contract_schema",
            "_synchronize_main_runtime_contract": "generation_contract_schema",
            "_synchronize_minimal_workflow_route_contracts": "generation_contract_routes",
            "_synchronize_route_schema_contract": "generation_contract_schema",
            "_synchronize_frontend_api_contract": "generation_contract_frontend",
            "_synchronize_frontend_navigation_contract": "generation_contract_frontend",
            "_synchronize_basic_page_state_contract": "generation_contract_frontend",
            "_operation_or_workspace_content": "generation_contract_schema",
            "_collect_files_to_read": "generation_targeting",
            "_canonicalize_target_files": "generation_targeting",
            "_resolve_code_edits": "generation_codegen",
            "_resolve_whole_file_code_edits": "generation_codegen",
            "_resolve_page_file_edits_async": "generation_codegen",
            "_should_prefer_deterministic_fast_cluster": "generation_codegen",
            "_deterministic_fast_cluster_result": "generation_codegen",
            "_whole_file_cluster_user_prompt": "generation_codegen_prompts",
            "_resolve_whole_file_cluster": "generation_codegen_clusters",
            "_timed_whole_file_cluster": "generation_codegen_clusters",
            "_page_edit_user_prompt": "generation_codegen_prompts",
            "_composition_user_prompt": "generation_codegen_prompts",
            "_resolve_page_file_edit": "generation_codegen_selection",
            "_resolve_composition_edit": "generation_codegen_clusters",
            "_timed_composition_cluster": "generation_codegen_clusters",
            "_selected_pages_for_edit": "generation_codegen_selection",
            "_backend_composition_targets": "generation_codegen_selection",
            "_frontend_composition_targets": "generation_codegen_selection",
            "_partition_frontend_composition_targets": "generation_codegen_selection",
            "_resolve_role_contract": "generation_role_contract",
            "_normalize_role_contract": "generation_role_contract",
            "_minimal_role_contract": "generation_role_contract",
            "_role_contract_user_prompt": "generation_role_contract",
            "_role_contract_gate_issues": "generation_role_contract",
            "_resolve_code_plan": "generation_code_plan",
            "_generate_code_plan_sections_with_timeout": "generation_code_plan",
            "_generate_code_plan_sections": "generation_code_plan",
            "_normalize_page_plan": "generation_code_plan_normalization",
            "_finalize_role_pages": "generation_code_plan_normalization",
            "_normalize_page_definition": "generation_code_plan_normalization",
            "_code_plan_user_prompt": "generation_code_plan_prompts",
            "_code_plan_section_user_prompt": "generation_code_plan_prompts",
            "_workspace_path_hints": "generation_code_plan_prompts",
        }

    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        document_service: DocumentIntelligenceService,
        code_index_service: CodeIndexService,
        context_pack_builder: ContextPackBuilder,
        patch_service: PatchService,
        preview_service: PreviewService,
        check_runner: CheckRunner,
        validation_suite: ValidationSuite,
        openrouter_client: OpenRouterClient,
        workspace_log_service: WorkspaceLogService,
        session_engine: Any | None = None,
        task_router: Any | None = None,
        context_budget_manager: Any | None = None,
        prompt_state_manager: Any | None = None,
        compaction_service: Any | None = None,
        artifact_recorder: Any | None = None,
        workspace_loop_engine: WorkspaceLoopEngine | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.document_service = document_service
        self.code_index_service = code_index_service
        self.context_pack_builder = context_pack_builder
        self.patch_service = patch_service
        self.preview_service = preview_service
        self.check_runner = check_runner
        self.validation_suite = validation_suite
        self.openrouter_client = openrouter_client
        self.workspace_log_service = workspace_log_service
        self.session_engine = session_engine
        self.task_router = task_router
        self.context_budget_manager = context_budget_manager
        self.prompt_state_manager = prompt_state_manager
        self.compaction_service = compaction_service
        self.artifact_recorder = artifact_recorder
        self.workspace_loop_engine = workspace_loop_engine
        self.artifact_builder = self._artifact_builder()
        self.materialization_service = self._materialization_service()
        self.generation_contract_pass = MiniappGenerationContractPass(self)
        self.generation_contract_schema = MiniappGenerationContractSchema(self)
        self.generation_contract_routes = MiniappGenerationContractRoutes(self)
        self.generation_contract_frontend = MiniappGenerationContractFrontend(self)
        self.generation_shell_contract = MiniappGenerationShellContract(self)
        self.runtime_contract_sync = MiniappRuntimeContractSync(workspace_service=self.workspace_service, read_content=self._operation_or_workspace_content)
        self.grounded_spec_builder = MiniappGroundedSpecBuilder(self)
        self.grounded_spec_orchestration = GroundedSpecOrchestrationRuntime(self)
        self.generation_completion = MiniappGenerationCompletion()
        self.generation_repair = MiniappGenerationRepair(self)
        self.generation_entry = MiniappGenerationEntry(self)
        self.generation_entity_contract = MiniappGenerationEntityContract(self)
        self.generation_contract_critic = MiniappGenerationContractCritic(self)
        self.generation_normal_loop = MiniappGenerationNormalLoop(self)
        self.generation_resume = MiniappGenerationResume(self)
        self.generation_plan_runtime = MiniappGenerationPlanRuntime(self)
        self.generation_role_contract = MiniappGenerationRoleContract(self)
        self.generation_code_plan = MiniappGenerationCodePlan(self)
        self.generation_code_plan_prompts = MiniappGenerationCodePlanPrompts(self)
        self.generation_code_plan_normalization = MiniappGenerationCodePlanNormalization(self)
        self.generation_codegen = MiniappGenerationCodegen(self)
        self.generation_codegen_clusters = MiniappGenerationCodegenClusters(self)
        self.generation_codegen_prompts = MiniappGenerationCodegenPrompts(self)
        self.generation_codegen_selection = MiniappGenerationCodegenSelection(self)
        self.generation_targeting = MiniappGenerationTargeting(self)
        self.generation_page_graph_runtime = MiniappGenerationPageGraphRuntime(self)
        self.generation_progress_reporting = GenerationProgressReportingRuntime(self)
        self.generation_reporting = MiniappGenerationReporting(self)
        self.generation_reporting_compaction = MiniappGenerationReportingCompaction(self)
        self.generation_reporting_repair = MiniappGenerationReportingRepair(self)
        self.generation_edit_gate = GenerationEditGate()
        self.generation_preflight_validation = GenerationPreflightValidation()

    @classmethod
    def _artifact_builder(cls) -> MiniappArtifactBuilder:
        return MiniappArtifactBuilder(
            normalize_role_route_path=lambda role, route_path: cls._normalize_role_route_path(role, route_path, index=0),
            absolute_role_route_path=cls._absolute_role_route_path,
            default_page_asset_path=lambda file_path, asset_kind: cls._default_page_asset_path(file_path, asset_kind=asset_kind),
            normalize_runtime_python_path=cls._normalize_runtime_python_path,
        )

    @classmethod
    def _materialization_helpers(cls) -> MiniappMaterializationService:
        return MiniappMaterializationService(
            default_page_asset_path=lambda file_path, asset_kind: cls._default_page_asset_path(file_path, asset_kind=asset_kind),
            workspace_file_tree=lambda workspace_id, run_id: [],
            build_stage_reports=lambda page_graph, role_scope, realized_paths: cls._artifact_builder().build_stage_reports(page_graph=page_graph, role_scope=role_scope, realized_paths=realized_paths),
        )

    def _materialization_service(self) -> MiniappMaterializationService:
        return MiniappMaterializationService(
            default_page_asset_path=lambda file_path, asset_kind: self._default_page_asset_path(file_path, asset_kind=asset_kind),
            workspace_file_tree=lambda workspace_id, run_id: self.workspace_service.file_tree(workspace_id, run_id=run_id),
            build_stage_reports=lambda page_graph, role_scope, realized_paths: self.artifact_builder.build_stage_reports(page_graph=page_graph, role_scope=role_scope, realized_paths=realized_paths),
        )

    def generate(self, workspace_id: str, request: GenerateRequest, *, should_stop: Callable[[], bool] | None = None) -> JobRecord:
        started_at = time.perf_counter()
        invalid_prompt_assets = self.validate_prompt_assets_are_english()
        if invalid_prompt_assets:
            raise ValueError(f"Prompt assets must remain English-only: {', '.join(invalid_prompt_assets[:5])}")
        effective_prompt = self._effective_prompt(request)
        target_platform = self._target_platform(request.target_platform)
        preview_profile = self._preview_profile(request.preview_profile)
        generation_mode = self._generation_mode(request.generation_mode)
        effective_model_profile = resolve_model_profile(request.model_profile, generation_mode)
        workspace = self.workspace_service.get_workspace(workspace_id)
        role_scope = [role for role in request.target_role_scope if role in ROLE_ORDER] or list(ROLE_ORDER)
        llm_config = self.openrouter_client.configuration()
        cache_context = {
            "prompt_cache_key": self.context_pack_builder.prompt_cache_key(workspace, effective_model_profile),
            "stable_prefix": self.context_pack_builder.stable_prefix(workspace, effective_model_profile),
        }
        cache_stats_sink = {"prompt_cache_key": cache_context["prompt_cache_key"], "stable_prefix_chars": len(cache_context["stable_prefix"]), "cached_tokens": 0, "cache_write_tokens": 0, "llm_requests": 0}
        cache_context_token = ACTIVE_LLM_CACHE_CONTEXT.set(cache_context)
        cache_stats_token = ACTIVE_LLM_CACHE_STATS.set(cache_stats_sink)
        resume_bundle = self._load_resume_checkpoint_bundle(workspace_id, request.resume_from_run_id)
        try:
            with self.openrouter_client.routing_context(
                model_profile=effective_model_profile,
                generation_mode=generation_mode,
            ):
                job = JobRecord(
                    workspace_id=workspace_id,
                    prompt=request.prompt,
                    status="running",
                    mode=request.mode,
                    generation_mode=generation_mode,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    current_revision_id=workspace.current_revision_id,
                    fidelity=QUALITY_FIDELITY[generation_mode],  # type: ignore[arg-type]
                    llm_enabled=bool(llm_config["enabled"]),
                    llm_provider="openai" if llm_config["enabled"] else None,
                    model_profile=effective_model_profile,
                    linked_run_id=request.linked_run_id,
                    error_context=request.error_context,
                    failure_class=self._failure_class_from_error_context(request.error_context),
                    root_cause_summary=self._root_cause_summary(request.error_context),
                )
                draft_run_id = request.linked_run_id or job.job_id
                if resume_bundle is None:
                    self.store.delete("reports", f"resume_checkpoint:{workspace_id}")
                self._clear_trace(workspace_id)
                self._append_event(job, "job_started", "Generation request accepted.")
                self.code_index_service.index_workspace(workspace, self.workspace_service.source_dir(workspace_id))
                stopped = self._stop_if_requested(job, workspace_id, should_stop)
                if stopped is not None:
                    return stopped
                missing_corpora = self.document_service.ensure_required_corpora(target_platform.value)
                if not workspace.template_cloned:
                    missing_corpora.append("Workspace template has not been cloned.")
                if missing_corpora:
                    return self._block_with_messages(job, missing_corpora, code="generation.missing_corpora", event_type="job_failed", failure_reason="Required corpora or template clone is missing.")
                if not self.openrouter_client.enabled:
                    return self._block_with_messages(job, ["Agentic app generation requires an LLM provider for every run.", "Set OPENAI_API_KEY or OPENROUTER_API_KEY before creating or editing a mini-app workspace."], code="generation.llm_required", event_type="job_failed", failure_reason="Generation requires an LLM provider because the workspace now uses the agentic direct code generation loop.")
                if resume_bundle is not None:
                    if request.resume_from_run_id and draft_run_id != request.resume_from_run_id and self.workspace_service.draft_exists(workspace_id, request.resume_from_run_id):
                        self.workspace_service.clone_draft(workspace_id, request.resume_from_run_id, draft_run_id)
                    draft_source = self.workspace_service.ensure_draft(workspace_id, draft_run_id)
                    return self.generation_entry.continue_generation_from_plan(
                        workspace=workspace,
                        workspace_id=workspace_id,
                        job=job,
                        request=request,
                        draft_run_id=draft_run_id,
                        draft_source=draft_source,
                        effective_prompt=effective_prompt,
                        grounded_spec=resume_bundle["grounded_spec"],
                        role_scope=list(resume_bundle.get("role_scope") or role_scope),
                        role_contract=resume_bundle["role_contract"],
                        plan_result=resume_bundle["plan_result"],
                        generation_mode=generation_mode,
                        creative_direction=self._select_creative_direction(effective_prompt),
                        retrieval_ms=0,
                        started_at=started_at,
                        should_stop=should_stop,
                    )
                doc_refs = self.document_service.retrieve(workspace_id=workspace_id, prompt=effective_prompt, target_platform=target_platform.value)
                retrieval_ms = int((time.perf_counter() - started_at) * 1000)
                chat_turn = ChatTurnRecord(workspace_id=workspace_id, role="user", content=request.prompt, linked_job_id=job.job_id, linked_run_id=request.linked_run_id)
                self.store.upsert("chat_turns", chat_turn.turn_id, chat_turn.model_dump(mode="json"))
                creative_direction = self._select_creative_direction(effective_prompt)
                return self.generation_entry.generate_with_agent_loop(
                    workspace=workspace,
                    workspace_id=workspace_id,
                    job=job,
                    request=request,
                    draft_run_id=draft_run_id,
                    effective_prompt=effective_prompt,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    generation_mode=generation_mode,
                    role_scope=role_scope,
                    doc_refs=doc_refs,
                    retrieval_ms=retrieval_ms,
                    started_at=started_at,
                    creative_direction=creative_direction,
                    should_stop=should_stop,
                    prompt_turn_id=chat_turn.turn_id,
                )
        finally:
            ACTIVE_LLM_CACHE_CONTEXT.reset(cache_context_token)
            ACTIVE_LLM_CACHE_STATS.reset(cache_stats_token)

    def _compile_prompt_to_scaffold(self, *, prompt: str, grounded_spec: GroundedSpecModel, entity_contract: dict[str, Any] | None, role_scope: list[str], workspace_tree: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
        return compile_prompt_to_scaffold(
            prompt=prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role_scope=role_scope,
            workspace_tree=workspace_tree,
            design_reference_files=DESIGN_REFERENCE_FILES,
            default_page_file=self._default_page_file,
            default_page_asset_path=self._default_page_asset_path,
            default_handoff_paths_for_page_kind=self._default_handoff_paths_for_page_kind,
            canonicalize_target_files=self._canonicalize_target_files,
            sanitize_planner_target_files=self._sanitize_planner_target_files,
            sanitize_backend_targets=self._sanitize_backend_targets,
            collect_files_to_read=self._collect_files_to_read,
            build_generation_clusters=self._build_generation_clusters,
            build_execution_plan=self._build_execution_plan,
        )

    def _scaffold_role_pages_for_role(self, *, role: str, prompt: str, grounded_spec: GroundedSpecModel) -> list[dict[str, Any]]:
        return scaffold_role_pages_for_role(role=role, prompt=prompt, grounded_spec=grounded_spec, default_page_file=self._default_page_file, default_page_asset_path=self._default_page_asset_path, default_handoff_paths_for_page_kind=self._default_handoff_paths_for_page_kind)

    @staticmethod
    def _scaffold_page_slug_for_route(route_path: str) -> str:
        return scaffold_page_slug_for_route(route_path)

    def _scaffold_backend_targets_from_spec(self, *, prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str]) -> list[str]:
        return scaffold_backend_targets_from_spec(prompt=prompt, grounded_spec=grounded_spec, role_scope=role_scope)

    @staticmethod
    def _mentions_schedule_or_time(prompt: str, grounded_spec: GroundedSpecModel) -> bool:
        return mentions_schedule_or_time(prompt, grounded_spec)

    @staticmethod
    def _scaffold_role_responsibility(role: str, grounded_spec: GroundedSpecModel) -> str:
        return scaffold_role_responsibility(role, grounded_spec)

    def _store_resume_checkpoint(self, *, workspace_id: str, draft_run_id: str, request: GenerateRequest, role_scope: list[str], role_contract: dict[str, Any], plan_result: dict[str, Any]) -> None:
        self.generation_resume.store_resume_checkpoint(workspace_id=workspace_id, draft_run_id=draft_run_id, request=request, role_scope=role_scope, role_contract=role_contract, plan_result=plan_result)

    def _load_resume_checkpoint_bundle(self, workspace_id: str, source_run_id: str | None) -> dict[str, Any] | None:
        return self.generation_resume.load_resume_checkpoint_bundle(workspace_id, source_run_id)

    def _continue_generation_from_plan(self, **kwargs: Any) -> JobRecord:
        return self.generation_entry.continue_generation_from_plan(**kwargs)

    def retry(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            mode=job.mode,
            target_platform=self._target_platform(job.target_platform),
            preview_profile=self._preview_profile(job.preview_profile),
            generation_mode=self._generation_mode(job.generation_mode),
            model_profile=job.model_profile,
            error_context=job.error_context,
        )
        return self.generate(job.workspace_id, request)

    def get_job(self, job_id: str) -> JobRecord:
        payload = self.store.get("jobs", job_id)
        if not payload:
            raise KeyError(f"Job not found: {job_id}")
        return JobRecord.model_validate(payload)

    def current_report(self, workspace_id: str, report_type: str) -> dict | None:
        return self.store.get("reports", f"{report_type}:{workspace_id}")

    def latest_job_for_workspace(self, workspace_id: str) -> JobRecord | None:
        jobs = [
            JobRecord.model_validate(item)
            for item in self.store.list("jobs")
            if item.get("workspace_id") == workspace_id
        ]
        if not jobs:
            return None
        return max(jobs, key=lambda item: item.created_at)
