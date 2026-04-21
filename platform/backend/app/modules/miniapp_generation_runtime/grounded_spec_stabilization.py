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
from app.modules.miniapp_generation_runtime.grounded_spec_hygiene import GroundedSpecHygieneRuntime


class GroundedSpecStabilizationRuntime:
    _GENERIC_RESOURCE_SLUGS = {"app", "data", "flow", "miniapp", "page", "record", "workflow", "workflows"}

    @staticmethod
    def _slugify(value: str) -> str:
        humanized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        return re.sub(r"[^a-z0-9]+", "_", humanized.lower()).strip("_")

    @classmethod
    def _singularize_slug(cls, value: str) -> str:
        slug = cls._slugify(value)
        if slug.endswith("ies") and len(slug) > 3:
            return f"{slug[:-3]}y"
        if slug.endswith("s") and not slug.endswith("ss") and len(slug) > 3:
            return slug[:-1]
        return slug

    @classmethod
    def _pluralize_slug(cls, value: str) -> str:
        slug = cls._singularize_slug(value)
        if not slug:
            return "records"
        if slug.endswith("y") and len(slug) > 1 and slug[-2] not in "aeiou":
            return f"{slug[:-1]}ies"
        if slug.endswith(("s", "x", "z", "ch", "sh")):
            return f"{slug}es"
        return f"{slug}s"

    @classmethod
    def _focus_resource_slug(cls, value: str) -> str:
        parts = [part for part in re.split(r"[^a-z0-9]+", str(value or "").lower()) if part]
        if len(parts) > 1:
            for candidate in reversed(parts):
                normalized = cls._singularize_slug(candidate)
                if normalized and normalized not in cls._GENERIC_RESOURCE_SLUGS:
                    return normalized
        return cls._singularize_slug(value)

    @classmethod
    def _default_api_resource_slug(cls, spec: GroundedSpecModel) -> str:
        candidates: list[str] = []
        if spec.domain_entities:
            candidates.append(str(spec.domain_entities[0].name or ""))
        prompt_entity = GroundedSpecHygieneRuntime.infer_entity_name(str(spec.product_goal or ""))
        if prompt_entity:
            candidates.append(prompt_entity)
        for candidate in candidates:
            singular = cls._focus_resource_slug(candidate)
            if not singular or singular in cls._GENERIC_RESOURCE_SLUGS:
                continue
            return cls._pluralize_slug(singular)
        return "records"

    @staticmethod
    def _default_api_request_fields(spec: GroundedSpecModel) -> list[APIField]:
        if spec.domain_entities and spec.domain_entities[0].attributes:
            return [
                APIField(
                    name=attribute.name,
                    type=attribute.type,
                    required=attribute.required,
                    description=attribute.description or f"Primary {attribute.name.replace('_', ' ')} value",
                )
                for attribute in spec.domain_entities[0].attributes[:6]
            ]
        return [
            APIField(name="title", type="string", required=True, description="Primary record title"),
            APIField(name="details", type="text", required=False, description="Additional record details"),
        ]

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
        if not api_requirements and (spec.domain_entities or spec.persistence_requirements or spec.user_flows):
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
                        request_fields=self._default_api_request_fields(spec),
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
                        rationale="Prompt-derived workflows should compile into a usable end-to-end demo without blocking on undocumented project-specific endpoint names.",
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
                description="Processes incoming records created by end-users and updates workflow status.",
                permissions_hint=["process_records"],
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
                description="Creates a new record and tracks its progress.",
                permissions_hint=["create_record"],
                evidence=evidence,
            )
        return list(actor_map.values())

    def expand_role_flows(self, spec: GroundedSpecModel, actors: list[Actor]) -> list[UserFlow]:
        existing = list(spec.user_flows)
        flow_names = {flow.name.lower() for flow in existing}
        actor_by_role = {actor.role.lower(): actor for actor in actors}
        actor_by_role.setdefault("client", next((actor for actor in actors if actor.role.lower() == "user"), actors[0]))
        evidence = [EvidenceLink(doc_ref_id="prompt-source", evidence_type="derived", note="Expanded to linked three-role runtime flow.")]
        entity_name = spec.domain_entities[0].name.replace("_", " ") if spec.domain_entities else "record"
        flow_label = entity_name.lower()
        primary_attributes = [attribute.name for attribute in spec.domain_entities[0].attributes] if spec.domain_entities else ["title", "details"]

        if not any("client" in name or "user" in name for name in flow_names):
            existing.insert(
                0,
                UserFlow(
                    flow_id="flow_client_record_creation",
                    name=f"Client creates {flow_label}",
                    goal=f"Allow a client to create a new {flow_label} and receive confirmation.",
                    steps=[
                        FlowStep(step_id="step_client_open_form", order=1, actor_id=actor_by_role["client"].actor_id, action="Open the main creation form."),
                        FlowStep(step_id="step_client_fill_form", order=2, actor_id=actor_by_role["client"].actor_id, action="Complete the required fields.", input_data=primary_attributes),
                        FlowStep(step_id="step_client_submit_form", order=3, actor_id=actor_by_role["client"].actor_id, action="Submit the form to create a new record.", output_data=["record_id", "status"]),
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
