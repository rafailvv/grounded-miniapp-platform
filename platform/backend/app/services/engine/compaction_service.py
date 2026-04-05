from __future__ import annotations

from app.services.engine.mode_profiles import ModeProfiles


class CompactionService:
    def compact_text(self, text: str, *, generation_mode: str, max_chars: int | None = None) -> str:
        profile = ModeProfiles.resolve(generation_mode)
        char_limit = max_chars or (1200 if profile.mode == "fast" else 2400 if profile.mode == "balanced" else 4000)
        stripped = " ".join(text.split())
        if len(stripped) <= char_limit:
            return stripped
        return f"{stripped[:char_limit].rstrip()}..."

    def summarize_paths(self, paths: list[str], *, generation_mode: str) -> list[str]:
        profile = ModeProfiles.resolve(generation_mode)
        limit = 4 if profile.mode == "fast" else 8 if profile.mode == "balanced" else 12
        return list(paths[:limit])
