from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES, ROLE_ORDER
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodePlanPrompts(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _code_plan_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "flow_mode": {"type": "string", "enum": ["single_page", "multi_page"]},
                "files_to_read": {"type": "array", "items": {"type": "string"}},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "shared_files": {"type": "array", "items": {"type": "string"}},
                "backend_targets": {"type": "array", "items": {"type": "string"}},
                "page_graph": {
                    "type": "object",
                    "properties": {
                        "app_title": {"type": "string"},
                        "summary": {"type": "string"},
                        "flow_mode": {"type": "string", "enum": ["single_page", "multi_page"]},
                        "shared_files": {"type": "array", "items": {"type": "string"}},
                        "backend_targets": {"type": "array", "items": {"type": "string"}},
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string", "enum": list(ROLE_ORDER)},
                                    "entry_path": {"type": "string"},
                                    "landing_page_id": {"type": "string"},
                                    "routes_file": {"type": "string"},
                                    "pages": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "page_id": {"type": "string"},
                                                "route_path": {"type": "string"},
                                                "navigation_label": {"type": "string"},
                                                "component_name": {"type": "string"},
                                                "file_path": {"type": "string"},
                                                "title": {"type": "string"},
                                                "description": {"type": "string"},
                                                "purpose": {"type": "string"},
                                                "page_kind": {"type": "string"},
                                                "primary_actions": {"type": "array", "items": {"type": "string"}},
                                                "handoff_paths": {"type": "array", "items": {"type": "string"}},
                                                "data_dependencies": {"type": "array", "items": {"type": "string"}},
                                                "loading_state": {"type": "string"},
                                                "empty_state": {"type": "string"},
                                                "error_state": {"type": "string"},
                                            },
                                            "required": [
                                                "page_id",
                                                "route_path",
                                                "navigation_label",
                                                "component_name",
                                                "file_path",
                                                "title",
                                                "description",
                                                "purpose",
                                                "page_kind",
                                                "primary_actions",
                                                "handoff_paths",
                                                "data_dependencies",
                                                "loading_state",
                                                "empty_state",
                                                "error_state",
                                            ],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": ["role", "entry_path", "landing_page_id", "routes_file", "pages"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["app_title", "summary", "flow_mode", "roles"],
                    "additionalProperties": False,
                },
            },
            "required": ["summary", "flow_mode", "files_to_read", "target_files", "shared_files", "backend_targets", "page_graph"],
            "additionalProperties": False,
        }

    @staticmethod
    def _code_plan_system_prompt() -> str:
        prompt = (
            "Plan a real file-level multi-page mini-app. "
            "Use the role contract first, then infer the page graph, route tree, shared app files, and miniapp touch points. "
            "Do not output placeholders, metrics-only dashboards, or one-screen role wrappers. "
            "Canonical role entry pages are /client, /specialist, and /manager only; do not invent /root or /<role>/root pages."
        )
        from app.services.miniapp_generation.service import GenerationService

        GenerationService._assert_english_control_text(prompt)
        return prompt

    @staticmethod
    def _code_plan_section_system_prompt(section_title: str) -> str:
        prompt = (
            "Plan one section of a real file-level multi-page mini-app. "
            f"Return only the requested section: {section_title}. "
            "Keep it concrete, schema-valid, and consistent with the role contract."
        )
        from app.services.miniapp_generation.service import GenerationService

        GenerationService._assert_english_control_text(prompt)
        return prompt

    def _code_plan_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        return json_dumps(
            {
                "task": "Plan a route/page graph for real code generation",
                "prompt": prompt,
                "role_scope": role_scope,
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec) if compact else grounded_spec.model_dump(mode="json"),
                "role_contract": role_contract,
                "doc_refs": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in (doc_refs[:4] if compact else doc_refs)
                ],
                "workspace_tree": workspace_tree[:36] if compact else workspace_tree,
                "workspace_path_hints": self._workspace_path_hints(workspace_tree),
                "design_reference_files": list(DESIGN_REFERENCE_FILES),
                "creative_direction": creative_direction,
                "constraints": [
                    "Keep Telegram/MAX mini-app compatibility.",
                    "Keep three-role preview compatibility.",
                    "Use the existing shared shell assets as the invariant style anchor.",
                    "If the prompt implies several flows, entities, or jobs, return a multi-page app with distinct page files and routes.",
                    "Size the route tree to the actual workflow instead of forcing a fixed page count.",
                    "Do not collapse workflow-heavy apps into index.html plus profile.html only.",
                    "Return distinct role page purposes, primary actions, and handoff paths instead of mirrored copies across roles.",
                    "For role root pages with dynamic data dependencies, require real first-paint sections and actions with honest empty states instead of loading-first shells or pseudo-records.",
                    "For pages with dynamic data dependencies, plan only the minimum loading/error behavior that remains necessary after first paint; do not force dedicated loading shells on role root pages.",
                    "Do not plan generic visible state labels like 'Loading data...' or 'Unable to load data. Try again.'; state markers can be hidden/empty initially and filled with contextual copy only when needed.",
                    "Do not plan visible placeholder labels, numeric block labels, decorative chevrons, or broken entity fragments such as 'Block 181', 'Section 181', '181;', '203a', or '›'.",
                    "For pages that reference /api endpoints in data dependencies, include the matching miniapp route modules in backend_targets from the start.",
                    "If the prompt requires persistent business data or mutable records, plan miniapp/app/db.py and miniapp/app/schemas.py as canonical backend contract files from the start.",
                    "If the prompt requires persistent writes, keep mutable business data in the canonical persistence layer rather than route-level lists, dicts, or module globals.",
                    "If route modules need request/response payload models, keep them in shared schema modules instead of defining them inline.",
                    "For targeted edits, keep target_files minimal and touch only the files required by the request.",
                    "Do not output role copies with changed titles only.",
                    "Canonical role entry pages are /client, /specialist, and /manager only; do not invent /root or /<role>/root pages or static files under miniapp/app/static/<role>/root/.",
                    "Return only repo-relative file paths that fit the current workspace tree and path hints.",
                    "Do not return HTTP endpoints, route strings, or prose labels inside target_files or backend_targets.",
                    "Do not invent alternate miniapp roots such as miniapp/src when the current workspace uses another miniapp layout.",
                ],
            }
        )

    def _code_plan_section_user_prompt(
        self,
        *,
        section_id: str,
        section_title: str,
        section_contract: list[str],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        return json_dumps(
            {
                "task": "Plan one route/page graph section for real code generation",
                "section_id": section_id,
                "section_title": section_title,
                "prompt": prompt,
                "role_scope": role_scope,
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec) if compact else grounded_spec.model_dump(mode="json"),
                "role_contract": role_contract,
                "doc_refs": [
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in (doc_refs[:3] if compact else doc_refs[:5])
                ],
                "workspace_tree": workspace_tree[:28] if compact else workspace_tree[:40],
                "workspace_path_hints": self._workspace_path_hints(workspace_tree),
                "design_reference_files": list(DESIGN_REFERENCE_FILES),
                "creative_direction": creative_direction,
                "constraints": [
                    "Keep Telegram/MAX mini-app compatibility.",
                    "Keep three-role preview compatibility.",
                    "Use the existing shared shell assets as a style anchor.",
                    "For role root pages with dynamic data dependencies, require content-first first paint with real sections and honest empty states instead of loading-first shells or pseudo-records.",
                    "For pages with dynamic data dependencies, include loading_state and error_state only when they remain truly necessary after first paint.",
                    "For pages that reference /api endpoints in data dependencies, include matching backend route modules in backend_targets instead of deferring this to repair.",
                    "If the prompt requires persistent business data or mutable records, include miniapp/app/db.py and miniapp/app/schemas.py in the backend contract from the start.",
                    "When persistence is required, plan it through shared backend models and shared schema modules instead of route-level dict/list stores or inline payload classes.",
                    "Keep role page purposes, primary actions, and handoff paths distinct.",
                    "Canonical role entry pages are /client, /specialist, and /manager only; do not invent /root or /<role>/root pages or static files under miniapp/app/static/<role>/root/.",
                    "Do not output role copies with changed titles only.",
                    "Return only repo-relative file paths that fit the current workspace tree and path hints.",
                    "Do not return HTTP endpoints, route strings, or prose labels inside target_files or backend_targets.",
                ],
                "section_contract": section_contract,
            }
        )

    def _workspace_path_hints(self, workspace_tree: list[dict[str, str]]) -> dict[str, Any]:
        file_paths = [
            str(item.get("path"))
            for item in workspace_tree
            if isinstance(item, dict) and item.get("type") == "file" and isinstance(item.get("path"), str)
        ]
        backend_files = [path for path in file_paths if path.startswith("miniapp/")]
        static_files = [path for path in file_paths if path.startswith("miniapp/app/static/")]
        top_level_dirs = sorted({path.split("/", 1)[0] for path in file_paths if "/" in path})
        return {
            "top_level_dirs": top_level_dirs[:12],
            "backend_root_candidates": sorted({"/".join(path.split("/")[:2]) for path in backend_files if "/" in path})[:8],
            "frontend_root_candidates": sorted({"/".join(path.split("/")[:4]) for path in static_files if path.count("/") >= 3})[:12],
            "backend_examples": backend_files[:12],
            "frontend_examples": static_files[:16],
        }

    def _code_plan_partial_schema(self, field_names: list[str]) -> dict[str, Any]:
        full_schema = self._code_plan_schema()
        properties = full_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {name: properties[name] for name in field_names if name in properties},
            "required": [name for name in field_names if name in properties],
            "additionalProperties": False,
        }
