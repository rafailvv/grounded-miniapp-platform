from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

from app.ai.model_registry import models_for_role
from app.ai.openai_client import OpenAIClient
from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord, DraftAction, RunCheckResult, utc_now
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.agent_tool_changes import (
    file_changes_from_mutating_tool_calls,
    is_mutating_agent_tool_call,
)
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.agent_tool_runtime import normalize_tool_calls
from app.modules.miniapp_agent_loop.agent_tool_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
)
from app.services.workspace.service import WorkspaceService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerBranchResult:
    worker_id: str
    owner_scope: str
    branch_run_id: str
    source_dir: str
    status: str
    file_changes: list[DraftAction] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    assistant_message: str = ""
    transcript: dict[str, Any] = field(default_factory=dict)
    tool_results: list[dict[str, object]] = field(default_factory=list)
    activity_events: list[dict[str, Any]] = field(default_factory=list)
    apply_results: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    error: str | None = None
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "owner_scope": self.owner_scope,
            "branch_run_id": self.branch_run_id,
            "source_dir": self.source_dir,
            "status": self.status,
            "changed_files": list(self.changed_files),
            "file_change_count": len(self.file_changes),
            "assistant_message": self.assistant_message[:1200],
            "transcript": self.transcript,
            "tool_results": list(self.tool_results)[-12:],
            "activity_events": list(self.activity_events)[-40:],
            "apply_results": list(self.apply_results),
            "token_usage": dict(self.token_usage),
            "model": self.model,
            "error": self.error,
            "created_at": self.created_at,
        }


class AgentWorkerBranchLoop:
    """Independent worker loop for a coordinator-owned branch draft."""

    def __init__(
        self,
        *,
        openai_client: OpenAIClient,
        workspace_service: WorkspaceService,
        read_artifact: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.openai_client = openai_client
        self.workspace_service = workspace_service
        self.read_artifact = read_artifact
        self.edit_validator = AgentEditValidator()

    def run(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        branch_run_id: str,
        branch_source: Path,
        generation_mode: GenerationMode,
        model_profile: str,
        user_prompt: str,
        worker_task: dict[str, Any],
        worker_prefix: dict[str, Any],
        max_steps: int = 4,
    ) -> WorkerBranchResult:
        worker_id = str(worker_task.get("worker_id") or "").strip() or "worker"
        owner_scope = str(worker_task.get("owner_scope") or worker_id).strip()
        transcript_key = f"{parent_run_id}:{worker_id}"
        transcript = AgentTranscriptStore()
        file_cache = AgentFileStateCache()
        process_manager = AgentProcessManager()
        tool_results: list[dict[str, object]] = []
        activity_events: list[dict[str, Any]] = []
        apply_results: list[dict[str, Any]] = []
        all_changes: list[DraftAction] = []
        assistant_message = ""
        token_usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
        model = ""
        worker_feedback: list[dict[str, Any]] = []
        mutation_required = False
        repeated_read_only_after_required_patch = 0

        def append_activity(kind: str, label: str, details: dict[str, Any] | None = None) -> None:
            activity_events.append(
                {
                    "kind": kind,
                    "label": label,
                    "details": details or {},
                    "worker_id": worker_id,
                    "created_at": _now(),
                }
            )

        def append_batch_summary(summary: dict[str, object]) -> None:
            tool_results.append({"tool": "tool_batch_summary", "worker_id": worker_id, **summary})

        def execute_checks(_: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            return (
                CheckExecutionRecord(
                    workspace_id=workspace_id,
                    run_id=branch_run_id,
                    changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                    results=[
                        RunCheckResult(
                            name="worker_self_check",
                            status="passed",
                            details="Worker branch checks are deferred to the coordinator merge and final proof gate.",
                            command="worker branch self-check",
                            logs=[],
                        )
                    ],
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    duration_ms=0,
                ),
                {"worker_id": worker_id, "branch_run_id": branch_run_id},
            )

        try:
            for step in range(max(1, int(max_steps or 1))):
                transcript_context = transcript.next_model_context(transcript_key)
                pending_tool_results = list(transcript_context.get("tool_result_messages") or [])
                available_tools = (
                    AgentToolRegistry.openai_tools({"apply_patch_to_draft", "write_file"})
                    if mutation_required
                    else AgentToolRegistry.openai_tools()
                )
                response = self.openai_client.generate_agent_tool_step(
                    tools=available_tools,
                    system_prompt=self._system_prompt(),
                    user_prompt=self._user_prompt(
                        user_prompt=user_prompt,
                        generation_mode=generation_mode,
                        worker_task=worker_task,
                        worker_prefix=worker_prefix,
                        branch_run_id=branch_run_id,
                        step=step,
                        worker_feedback=worker_feedback,
                    ),
                    prompt_cache_key=f"worker_branch:{workspace_id}:{parent_run_id}:{worker_id}",
                    stable_prefix="miniapp_worker_branch_tool_loop_v1",
                    model_override=models_for_role(
                        "agent_turn",
                        model_profile=model_profile,
                        generation_mode=generation_mode,
                    ),
                    responses_tuning_override=self._tuning(generation_mode),
                    previous_response_id=str(transcript_context.get("previous_response_id") or "") or None,
                    tool_result_messages=pending_tool_results,
                )
                payload = response.get("payload") if isinstance(response, dict) else {}
                parsed = payload if isinstance(payload, dict) else {}
                model = str(response.get("model") or model)
                stats = response.get("cache_stats") if isinstance(response, dict) else {}
                self._add_usage(token_usage, stats if isinstance(stats, dict) else {})
                raw_tool_calls = parsed.get("tool_calls") if isinstance(parsed, dict) else []
                tool_calls = normalize_tool_calls(raw_tool_calls if isinstance(raw_tool_calls, list) else [])
                assistant_message = str(parsed.get("assistant_message") or assistant_message or "")
                transcript.append_model_turn(
                    transcript_key,
                    attempt=step + 1,
                    tool_round=step,
                    response_id=str(parsed.get("response_id") or ""),
                    assistant_message=assistant_message,
                    tool_calls=tool_calls,
                    model=model,
                    usage=dict(stats or {}),
                    consumed_tool_result_count=len(pending_tool_results),
                )
                transcript.append_tool_calls(transcript_key, tool_calls)
                mutating_changes, mutating_trace = file_changes_from_mutating_tool_calls(
                    tool_calls,
                    default_worker_id=worker_id,
                    default_owner_scope=owner_scope,
                    read_text_file=lambda relative_path: self._read_branch_text(branch_source, relative_path),
                )
                failed_mutating_results = [
                    item
                    for item in mutating_trace
                    if isinstance(item, dict) and str(item.get("status") or "") == "failed" and item.get("repair_packet")
                ]
                if failed_mutating_results:
                    tool_results.extend(failed_mutating_results)
                    worker_feedback.extend(failed_mutating_results)
                    transcript.append_tool_results(transcript_key, failed_mutating_results)
                    for repair_result in failed_mutating_results:
                        transcript.append_repair(transcript_key, repair_result)
                    continue
                read_calls = [item for item in tool_calls if not is_mutating_agent_tool_call(item)]
                if mutation_required and not mutating_changes:
                    repeated_read_only_after_required_patch += 1
                    repair_results = [
                        {
                            "tool": "worker_mutation_required",
                            "tool_use_id": str(item.get("tool_use_id") or f"{worker_id}_mutation_required_{step + 1}_{index}"),
                            "status": "needs_mutating_tool",
                            "ignored_tool": str(item.get("tool") or ""),
                            "next_action": "call write_file or apply_patch_to_draft now; mutating tools write exactly one file_path per call",
                        }
                        for index, item in enumerate(read_calls)
                        if isinstance(item, dict)
                    ] or [
                        {
                            "tool": "worker_mutation_required",
                            "tool_use_id": f"{worker_id}_mutation_required_{step + 1}",
                            "status": "needs_mutating_tool",
                            "next_action": "call write_file or apply_patch_to_draft now; mutating tools write exactly one file_path per call",
                        }
                    ]
                    tool_results.extend(repair_results)
                    worker_feedback.extend(repair_results)
                    transcript.append_tool_results(transcript_key, repair_results)
                    for repair_result in repair_results:
                        transcript.append_repair(transcript_key, repair_result)
                    if repeated_read_only_after_required_patch >= 2:
                        transcript.clear_model_context(transcript_key)
                    continue
                if mutating_changes:
                    mutation_required = False
                    repeated_read_only_after_required_patch = 0
                if read_calls:
                    new_context, executed_results = self._execute_branch_tools(
                        workspace_id=workspace_id,
                        run_id=branch_run_id,
                        draft_source=branch_source,
                        tool_calls=read_calls,
                        file_cache=file_cache,
                        process_manager=process_manager,
                        execute_checks=execute_checks,
                        append_activity=append_activity,
                        append_batch_summary=append_batch_summary,
                    )
                    del new_context
                    tool_results.extend(executed_results)
                    transcript.append_tool_results(transcript_key, executed_results)
                if mutating_changes:
                    try:
                        validated_changes = self._validate_worker_changes(mutating_changes, worker_id=worker_id)
                    except ValueError as exc:
                        repair_results = [
                            {
                                "tool": "worker_edit_repair_packet",
                                "tool_use_id": str(item.get("tool_use_id") or f"{worker_id}_edit_repair_{step + 1}_{index}"),
                                "status": "failed",
                                "error": str(exc),
                                "file_path": str(item.get("file_path") or ""),
                                "next_action": "repair the same owned slice with a smaller valid patch",
                            }
                            for index, item in enumerate(mutating_trace)
                        ] or [
                            {
                                "tool": "worker_edit_repair_packet",
                                "tool_use_id": f"{worker_id}_edit_repair_{step + 1}",
                                "status": "failed",
                                "error": str(exc),
                                "next_action": "repair the same owned slice with a smaller valid patch",
                            }
                        ]
                        tool_results.extend(repair_results)
                        worker_feedback.extend(repair_results)
                        transcript.append_tool_results(transcript_key, repair_results)
                        for repair_result in repair_results:
                            transcript.append_repair(transcript_key, repair_result)
                        mutation_required = True
                        if step + 1 < max(1, int(max_steps or 1)):
                            continue
                        raise
                    transcript.append_tool_results(transcript_key, mutating_trace)
                    tool_results.extend(mutating_trace)
                    envelope = self.workspace_service.build_patch_envelope_for_file_changes(
                        workspace_id,
                        branch_run_id,
                        validated_changes,
                    )
                    started_at = time.perf_counter()
                    apply_result = self.workspace_service.apply_patch_envelope_to_draft(
                        workspace_id,
                        branch_run_id,
                        envelope,
                    )
                    apply_payload = apply_result.model_dump(mode="json")
                    apply_payload["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
                    apply_payload["worker_id"] = worker_id
                    apply_results.append(apply_payload)
                    transcript.append_file_changes(transcript_key, turn=step + 1, file_changes=validated_changes)
                    if apply_result.status != "applied":
                        repair_result = {
                            "tool": "worker_apply_repair_packet",
                            "tool_use_id": f"{worker_id}_apply_repair_{step + 1}",
                            "status": "failed",
                            "error": apply_result.conflict_reason or "Worker branch patch could not be applied.",
                            "next_action": "repair the same owned slice against the current branch draft",
                        }
                        tool_results.append(repair_result)
                        worker_feedback.append(repair_result)
                        transcript.append_repair(transcript_key, repair_result)
                        mutation_required = True
                        if step + 1 < max(1, int(max_steps or 1)):
                            continue
                        return WorkerBranchResult(
                            worker_id=worker_id,
                            owner_scope=owner_scope,
                            branch_run_id=branch_run_id,
                            source_dir=str(branch_source),
                            status="failed",
                            file_changes=all_changes,
                            changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                            assistant_message=assistant_message,
                            transcript=transcript.snapshot(transcript_key),
                            tool_results=tool_results,
                            activity_events=activity_events,
                            apply_results=apply_results,
                            token_usage=token_usage,
                            model=model,
                            error=str(repair_result["error"]),
                        )
                    all_changes.extend(validated_changes)
                    missing_completion = self._owned_completion_missing(worker_id, branch_source)
                    if missing_completion and step + 1 < max(1, int(max_steps or 1)):
                        repair_result = {
                            "tool": "worker_completion_missing",
                            "tool_use_id": f"{worker_id}_completion_missing_{step + 1}",
                            "status": "needs_more_edits",
                            "missing": missing_completion,
                            "next_action": "continue this same worker branch and patch the missing owned files before returning a mergeable diff",
                        }
                        tool_results.append(repair_result)
                        worker_feedback.append(repair_result)
                        transcript.append_repair(transcript_key, repair_result)
                        mutation_required = True
                        continue
                    if missing_completion:
                        return WorkerBranchResult(
                            worker_id=worker_id,
                            owner_scope=owner_scope,
                            branch_run_id=branch_run_id,
                            source_dir=str(branch_source),
                            status="failed",
                            file_changes=all_changes,
                            changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                            assistant_message=assistant_message,
                            transcript=transcript.snapshot(transcript_key),
                            tool_results=tool_results,
                            activity_events=activity_events,
                            apply_results=apply_results,
                            token_usage=token_usage,
                            model=model,
                            error="Worker branch stopped before completing owned slice: " + "; ".join(missing_completion),
                        )
                    return WorkerBranchResult(
                        worker_id=worker_id,
                        owner_scope=owner_scope,
                        branch_run_id=branch_run_id,
                        source_dir=str(branch_source),
                        status="changes_ready",
                        file_changes=all_changes,
                        changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                        assistant_message=assistant_message,
                        transcript=transcript.snapshot(transcript_key),
                        tool_results=tool_results,
                        activity_events=activity_events,
                        apply_results=apply_results,
                        token_usage=token_usage,
                        model=model,
                    )
                if not read_calls:
                    missing_completion = self._owned_completion_missing(worker_id, branch_source)
                    if all_changes and not missing_completion:
                        return WorkerBranchResult(
                            worker_id=worker_id,
                            owner_scope=owner_scope,
                            branch_run_id=branch_run_id,
                            source_dir=str(branch_source),
                            status="changes_ready",
                            file_changes=all_changes,
                            changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                            assistant_message=assistant_message,
                            transcript=transcript.snapshot(transcript_key),
                            tool_results=tool_results,
                            activity_events=activity_events,
                            apply_results=apply_results,
                            token_usage=token_usage,
                            model=model,
                        )
                    if step + 1 < max(1, int(max_steps or 1)):
                        repair_result = {
                            "tool": "worker_completion_missing" if all_changes else "worker_no_change_retry",
                            "tool_use_id": f"{worker_id}_continue_{step + 1}",
                            "status": "needs_more_edits",
                            "missing": missing_completion,
                            "next_action": (
                                "continue this same worker branch and patch the missing owned files"
                                if all_changes
                                else "produce actual file changes for this owned slice now"
                            ),
                        }
                        tool_results.append(repair_result)
                        worker_feedback.append(repair_result)
                        transcript.append_repair(transcript_key, repair_result)
                        mutation_required = True
                        continue
                    break
            final_missing = self._owned_completion_missing(worker_id, branch_source)
            if all_changes and not final_missing:
                return WorkerBranchResult(
                    worker_id=worker_id,
                    owner_scope=owner_scope,
                    branch_run_id=branch_run_id,
                    source_dir=str(branch_source),
                    status="changes_ready",
                    file_changes=all_changes,
                    changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                    assistant_message=assistant_message,
                    transcript=transcript.snapshot(transcript_key),
                    tool_results=tool_results,
                    activity_events=activity_events,
                    apply_results=apply_results,
                    token_usage=token_usage,
                    model=model,
                )
            if all_changes and final_missing:
                return WorkerBranchResult(
                    worker_id=worker_id,
                    owner_scope=owner_scope,
                    branch_run_id=branch_run_id,
                    source_dir=str(branch_source),
                    status="failed",
                    file_changes=all_changes,
                    changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                    assistant_message=assistant_message,
                    transcript=transcript.snapshot(transcript_key),
                    tool_results=tool_results,
                    activity_events=activity_events,
                    apply_results=apply_results,
                    token_usage=token_usage,
                    model=model,
                    error="Worker branch stopped before completing owned slice: " + "; ".join(final_missing),
                )
            return WorkerBranchResult(
                worker_id=worker_id,
                owner_scope=owner_scope,
                branch_run_id=branch_run_id,
                source_dir=str(branch_source),
                status="no_changes",
                file_changes=[],
                changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                assistant_message=assistant_message,
                transcript=transcript.snapshot(transcript_key),
                tool_results=tool_results,
                activity_events=activity_events,
                apply_results=apply_results,
                token_usage=token_usage,
                model=model,
            )
        except Exception as exc:
            return WorkerBranchResult(
                worker_id=worker_id,
                owner_scope=owner_scope,
                branch_run_id=branch_run_id,
                source_dir=str(branch_source),
                status="failed",
                file_changes=all_changes,
                changed_files=self._changed_files_from_diff(workspace_id, branch_run_id),
                assistant_message=assistant_message,
                transcript=transcript.snapshot(transcript_key),
                tool_results=tool_results,
                activity_events=activity_events,
                apply_results=apply_results,
                token_usage=token_usage,
                model=model,
                error=str(exc),
            )

    def _owned_completion_missing(self, worker_id: str, branch_source: Path) -> list[str]:
        worker = str(worker_id or "").strip()
        missing: list[str] = []
        if worker in {"client_ui", "specialist_ui", "manager_ui"}:
            role = worker.removesuffix("_ui")
            role_dir = branch_source / "miniapp/app/static" / role
            html = self._read_text(role_dir / "index.html")
            js = self._read_text(role_dir / "app.js")
            css = self._read_text(role_dir / "styles.css")
            if self._looks_like_neutral_shell(html):
                missing.append(f"{role}/index.html contract-derived role UI")
            if "fetch(" not in js or "addEventListener" not in js:
                missing.append(f"{role}/app.js API fetch handlers")
            if self._looks_like_placeholder_css(css):
                missing.append(f"{role}/styles.css non-placeholder mobile styling")
        elif worker == "backend_api":
            main = self._read_text(branch_source / "miniapp/app/main.py")
            routes_text = "\n".join(
                self._read_text(path)
                for path in sorted((branch_source / "miniapp/app/routes").glob("*.py"))
            )
            lowered_routes = routes_text.lower()
            if "/api" not in routes_text or "@router.get" not in lowered_routes:
                missing.append("backend GET /api route")
            if not any(method in lowered_routes for method in ("@router.post", "@router.put", "@router.patch", "@router.delete")):
                missing.append("backend prompt-owned mutating API route")
            if any(marker in routes_text for marker in ("APIRouter(prefix=\"/api", "APIRouter(prefix='/api")) and "include_router" not in main:
                missing.append("app.main includes API router")
        elif worker == "generated_tests":
            py_test = self._read_text(branch_source / "miniapp/tests/test_generated_app.py")
            js_test = self._read_text(branch_source / "miniapp/tests/generated_app.test.mjs")
            if "unittest.TestCase" not in py_test or "def test_" not in py_test:
                missing.append("unittest-discoverable Python generated tests")
            if "TestClient(app)" in py_test and "with TestClient(app)" not in py_test:
                missing.append("Python generated tests use FastAPI TestClient as a context manager so lifespan/table setup runs")
            if "node:test" not in js_test or ("fetch" not in js_test and "/api/" not in js_test):
                missing.append("node:test generated JS workflow tests")
            if re.search(r"read\(\s*([\"'])miniapp/", js_test) or re.search(r"path\.join\(\s*root\s*,\s*([\"'])miniapp/", js_test):
                missing.append("node:test file reads use cwd-safe app/... paths when tests run from miniapp/")
        return missing[:8]

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            return ""

    @staticmethod
    def _read_branch_text(branch_source: Path, relative_path: str) -> str | None:
        normalized = str(relative_path or "").strip().replace("\\", "/")
        if not normalized.startswith("miniapp/") or normalized.startswith(("/", "~")) or ".." in normalized.split("/"):
            return None
        try:
            target = branch_source / normalized
            return target.read_text(encoding="utf-8") if target.exists() else None
        except OSError:
            return None

    @staticmethod
    def _looks_like_neutral_shell(text: str) -> bool:
        lowered = str(text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "neutral shell",
                "preview entry",
                "client surface",
                "specialist surface",
                "manager surface",
                "should be replaced",
            )
        )

    @staticmethod
    def _looks_like_placeholder_css(text: str) -> bool:
        stripped = str(text or "").strip().lower()
        return not stripped or "styles can replace this file" in stripped or "generated " in stripped and "styles can replace" in stripped

    def _validate_worker_changes(self, file_changes: list[DraftAction], *, worker_id: str) -> list[DraftAction]:
        invalid = self.edit_validator._first_invalid_file_change(file_changes)
        if invalid:
            _, message = invalid
            raise ValueError(message)
        disallowed = [
            item.file_path
            for item in file_changes
            if not self._worker_can_write(worker_id, item.file_path)
        ]
        if disallowed:
            raise ValueError(
                f"Worker {worker_id} tried to write outside its owned slice: {', '.join(disallowed[:6])}."
            )
        role_isolation_errors = [
            message
            for item in file_changes
            for message in self._role_surface_isolation_errors(worker_id, item)
        ]
        if role_isolation_errors:
            raise ValueError(" ".join(role_isolation_errors[:3]))
        return file_changes

    @staticmethod
    def _role_surface_isolation_errors(worker_id: str, item: DraftAction) -> list[str]:
        if worker_id not in {"client_ui", "specialist_ui", "manager_ui"}:
            return []
        normalized = str(item.file_path or "").strip().replace("\\", "/")
        if not normalized.endswith((".html", ".js")):
            return []
        role = worker_id.removesuffix("_ui")
        if f"miniapp/app/static/{role}/" not in normalized:
            return []
        text = f"{item.content or ''}\n{item.diff or ''}".lower()
        if not text:
            return []
        errors: list[str] = []
        if any(marker in text for marker in ("data-role-tab", "data-role-panel", "role-switcher")):
            errors.append(
                f"Worker {worker_id} embedded role switching in {normalized}; create separate role surfaces instead."
            )
        other_roles = {"client", "specialist", "manager"} - {role}
        for other_role in sorted(other_roles):
            if f'data-role="{other_role}"' in text or f"data-role='{other_role}'" in text:
                errors.append(
                    f"Worker {worker_id} embedded {other_role} surface markup in {normalized}; keep each role app independent."
                )
            technical_markers = (
                f"{other_role}app",
                f"{other_role}dashboard",
                f"{other_role}form",
                f"{other_role}grid",
                f"{other_role}list",
                f"{other_role}panel",
                f"{other_role}page",
                f"{other_role}view",
            )
            if any(marker in text.replace("-", "").replace("_", "") for marker in technical_markers):
                errors.append(
                    f"Worker {worker_id} embedded {other_role} controls in {normalized}; each role app must own only its own UI."
                )
        return errors

    @staticmethod
    def _worker_can_write(worker_id: str, path: str) -> bool:
        owner = AgentWorkerManager.owner_for_path(path)
        if owner == worker_id:
            return True
        normalized = str(path or "").strip().replace("\\", "/")
        if owner == "shared_runtime" and normalized.startswith("miniapp/app/generated"):
            return worker_id in {"backend_api", "generated_tests"}
        if owner == "shared_runtime" and normalized.startswith("miniapp/app/static/shared"):
            return worker_id in {"client_ui", "specialist_ui", "manager_ui"}
        if owner == "shared_runtime" and worker_id in {"backend_api", "generated_tests"}:
            return True
        return False

    def _changed_files_from_diff(self, workspace_id: str, run_id: str) -> list[str]:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        paths: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith("diff --git "):
                continue
            parts = line.strip().split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                if path and path not in paths:
                    paths.append(path)
        return paths

    def _execute_branch_tools(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_calls: list[dict[str, Any]],
        file_cache: AgentFileStateCache,
        process_manager: AgentProcessManager,
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
        append_activity: Callable[[str, str, dict[str, Any] | None], None],
        append_batch_summary: Callable[[dict[str, object]], None],
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        del append_batch_summary
        workspace_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        loaded_context: dict[str, str] = {}
        results: list[dict[str, object]] = []

        def targets(request_item: dict[str, Any]) -> list[str]:
            return [
                str(item or "").strip().replace("\\", "/").lstrip("./")
                for item in request_item.get("targets") or []
                if str(item or "").strip()
            ]

        for index, request_item in enumerate(tool_calls, start=1):
            tool = str(request_item.get("tool") or "").strip().lower()
            use_id = str(request_item.get("tool_use_id") or f"{tool}_{index}")
            request_targets = targets(request_item)
            reason = str(request_item.get("reason") or "").strip()
            append_activity("tool_progress", "Worker branch tool started", {"tool_use_id": use_id, "tool": tool, "status": "started"})
            if tool == "list_files":
                results.append({**list_workspace_files(workspace_tree=workspace_tree, targets=request_targets), "tool_use_id": use_id, "reason": reason})
            elif tool == "read_files":
                file_contents: dict[str, str] = {}
                for target in request_targets[:16]:
                    content = file_cache.read(
                        run_id=run_id,
                        root=draft_source,
                        path=target,
                        read_text=lambda relative_path: self.workspace_service.try_read_text_file(
                            workspace_id,
                            relative_path,
                            run_id=run_id,
                        ),
                    )
                    if content is not None:
                        file_contents[target] = content
                        loaded_context[target] = content
                results.append(
                    {
                        "tool": "read_files",
                        "tool_use_id": use_id,
                        "targets": request_targets,
                        "files": summarize_read_file_payloads(file_contents=file_contents),
                        "reason": reason,
                    }
                )
            elif tool == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                results.append(
                    {
                        **search_workspace_files(
                            workspace_tree=workspace_tree,
                            read_text_file=lambda relative_path: file_cache.read(
                                run_id=run_id,
                                root=draft_source,
                                path=relative_path,
                                read_text=lambda cached_path: self.workspace_service.try_read_text_file(
                                    workspace_id,
                                    cached_path,
                                    run_id=run_id,
                                ),
                            ),
                            pattern=pattern,
                            targets=request_targets,
                        ),
                        "tool_use_id": use_id,
                        "reason": reason,
                    }
                )
            elif tool == "semantic_scan":
                results.append({**semantic_scan(root=draft_source, targets=request_targets), "tool_use_id": use_id, "reason": reason})
            elif tool == "inspect_diff":
                results.append({"tool": "inspect_diff", "tool_use_id": use_id, "diff": self.workspace_service.diff(workspace_id, run_id=run_id)[:12000], "reason": reason})
            elif tool == "read_artifact_ref":
                artifact_ref = str(request_item.get("artifact_ref") or (request_targets[0] if request_targets else "")).strip()
                payload = self.read_artifact(artifact_ref) if self.read_artifact is not None and artifact_ref else None
                results.append({"tool": "read_artifact_ref", "tool_use_id": use_id, "artifact_ref": artifact_ref, "found": payload is not None, "payload": payload, "reason": reason})
            elif tool == "run_command":
                command = str(request_item.get("command") or "").strip()
                results.append(
                    {
                        **run_workspace_command(
                            draft_source=draft_source,
                            command=command,
                            timeout_seconds=AgentToolRegistry.spec("run_command").timeout_seconds if AgentToolRegistry.spec("run_command") else 25,
                            max_output_chars=AgentToolRegistry.spec("run_command").output_cap_chars if AgentToolRegistry.spec("run_command") else 6000,
                            process_manager=process_manager,
                            process_id=use_id,
                        ),
                        "tool_use_id": use_id,
                        "reason": reason,
                    }
                )
            elif tool in {"run_checks", "browser_verify"}:
                execution, preview = execute_checks(request_targets)
                results.append(
                    {
                        "tool": tool,
                        "tool_use_id": use_id,
                        "results": [
                            {"name": item.name, "status": item.status, "details": item.details}
                            for item in execution.results
                        ],
                        "preview": preview,
                        "reason": reason,
                    }
                )
            else:
                results.append({"tool": tool or "unknown", "tool_use_id": use_id, "status": "ignored", "reason": reason})
            append_activity("tool_progress", "Worker branch tool completed", {"tool_use_id": use_id, "tool": tool, "status": "completed"})
        return loaded_context, results

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are an isolated code-agent worker in a coordinator run. "
            "Use tools to read, search, run safe diagnostics, and patch only your owned slice. "
            "Return progress through tool calls; do not assume hidden templates or preloaded data."
        )

    @staticmethod
    def _user_prompt(
        *,
        user_prompt: str,
        generation_mode: GenerationMode,
        worker_task: dict[str, Any],
        worker_prefix: dict[str, Any],
        branch_run_id: str,
        step: int,
        worker_feedback: list[dict[str, Any]] | None = None,
    ) -> str:
        payload = {
            "branch_run_id": branch_run_id,
            "step": step,
            "generation_mode": str(getattr(generation_mode, "value", generation_mode)),
            "user_prompt": user_prompt,
            "worker_task": worker_task,
            "shared_prefix": worker_prefix,
            "instructions": [
                "Inspect only the files you need for your owner scope.",
                "Patch a complete owned slice that supports the prompt-derived role workflow.",
                "Use shared_prefix.implementation_plan.prompt_hints as the source for nouns, fields, labels, and role actions. If prompt_hints has concrete terms, do not build a generic placeholder record UI.",
                "This branch must produce actual file changes; do not finish with read-only tool calls only.",
                "Keep each role surface independent and mobile-first. Do not include another role's primary workflow controls in this role just to make verification easier.",
                "Prompt analysis decides the role split through shared_prefix.implementation_plan.role_state_contract; do not force a fixed client/specialist/manager workflow split. If the user's prompt assigns shared-state creation to manager or specialist, that role must own that creation flow and client must consume the persisted state without duplicate source controls.",
                "Split role workflows into routeable mobile pages when that makes the product clearer; avoid one long dashboard-only page, but do not add pages just to satisfy a fixed count. Child pages must live under static/<role>/<page>/index.html and be reachable through route_manifest.json or filesystem role routing.",
                "Use shared_prefix.implementation_plan.routeable_screen_plan for screen intent guidance; concrete route names are still owned by this worker and must come from the prompt/product vocabulary.",
                "If you work on UI or generated tests, read the current backend route/schema files first and use their actual endpoint paths and field names exactly.",
                "If shared_prefix.backend_contract is present, treat it as the active API contract and do not invent a different /api route or field casing.",
                "In async JavaScript form handlers, store DOM references before awaited calls, e.g. `const form = event.currentTarget`; do not read `event.currentTarget` after await.",
                "For multi-page role apps, the shared static/<role>/app.js must initialize per page: branch by body[data-view] or route, guard optional DOM nodes from other pages, and bind every visible form/button/control on every child page to a real handler.",
                "Use read/search tools first if you need context; otherwise patch directly.",
                "Use apply_patch_to_draft for existing files and write_file for new or full owned files.",
                "Mutating tools write exactly one file_path per tool call. If your owned slice needs index.html, app.js, and styles.css, call write_file/apply_patch_to_draft separately for each file.",
                    "Do not write preloaded product records.",
            ],
            "current_step_instruction": (
                "You have inspected enough context; now call write_file or apply_patch_to_draft for every required owned file."
                if step >= 1
                else "You may inspect briefly, but the next step must patch the owned slice."
            ),
            "worker_feedback": list(worker_feedback or [])[-4:],
            "owned_slice_completion": {
                "client_ui": ["index.html", "child page index.html files", "app.js", "styles.css with the prompt-derived client/source flow"],
                "specialist_ui": ["index.html", "child page index.html files", "app.js", "styles.css with a POST/PATCH/PUT update flow when the prompt requires persisted changes"],
                "manager_ui": ["index.html", "child page index.html files", "app.js", "styles.css with shared-state visibility and any manager-owned creation/control flow from the prompt"],
                "generated_tests": [
                    "test_generated_app.py as import unittest + unittest.TestCase test_* methods",
                    "FastAPI tests must use `with TestClient(app) as client:` or explicitly create tables after importing generated ORM models before requests",
                    "generated_app.test.mjs as node:test",
                    "JS tests run with cwd=miniapp; read app/static/... and app/generated/... paths, not miniapp/app/... paths",
                    "JS tests run in Node without browser DOM; do not import browser-only role app.js files unless the test creates explicit window/document mocks first",
                    "JS tests must derive role pages from app/generated/route_manifest.json and search the actual role HTML files; never assert that the manifest is empty",
                    "tests must import/run cleanly; do not write pytest-only top-level functions for Python",
                    "Python helpers must be valid code, for example '\\n'.join([a, b, c]) not str.join(a, b, c)",
                ],
                "backend_api": [
                    "registered routes",
                    "schemas",
                    "table creation before requests",
                    "GET/POST/update endpoints",
                    "every APIRouter that declares /api endpoints must be imported and included by app.main",
                    "prefer one exported router when possible so page routes and API routes are both mounted",
                    "do not edit miniapp/app/routes/role_pages.py or miniapp/app/routes/role_routes.py; those files are platform shell routing for static role apps",
                ],
            }.get(str(worker_task.get("worker_id") or ""), []),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _tuning(generation_mode: GenerationMode) -> dict[str, Any]:
        if generation_mode == GenerationMode.QUALITY:
            return {"reasoning": {"effort": "medium"}, "max_output_tokens": 15000}
        if generation_mode == GenerationMode.BALANCED:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 12000}
        return {"reasoning": {"effort": "low"}, "max_output_tokens": 10000}

    @staticmethod
    def _add_usage(target: dict[str, int], stats: dict[str, Any]) -> None:
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
            try:
                target[key] = int(target.get(key) or 0) + int(stats.get(key) or 0)
            except (TypeError, ValueError):
                target[key] = int(target.get(key) or 0)
