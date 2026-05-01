from __future__ import annotations

import re
from typing import Any

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")
GENERIC_RESOURCE_DEFAULTS = ("records", "updates", "summaries")
PROMPT_RESOURCE_HINT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(request|requests|application|applications|inquiry|inquiries)\b", "requests"),
    (r"(заявк|обращен|запрос)", "requests"),
    (r"(задач|таск|\btask|\btasks)", "tasks"),
)

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
    "заказ",
    "после добавления",
    "должно появляться",
    "появлялось",
    "сохраняться",
    "срочн",
    "во всех трех",
    "во всех трёх",
)

def prompt_resource_candidates(prompt: str, *, limit: int = 3) -> list[str]:
    """Extract high-confidence generic resource slugs without assuming a business domain.

    This intentionally avoids turning arbitrary prompt nouns into API route names.
    Detailed domain entities belong in the LLM implementation plan; the platform
    contract should only provide stable generic anchors it can validate.
    """
    candidates: list[str] = []
    text = str(prompt or "").strip().lower().replace("ё", "е")
    for pattern, slug in PROMPT_RESOURCE_HINT_PATTERNS:
        if re.search(pattern, text) and slug not in candidates:
            candidates.append(slug)
        if len(candidates) >= limit:
            return candidates
    return candidates

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
        "customer",
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

    resource_limit = 4 if mode_value == GenerationMode.QUALITY.value else 3
    prompt_resources = prompt_resource_candidates(prompt, limit=resource_limit)
    endpoint_resources = [
        *prompt_resources,
        *[resource for resource in GENERIC_RESOURCE_DEFAULTS if resource not in prompt_resources],
    ][: max(1, len(prompt_resources) or 1)]
    flows: list[dict[str, Any]] = [
        {
            "id": "role_shared_persistence",
            "title": "Shared persisted role workflow",
            "roles": list(ROLE_ORDER),
            "requirements": [
                "Client role can submit a real form/action through a POST-capable backend API.",
                "Specialist role can see saved client-created state and perform an operational status/action update.",
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
                    "One prompt-derived shared resource supports create, process/update, review, and summary workflows.",
                    "Role pages expose different actions for creating, processing, and reviewing saved state.",
                    "Specialist or manager status/payment/attendance changes are visible to the other roles through later GET requests.",
                ],
                "required_tests": [
                    "Generated tests cover the primary create/list/update flow and one related status, payment, attendance, or summary flow.",
                ],
            }
        )
    required_endpoints = [
        {"resource": resource, "path": f"/api/{resource}", "methods": ["GET", "POST", "PATCH"]}
        for resource in endpoint_resources
    ]
    return {
        "required": True,
        "intent": intent_value,
        "generation_mode": mode_value,
        "workflow_kind": workflow_kind or ("create" if intent_value == "create" else "behavior_workflow_edit"),
        "roles": list(ROLE_ORDER),
        "features": {
            "cross_role_persistence": True,
            "refresh_persistence": True,
            "status_update": True,
            "resource_count": len(endpoint_resources),
            "prompt_resource_candidates": prompt_resources,
            "resource_strategy": "prompt_derived_without_fixed_domain_template",
        },
        "required_endpoints": required_endpoints,
        "required_controls": [
            {"role": "client", "action": "submit a prompt-derived create/request form through POST"},
            {"role": "specialist", "action": "process or update saved work through POST/PATCH"},
            {"role": "manager", "action": "review shared state and trigger an oversight/status action"},
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

    The plan intentionally avoids domain-specific templates. It describes the
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
    if not resources:
        resources = prompt_resource_candidates(prompt, limit=3) or list(GENERIC_RESOURCE_DEFAULTS[:1])
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
            "client": "perform the prompt-derived customer/user create, submit, select, or save action through UI and POST API",
            "specialist": "perform the prompt-derived operational processing/update action through UI and update API",
            "manager": "review or control shared state and summary information through manager UI",
        },
        "api_contract": {
            "required_endpoints": list(contract.get("required_endpoints") or []),
            "must_persist": True,
            "must_support_update": bool((contract.get("features") or {}).get("status_update", True)),
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
        "mobile_design_contract": {
            "target_viewports": ["360x740", "390x844", "430x932"],
            "no_horizontal_scroll": True,
            "responsive_cards_forms_lists": True,
            "quality_runs_require_post_green_design_pass": mode_value == GenerationMode.QUALITY.value,
        },
        "orchestration": {
            "execution_style": (orchestration or {}).get("execution_style"),
            "phases": list((orchestration or {}).get("phases") or []),
            "worker_count": (orchestration or {}).get("parallel_worker_count", 0),
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
        "fast_parallel_workers"
        if enabled and mode_value == GenerationMode.FAST.value
        else "deep_parallel_workers" if enabled else "none"
    )
    phases = [
        {
            "id": "spec_extract",
            "status": "planned" if enabled else "not_required",
            "description": "Extract role actions, data resources, buttons, APIs, and cross-role acceptance requirements.",
        },
        {
            "id": "parallel_build",
            "status": "planned" if enabled else "not_required",
            "description": "Build backend/API, client UI, specialist UI, manager UI, and generated tests as separately owned lanes before merge.",
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
            "responsibility": "Customer-facing forms, saved-state controls, and client-side API calls.",
        },
        {
            "worker": "specialist_ui",
            "ownership": ["miniapp/app/static/specialist/**"],
            "responsibility": "Operational queue, status actions, and saved-state visibility.",
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
        "parallel_worker_count": len(worker_summaries) if enabled else 0,
        "phases": phases,
        "worker_summaries": worker_summaries if enabled else [],
    }
