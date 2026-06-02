from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftAction
from app.services.generation_enhancements import SubagentForkContract
from app.modules.miniapp_agent_loop.product_workers import (
    PRODUCT_WORKERS,
    canonical_worker_id,
    ownership_for_worker,
    path_is_allowed,
    product_owner_contract,
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
            and generation_mode in {GenerationMode.QUALITY, GenerationMode.PRODUCTION}
            and has_contract
        )
        disabled_reason = ""
        if not enabled:
            if flag_disabled:
                disabled_reason = "isolated worker branches are disabled by GROUNDED_ENABLE_WORKER_BRANCHES=0"
            elif generation_mode not in {GenerationMode.QUALITY, GenerationMode.PRODUCTION}:
                disabled_reason = "isolated worker branches are available only for quality/production generation mode"
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
            and not (generation_mode not in {GenerationMode.QUALITY, GenerationMode.PRODUCTION} and role.worker_id == "mobile_polish_worker")
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
            "write_scope_report": cls.write_scope_report(workers),
            "subagent_contract": SubagentForkContract.build(
                implementation_plan=implementation_plan,
                generation_mode=str(getattr(generation_mode, "value", generation_mode) or ""),
            ),
            "merge_policy": "manager accepts only non-conflicting owned diffs with required proof; conflicts become repair_worker packets",
        }

    @classmethod
    def _mailbox_worker_payload(cls, *, worker_id: str, enabled: bool, disabled_reason: str, status: str) -> dict[str, Any]:
        role = role_for_worker(worker_id)
        ownership = ownership_for_worker(worker_id)
        owner_contract = product_owner_contract(worker_id)
        canonical = canonical_worker_id(worker_id)
        return {
            "worker": canonical,
            "worker_id": canonical,
            "worker_type": canonical,
            "alias_ids": list(owner_contract.get("alias_ids") or []),
            "lane_id": owner_contract.get("lane_id"),
            "ownership_kind": owner_contract.get("ownership_kind"),
            "branch_role": cls.branch_role(canonical),
            "branch_stage": cls.branch_stage(canonical),
            "branch_policy": cls.branch_policy(canonical),
            "owner_scope": role.owner_scope if role else canonical,
            "path_prefixes": list(ownership.get("allowed_paths") or []),
            "ownership": ownership,
            "product_owner_contract": owner_contract,
            "tool_allowlist": SubagentForkContract.tool_allowlist_for_worker(canonical),
            "status": status,
            "badge": status,
            "disabled_reason": disabled_reason if not enabled else "",
            "expected_proof": list(ownership.get("expected_proof") or []),
            "merge_evidence": list(ownership.get("merge_evidence") or []),
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
    def write_scope_report(cls, worker_specs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        locks = cls._write_locks_for_specs(worker_specs)
        overlaps: list[dict[str, Any]] = []
        for index, left in enumerate(locks):
            for right in locks[index + 1 :]:
                shared = cls._overlapping_prefixes(
                    [str(item) for item in left.get("path_prefixes") or []],
                    [str(item) for item in right.get("path_prefixes") or []],
                )
                if shared:
                    overlaps.append(
                        {
                            "left_worker": left.get("worker"),
                            "right_worker": right.get("worker"),
                            "overlapping_prefixes": shared,
                        }
                    )
        return {
            "schema": "grounded.worker_write_scope_report.v1",
            "status": "passed" if not overlaps else "conflict",
            "disjoint": not overlaps,
            "locks": locks,
            "overlaps": overlaps,
            "exclusive_worker_count": len(locks),
            "policy": "exclusive writer scopes must not overlap; verifier scopes are read-only and excluded from merge ownership locks",
        }

    @classmethod
    def conflict_report(cls, file_changes: list[DraftAction]) -> dict[str, Any]:
        ownership = cls.validate_non_conflicting(file_changes, include_conflict_report=False)
        conflict_paths = {
            str(item.get("path") or "")
            for item in ownership.get("conflicts", [])
            if isinstance(item, dict)
        }
        forbidden_paths = {
            str(item.get("path") or "")
            for item in ownership.get("forbidden", [])
            if isinstance(item, dict)
        }
        blocked_paths = sorted(path for path in {*conflict_paths, *forbidden_paths} if path)
        changed_paths = [str(change.file_path or "").strip() for change in file_changes if str(change.file_path or "").strip()]
        per_worker: dict[str, dict[str, Any]] = {}
        for path, owner in (ownership.get("owners") or {}).items():
            worker = str(owner or "shared")
            entry = per_worker.setdefault(worker, {"worker_id": worker, "paths": [], "blocked_paths": [], "mergeable_paths": []})
            entry["paths"].append(path)
            if path in blocked_paths:
                entry["blocked_paths"].append(path)
            else:
                entry["mergeable_paths"].append(path)
        return {
            "schema": "grounded.worker_conflict_report.v1",
            "status": "passed" if not blocked_paths else "conflict",
            "conflict_count": len(conflict_paths),
            "forbidden_count": len(forbidden_paths),
            "blocked_paths": blocked_paths,
            "mergeable_paths": [path for path in changed_paths if path not in blocked_paths],
            "per_worker": list(per_worker.values()),
            "conflicts": ownership.get("conflicts") or [],
            "forbidden": ownership.get("forbidden") or [],
        }

    @classmethod
    def merge_evidence_packet(
        cls,
        *,
        worker_id: str,
        changed_files: list[str],
        decision: str,
        output_ref: str | None = None,
        apply_result: dict[str, Any] | None = None,
        proof_refs: list[str] | None = None,
        blocked_paths: list[str] | None = None,
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        canonical = canonical_worker_id(worker_id)
        contract = product_owner_contract(canonical)
        changed = [str(path) for path in changed_files if str(path or "").strip()]
        blocked = [str(path) for path in blocked_paths or [] if str(path or "").strip()]
        proof = [str(ref) for ref in proof_refs or [] if str(ref or "").strip()]
        apply_status = str((apply_result or {}).get("status") or "")
        owned_paths_only = all(path_is_allowed(canonical, path) for path in changed) if changed else True
        structural_checks = [
            {
                "check": "owned_paths_only",
                "status": "passed" if owned_paths_only and not blocked else "failed",
                "evidence": {"changed_files": changed, "blocked_paths": blocked},
            },
            {
                "check": "apply_result",
                "status": "passed" if apply_status in {"applied", "skipped", ""} and decision in {"accepted", "deferred"} else "failed" if decision == "accepted" else "skipped",
                "evidence": {"apply_status": apply_status or "not_yet_applied"},
            },
            {
                "check": "worker_output_ref",
                "status": "passed" if output_ref else "missing",
                "evidence": {"output_ref": output_ref},
            },
        ]
        runtime_proof_status = "passed" if proof else "planned_after_merge" if decision in {"accepted", "deferred"} else "blocked"
        structural_ok = all(item["status"] in {"passed", "skipped"} for item in structural_checks)
        status = "passed" if structural_ok and decision in {"accepted", "deferred"} else "failed" if decision in {"rejected", "needs_repair"} or not structural_ok else "pending"
        return {
            "schema": "grounded.worker_merge_evidence.v1",
            "worker_id": canonical,
            "lane_id": contract.get("lane_id"),
            "ownership_kind": contract.get("ownership_kind"),
            "decision": decision,
            "status": status,
            "structural_checks": structural_checks,
            "runtime_proof": {
                "status": runtime_proof_status,
                "expected": list(contract.get("expected_proof") or []),
                "proof_refs": proof,
                "policy": "runtime/browser proof may be planned at merge time, but must be present before final readiness",
            },
            "required_evidence": list(contract.get("merge_evidence") or []),
            "product_owner_contract": contract,
            "failure_reason": failure_reason or "",
        }

    @classmethod
    def validate_non_conflicting(cls, file_changes: list[DraftAction], *, include_conflict_report: bool = True) -> dict[str, object]:
        ownership = cls._ownership_for_changes(file_changes)
        conflicts = [
            {
                "path": path,
                "edits": edits,
                "owners": sorted({edit["owner"] for edit in edits}),
            }
            for path, edits in ownership.items()
            if len(edits) > 1
        ]
        forbidden = [
            {"path": path, "edits": edits, "owners": sorted({edit["owner"] for edit in edits})}
            for path, edits in ownership.items()
            if any(edit.get("allowed") == "false" for edit in edits)
        ]
        payload: dict[str, object] = {
            "ok": not conflicts and not forbidden,
            "conflicts": conflicts,
            "forbidden": forbidden,
            "owners": {path: edits[0]["owner"] for path, edits in ownership.items() if edits},
            "ownership_locks": cls.ownership_locks(),
            "write_scope_report": cls.write_scope_report(),
        }
        if include_conflict_report:
            blocked_paths = {
                str(item.get("path") or "")
                for item in [*conflicts, *forbidden]
                if isinstance(item, dict)
            }
            payload["conflict_report"] = {
                "schema": "grounded.worker_conflict_report.v1",
                "status": "passed" if not blocked_paths else "conflict",
                "conflict_count": len(conflicts),
                "forbidden_count": len(forbidden),
                "blocked_paths": sorted(path for path in blocked_paths if path),
                "mergeable_paths": [path for path in ownership if path not in blocked_paths],
                "conflicts": conflicts,
                "forbidden": forbidden,
            }
        return payload

    @classmethod
    def _ownership_for_changes(cls, file_changes: list[DraftAction]) -> dict[str, list[dict[str, str]]]:
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
        return by_path

    @classmethod
    def _write_locks_for_specs(cls, worker_specs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if worker_specs is None:
            return [dict(item) for item in cls.ownership_locks() if bool(item.get("exclusive_write"))]
        locks: list[dict[str, Any]] = []
        for spec in worker_specs:
            if not isinstance(spec, dict):
                continue
            worker_id = canonical_worker_id(str(spec.get("worker_id") or spec.get("worker") or ""))
            if not worker_id:
                continue
            branch_role = str(spec.get("branch_role") or cls.branch_role(worker_id))
            ownership = spec.get("ownership") if isinstance(spec.get("ownership"), dict) else ownership_for_worker(worker_id)
            exclusive = bool(ownership.get("exclusive_write", branch_role == "writer"))
            if branch_role != "writer" or not exclusive:
                continue
            locks.append(
                {
                    "lock_id": worker_id,
                    "worker": worker_id,
                    "worker_type": worker_id,
                    "owner_scope": str(spec.get("owner_scope") or worker_id),
                    "path_prefixes": list(ownership.get("allowed_paths") or []),
                    "forbidden_paths": list(ownership.get("forbidden_paths") or []),
                    "exclusive_write": True,
                }
            )
        return locks

    @staticmethod
    def _overlapping_prefixes(left: list[str], right: list[str]) -> list[dict[str, str]]:
        overlaps: list[dict[str, str]] = []
        for left_prefix in left:
            normalized_left = left_prefix.rstrip("/")
            for right_prefix in right:
                normalized_right = right_prefix.rstrip("/")
                if not normalized_left or not normalized_right:
                    continue
                if (
                    normalized_left == normalized_right
                    or normalized_left.startswith(normalized_right + "/")
                    or normalized_right.startswith(normalized_left + "/")
                ):
                    overlaps.append({"left": normalized_left, "right": normalized_right})
        return overlaps

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
