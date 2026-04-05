from __future__ import annotations

from app.models.common import GenerationMode
from app.services.engine.mode_profiles import ModeProfiles


class ContextBudgetManager:
    def build_budget(
        self,
        *,
        generation_mode: GenerationMode | str,
        target_file_count: int = 0,
        run_mode: str = "generate",
    ) -> dict[str, int | str | bool]:
        profile = ModeProfiles.resolve(generation_mode)
        narrow_path = target_file_count > 0 and target_file_count <= max(4, profile.targeted_file_limit // 2)
        retrieval_chunks = profile.planning_code_limit + max(1, target_file_count // 3)
        file_bodies = min(profile.targeted_file_limit, max(target_file_count, profile.planning_code_limit))
        failure_packet = 9000 if run_mode == "fix" else 3000
        return {
            "stable_prefix": 2500,
            "workspace_summary": 800,
            "retrieval_chunks": retrieval_chunks,
            "file_bodies": file_bodies,
            "recent_diff_chars": 8000 if profile.mode != "fast" else 3000,
            "failure_packet_chars": failure_packet,
            "compact_summary_chars": 1200 if profile.mode == "fast" else 2400 if profile.mode == "balanced" else 4200,
            "narrow_path": narrow_path,
            "targeted_file_limit": profile.targeted_file_limit,
            "edit_iteration_limit": profile.edit_iteration_limit,
            "repair_attempt_limit": profile.repair_attempt_limit,
            "verification_depth": profile.verification_depth,
        }

    def trim_paths(self, *, paths: list[str], generation_mode: GenerationMode | str) -> list[str]:
        profile = ModeProfiles.resolve(generation_mode)
        if len(paths) <= profile.targeted_file_limit:
            return list(paths)
        return list(paths[: profile.targeted_file_limit])
