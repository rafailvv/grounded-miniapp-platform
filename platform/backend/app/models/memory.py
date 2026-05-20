from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class MemoryApiModel(StrictModel):
    pass


class MemoryCitation(MemoryApiModel):
    run_id: str | None = None
    workspace_id: str | None = None
    report_ref: str | None = None
    artifact_refs: dict[str, Any] = Field(default_factory=dict)
    file_path: str | None = None
    check_name: str | None = None
    source: str | None = None
    created_at: str | None = None


class MemoryConfidence(MemoryApiModel):
    score: float = 0.5
    level: str = "medium"
    signals: list[str] = Field(default_factory=list)


class MemoryExpiry(MemoryApiModel):
    expires_at: str | None = None
    ttl_days: int | None = None
    reason: str | None = None
    expired: bool = False


class MemoryStaleCheck(MemoryApiModel):
    status: str = "fresh_or_unreferenced"
    checks: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)


class RawMemoryItem(MemoryApiModel):
    memory_id: str
    kind: str
    text: str
    status: str = "candidate"
    fingerprint: str
    citation: dict[str, Any] | None = None
    citations: list[MemoryCitation] = Field(default_factory=list)
    confidence: MemoryConfidence = Field(default_factory=MemoryConfidence)
    expiry: MemoryExpiry = Field(default_factory=MemoryExpiry)
    evidence: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RunMemoryBatch(MemoryApiModel):
    schema_: str = Field(default="grounded.memory_stage1.v1", alias="schema")
    phase: str = "raw"
    workspace_id: str
    run_id: str
    status: str = "empty"
    items: list[RawMemoryItem] = Field(default_factory=list)
    raw_count: int = 0
    created_at: str


class ConsolidatedMemoryItem(MemoryApiModel):
    memory_id: str
    kind: str
    text: str
    status: str = "active"
    fingerprint: str
    citation: dict[str, Any] | None = None
    citations: list[MemoryCitation] = Field(default_factory=list)
    confidence: MemoryConfidence = Field(default_factory=MemoryConfidence)
    expiry: MemoryExpiry = Field(default_factory=MemoryExpiry)
    stale_check: MemoryStaleCheck = Field(default_factory=MemoryStaleCheck)
    evidence: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "memory_pipeline"
    created_at: str | None = None
    consolidated_at: str | None = None
    updated_at: str | None = None
    superseded_by: str | None = None
    retrieval: dict[str, Any] = Field(default_factory=dict)


class WorkspaceMemoryReport(MemoryApiModel):
    schema_: str = Field(default="grounded.workspace_memory.v2", alias="schema")
    workspace_id: str
    items: list[ConsolidatedMemoryItem] = Field(default_factory=list)
    pipeline: dict[str, Any] = Field(default_factory=dict)
    stale_check: MemoryStaleCheck = Field(default_factory=MemoryStaleCheck)
    project_rules: list[dict[str, Any]] = Field(default_factory=list)
    user_preferences: list[dict[str, Any]] = Field(default_factory=list)
    product_decisions: list[dict[str, Any]] = Field(default_factory=list)
    accepted_ux_rules: list[dict[str, Any]] = Field(default_factory=list)
    architecture_summary: list[dict[str, Any]] = Field(default_factory=list)
    known_failures: list[dict[str, Any]] = Field(default_factory=list)
    rejected_approaches: list[dict[str, Any]] = Field(default_factory=list)
    do_not_change: list[dict[str, Any]] = Field(default_factory=list)
    platform_constraints: list[dict[str, Any]] = Field(default_factory=list)
    repeated_fixes: list[dict[str, Any]] = Field(default_factory=list)


class MemoryConsolidationReport(MemoryApiModel):
    schema_: str = Field(default="grounded.memory_consolidation.v1", alias="schema")
    workspace_id: str
    status: str
    stage1_count: int = 0
    raw_count: int = 0
    active_count: int = 0
    stale_count: int = 0
    expired_count: int = 0
    superseded_count: int = 0
    deduped_count: int = 0
    updated_at: str


class MemoryRetrievalRequest(MemoryApiModel):
    prompt: str = ""
    paths: list[str] = Field(default_factory=list)
    top_k: int = 10
    include_inactive: bool = False
    failure_class: str | None = None


class MemoryRetrievalHit(MemoryApiModel):
    item: dict[str, Any]
    score: float
    selection_reason: list[str] = Field(default_factory=list)


class MemoryRetrievalResult(MemoryApiModel):
    schema_: str = Field(default="grounded.memory_retrieval.v1", alias="schema")
    workspace_id: str
    prompt_excerpt: str = ""
    top_k: int = 10
    status: str = "empty"
    hits: list[MemoryRetrievalHit] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: str
