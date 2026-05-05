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
    progress labels, and batching behavior. It does not know product categories or
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
        "read_artifact_ref": AgentToolSpec(
            name="read_artifact_ref",
            kind="read_only",
            concurrency_safe=True,
            output_cap_chars=12000,
            activity="reading",
            progress_label="Reading stored tool artifact",
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
            progress_label="Applying one draft file patch",
        ),
        "write_file": AgentToolSpec(
            name="write_file",
            kind="mutating",
            concurrency_safe=False,
            activity="editing",
            progress_label="Writing one draft file",
        ),
        "edit_file_exact": AgentToolSpec(
            name="edit_file_exact",
            kind="mutating",
            concurrency_safe=False,
            activity="editing",
            progress_label="Applying an exact old/new string edit to one draft file",
        ),
        "file.read": AgentToolSpec(
            name="file.read",
            kind="read_only",
            concurrency_safe=True,
            output_cap_chars=9000,
            activity="reading",
            progress_label="Reading files through the unified tool protocol",
        ),
        "file.write": AgentToolSpec(
            name="file.write",
            kind="mutating",
            concurrency_safe=False,
            activity="editing",
            progress_label="Writing a file through the unified tool protocol",
        ),
        "file.edit": AgentToolSpec(
            name="file.edit",
            kind="mutating",
            concurrency_safe=False,
            activity="applying_patch",
            progress_label="Editing a file through the unified tool protocol",
        ),
        "search.grep": AgentToolSpec(
            name="search.grep",
            kind="read_only",
            concurrency_safe=True,
            activity="searching",
            progress_label="Searching file contents through the unified tool protocol",
        ),
        "search.glob": AgentToolSpec(
            name="search.glob",
            kind="read_only",
            concurrency_safe=True,
            activity="searching",
            progress_label="Listing files through the unified tool protocol",
        ),
        "shell.exec": AgentToolSpec(
            name="shell.exec",
            kind="read_only",
            concurrency_safe=True,
            timeout_seconds=30,
            activity="running_command",
            progress_label="Running a governed shell command",
        ),
        "browser.verify": AgentToolSpec(
            name="browser.verify",
            kind="verification",
            concurrency_safe=False,
            timeout_seconds=180,
            output_cap_chars=14000,
            activity="browser_verifying",
            progress_label="Running browser verification through the unified tool protocol",
        ),
        "patch.apply": AgentToolSpec(
            name="patch.apply",
            kind="mutating",
            concurrency_safe=False,
            activity="applying_patch",
            progress_label="Applying a strict patch through the unified tool protocol",
        ),
        "contract.compile": AgentToolSpec(
            name="contract.compile",
            kind="read_only",
            concurrency_safe=False,
            activity="planning",
            progress_label="Compiling the typed mini-app contract",
        ),
        "registry.sync": AgentToolSpec(
            name="registry.sync",
            kind="mutating",
            concurrency_safe=False,
            activity="checking",
            progress_label="Synchronizing prompt-contract metadata",
        ),
        "ask_user": AgentToolSpec(
            name="ask_user",
            kind="read_only",
            concurrency_safe=False,
            activity="tool_progress",
            progress_label="Requesting user input",
        ),
        "todo.write": AgentToolSpec(
            name="todo.write",
            kind="read_only",
            concurrency_safe=False,
            activity="planning",
            progress_label="Updating the agent task plan",
        ),
        "review.start": AgentToolSpec(
            name="review.start",
            kind="verification",
            concurrency_safe=False,
            activity="checking",
            progress_label="Starting an automated review",
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

    @classmethod
    def openai_tools(cls, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for name in sorted(cls._SPECS):
            if "." in name or name == "browser_verify":
                continue
            if allowed_names is not None and name not in allowed_names:
                continue
            spec = cls._SPECS[name]
            if name == "apply_patch_to_draft":
                properties = {
                    "file_path": {
                        "type": "string",
                        "description": "One app-owned miniapp/... file. Do not target generated/platform-owned files such as app/routes/role_pages.py, app/routes/role_routes.py, or app/generated/*.json.",
                    },
                    "diff": {
                        "type": "string",
                        "description": "A unified diff for this one file. It must include @@ hunks with context and +/- lines.",
                    },
                    "worker_id": {"type": "string"},
                    "owner_scope": {"type": "string"},
                    "reason": {"type": "string"},
                }
                required = ["file_path", "diff", "reason"]
            elif name == "write_file":
                properties = {
                    "file_path": {
                        "type": "string",
                        "description": "One app-owned miniapp/... file. Do not target generated/platform-owned files such as app/routes/role_pages.py, app/routes/role_routes.py, or app/generated/*.json.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete resulting file content for this one file.",
                    },
                    "worker_id": {"type": "string"},
                    "owner_scope": {"type": "string"},
                    "reason": {"type": "string"},
                }
                required = ["file_path", "content", "reason"]
            elif name == "edit_file_exact":
                properties = {
                    "file_path": {
                        "type": "string",
                        "description": "One app-owned miniapp/... file. Do not target generated/platform-owned files such as app/routes/role_pages.py, app/routes/role_routes.py, or app/generated/*.json.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text currently present in the file. It must match exactly and uniquely unless replace_all is true.",
                    },
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean"},
                    "worker_id": {"type": "string"},
                    "owner_scope": {"type": "string"},
                    "reason": {"type": "string"},
                }
                required = ["file_path", "old_string", "new_string", "reason"]
            else:
                properties = {
                    "mode": {"type": "string", "enum": ["exact", "final"]},
                    "targets": {"type": "array", "items": {"type": "string"}},
                    "pattern": {"type": "string"},
                    "command": {"type": "string"},
                    "process_id": {"type": "string"},
                    "artifact_ref": {"type": "string"},
                    "reason": {"type": "string"},
                }
                required = ["reason"]
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": (
                        f"{spec.progress_label}. Kind: {spec.kind}. "
                        "Use this generic code-agent tool only when it directly advances the current plan. "
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


READ_ONLY_AGENT_TOOLS = frozenset(
    name for name, spec in AgentToolRegistry._SPECS.items() if spec.kind in {"read_only", "verification"}
)
MUTATING_AGENT_TOOLS = frozenset(
    name for name, spec in AgentToolRegistry._SPECS.items() if spec.kind == "mutating"
)
