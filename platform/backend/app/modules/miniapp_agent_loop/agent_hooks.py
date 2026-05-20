from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from app.models.hooks import HookContext, HookEvaluation
from app.services.hook_policy_service import HookPolicyService


AgentHookName = Literal[
    "session_start",
    "before_run",
    "after_run",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "before_apply",
    "after_apply",
    "before_checks",
    "after_checks",
    "on_check_failed",
    "pre_apply_patch",
    "post_apply_patch",
    "post_browser_verify",
    "after_gate",
    "on_memory_update",
    "on_export",
]


@dataclass(frozen=True)
class AgentHookEvent:
    hook: AgentHookName
    status: Literal["started", "completed", "failed"]
    payload: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "hook": self.hook,
            "status": self.status,
            "schema": "grounded.hook_event.v1",
            "side_effects_allowed": False,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class AgentHookOutcome:
    hook: AgentHookName
    should_block: bool
    block_reason: str | None
    additional_contexts: list[str]
    event: dict[str, Any]
    evaluation: dict[str, Any] | None = None
    context_items: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        evaluation = dict(self.evaluation or {})
        return {
            "schema": "grounded.hook_outcome.v1",
            "hook": self.hook,
            "should_block": self.should_block,
            "block_reason": self.block_reason,
            "additional_contexts": list(self.additional_contexts),
            "context_items": list(self.context_items or []),
            "matched_rules": list(evaluation.get("matched_rules") or []),
            "tags": dict(evaluation.get("tags") or {}),
            "validation_issues": list(evaluation.get("validation_issues") or []),
            "evaluation": evaluation or None,
            "event": self.event,
        }


class AgentHookManager:
    """Small lifecycle hook recorder for agent tools, patches, and proof steps."""

    def __init__(
        self,
        *,
        policy_service: HookPolicyService | None = None,
        event_journal_service: Any | None = None,
    ) -> None:
        self.policy_service = policy_service
        self.event_journal_service = event_journal_service
        self._events: dict[str, list[AgentHookEvent]] = {}

    def record(
        self,
        run_id: str,
        hook: AgentHookName,
        *,
        status: Literal["started", "completed", "failed"] = "completed",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_payload = dict(payload or {})
        safe_payload.setdefault("side_effects_allowed", False)
        safe_payload.setdefault("permission", "record_only")
        event = AgentHookEvent(
            hook=hook,
            status=status,
            payload=safe_payload,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._events.setdefault(run_id, []).append(event)
        return event.as_dict()

    def run(
        self,
        run_id: str,
        hook: AgentHookName,
        *,
        payload: dict[str, Any] | None = None,
    ) -> AgentHookOutcome:
        safe_payload = dict(payload or {})
        safe_payload.setdefault("run_id", run_id)
        workspace_id = self._workspace_id_from_payload(safe_payload)
        evaluation = self._evaluate(run_id=run_id, workspace_id=workspace_id, hook=hook, payload=safe_payload)
        context_items = list(evaluation.get("added_contexts") or [])
        additional_contexts = [
            str(item.get("text") or "")
            for item in context_items
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        should_block = bool(evaluation.get("should_block"))
        block_reason = str(evaluation.get("block_reason") or "") or None
        if safe_payload.get("block_reason"):
            should_block = True
            block_reason = str(safe_payload.get("block_reason"))
        event = self.record(
            run_id,
            hook,
            status="failed" if should_block else "completed",
            payload={
                **safe_payload,
                "additional_contexts": additional_contexts,
                "hook_context_items": context_items,
                "block_reason": block_reason,
                "evaluation": evaluation,
                "matched_rules": evaluation.get("matched_rules") or [],
                "tags": evaluation.get("tags") or {},
                "validation_issues": evaluation.get("validation_issues") or [],
            },
        )
        self._record_journal_events(
            workspace_id=workspace_id,
            run_id=run_id,
            hook=hook,
            should_block=should_block,
            block_reason=block_reason,
            contexts=context_items,
            event=event,
            evaluation=evaluation,
        )
        return AgentHookOutcome(
            hook=hook,
            should_block=should_block,
            block_reason=block_reason,
            additional_contexts=additional_contexts,
            event=event,
            evaluation=evaluation,
            context_items=context_items,
        )

    def snapshot(self, run_id: str) -> dict[str, Any]:
        events = [item.as_dict() for item in self._events.get(run_id, [])]
        counts: dict[str, int] = {}
        matched_rules: list[dict[str, Any]] = []
        validation_issues: list[dict[str, Any]] = []
        context_count = 0
        blocked_count = 0
        for item in events:
            key = f"{item.get('hook')}:{item.get('status')}"
            counts[key] = counts.get(key, 0) + 1
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("status") == "failed":
                blocked_count += 1
            contexts = payload.get("hook_context_items") or payload.get("additional_contexts") or []
            if isinstance(contexts, list):
                context_count += len(contexts)
            for rule in payload.get("matched_rules") or []:
                if isinstance(rule, dict):
                    matched_rules.append(rule)
            for issue in payload.get("validation_issues") or []:
                if isinstance(issue, dict):
                    validation_issues.append(issue)
        return {
            "schema": "grounded.hook_trace.v1",
            "run_id": run_id,
            "side_effects_allowed": False,
            "event_count": len(events),
            "counts": counts,
            "matched_rules": matched_rules[-100:],
            "context_count": context_count,
            "blocked_count": blocked_count,
            "validation_issues": validation_issues[-100:],
            "events": events[-500:],
        }

    def _evaluate(self, *, run_id: str, workspace_id: str | None, hook: AgentHookName, payload: dict[str, Any]) -> dict[str, Any]:
        if self.policy_service is not None:
            evaluation = self.policy_service.evaluate(
                HookContext(hook=hook, workspace_id=workspace_id, run_id=run_id, payload=payload)
            )
            return evaluation.model_dump(mode="json", by_alias=True)
        return self._fallback_evaluation(run_id=run_id, workspace_id=workspace_id, hook=hook, payload=payload).model_dump(mode="json", by_alias=True)

    def _fallback_evaluation(
        self,
        *,
        run_id: str,
        workspace_id: str | None,
        hook: AgentHookName,
        payload: dict[str, Any],
    ) -> HookEvaluation:
        additional_contexts: list[dict[str, Any]] = []
        matched_rules: list[dict[str, Any]] = []
        should_block = False
        block_reason = None
        if hook == "on_check_failed":
            failed = payload.get("failed_checks")
            if isinstance(failed, list) and failed:
                matched_rules.append({"rule_id": "builtin.on_check_failed.repair_context", "source": "builtin", "actions": ["add_context"]})
                additional_contexts.append(
                    {
                        "text": "Repair must start from the failing check evidence and named repair packet before broad edits.",
                        "priority": 100,
                        "target": "repair_turn",
                        "source": "builtin",
                        "source_rule_id": "builtin.on_check_failed.repair_context",
                        "metadata": {},
                    }
                )
        if hook == "pre_tool_use" and payload.get("risk") == "forbidden":
            matched_rules.append({"rule_id": "builtin.pre_tool_use.forbidden_risk", "source": "builtin", "actions": ["block"]})
            should_block = True
            block_reason = "Tool risk is forbidden by internal hook policy."
        return HookEvaluation.model_validate(
            {
                "trace_id": f"hook_eval_fallback_{datetime.now(timezone.utc).timestamp()}",
                "hook": hook,
                "workspace_id": workspace_id,
                "run_id": run_id,
                "should_block": should_block,
                "block_reason": block_reason,
                "added_contexts": additional_contexts,
                "matched_rules": matched_rules,
            }
        )

    def _workspace_id_from_payload(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("workspace_id")
        if value:
            return str(value)
        input_payload = payload.get("input")
        if isinstance(input_payload, dict) and input_payload.get("workspace_id"):
            return str(input_payload.get("workspace_id"))
        return None

    def _record_journal_events(
        self,
        *,
        workspace_id: str | None,
        run_id: str,
        hook: AgentHookName,
        should_block: bool,
        block_reason: str | None,
        contexts: list[dict[str, Any]],
        event: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> None:
        if self.event_journal_service is None or not workspace_id or not run_id:
            return
        payload = {
            "hook": hook,
            "event": event,
            "evaluation": {
                "trace_id": evaluation.get("trace_id"),
                "matched_rules": evaluation.get("matched_rules") or [],
                "should_block": should_block,
                "block_reason": block_reason,
                "context_count": len(contexts),
            },
        }
        try:
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type="hook.evaluated",
                payload=payload,
                actor="system",
                summary=f"{hook} evaluated",
            )
            if should_block:
                self.event_journal_service.append_run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    event_type="hook.blocked",
                    payload={"hook": hook, "reason": block_reason, "trace_id": evaluation.get("trace_id")},
                    actor="system",
                    summary=block_reason or f"{hook} blocked",
                )
            if contexts:
                self.event_journal_service.append_run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    event_type="hook.context_added",
                    payload={"hook": hook, "contexts": contexts, "trace_id": evaluation.get("trace_id")},
                    actor="system",
                    summary=f"{len(contexts)} hook context item(s) added",
                )
        except Exception:
            return
