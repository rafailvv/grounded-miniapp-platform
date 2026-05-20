from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from app.ai.model_registry import (
    MODEL_CAPABILITIES,
    MODEL_REGISTRY,
    PROVIDER_REGISTRY,
    TASK_PROFILES,
    default_profile_for_generation_mode,
    model_capabilities,
    models_for_role,
    provider_routing_table,
    resolve_model_profile,
)
from app.core.config import Settings
from app.models.common import GenerationMode
from app.models.model_manager import ModelCatalogCache, ModelManagerStatus, ModelProviderStatus, ModelRoute, ModelRouteCandidate
from app.repositories.state_store import StateStore


class ModelManagerService:
    """Provider-aware routing, catalog cache, and fallback policy for model calls."""

    CACHE_REF = "model_catalog_cache:v1"
    TASK_ROLES = ("agent_turn", "code_edit", "repair", "summarize", "cheap_task", "embedding")

    def __init__(self, *, settings: Settings, store: StateStore | None = None) -> None:
        self.settings = settings
        self.store = store

    def configuration(self) -> dict[str, Any]:
        status = self.status()
        catalog = self.catalog()
        return {
            "enabled": status.enabled,
            "base_url": self.provider_base_url("openai"),
            "models": MODEL_REGISTRY,
            "task_profiles": TASK_PROFILES,
            "default_coding_profile": status.default_coding_profile,
            "routing": {
                "provider": status.active_provider,
                "providers": [name for name, provider in status.providers.items() if provider.enabled],
            },
            "provider_routing": provider_routing_table(),
            "model_manager": status.model_dump(mode="json", by_alias=True),
            "model_catalog": catalog.model_dump(mode="json", by_alias=True),
            "supports_prompt_cache_key": True,
            "mode_profiles": status.mode_profiles,
        }

    def status(self) -> ModelManagerStatus:
        now = self._now()
        providers = {name: self.provider_status(name) for name in sorted(PROVIDER_REGISTRY)}
        enabled_providers = [name for name, status in providers.items() if status.enabled]
        catalog = self.catalog()
        routes: dict[str, Any] = {}
        for mode in ("fast", "balanced", "quality"):
            profile = default_profile_for_generation_mode(mode)
            routes[mode] = {
                role: self.select(role=role, model_profile=profile, generation_mode=mode).model_dump(mode="json")
                for role in self.TASK_ROLES
                if role in MODEL_REGISTRY
            }
        return ModelManagerStatus(
            enabled=bool(enabled_providers),
            active_provider=enabled_providers[0] if enabled_providers else None,
            providers=providers,
            catalog_cache={
                "status": "warm" if self._cached_catalog_payload() else "initialized",
                "ref": self.CACHE_REF,
                "source": catalog.source,
                "updated_at": catalog.updated_at,
                "offline_usable": True,
            },
            default_coding_profile=default_profile_for_generation_mode(GenerationMode.BALANCED),
            mode_profiles={
                "fast": default_profile_for_generation_mode(GenerationMode.FAST),
                "balanced": default_profile_for_generation_mode(GenerationMode.BALANCED),
                "quality": default_profile_for_generation_mode(GenerationMode.QUALITY),
                "basic": default_profile_for_generation_mode(GenerationMode.BASIC),
            },
            task_profiles=TASK_PROFILES,
            routes=routes,
            fallback_policy={
                "enabled": True,
                "env": {
                    "global": "OPENAI_CODE_FALLBACK_MODELS",
                    "per_role": "OPENAI_CODE_<ROLE>_FALLBACK_MODELS",
                },
                "fallback_on": ["rate_limit", "quota", "transient_provider", "network_transport", "model_unavailable"],
            },
            updated_at=now,
        )

    def catalog(self, *, refresh: bool = False) -> ModelCatalogCache:
        if not refresh:
            cached = self._cached_catalog_payload()
            if cached:
                try:
                    return ModelCatalogCache.model_validate(cached)
                except Exception:
                    pass
        catalog = ModelCatalogCache(
            source="builtin",
            providers=PROVIDER_REGISTRY,
            task_profiles=TASK_PROFILES,
            models={name: model_capabilities(name) for name in sorted(MODEL_CAPABILITIES)},
            default_coding_profile=default_profile_for_generation_mode(GenerationMode.BALANCED),
            mode_profiles={
                "fast": default_profile_for_generation_mode(GenerationMode.FAST),
                "balanced": default_profile_for_generation_mode(GenerationMode.BALANCED),
                "quality": default_profile_for_generation_mode(GenerationMode.QUALITY),
                "basic": default_profile_for_generation_mode(GenerationMode.BASIC),
            },
            updated_at=self._now(),
        )
        self._store_catalog(catalog)
        return catalog

    def select(
        self,
        *,
        role: str,
        model_profile: str | None,
        generation_mode: GenerationMode | str | None,
        model_override: str | None = None,
    ) -> ModelRoute:
        profile_name = resolve_model_profile(model_profile, generation_mode)
        mode_value = str(getattr(generation_mode, "value", generation_mode) or GenerationMode.BALANCED.value)
        primary = str(model_override or models_for_role(role, model_profile=profile_name, generation_mode=generation_mode) or MODEL_REGISTRY[role]["primary"])
        candidates: list[str] = [primary]
        candidates.extend(self._profile_fallbacks(role=role, profile_name=profile_name))
        candidates.extend(self._env_fallbacks(role))
        candidates.extend([str(MODEL_REGISTRY.get(role, {}).get("primary") or "")])
        normalized = [item for item in dict.fromkeys(model.strip() for model in candidates if str(model or "").strip())]
        route_candidates: list[ModelRouteCandidate] = []
        for index, model in enumerate(normalized):
            provider = self.provider_for_model(model)
            provider_status = self.provider_status(provider)
            source = "override" if model_override and model == model_override else "profile" if index == 0 else "fallback"
            route_candidates.append(
                ModelRouteCandidate(
                    model=model,
                    provider=provider,
                    available=provider_status.enabled,
                    source=source,
                    reason="provider_enabled" if provider_status.enabled else "provider_disabled",
                    priority=index,
                )
            )
        selected = next((item for item in route_candidates if item.available), route_candidates[0])
        status = "ready" if selected.available else "provider_disabled"
        return ModelRoute(
            role=role,
            model_profile=profile_name,
            generation_mode=mode_value,
            selected_model=selected.model,
            selected_provider=selected.provider,
            fallback_enabled=len(route_candidates) > 1,
            status=status,
            candidates=route_candidates,
        )

    def route_candidates_for_model(self, model: str) -> list[ModelRouteCandidate]:
        primary = str(model or "").strip()
        if not primary:
            return []
        fallback_models = [primary, *self._env_fallbacks("global")]
        candidates: list[ModelRouteCandidate] = []
        for index, candidate in enumerate(dict.fromkeys(item for item in fallback_models if item)):
            provider = self.provider_for_model(candidate)
            status = self.provider_status(provider)
            candidates.append(
                ModelRouteCandidate(
                    model=candidate,
                    provider=provider,
                    available=status.enabled,
                    source="request" if index == 0 else "fallback",
                    reason="provider_enabled" if status.enabled else "provider_disabled",
                    priority=index,
                )
            )
        return candidates

    def provider_for_model(self, model: str) -> str:
        return str(model_capabilities(model).get("provider") or "openai")

    def provider_status(self, provider: str) -> ModelProviderStatus:
        registry = PROVIDER_REGISTRY.get(provider, {})
        api_key_env = str(registry.get("enabled_env") or "OPENAI_API_KEY")
        base_url_env = str(registry.get("base_url_env") or "OPENAI_BASE_URL")
        configured = bool(os.getenv(api_key_env))
        enabled = configured
        return ModelProviderStatus(
            provider=provider,
            label=str(registry.get("label") or provider.title()),
            enabled=enabled,
            configured=configured,
            base_url=self.provider_base_url(provider),
            api_key_env=api_key_env,
            base_url_env=base_url_env,
            status="ready" if enabled else "missing_api_key",
            models=[str(item) for item in registry.get("models") or []],
        )

    def provider_base_url(self, provider: str) -> str:
        registry = PROVIDER_REGISTRY.get(provider, {})
        env_name = str(registry.get("base_url_env") or "OPENAI_BASE_URL")
        if provider == "openai":
            return os.getenv(env_name, "https://api.openai.com/v1")
        return os.getenv(env_name, "")

    def provider_api_key(self, provider: str) -> str | None:
        registry = PROVIDER_REGISTRY.get(provider, {})
        env_name = str(registry.get("enabled_env") or "OPENAI_API_KEY")
        return os.getenv(env_name)

    @staticmethod
    def should_fallback(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "rate limit",
                "rate_limited",
                "insufficient_quota",
                "quota",
                "temporarily unavailable",
                "timeout",
                "connection",
                "model_not_found",
                "model not found",
                "does not exist",
                "503",
                "502",
                "500",
                "429",
            )
        )

    def _profile_fallbacks(self, *, role: str, profile_name: str) -> list[str]:
        values: list[str] = []
        for other_name, profile in TASK_PROFILES.items():
            if other_name == profile_name:
                continue
            routing = profile.get("routing") if isinstance(profile, dict) else {}
            if isinstance(routing, dict) and routing.get(role):
                values.append(str(routing[role]))
        return values

    @staticmethod
    def _env_fallbacks(role: str) -> list[str]:
        normalized_role = "".join(ch if ch.isalnum() else "_" for ch in str(role or "global").upper())
        env_names = [f"OPENAI_CODE_{normalized_role}_FALLBACK_MODELS", "OPENAI_CODE_FALLBACK_MODELS"]
        values: list[str] = []
        for env_name in env_names:
            raw = os.getenv(env_name, "")
            values.extend(item.strip() for item in raw.split(",") if item.strip())
        return values

    def _cached_catalog_payload(self) -> dict[str, Any] | None:
        if self.store is None:
            return None
        payload = self.store.get("reports", self.CACHE_REF)
        return payload if isinstance(payload, dict) else None

    def _store_catalog(self, catalog: ModelCatalogCache) -> None:
        if self.store is None:
            return
        self.store.upsert("reports", self.CACHE_REF, catalog.model_dump(mode="json", by_alias=True))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
