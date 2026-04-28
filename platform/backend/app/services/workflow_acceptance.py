from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode


ROLE_ORDER = ("client", "specialist", "manager")

WORKFLOW_EDIT_MARKERS = (
    "add to cart",
    "after adding",
    "button",
    "cart",
    "catalog",
    "checkout",
    "does not load",
    "doesn't load",
    "order",
    "product",
    "refresh",
    "should appear",
    "не подгружается",
    "не работает",
    "кнопк",
    "каталог",
    "корзин",
    "оформлен",
    "заказ",
    "товар",
    "после добавления",
    "должно появляться",
    "появлялось",
    "сохраняться",
    "срочн",
    "во всех трех",
    "во всех трёх",
)

COMMERCE_FLOW_MARKERS = (
    "add to cart",
    "cart",
    "catalog",
    "checkout",
    "internet shop",
    "online store",
    "order",
    "product",
    "shop",
    "store",
    "интернет-магаз",
    "магазин",
    "каталог",
    "корзин",
    "оформлен",
    "заказ",
    "товар",
)


def normalized_generation_mode(generation_mode: GenerationMode | str | None) -> str:
    return str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()


def is_behavior_workflow_prompt(prompt: str) -> bool:
    text = str(prompt or "").strip().lower()
    if not text:
        return False
    strong_markers = (
        "add to cart",
        "after adding",
        "cart",
        "checkout",
        "does not load",
        "doesn't load",
        "не подгружается",
        "не работает",
        "не нажим",
        "корзин",
        "оформлен",
        "после добавления",
        "должно появляться",
        "появлялось",
    )
    if any(marker in text for marker in strong_markers):
        return True
    broad_flow_terms = ("button", "кнопк", "catalog", "product", "order", "каталог", "товар", "заказ")
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


def prompt_has_commerce_flow(prompt: str) -> bool:
    text = str(prompt or "").strip().lower()
    return bool(text) and any(marker in text for marker in COMMERCE_FLOW_MARKERS)


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

    commerce_flow = prompt_has_commerce_flow(prompt)
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
    required_endpoints = [{"resource": "records", "methods": ["GET", "POST", "PATCH"]}]
    required_buttons = ["client-submit", "specialist-status-update", "manager-oversight"]

    if commerce_flow:
        flows.append(
            {
                "id": "commerce_catalog_cart_order",
                "title": "Catalog, cart, checkout, and cross-role order visibility",
                "roles": list(ROLE_ORDER),
                "requirements": [
                    "Specialist can add or update products with inventory through a POST/PATCH product API.",
                    "Client catalog loads products through GET /api/products after refresh.",
                    "Client add-to-cart control has an effective JavaScript handler and updates cart state.",
                    "Client checkout sends an order through POST /api/orders with chosen products/quantities.",
                    "Specialist and manager can see the created order through persisted GET /api/orders and change/review status.",
                ],
                "required_tests": [
                    "Create product -> catalog sees product -> add to cart handler exists -> checkout POST -> order appears for specialist/manager.",
                    "Status change persists after GET.",
                ],
            }
        )
        required_endpoints = [
            {"resource": "products", "path": "/api/products", "methods": ["GET", "POST", "PATCH"]},
            {"resource": "orders", "path": "/api/orders", "methods": ["GET", "POST", "PATCH"]},
        ]
        required_buttons = [
            "product-create",
            "add-to-cart",
            "checkout",
            "specialist-order-status",
            "manager-order-review",
        ]

    return {
        "required": True,
        "intent": intent_value,
        "generation_mode": mode_value,
        "workflow_kind": workflow_kind or ("create" if intent_value == "create" else "behavior_workflow_edit"),
        "roles": list(ROLE_ORDER),
        "features": {
            "commerce_catalog_cart_order": commerce_flow,
            "cross_role_persistence": True,
            "refresh_persistence": True,
            "status_update": True,
        },
        "required_endpoints": required_endpoints,
        "required_buttons": required_buttons,
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
            "responsibility": "Customer-facing forms, catalog/cart/order controls, and client-side API calls.",
        },
        {
            "worker": "specialist_ui",
            "ownership": ["miniapp/app/static/specialist/**"],
            "responsibility": "Operational queue, product/status actions, and saved-state visibility.",
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
