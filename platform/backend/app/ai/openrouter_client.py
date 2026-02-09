from __future__ import annotations

import json
import os
from typing import Any

import httpx

from app.ai.model_registry import MODEL_REGISTRY
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
        model_config = MODEL_REGISTRY[role]
        primary_model = model_config["primary"]
        try:
            return self._request_structured(
                model=primary_model,
                schema_name=schema_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception:
            fallback_model = model_config["fallback"]
            if fallback_model == primary_model:
                raise
            return self._request_structured(
                model=fallback_model,
                schema_name=schema_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

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
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        return json.loads(content)

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
            response.raise_for_status()
            data = response.json()
        text = self._extract_response_text(data)
        return json.loads(text)

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
            return payload["output_text"]
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    return content["text"]
        raise RuntimeError("OpenRouter response did not contain structured text output.")
