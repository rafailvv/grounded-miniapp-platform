from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager


class AgentWorkerTaskPlanner:
    """Self-contained branch-worker directives for role-separated miniapp work."""

    @staticmethod
    def mode_depth(generation_mode: GenerationMode) -> dict[str, Any]:
        if generation_mode == GenerationMode.FAST:
            return {
                "depth": "compact",
                "passes": ["green_workflow"],
                "design_bar": "clean mobile UI, minimal but usable, with one consistent light neutral visual system across roles",
                "workflow_bar": "one complete prompt-derived flow across all roles",
                "page_bar": "prompt-derived role pages only where they make the mobile workflow clearer",
            }
        if generation_mode == GenerationMode.QUALITY:
            return {
                "depth": "deep",
                "passes": ["green_workflow", "role_consistency", "mobile_design_polish", "fresh_verifier"],
                "design_bar": "modern mobile product UI with polished spacing, states, responsive cards/forms/lists, no horizontal overflow, and consistent light theme across role apps unless explicitly requested otherwise",
                "workflow_bar": "multiple prompt-derived role actions where useful, with create/list/update/summary proof and refresh persistence",
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
            tasks.append(
                {
                    "worker_id": worker_id,
                    "owner_scope": owner_scope,
                    "path_prefixes": path_prefixes,
                    "mode_contract": mode_contract,
                    "prompt": cls._task_prompt(
                        worker_id=worker_id,
                        owner_scope=owner_scope,
                        path_prefixes=path_prefixes,
                        implementation_plan=implementation_plan,
                        mode_contract=mode_contract,
                    ),
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
        owner_scope: str,
        path_prefixes: list[str],
        implementation_plan: dict[str, Any],
        mode_contract: dict[str, Any],
    ) -> str:
        plan_summary = str(implementation_plan.get("principle") or "plan, inspect, patch, verify, repair")
        return (
            f"You are worker `{worker_id}` responsible for {owner_scope}. "
            f"Own only these paths unless the coordinator explicitly asks for shared files: {', '.join(path_prefixes) or 'shared workspace slice'}. "
            f"Use the implementation plan ({plan_summary}) and the user's prompt-derived entities/actions as source of truth. "
            f"Mode depth is {mode_contract.get('depth')}: {mode_contract.get('workflow_bar')}; page organization: {mode_contract.get('page_bar')}; design bar: {mode_contract.get('design_bar')}. "
            "Use implementation_plan.routeable_screen_plan for screen intent guidance; choose concrete route names from the prompt and keep only screens that clarify the mobile workflow. "
            "Read the files you need, patch the smallest complete owned slice, run or request the relevant checks, and report exact changed paths and self-check result. "
            "Do not create templates, seed records, mock records, business-category shortcuts, or cross-role navigation links. "
            "Do not copy another role's primary workflow into this role surface: client UI creates the user-provided state, specialist UI operates on existing shared state, manager UI reviews and controls shared state unless the user's prompt explicitly assigns a different action. "
            "If the prompt assigns shared-state creation to manager or specialist, that role must own the creation form and the client role must load and use that persisted state. "
            "Role UI workers must split long workflows into routeable child pages under their owned static/<role>/ directory rather than stacking every section on index.html. "
            "When a role has child pages, its shared app.js must be view-aware: detect body[data-view] or route, guard optional DOM from other pages, and bind every visible form/button/control on root and child pages. "
            "Do not expose raw API paths, HTTP methods, route slugs, role slugs, or enum codes in normal user-facing UI; use readable labels and keep label/value pairs visually separated."
        )
