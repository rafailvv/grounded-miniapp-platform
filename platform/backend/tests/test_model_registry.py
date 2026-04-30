from __future__ import annotations

from app.ai.model_registry import (
    BALANCED_CODE_MODEL,
    CODEX_MAX_MODEL,
    CODEX_MINI_MODEL,
    FAST_CODE_MODEL,
    QUALITY_CODE_MODEL,
    REPAIR_MODEL,
    SUMMARY_MODEL,
    TASK_PROFILES,
    models_for_role,
)
from app.models.common import GenerationMode


def test_generation_modes_route_agent_turn_to_gpt_54_mini_defaults() -> None:
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.FAST) == FAST_CODE_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.BALANCED) == BALANCED_CODE_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.QUALITY) == QUALITY_CODE_MODEL
    assert FAST_CODE_MODEL == CODEX_MINI_MODEL
    assert BALANCED_CODE_MODEL == CODEX_MINI_MODEL
    assert QUALITY_CODE_MODEL == CODEX_MINI_MODEL
    assert REPAIR_MODEL == CODEX_MINI_MODEL
    assert SUMMARY_MODEL == CODEX_MINI_MODEL
    assert CODEX_MINI_MODEL == "gpt-5.4-mini"
    assert CODEX_MAX_MODEL == "gpt-5.4-mini"


def test_unrelated_env_does_not_change_model_routing(monkeypatch) -> None:
    monkeypatch.setenv("IGNORED_MODEL_ROUTING_FLAG", "false")

    assert models_for_role("agent_turn", model_profile="openai_code_fast", generation_mode=GenerationMode.FAST) == FAST_CODE_MODEL
    assert models_for_role("code_edit", model_profile="research_balanced", generation_mode=GenerationMode.BALANCED) == BALANCED_CODE_MODEL
    assert models_for_role("repair", model_profile="openai_code_quality", generation_mode=GenerationMode.QUALITY) == REPAIR_MODEL


def test_code_agent_profiles_route_all_modes_to_gpt_54_mini_by_default() -> None:
    routed_agent_models = {str(profile["routing"]["agent_turn"]) for profile in TASK_PROFILES.values()}
    assert routed_agent_models == {CODEX_MINI_MODEL}
    assert TASK_PROFILES["openai_code_fast"]["routing"]["agent_turn"] == FAST_CODE_MODEL
    assert TASK_PROFILES["research_balanced"]["routing"]["agent_turn"] == BALANCED_CODE_MODEL
    assert TASK_PROFILES["openai_code_quality"]["routing"]["agent_turn"] == QUALITY_CODE_MODEL
    for profile_name in ("openai_code_fast", "research_balanced", "openai_code_quality"):
        routing = TASK_PROFILES[profile_name]["routing"]
        assert routing["agent_turn"] == CODEX_MINI_MODEL
        assert routing["code_edit"] == CODEX_MINI_MODEL
        assert routing["repair"] == REPAIR_MODEL
        assert routing["summarize"] == SUMMARY_MODEL
        assert routing["cheap_task"] == SUMMARY_MODEL
