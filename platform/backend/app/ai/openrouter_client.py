from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
import re
import time
from copy import deepcopy
from typing import Any

import httpx

from app.ai.model_registry import MODEL_REGISTRY, TASK_PROFILES
from app.core.config import Settings
from app.services.workspace_log_service import WorkspaceLogService

logger = logging.getLogger(__name__)
ACTIVE_WORKSPACE_LOG_CONTEXT: ContextVar[str | None] = ContextVar("active_workspace_log_context", default=None)


class OpenRouterClient:
    def __init__(self, settings: Settings, workspace_log_service: WorkspaceLogService | None = None) -> None:
        self.settings = settings
        self.workspace_log_service = workspace_log_service
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def configuration(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "models": MODEL_REGISTRY,
            "task_profiles": TASK_PROFILES,
            "default_coding_profile": "openai_code_fast",
            "routing": {
                "provider": "openai",
            },
            "supports_prompt_cache_key": True,
        }

    @contextmanager
    def workspace_logging(self, workspace_id: str | None) -> Any:
        token = ACTIVE_WORKSPACE_LOG_CONTEXT.set(workspace_id)
        try:
            yield
        finally:
            ACTIVE_WORKSPACE_LOG_CONTEXT.reset(token)

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
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenAI is not configured.")
        schema_name = self._sanitize_schema_name(schema_name)
        normalized_schema = self._normalize_schema(schema)
        if self._should_bypass_strict_schema(normalized_schema):
            logger.info(
                "Bypassing strict json_schema upload for %s and using json_object mode due to complex schema shape.",
                schema_name,
            )
            payload = self._request_json_mode(
                role=role,
                model=MODEL_REGISTRY[role]["primary"],
                schema_name=schema_name,
                schema=normalized_schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_cache_key=prompt_cache_key,
                stable_prefix=stable_prefix,
            )
            return {
                "model": MODEL_REGISTRY[role]["primary"],
                "payload": payload["payload"],
                "response_mode": "json_object",
                "cache_stats": payload["cache_stats"],
            }
        model_config = MODEL_REGISTRY[role]
        primary_model = model_config["primary"]
        fallback_model = model_config["fallback"]
        models = [primary_model] if fallback_model == primary_model else [primary_model, fallback_model]
        last_error: Exception | None = None
        for model in models:
            try:
                payload = self._request_structured(
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
                    "response_mode": "strict_json_schema",
                    "cache_stats": payload["cache_stats"],
                }
            except Exception as exc:
                last_error = exc
                if self._is_invalid_schema_error(exc):
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
        assert last_error is not None
        raise last_error

    def generate_code_plan(
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
            role="code_plan",
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_cache_key=prompt_cache_key,
            stable_prefix=stable_prefix,
        )

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

    def generate_repair(
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
            role="repair",
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
            raise RuntimeError("OpenAI is not configured.")
        normalized_schema = self._normalize_schema(schema)
        model_config = MODEL_REGISTRY[role]
        primary_model = model_config["primary"]
        fallback_model = model_config["fallback"]
        models = [primary_model] if fallback_model == primary_model else [primary_model, fallback_model]
        last_error: Exception | None = None
        for model in models:
            try:
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
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        return headers

    @staticmethod
    def _dump_for_log(payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)

    def _log_request(self, *, endpoint: str, model: str, payload: dict[str, Any]) -> None:
        logger.info(
            "LLM request endpoint=%s model=%s payload=%s",
            endpoint,
            model,
            self._dump_for_log(payload),
        )
        self._append_workspace_api_log(
            source="llm.request",
            message=f"OpenAI request sent to {endpoint}.",
            payload={"endpoint": endpoint, "model": model, "payload": payload},
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
        logger.info(
            "LLM prompt role=%s schema=%s endpoint=%s model=%s\nSYSTEM PROMPT:\n%s\nUSER PROMPT:\n%s",
            role,
            schema_name,
            endpoint,
            model,
            system_prompt,
            user_prompt,
        )
        self._append_workspace_api_log(
            source="llm.prompt",
            message=f"OpenAI prompt prepared for {role}.",
            payload={
                "role": role,
                "schema_name": schema_name,
                "endpoint": endpoint,
                "model": model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            },
        )

    def _log_response(self, *, endpoint: str, model: str, response: httpx.Response) -> None:
        logger.info(
            "LLM response endpoint=%s model=%s status=%s body=%s",
            endpoint,
            model,
            response.status_code,
            response.text,
        )
        self._append_workspace_api_log(
            source="llm.response",
            message=f"OpenAI response received from {endpoint}.",
            payload={
                "endpoint": endpoint,
                "model": model,
                "status_code": response.status_code,
                "body": response.text,
            },
        )

    def _log_parsed_text(self, *, endpoint: str, model: str, text: str) -> None:
        logger.info(
            "LLM parsed-text endpoint=%s model=%s text=%s",
            endpoint,
            model,
            text,
        )
        self._append_workspace_api_log(
            source="llm.parsed_text",
            message=f"OpenAI parsed text extracted from {endpoint}.",
            payload={"endpoint": endpoint, "model": model, "text": text},
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
    def _cache_control(model: str) -> dict[str, str] | None:
        if model.startswith("anthropic/"):
            return {"type": "ephemeral"}
        return None

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
        return {
            "prompt_cache_key": prompt_cache_key,
            "cached_tokens": int(cached_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
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
        cache_control = self._cache_control(model)
        if cache_control is not None:
            payload["cache_control"] = cache_control
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
    ) -> dict[str, Any]:
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
        cache_control = self._cache_control(model)
        if cache_control is not None:
            payload["cache_control"] = cache_control
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
        cache_control = self._cache_control(model)
        if cache_control is not None:
            payload["cache_control"] = cache_control
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
    ) -> dict[str, Any]:
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
        cache_control = self._cache_control(model)
        if cache_control is not None:
            payload["cache_control"] = cache_control
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
        text = self._extract_response_text(data)
        self._log_parsed_text(endpoint="responses", model=model, text=text)
        return {
            "payload": self._parse_json_payload(text, "responses"),
            "cache_stats": self._extract_cache_stats(data, prompt_cache_key),
        }

    def _post_json_with_retries(self, *, endpoint: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=120) as client:
                    response = client.post(f"{self.base_url}/{endpoint}", headers=self._headers(), json=payload)
                    self._log_response(endpoint=endpoint, model=model, response=response)
                    self._raise_for_status(response, endpoint)
                    return response.json()
            except Exception as exc:
                last_error = exc
                if attempt == 2 or not self._is_retryable_request_error(exc):
                    raise
                logger.warning("Retrying OpenAI request endpoint=%s model=%s after transient failure: %s", endpoint, model, exc)
                time.sleep(0.8 * (attempt + 1))
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
    def _should_bypass_strict_schema(schema: dict[str, Any]) -> bool:
        counters = {
            "defs": 0,
            "refs": 0,
            "any_of": 0,
            "objects": 0,
        }

        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if "$defs" in node:
                    counters["defs"] += 1
                if "$ref" in node:
                    counters["refs"] += 1
                if "anyOf" in node:
                    counters["any_of"] += 1
                if node.get("type") == "object":
                    counters["objects"] += 1
                for value in node.values():
                    visit(value)
                return
            if isinstance(node, list):
                for item in node:
                    visit(item)

        visit(schema)
        # Structured outputs are reliable for small hand-authored schemas, but
        # large Pydantic-derived partial schemas with many refs/nullable branches
        # frequently trigger invalid_json_schema on the Responses API.
        return (
            counters["defs"] > 0
            or counters["refs"] > 8
            or counters["any_of"] > 12
            or counters["objects"] > 40
        )

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
        status_match = re.search(r"returned\s+(\d{3})", text)
        if status_match:
            status_code = int(status_match.group(1))
            return status_code == 429 or 500 <= status_code <= 504

        transient_markers = (
            "internal_server_error",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
        return any(marker in text for marker in transient_markers)

    @staticmethod
    def _sanitize_schema_name(schema_name: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", schema_name).strip("_")
        return sanitized or "schema"

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
    def _raise_for_status(response: httpx.Response, endpoint: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text.strip()
            if body:
                raise RuntimeError(
                    f"OpenAI {endpoint} returned {response.status_code}: {body[:2000]}"
                ) from exc
            raise RuntimeError(
                f"OpenAI {endpoint} returned {response.status_code} with an empty body."
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
            return OpenRouterClient._extract_response_text(payload)
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
