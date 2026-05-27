from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.services.tool_protocol import (
    canonical_tool_name,
    model_visible_tool_names,
    registry_tool_names,
    tool_definition,
    tool_protocol_spec,
)


AgentToolKind = Literal["read_only", "mutating", "verification", "unknown"]
AgentActivityKind = Literal[
    "planning",
    "reading",
    "searching",
    "running_command",
    "editing",
    "applying_patch",
    "checking",
    "browser_verifying",
    "repairing",
    "compacting",
    "compact_boundary",
    "tool_progress",
    "tool_use_summary",
    "process_started",
    "command_output_delta",
    "process_completed",
    "process_failed",
    "context_suggestion",
    "hook_started",
    "hook_completed",
    "verifier_nudge",
    "worker_started",
    "worker_completed",
    "worker_failed",
    "completed",
]

MODEL_INJECTED_FIELDS = {"workspace_id", "run_id"}
MUTATING_FILE_FIELD_DESCRIPTIONS = {
    "file_path": "One app-owned miniapp/... file. Do not target generated/platform-owned files such as app/main.py, app/routes/role_pages.py, app/routes/role_routes.py, or app/generated/*.json.",
    "diff": "A unified diff for this one file. It must include @@ hunks with context and +/- lines.",
    "content": "The complete resulting file content for this one file.",
    "old_string": "Exact text currently present in the file. It must match exactly and uniquely unless replace_all is true.",
    "new_string": "Replacement text.",
}
DEFAULT_MODEL_REQUIRED = ("reason",)
MODEL_REQUIRED_OVERRIDES: dict[str, tuple[str, ...]] = {
    "apply_patch_to_draft": ("file_path", "diff", "reason"),
    "write_file": ("file_path", "content", "reason"),
    "edit_file_exact": ("file_path", "old_string", "new_string", "reason"),
    "tool_search": ("query", "reason"),
}


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    kind: AgentToolKind
    concurrency_safe: bool
    timeout_seconds: int = 25
    output_cap_chars: int = 6000
    activity: AgentActivityKind = "reading"
    progress_label: str = "Reading workspace context"
    aliases: tuple[str, ...] = ()
    mode_visibility: tuple[str, ...] = ("default", "read_only", "mutation_required", "verification", "worker_branch")
    dynamic: bool = False
    deferred: bool = False
    canonical: str = ""
    parallel_safe: bool = False
    model_visible: bool = False
    internal_only: bool = False
    argument_progress_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentToolBatch:
    concurrency_safe: bool
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tools(self) -> list[str]:
        return [str(item.get("tool") or "") for item in self.requests]


@dataclass(frozen=True)
class AgentToolBatchPlan:
    read_only_requests: list[dict[str, Any]] = field(default_factory=list)
    mutating_requests: list[dict[str, Any]] = field(default_factory=list)
    verification_requests: list[dict[str, Any]] = field(default_factory=list)
    unknown_requests: list[dict[str, Any]] = field(default_factory=list)
    ordered_batches: list[AgentToolBatch] = field(default_factory=list)

    @property
    def has_mutations(self) -> bool:
        return bool(self.mutating_requests)


class AgentToolRegistry:
    """Compatibility facade over the canonical tool protocol registry."""

    @classmethod
    def names(cls) -> set[str]:
        return registry_tool_names()

    @classmethod
    def spec(cls, tool_name: object) -> AgentToolSpec | None:
        name = str(tool_name or "").strip().lower()
        if not name or name not in cls.names():
            return None
        canonical = canonical_tool_name(name)
        definition = tool_definition(canonical)
        return AgentToolSpec(
            name=name,
            canonical=definition.canonical,
            kind=definition.kind,
            concurrency_safe=definition.concurrency_safe,
            timeout_seconds=definition.timeout_seconds,
            output_cap_chars=definition.output_cap_chars,
            activity=definition.activity,  # type: ignore[arg-type]
            progress_label=definition.progress_label,
            aliases=definition.aliases,
            mode_visibility=definition.mode_visibility,
            dynamic=definition.dynamic,
            deferred=definition.deferred,
            parallel_safe=definition.parallel_safe,
            model_visible=definition.model_visible,
            internal_only=definition.internal_only,
            argument_progress_fields=definition.argument_progress_fields,
        )

    @classmethod
    def kind(cls, tool_name: object) -> AgentToolKind:
        spec = cls.spec(tool_name)
        return spec.kind if spec is not None else "unknown"

    @classmethod
    def is_model_read_only(cls, tool_name: object) -> bool:
        return cls.kind(tool_name) in {"read_only", "verification"}

    @classmethod
    def plan_batches(cls, tool_calls: list[dict[str, Any]]) -> AgentToolBatchPlan:
        read_only: list[dict[str, Any]] = []
        mutating: list[dict[str, Any]] = []
        verification: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        batches: list[AgentToolBatch] = []

        def append_batch(request_item: dict[str, Any], *, concurrency_safe: bool) -> None:
            if concurrency_safe and batches and batches[-1].concurrency_safe:
                batches[-1].requests.append(request_item)
                return
            batches.append(AgentToolBatch(concurrency_safe=concurrency_safe, requests=[request_item]))

        for request in tool_calls:
            if not isinstance(request, dict):
                continue
            spec = cls.spec(request.get("tool"))
            if spec is None:
                unknown.append(request)
                append_batch(request, concurrency_safe=False)
                continue
            contract = tool_protocol_spec(canonical_tool_name(request.get("tool"))).as_contract()
            parallel_safe = bool(contract.get("parallel_safe")) and str(contract.get("side_effect_class") or "") in {"none", "read_workspace"}
            if spec.kind == "mutating":
                mutating.append(request)
            elif spec.kind == "verification":
                verification.append(request)
                read_only.append(request)
            else:
                read_only.append(request)
            append_batch(request, concurrency_safe=parallel_safe and spec.kind == "read_only")

        return AgentToolBatchPlan(
            read_only_requests=read_only,
            mutating_requests=mutating,
            verification_requests=verification,
            unknown_requests=unknown,
            ordered_batches=batches,
        )

    @classmethod
    def openai_tools(cls, allowed_names: set[str] | None = None, *, include_dynamic: bool = False) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        visible_names = model_visible_tool_names(include_dynamic=include_dynamic)
        for model_name in sorted(visible_names):
            if allowed_names is not None and model_name not in allowed_names:
                continue
            spec = cls.spec(model_name)
            if spec is None or spec.internal_only:
                continue
            if spec.dynamic and not include_dynamic and not spec.model_visible:
                continue
            contract = tool_protocol_spec(spec.canonical).as_contract()
            schema = dict(contract.get("input_schema") or {})
            properties = {
                key: _model_property_schema(model_name, key, value)
                for key, value in dict(schema.get("properties") or {}).items()
                if key not in MODEL_INJECTED_FIELDS
            }
            required = [
                key
                for key in MODEL_REQUIRED_OVERRIDES.get(model_name, DEFAULT_MODEL_REQUIRED)
                if key in properties
            ]
            tools.append(
                {
                    "type": "function",
                    "name": model_name,
                    "description": (
                        f"{spec.progress_label}. Kind: {spec.kind}. "
                        f"Capabilities: {', '.join(contract.get('capabilities') or [])}. "
                        f"Side effects: {contract.get('side_effect_class')}; parallel_safe={str(contract.get('parallel_safe')).lower()}. "
                        "Use this product-neutral code-agent tool only when it directly advances the current plan. "
                        + (
                            "This mutating tool applies exactly one app-owned file_path per call; for multiple files, call the tool once per file with that file's own content or diff. Generated/platform-owned paths are rejected."
                            if spec.kind == "mutating"
                            else ""
                        )
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": properties,
                        "required": required,
                    },
                }
            )
        return tools


def _model_property_schema(model_name: str, key: str, value: Any) -> dict[str, Any]:
    schema = dict(value) if isinstance(value, dict) else {"type": "string"}
    if key in MUTATING_FILE_FIELD_DESCRIPTIONS:
        schema["description"] = MUTATING_FILE_FIELD_DESCRIPTIONS[key]
    if model_name == "tool_search" and key == "query":
        schema["description"] = "Short description of the optional capability to discover, such as deploy, browser verification, database, payments, CMS, GitHub, or Vercel."
    return schema


READ_ONLY_AGENT_TOOLS = frozenset(
    name for name in AgentToolRegistry.names() if AgentToolRegistry.kind(name) in {"read_only", "verification"}
)
MUTATING_AGENT_TOOLS = frozenset(
    name for name in AgentToolRegistry.names() if AgentToolRegistry.kind(name) == "mutating"
)
