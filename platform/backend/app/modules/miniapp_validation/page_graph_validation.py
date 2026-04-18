from __future__ import annotations

from collections import Counter
import re
from typing import Any, Callable


class PageGraphValidation:
    @staticmethod
    def page_graph_gate_issues(
        page_graph: dict[str, Any],
        role_scope: list[str],
        *,
        scope_mode: str,
        require_multi_page: bool,
        require_business_pages: bool,
        normalize_role_route_path: Callable[[str, str, int], str],
        is_business_page: Callable[[str, dict[str, Any]], bool],
        is_canonical_target_path: Callable[[str], bool],
    ) -> list[str]:
        issues: list[str] = []
        enforce_expanded_structure = scope_mode != "minimal_patch" or len(role_scope) > 1
        roles = page_graph.get("roles") or {}
        planned_paths = set(page_graph.get("shared_files") or []) | set(page_graph.get("backend_targets") or [])
        total_pages = 0
        normalized_role_routes: list[tuple[str, tuple[str, ...]]] = []
        normalized_role_purposes: list[tuple[str, tuple[str, ...]]] = []
        for role in role_scope:
            role_payload = roles.get(role) or {}
            pages = role_payload.get("pages") or []
            routes_file = role_payload.get("routes_file")
            if not isinstance(routes_file, str) or not routes_file:
                issues.append(f"{role} is missing a routes file.")
            else:
                planned_paths.add(routes_file)
            if not isinstance(pages, list) or not pages:
                issues.append(f"{role} is missing page definitions.")
                continue
            total_pages += len(pages)
            if enforce_expanded_structure and require_multi_page and len(pages) < 2:
                issues.append(f"{role} did not receive enough distinct pages for a multi-page app.")
            actual_routes = {
                normalize_role_route_path(role, str(page.get("route_path") or ""), index)
                for index, page in enumerate(pages)
                if isinstance(page, dict)
            }
            if not actual_routes:
                issues.append(f"{role} is missing route paths.")
            if "/" not in actual_routes:
                issues.append(f"{role} is missing an entry route at /.")
            file_paths = [
                str(page.get("file_path") or "")
                for page in pages
                if isinstance(page, dict) and str(page.get("file_path") or "").strip()
            ]
            duplicate_paths = sorted(path for path, count in Counter(file_paths).items() if count > 1)
            if duplicate_paths:
                issues.append(f"{role} reuses the same file for multiple routes: {', '.join(duplicate_paths[:3])}.")
            if enforce_expanded_structure and require_business_pages:
                business_pages = [page for page in pages if isinstance(page, dict) and is_business_page(role, page)]
                if not business_pages:
                    issues.append(f"{role} is missing separate business pages beyond index.html and profile.html.")
            focused_pages = [
                page
                for page in pages
                if isinstance(page, dict) and str(page.get("page_kind") or "").strip().lower() in {"workspace", "details", "detail"}
            ]
            for focused_page in focused_pages:
                purpose = str(focused_page.get("purpose") or "").strip().lower()
                if not purpose:
                    issues.append(f"{role} focused detail page is missing a concrete purpose.")
                    continue
                if len(purpose) < 24 and not any(
                    token in purpose
                    for token in ("focus", "module", "detail", "inspect", "review", "context", "task", "record", "item", "update", "status", "assign", "progress", "comment", "create", "request", "workload", "manage", "history")
                ):
                    issues.append(f"{role} focused detail page is missing a concrete purpose.")
            for page in pages:
                if not isinstance(page, dict):
                    continue
                handoff_paths = {str(item).strip() for item in (page.get("handoff_paths") or []) if isinstance(item, str)}
                if not handoff_paths:
                    issues.append(f"{role} page {page.get('page_id') or page.get('file_path') or 'unknown'} is missing handoff_paths.")
                    continue
                unknown_handoffs = sorted(path for path in handoff_paths if path not in actual_routes)
                if unknown_handoffs:
                    issues.append(
                        f"{role} page {page.get('page_id') or page.get('file_path') or 'unknown'} points to unknown handoff paths: {', '.join(unknown_handoffs[:3])}."
                    )
            normalized_role_routes.append((role, tuple(sorted(str(page.get("route_path") or "") for page in pages))))
            normalized_role_purposes.append(
                (
                    role,
                    tuple(
                        sorted(
                            re.sub(r"\s+", " ", str(page.get("purpose") or "").strip().lower())
                            for page in pages
                            if isinstance(page, dict)
                        )
                    ),
                )
            )
            planned_paths.update(
                str(page.get("file_path"))
                for page in pages
                if isinstance(page, dict) and isinstance(page.get("file_path"), str)
            )
        if enforce_expanded_structure and require_multi_page and page_graph.get("flow_mode") != "multi_page":
            issues.append("The generated plan did not stay in multi-page mode.")
        if enforce_expanded_structure and require_multi_page and total_pages <= len(role_scope):
            issues.append("The generated plan still collapses the app into one screen per selected role.")
        if enforce_expanded_structure and require_business_pages:
            default_only = []
            for role in role_scope:
                role_payload = roles.get(role) or {}
                pages = role_payload.get("pages") or []
                route_paths = {str(page.get("route_path") or "") for page in pages if isinstance(page, dict)}
                if route_paths.issubset({f"/{role}", f"/{role}/profile"}):
                    default_only.append(role)
            if default_only:
                issues.append(f"Workflow-heavy planning stayed on root/profile routes only for: {', '.join(default_only)}.")
        if enforce_expanded_structure and len(normalized_role_routes) > 1:
            route_sets = [routes for _, routes in normalized_role_routes]
            purpose_sets = [purposes for _, purposes in normalized_role_purposes]
            if len(set(route_sets)) == 1 and len(set(purpose_sets)) == 1:
                issues.append("Selected roles still share the same route tree.")
        if enforce_expanded_structure and len(normalized_role_purposes) > 1:
            purpose_sets = [purposes for _, purposes in normalized_role_purposes]
            if len(set(purpose_sets)) == 1:
                issues.append("Selected roles still share the same page purposes and are not meaningfully differentiated.")
        invalid_paths = [path for path in planned_paths if isinstance(path, str) and not is_canonical_target_path(path)]
        if invalid_paths:
            issues.append(f"Planned targets left the canonical architecture: {', '.join(sorted(invalid_paths)[:5])}")
        return issues

    _page_graph_gate_issues = page_graph_gate_issues

    @classmethod
    def build_page_graph_verification_report(
        cls,
        page_graph: dict[str, Any],
        role_scope: list[str],
        *,
        normalize_role_route_path: Callable[[str, str, int], str],
        is_business_page: Callable[[str, dict[str, Any]], bool],
        is_canonical_target_path: Callable[[str], bool],
    ) -> dict[str, Any]:
        roles = page_graph.get("roles") or {}
        issues = cls.page_graph_gate_issues(
            page_graph,
            role_scope,
            scope_mode=str(page_graph.get("scope_mode") or "whole_file_build"),
            require_multi_page=str(page_graph.get("flow_mode") or "single_page") == "multi_page",
            require_business_pages=any(
                len((roles.get(role) or {}).get("pages") or []) > 2
                for role in role_scope
            ),
            normalize_role_route_path=normalize_role_route_path,
            is_business_page=is_business_page,
            is_canonical_target_path=is_canonical_target_path,
        )
        role_summaries: dict[str, Any] = {}
        for role in role_scope:
            role_payload = roles.get(role) or {}
            pages = role_payload.get("pages") or []
            role_summaries[role] = {
                "route_tree": [str(page.get("route_path") or "") for page in pages if isinstance(page, dict)],
                "page_ids": [str(page.get("page_id") or "") for page in pages if isinstance(page, dict)],
                "page_purposes": [str(page.get("purpose") or "") for page in pages if isinstance(page, dict)],
                "handoff_paths": {
                    str(page.get("page_id") or ""): list(page.get("handoff_paths") or [])
                    for page in pages
                    if isinstance(page, dict)
                },
            }
        return {
            "status": "passed" if not issues else "failed",
            "role_scope": role_scope,
            "summary": "Route and page-graph planning verification.",
            "issues": issues,
            "roles": role_summaries,
        }

    _build_page_graph_verification_report = build_page_graph_verification_report
