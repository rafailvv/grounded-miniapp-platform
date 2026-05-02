from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager


@dataclass
class WorkerDraft:
    worker_id: str
    owner_scope: str
    source_dir: str
    status: str = "ready"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "owner_scope": self.owner_scope,
            "source_dir": self.source_dir,
            "status": self.status,
            "created_at": self.created_at,
        }


class AgentWorkerRuntime:
    """Isolated draft manager for coordinator-owned workers."""

    def __init__(self) -> None:
        self._drafts: dict[str, list[WorkerDraft]] = {}
        self._merge_reports: dict[str, list[dict[str, Any]]] = {}

    def prepare(
        self,
        *,
        run_id: str,
        generation_mode: GenerationMode,
        draft_source: Path,
        worker_specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if generation_mode == GenerationMode.FAST:
            self._drafts[run_id] = []
            return {"enabled": False, "mode": "single_loop", "workers": []}
        worker_root = draft_source.parent / f"{draft_source.name}_workers"
        worker_root.mkdir(parents=True, exist_ok=True)
        drafts: list[WorkerDraft] = []
        for spec in worker_specs:
            worker_id = str(spec.get("worker_id") or "").strip()
            if not worker_id:
                continue
            target = worker_root / worker_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(draft_source, target)
            drafts.append(
                WorkerDraft(
                    worker_id=worker_id,
                    owner_scope=str(spec.get("owner_scope") or worker_id),
                    source_dir=str(target),
                )
            )
        self._drafts[run_id] = drafts
        return {"enabled": True, "mode": str(generation_mode.value), "workers": [item.as_dict() for item in drafts]}

    def merge_report(self, run_id: str, draft_actions: list[DraftAction]) -> dict[str, Any]:
        ownership = AgentWorkerManager.validate_non_conflicting(draft_actions)
        report = {
            "run_id": run_id,
            "status": "accepted" if ownership.get("ok") else "conflict",
            "ownership": ownership,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._merge_reports.setdefault(run_id, []).append(report)
        return report

    def worker_failure_packet(self, *, run_id: str, signature: str, paths: list[str], message: str) -> dict[str, Any]:
        owners = sorted({AgentWorkerManager.owner_for_path(path) for path in paths if str(path).strip()})
        return {
            "run_id": run_id,
            "signature": str(signature or "")[:240],
            "owners": owners or ["shared"],
            "paths": list(dict.fromkeys(paths))[:16],
            "message": str(message or "")[:1200],
            "next_action": "continue the owning worker with this compact repair packet",
        }

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "drafts": [item.as_dict() for item in self._drafts.get(run_id, [])],
            "merge_reports": list(self._merge_reports.get(run_id, [])),
        }

