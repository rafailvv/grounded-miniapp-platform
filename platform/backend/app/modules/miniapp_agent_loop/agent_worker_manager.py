from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.product_workers import (
    PRODUCT_WORKERS,
    canonical_worker_id,
    ownership_for_worker,
    path_is_allowed,
    role_for_worker,
)


@dataclass(frozen=True)
class AgentWorkerScope:
    worker: str
    owner_scope: str
    path_prefixes: tuple[str, ...]

    def owns(self, path: str) -> bool:
        return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in self.path_prefixes)


class AgentWorkerManager:
    """Product-aware worker ownership and merge guard for role-separated draft work."""

    SCOPES: tuple[AgentWorkerScope, ...] = (
        AgentWorkerScope("backend_api_worker", "backend/API", ("miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py")),
        AgentWorkerScope("client_surface_worker", "client UI", ("miniapp/app/static/client",)),
        AgentWorkerScope("specialist_surface_worker", "specialist UI", ("miniapp/app/static/specialist",)),
        AgentWorkerScope("manager_surface_worker", "manager UI", ("miniapp/app/static/manager",)),
        AgentWorkerScope("test_verifier_worker", "generated tests", ("miniapp/tests",)),
    )

    @classmethod
    def owner_for_path(cls, path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/")
        if normalized in {"miniapp/app/routes/role_pages.py", "miniapp/app/routes/role_routes.py"}:
            return "platform_shell"
        for scope in cls.SCOPES:
            if scope.owns(normalized):
                return scope.worker
        if normalized.startswith("miniapp/app/static/shared") or normalized.startswith("miniapp/app/generated"):
            return "shared_runtime"
        return "shared"

    @classmethod
    def mailbox_for_plan(
        cls,
        *,
        generation_mode: GenerationMode,
        implementation_plan: dict[str, Any],
    ) -> dict[str, object]:
        has_contract = cls._has_product_contract(implementation_plan)
        flag_value = os.getenv("GROUNDED_ENABLE_WORKER_BRANCHES", "").strip().lower()
        flag_disabled = flag_value in {"0", "false", "no", "off"}
        flag_enabled = flag_value in {"1", "true", "yes", "on"}
        enabled = (
            (flag_enabled or not flag_disabled)
            and generation_mode == GenerationMode.QUALITY
            and has_contract
        )
        disabled_reason = ""
        if not enabled:
            if flag_disabled:
                disabled_reason = "isolated worker branches are disabled by GROUNDED_ENABLE_WORKER_BRANCHES=0"
            elif generation_mode != GenerationMode.QUALITY:
                disabled_reason = "isolated worker branches are available only for quality generation mode"
            elif not has_contract:
                disabled_reason = "isolated worker branches require an acceptance/product contract before worker planning"
            else:
                disabled_reason = "isolated worker branches are unavailable for this run"
        prompt_contract = implementation_plan.get("prompt_contract_v1") if isinstance(implementation_plan, dict) else {}
        materialized_tests = bool(isinstance(prompt_contract, dict) and prompt_contract.get("materialized_tests"))
        workers = [
            cls._mailbox_worker_payload(
                worker_id=role.worker_id,
                enabled=enabled,
                disabled_reason=disabled_reason,
                status="planned" if enabled else "available_disabled",
            )
            for role in PRODUCT_WORKERS
            if role.worker_id != "repair_worker"
            and not (materialized_tests and role.worker_id == "test_verifier_worker")
            and not (generation_mode != GenerationMode.QUALITY and role.worker_id == "mobile_polish_worker")
        ]
        return {
            "schema": "grounded.product_worker_mailbox.v1",
            "branch_schema": "grounded.worker_branch_plan.v2",
            "enabled": enabled,
            "disabled_reason": disabled_reason,
            "mode": str(getattr(generation_mode, "value", generation_mode) or ""),
            "workers": workers,
            "worker_groups": {
                "writer": [
                    worker["worker_id"]
                    for worker in workers
                    if worker.get("branch_role") == "writer"
                ],
                "verifier": [
                    worker["worker_id"]
                    for worker in workers
                    if worker.get("branch_role") == "verifier"
                ],
                "repair": ["repair_worker"],
            },
            "execution_stages": [
                {
                    "stage": "backend_contract",
                    "workers": ["backend_api_worker"],
                    "mode": "serial_first",
                    "reason": "Role UI and tests fork from the backend/API contract.",
                },
                {
                    "stage": "role_ui_and_tests",
                    "workers": [
                        "client_surface_worker",
                        "specialist_surface_worker",
                        "manager_surface_worker",
                        "test_verifier_worker",
                    ],
                    "mode": "parallel_isolated_drafts",
                    "reason": "Owned writer branches run independently after backend contract merge.",
                },
                {
                    "stage": "guardian_verifier",
                    "workers": ["mobile_polish_worker"],
                    "mode": "read_only_after_green_checks",
                    "reason": "Verifier reviews the merged candidate; it does not write source.",
                },
                {
                    "stage": "conflict_repair",
                    "workers": ["repair_worker"],
                    "mode": "targeted_serial_repair",
                    "reason": "Merge conflicts and rejected worker diffs become repair cases.",
                },
            ],
            "plan_entities": list(implementation_plan.get("primary_entities") or [])[:8],
            "contract_ready": has_contract,
            "worker_prompt_contract": (
                "Each worker prompt must be self-contained: owner scope, path prefixes, exact product plan slice, "
                "expected self-check, worker memory snapshot, output artifact, and repair instruction. Continue the same worker for failures in owned paths; "
                "use a fresh verifier/polish worker only after green checks."
            ),
            "write_coordination": (
                "parallel_owned_branches"
                if enabled
                else "serial_contract_runtime_writes"
            ),
            "ownership_locks": cls.ownership_locks(),
            "merge_policy": "manager accepts only non-conflicting owned diffs with required proof; conflicts become repair_worker packets",
        }

    @classmethod
    def _mailbox_worker_payload(cls, *, worker_id: str, enabled: bool, disabled_reason: str, status: str) -> dict[str, Any]:
        role = role_for_worker(worker_id)
        ownership = ownership_for_worker(worker_id)
        canonical = canonical_worker_id(worker_id)
        return {
            "worker": canonical,
            "worker_id": canonical,
            "worker_type": canonical,
            "alias_ids": [],
            "branch_role": cls.branch_role(canonical),
            "branch_stage": cls.branch_stage(canonical),
            "branch_policy": cls.branch_policy(canonical),
            "owner_scope": role.owner_scope if role else canonical,
            "path_prefixes": list(ownership.get("allowed_paths") or []),
            "ownership": ownership,
            "status": status,
            "badge": status,
            "disabled_reason": disabled_reason if not enabled else "",
            "expected_proof": list(ownership.get("expected_proof") or []),
        }

    @staticmethod
    def branch_role(worker_id: str) -> str:
        canonical = canonical_worker_id(worker_id)
        if canonical == "mobile_polish_worker":
            return "verifier"
        if canonical == "repair_worker":
            return "repair"
        return "writer"

    @staticmethod
    def branch_stage(worker_id: str) -> str:
        canonical = canonical_worker_id(worker_id)
        if canonical == "backend_api_worker":
            return "backend_contract"
        if canonical in {"client_surface_worker", "specialist_surface_worker", "manager_surface_worker", "test_verifier_worker"}:
            return "role_ui_and_tests"
        if canonical == "mobile_polish_worker":
            return "guardian_verifier"
        if canonical == "repair_worker":
            return "conflict_repair"
        return "shared"

    @staticmethod
    def branch_policy(worker_id: str) -> str:
        role = AgentWorkerManager.branch_role(worker_id)
        if role == "verifier":
            return "read_only_after_green_checks"
        if role == "repair":
            return "targeted_serial_repair"
        return "isolated_draft_writer"

    @classmethod
    def ownership_locks(cls) -> list[dict[str, object]]:
        locks = [
            {
                "lock_id": role.worker_id,
                "worker": role.worker_id,
                "worker_type": role.worker_type,
                "alias_ids": [],
                "owner_scope": role.owner_scope,
                "path_prefixes": list(role.allowed_paths),
                "forbidden_paths": list(role.forbidden_paths),
                "exclusive_write": True,
            }
            for role in PRODUCT_WORKERS
            if role.writes and role.worker_id != "repair_worker"
        ]
        locks.append(
            {
                "lock_id": "read_only_verification",
                "worker": "mobile_polish_worker",
                "owner_scope": "read-only browser proof, mobile layout, and guardian review artifacts",
                "path_prefixes": ["verification", "browser_proof", "reports"],
                "exclusive_write": False,
            }
        )
        return locks

    @classmethod
    def validate_non_conflicting(cls, file_changes: list[DraftAction]) -> dict[str, object]:
        by_path: dict[str, list[dict[str, str]]] = {}
        for action in file_changes:
            path = str(action.file_path or "").strip()
            raw_worker = cls._worker_from_action(action)
            owner = canonical_worker_id(raw_worker or cls.owner_for_path(path))
            if owner == "coordinator":
                owner = cls.owner_for_path(path)
            allowed = path_is_allowed(owner, path) or owner in {"test_verifier_worker"} and cls.owner_for_path(path) == "test_verifier_worker"
            by_path.setdefault(path, []).append(
                {
                    "owner": owner,
                    "change_type": str(action.operation),
                    "reason": str(action.reason or "")[:240],
                    "allowed": str(bool(allowed)).lower(),
                }
            )
        conflicts = [
            {
                "path": path,
                "edits": edits,
                "owners": sorted({edit["owner"] for edit in edits}),
            }
            for path, edits in by_path.items()
            if len(edits) > 1
        ]
        forbidden = [
            {"path": path, "edits": edits, "owners": sorted({edit["owner"] for edit in edits})}
            for path, edits in by_path.items()
            if any(edit.get("allowed") == "false" for edit in edits)
        ]
        return {
            "ok": not conflicts and not forbidden,
            "conflicts": conflicts,
            "forbidden": forbidden,
            "owners": {path: edits[0]["owner"] for path, edits in by_path.items() if edits},
            "ownership_locks": cls.ownership_locks(),
        }

    @staticmethod
    def _has_product_contract(implementation_plan: dict[str, Any]) -> bool:
        if not isinstance(implementation_plan, dict):
            return False
        prompt_contract = implementation_plan.get("prompt_contract_v1")
        if isinstance(prompt_contract, dict) and (prompt_contract.get("entities") or prompt_contract.get("flows") or prompt_contract.get("required")):
            return True
        return bool(
            implementation_plan.get("primary_entities")
            or implementation_plan.get("role_state_contract")
            or implementation_plan.get("routeable_screen_plan")
        )

    @staticmethod
    def _worker_from_action(action: DraftAction) -> str:
        reason = str(getattr(action, "reason", "") or "")
        match = re.match(r"^\[([^\]]+)\]", reason)
        return match.group(1) if match else ""
