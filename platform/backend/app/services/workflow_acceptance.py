from __future__ import annotations

from typing import Any
import re

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")
PROMPT_ANALYSIS_SCHEMA_VERSION = "grounded.prompt_contract_analysis.v1"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _prompt_sentences(text: str) -> list[str]:
    return [
        sentence.strip(" .!?\t")
        for sentence in re.split(r"[\n.!?]+", _clean_text(text))
        if sentence.strip(" .!?\t")
    ][:10]


def _sanitize_prompt_label(value: Any, *, limit: int = 80) -> str:
    cleaned = _clean_text(str(value or "")).strip(" .:-")
    if len(cleaned) < 2:
        return ""
    return cleaned[:limit]


def _sanitize_prompt_list(values: Any, *, limit: int = 12, item_limit: int = 80) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        label = _sanitize_prompt_label(item, limit=item_limit)
        if label and label not in result:
            result.append(label)
        if len(result) >= limit:
            break
    return result


def _sanitize_role_lists(values: Any, *, limit: int = 12) -> dict[str, list[str]]:
    source = values if isinstance(values, dict) else {}
    return {
        role: _sanitize_prompt_list(source.get(role), limit=limit)
        for role in ROLE_ORDER
    }


def _sanitize_roles(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        role = str(item or "").strip().lower()
        if role in ROLE_ORDER and role not in result:
            result.append(role)
    return result


def _sanitize_role_state_contract(values: Any) -> dict[str, Any]:
    source = values if isinstance(values, dict) else {}
    return {
        "source_roles": _sanitize_roles(source.get("source_roles") or source.get("creator_roles") or source.get("create_roles")),
        "update_roles": _sanitize_roles(source.get("update_roles") or source.get("mutating_roles") or source.get("editor_roles")),
        "observer_roles": _sanitize_roles(source.get("observer_roles") or source.get("viewer_roles") or source.get("consumer_roles")),
        "status_values": _sanitize_prompt_list(source.get("status_values") or source.get("state_values"), limit=8),
    }


def _source_roles_from_prompt_hints(prompt_hints: dict[str, Any]) -> list[str]:
    state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints, dict) else {}
    if isinstance(state_contract, dict):
        source_roles = _sanitize_roles(state_contract.get("source_roles"))
        if source_roles:
            return source_roles
    role_fields = prompt_hints.get("role_field_hints") if isinstance(prompt_hints.get("role_field_hints"), dict) else {}
    field_owned_roles = [
        role
        for role in ROLE_ORDER
        if any(str(item).strip() for item in (role_fields.get(role) or []))
    ]
    return field_owned_roles[:1]


def normalize_prompt_contract_analysis(
    prompt: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Normalize structured LLM prompt analysis.

    This function only accepts and sanitizes structured model output. It does
    not decide product nouns, resources, fields, role ownership, or screen intent.
    """
    if not isinstance(analysis, dict):
        raise ValueError("Prompt contract analysis is required and must be a JSON object.")

    resource_hint = _sanitize_prompt_label(
        analysis.get("resource_hint")
        or analysis.get("resource")
        or analysis.get("shared_resource")
        or analysis.get("primary_resource")
    )
    role_actions = _sanitize_role_lists(
        analysis.get("role_action_prompts") or analysis.get("role_actions"),
        limit=4,
    )
    role_fields = _sanitize_role_lists(
        analysis.get("role_field_hints")
        or analysis.get("role_fields")
        or analysis.get("fields_by_role"),
        limit=12,
    )
    field_hints = _sanitize_prompt_list(analysis.get("field_hints") or analysis.get("fields"), limit=12)
    for role in ROLE_ORDER:
        for field in role_fields.get(role) or []:
            if field not in field_hints:
                field_hints.append(field)
            if len(field_hints) >= 12:
                break

    screen_plan = analysis.get("routeable_screen_plan") or analysis.get("screen_plan")
    if not isinstance(screen_plan, dict):
        screen_plan = {}
    role_state_contract = _sanitize_role_state_contract(
        analysis.get("role_state_contract")
        or analysis.get("state_contract")
        or analysis.get("role_ownership")
    )

    return {
        "schema_version": PROMPT_ANALYSIS_SCHEMA_VERSION,
        "analysis_source": "llm",
        "analysis_status": "ok",
        "prompt_summary": _sanitize_prompt_label(analysis.get("prompt_summary"), limit=1200)
        or _clean_text(prompt)[:1200],
        "prompt_sentences": _prompt_sentences(prompt),
        "field_hints": field_hints[:12],
        "role_field_hints": role_fields,
        "resource_hint": resource_hint or None,
        "role_action_prompts": role_actions,
        "role_state_contract": role_state_contract,
        "routeable_screen_plan": screen_plan,
    }


def extract_prompt_planning_hints(
    prompt: str,
    *,
    prompt_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Return contract hints from structured LLM analysis only.

    The runtime requires structured model analysis before product fields,
    resources, ownership, or screen intent enter the implementation plan.
    """
    return normalize_prompt_contract_analysis(prompt, prompt_analysis)


def _role_screen_plan(
    *,
    prompt_hints: dict[str, Any],
    generation_mode: GenerationMode | str | None,
) -> dict[str, Any]:
    """Suggest routeable screen intents from LLM-owned prompt analysis."""
    mode_value = normalized_generation_mode(generation_mode)
    supplied = prompt_hints.get("routeable_screen_plan") if isinstance(prompt_hints, dict) else {}
    if isinstance(supplied, dict) and supplied.get("roles"):
        roles_payload = supplied.get("roles") if isinstance(supplied.get("roles"), dict) else {}
        return {
            "multi_page_recommended": bool(supplied.get("multi_page_recommended", True)),
            "route_names_owned_by_agent": True,
            "no_fixed_page_count": True,
            "roles": {
                role: [
                    {
                        "intent": _sanitize_prompt_label(item.get("intent"), limit=48) or "overview",
                        "purpose": _sanitize_prompt_label(item.get("purpose"), limit=160)
                        or "prompt-derived role screen",
                        "source": _sanitize_prompt_list(item.get("source"), limit=3, item_limit=160)
                        if isinstance(item, dict)
                        else [],
                    }
                    for item in (roles_payload.get(role) or [])
                    if isinstance(item, dict)
                ][:5]
                or [{"intent": "overview", "purpose": "prompt-derived role entry screen", "source": []}]
                for role in ROLE_ORDER
            },
        }
    role_prompts = prompt_hints.get("role_action_prompts") if isinstance(prompt_hints, dict) else {}
    role_fields = prompt_hints.get("role_field_hints") if isinstance(prompt_hints.get("role_field_hints"), dict) else {}
    field_hints = prompt_hints.get("field_hints") if isinstance(prompt_hints, dict) else []
    sentences = prompt_hints.get("prompt_sentences") if isinstance(prompt_hints, dict) else []
    source_roles = set(_source_roles_from_prompt_hints(prompt_hints))

    role_screens: dict[str, list[dict[str, Any]]] = {}
    for role in ROLE_ORDER:
        source_phrases = [
            _clean_text(item)
            for item in (role_prompts.get(role) if isinstance(role_prompts, dict) else []) or []
            if _clean_text(str(item))
        ]
        detected: list[dict[str, Any]] = [
            {
                "intent": "overview",
                "purpose": "mobile entry screen with the role's next important action and current shared state",
                "source": source_phrases[:1],
            }
        ]
        role_owned_fields = [
            str(item).strip()
            for item in (role_fields.get(role) or [])
            if str(item).strip()
        ]
        source_fields = role_owned_fields or (field_hints if role in source_roles else [])
        if role in source_roles and source_fields and not any(item["intent"] == "create_or_configure" for item in detected):
            detected.append(
                {
                    "intent": "create_or_configure",
                    "purpose": "form/select screen for the prompt-assigned source data",
                    "source": list(source_fields)[:6],
                }
            )
        # De-duplicate while preserving order.
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in detected:
            intent = str(item.get("intent") or "")
            if not intent or intent in seen:
                continue
            seen.add(intent)
            unique.append(item)
        role_screens[role] = unique[:5]

    complexity_signals = sum(max(0, len(items) - 1) for items in role_screens.values())
    prompt_sentence_count = len(sentences) if isinstance(sentences, list) else 0
    multi_page_recommended = (
        complexity_signals >= 2
        or prompt_sentence_count >= 3
        or mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}
    )
    return {
        "multi_page_recommended": bool(multi_page_recommended),
        "route_names_owned_by_agent": True,
        "no_fixed_page_count": True,
        "roles": role_screens,
    }


def normalized_generation_mode(generation_mode: GenerationMode | str | None) -> str:
    return str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()


def build_acceptance_contract(
    *,
    prompt: str,
    intent: str | None,
    generation_mode: GenerationMode | str | None,
    focused_edit_kind: str = "",
    prompt_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent_value = str(intent or "").strip().lower()
    mode_value = normalized_generation_mode(generation_mode)
    workflow_kind = str(focused_edit_kind or "").strip().lower()
    requires_contract = (
        intent_value == "create"
        or workflow_kind == "behavior_workflow_edit"
        or mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}
    )
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
    if prompt_analysis is None:
        raise ValueError("LLM prompt analysis is required before building a workflow acceptance contract.")
    prompt_hints = extract_prompt_planning_hints(prompt, prompt_analysis=prompt_analysis)
    source_roles = _source_roles_from_prompt_hints(prompt_hints)
    state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
    update_roles = _sanitize_roles(state_contract.get("update_roles"))
    flows: list[dict[str, Any]] = [
        {
            "id": "role_shared_persistence",
            "title": "Shared persisted product workflow",
            "roles": list(ROLE_ORDER),
            "requirements": [
                "The agent chooses API routes, entities, and persistence shape from the prompt and current code; the platform does not provide a product CRUD scaffold.",
                "Prompt-assigned source roles perform state-producing actions only when the prompt requires that behavior.",
                "Other roles load or change the same persisted state only through prompt-assigned actions.",
                "No role is forced into unrequested workflow semantics.",
                "Saved data remains visible after reload through the app-owned API; app source starts with no seed/mock records.",
            ],
            "required_tests": [
                "Python generated test verifies the actual app-owned API and persistence behavior, without assuming fixed update routes.",
                "JS generated test verifies real role pages, contract-derived controls, frontend API usage, and handler wiring.",
            ],
        }
    ]
    if mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
        flows.append(
            {
                "id": "related_resource_workflow",
                "title": "Related prompt-derived operations",
                "roles": list(ROLE_ORDER),
                "requirements": [
                    "The LLM-selected shared state supports only the read, mutate, publish, edit, summary, or operational workflows implied by the prompt.",
                    "Role pages expose different actions based on prompt-owned role responsibilities, not fixed processing roles.",
                    "Prompt-assigned role updates are visible to the other relevant roles through later reads.",
                ],
                "required_tests": [
                    "Generated tests cover the primary persisted flow and one related prompt-derived action or summary flow.",
                ],
            }
        )
    return {
        "required": True,
        "intent": intent_value,
        "generation_mode": mode_value,
        "workflow_kind": workflow_kind or ("create" if intent_value == "create" else "product_quality_run"),
        "roles": list(ROLE_ORDER),
        "features": {
            "cross_role_persistence": True,
            "refresh_persistence": True,
            "workflow_update": bool(update_roles),
            "api_discovery_required": True,
            "platform_product_scaffold": False,
        },
        "required_endpoints": [],
        "prompt_hints": prompt_hints,
        "api_contract": {
            "field_hints": list(prompt_hints.get("field_hints") or [])[:12],
            "role_field_hints": dict(prompt_hints.get("role_field_hints") or {}),
            "resource_hint": prompt_hints.get("resource_hint") or None,
            "role_state_contract": dict(prompt_hints.get("role_state_contract") or {}),
            "analysis_source": prompt_hints.get("analysis_source"),
            "analysis_status": prompt_hints.get("analysis_status"),
        },
        "required_controls": [
            {
                "role": role,
                "action": "create, publish, configure, or import the prompt-derived shared state through app-owned UI/API",
            }
            for role in source_roles
        ]
        + [
            {
                "role": role,
                "action": "perform the prompt-derived persisted update/control action when the workflow needs one",
            }
            for role in update_roles
            if role not in source_roles
        ],
        "page_contract": {
            "multi_page_role_apps": True,
            "route_manifest_required": True,
            "child_pages_must_be_reachable": True,
        },
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
    prompt_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a generic agent plan from prompt-derived contract data.

    The plan intentionally avoids fixed-category templates. It describes the
    workflow proof the agent must satisfy, while the concrete nouns/fields are
    still owned by the LLM-generated implementation.
    """
    intent_value = str(intent or "").strip().lower()
    mode_value = normalized_generation_mode(generation_mode)
    contract = dict(acceptance_contract or {})
    existing_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else None
    if existing_hints:
        prompt_hints = existing_hints
    else:
        if prompt_analysis is None and contract.get("required"):
            raise ValueError("LLM prompt analysis is required before building a workflow implementation plan.")
        prompt_hints = (
            extract_prompt_planning_hints(prompt, prompt_analysis=prompt_analysis)
            if prompt_analysis is not None
            else {
                "prompt_summary": _clean_text(prompt)[:1200],
                "prompt_sentences": _prompt_sentences(prompt),
                "field_hints": [],
                "role_field_hints": {role: [] for role in ROLE_ORDER},
                "resource_hint": None,
                "role_action_prompts": {role: [] for role in ROLE_ORDER},
                "role_state_contract": {
                    "source_roles": [],
                    "update_roles": [],
                    "observer_roles": [],
                    "status_values": [],
                },
                "routeable_screen_plan": {},
            }
        )
    screen_plan = _role_screen_plan(prompt_hints=prompt_hints, generation_mode=generation_mode)
    source_roles = _source_roles_from_prompt_hints(prompt_hints)
    state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
    update_roles = _sanitize_roles(state_contract.get("update_roles"))
    observer_roles = _sanitize_roles(state_contract.get("observer_roles"))
    required_controls = [
        dict(item)
        for item in (contract.get("required_controls") or [])
        if isinstance(item, dict)
    ]
    page_contract = dict(contract.get("page_contract") or {})
    return {
        "version": 1,
        "required": bool(contract.get("required")),
        "intent": intent_value,
        "generation_mode": mode_value,
        "principle": "plan_inspect_build_verify_repair_final_browser_proof",
        "roles": list(contract.get("roles") or ROLE_ORDER),
        "prompt_hints": prompt_hints,
        "primary_entities": [prompt_hints.get("resource_hint")] if prompt_hints.get("resource_hint") else [],
        "role_actions": {
            role: (prompt_hints.get("role_action_prompts") or {}).get(role)
            or (
                ["create, publish, configure, or import the prompt-derived shared state through app-owned UI/API"]
                if role in source_roles
                else ["load shared state and perform the prompt-derived role action"]
                if role in update_roles
                else ["load shared state for this role's prompt-derived view"]
            )
            for role in ROLE_ORDER
        },
        "role_state_contract": {
            "source_roles": source_roles,
            "update_roles": update_roles,
            "observer_roles": observer_roles,
            "status_values": list(state_contract.get("status_values") or []),
        },
        "api_contract": {
            "required_endpoints": list(contract.get("required_endpoints") or []),
            "must_persist": True,
            "must_support_update": bool((contract.get("features") or {}).get("workflow_update", bool(update_roles))),
            "field_hints": list(prompt_hints.get("field_hints") or [])[:12],
            "role_field_hints": dict(prompt_hints.get("role_field_hints") or {}),
            "resource_hint": prompt_hints.get("resource_hint") or None,
            "role_state_contract": {
                "source_roles": source_roles,
                "update_roles": update_roles,
                "observer_roles": observer_roles,
                "status_values": list(state_contract.get("status_values") or []),
            },
            "analysis_source": prompt_hints.get("analysis_source"),
            "analysis_status": prompt_hints.get("analysis_status"),
        },
        "ui_contract": {
            "required_controls": required_controls,
            "three_separate_role_apps": True,
            "multi_page_role_apps": True,
            "routeable_screen_plan": screen_plan,
            "route_manifest_required": True,
            "no_cross_role_navigation": True,
            "role_specific_actions": True,
            "copy_quality": "Do not expose API paths, HTTP methods, route slugs, role slugs, raw enum codes, or internal implementation labels in normal role UI; render readable labels with clear spacing between label and value.",
            "role_independence": {
                role: (
                    "source mobile app for the prompt-derived state-producing action"
                    if role in source_roles
                    else "mobile app for this role's prompt-derived update/control action"
                    if role in update_roles
                    else "mobile app for this role's prompt-derived read/selection experience"
                )
                for role in ROLE_ORDER
            },
            "shared_state_contract": [
                "prompt-assigned source role(s), if any, perform the prompt-derived persisted action through UI",
                "update roles persist only prompt-derived changes when the prompt needs those changes",
                "observer roles load the same state without owning another role's controls when observer roles are explicit",
                "all relevant roles can reload and see persisted state through UI",
            ],
        },
        "test_contract": {
            "generated_tests_required": bool(contract.get("required")),
            "browser_flow_required": bool(contract.get("required")),
            "proof_steps": [
                "prompt_source_role_action_changes_persisted_state",
                "prompt_update_role_updates_same_state_when_required",
                "observer_roles_load_persisted_state",
                "source_role_observes_update_after_refresh",
                "browser_proof_visits_routeable_screens_used_by_the_workflow",
            ],
        },
        "agent_todos": [
            {"id": "plan", "status": "completed", "content": "Extract prompt-owned roles, entities, UI controls, APIs, tests, and mobile constraints."},
            {"id": "inspect", "status": "pending", "content": "Read/search only the workspace files needed for the next patch."},
            {"id": "build", "status": "pending", "content": "Patch backend, role UI, frontend behavior, and generated tests through validated code-agent write tools."},
            {"id": "verify", "status": "pending", "content": "Run static/API/generated/browser/mobile proof against the actual generated workflow."},
            {"id": "repair", "status": "pending", "content": "Patch the concrete failed slice until strict green completion."},
        ],
        "mobile_design_contract": {
            "target_viewports": ["360x740", "390x844", "430x932"],
            "no_horizontal_scroll": True,
            "responsive_cards_forms_lists": True,
            "safe_top_spacing_required": True,
            "consistent_light_visual_system_by_default": True,
            "role_differentiation": "Differentiate roles by workflow, layout hierarchy, and subtle accents, not by switching one role to a separate dark/digital theme.",
            "no_fixed_width_tables_or_panels": True,
            "touch_targets_min_height": "44px where practical",
            "states_required": ["empty", "loading", "success", "error"],
            "quality_runs_require_post_green_design_pass": mode_value == GenerationMode.QUALITY.value,
        },
        "mode_quality_contract": {
            "fast": "smallest complete mobile product: one shared persisted flow, compact CSS, all roles functional, with prompt-derived role pages only where they clarify the workflow",
            "balanced": "moderate mobile product: richer layout, one related update/summary flow, clear role separation, and enough role pages to keep mobile workflows focused",
            "quality": "product-ready mobile mini-app: polished UI, refined states, stronger validation, post-green design pass, and well-organized prompt-derived role pages",
        },
        "routeable_screen_plan": screen_plan,
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
        "agent_tool_call_loop"
        if enabled and mode_value == GenerationMode.FAST.value
        else "agent_tool_call_loop_with_design_pass" if enabled and mode_value == GenerationMode.QUALITY.value
        else "agent_tool_call_loop" if enabled else "none"
    )
    isolated_worker_drafts = False
    phases = [
        {
            "id": "spec_extract",
            "status": "planned" if enabled else "not_required",
            "description": "Extract role actions, data resources, buttons, APIs, and cross-role acceptance requirements.",
        },
        {
            "id": "build",
            "status": "planned" if enabled else "not_required",
            "description": "Use the blank technical runtime plus prompt-contract metadata; serialize backend/API, role UI, test, and design mutations through the coordinator tool loop.",
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
            "responsibility": "Prompt-derived persistent resources, read/write APIs, and route registration.",
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
            "responsibility": "Prompt-derived manager workflow, shared-state visibility, and any manager-owned source/update controls.",
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
        "agent_worker_count": len(worker_summaries) if enabled and isolated_worker_drafts else 0,
        "phases": phases,
        "worker_summaries": worker_summaries if enabled and isolated_worker_drafts else [],
    }
