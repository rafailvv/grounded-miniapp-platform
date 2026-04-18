from __future__ import annotations

from typing import Any

from app.models.common import PreviewProfile, TargetPlatform
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps


class GroundedSpecPromptsRuntime:
    @staticmethod
    def _grounded_spec_outline_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product_goal": {"type": "string"},
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "responsibility": {"type": "string"},
                            "primary_actions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["role", "responsibility", "primary_actions"],
                        "additionalProperties": False,
                    },
                },
                "entities": {"type": "array", "items": {"type": "string"}},
                "flows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "goal": {"type": "string"},
                            "roles": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "goal", "roles"],
                        "additionalProperties": False,
                    },
                },
                "api_needs": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["product_goal", "roles", "entities", "flows", "api_needs", "risks"],
            "additionalProperties": False,
        }

    @staticmethod
    def _grounded_spec_outline_system_prompt() -> str:
        return (
            "You are doing the first pass for a grounded mini-app specification. "
            "Return only a compact outline: product goal, roles, entities, flows, API needs, and risks. "
            "Do not emit the full final schema yet. Keep it short, concrete, and implementation-oriented."
        )

    @staticmethod
    def _grounded_spec_system_prompt() -> str:
        return (
            "You are generating a documentation-grounded multi-role mini-app specification. "
            "Prioritize architectural depth, domain modeling, and end-to-end workflow integrity. "
            "Allow creative variation in information architecture and product composition when multiple valid solutions exist. "
            "Use only information grounded in the supplied docs and prompt. "
            "Do not collapse the app into a single form if multi-role workflows are implied. "
            "Require explicit state lifecycle, role handoffs, operational control flow, and recoverable error paths. "
            "Prefer explicit assumptions and canonical template defaults over blocking unknowns for ordinary implementation details. "
            "Only emit high-impact unknowns when generation truly cannot proceed without user clarification. "
            "Avoid toy/demo framing and avoid repeating rigid template wording across runs. "
            "Honor the provided creative_direction while preserving schema validity and business realism. "
            "Keep the output strictly valid against the schema."
        )

    @staticmethod
    def _grounded_spec_section_system_prompt(section_title: str) -> str:
        return (
            "You are generating one section of a documentation-grounded multi-role mini-app specification. "
            f"Produce only the requested section: {section_title}. "
            "Keep the section concrete, implementation-oriented, and strictly valid against the schema. "
            "Do not repeat unrelated sections."
        )

    @classmethod
    def _grounded_spec_outline_user_prompt(
        cls,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec outline",
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "creative_direction": creative_direction,
                "architecture_contract": [
                    "Identify the real business domain and the selected roles.",
                    "Extract entities, user flows, and any obvious API needs.",
                    "Keep the output compact; this is only the outline pass.",
                ],
                "docs": cls._compact_doc_refs(doc_refs, limit=2 if compact else 4),
                "creative_direction_summary": cls._compact_creative_direction(creative_direction, compact=compact),
            }
        )

    @classmethod
    def _grounded_spec_user_prompt(
        cls,
        *,
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec",
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "architecture_contract": [
                    "Model a concrete business domain with realistic entities and statuses.",
                    "Define clear role boundaries and cross-role handoff points.",
                    "Specify complete role flows; screen count and depth are flexible.",
                    "Include failure handling, validation rules, and operational monitoring expectations.",
                    "Avoid generic placeholders and repeated template wording.",
                ],
                "outline": outline,
                "creative_direction": cls._compact_creative_direction(creative_direction, compact=compact),
                "variability_policy": [
                    "Use role requirements as capability constraints, not layout constraints.",
                    "You may pick any navigation and screen composition pattern that remains coherent.",
                    "Do not mirror the same information hierarchy across all roles.",
                ],
                "docs": cls._compact_doc_refs(doc_refs, limit=3 if compact else 6),
            }
        )

    @staticmethod
    def _grounded_spec_partial_schema(field_names: list[str]) -> dict[str, Any]:
        full_schema = GroundedSpecModel.model_json_schema()
        properties = full_schema.get("properties", {})
        return {
            "type": "object",
            "properties": {name: properties[name] for name in field_names if name in properties},
            "required": [name for name in field_names if name in properties],
            "additionalProperties": False,
            "$defs": full_schema.get("$defs", {}),
        }

    @classmethod
    def _grounded_spec_section_user_prompt(
        cls,
        *,
        section_id: str,
        section_title: str,
        field_names: list[str],
        prompt: str,
        doc_refs: list[Any],
        target_platform: TargetPlatform,
        preview_profile: PreviewProfile,
        template_revision_id: str,
        prompt_turn_id: str,
        creative_direction: dict[str, Any],
        outline: dict[str, Any],
        compact: bool = False,
    ) -> str:
        return json_dumps(
            {
                "task": "Build GroundedSpec section",
                "section_id": section_id,
                "section_title": section_title,
                "required_fields": field_names,
                "prompt": prompt,
                "target_platform": target_platform.value,
                "preview_profile": preview_profile.value,
                "template_revision_id": template_revision_id,
                "prompt_turn_id": prompt_turn_id,
                "outline": outline,
                "creative_direction": cls._compact_creative_direction(creative_direction, compact=compact),
                "section_contract": [
                    "Return only the requested fields.",
                    "Keep entity, role, and API naming consistent with the outline.",
                    "Prefer concrete business details over placeholders.",
                    "Do not duplicate top-level fields that were not requested.",
                ],
                "docs": cls._compact_doc_refs(doc_refs, limit=2 if compact else 4),
            }
        )

    @staticmethod
    def _compact_doc_refs(doc_refs: list[Any], limit: int = 6) -> list[dict[str, Any]]:
        compact_refs: list[dict[str, Any]] = []
        for item in doc_refs[:limit]:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            if not isinstance(raw, dict):
                continue
            compact_refs.append(
                {
                    "doc_ref_id": raw.get("doc_ref_id"),
                    "source_type": raw.get("source_type"),
                    "file_path": raw.get("file_path"),
                    "section_title": raw.get("section_title"),
                    "relevance": raw.get("relevance"),
                    "snippet": str(raw.get("snippet") or "")[:180],
                }
            )
        return compact_refs

    @staticmethod
    def _compact_creative_direction(creative_direction: dict[str, Any], *, compact: bool) -> dict[str, Any]:
        if not compact:
            return creative_direction
        return {
            "name": creative_direction.get("name"),
            "focus": creative_direction.get("focus"),
            "layout_bias": creative_direction.get("layout_bias"),
            "interaction_bias": creative_direction.get("interaction_bias"),
        }
