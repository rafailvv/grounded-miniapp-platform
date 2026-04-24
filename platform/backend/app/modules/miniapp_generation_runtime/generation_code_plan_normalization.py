from __future__ import annotations

import re
from typing import Any

from app.services.miniapp_generation.constants import SHARED_GENERATED_FILES, TEMPLATE_OWNED_SHARED_FILES

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodePlanNormalization(MiniappGenerationRuntimeOwner):

    def _normalize_page_plan(
        self,
        payload: dict[str, Any],
        *,
        role_scope: list[str],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
    ) -> dict[str, Any]:
        raw_graph = payload.get("page_graph")
        if not isinstance(raw_graph, dict):
            raise ValueError("Page graph payload is missing.")

        raw_roles = raw_graph.get("roles")
        roles_source: dict[str, dict[str, Any]] = {}
        if isinstance(raw_roles, list):
            for item in raw_roles:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().lower()
                if role in role_scope:
                    roles_source[role] = item
        elif isinstance(raw_roles, dict):
            for role, item in raw_roles.items():
                if role in role_scope and isinstance(item, dict):
                    roles_source[role] = item

        shared_files = self._normalize_path_list(raw_graph.get("shared_files") or payload.get("shared_files"), list(SHARED_GENERATED_FILES))
        shared_files = [path for path in shared_files if path not in TEMPLATE_OWNED_SHARED_FILES]
        backend_targets = self._normalize_path_list(raw_graph.get("backend_targets") or payload.get("backend_targets"), [])
        roles: dict[str, dict[str, Any]] = {}
        graph_page_targets: list[str] = []

        for role in role_scope:
            role_payload = roles_source.get(role)
            if not role_payload:
                raise ValueError(f"Page graph is missing the {role} role.")
            pages_raw = role_payload.get("pages")
            if not isinstance(pages_raw, list) or not pages_raw:
                raise ValueError(f"Page graph is missing page definitions for {role}.")
            pages = [self._normalize_page_definition(role, page, index) for index, page in enumerate(pages_raw)]
            pages = self._finalize_role_pages(role, pages, require_multi_page=require_multi_page)
            route_candidates = self._normalize_path_list([role_payload.get("routes_file")], [])
            routes_file = route_candidates[0] if route_candidates else self._default_routes_file(role)
            roles[role] = {
                "entry_path": str(role_payload.get("entry_path") or "/").strip() or "/",
                "landing_page_id": str(role_payload.get("landing_page_id") or pages[0]["page_id"]).strip() or pages[0]["page_id"],
                "routes_file": routes_file,
                "pages": pages,
            }
            graph_page_targets.extend(page["file_path"] for page in pages)

        proactive_backend_targets: list[str] = []

        computed_targets = list(
            dict.fromkeys(
                [
                    *shared_files,
                    *backend_targets,
                    *(role_payload["routes_file"] for role_payload in roles.values()),
                    *graph_page_targets,
                ]
            )
        )
        raw_target_files = self._normalize_path_list(payload.get("target_files"), [])
        if scope_mode in {"minimal_patch", "workflow_partial_build"} and raw_target_files:
            computed_target_set = set(computed_targets)
            intersection = [path for path in raw_target_files if path in computed_target_set]
            if computed_targets and not intersection:
                target_files = list(dict.fromkeys(computed_targets))
            else:
                target_files = list(dict.fromkeys([*raw_target_files, *computed_targets]))
        else:
            target_files = list(dict.fromkeys([*raw_target_files, *computed_targets]))

        flow_mode = str(raw_graph.get("flow_mode") or payload.get("flow_mode") or ("multi_page" if require_multi_page else "single_page"))
        target_files = self._canonicalize_target_files(target_files, scope_mode=scope_mode)
        raw_files_to_read = self._normalize_path_list(payload.get("files_to_read"), [])
        files_to_read = self._collect_files_to_read(raw_files_to_read, target_files, workspace_tree)
        shared_files = [path for path in shared_files if path in set(target_files)]
        backend_targets = self._sanitize_backend_targets([path for path in backend_targets if path in set(target_files)])
        target_files = self._sanitize_planner_target_files(
            target_files=target_files,
            backend_targets=backend_targets,
            page_graph={"roles": roles},
        )
        target_set = set(target_files)
        shared_files = [path for path in shared_files if path in target_set]
        backend_targets = [path for path in backend_targets if path in target_set]
        generation_clusters = self._build_generation_clusters(target_files)
        execution_plan = self._build_execution_plan(
            role_scope=role_scope,
            roles=roles,
            shared_files=shared_files,
            backend_targets=backend_targets,
            target_files=target_files,
            generation_clusters=generation_clusters,
        )

        return {
            "summary": str(payload.get("summary") or raw_graph.get("summary") or "").strip(),
            "flow_mode": flow_mode,
            "files_to_read": files_to_read,
            "target_files": target_files,
            "shared_files": shared_files,
            "backend_targets": backend_targets,
            "generation_clusters": generation_clusters,
            "active_role_scope": execution_plan["active_role_scope"],
            "execution_plan": execution_plan,
            "planner_contract_enrichment": {
                "proactive_backend_targets": proactive_backend_targets,
            },
            "page_graph": {
                "app_title": str(raw_graph.get("app_title") or "").strip(),
                "summary": str(raw_graph.get("summary") or payload.get("summary") or "").strip(),
                "flow_mode": flow_mode,
                "scope_mode": scope_mode,
                "write_strategy": scope_mode,
                "role_scope": role_scope,
                "shared_files": shared_files,
                "backend_targets": backend_targets,
                "roles": roles,
            },
            "scope_mode": scope_mode,
            "require_multi_page": require_multi_page,
        }

    def _finalize_role_pages(self, role: str, pages: list[dict[str, Any]], *, require_multi_page: bool) -> list[dict[str, Any]]:
        if not pages:
            raise ValueError(f"{role} page graph must declare at least one page.")
        deduped: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for index, page in enumerate(pages):
            normalized = self._normalize_page_definition(role, page, index)
            dedupe_key = (
                str(normalized.get("page_id") or ""),
                str(normalized.get("route_path") or ""),
                str(normalized.get("file_path") or ""),
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            if not normalized["purpose"]:
                normalized["purpose"] = self._default_page_purpose(role, normalized["page_kind"], route_path=normalized["route_path"])
            if not normalized["description"]:
                normalized["description"] = normalized["purpose"]
            if not normalized["primary_actions"]:
                normalized["primary_actions"] = self._default_primary_actions(role, normalized["page_kind"], route_path=normalized["route_path"])
            if not normalized["handoff_paths"]:
                normalized["handoff_paths"] = self._default_handoff_paths_for_page_kind(normalized["page_kind"], route_path=normalized["route_path"])
            deduped.append(normalized)
        available_routes = {str(page.get("route_path") or "") for page in deduped}
        page_id_to_route = {
            str(page.get("page_id") or "").strip().lower(): str(page.get("route_path") or "")
            for page in deduped
            if isinstance(page, dict)
        }
        for page in deduped:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("page_id") or "").strip().lower()
            route_path = str(page.get("route_path") or "")
            if not page_id or not route_path:
                continue
            stripped_page_id = re.sub(rf"^{role}[_-]", "", page_id)
            page_id_to_route.setdefault(stripped_page_id, route_path)
        for page in deduped:
            normalized_handoffs: list[str] = []
            for path in page["handoff_paths"]:
                normalized_path = self._normalize_role_route_path(role, path, index=0)
                if normalized_path not in available_routes:
                    handoff_key = normalized_path.strip("/").lower()
                    handoff_key = re.sub(rf"^{role}[_-]", "", handoff_key)
                    normalized_path = page_id_to_route.get(handoff_key, normalized_path)
                normalized_handoffs.append(normalized_path)
            filtered_handoffs = [path for path in normalized_handoffs if path in available_routes and path != page["route_path"]]
            if filtered_handoffs:
                page["handoff_paths"] = filtered_handoffs
            elif page["route_path"] != "/" and "/" in available_routes:
                page["handoff_paths"] = ["/"]
            elif page["route_path"] == "/" and "/profile" in available_routes:
                page["handoff_paths"] = ["/profile"]
        return deduped

    def _normalize_page_definition(self, role: str, payload: Any, index: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f"Page definition #{index + 1} for {role} is invalid.")
        component_name = self._component_name(role, payload, index)
        raw_route_path = str(payload.get("route_path") or "").strip() or ("/" if index == 0 else f"/page-{index + 1}")
        route_path = self._normalize_role_route_path(role, raw_route_path, index=index)
        file_path_candidates = self._normalize_path_list([payload.get("file_path")], [])
        data_dependencies = self._normalize_string_list(payload.get("data_dependencies"))
        default_file_path = self._default_page_file(
            role,
            component_name,
            route_path=self._page_file_route_path(role=role, raw_route_path=raw_route_path, normalized_route_path=route_path),
        )
        file_path = file_path_candidates[0] if file_path_candidates else default_file_path
        if not self._is_role_local_page_file(role=role, file_path=file_path):
            file_path = default_file_path
        elif self._should_rewrite_page_file_for_route(
            role=role,
            route_path=route_path,
            file_path=file_path,
            page_id=str(payload.get("page_id") or ""),
        ):
            file_path = default_file_path
        elif self._should_canonicalize_page_file_alias(role=role, route_path=route_path, file_path=file_path, default_file_path=default_file_path):
            file_path = default_file_path
        style_path = self._default_page_asset_path(file_path, asset_kind="css")
        script_path = self._default_page_asset_path(file_path, asset_kind="js")
        state_marker_base = self._state_marker_base(str(payload.get("page_id") or f"{role}_{index + 1}"), file_path, component_name)
        loading_state = str(payload.get("loading_state") or "").strip()
        empty_state = str(payload.get("empty_state") or "").strip()
        error_state = str(payload.get("error_state") or "").strip()
        if not data_dependencies:
            loading_state = ""
            empty_state = ""
            error_state = ""
        else:
            page_label = str(payload.get("title") or component_name).strip() or component_name
            if not loading_state:
                loading_state = self._default_state_contract(state_kind="loading", page_label=page_label, marker_base=state_marker_base)
            if not error_state:
                error_state = self._default_state_contract(state_kind="error", page_label=page_label, marker_base=state_marker_base)
            if not empty_state:
                empty_state = f"Show an empty-state container after {page_label} data loads but returns no records."
        return {
            "page_id": str(payload.get("page_id") or f"{role}_{index + 1}").strip() or f"{role}_{index + 1}",
            "route_path": route_path,
            "navigation_label": str(payload.get("navigation_label") or payload.get("title") or component_name).strip(),
            "component_name": component_name,
            "file_path": file_path,
            "style_path": style_path,
            "script_path": script_path,
            "title": str(payload.get("title") or component_name).strip(),
            "description": str(payload.get("description") or payload.get("purpose") or "").strip(),
            "purpose": str(payload.get("purpose") or payload.get("description") or "").strip(),
            "page_kind": self._page_kind(payload.get("page_kind"), route_path=route_path, file_path=file_path, page_id=str(payload.get("page_id") or "")),
            "primary_actions": self._normalize_string_list(payload.get("primary_actions")),
            "handoff_paths": self._normalize_handoff_paths(payload.get("handoff_paths")),
            "data_dependencies": data_dependencies,
            "loading_state": loading_state,
            "empty_state": empty_state,
            "error_state": error_state,
        }
