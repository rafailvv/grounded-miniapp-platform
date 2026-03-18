from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
import time


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(
                {
                    "workspaces": {},
                    "documents": {},
                    "chat_turns": {},
                    "jobs": {},
                    "runs": {},
                    "previews": {},
                    "exports": {},
                    "reports": {},
                }
            )

    def _read(self) -> dict[str, Any]:
        last_error: json.JSONDecodeError | None = None
        for _ in range(5):
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(0.01)
        assert last_error is not None
        raise last_error

    def _write(self, payload: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, default=str)
        temp_path.replace(self.path)

    def list(self, collection: str) -> list[dict[str, Any]]:
        with self.lock:
            state = self._read()
            return list(state.setdefault(collection, {}).values())

    def items(self, collection: str) -> list[tuple[str, dict[str, Any]]]:
        with self.lock:
            state = self._read()
            return [(key, value) for key, value in state.setdefault(collection, {}).items()]

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            state = self._read()
            return state.setdefault(collection, {}).get(key)

    def upsert(self, collection: str, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            state = self._read()
            state.setdefault(collection, {})[key] = value
            self._write(state)

    def delete(self, collection: str, key: str) -> None:
        with self.lock:
            state = self._read()
            state.setdefault(collection, {}).pop(key, None)
            self._write(state)
