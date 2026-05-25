from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.models.common import StrictModel


DEFAULT_WEBHOOK_EVENTS = ["run.completed", "run.failed", "run.blocked", "check.failed"]


class WebhookCreateRequest(StrictModel):
    url: str
    events: list[str] = Field(default_factory=lambda: list(DEFAULT_WEBHOOK_EVENTS))
    workspace_id: str | None = None
    enabled: bool = True
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("Webhook URL must start with http:// or https://.")
        return normalized

    @field_validator("events")
    @classmethod
    def _validate_events(cls, value: list[str]) -> list[str]:
        events = [str(item).strip() for item in value if str(item).strip()]
        if not events:
            raise ValueError("At least one webhook event is required.")
        if len(events) > 50:
            raise ValueError("Webhook event list is too large.")
        return events


class WebhookUpdateRequest(StrictModel):
    url: str | None = None
    events: list[str] | None = None
    workspace_id: str | None = None
    enabled: bool | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None
    secret: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("Webhook URL must start with http:// or https://.")
        return normalized

    @field_validator("events")
    @classmethod
    def _validate_events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        events = [str(item).strip() for item in value if str(item).strip()]
        if not events:
            raise ValueError("At least one webhook event is required.")
        if len(events) > 50:
            raise ValueError("Webhook event list is too large.")
        return events


class WebhookSubscription(StrictModel):
    schema_: str = Field(default="grounded.webhook.subscription.v1", alias="schema")
    webhook_id: str
    url: str
    events: list[str] = Field(default_factory=list)
    workspace_id: str | None = None
    enabled: bool = True
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    secret_configured: bool = False
    last_delivery: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class WebhookListReport(StrictModel):
    schema_: str = Field(default="grounded.webhooks.v1", alias="schema")
    status: str = "ok"
    workspace_id: str | None = None
    items: list[WebhookSubscription] = Field(default_factory=list)


class WebhookTestRequest(StrictModel):
    event_type: str = "webhook.test"
    payload: dict[str, Any] = Field(default_factory=dict)


class WebhookDeliveryReport(StrictModel):
    schema_: str = Field(default="grounded.webhook.delivery.v1", alias="schema")
    webhook_id: str
    event_type: str
    status: str = "simulated"
    simulated: bool = True
    delivered_at: str
    target_url: str
    payload_preview: dict[str, Any] = Field(default_factory=dict)
