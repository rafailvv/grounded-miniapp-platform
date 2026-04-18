from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from app.models.domain import DraftFileOperation, FixAttemptOutcome
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixPatchingRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def repair_outcome_from_response(
        self,
        *,
        llm_result: dict[str, Any],
        prompt_context: FixPromptContext,
        fix_turn: FixTurnContext,
        scope_entries,
        scope_expansions: list[dict[str, Any]],
    ) -> FixAttemptOutcome:
        if "error" in llm_result:
            return FixAttemptOutcome(
                outcome="fatal_invalid_response",
                validation_error=str(llm_result["error"]),
                raw_response=llm_result,
            )
        raw_operations = llm_result.get("operations") or []
        diagnosis_text = str(llm_result.get("diagnosis") or "")
        planned_targets = self.service._planned_target_paths(llm_result)
        outcome_hint = str(llm_result.get("outcome") or "").strip().lower()
        if outcome_hint == "needs_more_context":
            return FixAttemptOutcome(
                outcome="needs_more_context",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        if not raw_operations and (self.service._looks_like_context_refusal(diagnosis_text) or planned_targets):
            return FixAttemptOutcome(
                outcome="needs_more_context",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        try:
            operations = self.coerce_operations(
                raw_operations,
                scope_entries=scope_entries,
                fix_turn=fix_turn,
                scope_expansions=scope_expansions,
            )
        except Exception as exc:
            if self.should_retry_patch_validation(str(exc)):
                return FixAttemptOutcome(
                    outcome="needs_more_context",
                    diagnosis=diagnosis_text,
                    planned_targets=planned_targets,
                    validation_error=str(exc),
                    expected_verification=str(llm_result.get("expected_verification") or ""),
                    rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                    raw_response=llm_result,
                )
            return FixAttemptOutcome(
                outcome="no_progress",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                validation_error=str(exc),
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        if not operations:
            return FixAttemptOutcome(
                outcome="no_progress",
                diagnosis=diagnosis_text,
                planned_targets=planned_targets,
                validation_error="Repair model did not return any patch operations.",
                expected_verification=str(llm_result.get("expected_verification") or ""),
                rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
                raw_response=llm_result,
            )
        return FixAttemptOutcome(
            outcome="patch_ready",
            diagnosis=diagnosis_text,
            operations=operations,
            planned_targets=planned_targets,
            expected_verification=str(llm_result.get("expected_verification") or ""),
            rationale_by_file=dict(llm_result.get("rationale_by_file") or {}),
            raw_response=llm_result,
        )

    def plan_patch(
        self,
        *,
        job,
        prompt_context: FixPromptContext,
        repair_feedback: str | None = None,
    ) -> dict[str, Any]:
        if not self.service.openrouter_client.enabled:
            return {"error": "Fix mode requires an enabled LLM provider or a deterministic local repair path."}
        job.current_fix_phase = "patching"
        self.service._save_job(job)
        prompt_cache_key = self.service._prompt_cache_key(prompt_context)
        try:
            payload = self.service.openrouter_client.generate_workspace_edits(
                schema_name="fix_patch_v1",
                schema=self.service._repair_schema(),
                system_prompt=self.service._repair_system_prompt(),
                user_prompt=self.service._repair_user_prompt(prompt_context, repair_feedback=repair_feedback),
                prompt_cache_key=prompt_cache_key,
                stable_prefix=self.service._repair_system_prompt(),
            )
            job.llm_model = str(payload["model"])
            job.cache_stats = self.merge_cache_stats(job.cache_stats, payload.get("cache_stats") or {})
            self.service._save_job(job)
            normalized = payload["payload"]
            if isinstance(normalized, str):
                normalized = json.loads(normalized)
            return normalized if isinstance(normalized, dict) else {"error": "Repair model returned an invalid payload."}
        except Exception as exc:
            logger.exception(
                "fix_patch_generation_failed workspace_id=%s run_id=%s",
                prompt_context.workspace_id,
                prompt_context.run_id,
            )
            return {"error": f"Repair patch generation failed: {exc}"}

    def coerce_operations(
        self,
        raw_operations: list[Any],
        scope_entries,
        fix_turn: FixTurnContext,
        scope_expansions: list[dict[str, Any]],
    ) -> list[DraftFileOperation]:
        scope_paths = {entry.file_path for entry in scope_entries}
        operations: list[DraftFileOperation] = []
        for index, item in enumerate(raw_operations):
            operation = DraftFileOperation.model_validate(item)
            if operation.operation in {"create", "replace"} and operation.content is None:
                raise ValueError(f"Repair returned {operation.operation} for {operation.file_path} without content.")
            framework_validation_error = self.backend_framework_validation_error(operation)
            if framework_validation_error:
                raise ValueError(framework_validation_error)
            if operation.file_path.startswith("miniapp/tests/") and not self.allow_test_file_writes(fix_turn):
                raise ValueError(f"Repair attempted to edit generated tests instead of the app surface: {operation.file_path}")
            if self.is_read_only_generated_surface(operation.file_path):
                raise ValueError(f"Repair attempted to edit a generated manifest surface instead of the app bundle: {operation.file_path}")
            if operation.file_path not in scope_paths:
                if len(scope_expansions) >= self.service.MAX_SCOPE_EXPANSIONS or not self.can_expand_for_file(operation.file_path, fix_turn.implicated_files):
                    raise ValueError(f"Repair touched files outside the allowed evidence-based scope: {operation.file_path}")
                scope_expansions.append(
                    {
                        "attempt": fix_turn.attempt,
                        "files": [operation.file_path],
                        "reason": "Repair model requested an adjacent evidence-based file.",
                    }
                )
                scope_paths.add(operation.file_path)
            operations.append(
                DraftFileOperation(
                    operation_id=operation.operation_id or f"fix_op_{index}",
                    file_path=operation.file_path,
                    operation=operation.operation,
                    content=operation.content,
                    reason=operation.reason,
                )
            )
        return operations

    @staticmethod
    def is_read_only_generated_surface(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        return normalized in {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        }

    @staticmethod
    def can_expand_for_file(candidate: str, implicated_files: list[str]) -> bool:
        if not implicated_files:
            return candidate.startswith(("miniapp/", "docker/"))
        for file_path in implicated_files:
            if candidate.startswith(file_path.rsplit("/", 1)[0] + "/"):
                return True
            if candidate.split("/", 1)[0] == file_path.split("/", 1)[0]:
                return True
        return candidate.startswith(("docker/", "miniapp/app/", "miniapp/app/static/"))

    @staticmethod
    def operations_missing_content(raw_operations: list[Any]) -> list[str]:
        missing: list[str] = []
        for item in raw_operations:
            if not isinstance(item, dict):
                continue
            operation = str(item.get("operation") or "").strip().lower()
            if operation not in {"create", "replace"}:
                continue
            file_path = str(item.get("file_path") or "").strip()
            if not file_path:
                continue
            if item.get("content") is None:
                missing.append(file_path)
        return list(dict.fromkeys(missing))

    @staticmethod
    def should_retry_patch_validation(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(
            marker in lowered
            for marker in (
                "without content",
                "did not return any patch operations",
                "did not return any file operations",
                "flask/blueprint",
                "must stay on fastapi",
                "must define router = apirouter",
            )
        )

    @staticmethod
    def backend_framework_validation_error(operation: DraftFileOperation) -> str | None:
        file_path = str(operation.file_path or "").replace("\\", "/")
        if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
            return None
        content = str(operation.content or "")
        lowered = content.lower()
        if "from flask import" in lowered or "blueprint(" in lowered:
            return f"{file_path} must stay on FastAPI APIRouter, not Flask/Blueprint."
        if "apirouter" not in lowered:
            return f"{file_path} must stay on FastAPI APIRouter."
        if re.search(r"(?m)^\s*router\s*=\s*APIRouter\(", content) is None:
            return f"{file_path} must define router = APIRouter(...)."
        return None

    @staticmethod
    def allow_test_file_writes_for_failure(failure_class: str | None) -> bool:
        del failure_class
        return False

    @classmethod
    def allow_test_file_writes(cls, fix_turn: FixTurnContext) -> bool:
        del fix_turn
        return False

    @staticmethod
    def merge_cache_stats(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current or {})
        for key, value in dict(incoming or {}).items():
            if isinstance(value, (int, float)):
                merged[key] = (merged.get(key, 0) or 0) + value
            else:
                merged[key] = value
        return merged
