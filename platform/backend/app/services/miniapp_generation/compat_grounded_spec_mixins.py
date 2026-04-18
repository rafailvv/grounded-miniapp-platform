from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    GroundedSpecOrchestrationRuntime,
    GroundedSpecPayloadsRuntime,
    GroundedSpecPromptsRuntime,
)


class CompatGroundedSpecMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_grounded_spec_outline_schema": GroundedSpecPromptsRuntime,
            "_grounded_spec_outline_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_section_system_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_outline_user_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_user_prompt": GroundedSpecPromptsRuntime,
            "_grounded_spec_partial_schema": GroundedSpecPromptsRuntime,
            "_grounded_spec_section_user_prompt": GroundedSpecPromptsRuntime,
            "_compact_doc_refs": GroundedSpecPromptsRuntime,
            "_compact_creative_direction": GroundedSpecPromptsRuntime,
            "_normalize_model_payload": GroundedSpecPayloadsRuntime,
            "_normalize_assumption_item": GroundedSpecPayloadsRuntime,
            "_normalize_contradiction_item": GroundedSpecPayloadsRuntime,
            "_normalize_non_functional_requirement_item": GroundedSpecPayloadsRuntime,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_resolve_grounded_spec": "grounded_spec_orchestration",
            "_generate_grounded_spec_pair_with_timeout": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast_with_timeout": "grounded_spec_orchestration",
            "_resolve_grounded_spec_fast_inner": "grounded_spec_orchestration",
            "_generate_grounded_spec_pair": "grounded_spec_orchestration",
            "_generate_grounded_spec_section": "grounded_spec_orchestration",
        }
