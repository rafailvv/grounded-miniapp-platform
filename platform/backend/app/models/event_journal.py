from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class EventPayloadRef(StrictModel):
    payload_ref: str
    payload_sha256: str
    created_at: str


class EventJournalPayload(EventPayloadRef):
    payload: dict[str, Any] = Field(default_factory=dict)


class EventPayloadRecord(EventJournalPayload):
    pass


class RunEventV2(StrictModel):
    event_id: str
    workspace_id: str
    run_id: str
    sequence: int
    event_type: str
    actor: str = "system"
    payload_ref: str
    payload_sha256: str
    summary: str = ""
    source_ref: str | None = None
    idempotency_key: str | None = None
    created_at: str


class ThreadEventV2(StrictModel):
    event_id: str
    workspace_id: str
    thread_id: str
    turn_id: str | None = None
    run_id: str | None = None
    sequence: int
    event_type: str
    actor: str = "system"
    payload_ref: str
    payload_sha256: str
    summary: str = ""
    source_ref: str | None = None
    idempotency_key: str | None = None
    created_at: str


class EventJournalPage(StrictModel):
    schema_: str = Field(default="grounded.event_journal_page.v2", alias="schema")
    status: str = "ok"
    scope: str
    run_id: str | None = None
    thread_id: str | None = None
    items: list[RunEventV2 | ThreadEventV2] = Field(default_factory=list)
    next_sequence: int = 0


class RunJournalState(StrictModel):
    schema_: str = Field(default="grounded.run_journal_state.v2", alias="schema")
    run_id: str
    workspace_id: str | None = None
    status: str = "empty"
    event_count: int = 0
    next_sequence: int = 0
    latest_stage: str | None = None
    latest_status: str | None = None
    blocking: bool = False
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    tool_events: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    apply_events: list[dict[str, Any]] = Field(default_factory=list)
    repair_events: list[dict[str, Any]] = Field(default_factory=list)
    protocol_refs: list[dict[str, Any]] = Field(default_factory=list)
    replay_cursor: int = 0


class ThreadJournalState(StrictModel):
    schema_: str = Field(default="grounded.thread_journal_state.v2", alias="schema")
    thread_id: str
    workspace_id: str | None = None
    status: str = "empty"
    event_count: int = 0
    next_sequence: int = 0
    turns: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    linked_runs: list[dict[str, Any]] = Field(default_factory=list)
    replay_cursor: int = 0
