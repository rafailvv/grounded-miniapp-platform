from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.generation_contract_frontend import MiniappGenerationContractFrontend
from app.modules.miniapp_generation_runtime.generation_contract_routes import MiniappGenerationContractRoutes
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractPass(MiniappGenerationRuntimeOwner):
    ContractSyncMode = Literal["bootstrap_only", "repair_invariants"]
    _FORBIDDEN_GENERATED_ARTIFACTS = (
        "miniapp/app/generated/static_runtime_manifest.json",
        "miniapp/app/generated/role_seed.json",
        "miniapp/app/generated/role_experience.json",
        "miniapp/app/generated/runtime_state.json",
    )

    def _ensure_runtime_artifact_operations(
        self,
        *,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, object],
        role_scope: list[str],
        generation_mode: GenerationMode,
        operations: list[DraftFileOperation],
        existing_route_manifest: dict[str, object] | None = None,
        existing_runtime_manifest: dict[str, object] | None = None,
        existing_generated_graph: dict[str, object] | None = None,
        preserve_existing_roles: bool = False,
        draft_source: Path | None = None,
    ) -> list[DraftFileOperation]:
        builder = getattr(self, "artifact_builder", None) or self._artifact_builder()
        return builder.ensure_runtime_artifact_operations(
            grounded_spec=grounded_spec,
            page_graph=page_graph,
            role_scope=role_scope,
            generation_mode=generation_mode,
            operations=operations,
            existing_route_manifest=existing_route_manifest,
            existing_runtime_manifest=existing_runtime_manifest,
            existing_generated_graph=existing_generated_graph,
            preserve_existing_roles=preserve_existing_roles,
            draft_source=draft_source,
        )

    def _ensure_app_level_test_operations(
        self,
        *,
        page_graph: dict[str, object],
        role_scope: list[str],
        entity_contract: dict[str, object] | None,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        builder = getattr(self, "artifact_builder", None) or self._artifact_builder()
        return builder.ensure_app_level_test_operations(
            page_graph=page_graph,
            role_scope=role_scope,
            entity_contract=entity_contract,
            operations=operations,
        )

    def _run_pre_apply_contract_pass(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        page_graph: dict[str, object],
        role_scope: list[str],
        generation_mode: GenerationMode,
        operations: list[DraftFileOperation],
        entity_contract: dict[str, object] | None = None,
        contract_sync_mode: ContractSyncMode = "bootstrap_only",
    ) -> list[DraftFileOperation]:
        preserve_existing_roles = (
            bool(role_scope)
            and set(role_scope) < {"client", "specialist", "manager"}
        )
        existing_route_manifest: dict[str, object] | None = None
        existing_runtime_manifest: dict[str, object] | None = None
        existing_generated_graph: dict[str, object] | None = None
        if preserve_existing_roles:
            existing_route_manifest = self._load_existing_contract_artifact(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                file_path="miniapp/app/generated/route_manifest.json",
            )
            existing_runtime_manifest = self._load_existing_contract_artifact(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                file_path="miniapp/app/generated/runtime_manifest.json",
            )
            existing_generated_graph = self._load_existing_contract_artifact(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                file_path="artifacts/generated_app_graph.json",
            )
        grounded_spec: GroundedSpecModel | None = None
        try:
            grounded_spec = self._resolve_grounded_spec_for_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                operations=operations,
            )
        except ValueError:
            if contract_sync_mode != "repair_invariants":
                raise
        ensured = (
            self._ensure_runtime_artifact_operations(
                grounded_spec=grounded_spec,
                page_graph=page_graph,
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=operations,
                existing_route_manifest=existing_route_manifest,
                existing_runtime_manifest=existing_runtime_manifest,
                existing_generated_graph=existing_generated_graph,
                preserve_existing_roles=preserve_existing_roles,
                draft_source=self.workspace_service.draft_source_dir(workspace_id, draft_run_id),
            )
            if grounded_spec is not None
            else list(operations)
        )
        ensured = self._remove_seeded_generated_artifacts(operations=ensured)
        if grounded_spec is not None:
            ensured = self._ensure_runtime_artifact_operations(
                grounded_spec=grounded_spec,
                page_graph=page_graph,
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=ensured,
                existing_route_manifest=existing_route_manifest,
                existing_runtime_manifest=existing_runtime_manifest,
                existing_generated_graph=existing_generated_graph,
                preserve_existing_roles=preserve_existing_roles,
                draft_source=self.workspace_service.draft_source_dir(workspace_id, draft_run_id),
            )
        ensured = self._normalize_sqlalchemy_db_defaults(operations=ensured)
        ensured = self._normalize_generated_surface_operations(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            page_graph=page_graph,
            operations=ensured,
            entity_contract=entity_contract,
        )
        ensured = self._ensure_app_level_test_operations(
            page_graph=page_graph,
            role_scope=role_scope,
            entity_contract=entity_contract,
            operations=ensured,
        )
        return ensured

    @staticmethod
    def _declared_routes_by_role(page_graph: dict[str, object]) -> dict[str, set[str]]:
        declared: dict[str, set[str]] = {}
        for role, payload in (page_graph.get("roles") or {}).items():
            if not isinstance(payload, dict):
                continue
            role_routes: set[str] = set()
            for page in (payload.get("pages") or []):
                if not isinstance(page, dict):
                    continue
                route_path = str(page.get("route_path") or "").strip()
                if route_path:
                    role_routes.add(route_path)
            declared[str(role)] = role_routes
        return declared

    @staticmethod
    def _static_page_contract(file_path: str) -> tuple[str | None, str | None, str]:
        normalized = str(file_path or "").strip().replace("\\", "/")
        match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)(?P<suffix>(?:/[^/]+)*)/index\.html",
            normalized,
        )
        if not match:
            return None, None, ""
        role = str(match.group("role") or "")
        suffix = str(match.group("suffix") or "")
        asset_base = f"miniapp/app/static/{role}{suffix}/"
        expected_style_href = MiniappGenerationContractFrontend._static_asset_href(f"{asset_base}styles.css")
        expected_script_src = MiniappGenerationContractFrontend._static_asset_href(f"{asset_base}app.js")
        return expected_style_href, expected_script_src, role

    def _normalize_generated_surface_operations(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        page_graph: dict[str, object],
        operations: list[DraftFileOperation],
        entity_contract: dict[str, object] | None,
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        effective_entity_contract = dict(entity_contract or {})
        schema_operation = operation_map.get("miniapp/app/schemas.py")
        schema_content = (
            str(schema_operation.content or "")
            if schema_operation is not None and schema_operation.content is not None
            else self.workspace_service.try_read_text_file(
                workspace_id,
                "miniapp/app/schemas.py",
                run_id=draft_run_id,
            )
        )
        schema_status_literals = MiniappGenerationContractFrontend._schema_status_literals_from_text(schema_content or "")
        if schema_status_literals:
            effective_entity_contract["status_literals"] = schema_status_literals
        declared_routes_by_role = self._declared_routes_by_role(page_graph)
        entity_route_file = str((effective_entity_contract or {}).get("route_file") or "").strip().replace("\\", "/")
        route_modules_to_include: list[str] = []
        if entity_route_file.startswith("miniapp/app/routes/") and entity_route_file.endswith(".py"):
            entity_module_name = Path(entity_route_file).stem
            if entity_module_name not in {"", "health", "profiles", "client", "specialist", "manager", "role_pages"}:
                route_modules_to_include.append(entity_module_name)
        route_modules_to_include.append("runtime")
        for file_path, operation in list(operation_map.items()):
            if operation.content is None:
                continue
            normalized_path = str(file_path or "").replace("\\", "/")
            updated = str(operation.content or "")
            if normalized_path.startswith("miniapp/app/static/") and normalized_path.endswith("/index.html"):
                expected_style_href, expected_script_src, role = self._static_page_contract(normalized_path)
                updated = MiniappGenerationContractFrontend._clean_static_ui_text_artifacts(updated)
                updated = MiniappGenerationContractFrontend._normalize_entity_api_paths(updated, effective_entity_contract)
                updated = MiniappGenerationContractFrontend._normalize_api_aliases_in_text(updated)
                updated = MiniappGenerationContractFrontend._ensure_head_asset_link(
                    updated,
                    MiniappGenerationContractFrontend._static_asset_href("miniapp/app/static/shared/base.css"),
                )
                if expected_style_href:
                    updated = MiniappGenerationContractFrontend._ensure_head_asset_link(updated, expected_style_href)
                updated = MiniappGenerationContractFrontend._ensure_preview_bridge_ref(updated)
                if expected_script_src:
                    updated = MiniappGenerationContractFrontend._ensure_body_script_ref(updated, expected_script_src)
                updated = MiniappGenerationContractFrontend._ensure_page_shell_contract(updated)
                if role:
                    updated = MiniappGenerationContractFrontend._normalize_role_local_links(
                        updated,
                        role=role,
                        declared_routes=declared_routes_by_role.get(role, set()),
                    )
            elif normalized_path.startswith("miniapp/app/static/") and normalized_path.endswith(".js"):
                updated = MiniappGenerationContractFrontend._clean_static_ui_text_artifacts(updated)
                updated = MiniappGenerationContractFrontend._normalize_entity_api_paths(updated, effective_entity_contract)
                updated = MiniappGenerationContractFrontend._normalize_api_aliases_in_text(updated)
                updated = MiniappGenerationContractFrontend._inject_status_alias_bridge(
                    normalized_path,
                    updated,
                    effective_entity_contract,
                )
            elif normalized_path == entity_route_file:
                updated = MiniappGenerationContractRoutes._normalize_entity_route_module_source(
                    updated,
                    effective_entity_contract,
                )
            elif normalized_path == "miniapp/app/routes/runtime.py":
                updated = MiniappGenerationContractRoutes._normalize_runtime_manifest_route_source(updated)
            if updated != str(operation.content or ""):
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation=operation.operation,
                    content=updated,
                    reason="Pre-apply contract sync: normalize route and frontend shell contracts before exact checks so generated drafts keep canonical API paths, role-local assets, and runtime-safe route modules.",
                )
        main_file_path = "miniapp/app/main.py"
        main_operation = operation_map.get(main_file_path)
        main_content = (
            str(main_operation.content or "")
            if main_operation is not None and main_operation.content is not None
            else self.workspace_service.try_read_text_file(
                workspace_id,
                main_file_path,
                run_id=draft_run_id,
            )
        )
        if main_content:
            normalized_main = MiniappGenerationContractRoutes._normalize_main_app_router_includes(
                main_content,
                route_modules=route_modules_to_include,
            )
            if normalized_main != main_content:
                operation_map[main_file_path] = DraftFileOperation(
                    file_path=main_file_path,
                    operation=main_operation.operation if main_operation is not None else "replace",
                    content=normalized_main,
                    reason="Pre-apply contract sync: ensure canonical entity and runtime route modules are reachable from main.py so generated backends expose the API surface used by frontend and exact checks.",
                )
        return list(operation_map.values())

    @staticmethod
    def _normalize_sqlalchemy_db_defaults(
        *,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        db_path = "miniapp/app/db.py"
        db_operation = operation_map.get(db_path)
        if db_operation is None or db_operation.content is None:
            return operations
        content = str(db_operation.content or "")
        if "mapped_column" not in content or "default_factory=" not in content:
            return operations
        normalized = content.replace("default_factory=", "default=")
        if normalized == content:
            return operations
        operation_map[db_path] = DraftFileOperation(
            file_path=db_path,
            operation=db_operation.operation,
            content=normalized,
            reason="Pre-apply contract sync: normalize SQLAlchemy declarative defaults so db.py does not emit dataclass-only mapped_column(default_factory=...) arguments.",
        )
        return list(operation_map.values())

    @staticmethod
    def _grounded_spec_from_operations(operations: list[DraftFileOperation]) -> GroundedSpecModel:
        for operation in operations:
            if operation.file_path == "artifacts/grounded_spec.json" and operation.content:
                try:
                    return GroundedSpecModel.model_validate(json.loads(operation.content))
                except Exception:
                    break
        raise ValueError("grounded_spec.json operation must exist before runtime artifact synthesis.")

    def _resolve_grounded_spec_for_contract_pass(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> GroundedSpecModel:
        try:
            return self._grounded_spec_from_operations(operations)
        except ValueError:
            content = self.workspace_service.try_read_text_file(
                workspace_id,
                "artifacts/grounded_spec.json",
                run_id=draft_run_id,
            )
            if content:
                try:
                    return GroundedSpecModel.model_validate(json.loads(content))
                except Exception:
                    pass
            spec_payload = self.current_report(workspace_id, "spec")
            if spec_payload:
                try:
                    return GroundedSpecModel.model_validate(spec_payload)
                except Exception:
                    pass
            raise ValueError("grounded_spec.json is required for the contract pass and was not found in the draft or reports.")

    def _remove_seeded_generated_artifacts(
        self,
        *,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        for file_path in self._FORBIDDEN_GENERATED_ARTIFACTS:
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="delete",
                content=None,
                reason="Pre-apply contract sync: remove deprecated generated seed artifacts so runtime state is derived from real code and DB data only.",
            )
        return list(operation_map.values())

    def _load_existing_contract_artifact(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        file_path: str,
    ) -> dict[str, object] | None:
        content = self.workspace_service.try_read_text_file(
            workspace_id,
            file_path,
            run_id=draft_run_id,
        )
        if not content:
            return None
        try:
            payload = json.loads(content)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None
