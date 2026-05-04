from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Callable


@dataclass
class AgentFileStateEntry:
    run_id: str
    path: str
    content: str
    sha256: str
    mtime_ns: int | None = None
    size: int | None = None
    read_count: int = 1
    cache_hits: int = 0
    last_read_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def summary(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "read_count": self.read_count,
            "cache_hits": self.cache_hits,
            "last_read_at": self.last_read_at,
        }


class AgentFileStateCache:
    """Run-scoped read-file cache with mutation invalidation.

    The cache is intentionally process-local. Runtime persists only summaries so
    replays can see whether repeated reads were avoided without storing source
    contents in the main state document.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], AgentFileStateEntry] = {}

    @staticmethod
    def _normalize_path(path: object) -> str:
        normalized = str(path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _stat(root: Path, path: str) -> tuple[int | None, int | None]:
        try:
            stat = (root / path).stat()
        except OSError:
            return None, None
        return int(stat.st_mtime_ns), int(stat.st_size)

    def read(
        self,
        *,
        run_id: str,
        root: Path,
        path: str,
        read_text: Callable[[str], str | None],
    ) -> str | None:
        normalized = self._normalize_path(path)
        if not normalized:
            return None
        key = (run_id, normalized)
        mtime_ns, size = self._stat(root, normalized)
        cached = self._entries.get(key)
        if cached is not None and cached.mtime_ns == mtime_ns and cached.size == size:
            cached.cache_hits += 1
            cached.last_read_at = datetime.now(timezone.utc).isoformat()
            return cached.content

        content = read_text(normalized)
        if content is None:
            self._entries.pop(key, None)
            return None
        self._entries[key] = AgentFileStateEntry(
            run_id=run_id,
            path=normalized,
            content=content,
            sha256=self._hash_content(content),
            mtime_ns=mtime_ns,
            size=size,
            read_count=(cached.read_count + 1) if cached is not None else 1,
            cache_hits=cached.cache_hits if cached is not None else 0,
        )
        return content

    def invalidate(self, run_id: str, paths: list[str]) -> None:
        normalized_paths = {self._normalize_path(path) for path in paths if self._normalize_path(path)}
        for key in list(self._entries):
            cached_run_id, cached_path = key
            if cached_run_id == run_id and cached_path in normalized_paths:
                self._entries.pop(key, None)

    def freshness(self, *, run_id: str, root: Path, path: str) -> dict[str, object]:
        normalized = self._normalize_path(path)
        if not normalized:
            return {"path": "", "status": "unsafe_path", "fresh": False}
        mtime_ns, size = self._stat(root, normalized)
        key = (run_id, normalized)
        cached = self._entries.get(key)
        exists = mtime_ns is not None and size is not None
        if cached is None:
            return {
                "path": normalized,
                "status": "unread" if exists else "missing",
                "fresh": False,
                "exists": exists,
            }
        fresh = cached.mtime_ns == mtime_ns and cached.size == size
        return {
            **cached.summary(),
            "status": "fresh" if fresh else "stale",
            "fresh": fresh,
            "exists": exists,
            "current_mtime_ns": mtime_ns,
            "current_size": size,
        }

    def snapshot(self, run_id: str, *, root: Path | None = None) -> dict[str, object]:
        entries = [
            (
                {
                    **entry.summary(),
                    "freshness": self.freshness(run_id=run_id, root=root, path=entry.path) if root is not None else {"status": "unknown_without_root"},
                }
                if root is not None
                else entry.summary()
            )
            for (entry_run_id, _), entry in sorted(self._entries.items(), key=lambda item: item[0][1])
            if entry_run_id == run_id
        ]
        return {
            "run_id": run_id,
            "entry_count": len(entries),
            "total_cache_hits": sum(int(entry.get("cache_hits") or 0) for entry in entries),
            "entries": entries,
            "freshness_available": root is not None,
        }
