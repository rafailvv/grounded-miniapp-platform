from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re
from typing import Any

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
    AppIRValidatorResult,
    ArtifactPlanModel,
    GroundedSpecValidatorResult,
    PatchOperationModel,
    TraceabilityReportEntry,
    TraceabilityReportModel,
    ValidationIssue,
)
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import ChatTurnRecord, GenerateRequest, JobEvent, JobRecord, ValidationSnapshot, new_id, utc_now
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
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.patch_service import PatchService
from app.services.preview_service import PreviewService
from app.services.workspace_service import WorkspaceService, json_dumps
from app.validators.suite import ValidationSuite


ROLE_ORDER = ("client", "specialist", "manager")
QUALITY_FIDELITY = {
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
        patch_service: PatchService,
        preview_service: PreviewService,
        validation_suite: ValidationSuite,
        openrouter_client: OpenRouterClient,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.document_service = document_service
        self.patch_service = patch_service
        self.preview_service = preview_service
        self.validation_suite = validation_suite
        self.openrouter_client = openrouter_client

    def generate(self, workspace_id: str, request: GenerateRequest) -> JobRecord:
        target_platform = self._target_platform(request.target_platform)
        preview_profile = self._preview_profile(request.preview_profile)
        generation_mode = self._generation_mode(request.generation_mode)
        workspace = self.workspace_service.get_workspace(workspace_id)
        llm_config = self.openrouter_client.configuration()

        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="running",
            generation_mode=generation_mode,
            target_platform=target_platform,
            preview_profile=preview_profile,
            current_revision_id=workspace.current_revision_id,
            fidelity=QUALITY_FIDELITY[generation_mode],  # type: ignore[arg-type]
            llm_enabled=bool(llm_config["enabled"]),
            llm_provider="openrouter" if llm_config["enabled"] else None,
        )
        self._clear_trace(workspace_id)
        self._append_trace(
            workspace_id,
            "job_started",
            "Generation request accepted.",
            {
                "mode": generation_mode.value,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "llm_enabled": bool(llm_config["enabled"]),
            },
        )
        self._append_event(job, "retrieval_started", "Grounded retrieval started.", {"stage": "retrieval"})

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

        if generation_mode == GenerationMode.QUALITY and not self.openrouter_client.enabled:
            self._append_trace(
                workspace_id,
                "llm_blocked",
                "Quality mode blocked because no LLM provider is configured.",
                {"required_mode": generation_mode.value},
            )
            return self._block_with_messages(
                job,
                [
                    "Quality mode requires OpenRouter configuration.",
                    "Set OPENROUTER_API_KEY or choose balanced/basic mode explicitly.",
                ],
                code="generation.quality_requires_llm",
                event_type="job_failed",
                failure_reason="Quality mode blocked because OpenRouter is not configured.",
            )

        doc_refs = self.document_service.retrieve(
            workspace_id=workspace_id,
            prompt=request.prompt,
            target_platform=target_platform.value,
        )
        self._append_trace(
            workspace_id,
            "retrieval_completed",
            "Relevant documents and platform rules retrieved.",
            {"doc_refs": len(doc_refs)},
        )

        chat_turn = ChatTurnRecord(workspace_id=workspace_id, role="user", content=request.prompt, linked_job_id=job.job_id)
        self.store.upsert("chat_turns", chat_turn.turn_id, chat_turn.model_dump(mode="json"))

        spec_result = self._resolve_grounded_spec(
            workspace_id=workspace_id,
            prompt=request.prompt,
            target_platform=target_platform,
            preview_profile=preview_profile,
            doc_refs=doc_refs,
            template_revision_id=workspace.current_revision_id or "template-unknown",
            prompt_turn_id=chat_turn.turn_id,
            generation_mode=generation_mode,
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

        scenario_graph = self._build_scenario_graph(grounded_spec)
        self._append_trace(
            workspace_id,
            "scenario_graph_built",
            "Scenario graph expanded from grounded flows.",
            {
                "nodes": len(scenario_graph.get("nodes", [])),
                "edges": len(scenario_graph.get("edges", [])),
                "roles": scenario_graph.get("roles", []),
            },
        )
        app_ir_result = self._resolve_app_ir(
            spec=grounded_spec,
            scenario_graph=scenario_graph,
            generation_mode=generation_mode,
        )
        if "error" in app_ir_result:
            self._append_trace(
                workspace_id,
                "ir_failed",
                "AppIR generation failed.",
                {"error": app_ir_result["error"]},
            )
            return self._block_with_messages(
                job,
                [app_ir_result["error"]],
                code="generation.ir.llm_failure",
                event_type="validation_failed",
                failure_reason=app_ir_result["error"],
            )
        app_ir: AppIRModel = app_ir_result["ir"]
        if app_ir_result.get("model"):
            job.llm_model = str(app_ir_result["model"])
        self._append_trace(
            workspace_id,
            "ir_built",
            "AppIR created.",
            {
                "screens": len(app_ir.screens),
                "route_groups": len(app_ir.route_groups),
                "integrations": len(app_ir.integrations),
                "actions": sum(len(screen.actions) for screen in app_ir.screens),
                "model": app_ir_result.get("model"),
            },
        )

        ir_validation = self.validation_suite.validate_app_ir(app_ir)
        self._store_report(f"ir:{workspace_id}", app_ir.model_dump(mode="json"))
        self._append_event(job, "ir_ready", "AppIR created.")
        if ir_validation.blocking:
            repaired_ir = self._build_app_ir(grounded_spec, scenario_graph, generation_mode)
            repaired_validation = self.validation_suite.validate_app_ir(repaired_ir)
            if repaired_validation.blocking:
                self._block_job(job, ir_validation, grounded_spec.assumptions, failure_reason="AppIR validation blocked compilation.")
                self._append_trace(
                    workspace_id,
                    "ir_validation_failed",
                    "AppIR validation blocked compilation.",
                    {"issues": [issue.model_dump(mode="json") for issue in ir_validation.issues]},
                )
                self._append_event(job, "validation_failed", "AppIR validation blocked compilation.")
                return job
            app_ir = repaired_ir
            ir_validation = repaired_validation
            self._store_report(f"ir:{workspace_id}", app_ir.model_dump(mode="json"))
            self._append_event(job, "repair_iteration", "AppIR validation failed; switched to deterministic compiler IR.")
            self._append_trace(
                workspace_id,
                "ir_repaired",
                "Blocking AppIR issues were repaired via deterministic compiler IR.",
                {"strategy": "fallback_compiler_ir"},
            )

        artifact_plan = self._build_artifact_plan(workspace_id, grounded_spec, app_ir, generation_mode)
        self._store_report(f"artifact_plan:{workspace_id}", artifact_plan.model_dump(mode="json"))
        self._append_event(job, "artifact_plan_ready", f"{len(artifact_plan.operations)} patch operations planned.")
        self._append_trace(
            workspace_id,
            "artifact_plan_built",
            "Artifact plan prepared for template patching.",
            {
                "operations": len(artifact_plan.operations),
                "targets": [operation.file_path for operation in artifact_plan.operations[:12]],
            },
        )
        self.patch_service.apply(workspace_id=workspace_id, operations=artifact_plan.operations)
        self._append_event(job, "patch_applied", "Artifact plan applied to canonical template.")
        self._append_trace(
            workspace_id,
            "patch_applied",
            "Template workspace updated with generated artifacts.",
            {"revision_id": self.workspace_service.get_workspace(workspace_id).current_revision_id},
        )
        self._append_event(job, "build_started", "Build validation started.")

        build_issues = self.validation_suite.validate_build(self.workspace_service.source_dir(workspace_id))
        if build_issues:
            job.status = "failed"
            job.failure_reason = "Build validation failed after patch application."
            job.validation_snapshot = ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=False,
                blocking=True,
                issues=[issue.model_dump(mode="json") for issue in build_issues],
            )
            self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
            self._append_trace(
                workspace_id,
                "build_failed",
                "Build validation failed after patch application.",
                {"issues": [issue.model_dump(mode="json") for issue in build_issues]},
            )
            self._append_event(job, "job_failed", job.failure_reason)
            return job
        self._append_trace(
            workspace_id,
            "build_succeeded",
            "Build validation completed successfully.",
            {"source_dir": str(self.workspace_service.source_dir(workspace_id))},
        )

        traceability = self._build_traceability_report(workspace_id, app_ir)
        self._store_report(f"traceability:{workspace_id}", traceability.model_dump(mode="json"))
        preview = self.preview_service.rebuild(workspace_id)
        self._append_trace(
            workspace_id,
            "preview_rebuilt",
            "Preview runtime rebuilt and refreshed.",
            {
                "status": preview.status,
                "runtime_mode": preview.runtime_mode,
                "url": preview.url,
                "logs": preview.logs[-10:],
            },
        )

        summary = self._build_summary(grounded_spec, app_ir, artifact_plan, generation_mode)
        assistant_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="assistant",
            content=summary,
            summary=summary,
            linked_job_id=job.job_id,
        )
        self.store.upsert("chat_turns", assistant_turn.turn_id, assistant_turn.model_dump(mode="json"))

        job.status = "completed"
        job.failure_reason = None
        job.summary = summary
        job.traceability_report_id = traceability.report_id
        job.assumptions_report = [item.model_dump(mode="json") for item in grounded_spec.assumptions]
        job.validation_snapshot = ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=False,
            issues=[],
        )
        job.compile_summary = self._compile_summary(app_ir)
        job.artifacts = {
            "preview_url": preview.url or "",
            "grounded_spec": "reports/spec",
            "app_ir": "reports/ir",
            "traceability": "reports/traceability",
            "fidelity": job.fidelity,
        }
        self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, "preview_ready", "Preview is ready.")
        self._append_event(job, "job_completed", "Generation completed successfully.")
        self._append_trace(
            workspace_id,
            "job_completed",
            "Generation completed successfully.",
            {
                "summary": summary,
                "compile_summary": job.compile_summary,
                "artifacts": job.artifacts,
            },
        )
        return job

    def retry(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            target_platform=self._target_platform(job.target_platform),
            preview_profile=self._preview_profile(job.preview_profile),
            generation_mode=self._generation_mode(job.generation_mode),
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
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.BASIC or not self.openrouter_client.enabled:
            return {
                "spec": self._build_grounded_spec(
                    workspace_id=workspace_id,
                    prompt=prompt,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    doc_refs=doc_refs,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                    generation_mode=generation_mode,
                )
            }
        try:
            payload = self.openrouter_client.generate_structured(
                role="spec_analysis",
                schema_name="grounded_spec_v1",
                schema=GroundedSpecModel.model_json_schema(),
                system_prompt=self._grounded_spec_system_prompt(),
                user_prompt=self._grounded_spec_user_prompt(prompt, doc_refs, target_platform, preview_profile, template_revision_id, prompt_turn_id),
            )
            spec = GroundedSpecModel.model_validate(self._normalize_model_payload(payload["payload"]))
            return {"spec": spec, "model": payload["model"]}
        except Exception as exc:
            if generation_mode == GenerationMode.QUALITY:
                return {"error": f"Quality mode could not build GroundedSpec through OpenRouter: {exc}"}
            return {
                "spec": self._build_grounded_spec(
                    workspace_id=workspace_id,
                    prompt=prompt,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    doc_refs=doc_refs,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                    generation_mode=generation_mode,
                ),
                "model": None,
            }

    def _resolve_app_ir(
        self,
        *,
        spec: GroundedSpecModel,
        scenario_graph: dict[str, Any],
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.BASIC or not self.openrouter_client.enabled:
            return {"ir": self._build_app_ir(spec, scenario_graph, generation_mode)}
        try:
            payload = self.openrouter_client.generate_structured(
                role="ir_codegen",
                schema_name="app_ir_v1",
                schema=AppIRModel.model_json_schema(),
                system_prompt=self._app_ir_system_prompt(),
                user_prompt=self._app_ir_user_prompt(spec, scenario_graph),
            )
            llm_ir = AppIRModel.model_validate(self._normalize_model_payload(payload["payload"]))
            llm_ir = self._stabilize_app_ir(llm_ir, spec, scenario_graph, generation_mode)
            enriched_ir = self._enrich_app_ir(llm_ir, spec, scenario_graph, generation_mode)
            return {"ir": enriched_ir, "model": payload["model"]}
        except Exception as exc:
            if generation_mode == GenerationMode.QUALITY:
                return {"error": f"Quality mode could not build AppIR through OpenRouter: {exc}"}
            return {"ir": self._build_app_ir(spec, scenario_graph, generation_mode)}

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
        return {
            "roles": {
                "client": ["client_home", "client_form", "client_requests", "client_detail", "client_success", "client_profile"],
                "specialist": ["specialist_home", "specialist_queue", "specialist_detail", "specialist_profile"],
                "manager": ["manager_home", "manager_dashboard", "manager_records", "manager_profile"],
            },
            "transitions": [
                ("client_home", "client_form"),
                ("client_home", "client_requests"),
                ("client_form", "client_success"),
                ("client_requests", "client_detail"),
                ("specialist_home", "specialist_queue"),
                ("specialist_queue", "specialist_detail"),
                ("manager_home", "manager_dashboard"),
                ("manager_dashboard", "manager_records"),
            ],
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
        fallback_screens = {screen.screen_id: screen for screen in fallback.screens}
        screens = [
            fallback_screens.get(screen.screen_id, screen)
            if self._screen_is_generic(screen.title, screen.subtitle)
            else screen
            for screen in llm_ir.screens
        ]
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

    def _build_runtime_manifest(
        self,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        entity = spec.domain_entities[0]
        records = self._sample_records(entity, spec.product_goal)
        route_lookup = {route.screen_id: route for group in ir.route_groups for route in group.routes}
        screens_by_id = {screen.screen_id: screen for screen in ir.screens}
        roles: dict[str, Any] = {}
        for group in ir.route_groups:
            role_records = self._records_for_role(group.role, records)
            role_screens = {}
            for route in group.routes:
                screen = screens_by_id[route.screen_id]
                role_screens[screen.screen_id] = {
                    "screen_id": screen.screen_id,
                    "path": route.path,
                    "title": screen.title,
                    "subtitle": screen.subtitle,
                    "kind": screen.kind,
                    "components": [component.model_dump(mode="json") for component in screen.components],
                    "actions": [self._manifest_action(action, route_lookup) for action in screen.actions],
                    "sections": self._screen_sections(group.role, screen.screen_id, screen.kind, entity, role_records, spec.product_goal),
                }
            roles[group.role] = {
                "entry_path": group.entry_path,
                "routes": [route.model_dump(mode="json") for route in group.routes],
                "screens": role_screens,
                "navigation": self._navigation_items(group.role),
            }
        return {
            "app": {
                "title": spec.product_goal[:80],
                "goal": spec.product_goal,
                "generation_mode": generation_mode.value,
                "platform": spec.target_platform,
                "preview_profile": spec.preview_profile,
                "route_count": sum(len(group.routes) for group in ir.route_groups),
                "screen_count": len(ir.screens),
            },
            "roles": roles,
        }

    def _build_runtime_state(
        self,
        spec: GroundedSpecModel,
        ir: AppIRModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        entity = spec.domain_entities[0]
        records: list[dict[str, Any]] = []
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
                        "first_name": "",
                        "last_name": "",
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
                        "first_name": "",
                        "last_name": "",
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
                        "first_name": "",
                        "last_name": "",
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
                "feature_text": home_screen["sections"][0]["body"] if home_screen["sections"] else runtime_manifest["app"]["goal"],
                "primary_action_label": home_screen["actions"][0]["label"] if home_screen["actions"] else "Open",
                "secondary_action_label": "Profile",
                "metrics": role_state["metrics"],
                "profile": role_state["profile"],
            }
        return role_seed

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
                f"{entity_title} hub",
                f"Create and track your {flow_label} requests.",
                [
                    Action(action_id="client_open_form", type="navigate", target_screen_id="client_form"),
                    Action(action_id="client_open_requests", type="navigate", target_screen_id="client_requests"),
                    Action(action_id="client_open_profile", type="navigate", target_screen_id="client_profile"),
                ],
            ),
            screen(
                "client_form",
                "form",
                f"Book {flow_label}",
                f"Complete the {flow_label} form and submit it into the shared workflow.",
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
                f"Browse current and historical {flow_label} submissions.",
                [
                    Action(action_id="client_open_request_detail", type="navigate", target_screen_id="client_detail"),
                    Action(action_id="client_open_form_inline", type="navigate", target_screen_id="client_form"),
                ],
            ),
            screen(
                "client_detail",
                "details",
                f"{entity_title} detail",
                f"Inspect status, timeline, and next steps for this {flow_label}.",
                [Action(action_id="client_back_to_requests", type="navigate", target_screen_id="client_requests")],
            ),
            screen(
                "client_success",
                "success",
                f"{entity_title} submitted",
                f"The {flow_label} has been stored and routed to operations.",
                [
                    Action(action_id="client_success_to_requests", type="navigate", target_screen_id="client_requests"),
                    Action(action_id="client_success_to_home", type="navigate", target_screen_id="client_home"),
                ],
            ),
            screen(
                "client_profile",
                "info",
                "Client profile",
                f"Manage contact details used for {flow_label}.",
                [Action(action_id="client_profile_save", type="call_api", integration_id="integration_save_profile")],
            ),
            screen(
                "specialist_home",
                "landing",
                f"{entity_title} operations",
                f"Review incoming {flow_label} requests and next actions.",
                [
                    Action(action_id="specialist_open_queue", type="navigate", target_screen_id="specialist_queue"),
                    Action(action_id="specialist_open_profile", type="navigate", target_screen_id="specialist_profile"),
                ],
            ),
            screen(
                "specialist_queue",
                "list",
                f"{entity_title} queue",
                f"Claim {flow_label} items, update status, and open details.",
                [
                    Action(action_id="specialist_claim_next", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="specialist_open_detail", type="navigate", target_screen_id="specialist_detail"),
                ],
            ),
            screen(
                "specialist_detail",
                "details",
                f"{entity_title} processing",
                f"Review the {flow_label} context and change workflow status.",
                [
                    Action(action_id="specialist_mark_in_progress", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="specialist_complete_request", type="call_api", integration_id="integration_runtime_action"),
                ],
            ),
            screen(
                "specialist_profile",
                "info",
                "Specialist profile",
                f"Adjust specialist data used during {flow_label} handling.",
                [Action(action_id="specialist_profile_save", type="call_api", integration_id="integration_save_profile")],
            ),
            screen(
                "manager_home",
                "landing",
                f"{entity_title} oversight",
                f"Open control dashboards and inspect {flow_label} throughput across the whole flow.",
                [
                    Action(action_id="manager_open_dashboard", type="navigate", target_screen_id="manager_dashboard"),
                    Action(action_id="manager_open_profile", type="navigate", target_screen_id="manager_profile"),
                ],
            ),
            screen(
                "manager_dashboard",
                "list",
                f"{entity_title} dashboard",
                f"Review aggregate {flow_label} metrics, alerts, and control actions.",
                [
                    Action(action_id="manager_rebalance", type="call_api", integration_id="integration_runtime_action"),
                    Action(action_id="manager_open_records", type="navigate", target_screen_id="manager_records"),
                ],
            ),
            screen(
                "manager_records",
                "details",
                f"All {entity_plural}",
                f"Inspect {flow_label} records by status and ownership.",
                [Action(action_id="manager_refresh_records", type="call_api", integration_id="integration_runtime_action")],
            ),
            screen(
                "manager_profile",
                "info",
                "Manager profile",
                f"Manage profile data and notification preferences for {flow_label}.",
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
    ) -> list[dict[str, Any]]:
        flow_label = self._flow_label(prompt_goal, entity)
        entity_title = self._entity_title(entity)
        if screen_id.endswith("home"):
            return [
                {
                    "section_id": f"{screen_id}_hero",
                    "type": "hero",
                    "title": self._title_for_role(role, flow_label),
                    "body": self._role_body(role, flow_label),
                },
                {
                    "section_id": f"{screen_id}_stats",
                    "type": "stats",
                    "items": self._role_stats(role, records),
                },
            ]
        if screen_id == "client_form":
            return [
                {
                    "section_id": "client_form_intro",
                    "type": "hero",
                    "title": f"{entity_title} form",
                    "body": f"Complete the {flow_label} form and send it into the shared workflow.",
                },
                {
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
                },
            ]
        if kind == "list":
            return [
                {
                    "section_id": f"{screen_id}_list",
                    "type": "list",
                    "items": [
                        {
                            "item_id": record["record_id"],
                            "title": record["title"],
                            "subtitle": record["summary"],
                            "status": record["status"],
                            "meta": record["priority"],
                        }
                        for record in records
                    ],
                }
            ]
        if kind == "details":
            record = records[0] if records else {"title": entity.name, "summary": prompt_goal, "payload": {}, "timeline": []}
            return [
                {
                    "section_id": f"{screen_id}_detail",
                    "type": "detail",
                    "title": record["title"],
                    "body": record["summary"],
                    "fields": [{"label": key.replace("_", " ").title(), "value": value} for key, value in record["payload"].items()],
                },
                {
                    "section_id": f"{screen_id}_timeline",
                    "type": "timeline",
                    "items": record["timeline"],
                },
            ]
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
                    "body": f"The {flow_label} is now visible to the specialist queue and manager dashboard.",
                }
            ]
        return []

    def _manifest_action(self, action: Action, route_lookup: dict[str, RouteDefinition]) -> dict[str, Any]:
        payload = action.model_dump(mode="json")
        payload["label"] = self._action_label(action.action_id)
        if action.target_screen_id and action.target_screen_id in route_lookup:
            payload["target_path"] = route_lookup[action.target_screen_id].path
        return payload

    def _navigation_items(self, role: str) -> list[dict[str, str]]:
        items = {
            "client": [
                {"label": "Home", "path": "/"},
                {"label": "Book", "path": "/book"},
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
        return items[role]

    def _role_stats(self, role: str, records: list[dict[str, Any]]) -> list[dict[str, str]]:
        counts = Counter(record["status"] for record in records)
        if role == "client":
            return [
                {"label": "Bookings", "value": str(len(records))},
                {"label": "Awaiting response", "value": str(counts.get("new", 0) + counts.get("in_progress", 0))},
            ]
        if role == "specialist":
            return [
                {"label": "New bookings", "value": str(counts.get("new", 0))},
                {"label": "In review", "value": str(counts.get("in_progress", 0))},
            ]
        return [
            {"label": "Completed", "value": str(counts.get("completed", 0))},
            {"label": "Unassigned", "value": str(sum(1 for record in records if record.get("owner") == "unassigned"))},
        ]

    def _title_for_role(self, role: str, flow_label: str) -> str:
        return {
            "client": f"Book and track {flow_label}",
            "specialist": f"Process {flow_label} requests",
            "manager": f"Monitor {flow_label} operations",
        }[role]

    def _role_body(self, role: str, flow_label: str) -> str:
        return {
            "client": f"Create a new {flow_label}, review status updates, and keep contact details current.",
            "specialist": f"Claim incoming {flow_label} requests, review details, and move them through the workflow.",
            "manager": f"See the whole {flow_label} pipeline, workload distribution, and completion status.",
        }[role]

    @staticmethod
    def _action_label(action_id: str) -> str:
        labels = {
            "client_open_form": "Open booking form",
            "client_open_requests": "Open my bookings",
            "client_submit_request": "Submit booking",
            "client_open_request_detail": "Open booking details",
            "client_open_form_inline": "Create another booking",
            "client_success_to_requests": "View my bookings",
            "client_success_to_home": "Back to overview",
            "client_profile_save": "Save client profile",
            "specialist_open_queue": "Open booking queue",
            "specialist_claim_next": "Claim next booking",
            "specialist_open_detail": "Open booking details",
            "specialist_mark_in_progress": "Start processing",
            "specialist_complete_request": "Mark booking complete",
            "specialist_profile_save": "Save specialist profile",
            "manager_open_dashboard": "Open booking dashboard",
            "manager_open_records": "Open all bookings",
            "manager_rebalance": "Rebalance assignments",
            "manager_refresh_records": "Refresh booking records",
            "manager_profile_save": "Save manager profile",
        }
        return labels.get(action_id, action_id.replace("_", " ").replace("api", "API").title())

    def _flow_label(self, prompt: str, entity: DomainEntity) -> str:
        lowered = prompt.lower()
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
        self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, event_type, failure_reason)
        self._append_trace(
            job.workspace_id,
            "job_blocked",
            failure_reason,
            {"messages": messages, "code": code},
        )
        return job

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)

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

    def _grounded_spec_system_prompt(self) -> str:
        return (
            "You are generating a documentation-grounded multi-role mini-app specification. "
            "Use only information grounded in the supplied docs and prompt. "
            "Do not collapse the app into a single form if multi-role workflows are implied. "
            "Prefer explicit assumptions and canonical template defaults over blocking unknowns for ordinary implementation details. "
            "Only emit high-impact unknowns when generation truly cannot proceed without user clarification. "
            "Keep the output strictly valid against the schema."
        )

    def _grounded_spec_user_prompt(
        self,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec",
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "docs": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in doc_refs],
            }
        )

    def _app_ir_system_prompt(self) -> str:
        return (
            "You are generating a quality-first AppIR for a multi-page, role-aware mini-app. "
            "Prefer explicit route groups, multiple screens per role, real navigation, and stateful actions. "
            "Keep the output strictly valid against the schema."
        )

    def _app_ir_user_prompt(self, spec: GroundedSpecModel, scenario_graph: dict[str, Any]) -> str:
        return json_dumps(
            {
                "task": "Build AppIR",
                "grounded_spec": spec.model_dump(mode="json"),
                "scenario_graph": scenario_graph,
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
        if "consultation" in lowered:
            return "ConsultationRequest"
        if "booking" in lowered:
            return "BookingRequest"
        if "order" in lowered:
            return "OrderRequest"
        return "WorkflowRequest"

    @staticmethod
    def _infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        fields: list[EntityAttribute] = []
        mappings = [
            ("name", "string", "Requester name", True, True),
            ("phone", "phone", "Contact phone number", True, True),
            ("email", "email", "Contact email", False, True),
            ("date", "date", "Preferred date", False, False),
            ("comment", "text", "Additional notes", False, False),
            ("service", "string", "Requested service type", False, False),
            ("time", "string", "Preferred time window", False, False),
        ]
        lowered = prompt.lower()
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
            normalized = {key: GenerationService._normalize_model_payload(value) for key, value in payload.items()}
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
        return payload
