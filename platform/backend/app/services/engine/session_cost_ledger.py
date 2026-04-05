from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.repositories.state_store import StateStore


class SessionCostLedger:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def record_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        run_mode: str,
        generation_mode: str,
        status: str,
        model_profile: str | None,
        llm_model: str | None,
        cache_stats: dict[str, Any] | None,
        latency_breakdown: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = self.store.get("reports", f"session_costs:{workspace_id}") or {
            "workspace_id": workspace_id,
            "totals": {},
            "runs": [],
            "updated_at": None,
        }
        stats = dict(cache_stats or {})
        latency = dict(latency_breakdown or {})
        run_entry = {
            "run_id": run_id,
            "run_mode": run_mode,
            "generation_mode": generation_mode,
            "status": status,
            "model_profile": model_profile,
            "llm_model": llm_model,
            "llm_requests": int(stats.get("llm_requests", 0) or 0),
            "input_tokens": int(stats.get("input_tokens", 0) or 0),
            "output_tokens": int(stats.get("output_tokens", 0) or 0),
            "total_tokens": int(stats.get("total_tokens", 0) or 0),
            "reasoning_tokens": int(stats.get("reasoning_tokens", 0) or 0),
            "cached_tokens": int(stats.get("cached_tokens", 0) or 0),
            "cache_write_tokens": int(stats.get("cache_write_tokens", 0) or 0),
            "estimated_cost_usd": float(stats.get("estimated_cost_usd", 0.0) or 0.0),
            "wall_time_ms": int(latency.get("total_ms", latency.get("fix_total_ms", 0)) or 0),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        runs = [item for item in list(payload.get("runs") or []) if item.get("run_id") != run_id]
        runs.append(run_entry)
        runs = runs[-25:]
        totals = {
            "run_count": len(runs),
            "llm_requests": sum(int(item.get("llm_requests", 0) or 0) for item in runs),
            "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in runs),
            "output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in runs),
            "total_tokens": sum(int(item.get("total_tokens", 0) or 0) for item in runs),
            "reasoning_tokens": sum(int(item.get("reasoning_tokens", 0) or 0) for item in runs),
            "cached_tokens": sum(int(item.get("cached_tokens", 0) or 0) for item in runs),
            "cache_write_tokens": sum(int(item.get("cache_write_tokens", 0) or 0) for item in runs),
            "estimated_cost_usd": round(sum(float(item.get("estimated_cost_usd", 0.0) or 0.0) for item in runs), 6),
            "wall_time_ms": sum(int(item.get("wall_time_ms", 0) or 0) for item in runs),
        }
        totals["cache_hit_ratio"] = round(
            totals["cached_tokens"] / max(1, totals["cached_tokens"] + totals["input_tokens"]),
            4,
        )
        result = {
            "workspace_id": workspace_id,
            "totals": totals,
            "runs": runs,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"session_costs:{workspace_id}", result)
        return result
