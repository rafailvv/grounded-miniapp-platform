from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import random
import re
from typing import Literal


def _env_int(name: str, default: int, *, aliases: tuple[str, ...] = ()) -> int:
    for key in (name, *aliases):
        raw = str(os.getenv(key, "")).strip()
        if not raw:
            continue
        try:
            return max(0, int(raw))
        except ValueError:
            continue
    return default


def _env_float(name: str, default: float, *, aliases: tuple[str, ...] = ()) -> float:
    for key in (name, *aliases):
        raw = str(os.getenv(key, "")).strip()
        if not raw:
            continue
        try:
            return max(0.0, float(raw))
        except ValueError:
            continue
    return default


RetryErrorClass = Literal[
    "transient_provider",
    "network_transport",
    "quota_or_budget",
    "auth_or_config",
    "empty_model_output",
    "tool_budget_exhausted",
    "scope_mismatch",
    "invalid_request",
    "unknown",
]


@dataclass(frozen=True)
class TimeoutProfile:
    openai_connect_sec: float
    openai_read_sec: float
    openai_write_sec: float
    openai_pool_sec: float
    preview_start_sec: int

    @classmethod
    def from_env(cls) -> "TimeoutProfile":
        return cls(
            openai_connect_sec=_env_float("OPENAI_CONNECT_TIMEOUT_SEC", 30.0),
            openai_read_sec=_env_float("OPENAI_READ_TIMEOUT_SEC", 300.0),
            openai_write_sec=_env_float("OPENAI_WRITE_TIMEOUT_SEC", 300.0),
            openai_pool_sec=_env_float("OPENAI_POOL_TIMEOUT_SEC", 120.0),
            preview_start_sec=_env_int("PREVIEW_START_TIMEOUT_SEC", 120),
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_provider_attempts: int
    base_delay_ms: int
    max_delay_ms: int

    _NETWORK_MARKERS: tuple[str, ...] = field(
        default=(
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
            "timed out",
            "timeout",
            "temporarily unavailable",
        ),
        init=False,
        repr=False,
    )
    _QUOTA_MARKERS: tuple[str, ...] = field(
        default=(
            "insufficient_quota",
            "exceeded your current quota",
            "can only afford",
            "fewer max_tokens",
            "rate limit budget",
        ),
        init=False,
        repr=False,
    )
    _AUTH_MARKERS: tuple[str, ...] = field(
        default=(
            "incorrect api key",
            "invalid api key",
            "authentication",
            "unauthorized",
            "forbidden",
            "permission denied",
            "no llm provider is configured",
        ),
        init=False,
        repr=False,
    )

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        return cls(
            max_provider_attempts=max(
                1,
                _env_int(
                    "LLM_PROVIDER_RETRY_MAX_ATTEMPTS",
                    2,
                    aliases=("LLM_RETRY_MAX_ATTEMPTS",),
                ),
            ),
            base_delay_ms=max(100, _env_int("LLM_RETRY_BASE_DELAY_MS", 250)),
            max_delay_ms=max(500, _env_int("LLM_RETRY_MAX_DELAY_MS", 2000)),
        )

    def classify_error(self, error: Exception | str) -> RetryErrorClass:
        text = str(error).lower().strip()
        if not text:
            return "unknown"
        if any(marker in text for marker in self._QUOTA_MARKERS):
            return "quota_or_budget"
        if any(marker in text for marker in self._AUTH_MARKERS):
            return "auth_or_config"
        if "tool-request budget" in text or "tool budget" in text:
            return "tool_budget_exhausted"
        if "scope mismatch" in text or "outside its scope" in text or "outside the planned scope" in text:
            return "scope_mismatch"
        if "returned empty text" in text or "returned non-json text" in text or "did not contain text output" in text:
            return "empty_model_output"
        status_match = re.search(r"returned\s+(\d{3})", text)
        if status_match:
            status_code = int(status_match.group(1))
            if status_code in {429, 502, 503, 504, 529} or 500 <= status_code <= 599:
                return "transient_provider"
            if status_code in {401, 403}:
                return "auth_or_config"
            if status_code == 402:
                return "quota_or_budget"
            return "invalid_request"
        if any(marker in text for marker in self._NETWORK_MARKERS):
            return "network_transport"
        if "internal_server_error" in text or "server error" in text:
            return "transient_provider"
        return "unknown"

    def should_retry(self, error: Exception | str, attempt: int) -> bool:
        if attempt >= self.max_provider_attempts:
            return False
        classification = self.classify_error(error)
        if classification in {"quota_or_budget", "auth_or_config", "invalid_request"}:
            return False
        if classification == "scope_mismatch":
            return attempt <= 1
        if classification in {"empty_model_output", "tool_budget_exhausted"}:
            return attempt <= min(self.max_provider_attempts, 3)
        return classification in {"transient_provider", "network_transport", "unknown"}

    def backoff_seconds(self, attempt: int) -> float:
        capped_delay_ms = min(self.max_delay_ms, self.base_delay_ms * (2 ** max(0, attempt - 1)))
        jitter_ms = random.randint(0, max(50, capped_delay_ms // 4))
        return float(capped_delay_ms + jitter_ms) / 1000.0


@dataclass
class AgentTurnState:
    prompt_build_ms: int = 0
    tool_orchestration_ms: int = 0
    llm_retry_ms: int = 0
    checks_ms: int = 0
    last_error_class: str | None = None

    def latency_breakdown(self) -> dict[str, int]:
        return {
            "prompt_build_ms": int(self.prompt_build_ms),
            "tool_orchestration_ms": int(self.tool_orchestration_ms),
            "llm_retry_ms": int(self.llm_retry_ms),
            "checks_ms": int(self.checks_ms),
        }


ACTIVE_AGENT_TURN_STATE: ContextVar[AgentTurnState | None] = ContextVar(
    "active_agent_turn_state",
    default=None,
)
