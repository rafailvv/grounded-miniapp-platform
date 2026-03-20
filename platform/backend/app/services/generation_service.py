from __future__ import annotations

import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any, Callable

from app.ai.openrouter_client import OpenRouterClient
from app.models.app_ir import (
    Action,
    AppIRModel,
    Assignment,
    AuthModel,
    Component,
    Condition,
    DataField,
    Entity,
    IRAssumption,
    IRMetadata,
    Integration,
    OpenQuestion,
    Permission,
    PlatformHints,
    RoleActionGroup,
    RoleRouteGroup,
    RouteDefinition,
    Screen,
    ScreenDataSource,
    SecurityPolicy,
    StorageBinding,
    TelemetryHook,
    TraceabilityLink,
    Transition,
    ValidatorRule,
    Variable,
)
from app.models.artifacts import (
    ApplyPatchResult,
    AppIRValidatorResult,
    ArtifactPlanModel,
    GroundedSpecValidatorResult,
    PatchEnvelope,
    PatchOperationModel,
    TraceabilityReportEntry,
    TraceabilityReportModel,
    ValidationIssue,
)
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import (
    ChatTurnRecord,
    CheckExecutionRecord,
    ContextPack,
    DraftFileOperation,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    RunIterationOperation,
    RunIterationRecord,
    ValidationSnapshot,
    new_id,
    utc_now,
)
from app.models.grounded_spec import (
    APIField,
    APIRequirement,
    Actor,
    Assumption,
    Contradiction,
    DomainEntity,
    EntityAttribute,
    EvidenceLink,
    FlowStep,
    GroundedSpecModel,
    IntegrationRequirement,
    Metadata,
    NonFunctionalRequirement,
    PersistenceRequirement,
    PlatformConstraint,
    SecurityRequirement,
    UIRequirement,
    Unknown,
    UserFlow,
)
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.code_index_service import CodeIndexService
from app.services.context_pack_builder import ContextPackBuilder
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.patch_service import PatchService
from app.services.preview_service import PreviewService
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService, json_dumps
from app.validators.suite import ValidationSuite


ROLE_ORDER = ("client", "specialist", "manager")
ROLE_COMPONENT_PREFIX = {
    "client": "Client",
    "specialist": "Specialist",
    "manager": "Manager",
}
DESIGN_REFERENCE_FILES = (
    "frontend/src/features/profile/ui/RoleProfileEditorPage.tsx",
    "frontend/src/features/profile/ui/RoleProfileEditorPage.module.css",
    "frontend/src/entities/profile/ui/ProfileCabinetCard/ProfileCabinetCard.tsx",
    "frontend/src/entities/profile/ui/ProfileCabinetCard/ProfileCabinetCard.module.css",
    "frontend/src/widgets/role-home/RoleHomePage.tsx",
    "frontend/src/widgets/role-home/RoleHomePage.module.css",
)
SHARED_GENERATED_FILES = (
    "frontend/src/app/routing/RoleRouter.tsx",
    "frontend/src/app/layout/AppShell.tsx",
)
logger = logging.getLogger(__name__)
ACTIVE_LLM_CACHE_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("active_llm_cache_context", default=None)
ACTIVE_LLM_CACHE_STATS: ContextVar[dict[str, Any] | None] = ContextVar("active_llm_cache_stats", default=None)
QUALITY_FIDELITY = {
    GenerationMode.FAST: "fast_app",
    GenerationMode.QUALITY: "quality_app",
    GenerationMode.BALANCED: "balanced_app",
    GenerationMode.BASIC: "basic_scaffold",
}


class GenerationService:
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

    def generate(self, workspace_id: str, request: GenerateRequest, *, should_stop: Callable[[], bool] | None = None) -> JobRecord:
        started_at = time.perf_counter()
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
        cache_stats_sink = {
            "prompt_cache_key": cache_context["prompt_cache_key"],
            "stable_prefix_chars": len(cache_context["stable_prefix"]),
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "llm_requests": 0,
        }
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
        if request.linked_run_id:
            draft_run_id = request.linked_run_id
        else:
            draft_run_id = job.job_id
        if resume_bundle is None:
            self.store.delete("reports", f"resume_checkpoint:{workspace_id}")
        self._clear_trace(workspace_id)
        self._append_event(job, "job_started", "Generation request accepted.")
        if resume_bundle is not None:
            self._append_event(job, "resume_started", "Reusing saved planning and continuing generation from checkpoint.")
            self.workspace_log_service.append(
                workspace_id,
                source="generation.resume",
                message="Generation resumed from a saved planning checkpoint.",
                payload={
                    "source_run_id": request.resume_from_run_id,
                    "draft_run_id": draft_run_id,
                },
            )
        self._append_event(job, "indexing_started", "Refreshing workspace index.")
        self._append_event(job, "retrieval_started", "Preparing workspace index and grounded retrieval.")
        self._append_trace(
            workspace_id,
            "job_started",
            "Generation request accepted.",
            {
                "mode": generation_mode.value,
                "run_mode": request.mode,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "llm_enabled": bool(llm_config["enabled"]),
            },
        )
        self.code_index_service.index_workspace(workspace, self.workspace_service.source_dir(workspace_id))
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped

        missing_corpora = self.document_service.ensure_required_corpora(target_platform.value)
        if not workspace.template_cloned:
            missing_corpora.append("Workspace template has not been cloned.")
        if missing_corpora:
            self._append_trace(
                workspace_id,
                "preflight_blocked",
                "Generation blocked before retrieval.",
                {"missing": missing_corpora},
            )
            return self._block_with_messages(
                job,
                missing_corpora,
                code="generation.missing_corpora",
                event_type="job_failed",
                failure_reason="Required corpora or template clone is missing.",
            )

        if not self.openrouter_client.enabled:
            self._append_trace(
                workspace_id,
                "llm_blocked",
                "Generation was blocked because no LLM provider is configured.",
                {"required_mode": generation_mode.value, "pipeline": "llm_first_agentic_workspace"},
            )
            return self._block_with_messages(
                job,
                [
                    "Agentic app generation now requires OpenAI configuration for every run.",
                    "Set OPENAI_API_KEY before creating or editing a mini-app workspace.",
                ],
                code="generation.llm_required",
                event_type="job_failed",
                failure_reason="Generation requires OpenAI because the workspace now uses an LLM-first page planning and code editing pipeline.",
            )

        if resume_bundle is not None:
            if request.resume_from_run_id and draft_run_id != request.resume_from_run_id and self.workspace_service.draft_exists(workspace_id, request.resume_from_run_id):
                self.workspace_service.clone_draft(workspace_id, request.resume_from_run_id, draft_run_id)
            draft_source = self.workspace_service.ensure_draft(workspace_id, draft_run_id)
            grounded_spec = resume_bundle["grounded_spec"]
            role_contract = resume_bundle["role_contract"]
            plan_result = resume_bundle["plan_result"]
            resumed_role_scope = list(resume_bundle.get("role_scope") or role_scope)
            self._append_trace(
                workspace_id,
                "planning_resumed",
                "Saved planning artifacts were reused instead of rebuilding retrieval/spec/planning.",
                {
                    "source_run_id": request.resume_from_run_id,
                    "draft_run_id": draft_run_id,
                    "target_files": len(plan_result.get("target_files") or []),
                },
            )
            self.workspace_log_service.append(
                workspace_id,
                source="generation.resume",
                message="Saved planning artifacts were loaded from checkpoint.",
                payload={
                    "source_run_id": request.resume_from_run_id,
                    "draft_run_id": draft_run_id,
                    "target_files": len(plan_result.get("target_files") or []),
                },
            )
            self._append_event(job, "draft_prepared", "Reused draft workspace from the saved planning checkpoint.")
            self._append_event(job, "planning_ready", "Reused saved code plan.")
            return self._continue_generation_from_plan(
                workspace=workspace,
                workspace_id=workspace_id,
                job=job,
                request=request,
                draft_run_id=draft_run_id,
                draft_source=draft_source,
                effective_prompt=effective_prompt,
                grounded_spec=grounded_spec,
                role_scope=resumed_role_scope,
                role_contract=role_contract,
                plan_result=plan_result,
                generation_mode=generation_mode,
                creative_direction=self._select_creative_direction(effective_prompt),
                retrieval_ms=0,
                started_at=started_at,
                should_stop=should_stop,
            )

        doc_refs = self.document_service.retrieve(
            workspace_id=workspace_id,
            prompt=effective_prompt,
            target_platform=target_platform.value,
        )
        self._append_event(job, "retrieval_completed", f"Retrieved {len(doc_refs)} grounded document references.")
        retrieval_ms = int((time.perf_counter() - started_at) * 1000)
        job.latency_breakdown["retrieval_ms"] = retrieval_ms
        job.retrieval_stats = {
            "doc_refs": len(doc_refs),
            "workspace_index": self.code_index_service.get_workspace_status(workspace_id).model_dump(mode="json"),
            "document_index": self.code_index_service.get_document_status(workspace_id).model_dump(mode="json"),
        }
        self._append_trace(
            workspace_id,
            "retrieval_completed",
            "Relevant documents and platform rules retrieved.",
            {"doc_refs": len(doc_refs), "retrieval_ms": retrieval_ms},
        )
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped

        chat_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="user",
            content=request.prompt,
            linked_job_id=job.job_id,
            linked_run_id=request.linked_run_id,
        )
        self.store.upsert("chat_turns", chat_turn.turn_id, chat_turn.model_dump(mode="json"))
        creative_direction = self._select_creative_direction(effective_prompt)
        self._append_trace(
            workspace_id,
            "creative_direction_selected",
            "Creative direction selected for this run.",
            creative_direction,
        )
        self._append_event(job, "spec_started", "Building grounded specification.")
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped

        spec_result = self._resolve_grounded_spec(
            workspace_id=workspace_id,
            prompt=effective_prompt,
            target_platform=target_platform,
            preview_profile=preview_profile,
            doc_refs=doc_refs,
            template_revision_id=workspace.current_revision_id or "template-unknown",
            prompt_turn_id=chat_turn.turn_id,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        if "error" in spec_result:
            self._append_trace(
                workspace_id,
                "spec_failed",
                "GroundedSpec generation failed.",
                {"error": spec_result["error"]},
            )
            return self._block_with_messages(
                job,
                [spec_result["error"]],
                code="generation.spec.llm_failure",
                event_type="spec_blocked",
                failure_reason=spec_result["error"],
            )
        grounded_spec: GroundedSpecModel = spec_result["spec"]
        grounded_spec = self._stabilize_grounded_spec(grounded_spec)
        if spec_result.get("warning"):
            self._append_trace(
                workspace_id,
                str(spec_result.get("warning_stage") or "spec_recovery_used"),
                str(spec_result.get("warning_title") or "GroundedSpec used a recovery path before validation."),
                {"warning": spec_result["warning"], "warning_kind": spec_result.get("warning_kind")},
            )
        if spec_result.get("model"):
            job.llm_model = str(spec_result["model"])
        self._append_trace(
            workspace_id,
            "spec_built",
            "GroundedSpec created.",
            {
                "product_goal": grounded_spec.product_goal,
                "actors": len(grounded_spec.actors),
                "flows": len(grounded_spec.user_flows),
                "api_requirements": len(grounded_spec.api_requirements),
                "model": spec_result.get("model"),
            },
        )

        spec_validation = self.validation_suite.validate_grounded_spec(grounded_spec)
        self._store_report(f"spec:{workspace_id}", grounded_spec.model_dump(mode="json"))
        self._store_report(
            f"assumptions:{workspace_id}",
            {"workspace_id": workspace_id, "assumptions": [item.model_dump(mode="json") for item in grounded_spec.assumptions]},
        )
        self._append_event(job, "spec_ready", "GroundedSpec created.")
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped
        if spec_validation.blocking:
            self._block_job(job, spec_validation, grounded_spec.assumptions, failure_reason="GroundedSpec validation blocked generation.")
            self._append_trace(
                workspace_id,
                "spec_validation_failed",
                "GroundedSpec validation blocked generation.",
                {"issues": [issue.model_dump(mode="json") for issue in spec_validation.issues]},
            )
            self._append_event(job, "spec_blocked", "GroundedSpec validation blocked generation.")
            return job

        draft_source = self.workspace_service.prepare_draft(workspace_id, draft_run_id)
        self._append_event(job, "draft_prepared", "Prepared draft workspace from the current revision.")
        self._append_trace(
            workspace_id,
            "draft_prepared",
            "Draft workspace prepared from the current revision.",
            {
                "draft_run_id": draft_run_id,
                "role_scope": role_scope,
            },
        )
        self._append_event(job, "role_contract_started", "Analyzing role responsibilities before planning.")
        role_contract_result = self._resolve_role_contract(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            doc_refs=doc_refs,
            role_scope=role_scope,
            intent=request.intent,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        if "error" in role_contract_result:
            self._append_trace(
                workspace_id,
                "role_contract_failed",
                "Role architecture analysis failed.",
                {"error": role_contract_result["error"]},
            )
            return self._block_with_messages(
                job,
                [role_contract_result["error"]],
                code="generation.role_contract.llm_failure",
                event_type="validation_failed",
                failure_reason=role_contract_result["error"],
            )
        role_contract = role_contract_result["role_contract"]
        role_contract_issues = self._role_contract_gate_issues(role_contract, role_scope, scope_mode=self._scope_mode(request.intent, effective_prompt, role_scope))
        if role_contract_issues:
            self._append_trace(
                workspace_id,
                "role_contract_blocked",
                "Role architecture analysis did not separate the selected roles clearly enough.",
                {"issues": role_contract_issues},
            )
            return self._block_with_messages(
                job,
                role_contract_issues,
                code="generation.role_contract.invalid",
                event_type="validation_failed",
                failure_reason="Role architecture analysis did not produce sufficiently distinct responsibilities.",
            )
        self._append_trace(
            workspace_id,
            "role_contract_ready",
            "Role responsibilities and boundaries were analyzed before page planning.",
            {
                "model": role_contract_result.get("model"),
                "roles": {
                    role: role_contract["roles"][role]["responsibility"]
                    for role in role_scope
                    if role in role_contract["roles"]
                },
            },
        )
        self._append_event(job, "role_contract_ready", "Role boundaries prepared for page planning.")
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped
        self._append_event(job, "planning_started", "Planning route graph and target files.")
        plan_result = self._resolve_code_plan(
            workspace_id=workspace_id,
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            doc_refs=doc_refs,
            role_scope=role_scope,
            role_contract=role_contract,
            intent=request.intent,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        if "error" in plan_result:
            self._append_trace(
                workspace_id,
                "planning_failed",
                "Code planning failed.",
                {"error": plan_result["error"]},
            )
            return self._block_with_messages(
                job,
                [plan_result["error"]],
                code="generation.plan.llm_failure",
                event_type="validation_failed",
                failure_reason=plan_result["error"],
            )
        if plan_result.get("model"):
            job.llm_model = str(plan_result["model"])
        self._append_trace(
            workspace_id,
            "planning_ready",
            "Code plan was built from grounded spec and workspace context.",
            {
                "files_to_read": len(plan_result["files_to_read"]),
                "target_files": len(plan_result["target_files"]),
                "model": plan_result.get("model"),
                "role_scope": role_scope,
                "active_role_scope": plan_result.get("active_role_scope"),
                "flow_mode": plan_result.get("flow_mode"),
                "scope_mode": plan_result.get("scope_mode"),
                "execution_plan": plan_result.get("execution_plan"),
            },
        )
        self._append_event(job, "planning_ready", "Code plan created.")
        self._store_report(
            f"role_contract:{workspace_id}",
            {"run_id": draft_run_id, "role_contract": role_contract},
        )
        self._store_report(
            f"page_graph:{workspace_id}",
            {"run_id": draft_run_id, "page_graph": plan_result["page_graph"]},
        )
        self._store_report(
            f"execution_plan:{workspace_id}",
            {"run_id": draft_run_id, "execution_plan": plan_result.get("execution_plan", {})},
        )
        self._store_resume_checkpoint(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            request=request,
            role_scope=role_scope,
            role_contract=role_contract,
            plan_result=plan_result,
        )
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped
        plan_gate_issues = self._page_graph_gate_issues(
            plan_result["page_graph"],
            role_scope,
            scope_mode=plan_result["scope_mode"],
            require_multi_page=bool(plan_result["require_multi_page"]),
        )
        if plan_gate_issues:
            self._append_trace(
                workspace_id,
                "planning_blocked",
                "Code planning was blocked because it did not produce a real page-based app structure.",
                {"issues": plan_gate_issues},
            )
            return self._block_with_messages(
                job,
                plan_gate_issues,
                code="generation.plan.placeholder_output",
                event_type="validation_failed",
                failure_reason="Code planning did not produce a valid multi-page app structure.",
            )

        return self._continue_generation_from_plan(
            workspace=workspace,
            workspace_id=workspace_id,
            job=job,
            request=request,
            draft_run_id=draft_run_id,
            draft_source=draft_source,
            effective_prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            role_contract=role_contract,
            plan_result=plan_result,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            retrieval_ms=retrieval_ms,
            started_at=started_at,
            should_stop=should_stop,
        )

    def _store_resume_checkpoint(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        request: GenerateRequest,
        role_scope: list[str],
        role_contract: dict[str, Any],
        plan_result: dict[str, Any],
    ) -> None:
        payload = {
            "workspace_id": workspace_id,
            "source_run_id": request.linked_run_id,
            "draft_run_id": draft_run_id,
            "status": "pending",
            "prompt": request.prompt,
            "intent": request.intent,
            "mode": request.mode,
            "generation_mode": request.generation_mode.value if isinstance(request.generation_mode, GenerationMode) else str(request.generation_mode),
            "target_platform": request.target_platform.value if hasattr(request.target_platform, "value") else str(request.target_platform),
            "preview_profile": request.preview_profile.value if hasattr(request.preview_profile, "value") else str(request.preview_profile),
            "target_role_scope": role_scope,
            "model_profile": request.model_profile,
            "page_graph": plan_result.get("page_graph"),
            "role_contract": role_contract,
            "scope_mode": plan_result.get("scope_mode"),
            "flow_mode": plan_result.get("flow_mode"),
            "files_to_read": list(plan_result.get("files_to_read") or []),
            "target_files": list(plan_result.get("target_files") or []),
            "shared_files": list(plan_result.get("shared_files") or []),
            "backend_targets": list(plan_result.get("backend_targets") or []),
            "execution_plan": plan_result.get("execution_plan") or {},
            "created_at": utc_now().isoformat(),
        }
        self._store_report(f"resume_checkpoint:{workspace_id}", payload)

    def _load_resume_checkpoint_bundle(self, workspace_id: str, source_run_id: str | None) -> dict[str, Any] | None:
        source_run = str(source_run_id or "").strip()
        if not source_run:
            return None
        checkpoint = self.store.get("reports", f"resume_checkpoint:{workspace_id}")
        if not checkpoint or checkpoint.get("status") != "pending":
            return None
        if str(checkpoint.get("source_run_id") or "") != source_run:
            return None
        spec_payload = self.current_report(workspace_id, "spec")
        role_contract_payload = self.current_report(workspace_id, "role_contract")
        if not spec_payload or not role_contract_payload:
            return None
        try:
            grounded_spec = GroundedSpecModel.model_validate(spec_payload)
        except Exception:
            return None
        role_contract = role_contract_payload.get("role_contract")
        page_graph = checkpoint.get("page_graph")
        if not isinstance(role_contract, dict) or not isinstance(page_graph, dict):
            return None
        workspace_tree = self.workspace_service.file_tree(workspace_id, run_id=source_run)
        valid_tree_paths = {
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        }
        target_files = self._normalize_path_list(checkpoint.get("target_files"), [])
        files_to_read = self._normalize_path_list(checkpoint.get("files_to_read"), [])
        shared_files = self._normalize_path_list(checkpoint.get("shared_files"), [])
        backend_targets = self._normalize_path_list(checkpoint.get("backend_targets"), [])
        target_files = [path for path in target_files if path in valid_tree_paths or path.startswith("frontend/") or path.startswith("backend/")]
        files_to_read = [path for path in files_to_read if path in valid_tree_paths]
        shared_files = [path for path in shared_files if path in valid_tree_paths]
        backend_targets = [path for path in backend_targets if path in valid_tree_paths or path.startswith("backend/")]
        plan_result = {
            "page_graph": page_graph,
            "scope_mode": checkpoint.get("scope_mode") or "minimal_patch",
            "flow_mode": checkpoint.get("flow_mode") or "multi_page",
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "execution_plan": checkpoint.get("execution_plan") or {},
            "require_multi_page": True,
        }
        if not plan_result["target_files"]:
            return None
        return {
            "checkpoint": checkpoint,
            "grounded_spec": grounded_spec,
            "role_contract": role_contract,
            "plan_result": plan_result,
            "role_scope": list(checkpoint.get("target_role_scope") or []),
        }

    def _continue_generation_from_plan(
        self,
        *,
        workspace: WorkspaceRecord,
        workspace_id: str,
        job: JobRecord,
        request: GenerateRequest,
        draft_run_id: str,
        draft_source: Path,
        effective_prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        plan_result: dict[str, Any],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        retrieval_ms: int,
        started_at: float,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord:
        self._append_event(
            job,
            "context_pack_started",
            f"Collecting targeted file context for {len(plan_result['target_files'])} planned files.",
        )
        context_pack = self.context_pack_builder.build(
            workspace=workspace,
            prompt=effective_prompt,
            model_profile=request.model_profile,
            generation_mode=generation_mode,
            active_paths=plan_result["files_to_read"],
            target_files=plan_result["target_files"],
            run_id=draft_run_id,
        )
        files_read = sorted(set(plan_result["files_to_read"]) | set(context_pack.targeted_files.keys()) | {chunk.path for chunk in context_pack.code_chunks})
        file_contexts: dict[str, str] = dict(context_pack.targeted_files)
        for file_path in plan_result["files_to_read"]:
            if file_path in file_contexts:
                continue
            try:
                file_contexts[file_path] = self.workspace_service.read_file(workspace_id, file_path, run_id=draft_run_id)
            except FileNotFoundError:
                continue
        current_cache_stats = ACTIVE_LLM_CACHE_STATS.get() or {}
        current_cache_stats["prompt_cache_key"] = context_pack.prompt_cache_key
        current_cache_stats["stable_prefix_chars"] = len(context_pack.system_prefix)
        job.cache_stats = dict(current_cache_stats)
        job.latency_breakdown["context_pack_ms"] = max(0, int((time.perf_counter() - started_at) * 1000) - retrieval_ms)
        self._append_event(
            job,
            "context_pack_ready",
            f"Context pack ready with {len(context_pack.code_chunks)} code chunks, {len(context_pack.doc_chunks)} doc chunks, and {len(context_pack.targeted_files)} file bodies.",
        )

        self._append_event(job, "editing_started", "Generating draft file edits.")
        edit_result = self._resolve_code_edits(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            file_contexts=file_contexts,
            target_files=plan_result["target_files"],
            role_contract=role_contract,
            page_graph=plan_result["page_graph"],
            intent=request.intent,
            scope_mode=plan_result["scope_mode"],
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        stopped = self._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped
        if "error" in edit_result:
            self._append_trace(workspace_id, "editing_failed", "Code editing failed.", {"error": edit_result["error"]})
            return self._block_with_messages(
                job,
                [edit_result["error"]],
                code="generation.edit.llm_failure",
                event_type="validation_failed",
                failure_reason=edit_result["error"],
            )
        edit_gate_issues = self._edit_gate_issues(
            plan_result["page_graph"],
            edit_result["operations"],
            role_scope,
            scope_mode=plan_result["scope_mode"],
            target_files=plan_result["target_files"],
        )
        if edit_gate_issues:
            self._append_trace(
                workspace_id,
                "editing_blocked",
                "Code editing was blocked because the draft still collapses into placeholder surfaces.",
                {"issues": edit_gate_issues},
            )
            return self._block_with_messages(
                job,
                edit_gate_issues,
                code="generation.edit.placeholder_output",
                event_type="validation_failed",
                failure_reason="Code editing did not produce the required page-based app structure.",
            )
        invalid_operation_paths = [
            operation.file_path
            for operation in edit_result["operations"]
            if self._normalize_path_list([operation.file_path], []) != [operation.file_path]
        ]
        if invalid_operation_paths:
            self._append_trace(
                workspace_id,
                "editing_failed",
                "Code editing produced invalid file paths.",
                {"invalid_paths": invalid_operation_paths[:10]},
            )
            return self._block_with_messages(
                job,
                [f"Code editing produced invalid file paths: {', '.join(invalid_operation_paths[:5])}"],
                code="generation.edit.invalid_paths",
                event_type="validation_failed",
                failure_reason="Code editing produced invalid file paths.",
            )
        operations = [
            DraftFileOperation(
                file_path="artifacts/grounded_spec.json",
                operation="replace",
                content=json_dumps(grounded_spec.model_dump(mode="json")),
                reason="Persist the grounded planning artifact inside the draft workspace.",
            ),
            *edit_result["operations"],
        ]
        patch_envelope = self.workspace_service.build_patch_envelope_for_draft(workspace_id, draft_run_id, operations)
        apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, draft_run_id, patch_envelope)
        if apply_result.status != "applied":
            return self._block_with_messages(
                job,
                [apply_result.conflict_reason or "Draft patch could not be applied safely."],
                code="generation.patch.conflict",
                event_type="job_failed",
                failure_reason=apply_result.conflict_reason or "Draft patch could not be applied safely.",
            )
        job.apply_result = apply_result.model_dump(mode="json")
        job.latency_breakdown["patch_apply_ms"] = max(0, int((time.perf_counter() - started_at) * 1000) - retrieval_ms)
        latest_operations = list(operations)
        all_operations = list(operations)
        iterations: list[RunIterationRecord] = []
        latest_check_results: list[RunCheckResult] = []
        repair_iterations: list[RepairIterationRecord] = []
        latest_candidate_diff = ""
        latest_preview = self.preview_service.get(workspace_id)
        latest_assistant_message = edit_result["assistant_message"]
        repair_attempt_limit = self._repair_attempt_limit(generation_mode, request.intent)
        repair_contexts = self._collect_existing_file_contexts(workspace_id, draft_run_id, plan_result["target_files"])

        for attempt in range(repair_attempt_limit + 1):
            stopped = self._stop_if_requested(job, workspace_id, should_stop)
            if stopped is not None:
                return stopped
            latest_candidate_diff = self.workspace_service.diff(workspace_id, run_id=draft_run_id)
            if attempt == 0:
                self._append_event(job, "iteration_ready", f"Prepared {len(latest_operations)} draft file operations.")
            else:
                self._append_event(job, "repair_iteration", f"Applied repair iteration {attempt}.")
            self._append_event(job, "build_started", "Build validation started.")
            check_execution = self.check_runner.run(
                workspace_id=workspace_id,
                run_id=draft_run_id,
                source_dir=draft_source,
                changed_files=sorted({operation.file_path for operation in latest_operations}),
                preview_run_id=draft_run_id,
                scope_mode=plan_result["scope_mode"],
            )
            latest_preview = self.preview_service.get(workspace_id)
            latest_check_results = check_execution.results
            check_failure = self.check_runner.classify_failure(latest_check_results)
            check_issues = self.check_runner.failing_issues(latest_check_results)
            build_issues = [issue for issue in check_issues if issue.location != "preview"]
            preview_issue = next((issue for issue in check_issues if issue.location == "preview"), None)
            tooling_failure = self.check_runner.has_tooling_failure(latest_check_results)
            job.latency_breakdown["checks_ms"] = (job.latency_breakdown.get("checks_ms", 0) or 0) + (check_execution.duration_ms or 0)
            self._append_trace(
                workspace_id,
                "checks_completed",
                "Structured check pipeline finished for the current draft iteration.",
                {
                    "attempt": attempt,
                    "failure_class": check_failure,
                    "results": [item.model_dump(mode="json") for item in latest_check_results],
                },
            )
            failed_checks = [item.name for item in latest_check_results if item.status == "failed"]
            self._append_event(
                job,
                "checks_completed",
                "Checks completed." if not failed_checks else f"Checks completed with failures: {', '.join(failed_checks)}.",
                {"failed_checks": failed_checks, "attempt": attempt},
            )
            iteration = RunIterationRecord(
                run_id=draft_run_id,
                assistant_message=latest_assistant_message,
                files_read=files_read if attempt == 0 else sorted(repair_contexts.keys()),
                operations=[
                    RunIterationOperation(file_path=operation.file_path, operation=operation.operation, reason=operation.reason)
                    for operation in latest_operations
                ],
                check_results=latest_check_results,
                diff_summary=self._diff_summary(latest_candidate_diff),
                role_scope=role_scope,
                latency_breakdown={"checks_ms": check_execution.duration_ms or 0},
                failure_class=check_failure,
            )
            iterations.append(iteration)
            if attempt > 0:
                repair_iterations.append(
                    RepairIterationRecord(
                        run_id=draft_run_id,
                        attempt=attempt,
                        files_read=sorted(repair_contexts.keys()),
                        files_changed=sorted({operation.file_path for operation in latest_operations}),
                        failure_class=check_failure,
                        check_results=latest_check_results,
                        latency_breakdown={"checks_ms": check_execution.duration_ms or 0},
                    )
                )
            self._store_report(f"iterations:{workspace_id}", {"run_id": draft_run_id, "items": [item.model_dump(mode="json") for item in iterations]})
            self._store_report(f"candidate_diff:{workspace_id}", {"run_id": draft_run_id, "diff": latest_candidate_diff})
            self._store_report(
                f"check_results:{workspace_id}",
                {"run_id": draft_run_id, "items": [item.model_dump(mode="json") for item in latest_check_results], "execution": check_execution.model_dump(mode="json")},
            )
            self._store_report(
                f"patch:{workspace_id}",
                {"run_id": draft_run_id, "envelope": patch_envelope.model_dump(mode="json"), "apply_result": job.apply_result, "conflict_reason": apply_result.conflict_reason},
            )

            if not build_issues and (preview_issue is None or self._is_non_blocking_preview_issue(preview_issue)):
                break

            if tooling_failure:
                final_issues = [issue.model_dump(mode="json") for issue in build_issues]
                if preview_issue is not None:
                    final_issues.append(preview_issue.model_dump(mode="json"))
                job.status = "failed"
                job.failure_class = check_failure or "tooling/runtime_misconfiguration"
                job.root_cause_summary = self._summarize_failed_checks(build_issues, preview_issue) or "Frontend build tooling is unavailable in the backend runtime."
                job.failure_reason = f"Build validation could not run because the platform runtime is missing required tooling. Root cause: {job.root_cause_summary}"
                job.fix_targets = []
                job.handoff_from_failed_generate = None
                job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=False, blocking=True, issues=final_issues)
                self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
                self._append_event(job, "job_failed", job.failure_reason)
                job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
                return job

            if attempt >= repair_attempt_limit:
                final_issues = [issue.model_dump(mode="json") for issue in build_issues]
                if preview_issue is not None:
                    final_issues.append(preview_issue.model_dump(mode="json"))
                job.status = "failed"
                job.failure_reason = "Build validation failed after automatic repair attempts." if build_issues else "Preview rebuild failed after automatic repair attempts."
                job.failure_class = check_failure or self._failure_class_from_error_context(request.error_context)
                job.root_cause_summary = self._summarize_failed_checks(build_issues, preview_issue) or job.root_cause_summary
                if job.root_cause_summary:
                    job.failure_reason = f"{job.failure_reason} Root cause: {job.root_cause_summary}"
                job.fix_targets = sorted({issue.location for issue in check_issues if issue.location and issue.location not in {"generation", "preview"}})
                job.handoff_from_failed_generate = self._build_fix_handoff(
                    prompt=request.prompt,
                    failure_reason=job.failure_reason,
                    failure_class=job.failure_class,
                    issues=check_issues,
                    mode=request.mode,
                )
                job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=not bool(build_issues), blocking=True, issues=final_issues)
                self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
                self._append_event(job, "job_failed", job.failure_reason)
                job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
                return job

            self._append_event(job, "repair_started", "Build or compile checks failed. Preparing the next repair attempt.")
            repair_result = self._repair_draft_after_failure(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                prompt=effective_prompt,
                grounded_spec=grounded_spec,
                role_scope=role_scope,
                role_contract=role_contract,
                page_graph=plan_result["page_graph"],
                scope_mode=plan_result["scope_mode"],
                target_files=plan_result["target_files"],
                file_contexts=repair_contexts,
                build_issues=build_issues,
                preview_issue=preview_issue,
                preview_logs=latest_preview.logs if preview_issue is not None else [],
                attempt=attempt + 1,
            )
            if "error" in repair_result:
                issues = [issue.model_dump(mode="json") for issue in build_issues]
                if preview_issue is not None:
                    issues.append(preview_issue.model_dump(mode="json"))
                issues.append(
                    ValidationIssue(
                        code="repair.generation_failed",
                        message=str(repair_result["error"]),
                        severity="high",
                        location="repair",
                        blocking=True,
                    ).model_dump(mode="json")
                )
                job.status = "failed"
                job.failure_reason = str(repair_result["error"])
                job.failure_class = check_failure or self._failure_class_from_error_context(request.error_context)
                job.root_cause_summary = self._summarize_failed_checks(build_issues, preview_issue) or job.root_cause_summary
                if job.root_cause_summary and job.root_cause_summary not in job.failure_reason:
                    job.failure_reason = f"{job.failure_reason} Root cause: {job.root_cause_summary}"
                job.fix_targets = sorted({issue.location for issue in check_issues if issue.location and issue.location not in {"generation", "preview"}})
                job.handoff_from_failed_generate = self._build_fix_handoff(
                    prompt=request.prompt,
                    failure_reason=job.failure_reason,
                    failure_class=job.failure_class,
                    issues=check_issues,
                    mode=request.mode,
                )
                job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=not bool(build_issues), blocking=True, issues=issues)
                self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
                self._append_event(job, "job_failed", job.failure_reason)
                return job

            latest_operations = repair_result["operations"]
            latest_assistant_message = repair_result["assistant_message"] or latest_assistant_message
            all_operations.extend(latest_operations)
            patch_envelope = self.workspace_service.build_patch_envelope_for_draft(workspace_id, draft_run_id, latest_operations)
            apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, draft_run_id, patch_envelope)
            job.apply_result = apply_result.model_dump(mode="json")
            if apply_result.status != "applied":
                issues = [issue.model_dump(mode="json") for issue in build_issues]
                issues.append(
                    ValidationIssue(
                        code="repair.patch.conflict",
                        message=apply_result.conflict_reason or "Repair patch could not be applied safely.",
                        severity="high",
                        location="repair",
                        blocking=True,
                    ).model_dump(mode="json")
                )
                job.status = "failed"
                job.failure_reason = apply_result.conflict_reason or "Repair patch could not be applied safely."
                job.failure_class = "patch_conflict"
                job.root_cause_summary = job.failure_reason
                job.fix_targets = sorted({issue.location for issue in build_issues if issue.location and issue.location not in {"generation", "preview"}})
                job.handoff_from_failed_generate = self._build_fix_handoff(
                    prompt=request.prompt,
                    failure_reason=job.failure_reason,
                    failure_class=job.failure_class,
                    issues=build_issues,
                    mode=request.mode,
                )
                job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=False, blocking=True, issues=issues)
                self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
                self._append_event(job, "job_failed", job.failure_reason)
                job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
                return job
            repair_contexts = self._collect_existing_file_contexts(workspace_id, draft_run_id, plan_result["target_files"])
            stopped = self._stop_if_requested(job, workspace_id, should_stop)
            if stopped is not None:
                return stopped

        traceability = self._build_agent_traceability_report(workspace_id, grounded_spec, all_operations)
        self._store_report(f"traceability:{workspace_id}", traceability.model_dump(mode="json"))
        summary = self._build_agent_summary(
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            operations=all_operations,
            generation_mode=generation_mode,
            assistant_message=latest_assistant_message,
        )
        assistant_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="assistant",
            content=summary,
            summary=summary,
            linked_job_id=job.job_id,
            linked_run_id=request.linked_run_id,
        )
        self.store.upsert("chat_turns", assistant_turn.turn_id, assistant_turn.model_dump(mode="json"))

        job.status = "completed"
        job.failure_reason = None
        job.summary = summary
        job.traceability_report_id = traceability.report_id
        job.assumptions_report = [item.model_dump(mode="json") for item in grounded_spec.assumptions]
        job.fix_targets = sorted({operation.file_path for operation in all_operations})
        job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=True, blocking=False, issues=[])
        job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
        job.latency_breakdown["ttft_ms"] = retrieval_ms
        job.latency_breakdown["total_ms"] = int((time.perf_counter() - started_at) * 1000)
        job.cache_stats = dict(ACTIVE_LLM_CACHE_STATS.get() or job.cache_stats)
        job.compile_summary = self._compile_code_summary(all_operations, role_scope)
        job.artifacts = {
            "preview_url": latest_preview.url or "",
            "grounded_spec": "reports/spec",
            "traceability": "reports/traceability",
            "candidate_diff": "reports/candidate_diff",
            "iterations": "reports/iterations",
            "check_results": "reports/check_results",
            "patch": "reports/patch",
            "role_contract": "reports/role_contract",
            "page_graph": "reports/page_graph",
            "fidelity": job.fidelity,
        }
        self.store.delete("reports", f"resume_checkpoint:{workspace_id}")
        self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, "draft_ready", "Draft is ready for review.")
        self._append_event(job, "job_completed", "Generation completed successfully.")
        self._append_trace(
            workspace_id,
            "job_completed",
            "Generation completed successfully.",
            {"summary": summary, "compile_summary": job.compile_summary, "artifacts": job.artifacts},
        )
        return job

    def retry(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            mode=job.mode,
            target_platform=self._target_platform(job.target_platform),
            preview_profile=self._preview_profile(job.preview_profile),
            generation_mode=self._generation_mode(job.generation_mode),
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
            if item["workspace_id"] == workspace_id
        ]
        if not jobs:
            return None
        jobs.sort(key=lambda item: item.created_at)
        return jobs[-1]

    def _resolve_role_contract(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        intent: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.FAST or self._should_use_compiled_role_contract(
            prompt=prompt,
            role_scope=role_scope,
            intent=intent,
            generation_mode=generation_mode,
        ):
            return {"role_contract": self._compiled_role_contract(grounded_spec, role_scope)}
        try:
            payload = self._generate_structured_with_retry(
                role="code_plan",
                schema_name="role_contract_v1",
                schema=self._role_contract_schema(),
                system_prompt=self._role_contract_system_prompt(),
                user_prompt=self._role_contract_user_prompt(
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    intent=intent,
                    creative_direction=creative_direction,
                ),
            )
            normalized = self._normalize_model_payload(payload["payload"])
            role_contract = self._normalize_role_contract(normalized, role_scope)
            return {"role_contract": role_contract, "model": payload["model"]}
        except Exception as exc:
            return {"error": f"Role architecture analysis failed: {exc}"}

    def _should_use_compiled_role_contract(
        self,
        *,
        prompt: str,
        role_scope: list[str],
        intent: str,
        generation_mode: GenerationMode,
    ) -> bool:
        if generation_mode != GenerationMode.BALANCED:
            return False
        if self._scope_mode(intent, prompt, role_scope) == "minimal_patch":
            return True
        lowered = prompt.lower()
        if len(role_scope) == len(ROLE_ORDER) and intent in {"create", "refine"}:
            simple_markers = ("simple", "basic", "minimal", "fast", "quick draft", "template-safe")
            return any(marker in lowered for marker in simple_markers)
        return False

    def _resolve_code_plan(
        self,
        *,
        workspace_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        intent: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        scope_mode = self._scope_mode(intent, prompt, role_scope)
        require_multi_page = self._requires_multi_page(prompt, grounded_spec, role_scope, intent)
        try:
            workspace_tree = self.workspace_service.file_tree(workspace_id)
            payload = self._generate_code_plan_sections(
                prompt=prompt,
                grounded_spec=grounded_spec,
                doc_refs=doc_refs,
                role_scope=role_scope,
                role_contract=role_contract,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
            normalized = self._normalize_model_payload(payload["payload"])
            planned = self._normalize_page_plan(
                normalized,
                role_scope=role_scope,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
            )
            planned["model"] = payload["model"]
            return planned
        except Exception as exc:
            return {"error": f"Page graph planning failed: {exc}"}

    def _generate_code_plan_sections(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        graph_payload = self._generate_structured_with_retry(
            role="code_plan",
            schema_name="page_graph_structure_v1",
            schema=self._code_plan_partial_schema(["summary", "flow_mode", "page_graph"]),
            system_prompt=self._code_plan_section_system_prompt("Page graph and route structure"),
            user_prompt=self._code_plan_section_user_prompt(
                section_id="graph",
                section_title="Page graph and route structure",
                section_contract=[
                    "Return the real page graph, role routes, and page definitions.",
                    "Keep role surfaces distinct and multi-page when required.",
                    "Do not decide final file-read lists in this section.",
                ],
                prompt=prompt,
                grounded_spec=grounded_spec,
                doc_refs=doc_refs,
                role_scope=role_scope,
                role_contract=role_contract,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            ),
        )
        targeting_payload = self._generate_structured_with_retry(
            role="code_plan",
            schema_name="page_graph_targeting_v1",
            schema=self._code_plan_partial_schema(["files_to_read", "target_files", "shared_files", "backend_targets"]),
            system_prompt=self._code_plan_section_system_prompt("File targeting and read set"),
            user_prompt=self._code_plan_section_user_prompt(
                section_id="targeting",
                section_title="File targeting and read set",
                section_contract=[
                    "Return only read-set and file-target lists.",
                    "Target files must stay minimal for minimal_patch requests.",
                    "Use the page graph implied by the request and role contract, but do not re-emit full page definitions.",
                ],
                prompt=prompt,
                grounded_spec=grounded_spec,
                doc_refs=doc_refs,
                role_scope=role_scope,
                role_contract=role_contract,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            ),
        )
        merged_payload = {
            **self._normalize_model_payload(graph_payload["payload"]),
            **self._normalize_model_payload(targeting_payload["payload"]),
        }
        return {
            "model": targeting_payload["model"],
            "payload": merged_payload,
            "response_mode": "code_plan_sections",
        }

    def _resolve_code_edits(
        self,
        *,
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
        target_set = set(target_files)
        page_operations: list[DraftFileOperation] = []
        page_messages: list[str] = []
        generated_page_sources: dict[str, str] = {}
        generated_backend_sources: dict[str, str] = {}
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
            if any("error" in result and result.get("retryable") for result in ordered_page_results):
                for index, page_result in enumerate(ordered_page_results):
                    if "error" not in page_result or not page_result.get("retryable"):
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
            assert page_result is not None
            if "error" in page_result:
                return page_result
            operation = page_result["operation"]
            page_operations.append(operation)
            if operation.content is not None:
                generated_page_sources[operation.file_path] = operation.content
            page_messages.append(page_result["assistant_message"])

        backend_targets = self._backend_target_files(page_graph, target_set)
        backend_result = self._resolve_composition_edit(
            prompt=prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            role_contract=role_contract,
            page_graph=page_graph,
            scope_mode=scope_mode,
            intent=intent,
            stage_name="backend",
            target_files=backend_targets,
            file_contexts=file_contexts,
            generated_page_sources=generated_page_sources,
            generated_support_sources={},
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        if "error" in backend_result:
            return backend_result
        for operation in backend_result["operations"]:
            if operation.content is not None:
                generated_backend_sources[operation.file_path] = operation.content

        frontend_targets = self._frontend_target_files(page_graph, target_set)
        frontend_result = self._resolve_composition_edit(
            prompt=prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            role_contract=role_contract,
            page_graph=page_graph,
            scope_mode=scope_mode,
            intent=intent,
            stage_name="frontend",
            target_files=frontend_targets,
            file_contexts=file_contexts,
            generated_page_sources=generated_page_sources,
            generated_support_sources=generated_backend_sources,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        if "error" in frontend_result:
            return frontend_result

        operations = self._dedupe_operations(
            [
                DraftFileOperation(
                    file_path="artifacts/generated_app_graph.json",
                    operation="replace",
                    content=json_dumps(page_graph),
                    reason="Persist the LLM-generated page graph for validation, preview, and run artifacts.",
                ),
                *page_operations,
                *backend_result["operations"],
                *frontend_result["operations"],
            ]
        )
        assistant_parts = [message for message in page_messages if message]
        if backend_result["assistant_message"]:
            assistant_parts.append(backend_result["assistant_message"])
        if frontend_result["assistant_message"]:
            assistant_parts.append(frontend_result["assistant_message"])
        assistant_message = " ".join(assistant_parts).strip() or (
            f"Generated {len(page_operations)} page files and composed backend then frontend wiring for a {scope_mode} run."
        )
        return {"assistant_message": assistant_message, "operations": operations}

    @staticmethod
    def _normalize_path_list(value: Any, fallback: list[str] | None = None) -> list[str]:
        if not isinstance(value, list):
            return list(fallback or [])
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip().lstrip("/")
            if not candidate or ".." in candidate:
                continue
            if any(char.isspace() for char in candidate):
                continue
            normalized.append(candidate)
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if candidate:
                values.append(candidate)
        return list(dict.fromkeys(values))

    def _normalize_role_contract(self, payload: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles_raw = payload.get("roles")
        if not isinstance(roles_raw, list):
            raise ValueError("Role contract is missing the roles array.")
        roles: dict[str, dict[str, Any]] = {}
        for item in roles_raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in role_scope:
                continue
            roles[role] = {
                "role": role,
                "responsibility": str(item.get("responsibility") or "").strip(),
                "entry_goal": str(item.get("entry_goal") or "").strip(),
                "primary_jobs": self._normalize_string_list(item.get("primary_jobs")),
                "key_entities": self._normalize_string_list(item.get("key_entities")),
                "ui_style_notes": self._normalize_string_list(item.get("ui_style_notes")),
                "success_states": self._normalize_string_list(item.get("success_states")),
                "must_differ_from": [
                    value for value in self._normalize_string_list(item.get("must_differ_from")) if value in ROLE_ORDER and value != role
                ],
            }
        return {
            "app_title": str(payload.get("app_title") or "").strip(),
            "app_summary": str(payload.get("app_summary") or "").strip(),
            "shared_entities": self._normalize_string_list(payload.get("shared_entities")),
            "shared_logic": self._normalize_string_list(payload.get("shared_logic")),
            "roles": roles,
        }

    def _compiled_role_contract(self, grounded_spec: GroundedSpecModel, role_scope: list[str]) -> dict[str, Any]:
        entities = [entity.name for entity in grounded_spec.domain_entities if entity.name]
        flows = grounded_spec.user_flows
        roles: dict[str, dict[str, Any]] = {}
        for role in role_scope:
            actor = next((item for item in grounded_spec.actors if item.role == role), None)
            role_flows = [flow for flow in flows if any(step.actor_id == getattr(actor, "actor_id", "") for step in flow.steps)]
            ui_requirements = [
                requirement
                for requirement in grounded_spec.ui_requirements
                if role in (f"{requirement.description} {requirement.screen_hint or ''}".lower())
            ]
            primary_jobs = [flow.name for flow in role_flows[:4] if flow.name]
            key_entities = entities[:4]
            ui_style_notes = [item.description for item in ui_requirements[:3] if item.description]
            success_states = [
                criterion
                for flow in role_flows[:2]
                for criterion in flow.acceptance_criteria[:1]
                if criterion
            ] or primary_jobs[:2]
            roles[role] = {
                "role": role,
                "responsibility": getattr(actor, "description", None) or f"{role.capitalize()} workflow execution.",
                "entry_goal": primary_jobs[0] if primary_jobs else f"Open the {role} workspace and continue the main flow.",
                "primary_jobs": primary_jobs or [f"Handle the main {role} flow."],
                "key_entities": key_entities,
                "ui_style_notes": ui_style_notes or [f"Keep {role}-specific actions visible above generic metrics."],
                "success_states": success_states or [f"{role.capitalize()} completes the intended task without cross-role confusion."],
                "must_differ_from": [candidate for candidate in ROLE_ORDER if candidate in role_scope and candidate != role],
            }
        return {
            "app_title": grounded_spec.product_goal[:80] if grounded_spec.product_goal else "Generated mini-app",
            "app_summary": grounded_spec.product_goal or "Generated role-aware mini-app workspace.",
            "shared_entities": entities[:6],
            "shared_logic": [flow.name for flow in flows[:4] if flow.name],
            "roles": roles,
        }

    def _normalize_page_plan(
        self,
        payload: dict[str, Any],
        *,
        role_scope: list[str],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
    ) -> dict[str, Any]:
        raw_graph = payload.get("page_graph")
        if not isinstance(raw_graph, dict):
            raise ValueError("Page graph payload is missing.")

        raw_roles = raw_graph.get("roles")
        roles_source: dict[str, dict[str, Any]] = {}
        if isinstance(raw_roles, list):
            for item in raw_roles:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                if role in role_scope:
                    roles_source[role] = item
        elif isinstance(raw_roles, dict):
            for role, item in raw_roles.items():
                if role in role_scope and isinstance(item, dict):
                    roles_source[role] = item

        shared_files = self._normalize_path_list(raw_graph.get("shared_files") or payload.get("shared_files"), list(SHARED_GENERATED_FILES))
        backend_targets = self._normalize_path_list(raw_graph.get("backend_targets") or payload.get("backend_targets"), [])
        roles: dict[str, dict[str, Any]] = {}
        graph_page_targets: list[str] = []

        for role in role_scope:
            role_payload = roles_source.get(role)
            if not role_payload:
                raise ValueError(f"Page graph is missing the {role} role.")
            pages_raw = role_payload.get("pages")
            if not isinstance(pages_raw, list) or not pages_raw:
                raise ValueError(f"Page graph is missing page definitions for {role}.")
            pages = [self._normalize_page_definition(role, page, index) for index, page in enumerate(pages_raw)]
            route_candidates = self._normalize_path_list([role_payload.get("routes_file")], [])
            routes_file = route_candidates[0] if route_candidates else self._default_routes_file(role)
            roles[role] = {
                "entry_path": str(role_payload.get("entry_path") or "/").strip() or "/",
                "landing_page_id": str(role_payload.get("landing_page_id") or pages[0]["page_id"]).strip() or pages[0]["page_id"],
                "routes_file": routes_file,
                "pages": pages,
            }
            graph_page_targets.extend(page["file_path"] for page in pages)

        computed_targets = list(
            dict.fromkeys(
                [
                    *shared_files,
                    *backend_targets,
                    *(role_payload["routes_file"] for role_payload in roles.values()),
                    *graph_page_targets,
                ]
            )
        )
        raw_target_files = self._normalize_path_list(payload.get("target_files"), [])
        if scope_mode == "minimal_patch" and raw_target_files:
            computed_target_set = set(computed_targets)
            intersection = [path for path in raw_target_files if path in computed_target_set]
            if computed_targets and not intersection:
                target_files = list(dict.fromkeys(computed_targets))
            else:
                target_files = list(dict.fromkeys([*raw_target_files, *computed_targets]))
        else:
            target_files = list(dict.fromkeys([*raw_target_files, *computed_targets]))

        raw_files_to_read = self._normalize_path_list(payload.get("files_to_read"), [])
        files_to_read = self._collect_files_to_read(raw_files_to_read, target_files, workspace_tree)
        flow_mode = str(raw_graph.get("flow_mode") or payload.get("flow_mode") or ("multi_page" if require_multi_page else "single_page"))
        execution_plan = self._build_execution_plan(
            role_scope=role_scope,
            roles=roles,
            shared_files=shared_files,
            backend_targets=backend_targets,
            target_files=target_files,
        )

        return {
            "summary": str(payload.get("summary") or raw_graph.get("summary") or "").strip(),
            "flow_mode": flow_mode,
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "active_role_scope": execution_plan["active_role_scope"],
            "execution_plan": execution_plan,
            "page_graph": {
                "app_title": str(raw_graph.get("app_title") or "").strip(),
                "summary": str(raw_graph.get("summary") or payload.get("summary") or "").strip(),
                "flow_mode": flow_mode,
                "scope_mode": scope_mode,
                "role_scope": role_scope,
                "shared_files": shared_files,
                "backend_targets": backend_targets,
                "roles": roles,
            },
            "scope_mode": scope_mode,
            "require_multi_page": require_multi_page,
        }

    def _normalize_page_definition(self, role: str, payload: Any, index: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"Page definition #{index + 1} for {role} is invalid.")
        component_name = self._component_name(role, payload, index)
        route_path = str(payload.get("route_path") or "").strip() or ("/" if index == 0 else f"/page-{index + 1}")
        if not route_path.startswith("/"):
            route_path = f"/{route_path}"
        file_path_candidates = self._normalize_path_list([payload.get("file_path")], [])
        return {
            "page_id": str(payload.get("page_id") or f"{role}_{index + 1}").strip() or f"{role}_{index + 1}",
            "route_path": route_path,
            "navigation_label": str(payload.get("navigation_label") or payload.get("title") or component_name).strip(),
            "component_name": component_name,
            "file_path": file_path_candidates[0] if file_path_candidates else self._default_page_file(role, component_name),
            "title": str(payload.get("title") or component_name).strip(),
            "description": str(payload.get("description") or payload.get("purpose") or "").strip(),
            "purpose": str(payload.get("purpose") or payload.get("description") or "").strip(),
            "page_kind": str(payload.get("page_kind") or "workspace").strip(),
            "primary_actions": self._normalize_string_list(payload.get("primary_actions")),
            "data_dependencies": self._normalize_string_list(payload.get("data_dependencies")),
            "loading_state": str(payload.get("loading_state") or "").strip(),
            "empty_state": str(payload.get("empty_state") or "").strip(),
            "error_state": str(payload.get("error_state") or "").strip(),
        }

    @staticmethod
    def _default_routes_file(role: str) -> str:
        return f"frontend/src/roles/{role}/{ROLE_COMPONENT_PREFIX[role]}Routes.tsx"

    @staticmethod
    def _default_page_file(role: str, component_name: str) -> str:
        return f"frontend/src/roles/{role}/pages/generated/{component_name}.tsx"

    def _component_name(self, role: str, payload: dict[str, Any], index: int) -> str:
        raw_value = str(payload.get("component_name") or payload.get("title") or payload.get("page_id") or f"{role}_page_{index + 1}").strip()
        cleaned = re.sub(r"[^0-9A-Za-z]+", " ", raw_value)
        pascal = "".join(part[:1].upper() + part[1:] for part in cleaned.split() if part)
        prefix = ROLE_COMPONENT_PREFIX[role]
        if not pascal:
            return f"{prefix}GeneratedPage{index + 1}"
        if not pascal.startswith(prefix):
            pascal = f"{prefix}{pascal}"
        if not pascal.endswith("Page"):
            pascal = f"{pascal}Page"
        return pascal

    @staticmethod
    def _build_execution_plan(
        *,
        role_scope: list[str],
        roles: dict[str, dict[str, Any]],
        shared_files: list[str],
        backend_targets: list[str],
        target_files: list[str],
    ) -> dict[str, Any]:
        target_set = set(target_files)
        role_steps: list[dict[str, Any]] = []
        active_role_scope: list[str] = []
        for role in role_scope:
            pages = roles.get(role, {}).get("pages") or []
            selected_files = [
                str(page.get("file_path"))
                for page in pages
                if isinstance(page, dict) and isinstance(page.get("file_path"), str) and page.get("file_path") in target_set
            ]
            routes_file = roles.get(role, {}).get("routes_file")
            if isinstance(routes_file, str) and routes_file in target_set:
                selected_files.append(routes_file)
            selected_files = list(dict.fromkeys(selected_files))
            if selected_files:
                active_role_scope.append(role)
            role_steps.append(
                {
                    "role": role,
                    "status": "complete" if selected_files else "complete",
                    "target_files": selected_files,
                    "skipped": not bool(selected_files),
                }
            )

        backend_files = [path for path in backend_targets if path in target_set]
        frontend_files = [
            path
            for path in list(dict.fromkeys(shared_files))
            if path in target_set and path not in backend_files
        ]
        return {
            "role_steps": role_steps,
            "backend": {
                "status": "complete" if not backend_files else "pending",
                "target_files": backend_files,
                "skipped": not bool(backend_files),
            },
            "frontend": {
                "status": "complete" if not frontend_files else "pending",
                "target_files": frontend_files,
                "skipped": not bool(frontend_files),
            },
            "active_role_scope": active_role_scope,
        }

    def _collect_files_to_read(
        self,
        files_to_read: list[str],
        target_files: list[str],
        workspace_tree: list[dict[str, str]],
    ) -> list[str]:
        existing_files = {
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        }
        ordered = list(
            dict.fromkeys(
                [
                    *[path for path in DESIGN_REFERENCE_FILES if path in existing_files],
                    *files_to_read,
                    *[path for path in target_files if path in existing_files],
                ]
            )
        )
        return ordered

    @staticmethod
    def _page_edit_parallelism(*, scope_mode: str, generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            default = "2"
        else:
            default = "2" if scope_mode == "minimal_patch" else "3"
        configured = max(1, int(os.getenv("PAGE_EDIT_MAX_PARALLELISM", default)))
        return configured

    async def _resolve_page_file_edits_async(
        self,
        *,
        selected_pages: list[tuple[str, dict[str, Any]]],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, Any],
        role_contract: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(min(self._page_edit_parallelism(scope_mode=scope_mode, generation_mode=generation_mode), len(selected_pages)))

        async def run_one(role: str, page: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await asyncio.to_thread(
                    self._resolve_page_file_edit,
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

        tasks = [run_one(role, page) for role, page in selected_pages]
        return list(await asyncio.gather(*tasks))

    @staticmethod
    def _scope_mode(intent: str, prompt: str, role_scope: list[str]) -> str:
        lowered = prompt.lower()
        if GenerationService._looks_like_fix_request(lowered):
            return "minimal_patch"
        if intent in {"edit", "refine", "role_only_change"}:
            return "minimal_patch"
        if any(marker in lowered for marker in ("only ", "just ", "точечно", "только ", "without touching", "do not touch anything else")):
            return "minimal_patch"
        if len(role_scope) == 1 and any(marker in lowered for marker in ("change", "update", "fix", "refine", "polish")):
            return "minimal_patch"
        return "app_surface_build"

    @staticmethod
    def _requires_multi_page(prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], intent: str) -> bool:
        if intent == "create":
            return True
        lowered = prompt.lower()
        if GenerationService._looks_like_fix_request(lowered):
            return False
        if intent in {"edit", "refine", "role_only_change"}:
            return any(
                marker in lowered
                for marker in (
                    "new page",
                    "new pages",
                    "add page",
                    "add pages",
                    "multi-page",
                    "catalog",
                    "checkout",
                    "dashboard",
                    "workflow",
                    "workspace",
                )
            )
        if len(role_scope) > 1:
            return True
        if len(grounded_spec.user_flows) > 1 or len(grounded_spec.domain_entities) > 1:
            return True
        multi_markers = (
            "page",
            "pages",
            "browse",
            "detail",
            "cart",
            "checkout",
            "track",
            "queue",
            "dashboard",
            "management",
            "workspace",
            "catalog",
        )
        return any(marker in lowered for marker in multi_markers)

    @staticmethod
    def _looks_like_fix_request(prompt: str) -> bool:
        fix_markers = (
            "fix",
            "bug",
            "error",
            "failed",
            "failure",
            "exception",
            "traceback",
            "stacktrace",
            "stack trace",
            "build failed",
            "preview failed",
            "docker",
            "npm run build",
            "exit code",
            "исправ",
            "ошиб",
            "не работает",
            "слом",
            "падает",
            "сбой",
        )
        return any(marker in prompt for marker in fix_markers)

    @staticmethod
    def _effective_prompt(request: GenerateRequest) -> str:
        if request.mode != "fix" or request.error_context is None:
            return request.prompt
        segments = [
            request.prompt.strip(),
            "Repair only the reported error. Keep the diff minimal and preserve existing behavior.",
            f"Error source: {request.error_context.source or 'unknown'}",
            f"Failing target: {request.error_context.failing_target or 'unknown'}",
            request.error_context.raw_error.strip(),
        ]
        return "\n\n".join(segment for segment in segments if segment)

    @staticmethod
    def _failure_class_from_error_context(error_context: Any) -> str | None:
        if not error_context:
            return None
        source = str(getattr(error_context, "source", "") or "").lower()
        raw_error = str(getattr(error_context, "raw_error", "") or "").lower()
        text = f"{source}\n{raw_error}"
        if any(marker in text for marker in ("preview failed", "docker preview", "permission denied", "docker daemon")):
            return "preview_startup"
        if any(marker in text for marker in ("traceback", "importerror", "modulenotfounderror", "literal is not defined")):
            return "backend_startup"
        if any(marker in text for marker in ("npm run build", "ts230", "vite", "typescript", "jsx", "next/link")):
            return "frontend_build"
        if any(marker in text for marker in ("401", "403", "authorization", "permissions denied")):
            return "runtime_permission"
        if any(marker in text for marker in ("payload", "schema", "validationerror", "does not return", "unexpected key")):
            return "api_contract_mismatch"
        if source:
            return source
        return "runtime_failure"

    @staticmethod
    def _root_cause_summary(error_context: Any) -> str | None:
        if not error_context:
            return None
        source = str(getattr(error_context, "source", "") or "runtime")
        failing_target = str(getattr(error_context, "failing_target", "") or "current build")
        raw_error = str(getattr(error_context, "raw_error", "") or "").strip()
        first_line = raw_error.splitlines()[0].strip() if raw_error else ""
        if not first_line:
            return f"Fix run requested for {source} issue in {failing_target}."
        return f"{source} issue in {failing_target}: {first_line[:220]}"

    @staticmethod
    def _summarize_failed_checks(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None) -> str | None:
        primary_issue = build_issues[0] if build_issues else preview_issue
        if primary_issue is None:
            return None
        location = primary_issue.location or "workspace"
        return f"{primary_issue.message} ({location})"

    @staticmethod
    def _build_fix_handoff(
        *,
        prompt: str,
        failure_reason: str | None,
        failure_class: str | None,
        issues: list[ValidationIssue],
        mode: str,
    ) -> dict[str, Any] | None:
        if mode == "fix":
            return None
        error_lines = [failure_reason or "Run failed."]
        for issue in issues[:6]:
            error_lines.append(f"[{issue.code}] {issue.message}")
        return {
            "mode": "fix",
            "prompt": "Analyze this failure and apply the smallest safe fix.",
            "error_context": {
                "source": "runtime",
                "failing_target": issues[0].location if issues else None,
                "raw_error": "\n".join(line for line in error_lines if line),
            },
            "failure_class": failure_class,
        }

    @staticmethod
    def _role_contract_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_title": {"type": "string"},
                "app_summary": {"type": "string"},
                "shared_entities": {"type": "array", "items": {"type": "string"}},
                "shared_logic": {"type": "array", "items": {"type": "string"}},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": list(ROLE_ORDER)},
                            "responsibility": {"type": "string"},
                            "entry_goal": {"type": "string"},
                            "primary_jobs": {"type": "array", "items": {"type": "string"}},
                            "key_entities": {"type": "array", "items": {"type": "string"}},
                            "ui_style_notes": {"type": "array", "items": {"type": "string"}},
                            "success_states": {"type": "array", "items": {"type": "string"}},
                            "must_differ_from": {"type": "array", "items": {"type": "string", "enum": list(ROLE_ORDER)}},
                        },
                        "required": [
                            "role",
                            "responsibility",
                            "entry_goal",
                            "primary_jobs",
                            "key_entities",
                            "ui_style_notes",
                            "success_states",
                            "must_differ_from",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["app_title", "app_summary", "shared_entities", "shared_logic", "roles"],
            "additionalProperties": False,
        }

    @staticmethod
    def _code_plan_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "flow_mode": {"type": "string", "enum": ["single_page", "multi_page"]},
                "files_to_read": {"type": "array", "items": {"type": "string"}},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "shared_files": {"type": "array", "items": {"type": "string"}},
                "backend_targets": {"type": "array", "items": {"type": "string"}},
                "page_graph": {
                    "type": "object",
                    "properties": {
                        "app_title": {"type": "string"},
                        "summary": {"type": "string"},
                        "flow_mode": {"type": "string", "enum": ["single_page", "multi_page"]},
                        "shared_files": {"type": "array", "items": {"type": "string"}},
                        "backend_targets": {"type": "array", "items": {"type": "string"}},
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "enum": list(ROLE_ORDER)},
                                    "entry_path": {"type": "string"},
                                    "landing_page_id": {"type": "string"},
                                    "routes_file": {"type": "string"},
                                    "pages": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "page_id": {"type": "string"},
                                                "route_path": {"type": "string"},
                                                "navigation_label": {"type": "string"},
                                                "component_name": {"type": "string"},
                                                "file_path": {"type": "string"},
                                                "title": {"type": "string"},
                                                "description": {"type": "string"},
                                                "purpose": {"type": "string"},
                                                "page_kind": {"type": "string"},
                                                "primary_actions": {"type": "array", "items": {"type": "string"}},
                                                "data_dependencies": {"type": "array", "items": {"type": "string"}},
                                                "loading_state": {"type": "string"},
                                                "empty_state": {"type": "string"},
                                                "error_state": {"type": "string"},
                                            },
                                            "required": [
                                                "page_id",
                                                "route_path",
                                                "navigation_label",
                                                "component_name",
                                                "file_path",
                                                "title",
                                                "description",
                                                "purpose",
                                                "page_kind",
                                                "primary_actions",
                                                "data_dependencies",
                                                "loading_state",
                                                "empty_state",
                                                "error_state",
                                            ],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": ["role", "entry_path", "landing_page_id", "routes_file", "pages"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["app_title", "summary", "flow_mode", "roles"],
                    "additionalProperties": False,
                },
            },
            "required": ["summary", "flow_mode", "files_to_read", "target_files", "shared_files", "backend_targets", "page_graph"],
            "additionalProperties": False,
        }

    @staticmethod
    def _code_edit_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "assistant_message": {"type": "string"},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["create", "replace", "delete"]},
                            "content": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["file_path", "operation", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["assistant_message", "operations"],
            "additionalProperties": False,
        }

    @staticmethod
    def _role_contract_system_prompt() -> str:
        return (
            "You are the role analyst for a real mini-app coding workspace. "
            "Before planning files, separate what client, specialist, and manager each truly own. "
            "Do not collapse roles into relabeled versions of the same surface."
        )

    def _role_contract_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        intent: str,
        creative_direction: dict[str, Any],
    ) -> str:
        return json_dumps(
            {
                "task": "Analyze role boundaries before page planning",
                "prompt": prompt,
                "intent": intent,
                "role_scope": role_scope,
                "grounded_spec": grounded_spec.model_dump(mode="json"),
                "doc_refs": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in doc_refs],
                "creative_direction": creative_direction,
                "rules": [
                    "Explain what the client does, what the specialist does, and what the manager does before thinking about files.",
                    "Separate responsibilities, actions, and success states across roles.",
                    "If the prompt is a targeted edit, keep the role analysis narrow and avoid redefining unrelated areas.",
                ],
            }
        )

    @staticmethod
    def _code_plan_system_prompt() -> str:
        return (
            "Plan a real file-level multi-page mini-app. "
            "Use the role contract first, then infer the page graph, route tree, shared app files, and backend touch points. "
            "Do not output placeholders, metrics-only dashboards, or one-screen role wrappers."
        )

    @staticmethod
    def _code_plan_section_system_prompt(section_title: str) -> str:
        return (
            "Plan one section of a real file-level multi-page mini-app. "
            f"Return only the requested section: {section_title}. "
            "Keep it concrete, schema-valid, and consistent with the role contract."
        )

    def _code_plan_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        return json_dumps(
            {
                "task": "Plan a route/page graph for real code generation",
                "prompt": prompt,
                "role_scope": role_scope,
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec) if compact else grounded_spec.model_dump(mode="json"),
                "role_contract": role_contract,
                "doc_refs": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in (doc_refs[:4] if compact else doc_refs)
                ],
                "workspace_tree": workspace_tree[:36] if compact else workspace_tree,
                "workspace_path_hints": self._workspace_path_hints(workspace_tree),
                "design_reference_files": list(DESIGN_REFERENCE_FILES),
                "creative_direction": creative_direction,
                "constraints": [
                    "Keep Telegram/MAX mini-app compatibility.",
                    "Keep three-role preview compatibility.",
                    "Use the existing profile design language as a style anchor instead of inventing a generic dashboard shell.",
                    "If the prompt implies several flows, entities, or jobs, return a multi-page app with distinct page files and routes.",
                    "For targeted edits, keep target_files minimal and touch only the files required by the request.",
                    "Do not output role copies with changed titles only.",
                    "Return only repo-relative file paths that fit the current workspace tree and path hints.",
                    "Do not return HTTP endpoints, route strings, or prose labels inside target_files or backend_targets.",
                    "Do not invent alternate backend roots such as backend/src when the current workspace uses another backend layout.",
                ],
            }
        )

    def _code_plan_section_user_prompt(
        self,
        *,
        section_id: str,
        section_title: str,
        section_contract: list[str],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        return json_dumps(
            {
                "task": "Plan one route/page graph section for real code generation",
                "section_id": section_id,
                "section_title": section_title,
                "prompt": prompt,
                "role_scope": role_scope,
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec) if compact else grounded_spec.model_dump(mode="json"),
                "role_contract": role_contract,
                "doc_refs": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in (doc_refs[:3] if compact else doc_refs[:5])
                ],
                "workspace_tree": workspace_tree[:28] if compact else workspace_tree[:40],
                "workspace_path_hints": self._workspace_path_hints(workspace_tree),
                "design_reference_files": list(DESIGN_REFERENCE_FILES),
                "creative_direction": creative_direction,
                "constraints": [
                    "Keep Telegram/MAX mini-app compatibility.",
                    "Keep three-role preview compatibility.",
                    "Use the existing profile design language as a style anchor.",
                    "Do not output role copies with changed titles only.",
                    "Return only repo-relative file paths that fit the current workspace tree and path hints.",
                    "Do not return HTTP endpoints, route strings, or prose labels inside target_files or backend_targets.",
                ],
                "section_contract": section_contract,
            }
        )

    @staticmethod
    def _workspace_path_hints(workspace_tree: list[dict[str, str]]) -> dict[str, Any]:
        file_paths = [
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        ]
        backend_files = [path for path in file_paths if path.startswith("backend/")]
        frontend_files = [path for path in file_paths if path.startswith("frontend/")]
        top_level_dirs = sorted(
            {
                path.split("/", 1)[0]
                for path in file_paths
                if "/" in path
            }
        )
        return {
            "top_level_dirs": top_level_dirs[:12],
            "backend_root_candidates": sorted({"/".join(path.split("/")[:2]) for path in backend_files if "/" in path})[:8],
            "frontend_root_candidates": sorted({"/".join(path.split("/")[:3]) for path in frontend_files if path.count("/") >= 2})[:12],
            "backend_examples": backend_files[:12],
            "frontend_examples": frontend_files[:16],
        }

    def _code_plan_partial_schema(self, field_names: list[str]) -> dict[str, Any]:
        full_schema = self._code_plan_schema()
        properties = full_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {name: properties[name] for name in field_names if name in properties},
            "required": [name for name in field_names if name in properties],
            "additionalProperties": False,
        }

    @staticmethod
    def _page_edit_system_prompt() -> str:
        return (
            "Generate one real React page file for a Telegram/MAX mini-app workspace. "
            "The page must feel custom, role-specific, and grounded in the existing profile design language. "
            "Return a single create/replace operation for the requested page file only."
        )

    def _page_edit_user_prompt(
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
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        sibling_pages = [
            item
            for item in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if item.get("file_path") != page.get("file_path")
        ]
        design_reference_files = self._bounded_file_contexts(
            {
                path: file_contexts.get(path, "")
                for path in DESIGN_REFERENCE_FILES
                if path in file_contexts
            },
            max_file_chars=2400 if compact else 4000,
            max_total_chars=7200 if compact else 12000,
        )
        return json_dumps(
            {
                "task": "Generate one page file",
                "prompt": prompt,
                "intent": intent,
                "scope_mode": scope_mode,
                "role": role,
                "page": page,
                "sibling_pages": sibling_pages[:2] if compact else sibling_pages[:4],
                "role_contract": role_contract.get("roles", {}).get(role),
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec),
                "shared_contract": {
                    "preferred_imports": [
                        "@/shared/api/httpClient",
                        "@/entities/profile/ui/ProfileCabinetCard/ProfileCabinetCard",
                        "@/features/profile/model/profileStore",
                    ],
                    "design_reference_files": design_reference_files,
                },
                "current_file": self._limit_text(file_contexts.get(page["file_path"], ""), 6000 if compact else 12000),
                "creative_direction": creative_direction,
                "rules": [
                    "Create a real page, not a generic stats card screen.",
                    "Respect the requested role and make the actions specific to that role.",
                    "Add loading, empty, and error states when the page needs data.",
                    "If scope_mode is minimal_patch, preserve unrelated behavior and keep the diff minimal.",
                    "Return exactly one operation for the requested page file path.",
                ],
            }
        )

    @staticmethod
    def _composition_system_prompt(stage_name: str) -> str:
        if stage_name == "backend":
            return (
                "Compose the backend/runtime pieces after planning. "
                "Generate only backend, API, schema, store, or contract files needed by the requested change. "
                "Do not rewrite frontend page files. "
                "Return executable file operations, not an implementation plan."
            )
        return (
            "Compose the shared frontend/runtime pieces after the individual pages and backend are written. "
            "Generate route files, shared UI helpers, state modules, and frontend glue that connect the generated pages to the backend. "
            "Do not rewrite page files unless they are explicitly targeted. "
            "Return executable file operations, not an implementation plan."
        )

    def _composition_user_prompt(
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
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        trimmed_targets = self._bounded_file_contexts(
            {path: file_contexts.get(path, "") for path in target_files},
            max_file_chars=4000 if compact else 9000,
            max_total_chars=12000 if compact else 26000,
        )
        return json_dumps(
            {
                "task": f"Compose {stage_name} files for the planned app change",
                "prompt": prompt,
                "intent": intent,
                "stage_name": stage_name,
                "scope_mode": scope_mode,
                "role_scope": role_scope,
                "role_contract": role_contract,
                "page_graph": page_graph,
                "target_files": target_files,
                "workspace_path_hints": {
                    "target_roots": sorted({path.split("/", 1)[0] for path in target_files if "/" in path}),
                    "target_examples": target_files[:20],
                    "existing_file_context_paths": sorted(file_contexts.keys())[:20],
                },
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec),
                "file_contexts": trimmed_targets,
                "generated_page_sources": self._bounded_file_contexts(
                    generated_page_sources,
                    max_file_chars=2800 if compact else 7000,
                    max_total_chars=7200 if compact else 18000,
                ),
                "generated_support_sources": self._bounded_file_contexts(
                    generated_support_sources,
                    max_file_chars=2200 if compact else 5000,
                    max_total_chars=5200 if compact else 12000,
                ),
                "creative_direction": creative_direction,
                "rules": [
                    "Only touch files listed in target_files.",
                    "If stage_name is backend, generate only backend/server/shared contract files required by the request.",
                    "If stage_name is frontend, wire pages, routes, and shared UI/state to the already planned backend surface.",
                    "Generate role routes that expose the page graph as real separate pages when routes are targeted.",
                    "Generate shared app chrome/state files that support the pages instead of rendering placeholder dashboards.",
                    "For minimal_patch, preserve unrelated behavior and keep the diff minimal.",
                    "Do not touch page files unless they are included in target_files.",
                    "If target_files is non-empty, operations must include at least one create/replace/delete for one of those files.",
                    "Do not return a prose plan, checklist, or explanation instead of file operations.",
                    "Do not leave operations empty when target_files is non-empty.",
                    "assistant_message must briefly summarize the patch that was generated, not propose future work.",
                    "Do not invent files under alternate architecture roots that are absent from target_files.",
                    "If the workspace uses backend/app, do not switch to backend/src. If the workspace uses frontend/src/roles, do not switch to unrelated frontend page trees unless those exact files are in target_files.",
                ],
            }
        )

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
                        "- Return only the single requested page file operation.\n"
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
                operations = [DraftFileOperation.model_validate(item) for item in raw_operations]
                valid_operations = [
                    operation
                    for operation in operations
                    if operation.file_path == page["file_path"] and operation.operation in {"create", "replace"} and operation.content is not None
                ]
                if len(valid_operations) != 1:
                    raise ValueError(f"{page['file_path']} must be generated as a single create/replace operation.")
                return {
                    "assistant_message": str(normalized.get("assistant_message") or "").strip(),
                    "operation": valid_operations[0],
                    "model": payload["model"],
                }
            except Exception as exc:
                last_error = exc
                if mode_attempt + 1 < len(retry_modes) and self._is_retryable_llm_error(exc):
                    logger.warning(
                        "Retrying page generation for %s with compact recovery context after transient provider failure: %s",
                        page["file_path"],
                        exc,
                    )
                    continue
                break
        assert last_error is not None
        return {
            "error": f"Page generation failed for {page['file_path']}: {last_error}",
            "retryable": self._is_retryable_llm_error(last_error),
            "file_path": page["file_path"],
        }

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
    ) -> dict[str, Any]:
        if not target_files:
            return {"assistant_message": f"{stage_name.capitalize()} stage complete: no changes were required.", "operations": []}
        allowed_targets = set(target_files)
        retry_modes = [generation_mode]
        if generation_mode != GenerationMode.FAST:
            retry_modes.append(GenerationMode.FAST)
        last_error: Exception | None = None
        scope_recovery_used = False
        for mode_attempt, prompt_mode in enumerate(retry_modes):
            try:
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
                    file_contexts=file_contexts,
                    generated_page_sources=generated_page_sources,
                    generated_support_sources=generated_support_sources,
                    generation_mode=prompt_mode,
                    creative_direction=creative_direction,
                )
                if scope_recovery_used:
                    scope_recovery_note = (
                        "Scope recovery mode:\n"
                        "- The previous attempt used file paths outside the real workspace.\n"
                        "- You must only emit operations for the exact repo-relative files listed in target_files.\n"
                        "- Do not invent backend/src, src/server.ts, or any new architecture root unless that exact path is present in target_files.\n"
                        "- If a backend surface is requested but the target_files are frontend-only, leave backend untouched.\n"
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
                raw_operations = normalized.get("operations")
                if not isinstance(raw_operations, list):
                    raise ValueError("Composition did not return operations.")
                operations = [DraftFileOperation.model_validate(item) for item in raw_operations]
                invalid = [
                    operation.file_path
                    for operation in operations
                    if operation.file_path not in allowed_targets or (operation.operation in {"create", "replace"} and operation.content is None)
                ]
                if invalid:
                    raise ValueError(f"Composition touched files outside the planned scope: {', '.join(invalid[:5])}")
                self._validate_targeted_operations(
                    stage_name=stage_name,
                    target_files=target_files,
                    operations=operations,
                )
                return {
                    "assistant_message": str(normalized.get("assistant_message") or "").strip(),
                    "operations": operations,
                    "model": payload["model"],
                }
            except Exception as exc:
                last_error = exc
                error_text = str(exc)
                if (
                    not scope_recovery_used
                    and "outside the planned scope" in error_text.lower()
                ):
                    scope_recovery_used = True
                    logger.warning(
                        "Retrying %s composition after scope mismatch: %s",
                        stage_name,
                        exc,
                    )
                    continue
                if mode_attempt + 1 < len(retry_modes) and self._is_retryable_llm_error(exc):
                    logger.warning(
                        "Retrying %s composition with compact recovery context after transient provider failure: %s",
                        stage_name,
                        exc,
                    )
                    continue
                break
        assert last_error is not None
        return {"error": f"Composition step failed: {last_error}"}

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
    def _backend_target_files(page_graph: dict[str, Any], target_files: set[str]) -> list[str]:
        ordered = [path for path in (page_graph.get("backend_targets") or []) if isinstance(path, str)]
        if not target_files:
            return list(dict.fromkeys(ordered))
        return [path for path in dict.fromkeys(ordered) if path in target_files]

    @staticmethod
    def _frontend_target_files(page_graph: dict[str, Any], target_files: set[str]) -> list[str]:
        structural_targets = list(page_graph.get("shared_files") or [])
        structural_targets.extend(
            role_payload.get("routes_file")
            for role_payload in (page_graph.get("roles") or {}).values()
            if isinstance(role_payload, dict)
        )
        ordered = [path for path in structural_targets if isinstance(path, str)]
        if not target_files:
            return list(dict.fromkeys(ordered))
        return [path for path in dict.fromkeys(ordered) if path in target_files]

    @staticmethod
    def _dedupe_operations(operations: list[DraftFileOperation]) -> list[DraftFileOperation]:
        deduped: dict[str, DraftFileOperation] = {}
        for operation in operations:
            deduped[operation.file_path] = operation
        return list(deduped.values())

    @staticmethod
    def _role_contract_gate_issues(role_contract: dict[str, Any], role_scope: list[str], *, scope_mode: str) -> list[str]:
        issues: list[str] = []
        roles = role_contract.get("roles") or {}
        normalized_responsibilities: list[str] = []
        for role in role_scope:
            payload = roles.get(role)
            if not isinstance(payload, dict):
                issues.append(f"{role} is missing from the role contract.")
                continue
            responsibility = str(payload.get("responsibility") or "").strip()
            jobs = payload.get("primary_jobs") or []
            if not responsibility:
                issues.append(f"{role} is missing a concrete responsibility.")
            if not jobs:
                issues.append(f"{role} is missing primary jobs.")
            normalized_responsibilities.append(re.sub(r"\s+", " ", responsibility.lower()))
        if scope_mode == "minimal_patch":
            return issues
        if len(normalized_responsibilities) > 1 and len(set(normalized_responsibilities)) == 1:
            issues.append("All selected roles still have the same responsibility in the role contract.")
        return issues

    @staticmethod
    def _page_graph_gate_issues(page_graph: dict[str, Any], role_scope: list[str], *, scope_mode: str, require_multi_page: bool) -> list[str]:
        issues: list[str] = []
        roles = page_graph.get("roles") or {}
        total_pages = 0
        normalized_role_routes: list[tuple[str, tuple[str, ...]]] = []
        for role in role_scope:
            role_payload = roles.get(role) or {}
            pages = role_payload.get("pages") or []
            routes_file = role_payload.get("routes_file")
            if not isinstance(routes_file, str) or not routes_file:
                issues.append(f"{role} is missing a routes file.")
            if not isinstance(pages, list) or not pages:
                issues.append(f"{role} is missing page definitions.")
                continue
            total_pages += len(pages)
            if scope_mode != "minimal_patch" and require_multi_page and len(pages) < 2:
                issues.append(f"{role} did not receive enough distinct pages for a multi-page app.")
            normalized_role_routes.append(
                (
                    role,
                    tuple(sorted(str(page.get("route_path") or "") for page in pages)),
                )
            )
        if scope_mode != "minimal_patch" and require_multi_page and page_graph.get("flow_mode") != "multi_page":
            issues.append("The generated plan did not stay in multi-page mode.")
        if scope_mode != "minimal_patch" and require_multi_page and total_pages <= len(role_scope):
            issues.append("The generated plan still collapses the app into one screen per selected role.")
        if scope_mode != "minimal_patch" and len(normalized_role_routes) > 1:
            route_sets = [routes for _, routes in normalized_role_routes]
            if len(set(route_sets)) == 1:
                issues.append("Selected roles still share the same route tree.")
        return issues

    @staticmethod
    def _edit_gate_issues(
        page_graph: dict[str, Any],
        operations: list[DraftFileOperation],
        role_scope: list[str],
        *,
        scope_mode: str,
        target_files: list[str],
    ) -> list[str]:
        issues: list[str] = []
        operation_paths = {operation.file_path for operation in operations}
        allowed_target_paths = set(target_files) | {"artifacts/generated_app_graph.json"}
        unexpected_paths = [path for path in operation_paths if path not in allowed_target_paths]
        if unexpected_paths:
            issues.append(f"Generated draft touched files outside the planned target scope: {', '.join(unexpected_paths[:5])}")
        if scope_mode == "minimal_patch":
            meaningful_hits = [path for path in operation_paths if path in set(target_files)]
            if target_files and not meaningful_hits:
                issues.append("Minimal patch draft returned only artifact-level changes and did not touch any planned source targets.")
            if len(operation_paths) > max(1, len(target_files) + 1):
                issues.append("Minimal patch mode touched too many files.")
            return issues

        route_hits = 0
        page_hits = 0
        for role in role_scope:
            role_payload = page_graph.get("roles", {}).get(role) or {}
            routes_file = role_payload.get("routes_file")
            if isinstance(routes_file, str) and routes_file in operation_paths:
                route_hits += 1
            for page in role_payload.get("pages") or []:
                file_path = page.get("file_path")
                if isinstance(file_path, str) and file_path in operation_paths:
                    page_hits += 1
        if route_hits < len(role_scope):
            issues.append("Generated draft does not update the role route files for every selected role.")
        if page_hits <= len(role_scope):
            issues.append("Generated draft still collapses the app into too few real page files.")
        return issues

    @staticmethod
    def _build_check_results(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None = None) -> list[RunCheckResult]:
        results: list[RunCheckResult] = []
        if build_issues:
            results.append(
                RunCheckResult(
                    name="draft-build",
                    status="failed",
                    details="; ".join(issue.message for issue in build_issues[:5]),
                )
            )
        else:
            results.append(RunCheckResult(name="draft-build", status="passed", details="Scaffold entrypoints are present in the draft."))
        if preview_issue is not None:
            results.append(RunCheckResult(name="draft-preview", status="failed", details=preview_issue.message))
        elif build_issues:
            results.append(
                RunCheckResult(
                    name="draft-preview",
                    status="skipped",
                    details="Preview rebuild was skipped because build validation failed.",
                )
            )
        elif not build_issues:
            results.append(RunCheckResult(name="draft-preview", status="passed", details="Preview runtime rebuilt successfully."))
        return results

    @staticmethod
    def _filter_non_blocking_build_issues(build_issues: list[ValidationIssue], *, scope_mode: str) -> list[ValidationIssue]:
        if scope_mode != "minimal_patch":
            return build_issues
        ignored_codes = {
            "build.invalid_generated_app_graph",
            "build.missing_role_routes",
            "build.placeholder_role_surface",
            "build.insufficient_routes",
            "build.insufficient_pages",
        }
        return [issue for issue in build_issues if issue.code not in ignored_codes]

    @staticmethod
    def _repair_attempt_limit(generation_mode: GenerationMode, intent: str) -> int:
        if intent in {"edit", "refine", "role_only_change"}:
            return max(1, int(os.getenv("EDIT_AUTO_REPAIR_ATTEMPTS", "4")))
        if generation_mode == GenerationMode.FAST:
            return max(1, int(os.getenv("FAST_AUTO_REPAIR_ATTEMPTS", "3")))
        if generation_mode == GenerationMode.QUALITY:
            return max(1, int(os.getenv("QUALITY_AUTO_REPAIR_ATTEMPTS", "6")))
        if generation_mode == GenerationMode.BALANCED:
            return max(1, int(os.getenv("BALANCED_AUTO_REPAIR_ATTEMPTS", "5")))
        return max(0, int(os.getenv("BASIC_AUTO_REPAIR_ATTEMPTS", "1")))

    def _collect_existing_file_contexts(self, workspace_id: str, run_id: str, target_files: list[str]) -> dict[str, str]:
        file_contexts: dict[str, str] = {}
        for file_path in target_files:
            try:
                file_contexts[file_path] = self.workspace_service.read_file(workspace_id, file_path, run_id=run_id)
            except FileNotFoundError:
                continue
        return file_contexts

    @staticmethod
    def _preview_failure_issue(preview: Any) -> ValidationIssue:
        message = next((str(line).strip() for line in reversed(preview.logs or []) if str(line).strip()), "Preview runtime failed to rebuild.")
        return ValidationIssue(
            code="preview.rebuild_failed",
            message=message,
            severity="high",
            location="preview",
            blocking=True,
        )

    @staticmethod
    def _is_non_blocking_preview_issue(issue: ValidationIssue) -> bool:
        message = issue.message.lower()
        infra_markers = (
            "docker daemon socket",
            "operation not permitted",
            "permission denied",
            "connect to the docker daemon",
            "dial unix",
        )
        return any(marker in message for marker in infra_markers)

    def _repair_draft_after_failure(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        target_files: list[str],
        file_contexts: dict[str, str],
        build_issues: list[ValidationIssue],
        preview_issue: ValidationIssue | None,
        preview_logs: list[str],
        attempt: int,
    ) -> dict[str, Any]:
        allowed_targets = set(target_files)
        try:
            payload = self._generate_structured_with_retry(
                role="repair",
                schema_name="composition_bundle_v1",
                schema=self._code_edit_schema(),
                system_prompt=self._repair_system_prompt(),
                user_prompt=self._repair_user_prompt(
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    role_scope=role_scope,
                    role_contract=role_contract,
                    page_graph=page_graph,
                    scope_mode=scope_mode,
                    target_files=target_files,
                    file_contexts=file_contexts,
                    build_issues=build_issues,
                    preview_issue=preview_issue,
                    preview_logs=preview_logs,
                    attempt=attempt,
                ),
            )
            normalized = self._normalize_model_payload(payload["payload"])
            raw_operations = normalized.get("operations")
            if not isinstance(raw_operations, list):
                raise ValueError("Repair step did not return operations.")
            operations = [DraftFileOperation.model_validate(item) for item in raw_operations]
            invalid = [
                operation.file_path
                for operation in operations
                if operation.file_path not in allowed_targets or (operation.operation in {"create", "replace"} and operation.content is None)
            ]
            if invalid:
                raise ValueError(f"Repair touched files outside the planned scope: {', '.join(invalid[:5])}")
            self._validate_targeted_operations(
                stage_name="repair",
                target_files=target_files,
                operations=operations,
            )
            return {
                "assistant_message": str(normalized.get("assistant_message") or "").strip(),
                "operations": operations,
                "model": payload["model"],
            }
        except Exception as exc:
            return {"error": f"Automatic repair step failed: {exc}"}

    @staticmethod
    def _validate_targeted_operations(
        *,
        stage_name: str,
        target_files: list[str],
        operations: list[DraftFileOperation],
    ) -> None:
        if not target_files:
            return
        targeted_hits = [
            operation
            for operation in operations
            if operation.file_path in set(target_files)
        ]
        if not targeted_hits:
            raise RuntimeError(
                f"{stage_name.capitalize()} returned no file operations for the requested target_files."
            )

    @staticmethod
    def _repair_system_prompt() -> str:
        return (
            "You repair an existing draft workspace after build or preview failure. "
            "Return only the smallest safe set of file operations needed to make the draft compile and boot. "
            "Do not expand scope, do not redesign the app, and do not touch files outside the provided target list. "
            "Return executable file operations, not a repair plan."
        )

    def _repair_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        target_files: list[str],
        file_contexts: dict[str, str],
        build_issues: list[ValidationIssue],
        preview_issue: ValidationIssue | None,
        preview_logs: list[str],
        attempt: int,
    ) -> str:
        return json_dumps(
            {
                "task": "Repair build or preview failures in the generated draft",
                "attempt": attempt,
                "prompt": prompt,
                "role_scope": role_scope,
                "scope_mode": scope_mode,
                "target_files": target_files,
                "grounded_spec": grounded_spec.model_dump(mode="json"),
                "role_contract": role_contract,
                "page_graph": page_graph,
                "file_contexts": file_contexts,
                "build_issues": [issue.model_dump(mode="json") for issue in build_issues],
                "preview_issue": preview_issue.model_dump(mode="json") if preview_issue is not None else None,
                "preview_logs": preview_logs[-10:],
                "rules": [
                    "Fix only the concrete build/preview failures.",
                    "Prefer editing the existing generated files instead of creating new architecture.",
                    "Keep the diff minimal and preserve unrelated behavior.",
                    "Return operations only for files listed in target_files.",
                    "If target_files is non-empty, operations must include at least one create/replace/delete for one of those files.",
                    "Do not return a prose repair plan instead of file operations.",
                    "Do not leave operations empty when target_files is non-empty.",
                ],
            }
        )

    @staticmethod
    def _diff_summary(diff_text: str) -> str:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE):
            candidate = match.group(1).strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            paths.append(candidate)
        if not paths:
            return "No draft diff was produced."
        unique_paths = list(dict.fromkeys(paths))
        return f"Changed files: {', '.join(unique_paths[:6])}"

    def _build_agent_traceability_report(
        self,
        workspace_id: str,
        grounded_spec: GroundedSpecModel,
        operations: list[DraftFileOperation],
    ) -> TraceabilityReportModel:
        entries = [
            TraceabilityReportEntry(
                trace_id=new_id("trace"),
                source_ref="prompt-source",
                source_kind="user_prompt",
                target_id=operation.file_path,
                target_type="file",
                mapping_note=f"Prompt-grounded edit for {operation.file_path}.",
            )
            for operation in operations
        ]
        for doc_ref in grounded_spec.doc_refs[:3]:
            entries.append(
                TraceabilityReportEntry(
                    trace_id=new_id("trace"),
                    source_ref=doc_ref.doc_ref_id,
                    source_kind=doc_ref.source_type,
                    target_id="grounded_spec",
                    target_type="planning_artifact",
                    mapping_note="Source material kept for planning/debug traceability.",
                )
            )
        return TraceabilityReportModel(report_id=new_id("trace"), workspace_id=workspace_id, entries=entries)

    @staticmethod
    def _build_agent_summary(
        *,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        operations: list[DraftFileOperation],
        generation_mode: GenerationMode,
        assistant_message: str,
    ) -> str:
        return (
            f"{assistant_message} Built a {generation_mode.value} draft for {grounded_spec.target_platform} "
            f"with {len(operations)} file operations across {len(role_scope)} role views."
        )

    @staticmethod
    def _compile_code_summary(operations: list[DraftFileOperation], role_scope: list[str]) -> dict[str, int | str]:
        return {
            "file_count": len({operation.file_path for operation in operations}),
            "operation_count": len(operations),
            "role_count": len(role_scope),
            "iteration_count": 1,
        }

    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        head = max_chars // 2
        tail = max_chars - head
        return f"{text[:head]}\n/* ... truncated ... */\n{text[-tail:]}"

    def _bounded_file_contexts(
        self,
        file_contexts: dict[str, str],
        *,
        max_file_chars: int,
        max_total_chars: int,
    ) -> dict[str, str]:
        trimmed: dict[str, str] = {}
        total = 0
        for path, content in file_contexts.items():
            bounded = self._limit_text(content or "", max_file_chars)
            next_total = total + len(bounded)
            if trimmed and next_total > max_total_chars:
                break
            trimmed[path] = bounded
            total = next_total
        return trimmed

    @staticmethod
    def _compact_grounded_spec_for_codegen(grounded_spec: GroundedSpecModel) -> dict[str, Any]:
        return {
            "product_goal": grounded_spec.product_goal,
            "actors": [actor.model_dump(mode="json") for actor in grounded_spec.actors[:4]],
            "domain_entities": [entity.model_dump(mode="json") for entity in grounded_spec.domain_entities[:6]],
            "user_flows": [flow.model_dump(mode="json") for flow in grounded_spec.user_flows[:4]],
            "ui_requirements": [item.model_dump(mode="json") for item in grounded_spec.ui_requirements[:8]],
            "api_requirements": [item.model_dump(mode="json") for item in grounded_spec.api_requirements[:8]],
            "security_requirements": [item.model_dump(mode="json") for item in grounded_spec.security_requirements[:6]],
            "non_functional_requirements": [item.model_dump(mode="json") for item in grounded_spec.non_functional_requirements[:6]],
            "platform_constraints": [item.model_dump(mode="json") for item in grounded_spec.platform_constraints[:6]],
            "assumptions": [item.model_dump(mode="json") for item in grounded_spec.assumptions[:6]],
        }

    @staticmethod
    def _is_retryable_llm_error(error: Exception) -> bool:
        text = str(error).lower()
        retry_markers = (
            " returned 429",
            " returned 500",
            " returned 502",
            " returned 503",
            " returned 504",
            "internal_server_error",
            "rate limit",
            "timed out",
            "timeout",
            "connection reset",
            "temporarily unavailable",
            "returned non-json text",
            "returned empty text instead of json",
            "instead of json",
            "returned no file operations",
            "no file operations for the requested target_files",
        )
        return any(marker in text for marker in retry_markers)

    @staticmethod
    def _tighten_json_retry_kwargs(request_kwargs: dict[str, Any], error: Exception, attempt: int) -> dict[str, Any]:
        retry_note = (
            "JSON retry instruction:\n"
            f"- Previous attempt #{attempt + 1} returned invalid JSON.\n"
            f"- Error: {str(error)[:400]}\n"
            "- Return exactly one JSON object.\n"
            "- Do not return two objects.\n"
            "- Do not include analysis, commentary, markdown fences, or any text before or after the JSON.\n"
            "- If no file operations are needed, still return one valid JSON object with the required keys."
        )
        tightened = dict(request_kwargs)
        tightened["system_prompt"] = f"{str(request_kwargs.get('system_prompt') or '').rstrip()}\n\n{retry_note}".strip()
        tightened["user_prompt"] = f"{str(request_kwargs.get('user_prompt') or '').rstrip()}\n\n{retry_note}".strip()
        return tightened

    @staticmethod
    def _llm_cache_kwargs() -> dict[str, str]:
        context = ACTIVE_LLM_CACHE_CONTEXT.get() or {}
        prompt_cache_key = str(context.get("prompt_cache_key") or "").strip()
        stable_prefix = str(context.get("stable_prefix") or "").strip()
        payload: dict[str, str] = {}
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if stable_prefix:
            payload["stable_prefix"] = stable_prefix
        return payload

    @staticmethod
    def _record_llm_cache_stats(result: dict[str, Any]) -> None:
        sink = ACTIVE_LLM_CACHE_STATS.get()
        if sink is None:
            return
        sink["llm_requests"] = int(sink.get("llm_requests", 0)) + 1
        response_stats = result.get("cache_stats")
        if not isinstance(response_stats, dict):
            return
        sink["cached_tokens"] = int(sink.get("cached_tokens", 0)) + int(response_stats.get("cached_tokens", 0) or 0)
        sink["cache_write_tokens"] = int(sink.get("cache_write_tokens", 0)) + int(response_stats.get("cache_write_tokens", 0) or 0)

    def _generate_structured_with_retry(self, **kwargs: Any) -> dict[str, Any]:
        request_kwargs = {**self._llm_cache_kwargs(), **kwargs}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self.openrouter_client.generate_structured(**request_kwargs)
                self._record_llm_cache_stats(result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not self._is_retryable_llm_error(exc):
                    raise
                if self._should_tighten_json_retry(exc):
                    request_kwargs = self._tighten_json_retry_kwargs(request_kwargs, exc, attempt)
                logger.warning("Retrying structured generation after transient provider failure: %s", exc)
                time.sleep(0.8 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _generate_json_object_with_retry(self, **kwargs: Any) -> dict[str, Any]:
        request_kwargs = {**self._llm_cache_kwargs(), **kwargs}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self.openrouter_client.generate_json_object(**request_kwargs)
                self._record_llm_cache_stats(result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not self._is_retryable_llm_error(exc):
                    raise
                if self._should_tighten_json_retry(exc):
                    request_kwargs = self._tighten_json_retry_kwargs(request_kwargs, exc, attempt)
                logger.warning("Retrying relaxed JSON generation after transient provider failure: %s", exc)
                time.sleep(0.8 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _should_tighten_json_retry(error: Exception) -> bool:
        text = str(error).lower()
        json_markers = (
            "invalid json",
            "returned non-json text",
            "returned empty text instead of json",
            "instead of json",
            "jsondecodeerror",
        )
        return any(marker in text for marker in json_markers)

    def _resolve_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list[Any],
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.openrouter_client.enabled:
            return {"error": "GroundedSpec generation requires OpenAI configuration."}
        if generation_mode == GenerationMode.FAST:
            return self._resolve_grounded_spec_fast(
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
            )
        try:
            outline_payload, payload, outline = self._generate_grounded_spec_pair(
                workspace_id=workspace_id,
                prompt=prompt,
                doc_refs=doc_refs,
                target_platform=target_platform,
                preview_profile=preview_profile,
                template_revision_id=template_revision_id,
                prompt_turn_id=prompt_turn_id,
                creative_direction=creative_direction,
                relaxed=False,
            )
            spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
            model_path = [str(outline_payload["model"]), str(payload["model"])]
            return {"spec": spec, "model": payload["model"], "model_sequence": model_path}
        except Exception as strict_exc:
            if self._is_retryable_llm_error(strict_exc):
                try:
                    outline_payload, payload, outline = self._generate_grounded_spec_pair(
                        workspace_id=workspace_id,
                        prompt=prompt,
                        doc_refs=doc_refs,
                        target_platform=target_platform,
                        preview_profile=preview_profile,
                        template_revision_id=template_revision_id,
                        prompt_turn_id=prompt_turn_id,
                        creative_direction=creative_direction,
                        relaxed=False,
                        compact=True,
                    )
                    spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
                    model_path = [str(outline_payload["model"]), str(payload["model"])]
                    return {
                        "spec": spec,
                        "model": payload["model"],
                        "model_sequence": model_path,
                        "warning_kind": "provider_retry_recovery",
                        "warning_stage": "spec_provider_retry_recovered",
                        "warning_title": "GroundedSpec recovered after transient provider failure.",
                        "warning": f"GroundedSpec recovered after transient provider failure: {strict_exc}",
                    }
                except Exception as provider_recovery_exc:
                    strict_exc = RuntimeError(
                        "GroundedSpec strict mode failed after transient-provider recovery attempts: "
                        f"{strict_exc}; compact retry error: {provider_recovery_exc}"
                    )
            try:
                outline_payload, payload, outline = self._generate_grounded_spec_pair(
                    workspace_id=workspace_id,
                    prompt=prompt,
                    doc_refs=doc_refs,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                    creative_direction=creative_direction,
                    relaxed=True,
                    compact=True,
                )
                spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
                model_path = [str(outline_payload["model"]), str(payload["model"])]
                return {
                    "spec": spec,
                    "model": payload["model"],
                    "model_sequence": model_path,
                    "warning_kind": "relaxed_json_recovery",
                    "warning_stage": "spec_relaxed_mode_used",
                    "warning_title": "GroundedSpec used relaxed JSON recovery after strict-mode failure.",
                    "warning": f"GroundedSpec strict mode failed and relaxed JSON mode was used: {strict_exc}",
                }
            except Exception as relaxed_exc:
                return {
                    "error": (
                        "GroundedSpec generation failed: "
                        f"strict mode error: {strict_exc}; relaxed mode error: {relaxed_exc}"
                    )
                }

    def _resolve_grounded_spec_fast(
        self,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            payload = self._generate_structured_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_fast_v1",
                schema=GroundedSpecModel.model_json_schema(),
                system_prompt=self._grounded_spec_system_prompt(),
                user_prompt=self._grounded_spec_user_prompt(
                    prompt=prompt,
                    doc_refs=doc_refs,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                    creative_direction=creative_direction,
                    outline={},
                    compact=True,
                ),
            )
            spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
            return {"spec": spec, "model": payload["model"], "model_sequence": [str(payload["model"])]}
        except Exception as strict_exc:
            try:
                payload = self._generate_json_object_with_retry(
                    role="spec_analysis",
                    schema_name="grounded_spec_fast_v1",
                    schema=GroundedSpecModel.model_json_schema(),
                    system_prompt=self._grounded_spec_system_prompt(),
                    user_prompt=self._grounded_spec_user_prompt(
                        prompt=prompt,
                        doc_refs=doc_refs,
                        target_platform=target_platform,
                        preview_profile=preview_profile,
                        template_revision_id=template_revision_id,
                        prompt_turn_id=prompt_turn_id,
                        creative_direction=creative_direction,
                        outline={},
                        compact=True,
                    ),
                )
                spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
                return {
                    "spec": spec,
                    "model": payload["model"],
                    "model_sequence": [str(payload["model"])],
                    "warning_kind": "fast_relaxed_json_recovery",
                    "warning_stage": "spec_relaxed_mode_used",
                    "warning_title": "Fast GroundedSpec used compact relaxed JSON recovery.",
                    "warning": f"Fast GroundedSpec strict mode failed and compact relaxed JSON mode was used: {strict_exc}",
                }
            except Exception as relaxed_exc:
                return {
                    "error": (
                        "Fast GroundedSpec generation failed: "
                        f"strict mode error: {strict_exc}; relaxed mode error: {relaxed_exc}"
                    )
                }

    def _generate_grounded_spec_pair(
        self,
        *,
        workspace_id: str,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        relaxed: bool,
        compact: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        outline_schema = self._grounded_spec_outline_schema()
        outline_user_prompt = self._grounded_spec_outline_user_prompt(
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            compact=compact,
        )
        if relaxed:
            outline_payload = self._generate_json_object_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_outline_v1",
                schema=outline_schema,
                system_prompt=self._grounded_spec_outline_system_prompt(),
                user_prompt=outline_user_prompt,
            )
        else:
            outline_payload = self._generate_structured_with_retry(
                role="spec_analysis",
                schema_name="grounded_spec_outline_v1",
                schema=outline_schema,
                system_prompt=self._grounded_spec_outline_system_prompt(),
                user_prompt=outline_user_prompt,
            )
        outline = self._normalize_model_payload(outline_payload["payload"])
        core_fields = ["product_goal", "actors", "domain_entities", "user_flows"]
        requirements_fields = [
            "ui_requirements",
            "api_requirements",
            "persistence_requirements",
            "integration_requirements",
            "security_requirements",
            "platform_constraints",
            "non_functional_requirements",
        ]
        governance_fields = ["assumptions", "unknowns", "contradictions"]

        core_payload = self._generate_grounded_spec_section(
            section_id="core",
            section_title="Core domain and workflow",
            field_names=core_fields,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline=outline,
            relaxed=relaxed,
            compact=compact,
        )
        requirements_payload = self._generate_grounded_spec_section(
            section_id="requirements",
            section_title="Runtime requirements",
            field_names=requirements_fields,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline=outline,
            relaxed=relaxed,
            compact=compact,
        )
        governance_payload = self._generate_grounded_spec_section(
            section_id="governance",
            section_title="Assumptions and gaps",
            field_names=governance_fields,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline=outline,
            relaxed=relaxed,
            compact=compact,
        )

        merged_payload = {
            "schema_version": "1.0.0",
            "metadata": {
                "workspace_id": workspace_id,
                "conversation_id": f"conv_{workspace_id}",
                "prompt_turn_id": prompt_turn_id,
                "template_revision_id": template_revision_id,
                "language": "en",
                "created_at": utc_now().isoformat(),
            },
            "target_platform": target_platform.value,
            "preview_profile": preview_profile.value,
            "doc_refs": [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in doc_refs
            ],
            **self._normalize_model_payload(core_payload["payload"]),
            **self._normalize_model_payload(requirements_payload["payload"]),
            **self._normalize_model_payload(governance_payload["payload"]),
        }
        payload = {
            "model": governance_payload["model"],
            "payload": merged_payload,
            "response_mode": "grounded_spec_sections",
        }
        return outline_payload, payload, outline

    def _generate_grounded_spec_section(
        self,
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        relaxed: bool,
        compact: bool,
    ) -> dict[str, Any]:
        schema = self._grounded_spec_partial_schema(field_names)
        user_prompt = self._grounded_spec_section_user_prompt(
            section_id=section_id,
            section_title=section_title,
            field_names=field_names,
            prompt=prompt,
            doc_refs=doc_refs,
            target_platform=target_platform,
            preview_profile=preview_profile,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
            creative_direction=creative_direction,
            outline=outline,
            compact=compact,
        )
        if relaxed:
            return self._generate_json_object_with_retry(
                role="spec_analysis",
                schema_name=f"grounded_spec_{section_id}_v1",
                schema=schema,
                system_prompt=self._grounded_spec_section_system_prompt(section_title),
                user_prompt=user_prompt,
            )
        return self._generate_structured_with_retry(
            role="spec_analysis",
            schema_name=f"grounded_spec_{section_id}_v1",
            schema=schema,
            system_prompt=self._grounded_spec_section_system_prompt(section_title),
            user_prompt=user_prompt,
        )

    def _resolve_app_ir(
        self,
        *,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.BASIC or not self.openrouter_client.enabled:
            return {"ir": self._build_app_ir(spec, scenario_graph, generation_mode)}
        try:
            payload = self._generate_app_ir_sections(
                spec=spec,
                scenario_graph=scenario_graph,
                creative_direction=creative_direction,
            )
            llm_ir = AppIRModel.model_validate(self._normalize_model_payload(payload["payload"]))
            llm_ir = self._stabilize_app_ir(llm_ir, spec, scenario_graph, generation_mode)
            current_ir = self._enrich_app_ir(llm_ir, spec, scenario_graph, generation_mode)
            used_models = [str(payload["model"])]

            rounds = self._refinement_rounds_for_mode(generation_mode)
            for iteration in range(rounds):
                critique = self._critique_app_ir(spec, scenario_graph, current_ir, creative_direction)
                issues = critique.get("issues", [])
                should_repair = bool(critique.get("should_repair"))
                has_major_issues = any(
                    str(issue.get("severity", "")).lower() in {"critical", "high"}
                    for issue in issues
                    if isinstance(issue, dict)
                )
                if not should_repair and not has_major_issues:
                    break

                repair_payload = self._generate_structured_with_retry(
                    role="repair",
                    schema_name=f"app_ir_repair_v{iteration + 1}",
                    schema=AppIRModel.model_json_schema(),
                    system_prompt=self._app_ir_repair_system_prompt(),
                    user_prompt=self._app_ir_repair_user_prompt(spec, scenario_graph, current_ir, critique, creative_direction),
                )
                used_models.append(str(repair_payload["model"]))
                repaired_ir = AppIRModel.model_validate(self._normalize_model_payload(repair_payload["payload"]))
                repaired_ir = self._stabilize_app_ir(repaired_ir, spec, scenario_graph, generation_mode)
                current_ir = self._enrich_app_ir(repaired_ir, spec, scenario_graph, generation_mode)

                validation = self.validation_suite.validate_app_ir(current_ir)
                if not validation.blocking and not has_major_issues:
                    break

            return {
                "ir": current_ir,
                "model": used_models[-1],
                "model_sequence": used_models,
                "refinement_rounds": max(0, len(used_models) - 1),
            }
        except Exception as exc:
            return {
                "ir": self._build_app_ir(spec, scenario_graph, generation_mode),
                "model": None,
                "model_sequence": [],
                "refinement_rounds": 0,
                "warning": f"AppIR LLM step failed, fallback compiler IR was used: {exc}",
            }

    def _generate_app_ir_sections(
        self,
        *,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        structure_fields = [
            "app_id",
            "title",
            "platform",
            "preview_profile",
            "entry_screen_id",
            "screens",
            "transitions",
            "route_groups",
            "screen_data_sources",
            "role_action_groups",
            "terminal_screen_ids",
        ]
        domain_fields = ["variables", "entities", "integrations", "storage_bindings"]
        operations_fields = [
            "auth_model",
            "permissions",
            "security",
            "telemetry_hooks",
            "assumptions",
            "open_questions",
            "traceability",
        ]

        structure_payload = self._generate_app_ir_section(
            section_id="structure",
            section_title="Application structure and routes",
            field_names=structure_fields,
            spec=spec,
            scenario_graph=scenario_graph,
            creative_direction=creative_direction,
        )
        domain_payload = self._generate_app_ir_section(
            section_id="domain",
            section_title="State, entities, integrations, and storage",
            field_names=domain_fields,
            spec=spec,
            scenario_graph=scenario_graph,
            creative_direction=creative_direction,
        )
        operations_payload = self._generate_app_ir_section(
            section_id="operations",
            section_title="Auth, security, telemetry, and traceability",
            field_names=operations_fields,
            spec=spec,
            scenario_graph=scenario_graph,
            creative_direction=creative_direction,
        )
        merged_payload = {
            "schema_version": "1.0.0",
            "metadata": {
                "workspace_id": spec.metadata.workspace_id,
                "grounded_spec_version": spec.schema_version,
                "template_revision_id": spec.metadata.template_revision_id,
                "generated_at": utc_now().isoformat(),
            },
            **self._normalize_model_payload(structure_payload["payload"]),
            **self._normalize_model_payload(domain_payload["payload"]),
            **self._normalize_model_payload(operations_payload["payload"]),
        }
        return {
            "model": operations_payload["model"],
            "payload": merged_payload,
            "response_mode": "app_ir_sections",
        }

    def _generate_app_ir_section(
        self,
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        return self._generate_structured_with_retry(
            role="ir_codegen",
            schema_name=f"app_ir_{section_id}_v1",
            schema=self._app_ir_partial_schema(field_names),
            system_prompt=self._app_ir_section_system_prompt(section_title),
            user_prompt=self._app_ir_section_user_prompt(
                section_id=section_id,
                section_title=section_title,
                field_names=field_names,
                spec=spec,
                scenario_graph=scenario_graph,
                creative_direction=creative_direction,
            ),
        )

    def _critique_app_ir(
        self,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        ir: AppIRModel,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            payload = self._generate_structured_with_retry(
                role="cheap_task",
                schema_name="app_ir_critique_v1",
                schema=self._app_ir_critique_schema(),
                system_prompt=self._app_ir_critique_system_prompt(),
                user_prompt=self._app_ir_critique_user_prompt(spec, scenario_graph, ir, creative_direction),
            )
            normalized = self._normalize_model_payload(payload["payload"])
            if isinstance(normalized, dict):
                issues = normalized.get("issues")
                if not isinstance(issues, list):
                    normalized["issues"] = []
                normalized["should_repair"] = bool(normalized.get("should_repair"))
                normalized["repair_instructions"] = normalized.get("repair_instructions") or []
                return normalized
        except Exception:
            pass
        return {
            "should_repair": False,
            "issues": [],
            "repair_instructions": [],
            "summary": "Critique was skipped due to provider/runtime limits.",
        }

    @staticmethod
    def _refinement_rounds_for_mode(generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            return 0
        if generation_mode == GenerationMode.QUALITY:
            return max(1, int(os.getenv("QUALITY_REFINEMENT_ROUNDS", "3")))
        if generation_mode == GenerationMode.BALANCED:
            return max(0, int(os.getenv("BALANCED_REFINEMENT_ROUNDS", "1")))
        return 0

    def _stabilize_grounded_spec(self, spec: GroundedSpecModel) -> GroundedSpecModel:
        assumptions = list(spec.assumptions)
        unresolved_unknowns: list[Unknown] = []

        for unknown in spec.unknowns:
            question = unknown.question.lower()
            suggested_resolution = unknown.suggested_resolution or "Resolved through canonical template defaults."
            if any(
                marker in question
                for marker in (
                    "optional",
                    "required",
                    "endpoint",
                    "api",
                    "backend",
                    "manager",
                    "specialist",
                    "workflow",
                    "review flow",
                    "persistence",
                    "storage",
                )
            ):
                assumptions.append(
                    Assumption(
                        assumption_id=f"assume_{unknown.unknown_id}",
                        text=unknown.question,
                        status="active",
                        rationale=suggested_resolution,
                        impact="medium" if unknown.impact == "high" else unknown.impact,
                    )
                )
            else:
                unresolved_unknowns.append(unknown)

        api_requirements = list(spec.api_requirements)
        if not api_requirements and any(term in spec.product_goal.lower() for term in ("booking", "consultation", "form", "request")):
            evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Synthesized from prompt intent and canonical runtime defaults.")]
            api_requirements.extend(
                [
                    APIRequirement(
                        api_req_id="api_submit_primary_form",
                        name="Submit primary request",
                        method="POST",
                        path="/api/submissions",
                        purpose="Persist the primary end-user form submission in the generated mini-app backend.",
                        request_fields=[
                            APIField(name="name", type="string", required=True, description="End-user display name"),
                            APIField(name="phone", type="phone", required=True, description="End-user phone number"),
                            APIField(name="preferred_date", type="datetime", required=True, description="Requested consultation date"),
                            APIField(name="comment", type="text", required=False, description="Additional request comment"),
                        ],
                        response_fields=[
                            APIField(name="submission_id", type="uuid", required=True, description="Created request identifier"),
                            APIField(name="status", type="string", required=True, description="Current workflow status"),
                        ],
                        auth_required=False,
                        existing_in_template=False,
                        evidence=evidence,
                    ),
                    APIRequirement(
                        api_req_id="api_list_primary_requests",
                        name="List submitted requests",
                        method="GET",
                        path="/api/submissions",
                        purpose="Load current user submissions and role queues in the generated runtime.",
                        request_fields=[],
                        response_fields=[
                            APIField(name="items", type="array", required=True, description="Runtime submission records"),
                        ],
                        auth_required=False,
                        existing_in_template=False,
                        evidence=evidence,
                    ),
                ]
            )
            assumptions.append(
                Assumption(
                    assumption_id="assume_generated_submission_api",
                    text="The canonical generated backend exposes a default submission API for primary form flows.",
                    status="active",
                    rationale="Simple booking and request prompts should compile into a usable end-to-end demo without blocking on undocumented project-specific endpoints.",
                    impact="medium",
                )
            )

        actors = self._expand_role_actors(spec.actors, spec.doc_refs)
        user_flows = self._expand_role_flows(spec, actors)
        assumptions = self._ensure_role_expansion_assumption(spec, assumptions, actors)

        return spec.model_copy(
            update={
                "actors": actors,
                "user_flows": user_flows,
                "assumptions": assumptions,
                "unknowns": unresolved_unknowns,
                "api_requirements": api_requirements,
            }
        )

    def _stabilize_app_ir(
        self,
        ir: AppIRModel,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        generation_mode: GenerationMode,
    ) -> AppIRModel:
        fallback = self._build_app_ir(spec, scenario_graph, generation_mode)
        assumptions = list(ir.assumptions)
        open_questions: list[OpenQuestion] = []

        for question in ir.open_questions:
            lowered = question.text.lower()
            if any(
                marker in lowered
                for marker in (
                    "manager review",
                    "specialist assignment",
                    "notification",
                    "follow-up workflow",
                    "operational handling",
                    "approval flow",
                    "admin process",
                )
            ):
                assumptions.append(
                    IRAssumption(
                        assumption_id=f"assume_{question.question_id}",
                        text=question.text,
                        origin="compiler_default",
                    )
                )
                continue
            open_questions.append(question.model_copy(update={"blocking": False}))

        route_groups = ir.route_groups
        if {group.role for group in route_groups} != {"client", "specialist", "manager"}:
            route_groups = fallback.route_groups

        role_action_groups = ir.role_action_groups
        if {group.role for group in role_action_groups} != {"client", "specialist", "manager"}:
            role_action_groups = fallback.role_action_groups

        screen_data_sources = ir.screen_data_sources or fallback.screen_data_sources

        return ir.model_copy(
            update={
                "open_questions": open_questions,
                "assumptions": assumptions,
                "route_groups": route_groups,
                "role_action_groups": role_action_groups,
                "screen_data_sources": screen_data_sources,
            }
        )

    def _build_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list[Any],
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode: GenerationMode,
    ) -> GroundedSpecModel:
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="explicit")]
        entity_name = self._infer_entity_name(prompt)
        entity_attributes = self._infer_entity_attributes(prompt)
        contradictions = self._detect_contradictions(prompt)
        target_label = "Telegram Mini App" if target_platform == TargetPlatform.TELEGRAM else "MAX Mini App"
        return GroundedSpecModel(
            metadata=Metadata(
                workspace_id=workspace_id,
                conversation_id=f"conv_{workspace_id}",
                prompt_turn_id=prompt_turn_id,
                template_revision_id=template_revision_id,
                language="en",
                created_at=utc_now(),
            ),
            target_platform=target_platform,
            preview_profile=preview_profile,
            product_goal=prompt.strip(),
            actors=[
                Actor(
                    actor_id="actor_client",
                    name="End user",
                    role="client",
                    description="Submits and tracks requests in the mini-app.",
                    permissions_hint=["create_request", "view_own_requests"],
                    evidence=evidence,
                ),
                Actor(
                    actor_id="actor_specialist",
                    name="Specialist",
                    role="specialist",
                    description="Processes the incoming queue and updates request status.",
                    permissions_hint=["claim_request", "change_status", "respond"],
                    evidence=evidence,
                ),
                Actor(
                    actor_id="actor_manager",
                    name="Manager",
                    role="manager",
                    description="Monitors load, SLA, and the end-to-end workflow across roles.",
                    permissions_hint=["view_metrics", "rebalance_load"],
                    evidence=evidence,
                ),
            ],
            domain_entities=[
                DomainEntity(
                    entity_id="entity_request",
                    name=entity_name,
                    description=f"Primary domain object collected and processed for: {prompt}",
                    attributes=entity_attributes,
                    evidence=evidence,
                )
            ],
            user_flows=[
                UserFlow(
                    flow_id="flow_client_create",
                    name="Client request creation",
                    goal="Client submits a request and sees confirmation.",
                    steps=[
                        FlowStep(step_id="step_client_home", order=1, actor_id="actor_client", action="Open the client home screen"),
                        FlowStep(step_id="step_client_form", order=2, actor_id="actor_client", action="Navigate to the request form"),
                        FlowStep(step_id="step_client_submit", order=3, actor_id="actor_client", action="Submit the request"),
                    ],
                    postconditions=["A new request is created and visible to the client."],
                    acceptance_criteria=["The client can submit a request and open its detail page."],
                    evidence=evidence,
                ),
                UserFlow(
                    flow_id="flow_specialist_process",
                    name="Specialist queue processing",
                    goal="Specialist reviews the queue and updates request status.",
                    steps=[
                        FlowStep(step_id="step_specialist_home", order=1, actor_id="actor_specialist", action="Open specialist dashboard"),
                        FlowStep(step_id="step_specialist_queue", order=2, actor_id="actor_specialist", action="Open queue and claim an item"),
                        FlowStep(step_id="step_specialist_update", order=3, actor_id="actor_specialist", action="Move request to in-progress or completed"),
                    ],
                    postconditions=["Queue state and metrics are updated."],
                    acceptance_criteria=["The specialist can claim and complete requests."],
                    evidence=evidence,
                ),
                UserFlow(
                    flow_id="flow_manager_control",
                    name="Manager oversight",
                    goal="Manager views global metrics and rebalances workload.",
                    steps=[
                        FlowStep(step_id="step_manager_home", order=1, actor_id="actor_manager", action="Open manager home"),
                        FlowStep(step_id="step_manager_dashboard", order=2, actor_id="actor_manager", action="Open control dashboard"),
                        FlowStep(step_id="step_manager_rebalance", order=3, actor_id="actor_manager", action="Trigger load rebalance"),
                    ],
                    postconditions=["Control metrics reflect the current workload and SLA."],
                    acceptance_criteria=["The manager can see role health and trigger a control action."],
                    evidence=evidence,
                ),
            ],
            ui_requirements=[
                UIRequirement(req_id="ui_client_home", category="screen", description="Provide a client landing page with metrics and primary actions.", priority="must", evidence=evidence, screen_hint="client_home"),
                UIRequirement(req_id="ui_client_form", category="form", description="Render a multi-field request form with validation and confirmation.", priority="must", evidence=evidence, screen_hint="client_form"),
                UIRequirement(req_id="ui_client_requests", category="navigation", description="Allow the client to browse own requests and open detail pages.", priority="must", evidence=evidence, screen_hint="client_requests"),
                UIRequirement(req_id="ui_specialist_queue", category="screen", description="Render a specialist queue with next actions and request details.", priority="must", evidence=evidence, screen_hint="specialist_queue"),
                UIRequirement(req_id="ui_manager_dashboard", category="screen", description="Render a manager dashboard with metrics, alerts, and control actions.", priority="must", evidence=evidence, screen_hint="manager_dashboard"),
                UIRequirement(req_id="ui_theme", category="theme", description=f"Respect {target_label} theme and viewport constraints.", priority="should", evidence=evidence),
            ],
            api_requirements=[
                APIRequirement(
                    api_req_id="api_runtime_manifest",
                    name="Role manifest",
                    method="GET",
                    path="/api/runtime/{role}/manifest",
                    purpose="Fetch role-aware runtime manifest with screens, routes, and live data.",
                    response_fields=[APIField(name="screens", type="array", required=True)],
                    evidence=evidence,
                    existing_in_template=False,
                ),
                APIRequirement(
                    api_req_id="api_runtime_action",
                    name="Runtime action executor",
                    method="POST",
                    path="/api/runtime/{role}/actions/{action_id}",
                    purpose="Execute workflow actions, mutate state, and navigate.",
                    request_fields=[APIField(name="payload", type="object", required=False)],
                    response_fields=[APIField(name="next_path", type="string", required=False)],
                    evidence=evidence,
                    existing_in_template=False,
                ),
                APIRequirement(
                    api_req_id="api_submission_create",
                    name="Create request",
                    method="POST",
                    path="/api/submissions",
                    purpose="Persist user request submissions and expose them in queue/dashboard views.",
                    request_fields=[APIField(name=field.name, type=field.type, required=field.required) for field in entity_attributes],
                    response_fields=[
                        APIField(name="submission_id", type="uuid", required=True),
                        APIField(name="status", type="string", required=True),
                    ],
                    evidence=evidence,
                ),
            ],
            persistence_requirements=[
                PersistenceRequirement(
                    persistence_req_id="persist_request_create",
                    entity_id="entity_request",
                    operation="create",
                    storage_type="postgres",
                    evidence=evidence,
                ),
                PersistenceRequirement(
                    persistence_req_id="persist_request_list",
                    entity_id="entity_request",
                    operation="list",
                    storage_type="postgres",
                    evidence=evidence,
                ),
                PersistenceRequirement(
                    persistence_req_id="persist_request_update",
                    entity_id="entity_request",
                    operation="update",
                    storage_type="postgres",
                    evidence=evidence,
                ),
            ],
            integration_requirements=[
                IntegrationRequirement(
                    integration_req_id="integration_runtime_actions",
                    system_name="template_runtime_backend",
                    direction="bidirectional",
                    purpose="Drive live workflow actions and preview state transitions.",
                    auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
                    contract_ref="/api/runtime/{role}/actions/{action_id}",
                    evidence=evidence,
                )
            ],
            security_requirements=[
                SecurityRequirement(
                    security_req_id="security_initdata",
                    category="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "access_control",
                    rule="Trusted session context must only originate from validated host init data on the server.",
                    severity="critical",
                    evidence=evidence,
                ),
                SecurityRequirement(
                    security_req_id="security_input",
                    category="input_validation",
                    rule="All generated forms must validate user input before submission.",
                    severity="high",
                    evidence=evidence,
                ),
            ],
            platform_constraints=[
                PlatformConstraint(
                    constraint_id="platform_theme",
                    category="theme",
                    rule=f"Respect host-provided color scheme and viewport in {target_label}.",
                    severity="high",
                    evidence=evidence,
                ),
                PlatformConstraint(
                    constraint_id="platform_navigation",
                    category="navigation",
                    rule="Support role-aware navigation and host back behavior in every generated route tree.",
                    severity="high",
                    evidence=evidence,
                ),
            ],
            non_functional_requirements=[
                NonFunctionalRequirement(
                    nfr_id="nfr_traceability",
                    category="observability",
                    description="Every generated artifact must preserve prompt and document traceability.",
                    priority="must",
                    evidence=evidence,
                ),
                NonFunctionalRequirement(
                    nfr_id="nfr_quality_mode",
                    category="usability",
                    description="Quality mode should produce multi-page, stateful, role-aware applications with live actions.",
                    priority="must",
                    evidence=evidence,
                ),
            ],
            assumptions=[
                Assumption(
                    assumption_id="assume_three_roles",
                    text="The canonical template preserves the client, specialist, and manager roles.",
                    status="active",
                    rationale="The current platform preview requires simultaneous three-role runtime views.",
                    impact="medium",
                ),
                Assumption(
                    assumption_id="assume_runtime_dataset",
                    text="Balanced/basic generation uses generated demo runtime records and queues inside the canonical template.",
                    status="active",
                    rationale="A richer live preview needs interactive state even when external business APIs are not present.",
                    impact="medium",
                ),
            ],
            unknowns=[],
            contradictions=contradictions,
            doc_refs=list(doc_refs),
        )

    def _build_scenario_graph(self, spec: GroundedSpecModel) -> dict[str, Any]:
        roles = {
            "client": ["client_home", "client_form", "client_requests", "client_detail", "client_success", "client_profile"],
            "specialist": ["specialist_home", "specialist_queue", "specialist_detail", "specialist_profile"],
            "manager": ["manager_home", "manager_dashboard", "manager_records", "manager_profile"],
        }
        transitions = [
            ("client_home", "client_form"),
            ("client_home", "client_requests"),
            ("client_form", "client_success"),
            ("client_requests", "client_detail"),
            ("specialist_home", "specialist_queue"),
            ("specialist_queue", "specialist_detail"),
            ("manager_home", "manager_dashboard"),
            ("manager_dashboard", "manager_records"),
        ]
        nodes = sorted({screen_id for role_nodes in roles.values() for screen_id in role_nodes})
        edges = [{"from": source, "to": target} for source, target in transitions]
        return {
            "roles": roles,
            "nodes": nodes,
            "edges": edges,
            "transitions": transitions,
        }

    def _build_app_ir(
        self,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        generation_mode: GenerationMode,
    ) -> AppIRModel:
        target_platform = self._target_platform(spec.target_platform)
        primary_entity = spec.domain_entities[0]
        variables = self._build_variables(primary_entity.attributes)
        screens = self._build_screens(primary_entity, spec.product_goal)
        transitions = self._build_transitions()
        route_groups = self._build_route_groups()
        integrations = self._build_integrations(primary_entity.attributes, target_platform)
        traceability = self._build_traceability(route_groups, integrations, screens, spec)
        return AppIRModel(
            metadata=IRMetadata(
                workspace_id=spec.metadata.workspace_id,
                grounded_spec_version=spec.schema_version,
                template_revision_id=spec.metadata.template_revision_id,
                generated_at=utc_now(),
            ),
            app_id=f"app_{spec.metadata.workspace_id}",
            title=spec.product_goal[:80],
            platform=target_platform,
            preview_profile=self._preview_profile(spec.preview_profile),
            entry_screen_id="client_home",
            terminal_screen_ids=["client_success"],
            variables=variables,
            entities=[
                Entity(
                    entity_id=primary_entity.entity_id,
                    name=primary_entity.name,
                    fields=[
                        DataField(
                            name=attribute.name,
                            type=attribute.type,
                            required=attribute.required,
                            description=attribute.description,
                            pii=attribute.pii,
                        )
                        for attribute in primary_entity.attributes
                    ],
                )
            ],
            screens=screens,
            transitions=transitions,
            route_groups=route_groups,
            screen_data_sources=self._build_screen_data_sources(),
            role_action_groups=self._build_role_action_groups(),
            integrations=integrations,
            storage_bindings=[
                StorageBinding(
                    binding_id="storage_request",
                    entity_id=primary_entity.entity_id,
                    storage_type="postgres",
                    table_or_collection="requests",
                )
            ],
            auth_model=AuthModel(
                mode="telegram_session" if target_platform == TargetPlatform.TELEGRAM else "custom",
                telegram_initdata_validation_required=target_platform == TargetPlatform.TELEGRAM,
                server_side_session=True,
            ),
            permissions=[
                Permission(permission_id="permission_create_request", name="create_request", description="Allows request creation."),
                Permission(permission_id="permission_process_request", name="process_request", description="Allows queue processing."),
                Permission(permission_id="permission_control_dashboard", name="control_dashboard", description="Allows control actions."),
            ],
            security=SecurityPolicy(
                trusted_sources=["validated_init_data"] if target_platform == TargetPlatform.TELEGRAM else ["validated_host_session"],
                untrusted_sources=["user_input", "client_storage", "unsafe_host_payload"],
                secret_handling="server_env_only",
                pii_variables=[variable.variable_id for variable in variables if variable.pii],
            ),
            telemetry_hooks=[
                TelemetryHook(event_name="client_home_view", trigger_type="screen_view", screen_id="client_home"),
                TelemetryHook(event_name="client_submit", trigger_type="form_submit", action_id="client_submit_request"),
                TelemetryHook(event_name="specialist_claim", trigger_type="button_click", action_id="specialist_claim_next"),
                TelemetryHook(event_name="manager_rebalance", trigger_type="button_click", action_id="manager_rebalance"),
            ],
            assumptions=[
                IRAssumption(
                    assumption_id="ir_role_runtime",
                    text="The generated application compiles into a manifest-driven multi-role runtime template.",
                    origin="compiler_default",
                ),
                IRAssumption(
                    assumption_id="ir_generation_mode",
                    text=f"Generation mode is {generation_mode.value}.",
                    origin="grounded_spec",
                ),
            ],
            open_questions=[],
            traceability=traceability,
        )

    def _enrich_app_ir(
        self,
        llm_ir: AppIRModel,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        generation_mode: GenerationMode,
    ) -> AppIRModel:
        fallback = self._build_app_ir(spec, scenario_graph, generation_mode)
        screens = llm_ir.screens or fallback.screens
        route_groups = llm_ir.route_groups or fallback.route_groups
        screen_data_sources = llm_ir.screen_data_sources or fallback.screen_data_sources
        role_action_groups = llm_ir.role_action_groups or fallback.role_action_groups
        terminal_screen_ids = llm_ir.terminal_screen_ids or fallback.terminal_screen_ids
        return llm_ir.model_copy(
            update={
                "preview_profile": fallback.preview_profile,
                "entry_screen_id": llm_ir.entry_screen_id or fallback.entry_screen_id,
                "terminal_screen_ids": terminal_screen_ids,
                "screens": screens or fallback.screens,
                "route_groups": route_groups,
                "screen_data_sources": screen_data_sources,
                "role_action_groups": role_action_groups,
            }
        )

    def _build_artifact_plan(
        self,
        workspace_id: str,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        generation_mode: GenerationMode,
    ) -> ArtifactPlanModel:
        runtime_manifest = self._build_runtime_manifest(spec, ir, generation_mode)
        runtime_state = self._build_runtime_state(spec, ir, generation_mode)
        role_seed = self._build_role_seed(runtime_manifest, runtime_state)
        role_experience = {
            role: {
                "title": payload["title"],
                "featureText": payload["feature_text"],
            }
            for role, payload in role_seed["roles"].items()
        }
        operations = [
            PatchOperationModel(
                operation_id="op_grounded_spec",
                op="update",
                file_path="artifacts/grounded_spec.json",
                content=json_dumps(spec.model_dump(mode="json")),
                explanation="Persist the current grounded specification.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_app_ir",
                op="update",
                file_path="backend/app/generated/app_ir.json",
                content=json_dumps(ir.model_dump(mode="json")),
                explanation="Persist the current typed AppIR for the template backend.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_runtime_manifest_backend",
                op="update",
                file_path="backend/app/generated/runtime_manifest.json",
                content=json_dumps(runtime_manifest),
                explanation="Compile a role-aware runtime manifest for backend APIs.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_runtime_state",
                op="update",
                file_path="backend/app/generated/runtime_state.json",
                content=json_dumps(runtime_state),
                explanation="Seed mutable runtime state for live preview actions and list/detail flows.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_frontend_runtime_manifest",
                op="update",
                file_path="frontend/src/shared/generated/runtime-manifest.json",
                content=json_dumps(runtime_manifest),
                explanation="Provide a frontend fallback/runtime manifest for generated routing and rendering.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_preview_payload",
                op="update",
                file_path="frontend/src/shared/generated/role-experience.json",
                content=json_dumps(role_experience),
                explanation="Compile role-aware marketing/summary descriptors for platform preview cards.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_role_seed",
                op="update",
                file_path="backend/app/generated/role_seed.json",
                content=json_dumps(role_seed),
                explanation="Compile role-aware summaries and metrics for inline preview fallback.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_traceability",
                op="update",
                file_path="artifacts/traceability.json",
                content=json_dumps(self._build_traceability_report(workspace_id, ir).model_dump(mode="json")),
                explanation="Persist traceability links for the generated artifacts.",
                trace_refs=["prompt-source"],
            ),
        ]
        return ArtifactPlanModel(
            plan_id=new_id("artifact_plan"),
            workspace_id=workspace_id,
            summary="Compile grounded artifacts into the canonical multi-page role runtime.",
            operations=operations,
        )

    @staticmethod
    def _select_creative_direction(prompt: str) -> dict[str, Any]:
        strategies = [
            {
                "name": "workflow-command-center",
                "focus": "Operations-first control surface with strong status visibility.",
                "layout_bias": "dashboard",
                "interaction_bias": "bulk operations and quick triage actions",
                "tone": "decisive",
            },
            {
                "name": "guided-service-journey",
                "focus": "Step-based journey that emphasizes clarity and completion confidence.",
                "layout_bias": "stream",
                "interaction_bias": "guided progression with contextual details",
                "tone": "supportive",
            },
            {
                "name": "workspace-knowledge-hub",
                "focus": "Entity-centric workspace with dense detail and history views.",
                "layout_bias": "magazine",
                "interaction_bias": "exploration and drill-down",
                "tone": "analytical",
            },
            {
                "name": "lean-minimal-ops",
                "focus": "Minimal but complete flows with reduced chrome and direct actions.",
                "layout_bias": "minimal",
                "interaction_bias": "direct action with low navigation overhead",
                "tone": "concise",
            },
        ]
        seed = f"{prompt}|creative|{datetime.now(timezone.utc).isoformat(timespec='microseconds')}"
        index = sum(ord(ch) for ch in seed) % len(strategies)
        selected = dict(strategies[index])
        selected["seed"] = seed[-16:]
        return selected

    def _build_runtime_manifest(
        self,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        entity = spec.domain_entities[0]
        records = self._sample_records(entity, spec.product_goal)
        flow_label = self._flow_label(spec.product_goal, entity)
        ui_variant = self._select_ui_variant(spec.product_goal)
        layout_variant = self._select_layout_variant(spec.product_goal, ui_variant)
        theme_palette = self._select_theme_palette(spec.product_goal, ui_variant)
        roles: dict[str, Any] = {}
        for role in ROLE_ORDER:
            role_records = self._records_for_role(role, records)
            screen_id = f"{role}_workspace"
            route_id = f"route_{role}_workspace"
            role_screens = {
                screen_id: {
                    "screen_id": screen_id,
                    "path": "/",
                    "title": self._title_for_role(role, flow_label, ui_variant),
                    "subtitle": self._role_body(role, flow_label, ui_variant),
                    "kind": "workspace",
                    "components": [],
                    "actions": [],
                    "sections": self._freeform_role_sections(role, entity, role_records, spec.product_goal, ui_variant, layout_variant),
                }
            }
            roles[role] = {
                "entry_path": "/",
                "routes": [
                    {
                        "route_id": route_id,
                        "role": role,
                        "path": "/",
                        "screen_id": screen_id,
                        "label": "Workspace",
                        "is_entry": True,
                    }
                ],
                "screens": role_screens,
                "navigation": [],
            }
        return {
            "app": {
                "title": spec.product_goal[:80],
                "goal": spec.product_goal,
                "generation_mode": generation_mode.value,
                "ui_variant": ui_variant,
                "layout_variant": layout_variant,
                "theme": theme_palette,
                "platform": spec.target_platform,
                "preview_profile": spec.preview_profile,
                "route_count": len(ROLE_ORDER),
                "screen_count": len(ROLE_ORDER),
            },
            "roles": roles,
        }

    @staticmethod
    def _select_ui_variant(prompt: str) -> str:
        return "studio"

    @staticmethod
    def _select_layout_variant(prompt: str, ui_variant: str) -> str:
        if GenerationService._is_commerce_prompt(prompt):
            return "stream"
        return "stacked"

    @staticmethod
    def _select_theme_palette(prompt: str, ui_variant: str) -> dict[str, str]:
        palettes = [
            {"accent": "#2d7ff9", "accent_soft": "#e8f1ff", "surface": "#ffffff", "card": "#f8fbff", "border": "#d8e4f7"},
            {"accent": "#0f8f6a", "accent_soft": "#e6fbf3", "surface": "#fcfffd", "card": "#f1fbf7", "border": "#cdeedd"},
            {"accent": "#c24a2f", "accent_soft": "#fff1ea", "surface": "#fffdfc", "card": "#fff7f2", "border": "#f1d5ca"},
            {"accent": "#6b46d4", "accent_soft": "#f0eaff", "surface": "#fefcff", "card": "#f7f2ff", "border": "#dfd2fb"},
            {"accent": "#0c7a91", "accent_soft": "#e6f8fd", "surface": "#fbfeff", "card": "#f0fbff", "border": "#c9eaf3"},
            {"accent": "#9a3412", "accent_soft": "#fff0ea", "surface": "#fffefd", "card": "#fff7f4", "border": "#f2d9ce"},
        ]
        seed = f"{prompt}|{ui_variant}|{datetime.now(timezone.utc).isoformat(timespec='microseconds')}"
        index = sum(ord(ch) for ch in seed) % len(palettes)
        return palettes[index]

    def _build_runtime_state(
        self,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        entity = spec.domain_entities[0]
        records = self._sample_records(entity, spec.product_goal)
        counts = Counter(record["status"] for record in records)
        return {
            "metadata": {
                "generated_at": utc_now().isoformat(),
                "generation_mode": generation_mode.value,
                "goal": spec.product_goal,
            },
            "records": records,
            "roles": {
                "client": {
                    "profile": {
                        "first_name": "Иван",
                        "last_name": "Иванов",
                        "email": "",
                        "phone": "",
                        "photo_url": None,
                    },
                    "metrics": [
                        {"metric_id": "client_total", "label": "Requests", "value": str(len(records))},
                        {"metric_id": "client_active", "label": "Active", "value": str(counts.get("in_progress", 0) + counts.get("new", 0))},
                    ],
                },
                "specialist": {
                    "profile": {
                        "first_name": "Иван",
                        "last_name": "Иванов",
                        "email": "",
                        "phone": "",
                        "photo_url": None,
                    },
                    "metrics": [
                        {"metric_id": "queue_size", "label": "Queue", "value": str(counts.get("new", 0))},
                        {"metric_id": "in_progress", "label": "In progress", "value": str(counts.get("in_progress", 0))},
                    ],
                },
                "manager": {
                    "profile": {
                        "first_name": "Иван",
                        "last_name": "Иванов",
                        "email": "",
                        "phone": "",
                        "photo_url": None,
                    },
                    "metrics": [
                        {"metric_id": "completed", "label": "Completed", "value": str(counts.get("completed", 0))},
                        {"metric_id": "sla", "label": "SLA", "value": "96%"},
                    ],
                    "alerts": [],
                },
            },
            "activity": [],
        }

    def _build_role_seed(self, runtime_manifest: dict[str, Any], runtime_state: dict[str, Any]) -> dict[str, Any]:
        role_seed = {"roles": {}}
        for role in ROLE_ORDER:
            role_manifest = runtime_manifest["roles"][role]
            home_screen = next(iter(role_manifest["screens"].values()))
            role_state = runtime_state["roles"][role]
            role_seed["roles"][role] = {
                "title": home_screen["title"],
                "description": home_screen["subtitle"] or runtime_manifest["app"]["goal"],
                "feature_text": self._seed_feature_text(home_screen, runtime_manifest["app"]["goal"]),
                "primary_action_label": home_screen["actions"][0]["label"] if home_screen["actions"] else "Open",
                "secondary_action_label": "Profile",
                "metrics": role_state["metrics"],
                "profile": role_state["profile"],
            }
        return role_seed

    @staticmethod
    def _seed_feature_text(screen: dict[str, Any], fallback: str) -> str:
        sections = screen.get("sections", [])
        if not isinstance(sections, list):
            return fallback
        for section in sections:
            if not isinstance(section, dict):
                continue
            body = section.get("body")
            if isinstance(body, str) and body.strip():
                return body
            title = section.get("title")
            if isinstance(title, str) and title.strip():
                return title
        return fallback

    def _build_traceability_report(self, workspace_id: str, ir: AppIRModel) -> TraceabilityReportModel:
        return TraceabilityReportModel(
            report_id=new_id("trace"),
            workspace_id=workspace_id,
            entries=[
                TraceabilityReportEntry(
                    trace_id=link.trace_id,
                    source_ref=link.source_ref,
                    source_kind=link.source_kind,
                    target_id=link.target_id,
                    target_type=link.target_type,
                    mapping_note=link.mapping_note,
                )
                for link in ir.traceability
            ],
        )

    def _build_variables(self, attributes: list[EntityAttribute]) -> list[Variable]:
        variables = [
            Variable(
                variable_id=f"var_{attribute.name}",
                name=attribute.name,
                type=attribute.type,
                required=attribute.required,
                source="user_input",
                trust_level="untrusted",
                scope="screen",
                pii=attribute.pii,
            )
            for attribute in attributes
        ]
        variables.extend(
            [
                Variable(
                    variable_id="var_request_id",
                    name="request_id",
                    type="uuid",
                    required=False,
                    source="api_response",
                    trust_level="validated",
                    scope="flow",
                ),
                Variable(
                    variable_id="var_current_role",
                    name="current_role",
                    type="string",
                    required=True,
                    source="validated_init_data",
                    trust_level="trusted",
                    scope="session",
                ),
            ]
        )
        return variables

    def _build_screens(self, entity: DomainEntity, prompt: str) -> list[Screen]:
        flow_label = self._flow_label(prompt, entity)
        entity_title = self._entity_title(entity)
        entity_plural = self._entity_plural(entity)
        form_components = [
            Component(
                component_id=f"cmp_form_{attribute.name}",
                type=self._component_type(attribute.type),
                label=attribute.name.replace("_", " ").title(),
                binding_variable_id=f"var_{attribute.name}",
                required=attribute.required,
                validators=self._component_validators(attribute),
                placeholder=f"Enter {attribute.name.replace('_', ' ')}",
            )
            for attribute in entity.attributes
        ]
        form_components.append(
            Component(
                component_id="cmp_form_submit",
                type="button",
                label="Submit request",
                binding_variable_id="var_request_id",
                required=False,
                validators=[],
            )
        )

        def screen(screen_id: str, kind: str, title: str, subtitle: str, actions: list[Action], components: list[Component] | None = None) -> Screen:
            return Screen(
                screen_id=screen_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                subtitle=subtitle,
                components=components or [],
                actions=actions,
                platform_hints=PlatformHints(
                    use_back_button=screen_id not in {"client_home", "specialist_home", "manager_home"},
                    use_main_button=kind == "form",
                    respect_theme=True,
                    respect_viewport=True,
                ),
            )

        return [
            screen(
                "client_home",
                "landing",
                f"{entity_title} home",
                f"Create, monitor, and manage your {flow_label} lifecycle end-to-end.",
                [
                    Action(action_id="client_open_form", type="navigate", target_screen_id="client_form"),
                    Action(action_id="client_open_requests", type="navigate", target_screen_id="client_requests"),
                    Action(action_id="client_open_profile", type="navigate", target_screen_id="client_profile"),
                ],
            ),
            screen(
                "client_form",
                "form",
                f"Create {flow_label}",
                f"Provide complete details, validate inputs, and submit into the shared operational workflow.",
                [
                    Action(
                        action_id="client_submit_request",
                        type="submit_form",
                        source_component_id="cmp_form_submit",
                        integration_id="integration_submit_request",
                        input_variable_ids=[f"var_{attribute.name}" for attribute in entity.attributes],
                        success_transition_id="transition_client_success",
                        error_transition_id="transition_client_form_error",
                    )
                ],
                form_components,
            ),
            screen(
                "client_requests",
                "list",
                f"My {entity_plural}",
                f"Browse active, pending, and completed {flow_label} records.",
                [
                    Action(action_id="client_open_request_detail", type="navigate", target_screen_id="client_detail"),
                    Action(action_id="client_open_form_inline", type="navigate", target_screen_id="client_form"),
                ],
            ),
            screen(
                "client_detail",
                "details",
                f"{entity_title} detail",
                f"Inspect status history, ownership, timeline, and next actions for this {flow_label}.",
                [Action(action_id="client_back_to_requests", type="navigate", target_screen_id="client_requests")],
            ),
            screen(
                "client_success",
                "success",
                f"{entity_title} submitted",
                f"The {flow_label} was persisted and routed to downstream processing.",
                [
                    Action(action_id="client_success_to_requests", type="navigate", target_screen_id="client_requests"),
                    Action(action_id="client_success_to_home", type="navigate", target_screen_id="client_home"),
                ],
            ),
            screen(
                "client_profile",
                "info",
                "Client profile",
                f"Manage contact and preference details used throughout {flow_label} execution.",
                [Action(action_id="client_profile_save", type="call_api", integration_id="integration_save_profile")],
            ),
            screen(
                "specialist_home",
                "landing",
                f"{entity_title} operations",
                f"Review incoming {flow_label} workload, priorities, and next actions.",
                [
                    Action(action_id="specialist_open_queue", type="navigate", target_screen_id="specialist_queue"),
                    Action(action_id="specialist_open_profile", type="navigate", target_screen_id="specialist_profile"),
                ],
            ),
            screen(
                "specialist_queue",
                "list",
                f"{entity_title} worklist",
                f"Claim {flow_label} items, execute processing steps, and update lifecycle status.",
                [
                    Action(action_id="specialist_claim_next", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="specialist_open_detail", type="navigate", target_screen_id="specialist_detail"),
                ],
            ),
            screen(
                "specialist_detail",
                "details",
                f"{entity_title} details",
                f"Review full {flow_label} context and apply controlled status transitions.",
                [
                    Action(action_id="specialist_mark_in_progress", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="specialist_complete_request", type="call_api", integration_id="integration_runtime_action"),
                ],
            ),
            screen(
                "specialist_profile",
                "info",
                "Specialist profile",
                f"Adjust specialist data and operational preferences used in {flow_label} handling.",
                [Action(action_id="specialist_profile_save", type="call_api", integration_id="integration_save_profile")],
            ),
            screen(
                "manager_home",
                "landing",
                f"{entity_title} overview",
                f"Inspect {flow_label} throughput, bottlenecks, and control actions across the full pipeline.",
                [
                    Action(action_id="manager_open_dashboard", type="navigate", target_screen_id="manager_dashboard"),
                    Action(action_id="manager_open_profile", type="navigate", target_screen_id="manager_profile"),
                ],
            ),
            screen(
                "manager_dashboard",
                "list",
                f"{entity_title} operations board",
                f"Review aggregate {flow_label} metrics, SLA risks, and control decisions.",
                [
                    Action(action_id="manager_rebalance", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="manager_open_records", type="navigate", target_screen_id="manager_records"),
                ],
            ),
            screen(
                "manager_records",
                "details",
                f"All {entity_plural}",
                f"Inspect {flow_label} records by status, ownership, and escalation state.",
                [Action(action_id="manager_refresh_records", type="call_api", integration_id="integration_runtime_action")],
            ),
            screen(
                "manager_profile",
                "info",
                "Manager profile",
                f"Manage governance and notification preferences for {flow_label} operations.",
                [Action(action_id="manager_profile_save", type="call_api", integration_id="integration_save_profile")],
            ),
        ]

    def _build_transitions(self) -> list[Transition]:
        return [
            Transition(transition_id="transition_client_to_form", from_screen_id="client_home", to_screen_id="client_form", trigger="open_form"),
            Transition(transition_id="transition_client_to_requests", from_screen_id="client_home", to_screen_id="client_requests", trigger="open_requests"),
            Transition(transition_id="transition_client_success", from_screen_id="client_form", to_screen_id="client_success", trigger="submit_success"),
            Transition(transition_id="transition_client_form_error", from_screen_id="client_form", to_screen_id="client_form", trigger="submit_error"),
            Transition(transition_id="transition_client_detail", from_screen_id="client_requests", to_screen_id="client_detail", trigger="open_detail"),
            Transition(transition_id="transition_specialist_queue", from_screen_id="specialist_home", to_screen_id="specialist_queue", trigger="open_queue"),
            Transition(transition_id="transition_specialist_detail", from_screen_id="specialist_queue", to_screen_id="specialist_detail", trigger="open_detail"),
            Transition(transition_id="transition_manager_dashboard", from_screen_id="manager_home", to_screen_id="manager_dashboard", trigger="open_dashboard"),
            Transition(transition_id="transition_manager_records", from_screen_id="manager_dashboard", to_screen_id="manager_records", trigger="open_records"),
        ]

    def _build_route_groups(self) -> list[RoleRouteGroup]:
        return [
            RoleRouteGroup(
                role="client",
                entry_path="/",
                routes=[
                    RouteDefinition(route_id="route_client_home", role="client", path="/", screen_id="client_home", label="Home", is_entry=True),
                    RouteDefinition(route_id="route_client_form", role="client", path="/book", screen_id="client_form", label="Book"),
                    RouteDefinition(route_id="route_client_requests", role="client", path="/requests", screen_id="client_requests", label="Requests"),
                    RouteDefinition(route_id="route_client_detail", role="client", path="/requests/detail", screen_id="client_detail", label="Detail"),
                    RouteDefinition(route_id="route_client_success", role="client", path="/success", screen_id="client_success", label="Success"),
                    RouteDefinition(route_id="route_client_profile", role="client", path="/profile", screen_id="client_profile", label="Profile"),
                ],
            ),
            RoleRouteGroup(
                role="specialist",
                entry_path="/",
                routes=[
                    RouteDefinition(route_id="route_specialist_home", role="specialist", path="/", screen_id="specialist_home", label="Home", is_entry=True),
                    RouteDefinition(route_id="route_specialist_queue", role="specialist", path="/queue", screen_id="specialist_queue", label="Queue"),
                    RouteDefinition(route_id="route_specialist_detail", role="specialist", path="/queue/detail", screen_id="specialist_detail", label="Detail"),
                    RouteDefinition(route_id="route_specialist_profile", role="specialist", path="/profile", screen_id="specialist_profile", label="Profile"),
                ],
            ),
            RoleRouteGroup(
                role="manager",
                entry_path="/",
                routes=[
                    RouteDefinition(route_id="route_manager_home", role="manager", path="/", screen_id="manager_home", label="Home", is_entry=True),
                    RouteDefinition(route_id="route_manager_dashboard", role="manager", path="/dashboard", screen_id="manager_dashboard", label="Dashboard"),
                    RouteDefinition(route_id="route_manager_records", role="manager", path="/records", screen_id="manager_records", label="Records"),
                    RouteDefinition(route_id="route_manager_profile", role="manager", path="/profile", screen_id="manager_profile", label="Profile"),
                ],
            ),
        ]

    def _build_screen_data_sources(self) -> list[ScreenDataSource]:
        return [
            ScreenDataSource(source_id="ds_client_home", screen_id="client_home", kind="dashboard", state_key="roles.client", role="client"),
            ScreenDataSource(source_id="ds_client_requests", screen_id="client_requests", kind="list", state_key="records.client", role="client"),
            ScreenDataSource(source_id="ds_client_detail", screen_id="client_detail", kind="detail", state_key="records.client_detail", role="client"),
            ScreenDataSource(source_id="ds_client_form", screen_id="client_form", kind="form", state_key="forms.client_request", role="client"),
            ScreenDataSource(source_id="ds_specialist_home", screen_id="specialist_home", kind="dashboard", state_key="roles.specialist", role="specialist"),
            ScreenDataSource(source_id="ds_specialist_queue", screen_id="specialist_queue", kind="list", state_key="records.queue", role="specialist"),
            ScreenDataSource(source_id="ds_specialist_detail", screen_id="specialist_detail", kind="detail", state_key="records.queue_detail", role="specialist"),
            ScreenDataSource(source_id="ds_manager_home", screen_id="manager_home", kind="dashboard", state_key="roles.manager", role="manager"),
            ScreenDataSource(source_id="ds_manager_dashboard", screen_id="manager_dashboard", kind="dashboard", state_key="roles.manager_dashboard", role="manager"),
            ScreenDataSource(source_id="ds_manager_records", screen_id="manager_records", kind="list", state_key="records.all", role="manager"),
        ]

    def _build_role_action_groups(self) -> list[RoleActionGroup]:
        return [
            RoleActionGroup(role="client", action_ids=["client_open_form", "client_open_requests", "client_submit_request"]),
            RoleActionGroup(role="specialist", action_ids=["specialist_open_queue", "specialist_claim_next", "specialist_complete_request"]),
            RoleActionGroup(role="manager", action_ids=["manager_open_dashboard", "manager_rebalance", "manager_refresh_records"]),
        ]

    def _build_integrations(self, attributes: list[EntityAttribute], target_platform: TargetPlatform) -> list[Integration]:
        return [
            Integration(
                integration_id="integration_submit_request",
                name="Submit request",
                type="rest",
                method="POST",
                path="/api/submissions",
                request_schema=[
                    DataField(name=attribute.name, type=attribute.type, required=attribute.required, pii=attribute.pii)
                    for attribute in attributes
                ],
                response_schema=[
                    DataField(name="submission_id", type="uuid", required=True),
                    DataField(name="status", type="string", required=True),
                ],
                auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
            ),
            Integration(
                integration_id="integration_runtime_action",
                name="Runtime action",
                type="rest",
                method="POST",
                path="/api/runtime/{role}/actions/{action_id}",
                request_schema=[],
                response_schema=[
                    DataField(name="next_path", type="string", required=False),
                    DataField(name="message", type="string", required=False),
                ],
                auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
            ),
            Integration(
                integration_id="integration_save_profile",
                name="Save profile",
                type="rest",
                method="PUT",
                path="/api/profiles/{role}",
                request_schema=[
                    DataField(name="first_name", type="string", required=True),
                    DataField(name="last_name", type="string", required=False),
                    DataField(name="email", type="email", required=False),
                    DataField(name="phone", type="phone", required=False),
                ],
                response_schema=[DataField(name="updated_at", type="datetime", required=False)],
                auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
            ),
        ]

    def _build_traceability(
        self,
        route_groups: list[RoleRouteGroup],
        integrations: list[Integration],
        screens: list[Screen],
        spec: GroundedSpecModel,
    ) -> list[TraceabilityLink]:
        links = [
            TraceabilityLink(
                trace_id=f"trace_screen_{screen.screen_id}",
                target_type="screen",
                target_id=screen.screen_id,
                source_kind="prompt_fragment",
                source_ref="prompt-source",
                mapping_note=f"Compiled generated screen {screen.title} from grounded prompt and platform docs.",
            )
            for screen in screens
        ]
        links.extend(
            TraceabilityLink(
                trace_id=f"trace_integration_{integration.integration_id}",
                target_type="integration",
                target_id=integration.integration_id,
                source_kind="doc_ref",
                source_ref="prompt-source",
                mapping_note=f"Generated integration {integration.name} from grounded spec requirements.",
            )
            for integration in integrations
        )
        links.extend(
            TraceabilityLink(
                trace_id=f"trace_route_{route.route_id}",
                target_type="transition",
                target_id=route.screen_id,
                source_kind="doc_ref",
                source_ref="prompt-source",
                mapping_note=f"Generated route {route.path} for role {route.role}.",
            )
            for group in route_groups
            for route in group.routes
        )
        links.extend(
            TraceabilityLink(
                trace_id=f"trace_variable_{attribute.name}",
                target_type="variable",
                target_id=f"var_{attribute.name}",
                source_kind="doc_ref",
                source_ref="prompt-source",
                mapping_note=f"Generated variable for field {attribute.name}.",
            )
            for attribute in spec.domain_entities[0].attributes
        )
        return links

    def _sample_records(self, entity: DomainEntity, prompt: str) -> list[dict[str, Any]]:
        attribute_names = [attribute.name for attribute in entity.attributes]

        def field_value(attribute: EntityAttribute, index: int) -> str:
            if attribute.type == "phone":
                return ""
            if attribute.type == "email":
                return ""
            if attribute.type == "date":
                return f"2026-03-1{index}"
            if attribute.type == "text":
                return f"Notes for {prompt[:48]}"
            return f"{attribute.name.replace('_', ' ').title()} {index}"

        records = []
        statuses = ["new", "in_progress", "completed"]
        for index in range(1, 5):
            payload = {name: field_value(next(attribute for attribute in entity.attributes if attribute.name == name), index) for name in attribute_names}
            records.append(
                {
                    "record_id": f"req_{index}",
                    "title": f"{entity.name} #{index}",
                    "status": statuses[(index - 1) % len(statuses)],
                    "priority": "high" if index == 1 else "medium",
                    "owner": "specialist" if index % 2 == 0 else "unassigned",
                    "summary": prompt[:96],
                    "payload": payload,
                    "timeline": [
                        {"label": "Created", "value": f"2026-03-0{index} 09:30"},
                        {"label": "Routed", "value": f"2026-03-0{index} 09:45"},
                    ],
                }
            )
        return records

    def _records_for_role(self, role: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if role == "client":
            return records[:3]
        return records

    def _screen_sections(
        self,
        role: str,
        screen_id: str,
        kind: str,
        entity: DomainEntity,
        records: list[dict[str, Any]],
        prompt_goal: str,
        ui_variant: str,
        layout_variant: str,
    ) -> list[dict[str, Any]]:
        flow_label = self._flow_label(prompt_goal, entity)
        entity_title = self._entity_title(entity)

        def list_items(limit: int | None = None) -> list[dict[str, Any]]:
            source = records[:limit] if limit is not None else records
            return [
                {
                    "item_id": record["record_id"],
                    "title": record["title"],
                    "subtitle": record["summary"],
                    "status": record["status"],
                    "meta": record["priority"],
                }
                for record in source
            ]

        if screen_id.endswith("home"):
            stats = {
                "section_id": f"{screen_id}_stats",
                "type": "stats",
                "items": self._role_stats(role, records, ui_variant),
            }
            preview_list = {
                "section_id": f"{screen_id}_list_preview",
                "type": "list",
                "items": list_items(3),
            }
            summary = {
                "section_id": f"{screen_id}_summary",
                "type": "detail",
                "title": self._title_for_role(role, flow_label, ui_variant),
                "body": self._role_body(role, flow_label, ui_variant),
                "fields": self._role_summary_fields(role, flow_label, records),
            }
            activity = {
                "section_id": f"{screen_id}_activity",
                "type": "timeline",
                "items": self._role_timeline(role, flow_label, records),
            }
            if "order" in flow_label:
                commerce_layouts = {
                    "client": [preview_list, summary],
                    "specialist": [stats, preview_list, activity],
                    "manager": [stats, activity, summary],
                }
                return commerce_layouts[role]
            if layout_variant == "minimal":
                return [summary]
            if layout_variant == "stream":
                return [preview_list, summary]
            if layout_variant == "dashboard":
                return [stats, preview_list, summary]
            if layout_variant == "magazine":
                return [preview_list, summary, stats]
            if ui_variant == "atlas":
                return [summary, preview_list, stats]
            if ui_variant == "pulse":
                return [stats, summary]
            if ui_variant == "editorial":
                return [summary]
            return [summary, stats]

        if screen_id == "client_form":
            intro_body = (
                f"Complete the {flow_label} form with validated inputs and route it into the shared workflow."
                if ui_variant != "editorial"
                else f"Capture the core {flow_label} details and submit for operational processing."
            )
            intro_section = {
                "section_id": "client_form_intro",
                "type": "hero",
                "title": f"{entity_title} form",
                "body": intro_body,
            }
            form_section = {
                "section_id": "client_form_fields",
                "type": "form",
                "fields": [
                    {
                        "field_id": attribute.name,
                        "name": attribute.name,
                        "label": attribute.name.replace("_", " ").title(),
                        "field_type": attribute.type,
                        "required": attribute.required,
                        "placeholder": f"Enter {attribute.name.replace('_', ' ')}",
                    }
                    for attribute in entity.attributes
                ],
            }
            if layout_variant == "minimal":
                return [form_section]
            if layout_variant == "stream":
                return [form_section, intro_section]
            return [
                intro_section,
                form_section,
            ]

        if kind == "list":
            list_section = {
                "section_id": f"{screen_id}_list",
                "type": "list",
                "items": list_items(),
            }
            stats_section = {
                "section_id": f"{screen_id}_stats",
                "type": "stats",
                "items": self._role_stats(role, records, ui_variant),
            }
            if layout_variant == "minimal":
                return [list_section]
            if layout_variant == "stream":
                return [list_section, stats_section]
            if layout_variant == "dashboard" or ui_variant == "pulse":
                return [stats_section, list_section]
            return [list_section]

        if kind == "details":
            record = records[0] if records else {"title": entity.name, "summary": prompt_goal, "payload": {}, "timeline": []}
            detail_section = {
                "section_id": f"{screen_id}_detail",
                "type": "detail",
                "title": record["title"],
                "body": record["summary"],
                "fields": [{"label": key.replace("_", " ").title(), "value": value} for key, value in record["payload"].items()],
            }
            timeline_section = {
                "section_id": f"{screen_id}_timeline",
                "type": "timeline",
                "items": record["timeline"],
            }
            if layout_variant == "minimal":
                return [detail_section]
            if layout_variant in {"stream", "magazine"}:
                return [timeline_section, detail_section]
            if ui_variant == "editorial":
                return [timeline_section, detail_section]
            return [detail_section, timeline_section]

        if screen_id.endswith("profile"):
            return [
                {
                    "section_id": f"{screen_id}_profile",
                    "type": "profile",
                    "body": f"Update profile data and keep it consistent across {flow_label} runtime actions.",
                }
            ]

        if kind == "success":
            return [
                {
                    "section_id": "client_success_message",
                    "type": "hero",
                    "title": f"{entity_title} created",
                    "body": (
                        f"The {flow_label} is now visible in specialist and manager workflows."
                        if layout_variant != "magazine" and ui_variant != "atlas"
                        else f"Your {flow_label} has been registered and routed through the pipeline."
                    ),
                }
            ]

        return []

    def _freeform_role_sections(
        self,
        role: str,
        entity: DomainEntity,
        records: list[dict[str, Any]],
        prompt_goal: str,
        ui_variant: str,
        layout_variant: str,
    ) -> list[dict[str, Any]]:
        flow_label = self._flow_label(prompt_goal, entity)
        entity_title = self._entity_title(entity)
        if self._is_commerce_prompt(prompt_goal) or "order" in flow_label:
            return self._commerce_role_sections(role, records)
        intro = {
            "section_id": f"{role}_intro",
            "type": "heading",
            "title": self._title_for_role(role, flow_label, ui_variant),
            "body": self._role_body(role, flow_label, ui_variant),
        }
        stats = {
            "section_id": f"{role}_stats",
            "type": "stats",
            "items": self._role_stats(role, records, ui_variant),
        }
        recent_items = [
            {
                "item_id": record["record_id"],
                "title": record["title"],
                "subtitle": record["summary"],
                "status": record["status"],
                "meta": record.get("priority"),
            }
            for record in records[:4]
        ]
        recent_list = {
            "section_id": f"{role}_recent",
            "type": "list",
            "items": recent_items,
        }
        timeline = {
            "section_id": f"{role}_timeline",
            "type": "timeline",
            "items": self._role_timeline(role, flow_label, records),
        }
        summary = {
            "section_id": f"{role}_summary",
            "type": "detail",
            "title": "What this role handles",
            "body": f"This workspace is generated from the current prompt and adapts to {role} responsibilities.",
            "fields": self._role_summary_fields(role, flow_label, records),
        }

        if role == "client":
            form_fields = [
                {
                    "field_id": attribute.name,
                    "name": attribute.name,
                    "label": attribute.name.replace("_", " ").title(),
                    "field_type": "textarea" if attribute.type == "textarea" else "text",
                    "required": attribute.required,
                    "placeholder": f"Enter {attribute.name.replace('_', ' ')}",
                }
                for attribute in entity.attributes
            ]
            if not form_fields:
                form_fields = [
                    {"field_id": "order_name", "name": "order_name", "label": f"{entity_title} name", "field_type": "text", "required": True, "placeholder": f"Enter {entity_title.lower()} name"},
                    {"field_id": "details", "name": "details", "label": "Details", "field_type": "textarea", "required": False, "placeholder": "Add important details"},
                ]
            return [
                intro,
                {
                    "section_id": "client_order_form",
                    "type": "form",
                    "fields": form_fields,
                },
                {
                    "section_id": "client_actions",
                    "type": "actions",
                    "actions": [
                        {"action_id": "client_submit_request", "label": "Submit order", "type": "submit_form"},
                    ],
                },
                recent_list,
                summary,
            ]

        if role == "specialist":
            return [
                intro,
                stats,
                recent_list,
                timeline,
                {
                    "section_id": "specialist_actions",
                    "type": "actions",
                    "actions": [
                        {"action_id": "specialist_claim_next", "label": "Claim next", "type": "call_api"},
                        {"action_id": "specialist_mark_in_progress", "label": "Start processing", "type": "call_api"},
                        {"action_id": "specialist_complete_request", "label": "Mark complete", "type": "call_api"},
                    ],
                },
            ]

        return [
            intro,
            stats,
            timeline,
            summary,
            {
                "section_id": "manager_actions",
                "type": "actions",
                "actions": [
                    {"action_id": "manager_rebalance", "label": "Rebalance workload", "type": "call_api"},
                    {"action_id": "manager_refresh_records", "label": "Refresh records", "type": "call_api"},
                ],
            },
            recent_list,
        ]

    def _commerce_role_sections(self, role: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts = Counter(record["status"] for record in records)
        product_cards = [
            {
                "item_id": "product_1",
                "title": "Essential Hoodie",
                "subtitle": "Soft everyday hoodie with neutral fit and quick shipping.",
                "status": "in_stock",
                "meta": "$64",
            },
            {
                "item_id": "product_2",
                "title": "Canvas Backpack",
                "subtitle": "Compact daily backpack for office and short travel.",
                "status": "popular",
                "meta": "$79",
            },
            {
                "item_id": "product_3",
                "title": "Minimal Sneakers",
                "subtitle": "Low-profile sneakers with lightweight sole and clean finish.",
                "status": "new",
                "meta": "$118",
            },
        ]
        order_items = [
            {
                "item_id": record["record_id"],
                "title": record["title"],
                "subtitle": record["summary"],
                "status": record["status"],
                "meta": record.get("priority"),
            }
            for record in records[:4]
        ]

        if role == "client":
            return [
                {
                    "section_id": "client_catalog",
                    "type": "list",
                    "title": "Popular products",
                    "items": product_cards,
                },
                {
                    "section_id": "client_checkout",
                    "type": "form",
                    "title": "Quick checkout",
                    "fields": [
                        {"field_id": "customer_name", "name": "customer_name", "label": "Full name", "field_type": "text", "required": True, "placeholder": "Ivan Ivanov"},
                        {"field_id": "phone", "name": "phone", "label": "Phone", "field_type": "text", "required": True, "placeholder": "+43 660 123 4567"},
                        {"field_id": "product_name", "name": "product_name", "label": "Product", "field_type": "text", "required": True, "placeholder": "Essential Hoodie"},
                        {"field_id": "quantity", "name": "quantity", "label": "Quantity", "field_type": "text", "required": True, "placeholder": "1"},
                        {"field_id": "delivery_address", "name": "delivery_address", "label": "Delivery address", "field_type": "textarea", "required": False, "placeholder": "Street, city, ZIP"},
                        {"field_id": "comment", "name": "comment", "label": "Comment", "field_type": "textarea", "required": False, "placeholder": "Delivery note or question"},
                    ],
                },
                {
                    "section_id": "client_checkout_actions",
                    "type": "actions",
                    "actions": [
                        {"action_id": "client_submit_request", "label": "Place order", "type": "submit_form"},
                    ],
                },
                {
                    "section_id": "client_recent_orders",
                    "type": "list",
                    "title": "My orders",
                    "items": order_items,
                },
            ]

        if role == "specialist":
            return [
                {
                    "section_id": "specialist_queue_stats",
                    "type": "stats",
                    "items": [
                        {"label": "New orders", "value": str(counts.get("new", 0))},
                        {"label": "Packing", "value": str(counts.get("in_progress", 0))},
                    ],
                },
                {
                    "section_id": "specialist_orders",
                    "type": "list",
                    "title": "Orders to process",
                    "items": order_items,
                },
                {
                    "section_id": "specialist_actions",
                    "type": "actions",
                    "actions": [
                        {"action_id": "specialist_claim_next", "label": "Take next order", "type": "call_api"},
                        {"action_id": "specialist_mark_in_progress", "label": "Mark packing", "type": "call_api"},
                        {"action_id": "specialist_complete_request", "label": "Ready for pickup", "type": "call_api"},
                    ],
                },
                {
                    "section_id": "specialist_timeline",
                    "type": "timeline",
                    "items": self._role_timeline(role, "order management", records),
                },
            ]

        return [
            {
                "section_id": "manager_store_stats",
                "type": "stats",
                "items": [
                    {"label": "Created", "value": str(counts.get("new", 0) + counts.get("in_progress", 0) + counts.get("completed", 0))},
                    {"label": "Completed", "value": str(counts.get("completed", 0))},
                ],
            },
            {
                "section_id": "manager_problem_orders",
                "type": "detail",
                "title": "Store overview",
                "body": "Track live order flow, staff workload, and the cases that need intervention.",
                "fields": [
                    {"label": "In progress", "value": str(counts.get("in_progress", 0))},
                    {"label": "Need attention", "value": str(sum(1 for record in records if record.get("owner") == "unassigned"))},
                ],
            },
            {
                "section_id": "manager_orders",
                "type": "list",
                "title": "Recent orders",
                "items": order_items,
            },
            {
                "section_id": "manager_actions",
                "type": "actions",
                "actions": [
                    {"action_id": "manager_rebalance", "label": "Reassign workload", "type": "call_api"},
                    {"action_id": "manager_refresh_records", "label": "Refresh orders", "type": "call_api"},
                ],
            },
        ]

    def _manifest_action(
        self,
        action: Action,
        route_lookup: dict[str, RouteDefinition],
        ui_variant: str,
        flow_label: str,
    ) -> dict[str, Any]:
        payload = action.model_dump(mode="json")
        payload["label"] = self._action_label(action.action_id, ui_variant, flow_label)
        if action.target_screen_id and action.target_screen_id in route_lookup:
            payload["target_path"] = route_lookup[action.target_screen_id].path
        return payload

    def _navigation_items(self, role: str, flow_label: str, ui_variant: str, layout_variant: str) -> list[dict[str, str]]:
        def reorder(items: list[dict[str, str]], order: list[str]) -> list[dict[str, str]]:
            item_map = {item["path"]: item for item in items}
            return [item_map[path] for path in order if path in item_map]

        base_items = {
            "client": [
                {"label": "Home", "path": "/"},
                {"label": "Create", "path": "/book"},
                {"label": "Requests", "path": "/requests"},
                {"label": "Profile", "path": "/profile"},
            ],
            "specialist": [
                {"label": "Home", "path": "/"},
                {"label": "Queue", "path": "/queue"},
                {"label": "Profile", "path": "/profile"},
            ],
            "manager": [
                {"label": "Home", "path": "/"},
                {"label": "Dashboard", "path": "/dashboard"},
                {"label": "Records", "path": "/records"},
                {"label": "Profile", "path": "/profile"},
            ],
        }
        selected: list[dict[str, str]]
        if ui_variant == "atlas":
            atlas = {
                "client": [
                    {"label": "Overview", "path": "/"},
                    {"label": "Intake", "path": "/book"},
                    {"label": "Pipeline", "path": "/requests"},
                    {"label": "Profile", "path": "/profile"},
                ],
                "specialist": [
                    {"label": "Ops", "path": "/"},
                    {"label": "Backlog", "path": "/queue"},
                    {"label": "Profile", "path": "/profile"},
                ],
                "manager": [
                    {"label": "Command", "path": "/"},
                    {"label": "Insights", "path": "/dashboard"},
                    {"label": "Records", "path": "/records"},
                    {"label": "Profile", "path": "/profile"},
                ],
            }
            selected = atlas[role]
        elif ui_variant == "editorial":
            editorial = {
                "client": [
                    {"label": "Start", "path": "/"},
                    {"label": "New", "path": "/book"},
                    {"label": "History", "path": "/requests"},
                    {"label": "Me", "path": "/profile"},
                ],
                "specialist": [
                    {"label": "Start", "path": "/"},
                    {"label": "Work", "path": "/queue"},
                    {"label": "Me", "path": "/profile"},
                ],
                "manager": [
                    {"label": "Start", "path": "/"},
                    {"label": "Control", "path": "/dashboard"},
                    {"label": "Audit", "path": "/records"},
                    {"label": "Me", "path": "/profile"},
                ],
            }
            selected = editorial[role]
        elif ui_variant == "pulse":
            main_token = flow_label.split()[0].title() if flow_label else "Flow"
            pulse = {
                "client": [
                    {"label": "Now", "path": "/"},
                    {"label": f"New {main_token}", "path": "/book"},
                    {"label": "Track", "path": "/requests"},
                    {"label": "Profile", "path": "/profile"},
                ],
                "specialist": [
                    {"label": "Now", "path": "/"},
                    {"label": "Queue", "path": "/queue"},
                    {"label": "Profile", "path": "/profile"},
                ],
                "manager": [
                    {"label": "Now", "path": "/"},
                    {"label": "Board", "path": "/dashboard"},
                    {"label": "Records", "path": "/records"},
                    {"label": "Profile", "path": "/profile"},
                ],
            }
            selected = pulse[role]
        else:
            selected = base_items[role]

        if layout_variant == "dashboard":
            order = {
                "client": ["/requests", "/book", "/", "/profile"],
                "specialist": ["/queue", "/", "/profile"],
                "manager": ["/dashboard", "/records", "/", "/profile"],
            }
            return reorder(selected, order[role])

        if layout_variant == "stream":
            order = {
                "client": ["/book", "/requests", "/", "/profile"],
                "specialist": ["/queue", "/", "/profile"],
                "manager": ["/records", "/dashboard", "/", "/profile"],
            }
            return reorder(selected, order[role])

        if layout_variant == "minimal":
            order = {
                "client": ["/", "/book", "/profile"],
                "specialist": ["/", "/queue"],
                "manager": ["/", "/dashboard"],
            }
            return reorder(selected, order[role])

        if layout_variant == "magazine":
            order = {
                "client": ["/", "/requests", "/book", "/profile"],
                "specialist": ["/", "/profile", "/queue"],
                "manager": ["/", "/records", "/dashboard", "/profile"],
            }
            return reorder(selected, order[role])

        return selected

    def _role_stats(self, role: str, records: list[dict[str, Any]], ui_variant: str) -> list[dict[str, str]]:
        counts = Counter(record["status"] for record in records)
        if ui_variant == "editorial":
            client_labels = ("Cases", "Pending")
            specialist_labels = ("Intake", "In motion")
            manager_labels = ("Closed", "Unassigned")
        elif ui_variant == "atlas":
            client_labels = ("Requests", "Open")
            specialist_labels = ("Backlog", "Active")
            manager_labels = ("Delivered", "Unowned")
        elif ui_variant == "pulse":
            client_labels = ("Flow", "Hot")
            specialist_labels = ("Queue", "Running")
            manager_labels = ("Done", "Unassigned")
        else:
            client_labels = ("Requests", "Awaiting response")
            specialist_labels = ("New requests", "In review")
            manager_labels = ("Completed", "Unassigned")

        if role == "client":
            return [
                {"label": client_labels[0], "value": str(len(records))},
                {"label": client_labels[1], "value": str(counts.get("new", 0) + counts.get("in_progress", 0))},
            ]
        if role == "specialist":
            return [
                {"label": specialist_labels[0], "value": str(counts.get("new", 0))},
                {"label": specialist_labels[1], "value": str(counts.get("in_progress", 0))},
            ]
        return [
            {"label": manager_labels[0], "value": str(counts.get("completed", 0))},
            {"label": manager_labels[1], "value": str(sum(1 for record in records if record.get("owner") == "unassigned"))},
        ]

    def _role_summary_fields(self, role: str, flow_label: str, records: list[dict[str, Any]]) -> list[dict[str, str]]:
        counts = Counter(record["status"] for record in records)
        if "order" in flow_label:
            summaries = {
                "client": [
                    {"label": "What you can do", "value": "Browse products, review past orders, and follow delivery or packing updates."},
                    {"label": "Current focus", "value": f"{counts.get('new', 0) + counts.get('in_progress', 0)} orders still moving through the store workflow."},
                ],
                "specialist": [
                    {"label": "What you can do", "value": "Pick incoming orders, update fulfillment state, and flag stock or delivery issues."},
                    {"label": "Current focus", "value": f"{counts.get('new', 0)} new orders are waiting for action right now."},
                ],
                "manager": [
                    {"label": "What you can do", "value": "See throughput, delays, ownership gaps, and operational pressure across the store."},
                    {"label": "Current focus", "value": f"{counts.get('completed', 0)} orders are already closed and {counts.get('in_progress', 0)} are still active."},
                ],
            }
            return summaries[role]
        return [
            {"label": "Role scope", "value": self._title_for_role(role, flow_label, "studio")},
            {"label": "Open items", "value": str(counts.get("new", 0) + counts.get("in_progress", 0))},
        ]

    def _role_timeline(self, role: str, flow_label: str, records: list[dict[str, Any]]) -> list[dict[str, str]]:
        if records:
            base = records[0]
            timeline = base.get("timeline")
            if isinstance(timeline, list) and timeline:
                return timeline
        if "order" in flow_label:
            fallbacks = {
                "client": [
                    {"label": "Cart", "value": "Customer has selected items and is close to checkout."},
                    {"label": "Order", "value": "Payment and confirmation are waiting to be finalized."},
                ],
                "specialist": [
                    {"label": "Queue", "value": "New orders are waiting to be picked and packed."},
                    {"label": "Fulfillment", "value": "Problematic orders should be marked before handoff."},
                ],
                "manager": [
                    {"label": "Monitoring", "value": "Track slowdowns and handoff quality across the store."},
                    {"label": "Decision", "value": "Reassign work when the queue becomes uneven."},
                ],
            }
            return fallbacks[role]
        return [
            {"label": "Stage 1", "value": f"{flow_label.title()} was created."},
            {"label": "Stage 2", "value": f"{flow_label.title()} is moving through the workflow."},
        ]

    def _title_for_role(self, role: str, flow_label: str, ui_variant: str) -> str:
        if "order" in flow_label:
            commerce_titles = {
                "client": "Orders and recent activity",
                "specialist": "Fulfillment and handoff",
                "manager": "Operations snapshot",
            }
            return commerce_titles[role]
        presets = {
            "studio": {
                "client": f"Plan and track {flow_label}",
                "specialist": f"Execute {flow_label} workload",
                "manager": f"Control {flow_label} pipeline",
            },
            "atlas": {
                "client": f"{flow_label.title()} intake desk",
                "specialist": f"{flow_label.title()} execution board",
                "manager": f"{flow_label.title()} command view",
            },
            "pulse": {
                "client": f"{flow_label.title()} live feed",
                "specialist": f"{flow_label.title()} active queue",
                "manager": f"{flow_label.title()} health monitor",
            },
            "editorial": {
                "client": f"Your {flow_label} journal",
                "specialist": f"{flow_label.title()} work journal",
                "manager": f"{flow_label.title()} executive journal",
            },
        }
        return presets.get(ui_variant, presets["studio"])[role]

    def _role_body(self, role: str, flow_label: str, ui_variant: str) -> str:
        if "order" in flow_label:
            commerce_bodies = {
                "client": "Browse products, place orders, and follow status changes without extra template noise.",
                "specialist": "Review new orders, assemble them, update statuses, and resolve delivery or stock issues.",
                "manager": "Monitor order flow, workload, bottlenecks, and everything that needs attention across the store.",
            }
            return commerce_bodies[role]
        presets = {
            "studio": {
                "client": f"Create new {flow_label} entries, monitor progress, and keep core details accurate.",
                "specialist": f"Claim incoming {flow_label} work items, execute processing, and progress lifecycle states.",
                "manager": f"Observe the full {flow_label} pipeline, manage workload distribution, and resolve bottlenecks.",
            },
            "atlas": {
                "client": f"Start {flow_label} cases with full context and follow each step through completion.",
                "specialist": f"Prioritize queue items and process {flow_label} operations with clear ownership.",
                "manager": f"Track throughput, find bottlenecks, and steer {flow_label} execution across teams.",
            },
            "pulse": {
                "client": f"Create and track {flow_label} in a fast live workflow.",
                "specialist": f"Process incoming {flow_label} tasks and keep momentum.",
                "manager": f"Watch {flow_label} pulse, SLA pressure, and assignment load in real time.",
            },
            "editorial": {
                "client": f"Capture requests, context, and progress notes for each {flow_label}.",
                "specialist": f"Maintain a clear working narrative while processing {flow_label} records.",
                "manager": f"Review the operational story of {flow_label} and intervene where needed.",
            },
        }
        return presets.get(ui_variant, presets["studio"])[role]

    @staticmethod
    def _action_label(action_id: str, ui_variant: str, flow_label: str) -> str:
        labels = {
            "client_open_form": "New order",
            "client_open_requests": "Orders",
            "client_submit_request": "Submit request",
            "client_open_request_detail": "Order details",
            "client_open_form_inline": "Another order",
            "client_success_to_requests": "View orders",
            "client_success_to_home": "Back",
            "client_profile_save": "Save",
            "specialist_open_queue": "Queue",
            "specialist_claim_next": "Claim next",
            "specialist_open_detail": "Details",
            "specialist_mark_in_progress": "Start",
            "specialist_complete_request": "Complete",
            "specialist_profile_save": "Save",
            "manager_open_dashboard": "Dashboard",
            "manager_open_records": "Records",
            "manager_rebalance": "Rebalance",
            "manager_refresh_records": "Refresh",
            "manager_profile_save": "Save",
        }
        if ui_variant == "atlas":
            labels.update(
                {
                    "client_open_form": "Open intake",
                    "client_open_requests": "Open pipeline",
                    "specialist_open_queue": "Open backlog",
                    "manager_open_dashboard": "Open insights",
                }
            )
        if ui_variant == "editorial":
            labels.update(
                {
                    "client_open_form": f"Write new {flow_label}",
                    "client_open_requests": "Open history",
                    "specialist_open_queue": "Open worklist",
                    "manager_open_records": "Open audit trail",
                }
            )
        if ui_variant == "pulse":
            labels.update(
                {
                    "client_open_form": "Start now",
                    "specialist_claim_next": "Claim next live",
                    "manager_rebalance": "Balance live load",
                }
            )
        return labels.get(action_id, action_id.replace("_", " ").replace("api", "API").title())

    def _flow_label(self, prompt: str, entity: DomainEntity) -> str:
        lowered = prompt.lower()
        if self._is_commerce_prompt(prompt):
            return "order management"
        if "consultation" in lowered and "booking" in lowered:
            return "consultation booking"
        if "booking" in lowered:
            return "booking"
        if "appointment" in lowered:
            return "appointment"
        if "request" in lowered:
            return "request"
        return entity.name.replace("_", " ").lower()

    @staticmethod
    def _entity_plural(entity: DomainEntity) -> str:
        base = GenerationService._entity_title(entity)
        if base.endswith("y"):
            return f"{base[:-1]}ies"
        if base.endswith("s"):
            return base
        return f"{base}s"

    @staticmethod
    def _entity_title(entity: DomainEntity) -> str:
        value = entity.name.replace("_", " ")
        value = re.sub(r"(?<!^)(?=[A-Z])", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value.title()

    def _expand_role_actors(self, actors: list[Actor], doc_refs: list[Any]) -> list[Actor]:
        actor_map = {actor.actor_id: actor for actor in actors}
        role_names = {actor.role.lower() for actor in actors}
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Expanded to preserve linked multi-role workflow in the canonical runtime.")]
        if "specialist" not in role_names:
            actor_map["actor_specialist"] = Actor(
                actor_id="actor_specialist",
                name="Specialist",
                role="specialist",
                description="Processes incoming requests created by end-users and updates workflow status.",
                permissions_hint=["process_request"],
                evidence=evidence,
            )
        if "manager" not in role_names:
            actor_map["actor_manager"] = Actor(
                actor_id="actor_manager",
                name="Manager",
                role="manager",
                description="Monitors pipeline health, workload distribution, and operational outcomes.",
                permissions_hint=["control_dashboard"],
                evidence=evidence,
            )
        if "client" not in role_names and "user" not in role_names:
            actor_map["actor_client"] = Actor(
                actor_id="actor_client",
                name="Client",
                role="client",
                description="Creates a new request and tracks its progress.",
                permissions_hint=["create_request"],
                evidence=evidence,
            )
        return list(actor_map.values())

    def _expand_role_flows(self, spec: GroundedSpecModel, actors: list[Actor]) -> list[UserFlow]:
        existing = list(spec.user_flows)
        flow_names = {flow.name.lower() for flow in existing}
        actor_by_role = {actor.role.lower(): actor for actor in actors}
        actor_by_role.setdefault("client", next((actor for actor in actors if actor.role.lower() == "user"), actors[0]))
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Expanded to linked three-role runtime flow.")]
        entity_name = spec.domain_entities[0].name.replace("_", " ") if spec.domain_entities else "request"
        flow_label = entity_name.lower()

        if not any("submission" in name or "booking" in name or "request" in name for name in flow_names):
            existing.insert(
                0,
                UserFlow(
                    flow_id="flow_client_submission",
                    name=f"Client {flow_label} submission",
                    goal=f"Allow a client to submit a new {flow_label} and receive confirmation.",
                    steps=[
                        FlowStep(step_id="step_client_open_form", order=1, actor_id=actor_by_role["client"].actor_id, action="Open the submission form."),
                        FlowStep(step_id="step_client_fill_form", order=2, actor_id=actor_by_role["client"].actor_id, action="Fill in the requested fields.", input_data=[attribute.name for attribute in spec.domain_entities[0].attributes]),
                        FlowStep(step_id="step_client_submit_form", order=3, actor_id=actor_by_role["client"].actor_id, action="Submit the form to create a new record.", output_data=["submission_id", "status"]),
                    ],
                    acceptance_criteria=["A new record is created.", "The client sees a confirmation state."],
                    evidence=evidence,
                ),
            )

        if "specialist" in actor_by_role and not any("specialist" in name or "queue" in name for name in flow_names):
            existing.append(
                UserFlow(
                    flow_id="flow_specialist_processing",
                    name=f"Specialist processes {flow_label}",
                    goal=f"Let a specialist review and process incoming {flow_label} records.",
                    steps=[
                        FlowStep(step_id="step_specialist_open_queue", order=1, actor_id=actor_by_role["specialist"].actor_id, action="Open the incoming queue."),
                        FlowStep(step_id="step_specialist_claim_item", order=2, actor_id=actor_by_role["specialist"].actor_id, action="Claim the next unassigned record.", output_data=["owner"]),
                        FlowStep(step_id="step_specialist_update_status", order=3, actor_id=actor_by_role["specialist"].actor_id, action="Move the record through in-progress and completed states.", output_data=["status"]),
                    ],
                    acceptance_criteria=["The specialist can see incoming records.", "The specialist can update processing status."],
                    evidence=evidence,
                )
            )

        if "manager" in actor_by_role and not any("manager" in name or "dashboard" in name or "oversight" in name for name in flow_names):
            existing.append(
                UserFlow(
                    flow_id="flow_manager_oversight",
                    name=f"Manager oversees {flow_label} pipeline",
                    goal=f"Allow a manager to monitor the {flow_label} pipeline and intervene when necessary.",
                    steps=[
                        FlowStep(step_id="step_manager_open_dashboard", order=1, actor_id=actor_by_role["manager"].actor_id, action="Open the dashboard with aggregate metrics."),
                        FlowStep(step_id="step_manager_review_records", order=2, actor_id=actor_by_role["manager"].actor_id, action="Review records by status, owner, and completion stage."),
                        FlowStep(step_id="step_manager_rebalance", order=3, actor_id=actor_by_role["manager"].actor_id, action="Trigger balancing or refresh actions when workload distribution requires it."),
                    ],
                    acceptance_criteria=["The manager sees pipeline metrics.", "The manager can inspect and refresh operational records."],
                    evidence=evidence,
                )
            )

        return existing

    def _ensure_role_expansion_assumption(
        self,
        spec: GroundedSpecModel,
        assumptions: list[Assumption],
        actors: list[Actor],
    ) -> list[Assumption]:
        role_names = {actor.role.lower() for actor in actors}
        if {"client", "specialist", "manager"}.issubset(role_names) and not any(
            assumption.assumption_id == "assume_role_expansion" for assumption in assumptions
        ):
            assumptions.append(
                Assumption(
                    assumption_id="assume_role_expansion",
                    text="Single-role prompts are expanded into a linked client-specialist-manager workflow.",
                    status="active",
                    rationale="The platform should produce a complete multi-role mini-app even when the prompt describes only the end-user entry point.",
                    impact="medium",
                )
            )
        return assumptions

    @staticmethod
    def _screen_is_generic(title: str, subtitle: str | None) -> bool:
        lowered = f"{title} {subtitle or ''}".lower()
        return any(marker in lowered for marker in ("workspace", "control center", "control room"))

    def _build_summary(
        self,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        artifact_plan: ArtifactPlanModel,
        generation_mode: GenerationMode,
    ) -> str:
        compile_summary = self._compile_summary(ir)
        return (
            f"Built a {generation_mode.value} grounded mini-app for {spec.target_platform} with "
            f"{compile_summary['screen_count']} screens, {compile_summary['route_count']} routes, "
            f"and {compile_summary['action_count']} actions across three roles. "
            f"Applied {len(artifact_plan.operations)} template patches and preserved {len(spec.doc_refs)} source references."
        )

    def _compile_summary(self, ir: AppIRModel) -> dict[str, int | str]:
        return {
            "screen_count": len(ir.screens),
            "route_count": sum(len(group.routes) for group in ir.route_groups),
            "action_count": sum(len(screen.actions) for screen in ir.screens),
            "role_count": len(ir.route_groups),
        }

    def _block_job(
        self,
        job: JobRecord,
        validation_result: GroundedSpecValidatorResult | AppIRValidatorResult,
        assumptions: list[Any],
        *,
        failure_reason: str,
    ) -> None:
        job.status = "blocked"
        job.fidelity = "blocked"
        job.failure_reason = failure_reason
        if validation_result.issues:
            primary_issue = validation_result.issues[0]
            job.failure_class = job.failure_class or primary_issue.code
            job.root_cause_summary = job.root_cause_summary or primary_issue.message
            job.fix_targets = sorted(
                {
                    issue.location
                    for issue in validation_result.issues
                    if issue.location and issue.location not in {"generation", "preview"}
                }
            )
            job.handoff_from_failed_generate = job.handoff_from_failed_generate or self._build_fix_handoff(
                prompt=job.prompt,
                failure_reason=failure_reason,
                failure_class=job.failure_class,
                issues=validation_result.issues,
                mode=job.mode,
            )
        job.assumptions_report = [item.model_dump(mode="json") for item in assumptions]
        job.validation_snapshot = ValidationSnapshot(
            grounded_spec_valid=isinstance(validation_result, GroundedSpecValidatorResult) and validation_result.valid,
            app_ir_valid=isinstance(validation_result, AppIRValidatorResult) and validation_result.valid,
            build_valid=False,
            blocking=validation_result.blocking,
            issues=[issue.model_dump(mode="json") for issue in validation_result.issues],
        )
        self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))

    def _block_with_messages(
        self,
        job: JobRecord,
        messages: list[str],
        *,
        code: str,
        event_type: str,
        failure_reason: str,
    ) -> JobRecord:
        job.status = "blocked"
        job.fidelity = "blocked"
        job.failure_reason = failure_reason
        job.failure_class = job.failure_class or code
        job.root_cause_summary = job.root_cause_summary or (messages[0] if messages else failure_reason)
        job.validation_snapshot = ValidationSnapshot(
            build_valid=False,
            blocking=True,
            issues=[
                ValidationIssue(
                    code=code,
                    message=message,
                    severity="critical",
                    location="generation",
                ).model_dump(mode="json")
                for message in messages
            ],
        )
        issues = [
            ValidationIssue(
                code=code,
                message=message,
                severity="critical",
                location="generation",
            )
            for message in messages
        ]
        job.handoff_from_failed_generate = job.handoff_from_failed_generate or self._build_fix_handoff(
            prompt=job.prompt,
            failure_reason=failure_reason,
            failure_class=job.failure_class,
            issues=issues,
            mode=job.mode,
        )
        self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, event_type, failure_reason)
        self._append_trace(
            job.workspace_id,
            "job_blocked",
            failure_reason,
            {"messages": messages, "code": code},
        )
        return job

    def _stop_if_requested(
        self,
        job: JobRecord,
        workspace_id: str,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord | None:
        if not should_stop or not should_stop():
            return None
        job.status = "blocked"
        job.fidelity = "blocked"
        job.failure_reason = "Run stopped by user."
        job.failure_class = "stopped_by_user"
        job.root_cause_summary = "Run stopped by user."
        job.validation_snapshot = ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=False,
            blocking=False,
            issues=[],
        )
        self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, "job_failed", "Run stopped by user.")
        self._append_trace(workspace_id, "job_stopped", "Run stopped by user.", {})
        return job

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)
        self._sync_run_progress(job, event_type, message)
        self.workspace_log_service.append(job.workspace_id, source=f"generation.{event_type}", message=message, payload=details or {})
        logger.info("job_event workspace_id=%s job_id=%s event=%s message=%s", job.workspace_id, job.job_id, event_type, message)

    def _save_job(self, job: JobRecord) -> None:
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def _store_report(self, key: str, payload: dict) -> None:
        self.store.upsert("reports", key, payload)

    def _clear_trace(self, workspace_id: str) -> None:
        self._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append(
            {
                "stage": stage,
                "message": message,
                "payload": payload or {},
                "created_at": utc_now().isoformat(),
            }
        )
        current["entries"] = entries
        self._store_report(report_key, current)
        self.workspace_log_service.append(workspace_id, source=f"generation.trace.{stage}", message=message, payload=payload or {})
        logger.info("trace workspace_id=%s stage=%s message=%s", workspace_id, stage, message)

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str) -> None:
        if not job.linked_run_id:
            return
        run_payload = self.store.get("runs", job.linked_run_id)
        if not run_payload:
            return
        stage, progress = self._run_progress_for_event(event_type)
        run_payload["linked_job_id"] = job.job_id
        run_payload["current_stage"] = stage
        run_payload["progress_percent"] = max(int(run_payload.get("progress_percent", 0)), progress)
        if job.llm_provider:
            run_payload["llm_provider"] = job.llm_provider
        if job.llm_model:
            run_payload["llm_model"] = job.llm_model
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", job.linked_run_id, run_payload)

    @staticmethod
    def _run_progress_for_event(event_type: str) -> tuple[str, int]:
        progress_map = {
            "job_started": ("starting", 4),
            "indexing_started": ("indexing workspace", 8),
            "retrieval_started": ("retrieving context", 12),
            "retrieval_completed": ("retrieval complete", 18),
            "spec_started": ("building grounded spec", 24),
            "spec_ready": ("grounded spec ready", 32),
            "draft_prepared": ("preparing draft workspace", 38),
            "role_contract_started": ("analyzing role boundaries", 44),
            "role_contract_ready": ("role boundaries ready", 50),
            "planning_started": ("planning code changes", 56),
            "planning_ready": ("code plan ready", 64),
            "context_pack_started": ("collecting file context", 68),
            "context_pack_ready": ("context pack ready", 74),
            "editing_started": ("generating draft edits", 78),
            "iteration_ready": ("draft edits prepared", 84),
            "repair_started": ("repairing after build failure", 86),
            "repair_iteration": ("repairing draft", 88),
            "build_started": ("running validation and build", 90),
            "checks_completed": ("checks complete", 93),
            "preview_rebuild_started": ("refreshing preview", 96),
            "preview_ready": ("preview ready", 98),
            "draft_ready": ("awaiting review", 99),
            "job_completed": ("almost complete", 99),
            "spec_blocked": ("blocked on spec", 100),
            "validation_failed": ("validation failed", 100),
            "job_failed": ("failed", 100),
        }
        return progress_map.get(event_type, ("processing", 12))

    def _grounded_spec_system_prompt(self) -> str:
        return (
            "You are generating a documentation-grounded multi-role mini-app specification. "
            "Prioritize architectural depth, domain modeling, and end-to-end workflow integrity. "
            "Allow creative variation in information architecture and product composition when multiple valid solutions exist. "
            "Use only information grounded in the supplied docs and prompt. "
            "Do not collapse the app into a single form if multi-role workflows are implied. "
            "Require explicit state lifecycle, role handoffs, operational control flow, and recoverable error paths. "
            "Prefer explicit assumptions and canonical template defaults over blocking unknowns for ordinary implementation details. "
            "Only emit high-impact unknowns when generation truly cannot proceed without user clarification. "
            "Avoid toy/demo framing and avoid repeating rigid template wording across runs. "
            "Honor the provided creative_direction while preserving schema validity and business realism. "
            "Keep the output strictly valid against the schema."
        )

    @staticmethod
    def _grounded_spec_section_system_prompt(section_title: str) -> str:
        return (
            "You are generating one section of a documentation-grounded multi-role mini-app specification. "
            f"Produce only the requested section: {section_title}. "
            "Keep the section concrete, implementation-oriented, and strictly valid against the schema. "
            "Do not repeat unrelated sections."
        )

    @staticmethod
    def _grounded_spec_outline_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product_goal": {"type": "string"},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "responsibility": {"type": "string"},
                            "primary_actions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["role", "responsibility", "primary_actions"],
                        "additionalProperties": False,
                    },
                },
                "entities": {"type": "array", "items": {"type": "string"}},
                "flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "goal": {"type": "string"},
                            "roles": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "goal", "roles"],
                        "additionalProperties": False,
                    },
                },
                "api_needs": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["product_goal", "roles", "entities", "flows", "api_needs", "risks"],
            "additionalProperties": False,
        }

    def _grounded_spec_outline_system_prompt(self) -> str:
        return (
            "You are doing the first pass for a grounded mini-app specification. "
            "Return only a compact outline: product goal, roles, entities, flows, API needs, and risks. "
            "Do not emit the full final schema yet. Keep it short, concrete, and implementation-oriented."
        )

    def _grounded_spec_outline_user_prompt(
        self,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec outline",
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "creative_direction": creative_direction,
                "architecture_contract": [
                    "Identify the real business domain and the selected roles.",
                    "Extract entities, user flows, and any obvious API needs.",
                    "Keep the output compact; this is only the outline pass.",
                ],
                "docs": self._compact_doc_refs(doc_refs, limit=2 if compact else 4),
                "creative_direction_summary": self._compact_creative_direction(creative_direction, compact=compact),
            }
        )

    def _grounded_spec_user_prompt(
        self,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec",
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "architecture_contract": [
                    "Model a concrete business domain with realistic entities and statuses.",
                    "Define clear role boundaries and cross-role handoff points.",
                    "Specify complete role flows; screen count and depth are flexible.",
                    "Include failure handling, validation rules, and operational monitoring expectations.",
                    "Avoid generic placeholders and repeated template wording.",
                ],
                "outline": outline,
                "creative_direction": self._compact_creative_direction(creative_direction, compact=compact),
                "variability_policy": [
                    "Use role requirements as capability constraints, not layout constraints.",
                    "You may pick any navigation and screen composition pattern that remains coherent.",
                    "Do not mirror the same information hierarchy across all roles.",
                ],
                "docs": self._compact_doc_refs(doc_refs, limit=3 if compact else 6),
            }
        )

    @staticmethod
    def _grounded_spec_partial_schema(field_names: list[str]) -> dict[str, Any]:
        full_schema = GroundedSpecModel.model_json_schema()
        properties = full_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {name: properties[name] for name in field_names if name in properties},
            "required": [name for name in field_names if name in properties],
            "additionalProperties": False,
            "$defs": full_schema.get("$defs", {}),
        }

    def _grounded_spec_section_user_prompt(
        self,
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec section",
                "section_id": section_id,
                "section_title": section_title,
                "required_fields": field_names,
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "outline": outline,
                "creative_direction": self._compact_creative_direction(creative_direction, compact=compact),
                "section_contract": [
                    "Return only the requested fields.",
                    "Keep entity, role, and API naming consistent with the outline.",
                    "Prefer concrete business details over placeholders.",
                    "Do not duplicate top-level fields that were not requested.",
                ],
                "docs": self._compact_doc_refs(doc_refs, limit=2 if compact else 4),
            }
        )

    def _app_ir_system_prompt(self) -> str:
        return (
            "You are generating a quality-first AppIR for a multi-page, role-aware mini-app. "
            "Prefer explicit route groups, multiple screens per role, real navigation, and stateful actions. "
            "Ensure architecture coherence: domain lifecycle, role transitions, actionable dashboards, and resilient error handling. "
            "When possible, vary structure and composition between equally valid solutions instead of repeating one rigid pattern. "
            "Do not emit shallow mirrored role screens; each role must have distinct operational value. "
            "Honor the provided creative_direction and variability_policy while preserving schema validity. "
            "Keep the output strictly valid against the schema."
        )

    @staticmethod
    def _app_ir_section_system_prompt(section_title: str) -> str:
        return (
            "You are generating one section of a quality-first AppIR for a role-aware mini-app. "
            f"Return only the requested section: {section_title}. "
            "Keep the output strictly valid against the schema and consistent with the grounded spec and scenario graph."
        )

    def _app_ir_user_prompt(self, spec: GroundedSpecModel, scenario_graph: dict[str, Any], creative_direction: dict[str, Any]) -> str:
        return json_dumps(
            {
                "task": "Build AppIR",
                "grounded_spec": spec.model_dump(mode="json"),
                "scenario_graph": scenario_graph,
                "creative_direction": creative_direction,
                "delivery_contract": [
                    "Generate role-differentiated flows with meaningful actions and state transitions.",
                    "Include realistic list/detail/form/dashboard patterns where appropriate.",
                    "Preserve traceability between prompt intent, routes, actions, and integrations.",
                    "Avoid generic labels and placeholder-only screens.",
                    "Do not force fixed route/screen patterns when alternative architectures are equally valid.",
                ],
            }
        )

    @staticmethod
    def _app_ir_partial_schema(field_names: list[str]) -> dict[str, Any]:
        full_schema = AppIRModel.model_json_schema()
        properties = full_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {name: properties[name] for name in field_names if name in properties},
            "required": [name for name in field_names if name in properties],
            "additionalProperties": False,
            "$defs": full_schema.get("$defs", {}),
        }

    def _app_ir_section_user_prompt(
        self,
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        creative_direction: dict[str, Any],
    ) -> str:
        return json_dumps(
            {
                "task": "Build AppIR section",
                "section_id": section_id,
                "section_title": section_title,
                "required_fields": field_names,
                "grounded_spec": spec.model_dump(mode="json"),
                "scenario_graph": scenario_graph,
                "creative_direction": creative_direction,
                "delivery_contract": [
                    "Return only the requested fields.",
                    "Keep ids, routes, screen references, and integration names internally consistent.",
                    "Prefer realistic multi-screen role behavior over mirrored placeholders.",
                    "Do not emit unrelated top-level AppIR fields.",
                ],
            }
        )

    @staticmethod
    def _compact_doc_refs(doc_refs: list[Any], limit: int = 6) -> list[dict[str, Any]]:
        compact_refs: list[dict[str, Any]] = []
        for item in doc_refs[:limit]:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            if not isinstance(raw, dict):
                continue
            compact_refs.append(
                {
                    "doc_ref_id": raw.get("doc_ref_id"),
                    "source_type": raw.get("source_type"),
                    "file_path": raw.get("file_path"),
                    "section_title": raw.get("section_title"),
                    "relevance": raw.get("relevance"),
                    "snippet": str(raw.get("snippet") or "")[:180],
                }
            )
        return compact_refs

    @staticmethod
    def _compact_creative_direction(creative_direction: dict[str, Any], *, compact: bool) -> dict[str, Any]:
        if not compact:
            return creative_direction
        return {
            "name": creative_direction.get("name"),
            "focus": creative_direction.get("focus"),
            "layout_bias": creative_direction.get("layout_bias"),
            "interaction_bias": creative_direction.get("interaction_bias"),
        }

    @staticmethod
    def _app_ir_critique_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "should_repair": {"type": "boolean"},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                            "message": {"type": "string"},
                            "scope": {"type": "string", "enum": ["global", "route", "screen", "action", "integration"]},
                        },
                        "required": ["code", "severity", "message", "scope"],
                        "additionalProperties": False,
                    },
                },
                "repair_instructions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "should_repair", "issues", "repair_instructions"],
            "additionalProperties": False,
        }

    @staticmethod
    def _app_ir_critique_system_prompt() -> str:
        return (
            "You are a strict AppIR reviewer. "
            "Evaluate the candidate IR for business completeness, role differentiation, navigation coherence, "
            "action validity, and resilience states (loading/empty/error/success). "
            "Return concise structured findings only."
        )

    def _app_ir_critique_user_prompt(
        self,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        ir: AppIRModel,
        creative_direction: dict[str, Any],
    ) -> str:
        return json_dumps(
            {
                "task": "Critique AppIR",
                "grounded_spec": spec.model_dump(mode="json"),
                "scenario_graph": scenario_graph,
                "candidate_ir": ir.model_dump(mode="json"),
                "creative_direction": creative_direction,
                "acceptance_policy": [
                    "Roles must not be mirrored clones.",
                    "Flows must be end-to-end and actionable.",
                    "Critical/high issues imply should_repair=true.",
                ],
            }
        )

    @staticmethod
    def _app_ir_repair_system_prompt() -> str:
        return (
            "You repair AppIR based on structured critique findings. "
            "Keep valid parts, fix only what is needed, preserve coherence, and output strict schema-valid AppIR."
        )

    def _app_ir_repair_user_prompt(
        self,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        current_ir: AppIRModel,
        critique: dict[str, Any],
        creative_direction: dict[str, Any],
    ) -> str:
        return json_dumps(
            {
                "task": "Repair AppIR",
                "grounded_spec": spec.model_dump(mode="json"),
                "scenario_graph": scenario_graph,
                "current_ir": current_ir.model_dump(mode="json"),
                "critique": critique,
                "creative_direction": creative_direction,
                "repair_contract": [
                    "Address critical/high issues first.",
                    "Preserve existing valid routes/screens/actions where possible.",
                    "Do not reduce role coverage.",
                ],
            }
        )

    @staticmethod
    def _component_type(field_type: str) -> str:
        return {
            "phone": "phone_input",
            "email": "email_input",
            "text": "textarea",
            "date": "date_picker",
        }.get(field_type, "input")

    @staticmethod
    def _component_validators(attribute: EntityAttribute) -> list[ValidatorRule]:
        validators: list[ValidatorRule] = []
        if attribute.required:
            validators.append(ValidatorRule(rule_type="required", message=f"{attribute.name} is required."))
        if attribute.type == "email":
            validators.append(ValidatorRule(rule_type="email", message="Provide a valid email."))
        if attribute.type == "phone":
            validators.append(ValidatorRule(rule_type="phone", message="Provide a valid phone number."))
        return validators

    @staticmethod
    def _infer_entity_name(prompt: str) -> str:
        lowered = prompt.lower()
        if GenerationService._is_commerce_prompt(prompt):
            return "Order"
        if "consultation" in lowered:
            return "ConsultationRequest"
        if "booking" in lowered:
            return "BookingRequest"
        return "WorkflowRequest"

    @staticmethod
    def _infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        fields: list[EntityAttribute] = []
        lowered = prompt.lower()
        if GenerationService._is_commerce_prompt(prompt):
            return [
                EntityAttribute(name="customer_name", type="string", required=True, description="Customer full name", pii=True),
                EntityAttribute(name="phone", type="phone", required=True, description="Customer phone number", pii=True),
                EntityAttribute(name="product_name", type="string", required=True, description="Selected product name", pii=False),
                EntityAttribute(name="quantity", type="string", required=True, description="Requested quantity", pii=False),
                EntityAttribute(name="delivery_address", type="text", required=False, description="Delivery address", pii=True),
                EntityAttribute(name="comment", type="text", required=False, description="Order comment", pii=False),
            ]
        mappings = [
            ("name", "string", "Requester name", True, True),
            ("phone", "phone", "Contact phone number", True, True),
            ("email", "email", "Contact email", False, True),
            ("date", "date", "Preferred date", False, False),
            ("comment", "text", "Additional notes", False, False),
            ("service", "string", "Requested service type", False, False),
            ("time", "string", "Preferred time window", False, False),
        ]
        for field_name, field_type, description, required, pii in mappings:
            if field_name in lowered:
                fields.append(
                    EntityAttribute(
                        name=field_name,
                        type=field_type,  # type: ignore[arg-type]
                        required=required,
                        description=description,
                        pii=pii,
                    )
                )
        if not fields:
            fields = [
                EntityAttribute(name="title", type="string", required=True, description="Primary request title"),
                EntityAttribute(name="details", type="text", required=False, description="Primary request details"),
            ]
        return fields

    @staticmethod
    def _detect_contradictions(prompt: str) -> list[Contradiction]:
        lowered = prompt.lower()
        if "without backend" in lowered and "database" in lowered:
            return [
                Contradiction(
                    contradiction_id="contr_backend_database",
                    description="The prompt asks for no backend but also persistence in a database.",
                    left_side="without backend",
                    right_side="database persistence",
                    severity="critical",
                    resolution_hint="Choose whether the feature is frontend-only or persistent.",
                )
            ]
        return []

    @staticmethod
    def _is_commerce_prompt(prompt: str) -> bool:
        lowered = prompt.lower()
        markers = (
            "store",
            "shop",
            "catalog",
            "product",
            "cart",
            "order",
            "магазин",
            "интернет-магазин",
            "товар",
            "товары",
            "карточк",
            "корзин",
            "заказ",
            "доставк",
            "покуп",
            "покупател",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _target_platform(target_platform: TargetPlatform | str) -> TargetPlatform:
        if isinstance(target_platform, TargetPlatform):
            return target_platform
        return TargetPlatform(target_platform)

    @staticmethod
    def _preview_profile(preview_profile: PreviewProfile | str) -> PreviewProfile:
        if isinstance(preview_profile, PreviewProfile):
            return preview_profile
        return PreviewProfile(preview_profile)

    @staticmethod
    def _generation_mode(generation_mode: GenerationMode | str) -> GenerationMode:
        if isinstance(generation_mode, GenerationMode):
            return generation_mode
        return GenerationMode(generation_mode)

    @staticmethod
    def _normalize_model_payload(payload: Any) -> Any:
        if isinstance(payload, dict):
            def normalize_key(key: Any) -> Any:
                if not isinstance(key, str):
                    return key
                candidate = key.strip().strip("`'\"")
                candidate = re.sub(r"^[\(\[\{]+", "", candidate)
                candidate = re.sub(r"[\)\]\}:;,]+$", "", candidate)
                candidate = candidate.strip()
                aliases = {
                    "(trigger": "trigger",
                    "trigger)": "trigger",
                    ".trigger": "trigger",
                }
                candidate = aliases.get(candidate, candidate)
                return candidate or key

            normalized: dict[Any, Any] = {}
            for raw_key, raw_value in payload.items():
                fixed_key = normalize_key(raw_key)
                fixed_value = GenerationService._normalize_model_payload(raw_value)
                if fixed_key in normalized:
                    existing = normalized[fixed_key]
                    if existing not in (None, "", [], {}):
                        continue
                normalized[fixed_key] = fixed_value

            list_default_keys = {
                "input_data",
                "output_data",
                "preconditions",
                "postconditions",
                "error_paths",
                "request_fields",
                "response_fields",
                "permissions_hint",
                "unknowns",
                "contradictions",
                "assumptions",
                "telemetry_hooks",
                "traceability",
                "terminal_screen_ids",
                "on_enter_actions",
                "action_ids",
                "routes",
                "route_groups",
                "screen_data_sources",
                "role_action_groups",
                "input_variable_ids",
                "assignments",
                "enum_values",
                "validators",
                "components",
                "actions",
                "fields",
                "variables",
                "entities",
                "screens",
                "transitions",
                "integrations",
                "storage_bindings",
                "doc_refs",
                "actors",
                "domain_entities",
                "user_flows",
                "ui_requirements",
                "api_requirements",
                "persistence_requirements",
                "integration_requirements",
                "security_requirements",
                "platform_constraints",
                "non_functional_requirements",
                "issues",
            }
            dict_default_keys = {
                "params",
            }
            false_default_keys = {
                "auth_required",
                "existing_in_template",
                "required",
                "pii",
                "server_side_session",
                "telegram_initdata_validation_required",
                "is_entry",
                "blocking",
            }
            numeric_default_keys = {"timeout_ms"}

            for key in list_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = []
            for key in false_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = False
            for key in dict_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = {}
            for key in numeric_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = 5000
            return normalized
        if isinstance(payload, list):
            return [GenerationService._normalize_model_payload(item) for item in payload]
        if isinstance(payload, str):
            normalized_scalar = payload.strip().lower()
            if normalized_scalar == "implicit":
                return "derived"
        return payload
