from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.domain import RunCheckResult


ROLE_ORDER = ("client", "specialist", "manager")
COMPLETED_STATUSES = {"completed", "done", "passed"}
BLOCKED_STATUSES = {"blocked", "failed"}
ACTIVE_STATUSES = {"in_progress", "running", "started"}


class RunTaskLedger:
    """Builds the model-visible product task ledger into API/runtime tasks."""

    SCHEMA = "grounded.run_task_ledger.v1"

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        workspace_id: str,
        implementation_plan: dict[str, Any] | None,
        run_status: str = "pending",
        current_stage: str = "",
        results: list[RunCheckResult | dict[str, Any]] | None = None,
        remaining_issues: list[dict[str, Any]] | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        raw_items = plan.get("runtime_task_ledger") if isinstance(plan.get("runtime_task_ledger"), list) else None
        source = "implementation_plan.runtime_task_ledger" if raw_items is not None else "implementation_plan.product_task_ledger"
        if raw_items is None:
            raw_items = plan.get("product_task_ledger") if isinstance(plan.get("product_task_ledger"), list) else []
        by_check = cls._results_by_name(results or [])
        issues = [item for item in (remaining_issues or []) if isinstance(item, dict)]
        items = [
            cls._task_from_item(
                item=item,
                index=index,
                run_id=run_id,
                source=source,
                run_status=run_status,
                current_stage=current_stage,
                by_check=by_check,
                issues=issues,
                updated_at=updated_at,
            )
            for index, item in enumerate(raw_items if isinstance(raw_items, list) else [], start=1)
            if isinstance(item, dict)
        ]
        counts = {
            "planned": sum(1 for item in items if item["status"] == "planned"),
            "in_progress": sum(1 for item in items if item["status"] == "in_progress"),
            "blocked": sum(1 for item in items if item["status"] == "blocked"),
            "completed": sum(1 for item in items if item["status"] == "completed"),
        }
        return {
            "schema": cls.SCHEMA,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "source": source,
            "status": "blocked" if counts["blocked"] else "completed" if items and counts["completed"] == len(items) else "in_progress" if counts["in_progress"] else "planned",
            "counts": counts,
            "items": items,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def blocking_issues(
        cls,
        *,
        run_id: str,
        workspace_id: str,
        implementation_plan: dict[str, Any] | None,
        run_status: str = "pending",
        current_stage: str = "",
        results: list[RunCheckResult | dict[str, Any]] | None = None,
        remaining_issues: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        ledger = cls.build(
            run_id=run_id,
            workspace_id=workspace_id,
            implementation_plan=implementation_plan,
            run_status=run_status,
            current_stage=current_stage,
            results=results,
            remaining_issues=remaining_issues,
        )
        issues: list[dict[str, Any]] = []
        for item in ledger["items"]:
            if item.get("status") == "completed":
                continue
            if item.get("status") == "planned" and run_status not in {"completed", "blocked", "failed"}:
                continue
            issues.append(
                {
                    "kind": "runtime_task_ledger",
                    "check": "runtime_task_ledger",
                    "details": f"Runtime task {item.get('task_id')} is {item.get('status')} and must be completed before finalization.",
                    "task_id": item.get("task_id"),
                    "title": item.get("title"),
                    "role": item.get("role"),
                    "owner": item.get("owner"),
                    "proof_status": item.get("proof_status"),
                    "target_files": item.get("files") or [],
                    "blocking": True,
                    "evidence": {
                        "blocker": item.get("blocker"),
                        "proof": item.get("proof") or {},
                    },
                }
            )
        return issues

    @classmethod
    def _task_from_item(
        cls,
        *,
        item: dict[str, Any],
        index: int,
        run_id: str,
        source: str,
        run_status: str,
        current_stage: str,
        by_check: dict[str, dict[str, Any]],
        issues: list[dict[str, Any]],
        updated_at: str | None,
    ) -> dict[str, Any]:
        task_id = str(item.get("task_id") or item.get("id") or f"{run_id}:ledger:{index}")
        role = str(item.get("role") or "").strip().lower()
        proof_checks = cls._string_list(item.get("proof_checks") or item.get("required_tests"))
        blocker = cls._matching_blocker(task_id=task_id, item=item, issues=issues)
        proof = cls._proof_for_checks(proof_checks, by_check)
        explicit_status = cls._normalize_status(str(item.get("status") or ""))
        status = explicit_status or cls._derived_status(
            run_status=run_status,
            current_stage=current_stage,
            proof_checks=proof_checks,
            proof=proof,
            blocker=blocker,
        )
        title = str(
            item.get("title")
            or item.get("content")
            or item.get("description")
            or item.get("intent")
            or item.get("id")
            or f"Task {index}"
        ).strip()
        return {
            "task_id": task_id,
            "title": title,
            "phase": str(item.get("phase") or item.get("kind") or "product_task"),
            "status": status,
            "owner": str(item.get("owner") or cls._owner_for_item(item, role)),
            "role": role or None,
            "files": cls._string_list(item.get("owned_paths") or item.get("files")),
            "proof": proof,
            "proof_status": cls._proof_status(proof_checks, proof),
            "proof_checks": proof_checks,
            "blocker": blocker,
            "artifact_refs": {"task_ledger": f"task_ledger:{run_id}", "source": source},
            "source": "runtime_task_ledger",
            "updated_at": item.get("updated_at") or updated_at,
        }

    @staticmethod
    def _results_by_name(results: list[RunCheckResult | dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, RunCheckResult):
                payload = result.model_dump(mode="json")
            elif isinstance(result, dict):
                payload = dict(result)
            else:
                continue
            name = str(payload.get("name") or "").strip()
            if name:
                by_name[name] = payload
        return by_name

    @classmethod
    def _derived_status(
        cls,
        *,
        run_status: str,
        current_stage: str,
        proof_checks: list[str],
        proof: dict[str, Any],
        blocker: dict[str, Any] | str | None,
    ) -> str:
        if blocker:
            return "blocked"
        if proof_checks and proof and all(str((proof.get(check) or {}).get("status") or "") == "passed" for check in proof_checks):
            return "completed"
        if run_status in {"completed"} and not proof_checks:
            return "completed"
        if run_status in {"blocked", "failed"}:
            return "blocked"
        if run_status == "running" or current_stage not in {"", "queued"}:
            return "in_progress"
        return "planned"

    @staticmethod
    def _normalize_status(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"pending", "planned"}:
            return "planned"
        if normalized in ACTIVE_STATUSES:
            return "in_progress"
        if normalized in BLOCKED_STATUSES:
            return "blocked"
        if normalized in COMPLETED_STATUSES:
            return "completed"
        return ""

    @staticmethod
    def _proof_for_checks(proof_checks: list[str], by_check: dict[str, dict[str, Any]]) -> dict[str, Any]:
        proof: dict[str, Any] = {}
        for check in proof_checks:
            result = by_check.get(check)
            proof[check] = {
                "status": str((result or {}).get("status") or "missing"),
                "details": (result or {}).get("details"),
            }
        return proof

    @staticmethod
    def _proof_status(proof_checks: list[str], proof: dict[str, Any]) -> str:
        if not proof_checks:
            return "not_required"
        statuses = {str((proof.get(check) or {}).get("status") or "missing") for check in proof_checks}
        if statuses == {"passed"}:
            return "passed"
        if statuses & {"failed", "blocked"}:
            return "failed"
        if "missing" in statuses:
            return "missing"
        return "pending"

    @classmethod
    def _matching_blocker(cls, *, task_id: str, item: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any] | str | None:
        role = str(item.get("role") or "").strip().lower()
        item_id = str(item.get("id") or task_id)
        for issue in issues:
            issue_task = str(issue.get("task_id") or "")
            issue_ledger = str(issue.get("ledger_item_id") or "")
            issue_role = str(issue.get("role") or "").strip().lower()
            if issue_task == task_id or issue_ledger == item_id or (role and issue_role == role and issue.get("kind") in {"product_task_ledger", "runtime_task_ledger"}):
                return issue
        return item.get("blocker") or None

    @staticmethod
    def _owner_for_item(item: dict[str, Any], role: str) -> str:
        if role in ROLE_ORDER:
            return f"{role}_surface_worker"
        kind = str(item.get("kind") or "")
        if "api" in str(item.get("id") or "").lower() or kind == "shared_state":
            return "backend_api_worker"
        return "coordinator"

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
        if isinstance(value, tuple | set):
            return [str(item) for item in value if str(item or "").strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []
