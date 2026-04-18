from __future__ import annotations

from typing import Any

from app.models.common import PreviewProfile, TargetPlatform
from app.models.domain import utc_now
from app.models.grounded_spec import (
    APIField,
    APIRequirement,
    Actor,
    Assumption,
    DomainEntity,
    EvidenceLink,
    FlowStep,
    GroundedSpecModel,
    Metadata,
    NonFunctionalRequirement,
    PersistenceRequirement,
    PlatformConstraint,
    SecurityRequirement,
    UIRequirement,
    UserFlow,
)


class GroundedSpecModelingRuntime:
    def build_grounded_spec(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        doc_refs: list[Any],
        template_revision_id: str,
        prompt_turn_id: str,
        generation_mode,
    ) -> GroundedSpecModel:
        del generation_mode
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="explicit")]
        entity_name = self.infer_entity_name(prompt)
        entity_attributes = self.infer_entity_attributes(prompt)
        contradictions = self.detect_contradictions(prompt)
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
                    name="Client dashboard to detail flow",
                    goal="Client reviews the overview, opens the workbench, and continues into a dedicated workspace.",
                    steps=[
                        FlowStep(step_id="step_client_home", order=1, actor_id="actor_client", action="Open the client home screen"),
                        FlowStep(step_id="step_client_workbench", order=2, actor_id="actor_client", action="Open the workbench queue"),
                        FlowStep(step_id="step_client_workspace", order=3, actor_id="actor_client", action="Continue into the workspace page"),
                    ],
                    postconditions=["The client can move from overview into queue and detail views."],
                    acceptance_criteria=["The client can open the workbench and continue into a dedicated workspace page."],
                    evidence=evidence,
                ),
                UserFlow(
                    flow_id="flow_specialist_process",
                    name="Specialist queue processing",
                    goal="Specialist reviews the queue and updates item state from a dedicated workspace.",
                    steps=[
                        FlowStep(step_id="step_specialist_home", order=1, actor_id="actor_specialist", action="Open specialist dashboard"),
                        FlowStep(step_id="step_specialist_workbench", order=2, actor_id="actor_specialist", action="Open workbench and review queue items"),
                        FlowStep(step_id="step_specialist_workspace", order=3, actor_id="actor_specialist", action="Open workspace and progress the selected item"),
                    ],
                    postconditions=["Queue state and metrics are updated."],
                    acceptance_criteria=["The specialist can review queue items and complete work from the workspace page."],
                    evidence=evidence,
                ),
                UserFlow(
                    flow_id="flow_manager_control",
                    name="Manager oversight",
                    goal="Manager views global metrics, reviews queue state, and rebalances workload.",
                    steps=[
                        FlowStep(step_id="step_manager_home", order=1, actor_id="actor_manager", action="Open manager home"),
                        FlowStep(step_id="step_manager_workbench", order=2, actor_id="actor_manager", action="Open control workbench"),
                        FlowStep(step_id="step_manager_workspace", order=3, actor_id="actor_manager", action="Inspect a focused workspace and trigger load rebalance"),
                    ],
                    postconditions=["Control metrics reflect the current workload and SLA."],
                    acceptance_criteria=["The manager can see role health, open the workbench, and trigger a control action."],
                    evidence=evidence,
                ),
            ],
            ui_requirements=[
                UIRequirement(req_id="ui_client_home", category="screen", description="Provide a client landing page with metrics and primary actions.", priority="must", evidence=evidence, screen_hint="client_home"),
                UIRequirement(req_id="ui_client_workbench", category="screen", description="Render a client workbench with list items and route-based continuation into a detail page.", priority="must", evidence=evidence, screen_hint="client_workbench"),
                UIRequirement(req_id="ui_specialist_workbench", category="screen", description="Render a specialist workbench with next actions and request details.", priority="must", evidence=evidence, screen_hint="specialist_workbench"),
                UIRequirement(req_id="ui_manager_workbench", category="screen", description="Render a manager workbench with metrics, queue context, and control actions.", priority="must", evidence=evidence, screen_hint="manager_workbench"),
                UIRequirement(req_id="ui_workspace_page", category="screen", description="Render a module-oriented workspace page for focused detail work.", priority="must", evidence=evidence, screen_hint="client_workspace"),
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
                    api_req_id="api_submission_create",
                    name="Create request",
                    method="POST",
                    path="/api/submissions",
                    purpose="Persist workflow records and expose them in dashboard, workbench, and workspace views.",
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
                    storage_type="sqlite",
                    evidence=evidence,
                ),
                PersistenceRequirement(
                    persistence_req_id="persist_request_list",
                    entity_id="entity_request",
                    operation="list",
                    storage_type="sqlite",
                    evidence=evidence,
                ),
                PersistenceRequirement(
                    persistence_req_id="persist_request_update",
                    entity_id="entity_request",
                    operation="update",
                    storage_type="sqlite",
                    evidence=evidence,
                ),
            ],
            integration_requirements=[],
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
                    text="Balanced/basic generation should render honest empty states until persisted records exist.",
                    status="active",
                    rationale="The canonical runtime must stay DB-backed and must not inject demo workflow records.",
                    impact="medium",
                ),
            ],
            unknowns=[],
            contradictions=contradictions,
            doc_refs=list(doc_refs),
        )
