from __future__ import annotations

from typing import Any

from app.models.grounded_spec import APIRequirement, Contradiction, EntityAttribute


class GroundedSpecHygieneRuntime:
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
        lowered = prompt.lower()
        if GroundedSpecHygieneRuntime.is_commerce_prompt(prompt):
            return "Order"
        if "consultation" in lowered:
            return "ConsultationRequest"
        if "booking" in lowered:
            return "BookingRequest"
        return "WorkflowRequest"

    @staticmethod
    def infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        fields: list[EntityAttribute] = []
        lowered = prompt.lower()
        if GroundedSpecHygieneRuntime.is_commerce_prompt(prompt):
            return [
                EntityAttribute(name="customer_name", type="string", required=True, description="Customer full name", pii=True),
                EntityAttribute(name="phone", type="phone", required=True, description="Customer phone number", pii=True),
                EntityAttribute(name="product_name", type="string", required=True, description="Selected product name", pii=False),
                EntityAttribute(name="quantity", type="string", required=True, description="Requested quantity", pii=False),
                EntityAttribute(name="delivery_address", type="text", required=False, description="Delivery address", pii=True),
                EntityAttribute(name="comment", type="text", required=False, description="Order comment", pii=False),
            ]
        if "booking" in lowered and any(marker in lowered for marker in ("equipment", "laptop", "projector", "issuance", "returned", "availability")):
            return [
                EntityAttribute(name="item_type", type="string", required=True, description="Requested equipment type", pii=False),
                EntityAttribute(name="item_label", type="string", required=False, description="Specific equipment label or model", pii=False),
                EntityAttribute(name="start_date", type="datetime", required=True, description="Requested issue start date", pii=False),
                EntityAttribute(name="end_date", type="datetime", required=True, description="Requested return or end date", pii=False),
                EntityAttribute(name="reason", type="text", required=True, description="Business reason for the equipment request", pii=False),
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
