from __future__ import annotations

from dataclasses import asdict, dataclass

from app.models.common import GenerationMode


@dataclass(frozen=True)
class ModeProfile:
    mode: str
    planning_code_limit: int
    planning_doc_limit: int
    targeted_file_limit: int
    edit_iteration_limit: int
    repair_attempt_limit: int
    verification_depth: str
    compact_aggressiveness: str
    planner_effort: str
    editor_effort: str
    repair_effort: str
    acceptance_check_multiplier: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ModeProfiles:
    _PROFILES = {
        GenerationMode.FAST: ModeProfile(
            mode="fast",
            planning_code_limit=3,
            planning_doc_limit=1,
            targeted_file_limit=8,
            edit_iteration_limit=1,
            repair_attempt_limit=2,
            verification_depth="fast",
            compact_aggressiveness="high",
            planner_effort="low",
            editor_effort="medium",
            repair_effort="medium",
            acceptance_check_multiplier=1,
        ),
        GenerationMode.BALANCED: ModeProfile(
            mode="balanced",
            planning_code_limit=5,
            planning_doc_limit=3,
            targeted_file_limit=14,
            edit_iteration_limit=2,
            repair_attempt_limit=4,
            verification_depth="balanced",
            compact_aggressiveness="medium",
            planner_effort="medium",
            editor_effort="medium",
            repair_effort="medium",
            acceptance_check_multiplier=2,
        ),
        GenerationMode.QUALITY: ModeProfile(
            mode="quality",
            planning_code_limit=7,
            planning_doc_limit=4,
            targeted_file_limit=20,
            edit_iteration_limit=3,
            repair_attempt_limit=6,
            verification_depth="deep",
            compact_aggressiveness="low",
            planner_effort="high",
            editor_effort="high",
            repair_effort="high",
            acceptance_check_multiplier=3,
        ),
        GenerationMode.BASIC: ModeProfile(
            mode="basic",
            planning_code_limit=2,
            planning_doc_limit=0,
            targeted_file_limit=4,
            edit_iteration_limit=1,
            repair_attempt_limit=1,
            verification_depth="basic",
            compact_aggressiveness="high",
            planner_effort="low",
            editor_effort="low",
            repair_effort="low",
            acceptance_check_multiplier=1,
        ),
    }

    @classmethod
    def resolve(cls, generation_mode: GenerationMode | str) -> ModeProfile:
        mode = generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(generation_mode)
        return cls._PROFILES[mode]
