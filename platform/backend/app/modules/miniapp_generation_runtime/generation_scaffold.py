from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


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
