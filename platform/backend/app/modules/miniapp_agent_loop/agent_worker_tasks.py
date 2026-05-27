from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.product_workers import canonical_worker_id, ownership_for_worker


class AgentWorkerTaskPlanner:
    """Self-contained branch-worker directives for role-separated miniapp work."""

    @staticmethod
    def mode_depth(generation_mode: GenerationMode) -> dict[str, Any]:
        if generation_mode == GenerationMode.FAST:
            return {
                "depth": "compact",
                "passes": ["green_workflow"],
                "design_bar": "clean mobile UI, minimal but usable, with one consistent light neutral visual system across roles",
                "workflow_bar": "one complete prompt-derived flow across all roles, preserving all explicit role actions/resources from the prompt",
                "page_bar": "at least the product_scale_contract minimum prompt-derived role pages; split broad mobile workflows instead of stacking a long dashboard",
            }
        if generation_mode == GenerationMode.QUALITY:
            return {
                "depth": "deep",
                "passes": ["green_workflow", "role_consistency", "mobile_design_polish", "test_verifier_worker"],
                "design_bar": "modern mobile product UI with polished spacing, states, responsive cards/forms/lists, no horizontal overflow, and consistent light theme across role apps unless explicitly requested otherwise",
                "workflow_bar": "multiple prompt-derived role actions where useful, with persisted write/read/update proof only when the prompt implies it",
                "page_bar": "well-organized prompt-derived role pages with no long dashboard-only scrolls",
            }
        return {
            "depth": "balanced",
            "passes": ["green_workflow", "role_consistency"],
            "design_bar": "noticeably polished mobile UI without excessive pages or token-heavy decoration, using a consistent light role system",
            "workflow_bar": "one primary flow plus one related prompt-derived update/summary flow",
            "page_bar": "enough prompt-derived role pages to keep each mobile workflow focused",
        }

    @classmethod
    def worker_tasks(
        cls,
        *,
        generation_mode: GenerationMode,
        implementation_plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        mode_contract = cls.mode_depth(generation_mode)
        mailbox = AgentWorkerManager.mailbox_for_plan(
            generation_mode=generation_mode,
            implementation_plan=implementation_plan,
        )
        tasks: list[dict[str, Any]] = []
        for worker in mailbox.get("workers") or []:
            if not isinstance(worker, dict):
                continue
            worker_id = str(worker.get("worker") or "")
            owner_scope = str(worker.get("owner_scope") or "")
            path_prefixes = [str(item) for item in worker.get("path_prefixes") or []]
            canonical_id = canonical_worker_id(worker_id)
            ownership = dict(worker.get("ownership") or ownership_for_worker(canonical_id))
            ledger_slice = cls._ledger_slice_for_worker(canonical_id, implementation_plan)
            branch_role = str(worker.get("branch_role") or AgentWorkerManager.branch_role(canonical_id))
            branch_stage = str(worker.get("branch_stage") or AgentWorkerManager.branch_stage(canonical_id))
            branch_policy = str(worker.get("branch_policy") or AgentWorkerManager.branch_policy(canonical_id))
            tasks.append(
                {
                    "worker_id": canonical_id,
                    "worker_type": str(worker.get("worker_type") or canonical_id),
                    "alias_ids": list(worker.get("alias_ids") or []),
                    "lane_id": worker.get("lane_id"),
                    "ownership_kind": worker.get("ownership_kind"),
                    "branch_role": branch_role,
                    "branch_stage": branch_stage,
                    "branch_policy": branch_policy,
                    "isolated_branch": branch_role == "writer",
                    "owner_scope": owner_scope,
                    "path_prefixes": path_prefixes,
                    "ownership": ownership,
                    "product_owner_contract": worker.get("product_owner_contract") or {},
                    "tool_allowlist": list(worker.get("tool_allowlist") or []),
                    "product_task_ledger_slice": ledger_slice,
                    "expected_proof": list(worker.get("expected_proof") or ownership.get("expected_proof") or []),
                    "merge_evidence": list(worker.get("merge_evidence") or ownership.get("merge_evidence") or []),
                    "badge": str(worker.get("badge") or worker.get("status") or "planned"),
                    "mode_contract": mode_contract,
                    "prompt": cls._task_prompt(
                        worker_id=canonical_id,
                        branch_role=branch_role,
                        branch_policy=branch_policy,
                        owner_scope=owner_scope,
                        path_prefixes=path_prefixes,
                        ownership=ownership,
                        ledger_slice=ledger_slice,
                        implementation_plan=implementation_plan,
                        mode_contract=mode_contract,
                    ),
                    "merge_contract": {
                        "manager_decision": "accept owned diff, reject forbidden/conflicting paths, or create repair_worker packet",
                        "accepted_status": "branch_diff_ready",
                        "conflict_status": "needs_repair",
                        "proof_required": list(worker.get("expected_proof") or ownership.get("expected_proof") or []),
                    },
                    "self_check": [
                        "changes stay inside owner path prefixes unless shared runtime files are explicitly required",
                        "role UI actions are connected to JavaScript handlers and backend APIs",
                        "saved state is visible after reload and across the relevant roles",
                        "mobile layout works at 360-430px without horizontal scrolling or critical overlap",
                    ],
                    "repair_policy": "continue the same worker for failures in its owned paths; use a fresh verifier worker only after green checks",
                }
            )
        return tasks

    @staticmethod
    def _task_prompt(
        *,
        worker_id: str,
        branch_role: str,
        branch_policy: str,
        owner_scope: str,
        path_prefixes: list[str],
        ownership: dict[str, Any],
        ledger_slice: list[dict[str, Any]],
        implementation_plan: dict[str, Any],
        mode_contract: dict[str, Any],
    ) -> str:
        plan_summary = str(implementation_plan.get("principle") or "plan, inspect, patch, verify, repair")
        forbidden_paths = ", ".join(str(item) for item in ownership.get("forbidden_paths") or []) or "none"
        expected_proof = ", ".join(str(item) for item in ownership.get("expected_proof") or []) or "owned self-check"
        if branch_role == "verifier":
            return (
                f"You are verifier worker `{worker_id}` responsible for {owner_scope}. "
                "Run after backend, role UI, and generated test worker branches have merged and checks are green. "
                f"Branch policy is {branch_policy}: do not write source files; produce blocker findings, mobile/browser proof, and repair packets only. "
                f"Expected proof: {expected_proof}. Product task ledger slice for this verifier: {ledger_slice or 'none'}. "
                "Look for bugs, missing tests, broken role workflow, seeded/mock data, mobile overflow, and weak persistence. "
                "Return concrete blocker findings with target files and exact proof gaps; do not provide prose-only review."
            )
        return (
            f"You are worker `{worker_id}` responsible for {owner_scope}. "
            f"Branch role is {branch_role}; branch policy is {branch_policy}. "
            f"Own only these paths unless the coordinator explicitly asks for shared files: {', '.join(path_prefixes) or 'shared workspace slice'}. "
            f"Forbidden paths for this worker: {forbidden_paths}. Expected proof: {expected_proof}. "
            f"Use the implementation plan ({plan_summary}) and the user's prompt-derived entities/actions as source of truth. "
            f"Product task ledger slice for this worker: {ledger_slice or 'none'}. Complete these ledger items with product source and proof before reporting done. "
            f"Mode depth is {mode_contract.get('depth')}: {mode_contract.get('workflow_bar')}; page organization: {mode_contract.get('page_bar')}; design bar: {mode_contract.get('design_bar')}. "
            "Use implementation_plan.product_scale_contract.min_role_routes and implementation_plan.routeable_screen_plan for screen intent guidance; choose concrete route names from the prompt and satisfy the prompt-derived minimum routeable pages. "
            "miniapp/app/generated/miniapp_contract.json is prompt-analysis metadata only; do not treat it as a fixed product schema, route template, or API scaffold. "
            "Choose field keys and API routes from the prompt and keep them consistent across backend, JS payloads, renderers, and tests. "
            "Read the files you need, patch the smallest complete owned slice, run or request the relevant checks, and report exact changed paths and self-check result. "
            "Do not create templates, seed records, mock records, fixed product-category shortcuts, or cross-role navigation links. "
            "Do not copy another role's primary workflow into this role surface: prompt_hints.role_state_contract decides source roles, update roles, and observer roles. "
            "Do not force any workflow semantics unless the user's prompt explicitly assigns them. "
            "If the prompt assigns shared-state creation to manager or specialist, that role must own the creation form and the client role must load or use that persisted state without duplicate source controls. "
            "Role UI workers must split long workflows into routeable child pages under their owned static/<role>/ directory rather than stacking every section on index.html. "
            "When a role has child pages, its shared app.js must be view-aware: detect body[data-view] or route, guard optional DOM from other pages, and bind every visible form/button/control on root and child pages. "
            "Do not expose raw API paths, HTTP methods, route slugs, role slugs, or enum codes in normal user-facing UI; use readable labels and keep label/value pairs visually separated."
        )

    @staticmethod
    def _ledger_slice_for_worker(worker_id: str, implementation_plan: dict[str, Any]) -> list[dict[str, Any]]:
        ledger = implementation_plan.get("product_task_ledger") if isinstance(implementation_plan.get("product_task_ledger"), list) else []
        if not ledger:
            return []
        role_by_worker = {
            "client_surface_worker": "client",
            "specialist_surface_worker": "specialist",
            "manager_surface_worker": "manager",
        }
        role = role_by_worker.get(worker_id)
        selected: list[dict[str, Any]] = []
        for item in ledger:
            if not isinstance(item, dict):
                continue
            item_role = str(item.get("role") or "").strip().lower()
            item_kind = str(item.get("kind") or "").strip().lower()
            if role and item_role == role:
                selected.append(item)
            elif worker_id == "backend_api_worker" and item_kind == "backend":
                selected.append(item)
            elif worker_id == "test_verifier_worker" and item_kind == "proof":
                selected.append(item)
        return selected[:4]
