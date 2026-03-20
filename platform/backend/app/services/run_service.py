from __future__ import annotations

from pathlib import PurePosixPath
import re
import threading
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.ai.openrouter_client import OpenRouterClient
from app.models.common import GenerationMode
from app.models.domain import (
    CodeChangePlan,
    CodeChangeTarget,
    CreateRunRequest,
    GenerateRequest,
    JobEvent,
    RunChecksSummary,
    RunRecord,
    WorkspaceRecord,
)
from app.repositories.state_store import StateStore
from app.services.fix_orchestrator import FixOrchestrator
from app.services.generation_service import GenerationService
from app.services.preview_service import PreviewService
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService

ROLE_SCOPE = {"client", "specialist", "manager"}
MEANINGFUL_DIFF_IGNORED_PARTS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".vite",
    ".cache",
    "artifacts",
}
MEANINGFUL_DIFF_IGNORED_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
MEANINGFUL_DIFF_IGNORED_NAMES = {".DS_Store", "vite.config.js", "vite.config.d.ts"}
logger = logging.getLogger(__name__)


class RunService:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        generation_service: GenerationService,
        fix_orchestrator: FixOrchestrator,
        preview_service: PreviewService,
        openrouter_client: OpenRouterClient,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.generation_service = generation_service
        self.fix_orchestrator = fix_orchestrator
        self.preview_service = preview_service
        self.openrouter_client = openrouter_client
        self.workspace_log_service = workspace_log_service

    def stop_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.status != "running":
            return run
        self.store.upsert(
            "reports",
            f"run_stop_request:{run_id}",
            {
                "run_id": run_id,
                "requested": True,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        run.current_stage = "stopping"
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        return run

    def create_run(self, workspace_id: str, request: CreateRunRequest) -> RunRecord:
        return self._start_run(workspace_id, request, wait=False)

    def create_run_sync(self, workspace_id: str, request: CreateRunRequest) -> RunRecord:
        return self._start_run(workspace_id, request, wait=True)

    def _start_run(self, workspace_id: str, request: CreateRunRequest, *, wait: bool) -> RunRecord:
        workspace = self.workspace_service.get_workspace(workspace_id)
        resolved_intent = self._resolve_intent(workspace, request)
        effective_generation_mode = self._resolve_generation_mode(workspace, request, resolved_intent)
        run = RunRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode=request.mode,
            intent=resolved_intent,
            apply_strategy=request.apply_strategy,
            approval_required=request.apply_strategy == "manual_approve",
            target_role_scope=[role for role in request.target_role_scope if role in ROLE_SCOPE],
            model_profile=request.model_profile,
            generation_mode=effective_generation_mode,
            llm_provider="openai" if self.openrouter_client.enabled else None,
            resume_from_run_id=request.resume_from_run_id,
            source_revision_id=workspace.current_revision_id,
            error_context=request.error_context,
            status="pending",
            apply_status="pending",
            current_stage="queued",
            progress_percent=2,
        )
        self._save_run(run)
        self.store.delete("reports", f"run_stop_request:{run.run_id}")
        if wait:
            self._execute_run(run.run_id, request.model_dump(mode="python"))
            return self.get_run(run.run_id)
        worker = threading.Thread(
            target=self._execute_run,
            args=(run.run_id, request.model_dump(mode="python")),
            daemon=True,
        )
        worker.start()
        return self.get_run(run.run_id)

    def list_runs(self, workspace_id: str) -> list[RunRecord]:
        runs = [
            RunRecord.model_validate(item)
            for item in self.store.list("runs")
            if item["workspace_id"] == workspace_id
        ]
        runs.sort(key=lambda item: item.created_at, reverse=True)
        return runs

    def get_run(self, run_id: str) -> RunRecord:
        payload = self.store.get("runs", run_id)
        if not payload:
            raise KeyError(f"Run not found: {run_id}")
        return RunRecord.model_validate(payload)

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            run = self.get_run(run_id)
            if run.linked_job_id:
                job = self.generation_service.get_job(run.linked_job_id)
                preview = self.preview_service.get(run.workspace_id)
                change_plan = self._build_change_plan(
                    workspace_id=run.workspace_id,
                    run=run,
                    diff_text=self.workspace_service.diff(run.workspace_id, run_id=run.run_id if run.draft_ready else None),
                    prompt=run.prompt,
                )
                self._store_run_artifacts(run, change_plan, job, preview)
                payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            raise KeyError(f"Artifacts not found for run: {run_id}")
        run = self.get_run(run_id)
        preview = self.preview_service.get(run.workspace_id)
        preview_payload = self._preview_snapshot(run.workspace_id, preview)
        payload["run"] = run.model_dump(mode="json")
        payload["preview"] = preview_payload
        payload["draft_preview"] = {
            key: value
            for key, value in preview_payload.items()
            if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
        }
        self.store.upsert("reports", f"run_artifacts:{run_id}", payload)
        return payload

    def get_run_iterations(self, run_id: str) -> list[dict[str, Any]]:
        artifacts = self.get_run_artifacts(run_id)
        return list(artifacts.get("iterations", []) or [])

    def apply_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.apply_strategy != "manual_approve":
            return run
        if run.status != "awaiting_approval":
            return run
        apply_started_at = time.perf_counter()
        run.current_stage = "finalizing apply"
        run.progress_percent = 99
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(run.linked_job_id, "apply_started", "Applying the reviewed draft to the source workspace.")
        revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Approve AI draft for run {run.run_id}")
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.status = "completed"
        run.apply_status = "applied"
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.latency_breakdown["apply_ms"] = int((time.perf_counter() - apply_started_at) * 1000)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(run.linked_job_id, "apply_completed", "Draft was applied successfully.")
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Run draft applied manually.",
            payload={"run_id": run.run_id, "revision_id": revision.revision_id},
        )
        self._queue_preview_refresh(run, reason="manual approval")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.store.delete("reports", f"run_stop_request:{run_id}")
        return run

    def discard_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.status = "failed"
        run.apply_status = "failed"
        run.draft_status = "discarded"
        run.draft_ready = False
        run.current_stage = "discarded"
        run.progress_percent = 100
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Run draft discarded.",
            payload={"run_id": run.run_id},
        )
        self._queue_preview_refresh(run, reason="draft discard")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.store.delete("reports", f"run_stop_request:{run_id}")
        return run

    def rollback_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.rolled_back:
            return run
        if run.status != "completed" or run.apply_status != "applied" or not run.result_revision_id:
            raise ValueError("Only applied completed runs can be rolled back.")

        revision = self.workspace_service.revert_revision(
            run.workspace_id,
            run.result_revision_id,
            f"Rollback AI run {run.run_id}",
        )
        run.rolled_back = True
        run.rolled_back_at = datetime.now(timezone.utc)
        run.apply_status = "rolled_back"
        run.current_stage = "rolled back"
        run.progress_percent = 100
        run.candidate_revision_id = revision.revision_id
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Applied run rolled back.",
            payload={"run_id": run.run_id, "revision_id": revision.revision_id},
        )
        self._queue_preview_refresh(run, reason="rollback")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        return run

    def _save_run(self, run: RunRecord) -> None:
        self.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    def _execute_run(self, run_id: str, request_payload: dict[str, Any]) -> None:
        request = CreateRunRequest.model_validate(request_payload)
        run = self.get_run(run_id)
        workspace = self.workspace_service.get_workspace(run.workspace_id)
        effective_generation_mode = self._resolve_generation_mode(workspace, request, run.intent)
        run.status = "running"
        run.generation_mode = effective_generation_mode
        run.current_stage = "starting"
        run.progress_percent = max(run.progress_percent, 5)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Run started.",
            payload={"run_id": run.run_id, "mode": run.mode, "intent": run.intent},
        )
        logger.info("run_started run_id=%s workspace_id=%s intent=%s", run.run_id, run.workspace_id, run.intent)
        try:
            generate_request = GenerateRequest(
                prompt=request.prompt,
                mode=request.mode,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                generation_mode=effective_generation_mode,
                intent=run.intent,
                target_role_scope=run.target_role_scope,
                model_profile=request.model_profile,
                linked_run_id=run.run_id,
                resume_from_run_id=request.resume_from_run_id,
                error_context=request.error_context,
            )
            with self.openrouter_client.workspace_logging(run.workspace_id):
                if request.mode == "fix" and self._should_resume_failed_generation_from_checkpoint(run, request):
                    self.workspace_log_service.append(
                        run.workspace_id,
                        source="run.resume",
                        message="Fix request matched a saved generation checkpoint. Continuing generation from the prepared draft.",
                        payload={
                            "run_id": run.run_id,
                            "source_run_id": request.resume_from_run_id,
                        },
                    )
                    resumed_generate_request = generate_request.model_copy(update={"mode": "generate"})
                    job = self.generation_service.generate(
                        run.workspace_id,
                        resumed_generate_request,
                        should_stop=lambda: self._is_stop_requested(run.run_id),
                    )
                else:
                    job = (
                        self.fix_orchestrator.generate(
                            run.workspace_id,
                            generate_request,
                            should_stop=lambda: self._is_stop_requested(run.run_id),
                        )
                        if request.mode == "fix"
                        else self.generation_service.generate(
                            run.workspace_id,
                            generate_request,
                            should_stop=lambda: self._is_stop_requested(run.run_id),
                        )
                    )
            if self._should_auto_fix_failed_generate(request, job):
                run.current_stage = "auto-fixing build failure"
                run.progress_percent = max(run.progress_percent, 82)
                run.updated_at = datetime.now(timezone.utc)
                self._save_run(run)
                self._append_job_event(
                    job.job_id,
                    "repair_started",
                    "Frontend build failed during generate. Switching to fix mode automatically.",
                    {"run_id": run.run_id},
                )
                with self.openrouter_client.workspace_logging(run.workspace_id):
                    job = self.fix_orchestrator.generate(
                        run.workspace_id,
                        self._build_auto_fix_request(run=run, request=request, failed_job=job),
                        should_stop=lambda: self._is_stop_requested(run.run_id),
                    )

            preview = self.preview_service.get(run.workspace_id)
            change_plan = self._build_change_plan(
                workspace_id=run.workspace_id,
                run=run,
                diff_text=self.workspace_service.diff(run.workspace_id, run_id=run.run_id),
                prompt=request.prompt,
            )

            run.linked_job_id = job.job_id
            run.llm_provider = job.llm_provider
            run.llm_model = job.llm_model
            run.summary = job.summary
            run.failure_reason = job.failure_reason
            run.failure_class = job.failure_class
            run.failure_signature = job.failure_signature
            run.root_cause_summary = job.root_cause_summary
            run.current_fix_phase = job.current_fix_phase
            run.current_failing_command = job.current_failing_command
            run.current_exit_code = job.current_exit_code
            run.fix_targets = list(job.fix_targets)
            run.handoff_from_failed_generate = dict(job.handoff_from_failed_generate or {}) or None
            run.checks_summary = self._build_checks_summary(job.validation_snapshot, preview.status)
            run.touched_files = self._resolve_touched_files(
                workspace_id=run.workspace_id,
                run=run,
                change_plan=change_plan,
                request=request,
            )
            run.candidate_revision_id = f"draft:{run.run_id}"
            run.iteration_count = len((self.generation_service.current_report(run.workspace_id, "iterations") or {}).get("items", []))
            run.latency_breakdown = dict(job.latency_breakdown)
            run.repair_iterations = list(job.repair_iterations)
            run.fix_attempts = list(job.fix_attempts)
            run.scope_expansions = list(job.scope_expansions)
            run.apply_result = dict(job.apply_result or {})
            run.retrieval_stats = dict(job.retrieval_stats)
            run.cache_stats = dict(job.cache_stats)
            run.artifacts = {
                "grounded_spec": f"/workspaces/{run.workspace_id}/spec/current",
                "run_artifacts": f"/runs/{run.run_id}/artifacts",
                "preview_url": preview.url or "",
                "traceability": f"/workspaces/{run.workspace_id}/traceability/current",
                "iterations": f"/runs/{run.run_id}/iterations",
                "checks": f"/runs/{run.run_id}/checks",
                "patch": f"/runs/{run.run_id}/patch",
            }
            run.updated_at = datetime.now(timezone.utc)

            if job.status == "completed":
                should_apply_fix_draft = request.mode == "fix" and self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                if should_apply_fix_draft:
                    self._apply_completed_draft(run, message="Applying verified fix draft to the source workspace.")
                else:
                    meaningful_paths = self._meaningful_paths_for_run(
                        workspace_id=run.workspace_id,
                        run=run,
                        change_plan=change_plan,
                    )
                    if not meaningful_paths:
                        self._mark_run_without_meaningful_diff(run, job)
                    elif run.apply_strategy == "manual_approve":
                        run.status = "awaiting_approval"
                        run.apply_status = "awaiting_approval"
                        run.draft_status = "ready"
                        run.draft_ready = True
                        run.current_stage = "awaiting review"
                        run.progress_percent = 99
                    else:
                        self._apply_completed_draft(run, message="Applying generated draft to the source workspace.")
            elif job.status == "blocked":
                run.status = "blocked"
                run.apply_status = "blocked"
                run.draft_status = "failed"
                if run.current_fix_phase == "completed":
                    run.current_fix_phase = "failed"
                run.current_stage = "stopped" if self._is_stop_requested(run.run_id) else "blocked"
                run.progress_percent = max(run.progress_percent, 100)
            else:
                run.status = "failed"
                run.apply_status = "failed"
                run.draft_status = "failed"
                if run.current_fix_phase == "completed":
                    run.current_fix_phase = "failed"
                run.current_stage = "failed"
                run.progress_percent = max(run.progress_percent, 100)

            self._save_run(run)
            if job.status == "completed" and run.apply_status == "applied":
                self._queue_preview_refresh(run, reason="run completion")
                self._queue_resume_generation_from_checkpoint_if_needed(run, request)
                preview = self.preview_service.get(run.workspace_id)
            self._store_run_artifacts(run, change_plan, job, preview)
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            logger.info(
                "run_finished run_id=%s workspace_id=%s status=%s progress=%s",
                run.run_id,
                run.workspace_id,
                run.status,
                run.progress_percent,
            )
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                message="Run finished.",
                payload={"run_id": run.run_id, "status": run.status, "apply_status": run.apply_status},
            )
        except Exception as exc:
            run.status = "failed"
            run.apply_status = "failed"
            run.failure_reason = str(exc)
            if run.current_fix_phase == "completed":
                run.current_fix_phase = "failed"
            run.current_stage = "failed"
            run.progress_percent = max(run.progress_percent, 100)
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                level="ERROR",
                message="Run failed with an unexpected exception.",
                payload={"run_id": run.run_id, "error": str(exc)},
            )
            logger.exception("run_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)

    def _should_auto_fix_failed_generate(self, request: CreateRunRequest, job: Any) -> bool:
        if request.mode == "fix":
            return False
        if getattr(job, "status", None) != "failed":
            return False
        if not getattr(job, "handoff_from_failed_generate", None):
            return False
        validation_snapshot = getattr(job, "validation_snapshot", None)
        build_failed = bool(validation_snapshot and not getattr(validation_snapshot, "build_valid", True))
        haystack_parts = [
            getattr(job, "failure_reason", None),
            getattr(job, "root_cause_summary", None),
            getattr(job, "failure_class", None),
        ]
        if validation_snapshot is not None:
            haystack_parts.extend(
                str(issue.get("message") or "")
                for issue in getattr(validation_snapshot, "issues", [])
                if isinstance(issue, dict)
            )
        haystack = " ".join(part for part in haystack_parts if part).lower()
        frontend_build_markers = (
            "npm run build",
            "draft frontend",
            "vite build",
            "typescript",
            "tsc",
        )
        return build_failed and any(marker in haystack for marker in frontend_build_markers)

    def _build_auto_fix_request(
        self,
        *,
        run: RunRecord,
        request: CreateRunRequest,
        failed_job: Any,
    ) -> GenerateRequest:
        handoff = dict(getattr(failed_job, "handoff_from_failed_generate", None) or {})
        handoff_context = handoff.get("error_context") or {}
        raw_error = (
            handoff_context.get("raw_error")
            or getattr(failed_job, "failure_reason", None)
            or getattr(failed_job, "root_cause_summary", None)
            or "Frontend build failed during generation."
        )
        error_source = handoff_context.get("source") or "frontend"
        failing_target = handoff_context.get("failing_target") or "frontend build"
        return GenerateRequest(
            prompt=handoff.get("prompt") or "Analyze the reported failure and apply the smallest safe fix.",
            mode="fix",
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            generation_mode=GenerationMode.BALANCED,
            intent="edit",
            target_role_scope=run.target_role_scope,
            model_profile=request.model_profile,
            linked_run_id=run.run_id,
            error_context={
                "raw_error": raw_error,
                "source": error_source,
                "failing_target": failing_target,
            },
        )

    def _is_stop_requested(self, run_id: str) -> bool:
        payload = self.store.get("reports", f"run_stop_request:{run_id}")
        return bool(payload and payload.get("requested"))

    def _append_job_event(
        self,
        job_id: str | None,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not job_id:
            return
        payload = self.store.get("jobs", job_id)
        if not payload:
            return
        events = list(payload.get("events", []))
        events.append(JobEvent(event_type=event_type, message=message, details=details or {}).model_dump(mode="json"))
        payload["events"] = events
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("jobs", job_id, payload)

    def _queue_preview_refresh(self, run: RunRecord, *, reason: str) -> None:
        queue_started_at = time.perf_counter()
        self._append_job_event(
            run.linked_job_id,
            "preview_rebuild_started",
            f"Queued preview rebuild after {reason}.",
            {"reason": reason, "run_id": run.run_id},
        )

        def on_complete(preview: Any) -> None:
            if preview.status == "running":
                self._append_job_event(
                    run.linked_job_id,
                    "preview_rebuild_completed",
                    "Preview rebuild finished successfully.",
                    {
                        "url": preview.url,
                        "stage": getattr(preview, "stage", "running"),
                        "progress_percent": getattr(preview, "progress_percent", 100),
                    },
                )
            else:
                self._append_job_event(
                    run.linked_job_id,
                    "preview_rebuild_failed",
                    getattr(preview, "last_error", None) or "Preview rebuild failed.",
                    {
                        "stage": getattr(preview, "stage", "error"),
                        "progress_percent": getattr(preview, "progress_percent", 100),
                    },
                )
            artifacts_payload = self.store.get("reports", f"run_artifacts:{run.run_id}")
            if artifacts_payload:
                artifacts_payload["preview"] = self._preview_snapshot(run.workspace_id, preview)
                artifacts_payload["draft_preview"] = {
                    key: value
                    for key, value in artifacts_payload["preview"].items()
                    if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
                }
                self.store.upsert("reports", f"run_artifacts:{run.run_id}", artifacts_payload)

        preview = self.preview_service.rebuild_async(run.workspace_id, on_complete=on_complete)
        run.latency_breakdown["preview_enqueue_ms"] = int((time.perf_counter() - queue_started_at) * 1000)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        artifacts_payload = self.store.get("reports", f"run_artifacts:{run.run_id}")
        if artifacts_payload:
            artifacts_payload["preview"] = self._preview_snapshot(run.workspace_id, preview)
            artifacts_payload["draft_preview"] = {
                key: value
                for key, value in artifacts_payload["preview"].items()
                if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
            }
            self.store.upsert("reports", f"run_artifacts:{run.run_id}", artifacts_payload)

    def _queue_resume_generation_from_checkpoint_if_needed(self, run: RunRecord, request: CreateRunRequest) -> None:
        if request.mode == "fix":
            return
        checkpoint = self.store.get("reports", f"resume_checkpoint:{run.workspace_id}")
        if not checkpoint or checkpoint.get("status") != "pending":
            return
        if checkpoint.get("mode") == "fix":
            return
        if request.apply_strategy != "staged_auto_apply" or run.apply_status != "applied":
            return
        source_run_id = str(checkpoint.get("source_run_id") or "")
        if request.mode != "fix" and source_run_id != run.run_id:
            return

        resume_request = CreateRunRequest(
            prompt=str(checkpoint.get("prompt") or run.prompt),
            mode="generate",
            intent=str(checkpoint.get("intent") or "auto"),
            apply_strategy="staged_auto_apply",
            target_role_scope=list(checkpoint.get("target_role_scope") or run.target_role_scope),
            model_profile=str(checkpoint.get("model_profile") or run.model_profile),
            target_platform=str(checkpoint.get("target_platform") or "telegram_mini_app"),
            preview_profile=str(checkpoint.get("preview_profile") or "telegram_mock"),
            generation_mode=str(checkpoint.get("generation_mode") or run.generation_mode.value),
            resume_from_run_id=source_run_id or None,
        )
        resumed_run = self.create_run(run.workspace_id, resume_request)
        checkpoint["status"] = "resumed"
        checkpoint["resumed_run_id"] = resumed_run.run_id
        checkpoint["resumed_from_fix_run_id"] = run.run_id
        checkpoint["resumed_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("reports", f"resume_checkpoint:{run.workspace_id}", checkpoint)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run.resume",
            message="Fix applied successfully. Queued generation resume from checkpoint.",
            payload={
                "fix_run_id": run.run_id,
                "resumed_run_id": resumed_run.run_id,
                "source_run_id": checkpoint.get("source_run_id"),
            },
        )
        self._append_job_event(
            run.linked_job_id,
            "job_completed",
            f"Fix applied. Continuing generation in run {resumed_run.run_id}.",
            {"resumed_run_id": resumed_run.run_id, "source_run_id": checkpoint.get("source_run_id")},
        )

    def _should_resume_failed_generation_from_checkpoint(self, run: RunRecord, request: CreateRunRequest) -> bool:
        if request.mode != "fix":
            return False
        source_run_id = str(request.resume_from_run_id or "").strip()
        if not source_run_id:
            return False
        try:
            source_run = self.get_run(source_run_id)
        except KeyError:
            return False
        if source_run.workspace_id != run.workspace_id:
            return False
        if source_run.status not in {"blocked", "failed"}:
            return False
        failure_class = str(source_run.failure_class or "")
        if not failure_class.startswith("generation."):
            return False
        checkpoint = self.store.get("reports", f"resume_checkpoint:{run.workspace_id}")
        if not checkpoint or checkpoint.get("status") != "pending":
            return False
        return str(checkpoint.get("source_run_id") or "") == source_run_id

    def _preview_snapshot(self, workspace_id: str, preview: Any | None = None) -> dict[str, Any]:
        current = preview or self.preview_service.get(workspace_id)
        role_urls = {role: f"{current.url}/{role}" for role in ("client", "specialist", "manager")} if current.url else {}
        return {
            "status": current.status,
            "stage": getattr(current, "stage", "idle"),
            "progress_percent": getattr(current, "progress_percent", 0),
            "runtime_mode": current.runtime_mode,
            "url": current.url,
            "role_urls": role_urls,
            "logs": list(getattr(current, "logs", [])),
            "draft_run_id": current.draft_run_id,
            "latency_breakdown": dict(getattr(current, "latency_breakdown", {})),
            "last_error": getattr(current, "last_error", None),
        }

    def _store_run_artifacts(self, run: RunRecord, change_plan: CodeChangePlan, job: Any, preview: Any) -> None:
        workspace_id = run.workspace_id
        iterations = (self.generation_service.current_report(workspace_id, "iterations") or {}).get("items", [])
        candidate_diff = (self.generation_service.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        if candidate_diff:
            effective_diff = candidate_diff
        elif run.draft_ready and self.workspace_service.draft_exists(workspace_id, run.run_id):
            effective_diff = self.workspace_service.diff(workspace_id, run_id=run.run_id)
        else:
            effective_diff = self.workspace_service.diff(workspace_id)
        preview_payload = self._preview_snapshot(workspace_id, preview)
        payload = {
            "run": run.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "grounded_spec": self.generation_service.current_report(workspace_id, "spec"),
            "validation": self.generation_service.current_report(workspace_id, "validation"),
            "assumptions": self.generation_service.current_report(workspace_id, "assumptions"),
            "role_contract": self.generation_service.current_report(workspace_id, "role_contract"),
            "traceability": self.generation_service.current_report(workspace_id, "traceability"),
            "trace": self.generation_service.current_report(workspace_id, "trace"),
            "code_change_plan": change_plan.model_dump(mode="json"),
            "page_graph": self.generation_service.current_report(workspace_id, "page_graph"),
            "iterations": iterations,
            "candidate_diff": candidate_diff,
            "check_results": (self.generation_service.current_report(workspace_id, "check_results") or {}).get("items", []),
            "checks": self.generation_service.current_report(workspace_id, "check_results"),
            "patch": self.generation_service.current_report(workspace_id, "patch"),
            "diff": effective_diff,
            "preview": preview_payload,
            "draft_preview": {
                key: value
                for key, value in preview_payload.items()
                if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
            },
            "final_summary": job.summary,
            "latency_breakdown": job.latency_breakdown,
            "retrieval_stats": job.retrieval_stats,
            "cache_stats": job.cache_stats,
            "apply_result": job.apply_result,
            "repair_iterations": job.repair_iterations,
            "fix_case": self.generation_service.current_report(workspace_id, "fix_case"),
            "fix_attempts": self.generation_service.current_report(workspace_id, "fix_attempts"),
            "scope_expansions": self.generation_service.current_report(workspace_id, "scope_expansions"),
            "fix_runtime": self.generation_service.current_report(workspace_id, "fix_runtime"),
            "failure_analysis": {
                "mode": run.mode,
                "failure_class": job.failure_class,
                "failure_signature": job.failure_signature,
                "root_cause_summary": job.root_cause_summary,
                "fix_targets": job.fix_targets,
                "handoff_from_failed_generate": job.handoff_from_failed_generate,
                "error_context": job.error_context.model_dump(mode="json") if job.error_context else None,
                "current_fix_phase": job.current_fix_phase,
                "current_failing_command": job.current_failing_command,
                "current_exit_code": job.current_exit_code,
                "executed_checks": job.executed_checks,
                "container_statuses": job.container_statuses,
            },
        }
        self.store.upsert("reports", f"run_artifacts:{run.run_id}", payload)

    def _resolve_intent(self, workspace: WorkspaceRecord, request: CreateRunRequest) -> str:
        if request.intent != "auto":
            return request.intent
        if request.mode == "fix":
            return "edit"
        prompt = request.prompt.lower()
        if self._looks_like_fix_request(prompt):
            return "edit"
        if request.target_role_scope and len(request.target_role_scope) == 1:
            return "role_only_change"
        if any(token in prompt for token in ("refine", "polish", "improve", "tighten", "cleanup")):
            return "refine"
        has_existing_build = workspace.template_cloned and workspace.current_revision_id is not None and len(workspace.revisions) > 1
        if has_existing_build or any(token in prompt for token in ("change", "update", "edit", "modify", "rewrite", "fix", "исправ", "ошиб")):
            return "edit"
        return "create"

    def _resolve_generation_mode(
        self,
        workspace: WorkspaceRecord,
        request: CreateRunRequest,
        resolved_intent: str,
    ) -> GenerationMode:
        if request.mode == "fix":
            return request.generation_mode if request.generation_mode == GenerationMode.QUALITY else GenerationMode.BALANCED
        if request.generation_mode != GenerationMode.QUALITY:
            return request.generation_mode
        prompt = request.prompt.lower()
        has_existing_build = workspace.template_cloned and workspace.current_revision_id is not None and len(workspace.revisions) > 1
        if self._looks_like_fix_request(prompt):
            return GenerationMode.BALANCED
        if resolved_intent in {"edit", "refine", "role_only_change"} and has_existing_build:
            return GenerationMode.BALANCED
        return request.generation_mode

    def _build_change_plan(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        diff_text: str,
        prompt: str,
    ) -> CodeChangePlan:
        iteration_payload = self.generation_service.current_report(workspace_id, "iterations") or {}
        targets: list[CodeChangeTarget] = []
        seen_paths: set[str] = set()
        file_paths = self._paths_from_diff(diff_text)
        if not file_paths:
            file_paths = self._paths_from_iterations(iteration_payload.get("items", []))
        for file_path in file_paths:
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)
            targets.append(
                CodeChangeTarget(
                    file_path=file_path,
                    operation="replace",
                    reason="Touched by the draft workspace diff.",
                    risk="medium" if file_path.startswith("artifacts/") else "low",
                )
            )

        summary = f"Prepare draft code changes for prompt: {prompt[:120]}"
        risks = [
            "Role-specific behavior must remain valid for client, specialist, and manager previews.",
            "Draft changes should not overwrite manual workspace edits outside the reviewed draft.",
        ]
        if diff_text.strip():
            risks.append("Existing workspace edits must be preserved and not overwritten unexpectedly.")
        acceptance_checks = [
            "GroundedSpec planning diagnostics pass or provide explicit blocking issues.",
            "Build validation succeeds on the draft workspace.",
            "Preview runtime starts and exposes role-specific URLs.",
        ]
        return CodeChangePlan(
            workspace_id=workspace_id,
            run_id=run.run_id,
            intent=run.intent,
            summary=summary,
            target_role_scope=run.target_role_scope,
            targets=targets,
            risks=risks,
            acceptance_checks=acceptance_checks,
        )

    @staticmethod
    def _looks_like_fix_request(prompt: str) -> bool:
        fix_markers = (
            "fix",
            "bug",
            "error",
            "failed",
            "failure",
            "exception",
            "traceback",
            "stacktrace",
            "stack trace",
            "does not work",
            "broken",
            "preview failed",
            "build failed",
            "docker",
            "npm run build",
            "exit code",
            "исправ",
            "ошиб",
            "не работает",
            "слом",
            "падает",
            "сбой",
        )
        return any(marker in prompt for marker in fix_markers)

    def _resolve_touched_files(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        change_plan: CodeChangePlan,
        request: CreateRunRequest,
    ) -> list[str]:
        paths = [
            target.file_path
            for target in change_plan.targets
            if target.file_path and self._is_meaningful_source_path(target.file_path)
        ]
        if request.mode == "fix":
            inherited = self._inherited_touched_files_for_fix(workspace_id=workspace_id, run=run)
            if inherited:
                paths = list(dict.fromkeys([*paths, *inherited]))
        if paths:
            return list(dict.fromkeys(paths))
        fallback_paths = [target.file_path for target in change_plan.targets if target.file_path]
        if request.mode == "fix":
            inherited = self._inherited_touched_files_for_fix(workspace_id=workspace_id, run=run)
            if inherited:
                fallback_paths = list(dict.fromkeys([*fallback_paths, *inherited]))
        return fallback_paths

    def _inherited_touched_files_for_fix(self, *, workspace_id: str, run: RunRecord) -> list[str]:
        if run.mode != "fix":
            return []
        source_run_id = str(run.resume_from_run_id or "").strip()
        if not source_run_id:
            return []
        try:
            source_run = self.get_run(source_run_id)
        except KeyError:
            return []

        inherited = [path for path in source_run.touched_files if self._is_meaningful_source_path(path)]
        if inherited:
            return list(dict.fromkeys(inherited))

        if self.workspace_service.draft_exists(workspace_id, source_run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=source_run_id)
            diff_paths = [
                path
                for path in self._paths_from_diff(diff_text)
                if self._is_meaningful_source_path(path)
            ]
            if diff_paths:
                return list(dict.fromkeys(diff_paths))
        return []

    def _mark_run_without_meaningful_diff(self, run: RunRecord, job: Any) -> None:
        message = "Generation completed without meaningful source changes to apply."
        run.summary = message
        run.failure_reason = None
        run.status = "completed"
        run.apply_status = "noop"
        run.draft_status = "none"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = max(run.progress_percent, 100)
        run.current_fix_phase = job.current_fix_phase

        job.status = "completed"
        job.summary = message
        job.failure_reason = None
        self.generation_service._append_event(job, "job_completed", message, {"reason": "no_meaningful_diff"})

    def _apply_completed_draft(self, run: RunRecord, *, message: str) -> None:
        apply_started_at = time.perf_counter()
        run.current_stage = "finalizing apply"
        run.progress_percent = 99
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(run.linked_job_id, "apply_started", message)
        revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Auto-apply AI draft for run {run.run_id}")
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.status = "completed"
        run.apply_status = "applied"
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.latency_breakdown["apply_ms"] = int((time.perf_counter() - apply_started_at) * 1000)
        self._append_job_event(run.linked_job_id, "apply_completed", "Generated draft was applied successfully.")

    def _meaningful_paths_for_run(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        change_plan: CodeChangePlan,
    ) -> list[str]:
        candidate_diff = (self.generation_service.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        diff_text = candidate_diff
        if not diff_text and self.workspace_service.draft_exists(workspace_id, run.run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=run.run_id)

        paths = self._paths_from_diff(diff_text)
        if not paths:
            paths = [target.file_path for target in change_plan.targets if target.file_path]
        return [path for path in list(dict.fromkeys(paths)) if self._is_meaningful_source_path(path)]

    @staticmethod
    def _is_meaningful_source_path(file_path: str) -> bool:
        normalized = file_path.strip().lstrip("./")
        if not normalized:
            return False
        path = PurePosixPath(normalized)
        if any(part in MEANINGFUL_DIFF_IGNORED_PARTS for part in path.parts):
            return False
        if path.name in MEANINGFUL_DIFF_IGNORED_NAMES:
            return False
        if path.name.endswith(MEANINGFUL_DIFF_IGNORED_SUFFIXES):
            return False
        if normalized.startswith("miniapp/app/generated/"):
            return False
        return True

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE):
            candidate = match.group(1).strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            if candidate:
                paths.append(candidate)
        return paths

    @staticmethod
    def _paths_from_iterations(iterations: Any) -> list[str]:
        if not isinstance(iterations, list):
            return []
        paths: list[str] = []
        for iteration in iterations:
            if not isinstance(iteration, dict):
                continue
            operations = iteration.get("operations")
            if not isinstance(operations, list):
                continue
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                file_path = operation.get("file_path")
                if isinstance(file_path, str) and file_path.strip():
                    paths.append(file_path.strip())
        return list(dict.fromkeys(paths))

    @staticmethod
    def _build_checks_summary(validation_snapshot: Any, preview_status: str) -> RunChecksSummary:
        issues = []
        validators = "pending"
        build = "pending"
        preview = "pending"
        if validation_snapshot:
            issues = list(getattr(validation_snapshot, "issues", []) or [])
            issue_codes = {
                str(issue.get("code") or "")
                for issue in issues
                if isinstance(issue, dict)
            }
            has_generation_block = any(code.startswith("generation.") for code in issue_codes)
            has_build_issue = any(code.startswith("build.") for code in issue_codes)
            has_preview_issue = any(code.startswith("preview.") for code in issue_codes)
            if getattr(validation_snapshot, "blocking", False) and has_generation_block:
                validators = "blocked"
            elif has_build_issue or has_preview_issue:
                validators = "passed"
            elif getattr(validation_snapshot, "grounded_spec_valid", False) or getattr(validation_snapshot, "app_ir_valid", False):
                validators = "passed"
            else:
                validators = "failed"
            build = "passed" if getattr(validation_snapshot, "build_valid", False) else "failed"
            if not getattr(validation_snapshot, "build_valid", False):
                preview = "skipped"
            elif preview_status == "running":
                preview = "passed"
            elif preview_status == "error":
                preview = "failed"
            else:
                preview = "pending"
        elif preview_status == "running":
            preview = "passed"
        elif preview_status == "error":
            preview = "failed"
        return RunChecksSummary(validators=validators, build=build, preview=preview, issues=issues)
