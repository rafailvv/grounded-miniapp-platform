from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.product_workers import ownership_for_worker, product_owner_contract


@dataclass
class WorkerDraft:
    worker_id: str
    owner_scope: str
    branch_run_id: str
    source_dir: str
    agent_loop_ref: str
    transcript_ref: str
    repair_cycle_ref: str
    branch_role: str = "writer"
    branch_stage: str = "role_ui_and_tests"
    branch_policy: str = "isolated_draft_writer"
    base_run_id: str = ""
    branch_kind: str = "workspace_draft_clone"
    write_scope: dict[str, Any] = field(default_factory=dict)
    tool_allowlist: list[str] = field(default_factory=list)
    merge_base_ref: str = ""
    status: str = "ready"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "owner_scope": self.owner_scope,
            "branch_run_id": self.branch_run_id,
            "source_dir": self.source_dir,
            "agent_loop_ref": self.agent_loop_ref,
            "transcript_ref": self.transcript_ref,
            "repair_cycle_ref": self.repair_cycle_ref,
            "branch_role": self.branch_role,
            "branch_stage": self.branch_stage,
            "branch_policy": self.branch_policy,
            "base_run_id": self.base_run_id,
            "branch_kind": self.branch_kind,
            "write_scope": self.write_scope,
            "tool_allowlist": self.tool_allowlist,
            "merge_base_ref": self.merge_base_ref,
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
                    branch_role=str(spec.get("branch_role") or AgentWorkerManager.branch_role(worker_id)),
                    branch_stage=str(spec.get("branch_stage") or AgentWorkerManager.branch_stage(worker_id)),
                    branch_policy=str(spec.get("branch_policy") or AgentWorkerManager.branch_policy(worker_id)),
                    base_run_id=run_id,
                    branch_kind="filesystem_clone",
                    write_scope=dict(spec.get("ownership") or ownership_for_worker(worker_id)),
                    tool_allowlist=[str(item) for item in spec.get("tool_allowlist") or []],
                    merge_base_ref=f"coordinator_draft:{run_id}",
                )
            )
        self._drafts[run_id] = drafts
        write_scope_report = AgentWorkerManager.write_scope_report(worker_specs)
        return {
            "schema": "grounded.worker_drafts.v2",
            "enabled": bool(drafts),
            "mode": str(generation_mode.value),
            "isolation": "filesystem_clone_per_worker",
            "workers": [item.as_dict() for item in drafts],
            "write_scope_report": write_scope_report,
            "status": "ready" if write_scope_report.get("status") == "passed" else "conflict",
        }

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
                    branch_role=str(spec.get("branch_role") or AgentWorkerManager.branch_role(worker_id)),
                    branch_stage=str(spec.get("branch_stage") or AgentWorkerManager.branch_stage(worker_id)),
                    branch_policy=str(spec.get("branch_policy") or AgentWorkerManager.branch_policy(worker_id)),
                    base_run_id=run_id,
                    branch_kind="workspace_draft_clone",
                    write_scope=dict(spec.get("ownership") or ownership_for_worker(worker_id)),
                    tool_allowlist=[str(item) for item in spec.get("tool_allowlist") or []],
                    merge_base_ref=f"coordinator_draft:{workspace_id}:{run_id}",
                )
            )
        self._drafts[run_id] = drafts
        write_scope_report = AgentWorkerManager.write_scope_report(worker_specs)
        return {
            "schema": "grounded.worker_drafts.v2",
            "enabled": bool(drafts),
            "mode": str(generation_mode.value),
            "isolation": "workspace_draft_clone_per_worker",
            "workers": [item.as_dict() for item in drafts],
            "write_scope_report": write_scope_report,
            "status": "ready" if write_scope_report.get("status") == "passed" else "conflict",
        }

    def merge_report(self, run_id: str, file_changes: list[DraftAction]) -> dict[str, Any]:
        ownership = AgentWorkerManager.validate_non_conflicting(file_changes)
        conflict_report = AgentWorkerManager.conflict_report(file_changes)
        report = {
            "schema": "grounded.worker_merge_report.v2",
            "branch_schema": "grounded.worker_branch_plan.v2",
            "run_id": run_id,
            "status": "accepted" if ownership.get("ok") else "conflict",
            "ownership": ownership,
            "conflict_report": conflict_report,
            "merge_decision": {
                "decision": "accept" if ownership.get("ok") else "repair_required",
                "mergeable_paths": conflict_report.get("mergeable_paths") or [],
                "blocked_paths": conflict_report.get("blocked_paths") or [],
                "requires_verifier": bool(ownership.get("ok")),
            },
            "post_merge_verifier": {
                "worker_id": "mobile_polish_worker",
                "status": "planned" if ownership.get("ok") else "blocked_until_conflicts_resolved",
                "branch_policy": AgentWorkerManager.branch_policy("mobile_polish_worker"),
            },
            "manager_decision_policy": "accept non-conflicting owned diffs; reject forbidden/conflicting paths; create repair_worker packet",
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
                "lane_id": product_owner_contract(owner).get("lane_id"),
                "ownership_kind": product_owner_contract(owner).get("ownership_kind"),
                "status": "branch_diff_ready",
                "agent_loop": "branch_scoped",
                "transcript_ref": f"worker_transcript:{run_id}:{owner}",
                "repair_cycle_ref": f"worker_repair_cycle:{run_id}:{owner}",
                "file_count": len(changes),
                "paths": [item.file_path for item in changes],
                "product_owner_contract": product_owner_contract(owner),
                "self_check": {
                    "status": "ready_for_merge",
                    "owned_paths_only": all(AgentWorkerManager.owner_for_path(item.file_path) == owner for item in changes),
                    "path_count": len(changes),
                },
                "merge_evidence": AgentWorkerManager.merge_evidence_packet(
                    worker_id=owner,
                    changed_files=[item.file_path for item in changes],
                    decision="deferred",
                    output_ref=f"worker_output:{run_id}:{owner}",
                ),
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
