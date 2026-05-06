from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from typing import Any
from uuid import uuid4

from app.models.domain import CreateRunRequest, RunRecord
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.product_workers import (
    PRODUCT_WORKERS,
    canonical_worker_id,
    legacy_worker_id,
    ownership_for_worker,
    worker_refs,
)
from app.repositories.platform_db import PlatformDb
from app.services.background_task_service import BackgroundTaskService
from app.repositories.state_store import StateStore
from app.services.exec_policy_service import ExecPolicyService
from app.services.generation_enhancements import (
    AcceptanceScenarioGenerator,
    ConfigMigrationCatalog,
    MagicDocsBuilder,
    ProjectInstructionBundle,
    SkillPackCatalog,
    SlashCommandCatalog,
    TraceReducer,
    VisualQAGenerator,
    WorkerRoleCatalog,
)
from app.services.miniapp_contract import MiniAppContractMaterializer, MiniAppRouteRegistry
from app.services.repair_catalog import RepairCatalog
from app.services.run_state_machine import RunStateMachine
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, tool_envelope, tool_registry_contract
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.run_compaction import RunCompactionService
from app.services.run_protocol import RunProtocolConflict, RunProtocolService, diff_sha256
from app.services.trace_bundle import TraceBundleReducer
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService
from app.core.config import Settings
from app.ai.openai_client import OpenAIClient
from app.models.artifacts import PatchOperationModel


def re_slug(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "skill"


class WorkbenchService:
    """Read-model and scaffold service for the agent workbench APIs."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        workspace_service: WorkspaceService,
        run_service: RunService,
        openai_client: OpenAIClient,
        exec_policy_service: ExecPolicyService,
        platform_db: PlatformDb | None = None,
        run_protocol_service: RunProtocolService | None = None,
        run_compaction_service: RunCompactionService | None = None,
        background_task_service: BackgroundTaskService | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service
        self.run_service = run_service
        self.openai_client = openai_client
        self.exec_policy_service = exec_policy_service
        self.platform_db = platform_db
        self.run_protocol_service = run_protocol_service
        self.run_compaction_service = run_compaction_service
        self.background_task_service = background_task_service

    def tool_events(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        events: list[dict[str, Any]] = []
        events.extend(self._stored_tool_events(run_id))
        for item in artifacts.get("agent_activity_events") or run.agent_activity_events or []:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool") or (item.get("details") if isinstance(item.get("details"), dict) else {}).get("tool")
            events.append(
                tool_envelope(
                    tool=tool or item.get("type") or "agent.activity",
                    input_payload={"message": item.get("message"), "details": item.get("details") or {}},
                    result={"status": item.get("status") or item.get("type") or "recorded"},
                    artifacts=[{"ref": item.get("artifact_ref")}] if item.get("artifact_ref") else [],
                    timing={"duration_ms": item.get("duration_ms") or item.get("elapsed_ms")},
                    tool_call_id=str(item.get("tool_use_id") or item.get("batch_id") or f"activity_{len(events) + 1}"),
                )
            )
        for decision in artifacts.get("command_policy_decisions") or []:
            events.append(
                tool_envelope(
                    tool="policy.evaluate",
                    input_payload=decision if isinstance(decision, dict) else {"decision": decision},
                    result={"status": "recorded"},
                    risk="read_only",
                )
            )
        return {"run_id": run_id, "tool_protocol_version": TOOL_PROTOCOL_VERSION, "events": events}

    def run_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        items = self.platform_db.list_run_events(run_id, after_sequence=after_sequence, limit=limit) if self.platform_db is not None else []
        snapshots = self.platform_db.list_run_state_snapshots(run_id, limit=20) if self.platform_db is not None else []
        protocol_events = self.run_protocol_service.protocol_events(run_id).get("items", []) if self.run_protocol_service is not None else []
        return {
            "run_id": run_id,
            "schema": "grounded.run_events.v1",
            "status": "ok",
            "blocking": False,
            "items": items,
            "protocol_events": protocol_events,
            "compaction_events": [item for item in protocol_events if item.get("type") == "compact_boundary"],
            "state_snapshots": snapshots,
            "next_sequence": max([int(item.get("sequence") or 0) for item in items], default=int(after_sequence or 0)),
        }

    def protocol(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.run_protocol_service is None:
            return {"schema": "grounded.run_protocol.v1", "run_id": run_id, "workspace_id": run.workspace_id, "status": "unavailable", "items": []}
        payload = self.run_protocol_service.protocol_events(run_id)
        bookmarks = self.run_protocol_service.bookmarks(run_id)
        payload["workspace_id"] = run.workspace_id
        payload["bookmarks"] = bookmarks.get("items") or []
        payload["latest_bookmark"] = (bookmarks.get("items") or [None])[0]
        return payload

    def bookmarks(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        if self.run_protocol_service is None:
            return {"schema": "grounded.run_bookmarks.v1", "run_id": run_id, "status": "unavailable", "items": []}
        return self.run_protocol_service.bookmarks(run_id)

    def resume_from_bookmark(self, run_id: str, bookmark_id: str, *, prompt: str | None = None, fork: bool = False) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.run_protocol_service is None:
            raise RunProtocolConflict({"reason": "protocol_unavailable", "message": "Run protocol service is unavailable.", "run_id": run_id})
        bookmark = self.run_protocol_service.get_bookmark(run_id, bookmark_id)
        try:
            current_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id)
        except Exception:
            current_diff = ""
        self.run_protocol_service.validate_bookmark(run, bookmark, current_diff_sha256=diff_sha256(current_diff))
        request = CreateRunRequest(
            prompt=str(prompt or run.prompt),
            mode="fix" if not fork else run.mode,
            intent="edit" if not fork else run.intent,
            apply_strategy="staged_auto_apply",
            target_role_scope=list(run.target_role_scope),
            model_profile=run.model_profile,
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
            generation_mode=str(getattr(run.generation_mode, "value", run.generation_mode) or "balanced"),
            resume_from_run_id=run.run_id,
            session_id=run.session_id,
            resume_bookmark_id=bookmark_id,
            forked_from_run_id=run.run_id if fork else None,
        )
        created = self.run_service.create_run(run.workspace_id, request)
        return {
            "schema": "grounded.run_bookmark_action.v1",
            "status": "started",
            "action": "fork" if fork else "resume",
            "source_run_id": run.run_id,
            "bookmark_id": bookmark_id,
            "run": created.model_dump(mode="json"),
        }

    def trace_view(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        timeline = self.timeline(run_id)["items"]
        artifacts = self._run_artifacts_or_empty(run_id)
        tool_events = self.tool_events(run_id).get("events") or []
        failures = [
            item
            for item in timeline
            if str(item.get("status") or "").lower() in {"failed", "blocked", "conflict"}
            or str(item.get("kind") or "") in {"failure"}
        ]
        fixes = [
            item
            for item in timeline
            if str(item.get("kind") or "") in {"editing", "apply", "checks", "browser"}
            and str(item.get("status") or "").lower() in {"completed", "passed", "applied"}
        ]
        reducer = {
            "why": self._trace_why(run, artifacts),
            "failed_checks": [item for item in timeline if item.get("kind") == "checks" and item.get("status") == "failed"],
            "patches": [item for item in timeline if item.get("kind") in {"editing", "diff", "apply"}],
            "browser_proofs": [item for item in timeline if item.get("kind") == "browser"],
            "failures": failures,
            "fixes": fixes,
        }
        payload = {
            "run_id": run_id,
            "trace_id": f"trace_{run_id}",
            "status": run.status,
            "apply_status": run.apply_status,
            "timeline": timeline,
            "reducer": reducer,
            "artifact_refs": {
                "transcript": run.agent_transcript_ref,
                "tool_trace": run.tool_trace_ref,
                "rollout_trace": run.rollout_trace_ref,
                "browser_proof": run.browser_proof_ref,
                "verification": run.verification_report_ref,
            },
            "reduced_trace": TraceReducer.build(
                run=run,
                timeline=timeline,
                tool_events=[item for item in tool_events if isinstance(item, dict)],
                artifacts=artifacts,
            ),
        }
        self.store.upsert("reports", f"trace_view:{run_id}", payload)
        return payload

    def record_tool_event(self, run_id: str | None, event: dict[str, Any]) -> dict[str, Any]:
        if not run_id:
            return event
        self.run_service.get_run(run_id)
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": datetime.now(timezone.utc).isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)
        return item

    def evaluate_command_for_run(self, run_id: str, command: str, *, preset: str = "safe_auto") -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        evaluation = self.exec_policy_service.evaluate_command(command, preset=preset)
        approval = dict(evaluation.get("approval") or {})
        if approval.get("required") and approval.get("approval_id"):
            self._upsert_approval(
                run_id,
                {
                    "approval_id": str(approval["approval_id"]),
                    "status": "pending",
                    "kind": "command",
                    "risk": (evaluation.get("decision") or {}).get("risk"),
                    "summary": self.exec_policy_service.redact(command),
                    "input": {"command": self.exec_policy_service.redact(command), "workspace_id": run.workspace_id},
                    "policy_decision": evaluation.get("decision"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        self.record_tool_event(
            run_id,
            tool_envelope(
                tool="policy.evaluate",
                input_payload={"command": self.exec_policy_service.redact(command), "preset": preset},
                result=evaluation,
                risk=(evaluation.get("decision") or {}).get("risk") or "unknown",
                approval=approval,
            ),
        )
        return evaluation

    def assert_approval_allows(self, run_id: str | None, approval_id: str | None) -> None:
        if not run_id or not approval_id:
            return
        approval = self._approval_by_id(run_id, approval_id)
        if not approval:
            raise PermissionError(f"Approval not found: {approval_id}")
        if approval.get("status") != "approved":
            raise PermissionError(f"Approval {approval_id} is {approval.get('status')}.")

    def artifact(self, run_id: str, artifact_ref: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        normalized = str(artifact_ref or "").strip()
        if normalized == "run_artifacts":
            return self.run_service.get_run_artifacts(run_id)
        payload = self.store.get("reports", normalized)
        if payload is None:
            raise KeyError(f"Artifact not found: {artifact_ref}")
        return {"artifact_ref": normalized, "payload": payload}

    def timeline(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        items: list[dict[str, Any]] = [
            self._timeline_item("prompt", "completed", "Prompt received", {"prompt": run.prompt}, created_at=run.created_at.isoformat()),
        ]
        for approval in self.approvals(run_id)["items"]:
            items.append(
                self._timeline_item(
                    "approval",
                    str(approval.get("status") or "pending"),
                    str(approval.get("summary") or approval.get("kind") or "Approval"),
                    approval,
                    created_at=str(approval.get("decided_at") or approval.get("created_at") or datetime.now(timezone.utc).isoformat()),
                )
            )
        for event in self._stored_tool_events(run_id):
            items.append(
                self._timeline_item(
                    "policy" if event.get("tool") == "policy.evaluate" else "tool",
                    str(((event.get("result") if isinstance(event.get("result"), dict) else {}) or {}).get("status") or "recorded"),
                    str(event.get("tool") or "Tool event"),
                    event,
                    created_at=str(event.get("created_at") or datetime.now(timezone.utc).isoformat()),
                )
            )
        for event in artifacts.get("agent_activity_events") or run.agent_activity_events or []:
            if isinstance(event, dict):
                items.append(self._timeline_from_activity(event))
        for check in artifacts.get("check_results") or []:
            if isinstance(check, dict):
                items.append(self._timeline_item("checks", str(check.get("status") or "completed"), str(check.get("name") or "Check"), check))
        if artifacts.get("diff"):
            items.append(
                self._timeline_item(
                    "diff",
                    "completed",
                    "Draft diff recorded",
                    {"changed_files": run.touched_files, "artifact_ref": "run_artifacts"},
                )
            )
        if artifacts.get("browser_flow_proof") or artifacts.get("browser_proof_steps"):
            items.append(self._timeline_item("browser", "completed", "Browser proof recorded", {"artifact_ref": run.browser_proof_ref}))
        if run.apply_status == "applied":
            items.append(self._timeline_item("apply", "completed", "Draft applied to workspace", {"revision_id": run.result_revision_id}))
        if run.status in {"failed", "blocked"}:
            items.append(self._timeline_item("failure", run.status, run.failure_reason or "Run did not complete", {"failure_class": run.failure_class}))
        if run.status == "completed":
            items.append(self._timeline_item("complete", "completed", "Run completed", {"summary": run.summary}))
        return {
            "run_id": run_id,
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "items": [
                {**item, "sequence": index + 1}
                for index, item in enumerate(sorted(items, key=lambda item: str(item.get("created_at") or "")))
            ],
        }

    def observability(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        timeline_items = self.timeline(run_id).get("items") or []
        return {
            "trace_id": f"trace_{run.run_id}",
            "run_id": run.run_id,
            "thread_id": None,
            "turn_id": None,
            "tool_call_count": len((self.tool_events(run_id)).get("events") or []),
            "span_count": len(timeline_items),
            "spans": [
                {
                    "span_id": f"span_{index + 1}",
                    "name": item.get("kind"),
                    "status": item.get("status"),
                    "started_at": item.get("created_at"),
                    "attributes": {"title": item.get("title"), "sequence": item.get("sequence")},
                }
                for index, item in enumerate(timeline_items[:200])
            ],
            "latency_breakdown": artifacts.get("latency_breakdown") or {},
            "token_usage": run.token_usage,
            "model_profile": run.model_profile,
            "llm_provider": run.llm_provider,
            "llm_model": run.llm_model,
            "failure": {
                "class": run.failure_class,
                "signature": run.failure_signature,
                "reason": run.failure_reason,
            },
        }

    def git_status(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)

        def git(args: list[str]) -> tuple[int, str, str]:
            try:
                result = subprocess.run(["git", *args], cwd=source_dir, text=True, capture_output=True, timeout=8)
                return result.returncode, result.stdout.strip(), result.stderr.strip()
            except Exception as exc:
                return 1, "", str(exc)

        branch_code, branch, branch_err = git(["rev-parse", "--abbrev-ref", "HEAD"])
        status_code, status, status_err = git(["status", "--short"])
        log_code, log, log_err = git(["log", "--oneline", "-5"])
        return {
            "workspace_id": workspace_id,
            "source_dir": str(source_dir),
            "branch": branch if branch_code == 0 else None,
            "status": status.splitlines() if status_code == 0 and status else [],
            "recent_commits": log.splitlines() if log_code == 0 and log else [],
            "worktree_recommended_branch_prefix": "grounded/run-",
            "errors": [item for item in [branch_err, status_err, log_err] if item],
        }

    def workers(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        merge = ((artifacts.get("worker_results") or {}).get("merge") or {}) if isinstance(artifacts.get("worker_results"), dict) else {}
        mailbox = self.store.get("reports", run.worker_mailbox_ref) if run.worker_mailbox_ref else None
        mailbox_workers = ((mailbox or {}).get("mailbox") or {}).get("workers") if isinstance(mailbox, dict) else []
        if not mailbox_workers:
            synthesized_mailbox = AgentWorkerManager.mailbox_for_plan(
                generation_mode=run.generation_mode,
                implementation_plan=run.implementation_plan or {},
            )
            mailbox = {"mailbox": synthesized_mailbox}
            mailbox_workers = synthesized_mailbox.get("workers") or []
        artifact_run_id = self._worker_artifact_run_id(run)
        merge_decision_ref = f"worker_manager_merge_decision:{run.workspace_id}:{artifact_run_id}"
        merge_decision = self.store.get("reports", merge_decision_ref) or {}
        real_task_items = (
            self.background_task_service.real_tasks_for_run(run.run_id)
            if self.background_task_service is not None
            else []
        )
        real_tasks = {
            canonical_worker_id(str((item.get("input") or {}).get("worker_id") or item.get("owner") or "")): item
            for item in real_task_items
            if isinstance(item, dict) and item.get("type") == "worker_branch"
        }
        worker_ids = [role.worker_id for role in PRODUCT_WORKERS if role.worker_id != "repair_worker"]
        if any((item.get("repair_worker") if isinstance(item, dict) else None) for item in [merge_decision]):
            worker_ids.append("repair_worker")
        lanes = []
        for worker_id in worker_ids:
            canonical = canonical_worker_id(worker_id)
            aliases = [legacy_worker_id(canonical), *[alias for role in PRODUCT_WORKERS if role.worker_id == canonical for alias in role.aliases]]
            summaries = [
                item
                for item in run.worker_summaries
                if isinstance(item, dict) and canonical_worker_id(str(item.get("worker") or item.get("worker_id") or "")) == canonical
            ]
            merge_reports = [
                item
                for item in (merge.get("merge_reports") or [])
                if isinstance(item, dict) and canonical_worker_id(str(item.get("worker_id") or "")) == canonical
            ]
            refs = worker_refs(run.workspace_id, artifact_run_id, canonical)
            output = self.store.get("reports", refs["output_ref"]) or {}
            context = self.store.get("reports", refs["context_ref"]) or {}
            memory = self.store.get("reports", refs["memory_snapshot_ref"]) or {}
            decision = next(
                (
                    item
                    for item in (merge_decision.get("decisions") or [])
                    if isinstance(item, dict) and canonical_worker_id(str(item.get("worker_id") or "")) == canonical
                ),
                {},
            )
            task = real_tasks.get(canonical) or real_tasks.get(legacy_worker_id(canonical)) or {}
            status = self._worker_status(canonical, run, summaries, merge_reports, mailbox_workers)
            if isinstance(output, dict) and output.get("status"):
                status = str(output.get("status"))
            if isinstance(decision, dict) and decision.get("decision") in {"accepted", "rejected", "needs_repair"}:
                status = {"accepted": "merged", "rejected": "rejected", "needs_repair": "blocked"}[str(decision.get("decision"))]
            lanes.append(
                {
                    "worker_id": canonical,
                    "worker_type": canonical,
                    "alias_ids": sorted({alias for alias in aliases if alias and alias != canonical}),
                    "status": status,
                    "badge": str(output.get("badge") or status) if isinstance(output, dict) else status,
                    "owner_scope": self._worker_scope(canonical),
                    "ownership": ownership_for_worker(canonical),
                    "changed_files": list(output.get("changed_files") or [path for path in run.touched_files if self._path_owned_by_worker(canonical, path)]),
                    "summaries": summaries,
                    "merge_reports": merge_reports,
                    "disabled_reason": self._worker_disabled_reason(canonical, mailbox_workers),
                    "context_ref": refs["context_ref"] if context else None,
                    "memory_snapshot_ref": refs["memory_snapshot_ref"] if memory else None,
                    "output_ref": refs["output_ref"] if output else None,
                    "task_id": task.get("task_id") if isinstance(task, dict) else None,
                    "proof_refs": list(output.get("proof_refs") or []) if isinstance(output, dict) else [],
                    "merge_decision_ref": merge_decision_ref if merge_decision else None,
                    "merge_decision": decision or None,
                }
            )
        return {
            "schema": "grounded.product_workers.v1",
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "workers": lanes,
            "worker_branch_refs": run.worker_branch_refs,
            "merge_decision_ref": merge_decision_ref if merge_decision else None,
            "mailbox": (mailbox or {}).get("mailbox") if isinstance(mailbox, dict) else {},
        }

    def tasks(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        background_items = self._background_tasks_for_run(run)
        scratchpad = self.store.get("reports", run.scratchpad_ref) if run.scratchpad_ref else None
        raw_todos = []
        if isinstance(scratchpad, dict):
            raw_todos = scratchpad.get("todo_plan") or scratchpad.get("agent_todos") or []
        items: list[dict[str, Any]] = []
        for index, item in enumerate(raw_todos if isinstance(raw_todos, list) else [], start=1):
            if not isinstance(item, dict):
                continue
            phase = str(item.get("phase") or item.get("id") or f"task_{index}")
            status = self._task_status(str(item.get("status") or "planned"))
            items.append(
                {
                    "task_id": str(item.get("task_id") or item.get("id") or f"{run_id}:{index}"),
                    "title": str(item.get("task") or item.get("content") or phase).strip(),
                    "phase": phase,
                    "status": status,
                    "owner": str(item.get("owner") or self._owner_for_phase(phase)),
                    "files": list(item.get("files") or []),
                    "proof": item.get("proof") or {},
                    "blocker": item.get("blocker") or None,
                    "artifact_refs": {"scratchpad": run.scratchpad_ref},
                    "updated_at": item.get("updated_at"),
                }
            )
        if background_items:
            items = [*background_items, *items]
        if not items:
            items = self._tasks_from_activity(run)
        return {"schema": "grounded.run_tasks.v1", "run_id": run_id, "workspace_id": run.workspace_id, "status": run.status, "items": items}

    def create_background_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        task = self.background_task_service.create_task(
            workspace_id=str(payload.get("workspace_id") or ""),
            run_id=str(payload.get("run_id") or "").strip() or None,
            parent_task_id=str(payload.get("parent_task_id") or "").strip() or None,
            task_type=str(payload.get("type") or payload.get("task_type") or ""),
            title=str(payload.get("title") or "").strip() or None,
            input_payload=payload.get("input") if isinstance(payload.get("input"), dict) else {},
            owner=str(payload.get("owner") or "agent"),
            max_attempts=int(payload.get("max_attempts") or 1),
            auto_start=bool(payload.get("auto_start", True)),
        )
        return task.model_dump(mode="json")

    def list_background_tasks(self, *, workspace_id: str | None = None, run_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        if self.background_task_service is None:
            return {"schema": "grounded.background_tasks.v1", "status": "unavailable", "items": []}
        return self.background_task_service.list_tasks(workspace_id=workspace_id, run_id=run_id, status=status)

    def get_background_task(self, task_id: str) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.get_task(task_id).model_dump(mode="json")

    def update_background_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.update_task(task_id, payload).model_dump(mode="json")

    def stop_background_task(self, task_id: str) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.stop_task(task_id).model_dump(mode="json")

    def retry_background_task(self, task_id: str) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.retry_task(task_id).model_dump(mode="json")

    def requeue_background_task(self, task_id: str) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.requeue_task(task_id).model_dump(mode="json")

    def background_task_output(self, task_id: str, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        if self.background_task_service is None:
            raise KeyError("Background task service is unavailable.")
        return self.background_task_service.output(task_id, cursor=cursor, limit=limit)

    def worker_roles(self) -> dict[str, Any]:
        return WorkerRoleCatalog.roles()

    def worker_artifacts(self, run_id: str, worker_id: str) -> dict[str, Any]:
        workers = self.workers(run_id)["workers"]
        canonical = canonical_worker_id(worker_id)
        lane = next((item for item in workers if canonical_worker_id(item["worker_id"]) == canonical), None)
        if lane is None:
            raise KeyError(f"Worker not found: {worker_id}")
        return {"run_id": run_id, "worker_id": worker_id, "artifacts": lane}

    def worker_context(self, run_id: str, worker_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = worker_refs(run.workspace_id, self._worker_artifact_run_id(run), worker_id)["context_ref"]
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            raise KeyError(f"Worker context not found: {worker_id}")
        return payload

    def worker_memory(self, run_id: str, worker_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = worker_refs(run.workspace_id, self._worker_artifact_run_id(run), worker_id)["memory_snapshot_ref"]
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            raise KeyError(f"Worker memory snapshot not found: {worker_id}")
        return payload

    def worker_output(self, run_id: str, worker_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = worker_refs(run.workspace_id, self._worker_artifact_run_id(run), worker_id)["output_ref"]
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            raise KeyError(f"Worker output not found: {worker_id}")
        return payload

    def worker_merge_decision(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = f"worker_manager_merge_decision:{run.workspace_id}:{self._worker_artifact_run_id(run)}"
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            return {"schema": "grounded.worker_manager_merge_decision.v1", "run_id": run_id, "workspace_id": run.workspace_id, "status": "empty", "decisions": []}
        return payload

    def worker_diff(self, run_id: str, worker_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        diff = self._run_artifacts_or_empty(run_id).get("diff") or ""
        canonical = canonical_worker_id(worker_id)
        owned_files = [path for path in run.touched_files if self._path_owned_by_worker(canonical, path)]
        return {"run_id": run_id, "worker_id": canonical, "owned_files": owned_files, "diff": self._filter_diff(diff, owned_files)}

    def review(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        findings: list[dict[str, Any]] = []
        for issue in run.checks_summary.issues:
            findings.append(
                self._review_finding(
                    code=str(issue.get("code") or issue.get("kind") or "check_summary_issue"),
                    message=str(issue.get("message") or issue.get("details") or "Run check summary contains an unresolved issue."),
                    severity=str(issue.get("severity") or "medium"),
                    category="check",
                    source="checks_summary",
                    file_path=issue.get("file_path") or issue.get("file") or issue.get("path") or issue.get("location"),
                    line=issue.get("line"),
                    evidence=issue,
                    blocker=bool(issue.get("blocking") or issue.get("blocker") or run.checks_summary.gate_status in {"failed", "blocked"}),
                )
            )
        diff_text = str(artifacts.get("diff") or "")
        changed_files = run.touched_files or self._paths_from_diff(diff_text)
        check_results = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        check_by_name = {str(item.get("name") or ""): item for item in check_results}
        acceptance_required = bool((run.acceptance_contract or {}).get("required")) or run.intent == "create"
        browser_check = check_by_name.get("browser_flow_smoke")
        api_check = check_by_name.get("api_workflow_smoke")
        browser_passed = browser_check and browser_check.get("status") == "passed"
        api_passed = api_check and api_check.get("status") == "passed"
        browser_proof_present = bool(artifacts.get("browser_proof_steps") or run.browser_flow_proof or run.browser_proof_ref or browser_passed)
        if diff_text and not browser_proof_present:
            findings.append(
                self._review_finding(
                    code="browser_proof_gap",
                    message="Changed product draft has no recorded browser workflow proof.",
                    severity="high" if acceptance_required else "medium",
                    category="browser_proof",
                    source="review_gate",
                    blocker=acceptance_required,
                    evidence={"browser_proof_ref": run.browser_proof_ref, "browser_flow_smoke": browser_check},
                )
            )
        if acceptance_required and not api_passed:
            findings.append(
                self._review_finding(
                    code="product_contract_missing_api_smoke",
                    message="Required product contract is missing a passing API workflow smoke proof.",
                    severity="high",
                    category="product_contract",
                    source="acceptance_contract",
                    blocker=True,
                    evidence={"api_workflow_smoke": api_check, "acceptance_contract_required": True},
                )
            )
        if acceptance_required and not browser_passed:
            findings.append(
                self._review_finding(
                    code="product_contract_missing_browser_smoke",
                    message="Required product contract is missing a passing browser workflow proof.",
                    severity="high",
                    category="product_contract",
                    source="acceptance_contract",
                    blocker=True,
                    evidence={"browser_flow_smoke": browser_check, "acceptance_contract_required": True},
                )
            )
        risky_paths = [
            path
            for path in changed_files
            if path.startswith(("miniapp/app/generated/", "docker/", ".github/", "runtime/"))
        ]
        if risky_paths:
            findings.append(
                self._review_finding(
                    code="risky_generated_or_runtime_change",
                    message=f"Review risky generated/runtime paths before apply: {', '.join(risky_paths[:8])}.",
                    severity="high",
                    category="product_contract",
                    source="diff",
                    file_path=risky_paths[0],
                    evidence={"paths": risky_paths},
                    blocker=True,
                )
            )
        if len(changed_files) >= 12 and not check_results:
            findings.append(
                self._review_finding(
                    code="large_untested_change",
                    message="Large draft has no recorded check results.",
                    severity="high",
                    category="missing_tests",
                    source="diff",
                    blocker=acceptance_required,
                    evidence={"changed_file_count": len(changed_files)},
                )
            )
        findings.extend(self._review_findings_from_check_results(check_results, acceptance_required=acceptance_required))
        findings.extend(self._review_test_findings(run=run, changed_files=changed_files, check_by_name=check_by_name, acceptance_required=acceptance_required))
        findings.extend(self._review_contract_findings(run=run, check_by_name=check_by_name, acceptance_required=acceptance_required))
        findings = self._dedupe_review_findings(findings)
        findings.sort(key=self._review_finding_sort_key)
        blocker_count = sum(1 for item in findings if item.get("is_blocker_for_product_acceptance"))
        severity_counts: dict[str, int] = {}
        for item in findings:
            severity = str(item.get("severity") or "medium")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        payload = {
            "schema": "grounded.review_report.v2",
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": "failed" if findings else "passed",
            "summary": {
                "finding_count": len(findings),
                "blocker_count": blocker_count,
                "severity_counts": severity_counts,
                "missing_tests": sum(1 for item in findings if item.get("category") == "missing_tests"),
                "stale_test_risks": sum(1 for item in findings if item.get("category") == "stale_test_risk"),
                "browser_proof_gaps": sum(1 for item in findings if item.get("category") == "browser_proof"),
                "contract_mismatches": sum(1 for item in findings if item.get("category") == "product_contract"),
            },
            "findings": findings,
            "evidence": {
                "diff_available": bool(artifacts.get("diff")),
                "changed_files": changed_files,
                "checks": check_results,
                "browser_proof_ref": run.browser_proof_ref,
                "verifier_review_ref": run.verifier_review_ref,
                "acceptance_contract_required": acceptance_required,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"review:{run_id}", payload)
        return payload

    @staticmethod
    def _review_finding(
        *,
        code: str,
        message: str,
        severity: str,
        category: str,
        source: str,
        blocker: bool,
        file_path: object = None,
        line: object = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_path = str(file_path or "").strip()
        normalized_line: int | None = None
        try:
            normalized_line = int(line) if line is not None and str(line).strip() else None
        except (TypeError, ValueError):
            normalized_line = None
        payload: dict[str, Any] = {
            "code": code,
            "severity": severity if severity in {"critical", "high", "medium", "low", "info"} else "medium",
            "category": category,
            "source": source,
            "message": message,
            "is_blocker_for_product_acceptance": blocker,
            "evidence": evidence or {},
        }
        if normalized_path:
            payload["file_path"] = normalized_path
            payload["path"] = normalized_path
            payload["location"] = {"path": normalized_path, "line": normalized_line or 1}
        if normalized_line:
            payload["line"] = normalized_line
        return payload

    def _review_findings_from_check_results(self, check_results: list[dict[str, Any]], *, acceptance_required: bool) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for result in check_results:
            status = str(result.get("status") or "")
            if status not in {"failed", "blocked"}:
                continue
            diagnostics = result.get("diagnostics") if isinstance(result.get("diagnostics"), dict) else {}
            location = self._review_location_from_diagnostics(diagnostics)
            check_name = str(result.get("name") or "check")
            category = (
                "browser_proof"
                if check_name == "browser_flow_smoke"
                else "product_contract"
                if check_name in {"api_workflow_smoke", "platform_invariants", "frontend_interaction_static_smoke"}
                else "missing_tests"
                if "test" in check_name
                else "check"
            )
            findings.append(
                self._review_finding(
                    code=f"check_failed.{check_name}",
                    message=str(result.get("details") or f"{check_name} did not pass."),
                    severity="high" if category in {"browser_proof", "product_contract"} or acceptance_required else "medium",
                    category=category,
                    source="check_results",
                    file_path=location.get("path"),
                    line=location.get("line"),
                    blocker=acceptance_required and category in {"browser_proof", "product_contract", "missing_tests"},
                    evidence={"check": check_name, "status": status, "logs": list(result.get("logs") or [])[-6:], "diagnostics": diagnostics},
                )
            )
        return findings

    def _review_test_findings(
        self,
        *,
        run: RunRecord,
        changed_files: list[str],
        check_by_name: dict[str, dict[str, Any]],
        acceptance_required: bool,
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        app_changed = [path for path in changed_files if path.startswith("miniapp/app/")]
        tests_changed = [path for path in changed_files if path.startswith("miniapp/tests/")]
        generated_test_checks = [name for name in check_by_name if name in {"generated_app_python_tests", "generated_app_js_tests"}]
        if acceptance_required and app_changed and not generated_test_checks and not run.generated_tests:
            findings.append(
                self._review_finding(
                    code="missing_generated_acceptance_tests",
                    message="Product-changing run changed app files but has no generated acceptance test evidence.",
                    severity="high",
                    category="missing_tests",
                    source="generated_tests",
                    blocker=True,
                    evidence={"app_changed": app_changed[:12], "tests_changed": tests_changed[:12]},
                )
            )
        if app_changed and not tests_changed and run.intent in {"create", "edit"}:
            findings.append(
                self._review_finding(
                    code="stale_test_risk",
                    message="App files changed without generated test files changing; tests may be stale against the current workflow.",
                    severity="medium",
                    category="stale_test_risk",
                    source="diff",
                    blocker=False,
                    evidence={"app_changed": app_changed[:12], "tests_changed": tests_changed},
                )
            )
        for check_name in ("generated_app_python_tests", "generated_app_js_tests"):
            check = check_by_name.get(check_name)
            if check and check.get("status") in {"failed", "blocked"}:
                diagnostics = check.get("diagnostics") if isinstance(check.get("diagnostics"), dict) else {}
                location = self._review_location_from_diagnostics(diagnostics)
                findings.append(
                    self._review_finding(
                        code=f"stale_or_failing_test.{check_name}",
                        message=str(check.get("details") or "Generated acceptance tests are failing or stale."),
                        severity="high" if acceptance_required else "medium",
                        category="stale_test_risk",
                        source="generated_tests",
                        file_path=location.get("path"),
                        line=location.get("line"),
                        blocker=acceptance_required,
                        evidence={"check": check_name, "diagnostics": diagnostics, "logs": list(check.get("logs") or [])[-6:]},
                    )
                )
        return findings

    def _review_contract_findings(self, *, run: RunRecord, check_by_name: dict[str, dict[str, Any]], acceptance_required: bool) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        contract = run.acceptance_contract if isinstance(run.acceptance_contract, dict) else {}
        if acceptance_required and not contract:
            findings.append(
                self._review_finding(
                    code="missing_product_acceptance_contract",
                    message="Product-changing run has no stored acceptance contract.",
                    severity="high",
                    category="product_contract",
                    source="acceptance_contract",
                    blocker=True,
                )
            )
        required_roles = set(str(role) for role in (run.target_role_scope or ["client", "specialist", "manager"]))
        browser = check_by_name.get("browser_flow_smoke") or {}
        diagnostics = browser.get("diagnostics") if isinstance(browser.get("diagnostics"), dict) else {}
        checked_roles = set(str(role) for role in diagnostics.get("roles_checked") or [])
        if acceptance_required and checked_roles and not required_roles.issubset(checked_roles):
            findings.append(
                self._review_finding(
                    code="product_contract_role_proof_mismatch",
                    message="Browser proof did not cover all required role surfaces from the product contract.",
                    severity="high",
                    category="product_contract",
                    source="browser_flow_smoke",
                    blocker=True,
                    evidence={"required_roles": sorted(required_roles), "checked_roles": sorted(checked_roles)},
                )
            )
        return findings

    @staticmethod
    def _review_location_from_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
        for key in ("file_path", "file", "path", "location"):
            value = diagnostics.get(key)
            if isinstance(value, str) and value.strip():
                return {"path": value.strip(), "line": diagnostics.get("line")}
            if isinstance(value, dict):
                path = value.get("path") or value.get("file_path") or value.get("file")
                if path:
                    return {"path": str(path), "line": value.get("line") or diagnostics.get("line")}
        for nested_key in ("items", "issues", "diagnostics"):
            nested = diagnostics.get(nested_key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        location = WorkbenchService._review_location_from_diagnostics(item)
                        if location:
                            return location
            elif isinstance(nested, dict):
                location = WorkbenchService._review_location_from_diagnostics(nested)
                if location:
                    return location
        return {}

    @staticmethod
    def _dedupe_review_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for finding in findings:
            key = (str(finding.get("code") or ""), str(finding.get("file_path") or ""), str(finding.get("line") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(finding)
        return deduped

    @staticmethod
    def _review_finding_sort_key(finding: dict[str, Any]) -> tuple[int, int, str]:
        severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        blocker_rank = 0 if finding.get("is_blocker_for_product_acceptance") else 1
        return (blocker_rank, severity_rank.get(str(finding.get("severity") or "medium"), 2), str(finding.get("code") or ""))

    def start_review_fix(self, run_id: str) -> RunRecord:
        run = self.run_service.get_run(run_id)
        review = self.review(run_id)
        prompt = "Fix review findings:\n" + "\n".join(
            f"- {item.get('code')}: {item.get('message')}" for item in review.get("findings", []) if isinstance(item, dict)
        )
        if not review.get("findings"):
            prompt = "Run a focused verification pass and fix any concrete issue found."
        return self.run_service.create_run(
            run.workspace_id,
            CreateRunRequest(
                prompt=prompt,
                mode="fix",
                intent="edit",
                apply_strategy="staged_auto_apply",
                target_role_scope=list(run.target_role_scope or []),
                model_profile=run.model_profile,
                generation_mode=run.generation_mode,
            ),
        )

    def browser_proof(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        payload = self._normalize_browser_proof_payload(run, artifacts)
        self.store.upsert("reports", f"browser_proof:{run_id}", payload)
        return payload

    def acceptance_scenarios(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        payload = AcceptanceScenarioGenerator.build(run, self._run_artifacts_or_empty(run_id))
        self.store.upsert("reports", f"acceptance_scenarios:{run_id}", payload)
        return payload

    def visual_qa(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        source_dir = (
            self.workspace_service.draft_source_dir(run.workspace_id, run_id)
            if self.workspace_service.draft_exists(run.workspace_id, run_id)
            else self.workspace_service.source_dir(run.workspace_id)
        )
        payload = VisualQAGenerator.build(run=run, artifacts=self._run_artifacts_or_empty(run_id), source_dir=source_dir)
        self.store.upsert("reports", f"visual_qa:{run_id}", payload)
        return payload

    def trace_reducer(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        payload = TraceReducer.build(
            run=run,
            timeline=self.timeline(run_id).get("items") or [],
            tool_events=self.tool_events(run_id).get("events") or [],
            artifacts=self._run_artifacts_or_empty(run_id),
        )
        self.store.upsert("reports", f"trace_reducer:{run_id}", payload)
        return payload

    def trace_bundle(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = run.trace_bundle_ref or f"trace_bundle:{run.workspace_id}:{run_id}"
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            return {
                "schema": "grounded.trace_bundle.v1",
                "run_id": run_id,
                "workspace_id": run.workspace_id,
                "status": "missing",
                "event_count": 0,
                "state": {},
            }
        state = payload.get("state")
        if not isinstance(state, dict):
            state = self.trace_bundle_state(run_id)
            payload = {**payload, "state": state}
            self.store.upsert("reports", ref, payload)
        return payload

    def trace_bundle_state(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = run.trace_bundle_ref or f"trace_bundle:{run.workspace_id}:{run_id}"
        payload = self.store.get("reports", ref) or {}
        state = payload.get("state") if isinstance(payload, dict) else None
        if isinstance(state, dict) and state:
            return self._augment_trace_state_with_protocol(run_id, state)
        bundle_dir_value = str((payload or {}).get("bundle_dir") or "").strip()
        bundle_dir = Path(bundle_dir_value) if bundle_dir_value else None
        if bundle_dir is not None and bundle_dir.exists():
            state = TraceBundleReducer.reduce_bundle(bundle_dir)
        else:
            state = {
                "schema": "grounded.trace_bundle_state.v1",
                "run_id": run_id,
                "workspace_id": run.workspace_id,
                "event_count": 0,
                "blockers": [],
                "changed_files": [],
                "next_action": {"action": "none", "reason": "Trace bundle is missing."},
            }
        self.store.upsert("reports", f"trace_reducer:{run.workspace_id}:{run_id}", state)
        return self._augment_trace_state_with_protocol(run_id, state)

    def _augment_trace_state_with_protocol(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if self.run_protocol_service is None:
            return state
        protocol = self.run_protocol_service.protocol_events(run_id).get("items") or []
        if not protocol:
            return state
        turns = list(state.get("turns") or [])
        tool_calls = list(state.get("tool_calls") or [])
        proof_edges = list(state.get("proof_edges") or [])
        blockers = list(state.get("blockers") or [])
        compact_boundaries: list[dict[str, Any]] = []
        terminal = None
        for event in protocol:
            event_type = str(event.get("type") or "")
            compact = {
                "seq": event.get("sequence"),
                "event_type": f"protocol.{event_type}",
                "status": event.get("status"),
                "summary": event.get("message"),
                "turn_id": event.get("turn_id"),
                "bookmark_id": event.get("bookmark_id"),
            }
            if event_type in {"turn_started", "model_delta", "turn_completed"}:
                turns.append(compact)
            if event_type in {"tool_requested", "tool_completed"}:
                tool_calls.append(compact)
            if event_type in {"check_started", "run_completed"}:
                proof_edges.append(compact)
            if event_type == "compact_boundary":
                compact_boundaries.append({**compact, "refs": event.get("refs") or {}})
            if str(event.get("status") or "") in {"failed", "blocked"}:
                blockers.append(compact)
            if event_type == "run_completed":
                terminal = compact
        bookmarks = self.run_protocol_service.bookmarks(run_id).get("items") or []
        next_target = bookmarks[0] if bookmarks else None
        next_action = state.get("next_action") if isinstance(state.get("next_action"), dict) else {}
        if next_target and next_action.get("action") == "none":
            next_action = {
                "action": "resume_from_bookmark",
                "reason": "Latest model response bookmark is available.",
                "bookmark_id": next_target.get("bookmark_id"),
            }
        return {
            **state,
            "turns": turns[-80:],
            "tool_calls": tool_calls[-160:],
            "proof_edges": proof_edges[-80:],
            "blockers": blockers[-80:],
            "protocol_events": protocol[-300:],
            "compact_boundaries": compact_boundaries[-80:],
            "model_response_bookmarks": bookmarks[:80],
            "final_terminal_event": terminal,
            "next_action": next_action or {"action": "none", "reason": "No blocking trace event."},
        }

    def gate(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        diff_text = str(artifacts.get("diff") or "")
        check_results = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        checks_by_name = {str(item.get("name") or ""): item for item in check_results}
        mode_value = str(getattr(run.generation_mode, "value", run.generation_mode) or "").lower()
        acceptance_required = bool((run.acceptance_contract or {}).get("required")) or run.mode == "generate" or mode_value in {"quality", "balanced"}
        browser = checks_by_name.get("browser_flow_smoke") or {}
        api = checks_by_name.get("api_workflow_smoke") or {}
        mobile = run.mobile_layout_report or (browser.get("diagnostics") or {}).get("mobile_layout") if isinstance(browser, dict) else {}
        issues: list[dict[str, Any]] = []

        def add_issue(kind: str, check: str, details: str, *, blocking: bool = True, evidence: dict[str, Any] | None = None) -> None:
            issues.append({"kind": kind, "check": check, "details": details, "blocking": blocking, "evidence": evidence or {}})

        if run.mode in {"generate", "fix"} and not diff_text.strip() and not run.touched_files:
            add_issue("meaningful_diff", "meaningful_diff", "Run has no meaningful draft/source diff.")
        for item in check_results:
            if str(item.get("status")) in {"failed", "blocked"}:
                add_issue("check_failure", str(item.get("name") or "check"), str(item.get("details") or "Check failed."), evidence=item)
        if acceptance_required:
            if api.get("status") != "passed":
                add_issue("required_product_proof", "api_workflow_smoke", "API workflow proof must pass before completion.", evidence=api)
            if browser.get("status") != "passed":
                add_issue("required_product_proof", "browser_flow_smoke", "Browser workflow proof must pass before completion.", evidence=browser)
        if isinstance(mobile, dict) and mobile.get("status") == "failed":
            add_issue("mobile_layout", "browser_flow_smoke", "Mobile layout report contains blocking issues.", evidence=mobile)
        for signature in run.repair_issue_signatures:
            if isinstance(signature, dict) and not signature.get("resolved"):
                add_issue("unresolved_repair_signature", str(signature.get("check") or "repair"), str(signature.get("signature") or "Unresolved repair signature."), evidence=signature)
        if run.outcome_kind == "blocked_preview_infra":
            add_issue("preview_infra", "browser_flow_smoke", run.failure_reason or "Browser/preview infrastructure blocked product proof.", evidence=artifacts.get("preview_infra_diagnostics") or {})
        apply_ok = run.apply_status == "applied"
        if run.status in {"completed", "blocked", "failed"} and not apply_ok:
            add_issue("apply_gate", "apply_status", "Run must be applied after green checks.", evidence={"apply_status": run.apply_status, "status": run.status})

        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        checkpoint_packets = list((checkpoint or {}).get("repair_packets") or []) if isinstance(checkpoint, dict) else []
        next_forced_action = dict((checkpoint or {}).get("next_forced_action") or {}) if isinstance(checkpoint, dict) else {}
        blocking = any(item.get("blocking", True) for item in issues)
        repair_packets = RepairCatalog.classify_many(issues)
        repair_packets = self._llm_refine_unknown_repair_packets(run, artifacts, issues, repair_packets)
        include_checkpoint_packets = bool(blocking or run.status not in {"completed", "awaiting_approval"})
        if checkpoint_packets and include_checkpoint_packets:
            repair_packets = [*checkpoint_packets, *repair_packets]
        repair_history = [
            {**item, "resolved": True}
            for item in checkpoint_packets
            if isinstance(item, dict)
        ] if checkpoint_packets and not include_checkpoint_packets else []
        status = "passed" if not blocking and apply_ok else "blocked" if blocking else "pending"
        payload = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "blocking": blocking,
            "issues": issues,
            "repair_packets": repair_packets,
            "repair_history": repair_history,
            "next_forced_action": next_forced_action,
            "blocking_repair_packet": repair_packets[0] if blocking and repair_packets else {},
            "requirements": {
                "acceptance_required": acceptance_required,
                "meaningful_diff": True,
                "api_workflow_smoke": acceptance_required,
                "browser_flow_smoke": acceptance_required,
                "mobile_layout_non_blocking": True,
                "apply_status": "applied",
            },
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}",
                "browser_proof": run.browser_proof_ref,
                "repair_recipes": run.repair_recipes_ref,
                "final_report": f"final_report:{run_id}",
                "resume_checkpoint": run.resume_checkpoint_ref,
                "diagnostics_delta": (checkpoint or {}).get("diagnostics_delta_ref") if isinstance(checkpoint, dict) else None,
            },
        }
        browser_proof = self._normalize_browser_proof_payload(run, artifacts)
        state = RunStateMachine.evaluate(run=run, gate=payload, artifacts=artifacts, browser_proof=browser_proof)
        if state.get("invariant_issues"):
            payload["issues"] = [*payload["issues"], *state["invariant_issues"]]
            payload["blocking"] = True
            payload["status"] = "blocked"
            payload["blocking_repair_packet"] = (
                payload["repair_packets"][0]
                if payload.get("repair_packets")
                else {
                    "signature": "reliability_gate.state_invariant",
                    "issue_code": "state_invariant",
                    "code": "state_invariant",
                    "severity": "high",
                    "target_files": [],
                    "required_next_tool": "run_checks",
                    "suggested_tool_after_read": "run_checks",
                    "retryable": False,
                    "deterministic": True,
                    "failure_class": "reliability_gate.state_invariant",
                    "failure_signature": "reliability_gate.state_invariant",
                    "instruction": "Inspect the run state, gate issues, and artifacts before applying or marking completion.",
                    "evidence": {"run_state": state},
                }
            )
            state = RunStateMachine.evaluate(run=run, gate=payload, artifacts=artifacts, browser_proof=browser_proof)
        payload["run_state"] = state
        payload["artifact_refs"]["run_state"] = f"run_state:{run_id}"
        self.store.upsert("reports", f"gate:{run_id}", payload)
        self.store.upsert("reports", f"run_state:{run_id}", state)
        self.run_service.reconcile_run_with_gate(run_id, payload)
        return payload

    def repair_signatures(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        gate = self.gate(run_id)
        explicit = [item for item in run.repair_issue_signatures if isinstance(item, dict)]
        packets = RepairCatalog.classify_many([*explicit, *gate.get("issues", [])])
        packets = self._llm_refine_unknown_repair_packets(run, self._run_artifacts_or_empty(run_id), [*explicit, *gate.get("issues", [])], packets)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        checkpoint_packets = list((checkpoint or {}).get("repair_packets") or []) if isinstance(checkpoint, dict) else []
        include_checkpoint_packets = bool(gate.get("blocking") or run.status not in {"completed", "awaiting_approval"})
        if checkpoint_packets and include_checkpoint_packets:
            packets = [*checkpoint_packets, *packets]
        payload = {
            "run_id": run_id,
            "status": "available" if packets else "empty",
            "blocking": bool(gate.get("blocking")),
            "items": packets,
            "history": [
                {**item, "resolved": True}
                for item in checkpoint_packets
                if isinstance(item, dict)
            ] if checkpoint_packets and not include_checkpoint_packets else [],
            "next_forced_action": dict((checkpoint or {}).get("next_forced_action") or {}) if isinstance(checkpoint, dict) else {},
            "catalog": RepairCatalog.entries(),
        }
        self.store.upsert("reports", f"repair_signatures:{run_id}", payload)
        return payload

    def _llm_refine_unknown_repair_packets(
        self,
        run: RunRecord,
        artifacts: dict[str, Any],
        issues: list[dict[str, Any]],
        packets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refined: list[dict[str, Any]] = []
        unknown_issue_iter = iter([issue for issue in issues if isinstance(issue, dict)])
        for packet in packets:
            if not self._is_unknown_repair_packet(packet):
                refined.append(packet)
                continue
            issue = next(unknown_issue_iter, packet)
            cache_key = self._repair_classifier_cache_key(run.run_id, issue)
            cached = self.store.get("reports", cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("packet"), dict):
                refined.append(dict(cached["packet"]))
                continue
            if not self.openai_client.enabled:
                classified = self._llm_classifier_unavailable_packet(issue)
                self.store.upsert("reports", cache_key, {"run_id": run.run_id, "packet": classified, "status": "blocked", "created_at": datetime.now(timezone.utc).isoformat()})
                refined.append(classified)
                continue
            try:
                classified_raw = self.openai_client.classify_repair_issue(
                    issue=issue,
                    run_context={
                        "run_id": run.run_id,
                        "prompt": run.prompt,
                        "acceptance_contract": run.acceptance_contract,
                        "diff_summary": {
                            "changed_files": run.touched_files or self._paths_from_diff(str(artifacts.get("diff") or "")),
                            "diff_available": bool(str(artifacts.get("diff") or "").strip()),
                        },
                        "browser_proof": run.browser_flow_proof or artifacts.get("browser_flow_proof") or {},
                        "checks": artifacts.get("check_results") or [],
                    },
                    model_profile=run.model_profile,
                    generation_mode=run.generation_mode,
                )
                classified = self._normalize_llm_repair_packet(classified_raw, issue)
            except Exception as exc:
                classified = self._llm_classifier_failed_packet(issue, exc)
            self.store.upsert("reports", cache_key, {"run_id": run.run_id, "packet": classified, "status": classified.get("status") or "available", "created_at": datetime.now(timezone.utc).isoformat()})
            refined.append(classified)
        return refined

    @staticmethod
    def _is_unknown_repair_packet(packet: dict[str, Any]) -> bool:
        return str(packet.get("signature") or "") == "generation.unknown_failure" or str(packet.get("issue_code") or packet.get("code") or "") == "unknown_failure"

    @staticmethod
    def _repair_classifier_cache_key(run_id: str, issue: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return f"repair_llm_classifier:{run_id}:{digest}"

    @staticmethod
    def _normalize_llm_repair_packet(raw: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
        signature = str(raw.get("signature") or raw.get("failure_signature") or "llm_repair.classified").strip()
        issue_code = str(raw.get("issue_code") or raw.get("code") or "llm_classified_repair").strip()
        target_files = [
            str(item).strip().replace("\\", "/")
            for item in raw.get("target_files") or issue.get("paths") or []
            if str(item).strip().replace("\\", "/").startswith("miniapp/")
        ][:8]
        required_next_tool = str(raw.get("required_next_tool") or "read_files").strip() or "read_files"
        suggested = str(raw.get("suggested_tool_after_read") or "write_file").strip() or "write_file"
        return {
            "signature": signature,
            "issue_code": issue_code,
            "code": issue_code,
            "severity": str(raw.get("severity") or issue.get("severity") or "high"),
            "likely_root_cause": str(raw.get("likely_root_cause") or issue.get("details") or "LLM repair classifier identified a concrete repair path."),
            "target_files": target_files,
            "verification_check": str(raw.get("verification_check") or issue.get("check") or "checks.run"),
            "verification_command": str(raw.get("verification_command") or "run_checks"),
            "instruction": str(raw.get("instruction") or "Read the target files, apply the classified repair, and rerun the failing check."),
            "auto_fixable": bool(raw.get("retryable", True)),
            "required_next_tool": required_next_tool,
            "suggested_tool_after_read": suggested,
            "retry_policy": "llm_classified_repair",
            "retryable": bool(raw.get("retryable", True)),
            "deterministic": False,
            "failure_class": str(issue.get("failure_class") or issue.get("check") or "llm_repair_classifier"),
            "failure_signature": signature,
            "repair_recipe_id": f"llm.{issue_code}",
            "forbidden_tools_once": [],
            "next_forced_action": {
                "required_next_tool": required_next_tool,
                "target_files": target_files,
                "verification_check": str(raw.get("verification_check") or issue.get("check") or "checks.run"),
            },
            "llm_usage": raw.get("_llm_usage") or {},
            "llm_model": raw.get("_llm_model"),
            "evidence": {"source_issue": issue, "classifier": "llm_json"},
        }

    @staticmethod
    def _llm_classifier_unavailable_packet(issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "signature": "repair.llm_classifier_unavailable",
            "issue_code": "llm_repair_classifier_unavailable",
            "code": "llm_repair_classifier_unavailable",
            "severity": "high",
            "likely_root_cause": "Unknown repair issue requires LLM JSON classification, but OpenAI is not configured.",
            "target_files": list(issue.get("paths") or []),
            "verification_check": str(issue.get("check") or "checks.run"),
            "verification_command": "run_checks",
            "instruction": "Configure the LLM classifier, then classify the issue before another repair attempt.",
            "auto_fixable": False,
            "required_next_tool": "read_files",
            "suggested_tool_after_read": "write_file",
            "retry_policy": "requires_llm_classifier",
            "retryable": False,
            "deterministic": False,
            "failure_class": "repair.llm_classifier_unavailable",
            "failure_signature": "repair.llm_classifier_unavailable",
            "repair_recipe_id": "llm.classifier_unavailable",
            "forbidden_tools_once": [],
            "next_forced_action": {"required_next_tool": "read_files", "target_files": list(issue.get("paths") or []), "verification_check": str(issue.get("check") or "checks.run")},
            "evidence": {"source_issue": issue},
        }

    @staticmethod
    def _llm_classifier_failed_packet(issue: dict[str, Any], exc: Exception) -> dict[str, Any]:
        packet = WorkbenchService._llm_classifier_unavailable_packet(issue)
        packet.update(
            {
                "signature": "repair.llm_classifier_failed",
                "issue_code": "llm_repair_classifier_failed",
                "code": "llm_repair_classifier_failed",
                "likely_root_cause": "LLM repair classifier failed before returning a typed packet.",
                "failure_class": "repair.llm_classifier_failed",
                "failure_signature": f"repair.llm_classifier_failed:{type(exc).__name__}",
                "repair_recipe_id": "llm.classifier_failed",
                "evidence": {"source_issue": issue, "error": str(exc), "error_type": type(exc).__name__},
            }
        )
        return packet

    def final_report(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        preview = artifacts.get("preview") or {}
        gate = self.gate(run_id)
        run = self.run_service.get_run(run_id)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        diagnostics_delta_ref = (checkpoint or {}).get("diagnostics_delta_ref") if isinstance(checkpoint, dict) else None
        diagnostics_delta = self.store.get("reports", diagnostics_delta_ref) if diagnostics_delta_ref else None
        report = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": "passed" if gate.get("status") == "passed" else "blocked" if gate.get("blocking") else run.status,
            "blocking": bool(gate.get("blocking")),
            "prompt": run.prompt,
            "summary": run.summary,
            "acceptance_contract": run.acceptance_contract,
            "diff_summary": {
                "changed_files": run.touched_files or self._paths_from_diff(str(artifacts.get("diff") or "")),
                "diff_available": bool(str(artifacts.get("diff") or "").strip()),
            },
            "checks": artifacts.get("check_results") or [],
            "browser_proof": self.browser_proof(run_id),
            "repair_signatures": self.repair_signatures(run_id).get("items", []),
            "repair_packets": gate.get("repair_packets", []),
            "next_forced_action": gate.get("next_forced_action", {}),
            "run_state": gate.get("run_state") or self.run_state(run_id),
            "diagnostics_delta": diagnostics_delta,
            "token_usage": run.token_usage,
            "token_usage_status": "recorded" if run.token_usage else "not_recorded",
            "preview": {
                "url": preview.get("url"),
                "role_urls": preview.get("role_urls") or {},
                "status": preview.get("status"),
            },
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}",
                "gate": f"gate:{run_id}",
                "browser_proof": run.browser_proof_ref,
                "repair_recipes": run.repair_recipes_ref,
                "resume_checkpoint": run.resume_checkpoint_ref,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"final_report:{run_id}", report)
        return report

    def run_state(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        gate = self.store.get("reports", f"gate:{run_id}")
        if not isinstance(gate, dict):
            gate = {"status": "pending", "blocking": False, "issues": []}
        browser_proof = self._normalize_browser_proof_payload(run, artifacts)
        state = RunStateMachine.evaluate(run=run, gate=gate, artifacts=artifacts, browser_proof=browser_proof)
        self.store.upsert("reports", f"run_state:{run_id}", state)
        return state

    def resume_run(self, run_id: str) -> RunRecord:
        run = self.run_service.get_run(run_id)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        prompt = (
            "Resume the retained run from its checkpoint. Use the existing acceptance contract, "
            "repair signatures, and draft evidence; do not restart diagnosis from scratch."
        )
        if isinstance(checkpoint, dict) and checkpoint.get("reason"):
            prompt = f"{prompt}\nCheckpoint reason: {checkpoint.get('reason')}"
        if isinstance(checkpoint, dict) and checkpoint.get("repair_packets"):
            prompt = (
                f"{prompt}\nResume repair packet: "
                f"{json.dumps(checkpoint.get('repair_packets'), ensure_ascii=False, default=str)[:2400]}"
            )
        if isinstance(checkpoint, dict) and checkpoint.get("next_forced_action"):
            prompt = (
                f"{prompt}\nNext forced repair action: "
                f"{json.dumps(checkpoint.get('next_forced_action'), ensure_ascii=False, default=str)[:1200]}"
            )
        return self.run_service.create_run(
            run.workspace_id,
            CreateRunRequest(
                prompt=prompt,
                mode="fix",
                intent="edit",
                apply_strategy="staged_auto_apply",
                target_role_scope=list(run.target_role_scope or []),
                model_profile=run.model_profile,
                generation_mode=run.generation_mode,
                resume_from_run_id=run.run_id,
            ),
        )

    def memory(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        current = self.store.get("reports", f"workspace_memory:{workspace_id}") or {
            "workspace_id": workspace_id,
            "items": [],
            "project_rules": [],
            "user_preferences": [],
            "product_decisions": [],
            "accepted_ux_rules": [],
            "architecture_summary": [],
            "known_failures": [],
            "rejected_approaches": [],
            "do_not_change": [],
            "platform_constraints": [],
            "repeated_fixes": [],
        }
        current["stale_check"] = WorkspaceMemoryPipeline.stale_check(self.workspace_service.source_dir(workspace_id), current)
        current["pipeline"] = self.memory_pipeline(workspace_id)
        return current

    def extract_run_memory(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        payload = WorkspaceMemoryPipeline.extract_run(run, self._run_artifacts_or_empty(run_id))
        self.store.upsert("reports", f"memory_stage1:{run.workspace_id}:{run_id}", payload)
        return payload

    def memory_pipeline(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        stage1 = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"memory_stage1:{workspace_id}:") and isinstance(payload, dict)
        ]
        consolidated = self.store.get("reports", f"memory_consolidation:{workspace_id}") or {}
        return {
            "schema": "grounded.memory_pipeline.v1",
            "workspace_id": workspace_id,
            "status": "ready" if stage1 or consolidated else "empty",
            "stage1_count": len(stage1),
            "stage1_items": sum(len(payload.get("items") or []) for payload in stage1),
            "consolidated_at": consolidated.get("updated_at") or consolidated.get("created_at"),
            "items": stage1[-20:],
        }

    def consolidate_memory(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        stage1 = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"memory_stage1:{workspace_id}:") and isinstance(payload, dict)
        ]
        current = self.store.get("reports", f"workspace_memory:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        consolidated = WorkspaceMemoryPipeline.consolidate(workspace_id, stage1, current)
        consolidated["stale_check"] = WorkspaceMemoryPipeline.stale_check(self.workspace_service.source_dir(workspace_id), consolidated)
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", consolidated)
        summary = {
            "schema": "grounded.memory_consolidation.v1",
            "workspace_id": workspace_id,
            "status": "consolidated",
            "stage1_count": len(stage1),
            "active_count": len(consolidated.get("items") or []),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"memory_consolidation:{workspace_id}", summary)
        return {**consolidated, "pipeline": self.memory_pipeline(workspace_id)}

    def project_instructions(self) -> dict[str, Any]:
        payload = ProjectInstructionBundle.build(repo_root=self.settings.repo_root, template_dir=self.settings.template_dir)
        self.store.upsert("reports", "project_instructions:current", payload)
        return payload

    def upsert_memory(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.memory(workspace_id)
        item = {
            "memory_id": f"mem_{uuid4().hex}",
            "kind": str(payload.get("kind") or "note"),
            "text": str(payload.get("text") or payload.get("content") or "").strip(),
            "citation": payload.get("citation"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not item["text"]:
            raise ValueError("Memory text is required.")
        secret_scan = self._memory_secret_scan(str(item["text"]))
        if secret_scan["status"] != "passed":
            raise ValueError("Memory text appears to contain secret-like material; remove the secret before saving.")
        item["secret_scan"] = secret_scan
        current.setdefault("items", []).append(item)
        bucket_map = {
            "user_preference": "user_preferences",
            "project_rule": "project_rules",
            "product_decision": "product_decisions",
            "ux_rule": "accepted_ux_rules",
            "architecture": "architecture_summary",
            "known_failure": "known_failures",
            "rejected_approach": "rejected_approaches",
            "do_not_change": "do_not_change",
            "repeated_fix": "repeated_fixes",
        }
        bucket = bucket_map.get(item["kind"])
        if bucket:
            current.setdefault(bucket, []).append(item)
        current["stale_check"] = self._memory_stale_check(workspace_id, current)
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", current)
        return current

    def skills(self) -> dict[str, Any]:
        skills = dict(self._builtin_skills())
        prefetch = SkillPackCatalog.prefetch(self.settings.runtime_dir, self.settings.repo_root)
        for item in prefetch.get("items") or []:
            skills.setdefault(item["id"], item)
        for item in self._document_skills():
            skills.setdefault(item["id"], item)
        for item in skills.values():
            item.setdefault("activation_reason", "available_metadata")
        return {
            "schema": "grounded.skills.v1",
            "prefetch": {key: value for key, value in prefetch.items() if key != "items"},
            "items": sorted(skills.values(), key=lambda item: str(item.get("id") or "")),
        }

    def skill(self, skill_id: str) -> dict[str, Any]:
        item = {item["id"]: item for item in self.skills()["items"]}.get(skill_id)
        if item is None:
            raise KeyError(f"Skill not found: {skill_id}")
        return item

    def slash_commands(self) -> dict[str, Any]:
        payload = SlashCommandCatalog.list()
        self.store.upsert("reports", "slash_commands:current", payload)
        return payload

    def resolve_slash_command(self, command_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = SlashCommandCatalog.resolve(command_id, payload)
        self.store.upsert("reports", f"slash_command:{command_id}", resolved)
        return resolved

    def magic_doc(self, workspace_id: str, *, write: bool = False) -> dict[str, Any]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        runs = self.run_service.list_runs(workspace_id)
        payload = MagicDocsBuilder.build(
            workspace=workspace,
            memory=self.memory(workspace_id),
            runs=runs,
            source_dir=self.workspace_service.source_dir(workspace_id),
        )
        if write:
            doc_path = self.workspace_service.source_dir(workspace_id) / payload["path"]
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(str(payload["content"]), encoding="utf-8")
            payload["write_status"] = "written"
            payload["absolute_path"] = str(doc_path)
        else:
            payload["write_status"] = "preview"
        self.store.upsert("reports", f"magic_doc:{workspace_id}:product_architecture", payload)
        return payload

    def plugins(self) -> dict[str, Any]:
        items = [
            {"id": "core.validators", "version": "0.1.0", "capabilities": ["validators"], "status": "installed"},
            {"id": "core.exporters", "version": "0.1.0", "capabilities": ["exporters"], "status": "installed"},
            {"id": "core.preview", "version": "0.1.0", "capabilities": ["preview_adapters"], "status": "installed"},
        ]
        items.extend(self._load_plugin_manifests())
        return {"items": sorted(items, key=lambda item: str(item.get("id") or ""))}

    def install_plugin_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = dict(payload or {})
        plugin_id = str(manifest.get("id") or "").strip()
        if not plugin_id:
            raise ValueError("Plugin manifest id is required.")
        if not str(manifest.get("version") or "").strip():
            raise ValueError("Plugin manifest version is required.")
        if not isinstance(manifest.get("capabilities"), list):
            raise ValueError("Plugin manifest capabilities must be a list.")
        record = {"id": plugin_id, "status": "registered", "manifest": manifest, "installed_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"plugin:{plugin_id}", record)
        return record

    def mcp_servers(self) -> dict[str, Any]:
        config = self._mcp_config()
        return {"items": config.get("servers", []), "status": "configured" if config.get("servers") else "not_configured"}

    def mcp_tools(self) -> dict[str, Any]:
        config = self._mcp_config()
        return {"items": config.get("tools", []), "tool_protocol": tool_registry_contract()}

    def call_mcp_tool(self, tool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_id": tool_id,
            "status": "blocked",
            "approval_required": True,
            "reason": "External MCP tool execution is reserved until connector configuration is present.",
            "input": payload,
        }

    def doctor(self) -> dict[str, Any]:
        checks = [
            self._check("python", True, sys.version.split()[0], str(Path(sys.executable))),
            self._binary_check("node"),
            self._binary_check("npm"),
            self._binary_check("docker"),
            self._compose_check(),
            self._playwright_check(),
            self._openai_check(),
            self._writable_check("data_dir", self.settings.data_dir),
            self._template_check(),
            self._port_check(),
            self._backend_routes_check(),
            self._stale_backend_check(),
            self._playwright_browsers_check(),
            self._preview_container_check(),
            self._test_command_check(),
            self.exec_policy_service.doctor_check(),
        ]
        status = "passed" if all(item["status"] == "passed" for item in checks if item["required"]) else "failed"
        payload = {"status": status, "checks": checks, "created_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", "doctor:exec_policy", self.exec_policy_service.doctor_check())
        self.store.upsert("reports", "doctor:last", payload)
        return payload

    def metrics_summary(self) -> dict[str, Any]:
        runs = self.store.list("runs")
        return {
            "run_count": len(runs),
            "completed_runs": len([run for run in runs if run.get("status") == "completed"]),
            "failed_runs": len([run for run in runs if run.get("status") == "failed"]),
            "blocked_runs": len([run for run in runs if run.get("status") == "blocked"]),
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "token_usage_total": sum(int(((run.get("token_usage") or {}).get("total_tokens") or 0)) for run in runs),
            "latency_ms_total": sum(int(((run.get("latency_breakdown") or {}).get("total_ms") or 0)) for run in runs),
        }

    def config_schema(self) -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://grounded.local/schemas/grounded.platform.config.schema.json",
            "title": "Grounded Platform Configuration",
            "type": "object",
            "platform_config_version": "grounded.platform.v1",
            "workspace_config_version": "grounded.workspace.v1",
            "policy_config_version": "grounded.policy.v1",
            "plugin_config_version": "grounded.plugin.v1",
            "required": ["platform", "workspace", "policy", "plugin"],
            "properties": {
                "platform": {"$ref": "#/schemas/platform"},
                "workspace": {"$ref": "#/schemas/workspace"},
                "policy": {"$ref": "#/schemas/policy"},
                "plugin": {"$ref": "#/schemas/plugin"},
                "generation_enhancements": {"$ref": "#/schemas/generation_enhancements"},
            },
            "schemas": {
                "platform": {
                    "type": "object",
                    "required": ["data_dir", "runtime_dir", "template_dir", "preview_port_base"],
                    "properties": {
                        "data_dir": {"type": "string", "default": str(self.settings.data_dir)},
                        "runtime_dir": {"type": "string", "default": str(self.settings.runtime_dir)},
                        "template_dir": {"type": "string", "default": str(self.settings.template_dir)},
                        "preview_port_base": {"type": "integer", "default": self.settings.preview_port_base},
                    },
                    "additionalProperties": False,
                },
                "workspace": {
                    "type": "object",
                    "required": ["workspace_id", "name", "target_platform", "preview_profile", "current_revision_id"],
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "name": {"type": "string"},
                        "target_platform": {"type": "string"},
                        "preview_profile": {"type": "string"},
                        "current_revision_id": {"type": ["string", "null"]},
                    },
                    "strict_api_edges": True,
                    "additionalProperties": True,
                },
                "policy": self.exec_policy_service.snapshot(),
                "plugin": {
                    "type": "object",
                    "required": ["id", "version", "capabilities"],
                    "properties": {
                        "id": {"type": "string"},
                        "version": {"type": "string"},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                    },
                    "capabilities": [
                        "validators",
                        "exporters",
                        "preview_adapters",
                        "platform_adapters",
                        "skills",
                        "mcp_tools",
                        "slash_commands",
                        "acceptance_scenarios",
                        "visual_qa",
                        "trace_reducer",
                        "magic_docs",
                    ],
                    "additionalProperties": True,
                },
                "generation_enhancements": {
                    "type": "object",
                    "properties": {
                        "project_instructions": {"type": "boolean", "default": True},
                        "runtime_skills": {"type": "boolean", "default": True},
                        "workspace_memory": {"type": "boolean", "default": True},
                        "magic_docs": {"type": "boolean", "default": True},
                        "slash_commands": {"type": "boolean", "default": True},
                        "acceptance_scenarios": {"type": "boolean", "default": True},
                        "visual_qa": {"type": "boolean", "default": True},
                        "trace_reducer": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def migrations(self) -> dict[str, Any]:
        checks = [
            {
                "id": "state_store_v1",
                "status": "current",
                "description": "JSON state collections are readable with additive fields.",
            },
            {
                "id": "workspace_metadata_v1",
                "status": "current",
                "description": "Workspace records keep revision history and tolerate unknown future fields through strict migrations at API edges.",
            },
            {
                "id": "artifact_refs_v1",
                "status": "current",
                "description": "Run artifacts remain addressable through report refs.",
            },
            *ConfigMigrationCatalog.items(),
        ]
        return {"status": "current", "items": checks, "created_at": datetime.now(timezone.utc).isoformat()}

    def test_matrix(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        check_results = artifacts.get("check_results") or []
        checks_by_name = {str(item.get("name") or ""): item for item in check_results if isinstance(item, dict)}

        def entry(key: str, label: str, names: tuple[str, ...], *, required: bool = True) -> dict[str, Any]:
            matched = [checks_by_name[name] for name in names if name in checks_by_name]
            status = "skipped"
            if matched:
                status = "passed" if all(str(item.get("status")) == "passed" for item in matched) else "failed"
            return {"key": key, "label": label, "status": status, "required": required, "evidence": matched}

        items = [
            entry("backend_pytest", "Backend pytest", ("generated_backend_tests", "backend_tests", "pytest")),
            entry("frontend_js_smoke", "Frontend JS smoke", ("frontend_interaction_static_smoke", "js_syntax")),
            entry("role_pages", "Role page smoke", ("preview_route_smoke", "role_pages")),
            entry("accessibility", "Accessibility checks", ("mobile_layout", "accessibility"), required=False),
            entry("persistence", "Persisted workflow checks", ("api_workflow_proof", "browser_flow_smoke")),
            entry("docker_compose_boot", "Docker compose boot", ("preview_boot_smoke",), required=False),
            entry("playwright_proof", "Playwright proof", ("browser_flow_smoke", "browser_proof")),
            {
                "key": "acceptance_scenarios",
                "label": "Acceptance scenarios",
                "status": "passed" if self.acceptance_scenarios(run_id).get("items") else "failed",
                "required": True,
                "evidence": self.acceptance_scenarios(run_id).get("items", []),
            },
            {
                "key": "visual_qa",
                "label": "Visual QA",
                "status": "passed" if self.visual_qa(run_id).get("status") == "passed" else "failed",
                "required": False,
                "evidence": self.visual_qa(run_id).get("issues", []),
            },
        ]
        status = "passed" if all(item["status"] == "passed" for item in items if item["required"]) else "incomplete"
        payload = {"run_id": run_id, "workspace_id": run.workspace_id, "status": status, "items": items}
        self.store.upsert("reports", f"test_matrix:{run_id}", payload)
        return payload

    def prompt_contract(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        contract = dict(run.acceptance_contract or {})
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        analysis_status = str(prompt_hints.get("analysis_status") or "unknown")
        status = "passed" if not contract.get("required") or analysis_status == "ok" else "needs_review"
        payload = {
            "run_id": run_id,
            "status": status,
            "analysis_source": prompt_hints.get("analysis_source"),
            "analysis_status": analysis_status,
            "resource_hint": prompt_hints.get("resource_hint"),
            "field_hints": list(prompt_hints.get("field_hints") or [])[:12],
            "role_field_hints": dict(prompt_hints.get("role_field_hints") or {}),
            "findings": [] if status == "passed" else [{"severity": "medium", "message": "Prompt contract analysis is unavailable; generation should use LLM prompt analysis before applying product fields."}],
        }
        self.store.upsert("reports", f"prompt_contract:{run_id}", payload)
        return payload

    def miniapp_contract(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        contract_report = self.store.get("reports", run.miniapp_contract_ref) if run.miniapp_contract_ref else None
        if not isinstance(contract_report, dict):
            contract_report = artifacts.get("miniapp_contract") if isinstance(artifacts.get("miniapp_contract"), dict) else None
        contract_payload = (contract_report or {}).get("contract") if isinstance(contract_report, dict) else None
        source_dir = (
            self.workspace_service.draft_source_dir(run.workspace_id, run_id)
            if self.workspace_service.draft_exists(run.workspace_id, run_id)
            else self.workspace_service.source_dir(run.workspace_id)
        )
        contract = MiniAppRouteRegistry.load_contract(source_dir)
        if contract is None and isinstance(contract_payload, dict):
            try:
                from app.services.miniapp_contract import MiniAppContract

                contract = MiniAppContract.model_validate(contract_payload)
            except Exception:
                contract = None
        registry_report = self.store.get("reports", run.route_registry_ref) if run.route_registry_ref else None
        registry_snapshot = None
        if isinstance(registry_report, dict) and isinstance(registry_report.get("snapshot"), dict):
            registry_snapshot = registry_report["snapshot"]
        if registry_snapshot is None and contract is not None:
            registry_snapshot = MiniAppRouteRegistry.snapshot(source_dir, contract).model_dump(mode="json")
        if registry_snapshot is None:
            registry_snapshot = {
                "status": "not_available",
                "drift_issues": [],
                "repair_recipes": [],
            }
        repair_report = self.store.get("reports", run.repair_recipes_ref) if run.repair_recipes_ref else None
        repair_recipes = (
            repair_report.get("items")
            if isinstance(repair_report, dict) and isinstance(repair_report.get("items"), list)
            else registry_snapshot.get("repair_recipes", [])
        )
        payload = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": registry_snapshot.get("status") or "passed",
            "contract": contract.model_dump(mode="json") if contract is not None else None,
            "registry_snapshot": registry_snapshot,
            "drift_issues": registry_snapshot.get("drift_issues", []),
            "repair_recipes": repair_recipes,
            "artifact_refs": {
                "miniapp_contract": run.miniapp_contract_ref,
                "route_registry": run.route_registry_ref,
                "contract_compile": run.contract_compile_ref,
                "repair_recipes": run.repair_recipes_ref,
            },
        }
        self.store.upsert("reports", f"miniapp_contract_view:{run_id}", payload)
        return payload

    def security_summary(self) -> dict[str, Any]:
        return {
            "status": "configured",
            "permission_rules": self.permission_rules()["items"],
            "recent_denials": self.recent_denials()["items"],
            "checks": [
                {"key": "path_traversal", "status": "covered", "evidence": "Workspace paths normalize through safe relative path checks."},
                {"key": "write_denylist", "status": "covered", "evidence": self.exec_policy_service.write_grants()["deny"]},
                {"key": "command_allow_deny", "status": "covered", "evidence": self.exec_policy_service.snapshot()["risk_model"]},
                {"key": "approval_bypass_prevention", "status": "covered", "evidence": "Approval ids are matched against run-scoped approval records."},
                {"key": "secret_redaction", "status": "covered", "evidence": "ExecPolicyService.redact is applied to command events."},
                {"key": "artifact_access_boundaries", "status": "covered", "evidence": "Artifacts are fetched through run-scoped refs."},
            ],
        }

    def permission_rules(self) -> dict[str, Any]:
        stored = self.store.get("reports", "permission_rules") or {"items": []}
        defaults = [
            {"rule_id": "allow_readonly_diagnostics", "scope": "workspace", "risk": "read_only", "action": "allow", "source": "default"},
            {"rule_id": "prompt_mutating", "scope": "workspace", "risk": "mutating", "action": "prompt", "source": "default"},
            {"rule_id": "block_destructive", "scope": "workspace", "risk": "destructive", "action": "block", "source": "default"},
            {"rule_id": "block_network", "scope": "external", "risk": "network", "action": "prompt", "source": "default"},
        ]
        merged = {item["rule_id"]: item for item in defaults}
        for item in stored.get("items") or []:
            if isinstance(item, dict) and item.get("rule_id"):
                merged[str(item["rule_id"])] = item
        return {"items": sorted(merged.values(), key=lambda item: str(item.get("rule_id") or ""))}

    def upsert_permission_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = {
            "rule_id": str(payload.get("rule_id") or f"rule_{uuid4().hex}"),
            "scope": str(payload.get("scope") or "workspace"),
            "risk": str(payload.get("risk") or "unknown"),
            "action": str(payload.get("action") or "prompt"),
            "pattern": str(payload.get("pattern") or ""),
            "source": "user",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        current = self.permission_rules()
        current["items"] = [entry for entry in current["items"] if entry.get("rule_id") != item["rule_id"]]
        current["items"].append(item)
        self.store.upsert("reports", "permission_rules", current)
        return item

    def recent_denials(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for key, payload in self.store.items("reports"):
            if not key.startswith("tool_events:") or not isinstance(payload, dict):
                continue
            for event in payload.get("items") or []:
                if not isinstance(event, dict):
                    continue
                approval = event.get("approval") if isinstance(event.get("approval"), dict) else {}
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
                if approval.get("status") == "blocked" or decision.get("action") == "forbidden":
                    items.append(
                        {
                            "run_id": payload.get("run_id"),
                            "tool": event.get("tool"),
                            "risk": event.get("risk") or decision.get("risk"),
                            "reason": decision.get("reason") or ((event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else ""),
                            "created_at": event.get("created_at"),
                        }
                    )
        return {"items": sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:50]}

    def compact_run(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.run_compaction_service is None:
            raise KeyError("Run compaction service is unavailable.")
        artifacts = self._run_artifacts_or_empty(run_id)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else {}
        context_pressure = self.store.get("reports", run.context_pressure_ref) if run.context_pressure_ref else {}
        return self.run_compaction_service.compact_run(
            run=run,
            artifacts=artifacts,
            checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
            context_pressure=context_pressure if isinstance(context_pressure, dict) else {},
            reason="manual",
            source="manual",
        )

    def compaction(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        if self.run_compaction_service is None:
            return {"schema": "grounded.run_compaction.v1", "run_id": run_id, "status": "unavailable", "sections": {}, "refs": {}}
        return self.run_compaction_service.get_compaction(run_id)

    def compaction_boundaries(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        if self.run_compaction_service is None:
            return {"schema": "grounded.run_compaction_boundaries.v1", "run_id": run_id, "status": "unavailable", "items": []}
        return self.run_compaction_service.boundaries(run_id)

    def microcompact(self, run_id: str, digest: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.run_compaction_service is None:
            raise KeyError("Run compaction service is unavailable.")
        return self.run_compaction_service.microcompact(run.workspace_id, run_id, digest)

    def post_compact_message(self, run_id: str, boundary_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        if self.run_compaction_service is None:
            raise KeyError("Run compaction service is unavailable.")
        return self.run_compaction_service.post_compact_message(run_id, boundary_id)

    def stage_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        files = self._normalize_file_list(payload.get("files") or [])
        categories = {path: self._file_category(path) for path in files}
        record = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "files": files,
            "categories": categories,
            "status": "staged",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"staged_files:{run_id}", record)
        self.record_tool_event(run_id, tool_envelope(tool="approval.stage", input_payload=record, result={"status": "staged"}, risk="mutating"))
        return record

    def discard_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        files = self._normalize_file_list(payload.get("files") or [])
        result = self.workspace_service.discard_draft_files(run.workspace_id, run_id, files)
        record = {"run_id": run_id, "files": files, "result": result, "status": "discarded", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"discarded_files:{run_id}", record)
        self.record_tool_event(run_id, tool_envelope(tool="file.discard", input_payload={"files": files}, result=record, risk="mutating"))
        return record

    def apply_staged(self, run_id: str) -> Any:
        run = self.run_service.get_run(run_id)
        staged = self.store.get("reports", f"staged_files:{run_id}") or {}
        changed_before_apply = set(self.workspace_service.draft_changed_paths(run.workspace_id, run_id))
        staged_files = staged.get("files") if isinstance(staged.get("files"), list) else None
        files = self._normalize_file_list(staged_files if staged_files is not None else list(changed_before_apply))
        contract_owned = set(MiniAppContractMaterializer.contract_owned_paths())
        blocking_required = sorted(
            path
            for path in changed_before_apply
            if path in contract_owned or path.startswith("miniapp/app/generated/")
        )
        files = list(dict.fromkeys([*files, *blocking_required]))
        revision = self.workspace_service.apply_selected_draft_files(run.workspace_id, run_id, files, message=f"Apply staged AI draft files for run {run_id}")
        fully_applied = bool(changed_before_apply) and set(files).issuperset(changed_before_apply)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.touched_files = files
        run.apply_status = "applied" if fully_applied else "awaiting_approval"
        run.status = "completed" if fully_applied else "awaiting_approval"
        run.draft_status = "approved" if fully_applied else "ready"
        run.draft_ready = not fully_applied
        run.progress_percent = 100 if fully_applied else max(run.progress_percent, 95)
        run.current_stage = "completed" if fully_applied else "partially applied"
        run.updated_at = datetime.now(timezone.utc)
        if fully_applied:
            self.workspace_service.discard_draft(run.workspace_id, run_id)
        self.store.upsert("runs", run_id, run.model_dump(mode="json"))
        artifacts = self._run_artifacts_or_empty(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        artifacts["staged_apply"] = {"files": files, "revision_id": revision.revision_id, "fully_applied": fully_applied}
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.record_tool_event(run_id, tool_envelope(tool="patch.apply", input_payload={"files": files}, result=artifacts["staged_apply"], risk="mutating"))
        return run

    def diff(self, run_id: str, *, base: str, target: str, file: str | None = None, worker_id: str | None = None, category: str | None = None, status: str | None = None) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        diff = artifacts.get("diff") or self.workspace_service.diff(run.workspace_id, run_id=run_id)
        files = run.touched_files or self._paths_from_diff(diff)
        if file:
            files = [path for path in files if path == file]
        if worker_id:
            files = [path for path in files if self._path_owned_by_worker(worker_id, path)]
        if category:
            files = [path for path in files if self._file_category(path) == category]
        filtered_diff = self._filter_diff(diff, files) if (file or worker_id or category or status) else diff
        return {"run_id": run_id, "base": base, "target": target, "diff": filtered_diff, "files": files, "filters": {"file": file, "worker_id": worker_id, "category": category, "status": status}}

    def approvals(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        return self.store.get("reports", f"approvals:{run_id}") or {"run_id": run_id, "items": []}

    def file_search(self, workspace_id: str, *, query: str, run_id: str | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        if run_id and ("/" in run_id or "\\" in run_id or ".." in Path(run_id).parts):
            raise ValueError("Run id must not contain path traversal.")
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"workspace_id": workspace_id, "query": normalized_query, "items": []}
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        if run_id and not root.exists():
            raise KeyError(f"Draft not found for run: {run_id}")
        if shutil.which("rg"):
            rg_items = self._ripgrep_search(root, normalized_query)
            if rg_items:
                return {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "query": normalized_query,
                    "engine": "ripgrep",
                    "items": self._rank_search_items(rg_items, normalized_query, root),
                    "symbols": self._symbol_overview(root, normalized_query),
                }
        items: list[dict[str, Any]] = []
        for entry in self.workspace_service.file_tree(workspace_id, run_id=run_id):
            if entry.get("type") != "file":
                continue
            relative_path = str(entry.get("path") or "")
            if ".." in Path(relative_path).parts:
                raise ValueError("Search paths must stay inside the workspace.")
            haystack = relative_path.lower()
            content = self.workspace_service.try_read_text_file(workspace_id, relative_path, run_id=run_id)
            hits: list[dict[str, Any]] = []
            if content:
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if normalized_query.lower() in line.lower():
                        hits.append({"line": line_no, "text": line[:240]})
                    if len(hits) >= 5:
                        break
            if normalized_query.lower() in haystack or hits:
                items.append({"path": relative_path, "hits": hits})
            if len(items) >= 80:
                break
        return {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "query": normalized_query,
            "engine": "python",
            "items": self._rank_search_items(items, normalized_query, root),
            "symbols": self._symbol_overview(root, normalized_query),
        }

    def lsp_diagnostics(self, workspace_id: str, *, run_id: str | None = None, changed_only: bool = False, files: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        changed_files: list[str] = []
        if run_id:
            changed_files = self._paths_from_diff(self.workspace_service.diff(workspace_id, run_id=run_id))
        report = LspToolService.diagnostics(
            root=root,
            targets=files,
            changed_files=changed_files,
            changed_only=changed_only,
        )
        return {
            **report,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "sources": sorted({str(item.get("source") or "unknown") for item in report.get("items") or []} or {"none"}),
            "symbols": LspToolService.symbol_context(root=root, query="", targets=files).get("items", []),
        }

    def lsp_symbol_context(self, workspace_id: str, *, run_id: str | None = None, query: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.symbol_context(root=root, query=query, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_find_references(self, workspace_id: str, *, run_id: str | None = None, symbol: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.find_references(root=root, symbol=symbol, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_route_static_context(self, workspace_id: str, *, run_id: str | None = None, targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.route_static_context(root=root, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def patch_preflight(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ops = payload.get("ops") or payload.get("patch_actions") or []
        ops = [PatchOperationModel.model_validate(item) for item in raw_ops if isinstance(item, dict)]
        return self._patch_preflight(workspace_id, ops, payload)

    def _patch_preflight(self, workspace_id: str, ops: list[PatchOperationModel], payload: dict[str, Any]) -> dict[str, Any]:
        from app.services.patch_service import PatchService

        service = PatchService(self.workspace_service)
        return service.preflight(
            workspace_id=workspace_id,
            patch_actions=ops,
            run_id=payload.get("run_id"),
            base_revision_id=payload.get("base_revision_id"),
        )

    def _run_artifacts_or_empty(self, run_id: str) -> dict[str, Any]:
        try:
            return self.run_service.get_run_artifacts(run_id)
        except KeyError:
            self.run_service.get_run(run_id)
            return {}

    def _normalize_browser_proof_payload(self, run: RunRecord, artifacts: dict[str, Any]) -> dict[str, Any]:
        stored_ref_payload = self.store.get("reports", run.browser_proof_ref) if run.browser_proof_ref else None
        verification_report = self.store.get("reports", run.verification_report_ref) if run.verification_report_ref else None
        proof = self._first_dict(
            run.browser_flow_proof,
            artifacts.get("browser_flow_proof"),
            (stored_ref_payload or {}).get("proof") if isinstance(stored_ref_payload, dict) else None,
            stored_ref_payload,
            verification_report,
        )
        browser_check = self._check_result_by_name(artifacts, "browser_flow_smoke")
        diagnostics = browser_check.get("diagnostics") if isinstance(browser_check.get("diagnostics"), dict) else {}
        mobile_layout = self._first_dict(
            run.mobile_layout_report,
            proof.get("mobile_layout") if isinstance(proof, dict) else None,
            diagnostics.get("mobile_layout") if isinstance(diagnostics, dict) else None,
            (stored_ref_payload or {}).get("mobile_layout_report") if isinstance(stored_ref_payload, dict) else None,
        )
        steps = self._first_list(
            artifacts.get("browser_proof_steps"),
            proof.get("steps") if isinstance(proof, dict) else None,
            diagnostics.get("steps") if isinstance(diagnostics, dict) else None,
            (verification_report or {}).get("steps") if isinstance(verification_report, dict) else None,
        )
        screenshots = self._dedupe_strings(
            [
                *self._collect_values(artifacts, keys=("screenshot", "screenshot_path", "image_path")),
                *self._collect_values(proof, keys=("screenshot", "screenshot_path", "image_path", "screenshots")),
                *self._collect_values(verification_report or {}, keys=("screenshot", "screenshot_path", "image_path", "screenshots")),
            ]
        )
        console_errors = self._dedupe_strings(
            [
                *self._collect_values(artifacts, keys=("console_error", "console_errors")),
                *self._collect_values(proof, keys=("console_error", "console_errors")),
                *self._collect_values(verification_report or {}, keys=("console_error", "console_errors")),
            ]
        )
        network_errors = self._dedupe_strings(
            [
                *self._collect_values(artifacts, keys=("network_error", "network_errors")),
                *self._collect_values(proof, keys=("network_error", "network_errors")),
                *self._collect_values(verification_report or {}, keys=("network_error", "network_errors")),
            ]
        )
        roles_checked = self._browser_roles_checked(steps, proof)
        status = self._browser_proof_status(browser_check, proof, steps, console_errors, network_errors, mobile_layout)
        issues = self._browser_proof_issues(browser_check, steps, console_errors, network_errors, mobile_layout)
        return {
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "blocking": status in {"failed", "blocked", "not_recorded"} or bool(issues),
            "issues": issues,
            "roles_checked": roles_checked,
            "screenshots": screenshots,
            "video_refs": self._dedupe_strings(self._collect_values(proof, keys=("video", "video_path", "video_ref", "videos"))),
            "console_errors": console_errors,
            "network_errors": network_errors,
            "route_coverage": (run.flow_coverage or {}).get("routes", []),
            "mobile_layout": mobile_layout,
            "role_workflows": proof,
            "steps": steps,
            "verification_report": verification_report,
            "artifact_refs": {
                "browser_proof": run.browser_proof_ref,
                "verification_report": run.verification_report_ref,
                "run_artifacts": f"run_artifacts:{run.run_id}",
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _first_dict(*values: Any) -> dict[str, Any]:
        for value in values:
            if isinstance(value, dict) and value:
                return dict(value)
        return {}

    @staticmethod
    def _first_list(*values: Any) -> list[Any]:
        for value in values:
            if isinstance(value, list) and value:
                return list(value)
        return []

    @staticmethod
    def _dedupe_strings(values: list[Any]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _check_result_by_name(artifacts: dict[str, Any], name: str) -> dict[str, Any]:
        for item in artifacts.get("check_results") or []:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return {}

    @staticmethod
    def _browser_roles_checked(steps: list[Any], proof: dict[str, Any]) -> list[str]:
        roles: set[str] = set()
        for role in ("client", "specialist", "manager"):
            if role in proof:
                roles.add(role)
        for item in steps:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get(key) or "") for key in ("role", "route", "url", "path"))
            for role in ("client", "specialist", "manager"):
                if f"/{role}" in text or text.strip() == role:
                    roles.add(role)
        return sorted(roles)

    @staticmethod
    def _browser_proof_status(
        browser_check: dict[str, Any],
        proof: dict[str, Any],
        steps: list[Any],
        console_errors: list[str],
        network_errors: list[str],
        mobile_layout: dict[str, Any],
    ) -> str:
        explicit = str(proof.get("status") or browser_check.get("status") or "").strip().lower()
        if explicit in {"passed", "failed", "blocked"}:
            return explicit
        if not proof and not steps:
            return "not_recorded"
        if console_errors or network_errors:
            return "failed"
        if str(mobile_layout.get("status") or "").lower() == "failed":
            return "failed"
        if any(isinstance(item, dict) and str(item.get("status") or "").lower() in {"failed", "blocked"} for item in steps):
            return "failed"
        return "passed"

    @staticmethod
    def _browser_proof_issues(
        browser_check: dict[str, Any],
        steps: list[Any],
        console_errors: list[str],
        network_errors: list[str],
        mobile_layout: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if str(browser_check.get("status") or "").lower() in {"failed", "blocked"}:
            issues.append(
                {
                    "kind": "browser_check_failed",
                    "check": "browser_flow_smoke",
                    "details": str(browser_check.get("details") or "Browser proof failed."),
                    "blocking": True,
                    "evidence": browser_check,
                }
            )
        for item in steps:
            if isinstance(item, dict) and str(item.get("status") or "").lower() in {"failed", "blocked"}:
                issues.append(
                    {
                        "kind": "browser_step_failed",
                        "check": "browser_flow_smoke",
                        "details": str(item.get("message") or item.get("step") or "Browser step failed."),
                        "blocking": True,
                        "evidence": item,
                    }
                )
        if console_errors:
            issues.append({"kind": "browser_console_error", "check": "browser_console", "details": console_errors[0], "blocking": True, "evidence": {"items": console_errors}})
        if network_errors:
            issues.append({"kind": "browser_network_error", "check": "browser_network", "details": network_errors[0], "blocking": True, "evidence": {"items": network_errors}})
        if str(mobile_layout.get("status") or "").lower() == "failed":
            issues.append({"kind": "mobile_layout", "check": "browser_flow_smoke", "details": "Mobile layout report failed.", "blocking": True, "evidence": mobile_layout})
        return issues

    def approval_decision(self, run_id: str, approval_id: str, *, approved: bool) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        existing = self._approval_by_id(run_id, approval_id) or {
            "approval_id": approval_id,
            "kind": "manual",
            "summary": f"Manual approval {approval_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        item = {**existing, "status": "approved" if approved else "rejected", "decided_at": datetime.now(timezone.utc).isoformat()}
        self._upsert_approval(run_id, item)
        self.record_tool_event(run_id, tool_envelope(tool="approval.decision", input_payload={"approval_id": approval_id}, result=item, risk="safe"))
        return item

    def _upsert_approval(self, run_id: str, item: dict[str, Any]) -> None:
        key = f"approvals:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        items = [entry for entry in payload.get("items") or [] if isinstance(entry, dict)]
        replaced = False
        for index, entry in enumerate(items):
            if entry.get("approval_id") == item.get("approval_id"):
                items[index] = {**entry, **item}
                replaced = True
                break
        if not replaced:
            items.append(item)
        payload["items"] = items
        self.store.upsert("reports", key, payload)
        if self.run_protocol_service is not None and not replaced and str(item.get("status") or "") == "pending":
            try:
                run = self.run_service.get_run(run_id)
                self.run_protocol_service.append_event(
                    run_id=run_id,
                    workspace_id=run.workspace_id,
                    session_id=run.session_id,
                    event_type="approval_requested",
                    status="blocked",
                    message=str(item.get("summary") or item.get("kind") or "Approval requested."),
                    payload={"approval": item},
                    source_event_type="approval_requested",
                )
            except Exception:
                pass

    def _approval_by_id(self, run_id: str, approval_id: str) -> dict[str, Any] | None:
        for item in self.approvals(run_id).get("items") or []:
            if isinstance(item, dict) and item.get("approval_id") == approval_id:
                return item
        return None

    def _stored_tool_events(self, run_id: str) -> list[dict[str, Any]]:
        payload = self.store.get("reports", f"tool_events:{run_id}") or {}
        return [item for item in payload.get("items") or [] if isinstance(item, dict)]

    def _timeline_from_activity(self, event: dict[str, Any]) -> dict[str, Any]:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        kind = self._timeline_kind(str(event.get("type") or details.get("phase") or "tool"))
        return self._timeline_item(
            kind,
            str(event.get("status") or details.get("status") or "completed"),
            str(event.get("message") or details.get("summary") or details.get("reason") or kind),
            {
                "tool": event.get("tool") or details.get("tool"),
                "risk": details.get("risk"),
                "affected_files": details.get("targets") or details.get("changed_files") or [],
                "duration_ms": event.get("duration_ms") or event.get("elapsed_ms") or details.get("duration_ms"),
                "artifact_ref": event.get("artifact_ref") or details.get("artifact_ref"),
                "worker_id": event.get("worker_id") or details.get("worker_id"),
                "raw": event,
            },
            created_at=str(event.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _timeline_item(kind: str, status: str, title: str, payload: dict[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
        return {
            "kind": kind,
            "status": status,
            "title": title,
            "payload": payload,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _timeline_kind(raw: str) -> str:
        lowered = raw.lower()
        if "policy" in lowered:
            return "policy"
        if "approval" in lowered:
            return "approval"
        if "worker" in lowered:
            return "worker"
        if "check" in lowered:
            return "checks"
        if "browser" in lowered or "preview" in lowered:
            return "browser"
        if "patch" in lowered or "edit" in lowered or "write" in lowered:
            return "editing"
        if "read" in lowered or "search" in lowered:
            return "reading"
        if "apply" in lowered:
            return "apply"
        return "planning"

    @staticmethod
    def _worker_scope(worker_id: str) -> str:
        canonical = canonical_worker_id(worker_id)
        return {
            "backend_api_worker": "Backend API and shared persistence",
            "client_surface_worker": "Client role UI",
            "specialist_surface_worker": "Specialist role UI",
            "manager_surface_worker": "Manager role UI",
            "test_verifier_worker": "Generated tests and verification",
            "mobile_polish_worker": "Mobile polish and visual QA",
            "repair_worker": "Focused owned repair",
        }.get(canonical, canonical)

    @staticmethod
    def _path_owned_by_worker(worker_id: str, path: str) -> bool:
        worker_id = legacy_worker_id(worker_id)
        normalized = str(path or "").replace("\\", "/")
        if worker_id == "planner":
            return normalized.startswith("docs/") or normalized.endswith("README.md")
        if worker_id == "backend_api":
            return normalized.startswith("miniapp/app/") and "/static/" not in normalized
        if worker_id in {"client_ui", "specialist_ui", "manager_ui"}:
            role = worker_id.removesuffix("_ui")
            return f"/static/{role}/" in normalized or normalized.startswith(f"miniapp/app/static/{role}/")
        if worker_id == "generated_tests":
            return "test" in normalized
        return False

    @staticmethod
    def _worker_status(worker_id: str, run: Any, summaries: list[dict[str, Any]], merge_reports: list[dict[str, Any]], mailbox_workers: Any = None) -> str:
        canonical = canonical_worker_id(worker_id)
        if summaries or merge_reports:
            if any(str(item.get("status") or "") == "failed" for item in [*summaries, *merge_reports]):
                return "failed"
            if any(str(item.get("status") or "") in {"changes_ready", "merged"} for item in [*summaries, *merge_reports]):
                return "merged"
            return "completed"
        for worker in mailbox_workers if isinstance(mailbox_workers, list) else []:
            if isinstance(worker, dict) and canonical_worker_id(str(worker.get("worker") or worker.get("worker_id") or "")) == canonical:
                status = str(worker.get("status") or "")
                if status == "available_disabled":
                    return status
                if status:
                    return status
        return "not_started" if run.status == "completed" else "planned"

    @staticmethod
    def _worker_disabled_reason(worker_id: str, mailbox_workers: Any = None) -> str:
        canonical = canonical_worker_id(worker_id)
        for worker in mailbox_workers if isinstance(mailbox_workers, list) else []:
            if isinstance(worker, dict) and canonical_worker_id(str(worker.get("worker") or worker.get("worker_id") or "")) == canonical:
                return str(worker.get("disabled_reason") or "")
        return ""

    @staticmethod
    def _worker_artifact_run_id(run: Any) -> str:
        ref = str(getattr(run, "worker_mailbox_ref", "") or "")
        if ref.startswith("worker_mailbox:"):
            parts = ref.split(":")
            if len(parts) >= 3 and parts[-1]:
                return parts[-1]
        refs = list(getattr(run, "worker_branch_refs", []) or [])
        for item in refs:
            parts = str(item).split(":")
            if len(parts) >= 4 and parts[2]:
                return parts[2]
        return str(getattr(run, "run_id", "") or "")

    @staticmethod
    def _task_status(value: str) -> str:
        normalized = str(value or "").lower()
        if normalized in {"pending", "planned"}:
            return "planned"
        if normalized in {"in_progress", "running", "started"}:
            return "in_progress"
        if normalized in {"failed", "blocked"}:
            return "blocked"
        if normalized in {"done", "completed", "passed"}:
            return "completed"
        return normalized or "planned"

    @staticmethod
    def _owner_for_phase(phase: str) -> str:
        if phase in {"checking", "browser_verifying", "verify"}:
            return "verifier"
        if phase in {"editing", "build"}:
            return "coordinator"
        return "planner" if phase == "planning" else "coordinator"

    def _tasks_from_activity(self, run: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, event in enumerate(run.agent_activity_events or [], start=1):
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "activity")
            detail_status = event.get("details", {}).get("status") if isinstance(event.get("details"), dict) else None
            status = self._task_status(str(event.get("status") or detail_status or "completed"))
            items.append(
                {
                    "task_id": f"{run.run_id}:activity:{index}",
                    "title": str(event.get("message") or event_type),
                    "phase": str(event.get("phase") or event_type),
                    "status": status,
                    "owner": str(event.get("worker_id") or self._owner_for_phase(event_type)),
                    "files": list((event.get("details") or {}).get("changed_files") or []) if isinstance(event.get("details"), dict) else [],
                    "proof": {},
                    "blocker": None if status != "blocked" else event.get("message"),
                    "artifact_refs": {"artifact": event.get("artifact_ref")},
                    "updated_at": event.get("created_at"),
                }
            )
        return items[-80:]

    def _background_tasks_for_run(self, run: Any) -> list[dict[str, Any]]:
        if self.background_task_service is None:
            return []
        tasks = self.background_task_service.real_tasks_for_run(run.run_id)
        items: list[dict[str, Any]] = []
        for task in tasks:
            status = str(task.get("status") or "queued")
            items.append(
                {
                    "task_id": str(task.get("task_id") or ""),
                    "title": str(task.get("title") or task.get("type") or "Background task"),
                    "phase": str(task.get("type") or "background_task"),
                    "status": self._task_status(status),
                    "owner": str(task.get("owner") or "agent"),
                    "files": [],
                    "proof": task.get("linked_refs") or {},
                    "blocker": task.get("error") if status in {"failed", "blocked"} else None,
                    "artifact_refs": {
                        "background_task": str(task.get("task_id") or ""),
                        "run": str(task.get("run_id") or ""),
                    },
                    "updated_at": task.get("updated_at"),
                    "source": "background",
                    "background_status": status,
                    "attempt": task.get("attempt"),
                    "max_attempts": task.get("max_attempts"),
                    "output_summary": task.get("output_summary"),
                    "linked_refs": task.get("linked_refs") or {},
                }
            )
        return items

    @staticmethod
    def _filter_diff(diff: str, paths: list[str]) -> str:
        if not paths:
            return ""
        active = False
        chunks: list[str] = []
        path_set = set(paths)
        for line in str(diff or "").splitlines():
            if line.startswith("diff --git "):
                active = any(path in line for path in path_set)
            if active:
                chunks.append(line)
        return "\n".join(chunks)

    @staticmethod
    def _paths_from_diff(diff: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            candidate = line.rsplit(" b/", 1)[-1].strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            if candidate:
                paths.append(candidate)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _normalize_file_list(files: Any) -> list[str]:
        normalized: list[str] = []
        for item in files if isinstance(files, list) else []:
            path = str(item or "").strip().replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if not path or path.startswith("/") or ".." in Path(path).parts:
                raise ValueError("File paths must stay within the workspace.")
            normalized.append(path)
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _file_category(path: str) -> str:
        normalized = str(path or "").replace("\\", "/")
        if normalized.startswith("miniapp/app/generated/"):
            return "generated_manifest"
        if normalized.startswith("miniapp/app/static/client/"):
            return "client_ui"
        if normalized.startswith("miniapp/app/static/specialist/"):
            return "specialist_ui"
        if normalized.startswith("miniapp/app/static/manager/"):
            return "manager_ui"
        if "test" in normalized:
            return "tests"
        if normalized.endswith((".css", ".scss")):
            return "styles"
        if normalized.startswith("miniapp/app/"):
            return "backend"
        return "other"

    @staticmethod
    def _collect_values(payload: Any, *, keys: tuple[str, ...]) -> list[Any]:
        found: list[Any] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in keys:
                        if isinstance(nested, list):
                            found.extend(nested)
                        else:
                            found.append(nested)
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return found[:50]

    def _memory_stale_check(self, workspace_id: str, memory: dict[str, Any]) -> dict[str, Any]:
        try:
            source_dir = self.workspace_service.source_dir(workspace_id)
        except Exception:
            return {"status": "unknown", "items": []}
        items: list[dict[str, Any]] = []
        stale = False
        for item in memory.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            paths = sorted(set(re.findall(r"\bminiapp/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b", text)))[:12]
            routes = sorted(set(re.findall(r"(?<![A-Za-z0-9_])/(?:client|specialist|manager|api)[A-Za-z0-9_./{}:-]*", text)))[:12]
            path_checks = [{"path": path, "exists": (source_dir / path).exists()} for path in paths]
            route_checks = [{"route": route, "present_in_source": self._text_exists_in_workspace(source_dir, route)} for route in routes]
            item_stale = any(not check["exists"] for check in path_checks) or (
                bool(route_checks) and not any(check["present_in_source"] for check in route_checks)
            )
            stale = stale or item_stale
            items.append(
                {
                    "memory_id": item.get("memory_id"),
                    "status": "stale" if item_stale else "fresh_or_unreferenced",
                    "paths": path_checks,
                    "routes": route_checks,
                }
            )
        return {"status": "stale" if stale else "fresh", "items": items}

    def _memory_secret_scan(self, text: str) -> dict[str, Any]:
        redacted = self.exec_policy_service.redact(text)
        if redacted != text:
            return {
                "status": "blocked",
                "blocking": True,
                "issue": "secret_like_material",
                "redacted_preview": redacted[:160],
            }
        return {"status": "passed", "blocking": False}

    @staticmethod
    def _text_exists_in_workspace(source_dir: Path, needle: str) -> bool:
        if not needle:
            return False
        root = source_dir / "miniapp"
        if not root.exists():
            return False
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs", ".html", ".css", ".json"}:
                continue
            try:
                if needle in path.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
        return False

    def _document_skills(self) -> list[dict[str, Any]]:
        roots = [self.settings.runtime_dir / "platform-docs", self.settings.template_dir / "docs"]
        skills: list[dict[str, Any]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.md"))[:80]:
                text = path.read_text(encoding="utf-8", errors="ignore")
                title = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
                skill_id = re_slug(path.relative_to(root).with_suffix("").as_posix())
                skills.append(
                    {
                        "id": skill_id,
                        "name": title[:80],
                        "source": str(path.relative_to(self.settings.repo_root)) if path.is_relative_to(self.settings.repo_root) else str(path),
                        "activation": "llm_planning_only",
                        "constraints": [line.strip("- ").strip() for line in text.splitlines() if line.strip().startswith("-")][:6],
                        "validation_hints": [],
                    }
                )
        return skills

    def _load_plugin_manifests(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        roots = [self.settings.runtime_dir / "plugins", self.settings.data_dir / "plugins"]
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("plugin.json")):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(manifest, dict) or not manifest.get("id") or not manifest.get("version"):
                    continue
                items.append({**manifest, "status": "installed", "source": str(path)})
        for key, payload in self.store.items("reports"):
            if key.startswith("plugin:") and isinstance(payload, dict):
                manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
                items.append({**manifest, "status": payload.get("status", "registered"), "source": "state"})
        return items

    def _mcp_config(self) -> dict[str, Any]:
        candidates = [self.settings.data_dir / "mcp.json", self.settings.repo_root / "mcp.json"]
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return {"servers": [], "tools": []}

    @staticmethod
    def _ripgrep_search(root: Path, query: str) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                ["rg", "--line-number", "--no-heading", "--color", "never", "--", query, str(root)],
                text=True,
                capture_output=True,
                timeout=6,
            )
        except Exception:
            return []
        if result.returncode not in {0, 1}:
            return []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for raw_line in result.stdout.splitlines()[:400]:
            parts = raw_line.split(":", 2)
            if len(parts) != 3:
                continue
            path_text, line_text, snippet = parts
            try:
                relative_path = Path(path_text).resolve().relative_to(root.resolve()).as_posix()
            except Exception:
                continue
            if ".." in Path(relative_path).parts:
                continue
            try:
                line_number = int(line_text)
            except ValueError:
                line_number = 0
            grouped.setdefault(relative_path, []).append({"line": line_number, "text": snippet[:240]})
        return [{"path": path, "hits": hits[:5]} for path, hits in list(grouped.items())[:80]]

    @staticmethod
    def _rank_search_items(items: list[dict[str, Any]], query: str, root: Path) -> list[dict[str, Any]]:
        query_lower = query.lower()
        ranked: list[dict[str, Any]] = []
        for item in items:
            path = str(item.get("path") or "")
            hits = item.get("hits") if isinstance(item.get("hits"), list) else []
            score = len(hits) * 10
            if query_lower in path.lower():
                score += 25
            if path.endswith((".py", ".js", ".ts", ".tsx", ".html", ".css")):
                score += 3
            ranked.append({**item, "score": score, "language": WorkbenchService._language_for_path(path), "symbols": WorkbenchService._symbols_for_file(root / path)[:12]})
        return sorted(ranked, key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))[:80]

    @staticmethod
    def _symbol_overview(root: Path, query: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        query_lower = query.lower()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".ts", ".tsx"}:
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(part in {".git", "node_modules", "dist", "build", "__pycache__"} for part in Path(relative).parts):
                continue
            for symbol in WorkbenchService._symbols_for_file(path):
                if query_lower and query_lower not in symbol["name"].lower() and query_lower not in relative.lower():
                    continue
                symbols.append({"path": relative, **symbol})
                if len(symbols) >= 100:
                    return symbols
        return symbols

    @staticmethod
    def _symbols_for_file(path: Path) -> list[dict[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        patterns = [
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)", re.M)),
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)", re.M)),
            ("const", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=", re.M)),
            ("python_function", re.compile(r"^\s*def\s+([A-Za-z0-9_]+)\s*\(", re.M)),
            ("python_class", re.compile(r"^\s*class\s+([A-Za-z0-9_]+)\s*[:(]", re.M)),
        ]
        symbols: list[dict[str, Any]] = []
        line_starts = [0]
        for match in re.finditer("\n", text):
            line_starts.append(match.end())
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                line = 1
                for index, start in enumerate(line_starts):
                    if start > match.start():
                        break
                    line = index + 1
                symbols.append({"kind": kind, "name": match.group(1), "line": line})
                if len(symbols) >= 50:
                    return symbols
        return symbols

    @staticmethod
    def _language_for_path(path: str) -> str:
        suffix = Path(path).suffix.lower()
        return {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".html": "html",
            ".css": "css",
        }.get(suffix, "text")

    @staticmethod
    def _trace_why(run: RunRecord, artifacts: dict[str, Any]) -> str:
        if run.failure_reason:
            return f"Run is focused on resolving: {run.failure_reason}"
        if run.summary:
            return run.summary
        plan = artifacts.get("implementation_plan") or run.implementation_plan or {}
        if isinstance(plan, dict) and plan.get("summary"):
            return str(plan["summary"])
        return run.prompt[:500]

    def _check(self, name: str, ok: bool, details: str = "", command: str | None = None, *, required: bool = True) -> dict[str, Any]:
        return {"name": name, "status": "passed" if ok else "failed", "details": details, "command": command, "required": required}

    def _binary_check(self, binary: str) -> dict[str, Any]:
        path = shutil.which(binary)
        return self._check(binary, bool(path), path or f"{binary} not found", binary, required=binary in {"node", "npm"})

    def _compose_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("docker_compose", False, "docker not found", "docker compose version", required=False)
        try:
            result = subprocess.run([docker, "compose", "version"], text=True, capture_output=True, timeout=5)
            return self._check("docker_compose", result.returncode == 0, (result.stdout or result.stderr).strip(), "docker compose version", required=False)
        except Exception as exc:
            return self._check("docker_compose", False, str(exc), "docker compose version", required=False)

    def _playwright_check(self) -> dict[str, Any]:
        try:
            import playwright  # noqa: F401

            return self._check("playwright", True, "Python package import succeeded", "python -c 'import playwright'", required=False)
        except Exception as exc:
            return self._check("playwright", False, str(exc), "python -c 'import playwright'", required=False)

    def _openai_check(self) -> dict[str, Any]:
        config = self.openai_client.configuration()
        return self._check("openai", bool(config.get("enabled")), "configured" if config.get("enabled") else "not configured", required=False)

    def _writable_check(self, name: str, path: Path) -> dict[str, Any]:
        return self._check(name, os.access(path, os.W_OK), str(path), required=True)

    def _template_check(self) -> dict[str, Any]:
        required = [self.settings.template_dir / "miniapp" / "app" / "main.py", self.settings.template_dir / "docker" / "docker-compose.yml"]
        missing = [str(path) for path in required if not path.exists()]
        return self._check("template_integrity", not missing, "missing: " + ", ".join(missing) if missing else str(self.settings.template_dir), required=True)

    def _port_check(self) -> dict[str, Any]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = sock.connect_ex(("127.0.0.1", int(self.settings.preview_port_base)))
            return self._check("preview_port_base", result != 0, f"port {self.settings.preview_port_base} {'available' if result != 0 else 'in use'}", required=False)
        finally:
            sock.close()

    def _backend_routes_check(self) -> dict[str, Any]:
        route_file = self.settings.repo_root / "platform" / "backend" / "app" / "api" / "routes_workbench.py"
        if not route_file.exists():
            return self._check("backend_routes", False, f"missing {route_file}", required=True)
        text = route_file.read_text(encoding="utf-8", errors="ignore")
        required_routes = ["/doctor", "/runs/{run_id}/timeline", "/runs/{run_id}/approvals", "/workspaces/{workspace_id}/files/search"]
        missing = [route for route in required_routes if route not in text]
        return self._check(
            "backend_routes",
            not missing,
            "registered" if not missing else "missing: " + ", ".join(missing),
            required=True,
        )

    def _stale_backend_check(self) -> dict[str, Any]:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=1.0)
            conn.request("GET", "/doctor")
            response = conn.getresponse()
            body = response.read(240).decode("utf-8", errors="ignore")
            conn.close()
            ok = response.status < 500
            details = f"127.0.0.1:8000 returned {response.status}; {body[:120]}"
            return self._check("stale_backend_port_8000", ok, details, "GET http://127.0.0.1:8000/doctor", required=False)
        except Exception as exc:
            return self._check("stale_backend_port_8000", True, f"no conflicting backend detected ({exc})", required=False)

    def _playwright_browsers_check(self) -> dict[str, Any]:
        try:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run"], text=True, capture_output=True, timeout=10)
            output = (result.stdout or result.stderr).strip()
            return self._check(
                "playwright_browsers",
                result.returncode == 0,
                output[:600] or "dry run completed",
                "python -m playwright install --dry-run",
                required=False,
            )
        except Exception as exc:
            return self._check("playwright_browsers", False, str(exc), "python -m playwright install --dry-run", required=False)

    def _preview_container_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("preview_containers", True, "docker not available; skipped", "docker ps", required=False)
        try:
            result = subprocess.run([docker, "ps", "--format", "{{.Names}}"], text=True, capture_output=True, timeout=5)
            names = [line for line in result.stdout.splitlines() if "grounded" in line or "miniapp" in line or "preview" in line]
            return self._check(
                "preview_containers",
                result.returncode == 0,
                ", ".join(names[:12]) if names else "no matching preview containers",
                "docker ps --format '{{.Names}}'",
                required=False,
            )
        except Exception as exc:
            return self._check("preview_containers", False, str(exc), "docker ps --format '{{.Names}}'", required=False)

    def _test_command_check(self) -> dict[str, Any]:
        return self._check("platform_tests", (self.settings.repo_root / "platform" / "backend" / "tests").exists(), "pytest platform/backend/tests", required=True)

    @staticmethod
    def _builtin_skills() -> dict[str, dict[str, Any]]:
        return SkillPackCatalog.builtin()
