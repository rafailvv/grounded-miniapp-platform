from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
from typing import Any
from uuid import uuid4

from app.ai.model_registry import model_capabilities
from app.models.domain import CheckExecutionRecord, CreateRunRequest, RunCheckResult, RunRecord
from app.models.context_pressure import ContextPressureReport
from app.models.memory import MemoryConsolidationReport, MemoryRetrievalRequest
from app.models.observability import ObservabilityReport
from app.models.prompt_suggestions import PromptSuggestionsReport
from app.models.webhooks import WebhookCreateRequest, WebhookUpdateRequest
from app.models.workbench import (
    GateReport,
    RepairAttemptsReport,
    RepairCase,
    RepairCasesReport,
    RunBookmarksReport,
    RunEventsReport,
    RunProtocolReport,
    RunTimelineReport,
    RunTraceViewReport,
    ToolEventsReport,
    TraceBundleReport,
    TraceState,
    WorkbenchApiModel,
)
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.modules.miniapp_agent_loop.guardian_review import GuardianReview
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.product_workers import (
    PRODUCT_WORKERS,
    canonical_worker_id,
    ownership_for_worker,
    worker_refs,
)
from app.repositories.platform_db import PlatformDb
from app.services.background_task_service import BackgroundTaskService
from app.services.event_journal import EventJournalService
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
from app.services.skill_registry import SkillRegistryService
from app.services.miniapp_contract import MiniAppContractMaterializer, MiniAppRouteRegistry
from app.services.repair_catalog import RepairCatalog
from app.services.repair_cases import RepairCaseService
from app.services.run_state_machine import RunStateMachine
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, tool_envelope, tool_registry_contract
from app.modules.miniapp_agent_loop.tool_router import ToolRouter
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.output_artifact_service import OutputArtifactService
from app.services.prompt_suggestions import PromptSuggestionService
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

    @staticmethod
    def _typed_payload(model: type[WorkbenchApiModel], payload: dict[str, Any]) -> dict[str, Any]:
        return model.model_validate(payload).model_dump(mode="json", by_alias=True)

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
        repair_case_service: RepairCaseService | None = None,
        event_journal_service: EventJournalService | None = None,
        output_artifact_service: OutputArtifactService | None = None,
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
        self.event_journal_service = event_journal_service
        self.output_artifact_service = output_artifact_service
        self.repair_case_service = repair_case_service or RepairCaseService(store, event_journal_service=event_journal_service)
        self.prompt_suggestion_service = PromptSuggestionService()

    def list_webhooks(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        if workspace_id:
            self.workspace_service.get_workspace(workspace_id)
        items = [
            self._public_webhook(record)
            for record in self.store.list("webhooks")
            if not workspace_id or record.get("workspace_id") == workspace_id
        ]
        items.sort(key=lambda item: str(item.get("created_at") or ""))
        return {"schema": "grounded.webhooks.v1", "status": "ok", "workspace_id": workspace_id, "items": items}

    def create_webhook(self, request: WebhookCreateRequest, *, idempotency_key: str | None = None) -> dict[str, Any]:
        if request.workspace_id:
            self.workspace_service.get_workspace(request.workspace_id)
        if idempotency_key:
            existing = self._webhook_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._public_webhook(existing)
        now = datetime.now(timezone.utc).isoformat()
        webhook_id = f"wh_{uuid4().hex[:16]}"
        record: dict[str, Any] = {
            "schema": "grounded.webhook.subscription.v1",
            "webhook_id": webhook_id,
            "url": request.url,
            "events": list(request.events),
            "workspace_id": request.workspace_id,
            "enabled": request.enabled,
            "description": request.description,
            "metadata": self._webhook_safe_metadata(request.metadata),
            "secret_configured": bool(request.secret),
            "created_at": now,
            "updated_at": now,
        }
        if request.secret:
            record["secret_sha256"] = hashlib.sha256(request.secret.encode("utf-8")).hexdigest()
        if idempotency_key:
            record["idempotency_key"] = idempotency_key
        self.store.upsert("webhooks", webhook_id, record)
        return self._public_webhook(record)

    def get_webhook(self, webhook_id: str) -> dict[str, Any]:
        record = self.store.get("webhooks", webhook_id)
        if record is None:
            raise KeyError(f"Webhook not found: {webhook_id}")
        return self._public_webhook(record)

    def update_webhook(self, webhook_id: str, request: WebhookUpdateRequest) -> dict[str, Any]:
        record = self.store.get("webhooks", webhook_id)
        if record is None:
            raise KeyError(f"Webhook not found: {webhook_id}")
        updated = dict(record)
        if request.workspace_id is not None:
            self.workspace_service.get_workspace(request.workspace_id)
            updated["workspace_id"] = request.workspace_id
        if request.url is not None:
            updated["url"] = request.url
        if request.events is not None:
            updated["events"] = list(request.events)
        if request.enabled is not None:
            updated["enabled"] = request.enabled
        if request.description is not None:
            updated["description"] = request.description
        if request.metadata is not None:
            updated["metadata"] = self._webhook_safe_metadata(request.metadata)
        if request.secret is not None:
            updated["secret_configured"] = bool(request.secret)
            if request.secret:
                updated["secret_sha256"] = hashlib.sha256(request.secret.encode("utf-8")).hexdigest()
            else:
                updated.pop("secret_sha256", None)
        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("webhooks", webhook_id, updated)
        return self._public_webhook(updated)

    def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
        record = self.store.get("webhooks", webhook_id)
        if record is None:
            raise KeyError(f"Webhook not found: {webhook_id}")
        self.store.delete("webhooks", webhook_id)
        return {"schema": "grounded.webhook.deleted.v1", "webhook_id": webhook_id, "deleted": True}

    def test_webhook(self, webhook_id: str, *, event_type: str = "webhook.test", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.store.get("webhooks", webhook_id)
        if record is None:
            raise KeyError(f"Webhook not found: {webhook_id}")
        now = datetime.now(timezone.utc).isoformat()
        delivery = {
            "schema": "grounded.webhook.delivery.v1",
            "webhook_id": webhook_id,
            "event_type": event_type or "webhook.test",
            "status": "simulated",
            "simulated": True,
            "delivered_at": now,
            "target_url": record.get("url"),
            "payload_preview": self._webhook_safe_metadata(payload or {}) if isinstance(payload, dict) else {},
        }
        updated = {**record, "last_delivery": delivery, "updated_at": now}
        self.store.upsert("webhooks", webhook_id, updated)
        return delivery

    def _webhook_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        for record in self.store.list("webhooks"):
            if record.get("idempotency_key") == key:
                return record
        return None

    @staticmethod
    def _public_webhook(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key not in {"secret", "secret_sha256", "idempotency_key"}
        }

    @staticmethod
    def _webhook_safe_metadata(payload: dict[str, Any]) -> dict[str, Any]:
        def check_value(key: str, value: Any) -> Any:
            if re.search(r"(secret|token|password|api[_-]?key|authorization)", key, re.IGNORECASE):
                raise ValueError(f"Webhook metadata contains secret-like key: {key}")
            if isinstance(value, str) and re.search(r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|xox[baprs]-|AKIA[0-9A-Z]{12,})", value):
                raise ValueError("Webhook metadata contains secret-like value.")
            if isinstance(value, dict):
                return {str(nested_key): check_value(str(nested_key), nested_value) for nested_key, nested_value in value.items()}
            if isinstance(value, list):
                return [check_value(key, item) for item in value[:50]]
            return value

        return {str(key): check_value(str(key), value) for key, value in dict(payload or {}).items()}

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
        return self._typed_payload(ToolEventsReport, {"run_id": run_id, "tool_protocol_version": TOOL_PROTOCOL_VERSION, "events": events})

    def run_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        items = self.platform_db.list_run_events(run_id, after_sequence=after_sequence, limit=limit) if self.platform_db is not None else []
        snapshots = self.platform_db.list_run_state_snapshots(run_id, limit=20) if self.platform_db is not None else []
        protocol_events = self.run_protocol_service.protocol_events(run_id).get("items", []) if self.run_protocol_service is not None else []
        return self._typed_payload(RunEventsReport, {
            "run_id": run_id,
            "schema": "grounded.run_events.v1",
            "status": "ok",
            "blocking": False,
            "items": items,
            "protocol_events": protocol_events,
            "compaction_events": [item for item in protocol_events if item.get("type") == "compact_boundary"],
            "state_snapshots": snapshots,
            "next_sequence": max([int(item.get("sequence") or 0) for item in items], default=int(after_sequence or 0)),
        })

    def protocol(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.run_protocol_service is None:
            return self._typed_payload(
                RunProtocolReport,
                {"schema": "grounded.run_protocol.v1", "run_id": run_id, "workspace_id": run.workspace_id, "status": "unavailable", "items": []},
            )
        payload = self.run_protocol_service.protocol_events(run_id)
        bookmarks = self.run_protocol_service.bookmarks(run_id)
        payload["workspace_id"] = run.workspace_id
        payload["bookmarks"] = bookmarks.get("items") or []
        payload["latest_bookmark"] = (bookmarks.get("items") or [None])[0]
        return self._typed_payload(RunProtocolReport, payload)

    def bookmarks(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        if self.run_protocol_service is None:
            return self._typed_payload(RunBookmarksReport, {"schema": "grounded.run_bookmarks.v1", "run_id": run_id, "status": "unavailable", "items": []})
        return self._typed_payload(RunBookmarksReport, self.run_protocol_service.bookmarks(run_id))

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
        return self._typed_payload(RunTraceViewReport, payload)

    def record_tool_event(self, run_id: str | None, event: dict[str, Any]) -> dict[str, Any]:
        if not run_id:
            return event
        self.run_service.get_run(run_id)
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": datetime.now(timezone.utc).isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)
        self._journal_run_event(
            run_id,
            self._tool_journal_event_type(item),
            item,
            actor=f"tool:{item.get('tool') or 'unknown'}",
            summary=str(item.get("tool") or "Tool event"),
            idempotency_key=f"tool:{run_id}:{item.get('tool_call_id') or item.get('sequence')}",
        )
        return item

    def evaluate_command_for_run(self, run_id: str, command: str, *, preset: str = "safe_auto") -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        root = self.workspace_service.draft_source_dir(run.workspace_id, run_id) if self.workspace_service.draft_exists(run.workspace_id, run_id) else self.workspace_service.source_dir(run.workspace_id)
        evaluation = self.exec_policy_service.evaluate_command(command, preset=preset, root=root)
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
        return self._typed_payload(RunTimelineReport, {
            "run_id": run_id,
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "items": [
                {**item, "sequence": index + 1}
                for index, item in enumerate(sorted(items, key=lambda item: str(item.get("created_at") or "")))
            ],
        })

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
            task = real_tasks.get(canonical) or {}
            status = self._worker_status(canonical, run, summaries, merge_reports, mailbox_workers)
            if isinstance(output, dict) and output.get("status"):
                status = str(output.get("status"))
            if isinstance(decision, dict) and decision.get("decision") in {"accepted", "rejected", "needs_repair"}:
                status = {"accepted": "merged", "rejected": "rejected", "needs_repair": "blocked"}[str(decision.get("decision"))]
            lanes.append(
                {
                    "worker_id": canonical,
                    "worker_type": canonical,
                    "alias_ids": [],
                    "branch_role": AgentWorkerManager.branch_role(canonical),
                    "branch_stage": AgentWorkerManager.branch_stage(canonical),
                    "branch_policy": AgentWorkerManager.branch_policy(canonical),
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
            "branch_schema": "grounded.worker_branch_plan.v2",
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "workers": lanes,
            "worker_branch_refs": run.worker_branch_refs,
            "merge_decision_ref": merge_decision_ref if merge_decision else None,
            "mailbox": (mailbox or {}).get("mailbox") if isinstance(mailbox, dict) else {},
            "branch_plan": ((mailbox or {}).get("mailbox") or {}).get("execution_stages") if isinstance(mailbox, dict) else [],
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
        repair_cases = self.repair_case_service.list_cases(run_id)
        repair_tasks = [
            {
                "task_id": f"{case.get('case_id')}:repair",
                "title": str(case.get("failure_signature") or case.get("issue_code") or "Repair case"),
                "phase": "repair_case",
                "status": "blocked" if case.get("status") in {"blocked", "failed_attempt"} else str(case.get("status") or "planned"),
                "owner": "repair_worker",
                "files": list(case.get("target_files") or []),
                "proof": {"expected": case.get("expected_proof"), "case_id": case.get("case_id")},
                "blocker": case.get("likely_cause"),
                "artifact_refs": {"repair_case": RepairCaseService.case_ref(run.workspace_id, run_id, str(case.get("case_id")))},
                "source": "repair_case",
                "updated_at": case.get("updated_at"),
            }
            for case in repair_cases.get("items") or []
            if isinstance(case, dict)
        ]
        if repair_tasks:
            items = [*repair_tasks, *items]
        if not items:
            items = self._tasks_from_activity(run)
        return {"schema": "grounded.run_tasks.v1", "run_id": run_id, "workspace_id": run.workspace_id, "status": run.status, "items": items, "repair_cases": repair_cases}

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
        guardian_results: list[RunCheckResult] = []
        for item in check_results:
            try:
                guardian_results.append(RunCheckResult.model_validate(item))
            except Exception:
                continue
        guardian_execution = (
            CheckExecutionRecord(
                workspace_id=run.workspace_id,
                run_id=run_id,
                changed_files=changed_files,
                results=guardian_results,
                completed_at=datetime.now(timezone.utc),
            )
            if guardian_results
            else None
        )
        guardian_source = self.workspace_service.draft_source_dir(run.workspace_id, run_id)
        if not guardian_source.exists():
            guardian_source = self.workspace_service.source_dir(run.workspace_id)
        guardian_report = GuardianReview.review(
            workspace_id=run.workspace_id,
            run_id=run_id,
            draft_source=guardian_source,
            changed_files=changed_files,
            latest_execution=guardian_execution,
            preview_details=artifacts.get("preview") if isinstance(artifacts.get("preview"), dict) else {},
            acceptance_contract=run.acceptance_contract,
            implementation_plan=run.implementation_plan,
            target_role_scope=run.target_role_scope,
            intent=run.intent,
            source="manual_review",
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", f"guardian_review:{run.workspace_id}:{run_id}", guardian_report)
        for finding in guardian_report.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            findings.append(
                self._review_finding(
                    code=str(finding.get("code") or "guardian_blocker"),
                    message=str(finding.get("message") or "Guardian review found a blocker."),
                    severity=str(finding.get("severity") or "high"),
                    category=str(finding.get("category") or "product_contract"),
                    source="guardian_review",
                    file_path=finding.get("file_path"),
                    line=finding.get("line"),
                    evidence=dict(finding.get("evidence") or {}),
                    blocker=bool(finding.get("is_blocker_for_apply", True)),
                )
            )
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
        repair_cases = self.repair_case_service.sync_from_review(run=run, findings=findings)
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
            "repair_cases": repair_cases,
            "evidence": {
                "diff_available": bool(artifacts.get("diff")),
                "changed_files": changed_files,
                "checks": check_results,
                "browser_proof_ref": run.browser_proof_ref,
                "verifier_review_ref": run.verifier_review_ref,
                "guardian_review_ref": f"guardian_review:{run.workspace_id}:{run_id}",
                "guardian_review": guardian_report,
                "acceptance_contract_required": acceptance_required,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"review:{run_id}", payload)
        return payload

    def prompt_suggestions(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        report = self.prompt_suggestion_service.build(run, artifacts)
        payload = report.model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", f"prompt_suggestions:{run_id}", payload)
        return PromptSuggestionsReport.model_validate(payload).model_dump(mode="json", by_alias=True)

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
            return self._typed_payload(TraceBundleReport, {
                "schema": "grounded.trace_bundle.v1",
                "run_id": run_id,
                "workspace_id": run.workspace_id,
                "status": "missing",
                "event_count": 0,
                "state": {},
            })
        state = payload.get("state")
        if not isinstance(state, dict):
            state = self.trace_bundle_state(run_id)
            payload = {**payload, "state": state}
            self.store.upsert("reports", ref, payload)
        return self._typed_payload(TraceBundleReport, payload)

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
        return self._typed_payload(TraceState, self._augment_trace_state_with_protocol(run_id, state))

    def _augment_trace_state_with_protocol(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if self.run_protocol_service is None:
            return self._typed_payload(TraceState, state)
        protocol = self.run_protocol_service.protocol_events(run_id).get("items") or []
        if not protocol:
            return self._typed_payload(TraceState, state)
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
        return self._typed_payload(TraceState, {
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
        })

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
        if acceptance_required and (diff_text.strip() or run.touched_files) and not self._has_product_runtime_change(diff_text, run.touched_files):
            add_issue(
                "product_source_diff",
                "meaningful_product_diff",
                "Acceptance run changed only generated tests or contract metadata; product runtime source must change.",
                evidence={"touched_files": run.touched_files or [], "diff_paths": self._paths_from_diff(diff_text)},
            )
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
        repair_packets = self._normalize_uncatalogued_repair_packets(run, issues, repair_packets)
        include_checkpoint_packets = bool(blocking or run.status not in {"completed", "awaiting_approval"})
        if checkpoint_packets and include_checkpoint_packets:
            repair_packets = [*checkpoint_packets, *repair_packets]
        try:
            trace_state = self.trace_bundle_state(run_id)
        except Exception:
            trace_state = {}
        repair_cases = self.repair_case_service.sync_from_packets(
            workspace_id=run.workspace_id,
            run_id=run_id,
            packets=repair_packets,
            source="gate",
            trace_state=trace_state if isinstance(trace_state, dict) else {},
        ) if repair_packets else self.repair_case_service.list_cases(run_id)
        repair_packets = RepairCaseService.enrich_packets(repair_packets, repair_cases)
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
            "repair_cases": repair_cases,
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
                "repair_cases": RepairCaseService.index_ref(run_id),
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
        digest = hashlib.sha256(
            json.dumps(
                {
                    "status": payload.get("status"),
                    "blocking": payload.get("blocking"),
                    "issues": [
                        {
                            "kind": item.get("kind"),
                            "check": item.get("check"),
                            "details": item.get("details"),
                        }
                        for item in payload.get("issues") or []
                        if isinstance(item, dict)
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        self._journal_run_event(
            run_id,
            "gate.evaluated",
            {
                "run_id": run_id,
                "workspace_id": run.workspace_id,
                "status": payload.get("status"),
                "blocking": payload.get("blocking"),
                "issues": payload.get("issues") or [],
                "run_state": state,
            },
            summary=f"Gate {payload.get('status')}.",
            idempotency_key=f"gate:{run_id}:{digest}",
        )
        self.run_service.reconcile_run_with_gate(run_id, payload)
        return self._typed_payload(GateReport, payload)

    @classmethod
    def _has_product_runtime_change(cls, diff_text: str, touched_files: list[str] | None) -> bool:
        paths = [*cls._paths_from_diff(str(diff_text or "")), *[str(path) for path in (touched_files or [])]]
        return any(cls._is_product_runtime_source_path(path) for path in paths)

    @staticmethod
    def _is_product_runtime_source_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized.startswith("miniapp/"):
            return False
        path = PurePosixPath(normalized)
        if any(part in {"__pycache__", "node_modules", "dist", "build", ".cache"} for part in path.parts):
            return False
        if normalized.startswith("miniapp/tests/") or normalized.startswith("miniapp/app/generated/"):
            return False
        if normalized.endswith((".pyc", ".pyo", ".tsbuildinfo")):
            return False
        return normalized.startswith(("miniapp/app/", "miniapp/requirements.txt", "miniapp/Dockerfile"))

    def repair_signatures(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        gate = self.gate(run_id)
        explicit = [item for item in run.repair_issue_signatures if isinstance(item, dict)]
        packets = RepairCatalog.classify_many([*explicit, *gate.get("issues", [])])
        packets = self._normalize_uncatalogued_repair_packets(run, [*explicit, *gate.get("issues", [])], packets)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        checkpoint_packets = list((checkpoint or {}).get("repair_packets") or []) if isinstance(checkpoint, dict) else []
        include_checkpoint_packets = bool(gate.get("blocking") or run.status not in {"completed", "awaiting_approval"})
        if checkpoint_packets and include_checkpoint_packets:
            packets = [*checkpoint_packets, *packets]
        repair_cases = self.repair_case_service.sync_from_packets(
            workspace_id=run.workspace_id,
            run_id=run_id,
            packets=packets,
            source="repair_signatures",
            trace_state=self.store.get("reports", f"trace_reducer:{run.workspace_id}:{run_id}") or {},
        ) if packets else self.repair_case_service.list_cases(run_id)
        packets = RepairCaseService.enrich_packets(packets, repair_cases)
        payload = {
            "run_id": run_id,
            "status": "available" if packets else "empty",
            "blocking": bool(gate.get("blocking")),
            "items": packets,
            "repair_cases": repair_cases,
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

    def repair_cases(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        self.gate(run_id)
        return self._typed_payload(RepairCasesReport, self.repair_case_service.list_cases(run_id))

    def repair_case(self, run_id: str, case_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        case = self.repair_case_service.get_case(run_id, case_id)
        if not case:
            raise KeyError(case_id)
        return self._typed_payload(RepairCase, case)

    def repair_case_attempts(self, run_id: str, case_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        payload = self.repair_case_service.attempts(run_id, case_id)
        if payload.get("status") == "missing":
            raise KeyError(case_id)
        return self._typed_payload(RepairAttemptsReport, payload)

    def retry_repair_case(self, run_id: str, case_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        request = self.repair_case_service.retry_request(run, case_id)
        created = self.run_service.create_run(run.workspace_id, request)
        return {
            "schema": "grounded.repair_case_retry.v1",
            "status": "started",
            "run_id": run_id,
            "case_id": case_id,
            "retry_run": created.model_dump(mode="json"),
        }

    def _normalize_uncatalogued_repair_packets(
        self,
        run: RunRecord,
        issues: list[dict[str, Any]],
        packets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        refined: list[dict[str, Any]] = []
        uncatalogued_issue_iter = iter([issue for issue in issues if isinstance(issue, dict)])
        for packet in packets:
            if not self._is_uncatalogued_repair_packet(packet):
                refined.append(packet)
                continue
            issue = next(uncatalogued_issue_iter, packet)
            cache_key = self._repair_classifier_cache_key(run.run_id, issue)
            cached = self.store.get("reports", cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("packet"), dict):
                refined.append(dict(cached["packet"]))
                continue
            classified = self._uncatalogued_repair_case_packet(issue)
            self.store.upsert("reports", cache_key, {"run_id": run.run_id, "packet": classified, "status": "available", "created_at": datetime.now(timezone.utc).isoformat()})
            refined.append(classified)
        return refined

    @staticmethod
    def _is_uncatalogued_repair_packet(packet: dict[str, Any]) -> bool:
        signature = str(packet.get("signature") or "")
        code = str(packet.get("issue_code") or packet.get("code") or "")
        return "uncatalogued_repair_case" in signature or code == "uncatalogued_repair_case"

    @staticmethod
    def _repair_classifier_cache_key(run_id: str, issue: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        return f"repair_llm_classifier:{run_id}:{digest}"

    @staticmethod
    def _uncatalogued_repair_case_packet(issue: dict[str, Any]) -> dict[str, Any]:
        paths = [
            str(item).strip().replace("\\", "/")
            for item in issue.get("paths") or issue.get("target_files") or []
            if str(item).strip().replace("\\", "/").startswith("miniapp/")
        ][:8]
        check = str(issue.get("check") or issue.get("failure_class") or "checks.run")
        signature = str(issue.get("failure_signature") or issue.get("signature") or f"repair.uncatalogued_repair_case:{check}")
        return {
            "signature": signature,
            "issue_code": "uncatalogued_repair_case",
            "code": "uncatalogued_repair_case",
            "severity": str(issue.get("severity") or "high"),
            "likely_root_cause": str(issue.get("details") or issue.get("message") or "Uncatalogued failure requires exact evidence before patching."),
            "target_files": paths,
            "verification_check": check,
            "verification_command": str(issue.get("verification_command") or "run_checks"),
            "instruction": "Collect exact diagnostics for the implicated file/check, patch only the constrained slice, and rerun the failing proof.",
            "auto_fixable": True,
            "required_next_tool": "lsp.diagnostics" if paths else "semantic_scan",
            "suggested_tool_after_read": "apply_patch_to_draft_or_write_file",
            "retry_policy": "evidence_driven_repair_case",
            "retryable": True,
            "deterministic": True,
            "failure_class": str(issue.get("failure_class") or check),
            "failure_signature": signature,
            "repair_recipe_id": "repair.uncatalogued_case",
            "forbidden_tools_once": [],
            "next_forced_action": {"required_next_tool": "lsp.diagnostics" if paths else "semantic_scan", "target_files": paths, "verification_check": check},
            "expected_proof": check,
            "evidence": {"source_issue": issue},
        }

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
            "repair_cases": gate.get("repair_cases") or self.repair_case_service.list_cases(run_id),
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
        self._journal_run_event(
            run_id,
            "memory.raw_extracted",
            {"memory_ref": f"memory_stage1:{run.workspace_id}:{run_id}", "raw_count": len(payload.get("items") or [])},
            summary="Raw run memory extracted.",
            source_ref=f"memory_stage1:{run.workspace_id}:{run_id}",
            idempotency_key=f"memory.raw_extracted:{run_id}",
        )
        return payload

    def memory_pipeline(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        stage1 = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"memory_stage1:{workspace_id}:") and isinstance(payload, dict)
        ]
        consolidated = self.store.get("reports", f"memory_consolidation:{workspace_id}") or {}
        workspace_memory = self.store.get("reports", f"workspace_memory:{workspace_id}") or {"items": []}
        items = [item for item in workspace_memory.get("items") or [] if isinstance(item, dict)]
        active_count = sum(1 for item in items if item.get("status", "active") == "active")
        stale_count = sum(1 for item in items if item.get("status") == "stale")
        expired_count = sum(1 for item in items if item.get("status") == "expired" or (item.get("expiry") or {}).get("expired"))
        superseded_count = sum(1 for item in items if item.get("status") == "superseded")
        return {
            "schema": "grounded.memory_pipeline.v1",
            "workspace_id": workspace_id,
            "status": "ready" if stage1 or consolidated else "empty",
            "phase1": {
                "schema": "grounded.memory_stage1.v1",
                "batch_count": len(stage1),
                "raw_count": sum(len(payload.get("items") or []) for payload in stage1),
            },
            "phase2": {
                "schema": "grounded.workspace_memory.v2",
                "active_count": active_count,
                "stale_count": stale_count,
                "expired_count": expired_count,
                "superseded_count": superseded_count,
                "retrieval_schema": "grounded.memory_retrieval.v1",
            },
            "stage1_count": len(stage1),
            "stage1_items": sum(len(payload.get("items") or []) for payload in stage1),
            "active_count": active_count,
            "stale_count": stale_count,
            "expired_count": expired_count,
            "superseded_count": superseded_count,
            "consolidated_at": consolidated.get("updated_at") or consolidated.get("created_at"),
            "retrieval_schema": "grounded.memory_retrieval.v1",
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
        consolidated = WorkspaceMemoryPipeline.consolidate(
            workspace_id,
            stage1,
            current,
            workspace_root=self.workspace_service.source_dir(workspace_id),
        )
        consolidated["stale_check"] = WorkspaceMemoryPipeline.stale_check(self.workspace_service.source_dir(workspace_id), consolidated)
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", consolidated)
        pipeline = consolidated.get("pipeline") if isinstance(consolidated.get("pipeline"), dict) else {}
        summary = MemoryConsolidationReport(
            workspace_id=workspace_id,
            status="consolidated",
            stage1_count=len(stage1),
            raw_count=int(pipeline.get("stage1_items", 0) or 0),
            active_count=int(pipeline.get("active_count", 0) or 0),
            stale_count=int(pipeline.get("stale_count", 0) or 0),
            expired_count=int(pipeline.get("expired_count", 0) or 0),
            superseded_count=int(pipeline.get("superseded_count", 0) or 0),
            deduped_count=int(pipeline.get("deduped_count", 0) or 0),
            updated_at=datetime.now(timezone.utc).isoformat(),
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", f"memory_consolidation:{workspace_id}", summary)
        for payload in stage1:
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                continue
            self._journal_run_event(
                run_id,
                "memory.consolidated",
                {"memory_ref": f"workspace_memory:{workspace_id}", "consolidation_ref": f"memory_consolidation:{workspace_id}"},
                summary="Workspace memory consolidated.",
                source_ref=f"workspace_memory:{workspace_id}",
                idempotency_key=f"memory.consolidated:{workspace_id}:{run_id}",
            )
        return {**consolidated, "pipeline": self.memory_pipeline(workspace_id)}

    def retrieve_memory(self, workspace_id: str, payload: dict[str, Any] | MemoryRetrievalRequest) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        request = payload if isinstance(payload, MemoryRetrievalRequest) else MemoryRetrievalRequest.model_validate(payload or {})
        current = self.memory(workspace_id)
        result = WorkspaceMemoryPipeline.retrieve(
            workspace_id,
            current,
            prompt=request.prompt,
            paths=request.paths,
            top_k=request.top_k,
            include_inactive=request.include_inactive,
            failure_class=request.failure_class,
        )
        self.store.upsert("reports", f"memory_retrieval:last:{workspace_id}", result)
        return result

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
            "citations": [payload.get("citation")] if isinstance(payload.get("citation"), dict) else [],
            "status": "active",
            "source": "manual",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not item["text"]:
            raise ValueError("Memory text is required.")
        secret_scan = self._memory_secret_scan(str(item["text"]))
        if secret_scan["status"] != "passed":
            raise ValueError("Memory text appears to contain secret-like material; remove the secret before saving.")
        item["secret_scan"] = secret_scan
        item["fingerprint"] = WorkspaceMemoryPipeline._key(item)
        item["confidence"] = {"score": 0.7, "level": "medium", "signals": ["manual_entry"]}
        item["expiry"] = {"expires_at": None, "ttl_days": None, "reason": None, "expired": False}
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
        registry = SkillRegistryService(runtime_dir=self.settings.runtime_dir, repo_root=self.settings.repo_root, data_dir=self.settings.data_dir)
        prefetch = registry.prefetch()
        skills: dict[str, dict[str, Any]] = {}
        for item in prefetch.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                skills.setdefault(str(item["id"]), dict(item))
        for item in self._document_skills():
            item.setdefault("scope", "system")
            item.setdefault("scoped_id", f"system:{item['id']}")
            item.setdefault("invocationPolicy", "explicit")
            skills.setdefault(item["id"], item)
        for item in skills.values():
            item.setdefault("activation_reason", "available_metadata")
        return {
            "schema": "grounded.skills.v2",
            "prefetch": {key: value for key, value in prefetch.items() if key != "items"},
            "manifest": prefetch.get("manifest") or registry.manifest(),
            "scopes": prefetch.get("scopes") or {},
            "validation_issues": prefetch.get("validation_issues") or [],
            "items": sorted(skills.values(), key=lambda item: str(item.get("id") or "")),
        }

    def skill(self, skill_id: str) -> dict[str, Any]:
        items = list(self.skills()["items"])
        item = (
            {str(item.get("scoped_id") or ""): item for item in items}.get(skill_id)
            or {str(item.get("id") or ""): item for item in items}.get(skill_id)
        )
        if item is None:
            raise KeyError(f"Skill not found: {skill_id}")
        return item

    def skill_registry_manifest(self) -> dict[str, Any]:
        return SkillRegistryService(runtime_dir=self.settings.runtime_dir, repo_root=self.settings.repo_root, data_dir=self.settings.data_dir).manifest()

    def evaluate_skills(self, payload: dict[str, Any]) -> dict[str, Any]:
        registry = SkillRegistryService(runtime_dir=self.settings.runtime_dir, repo_root=self.settings.repo_root, data_dir=self.settings.data_dir)
        return registry.search_for_context(
            prompt=str(payload.get("prompt") or ""),
            intent=str(payload.get("intent") or "") or None,
            generation_mode=str(payload.get("generation_mode") or "") or None,
            paths=[str(item) for item in payload.get("paths") or [] if str(item).strip()],
            failure_class=str(payload.get("failure_class") or "") or None,
            max_skills=int(payload["max_skills"]) if str(payload.get("max_skills") or "").isdigit() else None,
        )

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
        return {"items": config.get("tools", []), "tool_protocol": {**tool_registry_contract(), "router": ToolRouter.manifest()}}

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
            self._python_version_check(),
            self._node_version_check(),
            self._npm_version_check(),
            self._binary_check("docker"),
            self._compose_check(),
            self._docker_daemon_check(),
            self._playwright_check(),
            self._playwright_browsers_check(),
            self._openai_check(),
            self._model_access_check(),
            self._writable_check("data_dir", self.settings.data_dir),
            self._writable_dirs_check(),
            self._disk_space_check(),
            self._template_check(),
            self._template_hash_check(),
            self._port_check(),
            self._preview_port_range_check(),
            self._backend_routes_check(),
            self._stale_backend_check(),
            self._preview_container_check(),
            self._test_command_check(),
            self._runtime_policy_files_check(),
            self.exec_policy_service.doctor_check(),
        ]
        status = "passed" if all(item["status"] == "passed" for item in checks if item["required"]) else "failed"
        payload = {"status": status, "checks": checks, "created_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", "doctor:exec_policy", self.exec_policy_service.doctor_check())
        self.store.upsert("reports", "doctor:last", payload)
        return payload

    def metrics_summary(self) -> dict[str, Any]:
        return self.observability_summary()

    def observability_summary(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        if workspace_id:
            self.workspace_service.get_workspace(workspace_id)
        runs = [
            run
            for run in self.store.list("runs")
            if isinstance(run, dict) and (not workspace_id or run.get("workspace_id") == workspace_id)
        ]
        by_status = self._count_by(runs, "status")
        token_usage = self._observability_token_usage(runs)
        cost = self._observability_cost(runs)
        latency = self._observability_latency(runs)
        failure_classes = self._observability_failure_classes(runs)
        repair_success = self._observability_repair_success(runs)
        green_rates = self._observability_green_rates(runs, cost.get("_cost_by_mode", {}))
        cost.pop("_cost_by_mode", None)
        payload = {
            "schema": "grounded.observability.v1",
            "status": "ok",
            "workspace_id": workspace_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_count": len(runs),
            "completed_runs": by_status.get("completed", 0),
            "failed_runs": by_status.get("failed", 0),
            "blocked_runs": by_status.get("blocked", 0),
            "running_runs": by_status.get("running", 0),
            "awaiting_approval_runs": by_status.get("awaiting_approval", 0),
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "token_usage_total": token_usage["total_tokens"],
            "latency_ms_total": latency["total_ms"],
            "token_usage": token_usage,
            "cost": cost,
            "latency": latency,
            "green_rate_by_generation_mode": green_rates,
            "failure_classes": failure_classes,
            "repair_success": repair_success,
            "by_status": by_status,
        }
        typed = ObservabilityReport.model_validate(payload).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", f"observability:{workspace_id or 'system'}", typed)
        return typed

    @staticmethod
    def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "unknown")
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    @classmethod
    def _observability_token_usage(cls, runs: list[dict[str, Any]]) -> dict[str, int]:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "turn_count": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        }
        for run in runs:
            usage = run.get("token_usage") if isinstance(run.get("token_usage"), dict) else {}
            cache_stats = run.get("cache_stats") if isinstance(run.get("cache_stats"), dict) else {}
            totals["input_tokens"] += cls._safe_int(usage.get("input_tokens"))
            totals["output_tokens"] += cls._safe_int(usage.get("output_tokens"))
            totals["reasoning_tokens"] += cls._safe_int(usage.get("reasoning_tokens"))
            total_tokens = cls._safe_int(usage.get("total_tokens"))
            if not total_tokens:
                total_tokens = cls._safe_int(usage.get("input_tokens")) + cls._safe_int(usage.get("output_tokens"))
            totals["total_tokens"] += total_tokens
            totals["turn_count"] += cls._safe_int(usage.get("turn_count"))
            totals["cached_tokens"] += cls._safe_int(cache_stats.get("cached_tokens"))
            totals["cache_write_tokens"] += cls._safe_int(cache_stats.get("cache_write_tokens"))
        return totals

    @classmethod
    def _observability_cost(cls, runs: list[dict[str, Any]]) -> dict[str, Any]:
        by_model: dict[str, dict[str, Any]] = {}
        cost_by_mode: dict[str, float] = {}
        explicit_total = 0.0
        estimated_total = 0.0
        unpriced_tokens = 0
        for run in runs:
            model = str(run.get("llm_model") or run.get("model") or "unknown")
            usage = run.get("token_usage") if isinstance(run.get("token_usage"), dict) else {}
            cache_stats = run.get("cache_stats") if isinstance(run.get("cache_stats"), dict) else {}
            input_tokens = cls._safe_int(usage.get("input_tokens"))
            output_tokens = cls._safe_int(usage.get("output_tokens"))
            reasoning_tokens = cls._safe_int(usage.get("reasoning_tokens"))
            total_tokens = cls._safe_int(usage.get("total_tokens")) or input_tokens + output_tokens
            explicit_cost = cls._safe_float(usage.get("estimated_cost_usd") or usage.get("cost_usd") or usage.get("cost") or cache_stats.get("estimated_cost_usd"))
            estimate = explicit_cost if explicit_cost > 0 else cls._estimate_cost_usd(model, input_tokens=input_tokens, output_tokens=output_tokens)
            if explicit_cost > 0:
                explicit_total += explicit_cost
                pricing_source = "explicit_usage"
            elif estimate > 0:
                estimated_total += estimate
                pricing_source = "builtin_cost_tier_estimate"
            else:
                unpriced_tokens += total_tokens
                pricing_source = "unknown"
            mode = str(run.get("generation_mode") or "unknown")
            cost_by_mode[mode] = cost_by_mode.get(mode, 0.0) + estimate
            item = by_model.setdefault(
                model,
                {
                    "model": model,
                    "provider": str(model_capabilities(model).get("provider") or "openai"),
                    "cost_tier": str(model_capabilities(model).get("cost_tier") or "unknown"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "pricing_source": pricing_source,
                    "run_count": 0,
                },
            )
            item["input_tokens"] += input_tokens
            item["output_tokens"] += output_tokens
            item["reasoning_tokens"] += reasoning_tokens
            item["total_tokens"] += total_tokens
            item["estimated_cost_usd"] = round(float(item["estimated_cost_usd"]) + estimate, 6)
            item["run_count"] += 1
            if item["pricing_source"] != pricing_source:
                item["pricing_source"] = "mixed"
        total = explicit_total + estimated_total
        return {
            "estimated_cost_usd": round(total, 6),
            "explicit_cost_usd": round(explicit_total, 6),
            "estimated_from_tokens_usd": round(estimated_total, 6),
            "unpriced_tokens": unpriced_tokens,
            "pricing_source": "explicit_usage" if explicit_total and not estimated_total else "builtin_cost_tier_estimate" if estimated_total and not explicit_total else "mixed" if total else "unknown",
            "by_model": sorted(by_model.values(), key=lambda item: float(item.get("estimated_cost_usd") or 0), reverse=True),
            "_cost_by_mode": cost_by_mode,
        }

    @staticmethod
    def _estimate_cost_usd(model: str, *, input_tokens: int, output_tokens: int) -> float:
        tier = str(model_capabilities(model).get("cost_tier") or "unknown")
        # Local estimate only. If providers return explicit usage cost, that value wins.
        rates_per_1m = {
            "low": {"input": 0.15, "output": 0.60},
            "high": {"input": 1.25, "output": 10.00},
            "embedding": {"input": 0.13, "output": 0.0},
        }.get(tier)
        if rates_per_1m is None:
            return 0.0
        return round((input_tokens / 1_000_000) * rates_per_1m["input"] + (output_tokens / 1_000_000) * rates_per_1m["output"], 6)

    @classmethod
    def _observability_latency(cls, runs: list[dict[str, Any]]) -> dict[str, Any]:
        phase_totals: dict[str, int] = {}
        run_totals: list[tuple[int, dict[str, Any]]] = []
        for run in runs:
            latency = run.get("latency_breakdown") if isinstance(run.get("latency_breakdown"), dict) else {}
            total = cls._safe_int(latency.get("total_ms") or latency.get("agent_total_ms"))
            if not total:
                total = sum(cls._safe_int(value) for value in latency.values() if isinstance(value, (int, float, str)))
            for key, value in latency.items():
                numeric = cls._safe_int(value)
                if numeric:
                    phase_totals[str(key)] = phase_totals.get(str(key), 0) + numeric
            if total:
                run_totals.append((total, run))
        sorted_totals = sorted(value for value, _run in run_totals)
        total_ms = sum(sorted_totals)
        return {
            "total_ms": total_ms,
            "average_ms": round(total_ms / len(sorted_totals), 2) if sorted_totals else 0.0,
            "p50_ms": cls._percentile(sorted_totals, 0.50),
            "p95_ms": cls._percentile(sorted_totals, 0.95),
            "phase_totals_ms": dict(sorted(phase_totals.items(), key=lambda item: item[1], reverse=True)),
            "slowest_runs": [
                {
                    "run_id": str(run.get("run_id") or ""),
                    "workspace_id": str(run.get("workspace_id") or ""),
                    "generation_mode": str(run.get("generation_mode") or "unknown"),
                    "status": str(run.get("status") or "unknown"),
                    "total_ms": total,
                }
                for total, run in sorted(run_totals, key=lambda item: item[0], reverse=True)[:5]
            ],
        }

    @classmethod
    def _observability_green_rates(cls, runs: list[dict[str, Any]], cost_by_mode: dict[str, float]) -> list[dict[str, Any]]:
        by_mode: dict[str, list[dict[str, Any]]] = {}
        for run in runs:
            by_mode.setdefault(str(run.get("generation_mode") or "unknown"), []).append(run)
        rows: list[dict[str, Any]] = []
        for mode, mode_runs in sorted(by_mode.items()):
            status_counts = cls._count_by(mode_runs, "status")
            terminal = [run for run in mode_runs if str(run.get("status") or "") in {"completed", "failed", "blocked", "awaiting_approval"}]
            green = [run for run in terminal if cls._is_green_run(run)]
            token_sum = sum(cls._safe_int((run.get("token_usage") or {}).get("total_tokens")) for run in mode_runs if isinstance(run.get("token_usage"), dict))
            rows.append(
                {
                    "generation_mode": mode,
                    "run_count": len(mode_runs),
                    "terminal_count": len(terminal),
                    "green_count": len(green),
                    "green_rate": round(len(green) / len(terminal), 4) if terminal else 0.0,
                    "status_counts": status_counts,
                    "average_total_tokens": round(token_sum / len(mode_runs), 2) if mode_runs else 0.0,
                    "estimated_cost_usd": round(float(cost_by_mode.get(mode) or 0.0), 6),
                }
            )
        return rows

    @classmethod
    def _observability_failure_classes(cls, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for run in runs:
            failure_class = str(run.get("failure_class") or "")
            if not failure_class:
                issues = ((run.get("checks_summary") or {}).get("issues") if isinstance(run.get("checks_summary"), dict) else []) or []
                if isinstance(issues, list) and issues:
                    first = issues[0] if isinstance(issues[0], dict) else {}
                    failure_class = str(first.get("failure_class") or first.get("code") or "")
            if not failure_class:
                continue
            bucket = buckets.setdefault(
                failure_class,
                {"failure_class": failure_class, "count": 0, "latest_run_id": None, "latest_at": None, "generation_modes": {}, "examples": []},
            )
            bucket["count"] += 1
            mode = str(run.get("generation_mode") or "unknown")
            bucket["generation_modes"][mode] = bucket["generation_modes"].get(mode, 0) + 1
            updated_at = str(run.get("updated_at") or run.get("created_at") or "")
            if not bucket["latest_at"] or updated_at >= str(bucket["latest_at"]):
                bucket["latest_at"] = updated_at
                bucket["latest_run_id"] = str(run.get("run_id") or "")
            if len(bucket["examples"]) < 3:
                bucket["examples"].append(
                    {
                        "run_id": str(run.get("run_id") or ""),
                        "status": str(run.get("status") or "unknown"),
                        "generation_mode": mode,
                        "summary": str(run.get("failure_reason") or run.get("root_cause_summary") or "")[:240],
                    }
                )
        return sorted(buckets.values(), key=lambda item: int(item.get("count") or 0), reverse=True)

    def _observability_repair_success(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        fix_runs = [run for run in runs if str(run.get("mode") or "") == "fix"]
        successful_fix_runs = [run for run in fix_runs if str(run.get("status") or "") == "completed"]
        case_count = 0
        resolved_case_count = 0
        attempt_count = 0
        successful_attempt_count = 0
        status_counts: dict[str, int] = {}
        for run in runs:
            cases = self._repair_cases_for_observability(str(run.get("run_id") or ""))
            repair_iterations = run.get("repair_iterations") if isinstance(run.get("repair_iterations"), list) else []
            for iteration in repair_iterations:
                if isinstance(iteration, dict):
                    status = str(iteration.get("status") or "recorded")
                    attempt_count += 1
                    status_counts[status] = status_counts.get(status, 0) + 1
                    if status in {"completed", "passed", "repaired", "resolved"}:
                        successful_attempt_count += 1
            for case in cases:
                if not isinstance(case, dict):
                    continue
                case_count += 1
                status = str(case.get("status") or "open")
                status_counts[status] = status_counts.get(status, 0) + 1
                if status in {"repaired", "resolved", "superseded"}:
                    resolved_case_count += 1
                for attempt in case.get("attempts") or []:
                    if isinstance(attempt, dict):
                        attempt_status = str(attempt.get("status") or "recorded")
                        attempt_count += 1
                        status_counts[attempt_status] = status_counts.get(attempt_status, 0) + 1
                        if attempt_status in {"completed", "passed", "repaired", "resolved"}:
                            successful_attempt_count += 1
        return {
            "fix_run_count": len(fix_runs),
            "successful_fix_runs": len(successful_fix_runs),
            "fix_success_rate": round(len(successful_fix_runs) / len(fix_runs), 4) if fix_runs else 0.0,
            "repair_case_count": case_count,
            "resolved_case_count": resolved_case_count,
            "case_resolution_rate": round(resolved_case_count / case_count, 4) if case_count else 0.0,
            "attempt_count": attempt_count,
            "successful_attempt_count": successful_attempt_count,
            "attempt_success_rate": round(successful_attempt_count / attempt_count, 4) if attempt_count else 0.0,
            "status_counts": dict(sorted(status_counts.items())),
        }

    def _repair_cases_for_observability(self, run_id: str) -> list[dict[str, Any]]:
        if not run_id:
            return []
        index = self.store.get("reports", RepairCaseService.index_ref(run_id)) or {}
        refs = index.get("case_refs") if isinstance(index, dict) else []
        cases: list[dict[str, Any]] = []
        for ref in refs if isinstance(refs, list) else []:
            payload = self.store.get("reports", str(ref))
            if isinstance(payload, dict):
                cases.append(payload)
        return cases

    @staticmethod
    def _is_green_run(run: dict[str, Any]) -> bool:
        if str(run.get("status") or "") != "completed":
            return False
        checks = run.get("checks_summary") if isinstance(run.get("checks_summary"), dict) else {}
        required_keys = ("validators", "build", "preview")
        return all(str(checks.get(key) or "pending") in {"passed", "skipped"} for key in required_keys)

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int:
        if not values:
            return 0
        index = min(len(values) - 1, max(0, int(round((len(values) - 1) * percentile))))
        return int(values[index])

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

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
                        "scoped_skills": {"type": "boolean", "default": True},
                        "skill_roots": {
                            "type": "object",
                            "properties": {
                                "system": {"type": "boolean", "default": True},
                                "repo": {"type": "boolean", "default": True},
                                "plugin": {"type": "boolean", "default": True},
                                "user": {"type": "boolean", "default": True},
                            },
                            "additionalProperties": False,
                        },
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

        acceptance = self.acceptance_scenarios(run_id)
        acceptance_items = list(acceptance.get("items") or [])
        acceptance_blocked = str(acceptance.get("status") or "").startswith("blocked_") or any(
            bool(item.get("blocking")) or str(item.get("status") or "").startswith("blocked_")
            for item in acceptance_items
            if isinstance(item, dict)
        )
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
                "status": "passed" if acceptance_items and not acceptance_blocked else "failed",
                "required": True,
                "evidence": acceptance_items,
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

    def context_pressure(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        ref = run.context_pressure_ref or f"context_pressure:{run.workspace_id}:{run_id}"
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            return ContextPressureReport(
                workspace_id=run.workspace_id,
                run_id=run_id,
                status="missing",
            ).model_dump(mode="json", by_alias=True)
        if payload.get("schema") == "grounded.context_pressure.v2":
            return ContextPressureReport.model_validate(payload).model_dump(mode="json", by_alias=True)
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        latest = items[-1] if items else None
        normalized = {
            "schema": "grounded.context_pressure.v2",
            "workspace_id": run.workspace_id,
            "run_id": run_id,
            "status": "ready" if latest else "empty",
            "latest": latest,
            "items": items,
            "sections": (latest or {}).get("sections") or {},
            "recommendations": (latest or {}).get("recommendations") or [],
            "microcompact_candidates": (latest or {}).get("microcompact_candidates") or [],
            "avoid_reread_files": (latest or {}).get("avoid_reread_files") or [],
            "compact_boundary": (latest or {}).get("compact_boundary") or {},
            "updated_at": (latest or {}).get("created_at"),
        }
        return ContextPressureReport.model_validate(normalized).model_dump(mode="json", by_alias=True)

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

    def output_artifacts(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.output_artifact_service is None:
            return {"schema": "grounded.output_artifact_index.v1", "workspace_id": run.workspace_id, "run_id": run_id, "items": []}
        return self.output_artifact_service.list_run(run_id, workspace_id=run.workspace_id)

    def output_artifact(self, run_id: str, artifact_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        if self.output_artifact_service is None:
            raise KeyError("Output artifact service is unavailable.")
        return self.output_artifact_service.get(run_id, artifact_id, workspace_id=run.workspace_id)

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
        self._journal_run_event(run_id, "apply.staged", record, summary="Files staged for apply.", idempotency_key=f"apply.staged:{run_id}:{record['updated_at']}")
        return record

    def discard_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        files = self._normalize_file_list(payload.get("files") or [])
        result = self.workspace_service.discard_draft_files(run.workspace_id, run_id, files)
        record = {"run_id": run_id, "files": files, "result": result, "status": "discarded", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"discarded_files:{run_id}", record)
        self.record_tool_event(run_id, tool_envelope(tool="file.discard", input_payload={"files": files}, result=record, risk="mutating"))
        self._journal_run_event(run_id, "apply.discarded", record, summary="Draft files discarded.", idempotency_key=f"apply.discarded:{run_id}:{record['updated_at']}")
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
        allowed, guardian_report = self.run_service.enforce_guardian_before_apply(
            run,
            source="pre_apply_guardian",
            changed_files=files,
        )
        if not allowed:
            artifacts = self._run_artifacts_or_empty(run_id)
            artifacts["guardian_review"] = guardian_report
            artifacts["run"] = self.run_service.get_run(run_id).model_dump(mode="json")
            self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
            return self.run_service.get_run(run_id)
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
        self._journal_run_event(
            run_id,
            "apply.applied",
            {"run_id": run_id, "workspace_id": run.workspace_id, **artifacts["staged_apply"]},
            summary="Staged draft files applied.",
            idempotency_key=f"apply.applied:{run_id}:{revision.revision_id}",
        )
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
        route_graph = LspToolService.route_graph(root=root, targets=files)
        payload = {
            **report,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "sources": sorted({str(item.get("source") or "unknown") for item in report.get("items") or []} or {"none"}),
            "symbols": LspToolService.symbol_context(root=root, query="", targets=files).get("items", []),
            "route_graph": route_graph,
        }
        self.store.upsert("reports", f"lsp_diagnostics:{workspace_id}:{run_id or 'source'}", payload)
        return payload

    def lsp_symbol_context(self, workspace_id: str, *, run_id: str | None = None, query: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.symbol_context(root=root, query=query, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_definition(self, workspace_id: str, *, run_id: str | None = None, symbol: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.definition(root=root, symbol=symbol, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_find_references(self, workspace_id: str, *, run_id: str | None = None, symbol: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.find_references(root=root, symbol=symbol, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_route_static_context(self, workspace_id: str, *, run_id: str | None = None, targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.route_static_context(root=root, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def lsp_route_graph(self, workspace_id: str, *, run_id: str | None = None, targets: list[str] | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        return {**LspToolService.route_graph(root=root, targets=targets), "workspace_id": workspace_id, "run_id": run_id}

    def patch_preflight(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ops = payload.get("ops") or payload.get("patch_actions") or []
        ops = [PatchOperationModel.model_validate(item) for item in raw_ops if isinstance(item, dict)]
        return self._patch_preflight(workspace_id, ops, payload)

    def _patch_preflight(self, workspace_id: str, ops: list[PatchOperationModel], payload: dict[str, Any]) -> dict[str, Any]:
        from app.services.patch_service import PatchService

        service = PatchService(self.workspace_service)
        report = service.preflight(
            workspace_id=workspace_id,
            patch_actions=ops,
            run_id=payload.get("run_id"),
            base_revision_id=payload.get("base_revision_id"),
        )
        run_id = payload.get("run_id")
        if run_id:
            event_type = "sandbox.preflight_passed" if (report.get("sandbox_report") or {}).get("status") == "passed" else "sandbox.preflight_blocked"
            self._journal_run_event(
                str(run_id),
                event_type,
                {"workspace_id": workspace_id, "report": report.get("sandbox_report") or {}},
                summary="Sandbox preflight completed.",
                idempotency_key=f"{event_type}:{run_id}:{len(ops)}:{report.get('status')}",
            )
        return report

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
        self._journal_run_event(
            run_id,
            "approval.decided",
            {"approval": item},
            summary=str(item.get("summary") or item.get("kind") or "Approval decided."),
            idempotency_key=f"approval.decided:{run_id}:{approval_id}:{item['status']}",
        )
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
        if str(item.get("status") or "") == "pending":
            self._journal_run_event(
                run_id,
                "approval.requested",
                {"approval": item},
                summary=str(item.get("summary") or item.get("kind") or "Approval requested."),
                idempotency_key=f"approval.requested:{run_id}:{item.get('approval_id')}",
            )
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

    def _journal_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "system",
        summary: str = "",
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if self.event_journal_service is None:
            return
        try:
            run = self.run_service.get_run(run_id)
            self.event_journal_service.append_run(
                workspace_id=run.workspace_id,
                run_id=run_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                summary=summary,
                source_ref=source_ref,
                idempotency_key=idempotency_key,
            )
        except Exception:
            pass

    @staticmethod
    def _tool_journal_event_type(item: dict[str, Any]) -> str:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        status = str(result.get("status") or item.get("status") or "").lower()
        if status in {"failed", "blocked", "error", "errored"}:
            return "tool.failed"
        if status in {"pending", "requested", "starting", "started"}:
            return "tool.requested"
        return "tool.completed"

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
        worker_id = canonical_worker_id(worker_id)
        normalized = str(path or "").replace("\\", "/")
        if worker_id == "planner":
            return normalized.startswith("docs/") or normalized.endswith("README.md")
        if worker_id == "backend_api_worker":
            return normalized.startswith("miniapp/app/") and "/static/" not in normalized
        role_by_worker = {
            "client_surface_worker": "client",
            "specialist_surface_worker": "specialist",
            "manager_surface_worker": "manager",
        }
        if worker_id in role_by_worker:
            role = role_by_worker[worker_id]
            return f"/static/{role}/" in normalized or normalized.startswith(f"miniapp/app/static/{role}/")
        if worker_id == "test_verifier_worker":
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
            return "client_surface_worker"
        if normalized.startswith("miniapp/app/static/specialist/"):
            return "specialist_surface_worker"
        if normalized.startswith("miniapp/app/static/manager/"):
            return "manager_surface_worker"
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

    def _python_version_check(self) -> dict[str, Any]:
        version = sys.version_info
        required_major, required_minor = self._required_python_version()
        ok = (version.major, version.minor) >= (required_major, required_minor)
        details = (
            f"python={version.major}.{version.minor}.{version.micro}; executable={Path(sys.executable)}; "
            f"required>={required_major}.{required_minor}"
        )
        return self._check("python", ok, details, str(Path(sys.executable)), required=True)

    def _required_python_version(self) -> tuple[int, int]:
        pyproject = self.settings.repo_root / "platform" / "backend" / "pyproject.toml"
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return (3, 11)
        match = re.search(r'requires-python\s*=\s*"[^"]*>=\s*(\d+)\.(\d+)', text)
        if not match:
            return (3, 11)
        return (int(match.group(1)), int(match.group(2)))

    def _node_version_check(self) -> dict[str, Any]:
        return self._versioned_binary_check("node", ["node", "--version"], minimum_major=18, required=True)

    def _npm_version_check(self) -> dict[str, Any]:
        return self._versioned_binary_check("npm", ["npm", "--version"], minimum_major=9, required=True)

    def _versioned_binary_check(self, name: str, command: list[str], *, minimum_major: int, required: bool) -> dict[str, Any]:
        path = shutil.which(command[0])
        if not path:
            return self._check(name, False, f"{name} not found; required>={minimum_major}", " ".join(command), required=required)
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        except Exception as exc:
            return self._check(name, False, f"{path}; version check failed: {exc}", " ".join(command), required=required)
        version_text = (result.stdout or result.stderr).strip()
        major = self._parse_major_version(version_text)
        ok = result.returncode == 0 and major is not None and major >= minimum_major
        details = f"path={path}; version={version_text or 'unknown'}; required>={minimum_major}"
        return self._check(name, ok, details, " ".join(command), required=required)

    @staticmethod
    def _parse_major_version(value: str) -> int | None:
        match = re.search(r"(\d+)", value or "")
        if not match:
            return None
        return int(match.group(1))

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

    def _docker_daemon_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("docker_daemon", False, "docker CLI not found", "docker info --format '{{.ServerVersion}}'", required=False)
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                text=True,
                capture_output=True,
                timeout=5,
            )
        except Exception as exc:
            return self._check("docker_daemon", False, str(exc), "docker info --format '{{.ServerVersion}}'", required=False)
        output = (result.stdout or result.stderr).strip()
        return self._check(
            "docker_daemon",
            result.returncode == 0,
            output or "docker daemon did not respond",
            "docker info --format '{{.ServerVersion}}'",
            required=False,
        )

    def _playwright_check(self) -> dict[str, Any]:
        try:
            import playwright  # noqa: F401

            return self._check("playwright", True, "Python package import succeeded", "python -c 'import playwright'", required=False)
        except Exception as exc:
            return self._check("playwright", False, str(exc), "python -c 'import playwright'", required=False)

    def _openai_check(self) -> dict[str, Any]:
        config = self.openai_client.configuration()
        return self._check("openai", bool(config.get("enabled")), "configured" if config.get("enabled") else "not configured", required=False)

    def _model_access_check(self) -> dict[str, Any]:
        manager = getattr(self.openai_client, "model_manager", None)
        if manager is None:
            return self._check("model_access", False, "model manager unavailable", required=False)
        try:
            status = manager.status()
            route = manager.select(role="agent_turn", model_profile=status.default_coding_profile, generation_mode="balanced")
        except Exception as exc:
            return self._check("model_access", False, f"model manager failed: {exc}", required=False)
        provider = status.providers.get(route.selected_provider)
        provider_status = provider.status if provider is not None else "unknown"
        details = (
            f"enabled={status.enabled}; provider={route.selected_provider}:{provider_status}; "
            f"selected={route.selected_model}; profile={route.model_profile}; fallback={route.fallback_enabled}"
        )
        return self._check("model_access", bool(status.enabled and route.status == "ready"), details, required=False)

    def _writable_check(self, name: str, path: Path) -> dict[str, Any]:
        return self._check(name, os.access(path, os.W_OK), str(path), required=True)

    def _writable_dirs_check(self) -> dict[str, Any]:
        paths = [
            self.settings.data_dir,
            self.settings.workspaces_dir,
            self.settings.exports_dir,
            self.settings.host_data_dir,
            self.settings.data_dir / ".sandbox" / "tmp",
            self.settings.data_dir / ".sandbox" / "home",
        ]
        failed: list[str] = []
        passed: list[str] = []
        for path in paths:
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".doctor-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                passed.append(str(path))
            except Exception as exc:
                failed.append(f"{path}: {exc}")
        details = f"writable={len(passed)}/{len(paths)}; " + ("failed: " + "; ".join(failed) if failed else ", ".join(passed[:6]))
        return self._check("writable_dirs", not failed, details, required=True)

    def _disk_space_check(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.settings.data_dir)
        except Exception as exc:
            return self._check("disk_space", False, str(exc), required=True)
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        ok = free_gb >= 1.0
        details = f"free={free_gb:.1f}GB total={total_gb:.1f}GB path={self.settings.data_dir}; required>=1GB"
        return self._check("disk_space", ok, details, required=True)

    def _template_check(self) -> dict[str, Any]:
        required = [self.settings.template_dir / "miniapp" / "app" / "main.py", self.settings.template_dir / "docker" / "docker-compose.yml"]
        missing = [str(path) for path in required if not path.exists()]
        return self._check("template_integrity", not missing, "missing: " + ", ".join(missing) if missing else str(self.settings.template_dir), required=True)

    def _template_hash_check(self) -> dict[str, Any]:
        root = self.settings.template_dir
        if not root.exists():
            return self._check("template_hash", False, f"missing template dir: {root}", required=True)
        digest = hashlib.sha256()
        file_count = 0
        ignored_dirs = {".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        try:
            paths = sorted(path for path in root.rglob("*") if path.is_file() and not any(part in ignored_dirs for part in path.relative_to(root).parts))
            for path in paths:
                rel = path.relative_to(root).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
                file_count += 1
        except Exception as exc:
            return self._check("template_hash", False, f"hash failed: {exc}", required=True)
        ok = file_count > 0
        return self._check("template_hash", ok, f"sha256={digest.hexdigest()}; files={file_count}; root={root}", required=True)

    def _port_check(self) -> dict[str, Any]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = sock.connect_ex(("127.0.0.1", int(self.settings.preview_port_base)))
            return self._check("preview_port_base", result != 0, f"port {self.settings.preview_port_base} {'available' if result != 0 else 'in use'}", required=False)
        finally:
            sock.close()

    def _preview_port_range_check(self) -> dict[str, Any]:
        base = int(self.settings.preview_port_base)
        ports = list(range(base, base + 8))
        available: list[int] = []
        in_use: list[int] = []
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                result = sock.connect_ex(("127.0.0.1", port))
                (available if result != 0 else in_use).append(port)
            finally:
                sock.close()
        details = f"available={available}; in_use={in_use}; range={base}-{base + 7}"
        return self._check("preview_port_range", bool(available), details, required=False)

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
            import playwright  # noqa: F401
        except Exception as exc:
            return self._check("playwright_browsers", False, f"playwright package unavailable: {exc}", "python -m playwright install", required=False)

        browser_roots = self._playwright_browser_roots()
        installed: list[str] = []
        for root in browser_roots:
            try:
                if root.exists():
                    installed.extend(
                        sorted(
                            child.name
                            for child in root.iterdir()
                            if child.is_dir() and any(marker in child.name.lower() for marker in ("chromium", "firefox", "webkit"))
                        )
                    )
            except OSError:
                continue
        if installed:
            return self._check(
                "playwright_browsers",
                True,
                f"installed={', '.join(installed[:8])}; roots={', '.join(str(path) for path in browser_roots)}",
                "python -m playwright install",
                required=False,
            )
        try:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run"], text=True, capture_output=True, timeout=10)
            output = (result.stdout or result.stderr).strip()
        except Exception as exc:
            output = str(exc)
        return self._check(
            "playwright_browsers",
            False,
            f"no browser cache found in {', '.join(str(path) for path in browser_roots)}; dry_run={output[:400]}",
            "python -m playwright install --dry-run",
            required=False,
        )

    def _playwright_browser_roots(self) -> list[Path]:
        configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        roots: list[Path] = []
        if configured and configured not in {"0", "false", "False"}:
            roots.append(Path(configured).expanduser())
        roots.extend(
            [
                Path.home() / "Library" / "Caches" / "ms-playwright",
                Path.home() / ".cache" / "ms-playwright",
                self.settings.data_dir / ".cache" / "ms-playwright",
            ]
        )
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                unique.append(root)
                seen.add(key)
        return unique

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

    def _runtime_policy_files_check(self) -> dict[str, Any]:
        policy_dir = self.settings.runtime_dir / "policies"
        files = [
            policy_dir / "agent_exec_policy.json",
            policy_dir / "agent_hooks.json",
        ]
        missing: list[str] = []
        invalid: list[str] = []
        loaded: list[str] = []
        for path in files:
            if not path.exists():
                missing.append(str(path))
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                invalid.append(f"{path}: {exc}")
                continue
            loaded.append(str(path))
        ok = not invalid and not any(path.endswith("agent_exec_policy.json") for path in missing)
        details = f"loaded={loaded}; missing_optional={missing}; invalid={invalid}"
        return self._check("runtime_policy_files", ok, details, required=True)

    @staticmethod
    def _builtin_skills() -> dict[str, dict[str, Any]]:
        return SkillPackCatalog.builtin()
