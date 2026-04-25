from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.services.generation_runtime_config import (
    ACTIVE_GENERATION_QUERY_CONFIG,
    ACTIVE_GENERATION_TURN_STATE,
    GenerationQueryConfig,
    GenerationTurnState,
)

if TYPE_CHECKING:
    from app.models.domain import JobRecord
    from app.services.miniapp_generation.service import GenerationService


class GenerationQueryRuntime:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def _capture_config(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str,
        model_profile: str,
        generation_mode: Any,
        target_role_scope: list[str],
        preview_profile: Any,
    ) -> tuple[GenerationQueryConfig, GenerationTurnState]:
        prompt_started_at = time.perf_counter()
        config = GenerationQueryConfig.capture(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt=prompt,
            intent=intent,
            model_profile=model_profile,
            generation_mode=generation_mode,
            target_role_scope=target_role_scope,
            preview_profile=str(getattr(preview_profile, "value", preview_profile)),
        )
        turn_state = GenerationTurnState(
            prompt_build_ms=int((time.perf_counter() - prompt_started_at) * 1000),
        )
        return config, turn_state

    def run_new_query(self, *, workspace_id: str, job: "JobRecord", request: Any, kwargs: dict[str, Any]) -> "JobRecord":
        config, turn_state = self._capture_config(
            workspace_id=workspace_id,
            run_id=str(kwargs.get("draft_run_id") or job.linked_run_id or job.job_id),
            prompt=str(kwargs.get("effective_prompt") or request.prompt),
            intent=str(request.intent),
            model_profile=str(job.model_profile or ""),
            generation_mode=kwargs.get("generation_mode"),
            target_role_scope=list(kwargs.get("role_scope") or []),
            preview_profile=kwargs.get("preview_profile"),
        )
        self.service._store_report(
            f"query_config:{workspace_id}",
            {
                "run_id": config.run_id,
                "workspace_id": config.workspace_id,
                "intent": config.intent,
                "generation_mode": config.generation_mode,
                "model_profile": config.model_profile,
                "target_role_scope": config.target_role_scope,
                "max_tool_concurrency": config.max_tool_concurrency,
                "retry_policy": {
                    "max_attempts": config.retry_policy.max_attempts,
                    "base_delay_ms": config.retry_policy.base_delay_ms,
                    "max_delay_ms": config.retry_policy.max_delay_ms,
                },
                "timeout_profile": config.timeout_profile.__dict__,
            },
        )
        self.service._append_trace(
            workspace_id,
            "query_runtime_started",
            "Generation query runtime started with an immutable config snapshot and mutable turn state.",
            {
                "run_id": config.run_id,
                "intent": config.intent,
                "generation_mode": config.generation_mode,
                "model_profile": config.model_profile,
                "target_role_scope": config.target_role_scope,
            },
        )
        config_token = ACTIVE_GENERATION_QUERY_CONFIG.set(config)
        turn_token = ACTIVE_GENERATION_TURN_STATE.set(turn_state)
        try:
            result = self.service.generation_entry.generate_with_agent_loop(**kwargs)
            self._merge_turn_state_metrics(result, turn_state)
            return result
        finally:
            ACTIVE_GENERATION_TURN_STATE.reset(turn_token)
            ACTIVE_GENERATION_QUERY_CONFIG.reset(config_token)

    def resume_query(self, *, workspace_id: str, job: "JobRecord", request: Any, kwargs: dict[str, Any]) -> "JobRecord":
        config, turn_state = self._capture_config(
            workspace_id=workspace_id,
            run_id=str(kwargs.get("draft_run_id") or job.linked_run_id or job.job_id),
            prompt=str(kwargs.get("effective_prompt") or request.prompt),
            intent=str(request.intent),
            model_profile=str(job.model_profile or ""),
            generation_mode=kwargs.get("generation_mode"),
            target_role_scope=list((kwargs.get("plan_result") or {}).get("page_graph", {}).get("role_scope") or []),
            preview_profile=job.preview_profile,
        )
        self.service._append_trace(
            workspace_id,
            "query_runtime_resumed",
            "Generation query runtime resumed from an existing grounded plan.",
            {
                "run_id": config.run_id,
                "intent": config.intent,
                "generation_mode": config.generation_mode,
                "model_profile": config.model_profile,
            },
        )
        config_token = ACTIVE_GENERATION_QUERY_CONFIG.set(config)
        turn_token = ACTIVE_GENERATION_TURN_STATE.set(turn_state)
        try:
            result = self.service.generation_entry.continue_generation_from_plan(**kwargs)
            self._merge_turn_state_metrics(result, turn_state)
            return result
        finally:
            ACTIVE_GENERATION_TURN_STATE.reset(turn_token)
            ACTIVE_GENERATION_QUERY_CONFIG.reset(config_token)

    @staticmethod
    def _merge_turn_state_metrics(job: "JobRecord", turn_state: GenerationTurnState) -> None:
        for metric_key, metric_value in turn_state.latency_breakdown().items():
            if metric_value > 0:
                job.latency_breakdown[metric_key] = int(metric_value)
