from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftAction


@dataclass(frozen=True)
class AgentWorkerScope:
    worker: str
    owner_scope: str
    path_prefixes: tuple[str, ...]

    def owns(self, path: str) -> bool:
        return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in self.path_prefixes)


class AgentWorkerManager:
    """Generic worker ownership and merge guard for role-separated draft work."""

    SCOPES: tuple[AgentWorkerScope, ...] = (
        AgentWorkerScope("backend_api", "backend/API", ("miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/app/main.py")),
        AgentWorkerScope("client_ui", "client UI", ("miniapp/app/static/client",)),
        AgentWorkerScope("specialist_ui", "specialist UI", ("miniapp/app/static/specialist",)),
        AgentWorkerScope("manager_ui", "manager UI", ("miniapp/app/static/manager",)),
        AgentWorkerScope("generated_tests", "generated tests", ("miniapp/tests",)),
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
        enabled = (
            os.getenv("GROUNDED_ENABLE_WORKER_BRANCHES", "").strip().lower() in {"1", "true", "yes", "on"}
            and generation_mode == GenerationMode.QUALITY
        )
        disabled_reason = "" if enabled else "isolated worker branches are gated off unless GROUNDED_ENABLE_WORKER_BRANCHES=1 and generation mode is quality"
        prompt_contract = implementation_plan.get("prompt_contract_v1") if isinstance(implementation_plan, dict) else {}
        materialized_tests = bool(isinstance(prompt_contract, dict) and prompt_contract.get("materialized_tests"))
        workers = [
            {
                "worker": scope.worker,
                "owner_scope": scope.owner_scope,
                "path_prefixes": list(scope.path_prefixes),
                    "status": "pending" if enabled else "available_disabled",
                    "disabled_reason": disabled_reason if not enabled else "",
            }
            for scope in cls.SCOPES
            if not (materialized_tests and scope.worker == "generated_tests")
        ]
        if generation_mode == GenerationMode.QUALITY:
            workers.append(
                {
                    "worker": "design_verifier",
                    "owner_scope": "mobile design and verification",
                    "path_prefixes": ["miniapp/app/static", "miniapp/tests"],
                    "status": "pending" if enabled else "available_disabled",
                    "disabled_reason": disabled_reason if not enabled else "",
                }
            )
        return {
            "enabled": enabled,
            "disabled_reason": disabled_reason,
            "mode": str(getattr(generation_mode, "value", generation_mode) or ""),
            "workers": workers,
            "plan_entities": list(implementation_plan.get("primary_entities") or [])[:8],
            "worker_prompt_contract": (
                "Each worker prompt must be self-contained: owner scope, path prefixes, exact product plan slice, "
                "expected self-check, and repair instruction. Continue the same worker for failures in owned paths; "
                "use a fresh verifier only after green checks."
            ),
            "write_coordination": (
                "parallel_owned_branches"
                if enabled
                else "serial_contract_runtime_writes"
            ),
            "ownership_locks": cls.ownership_locks(),
            "merge_policy": "accept non-conflicting owned diffs; return conflicts to the owning worker as a repair packet",
        }

    @classmethod
    def ownership_locks(cls) -> list[dict[str, object]]:
        locks = [
            {
                "lock_id": scope.worker,
                "worker": scope.worker,
                "owner_scope": scope.owner_scope,
                "path_prefixes": list(scope.path_prefixes),
                "exclusive_write": True,
            }
            for scope in cls.SCOPES
        ]
        locks.append(
            {
                "lock_id": "verifier",
                "worker": "verifier",
                "owner_scope": "checks, browser proof, and review artifacts",
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
            owner = cls.owner_for_path(path)
            by_path.setdefault(path, []).append(
                {
                    "owner": owner,
                    "change_type": str(action.operation),
                    "reason": str(action.reason or "")[:240],
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
        return {
            "ok": not conflicts,
            "conflicts": conflicts,
            "owners": {path: edits[0]["owner"] for path, edits in by_path.items() if edits},
            "ownership_locks": cls.ownership_locks(),
        }
