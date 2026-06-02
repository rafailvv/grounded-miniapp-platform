from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.models.common import StrictModel


SkillScope = Literal["system", "repo", "plugin", "user"]
SkillInvocationPolicy = Literal["always", "auto", "explicit", "disabled"]


class SkillApiModel(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)


class SkillDependency(SkillApiModel):
    id: str
    optional: bool = False


class SkillFrontmatter(SkillApiModel):
    metadata_schema: str = "grounded.skill.v2"
    description: str | None = None
    whenToUse: list[str] = Field(default_factory=list)
    trigger_rules: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    allowedTools: list[str] = Field(default_factory=list)
    model: str = ""
    effort: str = ""
    validation: list[str] = Field(default_factory=list)
    required_proof: list[str] = Field(default_factory=list)
    incompatible_skills: list[str] = Field(default_factory=list)
    output_expectations: list[str] = Field(default_factory=list)
    dependencies: list[SkillDependency] = Field(default_factory=list)
    invocationPolicy: SkillInvocationPolicy | None = None


class SkillValidationIssue(SkillApiModel):
    code: str
    message: str
    skill_id: str | None = None
    scoped_id: str | None = None
    scope: SkillScope | str | None = None
    source: str | None = None
    blocking: bool = False


class SkillDefinition(SkillApiModel):
    id: str
    scoped_id: str
    scope: SkillScope
    name: str
    source: str
    activation: str = "skill_match"
    invocationPolicy: SkillInvocationPolicy = "explicit"
    metadata_schema: str = "grounded.skill.v2"
    whenToUse: list[str] = Field(default_factory=list)
    trigger_rules: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    allowedTools: list[str] = Field(default_factory=list)
    model: str = ""
    effort: str = ""
    validation: list[str] = Field(default_factory=list)
    validation_hints: list[str] = Field(default_factory=list)
    required_proof: list[str] = Field(default_factory=list)
    incompatible_skills: list[str] = Field(default_factory=list)
    output_expectations: list[str] = Field(default_factory=list)
    dependencies: list[SkillDependency] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    body: str = ""
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    plugin_id: str | None = None
    mtime_ns: int = 0
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillSelection(SkillDefinition):
    activation_reason: str = ""
    activation_score: int = 0
    explicit: bool = False
    dependency: bool = False
    body_budget_chars: int = 0
    ranking: dict[str, Any] = Field(default_factory=dict)


class SkillRegistryManifest(SkillApiModel):
    schema_: str = Field(default="grounded.skill_registry_manifest.v1", alias="schema")
    status: str = "ready"
    roots: dict[str, str] = Field(default_factory=dict)
    scopes: dict[str, int] = Field(default_factory=dict)
    signature: str = ""
    cache: dict[str, Any] = Field(default_factory=dict)
    validation_issues: list[SkillValidationIssue] = Field(default_factory=list)
    created_at: str
