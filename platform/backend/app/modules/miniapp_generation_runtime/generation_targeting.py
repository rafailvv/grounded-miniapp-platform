from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.services.miniapp_generation.constants import (
    DESIGN_REFERENCE_FILES,
    LEGACY_ARCHITECTURE_MARKERS,
    CANONICAL_FILE_ROOTS,
    TEMPLATE_OWNED_SHARED_FILES,
)

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner
from app.modules.miniapp_generation_runtime.generation_paths import MiniappGenerationPaths

FORBIDDEN_ROUTE_MODULE_STEMS = {
    "__init__",
    "auth",
    "auth_telegram",
    "attachment",
    "attachments",
    "event",
    "events",
    "login",
    "me",
    "notification",
    "notifications",
    "polling",
    "push",
    "realtime",
    "session",
    "sessions",
    "sse",
    "telegram_auth",
    "upload",
    "uploads",
    "webhook",
    "webhooks",
    "websocket",
    "worklog",
}


class MiniappGenerationTargeting(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _is_canonical_role_local_static_path(path: str) -> bool:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized.startswith("miniapp/app/static/"):
            return False
        return bool(
            re.fullmatch(
                r"miniapp/app/static/(client|specialist|manager)/(index\.html|styles\.css|app\.js)",
                normalized,
            )
            or re.fullmatch(
                r"miniapp/app/static/(client|specialist|manager)/[^/]+/(index\.html|styles\.css|app\.js)",
                normalized,
            )
        )

    @classmethod
    def _has_explicit_triplet_targets(cls, path: str, target_files: list[str]) -> bool:
        normalized = str(path or "").strip().replace("\\", "/")
        if not cls._is_canonical_role_local_static_path(normalized):
            return False
        if normalized.endswith("/index.html") or re.fullmatch(r"miniapp/app/static/(client|specialist|manager)/(index\.html)", normalized):
            html_path = normalized
        elif normalized.endswith("/styles.css") or re.fullmatch(r"miniapp/app/static/(client|specialist|manager)/(styles\.css)", normalized):
            html_path = normalized[: -len("styles.css")] + "index.html"
        elif normalized.endswith("/app.js") or re.fullmatch(r"miniapp/app/static/(client|specialist|manager)/(app\.js)", normalized):
            html_path = normalized[: -len("app.js")] + "index.html"
        else:
            return False
        css_path = MiniappGenerationPaths._default_page_asset_path(html_path, asset_kind="css")
        js_path = MiniappGenerationPaths._default_page_asset_path(html_path, asset_kind="js")
        target_set = {str(item or "").strip().replace("\\", "/") for item in target_files if isinstance(item, str)}
        return {html_path, css_path, js_path}.issubset(target_set)

    def _collect_files_to_read(
        self,
        files_to_read: list[str],
        target_files: list[str],
        workspace_tree: list[dict[str, str]],
    ) -> list[str]:
        existing_files = {
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        }
        ordered = list(
            dict.fromkeys(
                [
                    *[path for path in DESIGN_REFERENCE_FILES if path in existing_files],
                    *files_to_read,
                    *[path for path in target_files if path in existing_files],
                ]
            )
        )
        return ordered

    @staticmethod
    def _is_canonical_target_path(path: str) -> bool:
        if any(path.startswith(marker) for marker in LEGACY_ARCHITECTURE_MARKERS):
            return False
        return any(path == root.rstrip("/") or path.startswith(root) for root in CANONICAL_FILE_ROOTS)

    @staticmethod
    def _is_legacy_role_entry_file(path: str) -> bool:
        return False

    def _canonicalize_target_files(self, target_files: list[str], *, scope_mode: str) -> list[str]:
        canonical: list[str] = []
        for path in target_files:
            if not isinstance(path, str):
                continue
            normalized_path = self._canonicalize_static_target_path(path)
            if not normalized_path:
                continue
            if (
                self._is_canonical_target_path(normalized_path)
                and not self._is_legacy_role_entry_file(normalized_path)
                and normalized_path not in TEMPLATE_OWNED_SHARED_FILES
            ):
                canonical.append(normalized_path)
        expanded = self._expand_page_triplet_targets(canonical)
        if scope_mode == "minimal_patch":
            return list(dict.fromkeys(expanded))
        return list(dict.fromkeys(expanded))

    @classmethod
    def _canonicalize_static_target_path(cls, path: str) -> str | None:
        normalized = MiniappGenerationPaths._normalize_generated_file_path(path)
        if not normalized:
            return None
        if normalized.startswith("miniapp/app/static/shared/"):
            allowed_shared = {
                "miniapp/app/static/shared/base.css",
                "miniapp/app/static/shared/common.js",
            }
            return normalized if normalized in allowed_shared else None
        role_root_match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)/(?P<name>index\.html|styles\.css|app\.js)",
            normalized,
        )
        if role_root_match:
            return normalized
        nested_match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)/(?P<rest>.+)",
            normalized,
        )
        if not nested_match:
            return normalized
        role = nested_match.group("role")
        rest = nested_match.group("rest")
        if re.fullmatch(r"[^/]+/(index\.html|styles\.css|app\.js)", rest):
            return normalized
        rest_path = Path(rest)
        suffix = rest_path.suffix.lower()
        if suffix not in {".html", ".css", ".js"}:
            return normalized
        parent_parts = [part for part in rest_path.parent.parts if part not in {".", ""}]
        stem = rest_path.stem.lower()
        slug_parts = [re.sub(r"[^a-z0-9]+", "_", part.lower()).strip("_") for part in parent_parts]
        if stem not in {"index", "styles", "app"}:
            slug_parts.append(re.sub(r"[^a-z0-9]+", "_", stem).strip("_"))
        slug = "_".join(part for part in slug_parts if part)
        if not slug:
            return normalized
        if suffix == ".html":
            return f"miniapp/app/static/{role}/{slug}/index.html"
        if suffix == ".css":
            return f"miniapp/app/static/{role}/{slug}/styles.css"
        return f"miniapp/app/static/{role}/{slug}/app.js"

    @staticmethod
    def _planned_page_static_targets(page_graph: dict[str, Any]) -> set[str]:
        targets: set[str] = set()
        for role_payload in (page_graph.get("roles") or {}).values():
            if not isinstance(role_payload, dict):
                continue
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                for key in ("file_path", "style_path", "script_path"):
                    path = page.get(key)
                    if isinstance(path, str) and path.startswith("miniapp/app/static/"):
                        targets.add(path)
        return targets

    @classmethod
    def _prune_non_page_static_targets(cls, target_files: list[str], *, page_graph: dict[str, Any]) -> list[str]:
        allowed_page_static = cls._planned_page_static_targets(page_graph)
        pruned: list[str] = []
        for path in target_files:
            if not isinstance(path, str):
                continue
            if path.startswith("miniapp/app/static/shared/"):
                if path in {
                    "miniapp/app/static/shared/base.css",
                    "miniapp/app/static/shared/common.js",
                }:
                    pruned.append(path)
                continue
            if re.match(r"miniapp/app/static/(client|specialist|manager)/", path):
                if path in allowed_page_static or cls._has_explicit_triplet_targets(path, target_files):
                    pruned.append(path)
                continue
            pruned.append(path)
        return list(dict.fromkeys(pruned))

    @classmethod
    def _sanitize_backend_targets(cls, backend_targets: list[str]) -> list[str]:
        sanitized: list[str] = []
        for path in backend_targets:
            if not isinstance(path, str):
                continue
            normalized = MiniappMaterializationService.normalize_runtime_python_path(
                path.strip().replace("\\", "/")
            )
            if normalized == "miniapp/app/generated/route_manifest.json":
                continue
            if normalized.startswith("miniapp/app/routes/") and normalized.endswith(".py"):
                stem = Path(normalized).stem.lower()
                if stem in FORBIDDEN_ROUTE_MODULE_STEMS:
                    continue
            sanitized.append(normalized)
        return list(dict.fromkeys(sanitized))

    @classmethod
    def _sanitize_planner_target_files(
        cls,
        *,
        target_files: list[str],
        backend_targets: list[str],
        page_graph: dict[str, Any],
    ) -> list[str]:
        sanitized_backend_targets = set(cls._sanitize_backend_targets(backend_targets))
        pruned_static = set(cls._prune_non_page_static_targets(target_files, page_graph=page_graph))
        sanitized: list[str] = []
        for path in target_files:
            if not isinstance(path, str):
                continue
            normalized = MiniappMaterializationService.normalize_runtime_python_path(
                path.strip().replace("\\", "/")
            )
            if normalized == "miniapp/app/generated/route_manifest.json":
                continue
            if normalized.startswith("miniapp/app/routes/"):
                stem = Path(normalized).stem.lower()
                if stem in FORBIDDEN_ROUTE_MODULE_STEMS:
                    continue
                if normalized.endswith(".py") and cls._is_canonical_target_path(normalized):
                    sanitized.append(normalized)
                    continue
            if normalized.startswith("miniapp/app/static/") and normalized not in pruned_static:
                continue
            sanitized.append(normalized)
        return list(dict.fromkeys(sanitized))

    @classmethod
    def _expand_page_triplet_targets(cls, target_files: list[str]) -> list[str]:
        expanded = list(target_files)
        for path in target_files:
            if not isinstance(path, str):
                continue
            if not path.startswith("miniapp/app/static/"):
                continue
            if not path.endswith(".html"):
                continue
            normalized = path.strip()
            if not normalized:
                continue
            expanded.append(MiniappGenerationPaths._default_page_asset_path(normalized, asset_kind="css"))
            expanded.append(MiniappGenerationPaths._default_page_asset_path(normalized, asset_kind="js"))
        return list(dict.fromkeys(expanded))

    @classmethod
    def _expand_cluster_targets_for_safe_companions(
        cls,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        invalid_paths: list[str],
    ) -> list[str] | None:
        allowed_prefix = None
        for role in ("client", "specialist", "manager"):
            if cluster_name.startswith(f"role_{role}_ui"):
                allowed_prefix = f"miniapp/app/static/{role}/"
                break
        if not allowed_prefix:
            return None
        additions: list[str] = []
        for path in invalid_paths:
            if not isinstance(path, str):
                return None
            if not path.startswith(allowed_prefix):
                return None
            if not cls._is_canonical_target_path(path):
                return None
            if not path.endswith((".js", ".css")):
                return None
            additions.append(path)
        if not additions:
            return None
        return list(dict.fromkeys([*cluster_targets, *additions]))

    @classmethod
    def _detect_missing_static_asset_targets(
        cls,
        *,
        generated_page_sources: dict[str, str],
        current_target_files: list[str],
        page_graph: dict[str, Any] | None = None,
    ) -> list[str]:
        existing_targets = set(current_target_files)
        allowed_page_assets = cls._planned_page_static_targets(page_graph or {}) if page_graph is not None else None
        inferred: list[str] = []
        for source_path, source in generated_page_sources.items():
            if not isinstance(source, str):
                continue
            for asset_path in cls._extract_static_asset_targets(source, source_path=source_path):
                if asset_path.startswith("miniapp/app/static/shared/"):
                    if asset_path not in existing_targets:
                        inferred.append(asset_path)
                    continue
                if asset_path not in existing_targets and (allowed_page_assets is None or asset_path in allowed_page_assets):
                    inferred.append(asset_path)
        return list(dict.fromkeys(inferred))

    @classmethod
    def _extract_static_asset_targets(cls, content: str, *, source_path: str) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        patterns = (
            r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css)(?:[?#][^"']*)?)["']""",
            r"""(?:import|from)\s*(?:\(\s*)?["']([^"']+\.(?:js|css)(?:[?#][^"']*)?)["']""",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, content, flags=re.IGNORECASE):
                resolved = cls._resolve_static_asset_target(match.group(1), source_path=source_path)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    refs.append(resolved)
        return refs

    @staticmethod
    def _resolve_static_asset_target(raw_ref: str, *, source_path: str) -> str | None:
        candidate = raw_ref.strip().split("?", 1)[0].split("#", 1)[0]
        if not candidate or candidate.startswith(("http://", "https://", "//", "data:")):
            return None
        if candidate.startswith("/static/"):
            resolved = f"miniapp/app{candidate}"
        elif candidate.startswith("static/"):
            resolved = f"miniapp/app/{candidate}"
        elif candidate.startswith("/"):
            return None
        else:
            source_parent = Path(source_path).parent.as_posix()
            resolved = posixpath.normpath(posixpath.join(source_parent, candidate))
        if not resolved.startswith("miniapp/app/static/"):
            return None
        if Path(resolved).suffix.lower() not in {".js", ".css"}:
            return None
        return resolved
