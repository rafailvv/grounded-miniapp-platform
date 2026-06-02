from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel
from app.models.domain import utc_now


WorkerSessionStatus = Literal["planned", "ready", "running", "waiting", "blocked", "failed", "completed", "merged", "rejected"]
WorkerTurnStatus = Literal["started", "running", "completed", "failed"]
WorkerMailboxMessageStatus = Literal["pending", "consumed", "cancelled"]


class WorkerSessionRecord(StrictModel):
    schema_: str = Field(default="grounded.worker_session.v1", alias="schema")
    worker_session_id: str
    parent_run_id: str
    workspace_id: str
    worker_id: str
    role: str = "writer"
    stage: str = "role_ui_and_tests"
    status: WorkerSessionStatus = "planned"
    branch_run_id: str | None = None
    ownership: dict[str, Any] = Field(default_factory=dict)
    tool_allowlist: list[str] = Field(default_factory=list)
    context_ref: str | None = None
    memory_ref: str | None = None
    output_ref: str | None = None
    mailbox_ref: str | None = None
    ownership_ref: str | None = None
    proof_refs: list[str] = Field(default_factory=list)
    latest_turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkerTurnRecord(StrictModel):
    schema_: str = Field(default="grounded.worker_turn.v1", alias="schema")
    worker_turn_id: str
    worker_session_id: str
    parent_run_id: str
    workspace_id: str
    worker_id: str
    status: WorkerTurnStatus = "started"
    input_refs: dict[str, Any] = Field(default_factory=dict)
    tool_trace_ref: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    diagnostics_ref: str | None = None
    failure_packet_ref: str | None = None
    output_ref: str | None = None
    proof_refs: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerMailboxMessage(StrictModel):
    message_id: str
    kind: str
    from_worker: str = Field(default="coordinator", alias="from")
    to_worker: str = Field(default="coordinator", alias="to")
    status: WorkerMailboxMessageStatus = "pending"
    payload_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    consumed_at: datetime | None = None


class WorkerMailboxReport(StrictModel):
    schema_: str = Field(default="grounded.worker_mailbox.v2", alias="schema")
    status: str = "ready"
    workspace_id: str
    parent_run_id: str
    mailbox_ref: str
    items: list[WorkerMailboxMessage] = Field(default_factory=list)
    next_sequence: int = 1
    updated_at: datetime = Field(default_factory=utc_now)


class WorkerOwnershipLock(StrictModel):
    lock_id: str
    worker_session_id: str | None = None
    worker_id: str
    lease_owner: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    exclusive_write: bool = True
    status: str = "locked"


class WorkerOwnershipReport(StrictModel):
    schema_: str = Field(default="grounded.worker_ownership.v1", alias="schema")
    status: str = "passed"
    workspace_id: str
    parent_run_id: str
    ownership_ref: str
    locks: list[WorkerOwnershipLock] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    forbidden: list[dict[str, Any]] = Field(default_factory=list)
    merge_eligible: bool = True
    updated_at: datetime = Field(default_factory=utc_now)


class WorkerSessionsReport(StrictModel):
    schema_: str = Field(default="grounded.worker_sessions.v1", alias="schema")
    status: str = "ready"
    workspace_id: str
    parent_run_id: str
    sessions_ref: str
    mailbox_ref: str
    ownership_ref: str
    items: list[WorkerSessionRecord] = Field(default_factory=list)
    mailbox: WorkerMailboxReport | None = None
    ownership: WorkerOwnershipReport | None = None
    resume_candidates: list[dict[str, Any]] = Field(default_factory=list)
    next_sequence: int = 1
    updated_at: datetime = Field(default_factory=utc_now)
