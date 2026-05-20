from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class TokenUsageTotals(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    turn_count: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


class CostModelBreakdown(StrictModel):
    model: str
    provider: str = "openai"
    cost_tier: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    pricing_source: str = "unknown"
    run_count: int = 0


class CostSummary(StrictModel):
    estimated_cost_usd: float = 0.0
    explicit_cost_usd: float = 0.0
    estimated_from_tokens_usd: float = 0.0
    unpriced_tokens: int = 0
    pricing_source: str = "mixed"
    by_model: list[CostModelBreakdown] = Field(default_factory=list)


class LatencySummary(StrictModel):
    total_ms: int = 0
    average_ms: float = 0.0
    p50_ms: int = 0
    p95_ms: int = 0
    phase_totals_ms: dict[str, int] = Field(default_factory=dict)
    slowest_runs: list[dict[str, Any]] = Field(default_factory=list)


class GenerationModeQuality(StrictModel):
    generation_mode: str
    run_count: int = 0
    terminal_count: int = 0
    green_count: int = 0
    green_rate: float = 0.0
    status_counts: dict[str, int] = Field(default_factory=dict)
    average_total_tokens: float = 0.0
    estimated_cost_usd: float = 0.0


class FailureClassBucket(StrictModel):
    failure_class: str
    count: int = 0
    latest_run_id: str | None = None
    latest_at: str | None = None
    generation_modes: dict[str, int] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)


class RepairSuccessSummary(StrictModel):
    fix_run_count: int = 0
    successful_fix_runs: int = 0
    fix_success_rate: float = 0.0
    repair_case_count: int = 0
    resolved_case_count: int = 0
    case_resolution_rate: float = 0.0
    attempt_count: int = 0
    successful_attempt_count: int = 0
    attempt_success_rate: float = 0.0
    status_counts: dict[str, int] = Field(default_factory=dict)


class ObservabilityReport(StrictModel):
    schema_: str = Field(default="grounded.observability.v1", alias="schema")
    status: str = "ok"
    workspace_id: str | None = None
    generated_at: str
    run_count: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    blocked_runs: int = 0
    running_runs: int = 0
    awaiting_approval_runs: int = 0
    token_usage_total: int = 0
    latency_ms_total: int = 0
    tool_protocol_version: str
    token_usage: TokenUsageTotals = Field(default_factory=TokenUsageTotals)
    cost: CostSummary = Field(default_factory=CostSummary)
    latency: LatencySummary = Field(default_factory=LatencySummary)
    green_rate_by_generation_mode: list[GenerationModeQuality] = Field(default_factory=list)
    failure_classes: list[FailureClassBucket] = Field(default_factory=list)
    repair_success: RepairSuccessSummary = Field(default_factory=RepairSuccessSummary)
    by_status: dict[str, int] = Field(default_factory=dict)
