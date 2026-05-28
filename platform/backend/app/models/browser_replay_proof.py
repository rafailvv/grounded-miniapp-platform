from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel
from app.models.domain import utc_now


class BrowserReplayScenarioReport(StrictModel):
    schema_: str = Field(default="grounded.browser_replay_scenario.v1", alias="schema")
    workspace_id: str
    run_id: str
    scenario_id: str
    status: Literal["passed", "failed", "partial", "unknown"] = "unknown"
    role: str | None = None
    route: str | None = None
    viewport: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    playwright_spec: str = ""
    playwright_spec_ref: str | None = None
    screenshot_refs: list[str] = Field(default_factory=list)
    dom_snapshot_refs: list[dict[str, Any]] = Field(default_factory=list)
    console_logs: list[str] = Field(default_factory=list)
    network_logs: list[str] = Field(default_factory=list)
    failed_step_context: dict[str, Any] = Field(default_factory=dict)
    replay_command_hint: str = "npx playwright test"
    created_at: datetime = Field(default_factory=utc_now)


class BrowserReplayProofReport(StrictModel):
    schema_: str = Field(default="grounded.browser_replay_proof.v1", alias="schema")
    workspace_id: str
    run_id: str
    status: Literal["ready", "partial", "empty"] = "empty"
    replay_proof_ref: str
    scenario_refs: list[str] = Field(default_factory=list)
    scenarios: list[BrowserReplayScenarioReport] = Field(default_factory=list)
    playwright_spec_refs: list[str] = Field(default_factory=list)
    failed_replay_packet_ref: str | None = None
    latest_failed_step: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, Any] = Field(default_factory=dict)
    next_sequence: int = 0
    created_at: datetime = Field(default_factory=utc_now)
