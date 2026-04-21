from __future__ import annotations

from app.models.common import GenerationMode

PLANNING_MODEL = "gpt-5-mini"
FAST_CODE_MODEL = "gpt-5.1-codex-mini"
STRONG_CODE_MODEL = "gpt-5.1-codex-max"
REPAIR_MODEL = STRONG_CODE_MODEL
SUMMARY_MODEL = "gpt-5-mini"

TASK_PROFILES = {
    "openai_code_fast": {
        "label": "OpenAI Code Fast",
        "provider": "openai",
        "description": "Default iterative coding profile tuned for lower cost while keeping solid general coding quality.",
        "routing": {
            "spec_analysis": PLANNING_MODEL,
            "ir_codegen": FAST_CODE_MODEL,
            "code_plan": PLANNING_MODEL,
            "code_edit": FAST_CODE_MODEL,
            "repair": REPAIR_MODEL,
            "summarize": SUMMARY_MODEL,
            "cheap_task": SUMMARY_MODEL,
        },
        "default": True,
    },
    "research_balanced": {
        "label": "Research Balanced",
        "provider": "openai",
        "description": "Balanced profile for grounded artifact generation with stronger planning and code editing before entering repair.",
        "routing": {
            "spec_analysis": PLANNING_MODEL,
            "ir_codegen": STRONG_CODE_MODEL,
            "code_plan": STRONG_CODE_MODEL,
            "code_edit": STRONG_CODE_MODEL,
            "repair": REPAIR_MODEL,
            "summarize": SUMMARY_MODEL,
            "cheap_task": SUMMARY_MODEL,
        },
        "default": False,
    },
    "openai_code_quality": {
        "label": "OpenAI Code Quality",
        "provider": "openai",
        "description": "Highest-confidence profile for grounded artifact generation and repair.",
        "routing": {
            "spec_analysis": STRONG_CODE_MODEL,
            "ir_codegen": STRONG_CODE_MODEL,
            "code_plan": STRONG_CODE_MODEL,
            "code_edit": STRONG_CODE_MODEL,
            "repair": REPAIR_MODEL,
            "summarize": SUMMARY_MODEL,
            "cheap_task": SUMMARY_MODEL,
        },
        "default": False,
    },
}

MODEL_REGISTRY = {
    "spec_analysis": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["spec_analysis"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["spec_analysis"],
    },
    "ir_codegen": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["ir_codegen"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["ir_codegen"],
    },
    "code_plan": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["code_plan"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["code_plan"],
    },
    "code_edit": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["code_edit"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["code_edit"],
    },
    "repair": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["repair"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["repair"],
    },
    "summarize": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["summarize"],
        "fallback": TASK_PROFILES["openai_code_fast"]["routing"]["summarize"],
    },
    "cheap_task": {
        "primary": TASK_PROFILES["research_balanced"]["routing"]["cheap_task"],
        "fallback": "gpt-5-mini",
    },
    "embedding": {
        "primary": "text-embedding-3-large",
        "fallback": "text-embedding-3-large",
    },
}

DEFAULT_PROFILE_BY_MODE = {
    GenerationMode.FAST: "openai_code_fast",
    GenerationMode.BALANCED: "research_balanced",
    GenerationMode.QUALITY: "openai_code_quality",
    GenerationMode.BASIC: "openai_code_fast",
}


def default_profile_for_generation_mode(generation_mode: GenerationMode | str | None) -> str:
    if generation_mode is None:
        return DEFAULT_PROFILE_BY_MODE[GenerationMode.BALANCED]
    mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(str(generation_mode))
    return DEFAULT_PROFILE_BY_MODE.get(mode, DEFAULT_PROFILE_BY_MODE[GenerationMode.BALANCED])


def resolve_model_profile(requested_profile: str | None, generation_mode: GenerationMode | str | None) -> str:
    normalized = str(requested_profile or "").strip()
    default_profile = default_profile_for_generation_mode(generation_mode)
    if not normalized or normalized not in TASK_PROFILES:
        return default_profile
    mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(str(generation_mode or GenerationMode.BALANCED.value))
    if mode == GenerationMode.BALANCED and normalized == "openai_code_fast":
        return default_profile
    if mode == GenerationMode.QUALITY and normalized in {"openai_code_fast", "research_balanced"}:
        return default_profile
    if mode in {GenerationMode.FAST, GenerationMode.BASIC} and normalized == "openai_code_quality":
        return default_profile
    return normalized


def routing_for_profile(*, model_profile: str | None, generation_mode: GenerationMode | str | None) -> dict[str, str]:
    profile_name = resolve_model_profile(model_profile, generation_mode)
    profile = TASK_PROFILES.get(profile_name) or TASK_PROFILES[default_profile_for_generation_mode(generation_mode)]
    return dict(profile.get("routing") or {})


def models_for_role(
    role: str,
    *,
    model_profile: str | None,
    generation_mode: GenerationMode | str | None,
) -> tuple[str, str]:
    routing = routing_for_profile(model_profile=model_profile, generation_mode=generation_mode)
    primary = str(routing.get(role) or MODEL_REGISTRY[role]["primary"])
    fallback_profile = TASK_PROFILES["openai_code_fast"]["routing"]
    fallback = str(fallback_profile.get(role) or MODEL_REGISTRY[role]["fallback"] or primary)
    return primary, fallback
