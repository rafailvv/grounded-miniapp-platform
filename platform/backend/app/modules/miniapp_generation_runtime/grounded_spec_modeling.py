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
        shared_entity_name = entity_name.strip() or "Record"
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
                    description=f"Starts the primary {shared_entity_name.lower()} flow and reviews their own state.",
                    permissions_hint=["create", "view_own_records", "continue_flow"],
                    evidence=evidence,
                ),
                Actor(
                    actor_id="actor_specialist",
                    name="Specialist",
                    role="specialist",
                    description=f"Works on shared {shared_entity_name.lower()} items and updates progress.",
                    permissions_hint=["review_assigned_records", "change_status", "respond"],
                    evidence=evidence,
                ),
                Actor(
                    actor_id="actor_manager",
                    name="Manager",
                    role="manager",
                    description=f"Oversees cross-role {shared_entity_name.lower()} activity and monitors the shared system state.",
                    permissions_hint=["view_metrics", "review_all_records", "intervene"],
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
                    flow_id="flow_shared_lifecycle",
                    name=f"{shared_entity_name} lifecycle",
                    goal=f"One {shared_entity_name.lower()} moves across the client, specialist, and manager surfaces while staying in one DB-backed state model.",
                    steps=[
                        FlowStep(step_id="step_client_create", order=1, actor_id="actor_client", action=f"Create or update a {shared_entity_name.lower()}"),
                        FlowStep(step_id="step_specialist_review", order=2, actor_id="actor_specialist", action=f"Review and progress the same {shared_entity_name.lower()}"),
                        FlowStep(step_id="step_manager_oversee", order=3, actor_id="actor_manager", action=f"Observe the shared {shared_entity_name.lower()} state and intervene when required"),
                    ],
                    postconditions=[f"The same {shared_entity_name.lower()} is readable across all three role surfaces."],
                    acceptance_criteria=[
                        f"Client can create or update a {shared_entity_name.lower()} and see it persisted.",
                        f"Specialist can read and update that same {shared_entity_name.lower()} from a dedicated role surface.",
                        f"Manager can view the aggregated or current shared {shared_entity_name.lower()} state.",
                    ],
                    evidence=evidence,
                ),
                UserFlow(
                    flow_id="flow_profile_reachability",
                    name="Role profile reachability",
                    goal="Each role can open a real profile/settings page while staying inside the shared shell contract.",
                    steps=[
                        FlowStep(step_id="step_client_profile", order=1, actor_id="actor_client", action="Open the client profile page"),
                        FlowStep(step_id="step_specialist_profile", order=2, actor_id="actor_specialist", action="Open the specialist profile page"),
                        FlowStep(step_id="step_manager_profile", order=3, actor_id="actor_manager", action="Open the manager profile page"),
                    ],
                    postconditions=["Each role has a stable routed profile/settings surface."],
                    acceptance_criteria=["Every role root can navigate into a real profile page without leaving the shared shell."],
                    evidence=evidence,
                ),
            ],
            ui_requirements=[
                UIRequirement(req_id="ui_role_roots", category="screen", description="Provide a real root page for each role with live state and primary actions.", priority="must", evidence=evidence, screen_hint="role_root"),
                UIRequirement(req_id="ui_role_profiles", category="screen", description="Provide a real profile/settings page for each role.", priority="must", evidence=evidence, screen_hint="role_profile"),
                UIRequirement(req_id="ui_shared_flow", category="screen", description=f"Expose the shared {shared_entity_name.lower()} flow through routed pages, not placeholders.", priority="must", evidence=evidence, screen_hint="shared_flow"),
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
                    name=f"Create {shared_entity_name}",
                    method="POST",
                    path=f"/api/{shared_entity_name.lower()}s",
                    purpose=f"Persist workflow records and expose them across the role-specific surfaces for {shared_entity_name.lower()} work.",
                    request_fields=[APIField(name=field.name, type=field.type, required=field.required) for field in entity_attributes],
                    response_fields=[
                        APIField(name=f"{shared_entity_name.lower()}_id", type="uuid", required=True),
                        APIField(name="status", type="string", required=True),
                    ],
                    evidence=evidence,
                ),
                APIRequirement(
                    api_req_id="api_submission_list",
                    name=f"List {shared_entity_name}",
                    method="GET",
                    path=f"/api/{shared_entity_name.lower()}s",
                    purpose=f"Read persisted {shared_entity_name.lower()} data for client, specialist, and manager views.",
                    response_fields=[APIField(name="items", type="array", required=True)],
                    evidence=evidence,
                ),
                APIRequirement(
                    api_req_id="api_submission_update",
                    name=f"Update {shared_entity_name}",
                    method="PUT",
                    path=f"/api/{shared_entity_name.lower()}s/{{item_id}}",
                    purpose=f"Update persisted {shared_entity_name.lower()} state from specialist or manager actions.",
                    request_fields=[APIField(name="status", type="string", required=False)],
                    response_fields=[APIField(name="status", type="string", required=True)],
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
