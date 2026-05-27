from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.models.common import StrictModel


HookSource = Literal["builtin", "project", "workspace"]
HookActionKind = Literal["block", "add_context", "tag", "request_permission"]
HookName = Literal[
    "session_start",
    "user_prompt_submit",
    "before_run",
    "after_run",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "permission_request",
    "stop",
    "before_apply",
    "after_apply",
    "before_checks",
    "after_checks",
    "on_check_failed",
    "pre_apply_patch",
    "post_apply_patch",
    "post_browser_verify",
    "after_gate",
    "on_memory_update",
    "on_export",
]


class HookApiModel(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)


class HookValidationIssue(HookApiModel):
    source: HookSource | str
    rule_id: str | None = None
    code: str
    message: str
    blocking: bool = False
    path: str | None = None


class HookCondition(HookApiModel):
    hook: str | list[str] | None = None
    tool: str | list[str] | None = None
    canonical_tool: str | list[str] | None = None
    risk: str | list[str] | None = None
    mode: str | list[str] | None = None
    path_globs: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    check_names: list[str] = Field(default_factory=list)
    check_statuses: list[str] = Field(default_factory=list)
    worker_id: str | list[str] | None = None
    run_intent: str | list[str] | None = None
    generation_mode: str | list[str] | None = None


class HookAction(HookApiModel):
    kind: HookActionKind = Field(alias="action")
    reason: str | None = None
    text: str | None = None
    priority: int = 0
    target: str = "next_turn"
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookRule(HookApiModel):
    rule_id: str
    source: HookSource = "workspace"
    enabled: bool = True
    description: str | None = None
    priority: int = 0
    conditions: HookCondition = Field(default_factory=HookCondition)
    actions: list[HookAction] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookPolicy(HookApiModel):
    schema_: str = Field(default="grounded.hook_policy.v1", alias="schema")
    policy_id: str = "default"
    source: HookSource = "workspace"
    enabled: bool = True
    rules: list[HookRule] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookContext(HookApiModel):
    schema_: str = Field(default="grounded.hook_context.v1", alias="schema")
    hook: str
    workspace_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class HookContextItem(HookApiModel):
    text: str
    priority: int = 0
    target: str = "next_turn"
    source: HookSource | str = "workspace"
    source_rule_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookEvaluation(HookApiModel):
    schema_: str = Field(default="grounded.hook_evaluation.v1", alias="schema")
    trace_id: str
    hook: str
    workspace_id: str | None = None
    run_id: str | None = None
    should_block: bool = False
    block_reason: str | None = None
    added_contexts: list[HookContextItem] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    validation_issues: list[HookValidationIssue] = Field(default_factory=list)


class HookRuntimePayload(HookApiModel):
    schema_: str = Field(default="grounded.hook_payload.v1", alias="schema")
    hook: str
    workspace_id: str | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class HookOutput(HookApiModel):
    schema_: str = Field(default="grounded.hook_output.v1", alias="schema")
    should_block: bool = False
    block_reason: str | None = None
    added_contexts: list[HookContextItem] = Field(default_factory=list)
    tags: dict[str, Any] = Field(default_factory=dict)
    permission_request: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookTrace(HookApiModel):
    schema_: str = Field(default="grounded.hook_trace.v1", alias="schema")
    run_id: str
    side_effects_allowed: bool = False
    event_count: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    context_count: int = 0
    blocked_count: int = 0
    validation_issues: list[HookValidationIssue] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
