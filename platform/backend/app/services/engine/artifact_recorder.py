from __future__ import annotations

from typing import Any

from app.repositories.state_store import StateStore


class ArtifactRecorder:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def store_workspace_report(self, workspace_id: str, report_type: str, payload: dict[str, Any]) -> None:
        self.store.upsert("reports", f"{report_type}:{workspace_id}", payload)

    def append_engine_trace(self, workspace_id: str, entry: dict[str, Any]) -> None:
        key = f"engine_trace:{workspace_id}"
        payload = self.store.get("reports", key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(payload.get("entries") or [])
        entries.append(entry)
        payload["entries"] = entries
        self.store.upsert("reports", key, payload)
