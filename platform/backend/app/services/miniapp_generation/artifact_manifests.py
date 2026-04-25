from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps


ROLE_ORDER = ("client", "specialist", "manager")


class ArtifactManifestsMixin:
    def _page_graph_page_key(self, role: str, page: dict[str, Any]) -> tuple[str, str]:
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip().replace("\\", "/")
        if file_path:
            return ("file", file_path)
        normalized_role_route = self._normalize_role_route_path(role, route_path)
        absolute_route = self._absolute_role_route_path(role, normalized_role_route)
        return ("route", absolute_route)

    def _merge_page_graph_pages(
        self,
        *,
        role: str,
        existing_pages: list[dict[str, Any]],
        supplement_pages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for page in [*existing_pages, *supplement_pages]:
            if not isinstance(page, dict):
                continue
            key = self._page_graph_page_key(role, page)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(page))
        return merged

    def _supplement_role_pages_from_draft(
        self,
        *,
        draft_source: Path,
        role: str,
        pages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        supplements: list[dict[str, Any]] = []
        role_static_dir = draft_source / f"miniapp/app/static/{role}"
        if not role_static_dir.exists():
            return pages
        for html_path in sorted(role_static_dir.glob("**/index.html")):
            rel_html = html_path.relative_to(draft_source).as_posix()
            rel_dir = html_path.parent.relative_to(role_static_dir).as_posix()
            if rel_dir == ".":
                route_path = f"/{role}"
                page_id = f"{role}_home"
                page_kind = "role_root"
                title = role.title()
                navigation_label = role.title()
                is_entry = True
            else:
                slug = rel_dir.strip("/")
                route_segments: list[str] = []
                for segment in slug.split("/"):
                    tokens = [token for token in segment.split("_") if token]
                    if not tokens:
                        continue
                    rebuilt: list[str] = []
                    index = 0
                    while index < len(tokens):
                        token = tokens[index]
                        if index + 1 < len(tokens) and tokens[index + 1] == "id":
                            rebuilt.append(f"{{{token}_id}}")
                            index += 2
                            continue
                        if token.endswith("id") and token != "id":
                            rebuilt.append(f"{{{token}}}")
                        else:
                            rebuilt.append(token)
                        index += 1
                    route_segments.extend(rebuilt)
                route_path = f"/{role}/{'/'.join(route_segments)}" if route_segments else f"/{role}"
                page_id = f"{role}_{slug.replace('/', '_')}".strip("_")
                normalized_slug = slug.lower()
                if normalized_slug == "profile":
                    page_kind = "profile"
                    navigation_label = "Profile"
                elif any(marker in normalized_slug for marker in ("detail", "{", "}", "_id")):
                    page_kind = "detail"
                    navigation_label = Path(rel_dir).name.replace("_", " ").title() or "Detail"
                else:
                    page_kind = "page"
                    navigation_label = Path(rel_dir).name.replace("_", " ").title() or "Open"
                title = Path(rel_dir).name.replace("_", " ").title() or role.title()
                is_entry = False
            supplements.append(
                {
                    "page_id": page_id,
                    "route_path": route_path,
                    "file_path": rel_html,
                    "style_path": self._default_page_asset_path(rel_html, "css"),
                    "script_path": self._default_page_asset_path(rel_html, "js"),
                    "page_kind": page_kind,
                    "title": title,
                    "navigation_label": navigation_label,
                    "is_entry": is_entry,
                }
            )
        return self._merge_page_graph_pages(role=role, existing_pages=pages, supplement_pages=supplements)

    def _supplement_page_graph_from_draft(
        self,
        *,
        page_graph: dict[str, Any],
        draft_source: Path | None,
        role_scope: list[str],
        existing_generated_graph: dict[str, Any] | None,
        preserve_existing_roles: bool,
    ) -> dict[str, Any]:
        if draft_source is None:
            return dict(page_graph)
        merged_graph = dict(page_graph)
        merged_roles: dict[str, Any] = {}
        if preserve_existing_roles and isinstance(existing_generated_graph, dict):
            merged_roles.update(
                {
                    str(role): dict(payload)
                    for role, payload in dict(existing_generated_graph.get("roles") or {}).items()
                    if isinstance(payload, dict)
                }
            )
        merged_roles.update(
            {
                str(role): dict(payload)
                for role, payload in dict(page_graph.get("roles") or {}).items()
                if isinstance(payload, dict)
            }
        )
        for role in role_scope:
            role_payload = dict(merged_roles.get(role) or {})
            role_pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
            role_payload["pages"] = self._supplement_role_pages_from_draft(
                draft_source=draft_source,
                role=role,
                pages=role_pages,
            )
            role_payload["entry_path"] = str(role_payload.get("entry_path") or f"/{role}")
            merged_roles[role] = role_payload
        ordered_roles: dict[str, Any] = {}
        for role in ROLE_ORDER:
            if role in merged_roles:
                ordered_roles[role] = merged_roles[role]
        for role, payload in merged_roles.items():
            if role not in ordered_roles:
                ordered_roles[role] = payload
        merged_graph["roles"] = ordered_roles
        return merged_graph

    @staticmethod
    def _route_manifest_page_key(page: dict[str, Any]) -> str:
        route_path = str(page.get("route_path") or "").strip()
        if route_path:
            return f"route:{route_path}"
        file_path = str(page.get("file_path") or "").strip()
        if file_path:
            return f"file:{file_path}"
        page_id = str(page.get("page_id") or "").strip()
        return f"id:{page_id}"

    @staticmethod
    def _route_manifest_page_score(page: dict[str, Any]) -> int:
        page_kind = str(page.get("page_kind") or "").strip().lower()
        route_path = str(page.get("route_path") or "").strip().lower()
        page_id = str(page.get("page_id") or "").strip().lower()
        score = 0
        if page.get("is_entry"):
            score += 100
        if page_kind == "profile" or route_path.endswith("/profile") or "profile" in page_id:
            score += 90
        elif page_kind == "detail" or "{" in route_path or ":" in route_path or "detail" in page_id:
            score += 80
        elif page_kind in {"list", "dashboard", "home", "landing"}:
            score += 70
        elif page_kind == "form":
            score += 60
        elif page_kind == "page":
            score += 10
        if page_id.endswith("_id") and page_kind != "detail":
            score -= 20
        if re.search(r"\{[^/{}]+\}", route_path):
            score += 3
        if re.search(r":[a-z][a-z0-9_]*", route_path):
            score -= 3
        return score

    @classmethod
    def _dedupe_route_manifest_pages_by_key(
        cls,
        pages: list[dict[str, Any]],
        key_name: str,
    ) -> list[dict[str, Any]]:
        best_by_key: dict[str, tuple[int, int, dict[str, Any]]] = {}
        passthrough: list[tuple[int, dict[str, Any]]] = []
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            key = str(page.get(key_name) or "").strip()
            if not key:
                passthrough.append((index, page))
                continue
            score = cls._route_manifest_page_score(page)
            previous = best_by_key.get(key)
            if previous is None or score > previous[1]:
                best_by_key[key] = (index, score, page)
        winners = [(index, page) for index, _score, page in best_by_key.values()]
        winners.extend(passthrough)
        return [page for _index, page in sorted(winners, key=lambda item: item[0])]

    @classmethod
    def _dedupe_route_manifest_pages(cls, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_pages: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            normalized = dict(page)
            file_path = str(page.get("file_path") or "").strip().replace("\\", "/")
            page_id = str(page.get("page_id") or "").strip()
            route_path = str(page.get("route_path") or "").strip().lower().rstrip("/") or "/"
            route_key = re.sub(r":[a-z][a-z0-9_]*", "{id}", route_path)
            route_key = re.sub(r"\{[^/{}]+\}", "{id}", route_key)
            normalized["_dedupe_file_path"] = file_path
            normalized["_dedupe_route_path"] = route_key
            normalized["_dedupe_page_id"] = page_id
            normalized_pages.append(normalized)
        deduped = cls._dedupe_route_manifest_pages_by_key(normalized_pages, "_dedupe_file_path")
        deduped = cls._dedupe_route_manifest_pages_by_key(deduped, "_dedupe_route_path")
        deduped = cls._dedupe_route_manifest_pages_by_key(deduped, "_dedupe_page_id")
        return [
            {key: value for key, value in page.items() if not key.startswith("_dedupe_")}
            for page in deduped
        ]

    @classmethod
    def _merge_route_manifest_pages(
        cls,
        generated_pages: list[dict[str, Any]],
        existing_pages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged_by_key: dict[str, dict[str, Any]] = {}
        for page in existing_pages:
            if isinstance(page, dict):
                merged_by_key[cls._route_manifest_page_key(page)] = page
        for page in generated_pages:
            if isinstance(page, dict):
                merged_by_key[cls._route_manifest_page_key(page)] = page
        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in [*generated_pages, *existing_pages]:
            if not isinstance(page, dict):
                continue
            key = cls._route_manifest_page_key(page)
            if key in seen:
                continue
            merged = merged_by_key.get(key)
            if not isinstance(merged, dict):
                continue
            ordered.append(merged)
            seen.add(key)
        return cls._dedupe_route_manifest_pages(ordered)

    @classmethod
    def _merged_role_mapping(
        cls,
        generated_roles: dict[str, Any],
        existing_roles: dict[str, Any],
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        ordered_roles = list(dict.fromkeys([*ROLE_ORDER, *generated_roles.keys(), *existing_roles.keys()]))
        for role in ordered_roles:
            generated_payload = dict(generated_roles.get(role) or {})
            existing_payload = dict(existing_roles.get(role) or {})
            if not generated_payload and not existing_payload:
                continue
            generated_pages = [page for page in (generated_payload.get("pages") or []) if isinstance(page, dict)]
            existing_pages = [page for page in (existing_payload.get("pages") or []) if isinstance(page, dict)]
            merged[role] = {
                "entry_path": str(generated_payload.get("entry_path") or existing_payload.get("entry_path") or f"/{role}"),
                "pages": cls._merge_route_manifest_pages(generated_pages, existing_pages),
            }
        return merged

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
        existing_route_manifest: dict[str, Any] | None = None,
        existing_runtime_manifest: dict[str, Any] | None = None,
        existing_generated_graph: dict[str, Any] | None = None,
        preserve_existing_roles: bool = False,
        draft_source: Path | None = None,
    ) -> list[DraftFileOperation]:
        page_graph = self._supplement_page_graph_from_draft(
            page_graph=page_graph,
            draft_source=draft_source,
            role_scope=role_scope,
            existing_generated_graph=existing_generated_graph,
            preserve_existing_roles=preserve_existing_roles,
        )
        route_manifest = self.route_manifest_from_page_graph(page_graph, role_scope)
        runtime_manifest = self.runtime_manifest_from_page_graph(route_manifest, grounded_spec, generation_mode)
        if preserve_existing_roles:
            existing_route_roles = dict((existing_route_manifest or {}).get("roles") or {})
            route_manifest["roles"] = self._merged_role_mapping(
                dict(route_manifest.get("roles") or {}),
                existing_route_roles,
            )
            runtime_manifest = self.runtime_manifest_from_page_graph(route_manifest, grounded_spec, generation_mode)
        required_artifacts = {
            "artifacts/generated_app_graph.json": (page_graph, "Persist the effective page graph after draft-aware runtime artifact normalization."),
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
            roles[role] = {"entry_path": str(payload.get("entry_path") or f"/{role}"), "pages": self._dedupe_route_manifest_pages(pages)}
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
