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
    ) -> dict[str, object]:
        failed = [result for result in results if result.status == "failed"]
        has_diff = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
        no_app_diff = request_mode in {"generate", "fix"} and not has_diff
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
                    "details": "Generation must create a prompt-specific draft diff before completion.",
                    "blocking": True,
                }
            )
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in validation_snapshot.issues
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        complete = not failed and not no_app_diff
        return {
            "strict_green": complete,
            "optimistic_complete": complete,
            "preview_ok": True,
            "validators_ok": not any(result.status == "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"}),
            "build_ok": not any(result.status == "failed" for result in results if result.name == "changed_files_static"),
            "canonical_smoke_ok": not any(result.status == "failed" for result in results if result.name == "platform_invariants"),
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
