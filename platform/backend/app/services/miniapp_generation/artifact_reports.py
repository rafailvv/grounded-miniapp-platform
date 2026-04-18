from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.artifacts import MaterializationReport


class ArtifactReportsMixin:
    def build_stage_reports(
        self,
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> list[dict[str, Any]]:
        planned_backend = {
            str(path)
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        }
        role_page_paths = {
            str(page.get("file_path"))
            for role in role_scope
            for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("file_path"), str)
        }
        required_manifests = {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        }
        backend_hits = sorted(path for path in planned_backend if path in realized_paths)
        page_hits = sorted(path for path in role_page_paths if path in realized_paths)
        manifest_hits = sorted(path for path in required_manifests if path in realized_paths)
        return [
            {
                "stage": "runtime_backend_foundation",
                "planned_files": sorted(planned_backend),
                "created_files": backend_hits,
                "completed": bool(planned_backend) and len(backend_hits) >= max(1, min(len(planned_backend), 3)),
            },
            {
                "stage": "workflow_page_surfaces",
                "planned_files": sorted(role_page_paths),
                "created_files": page_hits,
                "completed": len(page_hits) >= max(len(role_scope), len(role_page_paths) // 2 if role_page_paths else 0),
            },
            {
                "stage": "integration_contract_completion",
                "planned_files": sorted(required_manifests),
                "created_files": manifest_hits,
                "completed": "miniapp/app/generated/route_manifest.json" in manifest_hits
                and "artifacts/generated_app_graph.json" in manifest_hits,
            },
        ]

    def build_materialization_report(
        self,
        *,
        execution_class: str,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> MaterializationReport:
        normalized_realized_paths = {
            self._normalize_runtime_python_path(str(path))
            for path in realized_paths
            if isinstance(path, str)
        }
        planned_pages = [
            self._normalize_runtime_python_path(str(page.get("file_path")))
            for role in role_scope
            for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("file_path"), str)
        ]
        expected_backend_files = [
            self._normalize_runtime_python_path(str(path))
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        ]
        expected_manifests = [
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        ]
        missing_files = [path for path in planned_pages if path not in normalized_realized_paths]
        missing_backend_files = [path for path in expected_backend_files if path not in normalized_realized_paths]
        role_unique_page_counts: dict[str, int] = {}
        duplicate_page_file_roles: dict[str, list[str]] = {}
        role_page_counts = {
            role: sum(
                1
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict)
                and self._normalize_runtime_python_path(str(page.get("file_path") or "")) in normalized_realized_paths
            )
            for role in role_scope
        }
        for role in role_scope:
            role_pages = [
                self._normalize_runtime_python_path(str(page.get("file_path") or ""))
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict) and isinstance(page.get("file_path"), str)
            ]
            role_unique_page_counts[role] = len(set(role_pages))
            duplicates = sorted(path for path, count in Counter(role_pages).items() if count > 1 and path)
            if duplicates:
                duplicate_page_file_roles[role] = duplicates
        backend_surface_ok = bool(expected_backend_files) and not missing_backend_files
        page_surface_ok = (
            not missing_files
            and not duplicate_page_file_roles
            and all(count >= 2 for count in role_page_counts.values())
        )
        manifest_surface_ok = all(path in normalized_realized_paths for path in expected_manifests)
        collapsed_surface = not page_surface_ok and all(count <= 2 for count in role_page_counts.values())
        return MaterializationReport(
            execution_class=execution_class,  # type: ignore[arg-type]
            planned_files=sorted(dict.fromkeys(planned_pages)),
            created_files=sorted(normalized_realized_paths),
            missing_files=sorted(dict.fromkeys(missing_files)),
            expected_backend_files=sorted(dict.fromkeys(expected_backend_files)),
            missing_backend_files=sorted(dict.fromkeys(missing_backend_files)),
            backend_surface_ok=backend_surface_ok,
            page_surface_ok=page_surface_ok,
            manifest_surface_ok=manifest_surface_ok,
            collapsed_surface=collapsed_surface,
            role_page_counts=role_page_counts,
            role_unique_page_counts=role_unique_page_counts,
            duplicate_page_file_roles=duplicate_page_file_roles,
            stage_reports=self.build_stage_reports(page_graph=page_graph, role_scope=role_scope, realized_paths=normalized_realized_paths),
        )
