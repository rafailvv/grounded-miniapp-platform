from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from app.modules.miniapp_agent_loop.agent_tool_registry import (
    MUTATING_AGENT_TOOLS,
    READ_ONLY_AGENT_TOOLS,
    AgentToolBatch,
    AgentToolBatchPlan,
    AgentToolRegistry,
)


@dataclass
class AgentBudgetTracker:
    started_at: float = field(default_factory=monotonic)
    continuation_count: int = 0
    last_total_tokens: int = 0
    last_delta_tokens: int = 0


@dataclass(frozen=True)
class AgentBudgetDecision:
    action: Literal["continue", "stop"]
    reason: str
    total_tokens: int
    token_budget: int | None
    percentage: int | None = None


def agent_tool_kind(tool_name: object) -> Literal["read_only", "mutating", "unknown"]:
    kind = AgentToolRegistry.kind(tool_name)
    if kind in {"read_only", "verification"}:
        return "read_only"
    if kind == "mutating":
        return "mutating"
    return "unknown"


def plan_agent_tool_batches(tool_requests: list[dict[str, Any]]) -> AgentToolBatchPlan:
    return AgentToolRegistry.plan_batches(tool_requests)


def decide_agent_budget(
    tracker: AgentBudgetTracker,
    *,
    total_tokens: int,
    token_budget: int | None,
    completion_threshold: float = 0.9,
    diminishing_delta_tokens: int = 500,
) -> AgentBudgetDecision:
    if not token_budget or token_budget <= 0:
        return AgentBudgetDecision(action="continue", reason="no_token_budget", total_tokens=total_tokens, token_budget=token_budget)
    percentage = round((total_tokens / token_budget) * 100)
    delta = max(0, total_tokens - tracker.last_total_tokens)
    diminishing = (
        tracker.continuation_count >= 3
        and delta < diminishing_delta_tokens
        and tracker.last_delta_tokens < diminishing_delta_tokens
    )
    tracker.last_delta_tokens = delta
    tracker.last_total_tokens = total_tokens
    if total_tokens < int(token_budget * completion_threshold) and not diminishing:
        tracker.continuation_count += 1
        return AgentBudgetDecision(
            action="continue",
            reason="inside_budget",
            total_tokens=total_tokens,
            token_budget=token_budget,
            percentage=percentage,
        )
    return AgentBudgetDecision(
        action="stop",
        reason="diminishing_returns" if diminishing else "budget_threshold_reached",
        total_tokens=total_tokens,
        token_budget=token_budget,
        percentage=percentage,
    )


def compact_agent_memory(
    *,
    turn_history: list[dict[str, Any]],
    draft_action_count: int,
    last_assistant_message: str,
    max_recent_turns: int = 6,
) -> dict[str, Any]:
    recent_turns = turn_history[-max_recent_turns:] if max_recent_turns > 0 else []
    failed_signatures: list[str] = []
    for turn in turn_history:
        signature = str(turn.get("failure_signature") or turn.get("failure_class") or "").strip()
        if signature and signature not in failed_signatures:
            failed_signatures.append(signature)
    return {
        "kind": "agent_memory_summary",
        "turn_count": len(turn_history),
        "recent_turns": recent_turns,
        "draft_action_count": draft_action_count,
        "failed_signatures": failed_signatures[-10:],
        "last_assistant_message": str(last_assistant_message or "")[:1200],
    }
