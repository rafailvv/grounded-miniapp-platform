from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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
    "context_suggestion",
    "hook_started",
    "hook_completed",
    "verifier_nudge",
    "worker_started",
    "worker_completed",
    "worker_failed",
    "completed",
]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    kind: AgentToolKind
    concurrency_safe: bool
    timeout_seconds: int = 25
    output_cap_chars: int = 6000
    activity: AgentActivityKind = "reading"
    progress_label: str = "Reading workspace context"


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
    """Single source of truth for model-facing agent tools.

    The registry is intentionally generic: it describes tool semantics, safety,
    progress labels, and batching behavior. It does not know product domains or
    generated app resource names.
    """

    _SPECS: dict[str, AgentToolSpec] = {
        "list_files": AgentToolSpec(
            name="list_files",
            kind="read_only",
            concurrency_safe=True,
            activity="reading",
            progress_label="Reading workspace file list",
        ),
        "read_files": AgentToolSpec(
            name="read_files",
            kind="read_only",
            concurrency_safe=True,
            output_cap_chars=9000,
            activity="reading",
            progress_label="Reading selected files",
        ),
        "search_files": AgentToolSpec(
            name="search_files",
            kind="read_only",
            concurrency_safe=True,
            activity="searching",
            progress_label="Searching workspace",
        ),
        "inspect_diff": AgentToolSpec(
            name="inspect_diff",
            kind="read_only",
            concurrency_safe=True,
            output_cap_chars=12000,
            activity="reading",
            progress_label="Inspecting draft diff",
        ),
        "semantic_scan": AgentToolSpec(
            name="semantic_scan",
            kind="read_only",
            concurrency_safe=True,
            output_cap_chars=12000,
            activity="searching",
            progress_label="Scanning source semantics",
        ),
        "run_command": AgentToolSpec(
            name="run_command",
            kind="read_only",
            concurrency_safe=True,
            timeout_seconds=25,
            activity="running_command",
            progress_label="Running diagnostic command",
        ),
        "run_checks": AgentToolSpec(
            name="run_checks",
            kind="verification",
            concurrency_safe=False,
            timeout_seconds=120,
            output_cap_chars=10000,
            activity="checking",
            progress_label="Running validation checks",
        ),
        "browser_verify": AgentToolSpec(
            name="browser_verify",
            kind="verification",
            concurrency_safe=False,
            timeout_seconds=180,
            output_cap_chars=14000,
            activity="browser_verifying",
            progress_label="Running browser workflow proof",
        ),
        "apply_patch_to_draft": AgentToolSpec(
            name="apply_patch_to_draft",
            kind="mutating",
            concurrency_safe=False,
            activity="applying_patch",
            progress_label="Applying draft patch",
        ),
        "write_file": AgentToolSpec(
            name="write_file",
            kind="mutating",
            concurrency_safe=False,
            activity="editing",
            progress_label="Writing draft file",
        ),
    }

    @classmethod
    def names(cls) -> set[str]:
        return set(cls._SPECS)

    @classmethod
    def spec(cls, tool_name: object) -> AgentToolSpec | None:
        return cls._SPECS.get(str(tool_name or "").strip().lower())

    @classmethod
    def kind(cls, tool_name: object) -> AgentToolKind:
        spec = cls.spec(tool_name)
        return spec.kind if spec is not None else "unknown"

    @classmethod
    def is_model_read_only(cls, tool_name: object) -> bool:
        return cls.kind(tool_name) in {"read_only", "verification"}

    @classmethod
    def plan_batches(cls, tool_requests: list[dict[str, Any]]) -> AgentToolBatchPlan:
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

        for request in tool_requests:
            if not isinstance(request, dict):
                continue
            spec = cls.spec(request.get("tool"))
            if spec is None:
                unknown.append(request)
                append_batch(request, concurrency_safe=False)
                continue
            if spec.kind == "mutating":
                mutating.append(request)
            elif spec.kind == "verification":
                verification.append(request)
                read_only.append(request)
            else:
                read_only.append(request)
            append_batch(request, concurrency_safe=spec.concurrency_safe and spec.kind == "read_only")

        return AgentToolBatchPlan(
            read_only_requests=read_only,
            mutating_requests=mutating,
            verification_requests=verification,
            unknown_requests=unknown,
            ordered_batches=batches,
        )


READ_ONLY_AGENT_TOOLS = frozenset(
    name for name, spec in AgentToolRegistry._SPECS.items() if spec.kind in {"read_only", "verification"}
)
MUTATING_AGENT_TOOLS = frozenset(
    name for name, spec in AgentToolRegistry._SPECS.items() if spec.kind == "mutating"
)
