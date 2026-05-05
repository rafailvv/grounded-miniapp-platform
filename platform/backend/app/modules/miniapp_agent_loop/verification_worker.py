from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.models.domain import CheckExecutionRecord


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
    """Read-only final reviewer over generic workflow and mobile proof."""

    REQUIRED_CREATE_CHECKS = {"api_workflow_smoke", "browser_flow_smoke"}

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
        failed = [result for result in results if result.status in {"failed", "blocked"}]
        for result in failed:
            issues.append(
                {
                    "kind": "check_not_green",
                    "check": result.name,
                    "status": result.status,
                    "details": result.details,
                    "logs": list(result.logs or [])[-5:],
                }
            )
        result_by_name = {result.name: result for result in results}
        if require_browser_proof:
            missing = sorted(name for name in cls.REQUIRED_CREATE_CHECKS if name not in result_by_name)
            if missing:
                issues.append({"kind": "missing_required_proof", "checks": missing})
            browser = result_by_name.get("browser_flow_smoke")
            if browser is not None:
                diagnostics = dict(browser.diagnostics or {})
                if not diagnostics:
                    issues.append({"kind": "missing_browser_diagnostics", "check": "browser_flow_smoke"})
                roles_checked = set(str(role) for role in diagnostics.get("roles_checked") or [])
                required_roles = {"client", "specialist", "manager"}
                if not required_roles.issubset(roles_checked):
                    issues.append(
                        {
                            "kind": "browser_proof_missing_roles",
                            "check": "browser_flow_smoke",
                            "missing_roles": sorted(required_roles - roles_checked),
                        }
                    )
                ui_steps = diagnostics.get("ui_steps") or diagnostics.get("steps") or []
                if not isinstance(ui_steps, list) or not ui_steps:
                    issues.append({"kind": "browser_proof_missing_ui_steps", "check": "browser_flow_smoke"})
                if not (
                    diagnostics.get("persisted_marker")
                    or diagnostics.get("persisted_state_marker")
                    or diagnostics.get("created_marker")
                    or diagnostics.get("created_state_marker")
                ):
                    issues.append({"kind": "browser_proof_missing_persisted_marker", "check": "browser_flow_smoke"})
                features = dict((acceptance_contract or {}).get("features") or {})
                if features.get("workflow_update") and not (
                    diagnostics.get("update_marker")
                    or diagnostics.get("update_state_marker")
                    or diagnostics.get("updated_marker")
                    or diagnostics.get("updated_state_marker")
                ):
                    issues.append({"kind": "browser_proof_missing_update_marker", "check": "browser_flow_smoke"})
                mobile = diagnostics.get("mobile_layout")
                if isinstance(mobile, dict) and (mobile.get("horizontal_overflow") or mobile.get("critical_overlap")):
                    issues.append({"kind": "mobile_layout_failed", "diagnostics": mobile})
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
