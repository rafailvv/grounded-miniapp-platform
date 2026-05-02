from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")

WORKFLOW_EDIT_MARKERS = (
    "after adding",
    "button",
    "does not load",
    "doesn't load",
    "form",
    "list",
    "refresh",
    "should appear",
    "не подгружается",
    "не работает",
    "кнопк",
    "список",
    "форма",
    "после добавления",
    "должно появляться",
    "появлялось",
    "сохраняться",
    "срочн",
    "во всех трех",
    "во всех трёх",
)

def prompt_resource_candidates(prompt: str, *, limit: int = 3) -> list[str]:
    """Compatibility shim for older callers.

    The platform must not derive API/resource/page names from prompt words. A
    Claude/Codex-style code agent should infer concrete entities in its own
    implementation plan and then validators verify the actual code it produced.
    """
    del prompt, limit
    return []

def normalized_generation_mode(generation_mode: GenerationMode | str | None) -> str:
    return str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()


def is_behavior_workflow_prompt(prompt: str) -> bool:
    text = str(prompt or "").strip().lower()
    if not text:
        return False
    strong_markers = (
        "after adding",
        "does not load",
        "doesn't load",
        "не подгружается",
        "не работает",
        "не нажим",
        "после добавления",
        "должно появляться",
        "появлялось",
    )
    if any(marker in text for marker in strong_markers):
        return True
    broad_flow_terms = ("button", "form", "list", "record", "кнопк", "форма", "список")
    if any(marker in text for marker in broad_flow_terms) and any(
        marker in text
        for marker in (
            "does not",
            "doesn't",
            "not work",
            "не работает",
            "не нажим",
            "после",
            "появ",
            "refresh",
            "подгруж",
        )
    ):
        return True
    role_markers = (
        "client",
        "specialist",
        "manager",
        "worker",
        "клиент",
        "исполнитель",
        "специалист",
        "менеджер",
    )
    cross_role_markers = (
        "all three",
        "three parts",
        "across roles",
        "visible in",
        "во всех трех",
        "во всех трёх",
        "в трех частях",
        "в трёх частях",
        "видна во",
        "видно во",
        "видит",
    )
    add_or_extend_markers = (
        "add",
        "extend",
        "include",
        "добав",
        "расшир",
        "выбирает",
        "фильтр",
        "сводк",
    )
    persistence_markers = (
        "persist",
        "save",
        "refresh",
        "reload",
        "сохраня",
        "после обновления",
        "после перезагруз",
    )
    role_count = sum(1 for marker in role_markers if marker in text)
    return (
        any(marker in text for marker in add_or_extend_markers)
        and (role_count >= 2 or any(marker in text for marker in cross_role_markers))
        and any(marker in text for marker in persistence_markers + cross_role_markers)
    )


def build_acceptance_contract(
    *,
    prompt: str,
    intent: str | None,
    generation_mode: GenerationMode | str | None,
    focused_edit_kind: str = "",
) -> dict[str, Any]:
    intent_value = str(intent or "").strip().lower()
    mode_value = normalized_generation_mode(generation_mode)
    workflow_kind = str(focused_edit_kind or "").strip().lower()
    requires_contract = intent_value == "create" or workflow_kind == "behavior_workflow_edit"
    if not requires_contract:
        return {
            "required": False,
            "intent": intent_value,
            "generation_mode": mode_value,
            "workflow_kind": workflow_kind or "standard",
            "roles": list(ROLE_ORDER),
            "flows": [],
            "test_requirements": [],
        }

    flows: list[dict[str, Any]] = [
        {
            "id": "role_shared_persistence",
            "title": "Shared persisted role workflow",
            "roles": list(ROLE_ORDER),
            "requirements": [
                "Client role can submit a real form/action through a POST-capable backend API.",
                "Specialist role can see saved client-created state and perform the prompt-derived operational action.",
                "Manager role can see persisted shared state, summary metrics, and an oversight action.",
                "Saved data remains visible after a reload through GET APIs; app source starts with no seed/mock records.",
            ],
            "required_tests": [
                "Python generated test verifies empty GET, POST create, persisted GET, role update, and persisted update.",
                "JS generated test verifies role pages, role-specific controls, frontend API usage, and handler wiring.",
            ],
        }
    ]
    if mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
        flows.append(
            {
                "id": "related_resource_workflow",
                "title": "Related status, summary, and operational updates",
                "roles": list(ROLE_ORDER),
                "requirements": [
                    "The LLM-selected shared state supports create, process/update, review, and summary workflows.",
                    "Role pages expose different actions for creating, processing, and reviewing saved state.",
                    "Specialist or manager updates are visible to the other roles through later GET requests.",
                ],
                "required_tests": [
                    "Generated tests cover the primary create/list/update flow and one related prompt-derived update or summary flow.",
                ],
            }
        )
    return {
        "required": True,
        "intent": intent_value,
        "generation_mode": mode_value,
        "workflow_kind": workflow_kind or ("create" if intent_value == "create" else "behavior_workflow_edit"),
        "roles": list(ROLE_ORDER),
        "features": {
            "cross_role_persistence": True,
            "refresh_persistence": True,
            "workflow_update": True,
            "resource_count": 0,
            "prompt_resource_candidates": [],
            "resource_strategy": "llm_plan_owned_no_platform_resource_template",
            "api_discovery_required": True,
        },
        "required_endpoints": [],
        "required_controls": [
            {"role": "client", "action": "submit the user-facing prompt-derived create/select/save flow through UI and POST-capable API"},
            {"role": "specialist", "action": "perform the prompt-derived operational action through POST/PATCH when the workflow needs persisted updates"},
            {"role": "manager", "action": "review shared state and trigger the prompt-derived oversight/control action"},
        ],
        "flows": flows,
        "test_requirements": [item for flow in flows for item in flow.get("required_tests", [])],
    }


def build_implementation_plan(
    *,
    prompt: str,
    intent: str | None,
    generation_mode: GenerationMode | str | None,
    acceptance_contract: dict[str, Any] | None,
    orchestration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a generic agent plan from prompt-derived contract data.

    The plan intentionally avoids fixed-category templates. It describes the
    workflow proof the agent must satisfy, while the concrete nouns/fields are
    still owned by the LLM-generated implementation.
    """
    intent_value = str(intent or "").strip().lower()
    mode_value = normalized_generation_mode(generation_mode)
    contract = dict(acceptance_contract or {})
    resources = []
    for endpoint in contract.get("required_endpoints") or []:
        if isinstance(endpoint, dict) and str(endpoint.get("resource") or "").strip():
            resources.append(str(endpoint.get("resource")).strip())
    resources = list(dict.fromkeys(resources))
    required_controls = [
        dict(item)
        for item in (contract.get("required_controls") or [])
        if isinstance(item, dict)
    ]
    return {
        "version": 1,
        "required": bool(contract.get("required")),
        "intent": intent_value,
        "generation_mode": mode_value,
        "principle": "plan_inspect_build_verify_repair_final_browser_proof",
        "roles": list(contract.get("roles") or ROLE_ORDER),
        "primary_entities": resources,
        "role_actions": {
            "client": "perform the prompt-derived user create, submit, select, or save action through UI and POST API",
            "specialist": "perform the prompt-derived operational processing/update action through UI and update API",
            "manager": "review or control shared state and summary information through manager UI",
        },
        "api_contract": {
            "required_endpoints": list(contract.get("required_endpoints") or []),
            "must_persist": True,
            "must_support_update": bool((contract.get("features") or {}).get("workflow_update", True)),
        },
        "ui_contract": {
            "required_controls": required_controls,
            "three_separate_role_apps": True,
            "no_cross_role_navigation": True,
            "role_specific_actions": True,
        },
        "test_contract": {
            "generated_tests_required": bool(contract.get("required")),
            "browser_flow_required": bool(contract.get("required")),
            "proof_steps": [
                "client_ui_action_changes_persisted_state",
                "specialist_ui_action_updates_same_state",
                "manager_role_observes_updated_state",
                "client_role_observes_update_after_refresh",
            ],
        },
        "agent_todos": [
            {"id": "plan", "status": "completed", "content": "Extract prompt-owned roles, entities, UI controls, APIs, tests, and mobile constraints."},
            {"id": "inspect", "status": "pending", "content": "Read/search only the workspace files needed for the next patch."},
            {"id": "build", "status": "pending", "content": "Patch backend, role UI, frontend behavior, and generated tests through validated draft actions."},
            {"id": "verify", "status": "pending", "content": "Run static/API/generated/browser/mobile proof against the actual generated workflow."},
            {"id": "repair", "status": "pending", "content": "Patch the concrete failed slice until strict green completion."},
        ],
        "mobile_design_contract": {
            "target_viewports": ["360x740", "390x844", "430x932"],
            "no_horizontal_scroll": True,
            "responsive_cards_forms_lists": True,
            "quality_runs_require_post_green_design_pass": mode_value == GenerationMode.QUALITY.value,
        },
        "orchestration": {
            "execution_style": (orchestration or {}).get("execution_style"),
            "phases": list((orchestration or {}).get("phases") or []),
            "worker_count": (orchestration or {}).get("agent_worker_count", 0),
        },
    }


def orchestration_metadata_for_contract(
    *,
    contract: dict[str, Any] | None,
    generation_mode: GenerationMode | str | None,
    focused_edit_kind: str = "",
) -> dict[str, Any]:
    mode_value = normalized_generation_mode(generation_mode)
    workflow_kind = str(focused_edit_kind or "").strip().lower()
    enabled = bool((contract or {}).get("required"))
    execution_style = (
        "agent_query_loop"
        if enabled and mode_value == GenerationMode.FAST.value
        else "agent_query_loop_with_design_pass" if enabled and mode_value == GenerationMode.QUALITY.value
        else "agent_query_loop" if enabled else "none"
    )
    isolated_worker_drafts = enabled and mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}
    phases = [
        {
            "id": "spec_extract",
            "status": "planned" if enabled else "not_required",
            "description": "Extract role actions, data resources, buttons, APIs, and cross-role acceptance requirements.",
        },
        {
            "id": "build",
            "status": "planned" if enabled else "not_required",
            "description": (
                "Use owned agent worker drafts for backend/API, role UI, and generated tests, then merge non-conflicting diffs."
                if isolated_worker_drafts
                else "Use tool-loop patches to build backend/API, role UI, and generated tests from the implementation plan."
            ),
        },
        {
            "id": "merge",
            "status": "planned" if enabled else "not_required",
            "description": "Merge non-conflicting ownership zones and reject overlapping edits before applying.",
        },
        {
            "id": "verify_repair",
            "status": "planned" if enabled else "not_required",
            "description": "Convert check failures into targeted repair tasks tied to the failed user flow.",
        },
    ]
    worker_summaries = [
        {
            "worker": "backend_api",
            "ownership": ["miniapp/app/routes/**", "miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py"],
            "responsibility": "Persistent resources, GET/POST/update APIs, and route registration.",
        },
        {
            "worker": "client_ui",
            "ownership": ["miniapp/app/static/client/**"],
            "responsibility": "User-facing forms, saved-state controls, and client-side API calls.",
        },
        {
            "worker": "specialist_ui",
            "ownership": ["miniapp/app/static/specialist/**"],
            "responsibility": "Prompt-derived specialist workflow, role actions, and saved-state visibility.",
        },
        {
            "worker": "manager_ui",
            "ownership": ["miniapp/app/static/manager/**"],
            "responsibility": "Dashboard metrics, oversight controls, and shared-state visibility.",
        },
        {
            "worker": "generated_tests",
            "ownership": ["miniapp/tests/test_generated_app.py", "miniapp/tests/generated_app.test.mjs"],
            "responsibility": "Acceptance tests covering every required flow in the contract.",
        },
    ]
    return {
        "enabled": enabled,
        "mode": mode_value,
        "workflow_kind": workflow_kind,
        "execution_style": execution_style,
        "isolated_worker_drafts": isolated_worker_drafts,
        "agent_worker_count": len(worker_summaries) if enabled else 0,
        "phases": phases,
        "worker_summaries": worker_summaries if enabled else [],
    }
