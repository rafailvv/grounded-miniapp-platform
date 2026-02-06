from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import PreviewProfile, StrictModel, TargetPlatform


class IRMetadata(StrictModel):
    workspace_id: str
    grounded_spec_version: str
    template_revision_id: str
    generated_at: datetime | None = None


class DataField(StrictModel):
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
    default: Any | None = None
    enum_values: list[str] = Field(default_factory=list)
    pii: bool = False


class Variable(StrictModel):
    variable_id: str
    name: str
    type: DataField.model_fields["type"].annotation
    required: bool
    source: Literal[
        "user_input",
        "api_response",
        "derived",
        "validated_init_data",
        "constant",
        "session_state",
    ]
    trust_level: Literal["untrusted", "validated", "trusted"]
    scope: Literal["screen", "flow", "session", "persistent"]
    default: Any | None = None
    pii: bool = False


class Entity(StrictModel):
    entity_id: str
    name: str
    fields: list[DataField]


class ValidatorRule(StrictModel):
    rule_type: Literal[
        "required",
        "min_length",
        "max_length",
        "regex",
        "email",
        "phone",
        "date_not_past",
        "custom",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class Condition(StrictModel):
    expression: str
    referenced_variables: list[str] = Field(default_factory=list)


class Component(StrictModel):
    component_id: str
    type: Literal[
        "text",
        "input",
        "textarea",
        "select",
        "checkbox",
        "radio",
        "date_picker",
        "phone_input",
        "email_input",
        "button",
        "card",
        "list",
        "banner",
    ]
    label: str
    binding_variable_id: str
    required: bool
    validators: list[ValidatorRule]
    placeholder: str | None = None
    visibility_condition: Condition | None = None
    disabled_condition: Condition | None = None


class Assignment(StrictModel):
    target_variable_id: str
    expression: str


class Action(StrictModel):
    action_id: str
    type: Literal[
        "navigate",
        "submit_form",
        "call_api",
        "set_variable",
        "branch",
        "show_message",
        "close_app",
    ]
    source_component_id: str | None = None
    input_variable_ids: list[str] = Field(default_factory=list)
    target_screen_id: str | None = None
    integration_id: str | None = None
    assignments: list[Assignment] = Field(default_factory=list)
    success_transition_id: str | None = None
    error_transition_id: str | None = None
    condition: Condition | None = None


class PlatformHints(StrictModel):
    use_main_button: bool | None = None
    use_back_button: bool | None = None
    respect_theme: bool | None = None
    respect_viewport: bool | None = None


class Screen(StrictModel):
    screen_id: str
    kind: Literal[
        "landing",
        "form",
        "list",
        "details",
        "confirm",
        "success",
        "error",
        "info",
    ]
    title: str
    components: list[Component]
    actions: list[Action]
    subtitle: str | None = None
    on_enter_actions: list[str] = Field(default_factory=list)
    platform_hints: PlatformHints | None = None


class Transition(StrictModel):
    transition_id: str
    from_screen_id: str
    to_screen_id: str
    trigger: str
    condition: Condition | None = None


class Integration(StrictModel):
    integration_id: str
    name: str
    type: Literal["rest", "webhook"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    request_schema: list[DataField]
    response_schema: list[DataField]
    auth_type: Literal["none", "service_token", "bearer", "telegram_initdata", "custom"]
    timeout_ms: int = 5000


class StorageBinding(StrictModel):
    binding_id: str
    entity_id: str
    storage_type: Literal["postgres", "redis", "memory", "external"]
    table_or_collection: str


class AuthModel(StrictModel):
    mode: Literal["public", "telegram_session", "custom"]
    telegram_initdata_validation_required: bool
    server_side_session: bool = True


class Permission(StrictModel):
    permission_id: str
    name: str
    description: str


class SecurityPolicy(StrictModel):
    trusted_sources: list[str]
    untrusted_sources: list[str]
    secret_handling: Literal["server_only", "server_env_only"]
    pii_variables: list[str] = Field(default_factory=list)


class TelemetryHook(StrictModel):
    event_name: str
    trigger_type: Literal["screen_view", "button_click", "form_submit", "api_result"]
    screen_id: str | None = None
    action_id: str | None = None


class IRAssumption(StrictModel):
    assumption_id: str
    text: str
    origin: Literal["grounded_spec", "repair_step", "compiler_default"]


class OpenQuestion(StrictModel):
    question_id: str
    text: str
    blocking: bool


class TraceabilityLink(StrictModel):
    trace_id: str
    target_type: Literal[
        "variable",
        "entity",
        "screen",
        "component",
        "action",
        "transition",
        "integration",
    ]
    target_id: str
    source_kind: Literal["doc_ref", "prompt_fragment", "assumption", "repair"]
    source_ref: str
    mapping_note: str | None = None


class AppIRModel(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    metadata: IRMetadata
    app_id: str
    title: str
    platform: TargetPlatform
    preview_profile: PreviewProfile
    entry_screen_id: str
    variables: list[Variable]
    entities: list[Entity]
    screens: list[Screen]
    transitions: list[Transition]
    integrations: list[Integration]
    storage_bindings: list[StorageBinding]
    auth_model: AuthModel
    permissions: list[Permission]
    security: SecurityPolicy
    telemetry_hooks: list[TelemetryHook]
    assumptions: list[IRAssumption]
    open_questions: list[OpenQuestion]
    traceability: list[TraceabilityLink]
    terminal_screen_ids: list[str] = Field(default_factory=list)

