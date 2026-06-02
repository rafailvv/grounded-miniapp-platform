from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from app.models.domain import utc_now
from app.models.worker_sessions import (
    WorkerMailboxMessage,
    WorkerMailboxReport,
    WorkerOwnershipLock,
    WorkerOwnershipReport,
    WorkerSessionRecord,
    WorkerSessionsReport,
    WorkerTurnRecord,
)
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.product_workers import canonical_worker_id, ownership_for_worker, product_owner_contract, worker_refs
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService


TERMINAL_STATUSES = {"merged", "rejected"}
ALLOWED_STATUS_TRANSITIONS = {
    "planned": {"ready", "waiting", "running", "blocked", "failed", "completed", "merged", "rejected"},
    "ready": {"running", "waiting", "blocked", "failed", "completed", "merged", "rejected"},
    "waiting": {"ready", "running", "blocked", "failed", "completed", "merged", "rejected"},
    "running": {"waiting", "blocked", "failed", "completed", "merged", "rejected"},
    "blocked": {"waiting", "ready", "running", "failed", "completed", "merged", "rejected"},
    "failed": {"waiting", "ready", "running", "blocked", "rejected"},
    "completed": {"merged", "rejected", "blocked", "failed"},
    "merged": set(),
    "rejected": set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class WorkerSessionService:
    """Durable protocol layer for existing isolated worker branch execution."""

    def __init__(self, store: StateStore, *, event_journal_service: EventJournalService | None = None) -> None:
        self.store = store
        self.event_journal_service = event_journal_service

    def create_sessions(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        worker_tasks: list[dict[str, Any]],
        mailbox: dict[str, Any] | None = None,
        implementation_plan: dict[str, Any] | None = None,
        acceptance_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sessions: list[WorkerSessionRecord] = []
        mailbox_ref = self.mailbox_ref(workspace_id, artifact_run_id)
        ownership_ref = self.ownership_ref(workspace_id, artifact_run_id)
        for task in worker_tasks:
            if not isinstance(task, dict):
                continue
            worker_id = canonical_worker_id(str(task.get("worker_id") or task.get("worker") or ""))
            if not worker_id:
                continue
            session = self._session_from_task(
                workspace_id=workspace_id,
                parent_run_id=parent_run_id,
                artifact_run_id=artifact_run_id,
                worker_id=worker_id,
                task=task,
                mailbox_ref=mailbox_ref,
                ownership_ref=ownership_ref,
            )
            self.store.upsert("reports", self.session_ref(workspace_id, artifact_run_id, worker_id), session.model_dump(mode="json", by_alias=True))
            self._append_run_event(
                workspace_id,
                parent_run_id,
                "worker.session.created",
                {
                    "worker_session_id": session.worker_session_id,
                    "worker_id": worker_id,
                    "status": session.status,
                    "branch_run_id": session.branch_run_id,
                    "context_ref": session.context_ref,
                    "output_ref": session.output_ref,
                },
                source_ref=self.session_ref(workspace_id, artifact_run_id, worker_id),
                idempotency_key=f"worker.session.created:{parent_run_id}:{session.worker_session_id}",
            )
            sessions.append(session)
        ownership = self._build_ownership_report(
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            artifact_run_id=artifact_run_id,
            sessions=sessions,
        )
        self.store.upsert("reports", ownership_ref, ownership.model_dump(mode="json", by_alias=True))
        self._append_run_event(
            workspace_id,
            parent_run_id,
            "worker.ownership.locked" if ownership.status == "passed" else "worker.ownership.conflict",
            {
                "ownership_ref": ownership_ref,
                "status": ownership.status,
                "lock_count": len(ownership.locks),
                "conflicts": ownership.conflicts,
            },
            source_ref=ownership_ref,
            idempotency_key=f"worker.ownership:{parent_run_id}:{artifact_run_id}:{ownership.status}",
        )
        mailbox_report = self._build_mailbox_report(
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            artifact_run_id=artifact_run_id,
            sessions=sessions,
            mailbox=mailbox or {},
            implementation_plan=implementation_plan or {},
            acceptance_contract=acceptance_contract or {},
        )
        self.store.upsert("reports", mailbox_ref, mailbox_report.model_dump(mode="json", by_alias=True))
        report = self._report_from_parts(
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            artifact_run_id=artifact_run_id,
            sessions=sessions,
            mailbox=mailbox_report,
            ownership=ownership,
        )
        self.store.upsert("reports", self.sessions_ref(workspace_id, artifact_run_id), report)
        return report

    def list_sessions(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str | None = None) -> dict[str, Any]:
        artifact = artifact_run_id or parent_run_id
        stored = self.store.get("reports", self.sessions_ref(workspace_id, artifact))
        if isinstance(stored, dict):
            return stored
        sessions: list[WorkerSessionRecord] = []
        for payload in self.store.list("reports"):
            if not isinstance(payload, dict) or payload.get("schema") != "grounded.worker_session.v1":
                continue
            if str(payload.get("workspace_id") or "") == workspace_id and str(payload.get("parent_run_id") or "") == parent_run_id:
                sessions.append(WorkerSessionRecord.model_validate(payload))
        mailbox = self.mailbox(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact)
        ownership = self.ownership(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact)
        report = self._report_from_parts(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact, sessions=sessions, mailbox=WorkerMailboxReport.model_validate(mailbox), ownership=WorkerOwnershipReport.model_validate(ownership))
        self.store.upsert("reports", self.sessions_ref(workspace_id, artifact), report)
        return report

    def get_session(self, *, workspace_id: str, parent_run_id: str, worker_session_id: str, artifact_run_id: str | None = None) -> dict[str, Any]:
        for payload in self.store.list("reports"):
            if not isinstance(payload, dict) or payload.get("schema") != "grounded.worker_session.v1":
                continue
            if str(payload.get("worker_session_id") or "") == worker_session_id and str(payload.get("parent_run_id") or "") == parent_run_id:
                turns = [
                    item
                    for item in self.store.list("reports")
                    if isinstance(item, dict)
                    and item.get("schema") == "grounded.worker_turn.v1"
                    and item.get("worker_session_id") == worker_session_id
                ]
                return {"schema": "grounded.worker_session_detail.v1", "status": "ready", "session": payload, "turns": sorted(turns, key=lambda item: str(item.get("started_at") or "")), "mailbox": self.mailbox(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id or parent_run_id), "ownership": self.ownership(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id or parent_run_id)}
        raise KeyError(f"Worker session not found: {worker_session_id}")

    def mailbox(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str) -> dict[str, Any]:
        ref = self.mailbox_ref(workspace_id, artifact_run_id)
        payload = self.store.get("reports", ref)
        if isinstance(payload, dict):
            return payload
        return WorkerMailboxReport(workspace_id=workspace_id, parent_run_id=parent_run_id, mailbox_ref=ref).model_dump(mode="json", by_alias=True)

    def ownership(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str) -> dict[str, Any]:
        ref = self.ownership_ref(workspace_id, artifact_run_id)
        payload = self.store.get("reports", ref)
        if isinstance(payload, dict):
            return payload
        return WorkerOwnershipReport(workspace_id=workspace_id, parent_run_id=parent_run_id, ownership_ref=ref).model_dump(mode="json", by_alias=True)

    def append_message(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        from_worker: str,
        to_worker: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        mailbox_payload = WorkerMailboxReport.model_validate(self.mailbox(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id))
        message_id = message_id or self.message_id(parent_run_id, from_worker, to_worker, kind, payload or {})
        if any(item.message_id == message_id for item in mailbox_payload.items):
            return mailbox_payload.model_dump(mode="json", by_alias=True)
        payload_ref = f"worker_mailbox_message:{workspace_id}:{artifact_run_id}:{message_id}"
        self.store.upsert("reports", payload_ref, {"schema": "grounded.worker_mailbox_message_payload.v1", "workspace_id": workspace_id, "parent_run_id": parent_run_id, "payload": payload or {}, "created_at": _now()})
        mailbox_payload.items.append(
            WorkerMailboxMessage(
                message_id=message_id,
                kind=kind,
                from_worker=from_worker,
                to_worker=canonical_worker_id(to_worker),
                payload_ref=payload_ref,
                payload=payload or {},
            )
        )
        mailbox_payload.next_sequence = len(mailbox_payload.items) + 1
        mailbox_payload.updated_at = utc_now()
        self.store.upsert("reports", mailbox_payload.mailbox_ref, mailbox_payload.model_dump(mode="json", by_alias=True))
        self._append_run_event(
            workspace_id,
            parent_run_id,
            "worker.mailbox.message_created",
            {"message_id": message_id, "kind": kind, "from": from_worker, "to": to_worker, "payload_ref": payload_ref},
            source_ref=payload_ref,
            idempotency_key=f"worker.mailbox.message_created:{parent_run_id}:{message_id}",
        )
        return mailbox_payload.model_dump(mode="json", by_alias=True)

    def consume_message(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str, message_id: str) -> dict[str, Any]:
        mailbox_payload = WorkerMailboxReport.model_validate(self.mailbox(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id))
        updated = False
        for item in mailbox_payload.items:
            if item.message_id == message_id and item.status == "pending":
                item.status = "consumed"
                item.consumed_at = utc_now()
                updated = True
        if updated:
            mailbox_payload.updated_at = utc_now()
            self.store.upsert("reports", mailbox_payload.mailbox_ref, mailbox_payload.model_dump(mode="json", by_alias=True))
            self._append_run_event(workspace_id, parent_run_id, "worker.mailbox.message_consumed", {"message_id": message_id, "mailbox_ref": mailbox_payload.mailbox_ref}, source_ref=mailbox_payload.mailbox_ref, idempotency_key=f"worker.mailbox.message_consumed:{parent_run_id}:{message_id}")
        return mailbox_payload.model_dump(mode="json", by_alias=True)

    def start_turn(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str, worker_id: str, input_refs: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._load_session(workspace_id, artifact_run_id, worker_id)
        self.update_status(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id, worker_id=worker_id, status="running")
        turn_id = f"wturn_{_digest(f'{session.worker_session_id}:{len(self._turns_for_session(session.worker_session_id)) + 1}')}"
        turn = WorkerTurnRecord(
            worker_turn_id=turn_id,
            worker_session_id=session.worker_session_id,
            parent_run_id=parent_run_id,
            workspace_id=workspace_id,
            worker_id=canonical_worker_id(worker_id),
            status="running",
            input_refs=input_refs or {},
            tool_trace_ref=f"worker_agent_loop:{artifact_run_id}:{canonical_worker_id(worker_id)}",
        )
        session.latest_turn_id = turn_id
        session.updated_at = utc_now()
        self.store.upsert("reports", self.turn_ref(workspace_id, artifact_run_id, turn_id), turn.model_dump(mode="json", by_alias=True))
        self.store.upsert("reports", self.session_ref(workspace_id, artifact_run_id, worker_id), session.model_dump(mode="json", by_alias=True))
        self._refresh_index(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id)
        self._append_run_event(workspace_id, parent_run_id, "worker.turn.started", {"worker_session_id": session.worker_session_id, "worker_turn_id": turn_id, "worker_id": worker_id}, source_ref=self.turn_ref(workspace_id, artifact_run_id, turn_id), idempotency_key=f"worker.turn.started:{parent_run_id}:{turn_id}")
        return turn.model_dump(mode="json", by_alias=True)

    def complete_turn(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        worker_id: str,
        status: str,
        changed_files: list[str] | None = None,
        output_ref: str | None = None,
        diagnostics_ref: str | None = None,
        failure_packet_ref: str | None = None,
        proof_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._load_session(workspace_id, artifact_run_id, worker_id)
        turn_payload = self.store.get("reports", self.turn_ref(workspace_id, artifact_run_id, str(session.latest_turn_id or "")))
        if not isinstance(turn_payload, dict):
            turn_payload = self.start_turn(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id, worker_id=worker_id)
        turn = WorkerTurnRecord.model_validate(turn_payload)
        terminal = "failed" if status == "failed" else "completed"
        turn.status = terminal
        turn.changed_files = list(dict.fromkeys(changed_files or []))
        turn.output_ref = output_ref or turn.output_ref
        turn.diagnostics_ref = diagnostics_ref or turn.diagnostics_ref
        turn.failure_packet_ref = failure_packet_ref or turn.failure_packet_ref
        turn.proof_refs = list(dict.fromkeys([*turn.proof_refs, *(proof_refs or [])]))
        turn.completed_at = utc_now()
        turn.metadata = {**turn.metadata, **(metadata or {})}
        self.store.upsert("reports", self.turn_ref(workspace_id, artifact_run_id, turn.worker_turn_id), turn.model_dump(mode="json", by_alias=True))
        new_status = "failed" if status == "failed" else "completed"
        self.update_status(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id, worker_id=worker_id, status=new_status, output_ref=output_ref, proof_refs=proof_refs)
        self._append_run_event(
            workspace_id,
            parent_run_id,
            "worker.turn.failed" if terminal == "failed" else "worker.turn.completed",
            {"worker_session_id": session.worker_session_id, "worker_turn_id": turn.worker_turn_id, "worker_id": worker_id, "status": terminal, "changed_files": turn.changed_files, "output_ref": output_ref, "failure_packet_ref": failure_packet_ref},
            source_ref=self.turn_ref(workspace_id, artifact_run_id, turn.worker_turn_id),
            idempotency_key=f"worker.turn.{terminal}:{parent_run_id}:{turn.worker_turn_id}",
        )
        return turn.model_dump(mode="json", by_alias=True)

    def update_status(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        worker_id: str,
        status: str,
        output_ref: str | None = None,
        proof_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self._load_session(workspace_id, artifact_run_id, worker_id)
        if status != session.status and status not in ALLOWED_STATUS_TRANSITIONS.get(session.status, set()):
            raise ValueError(f"Invalid worker session transition: {session.status} -> {status}")
        session.status = status  # type: ignore[assignment]
        session.output_ref = output_ref or session.output_ref
        session.proof_refs = list(dict.fromkeys([*session.proof_refs, *(proof_refs or [])]))
        session.updated_at = utc_now()
        self.store.upsert("reports", self.session_ref(workspace_id, artifact_run_id, worker_id), session.model_dump(mode="json", by_alias=True))
        self._refresh_index(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id)
        return session.model_dump(mode="json", by_alias=True)

    def mark_merge_decision(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        decisions: list[dict[str, Any]],
        status: str,
        merge_ref: str,
    ) -> None:
        event_type = "worker.merge.accepted" if status in {"merged", "partial", "ready"} else "worker.merge.rejected"
        for decision in decisions:
            worker_id = canonical_worker_id(str(decision.get("worker_id") or ""))
            if not worker_id:
                continue
            decision_status = str(decision.get("decision") or "")
            target_status = "merged" if decision_status == "accepted" and status in {"merged", "partial"} else "rejected" if decision_status in {"rejected", "needs_repair"} else "completed"
            try:
                self.update_status(
                    workspace_id=workspace_id,
                    parent_run_id=parent_run_id,
                    artifact_run_id=artifact_run_id,
                    worker_id=worker_id,
                    status=target_status,
                    output_ref=str(decision.get("output_ref") or "") or None,
                    proof_refs=[str(item) for item in decision.get("proof_refs") or []],
                )
            except (KeyError, ValueError):
                continue
        self._append_run_event(workspace_id, parent_run_id, event_type, {"status": status, "merge_ref": merge_ref, "decisions": decisions}, source_ref=merge_ref, idempotency_key=f"{event_type}:{parent_run_id}:{artifact_run_id}:{status}")

    def resume(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str, worker_session_id: str) -> dict[str, Any]:
        detail = self.get_session(workspace_id=workspace_id, parent_run_id=parent_run_id, worker_session_id=worker_session_id, artifact_run_id=artifact_run_id)
        session = detail["session"]
        worker_id = str(session.get("worker_id") or "")
        output_ref = str(session.get("output_ref") or "")
        output = self.store.get("reports", output_ref) if output_ref else {}
        changed_files = [str(item) for item in (output.get("changed_files") if isinstance(output, dict) else []) or []]
        owner_matches = all(AgentWorkerManager.owner_for_path(path) == worker_id or not path for path in changed_files)
        target_worker = worker_id if owner_matches and session.get("status") in {"failed", "blocked", "waiting", "ready", "completed"} else "repair_worker"
        return {
            "schema": "grounded.worker_session_resume.v1",
            "status": "ready",
            "workspace_id": workspace_id,
            "run_id": parent_run_id,
            "worker_session_id": worker_session_id,
            "decision": "continue_worker" if target_worker == worker_id else "activate_repair_worker",
            "target_worker_id": target_worker,
            "input_refs": {
                "worker_session": self.session_ref(workspace_id, artifact_run_id, worker_id),
                "output_ref": output_ref or None,
                "mailbox_ref": session.get("mailbox_ref"),
                "ownership_ref": session.get("ownership_ref"),
            },
            "changed_files": changed_files,
        }

    def _session_from_task(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        worker_id: str,
        task: dict[str, Any],
        mailbox_ref: str,
        ownership_ref: str,
    ) -> WorkerSessionRecord:
        existing = self.store.get("reports", self.session_ref(workspace_id, artifact_run_id, worker_id))
        if isinstance(existing, dict):
            return WorkerSessionRecord.model_validate(existing)
        refs = worker_refs(workspace_id, artifact_run_id, worker_id)
        branch_role = str(task.get("branch_role") or AgentWorkerManager.branch_role(worker_id))
        status = "waiting" if branch_role == "verifier" else "ready"
        return WorkerSessionRecord(
            worker_session_id=self.worker_session_id(parent_run_id, worker_id),
            parent_run_id=parent_run_id,
            workspace_id=workspace_id,
            worker_id=worker_id,
            role=branch_role,
            stage=str(task.get("branch_stage") or AgentWorkerManager.branch_stage(worker_id)),
            status=status,
            branch_run_id=f"{artifact_run_id}__worker__{worker_id}" if branch_role == "writer" else None,
            ownership=dict(task.get("ownership") or ownership_for_worker(worker_id)),
            tool_allowlist=[str(item) for item in task.get("tool_allowlist") or []],
            context_ref=str(task.get("context_ref") or refs["context_ref"]),
            memory_ref=str(task.get("memory_snapshot_ref") or refs["memory_snapshot_ref"]),
            output_ref=str(task.get("output_ref") or refs["output_ref"]),
            mailbox_ref=mailbox_ref,
            ownership_ref=ownership_ref,
            metadata={"task": {key: value for key, value in task.items() if key not in {"prompt"}}},
        )

    def _build_ownership_report(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str, sessions: list[WorkerSessionRecord]) -> WorkerOwnershipReport:
        specs = [
            {
                "worker_id": session.worker_id,
                "worker": session.worker_id,
                "owner_scope": product_owner_contract(session.worker_id).get("owner_scope"),
                "branch_role": session.role,
                "ownership": session.ownership,
            }
            for session in sessions
        ]
        write_scope = AgentWorkerManager.write_scope_report(specs)
        locks = [
            WorkerOwnershipLock(
                lock_id=str(lock.get("lock_id") or lock.get("worker") or ""),
                worker_session_id=self.worker_session_id(parent_run_id, str(lock.get("worker") or "")) if lock.get("worker") else None,
                worker_id=canonical_worker_id(str(lock.get("worker") or "")),
                lease_owner=self.worker_session_id(parent_run_id, str(lock.get("worker") or "")) if lock.get("worker") else None,
                allowed_paths=[str(item) for item in lock.get("path_prefixes") or lock.get("allowed_paths") or []],
                forbidden_paths=[str(item) for item in lock.get("forbidden_paths") or []],
                exclusive_write=bool(lock.get("exclusive_write", True)),
                status="locked" if write_scope.get("status") == "passed" else "conflict",
            )
            for lock in write_scope.get("locks") or []
            if isinstance(lock, dict)
        ]
        return WorkerOwnershipReport(
            status=str(write_scope.get("status") or "passed"),
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            ownership_ref=self.ownership_ref(workspace_id, artifact_run_id),
            locks=locks,
            conflicts=[item for item in write_scope.get("overlaps") or write_scope.get("conflicts") or [] if isinstance(item, dict)],
            forbidden=[],
            merge_eligible=write_scope.get("status") == "passed",
        )

    def _build_mailbox_report(
        self,
        *,
        workspace_id: str,
        parent_run_id: str,
        artifact_run_id: str,
        sessions: list[WorkerSessionRecord],
        mailbox: dict[str, Any],
        implementation_plan: dict[str, Any],
        acceptance_contract: dict[str, Any],
    ) -> WorkerMailboxReport:
        report = WorkerMailboxReport(workspace_id=workspace_id, parent_run_id=parent_run_id, mailbox_ref=self.mailbox_ref(workspace_id, artifact_run_id))
        for session in sessions:
            if session.worker_id == "backend_api_worker":
                kind = "product_contract"
                from_worker = "planner"
            elif session.role == "verifier":
                kind = "wait_for_green_checks"
                from_worker = "coordinator"
            else:
                kind = "wait_for_backend_contract"
                from_worker = "backend_api_worker"
            message_id = self.message_id(parent_run_id, from_worker, session.worker_id, kind, {"worker_session_id": session.worker_session_id})
            payload = {
                "worker_session_id": session.worker_session_id,
                "worker_id": session.worker_id,
                "context_ref": session.context_ref,
                "memory_ref": session.memory_ref,
                "output_ref": session.output_ref,
                "ownership_ref": session.ownership_ref,
                "acceptance_contract_ref": f"acceptance_contract:{workspace_id}:{parent_run_id}",
                "primary_entities": list((implementation_plan or {}).get("primary_entities") or [])[:8],
                "contract_required": bool((acceptance_contract or {}).get("required")),
                "legacy_mailbox_enabled": bool(mailbox.get("enabled")) if isinstance(mailbox, dict) else False,
            }
            payload_ref = f"worker_mailbox_message:{workspace_id}:{artifact_run_id}:{message_id}"
            self.store.upsert("reports", payload_ref, {"schema": "grounded.worker_mailbox_message_payload.v1", "workspace_id": workspace_id, "parent_run_id": parent_run_id, "payload": payload, "created_at": _now()})
            report.items.append(WorkerMailboxMessage(message_id=message_id, kind=kind, from_worker=from_worker, to_worker=session.worker_id, payload_ref=payload_ref, payload=payload))
            self._append_run_event(workspace_id, parent_run_id, "worker.mailbox.message_created", {"message_id": message_id, "kind": kind, "from": from_worker, "to": session.worker_id, "payload_ref": payload_ref}, source_ref=payload_ref, idempotency_key=f"worker.mailbox.message_created:{parent_run_id}:{message_id}")
        report.next_sequence = len(report.items) + 1
        return report

    def _report_from_parts(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str, sessions: list[WorkerSessionRecord], mailbox: WorkerMailboxReport, ownership: WorkerOwnershipReport) -> dict[str, Any]:
        resume_candidates = [
            {
                "worker_session_id": session.worker_session_id,
                "worker_id": session.worker_id,
                "status": session.status,
                "reason": "failed_or_blocked_worker",
                "resume_endpoint": f"/runs/{parent_run_id}/worker-sessions/{session.worker_session_id}/resume",
            }
            for session in sessions
            if session.status in {"failed", "blocked", "waiting"}
        ]
        return WorkerSessionsReport(
            workspace_id=workspace_id,
            parent_run_id=parent_run_id,
            sessions_ref=self.sessions_ref(workspace_id, artifact_run_id),
            mailbox_ref=mailbox.mailbox_ref,
            ownership_ref=ownership.ownership_ref,
            items=sessions,
            mailbox=mailbox,
            ownership=ownership,
            resume_candidates=resume_candidates,
            next_sequence=self._next_sequence(self.sessions_ref(workspace_id, artifact_run_id)),
        ).model_dump(mode="json", by_alias=True)

    def _refresh_index(self, *, workspace_id: str, parent_run_id: str, artifact_run_id: str) -> None:
        sessions = [
            WorkerSessionRecord.model_validate(payload)
            for payload in self.store.list("reports")
            if isinstance(payload, dict)
            and payload.get("schema") == "grounded.worker_session.v1"
            and payload.get("workspace_id") == workspace_id
            and payload.get("parent_run_id") == parent_run_id
        ]
        mailbox = WorkerMailboxReport.model_validate(self.mailbox(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id))
        ownership = WorkerOwnershipReport.model_validate(self.ownership(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id))
        self.store.upsert("reports", self.sessions_ref(workspace_id, artifact_run_id), self._report_from_parts(workspace_id=workspace_id, parent_run_id=parent_run_id, artifact_run_id=artifact_run_id, sessions=sessions, mailbox=mailbox, ownership=ownership))

    def _load_session(self, workspace_id: str, artifact_run_id: str, worker_id: str) -> WorkerSessionRecord:
        payload = self.store.get("reports", self.session_ref(workspace_id, artifact_run_id, worker_id))
        if not isinstance(payload, dict):
            raise KeyError(f"Worker session not found for {worker_id}")
        return WorkerSessionRecord.model_validate(payload)

    def _turns_for_session(self, worker_session_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.store.list("reports")
            if isinstance(item, dict) and item.get("schema") == "grounded.worker_turn.v1" and item.get("worker_session_id") == worker_session_id
        ]

    def _next_sequence(self, ref: str) -> int:
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            return 1
        return int(payload.get("next_sequence") or 1) + 1

    def _append_run_event(self, workspace_id: str, run_id: str, event_type: str, payload: dict[str, Any], *, source_ref: str | None, idempotency_key: str | None = None) -> None:
        if self.event_journal_service is None:
            return
        try:
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                actor="system",
                summary=event_type,
                source_ref=source_ref,
                idempotency_key=idempotency_key,
            )
        except Exception:
            return

    @staticmethod
    def worker_session_id(parent_run_id: str, worker_id: str) -> str:
        return f"wsess_{_digest(f'{parent_run_id}:{canonical_worker_id(worker_id)}')}"

    @staticmethod
    def sessions_ref(workspace_id: str, artifact_run_id: str) -> str:
        return f"worker_sessions:{workspace_id}:{artifact_run_id}"

    @staticmethod
    def session_ref(workspace_id: str, artifact_run_id: str, worker_id: str) -> str:
        return f"worker_session:{workspace_id}:{artifact_run_id}:{canonical_worker_id(worker_id)}"

    @staticmethod
    def turn_ref(workspace_id: str, artifact_run_id: str, worker_turn_id: str) -> str:
        return f"worker_turn:{workspace_id}:{artifact_run_id}:{worker_turn_id}"

    @staticmethod
    def mailbox_ref(workspace_id: str, artifact_run_id: str) -> str:
        return f"worker_mailbox_v2:{workspace_id}:{artifact_run_id}"

    @staticmethod
    def ownership_ref(workspace_id: str, artifact_run_id: str) -> str:
        return f"worker_ownership:{workspace_id}:{artifact_run_id}"

    @staticmethod
    def message_id(parent_run_id: str, from_worker: str, to_worker: str, kind: str, payload: dict[str, Any]) -> str:
        return f"wmsg_{_digest(f'{parent_run_id}:{from_worker}:{canonical_worker_id(to_worker)}:{kind}:{payload}')}"
