from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation, FixScopeEntry
from app.modules.miniapp_agent_loop.fix_types import FixTurnContext
from app.modules.miniapp_agent_loop.fix_scope_builder import FixScopeBuilder

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixScopeRuntime:
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
        if not self.service._allow_test_file_writes_for_failure(failure_class):
            entries = [entry for entry in entries if not entry.file_path.startswith("miniapp/tests/")]
        return entries

    @staticmethod
    def deterministic_companion_scope(file_path: str) -> list[str]:
        return FixScopeBuilder.deterministic_companion_scope(file_path)

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
        bundle: list[str] = []
        for file_path in implicated_files:
            if file_path.startswith("miniapp/app/static/") and file_path.endswith((".html", ".css", ".js")):
                parent = Path(file_path).parent
                for sibling in (parent / "index.html", parent / "styles.css", parent / "app.js"):
                    normalized = sibling.as_posix()
                    if self.service._file_exists(workspace_id, run_id, normalized) or self.allow_missing_scope_path(normalized):
                        bundle.append(normalized)
        return list(dict.fromkeys(bundle))

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

    def deterministic_contract_repair_operations(
        self,
        *,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
        generation_mode: GenerationMode,
    ) -> list[DraftFileOperation]:
        if self.service.generation_service is None:
            return []
        page_graph = self.page_graph_for_deterministic_repair(workspace_id, run_id)
        role_scope = [role for role in ((page_graph.get("roles") or {}).keys()) if role in {"client", "specialist", "manager"}]
        if not role_scope:
            role_scope = ["client", "specialist", "manager"]
        seed_paths = self.deterministic_contract_seed_paths(workspace_id, run_id, fix_turn, scope_entries)
        if not seed_paths:
            return []
        seed_operations: list[DraftFileOperation] = []
        for file_path in seed_paths:
            if not self.service._file_exists(workspace_id, run_id, file_path):
                continue
            absolute_path = self.service.workspace_service.draft_source_dir(workspace_id, run_id) / file_path
            if absolute_path.is_dir():
                continue
            seed_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=self.service.workspace_service.read_file(workspace_id, file_path, run_id=run_id),
                    reason="Deterministic contract repair seed.",
                )
            )
        if not seed_operations:
            return []
        repaired = self.service.generation_service._run_pre_apply_contract_pass(
            workspace_id=workspace_id,
            draft_run_id=run_id,
            page_graph=page_graph,
            role_scope=role_scope,
            generation_mode=generation_mode,
            operations=seed_operations,
        )
        changed: list[DraftFileOperation] = []
        for operation in repaired:
            if operation.file_path.startswith(("artifacts/", "miniapp/app/generated/", "miniapp/tests/")):
                continue
            if operation.operation not in {"replace", "create", "delete"}:
                continue
            exists = self.service._file_exists(workspace_id, run_id, operation.file_path)
            if operation.operation == "delete":
                if exists:
                    changed.append(operation)
                continue
            current_content = self.service.workspace_service.read_file(workspace_id, operation.file_path, run_id=run_id) if exists else ""
            if current_content != (operation.content or ""):
                changed.append(operation)
        return changed

    def page_graph_for_deterministic_repair(self, workspace_id: str, run_id: str) -> dict[str, Any]:
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

    def deterministic_contract_seed_paths(
        self,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries: list[FixScopeEntry],
    ) -> list[str]:
        del workspace_id
        base_paths = [
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/profiles.py",
        ]
        candidates = list(base_paths)
        route_root = self.service.workspace_service.draft_source_dir(fix_turn.workspace_id, run_id) / "miniapp/app/routes"
        if route_root.exists():
            for route_file in sorted(route_root.glob("*.py")):
                relative = f"miniapp/app/routes/{route_file.name}"
                if relative not in candidates:
                    candidates.append(relative)
        for entry in scope_entries:
            if entry.file_path not in candidates:
                candidates.append(entry.file_path)
        for file_path in fix_turn.implicated_files:
            if file_path not in candidates:
                candidates.append(file_path)
        unique: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip().lstrip("./")
            if not normalized or normalized in unique:
                continue
            if self.service._file_exists(fix_turn.workspace_id, run_id, normalized):
                unique.append(normalized)
        return unique[:48]

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
