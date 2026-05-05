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
    duplicate_read_count: int = 2
    duplicate_read_tokens: int = 4_000


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

    def analyze_transcript(
        self,
        transcript: dict[str, Any] | None,
        *,
        current_file_contexts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Detect Claude-style duplicate file read pressure from tool trace.

        The model often loops by re-reading the same large files after every
        failed repair. This summary is intentionally generic: it does not know
        anything about product categories, only tool calls and file paths.
        """
        events = transcript.get("events") if isinstance(transcript, dict) else []
        if not isinstance(events, list):
            events = []
        read_counts: dict[str, int] = {}
        seen_tool_ids: set[str] = set()

        def record_read(tool: str, targets: Any, tool_use_id: str = "") -> None:
            if tool_use_id and tool_use_id in seen_tool_ids:
                return
            if tool_use_id:
                seen_tool_ids.add(tool_use_id)
            if str(tool or "") != "read_files":
                return
            if not isinstance(targets, list):
                return
            for target in targets:
                path = str(target or "").strip().replace("\\", "/")
                if not path:
                    continue
                read_counts[path] = read_counts.get(path, 0) + 1

        for event in events:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = str(event.get("event_type") or "")
            if event_type == "model_turn":
                for call in payload.get("tool_calls") or []:
                    if isinstance(call, dict):
                        record_read(
                            str(call.get("tool") or ""),
                            call.get("targets"),
                            str(call.get("tool_use_id") or ""),
                        )
            elif event_type == "tool_call":
                arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                record_read(
                    str(payload.get("tool") or arguments.get("tool") or ""),
                    arguments.get("targets") or payload.get("targets"),
                    str(payload.get("tool_use_id") or ""),
                )

        current_file_contexts = current_file_contexts or {}
        file_tokens = {
            str(path): self._tokens(content)
            for path, content in current_file_contexts.items()
            if isinstance(path, str)
        }
        duplicates: list[dict[str, Any]] = []
        duplicate_token_estimate = 0
        for path, count in sorted(read_counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.thresholds.duplicate_read_count:
                continue
            tokens_per_read = int(file_tokens.get(path) or 0)
            wasted = tokens_per_read * max(0, count - 1)
            duplicate_token_estimate += wasted
            duplicates.append(
                {
                    "path": path,
                    "read_count": count,
                    "duplicate_token_estimate": wasted,
                }
            )

        suggestions: list[dict[str, Any]] = []
        if duplicates and (
            duplicate_token_estimate >= self.thresholds.duplicate_read_tokens
            or any(item["read_count"] > self.thresholds.duplicate_read_count for item in duplicates)
        ):
            suggestions.append(
                {
                    "kind": "avoid_duplicate_reads",
                    "message": "Use cached file context/current diff for repeatedly read files; re-read only after a mutation or when a specific line range is needed.",
                    "section": "transcript",
                    "tokens": duplicate_token_estimate,
                    "paths": [item["path"] for item in duplicates[:8]],
                }
            )
        return {
            "duplicate_file_reads": duplicates[:20],
            "duplicate_read_token_estimate": duplicate_token_estimate,
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
