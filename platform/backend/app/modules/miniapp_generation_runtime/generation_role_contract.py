from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import ROLE_ORDER
from app.services.workspace.service import json_dumps

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationRoleContract:
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def resolve_role_contract(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        intent: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.FAST or self.should_use_compiled_role_contract(
            prompt=prompt,
            role_scope=role_scope,
            intent=intent,
            generation_mode=generation_mode,
        ):
            return {"role_contract": self.compiled_role_contract(grounded_spec, role_scope)}
        try:
            payload = self.service._generate_structured_with_retry(
                role="code_plan",
                schema_name="role_contract_v1",
                schema=self.role_contract_schema(),
                system_prompt=self.role_contract_system_prompt(),
                user_prompt=self.role_contract_user_prompt(
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    intent=intent,
                    creative_direction=creative_direction,
                ),
            )
            normalized = self.service._normalize_model_payload(payload["payload"])
            role_contract = self.normalize_role_contract(normalized, role_scope)
            return {"role_contract": role_contract, "model": payload["model"]}
        except Exception as exc:
            return {"error": f"Role architecture analysis failed: {exc}"}

    def should_use_compiled_role_contract(
        self,
        *,
        prompt: str,
        role_scope: list[str],
        intent: str,
        generation_mode: GenerationMode,
    ) -> bool:
        if generation_mode != GenerationMode.BALANCED:
            return False
        if self.service._scope_mode(intent, prompt, role_scope) == "minimal_patch":
            return True
        lowered = prompt.lower()
        if len(role_scope) == len(ROLE_ORDER) and intent in {"create", "refine"}:
            simple_markers = ("simple", "basic", "minimal", "fast", "quick draft", "template-safe")
            return any(marker in lowered for marker in simple_markers)
        return False

    def normalize_role_contract(self, payload: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles_raw = payload.get("roles")
        if not isinstance(roles_raw, list):
            raise ValueError("Role contract is missing the roles array.")
        roles: dict[str, dict[str, Any]] = {}
        for item in roles_raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in role_scope:
                continue
            roles[role] = {
                "role": role,
                "responsibility": str(item.get("responsibility") or "").strip(),
                "entry_goal": str(item.get("entry_goal") or "").strip(),
                "primary_jobs": self.service._normalize_string_list(item.get("primary_jobs")),
                "key_entities": self.service._normalize_string_list(item.get("key_entities")),
                "ui_style_notes": self.service._normalize_string_list(item.get("ui_style_notes")),
                "success_states": self.service._normalize_string_list(item.get("success_states")),
                "must_differ_from": [
                    value for value in self.service._normalize_string_list(item.get("must_differ_from")) if value in ROLE_ORDER and value != role
                ],
            }
        return {
            "app_title": str(payload.get("app_title") or "").strip(),
            "app_summary": str(payload.get("app_summary") or "").strip(),
            "shared_entities": self.service._normalize_string_list(payload.get("shared_entities")),
            "shared_logic": self.service._normalize_string_list(payload.get("shared_logic")),
            "roles": roles,
        }

    def compiled_role_contract(self, grounded_spec: GroundedSpecModel, role_scope: list[str]) -> dict[str, Any]:
        entities = [entity.name for entity in grounded_spec.domain_entities if entity.name]
        flows = grounded_spec.user_flows
        roles: dict[str, dict[str, Any]] = {}
        for role in role_scope:
            actor = next((item for item in grounded_spec.actors if item.role == role), None)
            role_flows = [flow for flow in flows if any(step.actor_id == getattr(actor, "actor_id", "") for step in flow.steps)]
            ui_requirements = [
                requirement
                for requirement in grounded_spec.ui_requirements
                if role in (f"{requirement.description} {requirement.screen_hint or ''}".lower())
            ]
            primary_jobs = [flow.name for flow in role_flows[:4] if flow.name]
            key_entities = entities[:4]
            ui_style_notes = [item.description for item in ui_requirements[:3] if item.description]
            success_states = [
                criterion
                for flow in role_flows[:2]
                for criterion in flow.acceptance_criteria[:1]
                if criterion
            ] or primary_jobs[:2]
            roles[role] = {
                "role": role,
                "responsibility": getattr(actor, "description", None) or f"{role.capitalize()} workflow execution.",
                "entry_goal": primary_jobs[0] if primary_jobs else f"Open the {role} workspace and continue the main flow.",
                "primary_jobs": primary_jobs or [f"Handle the main {role} flow."],
                "key_entities": key_entities,
                "ui_style_notes": ui_style_notes or [f"Keep {role}-specific actions visible above generic metrics."],
                "success_states": success_states or [f"{role.capitalize()} completes the intended task without cross-role confusion."],
                "must_differ_from": [candidate for candidate in ROLE_ORDER if candidate in role_scope and candidate != role],
            }
        return {
            "app_title": grounded_spec.product_goal[:80] if grounded_spec.product_goal else "Generated mini-app",
            "app_summary": grounded_spec.product_goal or "Generated role-aware mini-app workspace.",
            "shared_entities": entities[:6],
            "shared_logic": [flow.name for flow in flows[:4] if flow.name],
            "roles": roles,
        }

    @staticmethod
    def role_contract_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "app_title": {"type": "string"},
                "app_summary": {"type": "string"},
                "shared_entities": {"type": "array", "items": {"type": "string"}},
                "shared_logic": {"type": "array", "items": {"type": "string"}},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": list(ROLE_ORDER)},
                            "responsibility": {"type": "string"},
                            "entry_goal": {"type": "string"},
                            "primary_jobs": {"type": "array", "items": {"type": "string"}},
                            "key_entities": {"type": "array", "items": {"type": "string"}},
                            "ui_style_notes": {"type": "array", "items": {"type": "string"}},
                            "success_states": {"type": "array", "items": {"type": "string"}},
                            "must_differ_from": {"type": "array", "items": {"type": "string", "enum": list(ROLE_ORDER)}},
                        },
                        "required": [
                            "role",
                            "responsibility",
                            "entry_goal",
                            "primary_jobs",
                            "key_entities",
                            "ui_style_notes",
                            "success_states",
                            "must_differ_from",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["app_title", "app_summary", "shared_entities", "shared_logic", "roles"],
            "additionalProperties": False,
        }

    @staticmethod
    def role_contract_system_prompt() -> str:
        prompt = (
            "You are the role analyst for a real mini-app coding workspace. "
            "Before planning files, separate what client, specialist, and manager each truly own. "
            "Do not collapse roles into relabeled versions of the same surface."
        )
        GenerationService._assert_english_control_text(prompt)
        return prompt

    def role_contract_user_prompt(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        intent: str,
        creative_direction: dict[str, Any],
    ) -> str:
        return json_dumps(
            {
                "task": "Analyze role boundaries before page planning",
                "prompt": prompt,
                "intent": intent,
                "role_scope": role_scope,
                "grounded_spec": grounded_spec.model_dump(mode="json"),
                "doc_refs": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in doc_refs],
                "creative_direction": creative_direction,
                "rules": [
                    "Explain what the client does, what the specialist does, and what the manager does before thinking about files.",
                    "Separate responsibilities, actions, and success states across roles.",
                    "If the prompt is a targeted edit, keep the role analysis narrow and avoid redefining unrelated areas.",
                ],
            }
        )

    @staticmethod
    def role_contract_gate_issues(role_contract: dict[str, Any], role_scope: list[str], *, scope_mode: str) -> list[str]:
        issues: list[str] = []
        roles = role_contract.get("roles") or {}
        normalized_responsibilities: list[str] = []
        for role in role_scope:
            payload = roles.get(role)
            if not isinstance(payload, dict):
                issues.append(f"{role} is missing from the role contract.")
                continue
            responsibility = str(payload.get("responsibility") or "").strip()
            jobs = payload.get("primary_jobs") or []
            if not responsibility:
                issues.append(f"{role} is missing a concrete responsibility.")
            if not jobs:
                issues.append(f"{role} is missing primary jobs.")
            normalized_responsibilities.append(re.sub(r"\s+", " ", responsibility.lower()))
        if scope_mode == "minimal_patch":
            return issues
        if len(normalized_responsibilities) > 1 and len(set(normalized_responsibilities)) == 1:
            issues.append("All selected roles still have the same responsibility in the role contract.")
        return issues
