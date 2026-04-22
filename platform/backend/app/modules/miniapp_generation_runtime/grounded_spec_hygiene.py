from __future__ import annotations

import re
from typing import Any

from app.models.grounded_spec import APIRequirement, Contradiction, EntityAttribute


class GroundedSpecHygieneRuntime:
    _ENTITY_PHRASE_STOPWORDS = {
        "a",
        "an",
        "and",
        "app",
        "application",
        "build",
        "business",
        "but",
        "card",
        "cards",
        "clean",
        "dashboard",
        "data",
        "demo",
        "development",
        "displayed",
        "effect",
        "effects",
        "error",
        "filters",
        "for",
        "fully",
        "generic",
        "help",
        "i",
        "in",
        "internal",
        "interface",
        "it",
        "its",
        "loaded",
        "loading",
        "manage",
        "mini",
        "miniapp",
        "mobile",
        "modern",
        "of",
        "optimized",
        "our",
        "page",
        "pages",
        "portal",
        "presentation",
        "product",
        "profile",
        "real",
        "role",
        "roles",
        "screen",
        "screens",
        "simple",
        "state",
        "states",
        "static",
        "status",
        "statuses",
        "structured",
        "system",
        "that",
        "the",
        "their",
        "this",
        "tool",
        "track",
        "transitions",
        "trustworthy",
        "ui",
        "unnecessary",
        "use",
        "view",
        "views",
        "we",
        "workflow",
    }
    _LOW_SIGNAL_ENTITY_PHRASES = {
        "mobile use",
        "mobile app",
        "business app",
        "real business",
        "first screen",
        "main screen",
        "main screens",
        "user action",
        "user actions",
    }

    @staticmethod
    def _pascal_case(value: str) -> str:
        parts = re.split(r"[^a-zA-Z0-9]+", str(value or "").strip())
        return "".join(part.capitalize() for part in parts if part) or "WorkflowRecord"

    @classmethod
    def _normalize_entity_phrase(cls, value: str) -> str:
        phrase = str(value or "").strip().lower()
        if not phrase:
            return ""
        if phrase in cls._LOW_SIGNAL_ENTITY_PHRASES:
            return ""
        phrase = re.split(r"\b(?:where|that|which|who|when|while|with|including|because|so)\b", phrase, maxsplit=1)[0]
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", phrase)
            if token not in cls._ENTITY_PHRASE_STOPWORDS and len(token) > 1
        ]
        if not tokens:
            return ""
        normalized_phrase = " ".join(tokens)
        if normalized_phrase in cls._LOW_SIGNAL_ENTITY_PHRASES:
            return ""
        if len(tokens) > 3:
            tokens = tokens[-3:]
        return " ".join(tokens)

    @classmethod
    def _prompt_entity_name(cls, prompt: str) -> str | None:
        lowered = str(prompt or "").lower()
        phrase_patterns = (
            r"\b(?:mini[\s-]?app|app|tool|system|workflow|portal|dashboard)\s+for\s+([^.,;\n]+)",
            r"\bto\s+(?:manage|track|coordinate|handle|review|organize|process|submit|create|book|reserve|request|assign|monitor)s?\s+(?:a|an|the|their|own|new\s+)?([^.,;\n]+)",
            r"\b(?:submits?|creates?|opens?|updates?|assigns?|reviews?|process(?:es)?|tracks?|requests?)\s+(?:a|an|the|their|own|new\s+)?([^.,;\n]+)",
        )
        for pattern in phrase_patterns:
            for match in re.finditer(pattern, lowered):
                candidate = cls._normalize_entity_phrase(match.group(1))
                if candidate:
                    return cls._pascal_case(candidate)
        return None

    @staticmethod
    def is_forbidden_generated_api_requirement(requirement: APIRequirement) -> bool:
        path = str(requirement.path or "").strip().lower()
        name = str(requirement.name or "").strip().lower()
        purpose = str(requirement.purpose or "").strip().lower()
        if path.startswith("/auth/") or path.startswith("/api/auth") or path in {"/auth", "/api/auth", "/api/me"}:
            return True
        if any(marker in path for marker in ("/events", "/stream", "/sse", "/ws", "/websocket")):
            return True
        if any(marker in name for marker in ("login", "sign in", "auth", "session bootstrap", "user profile endpoint")):
            return True
        if any(marker in name for marker in ("websocket", "server-sent events", "sse", "push endpoint", "realtime", "push updates", "notifications/push", "webhook")):
            return True
        if any(marker in purpose for marker in ("auth/login", "role-based session bootstrap", "session bootstrap", "user profile endpoint", "role-aware bootstrapping")):
            return True
        if any(marker in purpose for marker in ("role-aware session token", "telegram init data", "websocket", "server-sent events", "sse", "push endpoint", "realtime updates", "push updates", "webhook", "notifications/push")):
            return True
        return False

    @staticmethod
    def is_forbidden_outline_api_need(item: str) -> bool:
        lowered = str(item or "").strip().lower()
        if not lowered:
            return False
        if any(
            marker in lowered
            for marker in (
                "auth / user profile",
                "auth/user profile",
                "auth / session",
                "auth/session",
                "auth:",
                "session:",
                "login",
                "sign in",
                "session bootstrap",
                "session token",
                "app session",
                "role-aware bootstrapping",
                "user profile endpoint",
                "/api/auth",
                "/api/me",
                "telegram auth",
                "telegram token",
                "telegram initdata",
                "initdata",
            )
        ):
            return True
        if any(
            marker in lowered
            for marker in (
                "push/realtime",
                "realtime",
                "real-time",
                "websocket",
                "server-sent events",
                "sse",
                "push endpoint",
                "push updates",
                "push notifications",
                "notifications",
                "long-poll",
                "long poll",
                "polling endpoint",
                "notifications/push",
                "webhook",
            )
        ):
            return True
        return False

    @classmethod
    def sanitize_grounded_spec_outline(cls, outline: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(outline)
        api_needs = sanitized.get("api_needs")
        if isinstance(api_needs, list):
            sanitized["api_needs"] = [
                item
                for item in api_needs
                if isinstance(item, str) and not cls.is_forbidden_outline_api_need(item)
            ]
        risks = sanitized.get("risks")
        if isinstance(risks, list):
            sanitized["risks"] = [
                item
                for item in risks
                if isinstance(item, str) and not cls.is_forbidden_spec_governance_text(item)
            ]
        return sanitized

    @staticmethod
    def is_forbidden_spec_governance_text(text: str) -> bool:
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        auth_markers = (
            "auth / user profile",
            "auth/user profile",
            "telegram initdata",
            "initdata",
            "role-aware session",
            "session bootstrap",
            "session token",
            "login",
            "sign in",
            "/api/auth",
            "/api/me",
        )
        realtime_markers = (
            "push/realtime",
            "realtime",
            "websocket",
            "server-sent events",
            "sse",
            "push endpoint",
            "periodic polling",
            "polling endpoint",
            "foreground polling",
            "poll intervals",
            "push notifications",
            "notifications can be implemented",
            "manual refresh",
        )
        return any(marker in lowered for marker in (*auth_markers, *realtime_markers))

    @staticmethod
    def infer_entity_name(prompt: str) -> str:
        prompt_entity_name = GroundedSpecHygieneRuntime._prompt_entity_name(prompt)
        if prompt_entity_name:
            return prompt_entity_name
        return "WorkflowRecord"

    @staticmethod
    def infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        fields: list[EntityAttribute] = []
        lowered = prompt.lower()
        temporal_markers = ("date", "dates", "time", "times", "slot", "range", "period", "schedule", "availability", "return", "due")
        resource_markers = ("item", "resource", "equipment", "asset", "room", "vehicle", "device", "inventory", "product")
        if any(marker in lowered for marker in temporal_markers):
            return [
                EntityAttribute(name="title", type="string", required=True, description="Primary record title", pii=False),
                *(
                    [EntityAttribute(name="resource_label", type="string", required=False, description="Optional resource or subject reference", pii=False)]
                    if any(marker in lowered for marker in resource_markers)
                    else []
                ),
                EntityAttribute(name="start_date", type="datetime", required=True, description="Requested start date", pii=False),
                EntityAttribute(name="end_date", type="datetime", required=True, description="Requested end date", pii=False),
                EntityAttribute(name="details", type="text", required=False, description="Additional record details", pii=False),
            ]
        mappings = [
            ("name", "string", "Requester name", True, True),
            ("phone", "phone", "Contact phone number", True, True),
            ("email", "email", "Contact email", False, True),
            ("date", "date", "Preferred date", False, False),
            ("comment", "text", "Additional notes", False, False),
            ("service", "string", "Requested service type", False, False),
            ("time", "string", "Preferred time window", False, False),
        ]
        for field_name, field_type, description, required, pii in mappings:
            if field_name in lowered:
                fields.append(
                    EntityAttribute(
                        name=field_name,
                        type=field_type,  # type: ignore[arg-type]
                        required=required,
                        description=description,
                        pii=pii,
                    )
                )
        if not fields:
            fields = [
                EntityAttribute(name="title", type="string", required=True, description="Primary request title"),
                EntityAttribute(name="details", type="text", required=False, description="Primary request details"),
            ]
        return fields

    @staticmethod
    def detect_contradictions(prompt: str) -> list[Contradiction]:
        lowered = prompt.lower()
        if "without miniapp" in lowered and "database" in lowered:
            return [
                Contradiction(
                    contradiction_id="contr_backend_database",
                    description="The prompt asks for no miniapp but also persistence in a database.",
                    left_side="without miniapp",
                    right_side="database persistence",
                    severity="critical",
                    resolution_hint="Choose whether the feature is frontend-only or persistent.",
                )
            ]
        return []

    @staticmethod
    def is_commerce_prompt(prompt: str) -> bool:
        lowered = prompt.lower()
        markers = (
            "store",
            "shop",
            "catalog",
            "product",
            "cart",
            "order",
            "магазин",
            "интернет-магазин",
            "товар",
            "товары",
            "карточк",
            "корзин",
            "заказ",
            "доставк",
            "покуп",
            "покупател",
        )
        return any(marker in lowered for marker in markers)
