from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodegenPrompts(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _whole_file_cluster_system_prompt(cluster_name: str) -> str:
        prompt = (
            "Generate a whole-file code bundle for a real Telegram/MAX mini-app workspace. "
            f"You own the {cluster_name} cluster only. "
            "Return complete file contents for the allowed target files. "
            "Do not emit partial patches, placeholder wrappers, or files outside the provided cluster scope."
        )
        from app.services.miniapp_generation.service import GenerationService

        GenerationService._assert_english_control_text(prompt)
        return prompt

    def _whole_file_cluster_user_prompt(
        self,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        tool_results: list[dict[str, Any]] | None = None,
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        bounded_contexts = self._bounded_file_contexts(
            {path: file_contexts.get(path, "") for path in cluster_targets},
            max_file_chars=2600 if compact else 5200,
            max_total_chars=10000 if compact else 18000,
        )
        return json_dumps(
            {
                "task": "Generate a whole-file cluster for the planned app change",
                "cluster_name": cluster_name,
                "prompt": prompt,
                "intent": intent,
                "scope_mode": scope_mode,
                "role_scope": role_scope,
                "cluster_targets": cluster_targets,
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec),
                "role_contract": self._compact_role_contract_for_codegen(role_contract, role_scope),
                "page_graph": self._compact_page_graph_for_codegen(page_graph, role_scope),
                "file_contexts": bounded_contexts,
                "tool_results": list(tool_results or []),
                "creative_direction": creative_direction,
                "rules": [
                    "If context is insufficient, return outcome=tool_request with tool_requests and no operations.",
                    "You may use list_files, read_files, search_files, run_command, and run_checks before editing.",
                    "Return only create/replace operations for files listed in cluster_targets.",
                    "Every returned file must contain the complete final file body.",
                    "Prefer larger coherent role/domain files over micro-modules.",
                    "Do not create entities, features, widgets, shared, domain, infrastructure, or api sub-architectures.",
                    "Stay inside the canonical roots miniapp/app, miniapp/app/routes, miniapp/app/static, and miniapp/app/generated.",
                    "All generated Python backend files must stay on the FastAPI stack. Route modules under miniapp/app/routes must use APIRouter with a top-level variable named router; never Flask, Blueprint, current_app, or send_from_directory.",
                    "Use English-only control text and code comments.",
                    "For every HTML page in cluster_targets, make it reference its own page-local styles.css and app.js companions when those files are also in cluster_targets.",
                    "For role root pages with data_dependencies, render a complete business surface on first paint with real sections, actions, and honest empty states instead of loading-first shells.",
                    "Do not force dedicated loading or error containers on role root pages; add state UI only when a real post-render fetch path needs it, and never as the primary visible surface.",
                    "Do not render manual Refresh buttons, pseudo-data rows, or loading-only placeholder shells as the primary surface.",
                    "Do not introduce flat role entry files like miniapp/app/static/client/index.html unless that exact path is listed in cluster_targets.",
                    "Use the template runtime conventions for API access instead of raw authless fetch patterns.",
                    "Do not invent auth/login/me endpoints, auth bootstrap modules, or /api/auth references in generated app code.",
                    "Preserve the template profile flow: root role pages may link to /<role>/profile, and profile persistence stays compatible with routes/profiles.py plus RoleProfileRecord in db.py.",
                ],
            }
        )

    @staticmethod
    def _page_edit_system_prompt() -> str:
        prompt = (
            "Generate one real React page file for a Telegram/MAX mini-app workspace. "
            "The page must feel custom, role-specific, and grounded in the existing profile design language. "
            "Return file operations for the requested page file only."
        )
        from app.services.miniapp_generation.service import GenerationService

        GenerationService._assert_english_control_text(prompt)
        return prompt

    def _page_edit_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role: str,
        page: dict[str, Any],
        page_graph: dict[str, Any],
        role_contract: dict[str, Any],
        scope_mode: str,
        intent: str,
        file_contexts: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        tool_results: list[dict[str, Any]] | None = None,
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        sibling_pages = [
            item
            for item in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if item.get("file_path") != page.get("file_path")
        ]
        design_reference_files = self._bounded_file_contexts(
            {path: file_contexts.get(path, "") for path in DESIGN_REFERENCE_FILES if path in file_contexts},
            max_file_chars=2400 if compact else 4000,
            max_total_chars=7200 if compact else 12000,
        )
        return json_dumps(
            {
                "task": "Generate one page file",
                "prompt": prompt,
                "intent": intent,
                "scope_mode": scope_mode,
                "role": role,
                "page": page,
                "sibling_pages": sibling_pages[:2] if compact else sibling_pages[:4],
                "role_contract": role_contract.get("roles", {}).get(role),
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec),
                "shared_contract": {
                    "preferred_imports": [
                        "@/lib/api/httpClient",
                        "@/lib/profile/ProfileCabinetCard",
                        "@/lib/profile/profileStore",
                    ],
                    "design_reference_files": design_reference_files,
                },
                "state_contract": {
                    "data_dependencies": list(page.get("data_dependencies") or []),
                    "loading_state": page.get("loading_state"),
                    "empty_state": page.get("empty_state"),
                    "error_state": page.get("error_state"),
                },
                "first_paint_contract": self._first_paint_contract_for_page(role=role, page=page, grounded_spec=grounded_spec),
                "current_file": self._limit_text(file_contexts.get(page["file_path"], ""), 6000 if compact else 12000),
                "tool_results": list(tool_results or []),
                "creative_direction": creative_direction,
                "rules": [
                    "If context is insufficient, return outcome=tool_request with tool_requests and no operations.",
                    "You may use list_files, read_files, search_files, run_command, and run_checks before editing.",
                    "Create a real page, not a generic stats card screen.",
                    "Respect the requested role and make the actions specific to that role.",
                    "Honor the page purpose, primary actions, and handoff_paths from the page graph.",
                    "Only add loading, empty, and error states when the page depends on data fetched after page load.",
                    "If the page has data_dependencies, add only the minimum state UI needed for real post-render refresh paths.",
                    "Use loading/error ids or data-ui-state markers that match the page contract instead of decorative prose only.",
                    "Static or mostly static pages should render immediately without spinner-first UX.",
                    "Role root pages with data dependencies must render a complete business surface on first paint without pseudo-data: use real sections, actions, and honest empty states immediately.",
                    "Keep dashboard pages complete on first paint and move business flows into separate workbench/workspace pages when the page graph requires them.",
                    "Do not rely on dedicated loading or error blocks to make role root pages function; the main surface must already be complete on first render.",
                    "If scope_mode is minimal_patch, preserve unrelated behavior and keep the diff minimal.",
                    "Return exactly one operation for the requested page file path.",
                ],
            }
        )

    @staticmethod
    def _composition_system_prompt(stage_name: str) -> str:
        if stage_name == "miniapp":
            prompt = (
                "Compose the miniapp/runtime pieces after planning. "
                "Generate only miniapp, API, schema, store, or contract files needed by the requested change. "
                "Do not rewrite frontend page files. "
                "Return executable file operations, not an implementation plan."
            )
        else:
            prompt = (
                "Compose the shared UI/runtime pieces after the individual pages and miniapp are written. "
                "Generate static HTML/CSS/JS files and miniapp-served glue that connect the generated pages to the miniapp. "
                "Do not rewrite page files unless they are explicitly targeted. "
                "Return executable file operations, not an implementation plan."
            )
        from app.services.miniapp_generation.service import GenerationService

        GenerationService._assert_english_control_text(prompt)
        return prompt

    def _composition_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        scope_mode: str,
        intent: str,
        stage_name: str,
        target_files: list[str],
        file_contexts: dict[str, str],
        generated_page_sources: dict[str, str],
        generated_support_sources: dict[str, str],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        tool_results: list[dict[str, Any]] | None = None,
    ) -> str:
        compact = generation_mode == GenerationMode.FAST
        trimmed_targets = self._bounded_file_contexts(
            {path: file_contexts.get(path, "") for path in target_files},
            max_file_chars=3000 if compact else 5200,
            max_total_chars=9000 if compact else 16000,
        )
        return json_dumps(
            {
                "task": f"Compose {stage_name} files for the planned app change",
                "prompt": prompt,
                "intent": intent,
                "stage_name": stage_name,
                "scope_mode": scope_mode,
                "role_scope": role_scope,
                "role_contract": self._compact_role_contract_for_codegen(role_contract, role_scope),
                "page_graph": self._compact_page_graph_for_codegen(page_graph, role_scope),
                "stateful_pages": self._stateful_page_contracts(page_graph, role_scope),
                "expected_backend_targets": list((page_graph.get("backend_targets") or [])[:12]),
                "target_files": target_files,
                "workspace_path_hints": {
                    "target_roots": sorted({path.split("/", 1)[0] for path in target_files if "/" in path}),
                    "target_examples": target_files[:12],
                    "existing_file_context_paths": sorted(file_contexts.keys())[:12],
                },
                "grounded_spec": self._compact_grounded_spec_for_codegen(grounded_spec),
                "file_contexts": trimmed_targets,
                "generated_page_sources": self._bounded_file_contexts(
                    generated_page_sources,
                    max_file_chars=2200 if compact else 3600,
                    max_total_chars=5600 if compact else 11000,
                ),
                "generated_support_sources": self._bounded_file_contexts(
                    generated_support_sources,
                    max_file_chars=1600 if compact else 2800,
                    max_total_chars=3600 if compact else 7200,
                ),
                "tool_results": list(tool_results or []),
                "first_paint_contracts": {
                    role: [
                        self._first_paint_contract_for_page(role=role, page=page, grounded_spec=grounded_spec)
                        for page in ((page_graph.get("roles", {}).get(role, {}) or {}).get("pages") or [])
                        if isinstance(page, dict) and self._is_role_root_page(role, page)
                    ]
                    for role in role_scope
                },
                "creative_direction": creative_direction,
                "rules": [
                    "If context is insufficient, return outcome=tool_request with tool_requests and no operations.",
                    "You may use list_files, read_files, search_files, run_command, and run_checks before editing.",
                    "Only touch files listed in target_files.",
                    "If stage_name is miniapp, generate only miniapp/server/shared contract files required by the request.",
                    "If stage_name is frontend, wire pages, routes, and shared UI/state to the already planned miniapp surface.",
                    "Treat static/shared/common.js and shared/base.css as first-class shared shell assets when they are in target_files.",
                    "For any workflow or stateful backend, use miniapp/app/db.py plus miniapp/app/schemas.py as the canonical persistence contract.",
                    "Persist mutable business entities through SQLAlchemy models and sessions from db.py; do not keep route-level dict/list stores for app data.",
                    "Define request/response models in schemas.py and import them from route modules instead of declaring inline Pydantic BaseModel classes inside routes.",
                    "When planned pages have dynamic dependencies, generate only the minimum state wiring needed for real refresh paths instead of defaulting to loading-first shells.",
                    "For role root pages, the first visible surface must already be a usable business page with real sections and honest empty states.",
                    "When generated sources reference /api endpoints, include the matching miniapp route modules and router wiring from the first draft whenever those files are in target_files.",
                    "Generate role routes that expose the page graph as real separate pages when routes are targeted.",
                    "Generate shared app chrome/state files that support the pages instead of rendering placeholder dashboards.",
                    "Keep role pages usable without waiting on client-side hydration for basic navigation and structure.",
                    "Do not fill role root pages with pseudo-records or invented live metrics just to avoid an empty screen.",
                    "Do not collapse business flows back into index.html when workbench/workspace pages are in target_files or page_graph.",
                    "For minimal_patch, preserve unrelated behavior and keep the diff minimal.",
                    "Do not touch page files unless they are included in target_files.",
                    "If target_files is non-empty, operations must include at least one create/replace/delete for one of those files.",
                    "Do not return a prose plan, checklist, or explanation instead of file operations.",
                    "Do not leave operations empty when target_files is non-empty.",
                    "assistant_message must briefly summarize the patch that was generated, not propose future work.",
                    "Do not invent files under alternate architecture roots that are absent from target_files.",
                    "Every planned page must materialize as a page triplet: one HTML, one CSS, and one JS file that share the same stem.",
                    "Do not collapse multiple pages into shared role-level app.js/styles.css files.",
                    "Do not leave page behavior or styling inline when page-level JS/CSS targets exist.",
                    "For frontend calls to /api/... use window.miniappApiFetch(...) from /static/preview_bridge.js instead of raw fetch(...).",
                    "For backend dependencies use Depends(get_actor_context), never Depends(lambda: get_actor_context()).",
                    "When linking to detail routes from JS, keep route templates aligned with route_manifest paths and use dynamic placeholders consistently.",
                    "Do not render manual Refresh buttons or links; rely on normal loading states and existing actions.",
                    "Do not pass typing.Literal aliases directly to sqlalchemy.Enum; keep persisted status fields as string-backed columns unless a real Python Enum class is defined.",
                    "If the workspace uses miniapp/app, do not switch to miniapp/src. If the workspace uses miniapp/app/static, do not switch to a separate frontend application unless those exact files are in target_files.",
                    "Do not generate auth/login/me endpoints, auth bootstrap modules, or /api/auth references for the generated app.",
                    "Keep the template profile contract intact: routes/profiles.py stays supported, db.py keeps RoleProfileRecord, and route manifests must retain each role's /profile page.",
                ],
            }
        )
