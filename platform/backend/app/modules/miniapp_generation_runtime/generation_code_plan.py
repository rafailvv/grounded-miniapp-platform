from __future__ import annotations

import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodePlan(MiniappGenerationRuntimeOwner):
    def _resolve_code_plan(
        self,
        *,
        workspace_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        intent: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        scope_mode = self._scope_mode(intent, prompt, role_scope)
        require_multi_page = self._requires_multi_page(prompt, grounded_spec, role_scope, intent)
        strategy_reason = self._strategy_reason(intent, prompt, role_scope, require_multi_page=require_multi_page)
        workspace_tree = self.workspace_service.file_tree(workspace_id)
        try:
            payload = self._generate_code_plan_sections_with_timeout(
                timeout_seconds=float(self.CODE_PLAN_TOTAL_TIMEOUT_SECONDS),
                workspace_id=workspace_id,
                prompt=prompt,
                grounded_spec=grounded_spec,
                doc_refs=doc_refs,
                role_scope=role_scope,
                role_contract=role_contract,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
            normalized = self._normalize_model_payload(payload["payload"])
            planned = self._normalize_page_plan(
                normalized,
                role_scope=role_scope,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
            )
            plan_gate_issues = self._page_graph_gate_issues(
                planned["page_graph"],
                role_scope,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                require_business_pages=False,
            )
            planned["write_strategy"] = scope_mode
            planned["strategy_reason"] = strategy_reason
            planned["model"] = payload["model"]
            planned["plan_gate_issues"] = plan_gate_issues
            planned["require_business_pages"] = False
            return planned
        except Exception as exc:
            self._append_trace(
                workspace_id,
                "code_plan_advisory_failed",
                "Advisory code plan failed; generation will continue from prompt, template affordances, and tool exploration.",
                {"error": str(exc)},
            )
            return {
                "summary": "",
                "flow_mode": "multi_page" if require_multi_page else "single_page",
                "files_to_read": [],
                "target_files": [],
                "shared_files": [],
                "backend_targets": [],
                "generation_clusters": [],
                "active_role_scope": [],
                "execution_plan": {},
                "planner_contract_enrichment": {"proactive_backend_targets": []},
                "page_graph": {"roles": {}},
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "write_strategy": scope_mode,
                "strategy_reason": strategy_reason,
                "model": "code-plan-advisory-missing",
                "plan_gate_issues": [],
                "require_business_pages": False,
                "error": f"Page graph planning failed: {exc}",
            }

    def _generate_code_plan_sections_with_timeout(
        self,
        *,
        timeout_seconds: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="code-plan-total")
        future = self._submit_with_context(executor, self._generate_code_plan_sections, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"Timed out waiting for code plan generation after {int(timeout_seconds)}s."
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    def _generate_code_plan_sections(
        self,
        *,
        workspace_id: str,
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
    ) -> dict[str, Any]:
        sections_started = time.perf_counter()
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="code-plan")
        futures = {
            "graph": self._submit_with_context(
                executor,
                self._generate_structured_with_retry,
                role="code_plan",
                schema_name="page_graph_structure_v1",
                schema=self._code_plan_partial_schema(["summary", "flow_mode", "page_graph"]),
                system_prompt=self._code_plan_section_system_prompt("Page graph and route structure"),
                user_prompt=self._code_plan_section_user_prompt(
                    section_id="graph",
                    section_title="Page graph and route structure",
                    section_contract=[
                        "Return the real page graph, role routes, page purposes, primary actions, and handoff paths.",
                        "Keep role surfaces distinct and multi-page when required.",
                        "Do not decide final file-read lists in this section.",
                    ],
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    require_multi_page=require_multi_page,
                    workspace_tree=workspace_tree,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                ),
            ),
            "targeting": self._submit_with_context(
                executor,
                self._generate_structured_with_retry,
                role="code_plan",
                schema_name="page_graph_targeting_v1",
                schema=self._code_plan_partial_schema(["files_to_read", "target_files", "shared_files", "backend_targets"]),
                system_prompt=self._code_plan_section_system_prompt("File targeting and read set"),
                user_prompt=self._code_plan_section_user_prompt(
                    section_id="targeting",
                    section_title="File targeting and read set",
                    section_contract=[
                        "Return only read-set and file-target lists.",
                        "Target files must stay minimal for minimal_patch requests.",
                        "Use the page graph implied by the request and role contract, but do not re-emit full page definitions.",
                    ],
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    require_multi_page=require_multi_page,
                    workspace_tree=workspace_tree,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                ),
            ),
        }
        section_timeout = float(self.CODE_PLAN_SECTION_TIMEOUT_SECONDS)
        completed, pending = wait(set(futures.values()), timeout=section_timeout, return_when=ALL_COMPLETED)
        section_payloads: dict[str, dict[str, Any]] = {}
        section_errors: dict[str, str] = {}
        try:
            for section_name, future in futures.items():
                if future in pending:
                    section_errors[section_name] = "timeout"
                    continue
                try:
                    section_payloads[section_name] = future.result()
                except Exception as exc:
                    section_errors[section_name] = str(exc)
            if pending:
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=False, cancel_futures=False)
        finally:
            if pending:
                for future in pending:
                    future.cancel()

        if section_errors:
            graph_payload = section_payloads.get("graph")
            if graph_payload is None:
                raise RuntimeError(
                    "Code plan generation returned incomplete sections without a valid agent response: "
                    f"{section_errors}"
                )
            merged_payload = self._normalize_model_payload(graph_payload["payload"])
            targeting_payload = section_payloads.get("targeting")
            if targeting_payload is not None:
                merged_payload.update(self._normalize_model_payload(targeting_payload["payload"]))
            else:
                merged_payload.setdefault("files_to_read", [])
                merged_payload.setdefault("target_files", [])
                merged_payload.setdefault("shared_files", [])
                merged_payload.setdefault("backend_targets", [])
            winning_model = str(
                (targeting_payload or {}).get("model")
                or graph_payload.get("model")
                or "code-plan-partial"
            )
            self._append_trace(
                workspace_id,
                "code_plan_sections_partial_merge",
                "Code plan section timed out; merged successful sections without deterministic planner fallback.",
                {
                    "duration_ms": int((time.perf_counter() - sections_started) * 1000),
                    "section_errors": section_errors,
                    "section_payloads": sorted(section_payloads.keys()),
                },
            )
            return {
                "model": winning_model,
                "payload": merged_payload,
                "response_mode": "code_plan_sections_partial_merge",
            }

        self._append_trace(
            workspace_id,
            "code_plan_sections_parallel",
            "Code plan graph and targeting sections completed in parallel.",
            {
                "duration_ms": int((time.perf_counter() - sections_started) * 1000),
                "sections": ["graph", "targeting"],
            },
        )
        graph_payload_normalized = self._normalize_model_payload(section_payloads["graph"]["payload"])
        targeting_payload_normalized = self._normalize_model_payload(section_payloads["targeting"]["payload"])
        merged_payload = {**graph_payload_normalized, **targeting_payload_normalized}
        return {
            "model": (
                section_payloads.get("targeting", {}).get("model")
                or section_payloads.get("graph", {}).get("model")
                or "code-plan-sections"
            ),
            "payload": merged_payload,
            "response_mode": "code_plan_sections",
        }
