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
        del require_business_pages, is_business_page, is_role_root_page, role_scope, scope_mode
        operation_paths = {operation.file_path for operation in operations}
        operation_map = {operation.file_path: operation for operation in operations}
        generated_manifest_paths = {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/app/generated/app_ir.json",
            "miniapp/app/generated/static_runtime_manifest.json",
            "miniapp/app/generated/role_seed.json",
            "miniapp/app/generated/role_experience.json",
            "miniapp/app/generated/runtime_state.json",
        }
        generated_test_paths = {
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
        }
        bootstrap_paths = {
            "miniapp/app/db.py",
            "miniapp/app/main.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/health.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/routes/runtime.py",
            "miniapp/app/routes/client.py",
            "miniapp/app/routes/specialist.py",
            "miniapp/app/routes/manager.py",
        }
        allowed_target_paths = set(target_files) | generated_manifest_paths | generated_test_paths | {
            "artifacts/generated_app_graph.json",
            "artifacts/page_graph_verification.json",
            "artifacts/grounded_spec.json",
        } | bootstrap_paths
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

        known_handoff_paths = {"/"}
        for role, role_payload in (page_graph.get("roles") or {}).items():
            role_payload = page_graph.get("roles", {}).get(role) or {}
            for page in role_payload.get("pages") or []:
                route_path = str(page.get("route_path") or "")
                if route_path:
                    known_handoff_paths.add(route_path)
        for role, role_payload in (page_graph.get("roles") or {}).items():
            role_pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
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
                if is_profile_page:
                    for handoff_path in page.get("handoff_paths") or []:
                        if isinstance(handoff_path, str) and handoff_path.strip() and handoff_path != route_path and handoff_path not in known_handoff_paths:
                            issues.append(f"{file_path} references a non-canonical handoff path: {handoff_path}.")
                    continue
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
