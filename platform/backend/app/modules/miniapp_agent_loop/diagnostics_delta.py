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
            payload["sources"] = AgentDiagnosticsDelta._sources_for_payload(payload)
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
            "source_counts": AgentDiagnosticsDelta._source_counts(current),
            "previous_source_counts": AgentDiagnosticsDelta._source_counts(previous),
        }

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        text = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _sources_for_payload(payload: dict[str, Any]) -> list[str]:
        name = str(payload.get("name") or "").lower()
        command = str(payload.get("command") or "").lower()
        details = str(payload.get("details") or "").lower()
        logs = "\n".join(str(item) for item in payload.get("logs") or []).lower()
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        keys = {str(key).lower() for key in diagnostics.keys()} if isinstance(diagnostics, dict) else set()
        text = "\n".join([name, command, details, logs, " ".join(sorted(keys))])
        sources: set[str] = set()
        if "pyright" in text:
            sources.add("pyright")
        if "ruff" in text:
            sources.add("ruff")
        if "tsc" in text or "typescript" in text or "frontend_build" in name or "npm run build" in command:
            sources.add("tsc")
        if "console_error" in text or "console errors" in text or "console_errors" in keys:
            sources.add("browser_console")
        if "network_error" in text or "network errors" in text or "network_errors" in keys:
            sources.add("browser_network")
        if "browser" in name or "playwright" in text:
            sources.add("browser_flow")
        if "api_workflow" in name or "api" in name and "smoke" in name:
            sources.add("api_workflow")
        if "backend" in name or "py_compile" in command or "compileall" in command:
            sources.add("python_compile")
        return sorted(sources or {"check"})

    @staticmethod
    def _source_counts(snapshot: dict[str, dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for payload in snapshot.values():
            for source in payload.get("sources") or ["check"]:
                key = str(source or "check")
                counts[key] = counts.get(key, 0) + 1
        return counts
