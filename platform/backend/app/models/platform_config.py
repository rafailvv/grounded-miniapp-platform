from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


class CompletionBudgetConfig(StrictModel):
    time_limit_ms: int = Field(ge=1)
    token_limit: int = Field(ge=1)
    turn_budget_cap: int = Field(ge=1)


class GenerationModeConfig(StrictModel):
    enabled: bool = True
    label: str
    objective: str
    required_checks: list[str] = Field(default_factory=list)
    optional_checks: list[str] = Field(default_factory=list)
    proof_requirements: list[str] = Field(default_factory=list)
    final_gate: list[str] = Field(default_factory=list)
    context_policy: str = "standard"
    worker_policy: str = "serial"
    max_repair_attempts: int = Field(default=1, ge=0)
    audit_level: Literal["none", "light", "standard", "deep", "release"] = "light"
    output_style: str = "concise"
    completion_budget: CompletionBudgetConfig


class CheckConfig(StrictModel):
    label: str
    category: Literal["static", "api", "browser", "tests", "visual", "security", "docs", "export", "observability", "audit"]
    blocking: bool = True
    proof_kind: str = ""
    required_for_modes: list[str] = Field(default_factory=list)
    diagnostic_contract: list[str] = Field(default_factory=list)
    repair_hint: str = ""


class ModelProviderConfig(StrictModel):
    label: str = ""
    enabled_env: str
    base_url_env: str = ""
    default_base_url: str = ""
    models: list[str] = Field(default_factory=list)


class ModelCapabilityConfig(StrictModel):
    provider: str = "openai"
    context_window: int = Field(default=128000, ge=1)
    supports_tools: bool = True
    supports_structured_output: bool = True
    supports_reasoning: bool = False
    cost_tier: str = "unknown"
    roles: list[str] = Field(default_factory=list)


class ModelProfileConfig(StrictModel):
    label: str
    provider: str = "openai"
    description: str = ""
    routing: dict[str, str] = Field(default_factory=dict)
    default: bool = False
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class SkillRootConfig(StrictModel):
    system: bool = True
    repo: bool = True
    plugin: bool = True
    user: bool = True


class SkillActivationConfig(StrictModel):
    enabled: bool = True
    roots: SkillRootConfig = Field(default_factory=SkillRootConfig)
    max_selected_by_mode: dict[str, int] = Field(default_factory=dict)
    required_skills_by_mode: dict[str, list[str]] = Field(default_factory=dict)
    activation_budget_by_mode: dict[str, dict[str, int]] = Field(default_factory=dict)


class BrowserViewportConfig(StrictModel):
    width: int = Field(default=390, ge=1)
    height: int = Field(default=844, ge=1)


class BrowserProofConfig(StrictModel):
    enabled: bool = True
    required_modes: list[str] = Field(default_factory=list)
    screenshot_modes: list[str] = Field(default_factory=list)
    require_ui_steps_modes: list[str] = Field(default_factory=list)
    require_persisted_marker_modes: list[str] = Field(default_factory=list)
    replay_artifacts: bool = True
    default_mobile_viewport: BrowserViewportConfig = Field(default_factory=BrowserViewportConfig)


class SlaConfig(StrictModel):
    default_mode: str = "balanced"
    full_audit_modes: list[str] = Field(default_factory=list)
    visual_snapshot_modes: list[str] = Field(default_factory=list)
    quality_like_modes: list[str] = Field(default_factory=list)
    second_queue: list[dict[str, Any]] = Field(default_factory=list)
    compatibility: dict[str, Any] = Field(default_factory=dict)


class PlatformConfig(StrictModel):
    schema_: str = Field(default="grounded.platform_config.v1", alias="schema")
    version: int = 1
    generation_modes: dict[str, GenerationModeConfig]
    checks: dict[str, CheckConfig]
    providers: dict[str, ModelProviderConfig]
    model_capabilities: dict[str, ModelCapabilityConfig]
    model_profiles: dict[str, ModelProfileConfig]
    default_profile_by_mode: dict[str, str]
    skill_activation: SkillActivationConfig
    browser_proof: BrowserProofConfig
    sla: SlaConfig
