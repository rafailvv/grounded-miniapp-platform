from __future__ import annotations

from app.models.artifacts import ValidationIssue
from app.models.domain import CheckExecutionRecord, RunCheckResult, ValidationSnapshot
from app.services.check_runner import CheckRunner


class MiniappGenerationCompletion:
    @staticmethod
    def informational_issues_from_execution(execution: CheckExecutionRecord) -> list[dict]:
        issues: list[dict] = []
        for result in execution.results:
            if result.status == "failed":
                continue
            if result.name == "changed_files_static":
                issues.append(
                    ValidationIssue(
                        code="build.static_checks_passed",
                        message=result.details or "Draft source passed static validation.",
                        severity="low",
                        location="miniapp/app",
                        blocking=False,
                    ).model_dump(mode="json")
                )
            elif result.name in {"generated_app_python_tests", "generated_app_js_tests"}:
                issues.append(
                    ValidationIssue(
                        code="tests.generated_app_verified",
                        message=result.details or "Generated app tests passed.",
                        severity="low",
                        location="tests",
                        blocking=False,
                    ).model_dump(mode="json")
                )
            elif result.name in {"preview_boot_smoke", "preview_connectivity_smoke"} and result.status in {"passed", "skipped"}:
                issues.append(
                    ValidationIssue(
                        code="preview.verification_recorded",
                        message=result.details or "Preview verification completed.",
                        severity="low",
                        location="preview",
                        blocking=False,
                    ).model_dump(mode="json")
                )
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for issue in issues:
            key = (str(issue.get("code") or ""), str(issue.get("location") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped

    @staticmethod
    def preview_failure_issue(preview: object) -> ValidationIssue:
        message = next(
            (
                str(line).strip()
                for line in reversed(getattr(preview, "logs", []) or [])
                if str(line).strip()
            ),
            "Preview runtime failed to rebuild.",
        )
        return ValidationIssue(
            code="preview.rebuild_failed",
            message=message,
            severity="high",
            location="preview",
            blocking=True,
        )

    @staticmethod
    def is_non_blocking_preview_issue(issue: ValidationIssue) -> bool:
        message = issue.message.lower()
        infra_markers = (
            "docker daemon socket",
            "operation not permitted",
            "permission denied",
            "connect to the docker daemon",
            "dial unix",
        )
        return any(marker in message for marker in infra_markers)

    @staticmethod
    def validation_snapshot_from_execution(execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        if not issues:
            issues = MiniappGenerationCompletion.informational_issues_from_execution(execution)
        return ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=not build_failed,
            blocking=any(bool(issue.get("blocking", False)) for issue in issues),
            issues=issues,
        )

    @classmethod
    def workspace_loop_completion_state(
        cls,
        results: list[RunCheckResult],
        preview_details: dict[str, object],
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, object]:
        validators_ok = all(
            result.status != "failed"
            for result in results
            if result.name in {"schema_validators", "connectivity_validators"}
        )
        build_ok = all(result.status != "failed" for result in results if result.name == "changed_files_static")
        preview_result = next((result for result in results if result.name == "preview_boot_smoke"), None)
        preview_connectivity_result = next((result for result in results if result.name == "preview_connectivity_smoke"), None)
        preview_ok = (
            preview_result is not None
            and preview_connectivity_result is not None
            and preview_result.status != "failed"
            and preview_connectivity_result.status != "failed"
        )
        app_test_failures = [
            result
            for result in results
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"} and result.status == "failed"
        ]
        remaining_issues = [
            {
                "kind": "generated_test_failure",
                "location": "tests",
                "blocking": False,
                "check": result.name,
                "details": result.details,
                "logs": result.logs[-8:],
            }
            for result in app_test_failures
        ]
        if validation_snapshot is not None:
            for issue in validation_snapshot.issues:
                if isinstance(issue, dict) and not issue.get("blocking", False):
                    remaining_issues.append(issue)
        strict_green = validators_ok and build_ok and not app_test_failures and preview_ok
        return {
            "strict_green": strict_green,
            "optimistic_complete": False,
            "remaining_issues": remaining_issues,
        }
