from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class ContextPressureThresholds:
    context_window_tokens: int = 128_000
    near_capacity_ratio: float = 0.80
    large_tool_result_tokens: int = 10_000
    large_tool_result_ratio: float = 0.15
    large_read_tokens: int = 10_000
    large_read_ratio: float = 0.05
    large_memory_tokens: int = 5_000
    large_memory_ratio: float = 0.05


class AgentContextPressureAnalyzer:
    """Estimate prompt pressure and recommend compaction before wasteful turns."""

    def __init__(self, thresholds: ContextPressureThresholds | None = None) -> None:
        self.thresholds = thresholds or ContextPressureThresholds()

    def analyze_payload(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"raw_prompt": payload}
        else:
            parsed = payload
        sections = {
            "file_contexts": self._tokens(parsed.get("file_contexts")),
            "tool_results": self._tokens(parsed.get("tool_results")),
            "agent_memory": self._tokens(parsed.get("agent_memory")),
            "latest_checks": self._tokens(parsed.get("latest_checks")),
            "preview": self._tokens(parsed.get("preview")),
            "implementation_plan": self._tokens(parsed.get("implementation_plan")),
            "acceptance_contract": self._tokens(parsed.get("acceptance_contract")),
            "full_payload": self._tokens(parsed),
        }
        total = sections["full_payload"]
        window = self.thresholds.context_window_tokens
        ratio = total / window if window else 0
        suggestions: list[dict[str, Any]] = []

        def add(kind: str, message: str, section: str, tokens: int) -> None:
            suggestions.append({"kind": kind, "message": message, "section": section, "tokens": tokens})

        if ratio >= self.thresholds.near_capacity_ratio:
            add("compact_next_turn", "Prompt payload is near context capacity; next turn should use compact repair context.", "full_payload", total)
        if sections["tool_results"] >= max(self.thresholds.large_tool_result_tokens, int(window * self.thresholds.large_tool_result_ratio)):
            add("spill_tool_results", "Tool results are large; persist full outputs and pass refs/excerpts only.", "tool_results", sections["tool_results"])
        if sections["file_contexts"] >= max(self.thresholds.large_read_tokens, int(window * self.thresholds.large_read_ratio)):
            add("narrow_file_context", "Read context is large; next turn should include only failing files and current diff summary.", "file_contexts", sections["file_contexts"])
        if sections["agent_memory"] >= max(self.thresholds.large_memory_tokens, int(window * self.thresholds.large_memory_ratio)):
            add("compact_memory", "Agent memory is large; compact to failed signatures and next action.", "agent_memory", sections["agent_memory"])

        return {
            "total_tokens_estimate": total,
            "context_window_tokens": window,
            "pressure_ratio": round(ratio, 4),
            "sections": sections,
            "suggestions": suggestions,
            "compact_recommended": bool(suggestions),
        }

    @staticmethod
    def _tokens(value: Any) -> int:
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = str(value)
        return max(1, len(serialized) // 4) if serialized else 0
