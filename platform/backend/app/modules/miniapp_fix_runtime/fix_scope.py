from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.models.domain import FixScopeEntry
from app.modules.miniapp_agent_loop.fix_scope_builder import FixScopeBuilder

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixScopeRuntime:
    _READ_ONLY_CONTEXT_SURFACE_PREFIXES = (
        "miniapp/app/generated/",
    )
    _READ_ONLY_CONTEXT_SURFACES = {
        "artifacts/generated_app_graph.json",
    }

    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def build_write_scope(
        self,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
        existing_scope: list[FixScopeEntry],
    ) -> list[FixScopeEntry]:
        entries = self.service.fix_scope_builder.build_write_scope(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            existing_scope=existing_scope,
            allow_missing_scope_path=self.allow_missing_scope_path,
        )
        entries = [entry for entry in entries if not self._is_read_only_context_surface(entry.file_path)]
        if not self.service._allow_test_file_writes_for_failure(failure_class):
            entries = [entry for entry in entries if not entry.file_path.startswith("miniapp/tests/")]
        entries.sort(key=lambda entry: self._scope_priority(entry.file_path, failure_class))
        return entries

    def structural_scope_bundle(
        self,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
    ) -> list[str]:
        return self.service.fix_scope_builder.structural_scope_bundle(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            allow_missing_scope_path=self.allow_missing_scope_path,
        )

    def feature_scope_bundle(self, workspace_id: str, run_id: str, implicated_files: list[str]) -> list[str]:
        del workspace_id, run_id
        return list(dict.fromkeys(implicated_files))

    @staticmethod
    def merge_scope(
        current_scope: list[FixScopeEntry],
        next_scope: list[FixScopeEntry],
        scope_expansions: list[dict[str, Any]],
    ) -> list[FixScopeEntry]:
        return FixScopeBuilder.merge_scope(
            current_scope,
            next_scope,
            scope_expansions,
            max_scope_expansions=12,
        )

    def page_graph_for_run(self, workspace_id: str, run_id: str) -> dict[str, Any]:
        if self.service._file_exists(workspace_id, run_id, "artifacts/generated_app_graph.json"):
            graph_content = self.service.workspace_service.try_read_text_file(
                workspace_id,
                "artifacts/generated_app_graph.json",
                run_id=run_id,
            )
        else:
            graph_content = None
        if graph_content:
            try:
                graph = json.loads(graph_content)
                if isinstance(graph, dict):
                    return graph
            except Exception:
                pass
        report_payload = self.service.store.get("reports", f"page_graph:{workspace_id}") or {}
        report_graph = report_payload.get("page_graph")
        if isinstance(report_graph, dict):
            return report_graph
        if self.service._file_exists(workspace_id, run_id, "miniapp/app/generated/route_manifest.json"):
            route_manifest_content = self.service.workspace_service.try_read_text_file(
                workspace_id,
                "miniapp/app/generated/route_manifest.json",
                run_id=run_id,
            )
        else:
            route_manifest_content = None
        if route_manifest_content:
            try:
                route_manifest = json.loads(route_manifest_content)
                if isinstance(route_manifest, dict):
                    roles_payload = route_manifest.get("roles") or {}
                    if isinstance(roles_payload, dict):
                        return {"roles": roles_payload}
            except Exception:
                pass
        return {"roles": {}}

    @staticmethod
    def allow_missing_scope_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().lstrip("./")
        if not normalized:
            return False
        return normalized.startswith(
            (
                "miniapp/app/static/",
                "miniapp/app/routes/",
                "miniapp/app/generated/",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/main.py",
                "miniapp/tests/",
                "artifacts/",
            )
        )

    @staticmethod
    def scope_can_still_expand(existing_scope: list[FixScopeEntry], next_scope: list[FixScopeEntry]) -> bool:
        current = {entry.file_path for entry in existing_scope}
        upcoming = {entry.file_path for entry in next_scope}
        return bool(upcoming - current)

    @classmethod
    def _is_read_only_context_surface(cls, file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        return normalized in cls._READ_ONLY_CONTEXT_SURFACES or normalized.startswith(cls._READ_ONLY_CONTEXT_SURFACE_PREFIXES)

    @staticmethod
    def _scope_priority(file_path: str, failure_class: str) -> tuple[int, str]:
        normalized = str(file_path or "").strip()
        if normalized.startswith("miniapp/app/routes/"):
            return (0, normalized)
        if normalized == "miniapp/app/schemas.py":
            return (1, normalized)
        if normalized == "miniapp/app/db.py":
            return (2, normalized)
        if normalized == "miniapp/app/main.py" and failure_class in {
            "backend_framework_mismatch",
            "runtime_manifest_route_missing",
            "router_not_registered",
            "db_dependency_export_missing",
        }:
            return (3, normalized)
        if normalized == "miniapp/app/main.py":
            return (9, normalized)
        return (5, normalized)
