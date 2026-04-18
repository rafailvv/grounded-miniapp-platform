from __future__ import annotations

import json

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractPass(MiniappGenerationRuntimeOwner):
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
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        builder = getattr(self, "artifact_builder", None) or self._artifact_builder()
        return builder.ensure_app_level_test_operations(
            page_graph=page_graph,
            role_scope=role_scope,
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
    ) -> list[DraftFileOperation]:
        ensured = self._ensure_runtime_artifact_operations(
            grounded_spec=self._resolve_grounded_spec_for_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                operations=operations,
            ),
            page_graph=page_graph,
            role_scope=role_scope,
            generation_mode=generation_mode,
            operations=operations,
        )
        ensured = self._synchronize_profile_schema_contract(workspace_id, draft_run_id, ensured)
        ensured = self._synchronize_route_schema_contract(workspace_id, draft_run_id, ensured)
        ensured = self.runtime_contract_sync.synchronize(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
        )
        ensured = self._synchronize_minimal_workflow_route_contracts(workspace_id, draft_run_id, ensured)
        ensured = self._synchronize_frontend_api_contract(workspace_id, draft_run_id, ensured)
        return self._synchronize_basic_page_state_contract(
            workspace_id,
            draft_run_id,
            page_graph=page_graph,
            operations=ensured,
        )

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
