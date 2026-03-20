from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import Impact, PreviewProfile, Priority, Severity, StrictModel, TargetPlatform


class Metadata(StrictModel):
    workspace_id: str
    conversation_id: str
    prompt_turn_id: str
    template_revision_id: str
    language: str | None = None
    created_at: datetime | None = None


class DocRef(StrictModel):
    doc_ref_id: str
    source_type: Literal[
        "project_doc",
        "openapi",
        "codebase",
        "platform_doc",
        "user_prompt",
        "assumption",
    ]
    file_path: str
    chunk_id: str
    section_title: str | None = None
    snippet: str | None = None
    relevance: float


class EvidenceLink(StrictModel):
    doc_ref_id: str
    evidence_type: Literal["explicit", "derived", "cross_checked"]
    note: str | None = None


class Actor(StrictModel):
    actor_id: str
    name: str
    role: str
    description: str
    permissions_hint: list[str] = Field(default_factory=list)
    evidence: list[EvidenceLink]


class EntityAttribute(StrictModel):
    name: str
    type: Literal[
        "string",
        "text",
        "int",
        "float",
        "bool",
        "date",
        "datetime",
        "phone",
        "email",
        "enum",
        "object",
        "array",
        "uuid",
    ]
    required: bool
    description: str | None = None
    pii: bool = False


class DomainEntity(StrictModel):
    entity_id: str
    name: str
    description: str
    attributes: list[EntityAttribute]
    evidence: list[EvidenceLink]


class FlowStep(StrictModel):
    step_id: str
    order: int
    actor_id: str
    action: str
    expected_system_response: str | None = None
    input_data: list[str] = Field(default_factory=list)
    output_data: list[str] = Field(default_factory=list)


class UserFlow(StrictModel):
    flow_id: str
    name: str
    goal: str
    steps: list[FlowStep]
    acceptance_criteria: list[str]
    evidence: list[EvidenceLink]
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    error_paths: list[str] = Field(default_factory=list)


class UIRequirement(StrictModel):
    req_id: str
    category: Literal[
        "screen",
        "component",
        "form",
        "navigation",
        "theme",
        "responsive",
        "feedback",
        "validation",
    ]
    description: str
    priority: Priority
    evidence: list[EvidenceLink]
    screen_hint: str | None = None


class APIField(StrictModel):
    name: str
    type: str
    required: bool
    description: str | None = None


class APIRequirement(StrictModel):
    api_req_id: str
    name: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    purpose: str
    request_fields: list[APIField] = Field(default_factory=list)
    response_fields: list[APIField] = Field(default_factory=list)
    evidence: list[EvidenceLink]
    auth_required: bool = False
    existing_in_template: bool = False


class PersistenceRequirement(StrictModel):
    persistence_req_id: str
    entity_id: str
    operation: Literal["create", "read", "update", "delete", "list"]
    storage_type: Literal["sqlite", "postgres", "redis", "memory", "external"]
    evidence: list[EvidenceLink]
    retention_policy: str | None = None


class IntegrationRequirement(StrictModel):
    integration_req_id: str
    system_name: str
    direction: Literal["inbound", "outbound", "bidirectional"]
    purpose: str
    evidence: list[EvidenceLink]
    auth_type: Literal["none", "api_key", "bearer", "oauth2", "telegram_initdata", "custom"]
    contract_ref: str | None = None


class SecurityRequirement(StrictModel):
    security_req_id: str
    category: Literal[
        "auth",
        "input_validation",
        "pii",
        "secret_management",
        "telegram_initdata",
        "access_control",
        "logging",
        "rate_limit",
    ]
    rule: str
    severity: Severity
    evidence: list[EvidenceLink]


class PlatformConstraint(StrictModel):
    constraint_id: str
    category: Literal[
        "sdk",
        "theme",
        "viewport",
        "navigation",
        "button_api",
        "launch_context",
        "storage",
        "security",
    ]
    rule: str
    severity: Severity
    evidence: list[EvidenceLink]


class NonFunctionalRequirement(StrictModel):
    nfr_id: str
    category: Literal[
        "performance",
        "maintainability",
        "observability",
        "portability",
        "testability",
        "usability",
    ]
    description: str
    priority: Priority
    evidence: list[EvidenceLink]


class Assumption(StrictModel):
    assumption_id: str
    text: str
    status: Literal["active", "confirmed", "rejected"]
    rationale: str
    impact: Impact = "medium"


class Unknown(StrictModel):
    unknown_id: str
    question: str
    impact: Impact
    suggested_resolution: str | None = None


class Contradiction(StrictModel):
    contradiction_id: str
    description: str
    left_side: str
    right_side: str
    severity: Severity
    resolution_hint: str | None = None


class GroundedSpecModel(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    metadata: Metadata
    target_platform: TargetPlatform
    preview_profile: PreviewProfile
    product_goal: str
    actors: list[Actor]
    domain_entities: list[DomainEntity]
    user_flows: list[UserFlow]
    ui_requirements: list[UIRequirement]
    api_requirements: list[APIRequirement]
    persistence_requirements: list[PersistenceRequirement]
    integration_requirements: list[IntegrationRequirement]
    security_requirements: list[SecurityRequirement]
    platform_constraints: list[PlatformConstraint]
    non_functional_requirements: list[NonFunctionalRequirement]
    assumptions: list[Assumption]
    unknowns: list[Unknown]
    contradictions: list[Contradiction]
    doc_refs: list[DocRef]
