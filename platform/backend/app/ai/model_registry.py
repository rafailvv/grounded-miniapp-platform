from __future__ import annotations

import os

from app.models.common import GenerationMode
from app.services.platform_config import platform_config


def _configured_model_for_role(profile_name: str, role: str) -> str:
    profile = platform_config().model_profiles.get(profile_name)
    return str((profile.routing if profile else {}).get(role) or "gpt-5.2-codex")


CODEX_MINI_MODEL = os.getenv("OPENAI_CODE_MINI_MODEL", _configured_model_for_role("openai_code_fast", "agent_turn"))
CODEX_MAX_MODEL = os.getenv("OPENAI_CODE_MAX_MODEL", _configured_model_for_role("openai_code_quality", "agent_turn"))

ROLE_MODEL_ENV = {
    "agent_turn": {
        "openai_code_fast": "OPENAI_CODE_FAST_MODEL",
        "research_balanced": "OPENAI_CODE_BALANCED_MODEL",
        "openai_code_quality": "OPENAI_CODE_QUALITY_MODEL",
    },
    "code_edit": {
        "openai_code_fast": "OPENAI_CODE_FAST_MODEL",
        "research_balanced": "OPENAI_CODE_BALANCED_MODEL",
        "openai_code_quality": "OPENAI_CODE_QUALITY_MODEL",
    },
    "repair": {"*": "OPENAI_CODE_REPAIR_MODEL"},
    "summarize": {"*": "OPENAI_CODE_SUMMARY_MODEL"},
    "cheap_task": {"*": "OPENAI_CODE_SUMMARY_MODEL"},
}


def _task_profiles() -> dict[str, dict[str, object]]:
    config = platform_config()
    profiles: dict[str, dict[str, object]] = {}
    for name, profile in config.model_profiles.items():
        routing = dict(profile.routing)
        for role, model in list(routing.items()):
            env_name = ROLE_MODEL_ENV.get(role, {}).get(name) or ROLE_MODEL_ENV.get(role, {}).get("*")
            routing[role] = os.getenv(env_name, model) if env_name else model
        profiles[name] = {
            "label": profile.label,
            "provider": profile.provider,
            "description": profile.description,
            "routing": routing,
            "default": profile.default,
            "fallbacks": {role: list(items) for role, items in profile.fallbacks.items()},
        }
    return profiles


def _model_capabilities() -> dict[str, dict[str, object]]:
    capabilities = {
        name: item.model_dump(mode="json")
        for name, item in platform_config().model_capabilities.items()
    }
    mini_model = os.getenv("OPENAI_CODE_MINI_MODEL")
    if mini_model:
        source = dict(next(iter(capabilities.values()), {}))
        source.update({"provider": "openai", "context_window": int(os.getenv("OPENAI_CODE_MINI_CONTEXT_WINDOW", "1000000")), "supports_tools": True, "supports_structured_output": True, "supports_reasoning": True, "cost_tier": "low"})
        capabilities[mini_model] = source
    max_model = os.getenv("OPENAI_CODE_MAX_MODEL")
    if max_model:
        capabilities[max_model] = {
            "provider": "openai",
            "context_window": int(os.getenv("OPENAI_CODE_MAX_CONTEXT_WINDOW", "1000000")),
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_reasoning": True,
            "cost_tier": "high",
            "roles": ["agent_turn", "code_edit", "repair"],
        }
    for profile in _task_profiles().values():
        for model in dict(profile.get("routing") or {}).values():
            capabilities.setdefault(
                str(model),
                {
                    "provider": "openai",
                    "context_window": 128000,
                    "supports_tools": str(model).startswith("gpt-"),
                    "supports_structured_output": str(model).startswith("gpt-"),
                    "supports_reasoning": str(model).startswith("gpt-5"),
                    "cost_tier": "unknown",
                    "roles": [],
                },
            )
    return capabilities


TASK_PROFILES = _task_profiles()
MODEL_CAPABILITIES = _model_capabilities()
PROVIDER_REGISTRY = {
    name: {
        "label": provider.label,
        "enabled_env": provider.enabled_env,
        "base_url_env": provider.base_url_env,
        "default_base_url": provider.default_base_url,
        "models": sorted(set([*provider.models, *MODEL_CAPABILITIES])),
    }
    for name, provider in platform_config().providers.items()
}
MODEL_REGISTRY = {
    role: {"primary": TASK_PROFILES["research_balanced"]["routing"][role]}
    for role in ("agent_turn", "code_edit", "repair", "summarize", "cheap_task")
}
MODEL_REGISTRY["embedding"] = {"primary": "text-embedding-3-large"}
DEFAULT_PROFILE_BY_MODE = {
    GenerationMode(mode): profile
    for mode, profile in platform_config().default_profile_by_mode.items()
}


def default_profile_for_generation_mode(generation_mode: GenerationMode | str | None) -> str:
    if generation_mode is None:
        return DEFAULT_PROFILE_BY_MODE.get(GenerationMode(platform_config().sla.default_mode), DEFAULT_PROFILE_BY_MODE[GenerationMode.BALANCED])
    mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(str(generation_mode))
    return DEFAULT_PROFILE_BY_MODE.get(mode, DEFAULT_PROFILE_BY_MODE[GenerationMode.BALANCED])


def resolve_model_profile(requested_profile: str | None, generation_mode: GenerationMode | str | None) -> str:
    normalized = str(requested_profile or "").strip()
    default_profile = default_profile_for_generation_mode(generation_mode)
    mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(str(generation_mode or GenerationMode.BALANCED.value))
    if mode == GenerationMode.FAST:
        return default_profile
    if not normalized or normalized not in TASK_PROFILES:
        return default_profile
    if mode == GenerationMode.BALANCED and normalized == "openai_code_fast":
        return default_profile
    if mode in {GenerationMode.QUALITY, GenerationMode.PRODUCTION} and normalized in {"openai_code_fast", "research_balanced"}:
        return default_profile
    if mode in {GenerationMode.FAST, GenerationMode.BASIC} and normalized == "openai_code_quality":
        return default_profile
    return normalized


def routing_for_profile(*, model_profile: str | None, generation_mode: GenerationMode | str | None) -> dict[str, str]:
    profile_name = resolve_model_profile(model_profile, generation_mode)
    profile = TASK_PROFILES.get(profile_name) or TASK_PROFILES[default_profile_for_generation_mode(generation_mode)]
    return dict(profile.get("routing") or {})


def model_capabilities(model: str) -> dict[str, object]:
    name = str(model or "").strip()
    capabilities = dict(MODEL_CAPABILITIES.get(name) or _model_capabilities().get(name) or {})
    if not capabilities:
        capabilities = {
            "provider": "openai",
            "context_window": 128000,
            "supports_tools": name.startswith("gpt-"),
            "supports_structured_output": name.startswith("gpt-"),
            "supports_reasoning": name.startswith("gpt-5"),
            "cost_tier": "unknown",
            "roles": [],
        }
    capabilities["model"] = name
    return capabilities


def provider_routing_table() -> dict[str, object]:
    profiles = {}
    for profile_name, profile in TASK_PROFILES.items():
        routing = dict(profile.get("routing") or {})
        profiles[profile_name] = {
            "label": profile.get("label"),
            "provider": profile.get("provider"),
            "default": profile.get("default"),
            "routing": routing,
            "capabilities": {role: model_capabilities(model) for role, model in routing.items()},
        }
    return {
        "providers": PROVIDER_REGISTRY,
        "profiles": profiles,
    }


def models_for_role(
    role: str,
    *,
    model_profile: str | None,
    generation_mode: GenerationMode | str | None,
) -> str:
    routing = routing_for_profile(model_profile=model_profile, generation_mode=generation_mode)
    return str(routing.get(role) or MODEL_REGISTRY[role]["primary"])
