from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from app.models.domain import JobEvent, JobRecord, utc_now
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class GenerationProgressReportingRuntime(MiniappGenerationRuntimeOwner):
    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)
        self._sync_run_progress(job, event_type, message)
        self.service.workspace_log_service.append(job.workspace_id, source=f"generation.{event_type}", message=message, payload=details or {})
        logger.info("job_event workspace_id=%s job_id=%s event=%s message=%s", job.workspace_id, job.job_id, event_type, message)

    def _save_job(self, job: JobRecord) -> None:
        self.service.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def _store_report(self, key: str, payload: dict) -> None:
        self.service.store.upsert("reports", key, payload)

    def _clear_trace(self, workspace_id: str) -> None:
        self._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.service.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append({"stage": stage, "message": message, "payload": payload or {}, "created_at": utc_now().isoformat()})
        current["entries"] = entries
        self._store_report(report_key, current)
        self.service.workspace_log_service.append(workspace_id, source=f"generation.trace.{stage}", message=message, payload=payload or {})
        logger.info("trace workspace_id=%s stage=%s message=%s", workspace_id, stage, message)

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str) -> None:
        if not job.linked_run_id:
            return
        run_payload = self.service.store.get("runs", job.linked_run_id)
        if not run_payload:
            return
        stage, progress = self._run_progress_for_event(event_type)
        run_payload["linked_job_id"] = job.job_id
        run_payload["current_stage"] = stage
        run_payload["progress_percent"] = max(int(run_payload.get("progress_percent", 0)), progress)
        if job.llm_provider:
            run_payload["llm_provider"] = job.llm_provider
        if job.llm_model:
            run_payload["llm_model"] = job.llm_model
        if job.execution_class:
            run_payload["execution_class"] = job.execution_class
        if job.outcome_kind:
            run_payload["outcome_kind"] = job.outcome_kind
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.service.store.upsert("runs", job.linked_run_id, run_payload)

    def _sync_generation_cluster_progress(self, *, linked_run_id: str | None, completed_target_files: int, total_target_files: int, cluster_name: str) -> None:
        if not linked_run_id:
            return
        run_payload = self.service.store.get("runs", linked_run_id)
        if not run_payload:
            return
        ratio = min(1.0, max(0.0, completed_target_files / max(1, total_target_files)))
        progress = 16 + int(round(ratio * 60))
        run_payload["current_stage"] = f"generating code ({completed_target_files}/{total_target_files} files)"
        run_payload["progress_percent"] = max(int(run_payload.get("progress_percent", 0)), progress)
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.service.store.upsert("runs", linked_run_id, run_payload)
        self.service.workspace_log_service.append(run_payload["workspace_id"], source="generation.progress", message="Updated generation progress from completed code clusters.", payload={"linked_run_id": linked_run_id, "cluster_name": cluster_name, "completed_target_files": completed_target_files, "total_target_files": total_target_files, "progress_percent": progress})

    def _sync_generation_cluster_started(self, *, linked_run_id: str | None, completed_target_files: int, total_target_files: int, cluster_name: str, cluster_targets: list[str]) -> None:
        if not linked_run_id:
            return
        run_payload = self.service.store.get("runs", linked_run_id)
        if not run_payload:
            return
        cluster_target_count = max(1, len(cluster_targets))
        in_flight_ratio = min(1.0, max(0.0, (completed_target_files + (cluster_target_count * 0.35)) / max(1, total_target_files)))
        progress = 16 + int(round(in_flight_ratio * 60))
        preview_target = cluster_targets[0] if cluster_targets else cluster_name
        suffix = f" (+{len(cluster_targets) - 1} more)" if len(cluster_targets) > 1 else ""
        run_payload["current_stage"] = f"generating {completed_target_files + 1}-{min(total_target_files, completed_target_files + cluster_target_count)}/{total_target_files}: {preview_target}{suffix}"
        run_payload["progress_percent"] = max(int(run_payload.get("progress_percent", 0)), progress)
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.service.store.upsert("runs", linked_run_id, run_payload)
        self.service.workspace_log_service.append(run_payload["workspace_id"], source="generation.progress", message="Started generation for the next code cluster.", payload={"linked_run_id": linked_run_id, "cluster_name": cluster_name, "cluster_targets": cluster_targets, "completed_target_files": completed_target_files, "total_target_files": total_target_files, "progress_percent": progress})

    def _sync_generation_batch_started(self, *, linked_run_id: str | None, completed_target_files: int, total_target_files: int, batch: list[dict[str, Any]]) -> None:
        if not linked_run_id or not batch:
            return
        run_payload = self.service.store.get("runs", linked_run_id)
        if not run_payload:
            return
        batch_target_count = sum(len(list(cluster["target_files"])) for cluster in batch)
        in_flight_ratio = min(1.0, max(0.0, (completed_target_files + (batch_target_count * 0.35)) / max(1, total_target_files)))
        progress = 16 + int(round(in_flight_ratio * 60))
        first_targets = list(batch[0]["target_files"])
        preview_target = first_targets[0] if first_targets else str(batch[0]["cluster_name"])
        extra_clusters = len(batch) - 1
        parallel_suffix = f" (+{extra_clusters} parallel cluster{'s' if extra_clusters != 1 else ''})" if extra_clusters > 0 else ""
        run_payload["current_stage"] = f"generating {completed_target_files + 1}-{min(total_target_files, completed_target_files + batch_target_count)}/{total_target_files}: {preview_target}{parallel_suffix}"
        run_payload["progress_percent"] = max(int(run_payload.get("progress_percent", 0)), progress)
        run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.service.store.upsert("runs", linked_run_id, run_payload)
        self.service.workspace_log_service.append(run_payload["workspace_id"], source="generation.progress", message="Started generation batch for code clusters.", payload={"linked_run_id": linked_run_id, "batch_clusters": [str(cluster["cluster_name"]) for cluster in batch], "batch_targets": [list(cluster["target_files"]) for cluster in batch], "completed_target_files": completed_target_files, "total_target_files": total_target_files, "progress_percent": progress})

    @staticmethod
    def _whole_file_parallel_group(cluster_name: str) -> str:
        if cluster_name in {"backend_support", "shared_static"}:
            return "serial"
        if cluster_name.startswith("backend_route_"):
            return "backend_route"
        if cluster_name.startswith("role_") and "_ui_" in cluster_name:
            parts = cluster_name.split("_")
            if len(parts) >= 3:
                return f"role_ui_{parts[1]}"
            return "role_ui"
        return "serial"

    @classmethod
    def _group_generation_clusters_for_execution(cls, clusters: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        grouped: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(clusters):
            cluster = clusters[index]
            group_name = cls._whole_file_parallel_group(str(cluster["cluster_name"]))
            if group_name == "serial":
                grouped.append([cluster])
                index += 1
                continue
            batch_limit = 2 if group_name == "backend_route" or group_name.startswith("role_ui_") else 3
            batch = [cluster]
            index += 1
            while index < len(clusters) and len(batch) < batch_limit:
                candidate = clusters[index]
                if cls._whole_file_parallel_group(str(candidate["cluster_name"])) != group_name:
                    break
                batch.append(candidate)
                index += 1
            grouped.append(batch)
        return grouped

    @staticmethod
    def _run_progress_for_event(event_type: str) -> tuple[str, int]:
        progress_map = {
            "job_started": ("starting", 2),
            "indexing_started": ("indexing workspace", 3),
            "retrieval_started": ("retrieving context", 4),
            "retrieval_completed": ("retrieval complete", 6),
            "building_scaffold": ("building scaffold", 7),
            "scaffold_ready": ("scaffold ready", 12),
            "spec_started": ("building grounded spec", 7),
            "spec_ready": ("grounded spec ready", 9),
            "draft_prepared": ("preparing draft workspace", 10),
            "context_pack_started": ("collecting file context", 13),
            "context_pack_ready": ("context pack ready", 15),
            "generating_code": ("generating code", 16),
            "editing_started": ("generating draft edits", 16),
            "iteration_ready": ("draft edits prepared", 78),
            "fixing_code": ("fixing generated code", 82),
            "repair_started": ("repairing after build failure", 82),
            "repair_iteration": ("repairing draft", 86),
            "repair_scope_expanded": ("expanding repair scope", 88),
            "repair_repeated_signature_aborted": ("repair aborted", 100),
            "running_checks": ("running validation and build", 90),
            "build_started": ("running validation and build", 90),
            "checks_completed": ("checks complete", 94),
            "preview_skipped_due_to_build_failure": ("preview skipped until build is green", 92),
            "planner_contract_gap_detected": ("expanding missing miniapp contract targets", 14),
            "applying": ("applying draft", 96),
            "preview_rebuild_started": ("refreshing preview", 96),
            "preview_ready": ("preview ready", 98),
            "draft_ready": ("awaiting review", 99),
            "job_completed": ("almost complete", 99),
            "spec_blocked": ("blocked on spec", 100),
            "validation_failed": ("validation failed", 100),
            "job_failed": ("failed", 100),
        }
        return progress_map.get(event_type, ("processing", 12))
