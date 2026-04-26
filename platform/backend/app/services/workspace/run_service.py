from __future__ import annotations

from pathlib import PurePosixPath
import re
import threading
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from app.ai.model_registry import resolve_model_profile
from app.ai.openai_client import OpenAIClient
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    CodeChangePlan,
    CodeChangeTarget,
    CreateRunRequest,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RunChecksSummary,
    RunRecord,
    ValidationSnapshot,
    WorkspaceRecord,
)
from app.modules.workspace_code_agent_runtime import WorkspaceCodeAgentRuntime
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.service import WorkspaceService

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
WORKSPACE_NAME_STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "build",
    "create",
    "for",
    "generate",
    "i",
    "in",
    "make",
    "me",
    "mini",
    "miniapp",
    "mini-app",
    "my",
    "need",
    "of",
    "simple",
    "that",
    "the",
    "this",
    "to",
    "with",
    "создай",
    "сделай",
    "сгенерируй",
    "приложение",
    "мини",
    "миниапп",
    "для",
    "мне",
    "нужно",
    "надо",
    "простое",
    "с",
    "и",
}
ROLE_SCOPE_HINTS: dict[str, tuple[str, ...]] = {
    "client": ("client", "customer", "buyer", "shopper", "user", "клиент", "покупатель", "покупательница", "пользователь", "заказчик"),
    "specialist": ("specialist", "worker", "staff", "employee", "master", "executor", "washer", "seller", "agent", "специалист", "сотрудник", "мастер", "исполнитель", "мойщик", "продавец", "операционист"),
    "manager": ("manager", "admin", "administrator", "operator", "owner", "менеджер", "администратор", "оператор", "админ", "руководитель", "владелец"),
}


class RunService:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        code_agent_runtime: WorkspaceCodeAgentRuntime,
        preview_service: PreviewService,
        check_runner: CheckRunner,
        openai_client: OpenAIClient,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.code_agent_runtime = code_agent_runtime
        self.preview_service = preview_service
        self.check_runner = check_runner
        self.openai_client = openai_client
        self.workspace_log_service = workspace_log_service
        self._active_workers: dict[str, threading.Thread] = {}
        self._startup_started_at = datetime.now(timezone.utc)
        self._recover_orphaned_active_runs()
        self._recover_orphaned_terminal_jobs()

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
        suggested_workspace_name = self._derive_workspace_name_from_prompt(request.prompt)
        if suggested_workspace_name:
            workspace = self.workspace_service.rename_workspace(workspace_id, suggested_workspace_name)
        resolved_role_scope = self._resolve_target_role_scope(request)
        resolved_intent = self._resolve_intent(workspace, request, resolved_role_scope=resolved_role_scope)
        effective_generation_mode = self._resolve_generation_mode(workspace, request, resolved_intent)
        effective_model_profile = self._resolve_model_profile(request.model_profile, effective_generation_mode)
        run = RunRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode=request.mode,
            intent=resolved_intent,
            apply_strategy=request.apply_strategy,
            approval_required=request.apply_strategy == "manual_approve",
            target_role_scope=resolved_role_scope,
            model_profile=effective_model_profile,
            generation_mode=effective_generation_mode,
            llm_provider=(self.openai_client.configuration().get("routing") or {}).get("provider") if self.openai_client.enabled else None,
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
            self._active_workers[run.run_id] = threading.current_thread()
            self._execute_run(run.run_id, request.model_dump(mode="python"))
            return self.get_run(run.run_id)
        worker = threading.Thread(
            target=self._execute_run,
            args=(run.run_id, request.model_dump(mode="python")),
            daemon=True,
        )
        self._active_workers[run.run_id] = worker
        worker.start()
        return self.get_run(run.run_id)

    @staticmethod
    def _derive_workspace_name_from_prompt(prompt: str) -> str:
        normalized = RunService._strip_prompt_time_prefix(str(prompt or ""))
        normalized = " ".join(normalized.split()).strip()
        if not normalized:
            return ""
        normalized = re.sub(r"^['\"`]+|['\"`]+$", "", normalized)
        lowered = normalized.lower()
        if lowered.startswith("i need "):
            normalized = normalized[7:]
        elif lowered.startswith("create "):
            normalized = normalized[7:]
        elif lowered.startswith("build "):
            normalized = normalized[6:]
        elif lowered.startswith("make "):
            normalized = normalized[5:]
        normalized = re.split(r"[.!?\n]", normalized, maxsplit=1)[0].strip(" ,;-:")
        if not normalized:
            return ""

        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", normalized)
        meaningful: list[str] = []
        for token in tokens:
            lowered_token = token.lower()
            if lowered_token in WORKSPACE_NAME_STOPWORDS:
                continue
            meaningful.append(token)
            if len(meaningful) >= 5:
                break
        if not meaningful:
            meaningful = tokens[:4]
        if not meaningful:
            return ""
        title = " ".join(word.capitalize() if not word.isupper() else word for word in meaningful)
        title = re.sub(r"\s+", " ", title).strip()
        return title[:48].rstrip(" -_,")

    @staticmethod
    def _strip_prompt_time_prefix(prompt: str) -> str:
        lines = str(prompt or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        timestamp_pattern = re.compile(
            r"^\s*(?:"
            r"(?:[01]?\d|2[0-3])[:.][0-5]\d(?:\s*(?:am|pm))?"
            r"|(?:1[0-2]|0?[1-9])[:.][0-5]\d\s*(?:am|pm)"
            r")\s*$",
            re.IGNORECASE,
        )
        while lines and timestamp_pattern.match(lines[0]):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        return "\n".join(lines).strip()

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

    def _recover_orphaned_active_runs(self) -> None:
        now = datetime.now(timezone.utc)
        for item in self.store.list("runs"):
            run = RunRecord.model_validate(item)
            if run.status not in {"pending", "running"} and run.current_stage != "stopping":
                continue
            if run.run_id in self._active_workers:
                continue
            if run.updated_at >= self._startup_started_at:
                continue
            stop_requested = self._is_stop_requested(run.run_id) or run.current_stage == "stopping"
            if stop_requested:
                run.status = "blocked"
                run.apply_status = "blocked"
                run.current_stage = "stopped"
                run.summary = run.summary or "Run was stopped during stale-run recovery."
                run.failure_reason = run.failure_reason or "Run was interrupted after a stop request and recovered during backend restart cleanup."
                run.outcome_kind = run.outcome_kind or "blocked_generation"
            else:
                run.status = "failed"
                run.apply_status = "failed"
                run.current_stage = "failed"
                run.summary = run.summary or "Run was interrupted before reaching a terminal state."
                run.failure_reason = run.failure_reason or "Run was recovered as stale after backend restart because no active worker existed."
                run.outcome_kind = run.outcome_kind or "blocked_generation"
            run.progress_percent = 100
            run.updated_at = now
            self._save_run(run)
            self._sync_linked_job_to_terminal_run_state(run, reason="stale_run_recovery")
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            logger.warning(
                "recovered_orphaned_run run_id=%s workspace_id=%s status=%s stage=%s",
                run.run_id,
                run.workspace_id,
                run.status,
                run.current_stage,
            )

    def _recover_orphaned_terminal_jobs(self) -> None:
        for item in self.store.list("jobs"):
            job = JobRecord.model_validate(item)
            if job.status not in {"pending", "running"}:
                continue
            run_id = str(job.linked_run_id or "").strip()
            if not run_id:
                continue
            run_payload = self.store.get("runs", run_id)
            if not run_payload:
                continue
            run = RunRecord.model_validate(run_payload)
            if run.status not in {"completed", "blocked", "failed"}:
                continue
            self._sync_linked_job_to_terminal_run_state(run, reason="terminal_run_job_sync")
            logger.warning(
                "recovered_orphaned_job job_id=%s run_id=%s workspace_id=%s job_status=%s run_status=%s",
                job.job_id,
                run.run_id,
                run.workspace_id,
                job.status,
                run.status,
            )

    def _sync_linked_job_to_terminal_run_state(self, run: RunRecord, *, reason: str) -> None:
        job_id = self._resolve_linked_job_id(run)
        if not job_id:
            return
        payload = self.store.get("jobs", job_id)
        if not payload:
            return
        job = JobRecord.model_validate(payload)
        if run.status == "completed":
            target_status = "completed"
            event_type = "job_completed"
            message = run.summary or "Job was synchronized to the completed run state."
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
        elif run.status == "blocked":
            target_status = "blocked"
            event_type = "job_failed"
            message = run.failure_reason or run.summary or "Job was synchronized to the blocked run state."
            job.failure_reason = run.failure_reason or message
            job.failure_class = run.failure_class
            job.failure_signature = run.failure_signature
            job.root_cause_summary = run.root_cause_summary
        elif run.status == "failed":
            target_status = "failed"
            event_type = "job_failed"
            message = run.failure_reason or run.summary or "Job was synchronized to the failed run state."
            job.failure_reason = run.failure_reason or message
            job.failure_class = run.failure_class
            job.failure_signature = run.failure_signature
            job.root_cause_summary = run.root_cause_summary
        else:
            return
        if job.status == target_status and (
            target_status == "completed" or (job.failure_reason or "") == (run.failure_reason or "")
        ):
            return
        job.status = target_status
        job.outcome_kind = run.outcome_kind
        job.summary = message
        job.linked_run_id = run.run_id
        job.updated_at = datetime.now(timezone.utc)
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
        self._append_job_event(
            job.job_id,
            event_type,
            message,
            {"reason": reason, "run_id": run.run_id, "run_status": run.status},
        )

    def _resolve_linked_job_id(self, run: RunRecord) -> str:
        direct_job_id = str(run.linked_job_id or "").strip()
        if direct_job_id:
            return direct_job_id
        latest_linked_job: JobRecord | None = None
        for item in self.store.list("jobs"):
            if str(item.get("linked_run_id") or "").strip() != run.run_id:
                continue
            candidate = JobRecord.model_validate(item)
            if latest_linked_job is None or candidate.updated_at > latest_linked_job.updated_at:
                latest_linked_job = candidate
        if latest_linked_job is None:
            return ""
        if run.linked_job_id != latest_linked_job.job_id:
            run.linked_job_id = latest_linked_job.job_id
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
        return latest_linked_job.job_id

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            run = self.get_run(run_id)
            if run.linked_job_id:
                job = self.code_agent_runtime.get_job(run.linked_job_id)
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
        run.outcome_kind = "applied"
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
        try:
            run = self.get_run(run_id)
            workspace = self.workspace_service.get_workspace(run.workspace_id)
            self.workspace_log_service.ensure_log_files(run.workspace_id)
            effective_generation_mode = self._resolve_generation_mode(workspace, request, run.intent)
            effective_model_profile = self._resolve_model_profile(request.model_profile, effective_generation_mode)
            run.status = "running"
            run.generation_mode = effective_generation_mode
            run.model_profile = effective_model_profile
            run.current_stage = "starting"
            run.progress_percent = max(run.progress_percent, 5)
            run.updated_at = datetime.now(timezone.utc)
            should_queue_followup_verification = self._should_queue_async_followup_verification(request, run)
            if should_queue_followup_verification:
                run.checks_summary = self._build_checks_summary(
                    job.validation_snapshot,
                    preview.status,
                    gate_status="passed",
                    followup_status="pending",
                    auto_fix_status="skipped",
                )
            self._save_run(run)
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                message="Run started.",
                payload={"run_id": run.run_id, "mode": run.mode, "intent": run.intent},
            )
            self.workspace_log_service.append_api(
                run.workspace_id,
                source="run",
                message="API log initialized for run.",
                payload={"run_id": run.run_id, "mode": run.mode},
            )
            logger.info("run_started run_id=%s workspace_id=%s intent=%s", run.run_id, run.workspace_id, run.intent)
            generate_request = GenerateRequest(
                prompt=request.prompt,
                mode=request.mode,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                generation_mode=effective_generation_mode,
                intent=run.intent,
                target_role_scope=run.target_role_scope,
                model_profile=effective_model_profile,
                linked_run_id=run.run_id,
                resume_from_run_id=request.resume_from_run_id,
                error_context=request.error_context,
            )
            with self.openai_client.workspace_logging(run.workspace_id):
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
                    job = self.code_agent_runtime.generate(
                        run.workspace_id,
                        resumed_generate_request,
                        should_stop=lambda: self._is_stop_requested(run.run_id),
                    )
                else:
                    job = (
                        self.code_agent_runtime.generate(
                            run.workspace_id,
                            generate_request,
                            should_stop=lambda: self._is_stop_requested(run.run_id),
                        )
                        if request.mode == "fix"
                        else self.code_agent_runtime.generate(
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
                with self.openai_client.workspace_logging(run.workspace_id):
                    job = self.code_agent_runtime.generate(
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
            if not run.failure_signature and run.failure_class:
                summary = str(run.root_cause_summary or run.failure_reason or run.failure_class).strip().lower()
                summary = re.sub(r"[^a-z0-9]+", "_", summary).strip("_")[:80]
                run.failure_signature = f"{run.failure_class}:{summary or 'failed'}"
            run.current_fix_phase = job.current_fix_phase
            run.current_failing_command = job.current_failing_command
            run.current_exit_code = job.current_exit_code
            run.fix_targets = list(job.fix_targets)
            run.remaining_issues = list(getattr(job, "remaining_issues", []) or [])
            run.handoff_from_failed_generate = dict(job.handoff_from_failed_generate or {}) or None
            run.checks_summary = self._build_checks_summary(job.validation_snapshot, preview.status)
            run.touched_files = self._resolve_touched_files(
                workspace_id=run.workspace_id,
                run=run,
                change_plan=change_plan,
                request=request,
            )
            run.candidate_revision_id = f"draft:{run.run_id}"
            run.iteration_count = len((self.code_agent_runtime.current_report(run.workspace_id, "iterations") or {}).get("items", []))
            run.latency_breakdown = dict(job.latency_breakdown)
            run.repair_iterations = list(job.repair_iterations)
            run.fix_attempts = list(job.fix_attempts)
            run.scope_expansions = list(job.scope_expansions)
            run.apply_result = dict(job.apply_result or {})
            run.retrieval_stats = dict(job.retrieval_stats)
            run.cache_stats = dict(job.cache_stats)
            run.artifacts = {
                "run_artifacts": f"/runs/{run.run_id}/artifacts",
                "preview_url": preview.url or "",
                "trace": f"/workspaces/{run.workspace_id}/logs",
                "iterations": f"/runs/{run.run_id}/iterations",
                "checks": f"/runs/{run.run_id}/checks",
                "patch": f"/runs/{run.run_id}/patch",
            }
            run.updated_at = datetime.now(timezone.utc)

            if job.status == "completed":
                should_apply_fix_draft = request.mode == "fix" and self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                if should_apply_fix_draft:
                    self._apply_completed_draft(run, message="Applying verified fix draft to the source workspace.")
                    self._clear_successful_completion_metadata(run=run, job=job)
                else:
                    meaningful_paths = self._meaningful_paths_for_run(
                        workspace_id=run.workspace_id,
                        run=run,
                        change_plan=change_plan,
                        job=job,
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
                        self._clear_successful_completion_metadata(run=run, job=job)
            else:
                meaningful_paths = self._meaningful_paths_for_run(
                    workspace_id=run.workspace_id,
                    run=run,
                    change_plan=change_plan,
                    job=job,
                )
                if self._complete_blocked_noop_run_from_green_source(
                    run=run,
                    job=job,
                    meaningful_paths=meaningful_paths,
                ):
                    self._save_run(run)
                    self._store_run_artifacts(run, change_plan, job, self.preview_service.get(run.workspace_id))
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
                    return
                if self._complete_failed_run_from_green_draft(
                    run=run,
                    job=job,
                    meaningful_paths=meaningful_paths,
                ):
                    should_queue_followup_verification = self._should_queue_async_followup_verification(request, run)
                    if should_queue_followup_verification:
                        run.checks_summary = self._build_checks_summary(
                            job.validation_snapshot,
                            self.preview_service.get(run.workspace_id).status,
                            gate_status="passed",
                            followup_status="pending",
                            auto_fix_status="skipped",
                        )
                    self._save_run(run)
                    self._store_run_artifacts(run, change_plan, job, self.preview_service.get(run.workspace_id))
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
                    if run.status == "completed" and run.apply_status == "applied":
                        self._queue_preview_refresh(
                            run,
                            reason="run completion",
                            draft_run_id=None,
                            followup_request=request if should_queue_followup_verification else None,
                        )
                    return
                if job.status == "blocked":
                    run.status = "blocked"
                    run.apply_status = "blocked"
                    run.outcome_kind = "blocked_preview_infra" if str(job.outcome_kind or "") == "blocked_preview_infra" else "blocked_generation"
                    has_draft = self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                    draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id) if has_draft else ""
                    run.draft_status = "ready" if has_draft and (meaningful_paths or draft_diff.strip()) else "failed"
                    run.draft_ready = run.draft_status == "ready"
                    if run.current_fix_phase == "completed":
                        run.current_fix_phase = "failed"
                    run.current_stage = "stopped" if self._is_stop_requested(run.run_id) else "blocked"
                    run.progress_percent = max(run.progress_percent, 100)
                    if run.draft_ready:
                        run.summary = "Strict-green validation did not pass. Draft was retained for inspection."
                else:
                    run.status = "failed"
                    run.apply_status = "failed"
                    run.outcome_kind = "blocked_preview_infra" if str(job.outcome_kind or "") == "blocked_preview_infra" else "blocked_generation"
                    has_draft = self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                    draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id) if has_draft else ""
                    run.draft_status = "ready" if has_draft and (meaningful_paths or draft_diff.strip()) else "failed"
                    run.draft_ready = run.draft_status == "ready"
                    if run.current_fix_phase == "completed":
                        run.current_fix_phase = "failed"
                    run.current_stage = "failed"
                    run.progress_percent = max(run.progress_percent, 100)
                    if run.draft_ready:
                        run.summary = "Strict-green validation did not pass. Draft was retained for inspection."

            if run.status == "awaiting_approval" and getattr(job, "status", None) != "completed":
                self._append_job_event(
                    run.linked_job_id,
                    "job_completed",
                    "Draft is ready for review after strict-green validation failed.",
                    {
                        "run_id": run.run_id,
                        "status": run.status,
                        "apply_status": run.apply_status,
                    },
                )
            queue_preview_reason: str | None = None
            if run.status == "completed" and run.apply_status == "applied":
                queue_preview_reason = "run completion"
                should_queue_followup_verification = self._should_queue_async_followup_verification(request, run)
                if should_queue_followup_verification:
                    run.checks_summary = self._build_checks_summary(
                        job.validation_snapshot,
                        self.preview_service.get(run.workspace_id).status,
                        gate_status="passed",
                        followup_status="pending",
                        auto_fix_status="skipped",
                    )
            self._save_run(run)
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
            if run.status == "completed" and run.apply_status == "applied":
                self._queue_resume_generation_from_checkpoint_if_needed(run, request)
            if queue_preview_reason is not None:
                self._queue_preview_refresh(
                    run,
                    reason=queue_preview_reason,
                    draft_run_id=None,
                    followup_request=request if should_queue_followup_verification else None,
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
            linked_job_id = self._resolve_linked_job_id(run)
            self._save_run(run)
            if linked_job_id:
                try:
                    job = self.code_agent_runtime.get_job(linked_job_id)
                except KeyError:
                    job = None
                if job is not None:
                    job.status = "failed"
                    job.summary = job.summary or "Run failed with an unexpected exception."
                    job.failure_reason = str(exc)
                    job.current_fix_phase = "failed" if job.current_fix_phase == "completed" else job.current_fix_phase
                    self.code_agent_runtime.append_event(
                        job,
                        "job_failed",
                        "Run failed with an unexpected exception.",
                        {
                            "run_id": run.run_id,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                level="ERROR",
                message="Run failed with an unexpected exception.",
                payload={
                    "run_id": run.run_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )
            logger.exception("run_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)
        finally:
            self._active_workers.pop(run_id, None)

    def _should_auto_fix_failed_generate(self, request: CreateRunRequest, job: Any) -> bool:
        if request.mode == "fix":
            return False
        if getattr(job, "status", None) != "failed":
            return False
        if not getattr(job, "handoff_from_failed_generate", None):
            return False
        validation_snapshot = getattr(job, "validation_snapshot", None)
        if validation_snapshot is None:
            return False
        if not getattr(validation_snapshot, "blocking", False):
            return False
        issues = [
            issue
            for issue in getattr(validation_snapshot, "issues", [])
            if isinstance(issue, dict) and issue.get("blocking", True)
        ]
        if not issues:
            return not getattr(validation_snapshot, "build_valid", True)
        if not getattr(validation_snapshot, "build_valid", True):
            return True
        return any(str(issue.get("location") or "") in {"preview", "tests"} for issue in issues)

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

    def _queue_preview_refresh(
        self,
        run: RunRecord,
        *,
        reason: str,
        draft_run_id: str | None = None,
        followup_request: CreateRunRequest | None = None,
    ) -> None:
        queue_started_at = time.perf_counter()
        self._append_job_event(
            run.linked_job_id,
            "preview_rebuild_started",
            f"Queued preview rebuild after {reason}.",
            {"reason": reason, "run_id": run.run_id, "draft_run_id": None},
        )

        source_dir = self.workspace_service.source_dir(run.workspace_id)

        def on_complete(preview: Any) -> None:
            if preview is None:
                preview = self.preview_service.get(run.workspace_id)
            actual_preview_ms = int(
                getattr(preview, "latency_breakdown", {}).get("last_rebuild_ms")
                or getattr(preview, "latency_breakdown", {}).get("last_start_ms")
                or 0
            )
            if preview.status == "running":
                self._append_job_event(
                    run.linked_job_id,
                    "preview_rebuild_completed",
                    "Preview rebuild finished successfully.",
                    {
                        "url": preview.url,
                        "stage": getattr(preview, "stage", "running"),
                        "progress_percent": getattr(preview, "progress_percent", 100),
                        "draft_run_id": getattr(preview, "draft_run_id", None),
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
                        "draft_run_id": getattr(preview, "draft_run_id", None),
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
            run_payload = self.store.get("runs", run.run_id)
            if run_payload is not None:
                latency_breakdown = dict(run_payload.get("latency_breakdown") or {})
                if actual_preview_ms > 0:
                    latency_breakdown["preview_ms"] = actual_preview_ms
                run_payload["latency_breakdown"] = latency_breakdown
                run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.store.upsert("runs", run.run_id, run_payload)
            if followup_request is not None:
                self._launch_async_followup_verification(parent_run_id=run.run_id, request=followup_request)

        preview = self.preview_service.rebuild_async(
            run.workspace_id,
            source_dir=source_dir,
            draft_run_id=None,
            on_complete=on_complete,
            force=True,
        )
        latency_breakdown = dict((self.store.get("runs", run.run_id) or {}).get("latency_breakdown") or run.latency_breakdown)
        latency_breakdown["preview_enqueue_ms"] = int((time.perf_counter() - queue_started_at) * 1000)
        latency_breakdown.setdefault("preview_ms", latency_breakdown["preview_enqueue_ms"])
        run.latency_breakdown = latency_breakdown
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

    def _launch_async_followup_verification(self, *, parent_run_id: str, request: CreateRunRequest) -> None:
        marker_key = f"followup_started:{parent_run_id}"
        if self.store.get("reports", marker_key):
            return
        self.store.upsert(
            "reports",
            marker_key,
            {"run_id": parent_run_id, "started_at": datetime.now(timezone.utc).isoformat()},
        )
        worker = threading.Thread(
            target=self._run_async_followup_verification,
            args=(parent_run_id, request.model_dump(mode="python")),
            daemon=True,
        )
        worker.start()

    def _run_async_followup_verification(self, parent_run_id: str, request_payload: dict[str, Any]) -> None:
        try:
            parent_run = self.get_run(parent_run_id)
        except KeyError:
            return
        request = CreateRunRequest.model_validate(request_payload)
        self._set_followup_status(parent_run_id, followup_status="pending")
        execution, validation_snapshot = self._run_followup_checks(parent_run)
        if self._followup_checks_passed(parent_run, execution, validation_snapshot):
            self._set_followup_status(parent_run_id, followup_status="passed")
            return
        self._set_followup_status(parent_run_id, followup_status="failed")
        if not self._should_auto_fix_followup_failure(execution, validation_snapshot):
            self._set_followup_status(parent_run_id, auto_fix_status="skipped")
            self._mark_parent_failed_followup(parent_run_id, execution)
            return
        self._set_followup_status(parent_run_id, auto_fix_status="pending")
        try:
            followup_run = self.create_run_sync(
                parent_run.workspace_id,
                self._build_followup_fix_request(parent_run=parent_run, request=request, execution=execution),
            )
        except Exception:
            self._set_followup_status(parent_run_id, auto_fix_status="failed")
            self._mark_parent_failed_followup(parent_run_id, execution)
            return
        self._set_followup_status(
            parent_run_id,
            followup_run_id=followup_run.run_id,
            auto_fix_status="passed" if followup_run.status == "completed" and followup_run.apply_status == "applied" else "failed",
        )
        if not (followup_run.status == "completed" and followup_run.apply_status == "applied"):
            self._mark_parent_failed_followup(parent_run_id, execution)
            return
        refreshed_parent = self.get_run(parent_run_id)
        post_fix_execution, _post_fix_validation = self._run_followup_checks(refreshed_parent)
        post_fix_passed = self._followup_checks_passed(refreshed_parent, post_fix_execution, _post_fix_validation)
        self._set_followup_status(
            parent_run_id,
            followup_status="passed" if post_fix_passed else "failed",
        )
        if not post_fix_passed:
            self._mark_parent_failed_followup(parent_run_id, post_fix_execution)

    def _run_followup_checks(self, run: RunRecord) -> tuple[CheckExecutionRecord, Any]:
        source_dir = self.workspace_service.source_dir(run.workspace_id)
        execution = self.check_runner.run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            source_dir=source_dir,
            changed_files=list(run.touched_files or ["miniapp"]),
            preview_run_id=None,
            scope_mode="full_build",
            check_profile="full",
        )
        validation_snapshot = self._validation_snapshot_from_execution(execution)
        self.store.upsert(
            "reports",
            f"followup_checks:{run.run_id}",
            {
                "execution": execution.model_dump(mode="json"),
                "validation": validation_snapshot.model_dump(mode="json"),
            },
        )
        return execution, validation_snapshot

    def _mark_parent_failed_followup(self, run_id: str, execution: CheckExecutionRecord) -> None:
        run_payload = self.store.get("runs", run_id)
        if run_payload is None:
            return
        failing_result = next((result for result in execution.results if result.status == "failed"), None)
        failure_reason = None
        if failing_result is not None:
            failure_reason = next(
                (line for line in reversed(failing_result.logs or []) if str(line).strip()),
                failing_result.details or f"{failing_result.name} failed.",
            )
        run_payload["status"] = "blocked"
        run_payload["current_stage"] = "follow-up failed"
        run_payload["failure_class"] = CheckRunner.classify_failure(execution.results) or "followup_verification"
        run_payload["failure_reason"] = failure_reason or "Follow-up verification failed after apply."
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", run_id, run_payload)

    def _followup_checks_passed(self, run: RunRecord, execution: CheckExecutionRecord, validation_snapshot: Any) -> bool:
        return bool(self._strict_green_completion_state(execution.results, validation_snapshot).get("strict_green"))

    @staticmethod
    def _validation_snapshot_from_execution(execution: CheckExecutionRecord) -> ValidationSnapshot:
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
    def _strict_green_completion_state(results: list[Any], validation_snapshot: Any | None) -> dict[str, object]:
        failed = [result for result in results if getattr(result, "status", None) == "failed"]
        remaining_issues = [
            {
                "kind": "check_failure",
                "check": getattr(result, "name", "unknown"),
                "details": getattr(result, "details", None),
                "logs": list(getattr(result, "logs", []) or [])[-8:],
                "blocking": True,
            }
            for result in failed
        ]
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in getattr(validation_snapshot, "issues", [])
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        return {
            "strict_green": not failed and not remaining_issues,
            "remaining_issues": remaining_issues,
        }

    def _should_auto_fix_followup_failure(self, execution: CheckExecutionRecord, validation_snapshot: Any) -> bool:
        if CheckRunner.has_tooling_failure(execution.results):
            return False
        failed_names = {result.name for result in execution.results if result.status == "failed"}
        if not failed_names:
            return False
        repairable_failures = {
            "schema_validators",
            "connectivity_validators",
            "changed_files_static",
            "platform_invariants",
            "generated_app_python_tests",
            "generated_app_js_tests",
            "preview_boot_smoke",
            "preview_connectivity_smoke",
        }
        if not failed_names.issubset(repairable_failures):
            return False
        if validation_snapshot is None:
            return True
        if not getattr(validation_snapshot, "build_valid", True):
            return True
        return any(
            isinstance(issue, dict) and issue.get("blocking", False)
            for issue in getattr(validation_snapshot, "issues", [])
        )

    def _build_followup_fix_request(
        self,
        *,
        parent_run: RunRecord,
        request: CreateRunRequest,
        execution: CheckExecutionRecord,
    ) -> CreateRunRequest:
        raw_error = "\n".join(
            [
                str(result.details or "").strip()
                for result in execution.results
                if result.status == "failed" and str(result.details or "").strip()
            ]
        ).strip() or "Follow-up verification failed after apply."
        failing_result = next((result for result in execution.results if result.status == "failed"), None)
        failing_target = str(failing_result.name if failing_result is not None else "followup_verification")
        error_source = "preview" if failing_target.startswith("preview_") else "runtime"
        return CreateRunRequest(
            prompt="Analyze the follow-up verification failure and apply the smallest safe fix.",
            mode="fix",
            intent="edit",
            apply_strategy="staged_auto_apply",
            target_role_scope=list(parent_run.target_role_scope),
            model_profile=parent_run.model_profile,
            generation_mode=parent_run.generation_mode if parent_run.generation_mode != GenerationMode.QUALITY else GenerationMode.BALANCED,
            resume_from_run_id=parent_run.run_id,
            error_context={
                "raw_error": raw_error,
                "source": error_source,
                "failing_target": failing_target,
            },
        )

    def _set_followup_status(
        self,
        run_id: str,
        *,
        followup_status: str | None = None,
        followup_run_id: str | None = None,
        auto_fix_status: str | None = None,
    ) -> None:
        run_payload = self.store.get("runs", run_id)
        if run_payload is None:
            return
        checks_payload = dict(run_payload.get("checks_summary") or {})
        if followup_status is not None:
            checks_payload["followup_status"] = followup_status
        if followup_run_id is not None:
            checks_payload["followup_run_id"] = followup_run_id
        if auto_fix_status is not None:
            checks_payload["auto_fix_status"] = auto_fix_status
        run_payload["checks_summary"] = checks_payload
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", run_id, run_payload)
        artifacts_payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if artifacts_payload is not None:
            run_snapshot = dict(artifacts_payload.get("run") or {})
            run_snapshot["checks_summary"] = checks_payload
            artifacts_payload["run"] = run_snapshot
            self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts_payload)

    def _queue_resume_generation_from_checkpoint_if_needed(self, run: RunRecord, request: CreateRunRequest) -> None:
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
            generation_mode=str(checkpoint.get("generation_mode") or getattr(run.generation_mode, "value", run.generation_mode)),
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
        iterations = (self.code_agent_runtime.current_report(workspace_id, "iterations") or {}).get("items", [])
        candidate_diff = (self.code_agent_runtime.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        if candidate_diff:
            effective_diff = candidate_diff
        elif run.draft_ready and self.workspace_service.draft_exists(workspace_id, run.run_id):
            effective_diff = self.workspace_service.diff(workspace_id, run_id=run.run_id)
        else:
            effective_diff = self.workspace_service.diff(workspace_id)
        preview_payload = self._preview_snapshot(workspace_id, preview)
        patch_payload = self.code_agent_runtime.current_report(workspace_id, "patch")
        if not patch_payload and effective_diff.strip():
            patch_paths = self._paths_from_diff(effective_diff)
            if not patch_paths:
                patch_paths = [target.file_path for target in change_plan.targets if target.file_path]
            patch_payload = {
                "envelope": {
                    "ops": [{"file_path": path, "operation": "replace"} for path in patch_paths],
                },
                "apply_result": job.apply_result,
            }
        payload = {
            "run": run.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "trace": self.code_agent_runtime.current_report(workspace_id, "trace"),
            "iterations": iterations,
            "check_results": (self.code_agent_runtime.current_report(workspace_id, "check_results") or {}).get("items", []),
            "checks": self.code_agent_runtime.current_report(workspace_id, "check_results"),
            "patch": patch_payload,
            "diff": effective_diff,
            "preview": preview_payload,
            "draft_preview": {
                key: value
                for key, value in preview_payload.items()
                if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
            },
            "latency_breakdown": job.latency_breakdown,
            "retrieval_stats": job.retrieval_stats,
            "cache_stats": job.cache_stats,
            "apply_result": job.apply_result,
            "preview_infra_diagnostics": {
                "failure_kind": getattr(preview, "preview_failure_kind", None),
                "retry_count": getattr(preview, "preview_retry_count", 0),
                "cleanup_attempted": getattr(preview, "preview_cleanup_attempted", False),
                "reused_existing_runtime": getattr(preview, "preview_reused_existing_runtime", False),
                "cooldown_until": getattr(preview, "preview_cooldown_until", None).isoformat()
                if getattr(preview, "preview_cooldown_until", None)
                else None,
                "last_error": preview.last_error,
            },
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

    def _resolve_intent(self, workspace: WorkspaceRecord, request: CreateRunRequest, *, resolved_role_scope: list[str] | None = None) -> str:
        if request.intent != "auto":
            return request.intent
        if request.mode == "fix":
            return "edit"
        prompt = request.prompt.lower()
        role_scope = list(resolved_role_scope if resolved_role_scope is not None else self._resolve_target_role_scope(request))
        if self._looks_like_fix_request(prompt):
            return "edit"
        if self._looks_like_create_request(prompt):
            return "create"
        if role_scope and len(role_scope) == 1:
            return "role_only_change"
        if any(token in prompt for token in ("refine", "polish", "improve", "tighten", "cleanup")):
            return "refine"
        has_existing_build = self._workspace_has_existing_build(workspace)
        if has_existing_build or any(token in prompt for token in ("change", "update", "edit", "modify", "rewrite", "fix", "исправ", "ошиб")):
            return "edit"
        return "create"

    @classmethod
    def _resolve_target_role_scope(cls, request: CreateRunRequest) -> list[str]:
        explicit_scope = [role for role in request.target_role_scope if role in ROLE_SCOPE]
        if explicit_scope:
            return explicit_scope
        prompt = str(request.prompt or "").lower()
        inferred_scope: list[str] = []
        for role in ("client", "specialist", "manager"):
            hints = ROLE_SCOPE_HINTS.get(role) or ()
            if any(hint in prompt for hint in hints):
                inferred_scope.append(role)
        return inferred_scope

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
        has_existing_build = self._workspace_has_existing_build(workspace)
        if self._looks_like_fix_request(prompt):
            return GenerationMode.BALANCED
        if resolved_intent in {"edit", "refine", "role_only_change"} and has_existing_build:
            return GenerationMode.BALANCED
        return request.generation_mode

    def _workspace_has_existing_build(self, workspace: WorkspaceRecord) -> bool:
        if not workspace.template_cloned or workspace.current_revision_id is None:
            return False
        if any(str(revision.source or "").strip() != "template_clone" for revision in workspace.revisions):
            return True
        source_dir = self.workspace_service.source_dir(workspace.workspace_id)
        try:
            status_output = self.workspace_service._git_output(source_dir, ["status", "--porcelain"])
        except Exception:
            return False
        paths: list[str] = []
        for line in status_output.splitlines():
            candidate = str(line[3:] if len(line) > 3 else "").strip()
            if " -> " in candidate:
                candidate = candidate.split(" -> ", 1)[-1].strip()
            if candidate:
                paths.append(candidate)
        return any(self._is_meaningful_source_path(path) for path in paths)

    @staticmethod
    def _resolve_model_profile(model_profile: str | None, generation_mode: GenerationMode) -> str:
        return resolve_model_profile(model_profile, generation_mode)

    def _build_change_plan(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        diff_text: str,
        prompt: str,
    ) -> CodeChangePlan:
        iteration_payload = self.code_agent_runtime.current_report(workspace_id, "iterations") or {}
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
            "Generated app behavior must remain valid in the preview shell.",
            "Draft changes should not overwrite manual workspace edits outside the reviewed draft.",
        ]
        if diff_text.strip():
            risks.append("Existing workspace edits must be preserved and not overwritten unexpectedly.")
        acceptance_checks = [
            "Agent-authored changes satisfy the user prompt.",
            "Platform checks succeed on the draft workspace.",
            "Preview runtime starts and serves usable app pages.",
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

    @staticmethod
    def _looks_like_create_request(prompt: str) -> bool:
        create_markers = (
            "create",
            "build",
            "make",
            "generate",
            "new app",
            "new workspace",
            "создай",
            "создать",
            "сделай",
            "сгенерируй",
            "новое приложение",
        )
        return any(marker in prompt for marker in create_markers)

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
        candidate_paths = [target.file_path for target in change_plan.targets if target.file_path]
        if request.mode == "fix":
            inherited = self._inherited_touched_files_for_fix(workspace_id=workspace_id, run=run)
            if inherited:
                candidate_paths = list(dict.fromkeys([*candidate_paths, *inherited]))
        return candidate_paths

    def _revision_commit_sha(self, workspace: WorkspaceRecord, revision_id: str | None) -> str | None:
        if not revision_id:
            return None
        for revision in workspace.revisions:
            if revision.revision_id == revision_id:
                return revision.commit_sha
        return None

    def _meaningful_paths_between_revisions(
        self,
        *,
        workspace_id: str,
        source_revision_id: str | None,
        result_revision_id: str | None,
    ) -> list[str]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_sha = self._revision_commit_sha(workspace, source_revision_id)
        result_sha = self._revision_commit_sha(workspace, result_revision_id)
        if not source_sha or not result_sha or source_sha == result_sha:
            return []
        try:
            diff_output = self.workspace_service._git_output(
                self.workspace_service.source_dir(workspace_id),
                ["diff", "--name-only", f"{source_sha}..{result_sha}"],
            )
        except Exception:
            return []
        paths = [line.strip() for line in diff_output.splitlines() if line.strip()]
        return [path for path in list(dict.fromkeys(paths)) if self._is_meaningful_source_path(path)]

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
        message = "Draft produced no meaningful source changes to apply."
        run.summary = message
        run.failure_reason = message
        run.status = "failed"
        run.apply_status = "failed"
        run.outcome_kind = "noop_generation_failure"
        run.draft_status = "discarded" if self.workspace_service.draft_exists(run.workspace_id, run.run_id) else "none"
        run.draft_ready = False
        run.current_stage = "failed"
        run.progress_percent = max(run.progress_percent, 100)
        run.current_fix_phase = job.current_fix_phase

        if self.workspace_service.draft_exists(run.workspace_id, run.run_id):
            self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        job.status = "failed"
        job.summary = message
        job.failure_reason = message
        self.code_agent_runtime.append_event(job, "job_failed", message, {"reason": "no_meaningful_diff"})

    def _complete_blocked_noop_run_from_green_source(
        self,
        *,
        run: RunRecord,
        job: Any,
        meaningful_paths: list[str],
    ) -> bool:
        if meaningful_paths:
            return False
        if str(getattr(job, "failure_class", "") or "") != "generation.edit.llm_failure":
            return False

        source_dir = self.workspace_service.source_dir(run.workspace_id)
        execution = self.check_runner.run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            source_dir=source_dir,
            changed_files=[],
            preview_run_id=None,
            scope_mode="full_build",
        )
        validation_snapshot = self._validation_snapshot_from_execution(execution)
        completion_state = self._strict_green_completion_state(execution.results, validation_snapshot)
        if not bool(completion_state.get("strict_green")):
            return False

        self.store.upsert(
            "reports",
            f"check_results:{run.workspace_id}",
            {
                "items": [result.model_dump(mode="json") for result in execution.results],
                "execution": execution.model_dump(mode="json"),
            },
        )
        self.store.upsert(
            "reports",
            f"validation:{run.workspace_id}",
            validation_snapshot.model_dump(mode="json"),
        )

        if self.workspace_service.draft_exists(run.workspace_id, run.run_id):
            self.workspace_service.discard_draft(run.workspace_id, run.run_id)

        message = "Current workspace source already passed the quality gates. No meaningful draft changes were needed."
        run.result_revision_id = run.source_revision_id
        run.candidate_revision_id = run.source_revision_id
        run.status = "completed"
        run.apply_status = "noop"
        run.outcome_kind = "warnings"
        run.summary = message
        run.failure_reason = None
        run.failure_class = None
        run.failure_signature = None
        run.root_cause_summary = None
        run.remaining_issues = list(completion_state.get("remaining_issues") or [])
        run.draft_status = "discarded"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.checks_summary = self._build_checks_summary(validation_snapshot, self.preview_service.get(run.workspace_id).status)
        run.current_fix_phase = None
        run.current_failing_command = None
        run.current_exit_code = None
        run.fix_targets = []
        run.handoff_from_failed_generate = None
        run.touched_files = []
        run.updated_at = datetime.now(timezone.utc)

        job.status = "completed"
        job.outcome_kind = "warnings"
        job.summary = message
        job.failure_reason = None
        job.failure_class = None
        job.failure_signature = None
        job.root_cause_summary = None
        job.validation_snapshot = validation_snapshot
        self._append_job_event(
            run.linked_job_id,
            "job_completed",
            message,
            {"reason": "green_source_noop", "run_id": run.run_id},
        )
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message=message,
            payload={"run_id": run.run_id, "mode": "noop_completion"},
        )
        return True

    def _complete_failed_run_from_green_draft(
        self,
        *,
        run: RunRecord,
        job: Any,
        meaningful_paths: list[str],
    ) -> bool:
        if not meaningful_paths:
            return False
        if str(getattr(job, "failure_class", "") or "") == "generation.edit.llm_failure":
            return False
        if any(
            isinstance(result, dict)
            and result.get("name") == "prompt_alignment_smoke"
            and result.get("status") == "failed"
            for result in getattr(job, "executed_checks", []) or []
        ):
            return False
        if not self.workspace_service.draft_exists(run.workspace_id, run.run_id):
            return False
        if run.apply_strategy != "staged_auto_apply":
            return False

        draft_source = self.workspace_service.draft_source_dir(run.workspace_id, run.run_id)
        execution = self.check_runner.run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            source_dir=draft_source,
            changed_files=meaningful_paths,
            preview_run_id=run.run_id,
            scope_mode="full_build",
        )
        validation_snapshot = self._validation_snapshot_from_execution(execution)
        results = execution.results
        draft_green = all(
            result.status != "failed"
            for result in results
            if result.name not in {"preview_boot_smoke", "preview_connectivity_smoke"}
        )
        if not draft_green:
            return False

        self.store.upsert(
            "reports",
            f"check_results:{run.workspace_id}",
            {
                "items": [result.model_dump(mode="json") for result in execution.results],
                "execution": execution.model_dump(mode="json"),
            },
        )
        self.store.upsert(
            "reports",
            f"validation:{run.workspace_id}",
            validation_snapshot.model_dump(mode="json"),
        )

        message = "Retained draft passed validators/build/tests and was applied automatically."
        run.checks_summary = self._build_checks_summary(validation_snapshot, self.preview_service.get(run.workspace_id).status)
        run.remaining_issues = []
        run.summary = message
        run.failure_reason = None
        run.failure_class = None
        run.failure_signature = None
        run.root_cause_summary = None
        run.current_fix_phase = None
        run.current_failing_command = None
        run.current_exit_code = None
        run.fix_targets = []
        run.handoff_from_failed_generate = None
        run.updated_at = datetime.now(timezone.utc)

        job.status = "completed"
        job.outcome_kind = "applied"
        job.summary = message
        job.failure_reason = None
        job.failure_class = None
        job.failure_signature = None
        job.root_cause_summary = None
        job.validation_snapshot = validation_snapshot
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
        self._append_job_event(
            run.linked_job_id,
            "job_completed",
            message,
            {"reason": "green_draft_auto_apply", "run_id": run.run_id},
        )
        self._apply_completed_draft(run, message="Applying retained green draft to the source workspace.")
        self._clear_successful_completion_metadata(run=run, job=job)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message=message,
            payload={"run_id": run.run_id, "mode": "green_draft_auto_apply"},
        )
        return True

    @staticmethod
    def _should_queue_async_followup_verification(request: CreateRunRequest, run: RunRecord) -> bool:
        if request.mode == "fix":
            return False
        if run.status != "completed" or run.apply_status != "applied":
            return False
        if run.generation_mode == GenerationMode.QUALITY:
            return False
        return run.intent in {"create", "edit", "refine", "role_only_change"}

    def _should_apply_best_effort_after_failed_repairs(self, run: RunRecord, job: Any, *, meaningful_paths: list[str]) -> bool:
        del run, job, meaningful_paths
        return False

    def _should_keep_draft_for_manual_review(self, run: RunRecord, job: Any, *, meaningful_paths: list[str]) -> bool:
        del run, job, meaningful_paths
        return False

    @staticmethod
    def _clear_successful_completion_metadata(*, run: RunRecord, job: Any | None = None) -> None:
        run.failure_reason = None
        run.failure_class = None
        run.failure_signature = None
        run.root_cause_summary = None
        run.current_fix_phase = None
        run.current_failing_command = None
        run.current_exit_code = None
        run.fix_targets = []
        run.handoff_from_failed_generate = None
        if job is not None:
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
            job.current_fix_phase = None
            job.current_failing_command = None
            job.current_exit_code = None
            job.fix_targets = []
            job.handoff_from_failed_generate = None

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
        run.outcome_kind = "applied"
        run.remaining_issues = []
        self._clear_successful_completion_metadata(run=run)
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.latency_breakdown["apply_ms"] = int((time.perf_counter() - apply_started_at) * 1000)
        revision_paths = self._meaningful_paths_between_revisions(
            workspace_id=run.workspace_id,
            source_revision_id=run.source_revision_id,
            result_revision_id=run.result_revision_id,
        )
        if revision_paths:
            run.touched_files = revision_paths
        self._append_job_event(
            run.linked_job_id,
            "apply_completed",
            "Generated draft was applied successfully.",
        )

    def _meaningful_paths_for_run(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        change_plan: CodeChangePlan,
        job: Any | None = None,
    ) -> list[str]:
        candidate_diff = (self.code_agent_runtime.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        diff_text = candidate_diff
        if not diff_text and self.workspace_service.draft_exists(workspace_id, run.run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=run.run_id)

        paths = self._paths_from_diff(diff_text)
        if not paths:
            paths = [target.file_path for target in change_plan.targets if target.file_path]
        if not paths and job is not None:
            apply_result = getattr(job, "apply_result", None) or {}
            if isinstance(apply_result, dict):
                changed_files = apply_result.get("changed_files")
                if isinstance(changed_files, list):
                    paths.extend(str(path) for path in changed_files if str(path).strip())
            fix_targets = getattr(job, "fix_targets", None)
            if isinstance(fix_targets, list):
                paths.extend(str(path) for path in fix_targets if str(path).strip())
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
    def _build_checks_summary(
        validation_snapshot: Any,
        preview_status: str,
        *,
        gate_status: str | None = None,
        followup_status: str | None = None,
        followup_run_id: str | None = None,
        auto_fix_status: str | None = None,
    ) -> RunChecksSummary:
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
            elif getattr(validation_snapshot, "platform_valid", False) or getattr(validation_snapshot, "checks_valid", False):
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
        resolved_gate_status = gate_status
        if resolved_gate_status is None:
            if validators in {"failed", "blocked"} or build == "failed":
                resolved_gate_status = "failed"
            elif validators == "passed" and build == "passed":
                resolved_gate_status = "passed"
            else:
                resolved_gate_status = "pending"
        resolved_followup_status = followup_status
        if resolved_followup_status is None:
            if preview == "passed":
                resolved_followup_status = "passed"
            elif preview == "failed":
                resolved_followup_status = "failed"
            elif preview == "skipped":
                resolved_followup_status = "skipped"
            else:
                resolved_followup_status = "pending"
        return RunChecksSummary(
            validators=validators,
            build=build,
            preview=preview,
            gate_status=resolved_gate_status,
            followup_status=resolved_followup_status,
            followup_run_id=followup_run_id,
            auto_fix_status=auto_fix_status or "skipped",
            issues=issues,
        )
