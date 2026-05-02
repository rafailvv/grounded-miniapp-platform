from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import hashlib
from typing import Any, Callable

from app.services.workspace.service import WorkspaceService


@dataclass
class AgentTurnDiffRecord:
    run_id: str
    turn: int
    status: str
    paths: list[str]
    owners: dict[str, str]
    changed_line_counts: dict[str, dict[str, int]]
    conflict_signature: str | None = None
    diff: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def summary(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "status": self.status,
            "paths": self.paths,
            "owners": self.owners,
            "changed_line_counts": self.changed_line_counts,
            "conflict_signature": self.conflict_signature,
            "diff_excerpt": self.diff[:5000],
            "created_at": self.created_at,
        }


class AgentTurnDiffTracker:
    """Tracks patch baselines and per-turn diffs for targeted repair packets."""

    def __init__(self) -> None:
        self._baselines: dict[tuple[str, int], dict[str, str | None]] = {}
        self._records: dict[str, list[AgentTurnDiffRecord]] = {}

    @staticmethod
    def _normalize_paths(paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for path in paths:
            value = str(path or "").strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if value and value not in normalized:
                normalized.append(value)
        return normalized

    @staticmethod
    def _line_counts(diff_text: str) -> dict[str, int]:
        added = 0
        removed = 0
        for line in diff_text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return {"added": added, "removed": removed}

    @staticmethod
    def _conflict_signature(status: str, reason: str | None, paths: list[str]) -> str | None:
        if status == "applied":
            return None
        raw = "|".join([status, str(reason or ""), *paths])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def capture_baseline(
        self,
        *,
        workspace_service: WorkspaceService,
        workspace_id: str,
        run_id: str,
        turn: int,
        paths: list[str],
    ) -> None:
        baseline: dict[str, str | None] = {}
        for path in self._normalize_paths(paths):
            baseline[path] = workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
        self._baselines[(run_id, turn)] = baseline

    def record_result(
        self,
        *,
        workspace_service: WorkspaceService,
        workspace_id: str,
        run_id: str,
        turn: int,
        paths: list[str],
        apply_result: Any,
        owner_for_path: Callable[[str], str],
    ) -> AgentTurnDiffRecord:
        normalized_paths = self._normalize_paths(paths)
        baseline = self._baselines.pop((run_id, turn), {})
        chunks: list[str] = []
        changed_line_counts: dict[str, dict[str, int]] = {}
        for path in normalized_paths:
            before = baseline.get(path)
            after = workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            before_lines = [] if before is None else str(before).splitlines()
            after_lines = [] if after is None else str(after).splitlines()
            diff_lines = list(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                )
            )
            diff_text = "\n".join(diff_lines)
            if diff_text:
                chunks.append(diff_text)
                changed_line_counts[path] = self._line_counts(diff_text)
            else:
                changed_line_counts[path] = {"added": 0, "removed": 0}
        status = str(getattr(apply_result, "status", None) or (apply_result.get("status") if isinstance(apply_result, dict) else "") or "")
        reason = str(getattr(apply_result, "conflict_reason", None) or (apply_result.get("conflict_reason") if isinstance(apply_result, dict) else "") or "")
        record = AgentTurnDiffRecord(
            run_id=run_id,
            turn=turn,
            status=status or "unknown",
            paths=normalized_paths,
            owners={path: owner_for_path(path) for path in normalized_paths},
            changed_line_counts=changed_line_counts,
            conflict_signature=self._conflict_signature(status or "unknown", reason, normalized_paths),
            diff="\n".join(chunks),
        )
        self._records.setdefault(run_id, []).append(record)
        return record

    def latest_summary(self, run_id: str) -> dict[str, object]:
        records = self._records.get(run_id) or []
        return records[-1].summary() if records else {}

    def snapshot(self, run_id: str) -> dict[str, object]:
        records = [record.summary() for record in self._records.get(run_id, [])]
        return {
            "run_id": run_id,
            "turn_count": len(records),
            "records": records,
        }
