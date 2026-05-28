from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field

from app.models.common import StrictModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


GuardianFindingSeverity = Literal["critical", "high", "medium", "low", "info"]
GuardianFindingCategory = Literal[
    "bug",
    "breaking_changes",
    "changed_size_risk",
    "context_bloat",
    "destructive_action",
    "missing_tests",
    "policy",
    "product_readiness",
    "role_workflow",
    "security_privacy",
    "seeded_mock_data",
    "stale_mock_data",
    "mobile_overflow",
    "weak_persistence",
    "check",
]
GuardianChecklistKey = Literal[
    "breaking_changes",
    "missing_tests",
    "product_readiness",
    "mobile_overflow",
    "stale_mock_data",
    "context_bloat",
    "changed_size_risk",
    "security_privacy",
]
GuardianChecklistStatus = Literal["passed", "failed", "warning", "not_applicable"]


class GuardianFinding(StrictModel):
    code: str
    severity: GuardianFindingSeverity = "high"
    category: GuardianFindingCategory
    message: str
    is_blocker_for_apply: bool = True
    file_path: str | None = None
    line: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    repair_hint: str | None = None


class GuardianChecklistItem(StrictModel):
    key: GuardianChecklistKey
    label: str
    status: GuardianChecklistStatus = "passed"
    required: bool = True
    blocker: bool = False
    finding_codes: list[str] = Field(default_factory=list)
    details: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class GuardianReviewReport(StrictModel):
    schema_: str = Field(default="grounded.guardian_review.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: Literal["passed", "failed"]
    source: Literal["pre_apply_guardian", "pre_mutation_guardian", "runtime_verifier", "manual_review"] = "runtime_verifier"
    findings: list[GuardianFinding] = Field(default_factory=list)
    checklist: list[GuardianChecklistItem] = Field(default_factory=list)
    final_review_gate: dict[str, Any] = Field(default_factory=dict)
    review_prompt: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class GuardianSemanticReviewReport(StrictModel):
    schema_: str = Field(default="grounded.guardian_semantic_review.v1", alias="schema")
    workspace_id: str
    run_id: str
    status: Literal["passed", "blocked", "uncertain", "skipped"] = "passed"
    verdict: Literal["allow", "block", "uncertain", "skipped"] = "allow"
    review_packet_ref: str
    findings: list[GuardianFinding] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class GuardianGateReport(StrictModel):
    schema_: str = Field(default="grounded.guardian_gate.v1", alias="schema")
    status: Literal["passed", "blocked", "failed", "unavailable"]
    workspace_id: str
    run_id: str
    guardian_gate_ref: str
    deterministic_review_ref: str | None = None
    semantic_review_ref: str | None = None
    draft_gate_ref: str | None = None
    prompt_contract_ref: str | None = None
    diff_sha256: str
    changed_files: list[str] = Field(default_factory=list)
    findings: list[GuardianFinding] = Field(default_factory=list)
    repair_packets: list[dict[str, Any]] = Field(default_factory=list)
    apply_decision: Literal["allow", "block"] = "block"
    next_sequence: int = 0
    semantic_verdict: Literal["allow", "block", "uncertain", "skipped"] = "skipped"
    created_at: datetime = Field(default_factory=utc_now)
