from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.models.domain import GenerateRequest, JobRecord
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
        self.service._append_event(job, "building_scaffold", "Building a prompt-driven generation plan on top of the minimal template bootstrap.")
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
            "Grounded spec compiled before prompt-driven scaffold planning.",
            {"llm_spec_stage_removed": True},
        )
        grounded_spec = self.service._stabilize_grounded_spec(grounded_spec)
        self.service._store_report(f"spec:{workspace_id}", grounded_spec.model_dump(mode="json"))
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

        workspace_tree = self.service.workspace_service.file_tree(workspace_id, run_id=draft_run_id)
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
        self.service._append_event(job, "scaffold_ready", "Role guidance compiled as advisory input for file-first generation.")

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
        inferred_role_contract, inferred_plan_result = self.service._compile_prompt_to_scaffold(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            workspace_tree=workspace_tree,
        )
        role_contract, plan_result = self._merge_advisory_generation_inputs(
            role_contract=role_contract,
            inferred_role_contract=inferred_role_contract,
            advisory_plan_result=advisory_plan_result,
            inferred_plan_result=inferred_plan_result,
        )
        plan_error = str(advisory_plan_result.get("error") or "").strip()
        self.service._append_event(job, "scaffold_ready", "Bootstrap targets and advisory generation hints are ready.")
        self.service._append_trace(
            workspace_id,
            "generation_plan_advisory",
            "Normal generation merged prompt/template inference with advisory planner hints before tool-owned code generation.",
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
            "Bootstrap targets and advisory generation inputs were prepared before code generation.",
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
        minimal_patch_uses_advisory_scope = bool(
            scope_mode == "minimal_patch" and (advisory_page_targets or advisory_backend_targets)
        )
        merged_page_targets = (
            list(dict.fromkeys(advisory_page_targets))
            if minimal_patch_uses_advisory_scope and advisory_page_targets
            else self._page_graph_target_files(current_graph)
        )
        for key in ("backend_targets", "shared_files", "files_to_read"):
            merged_plan[key] = list(
                dict.fromkeys(
                    [
                        *(inferred_plan_result.get(key) or []),
                        *(advisory_plan_result.get(key) or []),
                    ]
                )
            )
        page_target_roles = set(self._roles_for_static_targets(merged_page_targets))
        explicit_role_route_targets = [
            str(role_payload.get("routes_file"))
            for role, role_payload in current_roles.items()
            if isinstance(role_payload, dict)
            and isinstance(role_payload.get("routes_file"), str)
            and (
                not minimal_patch_uses_advisory_scope
                or role in page_target_roles
            )
        ]
        merged_backend_targets = list(
            dict.fromkeys(
                [
                    *(
                        advisory_backend_targets
                        if minimal_patch_uses_advisory_scope
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
        if minimal_patch_uses_advisory_scope:
            inferred_non_page_targets = []
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
