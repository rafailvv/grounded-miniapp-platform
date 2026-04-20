from __future__ import annotations

import re
from typing import Any

from app.models.grounded_spec import (
    APIField,
    APIRequirement,
    Actor,
    Assumption,
    EvidenceLink,
    FlowStep,
    GroundedSpecModel,
    Unknown,
    UserFlow,
)


class GroundedSpecStabilizationRuntime:
    @staticmethod
    def _default_api_resource_slug(spec: GroundedSpecModel) -> str:
        candidates: list[str] = []
        if spec.domain_entities:
            candidates.append(str(spec.domain_entities[0].name or ""))
        candidates.extend(
            match.group(1)
            for match in re.finditer(
                r"\b(bookings?|requests?|submissions?|orders?|tasks?|appointments?|tickets?|records?|cases?|items?)\b",
                str(spec.product_goal or "").lower(),
            )
        )
        for candidate in candidates:
            normalized = re.sub(r"[^a-z0-9]+", "_", candidate.lower()).strip("_")
            if not normalized or normalized in {"data", "page", "flow", "miniapp", "app"}:
                continue
            if not normalized.endswith("s"):
                normalized = f"{normalized}s"
            return normalized
        return "records"

    def stabilize_grounded_spec(self, spec: GroundedSpecModel) -> GroundedSpecModel:
        product_goal = str(spec.product_goal or "").strip()
        if self.is_forbidden_spec_governance_text(product_goal):
            product_goal = re.sub(
                r"\b(auth(?:entication)?|login|sign in|session|token|websocket|realtime|push|webhook|initdata)\b",
                "",
                product_goal,
                flags=re.IGNORECASE,
            )
            product_goal = re.sub(r"\s{2,}", " ", product_goal).strip(" ,.;:-")
        assumptions = [
            assumption
            for assumption in spec.assumptions
            if not self.is_forbidden_spec_governance_text(
                " ".join(part for part in (assumption.text, assumption.rationale) if part)
            )
        ]
        unresolved_unknowns: list[Unknown] = []
        contradictions = [
            contradiction
            for contradiction in spec.contradictions
            if not self.is_forbidden_spec_governance_text(
                " ".join(
                    part
                    for part in (
                        contradiction.description,
                        contradiction.left_side,
                        contradiction.right_side,
                        contradiction.resolution_hint,
                    )
                    if part
                )
            )
        ]

        for unknown in spec.unknowns:
            if self.is_forbidden_spec_governance_text(
                " ".join(part for part in (unknown.question, unknown.suggested_resolution) if part)
            ):
                continue
            question = unknown.question.lower()
            suggested_resolution = unknown.suggested_resolution or "Resolved through canonical template defaults."
            if any(
                marker in question
                for marker in (
                    "optional",
                    "required",
                    "endpoint",
                    "api",
                    "miniapp",
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

        api_requirements = [
            item
            for item in spec.api_requirements
            if not self.is_forbidden_generated_api_requirement(item)
        ]
        if not api_requirements and any(term in spec.product_goal.lower() for term in ("booking", "consultation", "form", "request")):
            resource_slug = self._default_api_resource_slug(spec)
            resource_path = f"/api/{resource_slug.replace('_', '-')}"
            evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Synthesized from prompt intent and canonical runtime defaults.")]
            api_requirements.extend(
                [
                    APIRequirement(
                        api_req_id="api_submit_primary_form",
                        name="Submit primary workflow record",
                        method="POST",
                        path=resource_path,
                        purpose="Persist the primary end-user workflow record in the generated mini-app.",
                        request_fields=[
                            APIField(name="name", type="string", required=True, description="End-user display name"),
                            APIField(name="phone", type="phone", required=True, description="End-user phone number"),
                            APIField(name="preferred_date", type="datetime", required=True, description="Requested consultation date"),
                            APIField(name="comment", type="text", required=False, description="Additional request comment"),
                        ],
                        response_fields=[
                            APIField(name="record_id", type="uuid", required=True, description="Created workflow record identifier"),
                            APIField(name="status", type="string", required=True, description="Current workflow status"),
                        ],
                        auth_required=False,
                        existing_in_template=False,
                        evidence=evidence,
                    ),
                    APIRequirement(
                        api_req_id="api_list_primary_requests",
                        name="List workflow records",
                        method="GET",
                        path=resource_path,
                        purpose="Load current user records and role queues in the generated runtime.",
                        request_fields=[],
                        response_fields=[
                            APIField(name="items", type="array", required=True, description="Runtime workflow records"),
                        ],
                        auth_required=False,
                        existing_in_template=False,
                        evidence=evidence,
                    ),
                ]
            )
            assumptions.append(
                Assumption(
                    assumption_id="assume_generated_workflow_api",
                    text=f"The generated miniapp exposes a default primary workflow API under {resource_path}.",
                    status="active",
                    rationale="Simple workflow prompts should compile into a usable end-to-end demo without blocking on undocumented project-specific endpoint names.",
                    impact="medium",
                )
            )

        actors = self.expand_role_actors(spec.actors, spec.doc_refs)
        user_flows = self.expand_role_flows(spec, actors)
        assumptions = self.ensure_role_expansion_assumption(spec, assumptions, actors)

        return spec.model_copy(
            update={
                "product_goal": product_goal or spec.product_goal,
                "actors": actors,
                "user_flows": user_flows,
                "assumptions": assumptions,
                "unknowns": unresolved_unknowns,
                "contradictions": contradictions,
                "api_requirements": api_requirements,
            }
        )

    def expand_role_actors(self, actors: list[Actor], doc_refs: list[Any]) -> list[Actor]:
        del doc_refs
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

    def expand_role_flows(self, spec: GroundedSpecModel, actors: list[Actor]) -> list[UserFlow]:
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

    @staticmethod
    def ensure_role_expansion_assumption(
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
