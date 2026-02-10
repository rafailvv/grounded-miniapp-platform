from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


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
                    "previews": {},
                    "exports": {},
                    "reports": {},
                }
            )

    def _read(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, payload: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, default=str)

    def list(self, collection: str) -> list[dict[str, Any]]:
        with self.lock:
            state = self._read()
            return list(state[collection].values())

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            state = self._read()
            return state[collection].get(key)

    def upsert(self, collection: str, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            state = self._read()
            state[collection][key] = value
            self._write(state)

    def delete(self, collection: str, key: str) -> None:
        with self.lock:
            state = self._read()
            state[collection].pop(key, None)
            self._write(state)

