from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.repositories.state_store import StateStore


class ProjectMemoryService:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def select(
        self,
        *,
        workspace_id: str,
        prompt: str,
        run_mode: str,
        generation_mode: str,
        limit: int = 4,
    ) -> dict[str, Any]:
        payload = self.store.get("reports", f"project_memory:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        prompt_terms = self._keywords(prompt)
        scored: list[tuple[float, dict[str, Any]]] = []
        for offset, item in enumerate(reversed(list(payload.get("items") or []))):
            haystack = " ".join(
                [
                    str(item.get("summary") or ""),
                    str(item.get("prompt_excerpt") or ""),
                    " ".join(item.get("tags") or []),
                    " ".join(item.get("files") or []),
                    str(item.get("failure_class") or ""),
                ]
            )
            overlap = len(prompt_terms & self._keywords(haystack))
            recency_bonus = max(0.0, 2.5 - offset * 0.25)
            gotcha_bonus = 1.5 if run_mode == "fix" and item.get("kind") in {"failure", "gotcha"} else 0.0
            sticky_bonus = 1.0 if item.get("sticky") else 0.0
            score = overlap * 2.0 + recency_bonus + gotcha_bonus + sticky_bonus
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        selected = [item for _, item in scored[:limit]]
        summary_lines: list[str] = []
        for item in selected:
            files = ", ".join(item.get("files") or [])
            suffix = f" Files: {files}." if files else ""
            summary_lines.append(f"- {item.get('summary', '').strip()}{suffix}")
        result = {
            "workspace_id": workspace_id,
            "run_mode": run_mode,
            "generation_mode": generation_mode,
            "items": selected,
            "summary": "\n".join(summary_lines),
            "selected_count": len(selected),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"project_memory_context:{workspace_id}", result)
        return result

    def record(
        self,
        *,
        workspace_id: str,
        prompt: str,
        summary: str,
        files: list[str],
        failure_class: str | None,
        status: str,
        run_mode: str,
    ) -> dict[str, Any]:
        compact_summary = " ".join(summary.split()).strip()
        if not compact_summary:
            return self.store.get("reports", f"project_memory:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        payload = self.store.get("reports", f"project_memory:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        prompt_excerpt = " ".join(prompt.split())[:240]
        tags = sorted(set(self._keywords(prompt_excerpt) | self._keywords(compact_summary) | {run_mode, status}))
        if failure_class:
            tags.append(failure_class)
        fingerprint = hashlib.sha256(
            f"{compact_summary}|{failure_class or ''}|{'|'.join(sorted(files))}|{run_mode}|{status}".encode("utf-8")
        ).hexdigest()
        items = [item for item in list(payload.get("items") or []) if item.get("fingerprint") != fingerprint]
        items.append(
            {
                "memory_id": f"mem_{fingerprint[:12]}",
                "fingerprint": fingerprint,
                "kind": "failure" if status == "failed" else "success",
                "summary": compact_summary[:600],
                "prompt_excerpt": prompt_excerpt,
                "files": list(dict.fromkeys(files))[:8],
                "failure_class": failure_class,
                "tags": tags[:16],
                "sticky": bool(failure_class or status == "failed"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        result = {
            "workspace_id": workspace_id,
            "items": items[-20:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"project_memory:{workspace_id}", result)
        return result

    @staticmethod
    def _keywords(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-zA-Z0-9_./-]{3,}", text.lower())
            if token not in {"with", "that", "this", "from", "into", "have"}
        }
