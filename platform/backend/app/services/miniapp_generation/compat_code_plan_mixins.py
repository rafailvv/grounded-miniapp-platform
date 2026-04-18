from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    MiniappGenerationCodePlanDefaults,
    MiniappGenerationCodePlanNormalization,
    MiniappGenerationCodePlanPrompts,
    MiniappGenerationRoleContract,
)


class CompatCodePlanMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_deterministic_code_plan_payload": MiniappGenerationCodePlanDefaults,
            "_deterministic_role_pages": MiniappGenerationCodePlanDefaults,
            "_code_plan_schema": MiniappGenerationCodePlanPrompts,
            "_code_plan_system_prompt": MiniappGenerationCodePlanPrompts,
            "_code_plan_section_system_prompt": MiniappGenerationCodePlanPrompts,
            "_code_plan_partial_schema": MiniappGenerationCodePlanPrompts,
            "_role_contract_schema": MiniappGenerationRoleContract,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_resolve_role_contract": "generation_role_contract",
            "_should_use_compiled_role_contract": "generation_role_contract",
            "_normalize_role_contract": "generation_role_contract",
            "_compiled_role_contract": "generation_role_contract",
            "_role_contract_user_prompt": "generation_role_contract",
            "_role_contract_gate_issues": "generation_role_contract",
            "_resolve_code_plan": "generation_code_plan",
            "_generate_code_plan_sections_with_timeout": "generation_code_plan",
            "_generate_code_plan_sections": "generation_code_plan",
            "_normalize_page_plan": "generation_code_plan_normalization",
            "_finalize_role_pages": "generation_code_plan_normalization",
            "_normalize_page_definition": "generation_code_plan_normalization",
            "_code_plan_user_prompt": "generation_code_plan_prompts",
            "_code_plan_section_user_prompt": "generation_code_plan_prompts",
            "_workspace_path_hints": "generation_code_plan_prompts",
        }
