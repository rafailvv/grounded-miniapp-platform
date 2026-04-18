from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.models.domain import GenerateRequest, JobRecord

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationEntry:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def generate_with_thin_loop(
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
            "thin_loop_started",
            "Thin direct generation loop selected as the default path.",
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
            "thin_spec_compiler",
            "Thin loop compiled grounded spec before prompt-driven scaffold planning.",
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
        role_contract = dict(role_contract_result.get("role_contract") or self.service._minimal_role_contract(grounded_spec, role_scope))
        role_contract_issues = self.service._role_contract_gate_issues(
            role_contract,
            role_scope,
            scope_mode="whole_file_build",
        )
        if role_contract_result.get("fallback_reason"):
            self.service._append_trace(
                workspace_id,
                "role_contract_fallback",
                "Role contract analysis fell back to minimal guidance, but generation continues code-first.",
                {"reason": str(role_contract_result.get("fallback_reason") or "")},
            )
        if role_contract_issues:
            self.service._append_trace(
                workspace_id,
                "role_contract_soft_issues",
                "Role contract analysis produced soft guidance issues; continuing with prompt-driven planning.",
                {"issues": role_contract_issues},
            )
        self.service._append_event(job, "scaffold_ready", "Role guidance compiled as advisory input for file-first generation.")

        plan_result = self.service._resolve_code_plan(
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
        plan_error = str(plan_result.get("error") or "").strip()
        inferred_role_contract, inferred_plan_result = self.service._compile_prompt_to_scaffold(
            prompt=effective_prompt,
            grounded_spec=grounded_spec,
            role_scope=role_scope,
            workspace_tree=workspace_tree,
        )
        if plan_error or not plan_result.get("page_graph"):
            merged_roles = dict((role_contract.get("roles") or {}))
            for role, payload in (inferred_role_contract.get("roles") or {}).items():
                merged_roles.setdefault(role, payload)
            role_contract = {
                **inferred_role_contract,
                **role_contract,
                "roles": merged_roles,
            }
            plan_result = inferred_plan_result
            self.service._append_event(job, "scaffold_ready", "Prompt and template affordances filled the minimal executable route surface.")
            self.service._append_trace(
                workspace_id,
                "generation_plan_inferred",
                "Generation inferred the minimal executable route surface from prompt and template affordances.",
                {"reason": plan_error or "missing_page_graph", "inferred_target_files": len(plan_result["target_files"])},
            )
        else:
            self.service._append_event(job, "scaffold_ready", "Advisory generation targets and route surfaces are ready.")
            self.service._append_trace(
                workspace_id,
                "generation_plan_advisory",
                "Advisory generation targets were compiled before code generation.",
                {
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
        if inferred_plan_result.get("page_graph"):
            inferred_targets = list(inferred_plan_result.get("target_files") or [])
            inferred_backend_targets = list(inferred_plan_result.get("backend_targets") or [])
            inferred_shared_files = list(inferred_plan_result.get("shared_files") or [])
            inferred_files_to_read = list(inferred_plan_result.get("files_to_read") or [])
            current_graph = dict(plan_result.get("page_graph") or {})
            current_roles = dict(current_graph.get("roles") or {})
            for role, inferred_role_payload in ((inferred_plan_result.get("page_graph") or {}).get("roles") or {}).items():
                existing_role_payload = current_roles.get(role)
                if not isinstance(existing_role_payload, dict) or not (existing_role_payload.get("pages") or []):
                    current_roles[role] = inferred_role_payload
            current_graph["roles"] = current_roles
            plan_result["page_graph"] = current_graph
            plan_result["target_files"] = list(dict.fromkeys([*(plan_result.get("target_files") or []), *inferred_targets]))
            plan_result["backend_targets"] = list(dict.fromkeys([*(plan_result.get("backend_targets") or []), *inferred_backend_targets]))
            plan_result["shared_files"] = list(dict.fromkeys([*(plan_result.get("shared_files") or []), *inferred_shared_files]))
            plan_result["files_to_read"] = list(dict.fromkeys([*(plan_result.get("files_to_read") or []), *inferred_files_to_read]))
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
