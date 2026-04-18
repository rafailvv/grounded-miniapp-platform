from __future__ import annotations

from typing import Any

from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationReportingCompaction(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _limit_text(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        head = max_chars // 2
        tail = max_chars - head
        return f"{text[:head]}\n/* ... truncated ... */\n{text[-tail:]}"

    def _bounded_file_contexts(
        self,
        file_contexts: dict[str, str],
        *,
        max_file_chars: int,
        max_total_chars: int,
    ) -> dict[str, str]:
        trimmed: dict[str, str] = {}
        total = 0
        for path, content in file_contexts.items():
            bounded = self._limit_text(content or "", max_file_chars)
            next_total = total + len(bounded)
            if trimmed and next_total > max_total_chars:
                break
            trimmed[path] = bounded
            total = next_total
        return trimmed

    @staticmethod
    def _compact_grounded_spec_for_codegen(grounded_spec: GroundedSpecModel) -> dict[str, Any]:
        return {
            "product_goal": grounded_spec.product_goal,
            "actors": [actor.model_dump(mode="json") for actor in grounded_spec.actors[:4]],
            "domain_entities": [entity.model_dump(mode="json") for entity in grounded_spec.domain_entities[:6]],
            "user_flows": [flow.model_dump(mode="json") for flow in grounded_spec.user_flows[:4]],
            "ui_requirements": [item.model_dump(mode="json") for item in grounded_spec.ui_requirements[:8]],
            "api_requirements": [item.model_dump(mode="json") for item in grounded_spec.api_requirements[:8]],
            "persistence_requirements": [item.model_dump(mode="json") for item in grounded_spec.persistence_requirements[:8]],
            "security_requirements": [item.model_dump(mode="json") for item in grounded_spec.security_requirements[:6]],
            "non_functional_requirements": [item.model_dump(mode="json") for item in grounded_spec.non_functional_requirements[:6]],
            "platform_constraints": [item.model_dump(mode="json") for item in grounded_spec.platform_constraints[:6]],
            "assumptions": [item.model_dump(mode="json") for item in grounded_spec.assumptions[:6]],
        }

    @staticmethod
    def _compact_role_contract_for_codegen(role_contract: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles = role_contract.get("roles") or {}
        return {
            "app_title": role_contract.get("app_title"),
            "app_summary": role_contract.get("app_summary"),
            "roles": {
                role: {
                    "responsibility": (roles.get(role) or {}).get("responsibility"),
                    "primary_jobs": list(((roles.get(role) or {}).get("primary_jobs") or [])[:4]),
                }
                for role in role_scope
                if role in roles
            },
        }

    @staticmethod
    def _compact_page_graph_for_codegen(page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles = page_graph.get("roles") or {}
        return {
            "app_title": page_graph.get("app_title"),
            "summary": page_graph.get("summary"),
            "flow_mode": page_graph.get("flow_mode"),
            "shared_files": list((page_graph.get("shared_files") or [])[:8]),
            "backend_targets": list((page_graph.get("backend_targets") or [])[:8]),
            "roles": {
                role: {
                    "routes_file": (roles.get(role) or {}).get("routes_file"),
                    "pages": [
                        {
                            "page_id": page.get("page_id"),
                            "route_path": page.get("route_path"),
                            "file_path": page.get("file_path"),
                            "page_kind": page.get("page_kind"),
                            "navigation_label": page.get("navigation_label"),
                            "title": page.get("title"),
                            "description": page.get("description"),
                            "purpose": page.get("purpose"),
                            "primary_actions": list((page.get("primary_actions") or [])[:6]),
                            "handoff_paths": list((page.get("handoff_paths") or [])[:6]),
                            "data_dependencies": list((page.get("data_dependencies") or [])[:6]),
                            "loading_state": page.get("loading_state"),
                            "empty_state": page.get("empty_state"),
                            "error_state": page.get("error_state"),
                        }
                        for page in ((roles.get(role) or {}).get("pages") or [])[:6]
                    ],
                }
                for role in role_scope
                if role in roles
            },
        }

    def _compact_file_contexts_for_repair(
        self,
        file_contexts: dict[str, str],
        *,
        max_file_chars: int,
        max_total_chars: int,
    ) -> dict[str, str]:
        return self._bounded_file_contexts(
            file_contexts,
            max_file_chars=max_file_chars,
            max_total_chars=max_total_chars,
        )
