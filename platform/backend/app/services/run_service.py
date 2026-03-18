from __future__ import annotations

import re
import threading
import logging
from datetime import datetime, timezone
from typing import Any

from app.ai.openrouter_client import OpenRouterClient
from app.models.common import GenerationMode
from app.models.domain import (
    CodeChangePlan,
    CodeChangeTarget,
    CreateRunRequest,
    GenerateRequest,
    RunChecksSummary,
    RunRecord,
    WorkspaceRecord,
)
from app.repositories.state_store import StateStore
from app.services.generation_service import GenerationService
from app.services.preview_service import PreviewService
from app.services.workspace_service import WorkspaceService

ROLE_SCOPE = {"client", "specialist", "manager"}
logger = logging.getLogger(__name__)


class RunService:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        generation_service: GenerationService,
        preview_service: PreviewService,
        openrouter_client: OpenRouterClient,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.generation_service = generation_service
        self.preview_service = preview_service
        self.openrouter_client = openrouter_client

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
            llm_provider="openrouter" if self.openrouter_client.enabled else None,
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
        revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Approve AI draft for run {run.run_id}")
        self.preview_service.rebuild(run.workspace_id)
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.status = "completed"
        run.apply_status = "applied"
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.store.delete("reports", f"run_stop_request:{run_id}")
        return run

    def discard_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        self.preview_service.rebuild(run.workspace_id)
        run.status = "failed"
        run.apply_status = "failed"
        run.draft_status = "discarded"
        run.draft_ready = False
        run.current_stage = "discarded"
        run.progress_percent = 100
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
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
        self.preview_service.rebuild(run.workspace_id)
        run.rolled_back = True
        run.rolled_back_at = datetime.now(timezone.utc)
        run.apply_status = "rolled_back"
        run.current_stage = "rolled back"
        run.progress_percent = 100
        run.candidate_revision_id = revision.revision_id
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
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
        run.current_stage = "starting"
        run.progress_percent = max(run.progress_percent, 5)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        logger.info("run_started run_id=%s workspace_id=%s intent=%s", run.run_id, run.workspace_id, run.intent)
        try:
            job = self.generation_service.generate(
                run.workspace_id,
                GenerateRequest(
                    prompt=request.prompt,
                    mode=request.mode,
                    target_platform=request.target_platform,
                    preview_profile=request.preview_profile,
                    generation_mode=effective_generation_mode,
                    intent=run.intent,
                    target_role_scope=run.target_role_scope,
                    model_profile=request.model_profile,
                    linked_run_id=run.run_id,
                    error_context=request.error_context,
                ),
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
            run.root_cause_summary = job.root_cause_summary
            run.fix_targets = list(job.fix_targets)
            run.handoff_from_failed_generate = dict(job.handoff_from_failed_generate or {}) or None
            run.checks_summary = self._build_checks_summary(job.validation_snapshot, preview.status)
            run.touched_files = self._resolve_touched_files(change_plan)
            run.candidate_revision_id = f"draft:{run.run_id}"
            run.iteration_count = len((self.generation_service.current_report(run.workspace_id, "iterations") or {}).get("items", []))
            run.latency_breakdown = dict(job.latency_breakdown)
            run.repair_iterations = list(job.repair_iterations)
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
                if run.apply_strategy == "manual_approve":
                    run.status = "awaiting_approval"
                    run.apply_status = "awaiting_approval"
                    run.draft_status = "ready"
                    run.draft_ready = True
                    run.current_stage = "awaiting review"
                    run.progress_percent = 100
                else:
                    revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Auto-apply AI draft for run {run.run_id}")
                    self.preview_service.rebuild(run.workspace_id)
                    self.workspace_service.discard_draft(run.workspace_id, run.run_id)
                    run.result_revision_id = revision.revision_id
                    run.candidate_revision_id = revision.revision_id
                    run.status = "completed"
                    run.apply_status = "applied"
                    run.draft_status = "approved"
                    run.draft_ready = False
                    run.current_stage = "completed"
                    run.progress_percent = 100
            elif job.status == "blocked":
                run.status = "blocked"
                run.apply_status = "blocked"
                run.draft_status = "failed"
                run.current_stage = "stopped" if self._is_stop_requested(run.run_id) else "blocked"
                run.progress_percent = max(run.progress_percent, 100)
            else:
                run.status = "failed"
                run.apply_status = "failed"
                run.draft_status = "failed"
                run.current_stage = "failed"
                run.progress_percent = max(run.progress_percent, 100)

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
        except Exception as exc:
            run.status = "failed"
            run.apply_status = "failed"
            run.failure_reason = str(exc)
            run.current_stage = "failed"
            run.progress_percent = max(run.progress_percent, 100)
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            logger.exception("run_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)

    def _is_stop_requested(self, run_id: str) -> bool:
        payload = self.store.get("reports", f"run_stop_request:{run_id}")
        return bool(payload and payload.get("requested"))

    def _store_run_artifacts(self, run: RunRecord, change_plan: CodeChangePlan, job: Any, preview: Any) -> None:
        workspace_id = run.workspace_id
        iterations = (self.generation_service.current_report(workspace_id, "iterations") or {}).get("items", [])
        candidate_diff = (self.generation_service.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
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
            "diff": self.workspace_service.diff(workspace_id, run_id=run.run_id),
            "preview": {
                "status": preview.status,
                "runtime_mode": preview.runtime_mode,
                "url": preview.url,
                "role_urls": self.preview_service.role_urls(workspace_id),
                "logs": preview.logs,
                "draft_run_id": preview.draft_run_id,
            },
            "draft_preview": {
                "status": preview.status,
                "runtime_mode": preview.runtime_mode,
                "url": preview.url,
                "role_urls": self.preview_service.role_urls(workspace_id),
            },
            "final_summary": job.summary,
            "latency_breakdown": job.latency_breakdown,
            "retrieval_stats": job.retrieval_stats,
            "cache_stats": job.cache_stats,
            "apply_result": job.apply_result,
            "repair_iterations": job.repair_iterations,
            "failure_analysis": {
                "mode": run.mode,
                "failure_class": job.failure_class,
                "root_cause_summary": job.root_cause_summary,
                "fix_targets": job.fix_targets,
                "handoff_from_failed_generate": job.handoff_from_failed_generate,
                "error_context": job.error_context.model_dump(mode="json") if job.error_context else None,
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
            return GenerationMode.BALANCED if request.generation_mode == GenerationMode.QUALITY else request.generation_mode
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

    @classmethod
    def _resolve_touched_files(cls, change_plan: CodeChangePlan) -> list[str]:
        paths = [target.file_path for target in change_plan.targets if target.file_path and not target.file_path.startswith("artifacts/")]
        if paths:
            return list(dict.fromkeys(paths))
        return [target.file_path for target in change_plan.targets if target.file_path]

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
            elif getattr(validation_snapshot, "grounded_spec_valid", False) and getattr(validation_snapshot, "app_ir_valid", False):
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
