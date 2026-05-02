from __future__ import annotations

from typing import Any

from app.repositories.state_store import StateStore


class WorkspaceAgentArtifactReporter:
    """Small writer facade for run-scoped agent artifacts."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def store_report(self, key: str | None, payload: dict[str, Any]) -> None:
        if not key:
            return
        self.store.upsert("reports", key, payload)

    def store_items(self, key: str | None, *, workspace_id: str, run_id: str | None, items: list[dict[str, Any]]) -> None:
        self.store_report(key, {"workspace_id": workspace_id, "run_id": run_id, "items": list(items)})
