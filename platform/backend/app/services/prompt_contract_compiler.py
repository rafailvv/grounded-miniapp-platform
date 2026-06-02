from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.openai_client import OpenAIClient
from app.models.common import GenerationMode
from app.models.domain import new_id
from app.models.prompt_contract import (
    PromptContract,
    PromptContractCompileReport,
    PromptContractRequirement,
    PromptContractScenario,
    PromptContractSection,
    ProductBlueprint,
)
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.miniapp_contract import MiniAppContract, MiniAppContractCompiler
from app.services.workflow_acceptance import (
    build_acceptance_contract,
    build_implementation_plan,
    derive_prompt_contract_analysis,
    normalized_generation_mode,
    orchestration_metadata_for_contract,
)


ROLE_ORDER = ("client", "specialist", "manager")


@dataclass
class PromptContractCompileResult:
    contract: PromptContract
    compile_report: PromptContractCompileReport
    product_blueprint: ProductBlueprint
    acceptance_contract: dict[str, Any]
    implementation_plan: dict[str, Any]
    orchestration: dict[str, Any]
    miniapp_contract: MiniAppContract | None
    prompt_analysis: dict[str, Any] | None
    prompt_analysis_usage: dict[str, Any]
    prompt_analysis_model: str | None


class PromptContractCompilerService:
    def __init__(
        self,
        *,
        store: StateStore,
        openai_client: OpenAIClient,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.store = store
        self.openai_client = openai_client
        self.event_journal_service = event_journal_service

    def compile(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str,
        generation_mode: GenerationMode | str | None,
        focused_edit_kind: str = "",
        model_profile: str | None = None,
        inherited_prompt_contract: dict[str, Any] | None = None,
        inherited_acceptance_contract: dict[str, Any] | None = None,
        source_run_id: str | None = None,
        contract_prompt: str | None = None,
    ) -> PromptContractCompileResult:
        self._journal(workspace_id, run_id, "prompt_contract.compile_started", {"intent": intent, "generation_mode": str(generation_mode or "")})
        if inherited_prompt_contract:
            result = self._compile_inherited(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=prompt,
                intent=intent,
                generation_mode=generation_mode,
                inherited_prompt_contract=inherited_prompt_contract,
                inherited_acceptance_contract=inherited_acceptance_contract,
                source_run_id=source_run_id,
            )
            self._persist_result(result)
            self._journal(workspace_id, run_id, "prompt_contract.compiled", result.compile_report.model_dump(mode="json", by_alias=True), source_ref=result.compile_report.prompt_contract_ref)
            return result

        mode_value = normalized_generation_mode(generation_mode) or GenerationMode.BALANCED.value
        intent_value = str(intent or "").strip().lower()
        requires_contract = (
            intent_value == "create"
            or str(focused_edit_kind or "").strip().lower() == "behavior_workflow_edit"
            or mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value, GenerationMode.PRODUCTION.value}
        )
        prompt_analysis: dict[str, Any] | None = None
        prompt_analysis_usage: dict[str, Any] = {}
        prompt_analysis_model: str | None = None
        if requires_contract:
            prompt_analysis, prompt_analysis_usage, prompt_analysis_model = self._analyze_prompt(
                prompt=prompt,
                generation_mode=generation_mode,
                model_profile=model_profile,
            )
        acceptance_contract = build_acceptance_contract(
            prompt=prompt,
            intent=intent_value,
            generation_mode=generation_mode,
            focused_edit_kind=focused_edit_kind,
            prompt_analysis=prompt_analysis,
        )
        if prompt_analysis_model == "fast-local-contract":
            self._set_analysis_source(acceptance_contract, "fast_local")
        orchestration = orchestration_metadata_for_contract(
            contract=acceptance_contract,
            generation_mode=generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        implementation_plan = build_implementation_plan(
            prompt=prompt,
            intent=intent_value,
            generation_mode=generation_mode,
            acceptance_contract=acceptance_contract,
            orchestration=orchestration,
            prompt_analysis=prompt_analysis,
        )
        if prompt_analysis_model == "fast-local-contract":
            self._set_analysis_source(implementation_plan, "fast_local")
        miniapp_contract: MiniAppContract | None = None
        if acceptance_contract.get("required"):
            miniapp_contract = MiniAppContractCompiler.compile(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=contract_prompt or prompt,
                intent=intent_value,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                implementation_plan=implementation_plan,
                prompt_analysis=prompt_analysis,
            )
            acceptance_contract = miniapp_contract.acceptance_summary
        miniapp_contract = None
        if acceptance_contract.get("required"):
            miniapp_contract = MiniAppContractCompiler.compile(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=prompt,
                intent=intent,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                implementation_plan=implementation_plan,
                prompt_analysis=None,
            )
            acceptance_contract = miniapp_contract.acceptance_summary
        contract = self._build_contract(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt=prompt,
            intent=intent_value,
            generation_mode=mode_value,
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            miniapp_contract=miniapp_contract,
            source_run_id=None,
        )
        product_blueprint = self._build_product_blueprint(contract)
        contract.product_blueprint = product_blueprint.model_dump(mode="json", by_alias=True)
        if not requires_contract and not acceptance_contract.get("required"):
            contract.status = "not_required"
            product_blueprint.status = "not_required"
        if acceptance_contract.get("blocking") or str(acceptance_contract.get("status") or "").startswith("blocked_"):
            contract.status = "blocked"
            product_blueprint.status = "blocked"
        contract.product_blueprint = product_blueprint.model_dump(mode="json", by_alias=True)
        compile_ref = f"prompt_contract_compile:{workspace_id}:{run_id}"
        report = PromptContractCompileReport(
            status="blocked" if contract.status == "blocked" else "not_required" if contract.status == "not_required" else "compiled",
            workspace_id=workspace_id,
            run_id=run_id,
            prompt_contract_ref=f"prompt_contract:{workspace_id}:{run_id}",
            acceptance_contract_ref=f"acceptance_contract:{workspace_id}:{run_id}" if acceptance_contract.get("required") or intent_value == "create" else None,
            product_blueprint_ref=f"product_blueprint:{workspace_id}:{run_id}",
            miniapp_contract_ref=f"miniapp_contract:{workspace_id}:{run_id}" if miniapp_contract is not None else None,
            contract_compile_ref=compile_ref,
            analysis_source=contract.analysis_source,
            analysis_model=prompt_analysis_model,
            blocking=contract.status == "blocked",
            issues=[str(item) for item in acceptance_contract.get("issues") or []],
            next_sequence=self._next_sequence("prompt_contract_compile"),
        )
        result = PromptContractCompileResult(
            contract=contract,
            compile_report=report,
            product_blueprint=product_blueprint,
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            orchestration=orchestration,
            miniapp_contract=miniapp_contract,
            prompt_analysis=prompt_analysis,
            prompt_analysis_usage=prompt_analysis_usage,
            prompt_analysis_model=prompt_analysis_model,
        )
        self._persist_result(result)
        self._journal(
            workspace_id,
            run_id,
            "prompt_contract.blocked" if contract.status == "blocked" else "prompt_contract.compiled",
            report.model_dump(mode="json", by_alias=True),
            source_ref=report.prompt_contract_ref,
        )
        return result

    def read(self, *, workspace_id: str, run_id: str) -> dict[str, Any]:
        ref = f"prompt_contract:{workspace_id}:{run_id}"
        payload = self.store.get("reports", ref)
        if isinstance(payload, dict):
            return payload
        return self.backfill_legacy(workspace_id=workspace_id, run_id=run_id)

    def list_for_workspace(self, workspace_id: str) -> dict[str, Any]:
        items = [
            payload
            for key, payload in self.store.items("reports")
            if str(key).startswith(f"prompt_contract:{workspace_id}:") and isinstance(payload, dict)
        ]
        items.sort(key=lambda item: str((item.get("compile_report") or {}).get("next_sequence") or ""))
        return {
            "schema": "grounded.prompt_contract_list.v1",
            "workspace_id": workspace_id,
            "items": items,
            "count": len(items),
            "next_sequence": self._next_sequence("prompt_contract_compile"),
        }

    def backfill_legacy(self, *, workspace_id: str, run_id: str) -> dict[str, Any]:
        acceptance_report = self.store.get("reports", f"acceptance_contract:{workspace_id}:{run_id}")
        acceptance_contract = (
            dict(acceptance_report.get("contract") or {})
            if isinstance(acceptance_report, dict) and isinstance(acceptance_report.get("contract"), dict)
            else {}
        )
        run_payload = self.store.get("runs", run_id)
        if not acceptance_contract and isinstance(run_payload, dict) and isinstance(run_payload.get("acceptance_contract"), dict):
            acceptance_contract = dict(run_payload["acceptance_contract"])
        implementation_plan = (
            dict(acceptance_report.get("implementation_plan") or {})
            if isinstance(acceptance_report, dict) and isinstance(acceptance_report.get("implementation_plan"), dict)
            else dict(run_payload.get("implementation_plan") or {}) if isinstance(run_payload, dict) else {}
        )
        contract = self._build_contract(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt=str((run_payload or {}).get("prompt") or ""),
            intent=str((run_payload or {}).get("intent") or ""),
            generation_mode=str((run_payload or {}).get("generation_mode") or ""),
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            miniapp_contract=None,
            source_run_id=None,
        )
        product_blueprint = self._build_product_blueprint(contract)
        contract.product_blueprint = product_blueprint.model_dump(mode="json", by_alias=True)
        report = PromptContractCompileReport(
            status="compiled" if acceptance_contract else "not_required",
            workspace_id=workspace_id,
            run_id=run_id,
            prompt_contract_ref=f"prompt_contract:{workspace_id}:{run_id}",
            acceptance_contract_ref=f"acceptance_contract:{workspace_id}:{run_id}" if acceptance_contract else None,
            product_blueprint_ref=f"product_blueprint:{workspace_id}:{run_id}",
            analysis_source=(acceptance_contract.get("prompt_hints") or {}).get("analysis_source") if isinstance(acceptance_contract.get("prompt_hints"), dict) else None,
            next_sequence=self._next_sequence("prompt_contract_compile"),
        )
        payload = self._response_payload(contract=contract, report=report, miniapp_contract=None)
        self.store.upsert("reports", report.prompt_contract_ref, payload)
        self.store.upsert("reports", report.product_blueprint_ref, product_blueprint.model_dump(mode="json", by_alias=True))
        return payload

    def _compile_inherited(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str,
        generation_mode: GenerationMode | str | None,
        inherited_prompt_contract: dict[str, Any],
        inherited_acceptance_contract: dict[str, Any] | None,
        source_run_id: str | None,
    ) -> PromptContractCompileResult:
        inherited_contract = dict(inherited_prompt_contract.get("contract") or inherited_prompt_contract)
        acceptance_contract = dict(
            inherited_acceptance_contract
            or inherited_contract.get("acceptance_contract")
            or inherited_prompt_contract.get("acceptance_contract")
            or {}
        )
        if acceptance_contract:
            acceptance_contract.update(
                {
                    "required": True,
                    "inherited_from_run_id": acceptance_contract.get("inherited_from_run_id") or source_run_id,
                    "continued_from_run_id": acceptance_contract.get("continued_from_run_id") or source_run_id,
                    "contract_source_run_id": acceptance_contract.get("contract_source_run_id") or source_run_id,
                    "repair_continuation": True,
                }
            )
        implementation_plan = dict(inherited_contract.get("implementation_plan") or inherited_prompt_contract.get("implementation_plan") or {})
        inherited_blueprint = dict(inherited_contract.get("product_blueprint") or inherited_prompt_contract.get("product_blueprint") or {})
        implementation_plan["repair_continuation"] = {
            **dict(implementation_plan.get("repair_continuation") or {}),
            "enabled": True,
            "source_run_id": source_run_id,
            "contract_source_run_id": source_run_id,
            "source_prompt_preserved": True,
            "contract_inherited": True,
        }
        miniapp_contract = None
        if acceptance_contract.get("required"):
            miniapp_contract = MiniAppContractCompiler.compile(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=prompt,
                intent=intent,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                implementation_plan=implementation_plan,
                prompt_analysis=None,
            )
            acceptance_contract = miniapp_contract.acceptance_summary
        contract = self._build_contract(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt=prompt,
            intent=intent,
            generation_mode=normalized_generation_mode(generation_mode),
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            miniapp_contract=miniapp_contract,
            source_run_id=source_run_id,
        )
        contract.status = "inherited"
        product_blueprint = self._build_product_blueprint(contract)
        if inherited_blueprint:
            product_blueprint.refs = {**dict(product_blueprint.refs or {}), "inherited_product_blueprint": inherited_blueprint.get("blueprint_id") or inherited_blueprint.get("refs")}
        product_blueprint.status = "inherited"
        contract.product_blueprint = product_blueprint.model_dump(mode="json", by_alias=True)
        report = PromptContractCompileReport(
            status="inherited",
            workspace_id=workspace_id,
            run_id=run_id,
            prompt_contract_ref=f"prompt_contract:{workspace_id}:{run_id}",
            acceptance_contract_ref=f"acceptance_contract:{workspace_id}:{run_id}",
            product_blueprint_ref=f"product_blueprint:{workspace_id}:{run_id}",
            miniapp_contract_ref=f"miniapp_contract:{workspace_id}:{run_id}" if miniapp_contract is not None else None,
            analysis_source=contract.analysis_source,
            blocking=False,
            next_sequence=self._next_sequence("prompt_contract_compile"),
        )
        return PromptContractCompileResult(
            contract=contract,
            compile_report=report,
            product_blueprint=product_blueprint,
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            orchestration=orchestration_metadata_for_contract(contract=acceptance_contract, generation_mode=generation_mode, focused_edit_kind="repair_continuation"),
            miniapp_contract=miniapp_contract,
            prompt_analysis=None,
            prompt_analysis_usage={},
            prompt_analysis_model=None,
        )

    def _analyze_prompt(
        self,
        *,
        prompt: str,
        generation_mode: GenerationMode | str | None,
        model_profile: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        mode_value = normalized_generation_mode(generation_mode)
        if mode_value in {GenerationMode.FAST.value, GenerationMode.BASIC.value} or str(generation_mode).lower() in {"fast", "basic", "generationmode.fast", "generationmode.basic"}:
            return derive_prompt_contract_analysis(prompt), {}, "fast-local-contract"
        if not self.openai_client.enabled:
            raise RuntimeError("LLM prompt analysis is required before creating a workflow run.")
        with self.openai_client.routing_context(model_profile=model_profile, generation_mode=generation_mode):
            prompt_analysis = self.openai_client.analyze_miniapp_prompt(
                prompt=prompt,
                generation_mode=generation_mode,
                model_profile=model_profile,
            )
        usage = dict((prompt_analysis or {}).pop("_llm_usage", {}) or {})
        model = str((prompt_analysis or {}).pop("_llm_model", "") or "") or None
        return prompt_analysis, usage, model

    @staticmethod
    def _set_analysis_source(payload: dict[str, Any], source: str) -> None:
        for key in ("prompt_hints", "api_contract"):
            value = payload.get(key)
            if isinstance(value, dict):
                value["analysis_source"] = source

    def _build_contract(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str,
        generation_mode: str,
        acceptance_contract: dict[str, Any],
        implementation_plan: dict[str, Any],
        miniapp_contract: MiniAppContract | None,
        source_run_id: str | None,
    ) -> PromptContract:
        hints = acceptance_contract.get("prompt_hints") if isinstance(acceptance_contract.get("prompt_hints"), dict) else {}
        resources = miniapp_contract.resources if miniapp_contract is not None else []
        entities = [
            {
                "entity_id": resource.resource_id,
                "slug": resource.slug,
                "name": resource.name,
                "display_name": resource.display_name,
                "fields": resource.fields,
                "source_roles": resource.source_roles,
                "update_roles": resource.update_roles,
                "observer_roles": resource.observer_roles,
            }
            for resource in resources
        ] or [
            {"name": item, "source": "prompt_hints"}
            for item in (hints.get("resource_hints") or ([hints.get("resource_hint")] if hints.get("resource_hint") else []))
            if str(item or "").strip()
        ]
        fields = self._fields(hints=hints, resources=resources)
        workflows = [dict(flow) for flow in acceptance_contract.get("flows") or [] if isinstance(flow, dict)]
        screen_plan = implementation_plan.get("routeable_screen_plan") or (implementation_plan.get("ui_contract") or {}).get("routeable_screen_plan") or (acceptance_contract.get("page_contract") or {}).get("routeable_screen_plan") or {}
        screens = self._screens(screen_plan)
        endpoints = [endpoint.model_dump(mode="json") for endpoint in (miniapp_contract.endpoints if miniapp_contract is not None else [])]
        api_contract = dict(implementation_plan.get("api_contract") or acceptance_contract.get("api_contract") or {})
        if endpoints:
            api_contract["required_endpoints"] = endpoints
        persistence = {
            "must_persist": bool(api_contract.get("must_persist", acceptance_contract.get("required"))),
            "must_support_update": bool(api_contract.get("must_support_update") or (acceptance_contract.get("features") or {}).get("workflow_update")),
            "refresh_persistence": bool((acceptance_contract.get("features") or {}).get("refresh_persistence")),
            "no_seed_or_mock_records": True,
        }
        visual = [
            {
                "requirement": "Role screens fit Telegram mini-app widths around 360-430px without horizontal overflow or blocking overlap.",
                "source": "platform_mobile_contract",
            },
            {
                "requirement": "Use polished product labels from the prompt, not internal role/API labels.",
                "source": "prompt_contract",
            },
        ]
        scenarios = self._scenarios(acceptance_contract)
        status = "blocked" if acceptance_contract.get("blocking") or str(acceptance_contract.get("status") or "").startswith("blocked_") else "planned" if acceptance_contract.get("required") else "not_required"
        contract = PromptContract(
            contract_id=new_id("prompt_contract"),
            status=status,
            workspace_id=workspace_id,
            run_id=run_id,
            source_run_id=source_run_id,
            prompt_summary=str(hints.get("prompt_summary") or prompt or "")[:1200],
            intent=str(intent or ""),
            generation_mode=str(generation_mode or ""),
            analysis_source=hints.get("analysis_source"),
            analysis_status=hints.get("analysis_status"),
            roles=list(acceptance_contract.get("roles") or ROLE_ORDER),
            entities=entities,
            fields=fields,
            workflows=workflows,
            screens=screens,
            api=api_contract,
            persistence=persistence,
            visual_requirements=visual,
            acceptance_scenarios=scenarios,
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            refs={},
        )
        contract.sections = self._sections(contract)
        return contract

    @staticmethod
    def _fields(*, hints: dict[str, Any], resources: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for resource in resources:
            for field in resource.fields:
                result.append({"name": field, "entity": resource.slug, "source": "miniapp_contract"})
        if result:
            return result
        for field in hints.get("field_hints") or []:
            if str(field or "").strip():
                result.append({"name": str(field), "source": "prompt_hints"})
        role_fields = hints.get("role_field_hints") if isinstance(hints.get("role_field_hints"), dict) else {}
        for role, values in role_fields.items():
            for field in values or []:
                if str(field or "").strip():
                    result.append({"name": str(field), "role": role, "source": "prompt_hints"})
        return result

    @staticmethod
    def _screens(screen_plan: dict[str, Any]) -> list[dict[str, Any]]:
        roles = screen_plan.get("roles") if isinstance(screen_plan.get("roles"), dict) else {}
        result: list[dict[str, Any]] = []
        for role, items in roles.items():
            for index, item in enumerate(items or []):
                if isinstance(item, dict):
                    result.append({"screen_id": f"{role}_{index + 1}", "role": role, **item})
        return result

    @staticmethod
    def _scenarios(acceptance_contract: dict[str, Any]) -> list[PromptContractScenario]:
        scenarios: list[PromptContractScenario] = []
        for flow in acceptance_contract.get("flows") or []:
            if not isinstance(flow, dict):
                continue
            scenarios.append(
                PromptContractScenario(
                    scenario_id=str(flow.get("id") or f"flow_{len(scenarios) + 1}"),
                    title=str(flow.get("title") or flow.get("id") or "Prompt-derived acceptance flow"),
                    role=None,
                    steps=[dict(item) for item in flow.get("steps") or [] if isinstance(item, dict)],
                    required_checks=[str(item) for item in flow.get("required_tests") or []],
                )
            )
        return scenarios

    @staticmethod
    def _build_product_blueprint(contract: PromptContract) -> ProductBlueprint:
        required_checks = sorted(
            {
                check
                for scenario in contract.acceptance_scenarios
                for check in scenario.required_checks
                if str(check or "").strip()
            }
        )
        acceptance_proof = {
            "status": "planned" if contract.status in {"planned", "inherited"} else contract.status,
            "scenarios": [scenario.model_dump(mode="json") for scenario in contract.acceptance_scenarios],
            "required_checks": required_checks,
            "browser_proof_required": contract.status != "not_required",
            "acceptance_contract_required": bool((contract.acceptance_contract or {}).get("required")),
            "proof_refs": {
                "acceptance_contract": f"acceptance_contract:{contract.workspace_id}:{contract.run_id}",
                "prompt_contract": f"prompt_contract:{contract.workspace_id}:{contract.run_id}",
            },
        }
        return ProductBlueprint(
            blueprint_id=new_id("blueprint"),
            status=contract.status,
            workspace_id=contract.workspace_id,
            run_id=contract.run_id,
            source_run_id=contract.source_run_id,
            prompt_summary=contract.prompt_summary,
            roles=list(contract.roles),
            entities=list(contract.entities),
            workflows=list(contract.workflows),
            api=dict(contract.api),
            persistence=dict(contract.persistence),
            screens=list(contract.screens),
            acceptance_proof=acceptance_proof,
            refs={
                "prompt_contract_ref": f"prompt_contract:{contract.workspace_id}:{contract.run_id}",
                "acceptance_contract_ref": f"acceptance_contract:{contract.workspace_id}:{contract.run_id}",
            },
        )

    @staticmethod
    def _sections(contract: PromptContract) -> list[PromptContractSection]:
        section_payloads = {
            "roles": [{"role": role} for role in contract.roles],
            "entities": contract.entities,
            "fields": contract.fields,
            "workflows": contract.workflows,
            "screens": contract.screens,
            "api": [contract.api] if contract.api else [],
            "persistence": [contract.persistence],
            "visual": contract.visual_requirements,
            "acceptance": [scenario.model_dump(mode="json") for scenario in contract.acceptance_scenarios],
        }
        sections: list[PromptContractSection] = []
        for key, items in section_payloads.items():
            status = "blocked" if contract.status == "blocked" and key in {"entities", "workflows", "acceptance"} else "not_required" if contract.status == "not_required" else "planned"
            sections.append(
                PromptContractSection(
                    key=key,  # type: ignore[arg-type]
                    status=status,  # type: ignore[arg-type]
                    items=list(items),
                    requirements=[
                        PromptContractRequirement(
                            requirement_id=f"{key}.required",
                            category=key,
                            text=f"{key} section must be satisfied by product source and proof.",
                            required=contract.status != "not_required",
                        )
                    ],
                )
            )
        return sections

    def _persist_result(self, result: PromptContractCompileResult) -> None:
        payload = self._response_payload(contract=result.contract, report=result.compile_report, miniapp_contract=result.miniapp_contract)
        self.store.upsert("reports", result.compile_report.prompt_contract_ref, payload)
        if result.compile_report.product_blueprint_ref:
            self.store.upsert("reports", result.compile_report.product_blueprint_ref, result.product_blueprint.model_dump(mode="json", by_alias=True))
        self.store.upsert("reports", f"prompt_contract_compile:{result.contract.workspace_id}:{result.contract.run_id}", result.compile_report.model_dump(mode="json", by_alias=True))
        self.store.upsert("reports", f"prompt_contract_compile:{result.compile_report.next_sequence}", result.compile_report.model_dump(mode="json", by_alias=True))

    @staticmethod
    def _response_payload(*, contract: PromptContract, report: PromptContractCompileReport, miniapp_contract: MiniAppContract | None) -> dict[str, Any]:
        return {
            "schema": "grounded.prompt_contract_report.v1",
            "status": contract.status,
            "workspace_id": contract.workspace_id,
            "run_id": contract.run_id,
            "contract": contract.model_dump(mode="json", by_alias=True),
            "compile_report": report.model_dump(mode="json", by_alias=True),
            "product_blueprint": contract.product_blueprint,
            "product_blueprint_ref": report.product_blueprint_ref,
            "acceptance_contract": contract.acceptance_contract,
            "acceptance_contract_ref": report.acceptance_contract_ref,
            "miniapp_contract_ref": report.miniapp_contract_ref,
            "miniapp_contract": miniapp_contract.model_dump(mode="json") if miniapp_contract is not None else None,
            "next_sequence": report.next_sequence,
        }

    def _next_sequence(self, prefix: str) -> int:
        return 1 + sum(1 for key, _payload in self.store.items("reports") if str(key).startswith(f"{prefix}:") and str(key).split(":")[-1].isdigit())

    def _journal(self, workspace_id: str, run_id: str, event_type: str, payload: dict[str, Any], *, source_ref: str | None = None) -> None:
        if self.event_journal_service is None:
            return
        try:
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                summary=event_type,
                source_ref=source_ref,
                idempotency_key=f"{event_type}:{run_id}:{source_ref or 'start'}",
            )
        except Exception:
            return
