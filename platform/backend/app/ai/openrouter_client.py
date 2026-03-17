from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any

import httpx

from app.ai.model_registry import MODEL_REGISTRY, TASK_PROFILES
from app.core.config import Settings


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

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
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }

    def generate_structured(
        self,
        *,
        role: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("OpenRouter is not configured.")
        normalized_schema = self._normalize_schema(schema)
        model_config = MODEL_REGISTRY[role]
        primary_model = model_config["primary"]
        fallback_model = model_config["fallback"]
        models = [primary_model] if fallback_model == primary_model else [primary_model, fallback_model]
        last_error: Exception | None = None
        for model in models:
            try:
                payload = self._request_structured(
                    model=model,
                    schema_name=schema_name,
                    schema=normalized_schema,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                return {"model": model, "payload": payload, "response_mode": "strict_json_schema"}
            except Exception as exc:
                last_error = exc
                if self._is_invalid_schema_error(exc):
                    payload = self._request_json_mode(
                        model=model,
                        schema_name=schema_name,
                        schema=normalized_schema,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                    )
                    return {"model": model, "payload": payload, "response_mode": "json_object"}
        assert last_error is not None
        raise last_error

    def _request_structured(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        if model.startswith("openai/gpt-5."):
            return self._responses_structured(
                model=model,
                schema_name=schema_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        return self._chat_structured(
            model=model,
            schema_name=schema_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    def _request_json_mode(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        schema_hint = self._schema_hint(schema_name, schema)
        augmented_system_prompt = (
            f"{system_prompt}\n\n"
            "Return JSON only. The JSON must match the provided schema as closely as possible. "
            "Do not wrap it in markdown."
        )
        augmented_user_prompt = f"{user_prompt}\n\n{schema_hint}"
        if model.startswith("openai/gpt-5."):
            return self._responses_json_object(
                model=model,
                system_prompt=augmented_system_prompt,
                user_prompt=augmented_user_prompt,
            )
        return self._chat_json_object(
            model=model,
            system_prompt=augmented_system_prompt,
            user_prompt=augmented_user_prompt,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.openrouter_site_url,
            "X-Title": self.settings.openrouter_app_name,
        }
        return headers

    def _chat_structured(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "provider": {
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        with httpx.Client(timeout=120) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload)
            self._raise_for_status(response, "chat/completions")
            data = response.json()
        content = self._extract_chat_text(data)
        return self._parse_json_payload(content, "chat/completions")

    def _responses_structured(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            "provider": {
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        with httpx.Client(timeout=120) as client:
            response = client.post(f"{self.base_url}/responses", headers=self._headers(), json=payload)
            self._raise_for_status(response, "responses")
            data = response.json()
        text = self._extract_response_text(data)
        return self._parse_json_payload(text, "responses")

    def _chat_json_object(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_object",
            },
            "provider": {
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        with httpx.Client(timeout=120) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=payload)
            self._raise_for_status(response, "chat/completions")
            data = response.json()
        content = self._extract_chat_text(data)
        return self._parse_json_payload(content, "chat/completions")

    def _responses_json_object(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
            "text": {
                "format": {
                    "type": "json_object",
                }
            },
            "provider": {
                "allow_fallbacks": True,
                "require_parameters": True,
                "data_collection": "deny",
            },
        }
        with httpx.Client(timeout=120) as client:
            response = client.post(f"{self.base_url}/responses", headers=self._headers(), json=payload)
            self._raise_for_status(response, "responses")
            data = response.json()
        text = self._extract_response_text(data)
        return self._parse_json_payload(text, "responses")

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
    def _parse_json_payload(raw_text: str, endpoint: str) -> dict[str, Any]:
        text = raw_text.strip()
        if not text:
            raise RuntimeError(f"OpenRouter {endpoint} returned empty text instead of JSON.")

        candidates = [text]

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
                f"OpenRouter {endpoint} returned JSON, but it was {type(parsed).__name__} instead of an object."
            )

        snippet = text[:1200]
        raise RuntimeError(f"OpenRouter {endpoint} returned non-JSON text: {snippet}")

    @staticmethod
    def _raise_for_status(response: httpx.Response, endpoint: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text.strip()
            if body:
                raise RuntimeError(
                    f"OpenRouter {endpoint} returned {response.status_code}: {body[:2000]}"
                ) from exc
            raise RuntimeError(
                f"OpenRouter {endpoint} returned {response.status_code} with an empty body."
            ) from exc

    @staticmethod
    def _extract_chat_text(payload: dict[str, Any]) -> str:
        if payload.get("error"):
            error = payload["error"]
            if isinstance(error, dict):
                message = error.get("message") or error.get("metadata") or error
                raise RuntimeError(f"OpenRouter chat error: {message}")
            raise RuntimeError(f"OpenRouter chat error: {error}")

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
            raise RuntimeError(f"OpenRouter chat response did not contain structured text output. Payload: {snippet}")

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    return content["text"]
        raise RuntimeError("OpenRouter response did not contain structured text output.")
