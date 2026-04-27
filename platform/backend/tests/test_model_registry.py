from __future__ import annotations

from app.ai.model_registry import BALANCED_CODE_MODEL, CODEX_MINI_MODEL, QUALITY_CODE_MODEL, TASK_PROFILES, models_for_role
from app.models.common import GenerationMode


def test_generation_modes_route_agent_turn_to_distinct_default_profiles() -> None:
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.FAST) == CODEX_MINI_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.BALANCED) == BALANCED_CODE_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.QUALITY) == QUALITY_CODE_MODEL
    assert len({CODEX_MINI_MODEL, BALANCED_CODE_MODEL, QUALITY_CODE_MODEL}) >= 2


def test_unrelated_env_does_not_change_model_routing(monkeypatch) -> None:
    monkeypatch.setenv("IGNORED_MODEL_ROUTING_FLAG", "false")

    assert models_for_role("agent_turn", model_profile="openai_code_fast", generation_mode=GenerationMode.FAST) == CODEX_MINI_MODEL
    assert models_for_role("code_edit", model_profile="research_balanced", generation_mode=GenerationMode.BALANCED) == BALANCED_CODE_MODEL
    assert models_for_role("repair", model_profile="openai_code_quality", generation_mode=GenerationMode.QUALITY) == BALANCED_CODE_MODEL


def test_code_agent_profiles_are_not_all_the_same_model() -> None:
    routed_agent_models = {str(profile["routing"]["agent_turn"]) for profile in TASK_PROFILES.values()}
    assert len(routed_agent_models) >= 2
    assert TASK_PROFILES["openai_code_fast"]["routing"]["agent_turn"] == CODEX_MINI_MODEL
    assert TASK_PROFILES["research_balanced"]["routing"]["agent_turn"] == BALANCED_CODE_MODEL
    assert TASK_PROFILES["openai_code_quality"]["routing"]["agent_turn"] == QUALITY_CODE_MODEL
    for profile in TASK_PROFILES.values():
        routing = profile["routing"]
        assert routing["summarize"] == CODEX_MINI_MODEL
        assert routing["cheap_task"] == CODEX_MINI_MODEL
