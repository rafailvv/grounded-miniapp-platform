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


def normalize_prompt_contract_analysis(
    prompt: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Normalize structured LLM prompt analysis without local lexical extraction.

    This function is intentionally not an NLP parser and does not locally mine
    product terms from the prompt. The LLM is the only component allowed to
    decide product nouns, resources, fields, role ownership, and screen intent.
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
        "routeable_screen_plan": screen_plan,
    }


def extract_prompt_planning_hints(
    prompt: str,
    *,
    prompt_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Return contract hints from structured LLM analysis only.

    The previous implementation tried to infer fields/resources from prompt
    local lexical parsing. That was too brittle and could accidentally bias
    generation toward a domain. The runtime now requires structured model
    analysis.
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
    field_hints = prompt_hints.get("field_hints") if isinstance(prompt_hints, dict) else []
    sentences = prompt_hints.get("prompt_sentences") if isinstance(prompt_hints, dict) else []

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
        if role == "client" and field_hints and not any(item["intent"] == "create_or_configure" for item in detected):
            detected.append(
                {
                    "intent": "create_or_configure",
                    "purpose": "form/select screen for the prompt-provided fields",
                    "source": list(field_hints)[:6],
                }
            )
        if role == "manager" and mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
            if not any(item["intent"] == "summary_or_insight" for item in detected):
                detected.append(
                    {
                        "intent": "summary_or_insight",
                        "purpose": "management summary screen when the prompt asks for oversight or the mode adds deeper review",
                        "source": source_phrases[:3],
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
    broad_flow_terms = ("button", "form", "list", "кнопк", "форма", "список")
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
    prompt_analysis: dict[str, Any] | None = None,
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
    if prompt_analysis is None:
        raise ValueError("LLM prompt analysis is required before building a workflow acceptance contract.")
    prompt_hints = extract_prompt_planning_hints(prompt, prompt_analysis=prompt_analysis)
    flows: list[dict[str, Any]] = [
        {
            "id": "role_shared_persistence",
            "title": "Shared persisted role workflow",
            "roles": list(ROLE_ORDER),
            "requirements": [
                "Client role can submit a real form/action through a POST-capable backend API.",
                "Specialist role can see saved client-created state and perform the prompt-derived operational action.",
                "Manager role can see persisted shared state, summary metrics, and the prompt-derived oversight or control action.",
                "If the prompt assigns creation of shared source state to manager or specialist, that role owns the creation flow and client consumes the persisted state.",
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
            "api_discovery_required": True,
        },
        "required_endpoints": [],
        "prompt_hints": prompt_hints,
        "api_contract": {
            "field_hints": list(prompt_hints.get("field_hints") or [])[:12],
            "role_field_hints": dict(prompt_hints.get("role_field_hints") or {}),
            "resource_hint": prompt_hints.get("resource_hint") or None,
            "analysis_source": prompt_hints.get("analysis_source"),
            "analysis_status": prompt_hints.get("analysis_status"),
        },
        "required_controls": [
            {"role": "client", "action": "submit the user-facing prompt-derived create/select/save flow through UI and POST-capable API"},
            {"role": "specialist", "action": "perform the prompt-derived operational action through POST/PATCH when the workflow needs persisted updates"},
            {"role": "manager", "action": "review shared state and trigger the prompt-derived oversight/control action"},
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
                "routeable_screen_plan": {},
            }
        )
    screen_plan = _role_screen_plan(prompt_hints=prompt_hints, generation_mode=generation_mode)
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
            "client": (prompt_hints.get("role_action_prompts") or {}).get("client") or ["perform the prompt-derived user create, submit, select, or save action through UI and POST API"],
            "specialist": (prompt_hints.get("role_action_prompts") or {}).get("specialist") or ["perform the prompt-derived operational processing/update action through UI and update API"],
            "manager": (prompt_hints.get("role_action_prompts") or {}).get("manager") or ["review or control shared state and summary information through manager UI"],
        },
        "api_contract": {
            "required_endpoints": list(contract.get("required_endpoints") or []),
            "must_persist": True,
            "must_support_update": bool((contract.get("features") or {}).get("workflow_update", True)),
            "field_hints": list(prompt_hints.get("field_hints") or [])[:12],
            "role_field_hints": dict(prompt_hints.get("role_field_hints") or {}),
            "resource_hint": prompt_hints.get("resource_hint") or None,
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
                "client": "user-facing mobile app for the primary prompt-derived create/select/save flow; no links to specialist or manager surfaces",
                "specialist": "operational mobile app for processing/updating shared state; no client-only duplicate page",
                "manager": "oversight mobile app for summary, control, status, workload visibility, and any prompt-assigned shared-state creation flow; no specialist-only duplicate page",
            },
            "shared_state_contract": [
                "the prompt-assigned source role creates or selects persisted shared state through UI",
                "specialist can load the same state and persist an update",
                "manager can load the updated state and summary",
                "client can reload and see the update through UI",
            ],
        },
        "test_contract": {
            "generated_tests_required": bool(contract.get("required")),
            "browser_flow_required": bool(contract.get("required")),
            "proof_steps": [
                "client_ui_action_changes_persisted_state",
                "specialist_ui_action_updates_same_state",
                "manager_role_observes_updated_state",
                "client_role_observes_update_after_refresh",
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
    isolated_worker_drafts = enabled and mode_value == GenerationMode.FAST.value
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
                else "Use the contract-owned runtime as the first draft and serialize further backend/API, role UI, generated test, and design mutations through the coordinator tool loop."
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
