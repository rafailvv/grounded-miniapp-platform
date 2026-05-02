from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import os
import re
import time
from copy import deepcopy
from typing import Any

import httpx

from app.ai.model_registry import (
    MODEL_REGISTRY,
    TASK_PROFILES,
    default_profile_for_generation_mode,
    models_for_role,
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

    def generate_structured(
        self,
        *,
        role: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        model_override: str | None = None,
        responses_tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI API key is not configured.")
        schema_name = self._sanitize_schema_name(schema_name)
        normalized_schema = self._normalize_schema(schema)
        model_config = MODEL_REGISTRY[role]
        routing_context = ACTIVE_LLM_ROUTING_CONTEXT.get() or {}
        requested_profile = str(routing_context.get("model_profile") or "").strip() or None
        generation_mode = str(routing_context.get("generation_mode") or "").strip() or None
        routed_model = models_for_role(
            role,
            model_profile=requested_profile,
            generation_mode=generation_mode,
        )
        model = str(model_override or routed_model or model_config["primary"])
        payload = self._request_structured(
            role=role,
            model=model,
            schema_name=schema_name,
            schema=normalized_schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
            responses_tuning_override=responses_tuning_override,
        )
        return {
            "model": model,
            "payload": payload["payload"],
            "response_mode": "strict_json_schema",
            "cache_stats": payload["cache_stats"],
        }

    def generate_code_edit(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        return self.generate_structured(
            role="code_edit",
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

    def generate_agent_turn(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        model_override: str | None = None,
        responses_tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.generate_structured(
            role="agent_turn",
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
            model_override=model_override,
            responses_tuning_override=responses_tuning_override,
        )

    def generate_workspace_edits(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        return self.generate_structured(
            role="code_edit",
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

    def generate_summary(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        return self.generate_structured(
            role="summarize",
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

    def generate_json_object(
        self,
        *,
        role: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI API key is not configured.")
        normalized_schema = self._normalize_schema(schema)
        routing_context = ACTIVE_LLM_ROUTING_CONTEXT.get() or {}
        requested_profile = str(routing_context.get("model_profile") or "").strip() or None
        generation_mode = str(routing_context.get("generation_mode") or "").strip() or None
        model = models_for_role(
            role,
            model_profile=requested_profile,
            generation_mode=generation_mode,
        )
        payload = self._request_json_mode(
            role=role,
            model=model,
            schema_name=schema_name,
            schema=normalized_schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )
        return {
            "model": model,
            "payload": payload["payload"],
            "response_mode": "json_object",
            "cache_stats": payload["cache_stats"],
        }

    def _request_structured(
        self,
        *,
        role: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        responses_tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if model.startswith("gpt-5"):
            return self._responses_structured(
                role=role,
                model=model,
                schema_name=schema_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
                tuning_override=responses_tuning_override,
            )
        return self._chat_structured(
            role=role,
            model=model,
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

    def _request_json_mode(
        self,
        *,
        role: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        responses_tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        schema_hint = self._schema_hint(schema_name, schema)
        augmented_system_prompt = (
            f"{system_prompt}\n\n"
            "Return JSON only. The JSON must match the provided schema as closely as possible. "
            "Do not wrap it in markdown."
        )
        augmented_user_prompt = f"{user_prompt}\n\n{schema_hint}"
        if model.startswith("gpt-5"):
            return self._responses_json_object(
                role=role,
                schema_name=schema_name,
                model=model,
                system_prompt=augmented_system_prompt,
                user_prompt=augmented_user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
                tuning_override=responses_tuning_override,
            )
        return self._chat_json_object(
            role=role,
            schema_name=schema_name,
            model=model,
            system_prompt=augmented_system_prompt,
            user_prompt=augmented_user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

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

    def _chat_structured(
        self,
        *,
        role: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": self._chat_messages(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
            ),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        self._log_prompt_bundle(
            role=role,
            schema_name=schema_name,
            endpoint="chat/completions",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="chat/completions", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="chat/completions", model=model, payload=payload)
        content = self._extract_chat_text(data)
        self._log_parsed_text(endpoint="chat/completions", model=model, text=content)
        return {
            "payload": self._parse_json_payload(content, "chat/completions"),
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _responses_structured(
        self,
        *,
        role: str,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tuning = self._responses_tuning(role=role, schema_name=schema_name)
        payload = {
            "model": model,
            "input": self._responses_input(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        payload.update(tuning)
        if tuning_override:
            payload.update(tuning_override)
        self._log_prompt_bundle(
            role=role,
            schema_name=schema_name,
            endpoint="responses",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="responses", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="responses", model=model, payload=payload)
        self._raise_for_incomplete_response(data, endpoint="responses")
        text = self._extract_response_text(data)
        self._log_parsed_text(endpoint="responses", model=model, text=text)
        return {
            "payload": self._parse_json_payload(text, "responses"),
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _chat_json_object(
        self,
        *,
        role: str,
        schema_name: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": self._chat_messages(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
            ),
            "response_format": {
                "type": "json_object",
            },
        }
        self._log_prompt_bundle(
            role=role,
            schema_name=schema_name,
            endpoint="chat/completions",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="chat/completions", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="chat/completions", model=model, payload=payload)
        content = self._extract_chat_text(data)
        self._log_parsed_text(endpoint="chat/completions", model=model, text=content)
        return {
            "payload": self._parse_json_payload(content, "chat/completions"),
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _responses_json_object(
        self,
        *,
        role: str,
        schema_name: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        prompt_cache_key: str | None = None,
        stable_prefix: str | None = None,
        tuning_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tuning = self._responses_tuning(role=role, schema_name=schema_name)
        payload = {
            "model": model,
            "input": self._responses_input(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
            ),
            "text": {
                "format": {
                    "type": "json_object",
                }
            },
        }
        payload.update(tuning)
        if tuning_override:
            payload.update(tuning_override)
        self._log_prompt_bundle(
            role=role,
            schema_name=schema_name,
            endpoint="responses",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self._log_request(endpoint="responses", model=model, payload=payload)
        data = self._post_json_with_retries(endpoint="responses", model=model, payload=payload)
        self._raise_for_incomplete_response(data, endpoint="responses")
        text = self._extract_response_text(data)
        self._log_parsed_text(endpoint="responses", model=model, text=text)
        return {
            "payload": self._parse_json_payload(text, "responses"),
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
    def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
        def make_nullable(node: dict[str, Any]) -> dict[str, Any]:
            if "anyOf" in node:
                options = node["anyOf"]
                if isinstance(options, list) and not any(
                    isinstance(option, dict) and option.get("type") == "null" for option in options
                ):
                    return {"anyOf": [*options, {"type": "null"}]}
                return node

            node_type = node.get("type")
            if isinstance(node_type, list):
                if "null" not in node_type:
                    node["type"] = [*node_type, "null"]
                return node
            if isinstance(node_type, str):
                if node_type != "null":
                    node["type"] = [node_type, "null"]
                return node
            return {"anyOf": [node, {"type": "null"}]}

        def visit(node: Any) -> Any:
            if isinstance(node, dict):
                updated = {}
                for key, value in node.items():
                    if key == "default":
                        continue
                    updated[key] = visit(value)

                if "$defs" in updated and isinstance(updated["$defs"], dict):
                    updated["$defs"] = {name: visit(definition) for name, definition in updated["$defs"].items()}

                if updated.get("type") == "object":
                    properties = updated.get("properties")
                    if isinstance(properties, dict):
                        original_required = set(updated.get("required", []))
                        normalized_properties: dict[str, Any] = {}
                        for prop_name, prop_schema in properties.items():
                            normalized_schema = visit(prop_schema)
                            if prop_name not in original_required:
                                normalized_schema = make_nullable(normalized_schema)
                            normalized_properties[prop_name] = normalized_schema
                        updated["properties"] = normalized_properties
                        updated["required"] = list(normalized_properties.keys())
                    else:
                        updated["properties"] = {}
                        updated["required"] = []
                    updated["additionalProperties"] = False

                if updated.get("type") == "array" and "items" in updated:
                    updated["items"] = visit(updated["items"])

                return updated

            if isinstance(node, list):
                return [visit(item) for item in node]

            return node

        return visit(deepcopy(schema))

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
    def _schema_hint(schema_name: str, schema: dict[str, Any]) -> str:
        return (
            f"Target JSON schema name: {schema_name}\n"
            "Return one JSON object matching this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

    @staticmethod
    def _is_invalid_schema_error(error: Exception) -> bool:
        text = str(error)
        markers = (
            "invalid_json_schema",
            "Invalid schema for response_format",
            "Please ensure it is a valid JSON Schema",
            "additionalProperties: true",
            "additionalProperties' to false",
            "not supported. Please set 'additionalProperties' to false",
            "compiled grammar is too large",
            "Simplify your tool schemas or reduce the number of strict tools",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_retryable_request_error(error: Exception) -> bool:
        text = str(error).lower()
        if "insufficient_quota" in text or "exceeded your current quota" in text:
            return False
        status_match = re.search(r"returned\s+(\d{3})", text)
        if status_match:
            status_code = int(status_match.group(1))
            return status_code == 429 or 500 <= status_code <= 504

        transient_markers = (
            "connecterror",
            "requesterror",
            "name or service not known",
            "nodename nor servname provided",
            "temporary failure in name resolution",
            "failed to resolve",
            "dns",
            "connection aborted",
            "connection refused",
            "connection error",
            "internal_server_error",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
        return any(marker in text for marker in transient_markers)

    @staticmethod
    def _sanitize_schema_name(schema_name: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", schema_name).strip("_")
        sanitized = sanitized or "schema"
        max_length = 64
        if len(sanitized) <= max_length:
            return sanitized
        digest = hashlib.sha1(sanitized.encode("utf-8")).hexdigest()[:10]
        prefix_budget = max_length - len(digest) - 1
        prefix = sanitized[:prefix_budget].rstrip("_-") or "schema"
        return f"{prefix}_{digest}"

    @staticmethod
    def _parse_json_payload(raw_text: str, endpoint: str) -> dict[str, Any]:
        text = raw_text.strip()
        if not text:
            raise RuntimeError(f"OpenAI {endpoint} returned empty text instead of JSON.")

        candidates = [text]
        decoder = json.JSONDecoder()
        try:
            parsed_prefix, end_index = decoder.raw_decode(text)
        except json.JSONDecodeError:
            parsed_prefix = None
            end_index = -1
        if isinstance(parsed_prefix, dict):
            trailing = text[end_index:].strip()
            if not trailing:
                return parsed_prefix
            candidates.append(text[:end_index].strip())

        fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        candidates.extend(item.strip() for item in fenced if item.strip())

        object_match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if object_match:
            candidates.append(object_match.group(0).strip())

        array_match = re.search(r"\[.*\]", text, flags=re.DOTALL)
        if array_match:
            candidates.append(array_match.group(0).strip())

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError(
                f"OpenAI {endpoint} returned JSON, but it was {type(parsed).__name__} instead of an object."
            )

        snippet = text[:1200]
        raise RuntimeError(f"OpenAI {endpoint} returned non-JSON text: {snippet}")

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
            raise RuntimeError(f"OpenAI chat response did not contain structured text output. Payload: {snippet}")

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    return content["text"]
        raise RuntimeError("OpenAI response did not contain structured text output.")
