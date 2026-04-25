from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

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

    def _run_with_config(
        self,
        *,
        workspace_id: str,
        config: GenerationQueryConfig,
        turn_state: GenerationTurnState,
        lifecycle_event: str,
        lifecycle_message: str,
        lifecycle_payload: dict[str, Any],
        executor: Callable[[], "JobRecord"],
    ) -> "JobRecord":
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
            lifecycle_event,
            lifecycle_message,
            lifecycle_payload,
        )
        config_token = ACTIVE_GENERATION_QUERY_CONFIG.set(config)
        turn_token = ACTIVE_GENERATION_TURN_STATE.set(turn_state)
        try:
            result = executor()
            self._merge_turn_state_metrics(result, turn_state)
            return result
        finally:
            ACTIVE_GENERATION_TURN_STATE.reset(turn_token)
            ACTIVE_GENERATION_QUERY_CONFIG.reset(config_token)

    def run_query(
        self,
        *,
        workspace_id: str,
        job: "JobRecord",
        request: Any,
        kwargs: dict[str, Any],
        resume_bundle: dict[str, Any] | None = None,
    ) -> "JobRecord":
        config, turn_state = self._capture_config(
            workspace_id=workspace_id,
            run_id=str(kwargs.get("draft_run_id") or job.linked_run_id or job.job_id),
            prompt=str(kwargs.get("effective_prompt") or request.prompt),
            intent=str(request.intent),
            model_profile=str(job.model_profile or ""),
            generation_mode=kwargs.get("generation_mode"),
            target_role_scope=list((resume_bundle or {}).get("role_scope") or kwargs.get("role_scope") or []),
            preview_profile=kwargs.get("preview_profile") or job.preview_profile,
        )
        if resume_bundle is not None:
            prepared = {
                **kwargs,
                "grounded_spec": resume_bundle["grounded_spec"],
                "role_scope": list(resume_bundle.get("role_scope") or kwargs.get("role_scope") or []),
                "role_contract": resume_bundle["role_contract"],
                "plan_result": resume_bundle["plan_result"],
            }
            return self._run_with_config(
                workspace_id=workspace_id,
                config=config,
                turn_state=turn_state,
                lifecycle_event="query_runtime_resumed",
                lifecycle_message="Generation query runtime resumed from an existing grounded plan.",
                lifecycle_payload={
                    "run_id": config.run_id,
                    "intent": config.intent,
                    "generation_mode": config.generation_mode,
                    "model_profile": config.model_profile,
                },
                executor=lambda: self.service.generation_entry.continue_generation_from_plan(**prepared),
            )

        return self._run_with_config(
            workspace_id=workspace_id,
            config=config,
            turn_state=turn_state,
            lifecycle_event="query_runtime_started",
            lifecycle_message="Generation query runtime started with an immutable config snapshot and mutable turn state.",
            lifecycle_payload={
                "run_id": config.run_id,
                "intent": config.intent,
                "generation_mode": config.generation_mode,
                "model_profile": config.model_profile,
                "target_role_scope": config.target_role_scope,
            },
            executor=lambda: self._run_new_query_from_surface(kwargs),
        )

    def _run_new_query_from_surface(self, kwargs: dict[str, Any]) -> "JobRecord":
        prepared = self.service.generation_entry.prepare_generation_surface(**kwargs)
        if not isinstance(prepared, dict):
            return prepared
        return self.service.generation_entry.continue_generation_from_plan(**prepared)

    @staticmethod
    def _merge_turn_state_metrics(job: "JobRecord", turn_state: GenerationTurnState) -> None:
        for metric_key, metric_value in turn_state.latency_breakdown().items():
            if metric_value > 0:
                job.latency_breakdown[metric_key] = int(metric_value)
