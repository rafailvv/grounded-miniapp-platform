from __future__ import annotations

import time
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import JobRecord


COMPLETION_BUDGETS = {
    GenerationMode.FAST: {
        "time_limit_ms": 18 * 60 * 1000,
        "token_limit": 600_000,
        "turn_budget_cap": 180,
    },
    GenerationMode.BALANCED: {
        "time_limit_ms": 25 * 60 * 1000,
        "token_limit": 1_200_000,
        "turn_budget_cap": 300,
    },
    GenerationMode.QUALITY: {
        "time_limit_ms": 45 * 60 * 1000,
        "token_limit": 2_500_000,
        "turn_budget_cap": 500,
    },
    GenerationMode.BASIC: {
        "time_limit_ms": 18 * 60 * 1000,
        "token_limit": 600_000,
        "turn_budget_cap": 180,
    },
}


def generation_mode(value: GenerationMode | str | None) -> GenerationMode:
    if isinstance(value, GenerationMode):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return GenerationMode(value.strip())
        except Exception:
            pass
    return GenerationMode.BALANCED


def completion_budget_for_mode(mode_value: GenerationMode | str | None) -> dict[str, Any]:
    mode = generation_mode(mode_value)
    budget = dict(COMPLETION_BUDGETS.get(mode) or COMPLETION_BUDGETS[GenerationMode.BALANCED])
    budget["mode"] = mode.value
    budget["policy"] = "time_or_token_budget"
    return budget


def token_usage_total(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def completion_budget_status(
    *,
    job: JobRecord,
    mode: GenerationMode,
    started_at: float,
    attempt: int,
) -> dict[str, Any]:
    budget = dict(job.completion_budget or completion_budget_for_mode(mode))
    elapsed_ms = int(max(0.0, time.perf_counter() - started_at) * 1000)
    token_limit = int(budget.get("token_limit") or 0)
    time_limit_ms = int(budget.get("time_limit_ms") or 0)
    total_tokens = token_usage_total(job.token_usage)
    reason: str | None = None
    if token_limit > 0 and total_tokens >= token_limit:
        reason = "token_budget_exhausted"
    elif time_limit_ms > 0 and elapsed_ms >= time_limit_ms:
        reason = "time_budget_exhausted"
    status = {
        "mode": mode.value,
        "attempt": int(attempt or 0),
        "elapsed_ms": elapsed_ms,
        "time_limit_ms": time_limit_ms,
        "total_tokens": total_tokens,
        "token_limit": token_limit,
        "exhausted": reason is not None,
        "reason": reason,
        "failure_class": "generation.budget_exhausted" if reason else None,
        "failure_signature": f"generation.budget_exhausted:{reason}" if reason else None,
        "current_phase": "blocked_budget_exhausted" if reason else "agent_loop",
    }
    job.budget_status = status
    return status
