from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from app.models.context_pressure import (
    CompactBoundaryWarning,
    ContextPhaseBudget,
    ContextPressureRecommendation,
    ContextPressureSection,
    ContextPressureSnapshot,
    FileReadHint,
    MicrocompactCandidate,
    StalePathReference,
)


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
        section_values = {
            "files": parsed.get("file_contexts"),
            "tool_outputs": parsed.get("tool_results"),
            "memory": {
                "agent_memory": parsed.get("agent_memory"),
                "hook_contexts": parsed.get("hook_contexts"),
                "repair_packets": parsed.get("repair_packets"),
            },
            "diff": {
                "latest_diff_summary": parsed.get("latest_diff_summary"),
                "latest_turn_diff": parsed.get("latest_turn_diff"),
                "last_turn_summary": parsed.get("last_turn_summary"),
            },
            "skills": {
                "tool_registry": parsed.get("tool_registry"),
                "context_pack_retrieval": ((parsed.get("context_pack") or {}).get("retrieval_stats") if isinstance(parsed.get("context_pack"), dict) else {}),
            },
            "checks": {
                "latest_checks": parsed.get("latest_checks"),
                "diagnostics_delta": parsed.get("diagnostics_delta"),
                "preview": parsed.get("preview"),
            },
            "prompt_contract": {
                "implementation_plan": parsed.get("implementation_plan"),
                "acceptance_contract": parsed.get("acceptance_contract"),
                "product_execution_contract": parsed.get("product_execution_contract"),
                "orchestration": parsed.get("orchestration"),
            },
            "full_payload": parsed,
        }
        section_tokens = {key: self._tokens(value) for key, value in section_values.items()}
        # Compatibility aliases for existing callers and old reports.
        section_tokens.update(
            {
                "file_contexts": section_tokens["files"],
                "tool_results": section_tokens["tool_outputs"],
                "agent_memory": section_tokens["memory"],
                "latest_checks": self._tokens(parsed.get("latest_checks")),
                "preview": self._tokens(parsed.get("preview")),
                "implementation_plan": self._tokens(parsed.get("implementation_plan")),
                "acceptance_contract": self._tokens(parsed.get("acceptance_contract")),
            }
        )
        total = section_tokens["full_payload"]
        window = self.thresholds.context_window_tokens
        ratio = total / window if window else 0
        suggestions: list[dict[str, Any]] = []
        recommendations: list[ContextPressureRecommendation] = []
        microcompact_candidates = self._microcompact_candidates(parsed.get("tool_results"))
        sections = {
            key: ContextPressureSection(
                key=key,
                label=self._section_label(key),
                tokens=tokens,
                ratio=round(tokens / window, 4) if window else 0.0,
                budget_tokens=self._section_budget(key, window),
                top_contributors=self._top_contributors(section_values.get(key), section=key),
            )
            for key, tokens in section_tokens.items()
            if key in section_values
        }

        def add(kind: str, message: str, section: str, tokens: int, *, code: str, severity: str = "warning", **extra: Any) -> None:
            suggestion = {"kind": kind, "message": message, "section": section, "tokens": tokens, "code": code, **extra}
            suggestions.append(suggestion)
            recommendations.append(
                ContextPressureRecommendation(
                    code=code,
                    message=message,
                    section=section,
                    severity=severity,
                    tokens=tokens,
                    action=str(extra.get("action") or self._action_for_code(code)),
                    artifact_ref=extra.get("artifact_ref"),
                    microcompact_ref=extra.get("microcompact_ref"),
                    paths=[str(path) for path in extra.get("paths") or [] if str(path).strip()],
                    metadata={key: value for key, value in extra.items() if key not in {"artifact_ref", "microcompact_ref", "paths", "action"}},
                )
            )

        if ratio >= self.thresholds.near_capacity_ratio:
            add(
                "compact_next_turn",
                "Context is close to the compact boundary; continue from the compact summary and refs.",
                "full_payload",
                total,
                code="compact_boundary_near",
                action="compact_next_turn",
                severity="critical" if ratio >= 0.9 else "warning",
            )
        if section_tokens["tool_outputs"] >= max(self.thresholds.large_tool_result_tokens, int(window * self.thresholds.large_tool_result_ratio)):
            ref = microcompact_candidates[0].microcompact_ref if microcompact_candidates else None
            add(
                "spill_tool_results",
                "Tool outputs are large; use artifact or microcompact refs instead of raw stdout/stderr.",
                "tool_outputs",
                section_tokens["tool_outputs"],
                code="use_artifact_ref",
                action="use_artifact_ref",
                microcompact_ref=ref,
            )
        if section_tokens["files"] >= max(self.thresholds.large_read_tokens, int(window * self.thresholds.large_read_ratio)):
            add(
                "narrow_file_context",
                "File context is large; avoid broad re-reads and keep only failing files plus current diff.",
                "files",
                section_tokens["files"],
                code="avoid_broad_file_reads",
                action="avoid_reread_files",
                paths=[item.get("label") for item in sections["files"].top_contributors if item.get("label")],
            )
        if section_tokens["memory"] >= max(self.thresholds.large_memory_tokens, int(window * self.thresholds.large_memory_ratio)):
            add(
                "compact_memory",
                "Memory context is large; keep top relevant memories, failed signatures, and next action only.",
                "memory",
                section_tokens["memory"],
                code="compact_memory_context",
                action="compact_memory",
            )

        compact_boundary = CompactBoundaryWarning(
            recommended=bool(any(item.code == "compact_boundary_near" for item in recommendations)),
            pressure_ratio=round(ratio, 4),
            threshold=self.thresholds.near_capacity_ratio,
            message=(
                "Context is close to the compact boundary."
                if ratio >= self.thresholds.near_capacity_ratio
                else "Context is below compact boundary."
            ),
            reason="pressure_ratio" if ratio >= self.thresholds.near_capacity_ratio else None,
        )
        snapshot = ContextPressureSnapshot(
            total_tokens_estimate=total,
            context_window_tokens=window,
            pressure_ratio=round(ratio, 4),
            sections=sections,
            section_tokens=section_tokens,
            recommendations=recommendations,
            suggestions=suggestions,
            microcompact_candidates=microcompact_candidates,
            compact_boundary=compact_boundary,
            compact_recommended=bool(recommendations),
            stale_path_refs=self._stale_path_refs_from_payload(parsed),
            phase_budgets=self._phase_budgets_from_payload(parsed),
            token_cost_budget=self._token_cost_budget_from_payload(parsed),
        )
        return snapshot.model_dump(mode="json", by_alias=True)

    def analyze_transcript(
        self,
        transcript: dict[str, Any] | None,
        *,
        current_file_contexts: dict[str, Any] | None = None,
        path_exists: Callable[[str], bool | None] | None = None,
        find_similar_path: Callable[[str], str | None] | None = None,
    ) -> dict[str, Any]:
        """Detect Claude-style duplicate file read pressure from tool trace.

        The model often loops by re-reading the same large files after every
        failed repair. This summary is intentionally product-neutral: it does not know
        anything about product categories, only tool calls and file paths.
        """
        events = transcript.get("events") if isinstance(transcript, dict) else []
        if not isinstance(events, list):
            events = []
        read_counts: dict[str, int] = {}
        read_sequences: dict[str, int] = {}
        seen_tool_ids: set[str] = set()

        def record_read(tool: str, targets: Any, tool_use_id: str = "", sequence: int = 0) -> None:
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
                read_sequences[path] = max(read_sequences.get(path, 0), int(sequence or 0))

        for event in events:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = str(event.get("event_type") or "")
            sequence = int(event.get("sequence") or 0)
            if event_type == "model_turn":
                for call in payload.get("tool_calls") or []:
                    if isinstance(call, dict):
                        record_read(
                            str(call.get("tool") or ""),
                            call.get("targets"),
                            str(call.get("tool_use_id") or ""),
                            sequence,
                        )
            elif event_type == "tool_call":
                arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                record_read(
                    str(payload.get("tool") or arguments.get("tool") or ""),
                    arguments.get("targets") or payload.get("targets"),
                    str(payload.get("tool_use_id") or ""),
                    sequence,
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
        avoid_reread_files: list[FileReadHint] = []
        if duplicates and (
            duplicate_token_estimate >= self.thresholds.duplicate_read_tokens
            or any(item["read_count"] > self.thresholds.duplicate_read_count for item in duplicates)
        ):
            avoid_reread_files = [
                FileReadHint(
                    path=str(item["path"]),
                    read_count=int(item["read_count"]),
                    duplicate_token_estimate=int(item["duplicate_token_estimate"]),
                )
                for item in duplicates[:8]
            ]
            suggestions.append(
                {
                    "kind": "avoid_duplicate_reads",
                    "code": "avoid_reread_files",
                    "message": "Use cached file context/current diff for repeatedly read files; re-read only after a mutation or when a specific line range is needed.",
                    "section": "transcript",
                    "tokens": duplicate_token_estimate,
                    "paths": [item["path"] for item in duplicates[:8]],
                }
            )
        stale_path_refs: list[StalePathReference] = []
        if path_exists is not None:
            for path, count in sorted(read_counts.items(), key=lambda item: (-item[1], item[0])):
                try:
                    exists = path_exists(path)
                except Exception:
                    exists = None
                if exists is not False:
                    continue
                suggestion = None
                if find_similar_path is not None:
                    try:
                        suggestion = find_similar_path(path)
                    except Exception:
                        suggestion = None
                stale_path_refs.append(
                    StalePathReference(
                        path=path,
                        source="transcript.read_files",
                        reason="missing_in_workspace",
                        read_count=int(count),
                        last_sequence=read_sequences.get(path) or None,
                        suggested_path=suggestion,
                    )
                )
            if stale_path_refs:
                suggestions.append(
                    {
                        "kind": "stale_path_refs",
                        "code": "refresh_stale_path_refs",
                        "message": "Some referenced files no longer exist in the active draft; resolve the current path before reading or patching.",
                        "section": "transcript",
                        "tokens": 0,
                        "paths": [item.path for item in stale_path_refs[:8]],
                    }
                )
        return {
            "duplicate_file_reads": duplicates[:20],
            "duplicate_read_token_estimate": duplicate_token_estimate,
            "avoid_reread_files": [item.model_dump(mode="json") for item in avoid_reread_files],
            "stale_path_refs": [item.model_dump(mode="json") for item in stale_path_refs[:20]],
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

    def _top_contributors(self, value: Any, *, section: str, limit: int = 6) -> list[dict[str, Any]]:
        contributors: list[dict[str, Any]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                contributors.append({"label": str(key), "tokens": self._tokens(item), "section": section})
        elif isinstance(value, list):
            for index, item in enumerate(value):
                contributors.append({"label": self._item_label(item, index=index), "tokens": self._tokens(item), "section": section})
        elif value:
            contributors.append({"label": section, "tokens": self._tokens(value), "section": section})
        contributors.sort(key=lambda item: (-int(item.get("tokens") or 0), str(item.get("label") or "")))
        return contributors[:limit]

    def _microcompact_candidates(self, tool_results: Any) -> list[MicrocompactCandidate]:
        if not isinstance(tool_results, list):
            return []
        candidates: list[MicrocompactCandidate] = []
        for item in tool_results:
            if not isinstance(item, dict):
                continue
            original_chars = int(item.get("original_chars") or item.get("total_chars") or self._serialized_len(item))
            microcompact_ref = str(item.get("microcompact_ref") or item.get("persisted_output_ref") or "") or None
            if not microcompact_ref and original_chars < self.thresholds.large_tool_result_tokens * 4:
                continue
            candidates.append(
                MicrocompactCandidate(
                    tool=str(item.get("tool") or item.get("name") or item.get("type") or "") or None,
                    status=str(item.get("status") or item.get("outcome") or "") or None,
                    original_chars=original_chars,
                    tokens_estimate=max(1, original_chars // 4),
                    microcompact_ref=microcompact_ref if microcompact_ref and "microcompact:" in microcompact_ref else None,
                    artifact_ref=microcompact_ref,
                    digest=str(item.get("digest") or "") or None,
                    reason="existing_microcompact_ref" if microcompact_ref else "large_tool_output",
                )
            )
        return candidates[:20]

    def _stale_path_refs_from_payload(self, parsed: dict[str, Any]) -> list[StalePathReference]:
        raw = parsed.get("stale_path_refs")
        if not isinstance(raw, list):
            raw = ((parsed.get("context_pressure") or {}).get("stale_path_refs") if isinstance(parsed.get("context_pressure"), dict) else [])
        refs: list[StalePathReference] = []
        for item in (raw if isinstance(raw, list) else []):
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                continue
            try:
                refs.append(StalePathReference.model_validate(item))
            except Exception:
                refs.append(
                    StalePathReference(
                        path=str(item.get("path") or ""),
                        source=str(item.get("source") or "payload"),
                        reason=str(item.get("reason") or "stale_reference"),
                        suggested_path=str(item.get("suggested_path") or "") or None,
                    )
                )
        return refs[:20]

    def _phase_budgets_from_payload(self, parsed: dict[str, Any]) -> list[ContextPhaseBudget]:
        raw = parsed.get("phase_budgets") or parsed.get("phase_budget")
        if isinstance(raw, dict):
            raw = raw.get("phases")
        if not isinstance(raw, list):
            return []
        budgets: list[ContextPhaseBudget] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            phase = str(item.get("phase") or item.get("id") or "").strip()
            if not phase:
                continue
            try:
                budgets.append(ContextPhaseBudget.model_validate({**item, "phase": phase}))
            except Exception:
                budgets.append(
                    ContextPhaseBudget(
                        phase=phase,
                        status=str(item.get("status") or "pending"),
                        token_budget=self._safe_int(item.get("token_budget")),
                        tokens_used=self._safe_int(item.get("tokens_used") or item.get("tokens_used_estimate")),
                        token_ratio=self._safe_float(item.get("token_ratio")),
                        cost_budget_usd=self._safe_float(item.get("cost_budget_usd")),
                        estimated_cost_usd=self._safe_float(item.get("estimated_cost_usd")),
                    )
                )
        return budgets[:20]

    def _token_cost_budget_from_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        for key in ("token_cost_budget", "phase_budget"):
            value = parsed.get(key)
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _section_label(key: str) -> str:
        return {
            "files": "Files",
            "tool_outputs": "Tool outputs",
            "memory": "Memory",
            "diff": "Diff",
            "skills": "Skills",
            "checks": "Checks",
            "prompt_contract": "Prompt contract",
            "full_payload": "Full payload",
        }.get(key, key.replace("_", " ").title())

    def _section_budget(self, key: str, window: int) -> int | None:
        if key == "tool_outputs":
            return max(self.thresholds.large_tool_result_tokens, int(window * self.thresholds.large_tool_result_ratio))
        if key == "files":
            return max(self.thresholds.large_read_tokens, int(window * self.thresholds.large_read_ratio))
        if key == "memory":
            return max(self.thresholds.large_memory_tokens, int(window * self.thresholds.large_memory_ratio))
        if key == "full_payload":
            return int(window * self.thresholds.near_capacity_ratio)
        return None

    @staticmethod
    def _action_for_code(code: str) -> str:
        return {
            "compact_boundary_near": "compact_next_turn",
            "use_artifact_ref": "use_artifact_ref",
            "avoid_broad_file_reads": "avoid_reread_files",
            "compact_memory_context": "compact_memory",
            "avoid_reread_files": "avoid_reread_files",
        }.get(code, "review_context")

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _item_label(item: Any, *, index: int) -> str:
        if isinstance(item, dict):
            return str(item.get("path") or item.get("tool") or item.get("name") or item.get("microcompact_ref") or f"item_{index}")
        return f"item_{index}"

    @staticmethod
    def _serialized_len(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return len(str(value))
