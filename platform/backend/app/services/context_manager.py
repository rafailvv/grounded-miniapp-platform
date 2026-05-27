from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from app.models.common import GenerationMode
from app.models.context_manager import (
    ContextFragmentDecision,
    ContextManagerReport,
    ContextManifest,
    ContextStaleRef,
)
from app.repositories.state_store import StateStore
from app.services.engine.context_budget_manager import ContextBudgetManager
from app.services.event_journal import EventJournalService


CONTEXT_MANAGER_SCHEMA = "grounded.context_manager.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _estimate_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return max(1, (len(text) + 3) // 4) if text else 0


def _truncate_text(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    half = max(1, max_chars // 2)
    return f"{value[:half]}\n...[context_manager omitted {len(value) - max_chars} chars]...\n{value[-half:]}"


class ContextManagerService:
    """Central context budget policy and manifest builder for model turns."""

    def __init__(
        self,
        store: StateStore,
        *,
        budget_manager: ContextBudgetManager | None = None,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.store = store
        self.budget_manager = budget_manager or ContextBudgetManager()
        self.event_journal_service = event_journal_service

    def prepare_turn_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        session_id: str | None = None,
        prompt: str = "",
        generation_mode: GenerationMode | str = GenerationMode.BALANCED,
        run_mode: str = "generate",
        prompt_payload: dict[str, Any] | str | None = None,
        transcript_snapshot: dict[str, Any] | None = None,
        context_pressure: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | dict[str, Any] | None = None,
        proofs: list[dict[str, Any]] | dict[str, Any] | None = None,
        bookmarks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(prompt_payload)
        pressure = context_pressure if isinstance(context_pressure, dict) else {}
        transcript = transcript_snapshot if isinstance(transcript_snapshot, dict) else {}
        policy = self.budget_manager.build_policy(
            generation_mode=generation_mode,
            run_mode=run_mode,
            target_file_count=self._target_file_count(payload),
        )
        decisions: list[ContextFragmentDecision] = []
        budgeted_payload = self._apply_policy(payload, policy=policy, decisions=decisions)
        history_normalization = transcript.get("normalization") if isinstance(transcript.get("normalization"), dict) else {}
        if history_normalization and history_normalization.get("status") != "ok":
            decisions.append(
                ContextFragmentDecision(
                    fragment_id="transcript:normalization",
                    section="transcript",
                    action="defer",
                    reason=str(history_normalization.get("status") or "history_normalized"),
                    priority=95,
                    estimated_tokens=_estimate_tokens(history_normalization),
                    budget_tokens=policy.sections["transcript"].budget_tokens,
                    source="agent_transcript",
                    metadata={"history_normalization": history_normalization},
                )
            )
        stale_refs = self._stale_refs(pressure)
        for stale_ref in stale_refs:
            decisions.append(
                ContextFragmentDecision(
                    fragment_id=f"stale:{stale_ref.path}",
                    section="transcript",
                    action="refresh",
                    reason=stale_ref.reason,
                    priority=96,
                    source=stale_ref.source,
                    metadata=stale_ref.model_dump(mode="json"),
                )
            )
        pressure_latest = pressure.get("latest") if isinstance(pressure.get("latest"), dict) else pressure
        compact_recommended = bool(
            pressure_latest.get("compact_recommended")
            or ((pressure_latest.get("compact_boundary") or {}).get("recommended") if isinstance(pressure_latest.get("compact_boundary"), dict) else False)
        )
        if compact_recommended:
            decisions.append(
                ContextFragmentDecision(
                    fragment_id="context:compact_boundary",
                    section="transcript",
                    action="summarize",
                    reason="auto_compact_recommended",
                    priority=98,
                    estimated_tokens=int(pressure_latest.get("total_tokens_estimate") or 0),
                    budget_tokens=policy.target_prompt_tokens,
                    source="context_pressure",
                    ref=self._pressure_ref(workspace_id, run_id),
                )
            )
        included_refs = self._refs_from_decisions(decisions, include=True)
        dropped_refs = self._refs_from_decisions(decisions, include=False)
        manifest_id = self._manifest_id(workspace_id=workspace_id, run_id=run_id, payload=budgeted_payload, decisions=decisions)
        manifest = ContextManifest(
            manifest_id=manifest_id,
            workspace_id=workspace_id,
            run_id=run_id,
            session_id=session_id,
            total_tokens_estimate=_estimate_tokens(payload),
            included_tokens_estimate=_estimate_tokens(budgeted_payload),
            target_prompt_tokens=policy.target_prompt_tokens,
            included_sections=self._sections_by_action(decisions, "include"),
            summarized_sections=self._sections_by_action(decisions, "summarize"),
            ref_sections=[*self._sections_by_action(decisions, "artifact_ref"), *self._sections_by_action(decisions, "microcompact")],
            dropped_sections=[*self._sections_by_action(decisions, "discard"), *self._sections_by_action(decisions, "defer")],
            included_refs=included_refs,
            dropped_refs=dropped_refs,
            decisions=decisions,
            metadata={"prompt_tokens_estimate": _estimate_tokens(prompt), "compact_recommended": compact_recommended},
        )
        report = self._append_report(
            workspace_id=workspace_id,
            run_id=run_id,
            session_id=session_id,
            policy=policy,
            manifest=manifest,
            decisions=decisions,
            pressure=pressure,
            artifacts=self._items(artifacts),
            proofs=self._items(proofs),
            bookmarks=bookmarks or [],
            included_refs=included_refs,
            dropped_refs=dropped_refs,
            stale_refs=stale_refs,
            history_normalization=history_normalization,
        )
        budgeted_payload["context_manifest"] = {
            "schema": "grounded.context_manifest.pointer.v1",
            "manifest_id": manifest.manifest_id,
            "context_manager_ref": report.report_ref,
            "target_prompt_tokens": manifest.target_prompt_tokens,
            "included_sections": manifest.included_sections,
            "summarized_sections": manifest.summarized_sections,
            "ref_sections": manifest.ref_sections,
            "dropped_sections": manifest.dropped_sections,
            "decisions": [decision.model_dump(mode="json") for decision in decisions[:30]],
        }
        return {
            "prompt_payload": json.dumps(budgeted_payload, ensure_ascii=False),
            "payload": budgeted_payload,
            "report": report.model_dump(mode="json", by_alias=True),
            "report_ref": report.report_ref,
            "manifest_ref": report.manifest_ref,
        }

    def get_run_report(self, *, workspace_id: str, run_id: str, session_id: str | None = None) -> dict[str, Any]:
        key = self._report_ref(workspace_id, run_id)
        payload = self.store.get("reports", key)
        if isinstance(payload, dict):
            return ContextManagerReport.model_validate(payload).model_dump(mode="json", by_alias=True)
        policy = self.budget_manager.build_policy(generation_mode=GenerationMode.BALANCED)
        manifest = ContextManifest(
            manifest_id=f"context_manifest:{workspace_id}:{run_id}:empty",
            workspace_id=workspace_id,
            run_id=run_id,
            session_id=session_id,
            status="empty",
            target_prompt_tokens=policy.target_prompt_tokens,
        )
        return ContextManagerReport(
            status="empty",
            workspace_id=workspace_id,
            session_id=session_id,
            run_id=run_id,
            report_ref=key,
            manifest_ref=manifest.manifest_id,
            policy=policy,
            manifest=manifest,
        ).model_dump(mode="json", by_alias=True)

    def session_report(self, *, session_id: str, run_reports: list[dict[str, Any]]) -> dict[str, Any]:
        items = [item for item in run_reports if isinstance(item, dict)]
        latest = items[-1] if items else None
        return {
            "schema": "grounded.session_context_manager.v1",
            "status": "ready" if items else "empty",
            "session_id": session_id,
            "workspace_id": (latest or {}).get("workspace_id"),
            "run_id": (latest or {}).get("run_id"),
            "items": items,
            "latest": latest,
            "next_sequence": max([int(item.get("next_sequence") or 0) for item in items] or [0]) + 1,
        }

    def _apply_policy(self, payload: dict[str, Any], *, policy: Any, decisions: list[ContextFragmentDecision]) -> dict[str, Any]:
        result = _jsonable(payload)
        if not isinstance(result, dict):
            result = {"raw_prompt": str(result)}
        for section, keys in self._payload_section_keys().items():
            section_policy = policy.sections.get(section)
            if section_policy is None:
                continue
            for key in keys:
                if key not in result:
                    continue
                value = result.get(key)
                estimated = _estimate_tokens(value)
                action = "include"
                reason = "within_budget"
                replacement = value
                if estimated > section_policy.budget_tokens and not section_policy.always_load:
                    action = section_policy.overflow_action
                    reason = "section_budget_exceeded"
                    replacement = self._compact_value(value, section=section, action=action, budget_tokens=section_policy.budget_tokens)
                elif estimated > section_policy.budget_tokens and section_policy.always_load:
                    action = "summarize"
                    reason = "always_load_summarized"
                    replacement = self._compact_value(value, section=section, action="summarize", budget_tokens=section_policy.budget_tokens)
                result[key] = replacement
                decisions.append(
                    ContextFragmentDecision(
                        fragment_id=f"{section}:{key}",
                        section=section,
                        action=action,  # type: ignore[arg-type]
                        reason=reason,
                        priority=section_policy.priority,
                        estimated_tokens=estimated,
                        budget_tokens=section_policy.budget_tokens,
                        source="prompt_payload",
                        ref=self._value_ref(replacement),
                    )
                )
        return result

    def _compact_value(self, value: Any, *, section: str, action: str, budget_tokens: int) -> Any:
        max_chars = max(400, int(budget_tokens or 256) * 4)
        if action in {"artifact_ref", "microcompact", "defer"}:
            refs = self._extract_refs(value)
            return {
                "schema": "grounded.context_section_ref.v1",
                "section": section,
                "action": action,
                "summary": _truncate_text(json.dumps(value, ensure_ascii=False, default=str), max_chars=min(max_chars, 1600)),
                "refs": refs[:24],
                "original_tokens_estimate": _estimate_tokens(value),
            }
        if isinstance(value, str):
            return _truncate_text(value, max_chars=max_chars)
        if isinstance(value, list):
            kept: list[Any] = []
            used = 0
            for item in value:
                item_tokens = _estimate_tokens(item)
                if used + item_tokens > budget_tokens and kept:
                    break
                kept.append(self._compact_nested(item, max_chars=min(max_chars, 1800)))
                used += item_tokens
            if len(kept) < len(value):
                kept.append({"context_manager_truncated_items": len(value) - len(kept)})
            return kept
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            used = 0
            for key, item in value.items():
                item_tokens = _estimate_tokens(item)
                if used + item_tokens > budget_tokens and compact:
                    compact["context_manager_truncated_keys"] = max(0, len(value) - len(compact))
                    break
                compact[str(key)] = self._compact_nested(item, max_chars=min(max_chars, 2200))
                used += item_tokens
            return compact
        return value

    def _compact_nested(self, value: Any, *, max_chars: int) -> Any:
        if isinstance(value, str):
            return _truncate_text(value, max_chars=max_chars)
        if isinstance(value, list):
            return [self._compact_nested(item, max_chars=max_chars) for item in value[:8]]
        if isinstance(value, dict):
            return {str(key): self._compact_nested(item, max_chars=max_chars) for key, item in list(value.items())[:12]}
        return value

    def _append_report(
        self,
        *,
        workspace_id: str,
        run_id: str,
        session_id: str | None,
        policy: Any,
        manifest: ContextManifest,
        decisions: list[ContextFragmentDecision],
        pressure: dict[str, Any],
        artifacts: list[dict[str, Any]],
        proofs: list[dict[str, Any]],
        bookmarks: list[dict[str, Any]],
        included_refs: list[str],
        dropped_refs: list[str],
        stale_refs: list[ContextStaleRef],
        history_normalization: dict[str, Any],
    ) -> ContextManagerReport:
        report_ref = self._report_ref(workspace_id, run_id)
        existing = self.store.get("reports", report_ref)
        items = list(existing.get("items") or []) if isinstance(existing, dict) else []
        next_sequence = len(items) + 1
        created_at = _now()
        manifest_ref = f"context_manifest:{workspace_id}:{run_id}:{manifest.manifest_id.rsplit(':', 1)[-1]}"
        report = ContextManagerReport(
            workspace_id=workspace_id,
            session_id=session_id,
            run_id=run_id,
            report_ref=report_ref,
            manifest_ref=manifest_ref,
            policy=policy,
            manifest=manifest,
            decisions=decisions,
            pressure=pressure,
            artifacts=artifacts,
            proofs=proofs,
            bookmarks=bookmarks,
            included_refs=included_refs,
            dropped_refs=dropped_refs,
            stale_refs=stale_refs,
            history_normalization=history_normalization,
            next_sequence=next_sequence + 1,
            created_at=created_at,
        )
        item = report.model_dump(mode="json", by_alias=True)
        item.pop("policy", None)
        payload = report.model_dump(mode="json", by_alias=True)
        payload["items"] = [*items, item][-200:]
        self.store.upsert("reports", report_ref, payload)
        self.store.upsert("reports", manifest_ref, manifest.model_dump(mode="json", by_alias=True))
        self._journal(report, compact_recommended=bool(manifest.metadata.get("compact_recommended")))
        return report

    def _journal(self, report: ContextManagerReport, *, compact_recommended: bool) -> None:
        if self.event_journal_service is None:
            return
        payload = {
            "context_manager_ref": report.report_ref,
            "manifest_ref": report.manifest_ref,
            "decision_count": len(report.decisions),
            "included_sections": report.manifest.included_sections,
            "summarized_sections": report.manifest.summarized_sections,
            "ref_sections": report.manifest.ref_sections,
            "dropped_sections": report.manifest.dropped_sections,
            "stale_ref_count": len(report.stale_refs),
            "history_status": report.history_normalization.get("status"),
        }
        events = [
            ("context.budget_decided", "Context budget policy applied."),
            ("context.manifest_created", "Context manifest created."),
        ]
        if compact_recommended:
            events.append(("context.auto_compact_triggered", "Context manager recommended compacting."))
        if report.stale_refs:
            events.append(("context.stale_refs_detected", "Context manager detected stale refs."))
        if report.history_normalization and report.history_normalization.get("status") != "ok":
            events.append(("context.history_normalized", "Context history normalization required."))
        for event_type, summary in events:
            try:
                self.event_journal_service.append_run(
                    workspace_id=report.workspace_id,
                    run_id=report.run_id,
                    event_type=event_type,
                    payload=payload,
                    actor="system",
                    summary=summary,
                    source_ref=report.report_ref,
                    idempotency_key=f"{event_type}:{report.report_ref}:{report.next_sequence}",
                )
            except Exception:
                continue

    @staticmethod
    def _payload(prompt_payload: dict[str, Any] | str | None) -> dict[str, Any]:
        if isinstance(prompt_payload, dict):
            return _jsonable(prompt_payload) if isinstance(_jsonable(prompt_payload), dict) else {}
        if isinstance(prompt_payload, str):
            try:
                parsed = json.loads(prompt_payload)
            except json.JSONDecodeError:
                return {"raw_prompt": prompt_payload}
            return parsed if isinstance(parsed, dict) else {"raw_prompt": prompt_payload}
        return {}

    @staticmethod
    def _payload_section_keys() -> dict[str, tuple[str, ...]]:
        return {
            "current_task": ("task", "prompt", "user_prompt", "current_task"),
            "workspace_memory": ("agent_memory", "memory", "workspace_memory"),
            "session_tail": ("last_turn_summary", "post_compact_messages", "read_cache_hints"),
            "transcript": ("transcript", "tool_result_messages"),
            "tool_results": ("tool_results", "large_tool_outputs", "process_outputs"),
            "code_context": ("file_contexts", "selected_code", "context_pack"),
            "targeted_files": ("targeted_files",),
            "recent_diff": ("latest_diff_summary", "recent_diff", "latest_turn_diff"),
            "proofs": ("browser_flow_proof", "browser_proof", "visual_qa", "readiness"),
            "checks": ("latest_checks", "check_results", "failed_checks"),
            "diagnostics": ("diagnostics_delta", "agent_diagnostics", "repair_packets"),
            "skills": ("skill_context", "tool_registry"),
            "artifacts": ("artifacts", "output_artifacts"),
        }

    @staticmethod
    def _target_file_count(payload: dict[str, Any]) -> int:
        value = payload.get("targeted_files")
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, list):
            return len(value)
        return 0

    @staticmethod
    def _items(value: list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            items = value.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [value]
        return []

    @staticmethod
    def _stale_refs(pressure: dict[str, Any]) -> list[ContextStaleRef]:
        latest = pressure.get("latest") if isinstance(pressure.get("latest"), dict) else pressure
        raw = latest.get("stale_path_refs") if isinstance(latest, dict) else []
        refs: list[ContextStaleRef] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                continue
            refs.append(
                ContextStaleRef(
                    path=str(item.get("path") or ""),
                    source=str(item.get("source") or "context_pressure"),
                    reason=str(item.get("reason") or "stale_reference"),
                    suggested_path=str(item.get("suggested_path") or "") or None,
                )
            )
        return refs[:40]

    @staticmethod
    def _sections_by_action(decisions: list[ContextFragmentDecision], action: str) -> list[str]:
        return list(dict.fromkeys(decision.section for decision in decisions if decision.action == action))

    @staticmethod
    def _refs_from_decisions(decisions: list[ContextFragmentDecision], *, include: bool) -> list[str]:
        actions = {"include", "summarize"} if include else {"artifact_ref", "microcompact", "discard", "defer"}
        return list(dict.fromkeys(str(decision.ref) for decision in decisions if decision.action in actions and decision.ref))

    @staticmethod
    def _extract_refs(value: Any) -> list[str]:
        refs: list[str] = []

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if str(key).endswith("_ref") or str(key) in {"ref", "artifact_ref", "microcompact_ref"}:
                        if str(nested or "").strip():
                            refs.append(str(nested))
                    walk(nested)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)

        walk(value)
        return list(dict.fromkeys(refs))

    @staticmethod
    def _value_ref(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in ("ref", "artifact_ref", "microcompact_ref", "context_manager_ref"):
                if value.get(key):
                    return str(value.get(key))
        return None

    @staticmethod
    def _manifest_id(*, workspace_id: str, run_id: str, payload: dict[str, Any], decisions: list[ContextFragmentDecision]) -> str:
        material = json.dumps(
            {"payload": payload, "decisions": [decision.model_dump(mode="json") for decision in decisions]},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        return f"context_manifest:{workspace_id}:{run_id}:{digest}"

    @staticmethod
    def _report_ref(workspace_id: str, run_id: str) -> str:
        return f"context_manager:{workspace_id}:{run_id}"

    @staticmethod
    def _pressure_ref(workspace_id: str, run_id: str) -> str:
        return f"context_pressure:{workspace_id}:{run_id}"
