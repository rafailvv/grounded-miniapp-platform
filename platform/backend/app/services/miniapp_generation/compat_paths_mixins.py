from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    MiniappGenerationPageGraphRuntime,
    MiniappGenerationPaths,
    MiniappGenerationTargeting,
)


class CompatPathsMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_snake_case_filename": MiniappGenerationPaths,
            "_normalize_generated_file_path": MiniappGenerationPaths,
            "_normalize_path_list": MiniappGenerationPaths,
            "_normalize_string_list": MiniappGenerationPaths,
            "_normalize_role_route_path": MiniappGenerationPaths,
            "_absolute_role_route_path": MiniappGenerationPaths,
            "_default_page_file": MiniappGenerationPaths,
            "_default_page_asset_path": MiniappGenerationPaths,
            "_build_execution_plan": MiniappGenerationPageGraphRuntime,
            "_is_canonical_target_path": MiniappGenerationTargeting,
            "_is_legacy_role_entry_file": MiniappGenerationTargeting,
            "_canonicalize_static_target_path": MiniappGenerationTargeting,
            "_planned_page_static_targets": MiniappGenerationTargeting,
            "_prune_non_page_static_targets": MiniappGenerationTargeting,
            "_sanitize_backend_targets": MiniappGenerationTargeting,
            "_sanitize_planner_target_files": MiniappGenerationTargeting,
            "_expand_page_triplet_targets": MiniappGenerationTargeting,
            "_build_generation_clusters": MiniappGenerationPageGraphRuntime,
            "_backend_cluster_name_for_path": MiniappGenerationPageGraphRuntime,
            "_static_cluster_suffix_for_path": MiniappGenerationPageGraphRuntime,
            "_expand_cluster_targets_for_safe_companions": MiniappGenerationTargeting,
            "_detect_missing_static_asset_targets": MiniappGenerationTargeting,
            "_extract_static_asset_targets": MiniappGenerationTargeting,
            "_resolve_static_asset_target": MiniappGenerationTargeting,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_collect_files_to_read": "generation_targeting",
            "_canonicalize_target_files": "generation_targeting",
        }
