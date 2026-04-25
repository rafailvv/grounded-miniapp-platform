from __future__ import annotations

from app.ai.model_registry import CODEX_MINI_MODEL, TASK_PROFILES, models_for_role
from app.models.common import GenerationMode


def test_all_generation_modes_route_agent_turn_to_codex_mini(monkeypatch) -> None:
    monkeypatch.delenv("CHIP", raising=False)

    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.FAST) == CODEX_MINI_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.BALANCED) == CODEX_MINI_MODEL
    assert models_for_role("agent_turn", model_profile=None, generation_mode=GenerationMode.QUALITY) == CODEX_MINI_MODEL


def test_chip_env_does_not_change_model_routing(monkeypatch) -> None:
    monkeypatch.setenv("CHIP", "false")

    assert models_for_role("agent_turn", model_profile="openai_code_fast", generation_mode=GenerationMode.FAST) == CODEX_MINI_MODEL
    assert models_for_role("code_edit", model_profile="research_balanced", generation_mode=GenerationMode.BALANCED) == CODEX_MINI_MODEL
    assert models_for_role("repair", model_profile="openai_code_quality", generation_mode=GenerationMode.QUALITY) == CODEX_MINI_MODEL


def test_every_code_agent_profile_uses_codex_mini() -> None:
    for profile in TASK_PROFILES.values():
        routing = profile["routing"]
        assert routing["agent_turn"] == CODEX_MINI_MODEL
        assert routing["code_edit"] == CODEX_MINI_MODEL
        assert routing["repair"] == CODEX_MINI_MODEL
        assert routing["summarize"] == CODEX_MINI_MODEL
        assert routing["cheap_task"] == CODEX_MINI_MODEL
