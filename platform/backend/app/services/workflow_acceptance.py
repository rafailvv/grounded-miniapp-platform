from __future__ import annotations

import re
from typing import Any

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")
GENERIC_RESOURCE_DEFAULTS = ("records", "updates", "summaries")
PROMPT_RESOURCE_SEMANTIC_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(request|requests|application|applications|inquiry|inquiries)\b", "requests"),
    (r"\b(order|orders)\b", "orders"),
    (r"\b(booking|bookings|reservation|reservations)\b", "bookings"),
    (r"\b(appointment|appointments|visit|visits)\b", "appointments"),
    (r"(заявк|обращен|запрос)", "requests"),
    (r"(заказ)", "orders"),
    (r"(брон|резерв)", "bookings"),
    (r"(запис|визит|прием|приём)", "appointments"),
)
PROMPT_RESOURCE_STOPWORDS = {
    "about",
    "action",
    "actions",
    "address",
    "after",
    "application",
    "available",
    "button",
    "client",
    "create",
    "daily",
    "dashboard",
    "date",
    "details",
    "form",
    "manager",
    "mini",
    "miniapp",
    "owner",
    "records",
    "request",
    "requests",
    "role",
    "roles",
    "small",
    "specialist",
    "status",
    "submit",
    "through",
    "update",
    "updates",
    "visible",
    "workflow",
    "workflows",
    "want",
    "with",
    "адрес",
    "аккуратный",
    "вводить",
    "видел",
    "видеть",
    "владею",
    "владелец",
    "возможность",
    "выбирать",
    "добавлять",
    "должен",
    "должна",
    "должны",
    "занятие",
    "занятия",
    "занятием",
    "занятий",
    "занятии",
    "занятиях",
    "данные",
    "дату",
    "заявка",
    "заявки",
    "информация",
    "клиент",
    "кнопка",
    "которые",
    "менеджер",
    "мини",
    "небольшим",
    "небольшой",
    "нужно",
    "оформлять",
    "отмечать",
    "работаю",
    "приложение",
    "сделать",
    "смотреть",
    "специалист",
    "статус",
    "статусы",
    "удобно",
    "форма",
    "хочу",
}
PROMPT_RESOURCE_NOISE_PREFIXES = (
    "build",
    "create",
    "need",
    "want",
    "work",
    "аккурат",
    "владе",
    "долж",
    "ежеднев",
    "детск",
    "занима",
    "заняти",
    "загруз",
    "клиент",
    "контакт",
    "компани",
    "маленьк",
    "менедж",
    "небольш",
    "педагог",
    "прилож",
    "работа",
    "расписан",
    "родител",
    "сервис",
    "сет",
    "служб",
    "современ",
    "специалист",
    "студи",
    "творчеств",
    "подготов",
    "пробн",
    "направлен",
    "центр",
    "удобн",
    "управля",
    "хоч",
)
CYRILLIC_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
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


def _slug_from_prompt_token(token: str) -> str:
    lowered = str(token or "").strip().lower().translate(CYRILLIC_TRANSLIT)
    slug = re.sub(r"[^a-z0-9_]+", "_", lowered).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if slug and slug[0].isdigit():
        slug = f"item_{slug}"
    return slug


def _is_prompt_resource_noise(token: str) -> bool:
    normalized = str(token or "").strip().lower().replace("ё", "е")
    if not normalized or normalized in PROMPT_RESOURCE_STOPWORDS:
        return True
    return any(normalized.startswith(prefix) for prefix in PROMPT_RESOURCE_NOISE_PREFIXES)


def prompt_resource_candidates(prompt: str, *, limit: int = 3) -> list[str]:
    """Extract prompt-derived resource slugs without assuming a business domain."""
    candidates: list[str] = []
    text = str(prompt or "").strip().lower().replace("ё", "е")
    for pattern, slug in PROMPT_RESOURCE_SEMANTIC_PATTERNS:
        if re.search(pattern, text) and slug not in candidates:
            candidates.append(slug)
        if len(candidates) >= limit:
            return candidates
    for token in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_-]{2,}", str(prompt or "")):
        normalized = token.strip().lower().replace("ё", "е")
        if _is_prompt_resource_noise(normalized):
            continue
        slug = _slug_from_prompt_token(normalized)
        if len(slug) < 3 or slug in ROLE_ORDER:
            continue
        if slug not in candidates:
            candidates.append(slug)
        if len(candidates) >= limit:
            break
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
    broad_flow_terms = ("button", "form", "list", "record", "кнопк", "форма", "список", "запис", "заказ")
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

    resource_count = 1
    prompt_resources = prompt_resource_candidates(prompt, limit=resource_count)
    endpoint_resources = [
        *prompt_resources,
        *[resource for resource in GENERIC_RESOURCE_DEFAULTS if resource not in prompt_resources],
    ][:resource_count]
    flows: list[dict[str, Any]] = [
        {
            "id": "role_shared_persistence",
            "title": "Shared persisted role workflow",
            "roles": list(ROLE_ORDER),
            "requirements": [
                "Client role can submit a real form/action through a POST-capable backend API.",
                "Specialist role can see saved client records and perform an operational status/action update.",
                "Manager role can see persisted shared records, summary metrics, and an oversight action.",
                "Saved data remains visible after a reload through GET APIs; app source starts with no seed/mock records.",
            ],
            "required_tests": [
                "Python generated test verifies empty GET, POST create, persisted GET, status/update, and persisted update.",
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
                    "Role pages expose different actions for creating, processing, and reviewing saved records.",
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
            "resource_count": resource_count,
            "prompt_resource_candidates": prompt_resources,
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
            "responsibility": "Customer-facing forms, saved-record controls, and client-side API calls.",
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
