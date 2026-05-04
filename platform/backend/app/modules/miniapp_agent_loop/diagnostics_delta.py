from __future__ import annotations

import hashlib
import json
from typing import Any


class AgentDiagnosticsDelta:
    @staticmethod
    def snapshot(results: list[Any]) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for result in results:
            if getattr(result, "status", None) not in {"failed", "blocked"}:
                continue
            payload = {
                "name": str(getattr(result, "name", "") or ""),
                "status": str(getattr(result, "status", "") or ""),
                "details": str(getattr(result, "details", "") or ""),
                "command": str(getattr(result, "command", "") or ""),
                "logs": [str(item) for item in list(getattr(result, "logs", []) or [])[-8:]],
                "diagnostics": dict(getattr(result, "diagnostics", {}) or {}),
            }
            key = payload["name"] or f"check_{len(snapshot) + 1}"
            payload["fingerprint"] = AgentDiagnosticsDelta._fingerprint(payload)
            snapshot[key] = payload
        return snapshot

    @staticmethod
    def delta(
        previous: dict[str, dict[str, Any]] | None,
        current: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        previous = previous or {}
        current = current or {}
        added: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        for name, payload in current.items():
            old = previous.get(name)
            if old is None:
                added.append(payload)
            elif old.get("fingerprint") != payload.get("fingerprint"):
                changed.append({"previous": old, "current": payload})
        for name, payload in previous.items():
            if name not in current:
                resolved.append(payload)
        return {
            "status": "changed" if added or changed or resolved else "unchanged",
            "added": added,
            "changed": changed,
            "resolved": resolved,
            "current_failed_count": len(current),
            "previous_failed_count": len(previous),
        }

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

