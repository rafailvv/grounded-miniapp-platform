from __future__ import annotations

from dataclasses import asdict, dataclass

from app.models.common import GenerationMode


@dataclass(frozen=True)
class ModeProfile:
    mode: str
    context_code_limit: int
    context_doc_limit: int
    targeted_file_limit: int
    edit_iteration_limit: int
    repair_attempt_limit: int
    verification_depth: str
    compact_aggressiveness: str
    agent_effort: str
    editor_effort: str
    repair_effort: str
    acceptance_check_multiplier: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ModeProfiles:
    _PROFILES = {
        GenerationMode.FAST: ModeProfile(
            mode="fast",
            context_code_limit=2,
            context_doc_limit=0,
            targeted_file_limit=5,
            edit_iteration_limit=1,
            repair_attempt_limit=2,
            verification_depth="fast",
            compact_aggressiveness="high",
            agent_effort="low",
            editor_effort="medium",
            repair_effort="medium",
            acceptance_check_multiplier=1,
        ),
        GenerationMode.BALANCED: ModeProfile(
            mode="balanced",
            context_code_limit=6,
            context_doc_limit=4,
            targeted_file_limit=18,
            edit_iteration_limit=3,
            repair_attempt_limit=5,
            verification_depth="balanced",
            compact_aggressiveness="medium",
            agent_effort="medium",
            editor_effort="high",
            repair_effort="high",
            acceptance_check_multiplier=2,
        ),
        GenerationMode.QUALITY: ModeProfile(
            mode="quality",
            context_code_limit=8,
            context_doc_limit=5,
            targeted_file_limit=24,
            edit_iteration_limit=4,
            repair_attempt_limit=6,
            verification_depth="deep",
            compact_aggressiveness="low",
            agent_effort="high",
            editor_effort="high",
            repair_effort="high",
            acceptance_check_multiplier=3,
        ),
        GenerationMode.BASIC: ModeProfile(
            mode="basic",
            context_code_limit=2,
            context_doc_limit=0,
            targeted_file_limit=4,
            edit_iteration_limit=1,
            repair_attempt_limit=1,
            verification_depth="basic",
            compact_aggressiveness="high",
            agent_effort="low",
            editor_effort="low",
            repair_effort="low",
            acceptance_check_multiplier=1,
        ),
    }

    @classmethod
    def resolve(cls, generation_mode: GenerationMode | str) -> ModeProfile:
        mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(generation_mode)
        return cls._PROFILES[mode]
