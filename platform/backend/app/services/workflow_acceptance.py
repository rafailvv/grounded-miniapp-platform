from __future__ import annotations

from typing import Any
import re

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")
PROMPT_PLAN_STOPWORDS = {
    "and",
    "app",
    "application",
    "create",
    "for",
    "from",
    "mini",
    "miniapp",
    "need",
    "that",
    "the",
    "this",
    "want",
    "with",
    "без",
    "будет",
    "вести",
    "видеть",
    "всё",
    "для",
    "должен",
    "должна",
    "должны",
    "есть",
    "каждый",
    "как",
    "маленький",
    "мне",
    "может",
    "нужно",
    "помогало",
    "приложение",
    "современно",
    "таблиц",
    "удобно",
    "удобным",
    "хочу",
    "чтобы",
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def extract_prompt_planning_hints(prompt: str) -> dict[str, Any]:
    """Extract prompt-owned planning hints without domain templates.

    The platform does not invent routes/resources from these hints. They are a
    compact scratchpad for the LLM workers so the first build turn is anchored
    to the user's nouns, role language, fields, and actions instead of generic
    placeholder records.
    """
    text = _clean_text(prompt)
    sentences = [
        sentence.strip(" .!?\t")
        for sentence in re.split(r"[\n.!?]+", text)
        if sentence.strip(" .!?\t")
    ]
    lowered_sentences = [(sentence, sentence.lower()) for sentence in sentences]
    actor_hints: dict[str, list[str]] = {role: [] for role in ROLE_ORDER}
    actor_patterns = {
        "client": (
            "клиент",
            "пользователь",
            "ученик",
            "посетитель",
            "покупатель",
            "заказчик",
            "пациент",
            "родитель",
            "user",
            "customer",
            "client",
        ),
        "specialist": (
            "специалист",
            "исполнитель",
            "мастер",
            "сотрудник",
            "преподаватель",
            "тренер",
            "оператор",
            "worker",
            "specialist",
            "staff",
            "teacher",
        ),
        "manager": (
            "менеджер",
            "администратор",
            "управля",
            "руководитель",
            "manager",
            "admin",
            "administrator",
            "owner",
        ),
    }
    action_markers = (
        "долж",
        "может",
        "видит",
        "видеть",
        "выбира",
        "оформ",
        "добав",
        "меня",
        "отмеч",
        "контрол",
        "create",
        "choose",
        "add",
        "update",
        "see",
        "manage",
    )
    action_sentences = [sentence for sentence, lowered in lowered_sentences if any(marker in lowered for marker in action_markers)]
    for sentence, lowered in lowered_sentences:
        for role, patterns in actor_patterns.items():
            if any(pattern in lowered for pattern in patterns):
                actor_hints[role].append(sentence)
    # If the user wrote actor/action sentences without platform role
    # names, preserve the sentence order as role hints instead of inventing a
    # domain template.
    for role, sentence in zip(ROLE_ORDER, action_sentences):
        if not actor_hints[role]:
            actor_hints[role].append(sentence)

    words = [
        item.lower()
        for item in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_-]{3,}", text)
        if item.lower() not in PROMPT_PLAN_STOPWORDS
    ]
    frequency: dict[str, int] = {}
    for word in words:
        frequency[word] = frequency.get(word, 0) + 1
    prompt_terms = [
        term
        for term, _count in sorted(frequency.items(), key=lambda item: (-item[1], item[0]))[:16]
    ]
    field_hints: list[str] = []
    for marker in ("выбирает", "выбрать", "указывает", "указать", "добавляет", "добавить", "заполняет", "заполнить", "chooses", "adds", "fills"):
        pattern = re.compile(rf"{marker}\s+(?P<tail>[^.!?\n]+)", re.IGNORECASE)
        for match in pattern.finditer(text):
            tail = match.group("tail")
            for part in re.split(r"[,;]|\s+и\s+|\s+and\s+", tail):
                cleaned = _clean_text(part).strip(" .")
                if 2 < len(cleaned) <= 80 and cleaned not in field_hints:
                    field_hints.append(cleaned)
                if len(field_hints) >= 12:
                    break
            if len(field_hints) >= 12:
                break
        if len(field_hints) >= 12:
            break
    return {
        "prompt_summary": text[:1200],
        "prompt_sentences": sentences[:10],
        "prompt_terms": prompt_terms,
        "field_hints": field_hints[:12],
        "role_action_prompts": {
            role: hints[:4]
            for role, hints in actor_hints.items()
        },
    }


def _role_screen_plan(
    *,
    prompt_hints: dict[str, Any],
    generation_mode: GenerationMode | str | None,
) -> dict[str, Any]:
    """Suggest routeable screen intents from prompt-owned role actions.

    This intentionally does not create route names, resource names, or a fixed
    page count. The LLM still owns concrete pages. The platform only gives a
    Claude/Codex-style planning nudge so complex mobile workflows do not get
    collapsed into one long dashboard.
    """
    mode_value = normalized_generation_mode(generation_mode)
    role_prompts = prompt_hints.get("role_action_prompts") if isinstance(prompt_hints, dict) else {}
    field_hints = prompt_hints.get("field_hints") if isinstance(prompt_hints, dict) else []
    sentences = prompt_hints.get("prompt_sentences") if isinstance(prompt_hints, dict) else []

    intent_markers: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "create_or_configure",
            (
                "add",
                "create",
                "fill",
                "submit",
                "choose",
                "save",
                "добав",
                "созда",
                "заполн",
                "оформ",
                "выбира",
                "выбрат",
                "сохран",
                "указывает",
                "указать",
            ),
        ),
        (
            "list_or_queue",
            (
                "list",
                "queue",
                "see",
                "view",
                "track",
                "видит",
                "видеть",
                "очеред",
                "спис",
                "отслеж",
                "смотр",
            ),
        ),
        (
            "detail_or_update",
            (
                "update",
                "change",
                "process",
                "mark",
                "control",
                "меня",
                "обнов",
                "обработ",
                "отмеч",
                "контрол",
                "управ",
            ),
        ),
        (
            "summary_or_insight",
            (
                "summary",
                "metrics",
                "analytics",
                "overview",
                "report",
                "свод",
                "метрик",
                "аналит",
                "отчет",
                "отчёт",
                "загруз",
            ),
        ),
        (
            "settings_or_availability",
            (
                "setting",
                "available",
                "availability",
                "status",
                "настрой",
                "доступ",
                "статус",
                "остат",
                "график",
            ),
        ),
    )

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
        combined = " ".join(source_phrases).lower()
        for intent, markers in intent_markers:
            if any(marker in combined for marker in markers):
                detected.append(
                    {
                        "intent": intent,
                        "purpose": "separate routeable screen when this task would make the role root too long or mix unrelated controls",
                        "source": source_phrases[:3],
                    }
                )
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
) -> dict[str, Any]:
    """Build a generic agent plan from prompt-derived contract data.

    The plan intentionally avoids fixed-category templates. It describes the
    workflow proof the agent must satisfy, while the concrete nouns/fields are
    still owned by the LLM-generated implementation.
    """
    intent_value = str(intent or "").strip().lower()
    mode_value = normalized_generation_mode(generation_mode)
    contract = dict(acceptance_contract or {})
    prompt_hints = extract_prompt_planning_hints(prompt)
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
        "primary_entities": list(prompt_hints.get("prompt_terms") or [])[:8],
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
    isolated_worker_drafts = enabled and mode_value in {
        GenerationMode.FAST.value,
        GenerationMode.BALANCED.value,
        GenerationMode.QUALITY.value,
    }
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
