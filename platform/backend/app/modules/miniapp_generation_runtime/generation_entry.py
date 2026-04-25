from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Callable

from app.models.domain import GenerateRequest, JobRecord
from app.modules.miniapp_visual_patch_fast_lane import MiniappVisualPatchFastLane
from app.services.miniapp_generation.constants import ROLE_ORDER

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationEntry:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def generate_with_agent_loop(
        self,
        *,
        workspace,
        workspace_id: str,
        job: JobRecord,
        request: GenerateRequest,
        draft_run_id: str,
        effective_prompt: str,
        target_platform,
        preview_profile,
        generation_mode,
        role_scope: list[str],
        doc_refs: list[Any],
        retrieval_ms: int,
        started_at: float,
        creative_direction: dict[str, Any],
        should_stop: Callable[[], bool] | None,
        prompt_turn_id: str,
    ) -> JobRecord:
        fast_visual_job = MiniappVisualPatchFastLane(self.service).try_run(
            workspace_id=workspace_id,
            run_id=draft_run_id,
            request=request,
            job=job,
            role_scope=role_scope,
            started_at=started_at,
            draft_source=None,
            run_mode="generate",
        )
        if fast_visual_job is not None:
            return fast_visual_job

        self.service._append_event(job, "building_surface", "Building a planner-driven generation surface on top of the minimal template bootstrap.")
        self.service._append_trace(
            workspace_id,
            "generation_loop_started",
            "Agentic generation loop selected as the default path.",
            {"role_scope": role_scope, "generation_mode": generation_mode.value},
        )
        grounded_spec = self.service._build_grounded_spec(
            workspace_id=workspace_id,
            prompt=effective_prompt,
            target_platform=target_platform,
            preview_profile=preview_profile,
            doc_refs=doc_refs,
            template_revision_id=workspace.current_revision_id or "template-unknown",
            prompt_turn_id=prompt_turn_id,
            generation_mode=generation_mode,
        )
        self.service._append_trace(
            workspace_id,
            "grounded_spec_ready",
            "Grounded spec compiled before planner-driven file targeting.",
            {"llm_spec_stage_removed": True},
        )
        grounded_spec = self.service._stabilize_grounded_spec(grounded_spec)
        self.service._store_report(f"spec:{workspace_id}", grounded_spec.model_dump(mode="json"))
        entity_contract = self.service.generation_entity_contract.extract_entity_contract(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            generation_mode=generation_mode,
        )
        role_patch_kind = self.service._role_only_patch_kind(
            prompt=effective_prompt,
            role_scope=role_scope,
            intent=request.intent,
        )
        source_contract_patch_kind = role_patch_kind
        if source_contract_patch_kind is None and request.intent in {"edit", "refine", "role_only_change"} and len(role_scope) == 1:
            source_contract_patch_kind = "role_patch"
        preserved_entity_contract = self._preserve_source_entity_contract_for_role_patch(
            workspace_id=workspace_id,
            extracted_entity_contract=entity_contract,
            role_patch_kind=source_contract_patch_kind,
        )
        if preserved_entity_contract is not entity_contract:
            entity_contract = preserved_entity_contract
            self.service._append_trace(
                workspace_id,
                "entity_contract_preserved",
                "Preserved the existing source entity contract for a narrow single-role patch.",
                {
                    "role_patch_kind": source_contract_patch_kind,
                    "entity_slug": entity_contract.get("entity_slug"),
                    "api_path": entity_contract.get("api_path"),
                    "route_file": entity_contract.get("route_file"),
                },
            )
        self.service._store_report(
            f"entity_contract:{workspace_id}",
            {"run_id": draft_run_id, "entity_contract": entity_contract},
        )
        self.service._append_trace(
            workspace_id,
            "entity_contract_ready",
            "Extracted a prompt-derived entity contract before planning and code generation.",
            {
                "entity_slug": entity_contract.get("entity_slug"),
                "api_path": entity_contract.get("api_path"),
                "route_file": entity_contract.get("route_file"),
                "generation_mode": generation_mode.value,
            },
        )
        self.service._append_event(job, "spec_ready", "Grounded specification compiled for thin generation.")

        execution_class = self.service._classify_execution_class(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            intent=request.intent,
        )
        job.execution_class = execution_class  # type: ignore[assignment]
        draft_source = self.service.workspace_service.prepare_draft(workspace_id, draft_run_id)
        self.service._append_event(job, "draft_prepared", "Prepared draft workspace from the current revision.")

        role_contract_result = self.service._resolve_role_contract(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            doc_refs=doc_refs,
            role_scope=role_scope,
            intent=request.intent,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        role_contract = dict(role_contract_result.get("role_contract") or {})
        role_contract_issues = self.service._role_contract_gate_issues(
            role_contract,
            role_scope,
            scope_mode="whole_file_build",
        )
        if role_contract_issues:
            self.service._append_trace(
                workspace_id,
                "role_contract_soft_issues",
                "Role contract analysis produced soft guidance issues; continuing with prompt-driven planning.",
                {"issues": role_contract_issues},
            )
        self.service._append_event(job, "surface_ready", "Role guidance compiled as advisory input for file-first generation.")

        advisory_plan_result = self.service._resolve_code_plan(
            workspace_id=workspace_id,
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            doc_refs=doc_refs,
            role_scope=role_scope,
            role_contract=role_contract,
            intent=request.intent,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
        )
        role_contract, plan_result = self._merge_advisory_generation_inputs(
            role_contract=role_contract,
            inferred_role_contract={},
            advisory_plan_result=advisory_plan_result,
            inferred_plan_result={},
        )
        plan_error = str(advisory_plan_result.get("error") or "").strip()
        self.service._append_event(job, "surface_ready", "Planner targets and advisory generation hints are ready.")
        self.service._append_trace(
            workspace_id,
            "generation_plan_advisory",
            "Normal generation used planner hints directly without Python-authored app bootstrapping before tool-owned code generation.",
            {
                "planner_error": plan_error or None,
                "target_files": len(plan_result["target_files"]),
                "backend_targets": len(plan_result["backend_targets"]),
                "generation_clusters": plan_result["generation_clusters"],
            },
        )
        if plan_result.get("plan_gate_issues"):
            self.service._append_trace(
                workspace_id,
                "generation_plan_soft_issues",
                "Advisory planning produced soft issues; generation continues code-first.",
                {"issues": list(plan_result.get("plan_gate_issues") or [])},
            )
        self.service._store_report(f"role_contract:{workspace_id}", {"run_id": draft_run_id, "role_contract": role_contract})
        self.service._append_trace(
            workspace_id,
            "generation_runtime_ready",
            "Planner targets and advisory generation inputs were prepared before code generation.",
            {
                "target_files": len(plan_result["target_files"]),
                "backend_targets": len(plan_result["backend_targets"]),
                "generation_clusters": plan_result["generation_clusters"],
            },
        )
        self.service._store_report(f"page_graph:{workspace_id}", {"run_id": draft_run_id, "page_graph": plan_result["page_graph"]})
        self.service._store_report(f"execution_plan:{workspace_id}", {"run_id": draft_run_id, "execution_plan": plan_result.get("execution_plan", {})})

        stopped = self.service._stop_if_requested(job, workspace_id, should_stop)
        if stopped is not None:
            return stopped

        return self.continue_generation_from_plan(
            workspace=workspace,
            workspace_id=workspace_id,
            job=job,
            request=request,
            draft_run_id=draft_run_id,
            draft_source=draft_source,
            effective_prompt=effective_prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role_scope=role_scope,
            role_contract=role_contract,
            plan_result=plan_result,
            execution_class=execution_class,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            retrieval_ms=retrieval_ms,
            started_at=started_at,
            should_stop=should_stop,
        )

    def _preserve_source_entity_contract_for_role_patch(
        self,
        *,
        workspace_id: str,
        extracted_entity_contract: dict[str, Any],
        role_patch_kind: str | None,
    ) -> dict[str, Any]:
        if role_patch_kind not in {"visual_patch", "ui_flow_patch", "contract_patch", "role_patch"}:
            return extracted_entity_contract
        source_entity_contract = self._load_existing_entity_contract(workspace_id)
        if not source_entity_contract:
            return extracted_entity_contract
        preserved = deepcopy(source_entity_contract)
        if extracted_entity_contract.get("extraction_mode"):
            preserved["extraction_mode"] = extracted_entity_contract.get("extraction_mode")
        if role_patch_kind in {"ui_flow_patch", "contract_patch"}:
            preserved_page_contract = dict(source_entity_contract.get("page_contract") or {})
            for key, value in dict(extracted_entity_contract.get("page_contract") or {}).items():
                if value is not None:
                    preserved_page_contract[key] = value
            if preserved_page_contract:
                preserved["page_contract"] = preserved_page_contract
        preserved_source = dict(source_entity_contract.get("source") or {})
        preserved_source["preserved_from_source_contract"] = True
        preserved_source["patch_kind"] = role_patch_kind
        preserved["source"] = preserved_source
        return preserved

    def _load_existing_entity_contract(self, workspace_id: str) -> dict[str, Any] | None:
        source_contract = self._load_existing_entity_contract_from_source_tests(workspace_id)
        if source_contract:
            return source_contract
        report_payload = self.service.store.get("reports", f"entity_contract:{workspace_id}") or {}
        report_contract = report_payload.get("entity_contract") if isinstance(report_payload, dict) else None
        if self._looks_like_entity_contract(report_contract):
            return dict(report_contract)
        return None

    def _load_existing_entity_contract_from_source_tests(self, workspace_id: str) -> dict[str, Any] | None:
        source_tests_path = self.service.workspace_service.source_dir(workspace_id) / "miniapp" / "tests" / "test_generated_app.py"
        if not source_tests_path.exists():
            return None
        try:
            content = source_tests_path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.search(
            r"ENTITY_CONTRACT\s*=\s*json\.loads\((?P<literal>(?:'[^']*'|\"[^\"]*\"))\)",
            content,
            re.DOTALL,
        )
        if not match:
            return None
        try:
            literal = ast.literal_eval(match.group("literal"))
            parsed = json.loads(literal)
        except Exception:
            return None
        if self._looks_like_entity_contract(parsed):
            return dict(parsed)
        return None

    @staticmethod
    def _looks_like_entity_contract(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        api_path = str(payload.get("api_path") or "").strip()
        route_file = str(payload.get("route_file") or "").strip()
        return api_path.startswith("/api/") and route_file.endswith(".py")

    def _merge_advisory_generation_inputs(
        self,
        *,
        role_contract: dict[str, Any],
        inferred_role_contract: dict[str, Any],
        advisory_plan_result: dict[str, Any],
        inferred_plan_result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        merged_roles = dict(inferred_role_contract.get("roles") or {})
        merged_roles.update(role_contract.get("roles") or {})
        merged_role_contract = {
            **inferred_role_contract,
            **role_contract,
            "roles": merged_roles,
        }
        merged_plan = dict(inferred_plan_result)
        current_graph = dict((merged_plan.get("page_graph") or {}))
        current_roles = dict((current_graph.get("roles") or {}))
        advisory_graph = dict(advisory_plan_result.get("page_graph") or {})
        scope_mode = advisory_plan_result.get("scope_mode") or inferred_plan_result.get("scope_mode") or "whole_file_build"
        for role, advisory_role_payload in (advisory_graph.get("roles") or {}).items():
            existing_role_payload = current_roles.get(role)
            if not isinstance(existing_role_payload, dict):
                current_roles[role] = advisory_role_payload
                continue
            if isinstance(advisory_role_payload, dict) and (advisory_role_payload.get("pages") or []):
                current_roles[role] = self._merge_role_page_graph_payload(
                    role=role,
                    existing_role_payload=existing_role_payload,
                    advisory_role_payload=advisory_role_payload,
                )
                continue
            if not (existing_role_payload.get("pages") or []):
                current_roles[role] = advisory_role_payload
        current_graph["roles"] = current_roles
        merged_plan["page_graph"] = current_graph
        advisory_target_files = [
            str(path)
            for path in (advisory_plan_result.get("target_files") or [])
            if isinstance(path, str) and str(path).strip()
        ]
        advisory_backend_targets = [
            str(path)
            for path in (advisory_plan_result.get("backend_targets") or [])
            if isinstance(path, str) and str(path).strip()
        ]
        advisory_page_targets = [
            path
            for path in advisory_target_files
            if path.startswith("miniapp/app/static/")
        ]
        runtime_bootstrap_targets = {
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/runtime.py",
        }
        visual_only_patch = bool(advisory_plan_result.get("visual_only_patch"))
        ui_flow_patch = bool(advisory_plan_result.get("ui_flow_patch") or inferred_plan_result.get("ui_flow_patch"))
        suppress_role_route_targets = bool(advisory_plan_result.get("suppress_role_route_targets"))
        role_patch_kind = (
            str(advisory_plan_result.get("role_patch_kind") or "").strip()
            or str(inferred_plan_result.get("role_patch_kind") or "").strip()
            or None
        )
        advisory_scope_uses_targeted_merge = bool(
            scope_mode in {"minimal_patch", "role_partial_build", "workflow_partial_build"} and (advisory_page_targets or advisory_backend_targets)
        )
        advisory_has_explicit_surface = bool(
            scope_mode == "whole_file_build"
            and (
                advisory_page_targets
                or advisory_backend_targets
                or any(
                    isinstance(payload, dict) and (payload.get("pages") or [])
                    for payload in (advisory_graph.get("roles") or {}).values()
                )
            )
        )
        merged_page_targets = (
            list(dict.fromkeys(advisory_page_targets))
            if advisory_scope_uses_targeted_merge and advisory_page_targets
            else self._page_graph_target_files(current_graph)
        )
        for key in ("shared_files", "files_to_read"):
            merged_plan[key] = list(
                dict.fromkeys(
                    [
                        *(inferred_plan_result.get(key) or []),
                        *(advisory_plan_result.get(key) or []),
                    ]
                )
            )
        inferred_runtime_targets = [
            path
            for path in (inferred_plan_result.get("backend_targets") or [])
            if isinstance(path, str) and path in runtime_bootstrap_targets
        ]
        merged_plan["backend_targets"] = list(
            dict.fromkeys(
                [
                    *(
                        inferred_runtime_targets
                        if advisory_has_explicit_surface
                        else (inferred_plan_result.get("backend_targets") or [])
                    ),
                    *(advisory_plan_result.get("backend_targets") or []),
                ]
            )
        )
        page_target_roles = set(self._roles_for_static_targets(merged_page_targets))
        explicit_role_route_targets = [
            str(role_payload.get("routes_file"))
            for role, role_payload in current_roles.items()
            if isinstance(role_payload, dict)
            and isinstance(role_payload.get("routes_file"), str)
            and not suppress_role_route_targets
            and (
                not advisory_scope_uses_targeted_merge
                or role in page_target_roles
            )
        ]
        merged_backend_targets = list(
            dict.fromkeys(
                [
                    *(
                        advisory_backend_targets
                        if advisory_scope_uses_targeted_merge
                        else (merged_plan.get("backend_targets") or [])
                    ),
                    *explicit_role_route_targets,
                ]
            )
        )
        merged_plan["backend_targets"] = merged_backend_targets
        inferred_non_page_targets = [
            path
            for path in (inferred_plan_result.get("target_files") or [])
            if not str(path).startswith("miniapp/app/static/")
        ]
        advisory_non_page_targets = [
            path
            for path in (advisory_plan_result.get("target_files") or [])
            if not str(path).startswith("miniapp/app/static/")
        ]
        if advisory_scope_uses_targeted_merge or advisory_has_explicit_surface:
            inferred_non_page_targets = [
                path
                for path in inferred_non_page_targets
                if path in runtime_bootstrap_targets
            ]
        merged_plan["target_files"] = list(
            dict.fromkeys(
                [
                    *(merged_plan.get("shared_files") or []),
                    *merged_backend_targets,
                    *merged_page_targets,
                    *inferred_non_page_targets,
                    *advisory_non_page_targets,
                ]
            )
        )
        if visual_only_patch:
            visual_read_targets = set(merged_page_targets)
            for role in page_target_roles:
                role_payload = current_roles.get(role)
                if not isinstance(role_payload, dict):
                    continue
                for page in (role_payload.get("pages") or []):
                    if not isinstance(page, dict):
                        continue
                    for key in ("file_path", "style_path", "script_path"):
                        path = page.get(key)
                        if isinstance(path, str) and path.startswith(f"miniapp/app/static/{role}/"):
                            visual_read_targets.add(path)
            merged_plan["files_to_read"] = [
                path
                for path in (merged_plan.get("files_to_read") or [])
                if isinstance(path, str)
                and (
                    path in visual_read_targets
                    or path.startswith("miniapp/app/static/shared/")
                )
            ]
        merged_plan["plan_gate_issues"] = list(advisory_plan_result.get("plan_gate_issues") or [])
        merged_plan["model"] = advisory_plan_result.get("model") or inferred_plan_result.get("model")
        merged_plan["strategy_reason"] = (
            str(advisory_plan_result.get("strategy_reason") or "").strip()
            or str(inferred_plan_result.get("strategy_reason") or "").strip()
            or "Prompt and template affordances shaped the writable surface before tool-owned generation."
        )
        merged_plan["write_strategy"] = advisory_plan_result.get("write_strategy") or inferred_plan_result.get("write_strategy") or "whole_file_build"
        merged_plan["scope_mode"] = scope_mode
        merged_plan["flow_mode"] = advisory_plan_result.get("flow_mode") or inferred_plan_result.get("flow_mode") or "multi_page"
        merged_plan["require_multi_page"] = bool(
            advisory_plan_result.get("require_multi_page")
            if "require_multi_page" in advisory_plan_result
            else inferred_plan_result.get("require_multi_page")
        )
        merged_plan["visual_only_patch"] = visual_only_patch
        merged_plan["ui_flow_patch"] = ui_flow_patch
        merged_plan["role_patch_kind"] = role_patch_kind
        merged_plan["suppress_role_route_targets"] = suppress_role_route_targets
        merged_plan["generation_clusters"] = list(self.service._build_generation_clusters(merged_plan["target_files"]) or [])
        role_scope = [
            role
            for role in ROLE_ORDER
            if role in current_roles or role in (merged_role_contract.get("roles") or {})
        ] or list(current_roles)
        merged_plan["execution_plan"] = dict(
            self.service._build_execution_plan(
                role_scope=role_scope,
                roles=current_roles,
                shared_files=list(merged_plan.get("shared_files") or []),
                backend_targets=merged_backend_targets,
                target_files=list(merged_plan["target_files"]),
                generation_clusters=merged_plan["generation_clusters"],
            )
        )
        return merged_role_contract, merged_plan

    @staticmethod
    def _page_signature(page: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(page.get("route_path") or "").strip(),
            str(page.get("file_path") or "").strip(),
            str(page.get("page_id") or "").strip(),
        )

    @staticmethod
    def _is_foundational_role_page(page: dict[str, Any]) -> bool:
        route_path = str(page.get("route_path") or "").strip()
        page_kind = str(page.get("page_kind") or "").strip().lower()
        return route_path in {"/", "/profile"} or page_kind in {"landing", "profile"}

    def _merge_role_page_graph_payload(
        self,
        *,
        role: str,
        existing_role_payload: dict[str, Any],
        advisory_role_payload: dict[str, Any],
    ) -> dict[str, Any]:
        merged_role_payload = deepcopy(existing_role_payload)
        advisory_pages = [
            deepcopy(page)
            for page in (advisory_role_payload.get("pages") or [])
            if isinstance(page, dict)
        ]
        if advisory_pages:
            existing_pages = [
                deepcopy(page)
                for page in (existing_role_payload.get("pages") or [])
                if isinstance(page, dict)
            ]
            seen_signatures = {self._page_signature(page) for page in advisory_pages}
            advisory_routes = {str(page.get("route_path") or "").strip() for page in advisory_pages}
            merged_pages = list(advisory_pages)
            for page in existing_pages:
                if not self._is_foundational_role_page(page):
                    continue
                if self._page_signature(page) in seen_signatures:
                    continue
                if str(page.get("route_path") or "").strip() in advisory_routes:
                    continue
                merged_pages.append(page)
            merged_role_payload["pages"] = merged_pages
        for key in ("entry_path", "landing_page_id", "routes_file"):
            advisory_value = advisory_role_payload.get(key)
            if advisory_value not in (None, "", []):
                merged_role_payload[key] = advisory_value
        return merged_role_payload

    @staticmethod
    def _page_graph_target_files(page_graph: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        for role_payload in (page_graph.get("roles") or {}).values():
            if not isinstance(role_payload, dict):
                continue
            routes_file = role_payload.get("routes_file")
            if isinstance(routes_file, str) and routes_file.strip():
                targets.append(routes_file)
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                for key in ("file_path", "style_path", "script_path"):
                    path = page.get(key)
                    if isinstance(path, str) and path.strip():
                        targets.append(path)
        return list(dict.fromkeys(targets))

    @staticmethod
    def _roles_for_static_targets(target_files: list[str]) -> list[str]:
        roles: list[str] = []
        for path in target_files:
            normalized = str(path or "").strip().replace("\\", "/")
            parts = normalized.split("/")
            if len(parts) >= 5 and parts[:4] == ["miniapp", "app", "static", parts[3]]:
                role = parts[3]
                if role in ROLE_ORDER:
                    roles.append(role)
        return list(dict.fromkeys(roles))

    def continue_generation_from_plan(
        self,
        *,
        workspace,
        workspace_id: str,
        job: JobRecord,
        request: GenerateRequest,
        draft_run_id: str,
        draft_source: Path,
        effective_prompt: str,
        grounded_spec,
        entity_contract: dict[str, Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        plan_result: dict[str, Any],
        execution_class: str,
        generation_mode,
        creative_direction: dict[str, Any],
        retrieval_ms: int,
        started_at: float,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord:
        return self.service.generation_normal_loop.run(
            workspace=workspace,
            workspace_id=workspace_id,
            job=job,
            request=request,
            draft_run_id=draft_run_id,
            draft_source=draft_source,
            effective_prompt=effective_prompt,
            grounded_spec=grounded_spec,
            entity_contract=entity_contract,
            role_scope=role_scope,
            role_contract=role_contract,
            plan_result=plan_result,
            execution_class=execution_class,
            generation_mode=generation_mode,
            creative_direction=creative_direction,
            retrieval_ms=retrieval_ms,
            started_at=started_at,
            should_stop=should_stop,
        )
