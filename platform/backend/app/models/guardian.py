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
    "missing_tests",
    "role_workflow",
    "seeded_mock_data",
    "mobile_overflow",
    "weak_persistence",
    "check",
]


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


class GuardianReviewReport(StrictModel):
    schema_: str = Field(default="grounded.guardian_review.v1", alias="schema")
    run_id: str
    workspace_id: str
    status: Literal["passed", "failed"]
    source: Literal["pre_apply_guardian", "runtime_verifier", "manual_review"] = "runtime_verifier"
    findings: list[GuardianFinding] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
