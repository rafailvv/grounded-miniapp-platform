from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.models.domain import JobEvent, JobRecord, RunIterationRecord, utc_now

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixReportingRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def append_iteration_report(self, workspace_id: str, iteration: RunIterationRecord) -> None:
        report_key = f"iterations:{workspace_id}"
        current = self.service.store.get("reports", report_key) or {"workspace_id": workspace_id, "items": []}
        items = list(current.get("items", []))
        items.append(iteration.model_dump(mode="json"))
        current["items"] = items
        self.store_report(report_key, current)

    def clear_reports(self, workspace_id: str, *, preserve_generation_state: bool = False) -> None:
        keys = [
            "validation",
            "check_results",
            "fix_case",
            "fix_attempts",
            "scope_expansions",
            "fix_runtime",
        ]
        if not preserve_generation_state:
            keys.extend(["iterations", "candidate_diff", "patch"])
        for key in keys:
            self.service.store.delete("reports", f"{key}:{workspace_id}")

    def save_job(self, job: JobRecord) -> None:
        self.service.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.service.store.upsert("reports", key, payload)

    def clear_trace(self, workspace_id: str) -> None:
        self.store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def append_trace(
        self,
        workspace_id: str,
        stage: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.service.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append(
            {
                "stage": stage,
                "message": message,
                "payload": payload or {},
                "created_at": utc_now().isoformat(),
            }
        )
        current["entries"] = entries
        self.store_report(report_key, current)
        self.service.workspace_log_service.append(
            workspace_id,
            source=f"fix.trace.{stage}",
            message=message,
            payload=payload or {},
        )
        logger.info("trace workspace_id=%s stage=%s message=%s", workspace_id, stage, message)

    def append_event(
        self,
        job: JobRecord,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = utc_now()
        self.sync_run_progress(job, event_type, message, details or {})
        self.service.workspace_log_service.append(
            job.workspace_id,
            source=f"fix.{event_type}",
            message=message,
            payload=details or {},
        )
        self.save_job(job)

    def sync_run_progress(
        self,
        job: JobRecord,
        event_type: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        del details
        if not job.linked_run_id:
            return
        payload = self.service.store.get("runs", job.linked_run_id)
        if not payload:
            return
        stage, progress = self.run_progress_for_event(event_type)
        payload["linked_job_id"] = job.job_id
        payload["current_stage"] = stage
        payload["progress_percent"] = max(int(payload.get("progress_percent", 0)), progress)
        payload["summary"] = job.summary
        payload["failure_reason"] = job.failure_reason
        payload["failure_class"] = job.failure_class
        payload["failure_signature"] = job.failure_signature
        payload["root_cause_summary"] = job.root_cause_summary
        payload["current_fix_phase"] = job.current_fix_phase
        payload["current_failing_command"] = job.current_failing_command
        payload["current_exit_code"] = job.current_exit_code
        payload["fix_targets"] = list(job.fix_targets)
        payload["remaining_issues"] = list(job.remaining_issues)
        payload["repair_iterations"] = list(job.repair_iterations)
        payload["fix_attempts"] = list(job.fix_attempts)
        payload["scope_expansions"] = list(job.scope_expansions)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.service.store.upsert("runs", job.linked_run_id, payload)
        logger.info(
            "fix_progress run_id=%s stage=%s progress=%s message=%s",
            job.linked_run_id,
            stage,
            progress,
            message,
        )

    @staticmethod
    def run_progress_for_event(event_type: str) -> tuple[str, int]:
        progress_map = {
            "job_started": ("starting fix", 6),
            "triage_started": ("triaging failure", 12),
            "frontend_build_started": ("compiling frontend", 22),
            "backend_compile_started": ("compiling miniapp", 30),
            "preview_validation_started": ("rebuilding preview", 40),
            "triage_completed": ("evidence ready", 48),
            "repair_planned": ("planning repair patch", 58),
            "fast_visual_patch": ("fast visual patch", 58),
            "patch_apply_started": ("applying repair patch", 68),
            "patch_apply_completed": ("repair patch applied", 76),
            "scope_expanded": ("expanding fix scope", 80),
            "failure_reanalyzed": ("reading new failure", 84),
            "repair_iteration": ("retrying repair", 88),
            "checks_completed": ("checks complete", 94),
            "draft_ready": ("awaiting review", 99),
            "job_completed": ("almost complete", 99),
            "job_failed": ("failed", 100),
        }
        return progress_map.get(event_type, ("processing", 12))
