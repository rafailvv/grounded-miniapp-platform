from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_agent_loop.tool_agent_runtime import tool_patch_schema
from app.modules.miniapp_generation_runtime import (
    MiniappGenerationCodePlanPrompts,
    MiniappGenerationCodegenPrompts,
    MiniappGenerationCodegenSelection,
    MiniappGenerationCodegenClusters,
    MiniappGenerationRoleContract,
)


class ServicePromptCodegenMixins:
    @staticmethod
    def _role_contract_schema() -> dict[str, Any]:
        return MiniappGenerationRoleContract.role_contract_schema()

    @staticmethod
    def _code_plan_schema() -> dict[str, Any]:
        return MiniappGenerationCodePlanPrompts._code_plan_schema()

    @staticmethod
    def _code_edit_schema() -> dict[str, Any]:
        return tool_patch_schema()

    @staticmethod
    def _role_contract_system_prompt() -> str:
        return MiniappGenerationRoleContract.role_contract_system_prompt()

    def _role_contract_user_prompt(self, **kwargs: Any) -> str:
        return self.generation_role_contract.role_contract_user_prompt(**kwargs)

    @staticmethod
    def _code_plan_system_prompt() -> str:
        return MiniappGenerationCodePlanPrompts._code_plan_system_prompt()

    @staticmethod
    def _code_plan_section_system_prompt(section_title: str) -> str:
        return MiniappGenerationCodePlanPrompts._code_plan_section_system_prompt(section_title)

    def _code_plan_user_prompt(self, **kwargs: Any) -> str:
        return self.generation_code_plan_prompts._code_plan_user_prompt(**kwargs)

    def _code_plan_section_user_prompt(self, **kwargs: Any) -> str:
        return self.generation_code_plan_prompts._code_plan_section_user_prompt(**kwargs)

    def _workspace_path_hints(self, workspace_tree: list[dict[str, str]]) -> dict[str, Any]:
        return self.generation_code_plan_prompts._workspace_path_hints(workspace_tree)

    def _code_plan_partial_schema(self, field_names: list[str]) -> dict[str, Any]:
        return self.generation_code_plan_prompts._code_plan_partial_schema(field_names)

    @staticmethod
    def _page_edit_system_prompt() -> str:
        return MiniappGenerationCodegenPrompts._page_edit_system_prompt()

    def _page_edit_user_prompt(self, **kwargs: Any) -> str:
        return self.generation_codegen_prompts._page_edit_user_prompt(**kwargs)

    @staticmethod
    def _composition_system_prompt(stage_name: str) -> str:
        return MiniappGenerationCodegenPrompts._composition_system_prompt(stage_name)

    def _composition_user_prompt(self, **kwargs: Any) -> str:
        return self.generation_codegen_prompts._composition_user_prompt(**kwargs)

    def _resolve_page_file_edit(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        entity_contract: dict[str, Any],
        role: str,
        page: dict[str, Any],
        page_graph: dict[str, Any],
        role_contract: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        recovery_mode: str = "default",
        workspace_id: str | None = None,
        draft_run_id: str | None = None,
        workspace_tree: list[dict[str, str]] | None = None,
        draft_source=None,
    ) -> dict[str, Any]:
        return self.generation_codegen_selection._resolve_page_file_edit(
            prompt=prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role=role,
            page=page,
            page_graph=page_graph,
            role_contract=role_contract,
            scope_mode=scope_mode,
            intent=intent,
            file_contexts=file_contexts,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            recovery_mode=recovery_mode,
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            workspace_tree=workspace_tree,
            draft_source=draft_source,
        )

    def _resolve_composition_edit(self, **kwargs: Any) -> dict[str, Any]:
        return self.generation_codegen_clusters._resolve_composition_edit(**kwargs)

    def _timed_composition_cluster(self, **kwargs: Any) -> dict[str, Any]:
        return self.generation_codegen_clusters._timed_composition_cluster(**kwargs)

    @staticmethod
    def _selected_pages_for_edit(page_graph: dict[str, Any], target_files: set[str]) -> list[tuple[str, dict[str, Any]]]:
        return MiniappGenerationCodegenSelection._selected_pages_for_edit(page_graph, target_files)

    @staticmethod
    def _backend_composition_targets(target_files: list[str], selected_pages: list[tuple[str, dict[str, Any]]]) -> list[str]:
        return MiniappGenerationCodegenSelection._backend_composition_targets(target_files, selected_pages)

    @staticmethod
    def _frontend_composition_targets(target_files: list[str], selected_pages: list[tuple[str, dict[str, Any]]]) -> list[str]:
        return MiniappGenerationCodegenSelection._frontend_composition_targets(target_files, selected_pages)

    @staticmethod
    def _partition_frontend_composition_targets(target_files: list[str], page_graph: dict[str, Any]) -> tuple[list[str], list[str]]:
        return MiniappGenerationCodegenSelection._partition_frontend_composition_targets(target_files, page_graph)
