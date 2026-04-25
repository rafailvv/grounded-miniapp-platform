from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from app.ai.model_registry import models_for_role, resolve_model_profile
from app.ai.openai_client import OpenAIClient
from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    FixAttemptRecord,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    ValidationSnapshot,
)
from app.modules.miniapp_agent_loop.engine import WorkspaceLoopEngine
from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    normalize_tool_requests,
    search_workspace_files,
    summarize_read_file_payloads,
    truncate_tool_text,
)
from app.modules.miniapp_agent_loop.types import WorkspaceLoopCallbacks, WorkspaceLoopResult, WorkspaceLoopTurnPlan
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.services.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

ROLE_ORDER = ("client", "specialist", "manager")
QUALITY_FIDELITY = {
    GenerationMode.FAST: "fast_app",
    GenerationMode.QUALITY: "quality_app",
    GenerationMode.BALANCED: "balanced_app",
    GenerationMode.BASIC: "basic_app",
}
READ_ONLY_WRITE_PREFIXES = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".next/",
    ".vite/",
    "miniapp/tests/",
)
SEED_CONTEXT_PATHS = (
    "README.md",
    "docs/agent-guidelines.md",
    "miniapp/app/main.py",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/routes/role_routes.py",
    "miniapp/app/routes/role_pages.py",
    "miniapp/app/static/shared/base.css",
    "miniapp/app/static/client/index.html",
    "miniapp/app/static/client/app.js",
    "miniapp/app/generated/route_manifest.json",
)


class WorkspaceCodeAgentRuntime:
    """Single agentic code path for create, edit, fix, refine, and visual changes."""

    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        check_runner: CheckRunner,
        preview_service: PreviewService,
        runtime_manager: PreviewRuntimeManager,
        openai_client: OpenAIClient,
        workspace_log_service: WorkspaceLogService,
        workspace_loop_engine: WorkspaceLoopEngine,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.check_runner = check_runner
        self.preview_service = preview_service
        self.runtime_manager = runtime_manager
        self.openai_client = openai_client
        self.workspace_log_service = workspace_log_service
        self.workspace_loop_engine = workspace_loop_engine

    def get_job(self, job_id: str) -> JobRecord:
        payload = self.store.get("jobs", job_id)
        if not payload:
            raise KeyError(f"Job not found: {job_id}")
        return JobRecord.model_validate(payload)

    def latest_job_for_workspace(self, workspace_id: str) -> JobRecord | None:
        latest: JobRecord | None = None
        for payload in self.store.list("jobs"):
            if str(payload.get("workspace_id") or "") != workspace_id:
                continue
            candidate = JobRecord.model_validate(payload)
            if latest is None or candidate.updated_at > latest.updated_at:
                latest = candidate
        return latest

    def current_report(self, workspace_id: str, report_type: str) -> dict[str, Any] | None:
        payload = self.store.get("reports", f"{report_type}:{workspace_id}")
        return dict(payload) if isinstance(payload, dict) else None

    def retry_from_job(self, job_id: str, *, should_stop: Callable[[], bool] | None = None) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            mode=job.mode,
            target_platform=job.target_platform,
            preview_profile=job.preview_profile,
            generation_mode=job.generation_mode,
            intent="edit" if job.mode == "fix" else "auto",
            target_role_scope=[],
            model_profile=job.model_profile or "",
            linked_run_id=job.linked_run_id,
            error_context=job.error_context,
        )
        return self.generate(job.workspace_id, request, should_stop=should_stop)

    def append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self._append_event(job, event_type, message, details)

    def validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        return self._validation_snapshot_from_execution(execution)

    def generate(
        self,
        workspace_id: str,
        request: GenerateRequest,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> JobRecord:
        started_at = time.perf_counter()
        workspace = self.workspace_service.get_workspace(workspace_id)
        generation_mode = self._generation_mode(request.generation_mode)
        model_profile = resolve_model_profile(request.model_profile, generation_mode)
        role_scope = [role for role in request.target_role_scope if role in ROLE_ORDER] or list(ROLE_ORDER)
        run_id = request.linked_run_id or f"agent_{int(time.time() * 1000)}"
        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode=request.mode,
            status="running",
            generation_mode=generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace.current_revision_id,
            fidelity=QUALITY_FIDELITY[generation_mode],  # type: ignore[arg-type]
            llm_enabled=self.openai_client.enabled,
            llm_provider=(self.openai_client.configuration().get("routing") or {}).get("provider") if self.openai_client.enabled else None,
            model_profile=model_profile,
            linked_run_id=run_id,
            error_context=request.error_context,
            current_fix_phase="agent_loop",
            execution_class="shell_app",
        )
        self._clear_agent_reports(workspace_id)
        self._clear_trace(workspace_id)
        self._save_job(job)
        self._append_event(job, "job_started", "Workspace code agent started.", {"run_id": run_id, "mode": request.mode})

        if not self.openai_client.enabled:
            job.status = "blocked"
            job.outcome_kind = "blocked_generation"
            job.summary = "Workspace code agent requires OpenAI."
            job.failure_reason = "Set OPENAI_API_KEY before generating or editing a workspace."
            job.failure_class = "generation.llm_required"
            job.current_fix_phase = "failed"
            job.latency_breakdown["agent_total_ms"] = int((time.perf_counter() - started_at) * 1000)
            self._append_event(job, "job_failed", job.summary, {"failure_class": job.failure_class})
            self._save_job(job)
            return job

        draft_source = self._prepare_draft(workspace_id=workspace_id, run_id=run_id, request=request)
        self._store_report(
            f"spec:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "source": "user_prompt",
                "prompt": request.prompt,
                "runtime": "workspace_code_agent",
            "domain_policy": "Prompt semantics are authoritative; no product domain is assumed.",
            },
        )
        self._store_report(
            f"execution_class:{workspace_id}",
            {"workspace_id": workspace_id, "execution_class": "shell_app", "runtime": "workspace_code_agent"},
        )

        with self.openai_client.routing_context(model_profile=model_profile, generation_mode=generation_mode):
            loop_result = self._run_loop(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request.model_copy(update={"model_profile": model_profile}),
                job=job,
                draft_source=draft_source,
                role_scope=role_scope,
                generation_mode=generation_mode,
                should_stop=should_stop,
            )
        return self._finalize_job(
            job=job,
            loop_result=loop_result,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )

    def _prepare_draft(self, *, workspace_id: str, run_id: str, request: GenerateRequest) -> Path:
        source_run_id = str(request.resume_from_run_id or "").strip()
        if source_run_id and source_run_id != run_id and self.workspace_service.draft_exists(workspace_id, source_run_id):
            return self.workspace_service.clone_draft(workspace_id, source_run_id, run_id)
        if self.workspace_service.draft_exists(workspace_id, run_id):
            return self.workspace_service.ensure_draft(workspace_id, run_id)
        return self.workspace_service.prepare_draft(workspace_id, run_id)

    def _run_loop(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        should_stop: Callable[[], bool] | None,
    ) -> WorkspaceLoopResult:
        seed_context = self._seed_file_context(workspace_id, run_id)
        tool_results: list[dict[str, object]] = []
        last_changed_files: list[str] = ["miniapp", "docs", "README.md"]

        def _execute_checks(changed_files: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            nonlocal last_changed_files
            last_changed_files = list(changed_files or last_changed_files)
            self._append_event(job, "frontend_build_started", "Running platform invariant checks.")
            check_profile = "full" if generation_mode == GenerationMode.QUALITY else "fast_gate"
            execution = self.check_runner.run(
                workspace_id=workspace_id,
                run_id=run_id,
                source_dir=draft_source,
                changed_files=list(last_changed_files),
                preview_run_id=run_id,
                scope_mode="agentic",
                check_profile=check_profile,
            )
            if (
                check_profile == "fast_gate"
                and self._fast_gate_passed(execution.results)
                and self.workspace_service.diff(workspace_id, run_id=run_id).strip()
            ):
                self._append_event(job, "frontend_build_started", "Running final generated app checks.")
                execution = self.check_runner.run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    source_dir=draft_source,
                    changed_files=list(last_changed_files),
                    preview_run_id=run_id,
                    scope_mode="agentic",
                    check_profile="full",
                )
            prompt_smoke = self._prompt_alignment_smoke(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=request.prompt,
            )
            execution.results.append(prompt_smoke)
            execution.completed_at = datetime.now(timezone.utc)
            preview = self.preview_service.get(workspace_id)
            preview_details = {
                "status": "skipped",
                "stage": getattr(preview, "stage", "idle"),
                "progress_percent": getattr(preview, "progress_percent", 0),
                "logs": list(getattr(preview, "logs", [])),
                "last_error": getattr(preview, "last_error", None),
                "containers": [],
                "container_logs": {},
            }
            if any(item.status == "failed" for item in execution.results):
                self._collect_preview_diagnostics(workspace_id, preview_details)
            return execution, preview_details

        def _plan_turn(
            *,
            attempt: int,
            latest_execution: CheckExecutionRecord,
            latest_preview_details: dict[str, object],
            validation_snapshot: ValidationSnapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            extra_file_context: dict[str, str] = {}
            local_tool_results = list(tool_results)
            seen_tool_requests: set[str] = set()
            for tool_round in range(self._tool_round_limit(generation_mode) + 1):
                llm_payload = self._request_agent_turn(
                    job=job,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request=request,
                    attempt=attempt,
                    tool_round=tool_round,
                    context_mode=context_mode,
                    repeated_no_progress=repeated_no_progress,
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    seed_context=seed_context,
                    extra_file_context=extra_file_context,
                    tool_results=local_tool_results,
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=latest_diff_summary,
                )
                if "error" in llm_payload:
                    return WorkspaceLoopTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(llm_payload.get("error") or ""),
                        diagnosis=str(llm_payload.get("error") or ""),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        failure_class="generation.agent_invalid_response",
                        failure_signature="generation.agent_invalid_response",
                        root_cause_summary=str(llm_payload.get("error") or ""),
                    )
                outcome = str(llm_payload.get("outcome") or "").strip().lower()
                raw_tool_requests = self._agent_tool_requests(llm_payload.get("tool_requests") or [])
                if outcome == "tool_request" or raw_tool_requests:
                    signature = json.dumps(raw_tool_requests, ensure_ascii=True, sort_keys=True)
                    if signature in seen_tool_requests:
                        return WorkspaceLoopTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent repeated an already satisfied tool request.",
                            diagnosis="The requested diagnostic context is already available; the next turn must patch code.",
                            files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                            metadata={"tool_requests": raw_tool_requests},
                        )
                    seen_tool_requests.add(signature)
                    new_context, executed_results = self._execute_tool_requests(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        draft_source=draft_source,
                        tool_requests=raw_tool_requests,
                        execute_checks=_execute_checks,
                    )
                    extra_file_context.update(new_context)
                    local_tool_results.extend(executed_results)
                    tool_results.extend(executed_results)
                    if tool_round < self._tool_round_limit(generation_mode):
                        continue
                    return WorkspaceLoopTurnPlan(
                        outcome="needs_context",
                        assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent requested more tools than the turn budget allows."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        metadata={"tool_requests": raw_tool_requests},
                    )
                if outcome == "fatal_invalid_response":
                    return WorkspaceLoopTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(llm_payload.get("assistant_message") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent declared a fatal invalid response."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    )
                operations = self._coerce_operations(llm_payload.get("operations") or [])
                if not operations:
                    return WorkspaceLoopTurnPlan(
                        outcome="no_op",
                        assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent did not return file edits."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        metadata={"raw_response": llm_payload},
                    )
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or "Agent prepared code edits."),
                    diagnosis=str(llm_payload.get("diagnosis") or ""),
                    operations=operations,
                    files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    expected_verification=str(llm_payload.get("expected_verification") or ""),
                    rationale_by_file={str(key): str(value) for key, value in dict(llm_payload.get("rationale_by_file") or {}).items()},
                    metadata={
                        "tool_results": list(local_tool_results),
                        "acceptance_checks": list(llm_payload.get("acceptance_checks") or []),
                    },
                )
            return WorkspaceLoopTurnPlan(
                outcome="no_op",
                assistant_message="Agent turn ended without producing edits.",
                diagnosis="Agent turn ended without producing edits.",
                files_read=list(seed_context.keys()),
            )

        callbacks = WorkspaceLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self._validation_snapshot_from_execution,
            completion_state=lambda results, preview_details, validation_snapshot=None: self._completion_state(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                results=results,
                preview_details=preview_details,
                validation_snapshot=validation_snapshot,
            ),
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=lambda operations: list(operations),
            post_apply_stabilize=None,
            append_event=self._append_event,
            append_trace=self._append_trace,
            store_report=self._store_report,
            allow_optimistic_completion=True,
            stop_if_requested=should_stop,
        )
        return self.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            max_attempts=self._max_attempts(generation_mode),
            initial_operations=[],
            initial_assistant_message="Workspace code agent initialized.",
            initial_files_read=list(seed_context.keys()),
            initial_changed_files=last_changed_files,
            callbacks=callbacks,
        )

    def _request_agent_turn(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        tool_round: int,
        context_mode: str,
        repeated_no_progress: int,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, object],
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> dict[str, Any]:
        self._append_event(
            job,
            "agent_turn_started",
            "Workspace code agent is planning the next code edit.",
            {"attempt": attempt, "tool_round": tool_round, "context_mode": context_mode},
        )
        try:
            generation_mode = self._generation_mode(request.generation_mode)
            primary_model = models_for_role(
                "agent_turn",
                model_profile=request.model_profile,
                generation_mode=generation_mode,
            )
            response = self.openai_client.generate_agent_turn(
                schema_name="workspace_code_agent_turn_v1",
                schema=self._agent_turn_schema(),
                system_prompt=self._agent_system_prompt(),
                user_prompt=self._agent_user_prompt(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request=request,
                    attempt=attempt,
                    tool_round=tool_round,
                    context_mode=context_mode,
                    repeated_no_progress=repeated_no_progress,
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    seed_context=seed_context,
                    extra_file_context=extra_file_context,
                    tool_results=tool_results,
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=latest_diff_summary,
                ),
                prompt_cache_key=self._prompt_cache_key(workspace_id, run_id, request.prompt),
                stable_prefix=self._agent_system_prompt(),
                model_override=primary_model,
                responses_tuning_override={"reasoning": {"effort": "medium" if generation_mode == GenerationMode.FAST else "high"}},
            )
            job.llm_model = str(response.get("model") or "")
            job.cache_stats = self._merge_cache_stats(job.cache_stats, response.get("cache_stats") or {})
            self._save_job(job)
            payload = response.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload if isinstance(payload, dict) else {"error": "Agent returned a non-object payload."}
        except Exception as exc:
            logger.exception("workspace_code_agent_turn_failed workspace_id=%s run_id=%s", workspace_id, run_id)
            return {"error": f"Workspace code agent turn failed: {exc}"}

    @staticmethod
    def _agent_turn_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {"type": "string", "enum": ["patch_ready", "tool_request", "no_progress", "fatal_invalid_response"]},
                "assistant_message": {"type": "string"},
                "diagnosis": {"type": "string"},
                "tool_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tool": {"type": "string", "enum": ["list_files", "read_files", "search_files", "inspect_diff", "run_checks"]},
                            "mode": {"type": "string", "enum": ["exact", "final"]},
                            "targets": {"type": "array", "items": {"type": "string"}},
                            "pattern": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["tool", "targets", "reason"],
                    },
                },
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "file_path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["create", "replace", "delete", "patch"]},
                            "content": {"type": ["string", "null"]},
                            "diff": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["file_path", "operation", "reason"],
                    },
                },
                "expected_verification": {"type": "string"},
                "rationale_by_file": {"type": "object", "additionalProperties": {"type": "string"}},
                "acceptance_checks": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "outcome",
                "assistant_message",
                "diagnosis",
                "tool_requests",
                "operations",
                "expected_verification",
                "rationale_by_file",
                "acceptance_checks",
            ],
        }

    @staticmethod
    def _agent_system_prompt() -> str:
        return (
            "You are a universal workspace code agent for a Telegram mini-app platform. "
            "Work like a coding agent: inspect files, edit source, run checks, and converge to a working app. "
            "The user's prompt is the only source of product semantics. Do not impose any generic queue, ticketing, lifecycle, or CRUD product model unless the prompt explicitly asks for it. "
            "Existing template docs and files are technical shell context only. They must not override the user's domain. "
            "Preserve the FastAPI + static-file shell, preview bridge, and role-root routing unless the user asks otherwise. "
            "For create tasks, build a complete domain-specific app from the prompt. For e-commerce prompts, prefer products, catalog, cart, orders, and management surfaces, never generic intake or application tracking. "
            "Use hunk patches when small edits are enough. Use full-file create/replace only when creating or substantially rewriting a file. "
            "Do not edit generated app tests or generated manifests to hide failures. Repair app code and platform invariants instead. "
            "Return only the structured JSON payload requested by the schema."
        )

    def _agent_user_prompt(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        tool_round: int,
        context_mode: str,
        repeated_no_progress: int,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, object],
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> str:
        file_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        payload = {
            "task": "Edit the draft workspace to satisfy the user prompt and pass platform invariant checks.",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "mode": request.mode,
            "intent": request.intent,
            "generation_mode": str(getattr(request.generation_mode, "value", request.generation_mode) or ""),
            "attempt": attempt,
            "tool_round": tool_round,
            "context_mode": context_mode,
            "repeated_no_progress": repeated_no_progress,
            "user_prompt": request.prompt,
            "error_context": request.error_context.model_dump(mode="json") if request.error_context else None,
            "role_scope": list(request.target_role_scope or ROLE_ORDER),
            "file_tree": file_tree[:240],
            "file_contexts": {
                **self._compact_file_contexts(seed_context, max_files=22 if context_mode == "full_bundle" else 14),
                **self._compact_file_contexts(extra_file_context, max_files=12),
            },
            "latest_checks": self._compact_checks(latest_execution),
            "preview": latest_preview_details,
            "latest_diff_summary": latest_diff_summary,
            "last_turn_summary": last_turn_summary,
            "tool_results": tool_results[-8:],
            "rules": [
                "If you need more context, request list_files/read_files/search_files/inspect_diff/run_checks.",
                "If you have enough context, return outcome=patch_ready with operations.",
                "Every create or replace operation must include full resulting file content.",
                "Every patch operation must include a unified diff for exactly file_path.",
                "Do not create generic queue, ticketing, lifecycle, or CRUD language unless the prompt asks for it.",
                "Do not return no_progress unless you can explain the exact unresolved blocker.",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    def _execute_tool_requests(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_requests: list[dict[str, Any]],
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        loaded_context: dict[str, str] = {}
        tool_results: list[dict[str, object]] = []
        workspace_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        for request_item in tool_requests:
            tool_name = str(request_item.get("tool") or "").strip().lower()
            targets = [str(item or "").strip().lstrip("./") for item in request_item.get("targets") or [] if str(item or "").strip()]
            reason = str(request_item.get("reason") or "").strip()
            if tool_name == "list_files":
                tool_results.append({**list_workspace_files(workspace_tree=workspace_tree, targets=targets), "reason": reason})
                continue
            if tool_name == "read_files":
                for target in targets[:16]:
                    content = self.workspace_service.try_read_text_file(workspace_id, target, run_id=run_id)
                    if content is not None:
                        loaded_context[target] = content
                tool_results.append(
                    {
                        "tool": "read_files",
                        "targets": targets,
                        "files": summarize_read_file_payloads(file_contents=loaded_context),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                tool_results.append(
                    {
                        **search_workspace_files(
                            workspace_tree=workspace_tree,
                            read_text_file=lambda relative_path: self.workspace_service.try_read_text_file(
                                workspace_id,
                                relative_path,
                                run_id=run_id,
                            ),
                            pattern=pattern,
                            targets=targets,
                        ),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "inspect_diff":
                tool_results.append({**self._inspect_diff(workspace_id=workspace_id, run_id=run_id, targets=targets), "reason": reason})
                continue
            if tool_name == "run_checks":
                mode = str(request_item.get("mode") or "exact").strip().lower()
                execution, preview_details = execute_checks(targets or ["miniapp"])
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
                tool_results.append(
                    {
                        "tool": "run_checks",
                        "mode": mode,
                        "targets": targets or ["miniapp"],
                        "failed_checks": failed_checks,
                        "preview": preview_details,
                        "reason": reason,
                    }
                )
        return loaded_context, tool_results

    @staticmethod
    def _agent_tool_requests(raw_tool_requests: list[Any]) -> list[dict[str, Any]]:
        allowed_tools = {"list_files", "read_files", "search_files", "inspect_diff", "run_checks"}
        return [
            item
            for item in normalize_tool_requests(raw_tool_requests)
            if str(item.get("tool") or "").strip().lower() in allowed_tools
        ]

    def _inspect_diff(self, *, workspace_id: str, run_id: str, targets: list[str]) -> dict[str, object]:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        paths = self._paths_from_diff(diff_text)
        normalized_targets = [target.rstrip("/") for target in targets if target.strip()]
        if normalized_targets:
            selected_chunks: list[str] = []
            current_chunk: list[str] = []
            current_path = ""
            for line in diff_text.splitlines():
                if line.startswith("diff --git "):
                    if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                        selected_chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_path = ""
                    match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
                    if match:
                        current_path = match.group(2).strip().strip('"')
                    continue
                if current_chunk:
                    current_chunk.append(line)
            if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                selected_chunks.append("\n".join(current_chunk))
            diff_text = "\n".join(selected_chunks)
            paths = [path for path in paths if self._path_matches_targets(path, normalized_targets)]
        return {
            "tool": "inspect_diff",
            "targets": targets,
            "paths": paths,
            "diff": truncate_tool_text(diff_text, max_chars=12000),
        }

    @staticmethod
    def _path_matches_targets(path: str, targets: list[str]) -> bool:
        normalized = path.strip().lstrip("./")
        return any(normalized == target or normalized.startswith(target.rstrip("/") + "/") for target in targets)

    def _coerce_operations(self, raw_operations: list[Any]) -> list[DraftFileOperation]:
        operations: list[DraftFileOperation] = []
        for item in raw_operations:
            if not isinstance(item, dict):
                continue
            operation = DraftFileOperation.model_validate(item)
            file_path = operation.file_path.strip().replace("\\", "/").lstrip("./")
            if not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
                raise ValueError(f"Agent returned an unsafe file path: {operation.file_path}")
            if any(file_path == prefix.rstrip("/") or file_path.startswith(prefix) for prefix in READ_ONLY_WRITE_PREFIXES):
                raise ValueError(f"Agent attempted to edit a read-only/generated surface: {file_path}")
            if operation.operation in {"create", "replace"} and operation.content is None:
                raise ValueError(f"Agent returned {operation.operation} for {file_path} without content.")
            if operation.operation == "patch" and not str(operation.diff or operation.content or "").strip():
                raise ValueError(f"Agent returned patch for {file_path} without a unified diff.")
            operations.append(
                DraftFileOperation(
                    operation_id=operation.operation_id,
                    file_path=file_path,
                    operation=operation.operation,
                    content=None if operation.operation == "patch" else operation.content,
                    diff=operation.diff or (operation.content if operation.operation == "patch" else None),
                    reason=operation.reason,
                )
            )
        return operations

    def _prompt_alignment_smoke(self, *, workspace_id: str, run_id: str, prompt: str) -> RunCheckResult:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        if not diff_text.strip():
            return RunCheckResult(
                name="prompt_alignment_smoke",
                status="skipped",
                details="Prompt alignment smoke skipped because the draft has no product changes yet.",
                command="prompt alignment static smoke",
                logs=[],
            )
        prompt_lower = prompt.lower()
        commerce_requested = any(marker in prompt_lower for marker in ("store", "shop", "ecommerce", "e-commerce", "catalog", "cart", "product", "магазин", "каталог", "товар", "корзин"))
        changed_paths = self._paths_from_diff(diff_text)
        combined = []
        for path in changed_paths:
            if not path.startswith("miniapp/app/"):
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content:
                combined.append(f"\n/* {path} */\n{content[:12000]}")
        haystack = "\n".join(combined).lower()
        issues: list[str] = []
        if commerce_requested:
            commerce_markers = ("product", "catalog", "cart", "order", "товар", "каталог", "корзин", "заказ", "магазин")
            if not any(marker in haystack for marker in commerce_markers):
                issues.append("Commerce prompt did not result in visible commerce/product/cart/order language in changed app files.")
        return RunCheckResult(
            name="prompt_alignment_smoke",
            status="failed" if issues else "passed",
            details="Prompt alignment smoke checks for obvious domain drift from the user prompt.",
            command="prompt alignment static smoke",
            logs=issues or ["Prompt alignment smoke passed."],
        )

    def _completion_state(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, object]:
        del preview_details
        failed = [result for result in results if result.status == "failed"]
        has_diff = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
        no_product_diff = request.mode == "generate" and not has_diff
        remaining_issues = [
            {
                "kind": "check_failure",
                "check": result.name,
                "details": result.details,
                "logs": result.logs[-8:],
                "blocking": True,
            }
            for result in failed
        ]
        if no_product_diff:
            remaining_issues.append(
                {
                    "kind": "prompt_alignment",
                    "check": "meaningful_diff",
                    "details": "Generation must create a prompt-specific draft diff before completion.",
                    "blocking": True,
                }
            )
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in validation_snapshot.issues
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        complete = not failed and not no_product_diff
        return {
            "strict_green": complete,
            "optimistic_complete": complete,
            "preview_ok": True,
            "validators_ok": not any(result.status == "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"}),
            "build_ok": not any(result.status == "failed" for result in results if result.name == "changed_files_static"),
            "canonical_smoke_ok": not any(result.status == "failed" for result in results if result.name == "platform_invariants"),
            "remaining_issues": remaining_issues,
        }

    def _validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        prompt_failed = any(item.status == "failed" for item in execution.results if item.name == "prompt_alignment_smoke")
        return ValidationSnapshot(
            platform_valid=not bool(issues),
            prompt_alignment_valid=not prompt_failed,
            checks_valid=not bool(issues),
            build_valid=not build_failed,
            blocking=bool(issues),
            issues=issues,
        )

    @staticmethod
    def _fast_gate_passed(results: list[RunCheckResult]) -> bool:
        return not any(result.status == "failed" for result in results)

    def _finalize_job(self, *, job: JobRecord, loop_result: WorkspaceLoopResult, elapsed_ms: int) -> JobRecord:
        job.status = loop_result.status
        job.outcome_kind = loop_result.outcome_kind
        job.summary = loop_result.summary
        job.failure_reason = loop_result.failure_reason
        job.failure_class = loop_result.failure_class
        job.failure_signature = loop_result.failure_signature
        job.root_cause_summary = loop_result.root_cause_summary
        job.current_fix_phase = loop_result.current_phase
        job.remaining_issues = [] if loop_result.status == "completed" else list(loop_result.remaining_issues)
        job.repair_iterations = [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in loop_result.repair_iterations]
        job.apply_result = loop_result.latest_apply_result
        if loop_result.latest_execution is not None:
            job.executed_checks = [item.model_dump(mode="json") for item in loop_result.latest_execution.results]
            job.validation_snapshot = self._validation_snapshot_from_execution(loop_result.latest_execution)
        if loop_result.status == "completed":
            job.outcome_kind = "applied"
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
            job.validation_snapshot = ValidationSnapshot(
                platform_valid=True,
                prompt_alignment_valid=True,
                checks_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        job.fix_attempts = [
            FixAttemptRecord(
                run_id=job.linked_run_id or "",
                attempt=int(turn.get("attempt") or 0),
                diagnosis=str(turn.get("diagnosis") or turn.get("assistant_message") or ""),
                files_changed=[str(path) for path in turn.get("files_changed") or []],
                implicated_files=[str(path) for path in turn.get("fix_targets") or []],
                failure_signature=str(turn.get("failure_signature") or "") or None,
                result="patched" if str(turn.get("result")) == "patched" else "failed",
            ).model_dump(mode="json")
            for turn in loop_result.turn_history
        ]
        job.latency_breakdown["agent_total_ms"] = elapsed_ms
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)
        self._store_report(f"fix_attempts:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.fix_attempts})
        self._store_report(f"remaining_issues:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.remaining_issues})
        if job.validation_snapshot is not None:
            self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        if loop_result.latest_preview_details:
            self._store_report(f"fix_runtime:{job.workspace_id}", {"workspace_id": job.workspace_id, **loop_result.latest_preview_details})
        event_type = "job_completed" if job.status == "completed" else "job_failed"
        self._append_event(job, event_type, job.summary or ("Workspace code agent completed." if job.status == "completed" else "Workspace code agent failed."))
        return job

    def _seed_file_context(self, workspace_id: str, run_id: str) -> dict[str, str]:
        contexts: dict[str, str] = {}
        for path in SEED_CONTEXT_PATHS:
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is not None:
                contexts[path] = content
        return contexts

    @staticmethod
    def _compact_file_contexts(file_contexts: dict[str, str], *, max_files: int, max_chars: int = 6000) -> dict[str, str]:
        compact: dict[str, str] = {}
        for path, content in list(file_contexts.items())[:max_files]:
            text = str(content or "")
            if len(text) > max_chars:
                text = f"{text[: max_chars // 2]}\n...[truncated {len(text) - max_chars} chars]...\n{text[-max_chars // 2 :]}"
            compact[path] = text
        return compact

    @staticmethod
    def _compact_checks(execution: CheckExecutionRecord) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for result in execution.results:
            payload.append(
                {
                    "name": result.name,
                    "status": result.status,
                    "details": result.details,
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "logs": result.logs[-10:],
                    "diagnostics": result.diagnostics,
                }
            )
        return payload

    def _collect_preview_diagnostics(self, workspace_id: str, preview_details: dict[str, Any]) -> None:
        preview = self.preview_service.get(workspace_id)
        if preview.proxy_port is None:
            return
        source_dir = self.workspace_service.source_dir(workspace_id)
        preview_details["containers"] = self.runtime_manager.inspect_containers(workspace_id, source_dir, preview.proxy_port)
        preview_details["container_logs"] = self.runtime_manager.collect_container_logs(workspace_id, source_dir, preview.proxy_port)

    def _clear_agent_reports(self, workspace_id: str) -> None:
        for key in (
            "validation",
            "check_results",
            "iterations",
            "candidate_diff",
            "patch",
            "fix_case",
            "fix_attempts",
            "scope_expansions",
            "fix_runtime",
            "remaining_issues",
        ):
            self.store.delete("reports", f"{key}:{workspace_id}")

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = datetime.now(timezone.utc)
        self._sync_run_progress(job, event_type, message)
        self._save_job(job)
        self.workspace_log_service.append(job.workspace_id, source=f"agent.{event_type}", message=message, payload=details or {})

    def _save_job(self, job: JobRecord) -> None:
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def _store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.store.upsert("reports", key, payload)

    def _clear_trace(self, workspace_id: str) -> None:
        self._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append({"stage": stage, "message": message, "payload": payload or {}, "created_at": datetime.now(timezone.utc).isoformat()})
        current["entries"] = entries
        self._store_report(report_key, current)
        self.workspace_log_service.append(workspace_id, source=f"agent.trace.{stage}", message=message, payload=payload or {})

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str) -> None:
        del message
        if not job.linked_run_id:
            return
        payload = self.store.get("runs", job.linked_run_id)
        if not payload:
            return
        stage, progress = self._run_progress_for_event(event_type)
        payload["linked_job_id"] = job.job_id
        payload["current_stage"] = stage
        payload["progress_percent"] = max(int(payload.get("progress_percent", 0)), progress)
        payload["summary"] = job.summary
        payload["failure_reason"] = job.failure_reason
        payload["failure_class"] = job.failure_class
        payload["failure_signature"] = job.failure_signature
        payload["root_cause_summary"] = job.root_cause_summary
        payload["current_fix_phase"] = job.current_fix_phase
        payload["fix_targets"] = list(job.fix_targets)
        payload["remaining_issues"] = list(job.remaining_issues)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", job.linked_run_id, payload)

    @staticmethod
    def _run_progress_for_event(event_type: str) -> tuple[str, int]:
        progress_map = {
            "job_started": ("running", 6),
            "running_checks": ("checks", 14),
            "build_started": ("checks", 16),
            "frontend_build_started": ("checks", 18),
            "backend_compile_started": ("checks", 22),
            "checks_completed": ("checks", 35),
            "agent_turn_started": ("agent_turn", 48),
            "scope_expanded": ("agent_turn", 54),
            "patch_apply_started": ("applying", 70),
            "patch_apply_completed": ("checks", 82),
            "repair_iteration": ("agent_turn", 88),
            "job_completed": ("complete", 99),
            "job_failed": ("failed", 100),
        }
        return progress_map.get(event_type, ("processing", 20))

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff_text or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
            if not match:
                continue
            for candidate in match.groups():
                normalized = candidate.strip().strip('"')
                if normalized.startswith("source/") or normalized.startswith("draft/"):
                    normalized = normalized.split("/", 1)[1]
                if normalized and normalized not in paths:
                    paths.append(normalized)
        return paths

    @staticmethod
    def _generation_mode(value: GenerationMode | str | None) -> GenerationMode:
        if isinstance(value, GenerationMode):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return GenerationMode(value.strip())
            except Exception:
                pass
        return GenerationMode.BALANCED

    @staticmethod
    def _max_attempts(generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            return 2
        if generation_mode == GenerationMode.QUALITY:
            return 8
        return 6

    @staticmethod
    def _tool_round_limit(generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            return 1
        if generation_mode == GenerationMode.QUALITY:
            return 4
        return 3

    @staticmethod
    def _prompt_cache_key(workspace_id: str, run_id: str, prompt: str) -> str:
        prompt_key = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.strip().lower())[:80].strip("_") or "prompt"
        return f"workspace_code_agent:{workspace_id}:{run_id}:{prompt_key}"

    @staticmethod
    def _merge_cache_stats(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing or {})
        for key, value in (incoming or {}).items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
        return merged
