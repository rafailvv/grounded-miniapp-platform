from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class ExistingAppMapReport(StrictModel):
    schema_: str = Field(default="grounded.existing_app_map.v1", alias="schema")
    status: str = "ready"
    workspace_id: str
    run_id: str
    source_dir: str = ""
    existing_app_map_ref: str
    role_pages: list[dict[str, Any]] = Field(default_factory=list)
    route_manifest: dict[str, Any] = Field(default_factory=dict)
    api_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    frontend_api_calls: list[dict[str, Any]] = Field(default_factory=list)
    persistence_models: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)
    known_proof_refs: dict[str, Any] = Field(default_factory=dict)
    prompt_contract_refs: list[str] = Field(default_factory=list)
    lsp_route_graph_ref: str | None = None
    created_at: str
    next_sequence: int = 1


class ImproveSlicePlan(StrictModel):
    schema_: str = Field(default="grounded.improve_slice_plan.v1", alias="schema")
    status: str = "planned"
    workspace_id: str
    run_id: str
    improve_slice_ref: str
    existing_app_map_ref: str
    requested_improvement: str
    connected_files: list[str] = Field(default_factory=list)
    protected_files: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    expected_behavioral_impact: list[str] = Field(default_factory=list)
    required_proof: list[str] = Field(default_factory=list)
    risk_level: str = "medium"
    blocked_reasons: list[str] = Field(default_factory=list)
    created_at: str
    next_sequence: int = 1


class ImproveModeReport(StrictModel):
    schema_: str = Field(default="grounded.improve_mode_report.v1", alias="schema")
    status: str = "ready"
    workspace_id: str
    run_id: str
    edit_mode: str = "improve"
    existing_app_map_ref: str
    improve_slice_ref: str
    map: dict[str, Any] = Field(default_factory=dict)
    slice: dict[str, Any] = Field(default_factory=dict)
    context_refs: dict[str, Any] = Field(default_factory=dict)
    run_refs: dict[str, Any] = Field(default_factory=dict)
    proof_refs: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    next_sequence: int = 1
