from __future__ import annotations

from typing import Any

from app.models.common import GenerationMode
from app.models.context_manager import ContextBudgetPolicy, ContextBudgetSection
from app.services.engine.mode_profiles import ModeProfiles


class ContextBudgetManager:
    SECTION_ORDER: tuple[str, ...] = (
        "stable_prefix",
        "current_task",
        "project_instructions",
        "workspace_memory",
        "skills",
        "session_tail",
        "transcript",
        "tool_results",
        "code_context",
        "targeted_files",
        "recent_diff",
        "artifacts",
        "proofs",
        "checks",
        "diagnostics",
    )

    def build_budget(
        self,
        *,
        generation_mode: GenerationMode | str,
        target_file_count: int = 0,
        run_mode: str = "generate",
    ) -> dict[str, int | str | bool]:
        profile = ModeProfiles.resolve(generation_mode)
        narrow_path = target_file_count > 0 and target_file_count <= max(4, profile.targeted_file_limit // 2)
        retrieval_chunks = profile.context_code_limit + max(1, target_file_count // 3)
        file_bodies = min(profile.targeted_file_limit, max(target_file_count, profile.context_code_limit))
        failure_packet = 9000 if run_mode == "fix" else 3000
        recent_diff_chars = 8000
        if profile.mode == "fast":
            recent_diff_chars = 1500 if run_mode == "fix" else 0
        elif profile.mode == "balanced":
            recent_diff_chars = 5000
        return {
            "stable_prefix": 2500,
            "workspace_summary": 800,
            "retrieval_chunks": retrieval_chunks,
            "file_bodies": file_bodies,
            "recent_diff_chars": recent_diff_chars,
            "failure_packet_chars": failure_packet,
            "compact_summary_chars": 1200 if profile.mode == "fast" else 2400 if profile.mode == "balanced" else 4200,
            "narrow_path": narrow_path,
            "targeted_file_limit": profile.targeted_file_limit,
            "edit_iteration_limit": profile.edit_iteration_limit,
            "repair_attempt_limit": profile.repair_attempt_limit,
            "verification_depth": profile.verification_depth,
        }

    def build_policy(
        self,
        *,
        generation_mode: GenerationMode | str,
        run_mode: str = "generate",
        target_file_count: int = 0,
        context_window_tokens: int = 128_000,
    ) -> ContextBudgetPolicy:
        budget = self.build_budget(
            generation_mode=generation_mode,
            target_file_count=target_file_count,
            run_mode=run_mode,
        )
        profile = ModeProfiles.resolve(generation_mode)
        target_ratio = 0.62 if profile.mode == "fast" else 0.72 if profile.mode == "balanced" else 0.80
        sections = self._policy_sections(profile_mode=profile.mode, budget=budget, context_window_tokens=context_window_tokens)
        return ContextBudgetPolicy(
            generation_mode=profile.mode,
            run_mode=str(run_mode or "generate"),
            context_window_tokens=context_window_tokens,
            target_prompt_tokens=int(context_window_tokens * target_ratio),
            compact_threshold_ratio=0.72 if profile.mode == "fast" else 0.80,
            tool_result_tail=6 if profile.mode == "fast" else 10 if profile.mode == "balanced" else 14,
            sections=sections,
        )

    def trim_paths(self, *, paths: list[str], generation_mode: GenerationMode | str) -> list[str]:
        profile = ModeProfiles.resolve(generation_mode)
        if len(paths) <= profile.targeted_file_limit:
            return list(paths)
        return list(paths[: profile.targeted_file_limit])

    def _policy_sections(
        self,
        *,
        profile_mode: str,
        budget: dict[str, Any],
        context_window_tokens: int,
    ) -> dict[str, ContextBudgetSection]:
        multiplier = 0.75 if profile_mode == "fast" else 1.0 if profile_mode == "balanced" else 1.3

        def tokens(value: int, *, floor: int = 256) -> int:
            return max(floor, int(value * multiplier))

        return {
            "stable_prefix": ContextBudgetSection(key="stable_prefix", priority=100, budget_tokens=tokens(900), always_load=True, overflow_action="include"),
            "current_task": ContextBudgetSection(key="current_task", priority=100, budget_tokens=tokens(1800), always_load=True, overflow_action="summarize"),
            "project_instructions": ContextBudgetSection(key="project_instructions", priority=92, budget_tokens=tokens(1400), always_load=True, overflow_action="summarize"),
            "workspace_memory": ContextBudgetSection(key="workspace_memory", priority=82, budget_tokens=tokens(2200), overflow_action="summarize"),
            "skills": ContextBudgetSection(key="skills", priority=80, budget_tokens=tokens(1600), overflow_action="summarize"),
            "session_tail": ContextBudgetSection(key="session_tail", priority=78, budget_tokens=tokens(2400), overflow_action="summarize"),
            "transcript": ContextBudgetSection(key="transcript", priority=65, budget_tokens=tokens(7000), overflow_action="defer"),
            "tool_results": ContextBudgetSection(key="tool_results", priority=70, budget_tokens=tokens(9000), overflow_action="microcompact"),
            "code_context": ContextBudgetSection(key="code_context", priority=86, budget_tokens=tokens(8000), overflow_action="summarize"),
            "targeted_files": ContextBudgetSection(key="targeted_files", priority=90, budget_tokens=tokens(12_000), overflow_action="summarize"),
            "recent_diff": ContextBudgetSection(key="recent_diff", priority=76, budget_tokens=max(300, int(budget.get("recent_diff_chars", 3000) or 3000) // 4), overflow_action="summarize"),
            "artifacts": ContextBudgetSection(key="artifacts", priority=60, budget_tokens=tokens(2400), overflow_action="artifact_ref"),
            "proofs": ContextBudgetSection(key="proofs", priority=84, budget_tokens=tokens(5000), overflow_action="artifact_ref"),
            "checks": ContextBudgetSection(key="checks", priority=88, budget_tokens=tokens(6500), overflow_action="summarize"),
            "diagnostics": ContextBudgetSection(key="diagnostics", priority=74, budget_tokens=tokens(4500), overflow_action="summarize"),
        }
