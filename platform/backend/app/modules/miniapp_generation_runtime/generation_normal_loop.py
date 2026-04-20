from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from app.models.domain import ChatTurnRecord, ValidationSnapshot
from app.models.domain import DraftFileOperation, JobRecord
from app.models.common import GenerationMode
from app.services.check_runner import CheckRunner
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner
from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync


class MiniappGenerationNormalLoop(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _normalize_future_annotations_import(py_source: str) -> str:
        return MiniappRuntimeContractSync.normalize_future_annotations_import(py_source)

    @staticmethod
    def _file_has_python_syntax_error(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError:
            return True
        except OSError:
            return False
        return False

    @staticmethod
    def _imported_schema_names(route_source: str) -> set[str]:
        match = re.search(r"from\s+app\.schemas\s+import\s+([^\n]+)", route_source)
        if not match:
            return set()
        raw_names = match.group(1)
        return {
            token.strip()
            for token in raw_names.split(",")
            if token.strip() and token.strip() != "*"
        }

    @staticmethod
    def _defined_schema_names(schema_source: str) -> set[str]:
        return set(re.findall(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|:)", schema_source, flags=re.MULTILINE))

    @staticmethod
    def _class_field_names(schema_source: str, class_name: str) -> set[str]:
        try:
            module = ast.parse(schema_source)
        except SyntaxError:
            return set()
        for node in module.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return {
                    item.target.id
                    for item in node.body
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                }
        return set()

    @staticmethod
    def _normalized_attr_reads(route_source: str) -> set[str]:
        return set(re.findall(r"\bnormalized\.([A-Za-z_][A-Za-z0-9_]*)\b", route_source))

    @staticmethod
    def _html_dom_ids(html_source: str) -> set[str]:
        return set(re.findall(r'id=["\']([A-Za-z0-9_-]+)["\']', html_source))

    @staticmethod
    def _js_required_dom_ids(js_source: str) -> set[str]:
        dom_ids = set(
            re.findall(
                r'querySelector(?:All)?\(\s*["\']#([A-Za-z0-9_-]+)',
                js_source,
            )
        )
        dom_ids.update(
            re.findall(
                r'getElementById\(\s*["\']([A-Za-z0-9_-]+)',
                js_source,
            )
        )
        return dom_ids

    @staticmethod
    def _missing_required_imports(py_source: str) -> set[str]:
        missing: set[str] = set()
        if re.search(r"\bre\.", py_source) and not re.search(r"^\s*(?:import|from)\s+re\b", py_source, flags=re.MULTILINE):
            missing.add("re")
        return missing

    @staticmethod
    def _has_invalid_datetime_timezone_call(py_source: str) -> bool:
        return bool(
            re.search(
                r"DateTime\s*\(\s*timezone\s*\(\s*(?:True|False)\s*\)\s*\)",
                py_source,
            )
        )

    @staticmethod
    def _normalize_sqlalchemy_default_factory(py_source: str) -> str:
        updated = str(py_source or "")
        if "mapped_column" not in updated or "default_factory=" not in updated:
            return updated
        return updated.replace("default_factory=", "default=")

    @staticmethod
    def _mapped_class_field_names(py_source: str, class_name: str) -> set[str]:
        return MiniappGenerationNormalLoop._class_field_names(py_source, class_name)

    @staticmethod
    def _record_constructor_kwargs(route_source: str, class_name: str) -> set[str]:
        kwargs: set[str] = set()
        for match in re.finditer(
            rf"{re.escape(class_name)}\((?P<body>[\s\S]*?)\n\s*\)",
            route_source,
        ):
            kwargs.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", match.group("body") or ""))
        return kwargs

    @staticmethod
    def _record_class_attr_reads(route_source: str, class_name: str) -> set[str]:
        return set(
            re.findall(
                rf"\b{re.escape(class_name)}\.([A-Za-z_][A-Za-z0-9_]*)\b",
                route_source,
            )
        )

    @staticmethod
    def _contains_manual_refresh_action(*, html_source: str, js_source: str) -> bool:
        if re.search(r">\s*Refresh\s*<", html_source):
            return True
        if re.search(r'createButton\(\s*["\']Refresh["\']', js_source):
            return True
        if re.search(r'getElementById\(\s*["\']refresh-action["\']', js_source):
            return True
        return False

    def stabilize_draft_contract_from_source(
        self,
        *,
        workspace_id: str,
        draft_source: Path,
    ) -> list[str]:
        service = self.service
        source_root = service.workspace_service.source_dir(workspace_id)
        changed_files: set[str] = set()
        app_root = draft_source / "miniapp/app"
        if app_root.exists():
            for draft_py_path in app_root.rglob("*.py"):
                try:
                    draft_py_source = draft_py_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                normalized_future = self._normalize_future_annotations_import(draft_py_source)
                if normalized_future == draft_py_source:
                    continue
                draft_py_path.write_text(normalized_future, encoding="utf-8")
                changed_files.add(draft_py_path.relative_to(draft_source).as_posix())
        runtime_rel_paths = (
            "miniapp/app/main.py",
            "miniapp/app/routes/runtime.py",
        )
        for rel_path in runtime_rel_paths:
            draft_path = draft_source / rel_path
            source_path = source_root / rel_path
            if not source_path.exists():
                continue
            try:
                draft_runtime_source = draft_path.read_text(encoding="utf-8") if draft_path.exists() else ""
                source_runtime_source = source_path.read_text(encoding="utf-8")
            except OSError:
                continue
            should_restore_runtime = False
            if self._file_has_python_syntax_error(draft_path) and not self._file_has_python_syntax_error(source_path):
                should_restore_runtime = True
            elif self._missing_required_imports(draft_runtime_source) and not self._missing_required_imports(source_runtime_source):
                should_restore_runtime = True
            if should_restore_runtime:
                draft_path.parent.mkdir(parents=True, exist_ok=True)
                draft_path.write_text(source_runtime_source, encoding="utf-8")
                changed_files.add(rel_path)

        draft_schemas_path = draft_source / "miniapp/app/schemas.py"
        source_schemas_path = source_root / "miniapp/app/schemas.py"
        if not (draft_schemas_path.exists() and source_schemas_path.exists()):
            return sorted(changed_files)
        try:
            draft_schema_source = draft_schemas_path.read_text(encoding="utf-8")
            source_schema_source = source_schemas_path.read_text(encoding="utf-8")
        except OSError:
            return
        draft_route_root = draft_source / "miniapp/app/routes"
        source_route_root = source_root / "miniapp/app/routes"
        feature_route_excluded = {
            "__init__",
            "client",
            "specialist",
            "manager",
            "profiles",
            "runtime",
            "users",
            "workload",
            "time_slots",
            "comments",
            "assignments",
            "role_pages",
            "health",
        }
        if draft_route_root.exists() and source_route_root.exists():
            for source_route_path in sorted(source_route_root.glob("*.py")):
                if source_route_path.stem in feature_route_excluded:
                    continue
                draft_route_path = draft_route_root / source_route_path.name
                if not draft_route_path.exists():
                    continue
                try:
                    route_source = draft_route_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                imported_names = self._imported_schema_names(route_source)
                if not imported_names:
                    continue
                missing_in_draft = imported_names - self._defined_schema_names(draft_schema_source)
                if missing_in_draft and missing_in_draft.issubset(self._defined_schema_names(source_schema_source)):
                    draft_schemas_path.write_text(source_schema_source, encoding="utf-8")
                    changed_files.add("miniapp/app/schemas.py")
                    break
        draft_db_path = draft_source / "miniapp/app/db.py"
        source_db_path = source_root / "miniapp/app/db.py"
        if draft_db_path.exists() and source_db_path.exists():
            try:
                draft_db_source = draft_db_path.read_text(encoding="utf-8")
                source_db_source = source_db_path.read_text(encoding="utf-8")
            except OSError:
                draft_db_source = ""
                source_db_source = ""
            normalized_draft_db_source = self._normalize_sqlalchemy_default_factory(draft_db_source)
            if normalized_draft_db_source != draft_db_source:
                draft_db_path.write_text(normalized_draft_db_source, encoding="utf-8")
                draft_db_source = normalized_draft_db_source
                changed_files.add("miniapp/app/db.py")
            if self._has_invalid_datetime_timezone_call(draft_db_source) and not self._has_invalid_datetime_timezone_call(source_db_source):
                draft_db_path.write_text(source_db_source, encoding="utf-8")
                changed_files.add("miniapp/app/db.py")

        route_manifest_path = draft_source / "miniapp/app/generated/route_manifest.json"
        runtime_manifest_path = draft_source / "miniapp/app/generated/runtime_manifest.json"
        if route_manifest_path.exists() and runtime_manifest_path.exists():
            try:
                draft_route_manifest = json.loads(route_manifest_path.read_text(encoding="utf-8"))
                draft_runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                draft_route_manifest = None
                draft_runtime_manifest = None
            if (
                isinstance(draft_route_manifest, dict)
                and isinstance(draft_runtime_manifest, dict)
                and not isinstance(draft_route_manifest.get("roles"), dict)
                and isinstance(draft_runtime_manifest.get("roles"), dict)
            ):
                repaired_route_manifest = service._build_route_manifest(draft_runtime_manifest)
                route_manifest_path.write_text(json_dumps(repaired_route_manifest), encoding="utf-8")
                changed_files.add("miniapp/app/generated/route_manifest.json")

        static_root = draft_source / "miniapp/app/static"
        source_static_root = source_root / "miniapp/app/static"
        for draft_html in static_root.rglob("index.html"):
            relative = draft_html.relative_to(static_root)
            draft_js = draft_html.parent / "app.js"
            source_html = source_static_root / relative
            source_js = source_html.parent / "app.js"
            source_css = source_html.parent / "styles.css"
            if not (draft_js.exists() and source_html.exists() and source_js.exists()):
                continue
            try:
                draft_html_source = draft_html.read_text(encoding="utf-8")
                draft_js_source = draft_js.read_text(encoding="utf-8")
                source_html_source = source_html.read_text(encoding="utf-8")
                source_js_source = source_js.read_text(encoding="utf-8")
            except OSError:
                continue
            if self._contains_manual_refresh_action(
                html_source=draft_html_source,
                js_source=draft_js_source,
            ) and not self._contains_manual_refresh_action(
                html_source=source_html_source,
                js_source=source_js_source,
            ):
                draft_html.write_text(source_html_source, encoding="utf-8")
                draft_js.write_text(source_js_source, encoding="utf-8")
                changed_files.add(f"miniapp/app/static/{relative.as_posix()}")
                changed_files.add(f"miniapp/app/static/{relative.parent.as_posix()}/app.js")
                draft_css = draft_html.parent / "styles.css"
                if draft_css.exists() and source_css.exists():
                    try:
                        draft_css.write_text(source_css.read_text(encoding="utf-8"), encoding="utf-8")
                        changed_files.add(f"miniapp/app/static/{relative.parent.as_posix()}/styles.css")
                    except OSError:
                        pass
                continue
            missing_ids = self._js_required_dom_ids(draft_js_source) - self._html_dom_ids(draft_html_source)
            if not missing_ids:
                continue
            source_missing_ids = self._js_required_dom_ids(source_js_source) - self._html_dom_ids(source_html_source)
            if source_missing_ids:
                continue
            draft_html.write_text(source_html_source, encoding="utf-8")
            draft_js.write_text(source_js_source, encoding="utf-8")
            changed_files.add(f"miniapp/app/static/{relative.as_posix()}")
            changed_files.add(f"miniapp/app/static/{relative.parent.as_posix()}/app.js")
            draft_css = draft_html.parent / "styles.css"
            if draft_css.exists() and source_css.exists():
                try:
                    draft_css.write_text(source_css.read_text(encoding="utf-8"), encoding="utf-8")
                    changed_files.add(f"miniapp/app/static/{relative.parent.as_posix()}/styles.css")
                except OSError:
                    pass
        return sorted(changed_files)

    def _stabilize_backend_contract_from_source(
        self,
        *,
        workspace_id: str,
        draft_source: Path,
    ) -> None:
        self.stabilize_draft_contract_from_source(
            workspace_id=workspace_id,
            draft_source=draft_source,
        )

    @staticmethod
    def _load_json_file(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _route_manifest_missing_active_role_routes(
        route_manifest: dict[str, Any] | None,
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
    ) -> bool:
        if not role_scope:
            return False
        manifest_roles = dict((route_manifest or {}).get("roles") or {})
        graph_roles = dict((page_graph.get("roles") or {}))
        for role in role_scope:
            graph_pages = [page for page in ((graph_roles.get(role) or {}).get("pages") or []) if isinstance(page, dict)]
            if not graph_pages:
                continue
            declared_paths = {
                str(page.get("route_path") or "").strip()
                for page in (((manifest_roles.get(role) or {}).get("pages") or []))
                if isinstance(page, dict)
            }
            expected_paths = {
                str(page.get("route_path") or "").strip()
                for page in graph_pages
                if str(page.get("route_path") or "").strip()
            }
            if expected_paths - declared_paths:
                return True
        return False

    def _merge_partial_scope_page_graph_from_draft(
        self,
        *,
        draft_source: Path,
        page_graph: dict[str, Any],
        role_scope: list[str],
        scope_mode: str,
    ) -> dict[str, Any]:
        if scope_mode != "minimal_patch":
            return page_graph
        active_roles = {str(role).strip() for role in role_scope if str(role).strip()}
        if not active_roles or active_roles >= {"client", "specialist", "manager"}:
            return page_graph
        existing_graph = self._load_json_file(draft_source / "artifacts/generated_app_graph.json") or {}
        existing_roles = dict((existing_graph.get("roles") or {}))
        if not existing_roles:
            merged = dict(page_graph)
            merged["roles"] = dict(page_graph.get("roles") or {})
            for role in active_roles:
                self._hydrate_existing_role_shell_pages_from_draft(
                    draft_source=draft_source,
                    merged_roles=merged["roles"],
                    role=role,
                )
            return merged
        merged = dict(page_graph)
        merged_roles = dict(existing_roles)
        merged_roles.update(dict((page_graph.get("roles") or {})))
        for role in active_roles:
            self._hydrate_existing_role_shell_pages_from_draft(
                draft_source=draft_source,
                merged_roles=merged_roles,
                role=role,
            )
        ordered_roles: dict[str, Any] = {}
        for role in ("client", "specialist", "manager"):
            if role in merged_roles:
                ordered_roles[role] = merged_roles[role]
        for role, payload in merged_roles.items():
            if role not in ordered_roles:
                ordered_roles[role] = payload
        merged["roles"] = ordered_roles
        return merged

    def _hydrate_existing_role_shell_pages_from_draft(
        self,
        *,
        draft_source: Path,
        merged_roles: dict[str, Any],
        role: str,
    ) -> None:
        role_payload = dict((merged_roles.get(role) or {}))
        role_pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
        known_routes = {str(page.get("route_path") or "").strip() for page in role_pages}
        shell_pages = (
            (
                draft_source / f"miniapp/app/static/{role}/index.html",
                f"/{role}",
                f"{role}_index",
                "landing",
                "Dashboard",
                "Home",
                True,
            ),
            (
                draft_source / f"miniapp/app/static/{role}/profile/index.html",
                f"/{role}/profile",
                f"{role}_profile",
                "profile",
                "Profile",
                "Profile",
                False,
            ),
        )
        for html_path, route_path, page_id, page_kind, title, navigation_label, is_entry in shell_pages:
            if route_path in known_routes or not html_path.exists():
                continue
            rel_html = html_path.relative_to(draft_source).as_posix()
            role_pages.append(
                {
                    "page_id": page_id,
                    "route_path": route_path,
                    "file_path": rel_html,
                    "style_path": self.service._default_page_asset_path(rel_html, asset_kind="css"),
                    "script_path": self.service._default_page_asset_path(rel_html, asset_kind="js"),
                    "page_kind": page_kind,
                    "title": title,
                    "navigation_label": navigation_label,
                    "is_entry": is_entry,
                }
            )
            known_routes.add(route_path)
        role_static_dir = draft_source / f"miniapp/app/static/{role}"
        for html_path in sorted(role_static_dir.glob("**/index.html")):
            rel_html = html_path.relative_to(draft_source).as_posix()
            rel_dir = html_path.parent.relative_to(role_static_dir).as_posix()
            if rel_dir == ".":
                route_path = f"/{role}"
                page_id = f"{role}_index"
            else:
                slug = rel_dir.strip("/")
                route_segments: list[str] = []
                for segment in slug.split("/"):
                    tokens = [token for token in segment.split("_") if token]
                    if not tokens:
                        continue
                    rebuilt: list[str] = []
                    index = 0
                    while index < len(tokens):
                        token = tokens[index]
                        if index + 1 < len(tokens) and tokens[index + 1] == "id":
                            rebuilt.append(f"{{{token}_id}}")
                            index += 2
                            continue
                        if token.endswith("id") and token != "id":
                            rebuilt.append(f"{{{token}}}")
                        else:
                            rebuilt.append(token)
                        index += 1
                    route_segments.extend(rebuilt)
                route_path = f"/{role}/{'/'.join(route_segments)}" if route_segments else f"/{role}"
                page_id = f"{role}_{slug.replace('/', '_')}".strip("_")
            if route_path in known_routes:
                continue
            page_kind = self.service._page_kind(
                None,
                route_path=route_path.replace(f"/{role}", "", 1) or "/",
                file_path=rel_html,
                page_id=page_id,
            )
            role_pages.append(
                {
                    "page_id": page_id,
                    "route_path": route_path,
                    "file_path": rel_html,
                    "style_path": self.service._default_page_asset_path(rel_html, asset_kind="css"),
                    "script_path": self.service._default_page_asset_path(rel_html, asset_kind="js"),
                    "page_kind": page_kind,
                    "title": str(Path(rel_dir).name.replace("_", " ").title() or "Page"),
                    "navigation_label": str(Path(rel_dir).name.replace("_", " ").title() or "Open"),
                    "is_entry": route_path == f"/{role}",
                }
            )
            known_routes.add(route_path)
        if role_pages:
            role_payload["pages"] = role_pages
            role_payload["entry_path"] = str(role_payload.get("entry_path") or f"/{role}")
            merged_roles[role] = role_payload

    def run(
        self,
        *,
        workspace,
        workspace_id: str,
        job: JobRecord,
        request,
        draft_run_id: str,
        draft_source: Path,
        effective_prompt: str,
        grounded_spec,
        entity_contract: dict[str, Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        plan_result: dict[str, Any],
        execution_class: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        retrieval_ms: int,
        started_at: float,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord:
        service = self.service
        plan_result = service.generation_plan_runtime.prepare_runtime_plan(
            workspace_id=workspace_id,
            draft_source=draft_source,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role_scope=role_scope,
            plan_result=plan_result,
        )
        plan_result["page_graph"] = self._merge_partial_scope_page_graph_from_draft(
            draft_source=draft_source,
            page_graph=plan_result["page_graph"],
            role_scope=role_scope,
            scope_mode=plan_result["scope_mode"],
        )
        if bool(plan_result.get("visual_only_patch")) and isinstance(plan_result.get("page_graph"), dict):
            plan_result["backend_targets"] = []
            plan_result["page_graph"]["backend_targets"] = []
        service._append_event(
            job,
            "context_pack_started",
            f"Collecting targeted file context for {len(plan_result['target_files'])} planned files.",
        )
        context_pack = service.context_pack_builder.build(
            workspace=workspace,
            prompt=effective_prompt,
            model_profile=request.model_profile,
            generation_mode=generation_mode,
            active_paths=plan_result["files_to_read"],
            target_files=plan_result["target_files"],
            grounded_spec=grounded_spec,
            execution_class=execution_class,
            run_id=draft_run_id,
        )
        files_read = sorted(
            set(plan_result["files_to_read"]) | set(context_pack.targeted_files.keys()) | {chunk.path for chunk in context_pack.code_chunks}
        )
        file_contexts: dict[str, str] = dict(context_pack.targeted_files)
        for file_path in plan_result["files_to_read"]:
            if file_path in file_contexts:
                continue
            try:
                content = service.workspace_service.try_read_text_file(workspace_id, file_path, run_id=draft_run_id)
            except FileNotFoundError:
                continue
            if content is None:
                continue
            file_contexts[file_path] = content
        from app.services.miniapp_generation.service import ACTIVE_LLM_CACHE_STATS

        current_cache_stats = ACTIVE_LLM_CACHE_STATS.get() or {}
        current_cache_stats["prompt_cache_key"] = context_pack.prompt_cache_key
        current_cache_stats["stable_prefix_chars"] = len(context_pack.system_prefix)
        job.cache_stats = dict(current_cache_stats)
        service._store_report(
            f"retrieval_anchor_report:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "run_id": draft_run_id,
                **dict((context_pack.retrieval_stats or {}).get("anchor_report") or {}),
            },
        )
        job.latency_breakdown["context_pack_ms"] = max(0, int((time.perf_counter() - started_at) * 1000) - retrieval_ms)
        service._append_event(
            job,
            "context_pack_ready",
            f"Context pack ready with {len(context_pack.code_chunks)} code chunks, {len(context_pack.doc_chunks)} doc chunks, and {len(context_pack.targeted_files)} file bodies.",
        )

        visual_only_patch = bool(plan_result.get("visual_only_patch"))

        service._append_event(job, "generating_code", "Generating backend and page bundles.")
        service._append_event(job, "editing_started", "Generating draft file edits.")
        edit_result = service._resolve_code_edits(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role_scope=role_scope,
            file_contexts=file_contexts,
            target_files=plan_result["target_files"],
            role_contract=role_contract,
            page_graph=plan_result["page_graph"],
            intent=request.intent,
            scope_mode=plan_result["scope_mode"],
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            visual_only_patch=visual_only_patch,
        )
        stopped = service._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped
        if "error" in edit_result:
            service._append_trace(workspace_id, "editing_failed", "Code editing failed.", {"error": edit_result["error"]})
            return service._block_with_messages(
                job,
                [edit_result["error"]],
                code="generation.edit.llm_failure",
                event_type="validation_failed",
                failure_reason=edit_result["error"],
            )
        if edit_result.get("planner_contract_gap_targets"):
            added_targets = list(edit_result["planner_contract_gap_targets"])
            service._append_event(
                job,
                "planner_contract_gap_detected",
                "Frontend or runtime invariant gaps were detected and draft targets were expanded before composition.",
                {"added_targets": added_targets},
            )
            service._append_event(
                job,
                "scope_expanded",
                "Expanded generation targets to cover invariant-related files discovered during code generation.",
                {"added_targets": added_targets},
            )
            plan_result["target_files"] = list(edit_result.get("effective_target_files") or plan_result["target_files"])
            plan_result["backend_targets"] = list(edit_result.get("effective_backend_targets") or plan_result.get("backend_targets") or [])
        for metric_key, metric_value in (edit_result.get("latency_breakdown") or {}).items():
            job.latency_breakdown[metric_key] = int(metric_value)
        for trace_stage, payload in (edit_result.get("trace_payloads") or {}).items():
            service._append_trace(workspace_id, trace_stage, payload["message"], payload["payload"])
        invalid_operation_paths = [
            operation.file_path
            for operation in edit_result["operations"]
            if service._normalize_path_list([operation.file_path], []) != [operation.file_path]
        ]
        if invalid_operation_paths:
            service._append_trace(
                workspace_id,
                "editing_failed",
                "Code editing produced invalid file paths.",
                {"invalid_paths": invalid_operation_paths[:10]},
            )
            return service._block_with_messages(
                job,
                [f"Code editing produced invalid file paths: {', '.join(invalid_operation_paths[:5])}"],
                code="generation.edit.invalid_paths",
                event_type="validation_failed",
                failure_reason="Code editing produced invalid file paths.",
            )
        operations = [
            DraftFileOperation(
                file_path="artifacts/grounded_spec.json",
                operation="replace",
                content=json_dumps(grounded_spec.model_dump(mode="json")),
                reason="Persist the grounded planning artifact inside the draft workspace.",
            ),
            *[
                operation.model_copy(update={"file_path": service._normalize_runtime_python_path(operation.file_path)})
                for operation in edit_result["operations"]
            ],
        ]
        existing_route_manifest = self._load_json_file(draft_source / "miniapp/app/generated/route_manifest.json")
        existing_runtime_manifest = self._load_json_file(draft_source / "miniapp/app/generated/runtime_manifest.json")
        preserve_existing_roles = (
            plan_result["scope_mode"] == "minimal_patch"
            and bool(role_scope)
            and set(role_scope) < {"client", "specialist", "manager"}
        )
        refresh_runtime_artifacts = self._route_manifest_missing_active_role_routes(
            existing_route_manifest,
            page_graph=plan_result["page_graph"],
            role_scope=role_scope,
        )
        plan_result["refresh_runtime_artifacts"] = refresh_runtime_artifacts
        if (not visual_only_patch) or refresh_runtime_artifacts:
            operations = service._ensure_runtime_artifact_operations(
                grounded_spec=grounded_spec,
                page_graph=plan_result["page_graph"],
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=operations,
                existing_route_manifest=existing_route_manifest,
                existing_runtime_manifest=existing_runtime_manifest,
                preserve_existing_roles=preserve_existing_roles,
            )
        if not visual_only_patch:
            operations = service._ensure_app_level_test_operations(
                page_graph=plan_result["page_graph"],
                role_scope=role_scope,
                entity_contract=entity_contract,
                operations=operations,
            )
        critic_report = {"executed": False, "issues": [], "issue_count": 0, "blocking_issue_count": 0}
        if service.generation_contract_critic.should_run_preapply_critic(
            generation_mode=generation_mode,
            operations=operations,
            entity_contract=entity_contract,
        ):
            critic_report = service.generation_contract_critic.build_preapply_report(
                operations=operations,
                entity_contract=entity_contract,
                generation_mode=generation_mode,
            )
            service._store_report(
                f"contract_critic:{workspace_id}",
                {"workspace_id": workspace_id, "run_id": draft_run_id, **critic_report},
            )
            if critic_report.get("issue_count"):
                service._append_event(
                    job,
                    "iteration_ready",
                    "Pre-apply contract critic found coherence issues that should be corrected during draft validation and repair.",
                    {"issue_count": critic_report.get("issue_count"), "issues": critic_report.get("issues")},
                )
                service._append_trace(
                    workspace_id,
                    "contract_critic_flagged",
                    "Pre-apply contract critic detected naming or persistence-coherence risks in the generated draft.",
                    critic_report,
                )
        if not visual_only_patch:
            operations = service._run_pre_apply_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                page_graph=plan_result["page_graph"],
                role_scope=role_scope,
                generation_mode=generation_mode,
                operations=operations,
                entity_contract=entity_contract,
                contract_sync_mode="bootstrap_only",
            )
        patch_envelope = service.workspace_service.build_patch_envelope_for_draft(workspace_id, draft_run_id, operations)
        apply_result = service.workspace_service.apply_patch_envelope_to_draft(workspace_id, draft_run_id, patch_envelope)
        if apply_result.status != "applied":
            return service._block_with_messages(
                job,
                [apply_result.conflict_reason or "Draft patch could not be applied safely."],
                code="generation.patch.conflict",
                event_type="job_failed",
                failure_reason=apply_result.conflict_reason or "Draft patch could not be applied safely.",
            )
        if not visual_only_patch:
            self._stabilize_backend_contract_from_source(
                workspace_id=workspace_id,
                draft_source=draft_source,
            )
        job.apply_result = apply_result.model_dump(mode="json")
        realized_paths = service._realized_draft_file_paths(workspace_id, draft_run_id)
        stage_reports = service._build_stage_reports(
            page_graph=plan_result["page_graph"],
            role_scope=role_scope,
            realized_paths=realized_paths,
        )
        materialization_report = service._build_materialization_report(
            execution_class=execution_class,
            page_graph=plan_result["page_graph"],
            role_scope=role_scope,
            realized_paths=realized_paths,
        )
        service._store_report(
            f"stage_reports:{workspace_id}",
            {"workspace_id": workspace_id, "run_id": draft_run_id, "items": stage_reports},
        )
        service._store_report(
            f"materialization_report:{workspace_id}",
            {"workspace_id": workspace_id, "run_id": draft_run_id, **materialization_report.model_dump(mode="json")},
        )
        materialization_gate = service._materialization_gate_result(
            materialization_report,
            require_multi_page=bool(plan_result["require_multi_page"]),
            scope_mode=plan_result["scope_mode"],
            generation_mode=generation_mode,
        )
        edit_gate_issues = service._edit_gate_issues(
            plan_result["page_graph"],
            operations,
            role_scope,
            scope_mode=plan_result["scope_mode"],
            target_files=plan_result["target_files"],
        )
        initial_loop_diagnostics: list[str] = []
        if critic_report.get("issue_count"):
            initial_loop_diagnostics.extend(
                [
                    str(issue.get("message") or "").strip()
                    for issue in list(critic_report.get("issues") or [])[:4]
                    if str(issue.get("message") or "").strip()
                ]
            )
        if materialization_gate is not None:
            failure_code, failure_messages = materialization_gate
            service._append_trace(
                workspace_id,
                "materialization_needs_iteration",
                "Initial code generation did not fully materialize the intended workflow surface and will continue through exact-check iteration.",
                {
                    "code": failure_code,
                    "messages": failure_messages,
                    "materialization_report": materialization_report.model_dump(mode="json"),
                },
            )
            service._append_event(
                job,
                "iteration_ready",
                "Initial generation produced a partial workflow surface. Continuing through exact-check iteration instead of blocking early.",
                {
                    "messages": failure_messages,
                    "iteration_reason": "materialization_needs_iteration",
                },
            )
            initial_loop_diagnostics.extend(failure_messages)
        if edit_gate_issues:
            service._append_trace(
                workspace_id,
                "editing_needs_iteration",
                "Initial code generation left placeholder or structural issues that will be handled by the workspace loop.",
                {"issues": edit_gate_issues, "materialization_report": materialization_report.model_dump(mode="json")},
            )
            service._append_event(
                job,
                "iteration_ready",
                "Initial generation left placeholder or structural issues. Continuing through exact-check iteration.",
                {
                    "issues": edit_gate_issues,
                    "iteration_reason": "editing_needs_iteration",
                },
            )
            initial_loop_diagnostics.extend(edit_gate_issues)
        initial_exact_execution, _initial_exact_preview = service.generation_repair._execute_generation_checks(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            draft_source=draft_source,
            changed_files=[operation.file_path for operation in operations],
            fallback_changed_files=[operation.file_path for operation in operations],
            page_graph=plan_result["page_graph"],
            role_scope=role_scope,
            scope_mode=plan_result["scope_mode"],
            mode="exact",
        )
        initial_exact_issues = CheckRunner.failing_issues(initial_exact_execution.results)
        build_issues = [issue for issue in initial_exact_issues if issue.location != "preview"]
        preview_issue = next((issue for issue in initial_exact_issues if issue.location == "preview"), None)
        if initial_exact_issues:
            exact_check_summary = service._summarize_failed_checks(build_issues, preview_issue)
            service._append_trace(
                workspace_id,
                "exact_checks_seeded",
                "Initial generation exact-check results were captured before entering the workspace loop.",
                {
                    "summary": exact_check_summary,
                    "failing_checks": [issue.model_dump(mode="json") for issue in initial_exact_issues],
                },
            )
            service._append_event(
                job,
                "checks_completed",
                "Seeded the iterative workspace loop with exact-check diagnostics from the first generated draft.",
                {"summary": exact_check_summary},
            )
            initial_loop_diagnostics.append(exact_check_summary)
        loop_seed_message = edit_result["assistant_message"]
        if initial_loop_diagnostics:
            loop_seed_message = (
                f"{loop_seed_message}\n\nInitial iteration diagnostics:\n- "
                + "\n- ".join(dict.fromkeys(initial_loop_diagnostics))
            ).strip()
        job.latency_breakdown["patch_apply_ms"] = max(0, int((time.perf_counter() - started_at) * 1000) - retrieval_ms)
        loop_result = service.generation_repair.run_generation_workspace_loop(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            job=job,
            request=request,
            draft_source=draft_source,
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            role_contract=role_contract,
            page_graph=plan_result["page_graph"],
            plan_result=plan_result,
            entity_contract=entity_contract,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            files_read=files_read,
            initial_operations=list(operations),
            initial_assistant_message=loop_seed_message,
            should_stop=should_stop,
        )
        latest_preview = service.preview_service.get(workspace_id)
        latest_assistant_message = loop_result.last_assistant_message or edit_result["assistant_message"]
        all_operations = list(loop_result.all_operations or operations)
        job.repair_iterations = [item.model_dump(mode="json") for item in loop_result.repair_iterations]
        if loop_result.latest_apply_result is not None:
            job.apply_result = loop_result.latest_apply_result
        if loop_result.latest_execution is not None:
            job.executed_checks = [item.model_dump(mode="json") for item in loop_result.latest_execution.results]
        job.remaining_issues = [] if loop_result.status == "completed" else list(loop_result.remaining_issues or [])

        if loop_result.status != "completed":
            latest_results = list(loop_result.latest_execution.results) if loop_result.latest_execution is not None else []
            latest_issues = CheckRunner.failing_issues(latest_results)
            preview_issue = next((issue for issue in latest_issues if issue.location == "preview"), None)
            build_issues = [issue for issue in latest_issues if issue.location != "preview"]
            if loop_result.latest_execution is not None:
                job.validation_snapshot = service.generation_completion.validation_snapshot_from_execution(loop_result.latest_execution)
                service._store_report(
                    f"validation:{workspace_id}",
                    job.validation_snapshot.model_dump(mode="json"),
                )
            job.status = loop_result.status
            job.outcome_kind = loop_result.outcome_kind
            job.failure_reason = loop_result.failure_reason or loop_result.summary
            job.failure_class = loop_result.failure_class or service._failure_class_from_error_context(request.error_context)
            job.root_cause_summary = loop_result.root_cause_summary or service._summarize_failed_checks(build_issues, preview_issue)
            job.summary = loop_result.summary
            job.fix_targets = sorted(
                {
                    issue.location
                    for issue in latest_issues
                    if issue.location and issue.location not in {"generation", "preview"}
                }
            )
            if job.status == "failed":
                job.handoff_from_failed_generate = service._build_fix_handoff(
                    prompt=request.prompt,
                    failure_reason=job.failure_reason,
                    failure_class=job.failure_class,
                    issues=latest_issues,
                    mode=request.mode,
                )
            service._append_event(job, "job_failed", job.failure_reason or loop_result.summary)
            return job

        job.outcome_kind = "applied"
        if loop_result.latest_execution is not None:
            job.validation_snapshot = service.generation_completion.validation_snapshot_from_execution(
                loop_result.latest_execution
            )
        else:
            job.validation_snapshot = ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        service._store_report(
            f"validation:{workspace_id}",
            job.validation_snapshot.model_dump(mode="json"),
        )

        traceability = service._build_agent_traceability_report(workspace_id, grounded_spec, all_operations)
        service._store_report(f"traceability:{workspace_id}", traceability.model_dump(mode="json"))
        summary = service._build_agent_summary(
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            operations=all_operations,
            generation_mode=generation_mode,
            assistant_message=latest_assistant_message,
        )
        assistant_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="assistant",
            content=summary,
            summary=summary,
            linked_job_id=job.job_id,
            linked_run_id=request.linked_run_id,
        )
        service.store.upsert("chat_turns", assistant_turn.turn_id, assistant_turn.model_dump(mode="json"))

        job.status = "completed"
        job.failure_reason = None
        job.summary = summary
        job.traceability_report_id = traceability.report_id
        job.assumptions_report = [item.model_dump(mode="json") for item in grounded_spec.assumptions]
        job.fix_targets = sorted({operation.file_path for operation in all_operations})
        job.latency_breakdown["ttft_ms"] = retrieval_ms
        job.latency_breakdown["total_ms"] = int((time.perf_counter() - started_at) * 1000)
        from app.services.miniapp_generation.service import ACTIVE_LLM_CACHE_STATS

        job.cache_stats = dict(ACTIVE_LLM_CACHE_STATS.get() or job.cache_stats)
        job.compile_summary = service._compile_code_summary(all_operations, role_scope)
        job.artifacts = {
            "preview_url": latest_preview.url or "",
            "grounded_spec": "reports/spec",
            "traceability": "reports/traceability",
            "candidate_diff": "reports/candidate_diff",
            "iterations": "reports/iterations",
            "check_results": "reports/check_results",
            "patch": "reports/patch",
            "role_contract": "reports/role_contract",
            "page_graph": "reports/page_graph",
            "materialization_report": "reports/materialization_report",
            "stage_reports": "reports/stage_reports",
        }
        service._append_event(job, "job_completed", "Generation run completed successfully.")
        return job
