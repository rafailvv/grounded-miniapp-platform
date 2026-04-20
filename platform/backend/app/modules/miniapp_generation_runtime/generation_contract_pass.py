from __future__ import annotations

import json
from typing import Literal

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel

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
    ) -> list[DraftFileOperation]:
        builder = getattr(self, "artifact_builder", None) or self._artifact_builder()
        return builder.ensure_runtime_artifact_operations(
            grounded_spec=grounded_spec,
            page_graph=page_graph,
            role_scope=role_scope,
            generation_mode=generation_mode,
            operations=operations,
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
            )
            if grounded_spec is not None
            else list(operations)
        )
        ensured = self._normalize_sqlalchemy_db_defaults(operations=ensured)
        ensured = self._remove_seeded_generated_artifacts(operations=ensured)
        ensured = self._synchronize_minimal_workflow_route_contracts(
            workspace_id,
            draft_run_id,
            ensured,
            contract_sync_mode=contract_sync_mode,
        )
        if contract_sync_mode == "repair_invariants":
            ensured = self._synchronize_profile_schema_contract(
                workspace_id,
                draft_run_id,
                ensured,
                contract_sync_mode=contract_sync_mode,
            )
        ensured = self.runtime_contract_sync.synchronize(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
            contract_sync_mode=contract_sync_mode,
        )
        ensured = self._synchronize_main_runtime_contract(
            workspace_id,
            draft_run_id,
            ensured,
        )
        ensured = self._synchronize_route_schema_contract(
            workspace_id,
            draft_run_id,
            ensured,
            contract_sync_mode="repair_invariants",
        )
        ensured = self._synchronize_frontend_api_contract(
            workspace_id,
            draft_run_id,
            ensured,
            entity_contract=entity_contract,
            contract_sync_mode="repair_invariants",
        )
        ensured = self._synchronize_basic_page_state_contract(
            workspace_id,
            draft_run_id,
            page_graph=page_graph,
            operations=ensured,
            contract_sync_mode="repair_invariants",
        )
        if grounded_spec is not None:
            ensured = self._ensure_runtime_artifact_operations(
                grounded_spec=grounded_spec,
                page_graph=page_graph,
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=ensured,
            )
        ensured = self._ensure_app_level_test_operations(
            page_graph=page_graph,
            role_scope=role_scope,
            entity_contract=entity_contract,
            operations=ensured,
        )
        return ensured

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
                reason="Pre-apply contract sync: remove legacy generated seed artifacts so runtime state is derived from real code and DB data only.",
            )
        return list(operation_map.values())
