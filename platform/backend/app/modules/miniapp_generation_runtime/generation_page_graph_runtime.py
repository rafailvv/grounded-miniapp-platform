from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationPageGraphRuntime(MiniappGenerationRuntimeOwner):
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
        target_set = set(target_files)
        role_steps: list[dict[str, Any]] = []
        active_role_scope: list[str] = []
        for role in role_scope:
            pages = roles.get(role, {}).get("pages") or []
            selected_files: list[str] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for path in (page.get("file_path"), page.get("style_path"), page.get("script_path")):
                    if isinstance(path, str) and path in target_set:
                        selected_files.append(path)
            routes_file = roles.get(role, {}).get("routes_file")
            if isinstance(routes_file, str) and routes_file in target_set:
                selected_files.append(routes_file)
            selected_files = list(dict.fromkeys(selected_files))
            if selected_files:
                active_role_scope.append(role)
            role_steps.append(
                {
                    "role": role,
                    "status": "complete" if selected_files else "complete",
                    "target_files": selected_files,
                    "skipped": not bool(selected_files),
                }
            )
        backend_files = [path for path in backend_targets if path in target_set]
        frontend_files = [
            path
            for path in list(dict.fromkeys(shared_files))
            if path in target_set and path not in backend_files
        ]
        return {
            "role_steps": role_steps,
            "miniapp": {
                "status": "complete" if not backend_files else "pending",
                "target_files": backend_files,
                "skipped": not bool(backend_files),
            },
            "frontend": {
                "status": "complete" if not frontend_files else "pending",
                "target_files": frontend_files,
                "skipped": not bool(frontend_files),
            },
            "generation_clusters": generation_clusters,
            "active_role_scope": active_role_scope,
        }

    @classmethod
    def _build_generation_clusters(cls, target_files: list[str]) -> list[dict[str, Any]]:
        backend_targets: list[str] = []
        shared_static_targets: list[str] = []
        role_page_groups: dict[tuple[str, str], list[str]] = {}
        for path in target_files:
            if path.startswith("miniapp/"):
                if path.startswith("miniapp/app/static/shared/"):
                    shared_static_targets.append(path)
                    continue
                if path.startswith("miniapp/app/static/manager/"):
                    role = "manager"
                elif path.startswith("miniapp/app/static/specialist/"):
                    role = "specialist"
                elif path.startswith("miniapp/app/static/client/"):
                    role = "client"
                else:
                    backend_targets.append(path)
                    continue
                cluster_suffix = cls._static_cluster_suffix_for_path(path)
                role_page_groups.setdefault((role, cluster_suffix), []).append(path)
                continue
        clusters: list[dict[str, Any]] = []
        if shared_static_targets:
            clusters.append({"cluster_name": "shared_static", "target_files": list(dict.fromkeys(shared_static_targets))})
        if backend_targets:
            backend_groups: dict[str, list[str]] = {}
            support_targets: list[str] = []
            for path in list(dict.fromkeys(backend_targets)):
                normalized = path.strip().replace("\\", "/")
                if normalized in {
                    "miniapp/app/routes/__init__.py",
                    "miniapp/app/routes/role_pages.py",
                }:
                    continue
                cluster_name = cls._backend_cluster_name_for_path(path)
                if cluster_name == "backend_support":
                    support_targets.append(path)
                    continue
                backend_groups.setdefault(cluster_name, []).append(path)
            if support_targets:
                clusters.append({"cluster_name": "backend_support", "target_files": support_targets})
            for cluster_name in sorted(backend_groups):
                clusters.append({"cluster_name": cluster_name, "target_files": backend_groups[cluster_name]})
        role_priority = {"manager": 0, "specialist": 1, "client": 2}
        for (role, cluster_suffix), paths in sorted(
            role_page_groups.items(),
            key=lambda item: (role_priority.get(item[0][0], 99), 1 if item[0][1] == "root" else 0, item[0][1]),
        ):
            clusters.append({"cluster_name": f"role_{role}_ui_{cluster_suffix}", "target_files": list(dict.fromkeys(paths))})
        return clusters

    @staticmethod
    def _backend_cluster_name_for_path(path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        if normalized.startswith("miniapp/app/routes/") and normalized.endswith(".py"):
            stem = Path(normalized).stem.lower()
            if stem in {"__init__", "profiles"}:
                return "backend_support"
            return f"backend_route_{re.sub(r'[^a-z0-9_]+', '_', stem).strip('_') or 'module'}"
        if normalized in {"miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py"}:
            return "backend_support"
        return "backend_support"

    @staticmethod
    def _static_cluster_suffix_for_path(path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        root_match = re.fullmatch(
            r"miniapp/app/static/(client|specialist|manager)/(index\.html|styles\.css|app\.js)",
            normalized,
        )
        if root_match:
            return "root"
        page_match = re.fullmatch(
            r"miniapp/app/static/(client|specialist|manager)/([^/]+)/(index\.html|styles\.css|app\.js)",
            normalized,
        )
        if page_match:
            return re.sub(r"[^a-z0-9_]+", "_", page_match.group(2).lower()).strip("_") or "page"
        return "misc"
