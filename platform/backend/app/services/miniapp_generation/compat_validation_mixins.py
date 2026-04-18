from __future__ import annotations

from app.modules.miniapp_validation import GenerationEditGate, GenerationPreflightValidation, PageGraphValidation


class CompatValidationMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_contains_placeholder_surface": GenerationEditGate,
            "_has_real_interactive_surface": GenerationEditGate,
            "_has_visible_loading_surface": GenerationEditGate,
            "_has_business_surface": GenerationEditGate,
            "_empty_business_container_count": GenerationEditGate,
            "_edit_gate_issues": GenerationEditGate,
            "_preflight_backend_syntax_issues": GenerationPreflightValidation,
            "_preflight_frontend_syntax_issues": GenerationPreflightValidation,
            "_preflight_profile_schema_issues": GenerationPreflightValidation,
            "_preflight_route_schema_issues": GenerationPreflightValidation,
            "_preflight_check_results": GenerationPreflightValidation,
            "_normalize_local_route_ref": GenerationPreflightValidation,
            "_page_graph_gate_issues": PageGraphValidation,
            "_build_page_graph_verification_report": PageGraphValidation,
            "_preflight_route_manifest_link_issues": GenerationPreflightValidation,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_edit_gate_issues": "generation_edit_gate",
            "_preflight_generation_issues": "generation_preflight_validation",
        }
