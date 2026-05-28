from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


PROMPT_CONTRACT_SCHEMA = "grounded.prompt_contract.v1"
PROMPT_CONTRACT_COMPILE_SCHEMA = "grounded.prompt_contract_compile.v1"


class PromptContractRequirement(StrictModel):
    requirement_id: str
    category: str
    text: str
    required: bool = True
    source: str = "prompt"
    refs: list[str] = Field(default_factory=list)


class PromptContractScenario(StrictModel):
    scenario_id: str
    title: str
    role: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    source: str = "prompt_contract"


class PromptContractSection(StrictModel):
    key: Literal["roles", "entities", "fields", "workflows", "screens", "api", "persistence", "visual", "acceptance"]
    status: Literal["planned", "blocked", "not_required"] = "planned"
    items: list[dict[str, Any]] = Field(default_factory=list)
    requirements: list[PromptContractRequirement] = Field(default_factory=list)


class PromptContract(StrictModel):
    schema_: str = Field(default=PROMPT_CONTRACT_SCHEMA, alias="schema")
    contract_id: str
    status: Literal["planned", "blocked", "not_required", "inherited"] = "planned"
    workspace_id: str
    run_id: str
    source_run_id: str | None = None
    prompt_summary: str = ""
    intent: str = ""
    generation_mode: str = ""
    analysis_source: str | None = None
    analysis_status: str | None = None
    roles: list[str] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    workflows: list[dict[str, Any]] = Field(default_factory=list)
    screens: list[dict[str, Any]] = Field(default_factory=list)
    api: dict[str, Any] = Field(default_factory=dict)
    persistence: dict[str, Any] = Field(default_factory=dict)
    visual_requirements: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_scenarios: list[PromptContractScenario] = Field(default_factory=list)
    sections: list[PromptContractSection] = Field(default_factory=list)
    acceptance_contract: dict[str, Any] = Field(default_factory=dict)
    implementation_plan: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)


class PromptContractCompileReport(StrictModel):
    schema_: str = Field(default=PROMPT_CONTRACT_COMPILE_SCHEMA, alias="schema")
    status: Literal["compiled", "blocked", "not_required", "inherited", "failed"] = "compiled"
    workspace_id: str
    run_id: str
    prompt_contract_ref: str
    acceptance_contract_ref: str | None = None
    miniapp_contract_ref: str | None = None
    contract_compile_ref: str | None = None
    analysis_source: str | None = None
    analysis_model: str | None = None
    blocking: bool = False
    issues: list[str] = Field(default_factory=list)
    next_sequence: int = 0

