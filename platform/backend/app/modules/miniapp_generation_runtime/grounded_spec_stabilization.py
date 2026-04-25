from __future__ import annotations

import re
from typing import Any

from app.models.grounded_spec import Actor, Assumption, EvidenceLink, FlowStep, GroundedSpecModel, Unknown, UserFlow
from app.modules.miniapp_generation_runtime.grounded_spec_hygiene import GroundedSpecHygieneRuntime


class GroundedSpecStabilizationRuntime(GroundedSpecHygieneRuntime):
    _GENERIC_RESOURCE_SLUGS = {"app", "data", "entity", "flow", "item", "miniapp", "page", "workflow", "workflows"}

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
                description="Works on assigned shared items and updates progress.",
                permissions_hint=["work_assigned_items"],
                evidence=evidence,
            )
        if "manager" not in role_names:
            actor_map["actor_manager"] = Actor(
                actor_id="actor_manager",
                name="Manager",
                role="manager",
                description="Oversees shared activity, workload distribution, and operational outcomes.",
                permissions_hint=["monitor_operations"],
                evidence=evidence,
            )
        if "client" not in role_names and "user" not in role_names:
            actor_map["actor_client"] = Actor(
                actor_id="actor_client",
                name="Client",
                role="client",
                description="Creates or reviews shared items and tracks progress.",
                permissions_hint=["create_item"],
                evidence=evidence,
            )
        return list(actor_map.values())

    def expand_role_flows(self, spec: GroundedSpecModel, actors: list[Actor]) -> list[UserFlow]:
        existing = list(spec.user_flows)
        flow_names = {flow.name.lower() for flow in existing}
        actor_by_role = {actor.role.lower(): actor for actor in actors}
        actor_by_role.setdefault("client", next((actor for actor in actors if actor.role.lower() == "user"), actors[0]))
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Expanded to linked three-role runtime flow without injecting a fixed workflow domain.")]
        entity_name = spec.domain_entities[0].name.replace("_", " ") if spec.domain_entities else "shared item"
        flow_label = entity_name.lower()
        primary_attributes = [attribute.name for attribute in spec.domain_entities[0].attributes] if spec.domain_entities else ["title", "details"]

        if not any("client" in name or "user" in name for name in flow_names):
            existing.insert(
                0,
                UserFlow(
                    flow_id="flow_client_record_creation",
                    name=f"Client creates {flow_label}",
                    goal=f"Allow a client to create or update a {flow_label} and receive confirmation.",
                    steps=[
                        FlowStep(step_id="step_client_open_form", order=1, actor_id=actor_by_role["client"].actor_id, action="Open the main creation form."),
                        FlowStep(step_id="step_client_fill_form", order=2, actor_id=actor_by_role["client"].actor_id, action="Complete the required fields.", input_data=primary_attributes),
                        FlowStep(step_id="step_client_submit_form", order=3, actor_id=actor_by_role["client"].actor_id, action="Submit the form and persist the shared item.", output_data=["item_id", "status"]),
                    ],
                    acceptance_criteria=["A new item is created or updated.", "The client sees a confirmation state."],
                    evidence=evidence,
                ),
            )

        if "specialist" in actor_by_role and not any("specialist" in name or "queue" in name for name in flow_names):
            existing.append(
                UserFlow(
                    flow_id="flow_specialist_processing",
                    name=f"Specialist processes {flow_label}",
                    goal=f"Let a specialist review and progress assigned {flow_label} items.",
                    steps=[
                        FlowStep(step_id="step_specialist_open_queue", order=1, actor_id=actor_by_role["specialist"].actor_id, action="Open the assigned work surface."),
                        FlowStep(step_id="step_specialist_claim_item", order=2, actor_id=actor_by_role["specialist"].actor_id, action="Claim or open the next actionable item.", output_data=["owner"]),
                        FlowStep(step_id="step_specialist_update_status", order=3, actor_id=actor_by_role["specialist"].actor_id, action="Move the shared item through its runtime statuses.", output_data=["status"]),
                    ],
                    acceptance_criteria=["The specialist can see assigned items.", "The specialist can update item status."],
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
                        FlowStep(step_id="step_manager_review_records", order=2, actor_id=actor_by_role["manager"].actor_id, action="Review shared items by status, owner, and completion stage."),
                        FlowStep(step_id="step_manager_rebalance", order=3, actor_id=actor_by_role["manager"].actor_id, action="Trigger balancing or refresh actions when workload distribution requires it."),
                    ],
                    acceptance_criteria=["The manager sees pipeline metrics.", "The manager can inspect and refresh operational items."],
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
                    text="Single-role prompts may be expanded into a linked client-specialist-manager runtime shell when the product still requires three preview roles.",
                    status="active",
                    rationale="The platform preview still expects three coordinated role surfaces, but the expanded flow should remain prompt-derived and domain-neutral.",
                    impact="medium",
                )
            )
        return assumptions
