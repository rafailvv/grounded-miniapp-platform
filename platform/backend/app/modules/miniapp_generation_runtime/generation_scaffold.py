from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.models.grounded_spec import GroundedSpecModel


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
            "name": "workspace-knowledge-hub",
            "focus": "Entity-centric workspace with dense detail and history views.",
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
    thin_core_backend_targets: tuple[str, ...],
    thin_optional_route_targets: dict[str, str],
    thin_role_page_blueprints: dict[str, tuple[dict[str, str], ...]],
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
    backend_targets = list(thin_core_backend_targets)
    inferred_route_targets = thin_backend_targets_from_spec(
        prompt=prompt,
        grounded_spec=grounded_spec,
        role_scope=role_scope,
        thin_optional_route_targets=thin_optional_route_targets,
    )
    backend_targets.extend(inferred_route_targets)
    for role in role_scope:
        pages = thin_role_pages_for_role(
            role=role,
            prompt=prompt,
            grounded_spec=grounded_spec,
            thin_role_page_blueprints=thin_role_page_blueprints,
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
    return (
        {"roles": role_contract_roles, "source": "thin_loop"},
        {
            "page_graph": {"roles": page_graph_roles, "backend_targets": backend_targets, "flow_mode": "multi_page"},
            "scope_mode": "whole_file_build",
            "write_strategy": "whole_file_build",
            "strategy_reason": "Thin scaffold-driven generation pipeline.",
            "flow_mode": "multi_page",
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "execution_plan": execution_plan,
            "generation_clusters": generation_clusters,
            "require_multi_page": True,
            "require_business_pages": True,
        },
    )


def thin_role_pages_for_role(
    *,
    role: str,
    prompt: str,
    grounded_spec: GroundedSpecModel,
    thin_role_page_blueprints: dict[str, tuple[dict[str, str], ...]],
    default_page_file,
    default_page_asset_path,
    default_handoff_paths_for_page_kind,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    include_create = role == "client"
    include_workload = role == "manager"
    include_requests = True
    include_detail = True
    if mentions_schedule_or_time(prompt, grounded_spec):
        include_create = True
    for blueprint in thin_role_page_blueprints.get(role, ()):
        route_path = str(blueprint["route_path"])
        if route_path == "/create" and not include_create:
            continue
        if route_path == "/workload" and not include_workload:
            continue
        if route_path == "/requests" and not include_requests:
            continue
        if route_path == "/requests/:requestId" and not include_detail:
            continue
        file_path = default_page_file(role, f"{role}_{blueprint['title']}", route_path=route_path)
        pages.append(
            {
                "page_id": f"{role}_{thin_page_slug_for_route(route_path)}",
                "route_path": route_path,
                "file_path": file_path,
                "style_path": default_page_asset_path(file_path, asset_kind="css"),
                "script_path": default_page_asset_path(file_path, asset_kind="js"),
                "page_kind": blueprint["page_kind"],
                "navigation_label": blueprint["navigation_label"],
                "title": blueprint["title"],
                "is_entry": route_path == "/",
                "handoff_paths": default_handoff_paths_for_page_kind(
                    str(blueprint["page_kind"]),
                    route_path=route_path,
                ),
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
    thin_optional_route_targets: dict[str, str],
) -> list[str]:
    targets = [thin_optional_route_targets["requests"]]
    evidence = " ".join(
        [
            prompt.lower(),
            " ".join(item.path.lower() for item in grounded_spec.api_requirements),
            " ".join(item.name.lower() for item in grounded_spec.api_requirements),
            " ".join(item.name.lower() for item in grounded_spec.domain_entities),
            " ".join(item.goal.lower() for item in grounded_spec.user_flows),
        ]
    )
    if any(token in evidence for token in ("comment", "progress", "note", "completion")):
        targets.append(thin_optional_route_targets["comments"])
    if any(role in role_scope for role in ("specialist", "manager")):
        targets.append(thin_optional_route_targets["assignments"])
    if "manager" in role_scope:
        targets.extend([thin_optional_route_targets["users"], thin_optional_route_targets["workload"]])
    if mentions_schedule_or_time(prompt, grounded_spec):
        targets.append(thin_optional_route_targets["time_slots"])
    return list(dict.fromkeys(targets))


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
    role_map = {
        "client": "Create requests, provide details, choose timing, and track status.",
        "specialist": "Work assigned requests, update status, and leave progress comments.",
        "manager": "Review requests, assign specialists, and monitor workload.",
    }
    return role_map.get(role, grounded_spec.product_goal)
