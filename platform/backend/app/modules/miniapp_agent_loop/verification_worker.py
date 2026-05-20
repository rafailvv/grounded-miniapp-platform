from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.models.domain import CheckExecutionRecord
from app.services.product_readiness import ProductReadinessContract, REQUIRED_PRODUCT_CHECKS


@dataclass(frozen=True)
class VerificationWorkerResult:
    status: str
    issues: list[dict[str, Any]]
    summary: str
    created_at: str

    def model_dump(self) -> dict[str, object]:
        return {
            "status": self.status,
            "issues": self.issues,
            "summary": self.summary,
            "created_at": self.created_at,
        }


class VerificationWorker:
    """Read-only final reviewer over contract workflow and mobile proof."""

    REQUIRED_CREATE_CHECKS = set(REQUIRED_PRODUCT_CHECKS)

    @classmethod
    def verify(
        cls,
        *,
        latest_execution: CheckExecutionRecord | None,
        preview_details: dict[str, Any],
        acceptance_contract: dict[str, Any] | None = None,
        require_browser_proof: bool = True,
    ) -> VerificationWorkerResult:
        del preview_details
        issues: list[dict[str, Any]] = []
        results = list(latest_execution.results if latest_execution is not None else [])
        result_by_name = {result.name: result for result in results}
        if require_browser_proof:
            missing = sorted(name for name in cls.REQUIRED_CREATE_CHECKS if name not in result_by_name)
            if missing:
                issues.append({"kind": "missing_required_proof", "checks": missing})
            readiness = ProductReadinessContract.evaluate(
                run_mode="generate",
                acceptance_contract=acceptance_contract,
                results=results,
                target_role_scope=None,
                require_diff=False,
                require_product_source_change=False,
                require_apply=False,
            )
            issues.extend([item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in readiness.blocking_reasons])
        if acceptance_contract and acceptance_contract.get("required") and not result_by_name.get("frontend_interaction_static_smoke"):
            issues.append({"kind": "missing_source_workflow_guard", "check": "frontend_interaction_static_smoke"})

        status = "passed" if not issues else "failed"
        summary = "Verification worker accepted strict green proof." if status == "passed" else "Verification worker found unresolved workflow proof issues."
        return VerificationWorkerResult(
            status=status,
            issues=issues,
            summary=summary,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
