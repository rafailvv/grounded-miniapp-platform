from __future__ import annotations

from contextvars import ContextVar
import time
from pathlib import Path
from typing import Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import ChatTurnRecord, GenerateRequest, JobRecord
from app.models.grounded_spec import GroundedSpecModel
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.miniapp_generation.artifact_builder import MiniappArtifactBuilder
from app.services.miniapp_generation.compat_code_plan_mixins import CompatCodePlanMixins
from app.services.miniapp_generation.compat_codegen_mixins import CompatCodegenMixins
from app.services.miniapp_generation.compat_contract_mixins import CompatContractMixins
from app.services.miniapp_generation.compat_dispatch import CompatibilityDispatchMixin, GenerationServiceMeta
from app.services.miniapp_generation.compat_grounded_spec_mixins import CompatGroundedSpecMixins
from app.services.miniapp_generation.compat_paths_mixins import CompatPathsMixins
from app.services.miniapp_generation.compat_reporting_mixins import CompatReportingMixins
from app.services.miniapp_generation.compat_validation_mixins import CompatValidationMixins
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
    MiniappGenerationCompletion,
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
    MiniappGenerationEntry,
    MiniappGenerationNormalLoop,
    MiniappGenerationPageGraphRuntime,
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
from app.modules.miniapp_validation import GenerationEditGate, GenerationPreflightValidation
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
    CompatGroundedSpecMixins,
    CompatValidationMixins,
    CompatReportingMixins,
    CompatContractMixins,
    CompatPathsMixins,
    CompatCodegenMixins,
    CompatCodePlanMixins,
    CompatibilityDispatchMixin,
    metaclass=GenerationServiceMeta,
):
    GROUNDED_SPEC_SECTION_TIMEOUT_SECONDS = 90
    GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS = 120
    CODE_PLAN_SECTION_TIMEOUT_SECONDS = 120
    CODE_PLAN_TOTAL_TIMEOUT_SECONDS = 150
    WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS = 240
    STRUCTURED_LLM_TIMEOUT_SECONDS = 180
    JSON_OBJECT_LLM_TIMEOUT_SECONDS = 120

    @classmethod
    def _compat_owner_factories(cls) -> dict[str, Any]:
        return {
            "grounded_spec_orchestration": GroundedSpecOrchestrationRuntime,
            "generation_completion": MiniappGenerationCompletion,
            "generation_repair": MiniappGenerationRepair,
            "generation_entry": MiniappGenerationEntry,
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
        workspace = self.workspace_service.get_workspace(workspace_id)
        role_scope = [role for role in request.target_role_scope if role in ROLE_ORDER] or list(ROLE_ORDER)
        llm_config = self.openrouter_client.configuration()
        cache_context = {
            "prompt_cache_key": self.context_pack_builder.prompt_cache_key(workspace, request.model_profile),
            "stable_prefix": self.context_pack_builder.stable_prefix(workspace, request.model_profile),
        }
        cache_stats_sink = {"prompt_cache_key": cache_context["prompt_cache_key"], "stable_prefix_chars": len(cache_context["stable_prefix"]), "cached_tokens": 0, "cache_write_tokens": 0, "llm_requests": 0}
        ACTIVE_LLM_CACHE_CONTEXT.set(cache_context)
        ACTIVE_LLM_CACHE_STATS.set(cache_stats_sink)
        resume_bundle = self._load_resume_checkpoint_bundle(workspace_id, request.resume_from_run_id)
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
            model_profile=request.model_profile,
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
            return self._block_with_messages(job, ["Agentic app generation now requires OpenAI configuration for every run.", "Set OPENAI_API_KEY before creating or editing a mini-app workspace."], code="generation.llm_required", event_type="job_failed", failure_reason="Generation requires OpenAI because the workspace now uses the agentic direct code generation loop.")
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

    def _compile_prompt_to_scaffold(self, *, prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], workspace_tree: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
        return compile_prompt_to_scaffold(
            prompt=prompt,
            grounded_spec=grounded_spec,
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
        request = GenerateRequest(prompt=job.prompt, mode=job.mode, target_platform=self._target_platform(job.target_platform), preview_profile=self._preview_profile(job.preview_profile), generation_mode=self._generation_mode(job.generation_mode), error_context=job.error_context)
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
