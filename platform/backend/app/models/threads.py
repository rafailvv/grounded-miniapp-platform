from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.domain import new_id, utc_now
from app.models.common import StrictModel


ThreadStatus = Literal["active", "running", "idle", "archived", "failed"]
TurnStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
TurnKind = Literal["user", "agent", "review", "compaction", "repair"]
ItemStatus = Literal["started", "completed", "failed"]


class ThreadRecord(StrictModel):
    thread_id: str = Field(default_factory=lambda: new_id("thread"))
    workspace_id: str
    title: str = "Untitled Thread"
    status: ThreadStatus = "active"
    archived: bool = False
    forked_from_thread_id: str | None = None
    current_turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TurnRecord(StrictModel):
    turn_id: str = Field(default_factory=lambda: new_id("turn"))
    thread_id: str
    workspace_id: str
    kind: TurnKind = "agent"
    status: TurnStatus = "queued"
    prompt: str = ""
    linked_run_id: str | None = None
    parent_turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ItemRecord(StrictModel):
    item_id: str = Field(default_factory=lambda: new_id("item"))
    thread_id: str
    turn_id: str | None = None
    item_type: str
    status: ItemStatus = "completed"
    sequence: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RolloutEventRecord(StrictModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    thread_id: str
    turn_id: str | None = None
    event_type: str
    sequence: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactRecord(StrictModel):
    artifact_id: str = Field(default_factory=lambda: new_id("artifact"))
    thread_id: str | None = None
    turn_id: str | None = None
    artifact_type: str
    storage_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ThreadSnapshot(StrictModel):
    thread: ThreadRecord
    turns: list[TurnRecord] = Field(default_factory=list)
    items: list[ItemRecord] = Field(default_factory=list)
    events: list[RolloutEventRecord] = Field(default_factory=list)


class CursorPage(StrictModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None

