from __future__ import annotations

from app.ai.model_registry import TASK_PROFILES
from app.models.common import GenerationMode
from app.services.engine.mode_profiles import ModeProfiles


class TaskRouter:
    def profile_snapshot(self, *, model_profile: str, generation_mode: GenerationMode | str, run_mode: str) -> dict[str, object]:
        requested_profile = TASK_PROFILES.get(model_profile) or TASK_PROFILES["openai_code_fast"]
        mode_profile = ModeProfiles.resolve(generation_mode)
        return {
            "requested_profile": model_profile,
            "resolved_profile": requested_profile["label"],
            "generation_mode": mode_profile.mode,
            "run_mode": run_mode,
            "planner_effort": mode_profile.planner_effort,
            "editor_effort": mode_profile.editor_effort,
            "repair_effort": mode_profile.repair_effort,
            "routing": dict(requested_profile.get("routing") or {}),
        }

    def route_for_phase(
        self,
        *,
        phase: str,
        model_profile: str,
        generation_mode: GenerationMode | str,
        run_mode: str,
        target_file_count: int = 0,
        repeated_failure: bool = False,
    ) -> dict[str, object]:
        snapshot = self.profile_snapshot(
            model_profile=model_profile,
            generation_mode=generation_mode,
            run_mode=run_mode,
        )
        routing = dict(snapshot["routing"])
        task_name = {
            "intent": "intent_classification",
            "scope": "scope_planning",
            "plan": "code_plan",
            "retrieve": "retrieval_rerank",
            "edit": "code_edit",
            "repair": "repair_edit" if repeated_failure else "repair_triage",
            "compact": "summary_compact",
        }.get(phase, "cheap_background_task")
        model = routing.get(task_name) or routing.get("code_edit") or routing.get("cheap_background_task")
        effort = {
            "intent": snapshot["planner_effort"],
            "scope": snapshot["planner_effort"],
            "plan": snapshot["planner_effort"],
            "retrieve": snapshot["planner_effort"],
            "edit": snapshot["editor_effort"],
            "repair": snapshot["repair_effort"],
            "compact": "low",
        }.get(phase, "low")
        strategy = "narrow_path" if target_file_count and target_file_count <= 4 else "workspace"
        if run_mode == "fix":
            strategy = "failure_packet"
        return {
            "phase": phase,
            "task_name": task_name,
            "model": model,
            "effort": effort,
            "strategy": strategy,
        }
