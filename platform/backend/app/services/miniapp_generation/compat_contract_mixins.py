from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    MiniappGenerationContractFrontend,
    MiniappGenerationContractPass,
    MiniappGenerationContractRoutes,
)


class CompatContractMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_grounded_spec_from_operations": MiniappGenerationContractPass,
            "_canonicalize_local_role_links_in_text": MiniappGenerationContractFrontend,
            "_route_module_needs_stub": MiniappGenerationContractRoutes,
            "_route_module_requires_db_backed_repair": MiniappGenerationContractRoutes,
            "_deterministic_client_page_route_source": MiniappGenerationContractRoutes,
            "_deterministic_specialist_page_route_source": MiniappGenerationContractRoutes,
            "_deterministic_manager_page_route_source": MiniappGenerationContractRoutes,
            "_strip_noncanonical_runtime_route_handlers": MiniappGenerationContractRoutes,
            "_normalize_runtime_route_module_source": MiniappGenerationContractRoutes,
            "_deterministic_main_runtime_source": MiniappGenerationContractRoutes,
            "_normalize_api_aliases_in_text": MiniappGenerationContractFrontend,
            "_deterministic_requests_route_source": MiniappGenerationContractRoutes,
            "_deterministic_comments_route_source": MiniappGenerationContractRoutes,
            "_deterministic_assignments_route_source": MiniappGenerationContractRoutes,
            "_deterministic_profiles_route_source": MiniappGenerationContractRoutes,
            "_deterministic_users_route_source": MiniappGenerationContractRoutes,
            "_deterministic_workload_route_source": MiniappGenerationContractRoutes,
            "_deterministic_runtime_route_source": MiniappGenerationContractRoutes,
            "_deterministic_time_slots_route_source": MiniappGenerationContractRoutes,
            "_ensure_fastapi_import_symbol": MiniappGenerationContractFrontend,
            "_inject_head_asset_link": MiniappGenerationContractFrontend,
            "_ensure_head_asset_link": MiniappGenerationContractFrontend,
            "_ensure_body_script_ref": MiniappGenerationContractFrontend,
            "_ensure_preview_bridge_ref": MiniappGenerationContractFrontend,
            "_ensure_page_shell_contract": MiniappGenerationContractFrontend,
            "_ensure_html_dom_ids_for_script": MiniappGenerationContractFrontend,
            "_static_asset_href": MiniappGenerationContractFrontend,
            "_normalize_role_local_links": MiniappGenerationContractFrontend,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_ensure_runtime_artifact_operations": "generation_contract_pass",
            "_ensure_app_level_test_operations": "generation_contract_pass",
            "_run_pre_apply_contract_pass": "generation_contract_pass",
            "_resolve_grounded_spec_for_contract_pass": "generation_contract_pass",
            "_synchronize_profile_schema_contract": "generation_contract_schema",
            "_synchronize_db_session_contract": "generation_contract_schema",
            "_synchronize_runtime_route_contract": "generation_contract_schema",
            "_synchronize_backend_dependency_contract": "generation_contract_schema",
            "_synchronize_main_runtime_contract": "generation_contract_schema",
            "_synchronize_minimal_workflow_route_contracts": "generation_contract_routes",
            "_synchronize_route_schema_contract": "generation_contract_schema",
            "_synchronize_frontend_api_contract": "generation_contract_frontend",
            "_synchronize_frontend_navigation_contract": "generation_contract_frontend",
            "_synchronize_basic_page_state_contract": "generation_contract_frontend",
            "_operation_or_workspace_content": "generation_contract_schema",
        }
