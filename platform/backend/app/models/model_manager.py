from __future__ import annotations

from typing import Any

from pydantic import Field

from app.models.common import StrictModel


class ModelProviderStatus(StrictModel):
    provider: str
    label: str = ""
    enabled: bool = False
    configured: bool = False
    base_url: str = ""
    api_key_env: str = ""
    base_url_env: str = ""
    status: str = "disabled"
    error: str | None = None
    models: list[str] = Field(default_factory=list)


class ModelRouteCandidate(StrictModel):
    model: str
    provider: str = "openai"
    available: bool = False
    source: str = "profile"
    reason: str = ""
    priority: int = 0


class ModelRoute(StrictModel):
    role: str
    model_profile: str
    generation_mode: str
    selected_model: str
    selected_provider: str = "openai"
    fallback_enabled: bool = True
    status: str = "ready"
    candidates: list[ModelRouteCandidate] = Field(default_factory=list)


class ModelCatalogCache(StrictModel):
    schema_: str = Field(default="grounded.model_catalog_cache.v1", alias="schema")
    source: str = "builtin"
    providers: dict[str, Any] = Field(default_factory=dict)
    task_profiles: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)
    default_coding_profile: str = ""
    mode_profiles: dict[str, str] = Field(default_factory=dict)
    updated_at: str


class ModelManagerStatus(StrictModel):
    schema_: str = Field(default="grounded.model_manager.v1", alias="schema")
    enabled: bool = False
    active_provider: str | None = None
    providers: dict[str, ModelProviderStatus] = Field(default_factory=dict)
    catalog_cache: dict[str, Any] = Field(default_factory=dict)
    default_coding_profile: str = ""
    mode_profiles: dict[str, str] = Field(default_factory=dict)
    task_profiles: dict[str, Any] = Field(default_factory=dict)
    routes: dict[str, Any] = Field(default_factory=dict)
    fallback_policy: dict[str, Any] = Field(default_factory=dict)
    updated_at: str
