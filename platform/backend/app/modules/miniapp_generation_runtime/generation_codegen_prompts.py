from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodegenPrompts(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _prioritized_supporting_contexts(
        *,
        cluster_name: str,
        cluster_targets: list[str],
        file_contexts: dict[str, str],
    ) -> dict[str, str]:
        supporting_paths = [
            path
            for path, content in file_contexts.items()
            if path not in cluster_targets and str(content or "").strip()
        ]
        if cluster_name == "backend_support":
            preferred_paths = [
                "miniapp/app/generated/route_manifest.json",
                "miniapp/app/routes/runtime.py",
                "miniapp/app/routes/client.py",
                "miniapp/app/routes/specialist.py",
                "miniapp/app/routes/manager.py",
            ]
        elif cluster_name.startswith("backend_route_"):
            preferred_paths = [
                "miniapp/app/main.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/routes/profiles.py",
                "miniapp/app/generated/runtime_manifest.json",
                "miniapp/app/generated/route_manifest.json",
                "miniapp/app/routes/runtime.py",
                "miniapp/app/routes/client.py",
                "miniapp/app/routes/specialist.py",
                "miniapp/app/routes/manager.py",
            ]
        elif "_ui_root" in cluster_name:
            role_match = cluster_name.split("_")
            role = role_match[1] if len(role_match) > 2 else ""
            preferred_paths = [
                "miniapp/app/static/shared/base.css",
                "miniapp/app/static/shared/common.js",
                "miniapp/app/static/preview_bridge.js",
                "miniapp/app/routes/profiles.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/routes/bookingrequests.py",
                "miniapp/app/generated/runtime_manifest.json",
                "miniapp/app/generated/route_manifest.json",
            ]
            if role in {"client", "specialist", "manager"}:
                preferred_paths.extend(
                    [
                        f"miniapp/app/static/{role}/profile/index.html",
                        f"miniapp/app/static/{role}/profile/styles.css",
                        f"miniapp/app/static/{role}/profile/app.js",
                    ]
                )
        elif "_ui_bookingrequests" in cluster_name:
            role_match = cluster_name.split("_")
            role = role_match[1] if len(role_match) > 2 else ""
            preferred_paths = [
                "miniapp/app/static/shared/base.css",
                "miniapp/app/static/shared/common.js",
                "miniapp/app/static/preview_bridge.js",
                "miniapp/app/routes/bookingrequests.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/generated/runtime_manifest.json",
                "miniapp/app/generated/route_manifest.json",
            ]
            if role in {"client", "specialist", "manager"}:
                preferred_paths.extend(
                    [
                        f"miniapp/app/static/{role}/index.html",
                        f"miniapp/app/static/{role}/styles.css",
                        f"miniapp/app/static/{role}/app.js",
                        f"miniapp/app/static/{role}/profile/index.html",
                        f"miniapp/app/static/{role}/profile/styles.css",
                        f"miniapp/app/static/{role}/profile/app.js",
                    ]
                )
        else:
            preferred_paths = []
        ordered_paths = [path for path in preferred_paths if path in supporting_paths]
        ordered_paths.extend(path for path in supporting_paths if path not in ordered_paths)
        return {path: file_contexts.get(path, "") for path in ordered_paths}

    @staticmethod
    def _whole_file_cluster_system_prompt(cluster_name: str) -> str:
        prompt = (
            "Generate a whole-file code bundle for a real Telegram/MAX mini-app workspace. "
            f"You own the {cluster_name} cluster only. "
            "Return complete file contents for the allowed target files. "
            "Do not emit partial patches, placeholder wrappers, or files outside the provided cluster scope."
        )
        if cluster_name == "backend_support":
            prompt = (
                f"{prompt.rstrip()}\n\n"
                "For backend_support specifically, treat role route files and route manifests as reference-only supporting context. "
                "They are not a prerequisite for emitting backend_support operations. "
                "If supporting route files are missing, continue anyway and generate the allowed backend_support targets from the product prompt, db contract, schemas contract, and template profile/runtime conventions."
            ).strip()
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
        supporting_contexts = self._bounded_file_contexts(
            self._prioritized_supporting_contexts(
                cluster_name=cluster_name,
                cluster_targets=cluster_targets,
                file_contexts=file_contexts,
            ),
            max_file_chars=2400 if compact else 4200,
            max_total_chars=7000 if compact else 16000,
        )
        cluster_specific_rules: list[str] = []
        if cluster_name == "backend_support":
            cluster_specific_rules = [
                "For backend_support, do not block on route module or route_manifest reads before writing operations.",
                "If supporting route files are shown as FILE_MISSING or only as references, proceed anyway and generate the allowed backend_support targets.",
                "Use supporting route/module context only to stay compatible with naming and runtime conventions; do not treat it as a prerequisite for emitting operations.",
                "For backend_support, app.db owns SQLAlchemy models, app.schemas owns request/response schemas, and app.routes.profiles owns profile persistence.",
                "Keep main.py limited to FastAPI app bootstrap, middleware, create-all startup, and router inclusion. Do not implement booking request CRUD or page-serving route handlers in main.py.",
                "Do not define BookingRequestRecord, role-specific route handlers, or inline Pydantic request/response models in main.py.",
            ]
        elif cluster_name.startswith("backend_route_"):
            cluster_specific_rules = [
                "For backend_route clusters, treat db.py, schemas.py, main.py, and runtime manifests as the primary supporting context.",
                "Do not return no_progress just because other role route files are thin, missing, or only present as compatibility references.",
                "If db.py and schemas.py are present in supporting_file_contexts, use them directly and emit the route module for the current cluster target.",
            ]
            if cluster_name in {"backend_route_client", "backend_route_specialist", "backend_route_manager"}:
                cluster_specific_rules.extend(
                    [
                        "Role route modules client.py, specialist.py, and manager.py are page-serving routers only.",
                        "Keep these modules limited to APIRouter + FileResponse handlers that serve existing role HTML pages.",
                        "Do not implement booking request CRUD, do not declare BookingRequestRecord, and do not define route-local Pydantic schemas in role route modules.",
                        "Do not import BookingRequestRecord or request/response schemas from peer role route modules. Shared persistence belongs only to app.db and app.schemas.",
                    ]
                )
            if cluster_name == "backend_route_bookingrequests":
                cluster_specific_rules.extend(
                    [
                        "For backend_route_bookingrequests, the booking request SQLAlchemy model already belongs in app.db and request/response schemas already belong in app.schemas.",
                        "Import BookingRequestRecord from app.db and booking request schema types from app.schemas instead of defining inline ORM or Pydantic models in the route module.",
                        "Do not declare Base subclasses, mapped_column fields, or route-local request/response models in bookingrequests.py.",
                        "Use only the canonical booking request statuses from app.schemas: submitted, in_review, issued, returned, cancelled.",
                        "Do not emit pending, pending_review, approved, or canceled in booking request API responses or defaults.",
                    ]
                )
            if cluster_name == "backend_route_runtime":
                cluster_specific_rules.extend(
                    [
                        "For backend_route_runtime, keep the module limited to runtime/init-data validation, lightweight runtime helpers, and compatibility endpoints only.",
                        "Do not implement booking request CRUD, do not define BookingRequestRecord, and do not duplicate persistence or request/response schemas already owned by app.db and app.schemas.",
                        "If the workflow needs /api/bookingrequests, that ownership belongs to bookingrequests.py, not runtime.py.",
                    ]
                )
            if cluster_name == "backend_route_runtime_manifest":
                cluster_specific_rules.extend(
                    [
                        "For backend_route_runtime_manifest, keep the module limited to role-aware manifest JSON for runtime navigation.",
                        "Do not duplicate booking request CRUD, profile persistence, or init-data verification logic that belongs in other runtime modules.",
                    ]
                )
        elif "_ui_bookingrequests" in cluster_name:
            cluster_specific_rules = [
                "For role bookingrequests UI clusters, use the current role root page, shared shell assets, and backend API routes as the primary references.",
                "The canonical runtime bridge is /static/preview_bridge.js. It provides window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role).",
                "If supporting_file_contexts already include preview_bridge.js or existing role pages that call /api/... directly, use those conventions as-is and do not search for static/shared/runtime.js or static/shared/api.js.",
                "Do not block on peer-role bookingrequests pages, shared runtime helper files, or any supporting file shown as FILE_MISSING.",
                "If peer-role bookingrequests pages are unavailable, continue anyway and generate a complete role-specific bookingrequests surface from the prompt, same-role root page, and live backend API contract.",
                "If same-role root files are present in supporting_file_contexts, use them directly as the style and navigation anchor instead of returning no_progress.",
                "Do not return no_progress only because same-role root files were previously generated in this run; consume the available supporting_file_contexts and emit the bookingrequests page operations now.",
            ]
        elif "_ui_profile" in cluster_name:
            cluster_specific_rules = [
                "For role profile UI clusters, use the same-role root page, routes/profiles.py, db.py, and the shared shell assets as the primary references.",
                "The canonical runtime bridge is /static/preview_bridge.js. It provides window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role).",
                "If supporting_file_contexts already include preview_bridge.js or existing role pages that call /api/... directly, use those conventions as-is and do not search for static/shared/runtime.js or static/shared/api.js.",
                "Do not block on static/shared/common.js, peer-role profile pages, or any supporting file shown as FILE_MISSING.",
                "If a shared helper file is unavailable, continue anyway and generate the complete role-specific profile surface from the template profile contract, preview bridge conventions, and the canonical /api/profiles/{role} read/write lifecycle.",
                "Do not return no_progress only because a shared helper or optional support file is absent from the current context bundle.",
                "For profile writes, prefer direct window.miniappApiFetch('/api/profiles/{role}', { method: 'PUT' | 'PATCH', ... }, role) calls or an alias that still visibly targets /api/... with a write method.",
            ]
        elif "_ui_root" in cluster_name:
            cluster_specific_rules = [
                "For role root UI clusters, treat the same-role profile files, shared shell assets, db.py, schemas.py, and the canonical bookingrequests route as the primary supporting context.",
                "The canonical runtime bridge is /static/preview_bridge.js. It provides window.setupPreviewBridge(role) and window.miniappApiFetch(input, init, role).",
                "If preview_bridge.js is already present in supporting_file_contexts, treat that as sufficient runtime/API convention context for the current cluster.",
                "If supporting examples call /api/... directly with same-origin fetch, that is also acceptable for the current cluster. Do not search for static/shared/runtime.js or static/shared/api.js.",
                "Do not block on sibling feature pages, peer-role pages, static/shared/runtime.js, static/shared/api.js, or any supporting file shown as FILE_MISSING.",
                "Do not request peer-role feature pages or sibling pages that belong to other generation clusters in the same batch.",
                "If a same-role feature page is not already present in supporting_file_contexts, continue anyway and generate the role root/dashboard directly from the product prompt, backend API contract, and shared shell conventions.",
                "The role root cluster must be self-sufficient: return operations for its own root targets without waiting for sibling cluster outputs.",
            ]
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
                "supporting_file_contexts": supporting_contexts,
                "tool_results": list(tool_results or []),
                "creative_direction": creative_direction,
                "rules": [
                    "If context is insufficient, return outcome=tool_request with tool_requests and no operations.",
                    "You may use list_files, read_files, search_files, run_command, and run_checks before editing.",
                    "When shared CRUD wiring, route registration, or DB persistence is uncertain, inspect first and prefer run_checks with mode=exact before returning operations.",
                    "Before concluding a cluster that touches shared CRUD, backend runtime wiring, or generated verification files, prefer run_checks with mode=final unless recent tool_results already prove the contract is green.",
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
                    "Use supporting_file_contexts as read-only references for routing, manifest, or shared runtime wiring before asking to read the same files again.",
                    "Do not invent auth/login/me endpoints, auth bootstrap modules, or /api/auth references in generated app code.",
                    "Preserve the template profile flow: root role pages may link to /<role>/profile, and profile persistence stays compatible with routes/profiles.py plus RoleProfileRecord in db.py.",
                    "For every workflow app, implement one real shared persisted entity lifecycle: client creates it, specialist reads and updates the same record, and manager observes the same DB-backed state.",
                    "Do not ship form UI, list UI, or role dashboards unless the corresponding /api read and write paths already exist in the same draft.",
                ]
                + cluster_specific_rules,
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
        page_kind = str(page.get("page_kind") or "").strip().lower()
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
        profile_specific_rules: list[str] = []
        if page_kind == "profile":
            profile_specific_rules = [
                "For profile pages, treat routes/profiles.py, RoleProfileRecord in db.py, and the template shell contract as the primary references.",
                "Do not block on static/shared/common.js, optional helper modules, or peer-role profile pages when they are absent or shown as FILE_MISSING.",
                "If a shared helper is unavailable, continue using the canonical /api/profiles/{role} contract, window.miniappApiFetch(...), and the existing page-shell conventions.",
                "Do not return outcome=no_progress only because an optional shared helper file is unavailable.",
            ]
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
                    "When page-level persistence, route wiring, or shared record lifecycle is uncertain, inspect first and prefer run_checks with mode=exact before returning operations.",
                    "Create a real page, not a generic stats card screen.",
                    "Respect the requested role and make the actions specific to that role.",
                    "Treat page purpose, primary actions, and handoff paths from the page graph as advisory hints, not as rigid structure.",
                    "Only add loading, empty, and error states when the page depends on data fetched after page load.",
                    "If the page has data_dependencies, add only the minimum state UI needed for real post-render refresh paths.",
                    "Use loading/error ids or data-ui-state markers that match the page contract instead of decorative prose only.",
                    "Static or mostly static pages should render immediately without spinner-first UX.",
                    "Role root pages with data dependencies must render a complete business surface on first paint without pseudo-data: use real sections, actions, and honest empty states immediately.",
                    "Keep role root pages complete on first paint and preserve any explicit nested feature routes that are already present in the writable surface.",
                    "Do not rely on dedicated loading or error blocks to make role root pages function; the main surface must already be complete on first render.",
                    "If the page edits or lists shared records, wire it to the real DB-backed /api lifecycle for the same entity instead of local arrays or console-only handlers.",
                    "Profile pages must keep the canonical /api/profiles/{role} read/write contract or the existing profileStore contract intact.",
                    "For pages that create, list, or update shared records, prefer honest CRUD wiring over speculative UI polish and do not conclude without evidence that the corresponding API contract exists in the same draft.",
                    "If scope_mode is minimal_patch, preserve unrelated behavior and keep the diff minimal.",
                    "Return exactly one operation for the requested page file path.",
                ]
                + profile_specific_rules,
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
                    "When shared CRUD, route registration, or runtime wiring is uncertain, inspect first and prefer run_checks with mode=exact before returning operations.",
                    "Before concluding a backend/runtime composition that touches shared CRUD or generated verification surfaces, prefer run_checks with mode=final unless recent tool_results already show a green result.",
                    "Only touch files listed in target_files.",
                    "If stage_name is miniapp, generate only miniapp/server/shared contract files required by the request.",
                    "If stage_name is frontend, wire pages, routes, and shared UI/state to the already planned miniapp surface.",
                    "Treat static/shared/common.js and shared/base.css as first-class shared shell assets when they are in target_files.",
                    "For any workflow or stateful backend, use miniapp/app/db.py plus miniapp/app/schemas.py as the canonical persistence contract.",
                    "Persist mutable business entities through SQLAlchemy models and sessions from db.py; do not keep route-level dict/list stores for app data.",
                    "Define request/response models in schemas.py and import them from route modules instead of declaring inline Pydantic BaseModel classes inside routes.",
                    "If dedicated backend_route_* targets exist, keep main.py focused on app bootstrap and router wiring; do not duplicate domain CRUD handlers inside main.py.",
                    "Route modules must import shared ORM/session objects from app.db and request/response models from app.schemas; do not redeclare BookingRequestRecord or inline BookingRequest BaseModel classes inside miniapp/app/routes/*.py.",
                    "When planned pages have dynamic dependencies, generate only the minimum state wiring needed for real refresh paths instead of defaulting to loading-first shells.",
                    "For role root pages, the first visible surface must already be a usable business page with real sections and honest empty states.",
                    "When generated sources reference /api endpoints, include the matching miniapp route modules and router wiring from the first draft whenever those files are in target_files.",
                    "Generate role routes that expose the page graph as real separate pages when routes are targeted.",
                    "Generate shared app chrome/state files that support the pages instead of rendering placeholder dashboards.",
                    "Keep role pages usable without waiting on client-side hydration for basic navigation and structure.",
                    "Do not fill role root pages with pseudo-records or invented live metrics just to avoid an empty screen.",
                    "Do not collapse explicit nested feature routes back into index.html when those routes are part of the writable surface.",
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
                    "Every generated page must keep <main class=\"page-shell\"> with style=\"padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));\" so the shared shell safe-area contract is explicit in the page HTML.",
                    "For backend dependencies use Depends(get_actor_context), never Depends(lambda: get_actor_context()).",
                    "When linking to detail routes from JS, keep route templates aligned with route_manifest paths and use dynamic placeholders consistently.",
                    "Do not render manual Refresh buttons or links; rely on normal loading states and existing actions.",
                    "Do not pass typing.Literal aliases directly to sqlalchemy.Enum; keep persisted status fields as string-backed columns unless a real Python Enum class is defined.",
                    "If the workspace uses miniapp/app, do not switch to miniapp/src. If the workspace uses miniapp/app/static, do not switch to a separate frontend application unless those exact files are in target_files.",
                    "Do not generate auth/login/me endpoints, auth bootstrap modules, or /api/auth references for the generated app.",
                    "Keep the template profile contract intact: routes/profiles.py stays supported, db.py keeps RoleProfileRecord, and route manifests must retain each role's /profile page.",
                    "Treat shared workflow persistence as mandatory: frontend pages, route modules, schemas.py, and db.py must agree on one create/list/update lifecycle for the same entity across client, specialist, and manager flows.",
                    "Do not leave fake save handlers, console-only submit handlers, or hardcoded live collections in the generated app.",
                ],
            }
        )
