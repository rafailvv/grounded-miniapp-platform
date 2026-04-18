from __future__ import annotations

import re
from typing import Any, Callable

from app.models.domain import DraftFileOperation
from app.services.miniapp_generation.constants import LEGACY_ARCHITECTURE_MARKERS


class GenerationEditGate:
    @classmethod
    def edit_gate_issues(
        cls,
        page_graph: dict[str, Any],
        operations: list[DraftFileOperation],
        role_scope: list[str],
        *,
        scope_mode: str,
        target_files: list[str],
        require_business_pages: bool,
        is_canonical_target_path: Callable[[str], bool],
        is_business_page: Callable[[str, dict[str, Any]], bool],
        is_role_root_page: Callable[[str, dict[str, Any]], bool],
    ) -> list[str]:
        issues: list[str] = []
        operation_paths = {operation.file_path for operation in operations}
        operation_map = {operation.file_path: operation for operation in operations}
        generated_manifest_paths = {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/app/generated/app_ir.json",
        }
        generated_test_paths = {
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
        }
        allowed_target_paths = set(target_files) | generated_manifest_paths | generated_test_paths | {
            "artifacts/generated_app_graph.json",
            "artifacts/page_graph_verification.json",
            "artifacts/grounded_spec.json",
        }
        unexpected_paths = [path for path in operation_paths if path not in allowed_target_paths]
        if unexpected_paths:
            issues.append(f"Generated draft touched files outside the planned target scope: {', '.join(unexpected_paths[:5])}")
        legacy_paths = [path for path in operation_paths if any(path.startswith(marker) for marker in LEGACY_ARCHITECTURE_MARKERS)]
        if legacy_paths:
            issues.append(f"Generated draft reintroduced legacy architecture paths: {', '.join(sorted(legacy_paths)[:5])}")
        non_canonical_paths = [
            path
            for path in operation_paths
            if path not in {"artifacts/generated_app_graph.json", "artifacts/page_graph_verification.json", *generated_manifest_paths}
            and not is_canonical_target_path(path)
        ]
        if non_canonical_paths:
            issues.append(f"Generated draft left the canonical architecture roots: {', '.join(sorted(non_canonical_paths)[:5])}")
        if scope_mode == "minimal_patch":
            meaningful_hits = [path for path in operation_paths if path in set(target_files)]
            support_hits = [
                path
                for path in operation_paths
                if path in generated_manifest_paths
                or path == "miniapp/app/static/shared/common.js"
                or path.endswith("/app.js")
                or path.endswith("/styles.css")
            ]
            if target_files and not meaningful_hits:
                issues.append("Minimal patch draft returned only artifact-level changes and did not touch any planned source targets.")
            if len(operation_paths) > max(1, len(target_files) + 1 + len(support_hits)):
                issues.append("Minimal patch mode touched too many files.")
            return issues

        route_hits = 0
        page_hits = 0
        route_manifest_touched = "miniapp/app/generated/route_manifest.json" in operation_paths
        known_handoff_paths = {"/"}
        for role in role_scope:
            role_payload = page_graph.get("roles", {}).get(role) or {}
            routes_file = role_payload.get("routes_file")
            if isinstance(routes_file, str) and routes_file in operation_paths:
                route_hits += 1
            for page in role_payload.get("pages") or []:
                file_path = page.get("file_path")
                route_path = str(page.get("route_path") or "")
                if route_path:
                    known_handoff_paths.add(route_path)
                if isinstance(file_path, str) and file_path in operation_paths:
                    page_hits += 1
        if route_hits < len(role_scope) and not route_manifest_touched:
            issues.append("Generated draft does not update the role route files for every selected role.")
        if page_hits < len(role_scope):
            issues.append("Generated draft still collapses the app into too few real page files.")
        if require_business_pages:
            for role in role_scope:
                role_payload = page_graph.get("roles", {}).get(role) or {}
                role_pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
                business_pages = [page for page in role_pages if is_business_page(role, page)]
                if business_pages and not any(str(page.get("file_path")) in operation_paths for page in business_pages):
                    issues.append(f"Generated draft skipped separate business page files for {role}.")
                index_path = f"miniapp/app/static/{role}/index.html"
                index_operation = operation_map.get(index_path)
                index_source = str(index_operation.content or "") if index_operation is not None else ""
                lowered_index = index_source.lower()
                if index_source:
                    keyword_hits = sum(lowered_index.count(token) for token in ("catalog", "product", "products", "cart", "checkout", "orders", "queue", "management"))
                    if keyword_hits >= 3 and not business_pages:
                        issues.append(f"{role} index.html still absorbs business content instead of splitting it into separate pages.")
                for page in role_pages:
                    file_path = str(page.get("file_path") or "")
                    if not file_path.endswith(".html"):
                        continue
                    page_operation = operation_map.get(file_path)
                    content = str(page_operation.content or "").lower() if page_operation is not None else ""
                    if not content:
                        continue
                    route_path = str(page.get("route_path") or "")
                    page_kind = str(page.get("page_kind") or "").strip().lower()
                    is_profile_page = page_kind == "profile" or route_path.rstrip("/") == "/profile" or file_path.endswith("/profile.html")
                    loading_hits = content.count("loading")
                    dependency_count = len(page.get("data_dependencies") or [])
                    has_real_surface = cls.has_real_interactive_surface(content)
                    has_business_surface = cls.has_business_surface(content)
                    role_root = is_role_root_page(role, page)
                    visible_loading_surface = cls.has_visible_loading_surface(content)
                    empty_business_containers = cls.empty_business_container_count(content)
                    if is_profile_page:
                        for handoff_path in page.get("handoff_paths") or []:
                            if isinstance(handoff_path, str) and handoff_path.strip() and handoff_path != route_path and handoff_path not in known_handoff_paths:
                                issues.append(f"{file_path} references a non-canonical handoff path: {handoff_path}.")
                        continue
                    if role_root and dependency_count > 0:
                        if visible_loading_surface:
                            issues.append(f"{file_path} still renders loading-first copy as the primary role root surface.")
                        if not has_business_surface and empty_business_containers > 0:
                            issues.append(f"{file_path} does not render an honest business surface for first paint on a role root page.")
                    if dependency_count == 0 and loading_hits > 0 and not has_real_surface:
                        issues.append(f"{file_path} still uses loading-first copy even though the page has no declared data dependencies.")
                    if loading_hits >= 3 and len(content) < 2500 and not has_real_surface:
                        issues.append(f"{file_path} is dominated by loading copy instead of real page content.")
                    if cls.contains_placeholder_surface(content):
                        issues.append(f"{file_path} still contains placeholder copy.")
                    for handoff_path in page.get("handoff_paths") or []:
                        if isinstance(handoff_path, str) and handoff_path.strip() and handoff_path != route_path and handoff_path not in known_handoff_paths:
                            issues.append(f"{file_path} references a non-canonical handoff path: {handoff_path}.")
        return issues

    @classmethod
    def _edit_gate_issues(cls, *args: Any, **kwargs: Any) -> list[str]:
        return cls.edit_gate_issues(*args, **kwargs)

    @staticmethod
    def contains_placeholder_surface(content: str) -> bool:
        lowered = content.lower()
        normalized = re.sub(r'placeholder\s*=\s*["\'][^"\']*["\']', "", lowered)
        markers = ("coming soon", "todo", "tbd", ">placeholder<", " placeholder text", " replace this ", " replace with ", " generic content", " sample placeholder")
        return any(marker in normalized for marker in markers)

    _contains_placeholder_surface = contains_placeholder_surface

    @staticmethod
    def has_real_interactive_surface(content: str) -> bool:
        markers = ("<main", "<section", "<article", "<button", "href=", "card-link", "feature-block", "filters", "chip", "task-list", "request-list", "empty", "error", "retry-button", "form-card")
        return sum(1 for marker in markers if marker in content) >= 4

    _has_real_interactive_surface = has_real_interactive_surface

    @staticmethod
    def has_visible_loading_surface(content: str) -> bool:
        lowered = str(content or "").lower()
        scrubbed = re.sub(
            r"<(?P<tag>div|section|article|aside|p|span)[^>]*(?:hidden|aria-hidden=['\"]true['\"])[^>]*>.*?</(?P=tag)>",
            "",
            lowered,
            flags=re.IGNORECASE | re.DOTALL,
        )
        markers = ("loading your workspace", "loading queue", "loading dashboard", "syncing", "pulling", "please wait", "loading...")
        return any(marker in scrubbed for marker in markers)

    _has_visible_loading_surface = has_visible_loading_surface

    @staticmethod
    def has_business_surface(content: str) -> bool:
        lowered = str(content or "").lower()
        markers = ("metric-card", "summary-card", "stat-card", "request-list", "queue-list", "workload-list", "approval-list", "availability-list", "conflict-list", "empty-state", "empty-panel", "empty-card", "primary-actions", "quick request", "recent requests", "next requests", "live overview")
        structural_hits = sum(1 for marker in markers if marker in lowered)
        semantic_hits = sum(lowered.count(tag) for tag in ("<section", "<article", "<button", "href="))
        return structural_hits >= 2 or (structural_hits >= 1 and semantic_hits >= 4)

    _has_business_surface = has_business_surface

    @staticmethod
    def empty_business_container_count(content: str) -> int:
        pattern = re.compile(
            r"<(?:div|section|article)[^>]+(?:id|class)=['\"][^'\"]*(?:metrics|summary|requests|queue|availability|conflict|approval|workload)[^'\"]*['\"][^>]*>\s*</(?:div|section|article)>",
            flags=re.IGNORECASE,
        )
        return len(pattern.findall(str(content or "")))

    _empty_business_container_count = empty_business_container_count
