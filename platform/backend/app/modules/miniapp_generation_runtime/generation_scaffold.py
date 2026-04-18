from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.models.grounded_spec import GroundedSpecModel

MINIMAL_BOOTSTRAP_TARGETS = (
    "miniapp/app/main.py",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/routes/profiles.py",
    "miniapp/app/routes/runtime.py",
)

def select_creative_direction(prompt: str) -> dict[str, Any]:
    strategies = [
        {
            "name": "workflow-command-center",
            "focus": "Operations-first control surface with strong status visibility.",
            "layout_bias": "dashboard",
            "interaction_bias": "bulk operations and quick triage actions",
            "tone": "decisive",
        },
        {
            "name": "guided-service-journey",
            "focus": "Step-based journey that emphasizes clarity and completion confidence.",
            "layout_bias": "stream",
            "interaction_bias": "guided progression with contextual details",
            "tone": "supportive",
        },
        {
            "name": "entity-history-surface",
            "focus": "Entity-centric detail surface with dense history and relationship views.",
            "layout_bias": "magazine",
            "interaction_bias": "exploration and drill-down",
            "tone": "analytical",
        },
        {
            "name": "lean-minimal-ops",
            "focus": "Minimal but complete flows with reduced chrome and direct actions.",
            "layout_bias": "minimal",
            "interaction_bias": "direct action with low navigation overhead",
            "tone": "concise",
        },
    ]
    seed = f"{prompt}|creative|{datetime.now(timezone.utc).isoformat(timespec='microseconds')}"
    index = sum(ord(ch) for ch in seed) % len(strategies)
    selected = dict(strategies[index])
    selected["seed"] = seed[-16:]
    return selected


def build_route_manifest(runtime_manifest: dict[str, Any], *, role_order: tuple[str, ...]) -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for role, payload in (runtime_manifest.get("roles") or {}).items():
        if role not in role_order or not isinstance(payload, dict):
            continue
        pages: list[dict[str, Any]] = []
        for route in payload.get("routes") or []:
            if not isinstance(route, dict):
                continue
            route_path = str(route.get("path") or "/").strip() or "/"
            route_path = "/" if route_path == "/" else f"/{route_path.strip('/')}"
            if route_path == "/":
                file_path = f"miniapp/app/static/{role}/index.html"
                style_path = f"miniapp/app/static/{role}/styles.css"
                script_path = f"miniapp/app/static/{role}/app.js"
            else:
                route_slug = re.sub(r":[^/]+", "detail", route_path.strip("/"))
                route_slug = route_slug.replace("/", "_").replace("-", "_")
                route_slug = re.sub(r"[^a-z0-9_]+", "_", route_slug.lower()).strip("_")
                route_slug = re.sub(r"_+", "_", route_slug)
                page_slug = route_slug or "page"
                file_path = f"miniapp/app/static/{role}/{page_slug}/index.html"
                style_path = f"miniapp/app/static/{role}/{page_slug}/styles.css"
                script_path = f"miniapp/app/static/{role}/{page_slug}/app.js"
            screen = ((payload.get("screens") or {}) or {}).get(route.get("screen_id")) or {}
            pages.append(
                {
                    "page_id": str(route.get("screen_id") or route.get("route_id") or f"{role}_{len(pages) + 1}"),
                    "route_path": route_path,
                    "file_path": file_path,
                    "style_path": style_path,
                    "script_path": script_path,
                    "page_kind": str(screen.get("kind") or "page"),
                    "navigation_label": str(route.get("label") or screen.get("title") or "Open"),
                    "title": str(screen.get("title") or route.get("label") or "Page"),
                    "is_entry": bool(route.get("is_entry") or route_path == "/"),
                }
            )
        roles[role] = {
            "entry_path": str(payload.get("entry_path") or "/"),
            "pages": pages,
        }
    return {"roles": roles}


def compile_prompt_to_scaffold(
    *,
    prompt: str,
    grounded_spec: GroundedSpecModel,
    role_scope: list[str],
    workspace_tree: list[dict[str, str]],
    design_reference_files: tuple[str, ...],
    default_page_file,
    default_page_asset_path,
    default_handoff_paths_for_page_kind,
    canonicalize_target_files,
    sanitize_planner_target_files,
    sanitize_backend_targets,
    collect_files_to_read,
    build_generation_clusters,
    build_execution_plan,
) -> tuple[dict[str, Any], dict[str, Any]]:
    page_graph_roles: dict[str, dict[str, Any]] = {}
    role_contract_roles: dict[str, dict[str, Any]] = {}
    target_files: list[str] = []
    backend_targets = list(MINIMAL_BOOTSTRAP_TARGETS)
    for role in role_scope:
        pages = thin_role_pages_for_role(
            role=role,
            prompt=prompt,
            grounded_spec=grounded_spec,
            default_page_file=default_page_file,
            default_page_asset_path=default_page_asset_path,
            default_handoff_paths_for_page_kind=default_handoff_paths_for_page_kind,
        )
        role_routes_file = f"miniapp/app/routes/{role}.py"
        page_graph_roles[role] = {
            "entry_path": f"/{role}",
            "routes_file": role_routes_file,
            "pages": pages,
        }
        role_contract_roles[role] = {
            "responsibility": thin_role_responsibility(role, grounded_spec),
            "pages": [str(page["page_id"]) for page in pages],
        }
        backend_targets.append(role_routes_file)
        for page in pages:
            target_files.extend([page["file_path"], page["style_path"], page["script_path"]])
    target_files.extend(backend_targets)
    target_files = sanitize_planner_target_files(
        target_files=canonicalize_target_files(target_files, scope_mode="whole_file_build"),
        backend_targets=backend_targets,
        page_graph={"roles": page_graph_roles},
    )
    backend_targets = sanitize_backend_targets(backend_targets)
    shared_files = [path for path in design_reference_files if path.startswith("miniapp/app/static/shared/")]
    files_to_read = collect_files_to_read(list(design_reference_files), target_files, workspace_tree)
    generation_clusters = build_generation_clusters(target_files)
    execution_plan = build_execution_plan(
        role_scope=role_scope,
        roles=page_graph_roles,
        shared_files=shared_files,
        backend_targets=backend_targets,
        target_files=target_files,
        generation_clusters=generation_clusters,
    )
    flow_mode = "multi_page" if any(len((page_graph_roles.get(role) or {}).get("pages") or []) > 1 for role in role_scope) else "single_page"
    return (
        {"roles": role_contract_roles, "source": "thin_loop"},
        {
            "page_graph": {"roles": page_graph_roles, "backend_targets": backend_targets, "flow_mode": flow_mode},
            "scope_mode": "whole_file_build",
            "write_strategy": "whole_file_build",
            "strategy_reason": "Minimal bootstrap scaffold compiled from prompt and template affordances as advisory generation input.",
            "flow_mode": flow_mode,
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "execution_plan": execution_plan,
            "generation_clusters": generation_clusters,
            "require_multi_page": flow_mode == "multi_page",
            "require_business_pages": False,
        },
    )


def thin_role_pages_for_role(
    *,
    role: str,
    prompt: str,
    grounded_spec: GroundedSpecModel,
    default_page_file,
    default_page_asset_path,
    default_handoff_paths_for_page_kind,
) -> list[dict[str, Any]]:
    route_specs = _derive_role_route_specs(role=role, prompt=prompt, grounded_spec=grounded_spec)
    all_routes = [spec["route_path"] for spec in route_specs]
    pages: list[dict[str, Any]] = []
    for spec in route_specs:
        route_path = str(spec["route_path"])
        title = str(spec["title"])
        page_kind = str(spec["page_kind"])
        file_path = default_page_file(role, f"{role}_{title}", route_path=route_path)
        pages.append(
            {
                "page_id": f"{role}_{thin_page_slug_for_route(route_path)}",
                "route_path": route_path,
                "file_path": file_path,
                "style_path": default_page_asset_path(file_path, asset_kind="css"),
                "script_path": default_page_asset_path(file_path, asset_kind="js"),
                "page_kind": page_kind,
                "navigation_label": str(spec["navigation_label"]),
                "title": title,
                "is_entry": route_path == "/",
                "handoff_paths": _handoff_paths_for_route(
                    route_path=route_path,
                    all_routes=all_routes,
                    default_handoff_paths_for_page_kind=default_handoff_paths_for_page_kind,
                    page_kind=page_kind,
                ),
                "purpose": str(spec["purpose"]),
                "data_dependencies": list(spec.get("data_dependencies") or []),
            }
        )
    return pages


def thin_page_slug_for_route(route_path: str) -> str:
    normalized = route_path.strip() or "/"
    if normalized == "/":
        return "index"
    slug = re.sub(r":[^/]+", "detail", normalized.strip("/"))
    slug = slug.replace("/", "_").replace("-", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_")
    return re.sub(r"_+", "_", slug) or "page"


def thin_backend_targets_from_spec(
    *,
    prompt: str,
    grounded_spec: GroundedSpecModel,
    role_scope: list[str],
) -> list[str]:
    del prompt, grounded_spec, role_scope
    return []


def mentions_schedule_or_time(prompt: str, grounded_spec: GroundedSpecModel) -> bool:
    haystack = " ".join(
        [
            prompt.lower(),
            " ".join(flow.goal.lower() for flow in grounded_spec.user_flows),
            " ".join(req.description.lower() for req in grounded_spec.ui_requirements),
        ]
    )
    return any(token in haystack for token in ("time", "slot", "schedule", "calendar", "booking"))


def thin_role_responsibility(role: str, grounded_spec: GroundedSpecModel) -> str:
    actor = next((item for item in grounded_spec.actors if item.role == role), None)
    if actor is not None and getattr(actor, "description", None):
        return str(actor.description)
    return grounded_spec.product_goal


def _derive_role_route_specs(*, role: str, prompt: str, grounded_spec: GroundedSpecModel) -> list[dict[str, Any]]:
    route_specs: list[dict[str, Any]] = [
        {
            "route_path": "/",
            "page_kind": "landing",
            "navigation_label": "Home",
            "title": "Dashboard",
            "purpose": f"{role.capitalize()} entry surface for the prompt-defined workflow.",
            "data_dependencies": [],
        },
        {
            "route_path": "/profile",
            "page_kind": "profile",
            "navigation_label": "Profile",
            "title": "Profile",
            "purpose": f"Profile and settings for the {role} experience.",
            "data_dependencies": ["/api/profiles"],
        },
    ]
    seen_paths = {spec["route_path"] for spec in route_specs}
    for feature in _prompt_feature_routes(role=role, prompt=prompt, grounded_spec=grounded_spec):
        route_path = str(feature["route_path"])
        if route_path in seen_paths:
            continue
        seen_paths.add(route_path)
        route_specs.append(feature)
    return route_specs


def _role_evidence(*, role: str, prompt: str, grounded_spec: GroundedSpecModel) -> str:
    actor_ids = {actor.actor_id for actor in grounded_spec.actors if actor.role == role}
    flow_text = " ".join(
        " ".join(
            [
                flow.name,
                flow.goal,
                *[step.action for step in flow.steps if step.actor_id in actor_ids],
                *flow.acceptance_criteria,
                *flow.error_paths,
            ]
        )
        for flow in grounded_spec.user_flows
    )
    ui_text = " ".join(
        f"{item.description} {item.screen_hint or ''}"
        for item in grounded_spec.ui_requirements
        if role in f"{item.description} {item.screen_hint or ''}".lower()
    )
    api_text = " ".join(
        f"{item.name} {item.path} {item.purpose}"
        for item in grounded_spec.api_requirements
    )
    entity_text = " ".join(item.name for item in grounded_spec.domain_entities)
    return " ".join([prompt.lower(), flow_text.lower(), ui_text.lower(), api_text.lower(), entity_text.lower()])


def _dependencies_for_route(role: str, route_path: str) -> list[str]:
    del role
    if route_path == "/profile":
        return ["/api/profiles"]
    return []


def _prompt_feature_routes(*, role: str, prompt: str, grounded_spec: GroundedSpecModel) -> list[dict[str, Any]]:
    candidates: list[tuple[str, str, str]] = []
    for requirement in grounded_spec.api_requirements:
        path = str(requirement.path or "").strip()
        if not path:
            continue
        slug = _route_slug_from_text(path)
        if not slug or slug in {"profiles", "runtime", "health"}:
            continue
        title = str(requirement.name or slug.replace("_", " ").title()).strip()
        candidates.append((slug, title, str(requirement.purpose or "").strip()))
    for item in grounded_spec.ui_requirements:
        screen_hint = str(item.screen_hint or "").strip()
        if not screen_hint:
            continue
        slug = _route_slug_from_text(screen_hint)
        if not slug or slug in {"profile", "home", role}:
            continue
        candidates.append((slug, str(item.description or slug.replace("_", " ").title()).strip(), str(item.description or "").strip()))
    for flow in grounded_spec.user_flows:
        flow_text = " ".join([str(flow.name or ""), str(flow.goal or "")])
        slug = _route_slug_from_text(flow_text)
        if not slug or slug in {"profile", "dashboard", "home", role}:
            continue
        candidates.append((slug, str(flow.name or slug.replace("_", " ").title()).strip(), str(flow.goal or "").strip()))

    feature_routes: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    prompt_text = prompt.lower()
    role_evidence = _role_evidence(role=role, prompt=prompt, grounded_spec=grounded_spec)
    for slug, title, purpose in candidates:
        if slug in seen_slugs:
            continue
        if slug not in prompt_text and slug not in role_evidence:
            continue
        seen_slugs.add(slug)
        route_path = f"/{slug}"
        feature_routes.append(
            {
                "route_path": route_path,
                "page_kind": "feature",
                "navigation_label": title.split()[0] if title else slug.replace("_", " ").title(),
                "title": title or slug.replace("_", " ").title(),
                "purpose": purpose or f"{role.capitalize()} feature flow for {slug.replace('_', ' ')}.",
                "data_dependencies": _dependencies_for_prompt_feature(slug, grounded_spec),
            }
        )
        if len(feature_routes) >= 2:
            break
    return feature_routes


def _route_slug_from_text(text: str) -> str:
    lowered = str(text or "").lower()
    lowered = re.sub(r"^/api/", "", lowered)
    lowered = re.sub(r"^/+", "", lowered)
    lowered = re.sub(r"/+", "_", lowered)
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part and part not in {"api", "role", "client", "specialist", "manager"}]
    if not parts:
        return ""
    slug = "_".join(parts[:3]).strip("_")
    if slug in {"profile", "profiles", "runtime", "health", "dashboard", "landing"}:
        return ""
    return slug


def _dependencies_for_prompt_feature(slug: str, grounded_spec: GroundedSpecModel) -> list[str]:
    dependencies: list[str] = []
    normalized_slug = slug.replace("_", "-")
    for requirement in grounded_spec.api_requirements:
        path = str(requirement.path or "").strip()
        if not path:
            continue
        if normalized_slug in path or slug in path.replace("-", "_"):
            dependencies.append(path)
    return list(dict.fromkeys(dependencies[:3]))


def _handoff_paths_for_route(
    *,
    route_path: str,
    all_routes: list[str],
    default_handoff_paths_for_page_kind,
    page_kind: str,
) -> list[str]:
    defaults = list(default_handoff_paths_for_page_kind(page_kind, route_path=route_path))
    extras = [candidate for candidate in all_routes if candidate != route_path]
    ordered = [route_path, *defaults, *extras]
    seen: set[str] = set()
    handoffs: list[str] = []
    for item in ordered:
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        handoffs.append(normalized)
    return handoffs
