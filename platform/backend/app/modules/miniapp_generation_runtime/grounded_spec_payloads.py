from __future__ import annotations

import re
from typing import Any


class GroundedSpecPayloadsRuntime:
    @classmethod
    def _normalize_model_payload(cls, payload: Any) -> Any:
        if isinstance(payload, dict):
            def normalize_key(key: Any) -> Any:
                if not isinstance(key, str):
                    return key
                candidate = key.strip().strip("`'\"")
                candidate = re.sub(r"^[\(\[\{]+", "", candidate)
                candidate = re.sub(r"[\)\]\}:;,]+$", "", candidate)
                candidate = candidate.strip()
                aliases = {
                    "(trigger": "trigger",
                    "trigger)": "trigger",
                    ".trigger": "trigger",
                }
                candidate = aliases.get(candidate, candidate)
                return candidate or key

            normalized: dict[Any, Any] = {}
            for raw_key, raw_value in payload.items():
                fixed_key = normalize_key(raw_key)
                fixed_value = cls._normalize_model_payload(raw_value)
                if fixed_key in normalized:
                    existing = normalized[fixed_key]
                    if existing not in (None, "", [], {}):
                        continue
                normalized[fixed_key] = fixed_value

            list_default_keys = {
                "input_data",
                "output_data",
                "preconditions",
                "postconditions",
                "error_paths",
                "request_fields",
                "response_fields",
                "permissions_hint",
                "unknowns",
                "contradictions",
                "assumptions",
                "telemetry_hooks",
                "traceability",
                "terminal_screen_ids",
                "on_enter_actions",
                "action_ids",
                "routes",
                "route_groups",
                "screen_data_sources",
                "role_action_groups",
                "input_variable_ids",
                "assignments",
                "enum_values",
                "validators",
                "components",
                "actions",
                "fields",
                "variables",
                "entities",
                "screens",
                "transitions",
                "integrations",
                "storage_bindings",
                "doc_refs",
                "actors",
                "domain_entities",
                "user_flows",
                "ui_requirements",
                "api_requirements",
                "persistence_requirements",
                "integration_requirements",
                "security_requirements",
                "platform_constraints",
                "non_functional_requirements",
                "issues",
            }
            dict_default_keys = {"params"}
            false_default_keys = {
                "auth_required",
                "existing_in_template",
                "required",
                "pii",
                "server_side_session",
                "telegram_initdata_validation_required",
                "is_entry",
                "blocking",
            }
            numeric_default_keys = {"timeout_ms"}

            for key in list_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = []
            for key in false_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = False
            for key in dict_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = {}
            for key in numeric_default_keys:
                if key in normalized and normalized[key] is None:
                    normalized[key] = 5000
            if isinstance(normalized.get("assumptions"), list):
                normalized["assumptions"] = [cls._normalize_assumption_item(item) for item in normalized["assumptions"]]
            if isinstance(normalized.get("contradictions"), list):
                normalized["contradictions"] = [cls._normalize_contradiction_item(item) for item in normalized["contradictions"]]
            if isinstance(normalized.get("non_functional_requirements"), list):
                normalized["non_functional_requirements"] = [
                    cls._normalize_non_functional_requirement_item(item)
                    for item in normalized["non_functional_requirements"]
                ]
            return normalized
        if isinstance(payload, list):
            return [cls._normalize_model_payload(item) for item in payload]
        if isinstance(payload, str):
            normalized_scalar = payload.strip().lower()
            if normalized_scalar == "implicit":
                return "derived"
        return payload

    @staticmethod
    def _normalize_assumption_item(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        normalized = dict(item)
        if "assumption_id" in normalized and "text" in normalized and "rationale" in normalized:
            normalized.setdefault("status", "active")
            normalized.setdefault("impact", "medium")
            return normalized
        fallback_key = next(
            (
                key
                for key in normalized.keys()
                if str(key).strip().lower() in {"assumption", "text", "summary", "note"}
            ),
            None,
        )
        if fallback_key is None:
            return normalized
        text = str(normalized.get(fallback_key) or "").strip()
        if not text:
            return normalized
        return {
            "assumption_id": normalized.get("assumption_id") or f"assumption_{abs(hash(text)) % 100000}",
            "text": text,
            "rationale": str(normalized.get("rationale") or normalized.get("reason") or "Assumed during generation.").strip(),
            "status": str(normalized.get("status") or "active").strip() or "active",
            "impact": str(normalized.get("impact") or "medium").strip() or "medium",
        }

    @staticmethod
    def _normalize_contradiction_item(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        normalized = dict(item)
        if "contradiction_id" in normalized and "summary" in normalized:
            normalized.setdefault("severity", "medium")
            normalized.setdefault("resolution_hint", "")
            return normalized
        summary_key = next(
            (
                key
                for key in normalized.keys()
                if str(key).strip().lower() in {"summary", "text", "issue", "conflict"}
            ),
            None,
        )
        if summary_key is None:
            return normalized
        summary = str(normalized.get(summary_key) or "").strip()
        if not summary:
            return normalized
        return {
            "contradiction_id": normalized.get("contradiction_id") or f"contradiction_{abs(hash(summary)) % 100000}",
            "summary": summary,
            "severity": str(normalized.get("severity") or "medium").strip() or "medium",
            "resolution_hint": str(normalized.get("resolution_hint") or normalized.get("hint") or "").strip(),
        }

    @staticmethod
    def _normalize_non_functional_requirement_item(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        normalized = dict(item)
        if "requirement_id" in normalized and "text" in normalized:
            normalized.setdefault("category", "general")
            normalized.setdefault("priority", "should")
            return normalized
        text_key = next(
            (
                key
                for key in normalized.keys()
                if str(key).strip().lower() in {"text", "requirement", "summary", "note"}
            ),
            None,
        )
        if text_key is None:
            return normalized
        text = str(normalized.get(text_key) or "").strip()
        if not text:
            return normalized
        return {
            "requirement_id": normalized.get("requirement_id") or f"nfr_{abs(hash(text)) % 100000}",
            "text": text,
            "category": str(normalized.get("category") or "general").strip() or "general",
            "priority": str(normalized.get("priority") or "should").strip() or "should",
        }
