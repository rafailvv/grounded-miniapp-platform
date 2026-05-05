from __future__ import annotations

from typing import Any

from app.models.domain import CheckExecutionRecord, RunCheckResult, ValidationSnapshot
from app.services.check_runner import CheckRunner
from app.services.workspace.service import WorkspaceService


class WorkspaceAgentCompletionGate:
    """Strict-green completion rules for the code-agent loop."""

    def __init__(self, workspace_service: WorkspaceService) -> None:
        self.workspace_service = workspace_service

    def completion_state(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request_mode: str,
        results: list[RunCheckResult],
        validation_snapshot: ValidationSnapshot | None,
        generation_mode: str | None = None,
        intent: str | None = None,
        acceptance_contract: dict[str, Any] | None = None,
        focused_visual_edit: bool = False,
    ) -> dict[str, object]:
        failed = [result for result in results if result.status in {"failed", "blocked"}]
        by_name = {result.name: result for result in results}
        has_diff = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
        no_app_diff = request_mode in {"generate", "fix"} and not has_diff
        mode_value = str(generation_mode or "").lower()
        acceptance_required = bool((acceptance_contract or {}).get("required")) or request_mode == "generate" or mode_value in {"quality", "balanced"}
        require_product_proof = acceptance_required and not focused_visual_edit
        remaining_issues = [
            {
                "kind": "check_failure",
                "check": result.name,
                "details": result.details,
                "logs": result.logs[-8:],
                "blocking": True,
            }
            for result in failed
        ]
        if no_app_diff:
            remaining_issues.append(
                {
                    "kind": "meaningful_diff",
                    "check": "meaningful_diff",
                    "details": "Generation must create a contract-derived draft diff before completion.",
                    "blocking": True,
                }
            )
        if require_product_proof:
            required_checks = ("api_workflow_smoke", "browser_flow_smoke")
            for check_name in required_checks:
                result = by_name.get(check_name)
                if result is None or result.status != "passed":
                    remaining_issues.append(
                        {
                            "kind": "required_product_proof",
                            "check": check_name,
                            "details": f"{check_name} must pass before a generate/balanced/quality run can complete.",
                            "blocking": True,
                        }
                    )
            browser = by_name.get("browser_flow_smoke")
            mobile = browser.diagnostics.get("mobile_layout") if browser and isinstance(browser.diagnostics, dict) else None
            if isinstance(mobile, dict) and mobile.get("status") == "failed":
                remaining_issues.append(
                    {
                        "kind": "mobile_layout",
                        "check": "browser_flow_smoke",
                        "details": "Mobile layout report contains blocking issues.",
                        "diagnostics": mobile,
                        "blocking": True,
                    }
                )
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in validation_snapshot.issues
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        blocking_issues = [issue for issue in remaining_issues if isinstance(issue, dict) and issue.get("blocking", True)]
        complete = not failed and not no_app_diff and not blocking_issues
        optimistic = complete or (focused_visual_edit and not failed and not no_app_diff)
        return {
            "strict_green": complete,
            "optimistic_complete": optimistic,
            "preview_ok": not any(result.status in {"failed", "blocked"} for result in results if result.name in {"preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke"}),
            "validators_ok": not any(result.status == "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"}),
            "build_ok": not any(result.status == "failed" for result in results if result.name == "changed_files_static"),
            "canonical_smoke_ok": not any(result.status == "failed" for result in results if result.name == "platform_invariants"),
            "acceptance_required": acceptance_required,
            "product_proof_required": require_product_proof,
            "remaining_issues": remaining_issues,
        }

    @staticmethod
    def validation_snapshot_from_execution(execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        return ValidationSnapshot(
            platform_valid=not bool(issues),
            checks_valid=not bool(issues),
            build_valid=not build_failed,
            blocking=bool(issues),
            issues=issues,
        )
