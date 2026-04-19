from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps


ROLE_ORDER = ("client", "specialist", "manager")


class ArtifactManifestsMixin:
    @staticmethod
    def _is_redundant_role_root_alias_page(
        role: str,
        page: dict[str, Any],
        *,
        normalized_route: str,
        has_canonical_entry_page: bool,
    ) -> bool:
        if not has_canonical_entry_page:
            return False
        if normalized_route not in {f"/{role}", f"/{role}/root"}:
            return False
        file_path = str(page.get("file_path") or "").strip().replace("\\", "/")
        if file_path == f"miniapp/app/static/{role}/root/index.html":
            return True
        route_path = str(page.get("route_path") or "").strip()
        return bool(route_path == f"/{role}/root" and file_path.startswith(f"miniapp/app/static/{role}/root/"))

    def _normalize_manifest_handoff_path(self, role: str, path: str) -> str:
        normalized_role_path = self._normalize_role_route_path(role, str(path or "").strip() or "/")
        return self._absolute_role_route_path(role, normalized_role_path)

    @staticmethod
    def _has_canonical_role_entry_page(role: str, pages: list[dict[str, Any]]) -> bool:
        for page in pages:
            if not isinstance(page, dict):
                continue
            route_path = str(page.get("route_path") or "").strip() or f"/{role}"
            if route_path in {"/", f"/{role}"}:
                return True
        return False

    def ensure_runtime_artifact_operations(
        self,
        *,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, Any],
        role_scope: list[str],
        generation_mode: GenerationMode,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        route_manifest = self.route_manifest_from_page_graph(page_graph, role_scope)
        runtime_manifest = self.runtime_manifest_from_page_graph(route_manifest, grounded_spec, generation_mode)
        required_artifacts = {
            "miniapp/app/generated/route_manifest.json": (route_manifest, "Persist the canonical route manifest for the generated role pages."),
            "miniapp/app/generated/runtime_manifest.json": (runtime_manifest, "Persist the lightweight runtime manifest for the generated role pages."),
        }
        ensured_operations = [operation for operation in operations if operation.file_path not in required_artifacts]
        for file_path, (payload, reason) in required_artifacts.items():
            ensured_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=json_dumps(payload),
                    reason=reason,
                )
            )
        return ensured_operations

    def route_manifest_from_page_graph(self, page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles: dict[str, Any] = {}
        role_payloads = page_graph.get("roles") or {}
        for role in role_scope:
            payload = role_payloads.get(role) or {}
            pages: list[dict[str, Any]] = []
            role_pages = [page for page in (payload.get("pages") or []) if isinstance(page, dict)]
            has_canonical_entry_page = self._has_canonical_role_entry_page(role, role_pages)
            for index, page in enumerate(role_pages):
                if not isinstance(page, dict):
                    continue
                route_path = str(page.get("route_path") or f"/{role}").strip() or f"/{role}"
                normalized_role_route = self._normalize_role_route_path(role, route_path)
                normalized_route = self._absolute_role_route_path(role, normalized_role_route)
                if self._is_redundant_role_root_alias_page(
                    role,
                    page,
                    normalized_route=normalized_route,
                    has_canonical_entry_page=has_canonical_entry_page,
                ):
                    continue
                page_kind = str(page.get("page_kind") or "").strip().lower()
                file_path = str(page.get("file_path") or f"miniapp/app/static/{role}/index.html")
                default_style_path = self._default_page_asset_path(file_path, "css")
                default_script_path = self._default_page_asset_path(file_path, "js")
                style_path = str(page.get("style_path") or default_style_path)
                script_path = str(page.get("script_path") or default_script_path)
                if not style_path.startswith(f"miniapp/app/static/{role}/"):
                    style_path = default_style_path
                if not script_path.startswith(f"miniapp/app/static/{role}/"):
                    script_path = default_script_path
                pages.append(
                    {
                        "page_id": str(page.get("page_id") or f"{role}_{index + 1}"),
                        "route_path": normalized_route,
                        "file_path": file_path,
                        "style_path": style_path,
                        "script_path": script_path,
                        "page_kind": page_kind or ("profile" if normalized_route.endswith("/profile") else "page"),
                        "navigation_label": str(page.get("navigation_label") or page.get("title") or "Open"),
                        "title": str(page.get("title") or page.get("navigation_label") or role.title()),
                        "handoff_paths": list(
                            dict.fromkeys(
                                [
                                    self._normalize_manifest_handoff_path(role, str(path))
                                    for path in (page.get("handoff_paths") or [])
                                    if isinstance(path, str)
                                    and self._normalize_manifest_handoff_path(role, str(path)) != normalized_route
                                ]
                            )
                        ),
                        "is_entry": bool(page.get("is_entry") or normalized_route == f"/{role}"),
                    }
                )
            roles[role] = {"entry_path": str(payload.get("entry_path") or f"/{role}"), "pages": pages}
        return {"roles": roles}

    def runtime_manifest_from_page_graph(
        self,
        route_manifest: dict[str, Any],
        grounded_spec: GroundedSpecModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        roles: dict[str, Any] = {}
        for role in ROLE_ORDER:
            role_payload = ((route_manifest.get("roles") or {}).get(role) or {})
            pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
            routes = []
            screens = {}
            navigation = []
            route_tree = []
            for index, page in enumerate(pages):
                route_path = str(page.get("route_path") or f"/{role}")
                screen_id = str(page.get("page_id") or f"{role}_{index + 1}")
                title = str(page.get("title") or page.get("navigation_label") or role.title())
                page_kind = str(page.get("page_kind") or "page")
                route_tree.append(route_path)
                routes.append(
                    {
                        "route_id": f"{role}_route_{index + 1}",
                        "role": role,
                        "path": route_path,
                        "screen_id": screen_id,
                        "label": str(page.get("navigation_label") or title),
                        "is_entry": bool(page.get("is_entry")),
                    }
                )
                screens[screen_id] = {
                    "screen_id": screen_id,
                    "path": route_path,
                    "title": title,
                    "subtitle": grounded_spec.product_goal[:160],
                    "kind": page_kind,
                    "page_purpose": title,
                    "handoff_paths": list(page.get("handoff_paths") or []),
                    "components": [],
                    "actions": [],
                    "sections": [],
                    "state_key": f"{role}:{screen_id}",
                }
                navigation.append({"path": route_path, "label": str(page.get("navigation_label") or title), "is_entry": bool(page.get("is_entry"))})
            roles[role] = {
                "entry_path": str(role_payload.get("entry_path") or f"/{role}"),
                "route_tree": route_tree,
                "routes": routes,
                "screens": screens,
                "action_model": [],
                "navigation": navigation,
            }
        return {
            "app": {
                "title": grounded_spec.product_goal[:80],
                "goal": grounded_spec.product_goal,
                "generation_mode": generation_mode.value,
                "ui_variant": "generated",
                "layout_variant": "stacked",
                "theme": {"accent": "#2d7ff9", "surface": "#ffffff", "card": "#f8fbff", "border": "#d8e4f7"},
                "platform": grounded_spec.target_platform,
                "route_count": sum(len(role_payload["routes"]) for role_payload in roles.values()),
                "screen_count": sum(len(role_payload["screens"]) for role_payload in roles.values()),
            },
            "roles": roles,
        }
