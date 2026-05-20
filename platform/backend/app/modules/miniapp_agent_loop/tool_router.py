from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path
from threading import Lock
import time
from typing import Any, Callable

from app.models.domain import CheckExecutionRecord, DraftAction
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager, AgentHookOutcome
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolBatch, AgentToolRegistry
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.dynamic_tool_catalog import DynamicToolCatalog
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.modules.miniapp_agent_loop.product_workers import canonical_worker_id
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.tool_batch_summary import summarize_tool_batch
from app.modules.miniapp_agent_loop.agent_tool_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
)
from app.services.tool_protocol import (
    TOOL_INPUT_SCHEMAS,
    canonical_tool_name,
    structured_tool_error,
    tool_envelope,
    tool_protocol_spec,
)
from app.services.workspace.service import WorkspaceService


MODEL_TOOL_FOR_CANONICAL: dict[str, str] = {
    "file.list": "list_files",
    "file.read": "read_files",
    "artifact.read": "read_artifact_ref",
    "search.grep": "search_files",
    "semantic.scan": "semantic_scan",
    "tool.search": "tool_search",
    "diff.inspect": "inspect_diff",
    "checks.run": "run_checks",
    "browser.verify": "browser_verify",
    "shell.exec": "run_command",
    "patch.apply": "apply_patch_to_draft",
    "file.write": "write_file",
    "file.edit": "edit_file_exact",
}

ROUTING_KEYS = {"tool", "tool_use_id"}
NORMALIZED_INPUT_KEYS = {
    "mode",
    "targets",
    "files",
    "pattern",
    "query",
    "symbol",
    "changed_only",
    "command",
    "process_id",
    "artifact_ref",
    "file_path",
    "content",
    "diff",
    "old_string",
    "new_string",
    "replace_all",
    "worker_id",
    "owner_scope",
    "reason",
    "domain",
    "intent",
    "capability",
    "question",
    "choices",
    "items",
    "prompt",
    "generation_mode",
    "contract_id",
    "allowed_file_graph",
}
MUTATING_MODEL_TOOLS = {"apply_patch_to_draft", "write_file", "edit_file_exact"}
EXECUTABLE_MODEL_TOOLS = {
    "list_files",
    "read_files",
    "search_files",
    "inspect_diff",
    "read_artifact_ref",
    "semantic_scan",
    "tool_search",
    "lsp_diagnostics",
    "lsp_symbol_context",
    "lsp_definition",
    "lsp_find_references",
    "lsp_route_graph",
    "lsp_route_static_context",
    "run_command",
    "run_checks",
    "browser_verify",
    *MUTATING_MODEL_TOOLS,
}


@dataclass(frozen=True)
class ToolRouterContext:
    workspace_id: str
    run_id: str
    draft_source: Path
    workspace_service: WorkspaceService
    execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]]
    file_state_cache: AgentFileStateCache = field(default_factory=AgentFileStateCache)
    process_manager: AgentProcessManager = field(default_factory=AgentProcessManager)
    read_artifact: Callable[[str], dict[str, Any] | None] | None = None
    append_activity: Callable[[str, str, dict[str, Any] | None], None] | None = None
    append_batch_summary: Callable[[dict[str, object]], None] | None = None
    hook_manager: AgentHookManager | None = None
    mode: str = "default"
    forced_allowed_tools: set[str] | None = None
    output_spill_writer: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None
    output_artifact_writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    denied_action_writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    max_parallel_read_tools: int = 6


@dataclass(frozen=True)
class ToolCallRequest:
    tool: str
    canonical_tool: str
    tool_call_id: str
    input: dict[str, Any]
    raw_input: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class ToolRouteDecision:
    allowed: bool
    reason: str
    kind: str
    risk: str
    approval_class: str
    concurrency_safe: bool
    timeout_seconds: int
    output_cap_chars: int
    sandbox_profile: str
    artifact_spill_policy: str
    deferred: bool = False
    dynamic: bool = False


@dataclass(frozen=True)
class ToolRouterResult:
    request: ToolCallRequest
    decision: ToolRouteDecision
    envelope: dict[str, Any]
    model_result: dict[str, object]
    loaded_context: dict[str, str] = field(default_factory=dict)
    deferred_changes: list[DraftAction] = field(default_factory=list)


@dataclass(frozen=True)
class ToolRouterBatchResult:
    loaded_context: dict[str, str]
    model_results: list[dict[str, object]]
    envelopes: list[dict[str, Any]]
    deferred_changes: list[DraftAction]
    batch_summary: dict[str, object]


class ToolRouter:
    """Model-facing boundary for agent tools.

    The router normalizes legacy model tool names into canonical protocol names,
    validates the current schema subset, executes allowed read/verification tools,
    defers mutating tools into typed draft-action proposals, and always returns a
    protocol envelope plus a legacy `tool_use_id` result for transcript resume.
    """

    def __init__(self, context: ToolRouterContext) -> None:
        self.context = context
        self._activity_lock = Lock()
        self._workspace_tree: list[dict[str, str]] | None = None

    @classmethod
    def normalize_tool_calls(cls, raw_tool_calls: list[Any]) -> list[dict[str, Any]]:
        if not isinstance(raw_tool_calls, list):
            return []
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(raw_tool_calls, start=1):
            request = cls.normalize_tool_call(item, ordinal=index)
            if request is None:
                continue
            normalized.append(
                {
                    "tool": request.tool,
                    "canonical_tool": request.canonical_tool,
                    "tool_use_id": request.tool_call_id,
                    **request.input,
                    "_raw_input": request.raw_input,
                }
            )
        return normalized

    @classmethod
    def normalize_tool_call(cls, item: Any, *, ordinal: int | None = None) -> ToolCallRequest | None:
        if not isinstance(item, dict):
            return None
        raw_for_validation = item.get("_raw_input") if isinstance(item.get("_raw_input"), dict) else dict(item)
        raw_tool = str(item.get("tool") or "").strip().lower()
        if not raw_tool:
            return None
        canonical = canonical_tool_name(raw_tool)
        tool = MODEL_TOOL_FOR_CANONICAL.get(canonical, raw_tool)
        use_id = str(item.get("tool_use_id") or "").strip() or f"{tool}_{ordinal or 1}"
        raw_targets = item.get("targets") or []
        if not isinstance(raw_targets, list):
            raw_targets = []
        targets: list[str] = []
        for target in raw_targets:
            value = _strip_leading_dot_slash(target)
            if value and value not in targets:
                targets.append(value)
        raw_files = item.get("files") or []
        if not isinstance(raw_files, list):
            raw_files = []
        files: list[str] = []
        for file_path in raw_files:
            value = _strip_leading_dot_slash(file_path)
            if value and value not in files:
                files.append(value)
        mode = str(item.get("mode") or ("exact" if tool == "run_checks" else "")).strip().lower()
        if tool == "run_checks" and mode not in {"exact", "final"}:
            mode = "exact"
        normalized_input = {
            "mode": mode,
            "targets": targets[:12],
            "files": files[:16],
            "file_path": _strip_leading_dot_slash(item.get("file_path") or ""),
            "pattern": str(item.get("pattern") or "").strip(),
            "query": str(item.get("query") or "").strip(),
            "symbol": str(item.get("symbol") or "").strip(),
            "changed_only": bool(item.get("changed_only") or False),
            "command": str(item.get("command") or "").strip(),
            "process_id": str(item.get("process_id") or "").strip(),
            "artifact_ref": str(item.get("artifact_ref") or "").strip(),
            "content": str(item.get("content") or ""),
            "diff": str(item.get("diff") or ""),
            "old_string": str(item.get("old_string") or ""),
            "new_string": str(item.get("new_string") or ""),
            "replace_all": bool(item.get("replace_all") or False),
            "worker_id": str(item.get("worker_id") or "").strip(),
            "owner_scope": str(item.get("owner_scope") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "domain": str(item.get("domain") or "").strip().lower(),
            "intent": str(item.get("intent") or "").strip(),
            "capability": str(item.get("capability") or "").strip(),
            "question": str(item.get("question") or "").strip(),
            "choices": item.get("choices") if isinstance(item.get("choices"), list) else [],
            "items": item.get("items") if isinstance(item.get("items"), list) else [],
            "prompt": str(item.get("prompt") or "").strip(),
            "generation_mode": str(item.get("generation_mode") or "").strip(),
            "contract_id": str(item.get("contract_id") or "").strip(),
            "allowed_file_graph": item.get("allowed_file_graph") if isinstance(item.get("allowed_file_graph"), dict) else {},
        }
        return ToolCallRequest(
            tool=tool,
            canonical_tool=canonical,
            tool_call_id=use_id,
            input=normalized_input,
            raw_input=dict(raw_for_validation),
            reason=str(normalized_input.get("reason") or ""),
        )

    @classmethod
    def allowed_tool_names(
        cls,
        *,
        mode: str = "default",
        forced_allowed: set[str] | None = None,
    ) -> set[str]:
        public = {
            name
            for name in AgentToolRegistry.names()
            if "." not in name and name != "browser_verify" and name in EXECUTABLE_MODEL_TOOLS
        }
        normalized_mode = str(mode or "default").strip().lower()
        if normalized_mode in {"read_only", "analysis"}:
            base = {
                name
                for name in public
                if AgentToolRegistry.kind(name) == "read_only"
            }
        elif normalized_mode in {"mutation_required", "mutating"}:
            base = {"apply_patch_to_draft", "write_file", "edit_file_exact"}
        elif normalized_mode in {"verification", "browser"}:
            base = {"run_checks"}
        else:
            base = set(public)
        if forced_allowed is not None:
            requested = {MODEL_TOOL_FOR_CANONICAL.get(canonical_tool_name(name), str(name).strip().lower()) for name in forced_allowed}
            base = {name for name in requested if AgentToolRegistry.spec(name) is not None or name == "browser_verify"}
        return base

    @classmethod
    def allowed_openai_tools(
        cls,
        *,
        mode: str = "default",
        forced_allowed: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        allowed = cls.allowed_tool_names(mode=mode, forced_allowed=forced_allowed)
        include_dynamic = "browser_verify" in allowed
        return AgentToolRegistry.openai_tools(allowed, include_dynamic=include_dynamic)

    @classmethod
    def manifest(cls) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        for name in sorted(AgentToolRegistry.names()):
            spec = AgentToolRegistry.spec(name)
            if spec is None:
                continue
            canonical = canonical_tool_name(name)
            protocol = tool_protocol_spec(canonical)
            tools.append(
                {
                    "name": name,
                    "canonical": canonical,
                    "kind": spec.kind,
                    "risk": protocol.risk,
                    "approval_class": protocol.approval_class,
                    "sandbox_profile": protocol.sandbox_profile,
                    "concurrency_safe": protocol.concurrency_safe,
                    "timeout_seconds": protocol.timeout_seconds,
                    "output_cap_chars": protocol.output_cap_chars,
                    "artifact_spill_policy": protocol.artifact_spill_policy,
                    "activity": spec.activity,
                    "progress_label": spec.progress_label,
                    "aliases": list(spec.aliases),
                    "mode_visibility": list(spec.mode_visibility),
                    "dynamic": bool(protocol.dynamic or spec.dynamic),
                    "deferred": bool(protocol.deferred or spec.deferred or spec.kind == "mutating"),
                    "input_schema": protocol.input_schema,
                    "output_schema": protocol.output_schema,
                }
            )
        return {
            "schema": "grounded.tool_router.manifest.v1",
            "boundary": "model_facing_agent_tools",
            "mutation_boundary": "validate_and_defer_to_draft_apply_pipeline",
            "allowed_modes": ["default", "read_only", "mutation_required", "verification", "worker_branch"],
            "dynamic_tool_discovery": DynamicToolCatalog.manifest(),
            "tools": tools,
        }

    @classmethod
    def dynamic_tool_manifest(cls) -> dict[str, Any]:
        return DynamicToolCatalog.manifest()

    @classmethod
    def deferred_mutations_from_calls(
        cls,
        tool_calls: list[dict[str, Any]],
        *,
        default_worker_id: str | None = None,
        default_owner_scope: str | None = None,
        read_text_file: Callable[[str], str | None] | None = None,
        file_freshness: Callable[[str], dict[str, object]] | None = None,
        find_similar_path: Callable[[str], str | None] | None = None,
    ) -> tuple[list[DraftAction], list[dict[str, object]]]:
        file_changes: list[DraftAction] = []
        trace: list[dict[str, object]] = []
        for request_item in tool_calls:
            tool = str(request_item.get("tool") or "").strip().lower()
            if tool not in MUTATING_MODEL_TOOLS:
                continue
            targets = [
                _strip_leading_dot_slash(target)
                for target in request_item.get("targets") or []
                if str(target or "").strip()
            ]
            raw_file_path = request_item.get("file_path") or (targets[0] if targets else "")
            raw_worker_id = str(request_item.get("worker_id") or default_worker_id or "").strip()
            file_path = _normalize_agent_file_path(raw_file_path, worker_id=raw_worker_id)
            reason = str(request_item.get("reason") or f"{tool} requested by agent").strip()
            worker_id = str(raw_worker_id or AgentWorkerManager.owner_for_path(file_path)).strip()
            owner_scope = str(request_item.get("owner_scope") or default_owner_scope or "").strip()
            if worker_id:
                reason = f"[{worker_id}] {reason}"
            if tool == "write_file":
                file_changes.append(
                    DraftAction(
                        file_path=file_path,
                        operation="replace",
                        content=str(request_item.get("content") or ""),
                        reason=reason,
                    )
                )
            elif tool == "edit_file_exact":
                path_safe = _is_safe_exact_path(file_path)
                freshness = file_freshness(file_path) if file_freshness is not None and path_safe else {}
                current = read_text_file(file_path) if read_text_file is not None and path_safe else None
                old_string = str(request_item.get("old_string") or "")
                new_string = str(request_item.get("new_string") or "")
                replace_all = bool(request_item.get("replace_all") or False)
                exact_failure = _exact_edit_failure(
                    file_path=file_path,
                    current=current,
                    old_string=old_string,
                    replace_all=replace_all,
                    freshness=freshness,
                    similar_path=find_similar_path(file_path) if find_similar_path is not None and path_safe and current is None else None,
                )
                if exact_failure is not None:
                    code, message, evidence = exact_failure
                    packet = AgentEditValidator.repair_packet_for_issue(
                        code=code,
                        message=message,
                        file_changes=[
                            DraftAction(file_path=file_path or "miniapp/invalid_exact_edit", operation="replace", content="invalid exact edit", reason=reason)
                        ],
                        evidence=evidence,
                    )
                    trace.append(
                        {
                            "tool": tool,
                            "tool_use_id": str(request_item.get("tool_use_id") or ""),
                            "status": "failed",
                            "failure_class": packet.get("failure_class"),
                            "failure_signature": packet.get("failure_signature"),
                            "error_code": code,
                            "message": message,
                            "file_path": file_path,
                            "worker_id": worker_id,
                            "owner_scope": owner_scope,
                            "reason": reason,
                            "repair_packet": packet,
                            "required_next_action": "Read the exact target file, then retry edit_file_exact with a unique old_string or use write_file.",
                        }
                    )
                    continue
                assert current is not None
                updated = current.replace(old_string, new_string) if replace_all else current.replace(old_string, new_string, 1)
                file_changes.append(
                    DraftAction(
                        file_path=file_path,
                        operation="replace",
                        content=updated,
                        reason=reason,
                    )
                )
            else:
                file_changes.append(
                    DraftAction(
                        file_path=file_path,
                        operation="patch",
                        diff=str(request_item.get("diff") or request_item.get("content") or ""),
                        reason=reason,
                    )
                )
            trace.append(
                {
                    "tool": tool,
                    "tool_use_id": str(request_item.get("tool_use_id") or ""),
                    "contract": "mutating tool call converted to a deferred DraftAction proposal for serialized draft apply",
                    "status": "deferred",
                    "file_path": file_path,
                    "worker_id": worker_id,
                    "owner_scope": owner_scope,
                    "reason": reason,
                }
            )
        return file_changes, trace

    def route_batch(self, tool_calls: list[dict[str, Any]]) -> ToolRouterBatchResult:
        requests = self._requests_from_tool_calls(tool_calls)
        plan = AgentToolRegistry.plan_batches(
            [{"tool": item.tool, "tool_use_id": item.tool_call_id, **item.input, "_request": item} for item in requests]
        )
        model_results: list[dict[str, object]] = [
            {
                "tool": "agent_tool_batch",
                "contract": "ToolRouter batches safe read-only tools concurrently; verification and deferred mutations are serialized.",
                "router": "tool_router.v1",
                "read_only_count": len(plan.read_only_requests),
                "mutating_count": len(plan.mutating_requests),
                "verification_count": len(plan.verification_requests),
                "unknown_count": len(plan.unknown_requests),
                "ordered_batches": [
                    {"concurrency_safe": batch.concurrency_safe, "tools": batch.tools}
                    for batch in plan.ordered_batches
                ],
            }
        ]
        envelopes: list[dict[str, Any]] = []
        loaded_context: dict[str, str] = {}
        deferred_changes: list[DraftAction] = []
        started_at = time.perf_counter()

        for batch_index, batch in enumerate(plan.ordered_batches):
            if batch.concurrency_safe:
                batch_result = self._flush_concurrent_batch(
                    [
                        (batch_index * 1000 + index, self._request_from_batch_item(request_item))
                        for index, request_item in enumerate(batch.requests)
                    ]
                )
                loaded_context.update(batch_result.loaded_context)
                model_results.extend(batch_result.model_results)
                envelopes.extend(batch_result.envelopes)
                deferred_changes.extend(batch_result.deferred_changes)
                continue
            for request_item in batch.requests:
                request = self._request_from_batch_item(request_item)
                before_count = len(model_results)
                serial_started = time.perf_counter()
                result = self.route_one(request)
                loaded_context.update(result.loaded_context)
                model_results.append(result.model_result)
                envelopes.append(result.envelope)
                deferred_changes.extend(result.deferred_changes)
                summary = summarize_tool_batch(
                    batch_id=f"batch_{len(model_results) + 1}",
                    requests=[{"tool": request.tool, **request.input}],
                    results=[result.model_result],
                    duration_ms=int((time.perf_counter() - serial_started) * 1000),
                    status="completed",
                )
                self._append_batch_summary(summary)
                self._emit_activity("tool_use_summary", str(summary["summary"]), summary)
                if len(model_results) == before_count:
                    continue

        return ToolRouterBatchResult(
            loaded_context=loaded_context,
            model_results=model_results,
            envelopes=envelopes,
            deferred_changes=deferred_changes,
            batch_summary={
                "schema": "grounded.tool_router.batch.v1",
                "tool_count": len(requests),
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "envelope_count": len(envelopes),
                "hook_context_count": sum(
                    len(item.get("hook_contexts") or [])
                    for item in model_results
                    if isinstance(item.get("hook_contexts"), list)
                ),
            },
        )

    def route_one(self, request: ToolCallRequest) -> ToolRouterResult:
        decision = self._decide(request)
        validation_error = self._schema_error(request) if decision.allowed else None
        if validation_error is not None:
            return self._failed_result(request, decision, validation_error)
        if not decision.allowed:
            return self._failed_result(
                request,
                decision,
                structured_tool_error(
                    code="tool_not_allowed",
                    message=decision.reason,
                    details={"tool": request.tool, "mode": self.context.mode},
                ),
            )
        pre_hook_error = self._pre_hook_error(request, decision)
        if pre_hook_error is not None:
            result = self._failed_result(request, decision, pre_hook_error)
            post_outcome = self._post_hook(request, result.envelope, failed=True)
            if post_outcome is not None and post_outcome.additional_contexts:
                result.model_result["hook_contexts"] = list(post_outcome.additional_contexts)
                result.model_result["hook_evaluation"] = post_outcome.as_dict()
            return result
        try:
            if decision.deferred:
                result = self._defer_mutation(request, decision)
            elif decision.kind == "verification":
                result = self._execute_verification(request, decision)
            else:
                result = self._execute_read_only(request, decision)
        except Exception as exc:
            result = self._failed_result(
                request,
                decision,
                structured_tool_error(
                    code="tool_execution_error",
                    message=str(exc),
                    retryable=True,
                    details={"error_class": exc.__class__.__name__},
                ),
            )
        post_outcome = self._post_hook(request, result.envelope, failed=str(result.envelope.get("status") or "") == "failed")
        if post_outcome is not None and post_outcome.additional_contexts:
            result.model_result["hook_contexts"] = list(post_outcome.additional_contexts)
            result.model_result["hook_evaluation"] = post_outcome.as_dict()
        return result

    def _requests_from_tool_calls(self, tool_calls: list[dict[str, Any]]) -> list[ToolCallRequest]:
        requests: list[ToolCallRequest] = []
        for index, item in enumerate(tool_calls, start=1):
            if isinstance(item, dict) and isinstance(item.get("_request"), ToolCallRequest):
                requests.append(item["_request"])
                continue
            request = self.normalize_tool_call(item, ordinal=index)
            if request is not None:
                requests.append(request)
        return requests

    @staticmethod
    def _request_from_batch_item(request_item: dict[str, Any]) -> ToolCallRequest:
        request = request_item.get("_request")
        if isinstance(request, ToolCallRequest):
            return request
        normalized = ToolRouter.normalize_tool_call(request_item)
        if normalized is None:
            return ToolCallRequest(tool="unknown", canonical_tool="unknown", tool_call_id="unknown_1", input={}, raw_input=dict(request_item))
        return normalized

    def _decide(self, request: ToolCallRequest) -> ToolRouteDecision:
        spec = AgentToolRegistry.spec(request.tool)
        canonical = request.canonical_tool
        protocol = tool_protocol_spec(canonical)
        kind = spec.kind if spec is not None else "unknown"
        allowed_names = self.allowed_tool_names(mode=self.context.mode, forced_allowed=self.context.forced_allowed_tools)
        allowed = spec is not None and request.tool in allowed_names
        reason = "allowed" if allowed else f"{request.tool or canonical} is not allowed in {self.context.mode or 'default'} mode."
        return ToolRouteDecision(
            allowed=allowed,
            reason=reason,
            kind=kind,
            risk=protocol.risk,
            approval_class=protocol.approval_class,
            concurrency_safe=protocol.concurrency_safe,
            timeout_seconds=protocol.timeout_seconds,
            output_cap_chars=protocol.output_cap_chars,
            sandbox_profile=protocol.sandbox_profile,
            artifact_spill_policy=protocol.artifact_spill_policy,
            deferred=bool(protocol.deferred or kind == "mutating"),
            dynamic=bool(protocol.dynamic or (spec.dynamic if spec else False)),
        )

    def _schema_error(self, request: ToolCallRequest) -> dict[str, Any] | None:
        schema = TOOL_INPUT_SCHEMAS.get(request.canonical_tool) or {}
        if not schema:
            return None
        validation_input = self._protocol_input(request)
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        missing = [
            key
            for key in schema.get("required", [])
            if key not in validation_input or validation_input.get(key) is None or validation_input.get(key) == ""
        ]
        if missing:
            return structured_tool_error(
                code="tool_schema_invalid",
                message=f"Missing required tool input: {', '.join(missing)}.",
                retryable=True,
                details={"missing": missing, "tool": request.canonical_tool},
            )
        if schema.get("additionalProperties") is False:
            raw_keys = set(request.raw_input) - ROUTING_KEYS
            extras = sorted(key for key in raw_keys if key not in properties and key not in NORMALIZED_INPUT_KEYS)
            if extras:
                return structured_tool_error(
                    code="tool_schema_invalid",
                    message=f"Unsupported tool input field: {', '.join(extras)}.",
                    retryable=True,
                    details={"extra": extras, "tool": request.canonical_tool},
                )
        for key, property_schema in properties.items():
            if key not in validation_input:
                continue
            value = validation_input.get(key)
            expected = property_schema.get("type") if isinstance(property_schema, dict) else None
            if expected and not _json_type_matches(value, expected):
                return structured_tool_error(
                    code="tool_schema_invalid",
                    message=f"Tool input {key} must be {expected}.",
                    retryable=True,
                    details={"field": key, "expected": expected, "tool": request.canonical_tool},
                )
            enum = property_schema.get("enum") if isinstance(property_schema, dict) else None
            if isinstance(enum, list) and value not in enum and value not in {None, ""}:
                return structured_tool_error(
                    code="tool_schema_invalid",
                    message=f"Tool input {key} must be one of: {', '.join(str(item) for item in enum)}.",
                    retryable=True,
                    details={"field": key, "enum": enum, "tool": request.canonical_tool},
                )
            item_schema = property_schema.get("items") if isinstance(property_schema, dict) else None
            if expected == "array" and isinstance(value, list) and isinstance(item_schema, dict):
                item_type = item_schema.get("type")
                if item_type and any(not _json_type_matches(item, item_type) for item in value):
                    return structured_tool_error(
                        code="tool_schema_invalid",
                        message=f"Tool input {key} items must be {item_type}.",
                        retryable=True,
                        details={"field": key, "expected": item_type, "tool": request.canonical_tool},
                    )
        return None

    def _protocol_input(self, request: ToolCallRequest) -> dict[str, Any]:
        payload = {key: value for key, value in request.input.items() if key in NORMALIZED_INPUT_KEYS}
        payload["workspace_id"] = self.context.workspace_id
        payload["run_id"] = self.context.run_id
        return payload

    def _pre_hook_error(self, request: ToolCallRequest, decision: ToolRouteDecision) -> dict[str, Any] | None:
        payload = {
            "workspace_id": self.context.workspace_id,
            "run_id": self.context.run_id,
            "tool": request.canonical_tool,
            "model_tool": request.tool,
            "tool_use_id": request.tool_call_id,
            "risk": decision.risk,
            "mode": self.context.mode,
            "input": self._protocol_input(request),
        }
        self._emit_activity(
            "hook_started",
            "Pre-tool hook",
            {"hook": "pre_tool_use", "tool_use_id": request.tool_call_id, "tool": request.tool, "status": "started"},
        )
        if self.context.hook_manager is None:
            return None
        outcome = self.context.hook_manager.run(self.context.run_id, "pre_tool_use", payload=payload)
        if not outcome.should_block:
            return None
        return structured_tool_error(
            code="tool_hook_blocked",
            message=outcome.block_reason or "Tool call blocked by hook policy.",
            retryable=False,
            details={"hook": "pre_tool_use", "outcome": outcome.as_dict()},
        )

    def _post_hook(self, request: ToolCallRequest, envelope: dict[str, Any], *, failed: bool) -> AgentHookOutcome | None:
        hook = "post_tool_use_failure" if failed else "post_tool_use"
        payload = {
            "workspace_id": self.context.workspace_id,
            "run_id": self.context.run_id,
            "tool": request.canonical_tool,
            "model_tool": request.tool,
            "tool_use_id": request.tool_call_id,
            "status": envelope.get("status"),
            "risk": envelope.get("risk"),
            "error": envelope.get("error"),
        }
        outcome = None
        if self.context.hook_manager is not None:
            outcome = self.context.hook_manager.run(self.context.run_id, hook, payload=payload)  # type: ignore[arg-type]
            outcome_payload = outcome.as_dict()
            envelope["hook_evaluation"] = outcome_payload
            if outcome.additional_contexts:
                envelope["hook_contexts"] = list(outcome.additional_contexts)
                result_payload = envelope.get("result")
                if isinstance(result_payload, dict):
                    result_payload["hook_contexts"] = list(outcome.additional_contexts)
        self._emit_activity(
            "hook_completed",
            "Tool failure hook" if failed else "Post-tool hook",
            {"hook": hook, "tool_use_id": request.tool_call_id, "tool": request.tool, "status": "failed" if failed else "completed"},
        )
        return outcome

    def _execute_read_only(self, request: ToolCallRequest, decision: ToolRouteDecision) -> ToolRouterResult:
        started_at = time.perf_counter()
        local_context: dict[str, str] = {}
        tool_name = request.tool
        request_targets = self._visible_targets(request)
        blocked_targets = self._blocked_targets(request)
        activity, label = self._tool_activity(tool_name)
        self._emit_activity(activity, label, {"tool_use_id": request.tool_call_id, "tool": tool_name, "targets": request_targets, "status": "started"})

        if tool_name == "list_files":
            result = {**list_workspace_files(workspace_tree=self._visible_workspace_tree(), targets=request_targets), "reason": request.reason, "tool_use_id": request.tool_call_id}
        elif tool_name == "read_files":
            for target in request_targets[:16]:
                content = self.context.file_state_cache.read(
                    run_id=self.context.run_id,
                    root=self.context.draft_source,
                    path=target,
                    read_text=lambda relative_path: self.context.workspace_service.try_read_text_file(
                        self.context.workspace_id,
                        relative_path,
                        run_id=self.context.run_id,
                    ),
                )
                if content is not None:
                    local_context[target] = content
            result = {
                "tool": "read_files",
                "tool_use_id": request.tool_call_id,
                "targets": request_targets,
                "blocked_targets": blocked_targets,
                "blocked_target_reason": "protected generated/platform-owned files are hidden from model-facing read context; patch app-owned static role files, backend modules, or generated tests instead."
                if blocked_targets
                else "",
                "files": summarize_read_file_payloads(file_contents=local_context),
                "reason": request.reason,
            }
        elif tool_name == "search_files":
            result = {
                **search_workspace_files(
                    workspace_tree=self._visible_workspace_tree(),
                    read_text_file=lambda relative_path: self.context.file_state_cache.read(
                        run_id=self.context.run_id,
                        root=self.context.draft_source,
                        path=relative_path,
                        read_text=lambda cached_path: self.context.workspace_service.try_read_text_file(
                            self.context.workspace_id,
                            cached_path,
                            run_id=self.context.run_id,
                        ),
                    ),
                    pattern=str(request.input.get("pattern") or ""),
                    targets=request_targets,
                ),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
            }
        elif tool_name == "semantic_scan":
            result = {**semantic_scan(root=self.context.draft_source, targets=request_targets), "reason": request.reason, "tool_use_id": request.tool_call_id, "blocked_targets": blocked_targets}
        elif tool_name in {"tool.search", "tool_search"}:
            result = {
                **DynamicToolCatalog.search(
                    query=str(request.input.get("query") or request.input.get("capability") or ""),
                    domain=str(request.input.get("domain") or ""),
                    intent=str(request.input.get("intent") or request.reason or ""),
                ),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
                "visible_now": sorted(self.allowed_tool_names(mode=self.context.mode, forced_allowed=self.context.forced_allowed_tools)),
            }
        elif tool_name in {"lsp.diagnostics", "lsp_diagnostics"}:
            changed_files = _paths_from_diff(self.context.workspace_service.diff(self.context.workspace_id, run_id=self.context.run_id))
            files = [
                _strip_leading_dot_slash(item)
                for item in request.input.get("files") or []
                if str(item or "").strip() and _is_model_visible_path(_strip_leading_dot_slash(item))
            ]
            effective_targets = files or request_targets
            result = {
                **LspToolService.diagnostics(
                    root=self.context.draft_source,
                    targets=effective_targets,
                    changed_files=changed_files,
                    changed_only=bool(request.input.get("changed_only")),
                ),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name in {"lsp.symbol_context", "lsp_symbol_context"}:
            query = str(request.input.get("query") or request.input.get("pattern") or "").strip()
            result = {
                **LspToolService.symbol_context(root=self.context.draft_source, query=query, targets=request_targets),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name in {"lsp.find_references", "lsp_find_references"}:
            symbol = str(request.input.get("symbol") or request.input.get("query") or request.input.get("pattern") or "").strip()
            result = {
                **LspToolService.find_references(root=self.context.draft_source, symbol=symbol, targets=request_targets),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name in {"lsp.definition", "lsp_definition"}:
            symbol = str(request.input.get("symbol") or request.input.get("query") or request.input.get("pattern") or "").strip()
            result = {
                **LspToolService.definition(root=self.context.draft_source, symbol=symbol, targets=request_targets),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name in {"lsp.route_graph", "lsp_route_graph"}:
            result = {
                **LspToolService.route_graph(root=self.context.draft_source, targets=request_targets),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name in {"lsp.route_static_context", "lsp_route_static_context"}:
            result = {
                **LspToolService.route_static_context(root=self.context.draft_source, targets=request_targets),
                "reason": request.reason,
                "tool_use_id": request.tool_call_id,
                "blocked_targets": blocked_targets,
            }
        elif tool_name == "inspect_diff":
            result = {**self._inspect_diff(targets=request_targets), "reason": request.reason, "tool_use_id": request.tool_call_id}
        elif tool_name == "read_artifact_ref":
            artifact_ref = str(request.input.get("artifact_ref") or (request_targets[0] if request_targets else "")).strip()
            artifact_payload = self.context.read_artifact(artifact_ref) if self.context.read_artifact is not None and artifact_ref else None
            result = {
                "tool": "read_artifact_ref",
                "tool_use_id": request.tool_call_id,
                "artifact_ref": artifact_ref,
                "found": artifact_payload is not None,
                "payload": artifact_payload,
                "reason": request.reason,
            }
        elif tool_name == "run_command":
            result = self._execute_command(request, decision)
        else:
            return self._failed_result(
                request,
                decision,
                structured_tool_error(
                    code="tool_not_implemented",
                    message=f"{tool_name or request.canonical_tool} is registered but not executable by ToolRouter.",
                    retryable=False,
                ),
            )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        self._emit_activity(activity, label, {"tool_use_id": request.tool_call_id, "tool": tool_name, "targets": request_targets, "status": "completed", "elapsed_ms": elapsed_ms})
        return self._completed_result(request, decision, result, loaded_context=local_context, duration_ms=elapsed_ms)

    def _execute_verification(self, request: ToolCallRequest, decision: ToolRouteDecision) -> ToolRouterResult:
        started_at = time.perf_counter()
        tool_name = request.tool
        targets = self._visible_targets(request) or ["miniapp"]
        activity, label = self._tool_activity(tool_name)
        self._emit_activity(activity, label, {"tool_use_id": request.tool_call_id, "tool": tool_name, "targets": targets, "status": "started"})
        execution, preview_details = self.context.execute_checks(targets)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        if tool_name == "browser_verify":
            workflow_results = [
                {
                    "name": result.name,
                    "status": result.status,
                    "details": result.details,
                    "logs": result.logs[-8:],
                    "diagnostics": result.diagnostics,
                }
                for result in execution.results
                if result.name in {"api_workflow_smoke", "browser_flow_smoke"}
            ]
            result_payload = {
                "tool": "browser_verify",
                "tool_use_id": request.tool_call_id,
                "contract": "serialized_read_only_browser_and_api_workflow_snapshot",
                "writes_files": False,
                "targets": targets,
                "workflow_results": workflow_results,
                "preview": preview_details,
                "reason": request.reason,
            }
        else:
            failed_checks = [
                {
                    "name": result.name,
                    "details": result.details,
                    "command": result.command,
                    "logs": result.logs[-8:],
                }
                for result in execution.results
                if result.status == "failed"
            ]
            result_payload = {
                "tool": "run_checks",
                "tool_use_id": request.tool_call_id,
                "contract": "serialized_read_only_validation_snapshot",
                "writes_files": False,
                "executes_arbitrary_commands": False,
                "mode": str(request.input.get("mode") or "exact"),
                "targets": targets,
                "failed_checks": failed_checks,
                "preview": preview_details,
                "reason": request.reason,
            }
        self._emit_activity(activity, label, {"tool_use_id": request.tool_call_id, "tool": tool_name, "targets": targets, "status": "completed", "elapsed_ms": elapsed_ms})
        return self._completed_result(request, decision, result_payload, duration_ms=elapsed_ms)

    def _defer_mutation(self, request: ToolCallRequest, decision: ToolRouteDecision) -> ToolRouterResult:
        changes, trace = self.deferred_mutations_from_calls(
            [{"tool": request.tool, "tool_use_id": request.tool_call_id, **request.input}]
        )
        if trace and str(trace[0].get("status") or "") == "failed":
            packet = trace[0].get("repair_packet") if isinstance(trace[0], dict) else None
            error = structured_tool_error(
                code=str((packet or {}).get("code") or trace[0].get("error_code") or "mutation_defer_failed"),
                message=str(trace[0].get("message") or "Mutating tool could not be converted into a deferred DraftAction."),
                retryable=True,
                details={"repair_packet": packet or {}, "trace": trace[0]},
            )
            return self._failed_result(request, decision, error)
        invalid_change = AgentEditValidator._first_invalid_file_change(changes)
        if invalid_change is not None:
            code, message = invalid_change
            packet = AgentEditValidator.repair_packet_for_issue(
                code=code,
                message=message,
                file_changes=changes,
                evidence={"tool_use_id": request.tool_call_id, "tool": request.tool},
            )
            return self._failed_result(
                request,
                decision,
                structured_tool_error(
                    code=code,
                    message=message,
                    retryable=True,
                    details={"repair_packet": packet, "trace": trace},
                ),
            )
        sandbox_service = getattr(self.context.workspace_service, "sandbox_service", None)
        if sandbox_service is None:
            from app.services.sandbox_service import SandboxService

            sandbox_service = SandboxService()
        sandbox_report = sandbox_service.preflight_apply(
            self.context.draft_source,
            [item.file_path for item in changes],
            profile="agent_draft_write",
            operation="apply",
            allow_generated=False,
        )
        if sandbox_report.status != "passed":
            message = "; ".join(item.message for item in sandbox_report.violations) or "Sandbox preflight failed for deferred mutation."
            return self._failed_result(
                request,
                decision,
                structured_tool_error(
                    code="sandbox_preflight_blocked",
                    message=message,
                    retryable=True,
                    details={"sandbox_report": sandbox_report.model_dump(mode="json"), "trace": trace},
                ),
            )
        proposals = [
            {
                "file_path": item.file_path,
                "operation": item.operation,
                "has_content": item.content is not None,
                "has_diff": item.diff is not None,
                "reason": item.reason,
            }
            for item in changes
        ]
        result = {
            "tool": request.tool,
            "tool_use_id": request.tool_call_id,
            "status": "deferred",
            "contract": "validated deferred DraftAction proposal; guarded apply pipeline remains the only writer",
            "writes_files": False,
            "proposal_count": len(proposals),
            "deferred_changes": proposals,
            "sandbox_report": sandbox_report.model_dump(mode="json"),
            "trace": trace,
            "reason": request.reason,
        }
        envelope = self._make_envelope(
            request,
            decision,
            result=result,
            status="deferred",
            approval={"required": True, "status": "deferred", "mode": "draft_action_proposal"},
            changed_files=[item.file_path for item in changes],
        )
        return ToolRouterResult(
            request=request,
            decision=decision,
            envelope=envelope,
            model_result=self._model_result(request, envelope, result),
            deferred_changes=changes,
        )

    def _execute_command(self, request: ToolCallRequest, decision: ToolRouteDecision) -> dict[str, object]:
        command = str(request.input.get("command") or "").strip()
        process_id = str(request.input.get("process_id") or request.tool_call_id)
        delta_counter = {"count": 0}

        def command_progress(payload: dict[str, Any]) -> None:
            status = str(payload.get("status") or "")
            if status == "started":
                self._emit_activity(
                    "process_started",
                    "Diagnostic process started",
                    {"tool_use_id": request.tool_call_id, "process_id": process_id, "tool": request.tool, "command": command, **payload},
                )
            elif status == "output_delta":
                delta_counter["count"] += 1
                if delta_counter["count"] in {1, 2, 3} or delta_counter["count"] % 20 == 0:
                    self._emit_activity(
                        "command_output_delta",
                        "Diagnostic command emitted output",
                        {"tool_use_id": request.tool_call_id, "process_id": process_id, "tool": request.tool, "command": command, **payload},
                    )
            elif status == "heartbeat":
                self._emit_activity(
                    "tool_progress",
                    "Diagnostic command still running",
                    {"tool_use_id": request.tool_call_id, "process_id": process_id, "tool": request.tool, "command": command, **payload},
                )
            elif status == "completed":
                self._emit_activity(
                    "process_completed" if payload.get("success") else "process_failed",
                    "Diagnostic process completed" if payload.get("success") else "Diagnostic process failed",
                    {"tool_use_id": request.tool_call_id, "process_id": process_id, "tool": request.tool, "command": command, **payload},
                )

        result = {
            **run_workspace_command(
                draft_source=self.context.draft_source,
                command=command,
                timeout_seconds=decision.timeout_seconds,
                max_output_chars=decision.output_cap_chars,
                progress_callback=command_progress,
                process_manager=self.context.process_manager,
                process_id=process_id,
                output_artifact_writer=self.context.output_artifact_writer,
            ),
            "reason": request.reason,
            "tool_use_id": request.tool_call_id,
        }
        policy_decision = result.get("policy_decision") if isinstance(result.get("policy_decision"), dict) else {}
        if (
            self.context.denied_action_writer is not None
            and (policy_decision.get("action") == "forbidden" or result.get("semantic_status") in {"blocked_by_policy", "blocked_by_sandbox"})
        ):
            artifact = self.context.denied_action_writer(
                {
                    "workspace_id": self.context.workspace_id,
                    "run_id": self.context.run_id,
                    "tool_call_id": request.tool_call_id,
                    "tool": request.canonical_tool,
                    "command": command,
                    "process_id": process_id,
                    "semantic_status": result.get("semantic_status"),
                    "policy_decision": policy_decision,
                    "error": result.get("error"),
                }
            )
            if artifact:
                result["denied_action_ref"] = artifact.get("ref") or artifact.get("denial_id")
        return result

    def _flush_concurrent_batch(self, indexed_requests: list[tuple[int, ToolCallRequest]]) -> ToolRouterBatchResult:
        if not indexed_requests:
            return ToolRouterBatchResult({}, [], [], [], {})
        started_at = time.perf_counter()
        batch_id = f"batch_{indexed_requests[0][0] + 1}"
        self._emit_activity(
            "reading",
            "Running read-only tool batch",
            {
                "batch_id": batch_id,
                "tool_use_id": batch_id,
                "status": "started",
                "tool_count": len(indexed_requests),
                "tools": [request.tool for _, request in indexed_requests],
            },
        )
        concurrent_outputs: list[tuple[int, ToolRouterResult]] = []
        max_workers = min(max(1, self.context.max_parallel_read_tools), len(indexed_requests))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-read-tool") as executor:
            future_to_request = {
                executor.submit(self.route_one, request): (index, request)
                for index, request in indexed_requests
            }
            for future in as_completed(future_to_request):
                index, request = future_to_request[future]
                try:
                    result = future.result()
                except Exception as exc:
                    decision = self._decide(request)
                    result = self._failed_result(
                        request,
                        decision,
                        structured_tool_error(
                            code="tool_execution_error",
                            message=str(exc),
                            retryable=True,
                            details={"error_class": exc.__class__.__name__},
                        ),
                    )
                concurrent_outputs.append((index, result))
        loaded_context: dict[str, str] = {}
        model_results: list[dict[str, object]] = []
        envelopes: list[dict[str, Any]] = []
        deferred_changes: list[DraftAction] = []
        for _, result in sorted(concurrent_outputs, key=lambda item: item[0]):
            loaded_context.update(result.loaded_context)
            model_results.append(result.model_result)
            envelopes.append(result.envelope)
            deferred_changes.extend(result.deferred_changes)
        summary = summarize_tool_batch(
            batch_id=batch_id,
            requests=[{"tool": request.tool, **request.input} for _, request in indexed_requests],
            results=model_results,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            status="completed",
        )
        self._append_batch_summary(summary)
        self._emit_activity("tool_use_summary", summary["summary"], {**summary, "status": "completed"})
        return ToolRouterBatchResult(loaded_context, model_results, envelopes, deferred_changes, summary)

    def _completed_result(
        self,
        request: ToolCallRequest,
        decision: ToolRouteDecision,
        result: dict[str, Any],
        *,
        loaded_context: dict[str, str] | None = None,
        duration_ms: int | None = None,
    ) -> ToolRouterResult:
        compacted, truncation, artifacts = self._compact_result(request, decision, result)
        envelope = self._make_envelope(
            request,
            decision,
            result=compacted,
            status="completed",
            artifacts=artifacts,
            truncation=truncation,
            duration_ms=duration_ms,
        )
        return ToolRouterResult(
            request=request,
            decision=decision,
            envelope=envelope,
            model_result=self._model_result(request, envelope, compacted),
            loaded_context=loaded_context or {},
        )

    def _failed_result(
        self,
        request: ToolCallRequest,
        decision: ToolRouteDecision,
        error: dict[str, Any],
    ) -> ToolRouterResult:
        result = {
            "tool": request.tool or request.canonical_tool,
            "tool_use_id": request.tool_call_id,
            "status": "failed",
            "error": error.get("message"),
            "error_code": error.get("code"),
            "details": error.get("details") or {},
        }
        envelope = self._make_envelope(request, decision, result=result, error=error, status="failed")
        return ToolRouterResult(
            request=request,
            decision=decision,
            envelope=envelope,
            model_result=self._model_result(request, envelope, result),
        )

    def _make_envelope(
        self,
        request: ToolCallRequest,
        decision: ToolRouteDecision,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        status: str | None = None,
        approval: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        truncation: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        changed_files: list[str] | None = None,
    ) -> dict[str, Any]:
        resolved_approval = self._approval_payload(decision, approval)
        return tool_envelope(
            tool=request.canonical_tool,
            input_payload=self._protocol_input(request),
            result=result or {},
            risk=decision.risk,  # type: ignore[arg-type]
            approval=resolved_approval,
            artifacts=artifacts,
            error=error,
            status=status,
            sandbox_profile=decision.sandbox_profile,
            truncation=truncation,
            tool_call_id=request.tool_call_id,
            duration_ms=duration_ms,
            changed_files=changed_files,
        )

    @staticmethod
    def _approval_payload(decision: ToolRouteDecision, override: dict[str, Any] | None) -> dict[str, Any]:
        if override is not None:
            payload = dict(override)
            payload.setdefault("class", decision.approval_class)
            payload.setdefault("policy", decision.reason)
            return payload
        if decision.approval_class == "policy":
            return {"required": False, "status": "policy_checked", "class": "policy", "policy": decision.reason}
        if decision.approval_class == "human":
            return {"required": True, "status": "pending", "class": "human", "policy": decision.reason}
        if decision.approval_class == "forbidden":
            return {"required": True, "status": "rejected", "class": "forbidden", "policy": decision.reason}
        return {"required": False, "status": "not_required", "class": "none"}

    @staticmethod
    def _model_result(request: ToolCallRequest, envelope: dict[str, Any], result: dict[str, Any]) -> dict[str, object]:
        payload: dict[str, object] = dict(result or {})
        payload.setdefault("tool", request.tool or request.canonical_tool)
        payload["tool_use_id"] = request.tool_call_id
        payload.setdefault("status", envelope.get("status") or "completed")
        payload["envelope"] = envelope
        payload["tool_envelope"] = envelope
        return payload

    def _compact_result(
        self,
        request: ToolCallRequest,
        decision: ToolRouteDecision,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, default=str)
        should_truncate = len(encoded) > decision.output_cap_chars
        should_spill = decision.artifact_spill_policy == "always" or (
            decision.artifact_spill_policy == "on_truncation" and should_truncate
        )
        if decision.artifact_spill_policy == "never" or not should_spill:
            return result, {"truncated": False}, []
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        excerpt = encoded[: max(200, decision.output_cap_chars)]
        artifacts: list[dict[str, Any]] = []
        if self.context.output_spill_writer is not None:
            artifact = self.context.output_spill_writer(
                f"tool-output:{self.context.run_id}:{request.tool_call_id}:{digest[:12]}",
                {"tool": request.canonical_tool, "sha256": digest, "result": result},
            )
            if artifact:
                artifacts.append(self._tool_result_artifact_ref(artifact))
        elif self.context.output_artifact_writer is not None:
            artifact = self.context.output_artifact_writer(
                {
                    "process_id": f"tool:{request.tool_call_id}",
                    "stream": "tool",
                    "command": f"tool:{request.canonical_tool}",
                    "content": encoded,
                    "head_tail": _head_tail_payload(encoded, max_chars=decision.output_cap_chars),
                    "semantic_status": "completed",
                    "metadata": {
                        "source": "tool_result_spill",
                        "tool": request.canonical_tool,
                        "model_tool": request.tool,
                        "tool_call_id": request.tool_call_id,
                        "sha256": digest,
                    },
                }
            )
            if artifact:
                artifacts.append(self._tool_result_artifact_ref(artifact))
        artifact_ref = str(artifacts[0].get("ref") or "") if artifacts else ""
        if not should_truncate:
            enriched = dict(result)
            enriched["artifact_ref"] = artifact_ref
            enriched["artifacts"] = artifacts
            return enriched, {
                "truncated": False,
                "sha256": digest,
                "original_chars": len(encoded),
                "artifact_ref": artifact_ref,
                "spilled": bool(artifacts),
                "spill_policy": decision.artifact_spill_policy,
            }, artifacts
        compacted = {
            "tool": str(result.get("tool") or request.tool),
            "tool_use_id": request.tool_call_id,
            "status": str(result.get("status") or "completed"),
            "excerpt": excerpt,
            "sha256": digest,
            "original_chars": len(encoded),
            "artifact_ref": artifact_ref,
            "artifacts": artifacts,
        }
        return compacted, {
            "truncated": True,
            "sha256": digest,
            "excerpt_chars": len(excerpt),
            "original_chars": len(encoded),
            "artifact_ref": artifact_ref,
            "spilled": bool(artifacts),
            "spill_policy": decision.artifact_spill_policy,
        }, artifacts

    @staticmethod
    def _tool_result_artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            **artifact,
            "kind": "tool_result",
            "mime_type": "application/json",
            "label": "Full tool result",
        }

    def _visible_workspace_tree(self) -> list[dict[str, str]]:
        if self._workspace_tree is None:
            self._workspace_tree = [
                path
                for path in self.context.workspace_service.file_tree(self.context.workspace_id, run_id=self.context.run_id)
                if _is_model_visible_path(path.get("path") if isinstance(path, dict) else path)
            ]
        return self._workspace_tree

    def _visible_targets(self, request: ToolCallRequest) -> list[str]:
        return [
            _strip_leading_dot_slash(item)
            for item in request.input.get("targets") or []
            if str(item or "").strip() and _is_model_visible_path(_strip_leading_dot_slash(item))
        ]

    @staticmethod
    def _blocked_targets(request: ToolCallRequest) -> list[str]:
        return [
            _strip_leading_dot_slash(item)
            for item in request.input.get("targets") or []
            if str(item or "").strip() and not _is_model_visible_path(_strip_leading_dot_slash(item))
        ]

    def _inspect_diff(self, *, targets: list[str]) -> dict[str, object]:
        diff_text = self.context.workspace_service.diff(self.context.workspace_id, run_id=self.context.run_id)
        paths = _paths_from_diff(diff_text)
        normalized_targets = [target.rstrip("/") for target in targets if target.strip()]
        if normalized_targets:
            selected_chunks: list[str] = []
            current_chunk: list[str] = []
            current_path = ""
            for line in diff_text.splitlines():
                if line.startswith("diff --git "):
                    if current_chunk and _path_matches_targets(current_path, normalized_targets):
                        selected_chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_path = ""
                    match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
                    if match:
                        current_path = _strip_leading_dot_slash(match.group(2))
                    continue
                current_chunk.append(line)
            if current_chunk and _path_matches_targets(current_path, normalized_targets):
                selected_chunks.append("\n".join(current_chunk))
            diff_text = "\n".join(selected_chunks)
        return {"tool": "inspect_diff", "paths": paths, "diff": diff_text[:12000], "truncated": len(diff_text) > 12000}

    def _tool_activity(self, tool_name: str) -> tuple[str, str]:
        spec = AgentToolRegistry.spec(tool_name)
        if spec is None:
            return "reading", "Reading workspace context"
        return spec.activity, spec.progress_label

    def _emit_activity(self, kind: str, label: str, details: dict[str, Any] | None = None) -> None:
        if self.context.append_activity is None:
            return
        with self._activity_lock:
            self.context.append_activity(kind, label, details or {})

    def _append_batch_summary(self, summary: dict[str, object]) -> None:
        if self.context.append_batch_summary is not None:
            self.context.append_batch_summary(summary)


def _json_type_matches(value: Any, expected: str) -> bool:
    if value is None:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return True


def _head_tail_payload(content: str, *, max_chars: int) -> dict[str, Any]:
    text = str(content or "")
    cap = max(200, int(max_chars or 6000))
    if len(text) <= cap:
        return {
            "head": text,
            "tail": "",
            "excerpt": text,
            "total_chars": len(text),
            "omitted_chars": 0,
            "chunk_count": 1 if text else 0,
        }
    head_chars = max(100, cap // 2)
    tail_chars = max(100, cap - head_chars)
    head = text[:head_chars]
    tail = text[-tail_chars:]
    omitted = max(0, len(text) - len(head) - len(tail))
    return {
        "head": head,
        "tail": tail,
        "excerpt": f"{head}\n...[omitted {omitted} chars]...\n{tail}",
        "total_chars": len(text),
        "omitted_chars": omitted,
        "chunk_count": 1,
    }


def _is_safe_exact_path(file_path: str) -> bool:
    normalized = str(file_path or "").replace("\\", "/")
    return bool(normalized and normalized.startswith("miniapp/") and not normalized.startswith(("/", "~")) and ".." not in normalized.split("/"))


def _exact_edit_failure(
    *,
    file_path: str,
    current: str | None,
    old_string: str,
    replace_all: bool,
    freshness: dict[str, object] | None = None,
    similar_path: str | None = None,
) -> tuple[str, str, dict[str, object]] | None:
    if not _is_safe_exact_path(file_path):
        return ("unsafe_path", "Exact edit must target a relative file path inside miniapp/.", {})
    freshness_payload = dict(freshness or {})
    freshness_status = str(freshness_payload.get("status") or "").strip()
    if freshness_status in {"unread", "stale", "partial"}:
        code = "file_not_read" if freshness_status == "unread" else "stale_file" if freshness_status == "stale" else "partial_read"
        return (
            code,
            f"{file_path} must be read with a fresh full read before edit_file_exact.",
            {"target_files": [file_path], "freshness": freshness_payload},
        )
    if current is None:
        if similar_path:
            return (
                "similar_path_found",
                f"{file_path} could not be read before exact edit; a similar file exists at {similar_path}.",
                {"target_files": [similar_path], "requested_file_path": file_path, "similar_path": similar_path},
            )
        return ("file_missing", f"{file_path} could not be read before exact edit.", {"target_files": [file_path]})
    if not old_string:
        return ("old_string_not_found", f"{file_path} exact edit requires a non-empty old_string.", {"target_files": [file_path]})
    count = current.count(old_string)
    if count == 0:
        return ("old_string_not_found", f"{file_path} old_string was not found exactly.", {"target_files": [file_path], "old_string_length": len(old_string)})
    if count > 1 and not replace_all:
        return ("multiple_matches", f"{file_path} old_string matched {count} times; make it unique or set replace_all.", {"target_files": [file_path], "match_count": count})
    return None


def _normalize_agent_file_path(raw_path: object, *, worker_id: str | None = None) -> str:
    path = _strip_leading_dot_slash(raw_path)
    if not path:
        return path
    if path.startswith("source/"):
        path = path[len("source/") :]
    if path.startswith("miniapp/"):
        return path
    worker = canonical_worker_id(str(worker_id or "").strip())
    role_by_worker = {
        "client_surface_worker": "client",
        "specialist_surface_worker": "specialist",
        "manager_surface_worker": "manager",
    }
    if worker in role_by_worker:
        role = role_by_worker[worker]
        if path in {"index.html", "app.js", "styles.css"}:
            return f"miniapp/app/static/{role}/{path}"
    if worker == "test_verifier_worker" and path in {"test_generated_app.py", "generated_app.test.mjs"}:
        return f"miniapp/tests/{path}"
    if worker == "backend_api_worker":
        if path in {"main.py", "db.py", "schemas.py"}:
            return f"miniapp/app/{path}"
        if path.endswith(".py") and "/" not in path:
            return f"miniapp/app/routes/{path}"
    if path.startswith(("app/", "tests/")) or path in {"Dockerfile", "requirements.txt"}:
        return f"miniapp/{path}"
    if path.startswith("static/"):
        return f"miniapp/app/{path}"
    if path.startswith("routes/"):
        return f"miniapp/app/{path}"
    if path.startswith("generated/"):
        return f"miniapp/app/{path}"
    return path


def _path_matches_targets(path: str, targets: list[str]) -> bool:
    if not targets:
        return True
    normalized = _strip_leading_dot_slash(path)
    return any(normalized == target or normalized.startswith(f"{target.rstrip('/')}/") for target in targets)


def _paths_from_diff(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in str(diff_text or "").splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
        if match:
            path = _strip_leading_dot_slash(match.group(2))
            if path and path not in paths:
                paths.append(path)
    return paths


def _strip_leading_dot_slash(raw_path: object) -> str:
    path = str(raw_path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_model_visible_path(raw_path: object) -> bool:
    path = _strip_leading_dot_slash(raw_path)
    return bool(path) and not AgentEditValidator.is_protected_path(path)
