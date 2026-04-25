from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_generation_runtime import MiniappGenerationPageGraphRuntime, MiniappGenerationPaths, MiniappGenerationTargeting
from app.services.miniapp_generation.constants import ROLE_COMPONENT_PREFIX


class ServicePageDefaultsMixins:
    @staticmethod
    def _default_page_purpose(role: str, page_kind: str, *, route_path: str | None = None) -> str:
        role_label = role.title()
        normalized_kind = page_kind.strip().lower()
        normalized_route = (route_path or "").strip().lower()
        if normalized_kind in {"dashboard", "landing"}:
            return f"{role_label} dashboard with overview metrics, prepared blocks, and route-based next actions."
        if normalized_kind in {"list", "queue"}:
            return f"{role_label} list surface for queue, record, or list-based coordination."
        if normalized_kind in {"workspace", "details", "detail", "feature"}:
            return f"{role_label} detail surface for focused work, module actions, and decision handoffs."
        if normalized_kind == "form":
            return f"{role_label} form page for collecting, updating, or validating workflow data."
        if normalized_kind in {"profile", "info"} or normalized_route.endswith("/profile"):
            return f"{role_label} profile page for editable identity and preferences."
        return f"{role_label} profile page for editable identity and preferences."

    @staticmethod
    def _default_primary_actions(role: str, page_kind: str, *, route_path: str | None = None) -> list[str]:
        normalized_kind = page_kind.strip().lower()
        normalized_route = (route_path or "").strip().lower()
        if normalized_kind in {"dashboard", "landing"}:
            return [f"{role}_open_list", f"{role}_open_detail", f"{role}_open_profile"]
        if normalized_kind in {"list", "queue"}:
            return [f"{role}_open_detail_from_list", f"{role}_open_profile"]
        if normalized_kind in {"workspace", "details", "detail", "feature"}:
            return [f"{role}_back_to_list", f"{role}_open_profile"]
        if normalized_kind == "form":
            return [f"{role}_submit_form"]
        if normalized_kind in {"profile", "info"} or normalized_route.endswith("/profile"):
            return [f"{role}_profile_save"]
        return [f"{role}_profile_save"]

    @staticmethod
    def _page_file_route_path(*, role: str, raw_route_path: str, normalized_route_path: str) -> str:
        raw = str(raw_route_path or "").strip() or normalized_route_path
        normalized = str(normalized_route_path or "").strip() or "/"
        role_prefix = f"/{role}"
        if raw.startswith((role_prefix, f"{role_prefix}/", f"/{role}-", f"/{role}_")):
            return normalized
        if raw in {"/dashboard", "/home"}:
            return normalized
        if normalized in {"/", "/profile"}:
            return normalized
        if raw and raw != normalized:
            return raw
        return normalized

    @staticmethod
    def _should_canonicalize_page_file_alias(*, role: str, route_path: str, file_path: str, default_file_path: str) -> bool:
        if file_path == default_file_path:
            return False
        if not file_path.startswith(f"miniapp/app/static/{role}/"):
            return False
        if not file_path.endswith(".html"):
            return False
        if route_path.rstrip("/") == "/profile":
            return True
        if route_path in {"", "/"}:
            return True
        return file_path.endswith("/index.html")

    @staticmethod
    def _normalize_role_route_path(role: str, route_path: str, *, index: int) -> str:
        return MiniappGenerationPaths._normalize_role_route_path(role, route_path, index=index)

    @staticmethod
    def _absolute_role_route_path(role: str, route_path: str) -> str:
        return MiniappGenerationPaths._absolute_role_route_path(role, route_path)

    @staticmethod
    def _is_role_local_page_file(*, role: str, file_path: str) -> bool:
        normalized = file_path.strip().replace("\\", "/").lower()
        return normalized.startswith(f"miniapp/app/static/{role}/") and normalized.endswith(".html")

    @classmethod
    def _should_rewrite_page_file_for_route(cls, *, role: str, route_path: str, file_path: str, page_id: str) -> bool:
        normalized_path = file_path.strip()
        normalized_suffix = normalized_path.replace("\\", "/").lower()
        page_id_lower = page_id.lower()
        if not cls._is_role_local_page_file(role=role, file_path=file_path):
            return True
        if not normalized_suffix.endswith(".html"):
            return True
        if route_path == "/":
            return re.fullmatch(rf"miniapp/app/static/{role}/index\.html", normalized_suffix) is None
        if route_path.rstrip("/") == "/profile" or "profile" in page_id_lower:
            return normalized_suffix != f"miniapp/app/static/{role}/profile/index.html"
        return (
            not normalized_suffix.endswith("/index.html")
            or re.fullmatch(rf"miniapp/app/static/{role}/index\.html", normalized_suffix) is not None
            or normalized_suffix.endswith(f"/{role}/profile/index.html")
        )

    @staticmethod
    def _default_handoff_paths_for_page_kind(page_kind: str, *, route_path: str | None = None) -> list[str]:
        normalized = page_kind.strip().lower()
        normalized_route = (route_path or "").strip().lower()
        if normalized in {"dashboard", "landing"} or normalized_route == "/":
            return []
        if normalized in {"list", "queue"}:
            return ["/"]
        if normalized in {"workspace", "details", "detail", "feature"}:
            return ["/"]
        return ["/"]

    @staticmethod
    def _page_kind(value: Any, *, route_path: str, file_path: str, page_id: str) -> str:
        raw = str(value or "").strip().lower()
        if raw:
            return raw
        slug = " ".join([route_path.lower(), file_path.lower(), page_id.lower()])
        if "/profile" in slug or slug.endswith("/profile/index.html") or "profile" in page_id.lower():
            return "profile"
        if any(token in slug for token in ("/workspace", "workspace.html", "detail", "details", "feature")):
            return "workspace"
        if any(token in slug for token in ("/workbench", "workbench.html", "queue", "list", "records", "orders")):
            return "list"
        if any(token in slug for token in ("form", "create", "edit")):
            return "form"
        if route_path == "/":
            return "dashboard"
        return "page"

    @staticmethod
    def _normalize_handoff_paths(value: Any) -> list[str]:
        if isinstance(value, list):
            paths = [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
        elif isinstance(value, str) and value.strip():
            paths = [value.strip()]
        else:
            paths = []
        normalized = []
        for path in paths:
            normalized.append(path if path.startswith("/") else f"/{path}")
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _state_marker_base(page_id: str, file_path: str, component_name: str) -> str:
        path_obj = Path(file_path)
        stem = path_obj.parent.name if path_obj.stem == "index" and path_obj.parent.name else path_obj.stem
        raw_value = page_id.strip() or stem or component_name or "page"
        slug = re.sub(r"[^a-z0-9]+", "-", raw_value.lower()).strip("-")
        return slug or "page"

    @staticmethod
    def _default_state_contract(*, state_kind: str, page_label: str, marker_base: str) -> str:
        if state_kind == "loading":
            return f"Provide #{marker_base}-loading or [data-ui-state=\"loading\"] as a compact hidden/empty marker or contextual {page_label} refresh status; do not use generic 'Loading data...' copy."
        return f"Provide #{marker_base}-error or [data-ui-state=\"error\"] as a compact hidden/empty marker or contextual {page_label} error status; do not use generic 'Unable to load data. Try again.' copy."

    @staticmethod
    def _default_routes_file(role: str) -> str:
        return f"miniapp/app/routes/{role}.py"

    @staticmethod
    def _default_page_file(role: str, component_name: str, *, route_path: str | None = None) -> str:
        return MiniappGenerationPaths._default_page_file(role, component_name, route_path=route_path)

    @staticmethod
    def _default_page_asset_path(file_path: str, *, asset_kind: str) -> str:
        return MiniappGenerationPaths._default_page_asset_path(file_path, asset_kind=asset_kind)

    def _component_name(self, role: str, payload: dict[str, Any], index: int) -> str:
        raw_value = str(payload.get("component_name") or payload.get("title") or payload.get("page_id") or f"{role}_page_{index + 1}").strip()
        cleaned = re.sub(r"[^0-9A-Za-z]+", " ", raw_value)
        pascal = "".join(part[:1].upper() + part[1:] for part in cleaned.split() if part)
        prefix = ROLE_COMPONENT_PREFIX[role]
        if not pascal:
            return f"{prefix}GeneratedPage{index + 1}"
        if not pascal.startswith(prefix):
            pascal = f"{prefix}{pascal}"
        if not pascal.endswith("Page"):
            pascal = f"{pascal}Page"
        return pascal

    @staticmethod
    def _build_execution_plan(
        *,
        role_scope: list[str],
        roles: dict[str, dict[str, Any]],
        shared_files: list[str],
        backend_targets: list[str],
        target_files: list[str],
        generation_clusters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return MiniappGenerationPageGraphRuntime._build_execution_plan(
            role_scope=role_scope,
            roles=roles,
            shared_files=shared_files,
            backend_targets=backend_targets,
            target_files=target_files,
            generation_clusters=generation_clusters,
        )

    def _collect_files_to_read(self, files_to_read: list[str], target_files: list[str], workspace_tree: list[dict[str, str]]) -> list[str]:
        return self.generation_targeting._collect_files_to_read(files_to_read, target_files, workspace_tree)

    @staticmethod
    def _is_canonical_target_path(path: str) -> bool:
        return MiniappGenerationTargeting._is_canonical_target_path(path)

    def _canonicalize_target_files(self, target_files: list[str], *, scope_mode: str) -> list[str]:
        return self.generation_targeting._canonicalize_target_files(target_files, scope_mode=scope_mode)

    @classmethod
    def _canonicalize_static_target_path(cls, path: str) -> str | None:
        return MiniappGenerationTargeting._canonicalize_static_target_path(path)

    @staticmethod
    def _planned_page_static_targets(page_graph: dict[str, Any]) -> set[str]:
        return MiniappGenerationTargeting._planned_page_static_targets(page_graph)

    @classmethod
    def _prune_non_page_static_targets(cls, target_files: list[str], *, page_graph: dict[str, Any]) -> list[str]:
        return MiniappGenerationTargeting._prune_non_page_static_targets(target_files, page_graph=page_graph)

    @classmethod
    def _sanitize_backend_targets(cls, backend_targets: list[str]) -> list[str]:
        return MiniappGenerationTargeting._sanitize_backend_targets(backend_targets)

    @classmethod
    def _sanitize_planner_target_files(
        cls,
        *,
        target_files: list[str],
        backend_targets: list[str],
        page_graph: dict[str, Any],
    ) -> list[str]:
        return MiniappGenerationTargeting._sanitize_planner_target_files(
            target_files=target_files,
            backend_targets=backend_targets,
            page_graph=page_graph,
        )

    @classmethod
    def _expand_page_triplet_targets(cls, target_files: list[str]) -> list[str]:
        return MiniappGenerationTargeting._expand_page_triplet_targets(target_files)

    @staticmethod
    def _build_generation_clusters(target_files: list[str]) -> list[dict[str, Any]]:
        return MiniappGenerationPageGraphRuntime._build_generation_clusters(target_files)

    @staticmethod
    def _backend_cluster_name_for_path(path: str) -> str:
        return MiniappGenerationPageGraphRuntime._backend_cluster_name_for_path(path)

    @staticmethod
    def _static_cluster_suffix_for_path(path: str) -> str:
        return MiniappGenerationPageGraphRuntime._static_cluster_suffix_for_path(path)

    @classmethod
    def _expand_cluster_targets_for_safe_companions(
        cls,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        invalid_paths: list[str],
    ) -> list[str] | None:
        return MiniappGenerationTargeting._expand_cluster_targets_for_safe_companions(
            cluster_name=cluster_name,
            cluster_targets=cluster_targets,
            invalid_paths=invalid_paths,
        )

    @staticmethod
    def _page_edit_parallelism(*, scope_mode: str, generation_mode: GenerationMode) -> int:
        if scope_mode in {"minimal_patch", "role_partial_build", "workflow_partial_build"}:
            default = "3"
        else:
            default = "2" if generation_mode == GenerationMode.FAST else "3"
        configured = max(1, int(os.getenv("PAGE_EDIT_MAX_PARALLELISM", default)))
        return configured

    async def _resolve_page_file_edits_async(
        self,
        *,
        selected_pages: list[tuple[str, dict[str, Any]]],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        entity_contract: dict[str, Any],
        page_graph: dict[str, Any],
        role_contract: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        workspace_id: str | None = None,
        draft_run_id: str | None = None,
        workspace_tree: list[dict[str, str]] | None = None,
        draft_source=None,
    ) -> list[dict[str, Any]]:
        return await self.generation_codegen._resolve_page_file_edits_async(
            selected_pages=selected_pages,
            prompt=prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            page_graph=page_graph,
            role_contract=role_contract,
            scope_mode=scope_mode,
            intent=intent,
            file_contexts=file_contexts,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            workspace_tree=workspace_tree,
            draft_source=draft_source,
        )
