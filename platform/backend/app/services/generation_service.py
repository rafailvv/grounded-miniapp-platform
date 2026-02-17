from __future__ import annotations

import json
from datetime import datetime, timezone

from app.models.app_ir import (
    Action,
    AppIRModel,
    Assignment,
    AuthModel,
    Component,
    DataField,
    Entity,
    IRAssumption,
    IRMetadata,
    Integration,
    OpenQuestion,
    Permission,
    PlatformHints,
    Screen,
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
from app.models.common import PreviewProfile, TargetPlatform
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
from app.ai.openrouter_client import OpenRouterClient
from app.services.document_intelligence import DocumentIntelligenceService
from app.services.patch_service import PatchService
from app.services.platform_adapters import get_platform_adapter
from app.services.preview_service import PreviewService
from app.services.workspace_service import WorkspaceService, json_dumps
from app.validators.suite import ValidationSuite


class GenerationService:
    TEMPLATE_ROLES = ("client", "specialist", "manager")

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
        workspace = self.workspace_service.get_workspace(workspace_id)
        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="running",
            target_platform=target_platform,
            preview_profile=preview_profile,
            current_revision_id=workspace.current_revision_id,
        )
        self._append_event(job, "retrieval_started", "Grounded retrieval started.")
        self._save_job(job)

        missing_corpora = self.document_service.ensure_required_corpora(target_platform.value)
        if not workspace.template_cloned:
            missing_corpora.append("Workspace template has not been cloned.")
        if missing_corpora:
            job.status = "blocked"
            job.validation_snapshot = ValidationSnapshot(
                issues=[
                    ValidationIssue(
                        code="generation.missing_corpora",
                        message=message,
                        severity="critical",
                        location="generation",
                    ).model_dump(mode="json")
                    for message in missing_corpora
                ]
            )
            self._append_event(job, "job_failed", "; ".join(missing_corpora))
            self._save_job(job)
            return job

        doc_refs = self.document_service.retrieve(
            workspace_id=workspace_id,
            prompt=request.prompt,
            target_platform=target_platform.value,
        )

        chat_turn = ChatTurnRecord(workspace_id=workspace_id, role="user", content=request.prompt, linked_job_id=job.job_id)
        self.store.upsert("chat_turns", chat_turn.turn_id, chat_turn.model_dump(mode="json"))

        grounded_spec = self._generate_grounded_spec(
            workspace_id=workspace_id,
            prompt=request.prompt,
            target_platform=target_platform,
            preview_profile=preview_profile,
            doc_refs=doc_refs,
            template_revision_id=workspace.current_revision_id or "template-unknown",
            prompt_turn_id=chat_turn.turn_id,
        )
        spec_validation = self.validation_suite.validate_grounded_spec(grounded_spec)
        self._store_report(f"spec:{workspace_id}", grounded_spec.model_dump(mode="json"))
        self._store_report(
            f"assumptions:{workspace_id}",
            {"workspace_id": workspace_id, "assumptions": [item.model_dump(mode="json") for item in grounded_spec.assumptions]},
        )
        self._append_event(job, "spec_ready", "GroundedSpec created.")
        if spec_validation.blocking:
            self._block_job(job, spec_validation, grounded_spec.assumptions)
            self._append_event(job, "spec_blocked", "GroundedSpec validation blocked generation.")
            self._save_job(job)
            return job

        scenario_graph = self._build_scenario_graph(grounded_spec)
        app_ir = self._generate_app_ir(grounded_spec, scenario_graph)
        ir_validation = self.validation_suite.validate_app_ir(app_ir)
        self._store_report(f"ir:{workspace_id}", app_ir.model_dump(mode="json"))
        self._append_event(job, "ir_ready", "AppIR created.")
        if ir_validation.blocking:
            self._block_job(job, ir_validation, grounded_spec.assumptions)
            self._append_event(job, "validation_failed", "AppIR validation blocked compilation.")
            self._save_job(job)
            return job

        artifact_plan = self._build_artifact_plan(workspace_id, grounded_spec, app_ir)
        self._store_report(f"artifact_plan:{workspace_id}", artifact_plan.model_dump(mode="json"))
        self._append_event(job, "artifact_plan_ready", f"{len(artifact_plan.operations)} patch operations planned.")
        self.patch_service.apply(workspace_id=workspace_id, operations=artifact_plan.operations)
        self._append_event(job, "patch_applied", "Artifact plan applied to canonical template.")
        self._append_event(job, "build_started", "Build validation started.")

        build_issues = self.validation_suite.validate_build(self.workspace_service.source_dir(workspace_id))
        if build_issues:
            issues = [issue.model_dump(mode="json") for issue in build_issues]
            job.status = "failed"
            job.validation_snapshot = ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=False,
                blocking=True,
                issues=issues,
            )
            self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
            self._append_event(job, "job_failed", "Build validation failed after patch application.")
            self._save_job(job)
            return job

        traceability = self._build_traceability_report(workspace_id, app_ir)
        self._store_report(f"traceability:{workspace_id}", traceability.model_dump(mode="json"))
        preview = self.preview_service.rebuild(workspace_id)

        summary = self._build_summary(grounded_spec, artifact_plan)
        assistant_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="assistant",
            content=summary,
            summary=summary,
            linked_job_id=job.job_id,
        )
        self.store.upsert("chat_turns", assistant_turn.turn_id, assistant_turn.model_dump(mode="json"))

        job.status = "completed"
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
        job.artifacts = {
            "preview_url": preview.url or "",
            "grounded_spec": "reports/spec",
            "app_ir": "reports/ir",
            "traceability": "reports/traceability",
        }
        self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, "preview_ready", "Preview is ready.")
        self._append_event(job, "job_completed", "Generation completed successfully.")
        self._save_job(job)
        return job

    def retry(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            target_platform=self._target_platform(job.target_platform),
            preview_profile=self._preview_profile(job.preview_profile),
        )
        return self.generate(job.workspace_id, request)

    def get_job(self, job_id: str) -> JobRecord:
        payload = self.store.get("jobs", job_id)
        if not payload:
            raise KeyError(f"Job not found: {job_id}")
        return JobRecord.model_validate(payload)

    def current_report(self, workspace_id: str, report_type: str) -> dict | None:
        return self.store.get("reports", f"{report_type}:{workspace_id}")

    def _generate_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list,
        template_revision_id: str,
        prompt_turn_id: str,
    ) -> GroundedSpecModel:
        fallback = self._build_grounded_spec(
            workspace_id=workspace_id,
            prompt=prompt,
            target_platform=target_platform,
            preview_profile=preview_profile,
            doc_refs=doc_refs,
            template_revision_id=template_revision_id,
            prompt_turn_id=prompt_turn_id,
        )
        if not self.openrouter_client.enabled:
            return fallback
        try:
            payload = self.openrouter_client.generate_structured(
                role="spec_analysis",
                schema_name="grounded_spec_v1",
                schema=GroundedSpecModel.model_json_schema(),
                system_prompt=(
                    "You build GroundedSpec only from the provided prompt, project docs, platform docs, "
                    "and current template facts. Do not invent undocumented APIs or env vars. "
                    "Return strict JSON matching the schema."
                ),
                user_prompt=self._grounded_spec_prompt(
                    workspace_id=workspace_id,
                    prompt=prompt,
                    target_platform=target_platform,
                    preview_profile=preview_profile,
                    doc_refs=doc_refs,
                    template_revision_id=template_revision_id,
                    prompt_turn_id=prompt_turn_id,
                ),
            )
            return GroundedSpecModel.model_validate(payload)
        except Exception:
            return fallback

    def _generate_app_ir(self, spec: GroundedSpecModel, scenario_graph: dict) -> AppIRModel:
        fallback = self._build_app_ir(spec, scenario_graph)
        if not self.openrouter_client.enabled:
            return fallback
        try:
            payload = self.openrouter_client.generate_structured(
                role="ir_codegen",
                schema_name="app_ir_v1",
                schema=AppIRModel.model_json_schema(),
                system_prompt=(
                    "You convert a valid GroundedSpec into a typed AppIR for a Telegram or MAX mini-app. "
                    "Preserve traceability, respect platform constraints, and do not introduce open questions "
                    "or trusted variables from untrusted sources."
                ),
                user_prompt=self._app_ir_prompt(spec, scenario_graph),
            )
            return AppIRModel.model_validate(payload)
        except Exception:
            return fallback

    def _build_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform | str,
        preview_profile: PreviewProfile | str,
        doc_refs: list,
        template_revision_id: str,
        prompt_turn_id: str,
    ) -> GroundedSpecModel:
        target_platform = self._target_platform(target_platform)
        preview_profile = self._preview_profile(preview_profile)
        adapter = get_platform_adapter(target_platform)
        fields = self._infer_entity_attributes(prompt)
        actor = Actor(
            actor_id="actor_end_user",
            name="End user",
            role="primary_user",
            description="A single user interacting with the generated mini-app.",
            evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
        )
        entity = DomainEntity(
            entity_id="entity_submission",
            name="Submission",
            description="Primary data captured by the generated flow.",
            attributes=fields,
            evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
        )
        flow = UserFlow(
            flow_id="flow_primary",
            name="Primary submission flow",
            goal=prompt,
            preconditions=["Workspace template is available."],
            steps=[
                FlowStep(
                    step_id="step_open",
                    order=1,
                    actor_id=actor.actor_id,
                    action="Open the mini-app.",
                    expected_system_response="The form screen is shown.",
                ),
                FlowStep(
                    step_id="step_fill",
                    order=2,
                    actor_id=actor.actor_id,
                    action="Enter the requested values.",
                    input_data=[field.name for field in fields],
                    expected_system_response="Inputs are validated locally.",
                ),
                FlowStep(
                    step_id="step_submit",
                    order=3,
                    actor_id=actor.actor_id,
                    action="Submit the form.",
                    output_data=["submission_id"],
                    expected_system_response="The platform stores the request and shows success feedback.",
                ),
            ],
            postconditions=["Submission is stored in the backend."],
            error_paths=["Validation errors keep the user on the form screen."],
            acceptance_criteria=[
                "The user can complete the flow without leaving the mini-app container.",
                "The platform shows success feedback after a valid submission.",
            ],
            evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
        )
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
            product_goal=prompt,
            actors=[actor],
            domain_entities=[entity],
            user_flows=[flow],
            ui_requirements=[
                UIRequirement(
                    req_id="ui_form_screen",
                    category="form",
                    description="Provide a single mobile-first form screen for the primary task.",
                    priority="must",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                    screen_hint="screen_form",
                ),
                UIRequirement(
                    req_id="ui_feedback",
                    category="feedback",
                    description="Display clear success feedback after submission.",
                    priority="must",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                    screen_hint="screen_success",
                ),
            ],
            api_requirements=[
                APIRequirement(
                    api_req_id="api_create_submission",
                    name="Create submission",
                    method="POST",
                    path="/api/submissions",
                    purpose="Store the primary user submission.",
                    request_fields=[
                        APIField(name=field.name, type=field.type, required=field.required, description=field.description)
                        for field in fields
                    ],
                    response_fields=[
                        APIField(name="submission_id", type="uuid", required=True, description="Server-generated id"),
                        APIField(name="status", type="string", required=True, description="Submission status"),
                    ],
                    auth_required=target_platform == TargetPlatform.TELEGRAM,
                    existing_in_template=False,
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                )
            ],
            persistence_requirements=[
                PersistenceRequirement(
                    persistence_req_id="persist_submission",
                    entity_id=entity.entity_id,
                    operation="create",
                    storage_type="postgres",
                    retention_policy="Retain until manually deleted in research mode.",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                ),
                PersistenceRequirement(
                    persistence_req_id="list_submission",
                    entity_id=entity.entity_id,
                    operation="list",
                    storage_type="postgres",
                    evidence=[EvidenceLink(doc_ref_id="template-docs", evidence_type="cross_checked")],
                ),
            ],
            integration_requirements=[
                IntegrationRequirement(
                    integration_req_id="integration_backend",
                    system_name="platform_backend",
                    direction="outbound",
                    purpose="Send generated form data to the generated backend endpoint.",
                    auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
                    contract_ref="/api/submissions",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                )
            ],
            security_requirements=adapter.build_security_requirements(),
            platform_constraints=adapter.build_platform_constraints(),
            non_functional_requirements=[
                NonFunctionalRequirement(
                    nfr_id="nfr_traceability",
                    category="observability",
                    description="Every generated artifact must preserve prompt and document traceability.",
                    priority="must",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="cross_checked")],
                ),
                NonFunctionalRequirement(
                    nfr_id="nfr_preview",
                    category="usability",
                    description="Preview must remain interactive within a phone-sized shell.",
                    priority="must",
                    evidence=[EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived")],
                ),
            ],
            assumptions=[
                Assumption(
                    assumption_id="assume_single_form",
                    text="The requested prompt can be represented as a single primary form flow in v1.",
                    status="active",
                    rationale="The canonical template compiles form-centric AppIR into a stable preview.",
                    impact="medium",
                )
            ],
            unknowns=[],
            contradictions=self._detect_contradictions(prompt),
            doc_refs=doc_refs,
        )

    def _build_scenario_graph(self, spec: GroundedSpecModel) -> dict:
        return {
            "start_state": "screen_form",
            "terminal_states": ["screen_success"],
            "nodes": ["screen_form", "screen_success", "screen_error"],
            "edges": [
                {"from": "screen_form", "to": "screen_success", "action": "submit_success"},
                {"from": "screen_form", "to": "screen_error", "action": "submit_error"},
            ],
            "flow_ids": [flow.flow_id for flow in spec.user_flows],
        }

    def _build_app_ir(self, spec: GroundedSpecModel, scenario_graph: dict) -> AppIRModel:
        target_platform = self._target_platform(spec.target_platform)
        entity = spec.domain_entities[0]
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
            for attribute in entity.attributes
        ]
        variables.append(
            Variable(
                variable_id="var_submission_id",
                name="submission_id",
                type="uuid",
                required=False,
                source="api_response",
                trust_level="validated" if target_platform == TargetPlatform.TELEGRAM else "trusted",
                scope="session",
            )
        )
        components = [
            Component(
                component_id=f"cmp_{attribute.name}",
                type=self._component_type(attribute.type),
                label=attribute.name.replace("_", " ").title(),
                placeholder=f"Enter {attribute.name.replace('_', ' ')}",
                binding_variable_id=f"var_{attribute.name}",
                required=attribute.required,
                validators=self._component_validators(attribute),
            )
            for attribute in entity.attributes
        ]
        components.append(
            Component(
                component_id="cmp_submit",
                type="button",
                label="Submit",
                binding_variable_id="var_submission_id",
                required=False,
                validators=[],
            )
        )
        submit_action = Action(
            action_id="action_submit",
            type="submit_form",
            source_component_id="cmp_submit",
            input_variable_ids=[variable.variable_id for variable in variables if variable.source == "user_input"],
            integration_id="integration_submit",
            success_transition_id="transition_success",
            error_transition_id="transition_error",
            assignments=[Assignment(target_variable_id="var_submission_id", expression="$response.submission_id")],
        )
        form_screen = Screen(
            screen_id="screen_form",
            kind="form",
            title=entity.name,
            subtitle=spec.product_goal,
            components=components,
            actions=[submit_action],
            platform_hints=PlatformHints(
                use_main_button=True,
                use_back_button=False,
                respect_theme=True,
                respect_viewport=True,
            ),
        )
        success_screen = Screen(
            screen_id="screen_success",
            kind="success",
            title="Success",
            subtitle="The submission was stored successfully.",
            components=[
                Component(
                    component_id="cmp_success_message",
                    type="text",
                    label="Success",
                    binding_variable_id="var_submission_id",
                    required=False,
                    validators=[],
                )
            ],
            actions=[],
            platform_hints=PlatformHints(
                use_main_button=False,
                use_back_button=True,
                respect_theme=True,
                respect_viewport=True,
            ),
        )
        error_screen = Screen(
            screen_id="screen_error",
            kind="error",
            title="Validation error",
            subtitle="Fix the input and try again.",
            components=[],
            actions=[],
        )
        traceability = [
            TraceabilityLink(
                trace_id=f"trace_var_{variable.variable_id}",
                target_type="variable",
                target_id=variable.variable_id,
                source_kind="prompt_fragment",
                source_ref="prompt-source",
                mapping_note=f"Variable {variable.name} inferred from the primary prompt.",
            )
            for variable in variables
        ]
        traceability.append(
            TraceabilityLink(
                trace_id="trace_integration_submit",
                target_type="integration",
                target_id="integration_submit",
                source_kind="doc_ref",
                source_ref="prompt-source",
                mapping_note="Generated submission endpoint from grounded prompt and template docs.",
            )
        )
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
            entry_screen_id="screen_form",
            terminal_screen_ids=["screen_success"],
            variables=variables,
            entities=[
                Entity(
                    entity_id=entity.entity_id,
                    name=entity.name,
                    fields=[
                        DataField(
                            name=attribute.name,
                            type=attribute.type,
                            required=attribute.required,
                            description=attribute.description,
                            pii=attribute.pii,
                        )
                        for attribute in entity.attributes
                    ],
                )
            ],
            screens=[form_screen, success_screen, error_screen],
            transitions=[
                Transition(
                    transition_id="transition_success",
                    from_screen_id="screen_form",
                    to_screen_id="screen_success",
                    trigger="submit_success",
                ),
                Transition(
                    transition_id="transition_error",
                    from_screen_id="screen_form",
                    to_screen_id="screen_error",
                    trigger="submit_error",
                ),
            ],
            integrations=[
                Integration(
                    integration_id="integration_submit",
                    name="Submit form",
                    type="rest",
                    method="POST",
                    path="/api/submissions",
                    request_schema=[
                        DataField(name=attribute.name, type=attribute.type, required=attribute.required, pii=attribute.pii)
                        for attribute in entity.attributes
                    ],
                    response_schema=[
                        DataField(name="submission_id", type="uuid", required=True),
                        DataField(name="status", type="string", required=True),
                    ],
                    auth_type="telegram_initdata" if target_platform == TargetPlatform.TELEGRAM else "custom",
                )
            ],
            storage_bindings=[
                StorageBinding(
                    binding_id="storage_submission",
                    entity_id=entity.entity_id,
                    storage_type="postgres",
                    table_or_collection="submissions",
                )
            ],
            auth_model=AuthModel(
                mode="telegram_session" if target_platform == TargetPlatform.TELEGRAM else "custom",
                telegram_initdata_validation_required=target_platform == TargetPlatform.TELEGRAM,
                server_side_session=True,
            ),
            permissions=[
                Permission(
                    permission_id="permission_submit",
                    name="submit_submission",
                    description="Allows the end user to submit the generated form.",
                )
            ],
            security=SecurityPolicy(
                trusted_sources=["validated_init_data"] if target_platform == TargetPlatform.TELEGRAM else ["validated_host_session"],
                untrusted_sources=["user_input", "client_storage", "unsafe_host_payload"],
                secret_handling="server_env_only",
                pii_variables=[variable.variable_id for variable in variables if variable.pii],
            ),
            telemetry_hooks=[
                TelemetryHook(event_name="screen_form_view", trigger_type="screen_view", screen_id="screen_form"),
                TelemetryHook(event_name="form_submit", trigger_type="form_submit", action_id="action_submit"),
            ],
            assumptions=[
                IRAssumption(
                    assumption_id="ir_single_form",
                    text="The generated application remains within the canonical single-form template.",
                    origin="grounded_spec",
                )
            ],
            open_questions=[],
            traceability=traceability,
        )

    def _build_artifact_plan(self, workspace_id: str, spec: GroundedSpecModel, ir: AppIRModel) -> ArtifactPlanModel:
        role_seed = self._build_role_seed(spec, ir)
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
                operation_id="op_preview_payload",
                op="update",
                file_path="frontend/src/shared/generated/role-experience.json",
                content=json_dumps(role_experience),
                explanation="Compile role-aware frontend experience descriptors.",
                trace_refs=["prompt-source"],
            ),
            PatchOperationModel(
                operation_id="op_role_seed",
                op="update",
                file_path="backend/app/generated/role_seed.json",
                content=json_dumps(role_seed),
                explanation="Compile role-aware backend seed data and dashboards.",
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
            summary="Compile grounded artifacts into the canonical template.",
            operations=operations,
        )

    def _build_role_seed(self, spec: GroundedSpecModel, ir: AppIRModel) -> dict:
        primary_entity = spec.domain_entities[0]
        prompt_goal = spec.product_goal
        role_seed = {
            "roles": {
                "client": {
                    "title": "Кабинет клиента",
                    "description": prompt_goal,
                    "feature_text": f"Клиент создает и отслеживает запрос по сценарию: {prompt_goal}",
                    "primary_action_label": "Создать заявку",
                    "secondary_action_label": "Профиль клиента",
                    "metrics": [
                        {"metric_id": "client_flow", "label": "Формы", "value": str(len(ir.screens))},
                        {"metric_id": "client_fields", "label": "Поля", "value": str(len(primary_entity.attributes))},
                    ],
                    "profile": {
                        "first_name": "Клиент",
                        "last_name": "Сценарий",
                        "email": "client@example.local",
                        "phone": "+7 (999) 111-22-33",
                        "photo_url": None,
                    },
                },
                "specialist": {
                    "title": "Кабинет специалиста",
                    "description": "Обработка и сопровождение клиентских запросов.",
                    "feature_text": f"Специалист видит входящий поток по сущности {primary_entity.name}.",
                    "primary_action_label": "Открыть очередь",
                    "secondary_action_label": "Профиль специалиста",
                    "metrics": [
                        {"metric_id": "specialist_queue", "label": "Очередь", "value": "5"},
                        {"metric_id": "specialist_bindings", "label": "Интеграции", "value": str(len(ir.integrations))},
                    ],
                    "profile": {
                        "first_name": "Специалист",
                        "last_name": "Сценарий",
                        "email": "specialist@example.local",
                        "phone": "+7 (999) 222-33-44",
                        "photo_url": None,
                    },
                },
                "manager": {
                    "title": "Кабинет менеджера",
                    "description": "Контроль состояния сценария и распределения нагрузки.",
                    "feature_text": "Менеджер видит сводные метрики, SLA и общую картину по ролям.",
                    "primary_action_label": "Открыть панель контроля",
                    "secondary_action_label": "Профиль менеджера",
                    "metrics": [
                        {"metric_id": "manager_transitions", "label": "Переходы", "value": str(len(ir.transitions))},
                        {"metric_id": "manager_traceability", "label": "Trace links", "value": str(len(ir.traceability))},
                    ],
                    "profile": {
                        "first_name": "Менеджер",
                        "last_name": "Сценарий",
                        "email": "manager@example.local",
                        "phone": "+7 (999) 333-44-55",
                        "photo_url": None,
                    },
                },
            }
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

    def _block_job(
        self,
        job: JobRecord,
        validation_result: GroundedSpecValidatorResult | AppIRValidatorResult,
        assumptions: list,
    ) -> None:
        job.status = "blocked"
        job.assumptions_report = [item.model_dump(mode="json") for item in assumptions]
        job.validation_snapshot = ValidationSnapshot(
            grounded_spec_valid=isinstance(validation_result, GroundedSpecValidatorResult) and validation_result.valid,
            app_ir_valid=isinstance(validation_result, AppIRValidatorResult) and validation_result.valid,
            build_valid=False,
            blocking=validation_result.blocking,
            issues=[issue.model_dump(mode="json") for issue in validation_result.issues],
        )
        self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))

    def _build_summary(self, spec: GroundedSpecModel, artifact_plan: ArtifactPlanModel) -> str:
        target_platform = self._target_platform(spec.target_platform)
        return (
            f"Built a grounded single-flow mini-app for {target_platform.value}. "
            f"Applied {len(artifact_plan.operations)} template patches, "
            f"preserved {len(spec.doc_refs)} source references, "
            f"and recorded {len(spec.assumptions)} active assumptions."
        )

    def _append_event(self, job: JobRecord, event_type: str, message: str) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message))
        job.updated_at = datetime.now(timezone.utc)

    def _save_job(self, job: JobRecord) -> None:
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def _store_report(self, key: str, payload: dict) -> None:
        self.store.upsert("reports", key, payload)

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
    def _infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        fields: list[EntityAttribute] = []
        mappings = [
            ("name", "string", "Requester name", True, True),
            ("phone", "phone", "Contact phone number", True, True),
            ("email", "email", "Contact email", False, True),
            ("date", "date", "Preferred date", False, False),
            ("comment", "text", "Additional notes", False, False),
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
    def _grounded_spec_prompt(
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list,
        template_revision_id: str,
        prompt_turn_id: str,
    ) -> str:
        docs = "\n".join(
            f"- [{item.source_type}] {item.file_path} :: {item.section_title or item.chunk_id} :: {item.snippet or ''}"
            for item in doc_refs
        )
        return (
            f"Workspace ID: {workspace_id}\n"
            f"Prompt turn ID: {prompt_turn_id}\n"
            f"Template revision: {template_revision_id}\n"
            f"Target platform: {target_platform.value}\n"
            f"Preview profile: {preview_profile.value}\n"
            f"User prompt:\n{prompt}\n\n"
            f"Retrieved evidence:\n{docs}\n"
        )

    @staticmethod
    def _app_ir_prompt(spec: GroundedSpecModel, scenario_graph: dict) -> str:
        return (
            "GroundedSpec JSON:\n"
            f"{json.dumps(spec.model_dump(mode='json'), ensure_ascii=True, indent=2)}\n\n"
            "Scenario graph JSON:\n"
            f"{json.dumps(scenario_graph, ensure_ascii=True, indent=2)}\n"
        )
