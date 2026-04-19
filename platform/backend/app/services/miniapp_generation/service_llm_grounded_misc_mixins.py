from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable

from app.models.artifacts import RunOutcomeKind
from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import JobRecord, ValidationSnapshot
from app.models.grounded_spec import APIRequirement, Actor, Assumption, Contradiction, EntityAttribute, GroundedSpecModel, UserFlow
from app.modules.miniapp_generation_runtime import MiniappGroundedSpecBuilder, build_route_manifest, select_creative_direction
from app.services.miniapp_generation.constants import ROLE_ORDER


class ServiceLlmGroundedMiscMixins:
    @staticmethod
    def _llm_cache_kwargs() -> dict[str, str]:
        from app.services.miniapp_generation.service import ACTIVE_LLM_CACHE_CONTEXT
        context = ACTIVE_LLM_CACHE_CONTEXT.get() or {}
        prompt_cache_key = str(context.get("prompt_cache_key") or "").strip()
        stable_prefix = str(context.get("stable_prefix") or "").strip()
        payload: dict[str, str] = {}
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if stable_prefix:
            payload["stable_prefix"] = stable_prefix
        return payload

    @staticmethod
    def _record_llm_cache_stats(result: dict[str, Any]) -> None:
        from app.services.miniapp_generation.service import ACTIVE_LLM_CACHE_STATS
        sink = ACTIVE_LLM_CACHE_STATS.get()
        if sink is None:
            return
        sink["llm_requests"] = int(sink.get("llm_requests", 0)) + 1
        response_stats = result.get("cache_stats")
        if not isinstance(response_stats, dict):
            return
        sink["cached_tokens"] = int(sink.get("cached_tokens", 0)) + int(response_stats.get("cached_tokens", 0) or 0)
        sink["cache_write_tokens"] = int(sink.get("cache_write_tokens", 0)) + int(response_stats.get("cache_write_tokens", 0) or 0)

    @staticmethod
    def _is_retryable_llm_error(error: Exception) -> bool:
        text = str(error).lower()
        retry_markers = (" returned 429", " returned 500", " returned 502", " returned 503", " returned 504", "nodename nor servname provided", "name or service not known", "temporary failure in name resolution", "failed to resolve", "connecterror", "requesterror", "connection error", "connection refused", "connection aborted", "internal_server_error", "rate limit", "timed out", "timeout", "connection reset", "temporarily unavailable", "returned non-json text", "returned empty text instead of json", "instead of json", "returned no file operations", "no file operations for the requested target_files")
        return any(marker in text for marker in retry_markers)

    @staticmethod
    def _tighten_json_retry_kwargs(request_kwargs: dict[str, Any], error: Exception, attempt: int) -> dict[str, Any]:
        retry_note = (
            "JSON retry instruction:\n"
            f"- Previous attempt #{attempt + 1} returned invalid JSON.\n"
            f"- Error: {str(error)[:400]}\n"
            "- Return exactly one JSON object.\n"
            "- Do not return two objects.\n"
            "- Do not include analysis, commentary, markdown fences, or any text before or after the JSON.\n"
            "- If no file operations are needed, still return one valid JSON object with the required keys."
        )
        tightened = dict(request_kwargs)
        tightened["system_prompt"] = f"{str(request_kwargs.get('system_prompt') or '').rstrip()}\n\n{retry_note}".strip()
        tightened["user_prompt"] = f"{str(request_kwargs.get('user_prompt') or '').rstrip()}\n\n{retry_note}".strip()
        return tightened

    @staticmethod
    def _contains_non_english_control_text(text: str) -> bool:
        return bool(re.search(r"[\u0400-\u04FF]", text))

    @classmethod
    def _assert_english_control_text(cls, *texts: str) -> None:
        invalid = [text for text in texts if isinstance(text, str) and cls._contains_non_english_control_text(text)]
        if invalid:
            raise ValueError("Control prompts must remain English-only.")

    @classmethod
    def validate_prompt_assets_are_english(cls) -> list[str]:
        prompt_dir = Path(__file__).resolve().parents[1] / "ai" / "prompts"
        invalid_files: list[str] = []
        for file_path in sorted(prompt_dir.glob("*.md")):
            content = file_path.read_text(encoding="utf-8")
            if cls._contains_non_english_control_text(content):
                invalid_files.append(str(file_path))
        return invalid_files

    def _generate_structured_with_retry(self, **kwargs: Any) -> dict[str, Any]:
        request_kwargs = {**self._llm_cache_kwargs(), **kwargs}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._invoke_llm_with_timeout(self.openrouter_client.generate_structured, timeout_seconds=float(self.STRUCTURED_LLM_TIMEOUT_SECONDS), **request_kwargs)
                self._record_llm_cache_stats(result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not self._is_retryable_llm_error(exc):
                    raise
                if self._should_tighten_json_retry(exc):
                    request_kwargs = self._tighten_json_retry_kwargs(request_kwargs, exc, attempt)
                time.sleep(0.8 * (attempt + 1))
        raise last_error  # type: ignore[misc]

    def _generate_json_object_with_retry(self, **kwargs: Any) -> dict[str, Any]:
        request_kwargs = {**self._llm_cache_kwargs(), **kwargs}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._invoke_llm_with_timeout(self.openrouter_client.generate_json_object, timeout_seconds=float(self.JSON_OBJECT_LLM_TIMEOUT_SECONDS), **request_kwargs)
                self._record_llm_cache_stats(result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not self._is_retryable_llm_error(exc):
                    raise
                if self._should_tighten_json_retry(exc):
                    request_kwargs = self._tighten_json_retry_kwargs(request_kwargs, exc, attempt)
                time.sleep(0.8 * (attempt + 1))
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _invoke_llm_with_timeout(func: Any, *, timeout_seconds: float, **kwargs: Any) -> dict[str, Any]:
        role = str(kwargs.get("role") or "llm")
        schema_name = str(kwargs.get("schema_name") or "request")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"llm-{role}")
        context = copy_context()
        future = executor.submit(lambda: context.run(func, **kwargs))
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(f"Timed out waiting for {role} structured generation ({schema_name}) after {int(timeout_seconds)}s.") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _submit_with_context(executor: ThreadPoolExecutor, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        context = copy_context()
        return executor.submit(lambda: context.run(func, *args, **kwargs))

    @staticmethod
    def _should_tighten_json_retry(error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in ("invalid json", "returned non-json text", "returned empty text instead of json", "instead of json", "jsondecodeerror"))

    def _stabilize_grounded_spec(self, spec: GroundedSpecModel) -> GroundedSpecModel:
        return self.grounded_spec_builder.stabilize_grounded_spec(spec)

    @staticmethod
    def _is_forbidden_generated_api_requirement(requirement: APIRequirement) -> bool:
        return MiniappGroundedSpecBuilder.is_forbidden_generated_api_requirement(requirement)

    @staticmethod
    def _is_forbidden_outline_api_need(item: str) -> bool:
        return MiniappGroundedSpecBuilder.is_forbidden_outline_api_need(item)

    @classmethod
    def _sanitize_grounded_spec_outline(cls, outline: dict[str, Any]) -> dict[str, Any]:
        return MiniappGroundedSpecBuilder.sanitize_grounded_spec_outline(outline)

    @staticmethod
    def _is_forbidden_spec_governance_text(text: str) -> bool:
        return MiniappGroundedSpecBuilder.is_forbidden_spec_governance_text(text)

    def _build_grounded_spec(self, **kwargs: Any) -> GroundedSpecModel:
        return self.grounded_spec_builder.build_grounded_spec(**kwargs)

    @staticmethod
    def _build_route_manifest(runtime_manifest: dict[str, Any]) -> dict[str, Any]:
        return build_route_manifest(runtime_manifest, role_order=ROLE_ORDER)

    @staticmethod
    def _select_creative_direction(prompt: str) -> dict[str, Any]:
        return select_creative_direction(prompt)

    def _expand_role_actors(self, actors: list[Actor], doc_refs: list[Any]) -> list[Actor]:
        return self.grounded_spec_builder.expand_role_actors(actors, doc_refs)

    def _expand_role_flows(self, spec: GroundedSpecModel, actors: list[Actor]) -> list[UserFlow]:
        return self.grounded_spec_builder.expand_role_flows(spec, actors)

    def _ensure_role_expansion_assumption(self, spec: GroundedSpecModel, assumptions: list[Assumption], actors: list[Actor]) -> list[Assumption]:
        return self.grounded_spec_builder.ensure_role_expansion_assumption(spec, assumptions, actors)

    def _block_job(self, job: JobRecord, validation_result: Any, assumptions: list[Any], *, failure_reason: str) -> None:
        job.status = "blocked"
        job.fidelity = "blocked"
        job.failure_reason = failure_reason
        job.assumptions_report = [item.model_dump(mode="json") for item in assumptions]
        job.validation_snapshot = ValidationSnapshot(build_valid=False, blocking=getattr(validation_result, "blocking", True), issues=[issue.model_dump(mode="json") for issue in getattr(validation_result, "issues", [])])
        self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))

    def _block_with_messages(self, job: JobRecord, messages: list[str], *, code: str, event_type: str, failure_reason: str) -> JobRecord:
        job.status = "blocked"
        job.fidelity = "blocked"
        job.outcome_kind = self._outcome_kind_for_failure(code)
        job.failure_reason = failure_reason
        job.failure_class = job.failure_class or code
        job.root_cause_summary = job.root_cause_summary or (messages[0] if messages else failure_reason)
        self._append_event(job, event_type, failure_reason)
        self._append_trace(job.workspace_id, "job_blocked", failure_reason, {"messages": messages, "code": code})
        return job

    @staticmethod
    def _outcome_kind_for_failure(code: str) -> RunOutcomeKind:
        if code.startswith("preview.") or code.startswith("generation.preview.") or code.startswith("runtime.preview."):
            return "blocked_preview_infra"
        return "blocked_generation"

    def _stop_if_requested(self, job: JobRecord, workspace_id: str, should_stop: Callable[[], bool] | None) -> JobRecord | None:
        if not should_stop or not should_stop():
            return None
        job.status = "blocked"
        job.fidelity = "blocked"
        job.failure_reason = "Run stopped by user."
        job.failure_class = "stopped_by_user"
        job.root_cause_summary = "Run stopped by user."
        job.validation_snapshot = ValidationSnapshot(grounded_spec_valid=True, app_ir_valid=True, build_valid=False, blocking=False, issues=[])
        self._store_report(f"validation:{workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        self._append_event(job, "job_failed", "Run stopped by user.")
        self._append_trace(workspace_id, "job_stopped", "Run stopped by user.", {})
        return job

    @staticmethod
    def _infer_entity_name(prompt: str) -> str:
        return MiniappGroundedSpecBuilder.infer_entity_name(prompt)

    @staticmethod
    def _infer_entity_attributes(prompt: str) -> list[EntityAttribute]:
        return MiniappGroundedSpecBuilder.infer_entity_attributes(prompt)

    @staticmethod
    def _detect_contradictions(prompt: str) -> list[Contradiction]:
        return MiniappGroundedSpecBuilder.detect_contradictions(prompt)

    @staticmethod
    def _is_commerce_prompt(prompt: str) -> bool:
        return MiniappGroundedSpecBuilder.is_commerce_prompt(prompt)

    @staticmethod
    def _target_platform(target_platform: TargetPlatform | str) -> TargetPlatform:
        return target_platform if isinstance(target_platform, TargetPlatform) else TargetPlatform(target_platform)

    @staticmethod
    def _preview_profile(preview_profile: PreviewProfile | str) -> PreviewProfile:
        return preview_profile if isinstance(preview_profile, PreviewProfile) else PreviewProfile(preview_profile)

    @staticmethod
    def _generation_mode(generation_mode: GenerationMode | str) -> GenerationMode:
        return generation_mode if isinstance(generation_mode, GenerationMode) else GenerationMode(generation_mode)

    @staticmethod
    def _clean_generated_text(content: str) -> str:
        return "".join(ch for ch in content if ch in "\n\r\t" or (32 <= ord(ch) and ord(ch) != 0x7F and not 0x80 <= ord(ch) < 0xA0) or ord(ch) in {0x85, 0xA0})

    @staticmethod
    def _strip_llm_sentinel_lines(content: str) -> str:
        lines = content.splitlines()
        cleaned_lines: list[str] = []
        sentinel_pattern = re.compile(r"^\*{3}\s*end of file\s*\*{3}$", re.IGNORECASE)
        for line in lines:
            if sentinel_pattern.match(line.strip()):
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)
        if content.endswith("\n") and not cleaned.endswith("\n"):
            cleaned += "\n"
        return cleaned

    @classmethod
    def _sanitize_draft_operations(cls, operations: list[Any]) -> list[Any]:
        sanitized: list[Any] = []
        for operation in operations:
            if operation.content is None:
                sanitized.append(operation)
                continue
            cleaned = cls._strip_llm_sentinel_lines(cls._clean_generated_text(operation.content))
            sanitized.append(operation if cleaned == operation.content else operation.model_copy(update={"content": cleaned}))
        return sanitized

    @staticmethod
    def _is_recoverable_page_error(exc: Exception) -> bool:
        return ServiceLlmGroundedMiscMixins._is_recoverable_page_error_message(str(exc))

    @staticmethod
    def _is_recoverable_page_error_message(message: str) -> bool:
        lowered = message.lower()
        markers = ("did not return operations", "did not produce a valid create/replace operation", "returned operations for other files", "must be generated as a single create/replace operation")
        return any(marker in lowered for marker in markers)
