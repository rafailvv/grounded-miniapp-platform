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
    branch_run_id: str
    source_dir: str
    agent_loop_ref: str
    transcript_ref: str
    repair_cycle_ref: str
    status: str = "ready"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "owner_scope": self.owner_scope,
            "branch_run_id": self.branch_run_id,
            "source_dir": self.source_dir,
            "agent_loop_ref": self.agent_loop_ref,
            "transcript_ref": self.transcript_ref,
            "repair_cycle_ref": self.repair_cycle_ref,
            "status": self.status,
            "created_at": self.created_at,
        }


class AgentWorkerRuntime:
    """Isolated draft manager for coordinator-owned workers."""

    def __init__(self) -> None:
        self._drafts: dict[str, list[WorkerDraft]] = {}
        self._merge_reports: dict[str, list[dict[str, Any]]] = {}
        self._branch_results: dict[str, list[dict[str, Any]]] = {}

    def prepare(
        self,
        *,
        run_id: str,
        generation_mode: GenerationMode,
        draft_source: Path,
        worker_specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
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
                    branch_run_id=f"{run_id}__worker__{worker_id}",
                    source_dir=str(target),
                    agent_loop_ref=f"worker_agent_loop:{run_id}:{worker_id}",
                    transcript_ref=f"worker_transcript:{run_id}:{worker_id}",
                    repair_cycle_ref=f"worker_repair_cycle:{run_id}:{worker_id}",
                )
            )
        self._drafts[run_id] = drafts
        return {"enabled": True, "mode": str(generation_mode.value), "workers": [item.as_dict() for item in drafts]}

    def prepare_workspace_branches(
        self,
        *,
        workspace_id: str,
        run_id: str,
        generation_mode: GenerationMode,
        workspace_service: Any,
        worker_specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        drafts: list[WorkerDraft] = []
        for spec in worker_specs:
            worker_id = str(spec.get("worker_id") or "").strip()
            if not worker_id:
                continue
            branch_run_id = f"{run_id}__worker__{worker_id}"
            source_dir = workspace_service.clone_draft(workspace_id, run_id, branch_run_id)
            drafts.append(
                WorkerDraft(
                    worker_id=worker_id,
                    owner_scope=str(spec.get("owner_scope") or worker_id),
                    branch_run_id=branch_run_id,
                    source_dir=str(source_dir),
                    agent_loop_ref=f"worker_agent_loop:{run_id}:{worker_id}",
                    transcript_ref=f"worker_transcript:{run_id}:{worker_id}",
                    repair_cycle_ref=f"worker_repair_cycle:{run_id}:{worker_id}",
                )
            )
        self._drafts[run_id] = drafts
        return {"enabled": True, "mode": str(generation_mode.value), "workers": [item.as_dict() for item in drafts]}

    def merge_report(self, run_id: str, file_changes: list[DraftAction]) -> dict[str, Any]:
        ownership = AgentWorkerManager.validate_non_conflicting(file_changes)
        report = {
            "run_id": run_id,
            "status": "accepted" if ownership.get("ok") else "conflict",
            "ownership": ownership,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._merge_reports.setdefault(run_id, []).append(report)
        return report

    def record_branch_results(self, run_id: str, file_changes: list[DraftAction]) -> list[dict[str, Any]]:
        grouped: dict[str, list[DraftAction]] = {}
        for action in file_changes:
            grouped.setdefault(AgentWorkerManager.owner_for_path(action.file_path), []).append(action)
        results: list[dict[str, Any]] = []
        for owner, changes in sorted(grouped.items()):
            result = {
                "run_id": run_id,
                "worker_id": owner,
                "status": "branch_diff_ready",
                "agent_loop": "branch_scoped",
                "transcript_ref": f"worker_transcript:{run_id}:{owner}",
                "repair_cycle_ref": f"worker_repair_cycle:{run_id}:{owner}",
                "file_count": len(changes),
                "paths": [item.file_path for item in changes],
                "self_check": {
                    "status": "ready_for_merge",
                    "owned_paths_only": all(AgentWorkerManager.owner_for_path(item.file_path) == owner for item in changes),
                    "path_count": len(changes),
                },
                "summary": f"{owner} branch produced {len(changes)} owned draft change(s).",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            results.append(result)
        self._branch_results.setdefault(run_id, []).extend(results)
        return results

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
            "branch_results": list(self._branch_results.get(run_id, [])),
        }
