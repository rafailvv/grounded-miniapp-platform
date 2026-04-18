from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    MiniappGenerationCodegenPrompts,
    MiniappGenerationCodegenSelection,
)


class CompatCodegenMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_whole_file_cluster_system_prompt": MiniappGenerationCodegenPrompts,
            "_page_edit_system_prompt": MiniappGenerationCodegenPrompts,
            "_composition_system_prompt": MiniappGenerationCodegenPrompts,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_resolve_code_edits": "generation_codegen",
            "_resolve_whole_file_code_edits": "generation_codegen",
            "_resolve_page_file_edits_async": "generation_codegen",
            "_whole_file_cluster_user_prompt": "generation_codegen_prompts",
            "_resolve_whole_file_cluster": "generation_codegen_clusters",
            "_timed_whole_file_cluster": "generation_codegen_clusters",
            "_page_edit_user_prompt": "generation_codegen_prompts",
            "_composition_user_prompt": "generation_codegen_prompts",
            "_resolve_page_file_edit": "generation_codegen_selection",
            "_resolve_composition_edit": "generation_codegen_clusters",
            "_timed_composition_cluster": "generation_codegen_clusters",
            "_selected_pages_for_edit": "generation_codegen_selection",
            "_backend_composition_targets": "generation_codegen_selection",
            "_frontend_composition_targets": "generation_codegen_selection",
            "_partition_frontend_composition_targets": "generation_codegen_selection",
        }
