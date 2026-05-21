from __future__ import annotations

import re
from typing import Any

from app.models.domain import CheckExecutionRecord, RunCheckResult, ValidationSnapshot
from app.services.check_runner import CheckRunner
from app.services.generation_sla import GenerationSla
from app.services.product_readiness import ProductReadinessContract
from app.services.requirement_traceability import RequirementTraceabilityMatrix
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
        implementation_plan: dict[str, Any] | None = None,
        focused_visual_edit: bool = False,
    ) -> dict[str, object]:
        failed = [result for result in results if result.status in {"failed", "blocked"}]
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        has_diff = bool(diff_text.strip())
        no_app_diff = request_mode in {"generate", "fix"} and not has_diff
        readiness = ProductReadinessContract.evaluate(
            run_mode=request_mode,
            generation_mode=generation_mode,
            intent=intent,
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            results=results,
            diff_text=diff_text,
            touched_files=None,
            require_diff=True,
            require_product_source_change=True,
            require_apply=False,
        )
        acceptance_required = readiness.acceptance_required
        require_product_proof = acceptance_required
        remaining_issues = [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in readiness.blocking_reasons]
        traceability = RequirementTraceabilityMatrix.build(
            run={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "prompt": "",
                "mode": request_mode,
                "intent": intent,
                "generation_mode": generation_mode,
                "acceptance_contract": acceptance_contract or {},
                "implementation_plan": implementation_plan or {},
            },
            artifacts={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "diff": diff_text,
                "check_results": [result.model_dump(mode="json") for result in results],
            },
        )
        if GenerationSla.requires_full_audit(generation_mode):
            remaining_issues.extend(RequirementTraceabilityMatrix.blocking_issues(traceability))
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in validation_snapshot.issues
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        blocking_issues = [issue for issue in remaining_issues if isinstance(issue, dict) and issue.get("blocking", True)]
        complete = not failed and not no_app_diff and not blocking_issues
        optimistic = complete
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
            "product_readiness": readiness.model_dump(mode="json", by_alias=True),
            "requirement_traceability": traceability,
        }

    @classmethod
    def _product_task_ledger_issues(
        cls,
        results: list[RunCheckResult],
        *,
        implementation_plan: dict[str, Any] | None = None,
    ) -> list[dict[str, object]]:
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        ledger = plan.get("product_task_ledger") if isinstance(plan.get("product_task_ledger"), list) else []
        if not ledger:
            return []
        role_coverage = cls._role_coverage_from_results(results)
        if not role_coverage:
            return []
        issues: list[dict[str, object]] = []
        for item in ledger:
            if not isinstance(item, dict) or str(item.get("kind") or "") not in {"source", "update", "observer", "participant"}:
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"client", "specialist", "manager"}:
                continue
            try:
                expected_routes = max(1, int(item.get("expected_min_routes") or 1))
            except (TypeError, ValueError):
                expected_routes = 1
            coverage = role_coverage.get(role) if isinstance(role_coverage.get(role), dict) else {}
            status = str(coverage.get("status") or "").strip()
            try:
                route_count = int(coverage.get("route_count") or 0)
            except (TypeError, ValueError):
                route_count = 0
            if status == "present" and route_count >= expected_routes:
                continue
            issues.append(
                {
                    "kind": "product_task_ledger",
                    "check": "product_task_ledger",
                    "details": (
                        f"{role} ledger item {item.get('id') or role} is incomplete: "
                        f"expected at least {expected_routes} routeable product page(s), got {route_count} with status {status or 'missing'}."
                    ),
                    "role": role,
                    "ledger_item_id": item.get("id"),
                    "expected_min_routes": expected_routes,
                    "actual_route_count": route_count,
                    "coverage_status": status,
                    "target_files": list(item.get("owned_paths") or []),
                    "expected_proof": list(item.get("proof_checks") or []),
                    "blocking": True,
                }
            )
        return issues

    @staticmethod
    def _role_coverage_from_results(results: list[RunCheckResult]) -> dict[str, Any]:
        for result in results:
            if result.name != "platform_invariants":
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            role_coverage = diagnostics.get("role_coverage")
            return dict(role_coverage) if isinstance(role_coverage, dict) else {}
        return {}

    @classmethod
    def _product_paths_from_diff(cls, diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", str(diff_text or ""), flags=re.MULTILINE):
            path = match.group(1).strip()
            if path.startswith("draft/"):
                path = path.split("draft/", 1)[-1]
            if path.startswith("source/"):
                path = path.split("source/", 1)[-1]
            if cls._is_product_runtime_path(path):
                paths.append(path)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _is_product_runtime_path(path: str) -> bool:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized.startswith("miniapp/"):
            return False
        if normalized.startswith("miniapp/tests/") or normalized.startswith("miniapp/app/generated/"):
            return False
        if "/__pycache__/" in normalized or normalized.endswith((".pyc", ".pyo")):
            return False
        return normalized.startswith(("miniapp/app/", "miniapp/requirements.txt", "miniapp/Dockerfile"))

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
