from __future__ import annotations

import re
from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.grounded_spec_hygiene import GroundedSpecHygieneRuntime
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationEntityContract(MiniappGenerationRuntimeOwner):
    _EXCLUDED_API_PATHS = {
        "/api/runtime/{role}/manifest",
        "/api/runtime/manifest",
        "/api/profiles/{role}",
        "/api/profiles",
        "/health",
    }
    _STATUS_HINTS = (
        "draft",
        "pending",
        "approved",
        "rejected",
        "confirmed",
        "cancelled",
        "canceled",
        "completed",
        "in_progress",
        "assigned",
        "available",
        "unavailable",
        "returned",
        "issued",
        "closed",
        "open",
    )
    _IRREGULAR_PLURALS = {
        "analysis": "analyses",
        "person": "people",
        "status": "statuses",
    }
    _GENERIC_ENTITY_NAMES = {
        "workflowrecord",
        "workflow_record",
        "workflow record",
        "workflowrequest",
        "workflow_request",
        "workflow request",
        "record",
        "item",
    }
    _GENERIC_API_STEMS = {"workflowrequests", "workflowrecords", "records", "submissions"}
    _LOW_SIGNAL_ENTITY_SLUGS = {
        "app",
        "business",
        "dashboard",
        "data",
        "interface",
        "mobile",
        "mobile_app",
        "mobile_use",
        "screen",
        "screens",
        "status",
        "ui",
        "use",
        "uses",
        "workflow",
    }
    _LOW_SIGNAL_API_STEMS = {"uses", "mobileuses", "mobile_uses", "statuses", "data"}

    @classmethod
    def _humanize(cls, value: str) -> str:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        text = text.replace("-", " ").replace("_", " ")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _slugify(cls, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", cls._humanize(value).lower()).strip("_")
        return slug or "record"

    @classmethod
    def _singularize_slug(cls, slug: str) -> str:
        normalized = str(slug or "").strip().lower()
        if not normalized:
            return "record"
        if normalized.endswith("ies") and len(normalized) > 3:
            return f"{normalized[:-3]}y"
        if normalized.endswith("ses") and len(normalized) > 4 and normalized[:-2].endswith("s"):
            return normalized[:-2]
        if normalized.endswith("s") and not normalized.endswith("ss") and len(normalized) > 3:
            return normalized[:-1]
        return normalized

    @classmethod
    def _pluralize_slug(cls, slug: str) -> str:
        normalized = cls._singularize_slug(slug)
        if normalized in cls._IRREGULAR_PLURALS:
            return cls._IRREGULAR_PLURALS[normalized]
        if normalized.endswith("y") and len(normalized) > 1 and normalized[-2] not in "aeiou":
            return f"{normalized[:-1]}ies"
        if normalized.endswith(("s", "x", "z", "ch", "sh")):
            return f"{normalized}es"
        return f"{normalized}s"

    @classmethod
    def _pascal_case(cls, value: str) -> str:
        return "".join(part.capitalize() for part in cls._humanize(value).split()) or "Record"

    @classmethod
    def _dominant_api_path(cls, grounded_spec: GroundedSpecModel) -> str | None:
        candidates: list[str] = []
        for requirement in grounded_spec.api_requirements:
            path = str(requirement.path or "").strip()
            if not path.startswith("/api/"):
                continue
            if path in cls._EXCLUDED_API_PATHS:
                continue
            candidates.append(path)
        if not candidates:
            return None
        candidates.sort(key=lambda path: ("/{" in path, len(path)))
        return candidates[0]

    @classmethod
    def _status_literals(cls, prompt: str, grounded_spec: GroundedSpecModel, *, max_items: int) -> list[str]:
        evidence = "\n".join(
            [
                prompt,
                grounded_spec.product_goal,
                *[flow.goal for flow in grounded_spec.user_flows],
                *[item.description for item in grounded_spec.ui_requirements],
                *[item.purpose for item in grounded_spec.api_requirements],
                *[item.text for item in grounded_spec.assumptions],
            ]
        ).lower()
        found: list[str] = []
        for marker in cls._STATUS_HINTS:
            if re.search(rf"\b{re.escape(marker)}\b", evidence):
                found.append(marker)
        return list(dict.fromkeys(found))[:max_items]

    @classmethod
    def _prompt_entity_slug(cls, prompt: str) -> str | None:
        entity_name = GroundedSpecHygieneRuntime.infer_entity_name(prompt)
        focus_parts = [part for part in cls._humanize(entity_name).lower().split() if part]
        if len(focus_parts) > 1:
            for candidate in reversed(focus_parts):
                normalized_candidate = cls._singularize_slug(cls._slugify(candidate))
                if normalized_candidate and normalized_candidate not in cls._GENERIC_ENTITY_NAMES:
                    normalized = normalized_candidate
                    break
            else:
                normalized = cls._singularize_slug(cls._slugify(entity_name))
        else:
            normalized = cls._singularize_slug(cls._slugify(entity_name))
        if normalized in cls._GENERIC_ENTITY_NAMES:
            return None
        if normalized in cls._LOW_SIGNAL_ENTITY_SLUGS:
            return None
        if normalized:
            return normalized
        return None

    def extract_entity_contract(
        self,
        *,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        dominant_entity_name = str(
            (grounded_spec.domain_entities[0].name if grounded_spec.domain_entities else "")
            or self._infer_entity_name(prompt)
        ).strip() or "WorkflowRecord"
        prompt_entity_slug = self._prompt_entity_slug(prompt)
        api_path = self._dominant_api_path(grounded_spec)
        dominant_name_slug = self._singularize_slug(self._slugify(dominant_entity_name))
        dominant_name_is_generic = (
            dominant_name_slug in self._GENERIC_ENTITY_NAMES
            or dominant_name_slug.replace("_", " ") in self._GENERIC_ENTITY_NAMES
            or dominant_name_slug.replace("_", "") in self._GENERIC_ENTITY_NAMES
        )
        dominant_name_is_low_signal = (
            dominant_name_slug in self._LOW_SIGNAL_ENTITY_SLUGS
            or dominant_name_slug.replace("_", " ") in self._LOW_SIGNAL_ENTITY_SLUGS
            or dominant_name_slug.replace("_", "") in self._LOW_SIGNAL_ENTITY_SLUGS
        )
        if api_path:
            api_stem = api_path.removeprefix("/api/").split("/", 1)[0].strip()
            normalized_api_stem = self._slugify(api_stem)
            if (
                normalized_api_stem in self._GENERIC_API_STEMS
                or normalized_api_stem in self._LOW_SIGNAL_API_STEMS
                or self._singularize_slug(normalized_api_stem) in self._LOW_SIGNAL_ENTITY_SLUGS
            ) and prompt_entity_slug:
                entity_slug = prompt_entity_slug
                api_path = f"/api/{self._pluralize_slug(entity_slug)}"
            else:
                entity_slug = self._singularize_slug(normalized_api_stem)
        else:
            entity_slug = prompt_entity_slug or dominant_name_slug
            api_path = f"/api/{self._pluralize_slug(entity_slug)}"
        if (dominant_name_is_generic or dominant_name_is_low_signal) and prompt_entity_slug:
            dominant_entity_name = self._pascal_case(prompt_entity_slug)
        plural_slug = self._pluralize_slug(entity_slug)
        singular_label = self._humanize(dominant_entity_name) or self._humanize(entity_slug)
        plural_label = self._humanize(plural_slug)
        schema_prefix = self._pascal_case(dominant_entity_name)
        key_fields = [
            {
                "name": attribute.name,
                "type": attribute.type,
                "required": attribute.required,
            }
            for attribute in (grounded_spec.domain_entities[0].attributes if grounded_spec.domain_entities else [])[:8]
        ]
        status_limit = 8 if generation_mode == GenerationMode.QUALITY else 6 if generation_mode == GenerationMode.BALANCED else 4
        return {
            "extraction_mode": generation_mode.value,
            "entity_name": dominant_entity_name,
            "entity_slug": entity_slug,
            "entity_slug_plural": plural_slug,
            "singular_label": singular_label,
            "plural_label": plural_label,
            "api_path": api_path,
            "detail_api_path": f"{api_path}/{{item_id}}",
            "route_file": f"miniapp/app/routes/{plural_slug}.py",
            "detail_route_slug": plural_slug,
            "schema_prefix": schema_prefix,
            "model_prefix": schema_prefix,
            "read_schema_name": f"{schema_prefix}Read",
            "list_schema_name": f"{schema_prefix}ListResponse",
            "record_name": f"{schema_prefix}Record",
            "key_fields": key_fields,
            "status_literals": self._status_literals(prompt, grounded_spec, max_items=status_limit),
            "role_responsibilities": {
                actor.role: actor.description
                for actor in grounded_spec.actors
                if getattr(actor, "role", None)
            },
            "page_contract": {
                "list_page_expected": True,
                "detail_page_expected": bool(
                    re.search(r"\b(detail|details|open|inspect|review|separate page)\b", prompt.lower())
                ),
                "update_action_expected": True,
            },
            "source": {
                "grounded_entity": dominant_entity_name,
                "api_path": api_path,
            },
        }
