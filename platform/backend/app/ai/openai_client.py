from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
import time
from typing import Any

import httpx

from app.ai.model_registry import (
    MODEL_REGISTRY,
    TASK_PROFILES,
    default_profile_for_generation_mode,
    models_for_role,
    provider_routing_table,
)
from app.core.config import Settings
from app.models.common import GenerationMode
from app.services.agent_runtime_config import ACTIVE_AGENT_TURN_STATE, RetryPolicy, TimeoutProfile
from app.services.workspace.log_service import WorkspaceLogService

logger = logging.getLogger(__name__)
ACTIVE_WORKSPACE_LOG_CONTEXT: ContextVar[str | None] = ContextVar("active_workspace_log_context", default=None)
ACTIVE_LLM_ROUTING_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("active_llm_routing_context", default=None)


class OpenAIClient:
    _LOG_STRING_LIMIT = 4000
    _LOG_LIST_LIMIT = 12
    _LOG_DICT_LIMIT = 40
    _LOG_MAX_DEPTH = 5

    def __init__(self, settings: Settings, workspace_log_service: WorkspaceLogService | None = None) -> None:
        self.settings = settings
        self.workspace_log_service = workspace_log_service
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.timeout_profile = TimeoutProfile.from_env()
        self.retry_policy = RetryPolicy.from_env()
        self.connect_timeout_sec = self.timeout_profile.openai_connect_sec
        self.read_timeout_sec = self.timeout_profile.openai_read_sec
        self.write_timeout_sec = self.timeout_profile.openai_write_sec
        self.pool_timeout_sec = self.timeout_profile.openai_pool_sec
        self.api_key = self.openai_api_key
        self.base_url = self.openai_base_url

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def enabled(self) -> bool:
        return self.openai_enabled

    def configuration(self) -> dict[str, object]:
        default_profile = default_profile_for_generation_mode(GenerationMode.BALANCED)
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "models": MODEL_REGISTRY,
            "task_profiles": TASK_PROFILES,
            "default_coding_profile": default_profile,
            "routing": {
                "provider": "openai" if self.enabled else None,
                "providers": ["openai"] if self.enabled else [],
            },
            "provider_routing": provider_routing_table(),
            "supports_prompt_cache_key": True,
            "mode_profiles": {
                "fast": "openai_code_fast",
                "balanced": default_profile,
                "quality": default_profile_for_generation_mode(GenerationMode.QUALITY),
            },
        }

    @contextmanager
    def workspace_logging(self, workspace_id: str | None) -> Any:
        token = ACTIVE_WORKSPACE_LOG_CONTEXT.set(workspace_id)
        try:
            yield
        finally:
            ACTIVE_WORKSPACE_LOG_CONTEXT.reset(token)

    @contextmanager
    def routing_context(self, *, model_profile: str | None, generation_mode: GenerationMode | str | None) -> Any:
        payload = {
            "model_profile": str(model_profile or "").strip(),
            "generation_mode": str(getattr(generation_mode, "value", generation_mode) or "").strip(),
        }
        token = ACTIVE_LLM_ROUTING_CONTEXT.set(payload)
        try:
            yield
        finally:
            ACTIVE_LLM_ROUTING_CONTEXT.reset(token)

    def generate_agent_tool_step(
        self,
        *,
        tools: list[dict[str, Any]],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        model_override: str | None = None,
        responses_tuning_override: dict[str, Any] | None = None,
        previous_response_id: str | None = None,
        tool_result_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI API key is not configured.")
        routing_context = ACTIVE_LLM_ROUTING_CONTEXT.get() or {}
        requested_profile = str(routing_context.get("model_profile") or "").strip() or None
        generation_mode = str(routing_context.get("generation_mode") or "").strip() or None
        routed_model = models_for_role(
            "agent_turn",
            model_profile=requested_profile,
            generation_mode=generation_mode,
        )
        model = str(model_override or routed_model or MODEL_REGISTRY["agent_turn"]["primary"])
        if model.startswith("gpt-5"):
            payload = self._responses_tool_step(
                role="agent_turn",
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
                tuning_override=responses_tuning_override,
                previous_response_id=previous_response_id,
                tool_result_messages=tool_result_messages,
            )
        else:
            payload = self._chat_tool_step(
                role="agent_turn",
                model=model,
                tools=tools,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
                tool_result_messages=tool_result_messages,
            )
        return {
            "model": model,
            "payload": payload["payload"],
            "response_mode": "tool_calls",
            "cache_stats": payload["cache_stats"],
        }

    def analyze_miniapp_prompt(
        self,
        *,
        prompt: str,
        generation_mode: GenerationMode | str | None,
        model_profile: str | None = None,
    ) -> dict[str, Any]:
        """Ask the model for product contract hints instead of local parsing."""
        if not self.enabled:
            raise RuntimeError("OpenAI API key is not configured.")
        mode_value = str(getattr(generation_mode, "value", generation_mode) or "").strip()
        model = models_for_role("cheap_task", model_profile=model_profile, generation_mode=generation_mode)
        system_prompt = (
            "You extract a domain-neutral mini-app contract from a user prompt. "
            "Do not use templates or infer from local lexical lists. Return only valid JSON. "
            "Use the user's wording for labels. If a detail is not explicit, leave the list empty."
        )
        user_prompt = json.dumps(
            {
                "schema": "grounded.prompt_contract_analysis.v1",
                "generation_mode": mode_value,
                "prompt": str(prompt or ""),
                "required_json_shape": {
                    "prompt_summary": "short summary in the prompt language",
                    "resource_hint": "single shared business object name or null",
                    "field_hints": ["all explicit create/update field labels from the prompt"],
                    "role_field_hints": {
                        "client": ["fields owned by client role"],
                        "specialist": ["fields owned by specialist role"],
                        "manager": ["fields owned by manager role"],
                    },
                    "role_action_prompts": {
                        "client": ["client role actions from the prompt"],
                        "specialist": ["specialist role actions from the prompt"],
                        "manager": ["manager role actions from the prompt"],
                    },
                    "routeable_screen_plan": {
                        "multi_page_recommended": True,
                        "roles": {
                            "client": [{"intent": "overview|create_or_configure|list_or_queue|detail_or_update|summary_or_insight", "purpose": "why this screen exists", "source": ["prompt phrase"]}],
                            "specialist": [],
                            "manager": [],
                        },
                    },
                },
            },
            ensure_ascii=False,
        )
        if model.startswith("gpt-5"):
            payload = {
                "model": model,
                "input": self._responses_input(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_cache_key=None,
                    stable_prefix=None,
                ),
                "reasoning": {"effort": "low"},
            }
            endpoint = "responses"
            schema_name = "prompt_contract_analysis"
            self._log_prompt_bundle(
                role="summarize",
                schema_name=schema_name,
                endpoint=endpoint,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._log_request(endpoint=endpoint, model=model, payload=payload)
            data = self._post_json_with_retries(endpoint=endpoint, model=model, payload=payload)
            self._raise_for_incomplete_response(data, endpoint=endpoint)
            text = self._extract_response_text(data)
            usage = self._usage_from_response_payload(data)
        else:
            payload = {
                "model": model,
                "messages": self._chat_messages(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_cache_key=None,
                    stable_prefix=None,
                ),
                "response_format": {"type": "json_object"},
            }
            endpoint = "chat/completions"
            self._log_prompt_bundle(
                role="summarize",
                schema_name="prompt_contract_analysis",
                endpoint=endpoint,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._log_request(endpoint=endpoint, model=model, payload=payload)
            data = self._post_json_with_retries(endpoint=endpoint, model=model, payload=payload)
            text = self._extract_chat_text(data)
            usage = self._usage_from_response_payload(data)
        parsed = self._parse_json_object_from_text(text)
        if usage:
            parsed["_llm_usage"] = usage
            parsed["_llm_model"] = model
        self._log_parsed_text(endpoint=endpoint, model=model, text=json.dumps(parsed, ensure_ascii=False)[:4000])
        return parsed

    def classify_repair_issue(
        self,
        *,
        issue: dict[str, Any],
        run_context: dict[str, Any] | None = None,
        model_profile: str | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> dict[str, Any]:
        """Classify non-deterministic repair needs with a structured LLM packet."""
        if not self.enabled:
            raise RuntimeError("OpenAI API key is not configured.")
        model = models_for_role("cheap_task", model_profile=model_profile, generation_mode=generation_mode)
        system_prompt = (
            "You classify a failed mini-app generation run into one concrete repair packet. "
            "Use only the supplied logs, browser proof, diff summary, and check metadata. "
            "Do not infer from business-domain keywords. Return only valid JSON."
        )
        user_prompt = json.dumps(
            {
                "schema": "grounded.repair_classifier.v1",
                "issue": issue,
                "run_context": run_context or {},
                "required_json_shape": {
                    "signature": "stable dotted signature",
                    "issue_code": "stable_snake_case_code",
                    "severity": "low|medium|high|critical",
                    "likely_root_cause": "short concrete cause",
                    "target_files": ["miniapp/..."],
                    "verification_check": "check name or browser_verify",
                    "instruction": "specific repair instruction",
                    "required_next_tool": "read_files",
                    "suggested_tool_after_read": "write_file|apply_patch_to_draft|edit_file_exact",
                    "retryable": True,
                    "deterministic": False,
                },
            },
            ensure_ascii=False,
            default=str,
        )
        if model.startswith("gpt-5"):
            payload = {
                "model": model,
                "input": self._responses_input(system_prompt=system_prompt, user_prompt=user_prompt, prompt_cache_key=None, stable_prefix=None),
                "reasoning": {"effort": "low"},
            }
            endpoint = "responses"
            self._log_prompt_bundle(
                role="repair",
                schema_name="repair_classifier",
                endpoint=endpoint,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._log_request(endpoint=endpoint, model=model, payload=payload)
            data = self._post_json_with_retries(endpoint=endpoint, model=model, payload=payload)
            self._raise_for_incomplete_response(data, endpoint=endpoint)
            text = self._extract_response_text(data)
            usage = self._usage_from_response_payload(data)
        else:
            payload = {
                "model": model,
                "messages": self._chat_messages(system_prompt=system_prompt, user_prompt=user_prompt, prompt_cache_key=None, stable_prefix=None),
                "response_format": {"type": "json_object"},
            }
            endpoint = "chat/completions"
            self._log_prompt_bundle(
                role="repair",
                schema_name="repair_classifier",
                endpoint=endpoint,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._log_request(endpoint=endpoint, model=model, payload=payload)
            data = self._post_json_with_retries(endpoint=endpoint, model=model, payload=payload)
            text = self._extract_chat_text(data)
            usage = self._usage_from_response_payload(data)
        parsed = self._parse_json_object_from_text(text)
        parsed["_llm_usage"] = usage or {}
        parsed["_llm_model"] = model
        return parsed

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _dump_for_log(payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)

    @staticmethod
    def _parse_json_object_from_text(text: str) -> dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            raise RuntimeError("Model returned empty prompt analysis.")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(raw[start : end + 1])
        if not isinstance(parsed, dict):
            raise RuntimeError("Model prompt analysis must be a JSON object.")
        return parsed

    @classmethod
    def _truncate_log_text(cls, text: str, *, limit: int | None = None) -> str:
        max_len = limit or cls._LOG_STRING_LIMIT
        if len(text) <= max_len:
            return text
        head = max_len // 2
        tail = max_len - head
        return f"{text[:head]}\n...[truncated {len(text) - max_len} chars]...\n{text[-tail:]}"

    @classmethod
    def _compact_for_log(cls, value: Any, *, depth: int = 0) -> Any:
        if isinstance(value, str):
            return cls._truncate_log_text(value)
        if depth >= cls._LOG_MAX_DEPTH:
            return f"<truncated depth={depth} type={type(value).__name__}>"
        if isinstance(value, list):
            items = [cls._compact_for_log(item, depth=depth + 1) for item in value[: cls._LOG_LIST_LIMIT]]
            if len(value) > cls._LOG_LIST_LIMIT:
                items.append(f"<truncated {len(value) - cls._LOG_LIST_LIMIT} more items>")
            return items
        if isinstance(value, tuple):
            items = [cls._compact_for_log(item, depth=depth + 1) for item in value[: cls._LOG_LIST_LIMIT]]
            if len(value) > cls._LOG_LIST_LIMIT:
                items.append(f"<truncated {len(value) - cls._LOG_LIST_LIMIT} more items>")
            return tuple(items)
        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            items = list(value.items())
            for key, item in items[: cls._LOG_DICT_LIMIT]:
                compacted[str(key)] = cls._compact_for_log(item, depth=depth + 1)
            if len(items) > cls._LOG_DICT_LIMIT:
                compacted["__truncated_keys__"] = len(items) - cls._LOG_DICT_LIMIT
            return compacted
        return value

    @classmethod
    def _compact_log_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return cls._compact_for_log(payload, depth=0)

    def _log_request(self, *, endpoint: str, model: str, payload: dict[str, Any]) -> None:
        compact_payload = self._compact_log_payload(payload)
        logger.info("LLM request endpoint=%s model=%s", endpoint, model)
        logger.debug(
            "LLM request endpoint=%s model=%s payload=%s",
            endpoint,
            model,
            self._dump_for_log(compact_payload),
        )
        self._append_workspace_api_log(
            source="llm.request",
            message=f"OpenAI request sent to {endpoint}.",
            payload={"endpoint": endpoint, "model": model, "payload": compact_payload},
        )

    def _log_prompt_bundle(
        self,
        *,
        role: str,
        schema_name: str,
        endpoint: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        compact_payload = {
            "role": role,
            "schema_name": schema_name,
            "endpoint": endpoint,
            "model": model,
            "system_prompt": self._truncate_log_text(system_prompt),
            "user_prompt": self._truncate_log_text(user_prompt),
        }
        logger.info(
            "LLM prompt role=%s schema=%s endpoint=%s model=%s",
            role,
            schema_name,
            endpoint,
            model,
        )
        logger.debug(
            "LLM prompt role=%s schema=%s endpoint=%s model=%s\nSYSTEM PROMPT:\n%s\nUSER PROMPT:\n%s",
            role,
            schema_name,
            endpoint,
            model,
            compact_payload["system_prompt"],
            compact_payload["user_prompt"],
        )
        self._append_workspace_api_log(
            source="llm.prompt",
            message=f"OpenAI prompt prepared for {role}.",
            payload=compact_payload,
        )

    def _log_response(self, *, endpoint: str, model: str, response: httpx.Response) -> None:
        usage_summary = self._extract_usage_summary(response)
        response_body = self._truncate_log_text(response.text, limit=6000)
        logger.info("LLM response endpoint=%s model=%s status=%s", endpoint, model, response.status_code)
        logger.debug(
            "LLM response endpoint=%s model=%s status=%s body=%s",
            endpoint,
            model,
            response.status_code,
            response_body,
        )
        self._append_workspace_api_log(
            source="llm.response",
            message=f"OpenAI response received from {endpoint}.",
            payload={
                "endpoint": endpoint,
                "model": model,
                "status_code": response.status_code,
                "body": response_body,
                "usage": usage_summary,
            },
        )

    def _log_parsed_text(self, *, endpoint: str, model: str, text: str) -> None:
        compact_text = self._truncate_log_text(text, limit=4000)
        logger.info("LLM parsed-text endpoint=%s model=%s", endpoint, model)
        logger.debug(
            "LLM parsed-text endpoint=%s model=%s text=%s",
            endpoint,
            model,
            compact_text,
        )
        self._append_workspace_api_log(
            source="llm.parsed_text",
            message=f"OpenAI parsed text extracted from {endpoint}.",
            payload={"endpoint": endpoint, "model": model, "text": compact_text},
        )

    def _append_workspace_api_log(self, *, source: str, message: str, payload: dict[str, Any]) -> None:
        workspace_id = ACTIVE_WORKSPACE_LOG_CONTEXT.get()
        if not workspace_id or self.workspace_log_service is None:
            return
        self.workspace_log_service.append_api(
            workspace_id,
            source=source,
            message=message,
            payload=payload,
        )

    @staticmethod
    def _stable_prompt_block(prompt_cache_key: str | None, stable_prefix: str | None) -> str | None:
        parts: list[str] = []
        if stable_prefix and stable_prefix.strip():
            parts.append(stable_prefix.strip())
        if prompt_cache_key and prompt_cache_key.strip():
            parts.append(f"Prompt cache key: {prompt_cache_key.strip()}")
        if not parts:
            return None
        parts.append("Keep the reusable prefix stable across retries and repeated workspace runs.")
        return "\n".join(parts)

    def _chat_messages(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None,
        stable_prefix: str | None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        stable_block = self._stable_prompt_block(prompt_cache_key, stable_prefix)
        if stable_block:
            messages.append({"role": "user", "content": stable_block})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _responses_input(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None,
        stable_prefix: str | None,
    ) -> list[dict[str, Any]]:
        input_items: list[dict[str, Any]] = [{"role": "system", "content": [{"type": "input_text", "text": system_prompt}]}]
        stable_block = self._stable_prompt_block(prompt_cache_key, stable_prefix)
        if stable_block:
            input_items.append({"role": "user", "content": [{"type": "input_text", "text": stable_block}]})
        input_items.append({"role": "user", "content": [{"type": "input_text", "text": user_prompt}]})
        return input_items

    @staticmethod
    def _extract_cache_stats(payload: dict[str, Any], prompt_cache_key: str | None = None) -> dict[str, Any]:
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        cached_tokens = (
            prompt_details.get("cached_tokens")
            or usage.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
            or 0
        )
        cache_write_tokens = (
            prompt_details.get("cache_write_tokens")
            or usage.get("cache_write_tokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
        output_details = usage.get("output_tokens_details")
        if not isinstance(output_details, dict):
            output_details = {}
        estimated_cost = usage.get("cost") or payload.get("cost") or payload.get("total_cost") or 0.0
        return {
            "prompt_cache_key": prompt_cache_key,
            "cached_tokens": int(cached_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
            "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "estimated_cost_usd": float(estimated_cost or 0.0),
        }

    def _responses_tool_step(
        self,
        *,
        role: str,
        model: str,
        tools: list[dict[str, Any]],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        tuning_override: dict[str, Any] | None = None,
        previous_response_id: str | None = None,
        tool_result_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tuning = self._responses_tuning(role=role, schema_name="agent_tool_step")
        prior_tool_items = self._responses_tool_result_items(tool_result_messages or []) if previous_response_id else []
        payload = {
            "model": model,
            "input": [
                *prior_tool_items,
                *self._responses_input(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    prompt_cache_key=prompt_cache_key,
                    stable_prefix=stable_prefix,
                ),
            ],
            "tools": tools,
            "tool_choice": "auto",
        }
        if previous_response_id and prior_tool_items:
            payload["previous_response_id"] = previous_response_id
        payload.update(tuning)
        if tuning_override:
            payload.update(tuning_override)
        self._log_prompt_bundle(
            role=role,
            schema_name="agent_tool_step",
            endpoint="responses",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="responses", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="responses", model=model, payload=payload)
        self._raise_for_incomplete_response(data, endpoint="responses")
        parsed = self._extract_response_tool_step(data)
        self._log_parsed_text(endpoint="responses", model=model, text=json.dumps(parsed, ensure_ascii=False)[:4000])
        return {
            "payload": parsed,
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _chat_tool_step(
        self,
        *,
        role: str,
        model: str,
        tools: list[dict[str, Any]],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        tool_result_messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        messages = self._chat_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )
        if tool_result_messages:
            messages = [messages[0], *self._chat_tool_result_messages(tool_result_messages), *messages[1:]]
        payload = {
            "model": model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ],
            "tool_choice": "auto",
        }
        self._log_prompt_bundle(
            role=role,
            schema_name="agent_tool_step",
            endpoint="chat/completions",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="chat/completions", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="chat/completions", model=model, payload=payload)
        parsed = self._extract_chat_tool_step(data)
        self._log_parsed_text(endpoint="chat/completions", model=model, text=json.dumps(parsed, ensure_ascii=False)[:4000])
        return {
            "payload": parsed,
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _post_json_with_retries(self, *, endpoint: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        provider_label = "OpenAI"
        base_url = self.openai_base_url.rstrip("/")
        for attempt in range(1, self.retry_policy.max_provider_attempts + 1):
            try:
                started = time.perf_counter()
                with httpx.Client(
                    timeout=httpx.Timeout(
                        connect=self.connect_timeout_sec,
                        read=self.read_timeout_sec,
                        write=self.write_timeout_sec,
                        pool=self.pool_timeout_sec,
                    )
                ) as client:
                    response = client.post(f"{base_url}/{endpoint}", headers=self._headers(), json=payload)
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    self._log_response(endpoint=endpoint, model=model, response=response)
                    self._append_workspace_api_log(
                        source="llm.metrics",
                        message=f"{provider_label} request metrics recorded for {endpoint}.",
                        payload={
                            "endpoint": endpoint,
                            "model": model,
                            "provider": "openai",
                            "duration_ms": duration_ms,
                            "target_file_count": self._extract_target_file_count(payload),
                            "usage": self._extract_usage_summary(response),
                        },
                    )
                    self._raise_for_status(response, endpoint, provider_label)
                    return response.json()
            except Exception as exc:
                last_error = exc
                error_class = self.retry_policy.classify_error(exc)
                turn_state = ACTIVE_AGENT_TURN_STATE.get()
                if turn_state is not None:
                    turn_state.last_error_class = error_class
                self._append_workspace_api_log(
                    source="llm.error",
                    message=f"{provider_label} request failed for {endpoint}.",
                    payload={
                        "endpoint": endpoint,
                        "model": model,
                        "provider": "openai",
                        "attempt": attempt,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "error_class": error_class,
                    },
                )
                if not self.retry_policy.should_retry(exc, attempt):
                    raise
                delay_seconds = self.retry_policy.backoff_seconds(attempt)
                if turn_state is not None:
                    turn_state.llm_retry_ms += int(delay_seconds * 1000)
                logger.warning(
                    "Retrying %s request endpoint=%s model=%s after %s failure: %s",
                    provider_label,
                    endpoint,
                    model,
                    error_class,
                    exc,
                )
                time.sleep(delay_seconds)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _responses_tuning(*, role: str, schema_name: str) -> dict[str, Any]:
        del schema_name
        if role == "agent_turn":
            return {"reasoning": {"effort": "high"}}
        if role == "code_edit":
            return {"reasoning": {"effort": "medium"}}
        if role == "repair":
            return {"reasoning": {"effort": "medium"}}
        if role == "summarize":
            return {"reasoning": {"effort": "low"}}
        return {}

    @staticmethod
    def _extract_usage_summary(response: httpx.Response) -> dict[str, int] | None:
        try:
            payload = response.json()
        except Exception:
            return None
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        output_details = usage.get("output_tokens_details")
        if not isinstance(output_details, dict):
            output_details = {}
        return {
            "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    @staticmethod
    def _usage_from_response_payload(payload: dict[str, Any]) -> dict[str, int] | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        output_details = usage.get("output_tokens_details")
        if not isinstance(output_details, dict):
            output_details = {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        input_tokens = usage.get("input_tokens", prompt_tokens)
        output_tokens = usage.get("output_tokens", completion_tokens)
        try:
            return {
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or ((input_tokens or 0) + (output_tokens or 0))),
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raise_for_incomplete_response(payload: dict[str, Any], *, endpoint: str) -> None:
        if str(payload.get("status") or "").lower() != "incomplete":
            return
        details = payload.get("incomplete_details")
        reason = ""
        if isinstance(details, dict):
            reason = str(details.get("reason") or "")
        raise RuntimeError(f"OpenAI {endpoint} returned incomplete response: {reason or 'unknown'}")

    @staticmethod
    def _extract_target_file_count(payload: dict[str, Any]) -> int | None:
        try:
            input_items = payload.get("input")
            if not isinstance(input_items, list):
                return None
            for item in reversed(input_items):
                if not isinstance(item, dict):
                    continue
                contents = item.get("content")
                if not isinstance(contents, list):
                    continue
                for content in contents:
                    if not isinstance(content, dict):
                        continue
                    text = content.get("text")
                    if not isinstance(text, str):
                        continue
                    stripped = text.strip()
                    if not stripped.startswith("{"):
                        continue
                    parsed = json.loads(stripped)
                    target_files = parsed.get("target_files")
                    if isinstance(target_files, list):
                        return len(target_files)
        except Exception:
            return None
        return None

    @staticmethod
    def _raise_for_status(response: httpx.Response, endpoint: str, provider_label: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text.strip()
            if body:
                raise RuntimeError(
                    f"{provider_label} {endpoint} returned {response.status_code}: {body[:2000]}"
                ) from exc
            raise RuntimeError(
                f"{provider_label} {endpoint} returned {response.status_code} with an empty body."
            ) from exc

    @staticmethod
    def _extract_chat_text(payload: dict[str, Any]) -> str:
        if payload.get("error"):
            error = payload["error"]
            if isinstance(error, dict):
                message = error.get("message") or error.get("metadata") or error
                raise RuntimeError(f"OpenAI chat error: {message}")
            raise RuntimeError(f"OpenAI chat error: {error}")

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                if parts:
                    return "".join(parts)

        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]

        try:
            return OpenAIClient._extract_response_text(payload)
        except RuntimeError:
            snippet = json.dumps(payload)[:1000]
            raise RuntimeError(f"OpenAI chat response did not contain text output. Payload: {snippet}")

    @staticmethod
    def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return dict(raw_arguments)
        if not isinstance(raw_arguments, str) or not raw_arguments.strip():
            return {}
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {"raw_arguments": raw_arguments}
        return dict(parsed) if isinstance(parsed, dict) else {"raw_arguments": raw_arguments}

    @staticmethod
    def _tool_result_output(message: dict[str, Any]) -> str:
        output = message.get("output")
        if isinstance(output, str):
            return output[:12000]
        try:
            return json.dumps(output if output is not None else message, ensure_ascii=False, default=str)[:12000]
        except TypeError:
            return str(output if output is not None else message)[:12000]

    @classmethod
    def _responses_tool_result_items(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages[-12:]:
            if not isinstance(message, dict):
                continue
            call_id = str(message.get("tool_use_id") or message.get("call_id") or "").strip()
            if not call_id:
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": cls._tool_result_output(message),
                }
            )
        return items

    @classmethod
    def _chat_tool_result_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages[-12:]:
            if not isinstance(message, dict):
                continue
            call_id = str(message.get("tool_use_id") or message.get("call_id") or "").strip()
            if not call_id:
                continue
            tool_name = str(message.get("tool") or "read_artifact_ref").strip() or "read_artifact_ref"
            items.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": "{}"},
                        }
                    ],
                }
            )
            items.append({"role": "tool", "tool_call_id": call_id, "content": cls._tool_result_output(message)})
        return items

    @classmethod
    def _extract_chat_tool_step(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("error"):
            cls._extract_chat_text(payload)
        choices = payload.get("choices")
        message: dict[str, Any] = {}
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            candidate = choices[0].get("message")
            if isinstance(candidate, dict):
                message = candidate
        assistant_message = ""
        content = message.get("content")
        if isinstance(content, str):
            assistant_message = content
        tool_calls: list[dict[str, Any]] = []
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(function.get("name") or item.get("name") or "").strip()
            if not name:
                continue
            arguments = cls._parse_tool_arguments(function.get("arguments") or item.get("arguments"))
            tool_calls.append(
                {
                    **arguments,
                    "tool": name,
                    "tool_use_id": str(item.get("id") or item.get("call_id") or f"{name}_{len(tool_calls) + 1}"),
                }
            )
        return {
            "assistant_message": assistant_message,
            "tool_calls": tool_calls,
            "response_id": str(payload.get("id") or ""),
            "raw_status": "completed",
        }

    @classmethod
    def _extract_response_tool_step(cls, payload: dict[str, Any]) -> dict[str, Any]:
        assistant_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            assistant_parts.append(payload["output_text"].strip())
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            if item_type == "function_call":
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                arguments = cls._parse_tool_arguments(item.get("arguments"))
                tool_calls.append(
                    {
                        **arguments,
                        "tool": name,
                        "tool_use_id": str(item.get("call_id") or item.get("id") or f"{name}_{len(tool_calls) + 1}"),
                    }
                )
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    assistant_parts.append(content["text"])
        return {
            "assistant_message": "\n".join(part for part in assistant_parts if part).strip(),
            "tool_calls": tool_calls,
            "response_id": str(payload.get("id") or ""),
            "raw_status": str(payload.get("status") or ""),
        }

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    return content["text"]
        raise RuntimeError("OpenAI response did not contain text output.")
