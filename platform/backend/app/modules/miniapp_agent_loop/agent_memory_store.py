from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Literal


MemoryKind = Literal["feedback", "project", "reference", "failure_signature"]


@dataclass
class AgentMemoryItem:
    kind: MemoryKind
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stale_check: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "payload": self.payload,
            "created_at": self.created_at,
            "stale_check": self.stale_check,
        }


class AgentMemoryStore:
    """Run-scoped memory for feedback and failure signatures."""

    _PATH_RE = re.compile(r"\bminiapp/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b")
    _ROUTE_RE = re.compile(r"(?<![A-Za-z0-9_])/[A-Za-z0-9_./{}:-]+")
    _IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")

    def __init__(self) -> None:
        self._items: dict[str, list[AgentMemoryItem]] = {}

    def add(self, run_id: str, kind: MemoryKind, text: str, payload: dict[str, Any] | None = None) -> AgentMemoryItem:
        item = AgentMemoryItem(kind=kind, text=str(text or "")[:1600], payload=dict(payload or {}))
        self._items.setdefault(run_id, []).append(item)
        return item

    def record_failure(self, run_id: str, signature: str, summary: str, payload: dict[str, Any] | None = None) -> AgentMemoryItem:
        return self.add(
            run_id,
            "failure_signature",
            summary,
            {"signature": str(signature or "")[:240], **dict(payload or {})},
        )

    def verify_stale_claims(self, run_id: str, root: Path) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for item in self._items.get(run_id, []):
            text = item.text
            paths = sorted(set(self._PATH_RE.findall(text)))
            routes = sorted(set(match.group(0) for match in self._ROUTE_RE.finditer(text)))[:16]
            identifiers = sorted(set(self._IDENT_RE.findall(text)))[:20]
            path_checks = [{"path": path, "exists": (root / path).exists()} for path in paths[:16]]
            route_checks = [{"route": route, "present_in_source": self._text_exists(root, route)} for route in routes]
            identifier_checks = [
                {"identifier": ident, "present_in_source": self._text_exists(root, ident)}
                for ident in identifiers
                if ident not in {"miniapp", "client", "specialist", "manager"}
            ][:12]
            stale = any(not item["exists"] for item in path_checks) or (
                bool(route_checks) and not any(item["present_in_source"] for item in route_checks)
            )
            item.stale_check = {
                "status": "stale" if stale else "fresh_or_unreferenced",
                "paths": path_checks,
                "routes": route_checks,
                "identifiers": identifier_checks,
            }
            checks.append({"kind": item.kind, "text": item.text[:240], **item.stale_check})
        return checks

    @staticmethod
    def _text_exists(root: Path, needle: str) -> bool:
        if not needle:
            return False
        for path in (root / "miniapp").rglob("*") if (root / "miniapp").exists() else []:
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs", ".html", ".css", ".json"}:
                continue
            try:
                if needle in path.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
        return False

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "items": [item.as_dict() for item in self._items.get(run_id, [])],
        }

