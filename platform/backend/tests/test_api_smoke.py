from __future__ import annotations

import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunCheckResult

ROLE_PREFIX = {
    "client": "Client",
    "specialist": "Specialist",
    "manager": "Manager",
}


def _install_llm_stub(app) -> None:
    openrouter = app.state.container.openrouter_client
    openrouter.api_key = "test-key"

    def fake_generate_structured(
        *,
        role: str,
        schema_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict:
        del role, schema, system_prompt, prompt_cache_key, stable_prefix
        payload = json.loads(user_prompt)
        if schema_name == "grounded_spec_outline_v1":
            return {
                "model": "openai/gpt-5-mini",
                "payload": {
                    "product_goal": str(payload.get("prompt") or "Generated mini-app"),
                    "roles": [
                        {"role": "client", "responsibility": "Browse and place orders.", "primary_actions": ["Browse", "Order", "Track"]},
                        {"role": "specialist", "responsibility": "Process incoming work.", "primary_actions": ["Review", "Pack", "Resolve"]},
                        {"role": "manager", "responsibility": "Oversee operations.", "primary_actions": ["Inspect", "Prioritize", "Supervise"]},
                    ],
                    "entities": ["Order", "Catalog", "Status"],
                    "flows": [{"name": "Primary flow", "goal": "Move through the main app journey.", "roles": ["client", "specialist", "manager"]}],
                    "api_needs": ["Read records", "Update statuses"],
                    "risks": ["Provider instability"],
                },
            }
        if schema_name == "grounded_spec_core_v1":
            spec = _grounded_spec_payload(str(payload.get("prompt") or "Generated mini-app"))
            return {
                "model": "openai/gpt-5-mini",
                "payload": {
                    "product_goal": spec["product_goal"],
                    "actors": spec["actors"],
                    "domain_entities": spec["domain_entities"],
                    "user_flows": spec["user_flows"],
                },
            }
        if schema_name == "grounded_spec_requirements_v1":
            spec = _grounded_spec_payload(str(payload.get("prompt") or "Generated mini-app"))
            return {
                "model": "openai/gpt-5-mini",
                "payload": {
                    "ui_requirements": spec["ui_requirements"],
                    "api_requirements": spec["api_requirements"],
                    "persistence_requirements": spec["persistence_requirements"],
                    "integration_requirements": spec["integration_requirements"],
                    "security_requirements": spec["security_requirements"],
                    "platform_constraints": spec["platform_constraints"],
                    "non_functional_requirements": spec["non_functional_requirements"],
                },
            }
        if schema_name == "grounded_spec_governance_v1":
            spec = _grounded_spec_payload(str(payload.get("prompt") or "Generated mini-app"))
            return {
                "model": "openai/gpt-5-mini",
                "payload": {
                    "assumptions": spec["assumptions"],
                    "unknowns": spec["unknowns"],
                    "contradictions": spec["contradictions"],
                },
            }
        if schema_name == "grounded_spec_v1":
            prompt = str(payload.get("prompt") or "Generated mini-app")
            return {"model": "openai/gpt-5-mini", "payload": _grounded_spec_payload(prompt)}
        if schema_name == "grounded_spec_fast_v1":
            prompt = str(payload.get("prompt") or "Generated mini-app")
            return {"model": "openai/gpt-5-mini", "payload": _grounded_spec_payload(prompt)}
        if schema_name == "role_contract_v1":
            return {"model": "openai/gpt-5-mini", "payload": _role_contract_payload(payload.get("role_scope") or [])}
        if schema_name in {"page_graph_v2", "page_graph_structure_v1", "page_graph_targeting_v1"}:
            return {
                "model": "openai/gpt-5-mini",
                "payload": _page_graph_payload(
                    role_scope=payload.get("role_scope") or [],
                    scope_mode=str(payload.get("scope_mode") or "whole_file_build"),
                ),
            }
        if schema_name.startswith("page_file_v1_"):
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _page_file_payload(payload)}
        if schema_name == "composition_bundle_v1":
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _composition_payload(payload)}
        if schema_name.startswith("whole_file_bundle_v1_"):
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _whole_file_bundle_payload(payload)}
        if schema_name == "fix_patch_v1":
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _fix_patch_payload(payload)}
        raise AssertionError(f"Unexpected schema name: {schema_name}")

    openrouter.generate_structured = fake_generate_structured
    openrouter.generate_json_object = fake_generate_structured

    def fake_static_check(*, source_dir, changed_files):
        del source_dir, changed_files
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Stubbed compile checks passed.",
            logs=["Stubbed compile checks passed."],
        )

    app.state.container.check_runner._static_check = fake_static_check


def _grounded_spec_payload(prompt: str) -> dict:
    return {
        "metadata": {
            "workspace_id": "ws_stub",
            "conversation_id": "conv_stub",
            "prompt_turn_id": "turn_stub",
            "template_revision_id": "rev_stub",
        },
        "target_platform": "telegram_mini_app",
        "preview_profile": "telegram_mock",
        "product_goal": prompt,
        "actors": [
            {
                "actor_id": "actor_client",
                "name": "Shopper",
                "role": "client",
                "description": "Primary customer flow.",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "domain_entities": [
            {
                "entity_id": "entity_order",
                "name": "Order",
                "description": "Customer order and processing state.",
                "attributes": [{"name": "status", "type": "string", "required": True}],
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "user_flows": [
            {
                "flow_id": "flow_primary",
                "name": "Primary flow",
                "goal": "Move through the main app journey.",
                "steps": [{"step_id": "step_1", "order": 1, "actor_id": "actor_client", "action": "Open the app"}],
                "acceptance_criteria": ["The generated app exposes real routes and pages."],
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "ui_requirements": [
            {
                "req_id": "ui_1",
                "category": "navigation",
                "description": "Provide real routed pages.",
                "priority": "must",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "api_requirements": [],
        "persistence_requirements": [],
        "integration_requirements": [],
        "security_requirements": [
            {
                "security_req_id": "sec_1",
                "category": "telegram_initdata",
                "rule": "Validate Telegram init data.",
                "severity": "critical",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "platform_constraints": [
            {
                "constraint_id": "platform_1",
                "category": "sdk",
                "rule": "Use Telegram Mini App compatible runtime behavior.",
                "severity": "critical",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "non_functional_requirements": [
            {
                "nfr_id": "nfr_1",
                "category": "usability",
                "description": "Avoid template-like placeholder screens.",
                "priority": "must",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "assumptions": [
            {
                "assumption_id": "assume_1",
                "text": "Use stubbed LLM responses in tests.",
                "status": "active",
                "rationale": "The miniapp tests verify the orchestration flow without calling a real provider.",
            }
        ],
        "unknowns": [],
        "contradictions": [],
        "doc_refs": [],
    }


def _role_contract_payload(role_scope: list[str]) -> dict:
    templates = {
        "client": {
            "responsibility": "Explore the offering, open detail pages, submit a request or order, and track progress.",
            "entry_goal": "Help the user start from a clear landing screen and move into the core journey.",
            "primary_jobs": ["Browse", "Open details", "Submit", "Track"],
            "key_entities": ["Order", "Request", "Catalog"],
            "ui_style_notes": ["Use welcoming entry language", "Make primary actions obvious"],
            "success_states": ["Action submitted", "Status visible"],
            "must_differ_from": ["specialist", "manager"],
        },
        "specialist": {
            "responsibility": "Handle incoming work items, progress them, and resolve blocked cases.",
            "entry_goal": "Surface the active queue and the next task to process.",
            "primary_jobs": ["Review queue", "Open work item", "Change status", "Resolve blockers"],
            "key_entities": ["Queue", "Task", "Issue"],
            "ui_style_notes": ["Keep workspace dense but readable", "Expose action context quickly"],
            "success_states": ["Work item updated", "Queue pressure reduced"],
            "must_differ_from": ["client", "manager"],
        },
        "manager": {
            "responsibility": "See overall health, supervise operations, and inspect items that need intervention.",
            "entry_goal": "Give an overview first, then routes into management surfaces.",
            "primary_jobs": ["Review dashboard", "Inspect activity", "Watch workload", "Intervene"],
            "key_entities": ["Metrics", "Team", "Operations"],
            "ui_style_notes": ["Keep overview concise", "Avoid operational clutter on the landing screen"],
            "success_states": ["Attention points visible", "Operational trends readable"],
            "must_differ_from": ["client", "specialist"],
        },
    }
    return {
        "app_title": "Booking workspace",
        "app_summary": "A routed booking and operations mini app with separate customer, specialist, and manager flows.",
        "shared_entities": ["Booking", "Status", "Team load"],
        "shared_logic": ["Routing", "Role-aware navigation", "Action feedback"],
        "roles": [{"role": role, **templates[role]} for role in role_scope],
    }


def _page_graph_payload(*, role_scope: list[str], scope_mode: str) -> dict:
    if scope_mode == "minimal_patch":
        pages = [
            {
                "role": "client",
                "entry_path": "/client",
                "landing_page_id": "client_home",
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    _page(
                        "client_home",
                        "/client",
                        "Home",
                        "client_index",
                        "miniapp/app/static/client/index.html",
                        "Client workspace",
                        "A real role landing page with actions instead of placeholder metrics.",
                    ),
                    _page(
                        "client_profile",
                        "/client/profile",
                        "Profile",
                        "client_profile",
                        "miniapp/app/static/client/profile.html",
                        "Profile",
                        "Profile editing page.",
                    ),
                ],
            }
        ]
        return {
            "summary": "Refine the existing client flow without touching unrelated roles.",
            "flow_mode": "multi_page",
            "files_to_read": [
                "miniapp/app/static/client/index.html",
                "miniapp/app/static/client/styles.css",
            ],
            "target_files": ["miniapp/app/static/client/index.html"],
            "shared_files": [],
            "backend_targets": [],
            "page_graph": {
                "app_title": "Booking workspace",
                "summary": "Refine the client landing surface while preserving the rest of the workspace.",
                "flow_mode": "multi_page",
                "roles": pages,
            },
        }

    templates = {
        "client": [
            _page("client_home", "/client", "Home", "client_index", "miniapp/app/static/client/index.html", "Booking home", "Entry page with clear shopper actions."),
            _page("client_catalog", "/client/catalog", "Catalog", "client_catalog", "miniapp/app/static/client/catalog.html", "Catalog", "Browse the current flower assortment."),
            _page("client_product", "/client/product", "Product", "client_product", "miniapp/app/static/client/product.html", "Product details", "Inspect one product in detail."),
            _page("client_cart", "/client/cart", "Cart", "client_cart", "miniapp/app/static/client/cart.html", "Cart", "Review selected products and place the order."),
            _page("client_profile", "/client/profile", "Profile", "client_profile", "miniapp/app/static/client/profile.html", "Profile", "Profile editing page."),
        ],
        "specialist": [
            _page("specialist_home", "/specialist", "Desk", "specialist_index", "miniapp/app/static/specialist/index.html", "Operations desk", "Entry page for active work."),
            _page("specialist_orders", "/specialist/orders", "Orders", "specialist_orders", "miniapp/app/static/specialist/orders.html", "Orders", "Review incoming customer orders."),
            _page("specialist_order_detail", "/specialist/order-detail", "Order detail", "specialist_order_detail", "miniapp/app/static/specialist/order-detail.html", "Order detail", "Inspect a single order and update its status."),
            _page("specialist_profile", "/specialist/profile", "Profile", "specialist_profile", "miniapp/app/static/specialist/profile.html", "Profile", "Profile editing page."),
        ],
        "manager": [
            _page("manager_home", "/manager", "Overview", "manager_index", "miniapp/app/static/manager/index.html", "Operations overview", "Landing page with attention points."),
            _page("manager_catalog", "/manager/catalog", "Catalog", "manager_catalog", "miniapp/app/static/manager/catalog.html", "Catalog management", "Review and manage the product catalog."),
            _page("manager_orders", "/manager/orders", "Orders", "manager_orders", "miniapp/app/static/manager/orders.html", "Orders overview", "Inspect current order volume and statuses."),
            _page("manager_profile", "/manager/profile", "Profile", "manager_profile", "miniapp/app/static/manager/profile.html", "Profile", "Profile editing page."),
        ],
    }
    roles = []
    target_files = ["miniapp/app/main.py", "miniapp/app/routes/profiles.py", "miniapp/app/db.py"]
    for role in role_scope:
        role_pages = templates[role]
        roles.append(
            {
                "role": role,
                "entry_path": f"/{role}",
                "landing_page_id": role_pages[0]["page_id"],
                "routes_file": f"miniapp/app/static/{role}/index.html",
                "pages": role_pages,
            }
        )
        target_files.extend(
            [
                f"miniapp/app/static/{role}/styles.css",
                f"miniapp/app/static/{role}/app.js",
                f"miniapp/app/static/{role}/profile.js",
            ]
        )
        target_files.extend(page["file_path"] for page in role_pages)
    return {
        "summary": "Create a role-based booking workspace with custom static pages served by the miniapp.",
        "flow_mode": "multi_page",
        "files_to_read": [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
        ],
        "target_files": target_files,
        "shared_files": [
            "miniapp/app/main.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/db.py",
        ],
        "backend_targets": [],
        "page_graph": {
            "app_title": "Booking workspace",
            "summary": "A routed booking mini app with separate client, specialist, and manager page trees.",
            "flow_mode": "multi_page",
            "roles": roles,
        },
    }


def _page(page_id: str, route_path: str, navigation_label: str, component_name: str, file_path: str, title: str, description: str) -> dict:
    dynamic_dependencies = ["records"] if any(token in route_path for token in ("/catalog", "/product", "/cart", "/orders", "/order-detail")) else []
    return {
        "page_id": page_id,
        "route_path": route_path,
        "navigation_label": navigation_label,
        "component_name": component_name,
        "file_path": file_path,
        "title": title,
        "description": description,
        "purpose": description,
        "page_kind": "static_page",
        "primary_actions": ["Open", "Continue"],
        "data_dependencies": dynamic_dependencies,
        "loading_state": "Loading content…" if dynamic_dependencies else "",
        "empty_state": "No items yet." if dynamic_dependencies else "",
        "error_state": "Something went wrong." if dynamic_dependencies else "",
    }


def _page_file_payload(payload: dict) -> dict:
    page = payload["page"]
    role = payload["role"]
    role_titles = {
        "client": ("Client booking", "Start the customer booking flow and open the profile."),
        "specialist": ("Specialist desk", "Review the active workload and open the profile workspace."),
        "manager": ("Manager overview", "Inspect operations and open the manager profile workspace."),
    }
    default_title, default_description = role_titles.get(role, ("Page", "Open the page."))
    title = str(page.get("title") or page.get("navigation_label") or default_title)
    description = str(page.get("description") or page.get("purpose") or default_description)
    profile_link = f"/{role}/profile"
    card_href = profile_link if page["route_path"] == f"/{role}" else f"/{role}"
    content = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
    <link rel="stylesheet" href="/static/{role}/styles.css" />
  </head>
  <body>
    <main class="page-shell">
      <section class="page">
        <header class="header">
          <h1 class="title">{title}</h1>
        </header>
        <section class="feature-block">
          <div class="feature-content">
            <span class="feature-title">{description}</span>
            <a class="card-link" href="{card_href}">Open</a>
          </div>
        </section>
      </section>
    </main>
    <script src="/static/{role}/{'profile.js' if page['route_path'].endswith('/profile') else 'app.js'}" defer></script>
  </body>
</html>
"""
    return {
        "assistant_message": f"Generated {page['file_path']}.",
        "operations": [
            {
                "file_path": page["file_path"],
                "operation": "replace",
                "content": content,
                "reason": f"Implement the {title} page.",
            }
        ],
    }


def _composition_payload(payload: dict) -> dict:
    page_graph = payload["page_graph"]
    target_files = set(payload["target_files"])
    operations: list[dict] = []
    if "miniapp/app/main.py" in target_files:
        operations.append(
            {
                "file_path": "miniapp/app/main.py",
                "operation": "replace",
                "content": """from pathlib import Path\n\nfrom fastapi import FastAPI\nfrom fastapi.responses import FileResponse, JSONResponse, RedirectResponse\nfrom fastapi.staticfiles import StaticFiles\n\nfrom app.db import Base, engine\nfrom app.routes.health import router as health_router\nfrom app.routes.profiles import router as profiles_router\n\nSTATIC_DIR = Path(__file__).resolve().parent / 'static'\nROLES = ('client', 'specialist', 'manager')\n\napp = FastAPI()\napp.include_router(health_router)\napp.include_router(profiles_router)\napp.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')\n\n@app.on_event('startup')\ndef startup() -> None:\n    Base.metadata.create_all(bind=engine)\n\n@app.get('/')\ndef index() -> RedirectResponse:\n    return RedirectResponse('/client', status_code=307)\n\n@app.get('/{role}')\ndef role_page(role: str) -> FileResponse:\n    if role not in ROLES:\n        raise KeyError(role)\n    return FileResponse(STATIC_DIR / role / 'index.html')\n\n@app.get('/{role}/profile')\ndef role_profile(role: str) -> FileResponse:\n    if role not in ROLES:\n        raise KeyError(role)\n    return FileResponse(STATIC_DIR / role / 'profile.html')\n\n@app.get('/{role}/{page_slug}')\ndef role_subpage(role: str, page_slug: str) -> FileResponse:\n    if role not in ROLES:\n        raise KeyError(role)\n    page_path = STATIC_DIR / role / f'{page_slug}.html'\n    if not page_path.exists():\n        raise KeyError(f'{role}/{page_slug}')\n    return FileResponse(page_path)\n\n@app.exception_handler(KeyError)\ndef key_error_handler(_, exc: KeyError) -> JSONResponse:\n    return JSONResponse(status_code=404, content={'detail': str(exc)})\n""",
                "reason": "Provide the miniapp-served static entrypoint.",
            }
        )
    if "miniapp/app/db.py" in target_files:
        operations.append(
            {
                "file_path": "miniapp/app/db.py",
                "operation": "replace",
                "content": """from datetime import datetime, timezone\nfrom pathlib import Path\n\nfrom sqlalchemy import DateTime, String, create_engine\nfrom sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker\n\nDATABASE_URL = 'sqlite:///./app/generated/app.db'\nengine = create_engine(DATABASE_URL, future=True, connect_args={'check_same_thread': False})\nSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)\n\nclass Base(DeclarativeBase):\n    pass\n\nclass RoleProfileRecord(Base):\n    __tablename__ = 'role_profiles'\n    role: Mapped[str] = mapped_column(String(32), primary_key=True)\n    first_name: Mapped[str] = mapped_column(String(255), default='Ivan')\n    last_name: Mapped[str] = mapped_column(String(255), default='Ivanov')\n    email: Mapped[str] = mapped_column(String(255), default='')\n    phone: Mapped[str] = mapped_column(String(255), default='')\n    photo_url: Mapped[str | None] = mapped_column(String(4096), nullable=True)\n    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))\n""",
                "reason": "Provide the SQLite-backed storage module.",
            }
        )
    if "miniapp/app/routes/profiles.py" in target_files:
        operations.append(
            {
                "file_path": "miniapp/app/routes/profiles.py",
                "operation": "replace",
                "content": """from datetime import datetime, timezone\n\nfrom fastapi import APIRouter\n\nfrom app.db import RoleProfileRecord, SessionLocal\nfrom app.schemas import AppRole, RoleProfile\n\nrouter = APIRouter(prefix='/api/profiles', tags=['profiles'])\nDEFAULT_PROFILES = {\n    'client': {'first_name': 'Ivan', 'last_name': 'Ivanov', 'email': '', 'phone': '', 'photo_url': None},\n    'specialist': {'first_name': 'Ivan', 'last_name': 'Ivanov', 'email': '', 'phone': '', 'photo_url': None},\n    'manager': {'first_name': 'Ivan', 'last_name': 'Ivanov', 'email': '', 'phone': '', 'photo_url': None},\n}\n\ndef _to_schema(record: RoleProfileRecord) -> RoleProfile:\n    return RoleProfile(first_name=record.first_name, last_name=record.last_name, email=record.email, phone=record.phone, photo_url=record.photo_url, updated_at=record.updated_at)\n\n@router.get('/{role}', response_model=RoleProfile)\ndef get_profile(role: AppRole) -> RoleProfile:\n    with SessionLocal() as session:\n        record = session.get(RoleProfileRecord, role)\n        if record is None:\n            record = RoleProfileRecord(role=role, **DEFAULT_PROFILES[role])\n            session.add(record)\n            session.commit()\n            session.refresh(record)\n        return _to_schema(record)\n\n@router.put('/{role}', response_model=RoleProfile)\ndef update_profile(role: AppRole, profile: RoleProfile) -> RoleProfile:\n    with SessionLocal() as session:\n        record = session.get(RoleProfileRecord, role)\n        if record is None:\n            record = RoleProfileRecord(role=role, **DEFAULT_PROFILES[role])\n            session.add(record)\n        record.first_name = profile.first_name\n        record.last_name = profile.last_name\n        record.email = profile.email\n        record.phone = profile.phone\n        record.photo_url = profile.photo_url\n        record.updated_at = datetime.now(timezone.utc)\n        session.commit()\n        session.refresh(record)\n        return _to_schema(record)\n""",
                "reason": "Provide the shared profile persistence API.",
            }
        )
    for role in ("client", "specialist", "manager"):
        if f"miniapp/app/static/{role}/styles.css" in target_files:
            operations.append(
                {
                    "file_path": f"miniapp/app/static/{role}/styles.css",
                    "operation": "replace",
                    "content": ":root { --app-max-width: 620px; --surface-page: #f5f7fb; --surface-card: #f8fafd; --surface-primary: #ffffff; --text-primary: #16263d; --text-secondary: #4d607d; --text-tertiary: #7386a3; --accent: #2d7ff9; --accent-contrast: #ffffff; --accent-soft: #e6f0ff; --border-subtle: #dde6f3; --shadow-soft: 0 14px 38px rgba(22, 49, 95, 0.12); }\n* { box-sizing: border-box; }\nhtml, body { margin: 0; min-height: 100%; }\nbody { font-family: 'Inter', 'Segoe UI', sans-serif; background: var(--surface-page); color: var(--text-primary); }\n.page-shell { max-width: var(--app-max-width); min-height: 100vh; margin: 0 auto; padding: 60px 16px 20px; }\n.page { display: flex; flex-direction: column; gap: 12px; }\n.feature-block, .preview-card, .form-card, .card { border: 1px solid var(--border-subtle); border-radius: 24px; background: var(--surface-primary); box-shadow: var(--shadow-soft); }\n.card { padding: 14px; display: grid; grid-template-columns: auto 1fr auto; gap: 14px; align-items: center; text-decoration: none; color: inherit; }\n.avatar-wrap, .avatar-large-wrap { width: 74px; height: 74px; border-radius: 50%; overflow: hidden; }\n.avatar-large-wrap { width: 96px; height: 96px; }\n.avatar, .avatar-large { width: 100%; height: 100%; object-fit: cover; }\n.avatar-fallback, .avatar-large-fallback { width: 100%; height: 100%; display: grid; place-items: center; border-radius: 50%; background: var(--accent-soft); font-weight: 700; }\n.info, .preview-info, .feature-content, .form-card { display: flex; flex-direction: column; }\n.name, .preview-name, .title, .feature-title { font-weight: 700; }\n.preview-card, .form-card, .feature-block { padding: 16px; }\n.input-wrapper { position: relative; margin-top: 6px; }\n.input-label { position: absolute; top: -8px; left: 10px; padding: 0 5px; background: var(--surface-primary); font-size: 14px; font-weight: 700; }\n.text-input { width: 100%; min-height: 50px; padding: 12px 14px; border: 1px solid var(--border-subtle); border-radius: 10px; }\n.error { display: none; margin-top: 4px; padding-left: 2px; font-size: 12px; color: #ef4444; }\n.error:not(:empty) { display: block; }\n.primary-button { margin-top: 8px; height: 48px; border: none; border-radius: 12px; background: var(--accent); color: var(--accent-contrast); }\n",
                    "reason": f"Provide the shared static styles for {role}.",
                }
            )
        if f"miniapp/app/static/{role}/app.js" in target_files:
            operations.append(
                {
                    "file_path": f"miniapp/app/static/{role}/app.js",
                    "operation": "replace",
                    "content": f"const role = '{role}';\nfetch(`/api/profiles/${{role}}`).then((response) => response.json()).then((profile) => {{ const name = `${{profile.first_name || ''}} ${{profile.last_name || ''}}`.trim() || 'Ivan Ivanov'; document.getElementById('profile-name').textContent = name; document.getElementById('profile-avatar').innerHTML = profile.photo_url ? `<img class=\"avatar\" src=\"${{profile.photo_url}}\" alt=\"\" />` : '<div class=\"avatar-fallback\">II</div>'; }});\n",
                    "reason": f"Provide the landing-page script for {role}.",
                }
            )
        if f"miniapp/app/static/{role}/profile.js" in target_files:
            operations.append(
                {
                    "file_path": f"miniapp/app/static/{role}/profile.js",
                    "operation": "replace",
                    "content": f"const role = '{role}'; const form = document.getElementById('profile-form'); const saveButton = document.getElementById('save-button'); let currentPhotoUrl = null; fetch(`/api/profiles/${{role}}`).then((response) => response.json()).then((profile) => {{ currentPhotoUrl = profile.photo_url; form.elements.first_name.value = profile.first_name || ''; form.elements.last_name.value = profile.last_name || ''; form.elements.email.value = profile.email || ''; form.elements.phone.value = profile.phone || ''; document.getElementById('preview-name').textContent = `${{profile.first_name || ''}} ${{profile.last_name || ''}}`.trim() || 'Ivan Ivanov'; }}); form.addEventListener('submit', async (event) => {{ event.preventDefault(); document.getElementById('email-error').textContent = ''; document.getElementById('phone-error').textContent = ''; const payload = {{ first_name: form.elements.first_name.value.trim(), last_name: form.elements.last_name.value.trim(), email: form.elements.email.value.trim(), phone: form.elements.phone.value.trim(), photo_url: currentPhotoUrl }}; if (!payload.email) {{ document.getElementById('email-error').textContent = 'Enter an email address'; return; }} if (!payload.phone) {{ document.getElementById('phone-error').textContent = 'Enter a phone number'; return; }} saveButton.textContent = 'Saving...'; const response = await fetch(`/api/profiles/${{role}}`, {{ method: 'PUT', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }}); const profile = await response.json(); document.getElementById('preview-name').textContent = `${{profile.first_name || ''}} ${{profile.last_name || ''}}`.trim() || 'Ivan Ivanov'; saveButton.textContent = 'Saved'; setTimeout(() => {{ saveButton.textContent = 'Save'; }}, 1200); }});\n",
                    "reason": f"Provide the profile-page script for {role}.",
                }
            )
    return {"assistant_message": "Composed shared files and routes.", "operations": operations}


def _whole_file_bundle_payload(payload: dict) -> dict:
    target_files = set(payload.get("cluster_targets") or [])
    page_graph = payload.get("page_graph") or {}
    operations = list(_composition_payload({"target_files": list(target_files), "page_graph": page_graph})["operations"])

    for role, role_payload in (page_graph.get("roles") or {}).items():
        for page in role_payload.get("pages") or []:
            file_path = page.get("file_path")
            if file_path not in target_files:
                continue
            page_payload = {
                "role": role,
                "page": page,
            }
            operations.extend(_page_file_payload(page_payload)["operations"])

    if any(path.startswith("miniapp/") for path in target_files):
        for path in sorted(target_files):
            if path == "miniapp/app/main.py":
                continue
            elif path == "miniapp/app/schemas.py":
                operations.append(
                    {
                        "file_path": path,
                        "operation": "replace",
                        "content": "from datetime import datetime\nfrom typing import Literal\nfrom pydantic import BaseModel\n\nAppRole = Literal['client', 'specialist', 'manager']\n\nclass RoleProfile(BaseModel):\n    first_name: str\n    last_name: str = ''\n    email: str = ''\n    phone: str = ''\n    photo_url: str | None = None\n    updated_at: datetime | None = None\n",
                        "reason": "Provide minimal miniapp schemas.",
                    }
                )
            elif path.startswith("miniapp/app/routes/") and path != "miniapp/app/routes/profiles.py":
                operations.append(
                    {
                        "file_path": path,
                        "operation": "replace",
                        "content": "from fastapi import APIRouter\n\nrouter = APIRouter()\n",
                        "reason": f"Provide a minimal miniapp route for {path}.",
                    }
                )

    return {"assistant_message": "Generated whole-file bundle.", "operations": operations}

def _fix_patch_payload(payload: dict) -> dict:
    fix_case = payload.get("fix_case") or {}
    file_contexts = payload.get("file_contexts") or {}
    operations: list[dict] = []
    rationale: dict[str, str] = {}

    for file_path, content in file_contexts.items():
        if file_path.endswith("ClientRoutes.tsx") and "ClientMyBookingsPage" in content:
            operations.append(
                {
                    "file_path": file_path,
                    "operation": "replace",
                    "content": content.replace(
                        "import { ClientMyBookingsPage } from '@/roles/client/pages/MyBookingsPage';",
                        "import ClientMyBookingsPage from '@/roles/client/pages/MyBookingsPage';",
                    ),
                    "reason": "Align the client route import with the page module's default export.",
                }
            )
            rationale[file_path] = "Switch the route import to the page's default export."
        elif file_path.endswith("ManagerRoutes.tsx") and "ManagerServicesManagementPage" in content:
            next_content = content.replace(
                "import { ManagerServicesManagementPage } from '@/roles/manager/pages/ServicesManagementPage';",
                "import ManagerServicesManagementPage from '@/roles/manager/pages/ServicesManagementPage';",
            ).replace(
                "import { ManagerBookingsOverviewPage } from '@/roles/manager/pages/BookingsOverviewPage';",
                "import ManagerBookingsOverviewPage from '@/roles/manager/pages/BookingsOverviewPage';",
            )
            operations.append(
                {
                    "file_path": file_path,
                    "operation": "replace",
                    "content": next_content,
                    "reason": "Align manager route imports with the pages' default exports.",
                }
            )
            rationale[file_path] = "Switch the manager route imports to default exports."
        elif file_path.endswith("BookingFormPage.tsx") and "phone: undefined" in content:
            operations.append(
                {
                    "file_path": file_path,
                    "operation": "replace",
                    "content": content.replace(
                        "setErrors((prev) => ({ ...prev, phone: undefined }));",
                        "setErrors((prev) => {\n    const next = { ...prev };\n    delete next.phone;\n    return next;\n  });",
                    ),
                    "reason": "Delete the error key instead of writing undefined into Record<string, string>.",
                }
            )
            rationale[file_path] = "Keep the updater return type compatible with Record<string, string>."

    if not operations and file_contexts:
        first_path = next(iter(file_contexts))
        operations.append(
            {
                "file_path": first_path,
                "operation": "replace",
                "content": file_contexts[first_path],
                "reason": "No-op fallback for test stubs.",
            }
        )
        rationale[first_path] = "Fallback test stub patch."

    return {
        "diagnosis": str(fix_case.get("root_cause_summary") or "Apply the smallest targeted fix."),
        "planned_targets": list(file_contexts.keys()),
        "expected_verification": "npm run build should pass and the preview runtime should stay healthy.",
        "rationale_by_file": rationale,
        "operations": operations,
    }


def test_generation_pipeline_smoke(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Smoke Workspace",
            "description": "End-to-end smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    document = client.post(
        f"/workspaces/{workspace_id}/documents",
        json={
            "file_name": "requirements.md",
            "file_path": "docs/requirements.md",
            "source_type": "project_doc",
            "content": "Booking form with name, phone, date and comment fields.",
        },
    ).json()
    index_response = client.post(f"/documents/{document['document_id']}/index")
    assert index_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Create a consultation booking mini-app with name phone date and comment fields.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "basic",
        },
    )
    assert run_response.status_code == 200
    run = run_response.json()

    final_run = run
    for _ in range(90):
        current = client.get(f"/runs/{run['run_id']}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["status"] == "awaiting_approval"
    assert final_run["apply_status"] == "awaiting_approval"
    assert final_run["draft_ready"] is True

    spec_payload = client.get(f"/workspaces/{workspace_id}/spec/current").json()
    validation_payload = client.get(f"/workspaces/{workspace_id}/validation/current").json()
    preview_payload = client.get(f"/workspaces/{workspace_id}/preview/url").json()
    file_tree = client.get(f"/workspaces/{workspace_id}/files/tree").json()
    draft_tree = client.get(f"/workspaces/{workspace_id}/files/tree?run_id={run['run_id']}").json()
    draft_diff = client.get(f"/workspaces/{workspace_id}/diff?run_id={run['run_id']}").json()["diff"]

    assert spec_payload["product_goal"].startswith("Create a consultation booking")
    assert validation_payload["blocking"] is False
    assert preview_payload["url"] is None or preview_payload["url"].startswith("http://localhost:")
    if preview_payload["url"] is None:
        assert preview_payload["role_urls"] == {}
    else:
        assert preview_payload["role_urls"]["client"].startswith(preview_payload["url"])
    assert all("__pycache__" not in item["path"] for item in file_tree)
    assert all(not item["path"].endswith(".tsbuildinfo") for item in file_tree)
    assert any(item["path"] == "artifacts/grounded_spec.json" for item in draft_tree)
    assert any(item["path"] == "artifacts/generated_app_graph.json" for item in draft_tree)
    assert "miniapp/app/static/client/index.html" in draft_diff
    assert "miniapp/app/static/client/catalog.html" in draft_diff
    assert "generated_app_graph.json" in draft_diff

    preview_response = client.get(f"/preview/{workspace_id}?role=manager&run_id={run['run_id']}")
    assert preview_response.status_code == 200
    assert "booking" in preview_response.text.lower()

    approve_response = client.post(f"/runs/{run['run_id']}/approve")
    assert approve_response.status_code == 200
    approved_run = approve_response.json()
    assert approved_run["status"] == "completed"
    assert approved_run["apply_status"] == "applied"
    assert approved_run["draft_status"] == "approved"

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "docs/manual-note.md",
            "content": "# Manual edit\n\nThis file was added in the smoke test.\n",
        },
    )
    assert save_response.status_code == 200

    diff_response = client.get(f"/workspaces/{workspace_id}/diff")
    assert diff_response.status_code == 200
    assert "manual-note.md" in diff_response.json()["diff"]

    zip_export = client.post(f"/workspaces/{workspace_id}/export/zip").json()
    patch_export = client.post(f"/workspaces/{workspace_id}/export/git-patch").json()
    assert Path(zip_export["file_path"]).exists()
    assert Path(patch_export["file_path"]).exists()

    events_response = client.get(f"/jobs/{final_run['linked_job_id']}/events")
    assert events_response.status_code == 200
    assert "job_completed" in events_response.text


def test_quality_mode_blocks_without_openrouter(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Quality Workspace",
            "description": "Quality mode should block without OpenRouter",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Build a high-quality role-aware consultation workflow mini-app.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "quality",
        },
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "blocked"
    assert payload["failure_reason"]


def test_run_api_exposes_artifacts_and_links_job(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Run Workspace",
            "description": "Run API smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Refine the existing mini-app with a stronger AI workspace framing and preserved role previews.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "basic",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["status"] in {"pending", "running", "awaiting_approval"}

    final_run = run_payload
    for _ in range(90):
        current = client.get(f"/runs/{run_payload['run_id']}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["linked_job_id"]
    assert final_run["status"] == "awaiting_approval"
    assert final_run["apply_status"] == "awaiting_approval"
    assert final_run["draft_status"] == "ready"
    assert final_run["draft_ready"] is True

    list_response = client.get(f"/workspaces/{workspace_id}/runs")
    assert list_response.status_code == 200
    listed_runs = list_response.json()
    assert listed_runs[0]["run_id"] == final_run["run_id"]

    artifact_response = client.get(f"/runs/{final_run['run_id']}/artifacts")
    assert artifact_response.status_code == 200
    artifacts = artifact_response.json()
    assert artifacts["run"]["run_id"] == final_run["run_id"]
    assert artifacts["job"]["job_id"] == final_run["linked_job_id"]
    assert artifacts["code_change_plan"]["summary"]
    assert isinstance(artifacts["code_change_plan"]["targets"], list)
    assert artifacts["page_graph"]["page_graph"]["roles"]["client"]["pages"]
    assert artifacts["role_contract"]["role_contract"]["roles"]["client"]["responsibility"]
    assert isinstance(artifacts["iterations"], list)
    assert artifacts["candidate_diff"]
    assert "validation" in artifacts
    assert "trace" in artifacts

    iterations_response = client.get(f"/runs/{final_run['run_id']}/iterations")
    assert iterations_response.status_code == 200
    iterations = iterations_response.json()
    assert iterations
    assert iterations[0]["role_scope"] == ["client"]

    discard_response = client.post(f"/runs/{final_run['run_id']}/discard")
    assert discard_response.status_code == 200
    discarded_run = discard_response.json()
    assert discarded_run["draft_status"] == "discarded"


def test_generate_endpoint_acts_as_compatibility_shim(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Compat Workspace",
            "description": "Generate endpoint compatibility smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Create a booking mini-app draft through the compatibility endpoint.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "basic",
        },
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "completed"
    assert payload["summary"]
    file_tree = client.get(f"/workspaces/{workspace_id}/files/tree").json()
    assert all("__pycache__" not in item["path"] for item in file_tree)
    assert all(not item["path"].endswith(".tsbuildinfo") for item in file_tree)


def test_clone_template_boots_preview_automatically(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Workspace",
            "description": "Preview should boot after cloning",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    preview_payload = None
    for _ in range(20):
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
        assert response.status_code == 200
        preview_payload = response.json()
        if preview_payload["status"] in {"running", "error"}:
            break
        time.sleep(0.1)

    assert preview_payload is not None
    assert preview_payload["status"] in {"running", "error"}


def test_preview_url_waits_for_http_readiness_before_exposing_url(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Readiness Workspace",
            "description": "Preview URL should stay hidden until runtime pages respond.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    container = app.state.container
    preview = container.preview_service._get_or_create(workspace_id)
    preview.runtime_mode = "docker"
    preview.status = "running"
    preview.stage = "running"
    preview.url = "http://localhost:19999"
    preview.frontend_url = preview.url
    preview.backend_url = f"{preview.url}/api"
    preview.proxy_port = 19999
    preview.project_name = "grounded_preview_test"
    container.preview_service._persist(preview)

    original_probe = container.preview_service._http_preview_ready
    original_inspect = container.runtime_manager.inspect_containers
    container.preview_service._http_preview_ready = lambda preview_url: False
    container.runtime_manager.inspect_containers = (
        lambda current_workspace_id, source_dir, proxy_port: [{"state": "running", "published_port": str(proxy_port or 19999)}]
    )
    try:
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
    finally:
        container.preview_service._http_preview_ready = original_probe
        container.runtime_manager.inspect_containers = original_inspect

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["url"] == "http://localhost:19999"
    assert payload["role_urls"]["client"] == "http://localhost:19999/client"
