from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.common import GenerationMode
from app.repositories.state_store import StateStore


class DiminishingReturnsService:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def evaluate(
        self,
        *,
        workspace_id: str,
        run_id: str,
        phase: str,
        generation_mode: GenerationMode | str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(generation_mode)
        payload = self.store.get("reports", f"diminishing_returns:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        phase_items = [item for item in list(payload.get("items") or []) if item.get("run_id") == run_id and item.get("phase") == phase]
        previous = phase_items[-1] if phase_items else None
        thresholds = self._thresholds(mode)
        current_total = int(metrics.get("total_tokens", 0) or 0)
        previous_total = int(previous.get("metrics", {}).get("total_tokens", 0) or 0) if previous else 0
        delta_tokens = max(0, current_total - previous_total)
        diff_chars = int(metrics.get("diff_chars", 0) or 0)
        changed_files = int(metrics.get("changed_files_count", 0) or 0)
        failure_signature = str(metrics.get("failure_signature") or "")
        repeated_signature = bool(previous and failure_signature and previous.get("metrics", {}).get("failure_signature") == failure_signature)
        low_progress = (
            delta_tokens <= thresholds["delta_tokens"]
            and diff_chars <= thresholds["diff_chars"]
            and changed_files <= thresholds["changed_files"]
        )
        consecutive_low = (int(previous.get("consecutive_low_progress", 0) or 0) + 1) if previous and low_progress else (1 if low_progress else 0)
        should_stop = bool(previous and low_progress and (repeated_signature or (consecutive_low >= 2 and changed_files <= 1)))
        decision = {
            "run_id": run_id,
            "phase": phase,
            "generation_mode": mode.value,
            "metrics": {
                **metrics,
                "delta_tokens": delta_tokens,
                "repeated_signature": repeated_signature,
            },
            "consecutive_low_progress": consecutive_low,
            "should_stop": should_stop,
            "reason": (
                "Automatic iteration stopped because consecutive low-signal iterations produced negligible new output."
                if should_stop
                else None
            ),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        items = list(payload.get("items") or [])
        items.append(decision)
        payload["items"] = items[-60:]
        self.store.upsert("reports", f"diminishing_returns:{workspace_id}", payload)
        return decision

    @staticmethod
    def _thresholds(mode: GenerationMode) -> dict[str, int]:
        if mode == GenerationMode.FAST:
            return {"delta_tokens": 250, "diff_chars": 120, "changed_files": 1}
        if mode == GenerationMode.QUALITY:
            return {"delta_tokens": 700, "diff_chars": 220, "changed_files": 1}
        return {"delta_tokens": 450, "diff_chars": 160, "changed_files": 1}
