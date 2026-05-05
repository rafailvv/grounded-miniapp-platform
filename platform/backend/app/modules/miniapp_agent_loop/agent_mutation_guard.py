from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator


class AgentMutationGuard:
    """Run-scoped critical section for draft mutations.

    Tool reads can be concurrent, but all writes for the same run must cross one
    serialized section so stale checks, patch envelope construction, and draft
    writes observe one coherent filesystem state.
    """

    def __init__(self) -> None:
        self._global_lock = RLock()
        self._locks: dict[str, RLock] = {}

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        key = str(run_id or "default")
        with self._global_lock:
            lock = self._locks.setdefault(key, RLock())
        with lock:
            yield
