from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.services.engine.artifact_recorder import ArtifactRecorder
from app.services.engine.compaction_service import CompactionService
from app.services.engine.context_budget_manager import ContextBudgetManager
from app.services.engine.diminishing_returns_service import DiminishingReturnsService
from app.services.engine.prompt_state_manager import PromptStateManager
from app.services.engine.project_memory_service import ProjectMemoryService
from app.services.engine.session_cost_ledger import SessionCostLedger
from app.services.engine.task_router import TaskRouter
from app.services.engine.telemetry_log_bridge import TelemetryLogBridge


class SessionEngine:
    def __init__(
        self,
        artifact_recorder: ArtifactRecorder,
        task_router: TaskRouter,
        context_budget_manager: ContextBudgetManager,
        prompt_state_manager: PromptStateManager,
        compaction_service: CompactionService,
        telemetry_log_bridge: TelemetryLogBridge,
        session_cost_ledger: SessionCostLedger,
        project_memory_service: ProjectMemoryService,
        diminishing_returns_service: DiminishingReturnsService,
    ) -> None:
        self.artifact_recorder = artifact_recorder
        self.task_router = task_router
        self.context_budget_manager = context_budget_manager
        self.prompt_state_manager = prompt_state_manager
        self.compaction_service = compaction_service
        self.telemetry_log_bridge = telemetry_log_bridge
        self.session_cost_ledger = session_cost_ledger
        self.project_memory_service = project_memory_service
        self.diminishing_returns_service = diminishing_returns_service

    def bootstrap(
        self,
        *,
        workspace_id: str,
        prompt: str,
        generation_mode: str,
        model_profile: str,
        run_mode: str,
        stable_prefix: str,
        cache_key: str,
        target_file_count: int = 0,
    ) -> dict[str, Any]:
        fingerprint = self.prompt_state_manager.fingerprint(
            prompt=prompt,
            stable_prefix=stable_prefix,
            cache_key=cache_key,
        )
        budget = self.context_budget_manager.build_budget(
            generation_mode=generation_mode,
            target_file_count=target_file_count,
            run_mode=run_mode,
        )
        mode_snapshot = self.task_router.profile_snapshot(
            model_profile=model_profile,
            generation_mode=generation_mode,
            run_mode=run_mode,
        )
        self.artifact_recorder.store_workspace_report(workspace_id, "prompt_fingerprint", fingerprint.to_dict())
        self.artifact_recorder.store_workspace_report(workspace_id, "context_budget", {"workspace_id": workspace_id, "budget": budget})
        self.artifact_recorder.store_workspace_report(workspace_id, "mode_profile_snapshot", {"workspace_id": workspace_id, **mode_snapshot})
        self.telemetry_log_bridge.phase(
            workspace_id,
            "bootstrap",
            "Session engine bootstrap completed.",
            payload={
                "generation_mode": generation_mode,
                "run_mode": run_mode,
                "target_file_count": target_file_count,
            },
        )
        return {
            "prompt_fingerprint": fingerprint.to_dict(),
            "context_budget": budget,
            "mode_profile_snapshot": mode_snapshot,
        }

    def select_project_memory(
        self,
        *,
        workspace_id: str,
        prompt: str,
        generation_mode: str,
        run_mode: str,
    ) -> dict[str, Any]:
        payload = self.project_memory_service.select(
            workspace_id=workspace_id,
            prompt=prompt,
            run_mode=run_mode,
            generation_mode=generation_mode,
        )
        self.telemetry_log_bridge.phase(
            workspace_id,
            "memory_select",
            "Relevant project memory was selected for the current run.",
            payload={"selected_count": payload.get("selected_count", 0), "run_mode": run_mode},
        )
        return payload

    def record_run_summary(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        run_mode: str,
        generation_mode: str,
        status: str,
        model_profile: str | None,
        llm_model: str | None,
        cache_stats: dict[str, Any] | None,
        latency_breakdown: dict[str, Any] | None,
        summary: str,
        files: list[str],
        failure_class: str | None,
    ) -> dict[str, Any]:
        session_costs = self.session_cost_ledger.record_run(
            workspace_id=workspace_id,
            run_id=run_id,
            run_mode=run_mode,
            generation_mode=generation_mode,
            status=status,
            model_profile=model_profile,
            llm_model=llm_model,
            cache_stats=cache_stats,
            latency_breakdown=latency_breakdown,
        )
        project_memory = self.project_memory_service.record(
            workspace_id=workspace_id,
            prompt=prompt,
            summary=summary,
            files=files,
            failure_class=failure_class,
            status=status,
            run_mode=run_mode,
        )
        return {"session_costs": session_costs, "project_memory": project_memory}

    def should_stop_for_diminishing_returns(
        self,
        *,
        workspace_id: str,
        run_id: str,
        phase: str,
        generation_mode: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        decision = self.diminishing_returns_service.evaluate(
            workspace_id=workspace_id,
            run_id=run_id,
            phase=phase,
            generation_mode=generation_mode,
            metrics=metrics,
        )
        if decision.get("should_stop"):
            self.telemetry_log_bridge.phase(
                workspace_id,
                "diminishing_returns",
                "Consecutive low-signal iterations triggered an early stop.",
                payload=decision,
            )
        return decision

    def record_phase(
        self,
        *,
        workspace_id: str,
        phase: str,
        generation_mode: str,
        model_profile: str,
        run_mode: str,
        target_file_count: int = 0,
        started_at: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        route = self.task_router.route_for_phase(
            phase=phase,
            model_profile=model_profile,
            generation_mode=generation_mode,
            run_mode=run_mode,
            target_file_count=target_file_count,
        )
        payload = {
            "phase": phase,
            "route": route,
            "details": details or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if started_at is not None:
            payload["duration_ms"] = int((time.perf_counter() - started_at) * 1000)
        self.artifact_recorder.append_engine_trace(workspace_id, payload)
        self.telemetry_log_bridge.phase(workspace_id, phase, f"Phase {phase} recorded.", payload=payload)
        phase_metrics = self.artifact_recorder.store_workspace_report
        current = self.artifact_recorder.store
        existing = current.get("reports", f"phase_metrics:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        items = list(existing.get("items") or [])
        items.append(payload)
        existing["items"] = items
        phase_metrics(workspace_id, "phase_metrics", existing)
        return payload
