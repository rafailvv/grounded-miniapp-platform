from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptFingerprint:
    cache_key: str
    stable_prefix_hash: str
    prompt_hash: str
    combined_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "cache_key": self.cache_key,
            "stable_prefix_hash": self.stable_prefix_hash,
            "prompt_hash": self.prompt_hash,
            "combined_hash": self.combined_hash,
        }


class PromptStateManager:
    @staticmethod
    def fingerprint(*, prompt: str, stable_prefix: str, cache_key: str) -> PromptFingerprint:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        stable_prefix_hash = hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()
        combined_hash = hashlib.sha256(f"{cache_key}:{prompt_hash}:{stable_prefix_hash}".encode("utf-8")).hexdigest()
        return PromptFingerprint(
            cache_key=cache_key,
            stable_prefix_hash=stable_prefix_hash,
            prompt_hash=prompt_hash,
            combined_hash=combined_hash,
        )
