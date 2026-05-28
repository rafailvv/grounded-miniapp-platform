from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath
import os
import re
import threading
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from app.ai.model_registry import resolve_model_profile
from app.ai.openai_client import OpenAIClient
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    CodeChangePlan,
    CodeChangeTarget,
    CreateRunRequest,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RunCheckResult,
    RunChecksSummary,
    RunRecord,
    ValidationSnapshot,
    WorkspaceRecord,
    new_id,
)
from app.modules.miniapp_agent_loop.guardian_review import GuardianReview
from app.modules.workspace_code_agent_runtime import WorkspaceCodeAgentRuntime
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.guardian_gate import GuardianGateService
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.miniapp_contract import MiniAppContractCompiler
from app.services.prompt_contract_compiler import PromptContractCompilerService
from app.services.repair_cases import RepairCaseService
from app.services.event_journal import EventJournalService
from app.services.run_protocol import RunProtocolService
from app.services.run_state_machine import RunStateMachine
from app.services.run_task_ledger import RunTaskLedger
from app.services.workflow_acceptance import (
    build_acceptance_contract,
    build_implementation_plan,
    derive_prompt_contract_analysis,
    orchestration_metadata_for_contract,
)
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.service import WorkspaceService

ROLE_ORDER = ("client", "specialist", "manager")
ROLE_SCOPE = set(ROLE_ORDER)
ACTIVE_RUN_RECOVERY_STALE_SECONDS = int(os.getenv("ACTIVE_RUN_RECOVERY_STALE_SECONDS", "3600"))
TERMINAL_RUN_STATUSES = {"completed", "blocked", "failed", "awaiting_approval"}
MEANINGFUL_DIFF_IGNORED_PARTS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".vite",
    ".cache",
    "artifacts",
}
MEANINGFUL_DIFF_IGNORED_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
MEANINGFUL_DIFF_IGNORED_NAMES = {".DS_Store", "vite.config.js", "vite.config.d.ts"}
logger = logging.getLogger(__name__)
WORKSPACE_NAME_STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "build",
    "create",
    "for",
    "generate",
    "i",
    "in",
    "make",
    "me",
    "mini",
    "miniapp",
    "mini-app",
    "my",
    "need",
    "of",
    "simple",
    "that",
    "the",
    "this",
    "to",
    "with",
    "создай",
    "сделай",
    "сгенерируй",
    "приложение",
    "мини",
    "миниапп",
    "для",
    "мне",
    "нужно",
    "надо",
    "простое",
    "с",
    "и",
}
class RunService:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        code_agent_runtime: WorkspaceCodeAgentRuntime,
        preview_service: PreviewService,
        check_runner: CheckRunner,
        openai_client: OpenAIClient,
        workspace_log_service: WorkspaceLogService,
        run_protocol_service: RunProtocolService | None = None,
        event_journal_service: EventJournalService | None = None,
        prompt_contract_compiler_service: PromptContractCompilerService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.code_agent_runtime = code_agent_runtime
        self.preview_service = preview_service
        self.check_runner = check_runner
        self.openai_client = openai_client
        self.workspace_log_service = workspace_log_service
        self.run_protocol_service = run_protocol_service
        self.event_journal_service = event_journal_service
        self.prompt_contract_compiler_service = prompt_contract_compiler_service or PromptContractCompilerService(
            store=store,
            openai_client=openai_client,
            event_journal_service=event_journal_service,
        )
        self.guardian_gate_service = GuardianGateService(store=store, workspace_service=workspace_service, event_journal_service=event_journal_service)
        self.background_task_service: Any | None = None
        self._active_workers: dict[str, threading.Thread] = {}
        self._startup_started_at = datetime.now(timezone.utc)
        self._recover_orphaned_active_runs()
        self._recover_orphaned_terminal_jobs()

    def attach_background_task_service(self, background_task_service: Any) -> None:
        self.background_task_service = background_task_service

    def attach_guardian_gate_service(self, guardian_gate_service: GuardianGateService) -> None:
        self.guardian_gate_service = guardian_gate_service

    def stop_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.status != "running":
            return run
        self.store.upsert(
            "reports",
            f"run_stop_request:{run_id}",
            {
                "run_id": run_id,
                "requested": True,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        run.current_stage = "stopping"
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._journal_run(
            run,
            "run.stop_requested",
            {"run_id": run.run_id, "status": run.status, "current_stage": run.current_stage},
            summary="Run stop requested.",
            idempotency_key=f"run.stop_requested:{run.run_id}:{run.updated_at.isoformat()}",
        )
        return run

    def create_run(self, workspace_id: str, request: CreateRunRequest) -> RunRecord:
        return self._start_run(workspace_id, request, wait=False)

    def create_run_sync(self, workspace_id: str, request: CreateRunRequest) -> RunRecord:
        return self._start_run(workspace_id, request, wait=True)

    def _start_run(self, workspace_id: str, request: CreateRunRequest, *, wait: bool) -> RunRecord:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_run: RunRecord | None = None
        if request.resume_from_run_id:
            source_run = self.get_run(request.resume_from_run_id)
            if source_run.workspace_id != workspace_id:
                raise ValueError("Cannot resume a run from another workspace.")
        suggested_workspace_name = "" if source_run is not None else self._derive_workspace_name_from_prompt(request.prompt)
        if suggested_workspace_name:
            workspace = self.workspace_service.rename_workspace(workspace_id, suggested_workspace_name)
        resolved_role_scope = self._resolve_target_role_scope(request)
        resolved_intent = self._resolve_intent(workspace, request, resolved_role_scope=resolved_role_scope)
        if resolved_intent == "create":
            resolved_role_scope = list(ROLE_ORDER)
        effective_generation_mode = self._resolve_generation_mode(workspace, request, resolved_intent)
        effective_model_profile = self._resolve_model_profile(request.model_profile, effective_generation_mode)
        contract_probe = GenerateRequest(
            prompt=request.prompt,
            mode=request.mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            generation_mode=effective_generation_mode,
            intent=resolved_intent,
            target_role_scope=resolved_role_scope,
            model_profile=effective_model_profile,
            linked_run_id=None,
            resume_from_run_id=request.resume_from_run_id,
            session_id=request.session_id,
            resume_bookmark_id=request.resume_bookmark_id,
            forked_from_run_id=request.forked_from_run_id,
            error_context=request.error_context,
        )
        focused_edit_kind = WorkspaceCodeAgentRuntime._focused_edit_kind(contract_probe)
        contract_source_run, inherited_acceptance_contract = self._resolve_inherited_acceptance_contract(
            source_run,
            request=request,
        )
        prompt_contract_source_run, inherited_prompt_contract = self._resolve_inherited_prompt_contract(source_run, request=request)
        contract_prompt = contract_source_run.prompt if inherited_acceptance_contract and contract_source_run is not None else request.prompt
        if inherited_prompt_contract and prompt_contract_source_run is not None:
            contract_prompt = prompt_contract_source_run.prompt
            if not inherited_acceptance_contract:
                inherited_acceptance_contract = self._required_acceptance_contract_for_run(prompt_contract_source_run)
                contract_source_run = prompt_contract_source_run
        elif inherited_acceptance_contract:
            inherited_acceptance_contract = {
                **inherited_acceptance_contract,
                "required": True,
                "inherited_from_run_id": contract_source_run.run_id if contract_source_run else request.resume_from_run_id,
                "continued_from_run_id": source_run.run_id if source_run else request.resume_from_run_id,
                "contract_source_run_id": contract_source_run.run_id if contract_source_run else request.resume_from_run_id,
                "repair_continuation": True,
            }
            inherited_prompt_contract = {
                "acceptance_contract": inherited_acceptance_contract,
                "implementation_plan": dict((contract_source_run or source_run).implementation_plan or {}) if (contract_source_run or source_run) is not None else {},
            }
        run_id = new_id("run")
        prompt_contract_result = self.prompt_contract_compiler_service.compile(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt=request.prompt,
            intent=resolved_intent,
            generation_mode=effective_generation_mode,
            focused_edit_kind=focused_edit_kind,
            model_profile=effective_model_profile,
            inherited_prompt_contract=inherited_prompt_contract,
            inherited_acceptance_contract=inherited_acceptance_contract,
            source_run_id=(prompt_contract_source_run or contract_source_run).run_id if (prompt_contract_source_run or contract_source_run) else None,
            contract_prompt=contract_prompt,
        )
        acceptance_contract = prompt_contract_result.acceptance_contract
        implementation_plan = prompt_contract_result.implementation_plan
        orchestration = prompt_contract_result.orchestration
        prompt_analysis_usage = prompt_contract_result.prompt_analysis_usage
        prompt_analysis_model = prompt_contract_result.prompt_analysis_model
        contract_blocked = bool(acceptance_contract.get("blocking")) or str(acceptance_contract.get("status") or "").startswith("blocked_")
        run = RunRecord(
            run_id=run_id,
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode=request.mode,
            intent=resolved_intent,
            apply_strategy="staged_auto_apply",
            approval_required=False,
            target_role_scope=resolved_role_scope,
            model_profile=effective_model_profile,
            generation_mode=effective_generation_mode,
            llm_provider=(self.openai_client.configuration().get("routing") or {}).get("provider") if self.openai_client.enabled else None,
            llm_model=prompt_analysis_model,
            resume_from_run_id=request.resume_from_run_id,
            session_id=request.session_id or workspace_id,
            resume_bookmark_id=request.resume_bookmark_id,
            forked_from_run_id=request.forked_from_run_id,
            source_revision_id=workspace.current_revision_id,
            error_context=request.error_context,
            implementation_plan=implementation_plan,
            acceptance_contract=acceptance_contract,
            orchestration_phases=list(orchestration.get("phases") or []),
            worker_summaries=list(orchestration.get("worker_summaries") or []),
            flow_coverage={
                "status": "blocked_contract_missing" if contract_blocked else "planned" if acceptance_contract.get("required") else "not_required",
                "required_flows": [flow.get("id") for flow in acceptance_contract.get("flows", []) if isinstance(flow, dict)],
            },
            status="blocked" if contract_blocked else "pending",
            apply_status="blocked" if contract_blocked else "pending",
            current_stage="blocked: acceptance contract missing" if contract_blocked else "queued",
            progress_percent=100 if contract_blocked else 2,
            storage_version=2,
            token_usage=self._token_usage_from_prompt_analysis(prompt_analysis_usage),
        )
        run.prompt_contract_ref = prompt_contract_result.compile_report.prompt_contract_ref
        run.task_ledger_ref = f"task_ledger:{workspace_id}:{run.run_id}"
        run.implementation_plan = implementation_plan
        self.store.upsert(
            "reports",
            run.task_ledger_ref,
            RunTaskLedger.build(
                run_id=run.run_id,
                workspace_id=workspace_id,
                implementation_plan=implementation_plan,
                run_status=run.status,
                current_stage=run.current_stage,
                updated_at=run.updated_at.isoformat(),
            ),
        )
        if acceptance_contract.get("required"):
            miniapp_contract = prompt_contract_result.miniapp_contract
            if miniapp_contract is None:
                raise RuntimeError("Prompt contract compiler did not return a miniapp contract for a required acceptance contract.")
            run.acceptance_contract = miniapp_contract.acceptance_summary
            run.miniapp_contract_ref = f"miniapp_contract:{workspace_id}:{run.run_id}"
            run.contract_compile_ref = f"contract_compile:{workspace_id}:{run.run_id}"
            run.route_registry_ref = f"route_registry:{workspace_id}:{run.run_id}"
            run.repair_recipes_ref = f"repair_recipes:{workspace_id}:{run.run_id}"
            self.store.upsert(
                "reports",
                run.miniapp_contract_ref,
                {"workspace_id": workspace_id, "run_id": run.run_id, "contract": miniapp_contract.model_dump(mode="json")},
            )
            self.store.upsert(
                "reports",
                run.contract_compile_ref,
                {
                    "workspace_id": workspace_id,
                    "run_id": run.run_id,
                    "status": "compiled",
                    "contract_id": miniapp_contract.contract_id,
                    "contract_owned_paths": miniapp_contract.allowed_file_graph.contract_owned_paths,
                },
            )
        if acceptance_contract.get("required") or request.mode == "generate":
            self.store.upsert(
                "reports",
                f"acceptance_contract:{workspace_id}:{run.run_id}",
                {
                    "workspace_id": workspace_id,
                    "run_id": run.run_id,
                    "contract": run.acceptance_contract,
                    "implementation_plan": implementation_plan,
                    "orchestration": orchestration,
                },
            )
        self._save_run(run)
        self._journal_run(
            run,
            "run.created",
            {
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "session_id": run.session_id,
                "status": run.status,
                "apply_status": run.apply_status,
                "current_stage": run.current_stage,
                "mode": run.mode,
                "intent": run.intent,
                "generation_mode": str(getattr(run.generation_mode, "value", run.generation_mode)),
                "model_profile": run.model_profile,
                "resume_from_run_id": run.resume_from_run_id,
                "forked_from_run_id": run.forked_from_run_id,
                "prompt_contract_ref": run.prompt_contract_ref,
            },
            summary="Run record created.",
            idempotency_key=f"run.created:{run.run_id}",
        )
        self._journal_run(
            run,
            "run.session_configured",
            {
                "workspace_id": workspace_id,
                "session_id": run.session_id,
                "resume_from_run_id": run.resume_from_run_id,
                "resume_bookmark_id": run.resume_bookmark_id,
                "forked_from_run_id": run.forked_from_run_id,
            },
            summary="Run session context configured.",
            idempotency_key=f"run.session_configured:{run.run_id}",
        )
        self._journal_run(
            run,
            "run.started",
            {
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "session_id": run.session_id,
                "status": run.status,
                "apply_status": run.apply_status,
                "current_stage": run.current_stage,
                "mode": run.mode,
                "intent": run.intent,
            },
            summary="Run started.",
            idempotency_key=f"run.started:{run.run_id}",
        )
        if self.run_protocol_service is not None:
            self.run_protocol_service.append_event(
                run_id=run.run_id,
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                event_type="session_configured",
                status="completed",
                message="Run session context configured.",
                payload={
                    "workspace_id": workspace_id,
                    "session_id": run.session_id,
                    "resume_from_run_id": run.resume_from_run_id,
                    "resume_bookmark_id": run.resume_bookmark_id,
                    "forked_from_run_id": run.forked_from_run_id,
                },
            )
            self.run_protocol_service.append_event(
                run_id=run.run_id,
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                event_type="run_started",
                status="started",
                message="Run record created.",
                payload={
                    "mode": run.mode,
                    "intent": run.intent,
                    "generation_mode": str(getattr(run.generation_mode, "value", run.generation_mode)),
                    "model_profile": run.model_profile,
                    "apply_strategy": run.apply_strategy,
                },
            )
        self.store.delete("reports", f"run_stop_request:{run.run_id}")
        if contract_blocked:
            if self.run_protocol_service is not None:
                self.run_protocol_service.append_event(
                    run_id=run.run_id,
                    workspace_id=run.workspace_id,
                    session_id=run.session_id,
                    event_type="run_completed",
                    status="blocked",
                    message="Run blocked because prompt-derived acceptance contract is missing.",
                    payload={
                        "status": run.status,
                        "reason": acceptance_contract.get("reason"),
                        "issues": acceptance_contract.get("issues") or [],
                    },
                )
            self._journal_run(
                run,
                "run.blocked",
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "apply_status": run.apply_status,
                    "current_stage": run.current_stage,
                    "reason": acceptance_contract.get("reason"),
                    "issues": acceptance_contract.get("issues") or [],
                },
                summary="Run blocked because prompt-derived acceptance contract is missing.",
                idempotency_key=f"run.blocked:{run.run_id}:contract",
            )
            return self.get_run(run.run_id)
        if wait:
            self._active_workers[run.run_id] = threading.current_thread()
            self._execute_run(run.run_id, request.model_dump(mode="python"))
            return self.get_run(run.run_id)
        worker = threading.Thread(
            target=self._execute_run,
            args=(run.run_id, request.model_dump(mode="python")),
            daemon=True,
        )
        self._active_workers[run.run_id] = worker
        worker.start()
        return self.get_run(run.run_id)

    @staticmethod
    def _derive_workspace_name_from_prompt(prompt: str) -> str:
        normalized = RunService._strip_prompt_time_prefix(str(prompt or ""))
        normalized = " ".join(normalized.split()).strip()
        if not normalized:
            return ""
        normalized = re.sub(r"^['\"`]+|['\"`]+$", "", normalized)
        lowered = normalized.lower()
        if lowered.startswith("i need "):
            normalized = normalized[7:]
        elif lowered.startswith("create "):
            normalized = normalized[7:]
        elif lowered.startswith("build "):
            normalized = normalized[6:]
        elif lowered.startswith("make "):
            normalized = normalized[5:]
        normalized = re.split(r"[.!?\n]", normalized, maxsplit=1)[0].strip(" ,;-:")
        if not normalized:
            return ""

        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", normalized)
        meaningful: list[str] = []
        for token in tokens:
            lowered_token = token.lower()
            if lowered_token in WORKSPACE_NAME_STOPWORDS:
                continue
            meaningful.append(token)
            if len(meaningful) >= 5:
                break
        if not meaningful:
            meaningful = tokens[:4]
        if not meaningful:
            return ""
        title = " ".join(word.capitalize() if not word.isupper() else word for word in meaningful)
        title = re.sub(r"\s+", " ", title).strip()
        return title[:48].rstrip(" -_,")

    @staticmethod
    def _strip_prompt_time_prefix(prompt: str) -> str:
        lines = str(prompt or "").splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        timestamp_pattern = re.compile(
            r"^\s*(?:"
            r"(?:[01]?\d|2[0-3])[:.][0-5]\d(?:\s*(?:am|pm))?"
            r"|(?:1[0-2]|0?[1-9])[:.][0-5]\d\s*(?:am|pm)"
            r")\s*$",
            re.IGNORECASE,
        )
        while lines and timestamp_pattern.match(lines[0]):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        return "\n".join(lines).strip()

    def list_runs(self, workspace_id: str) -> list[RunRecord]:
        runs = [
            self._hydrate_run_observability(RunRecord.model_validate(item), persist=True)
            for item in self.store.list("runs")
            if item["workspace_id"] == workspace_id
        ]
        runs.sort(key=lambda item: item.created_at, reverse=True)
        return runs

    def get_run(self, run_id: str) -> RunRecord:
        payload = self.store.get("runs", run_id)
        if not payload:
            raise KeyError(f"Run not found: {run_id}")
        return self._hydrate_run_observability(RunRecord.model_validate(payload), persist=True)

    def _recover_orphaned_active_runs(self) -> None:
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(seconds=ACTIVE_RUN_RECOVERY_STALE_SECONDS)
        for item in self.store.list("runs"):
            run = RunRecord.model_validate(item)
            if run.status not in {"pending", "running"} and run.current_stage != "stopping":
                continue
            if run.run_id in self._active_workers:
                continue
            if run.updated_at > stale_cutoff:
                continue
            if run.updated_at >= self._startup_started_at:
                continue
            stop_requested = self._is_stop_requested(run.run_id) or run.current_stage == "stopping"
            if stop_requested:
                run.status = "blocked"
                run.apply_status = "blocked"
                run.current_stage = "stopped"
                run.summary = run.summary or "Run was stopped during stale-run recovery."
                run.failure_reason = run.failure_reason or "Run was interrupted after a stop request and recovered during backend restart cleanup."
                run.outcome_kind = run.outcome_kind or "blocked_generation"
            elif self._recover_stale_run_with_retained_draft(run):
                pass
            else:
                run.status = "failed"
                run.apply_status = "failed"
                run.current_stage = "failed"
                run.summary = run.summary or "Run was interrupted before reaching a terminal state."
                run.failure_reason = run.failure_reason or "Run was recovered as stale after backend restart because no active worker existed."
                run.outcome_kind = run.outcome_kind or "blocked_generation"
            run.progress_percent = self._terminal_failure_progress(run.progress_percent)
            run.updated_at = now
            self._save_run(run)
            self._sync_linked_job_to_terminal_run_state(run, reason="stale_run_recovery")
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            logger.warning(
                "recovered_orphaned_run run_id=%s workspace_id=%s status=%s stage=%s",
                run.run_id,
                run.workspace_id,
                run.status,
                run.current_stage,
            )

    def _recover_stale_run_with_retained_draft(self, run: RunRecord) -> bool:
        if not self.workspace_service.draft_exists(run.workspace_id, run.run_id):
            return False
        try:
            draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id)
        except Exception:
            draft_diff = ""
        if not draft_diff.strip():
            return False
        run.status = "blocked"
        run.apply_status = "blocked"
        run.draft_status = "ready"
        run.draft_ready = True
        run.current_stage = "interrupted; draft retained"
        run.summary = "Run was interrupted after generating a draft. The draft was retained and can be resumed."
        run.failure_reason = (
            "Backend restarted or the worker disappeared while the agent was repairing the generated draft. "
            "The draft changes were kept instead of being discarded."
        )
        run.failure_class = run.failure_class or "generation.interrupted_stale_worker"
        run.outcome_kind = run.outcome_kind or "blocked_generation"
        target_platform = getattr(run, "target_platform", None)
        preview_profile = getattr(run, "preview_profile", None)
        run.resume_checkpoint_ref = self._resume_checkpoint_ref_for_run(run)
        checkpoint = {
            "status": "pending",
            "source_run_id": run.run_id,
            "prompt": run.prompt,
            "intent": run.intent,
            "target_role_scope": list(run.target_role_scope),
            "model_profile": run.model_profile,
            "target_platform": str(getattr(target_platform, "value", target_platform) or "telegram_mini_app"),
            "preview_profile": str(getattr(preview_profile, "value", preview_profile) or "telegram_mock"),
            "generation_mode": str(getattr(run.generation_mode, "value", run.generation_mode) or "balanced"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "stale_run_recovery_retained_draft",
        }
        self.store.upsert("reports", run.resume_checkpoint_ref, checkpoint)
        return True

    @staticmethod
    def _resume_checkpoint_ref_for_run(run: RunRecord) -> str:
        return run.resume_checkpoint_ref or f"resume_checkpoint:{run.workspace_id}:{run.run_id}"

    def _load_run_resume_checkpoint(self, run: RunRecord) -> dict[str, Any] | None:
        payload = self.store.get("reports", self._resume_checkpoint_ref_for_run(run))
        return dict(payload) if isinstance(payload, dict) else None

    def _record_session_checkpoint(
        self,
        run: RunRecord,
        *,
        kind: str,
        status: str,
        source: str,
        summary: str,
        refs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = f"session_checkpoint:{run.workspace_id}:{run.run_id}:{kind}"
        payload = {
            "schema": "grounded.session_checkpoint.v1",
            "checkpoint_id": checkpoint_id,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "kind": kind,
            "status": status,
            "source": source,
            "summary": summary,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_status": run.status,
            "apply_status": run.apply_status,
            "current_stage": run.current_stage,
            "revision_id": run.result_revision_id or run.candidate_revision_id or run.source_revision_id,
            "failure_class": run.failure_class,
            "failure_signature": run.failure_signature,
            "refs": dict(refs or {}),
            "metadata": dict(metadata or {}),
        }
        self.store.upsert("reports", checkpoint_id, payload)
        return payload

    def _record_before_apply_checkpoint(self, run: RunRecord, *, source: str) -> None:
        try:
            draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id)
        except Exception:
            draft_diff = ""
        self._record_session_checkpoint(
            run,
            kind="before_apply",
            status="ready",
            source=source,
            summary="Checkpoint captured before applying the generated draft.",
            refs={
                "draft_run_id": run.run_id,
                "source_revision_id": run.source_revision_id,
                "run_artifacts": f"run_artifacts:{run.run_id}",
            },
            metadata={
                "diff_sha256": hashlib.sha256(draft_diff.encode("utf-8")).hexdigest() if draft_diff else None,
                "changed_files": list(run.touched_files or []),
            },
        )

    def _recover_orphaned_terminal_jobs(self) -> None:
        for item in self.store.list("jobs"):
            job = JobRecord.model_validate(item)
            if job.status not in {"pending", "running"}:
                continue
            run_id = str(job.linked_run_id or "").strip()
            if not run_id:
                continue
            run_payload = self.store.get("runs", run_id)
            if not run_payload:
                continue
            run = RunRecord.model_validate(run_payload)
            if run.status not in {"completed", "blocked", "failed"}:
                continue
            self._sync_linked_job_to_terminal_run_state(run, reason="terminal_run_job_sync")
            logger.warning(
                "recovered_orphaned_job job_id=%s run_id=%s workspace_id=%s job_status=%s run_status=%s",
                job.job_id,
                run.run_id,
                run.workspace_id,
                job.status,
                run.status,
            )

    def _sync_linked_job_to_terminal_run_state(self, run: RunRecord, *, reason: str) -> None:
        job_id = self._resolve_linked_job_id(run)
        if not job_id:
            return
        payload = self.store.get("jobs", job_id)
        if not payload:
            return
        job = JobRecord.model_validate(payload)
        if run.status == "completed":
            target_status = "completed"
            event_type = "job_completed"
            message = run.summary or "Job was synchronized to the completed run state."
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
        elif run.status == "blocked":
            target_status = "blocked"
            event_type = "job_failed"
            message = run.failure_reason or run.summary or "Job was synchronized to the blocked run state."
            job.failure_reason = run.failure_reason or message
            job.failure_class = run.failure_class
            job.failure_signature = run.failure_signature
            job.root_cause_summary = run.root_cause_summary
        elif run.status == "failed":
            target_status = "failed"
            event_type = "job_failed"
            message = run.failure_reason or run.summary or "Job was synchronized to the failed run state."
            job.failure_reason = run.failure_reason or message
            job.failure_class = run.failure_class
            job.failure_signature = run.failure_signature
            job.root_cause_summary = run.root_cause_summary
        else:
            return
        if job.status == target_status and (
            target_status == "completed" or (job.failure_reason or "") == (run.failure_reason or "")
        ):
            return
        job.status = target_status
        job.outcome_kind = run.outcome_kind
        job.summary = message
        job.linked_run_id = run.run_id
        job.updated_at = datetime.now(timezone.utc)
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
        self._append_job_event(
            job.job_id,
            event_type,
            message,
            {"reason": reason, "run_id": run.run_id, "run_status": run.status},
        )

    def _resolve_linked_job_id(self, run: RunRecord) -> str:
        direct_job_id = str(run.linked_job_id or "").strip()
        if direct_job_id:
            return direct_job_id
        latest_linked_job: JobRecord | None = None
        for item in self.store.list("jobs"):
            if str(item.get("linked_run_id") or "").strip() != run.run_id:
                continue
            candidate = JobRecord.model_validate(item)
            if latest_linked_job is None or candidate.updated_at > latest_linked_job.updated_at:
                latest_linked_job = candidate
        if latest_linked_job is None:
            return ""
        if run.linked_job_id != latest_linked_job.job_id:
            run.linked_job_id = latest_linked_job.job_id
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
        return latest_linked_job.job_id

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            run = self.get_run(run_id)
            if run.linked_job_id:
                job = self.code_agent_runtime.get_job(run.linked_job_id)
                preview = self.preview_service.get(run.workspace_id)
                change_plan = self._build_change_plan(
                    workspace_id=run.workspace_id,
                    run=run,
                    diff_text=self.workspace_service.diff(run.workspace_id, run_id=run.run_id if run.draft_ready else None),
                    prompt=run.prompt,
                )
                self._store_run_artifacts(run, change_plan, job, preview)
                payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            raise KeyError(f"Artifacts not found for run: {run_id}")
        run = self.get_run(run_id)
        preview = self.preview_service.get(run.workspace_id)
        preview_payload = self._preview_snapshot(run.workspace_id, preview)
        payload["run"] = run.model_dump(mode="json")
        payload["preview"] = preview_payload
        payload["draft_preview"] = {
            key: value
            for key, value in preview_payload.items()
            if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
        }
        self.store.upsert("reports", f"run_artifacts:{run_id}", payload)
        return payload

    def reconcile_run_with_gate(self, run_id: str, gate: dict[str, Any]) -> RunRecord:
        payload = self.store.get("runs", run_id)
        if not payload:
            raise KeyError(f"Run not found: {run_id}")
        run = RunRecord.model_validate(payload)
        if run.status not in TERMINAL_RUN_STATUSES:
            return run
        artifacts = self.store.get("reports", f"run_artifacts:{run_id}") if run_id else {}
        browser_proof = self.store.get("reports", run.browser_proof_ref) if run.browser_proof_ref else {}
        state = RunStateMachine.evaluate(
            run=run,
            gate=gate,
            artifacts=artifacts if isinstance(artifacts, dict) else {},
            browser_proof=browser_proof if isinstance(browser_proof, dict) else {},
        )
        self.store.upsert("reports", f"run_state:{run_id}", state)
        changed = False
        first_issue = next((item for item in state.get("issues") or [] if isinstance(item, dict) and item.get("blocking", True)), {})
        if state.get("blocking"):
            if run.status != "failed" and run.status != "blocked":
                run.status = "blocked"
                changed = True
            if run.status != "failed" and run.apply_status not in {"applied", "awaiting_approval"}:
                run.apply_status = "blocked"
                changed = True
            if run.status != "failed" and run.current_stage != "blocked by reliability gate":
                run.current_stage = "blocked by reliability gate"
                changed = True
            if not run.failure_class:
                run.failure_class = "reliability_gate.blocked"
                changed = True
            if not run.failure_signature:
                run.failure_signature = f"reliability_gate.blocked:{str(first_issue.get('check') or 'unknown')}"
                changed = True
            details = str(first_issue.get("details") or "").strip()
            if details and (not run.failure_reason or self._is_nonspecific_failure_reason(run.failure_reason)):
                run.failure_reason = details
                changed = True
        elif state.get("status") == "passed" and run.apply_status == "applied":
            if run.status != "completed":
                run.status = "completed"
                changed = True
            if run.current_stage != "completed":
                run.current_stage = "completed"
                changed = True
            if run.failure_reason or run.failure_class or run.failure_signature or run.root_cause_summary:
                run.failure_reason = None
                run.failure_class = None
                run.failure_signature = None
                run.root_cause_summary = None
                changed = True
        elif state.get("manual_approval_ok"):
            if run.status != "awaiting_approval":
                run.status = "awaiting_approval"
                changed = True
            if run.apply_status != "awaiting_approval":
                run.apply_status = "awaiting_approval"
                changed = True
        gate_status = "blocked" if state.get("blocking") else "passed" if state.get("status") == "passed" else "pending"
        if run.checks_summary.gate_status != gate_status:
            run.checks_summary.gate_status = gate_status  # type: ignore[assignment]
            changed = True
        if changed:
            run.progress_percent = 100 if run.status in TERMINAL_RUN_STATUSES else run.progress_percent
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
            event_type = "run.blocked" if run.status == "blocked" else "run.failed" if run.status == "failed" else "run.completed" if run.status == "completed" else "run.status_changed"
            self._journal_run(
                run,
                event_type,
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "apply_status": run.apply_status,
                    "current_stage": run.current_stage,
                    "gate_status": gate_status,
                    "blocking": state.get("blocking"),
                    "issues": state.get("issues") or [],
                },
                summary=f"Run status changed to {run.status}.",
                idempotency_key=f"run.status_changed:{run.run_id}:{run.updated_at.isoformat()}",
            )
            artifacts_payload = self.store.get("reports", f"run_artifacts:{run_id}")
            if isinstance(artifacts_payload, dict):
                artifacts_payload["run"] = run.model_dump(mode="json")
                artifacts_payload["run_state"] = state
                self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts_payload)
        return run

    def get_run_iterations(self, run_id: str) -> list[dict[str, Any]]:
        artifacts = self.get_run_artifacts(run_id)
        return list(artifacts.get("iterations", []) or [])

    def enforce_guardian_before_apply(
        self,
        run: RunRecord,
        *,
        source: str,
        changed_files: list[str] | None = None,
        semantic_override: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        gate = self.guardian_gate_service.run_gate(run=run, source=source, changed_files=changed_files, semantic_override=semantic_override)
        report = gate.model_dump(mode="json", by_alias=True)
        run.guardian_gate_ref = gate.guardian_gate_ref
        if gate.apply_decision == "allow" and gate.status == "passed":
            self._save_run(run)
            return True, report
        findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
        run.status = "blocked"
        run.apply_status = "blocked"
        run.outcome_kind = "blocked_generation"
        run.draft_status = "ready" if self.workspace_service.draft_exists(run.workspace_id, run.run_id) else run.draft_status
        run.draft_ready = run.draft_status == "ready"
        run.current_stage = "guardian review blocked apply"
        run.progress_percent = self._terminal_failure_progress(run.progress_percent)
        run.failure_class = "guardian.pre_apply_blocked"
        run.failure_signature = f"guardian.pre_apply_blocked:{findings[0].get('code') if findings else 'blocker'}"
        run.failure_reason = findings[0].get("message") if findings else "Guardian review blocked source apply."
        run.remaining_issues = [
            {
                "kind": "guardian_blocker",
                "code": item.get("code"),
                "category": item.get("category"),
                "details": item.get("message"),
                "file_path": item.get("file_path"),
                "line": item.get("line"),
                "blocking": True,
                "evidence": item.get("evidence") or {},
            }
            for item in findings
        ]
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(
            run.linked_job_id,
            "validation_failed",
            "Guardian review blocked source apply.",
            {"run_id": run.run_id, "guardian_gate_ref": gate.guardian_gate_ref, "guardian_review_ref": gate.deterministic_review_ref, "findings": findings, "repair_packets": report.get("repair_packets") or []},
        )
        self._journal_run(
            run,
            "guardian.apply_blocked",
            {"run_id": run.run_id, "workspace_id": run.workspace_id, "guardian_gate_ref": gate.guardian_gate_ref, "findings": findings, "repair_packets": report.get("repair_packets") or []},
            summary="Guardian review blocked source apply.",
            idempotency_key=f"guardian.apply_blocked:{run.run_id}:{report.get('created_at')}",
        )
        return False, report

    def _guardian_review_for_apply(
        self,
        run: RunRecord,
        *,
        source: str,
        changed_files: list[str] | None = None,
    ) -> dict[str, Any]:
        stored = self._stored_guardian_review(run)
        if stored and source != "pre_apply_guardian" and stored.get("status") == "passed":
            self.store.upsert("reports", f"guardian_review:{run.workspace_id}:{run.run_id}", stored)
            return stored
        artifacts = self.store.get("reports", f"run_artifacts:{run.run_id}")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        raw_checks = artifacts.get("check_results") if isinstance(artifacts.get("check_results"), list) else []
        if not raw_checks and run.linked_job_id:
            job_payload = self.store.get("jobs", run.linked_job_id)
            raw_checks = job_payload.get("executed_checks") if isinstance(job_payload, dict) and isinstance(job_payload.get("executed_checks"), list) else []
        results: list[RunCheckResult] = []
        for item in raw_checks or []:
            if not isinstance(item, dict):
                continue
            try:
                results.append(RunCheckResult.model_validate(item))
            except Exception:
                continue
        execution = CheckExecutionRecord(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            changed_files=list(changed_files or run.touched_files or self._paths_from_diff(str(artifacts.get("diff") or ""))),
            results=results,
            completed_at=datetime.now(timezone.utc),
        ) if results else None
        draft_source = self.workspace_service.draft_source_dir(run.workspace_id, run.run_id)
        if not draft_source.exists():
            draft_source = self.workspace_service.source_dir(run.workspace_id)
        report = GuardianReview.review(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            draft_source=draft_source,
            changed_files=list(changed_files or run.touched_files or self._paths_from_diff(str(artifacts.get("diff") or ""))),
            latest_execution=execution,
            preview_details=artifacts.get("preview") if isinstance(artifacts.get("preview"), dict) else {},
            acceptance_contract=run.acceptance_contract,
            implementation_plan=run.implementation_plan,
            target_role_scope=run.target_role_scope,
            intent=run.intent,
            source="pre_apply_guardian",
            review_context={
                "run": run.model_dump(mode="json"),
                "diff": str(artifacts.get("diff") or ""),
                "token_usage": run.token_usage,
                "context_pressure": self.store.get("reports", run.context_pressure_ref) if run.context_pressure_ref else {},
            },
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", f"guardian_review:{run.workspace_id}:{run.run_id}", report)
        return report

    def _stored_guardian_review(self, run: RunRecord) -> dict[str, Any] | None:
        candidates: list[Any] = []
        if run.verifier_review_ref:
            candidates.append(self.store.get("reports", run.verifier_review_ref))
        candidates.append(self.store.get("reports", f"guardian_review:{run.workspace_id}:{run.run_id}"))
        for payload in candidates:
            if not isinstance(payload, dict):
                continue
            if payload.get("schema") == "grounded.guardian_review.v1":
                return dict(payload)
            for key in ("review", "report"):
                review = payload.get(key) if isinstance(payload.get(key), dict) else None
                if isinstance(review, dict) and isinstance(review.get("guardian_review"), dict):
                    return dict(review["guardian_review"])
            if isinstance(payload.get("guardian_review"), dict):
                return dict(payload["guardian_review"])
        return None

    def apply_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.apply_strategy != "manual_approve":
            return run
        if run.status != "awaiting_approval":
            return run
        allowed, _ = self.enforce_guardian_before_apply(run, source="pre_apply_guardian")
        if not allowed:
            return self.get_run(run_id)
        apply_started_at = time.perf_counter()
        run.current_stage = "finalizing apply"
        run.progress_percent = 99
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._journal_run(
            run,
            "apply.started",
            {"run_id": run.run_id, "status": run.status, "apply_status": run.apply_status, "current_stage": run.current_stage},
            summary="Manual apply started.",
            idempotency_key=f"apply.started:{run.run_id}:{run.updated_at.isoformat()}",
        )
        self._append_job_event(run.linked_job_id, "apply_started", "Applying the reviewed draft to the source workspace.")
        self._record_before_apply_checkpoint(run, source="manual_apply")
        revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Approve AI draft for run {run.run_id}")
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.status = "completed"
        run.apply_status = "applied"
        run.outcome_kind = "applied"
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.latency_breakdown["apply_ms"] = int((time.perf_counter() - apply_started_at) * 1000)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._journal_run(
            run,
            "apply.applied",
            {
                "run_id": run.run_id,
                "status": run.status,
                "apply_status": run.apply_status,
                "revision_id": revision.revision_id,
            },
            summary="Draft was applied successfully.",
            idempotency_key=f"apply.applied:{run.run_id}:{revision.revision_id}",
        )
        self._append_job_event(run.linked_job_id, "apply_completed", "Draft was applied successfully.")
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Run draft applied manually.",
            payload={"run_id": run.run_id, "revision_id": revision.revision_id},
        )
        self._queue_preview_refresh(run, reason="manual approval")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.store.delete("reports", f"run_stop_request:{run_id}")
        return run

    def discard_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.status = "failed"
        run.apply_status = "failed"
        run.draft_status = "discarded"
        run.draft_ready = False
        run.current_stage = "discarded"
        run.progress_percent = 100
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._journal_run(
            run,
            "apply.discarded",
            {"run_id": run.run_id, "status": run.status, "apply_status": run.apply_status, "draft_status": run.draft_status},
            summary="Run draft discarded.",
            idempotency_key=f"apply.discarded:{run.run_id}:{run.updated_at.isoformat()}",
        )
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Run draft discarded.",
            payload={"run_id": run.run_id},
        )
        self._queue_preview_refresh(run, reason="draft discard")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.store.delete("reports", f"run_stop_request:{run_id}")
        return run

    def rollback_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.rolled_back:
            return run
        if run.status != "completed" or run.apply_status != "applied" or not run.result_revision_id:
            raise ValueError("Only applied completed runs can be rolled back.")

        revision = self.workspace_service.revert_revision(
            run.workspace_id,
            run.result_revision_id,
            f"Rollback AI run {run.run_id}",
        )
        run.rolled_back = True
        run.rolled_back_at = datetime.now(timezone.utc)
        run.apply_status = "rolled_back"
        run.current_stage = "rolled back"
        run.progress_percent = 100
        run.candidate_revision_id = revision.revision_id
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._journal_run(
            run,
            "run.rollback",
            {
                "run_id": run.run_id,
                "status": run.status,
                "apply_status": run.apply_status,
                "source_revision_id": run.result_revision_id,
                "rollback_revision_id": revision.revision_id,
            },
            summary="Applied run rolled back.",
            idempotency_key=f"run.rollback:{run.run_id}:{revision.revision_id}",
        )
        self.workspace_log_service.append(
            run.workspace_id,
            source="run",
            message="Applied run rolled back.",
            payload={"run_id": run.run_id, "revision_id": revision.revision_id},
        )
        self._queue_preview_refresh(run, reason="rollback")
        artifacts = self.get_run_artifacts(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        return run

    def write_process_stdin(self, run_id: str, process_id: str, data: str) -> dict[str, Any]:
        self.get_run(run_id)
        result = self.code_agent_runtime.write_process_stdin(process_id, data)
        result["run_id"] = run_id
        return result

    def terminate_process(self, run_id: str, process_id: str) -> dict[str, Any]:
        self.get_run(run_id)
        result = self.code_agent_runtime.terminate_process(process_id)
        result["run_id"] = run_id
        return result

    def read_process_output(
        self,
        run_id: str,
        process_id: str,
        *,
        stream: str = "stdout",
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        self.get_run(run_id)
        result = self.code_agent_runtime.read_process_output(process_id, stream=stream, start=start, end=end)
        result["run_id"] = run_id
        return result

    def _journal_run(
        self,
        run: RunRecord,
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
            self.event_journal_service.append_run(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                summary=summary,
                source_ref=source_ref,
                idempotency_key=idempotency_key,
            )
        except Exception:
            logger.exception("Failed to append run journal event %s for run %s.", event_type, run.run_id)

    def _save_run(self, run: RunRecord) -> None:
        existing = self.store.get("runs", run.run_id)
        if isinstance(existing, dict):
            existing_usage = existing.get("token_usage") if isinstance(existing.get("token_usage"), dict) else {}
            run.token_usage = self._richer_token_usage(run.token_usage, existing_usage)
            successful_clear = run.status == "completed" and run.apply_status == "applied"
            if not successful_clear:
                for attr in ("failure_reason", "failure_class", "failure_signature", "root_cause_summary"):
                    if not getattr(run, attr, None) and existing.get(attr):
                        setattr(run, attr, str(existing.get(attr) or ""))
            if not run.linked_job_id and existing.get("linked_job_id"):
                run.linked_job_id = str(existing.get("linked_job_id") or "")
            for attr in ("orchestration_phases", "worker_summaries", "repair_issue_signatures", "agent_activity_events"):
                if not getattr(run, attr, None) and isinstance(existing.get(attr), list):
                    setattr(run, attr, list(existing.get(attr) or []))
            for attr in (
                "agent_transcript_ref",
                "tool_trace_ref",
                "file_change_history_ref",
                "browser_proof_ref",
                "browser_replay_proof_ref",
                "large_tool_outputs_ref",
                "file_state_cache_ref",
                "turn_diff_ref",
                "environment_snapshot_ref",
                "tool_batch_summaries_ref",
                "task_ledger_ref",
                "worker_mailbox_ref",
                "worker_sessions_ref",
                "worker_ownership_ref",
                "draft_isolation_ref",
                "draft_gate_ref",
                "draft_apply_decision_ref",
                "guardian_gate_ref",
                "scratchpad_ref",
                "memory_ref",
                "worker_drafts_ref",
                "worker_merge_ref",
                "trace_bundle_ref",
                "trace_reducer_ref",
                "command_policy_ref",
                "verification_report_ref",
                "rollout_trace_ref",
                "exec_trace_ref",
                "process_outputs_ref",
                "tool_result_messages_ref",
                "artifact_read_trace_ref",
                "resume_checkpoint_ref",
                "verifier_review_ref",
                "lsp_context_ref",
                "context_manager_ref",
                "context_pressure_ref",
                "hook_trace_ref",
                "semantic_graph_ref",
                "worker_prefix_ref",
                "replay_trace_ref",
                "prompt_contract_ref",
                "miniapp_contract_ref",
                "route_registry_ref",
                "contract_compile_ref",
                "repair_recipes_ref",
            ):
                if not getattr(run, attr, None) and existing.get(attr):
                    setattr(run, attr, str(existing.get(attr) or ""))
            for attr in ("active_processes", "worker_branch_refs", "browser_step_refs"):
                if not getattr(run, attr, None) and isinstance(existing.get(attr), list):
                    setattr(run, attr, list(existing.get(attr) or []))
            for attr in (
                "implementation_plan",
                "agent_memory",
                "acceptance_contract",
                "flow_coverage",
                "browser_flow_proof",
                "mobile_layout_report",
                "completion_budget",
                "budget_status",
            ):
                if not getattr(run, attr, None) and isinstance(existing.get(attr), dict):
                    setattr(run, attr, dict(existing.get(attr) or {}))
            for attr in ("event_storage_ref", "artifact_storage_ref"):
                if not getattr(run, attr, None) and existing.get(attr):
                    setattr(run, attr, str(existing.get(attr) or ""))
            if int(existing.get("storage_version") or 0) > int(run.storage_version or 0):
                run.storage_version = int(existing.get("storage_version") or run.storage_version)
            try:
                run.iteration_count = max(int(run.iteration_count or 0), int(existing.get("iteration_count") or 0))
            except (TypeError, ValueError):
                pass
        if run.status in {"failed", "blocked"}:
            run.progress_percent = self._terminal_failure_progress(run.progress_percent)
        run.budget_status = self._budget_status_with_token_usage(run.budget_status, run.token_usage)
        self.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    @staticmethod
    def _token_usage_weight(usage: dict[str, Any] | None) -> int:
        if not isinstance(usage, dict):
            return 0
        try:
            return int(usage.get("turn_count") or 0) * 1_000_000 + int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _richer_token_usage(cls, candidate: dict[str, Any] | None, existing: dict[str, Any] | None) -> dict[str, Any]:
        candidate = dict(candidate or {}) if isinstance(candidate, dict) else {}
        existing = dict(existing or {}) if isinstance(existing, dict) else {}
        candidate_weight = cls._token_usage_weight(candidate)
        existing_weight = cls._token_usage_weight(existing)
        if candidate_weight > existing_weight:
            return candidate
        if existing_weight > candidate_weight:
            return existing
        candidate_explicit = cls._token_usage_has_explicit_values(candidate)
        existing_explicit = cls._token_usage_has_explicit_values(existing)
        if candidate_explicit and not existing_explicit:
            return candidate
        if existing_explicit and not candidate_explicit:
            return existing
        return candidate if len(candidate) >= len(existing) else existing

    @staticmethod
    def _token_usage_has_explicit_values(usage: dict[str, Any] | None) -> bool:
        if not isinstance(usage, dict) or not usage:
            return False
        usage_keys = {"input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "turn_count", "last_turn"}
        return any(key in usage for key in usage_keys)

    @staticmethod
    def _token_usage_from_prompt_analysis(usage: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(usage, dict) or not usage:
            return {}
        result: dict[str, Any] = {"turn_count": 1, "prompt_analysis": dict(usage)}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
            try:
                result[key] = int(usage.get(key) or 0)
            except (TypeError, ValueError):
                result[key] = 0
        result["last_turn"] = dict(usage)
        return result

    @staticmethod
    def _budget_status_with_token_usage(budget_status: dict[str, Any] | None, token_usage: dict[str, Any] | None) -> dict[str, Any]:
        status = dict(budget_status or {}) if isinstance(budget_status, dict) else {}
        if not status or not isinstance(token_usage, dict):
            return status
        try:
            total_tokens = int(token_usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            total_tokens = 0
        if total_tokens:
            status["total_tokens"] = total_tokens
        return status

    @staticmethod
    def _terminal_failure_progress(progress: int | float | None) -> int:
        del progress
        return 100

    @staticmethod
    def _preview_refresh_progress(progress: int | float | None) -> int:
        try:
            current = int(progress or 0)
        except (TypeError, ValueError):
            current = 0
        return max(98, min(current, 99))

    def _hydrate_run_observability(self, run: RunRecord, *, persist: bool = False) -> RunRecord:
        changed = False
        job_id = str(run.linked_job_id or "").strip() or self._resolve_linked_job_id(run)
        job_payload = self.store.get("jobs", job_id) if job_id else None
        job: JobRecord | None = JobRecord.model_validate(job_payload) if isinstance(job_payload, dict) else None
        if job is not None:
            usage = self._richer_token_usage(run.token_usage, job.token_usage)
            usage = self._richer_token_usage(usage, self._token_usage_from_job_events(job))
            if usage and usage != run.token_usage:
                run.token_usage = usage
                changed = True
            synced_budget_status = self._budget_status_with_token_usage(run.budget_status, run.token_usage)
            if synced_budget_status != run.budget_status:
                run.budget_status = synced_budget_status
                changed = True
            if not run.linked_job_id:
                run.linked_job_id = job.job_id
                changed = True
            if job.event_storage_ref and run.event_storage_ref != job.event_storage_ref:
                run.event_storage_ref = job.event_storage_ref
                changed = True
            if job.storage_version and job.storage_version > run.storage_version:
                run.storage_version = job.storage_version
                changed = True
            if run.status in {"failed", "blocked"} and self._is_nonspecific_failure_reason(run.failure_reason):
                specific = self._specific_failure_reason_from_job(job)
                if specific and specific != run.failure_reason:
                    run.failure_reason = specific
                    changed = True
            for attr in ("orchestration_phases", "worker_summaries", "repair_issue_signatures", "agent_activity_events"):
                if not getattr(run, attr, None) and getattr(job, attr, None):
                    setattr(run, attr, list(getattr(job, attr) or []))
                    changed = True
            for attr in (
                "agent_transcript_ref",
                "tool_trace_ref",
                "file_change_history_ref",
                "browser_proof_ref",
                "browser_replay_proof_ref",
                "large_tool_outputs_ref",
                "file_state_cache_ref",
                "turn_diff_ref",
                "environment_snapshot_ref",
                "tool_batch_summaries_ref",
                "worker_mailbox_ref",
                "worker_sessions_ref",
                "worker_ownership_ref",
                "draft_isolation_ref",
                "draft_gate_ref",
                "draft_apply_decision_ref",
                "guardian_gate_ref",
                "scratchpad_ref",
                "memory_ref",
                "worker_drafts_ref",
                "worker_merge_ref",
                "trace_bundle_ref",
                "trace_reducer_ref",
                "command_policy_ref",
                "verification_report_ref",
                "rollout_trace_ref",
                "exec_trace_ref",
                "process_outputs_ref",
                "tool_result_messages_ref",
                "artifact_read_trace_ref",
                "resume_checkpoint_ref",
                "verifier_review_ref",
                "lsp_context_ref",
                "context_manager_ref",
                "context_pressure_ref",
                "hook_trace_ref",
                "semantic_graph_ref",
                "worker_prefix_ref",
                "replay_trace_ref",
                "miniapp_contract_ref",
                "route_registry_ref",
                "contract_compile_ref",
                "repair_recipes_ref",
            ):
                if not getattr(run, attr, None) and getattr(job, attr, None):
                    setattr(run, attr, str(getattr(job, attr) or ""))
                    changed = True
            for attr in ("active_processes", "worker_branch_refs", "browser_step_refs"):
                if not getattr(run, attr, None) and getattr(job, attr, None):
                    setattr(run, attr, list(getattr(job, attr) or []))
                    changed = True
            for attr in (
                "implementation_plan",
                "agent_memory",
                "acceptance_contract",
                "flow_coverage",
                "browser_flow_proof",
                "mobile_layout_report",
                "completion_budget",
                "budget_status",
            ):
                if not getattr(run, attr, None) and getattr(job, attr, None):
                    setattr(run, attr, dict(getattr(job, attr) or {}))
                    changed = True
        if self._reconcile_terminal_run_state_from_authoritative_records(run, job):
            changed = True
        synced_budget_status = self._budget_status_with_token_usage(run.budget_status, run.token_usage)
        if synced_budget_status != run.budget_status:
            run.budget_status = synced_budget_status
            changed = True
        if run.status in {"failed", "blocked"} and int(run.progress_percent or 0) != 100:
            run.progress_percent = self._terminal_failure_progress(run.progress_percent)
            changed = True
        if self._failed_run_has_retained_draft(run):
            run.status = "blocked"
            run.apply_status = "blocked"
            run.current_stage = "blocked"
            run.summary = run.summary or "Strict-green validation did not pass. Draft was retained for inspection."
            changed = True
        if persist and changed:
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
        return run

    def _reconcile_terminal_run_state_from_authoritative_records(
        self,
        run: RunRecord,
        job: JobRecord | None,
    ) -> bool:
        """Repair stale active run rows from terminal job/artifact snapshots.

        The platform stores heavy run evidence separately from the indexed run
        row. A preview refresh can complete after artifacts/job have already
        reached a terminal state, so reads must reconcile the indexed row before
        the UI/API decides gate status.
        """
        if run.status in TERMINAL_RUN_STATUSES and run.apply_status != "pending":
            return False
        source = self._terminal_run_snapshot_from_artifacts(run.run_id)
        source_reason = "run_artifacts"
        if source is None:
            source = self._terminal_run_snapshot_from_job(run, job)
            source_reason = "linked_job"
        if source is None:
            return False
        before = {
            "status": run.status,
            "apply_status": run.apply_status,
            "current_stage": run.current_stage,
            "updated_at": run.updated_at.isoformat(),
        }
        changed = self._merge_terminal_run_snapshot(run, source)
        if not changed:
            return False
        run.updated_at = datetime.now(timezone.utc)
        self.store.upsert(
            "reports",
            f"run_state_reconciliation:{run.run_id}",
            {
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "source": source_reason,
                "before": before,
                "after": {
                    "status": run.status,
                    "apply_status": run.apply_status,
                    "current_stage": run.current_stage,
                    "updated_at": run.updated_at.isoformat(),
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return True

    def _terminal_run_snapshot_from_artifacts(self, run_id: str) -> RunRecord | None:
        payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not isinstance(payload, dict):
            return None
        candidate = payload.get("run")
        if not isinstance(candidate, dict):
            return None
        try:
            snapshot = RunRecord.model_validate(candidate)
        except Exception:
            return None
        if snapshot.run_id != run_id:
            return None
        if snapshot.status not in TERMINAL_RUN_STATUSES:
            return None
        if snapshot.status == "completed" and snapshot.apply_status != "applied":
            return None
        if snapshot.status == "awaiting_approval" and snapshot.apply_status != "awaiting_approval":
            return None
        return snapshot

    def _terminal_run_snapshot_from_job(self, run: RunRecord, job: JobRecord | None) -> RunRecord | None:
        if job is None or job.status not in {"completed", "blocked", "failed"}:
            return None
        snapshot = run.model_copy(deep=True)
        snapshot.linked_job_id = job.job_id
        snapshot.summary = job.summary or snapshot.summary
        snapshot.failure_reason = job.failure_reason
        snapshot.failure_class = job.failure_class
        snapshot.failure_signature = job.failure_signature
        snapshot.root_cause_summary = job.root_cause_summary
        snapshot.outcome_kind = job.outcome_kind or snapshot.outcome_kind
        snapshot.current_fix_phase = job.current_fix_phase or snapshot.current_fix_phase
        snapshot.current_failing_command = job.current_failing_command or snapshot.current_failing_command
        snapshot.current_exit_code = job.current_exit_code if job.current_exit_code is not None else snapshot.current_exit_code
        snapshot.remaining_issues = list(job.remaining_issues or snapshot.remaining_issues)
        snapshot.token_usage = self._richer_token_usage(self._richer_token_usage(snapshot.token_usage, job.token_usage), self._token_usage_from_job_events(job))
        snapshot.latency_breakdown = dict(job.latency_breakdown or snapshot.latency_breakdown)
        snapshot.iteration_count = max(int(snapshot.iteration_count or 0), len(job.repair_iterations or []))
        snapshot.repair_iterations = list(job.repair_iterations or snapshot.repair_iterations)
        snapshot.repair_issue_signatures = list(job.repair_issue_signatures or snapshot.repair_issue_signatures)
        snapshot.prompt_contract_ref = job.prompt_contract_ref or snapshot.prompt_contract_ref
        snapshot.acceptance_contract = dict(job.acceptance_contract or snapshot.acceptance_contract)
        snapshot.browser_flow_proof = dict(job.browser_flow_proof or snapshot.browser_flow_proof)
        snapshot.mobile_layout_report = dict(job.mobile_layout_report or snapshot.mobile_layout_report)
        snapshot.flow_coverage = dict(job.flow_coverage or snapshot.flow_coverage)
        if job.status == "completed":
            applied_by_job = str(job.outcome_kind or "") == "applied" or bool((job.apply_result or {}).get("revision_id"))
            applied_by_run = run.apply_status == "applied"
            if applied_by_job or applied_by_run:
                snapshot.status = "completed"
                snapshot.apply_status = "applied"
                snapshot.current_stage = "completed"
                snapshot.progress_percent = 100
            else:
                return None
        elif job.status == "blocked":
            snapshot.status = "blocked"
            snapshot.apply_status = "blocked"
            snapshot.current_stage = "blocked"
            snapshot.progress_percent = self._terminal_failure_progress(snapshot.progress_percent)
        else:
            snapshot.status = "failed"
            snapshot.apply_status = "failed"
            snapshot.current_stage = "failed"
            snapshot.progress_percent = self._terminal_failure_progress(snapshot.progress_percent)
        return snapshot

    def _merge_terminal_run_snapshot(self, run: RunRecord, snapshot: RunRecord) -> bool:
        changed = False
        protected = {"run_id", "workspace_id", "prompt", "created_at"}
        always = {
            "status",
            "apply_status",
            "draft_status",
            "draft_ready",
            "approval_required",
            "current_stage",
            "progress_percent",
            "summary",
            "failure_reason",
            "failure_class",
            "failure_signature",
            "root_cause_summary",
            "outcome_kind",
            "linked_job_id",
            "result_revision_id",
            "candidate_revision_id",
            "checks_summary",
            "latency_breakdown",
            "token_usage",
            "budget_status",
            "browser_flow_proof",
            "mobile_layout_report",
            "preview_refresh_status",
        }
        for field in RunRecord.model_fields:
            if field in protected:
                continue
            value = copy.deepcopy(getattr(snapshot, field))
            current = getattr(run, field)
            if field == "token_usage":
                value = self._richer_token_usage(value, current)
            should_copy = field in always or not self._empty_observability_value(value) or self._empty_observability_value(current)
            if should_copy and current != value:
                setattr(run, field, value)
                changed = True
        return changed

    @staticmethod
    def _empty_observability_value(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    @staticmethod
    def _failed_run_has_retained_draft(run: RunRecord) -> bool:
        if run.status != "failed":
            return False
        if str(run.outcome_kind or "") not in {"", "blocked_generation", "blocked_preview_infra"}:
            return False
        return bool(run.draft_ready or str(run.draft_status or "") == "ready")

    @staticmethod
    def _token_usage_from_job_events(job: JobRecord) -> dict[str, Any]:
        usage: dict[str, Any] = {}
        for event in job.events:
            if event.event_type != "iteration_ready":
                continue
            details = event.details if isinstance(event.details, dict) else {}
            usage = WorkspaceCodeAgentRuntime._merge_run_token_usage(usage, details)
        return usage

    @staticmethod
    def _is_nonspecific_failure_reason(reason: str | None) -> bool:
        text = str(reason or "").strip().lower()
        return not text or text.startswith("missing create coverage:") or text in {
            "workspace code agent exhausted its completion budget without reaching a usable state.",
            "workspace code agent failed.",
            "agent loop did not produce a complete patch.",
        }

    @staticmethod
    def _specific_failure_reason_from_job(job: JobRecord) -> str | None:
        def important_line(logs: list[Any]) -> str:
            specific_markers = ("operationalerror", "assertionerror", "syntaxerror", "no such table")
            broad_error_markers = ("failed", "error:", "traceback")
            clean = [" ".join(str(line or "").split()).strip() for line in logs if str(line or "").strip()]
            for line in reversed(clean):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict) and (payload.get("blocking") is True or str(payload.get("severity") or "").lower() == "high"):
                    return line[:320]
            for line in reversed(clean):
                lowered = line.lower()
                if any(marker in lowered for marker in specific_markers):
                    return line[:320]
            for line in reversed(clean):
                lowered = line.lower()
                if any(marker in lowered for marker in broad_error_markers):
                    return line[:320]
            return clean[-1][:320] if clean else ""

        for issue in job.remaining_issues:
            check = str(issue.get("check") or issue.get("code") or "validation").strip()
            logs = issue.get("logs")
            if isinstance(logs, list):
                line = important_line(logs)
                if line:
                    return f"{check}: {line}"
            message = str(issue.get("message") or "").strip()
            if message:
                return f"{check}: {message[:320]}"
        for check in job.executed_checks:
            diagnostics = check.get("diagnostics") if isinstance(check.get("diagnostics"), dict) else {}
            if check.get("name") == "browser_flow_smoke" and check.get("status") == "failed" and diagnostics.get("infra_unavailable"):
                return (
                    "browser_flow_smoke: Playwright browser proof could not run because browser "
                    "verification infrastructure is unavailable."
                )
        for check in job.executed_checks:
            if check.get("status") != "failed":
                continue
            line = important_line(list(check.get("logs") or []))
            name = str(check.get("name") or "validation")
            if line:
                return f"{name}: {line}"
            details = str(check.get("details") or "").strip()
            if details:
                return f"{name}: {details[:320]}"
        return None

    def _execute_run(self, run_id: str, request_payload: dict[str, Any]) -> None:
        request = CreateRunRequest.model_validate(request_payload)
        try:
            run = self.get_run(run_id)
            workspace = self.workspace_service.get_workspace(run.workspace_id)
            self.workspace_log_service.ensure_log_files(run.workspace_id)
            effective_generation_mode = self._resolve_generation_mode(workspace, request, run.intent)
            effective_model_profile = self._resolve_model_profile(request.model_profile, effective_generation_mode)
            run.status = "running"
            run.generation_mode = effective_generation_mode
            run.model_profile = effective_model_profile
            run.current_stage = "starting"
            run.progress_percent = max(run.progress_percent, 5)
            run.updated_at = datetime.now(timezone.utc)
            self._save_run(run)
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                message="Run started.",
                payload={"run_id": run.run_id, "mode": run.mode, "intent": run.intent},
            )
            self.workspace_log_service.append_api(
                run.workspace_id,
                source="run",
                message="API log initialized for run.",
                payload={"run_id": run.run_id, "mode": run.mode},
            )
            logger.info("run_started run_id=%s workspace_id=%s intent=%s", run.run_id, run.workspace_id, run.intent)
            generate_request = GenerateRequest(
                prompt=request.prompt,
                mode=request.mode,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                generation_mode=effective_generation_mode,
                intent=run.intent,
                target_role_scope=run.target_role_scope,
                model_profile=effective_model_profile,
                linked_run_id=run.run_id,
                resume_from_run_id=request.resume_from_run_id,
                session_id=run.session_id,
                resume_bookmark_id=request.resume_bookmark_id,
                forked_from_run_id=request.forked_from_run_id,
                error_context=request.error_context,
            )
            with self.openai_client.workspace_logging(run.workspace_id):
                job = self.code_agent_runtime.generate(
                    run.workspace_id,
                    generate_request,
                    should_stop=lambda: self._is_stop_requested(run.run_id),
                )
            preview = self.preview_service.get(run.workspace_id)
            change_plan = self._build_change_plan(
                workspace_id=run.workspace_id,
                run=run,
                diff_text=self.workspace_service.diff(run.workspace_id, run_id=run.run_id),
                prompt=request.prompt,
            )

            run.linked_job_id = job.job_id
            run.llm_provider = job.llm_provider
            run.llm_model = job.llm_model
            run.summary = job.summary
            run.failure_reason = job.failure_reason
            run.failure_class = job.failure_class
            run.failure_signature = job.failure_signature
            run.root_cause_summary = job.root_cause_summary
            if not run.failure_signature and run.failure_class:
                summary = str(run.root_cause_summary or run.failure_reason or run.failure_class).strip().lower()
                summary = re.sub(r"[^a-z0-9]+", "_", summary).strip("_")[:80]
                run.failure_signature = f"{run.failure_class}:{summary or 'failed'}"
            run.current_fix_phase = job.current_fix_phase
            run.current_failing_command = job.current_failing_command
            run.current_exit_code = job.current_exit_code
            run.fix_targets = list(job.fix_targets)
            run.remaining_issues = list(getattr(job, "remaining_issues", []) or [])
            run.handoff_from_failed_generate = dict(job.handoff_from_failed_generate or {}) or None
            run.checks_summary = self._build_checks_summary(job.validation_snapshot, preview.status)
            agent_quality = self._agent_quality_report(run.workspace_id)
            run.role_coverage = dict(agent_quality.get("role_coverage") or {})
            run.generated_tests = dict(agent_quality.get("generated_tests") or {})
            if agent_quality.get("flow_coverage"):
                run.flow_coverage = dict(agent_quality.get("flow_coverage") or {})
            run.neutral_template_findings = list(agent_quality.get("neutral_template_findings") or [])
            run.touched_files = self._resolve_touched_files(
                workspace_id=run.workspace_id,
                run=run,
                change_plan=change_plan,
                request=request,
            )
            run.candidate_revision_id = f"draft:{run.run_id}"
            run.iteration_count = len((self.code_agent_runtime.current_report(run.workspace_id, "iterations") or {}).get("items", []))
            run.latency_breakdown = dict(job.latency_breakdown)
            run.token_usage = self._richer_token_usage(
                self._richer_token_usage(run.token_usage, job.token_usage),
                self._token_usage_from_job_events(job),
            )
            run.orchestration_phases = list(getattr(job, "orchestration_phases", []) or run.orchestration_phases)
            run.implementation_plan = dict(getattr(job, "implementation_plan", {}) or run.implementation_plan)
            run.agent_activity_events = list(getattr(job, "agent_activity_events", []) or run.agent_activity_events)
            run.agent_memory = dict(getattr(job, "agent_memory", {}) or run.agent_memory)
            run.agent_transcript_ref = getattr(job, "agent_transcript_ref", None) or run.agent_transcript_ref
            run.tool_trace_ref = getattr(job, "tool_trace_ref", None) or run.tool_trace_ref
            run.file_change_history_ref = getattr(job, "file_change_history_ref", None) or run.file_change_history_ref
            run.browser_proof_ref = getattr(job, "browser_proof_ref", None) or run.browser_proof_ref
            run.browser_replay_proof_ref = getattr(job, "browser_replay_proof_ref", None) or run.browser_replay_proof_ref
            run.large_tool_outputs_ref = getattr(job, "large_tool_outputs_ref", None) or run.large_tool_outputs_ref
            run.file_state_cache_ref = getattr(job, "file_state_cache_ref", None) or run.file_state_cache_ref
            run.turn_diff_ref = getattr(job, "turn_diff_ref", None) or run.turn_diff_ref
            run.environment_snapshot_ref = getattr(job, "environment_snapshot_ref", None) or run.environment_snapshot_ref
            run.tool_batch_summaries_ref = getattr(job, "tool_batch_summaries_ref", None) or run.tool_batch_summaries_ref
            run.worker_mailbox_ref = getattr(job, "worker_mailbox_ref", None) or run.worker_mailbox_ref
            run.worker_sessions_ref = getattr(job, "worker_sessions_ref", None) or run.worker_sessions_ref
            run.worker_ownership_ref = getattr(job, "worker_ownership_ref", None) or run.worker_ownership_ref
            run.draft_isolation_ref = getattr(job, "draft_isolation_ref", None) or run.draft_isolation_ref
            run.draft_gate_ref = getattr(job, "draft_gate_ref", None) or run.draft_gate_ref
            run.draft_apply_decision_ref = getattr(job, "draft_apply_decision_ref", None) or run.draft_apply_decision_ref
            run.guardian_gate_ref = getattr(job, "guardian_gate_ref", None) or run.guardian_gate_ref
            run.scratchpad_ref = getattr(job, "scratchpad_ref", None) or run.scratchpad_ref
            run.memory_ref = getattr(job, "memory_ref", None) or run.memory_ref
            run.worker_drafts_ref = getattr(job, "worker_drafts_ref", None) or run.worker_drafts_ref
            run.worker_merge_ref = getattr(job, "worker_merge_ref", None) or run.worker_merge_ref
            run.trace_bundle_ref = getattr(job, "trace_bundle_ref", None) or run.trace_bundle_ref
            run.trace_reducer_ref = getattr(job, "trace_reducer_ref", None) or run.trace_reducer_ref
            run.command_policy_ref = getattr(job, "command_policy_ref", None) or run.command_policy_ref
            run.verification_report_ref = getattr(job, "verification_report_ref", None) or run.verification_report_ref
            run.rollout_trace_ref = getattr(job, "rollout_trace_ref", None) or run.rollout_trace_ref
            run.exec_trace_ref = getattr(job, "exec_trace_ref", None) or run.exec_trace_ref
            run.process_outputs_ref = getattr(job, "process_outputs_ref", None) or run.process_outputs_ref
            run.tool_result_messages_ref = getattr(job, "tool_result_messages_ref", None) or run.tool_result_messages_ref
            run.active_processes = list(getattr(job, "active_processes", []) or run.active_processes)
            run.artifact_read_trace_ref = getattr(job, "artifact_read_trace_ref", None) or run.artifact_read_trace_ref
            run.active_tool_uses = list(getattr(job, "active_tool_uses", []) or run.active_tool_uses)
            run.resume_checkpoint_ref = getattr(job, "resume_checkpoint_ref", None) or run.resume_checkpoint_ref
            run.worker_branch_refs = list(getattr(job, "worker_branch_refs", []) or run.worker_branch_refs)
            run.verifier_review_ref = getattr(job, "verifier_review_ref", None) or run.verifier_review_ref
            run.browser_step_refs = list(getattr(job, "browser_step_refs", []) or run.browser_step_refs)
            run.lsp_context_ref = getattr(job, "lsp_context_ref", None) or run.lsp_context_ref
            run.context_manager_ref = getattr(job, "context_manager_ref", None) or run.context_manager_ref
            run.context_pressure_ref = getattr(job, "context_pressure_ref", None) or run.context_pressure_ref
            run.hook_trace_ref = getattr(job, "hook_trace_ref", None) or run.hook_trace_ref
            run.semantic_graph_ref = getattr(job, "semantic_graph_ref", None) or run.semantic_graph_ref
            run.worker_prefix_ref = getattr(job, "worker_prefix_ref", None) or run.worker_prefix_ref
            run.replay_trace_ref = getattr(job, "replay_trace_ref", None) or run.replay_trace_ref
            run.prompt_contract_ref = getattr(job, "prompt_contract_ref", None) or run.prompt_contract_ref
            run.miniapp_contract_ref = getattr(job, "miniapp_contract_ref", None) or run.miniapp_contract_ref
            run.route_registry_ref = getattr(job, "route_registry_ref", None) or run.route_registry_ref
            run.contract_compile_ref = getattr(job, "contract_compile_ref", None) or run.contract_compile_ref
            run.repair_recipes_ref = getattr(job, "repair_recipes_ref", None) or run.repair_recipes_ref
            run.acceptance_contract = dict(getattr(job, "acceptance_contract", {}) or run.acceptance_contract)
            run.worker_summaries = list(getattr(job, "worker_summaries", []) or run.worker_summaries)
            run.flow_coverage = dict(getattr(job, "flow_coverage", {}) or run.flow_coverage)
            run.browser_flow_proof = dict(getattr(job, "browser_flow_proof", {}) or run.browser_flow_proof)
            run.repair_issue_signatures = list(getattr(job, "repair_issue_signatures", []) or run.repair_issue_signatures)
            run.mobile_layout_report = dict(getattr(job, "mobile_layout_report", {}) or run.mobile_layout_report)
            run.completion_budget = dict(getattr(job, "completion_budget", {}) or run.completion_budget)
            run.budget_status = dict(getattr(job, "budget_status", {}) or run.budget_status)
            run.repair_iterations = list(job.repair_iterations)
            run.apply_result = dict(job.apply_result or {})
            run.retrieval_stats = dict(job.retrieval_stats)
            run.cache_stats = dict(job.cache_stats)
            run.artifacts = {
                "run_artifacts": f"/runs/{run.run_id}/artifacts",
                "preview_url": preview.url or "",
                "trace": f"/workspaces/{run.workspace_id}/logs",
                "iterations": f"/runs/{run.run_id}/iterations",
                "checks": f"/runs/{run.run_id}/checks",
                "patch": f"/runs/{run.run_id}/patch",
            }
            run.updated_at = datetime.now(timezone.utc)

            if job.status == "completed":
                should_apply_fix_draft = request.mode == "fix" and self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                if should_apply_fix_draft:
                    if self._apply_completed_draft(run, message="Applying verified fix draft to the source workspace."):
                        self._clear_successful_completion_metadata(run=run, job=job)
                else:
                    meaningful_paths = self._meaningful_paths_for_run(
                        workspace_id=run.workspace_id,
                        run=run,
                        change_plan=change_plan,
                        job=job,
                    )
                    if not meaningful_paths:
                        self._mark_run_without_meaningful_diff(run, job)
                    else:
                        if self._apply_completed_draft(run, message="Applying generated draft to the source workspace."):
                            self._clear_successful_completion_metadata(run=run, job=job)
            else:
                meaningful_paths = self._meaningful_paths_for_run(
                    workspace_id=run.workspace_id,
                    run=run,
                    change_plan=change_plan,
                    job=job,
                )
                if job.status == "blocked":
                    run.status = "blocked"
                    run.apply_status = "blocked"
                    run.outcome_kind = "blocked_preview_infra" if str(job.outcome_kind or "") == "blocked_preview_infra" else "blocked_generation"
                    has_draft = self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                    draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id) if has_draft else ""
                    run.draft_status = "ready" if has_draft and (meaningful_paths or draft_diff.strip()) else "failed"
                    run.draft_ready = run.draft_status == "ready"
                    if run.current_fix_phase == "completed":
                        run.current_fix_phase = "failed"
                    if str(run.current_fix_phase or "") == "blocked_provider_quota":
                        run.current_stage = "blocked_provider_quota"
                    else:
                        run.current_stage = "stopped" if self._is_stop_requested(run.run_id) else "blocked"
                    run.progress_percent = self._terminal_failure_progress(run.progress_percent)
                    if run.draft_ready:
                        run.summary = "Strict-green validation did not pass. Draft was retained for inspection."
                else:
                    run.outcome_kind = "blocked_preview_infra" if str(job.outcome_kind or "") == "blocked_preview_infra" else "blocked_generation"
                    has_draft = self.workspace_service.draft_exists(run.workspace_id, run.run_id)
                    draft_diff = self.workspace_service.diff(run.workspace_id, run_id=run.run_id) if has_draft else ""
                    run.draft_status = "ready" if has_draft and (meaningful_paths or draft_diff.strip()) else "failed"
                    run.draft_ready = run.draft_status == "ready"
                    run.status = "blocked" if run.draft_ready else "failed"
                    run.apply_status = "blocked" if run.draft_ready else "failed"
                    if run.current_fix_phase == "completed":
                        run.current_fix_phase = "failed"
                    if str(run.current_fix_phase or "") == "blocked_provider_quota":
                        run.current_stage = "blocked_provider_quota"
                    elif str(run.current_fix_phase or "") == "blocked_budget_exhausted":
                        run.current_stage = "blocked_budget_exhausted"
                    else:
                        run.current_stage = "blocked" if run.draft_ready else "failed"
                    run.progress_percent = self._terminal_failure_progress(run.progress_percent)
                    if run.draft_ready:
                        run.summary = "Strict-green validation did not pass. Draft was retained for inspection."

            if run.status == "awaiting_approval" and getattr(job, "status", None) != "completed":
                self._append_job_event(
                    run.linked_job_id,
                    "job_completed",
                    "Draft is ready for review after strict-green validation failed.",
                    {
                        "run_id": run.run_id,
                        "status": run.status,
                        "apply_status": run.apply_status,
                    },
                )
            queue_preview_reason: str | None = None
            if run.status == "completed" and run.apply_status == "applied":
                queue_preview_reason = "run completion"
            if queue_preview_reason is not None:
                wait_for_preview = self._should_wait_for_preview_refresh(request, run)
                if wait_for_preview:
                    run.status = "running"
                    run.current_stage = "refreshing preview"
                    run.progress_percent = self._preview_refresh_progress(run.progress_percent)
                else:
                    run.preview_refresh_status = "running"
                self._save_run(run)
                preview = self._queue_preview_refresh(
                    run,
                    reason=queue_preview_reason,
                    draft_run_id=None,
                    wait=wait_for_preview,
                )
            self._save_run(run)
            self._store_run_artifacts(run, change_plan, job, preview)
            if run.status in TERMINAL_RUN_STATUSES or run.apply_status in {"applied", "blocked", "failed"}:
                self._extract_run_memory_stage1(run)
            self._schedule_auto_repair_continuation_if_needed(run)
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            logger.info(
                "run_finished run_id=%s workspace_id=%s status=%s progress=%s",
                run.run_id,
                run.workspace_id,
                run.status,
                run.progress_percent,
            )
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                message="Run finished.",
                payload={"run_id": run.run_id, "status": run.status, "apply_status": run.apply_status},
            )
            if self.run_protocol_service is not None:
                self.run_protocol_service.append_once_terminal(run, source_event_type="run_service_finished")
            if run.status == "completed" and run.apply_status == "applied":
                self._queue_resume_generation_from_checkpoint_if_needed(run, request)
        except Exception as exc:
            run.status = "failed"
            run.apply_status = "failed"
            run.failure_reason = str(exc)
            if run.current_fix_phase == "completed":
                run.current_fix_phase = "failed"
            run.current_stage = "failed"
            run.progress_percent = self._terminal_failure_progress(run.progress_percent)
            run.updated_at = datetime.now(timezone.utc)
            linked_job_id = self._resolve_linked_job_id(run)
            self._save_run(run)
            try:
                self._extract_run_memory_stage1(run)
            except Exception:
                logger.exception("memory_stage1_extract_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)
            if linked_job_id:
                try:
                    job = self.code_agent_runtime.get_job(linked_job_id)
                except KeyError:
                    job = None
                if job is not None:
                    job.status = "failed"
                    job.summary = job.summary or "Run failed with an unexpected exception."
                    job.failure_reason = str(exc)
                    job.current_fix_phase = "failed" if job.current_fix_phase == "completed" else job.current_fix_phase
                    self.code_agent_runtime.append_event(
                        job,
                        "job_failed",
                        "Run failed with an unexpected exception.",
                        {
                            "run_id": run.run_id,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
            self.store.delete("reports", f"run_stop_request:{run.run_id}")
            self.workspace_log_service.append(
                run.workspace_id,
                source="run",
                level="ERROR",
                message="Run failed with an unexpected exception.",
                payload={
                    "run_id": run.run_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )
            if self.run_protocol_service is not None:
                self.run_protocol_service.append_once_terminal(run, source_event_type="run_service_exception")
            logger.exception("run_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)
        finally:
            self._active_workers.pop(run_id, None)

    def _is_stop_requested(self, run_id: str) -> bool:
        payload = self.store.get("reports", f"run_stop_request:{run_id}")
        return bool(payload and payload.get("requested"))

    def _append_job_event(
        self,
        job_id: str | None,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not job_id:
            return
        payload = self.store.get("jobs", job_id)
        if not payload:
            return
        events = list(payload.get("events", []))
        events.append(JobEvent(event_type=event_type, message=message, details=details or {}).model_dump(mode="json"))
        payload["events"] = events
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("jobs", job_id, payload)

    def _queue_preview_refresh(
        self,
        run: RunRecord,
        *,
        reason: str,
        draft_run_id: str | None = None,
        wait: bool = False,
    ) -> Any:
        queue_started_at = time.perf_counter()
        self._append_job_event(
            run.linked_job_id,
            "preview_rebuild_started",
            f"{'Running' if wait else 'Queued'} preview rebuild after {reason}.",
            {"reason": reason, "run_id": run.run_id, "draft_run_id": None},
        )

        source_dir = self.workspace_service.source_dir(run.workspace_id)

        def on_complete(preview: Any) -> None:
            if preview is None:
                preview = self.preview_service.get(run.workspace_id)
            actual_preview_ms = int(
                getattr(preview, "latency_breakdown", {}).get("last_rebuild_ms")
                or getattr(preview, "latency_breakdown", {}).get("last_start_ms")
                or 0
            )
            if preview.status == "running":
                self._append_job_event(
                    run.linked_job_id,
                    "preview_rebuild_completed",
                    "Preview rebuild finished successfully.",
                    {
                        "url": preview.url,
                        "stage": getattr(preview, "stage", "running"),
                        "progress_percent": getattr(preview, "progress_percent", 100),
                        "draft_run_id": getattr(preview, "draft_run_id", None),
                    },
                )
            else:
                self._append_job_event(
                    run.linked_job_id,
                    "preview_rebuild_failed",
                    getattr(preview, "last_error", None) or "Preview rebuild failed.",
                    {
                        "stage": getattr(preview, "stage", "error"),
                        "progress_percent": getattr(preview, "progress_percent", 100),
                        "draft_run_id": getattr(preview, "draft_run_id", None),
                    },
                )
            artifacts_payload = self.store.get("reports", f"run_artifacts:{run.run_id}")
            if artifacts_payload:
                artifacts_payload["preview"] = self._preview_snapshot(run.workspace_id, preview)
                artifacts_payload["draft_preview"] = {
                    key: value
                    for key, value in artifacts_payload["preview"].items()
                    if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
                }
                artifacts_payload["preview_infra_diagnostics"] = {
                    "failure_kind": getattr(preview, "preview_failure_kind", None),
                    "retry_count": getattr(preview, "preview_retry_count", 0),
                    "cleanup_attempted": getattr(preview, "preview_cleanup_attempted", False),
                    "reused_existing_runtime": getattr(preview, "preview_reused_existing_runtime", False),
                    "cooldown_until": getattr(preview, "preview_cooldown_until", None).isoformat()
                    if getattr(preview, "preview_cooldown_until", None)
                    else None,
                    "last_error": getattr(preview, "last_error", None),
                }
                self.store.upsert("reports", f"run_artifacts:{run.run_id}", artifacts_payload)
            run_payload = self.store.get("runs", run.run_id)
            if run_payload is not None:
                latency_breakdown = dict(run_payload.get("latency_breakdown") or {})
                if actual_preview_ms > 0:
                    latency_breakdown["preview_ms"] = actual_preview_ms
                run_payload["latency_breakdown"] = latency_breakdown
                checks_summary = dict(run_payload.get("checks_summary") or {})
                if preview.status == "running":
                    checks_summary["preview"] = "passed"
                    run_payload["preview_refresh_status"] = "passed"
                elif preview.status == "error":
                    checks_summary["preview"] = "failed"
                    run_payload["preview_refresh_status"] = "failed"
                if checks_summary:
                    run_payload["checks_summary"] = checks_summary
                run_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.store.upsert("runs", run.run_id, run_payload)
        if wait:
            preview = self.preview_service.rebuild(
                run.workspace_id,
                source_dir=source_dir,
                draft_run_id=None,
            )
            on_complete(preview)
        else:
            preview = self.preview_service.rebuild_async(
                run.workspace_id,
                source_dir=source_dir,
                draft_run_id=None,
                on_complete=on_complete,
                force=True,
            )
        latency_breakdown = dict((self.store.get("runs", run.run_id) or {}).get("latency_breakdown") or run.latency_breakdown)
        latency_breakdown["preview_enqueue_ms"] = int((time.perf_counter() - queue_started_at) * 1000)
        latency_breakdown.setdefault("preview_ms", latency_breakdown["preview_enqueue_ms"])
        run.latency_breakdown = latency_breakdown
        if preview.status == "running":
            run.status = "completed"
            run.apply_status = "applied"
            run.checks_summary = self._copy_checks_summary(run.checks_summary, preview="passed")
            run.current_stage = "completed"
            run.progress_percent = 100
            run.preview_refresh_status = "passed"
            if wait and reason == "run completion":
                self._run_post_apply_source_proof(run=run, source_dir=source_dir, preview=preview)
        elif wait:
            run.status = "blocked"
            run.outcome_kind = "blocked_preview_infra"
            run.failure_reason = getattr(preview, "last_error", None) or "Preview rebuild failed after applying the draft."
            run.current_stage = "preview failed"
            run.progress_percent = self._terminal_failure_progress(run.progress_percent)
            run.checks_summary = self._copy_checks_summary(run.checks_summary, preview="failed")
            run.preview_refresh_status = "failed"
        elif run.preview_refresh_status == "pending":
            run.preview_refresh_status = "running"
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        artifacts_payload = self.store.get("reports", f"run_artifacts:{run.run_id}")
        if artifacts_payload:
            artifacts_payload["preview"] = self._preview_snapshot(run.workspace_id, preview)
            artifacts_payload["draft_preview"] = {
                key: value
                for key, value in artifacts_payload["preview"].items()
                if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
            }
            self.store.upsert("reports", f"run_artifacts:{run.run_id}", artifacts_payload)
        return preview

    def _run_post_apply_source_proof(self, *, run: RunRecord, source_dir: Any, preview: Any) -> None:
        """Re-run product proof against the applied source workspace.

        Draft checks prove the candidate can work before apply. This second pass
        proves that the final served app still matches the applied source after
        draft cleanup and preview rebuild.
        """
        execution = self.check_runner.run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            source_dir=source_dir,
            changed_files=list(run.touched_files or []),
            preview_run_id=None,
            scope_mode="full_build",
            check_profile="full",
            intent=run.intent,
            generation_mode=run.generation_mode,
            acceptance_contract=run.acceptance_contract,
        )
        report_ref = f"post_apply_checks:{run.workspace_id}:{run.run_id}"
        self.store.upsert(
            "reports",
            report_ref,
            {
                "workspace_id": run.workspace_id,
                "run_id": run.run_id,
                "phase": "post_apply_source_proof",
                "results": [item.model_dump(mode="json") for item in execution.results],
            },
        )
        browser_result = next((item for item in execution.results if item.name == "browser_flow_smoke"), None)
        if browser_result is not None:
            run.browser_flow_proof = dict(browser_result.diagnostics or {})
            if isinstance(run.browser_flow_proof.get("mobile_layout"), dict):
                run.mobile_layout_report = dict(run.browser_flow_proof.get("mobile_layout") or {})
            if run.browser_flow_proof:
                run.browser_proof_ref = f"browser_proof:{run.workspace_id}:{run.run_id}"
                self.store.upsert(
                    "reports",
                    run.browser_proof_ref,
                    {
                        "workspace_id": run.workspace_id,
                        "run_id": run.run_id,
                        "phase": "post_apply_source_proof",
                        "proof": run.browser_flow_proof,
                        "mobile_layout_report": run.mobile_layout_report,
                    },
                )
        failures = [item for item in execution.results if item.status in {"failed", "blocked"}]
        if failures:
            issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
            validation_snapshot = ValidationSnapshot(
                platform_valid=False,
                checks_valid=False,
                build_valid=not any(item.name in {"schema_validators", "changed_files_static"} for item in failures),
                blocking=True,
                issues=issues
                or [
                    {
                        "kind": "post_apply_check_failure",
                        "check": item.name,
                        "details": item.details,
                        "logs": list(item.logs or [])[-8:],
                        "blocking": True,
                    }
                    for item in failures
                ],
            )
            run.status = "blocked"
            run.apply_status = "applied"
            run.outcome_kind = "blocked_preview_infra" if self.check_runner.classify_failure(execution.results) == "blocked_preview_infra" else "blocked_generation"
            run.current_stage = "post-apply proof failed"
            run.failure_reason = self._specific_failure_reason_from_results(execution.results) or "Post-apply source proof failed."
            run.failure_class = "post_apply.source_proof_failed"
            run.failure_signature = f"{run.failure_class}:{failures[0].name}"
            run.remaining_issues = [
                {
                    "kind": "post_apply_check_failure",
                    "check": item.name,
                    "details": item.details,
                    "logs": list(item.logs or [])[-8:],
                    "blocking": True,
                }
                for item in failures
            ]
            run.checks_summary = self._build_checks_summary(validation_snapshot, getattr(preview, "status", "running"), gate_status="blocked")
            self.store.upsert("reports", f"validation:{run.workspace_id}", validation_snapshot.model_dump(mode="json"))
            self._append_job_event(
                run.linked_job_id,
                "job_failed",
                run.failure_reason,
                {"reason": "post_apply_source_proof_failed", "run_id": run.run_id, "report_ref": report_ref},
            )
        else:
            validation_snapshot = ValidationSnapshot(platform_valid=True, checks_valid=True, build_valid=True, blocking=False, issues=[])
            run.checks_summary = self._build_checks_summary(validation_snapshot, getattr(preview, "status", "running"), gate_status="passed")
            self.store.upsert("reports", f"validation:{run.workspace_id}", validation_snapshot.model_dump(mode="json"))
            self._append_job_event(
                run.linked_job_id,
                "preview_ready",
                "Applied source preview passed post-apply product proof.",
                {"reason": "post_apply_source_proof", "run_id": run.run_id, "report_ref": report_ref},
            )
            self._record_session_checkpoint(
                run,
                kind="after_successful_tests",
                status="ready",
                source="post_apply_source_proof",
                summary="Checkpoint captured after applied source passed post-apply product proof.",
                refs={
                    "post_apply_checks": report_ref,
                    "browser_proof": run.browser_proof_ref,
                    "result_revision_id": run.result_revision_id,
                },
                metadata={"check_count": len(execution.results)},
            )

    @staticmethod
    def _specific_failure_reason_from_results(results: list[RunCheckResult]) -> str | None:
        for result in results:
            if result.status not in {"failed", "blocked"}:
                continue
            logs = [" ".join(str(line or "").split()).strip() for line in list(result.logs or []) if str(line or "").strip()]
            if logs:
                return f"{result.name}: {logs[-1][:320]}"
            if result.details:
                return f"{result.name}: {result.details[:320]}"
        return None

    def _queue_resume_generation_from_checkpoint_if_needed(self, run: RunRecord, request: CreateRunRequest) -> None:
        if request.mode != "fix":
            return
        source_run_id = str(request.resume_from_run_id or "").strip()
        if not source_run_id:
            return
        try:
            source_run = self.get_run(source_run_id)
        except KeyError:
            return
        if source_run.workspace_id != run.workspace_id:
            return
        checkpoint = self._load_run_resume_checkpoint(source_run)
        if not checkpoint or checkpoint.get("status") != "pending":
            return
        if request.apply_strategy != "staged_auto_apply" or run.apply_status != "applied":
            return

        resume_request = CreateRunRequest(
            prompt=str(checkpoint.get("prompt") or run.prompt),
            mode="generate",
            intent=str(checkpoint.get("intent") or "auto"),
            apply_strategy="staged_auto_apply",
            target_role_scope=list(checkpoint.get("target_role_scope") or run.target_role_scope),
            model_profile=str(checkpoint.get("model_profile") or run.model_profile),
            target_platform=str(checkpoint.get("target_platform") or "telegram_mini_app"),
            preview_profile=str(checkpoint.get("preview_profile") or "telegram_mock"),
            generation_mode=str(checkpoint.get("generation_mode") or getattr(run.generation_mode, "value", run.generation_mode)),
            resume_from_run_id=source_run_id or None,
            session_id=run.session_id,
            resume_bookmark_id=run.resume_bookmark_id,
            forked_from_run_id=run.forked_from_run_id,
        )
        resumed_run = self.create_run(run.workspace_id, resume_request)
        checkpoint["status"] = "resumed"
        checkpoint["resumed_run_id"] = resumed_run.run_id
        checkpoint["resumed_from_fix_run_id"] = run.run_id
        checkpoint["resumed_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("reports", self._resume_checkpoint_ref_for_run(source_run), checkpoint)
        self.workspace_log_service.append(
            run.workspace_id,
            source="run.resume",
            message="Fix applied successfully. Queued generation resume from checkpoint.",
            payload={
                "fix_run_id": run.run_id,
                "resumed_run_id": resumed_run.run_id,
                "source_run_id": checkpoint.get("source_run_id"),
            },
        )
        self._append_job_event(
            run.linked_job_id,
            "job_completed",
            f"Fix applied. Continuing generation in run {resumed_run.run_id}.",
            {"resumed_run_id": resumed_run.run_id, "source_run_id": checkpoint.get("source_run_id")},
        )

    def _preview_snapshot(self, workspace_id: str, preview: Any | None = None) -> dict[str, Any]:
        current = preview or self.preview_service.get(workspace_id)
        role_urls = {role: f"{current.url}/{role}" for role in ("client", "specialist", "manager")} if current.url else {}
        return {
            "status": current.status,
            "stage": getattr(current, "stage", "idle"),
            "progress_percent": getattr(current, "progress_percent", 0),
            "runtime_mode": current.runtime_mode,
            "url": current.url,
            "role_urls": role_urls,
            "logs": list(getattr(current, "logs", [])),
            "draft_run_id": current.draft_run_id,
            "latency_breakdown": dict(getattr(current, "latency_breakdown", {})),
            "last_error": getattr(current, "last_error", None),
        }

    def _store_run_artifacts(self, run: RunRecord, change_plan: CodeChangePlan, job: Any, preview: Any) -> None:
        workspace_id = run.workspace_id
        iterations = (self.code_agent_runtime.current_report(workspace_id, "iterations") or {}).get("items", [])
        candidate_diff = (self.code_agent_runtime.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        if candidate_diff:
            effective_diff = candidate_diff
        elif run.draft_ready and self.workspace_service.draft_exists(workspace_id, run.run_id):
            effective_diff = self.workspace_service.diff(workspace_id, run_id=run.run_id)
        else:
            effective_diff = self.workspace_service.diff(workspace_id)
        preview_payload = self._preview_snapshot(workspace_id, preview)
        patch_payload = self.code_agent_runtime.current_report(workspace_id, "patch")
        post_apply_checks = self.store.get("reports", f"post_apply_checks:{workspace_id}:{run.run_id}")
        check_results_payload = (
            {"items": list(post_apply_checks.get("results") or [])}
            if isinstance(post_apply_checks, dict) and post_apply_checks.get("results")
            else self.code_agent_runtime.current_report(workspace_id, "check_results")
        )
        if not patch_payload and effective_diff.strip():
            patch_paths = self._paths_from_diff(effective_diff)
            if not patch_paths:
                patch_paths = [target.file_path for target in change_plan.targets if target.file_path]
            patch_payload = {
                "file_changes": [{"file_path": path, "change_type": "modified"} for path in patch_paths],
                "apply_result": job.apply_result,
            }
        agent_transcript = self.store.get("reports", run.agent_transcript_ref) if run.agent_transcript_ref else None
        scratchpad = self.store.get("reports", run.scratchpad_ref) if run.scratchpad_ref else None
        worker_merge = self.store.get("reports", run.worker_merge_ref) if run.worker_merge_ref else None
        process_outputs = self.store.get("reports", run.process_outputs_ref) if run.process_outputs_ref else None
        payload = {
            "run": run.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "trace": self.code_agent_runtime.current_report(workspace_id, "trace"),
            "agent_diagnostics": self.code_agent_runtime.current_report(workspace_id, "agent_diagnostics"),
            "iterations": iterations,
            "check_results": (check_results_payload or {}).get("items", []),
            "checks": check_results_payload,
            "patch": patch_payload,
            "diff": effective_diff,
            "role_coverage": run.role_coverage,
            "generated_tests": run.generated_tests,
            "neutral_template_findings": run.neutral_template_findings,
            "orchestration_phases": run.orchestration_phases,
            "implementation_plan": run.implementation_plan,
            "agent_activity_events": run.agent_activity_events,
            "agent_memory": run.agent_memory,
            "agent_transcript_ref": run.agent_transcript_ref,
            "acceptance_contract": run.acceptance_contract,
            "worker_summaries": run.worker_summaries,
            "flow_coverage": run.flow_coverage,
            "browser_flow_proof": run.browser_flow_proof,
            "tool_trace_ref": run.tool_trace_ref,
            "file_change_history_ref": run.file_change_history_ref,
            "browser_proof_ref": run.browser_proof_ref,
            "browser_replay_proof_ref": run.browser_replay_proof_ref,
            "large_tool_outputs_ref": run.large_tool_outputs_ref,
            "file_state_cache_ref": run.file_state_cache_ref,
            "turn_diff_ref": run.turn_diff_ref,
            "environment_snapshot_ref": run.environment_snapshot_ref,
            "tool_batch_summaries_ref": run.tool_batch_summaries_ref,
            "worker_mailbox_ref": run.worker_mailbox_ref,
            "worker_sessions_ref": run.worker_sessions_ref,
            "worker_ownership_ref": run.worker_ownership_ref,
            "draft_isolation_ref": run.draft_isolation_ref,
            "draft_gate_ref": run.draft_gate_ref,
            "draft_apply_decision_ref": run.draft_apply_decision_ref,
            "guardian_gate_ref": run.guardian_gate_ref,
            "scratchpad_ref": run.scratchpad_ref,
            "memory_ref": run.memory_ref,
            "worker_drafts_ref": run.worker_drafts_ref,
            "worker_merge_ref": run.worker_merge_ref,
            "trace_bundle_ref": run.trace_bundle_ref,
            "trace_reducer_ref": run.trace_reducer_ref,
            "command_policy_ref": run.command_policy_ref,
            "verification_report_ref": run.verification_report_ref,
            "rollout_trace_ref": run.rollout_trace_ref,
            "exec_trace_ref": run.exec_trace_ref,
            "process_outputs_ref": run.process_outputs_ref,
            "tool_result_messages_ref": run.tool_result_messages_ref,
            "active_processes": run.active_processes,
            "artifact_read_trace_ref": run.artifact_read_trace_ref,
            "resume_checkpoint_ref": run.resume_checkpoint_ref,
            "worker_branch_refs": run.worker_branch_refs,
            "verifier_review_ref": run.verifier_review_ref,
            "browser_step_refs": run.browser_step_refs,
            "active_tool_uses": run.active_tool_uses,
            "lsp_context_ref": run.lsp_context_ref,
            "context_manager_ref": run.context_manager_ref,
            "context_pressure_ref": run.context_pressure_ref,
            "hook_trace_ref": run.hook_trace_ref,
            "semantic_graph_ref": run.semantic_graph_ref,
            "worker_prefix_ref": run.worker_prefix_ref,
            "replay_trace_ref": run.replay_trace_ref,
            "prompt_contract_ref": run.prompt_contract_ref,
            "miniapp_contract_ref": run.miniapp_contract_ref,
            "route_registry_ref": run.route_registry_ref,
            "contract_compile_ref": run.contract_compile_ref,
            "repair_recipes_ref": run.repair_recipes_ref,
            "miniapp_contract": self.store.get("reports", run.miniapp_contract_ref) if run.miniapp_contract_ref else None,
            "route_registry": self.store.get("reports", run.route_registry_ref) if run.route_registry_ref else None,
            "repair_recipes": self.store.get("reports", run.repair_recipes_ref) if run.repair_recipes_ref else None,
            "prompt_contract": self.store.get("reports", run.prompt_contract_ref) if run.prompt_contract_ref else None,
            "process_outputs": process_outputs,
            "tool_result_messages": self.store.get("reports", run.tool_result_messages_ref) if run.tool_result_messages_ref else None,
            "agent_transcript": agent_transcript,
            "artifact_read_trace": self.store.get("reports", run.artifact_read_trace_ref) if run.artifact_read_trace_ref else None,
            "resume_checkpoint": self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None,
            "verifier_review": self.store.get("reports", run.verifier_review_ref) if run.verifier_review_ref else None,
            "browser_steps": [self.store.get("reports", ref) for ref in run.browser_step_refs if ref],
            "browser_replay_proof": self.store.get("reports", run.browser_replay_proof_ref) if run.browser_replay_proof_ref else None,
            "lsp_context": self.store.get("reports", run.lsp_context_ref) if run.lsp_context_ref else None,
            "context_manager": self.store.get("reports", run.context_manager_ref) if run.context_manager_ref else None,
            "context_pressure": self.store.get("reports", run.context_pressure_ref) if run.context_pressure_ref else None,
            "draft_isolation": self.store.get("reports", run.draft_isolation_ref) if run.draft_isolation_ref else None,
            "draft_gate": self.store.get("reports", run.draft_gate_ref) if run.draft_gate_ref else None,
            "draft_apply_decision": self.store.get("reports", run.draft_apply_decision_ref) if run.draft_apply_decision_ref else None,
            "guardian_gate": self.store.get("reports", run.guardian_gate_ref) if run.guardian_gate_ref else None,
            "hook_trace": self.store.get("reports", run.hook_trace_ref) if run.hook_trace_ref else None,
            "semantic_graph": self.store.get("reports", run.semantic_graph_ref) if run.semantic_graph_ref else None,
            "replay_trace": self.store.get("reports", run.replay_trace_ref) if run.replay_trace_ref else None,
            "reduced_graph": (agent_transcript or {}).get("reduced_graph", []) if isinstance(agent_transcript, dict) else [],
            "todo_plan": (scratchpad or {}).get("todo_plan", []) if isinstance(scratchpad, dict) else [],
            "todo_plan_markdown": ((scratchpad or {}).get("files") or {}).get("plan.md") if isinstance(scratchpad, dict) else None,
            "scratchpad": scratchpad,
            "worker_results": {
                "drafts": self.store.get("reports", run.worker_drafts_ref) if run.worker_drafts_ref else None,
                "merge": worker_merge,
            },
            "worker_merge_reports": (worker_merge or {}).get("merge_reports", []) if isinstance(worker_merge, dict) else [],
            "merge_conflicts": [
                item
                for item in ((worker_merge or {}).get("merge_reports", []) if isinstance(worker_merge, dict) else [])
                if isinstance(item, dict) and item.get("status") == "conflict"
            ],
            "compact_boundaries": (scratchpad or {}).get("compact_boundaries", []) if isinstance(scratchpad, dict) else [],
            "command_policy_decisions": (self.store.get("reports", run.command_policy_ref) or {}).get("examples", []) if run.command_policy_ref else [],
            "browser_proof_steps": (run.browser_flow_proof or {}).get("steps", []),
            "process_refs": {
                "process_outputs_ref": run.process_outputs_ref,
                "active_processes": run.active_processes,
                "output_count": len((process_outputs or {}).get("items", [])) if isinstance(process_outputs, dict) else 0,
            },
            "repair_issue_signatures": run.repair_issue_signatures,
            "mobile_layout_report": run.mobile_layout_report,
            "preview": preview_payload,
            "draft_preview": {
                key: value
                for key, value in preview_payload.items()
                if key in {"status", "stage", "progress_percent", "runtime_mode", "url", "role_urls", "draft_run_id"}
            },
            "latency_breakdown": job.latency_breakdown,
            "token_usage": run.token_usage,
            "token_usage_status": "recorded" if run.token_usage else "not_recorded",
            "retrieval_stats": job.retrieval_stats,
            "cache_stats": job.cache_stats,
            "apply_result": job.apply_result,
            "preview_infra_diagnostics": {
                "failure_kind": getattr(preview, "preview_failure_kind", None),
                "retry_count": getattr(preview, "preview_retry_count", 0),
                "cleanup_attempted": getattr(preview, "preview_cleanup_attempted", False),
                "reused_existing_runtime": getattr(preview, "preview_reused_existing_runtime", False),
                "cooldown_until": getattr(preview, "preview_cooldown_until", None).isoformat()
                if getattr(preview, "preview_cooldown_until", None)
                else None,
                "last_error": preview.last_error,
            },
            "failure_analysis": {
                "mode": run.mode,
                "failure_class": job.failure_class,
                "failure_signature": job.failure_signature,
                "root_cause_summary": job.root_cause_summary,
                "fix_targets": job.fix_targets,
                "handoff_from_failed_generate": job.handoff_from_failed_generate,
                "error_context": job.error_context.model_dump(mode="json") if job.error_context else None,
                "current_fix_phase": job.current_fix_phase,
                "current_failing_command": job.current_failing_command,
                "current_exit_code": job.current_exit_code,
                "executed_checks": job.executed_checks,
                "container_statuses": job.container_statuses,
            },
        }
        self.store.upsert("reports", f"run_artifacts:{run.run_id}", payload)
        run.storage_version = 2
        run.artifact_storage_ref = self.store.expected_storage_ref("reports", f"run_artifacts:{run.run_id}")
        self._save_run(run)

    def _extract_run_memory_stage1(self, run: RunRecord) -> None:
        artifacts = self.store.get("reports", f"run_artifacts:{run.run_id}")
        if not isinstance(artifacts, dict):
            artifacts = {}
        payload = WorkspaceMemoryPipeline.extract_run(run, artifacts)
        self.store.upsert("reports", f"memory_stage1:{run.workspace_id}:{run.run_id}", payload)
        try:
            self._auto_consolidate_workspace_memory(run.workspace_id)
        except Exception:
            logger.exception("memory_auto_consolidate_failed run_id=%s workspace_id=%s", run.run_id, run.workspace_id)
        if self.event_journal_service is not None:
            try:
                self.event_journal_service.append_run(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    event_type="memory.raw_extracted",
                    payload={"memory_ref": f"memory_stage1:{run.workspace_id}:{run.run_id}", "raw_count": len(payload.get("items") or [])},
                    actor="system",
                    summary="Raw run memory extracted.",
                    source_ref=f"memory_stage1:{run.workspace_id}:{run.run_id}",
                    idempotency_key=f"memory.raw_extracted:{run.run_id}",
                )
                self.event_journal_service.append_run(
                    workspace_id=run.workspace_id,
                    run_id=run.run_id,
                    event_type="memory.phase1.extracted",
                    payload={
                        "memory_ref": f"memory_stage1:{run.workspace_id}:{run.run_id}",
                        "raw_count": len(payload.get("items") or []),
                        "kinds": sorted({str(item.get("kind") or "") for item in payload.get("items") or [] if isinstance(item, dict)}),
                    },
                    actor="system",
                    summary="Phase 1 run memory extracted.",
                    source_ref=f"memory_stage1:{run.workspace_id}:{run.run_id}",
                    idempotency_key=f"memory.phase1.extracted:{run.run_id}",
                )
            except Exception:
                pass

    def _auto_consolidate_workspace_memory(self, workspace_id: str) -> None:
        stage1 = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"memory_stage1:{workspace_id}:") and isinstance(payload, dict)
        ]
        current = self.store.get("reports", f"workspace_memory:{workspace_id}") or {"workspace_id": workspace_id, "items": []}
        source_dir = self.workspace_service.source_dir(workspace_id)
        consolidated = WorkspaceMemoryPipeline.consolidate(
            workspace_id,
            stage1,
            current,
            workspace_root=source_dir,
        )
        stale_check = WorkspaceMemoryPipeline.stale_check(source_dir, consolidated)
        consolidated["stale_check"] = stale_check
        WorkspaceMemoryPipeline.apply_stale_status(consolidated, stale_check)
        consolidated["memory_summary"] = WorkspaceMemoryPipeline.summary(workspace_id, consolidated)
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", consolidated)
        pipeline = consolidated.get("pipeline") if isinstance(consolidated.get("pipeline"), dict) else {}
        self.store.upsert(
            "reports",
            f"memory_consolidation:{workspace_id}",
            {
                "schema": "grounded.memory_consolidation.v1",
                "workspace_id": workspace_id,
                "status": "auto_consolidated",
                "stage1_count": len(stage1),
                "raw_count": int(pipeline.get("stage1_items", 0) or 0),
                "active_count": int(pipeline.get("active_count", 0) or 0),
                "stale_count": int(pipeline.get("stale_count", 0) or 0),
                "expired_count": int(pipeline.get("expired_count", 0) or 0),
                "superseded_count": int(pipeline.get("superseded_count", 0) or 0),
                "deduped_count": int(pipeline.get("deduped_count", 0) or 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if self.event_journal_service is not None:
            repeated_stats = pipeline.get("repeated_failure_stats") if isinstance(pipeline.get("repeated_failure_stats"), dict) else {}
            for payload in stage1:
                run_id = str(payload.get("run_id") or "")
                if not run_id:
                    continue
                try:
                    self.event_journal_service.append_run(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        event_type="memory.phase2.consolidated",
                        payload={
                            "memory_ref": f"workspace_memory:{workspace_id}",
                            "consolidation_ref": f"memory_consolidation:{workspace_id}",
                            "stage1_count": len(stage1),
                            "repeated_failure_stats": repeated_stats,
                        },
                        actor="system",
                        summary="Phase 2 workspace memory consolidated.",
                        source_ref=f"workspace_memory:{workspace_id}",
                        idempotency_key=f"memory.phase2.consolidated:{workspace_id}:{run_id}",
                    )
                    if int(repeated_stats.get("repeated_failure_count", 0) or 0) > 0:
                        self.event_journal_service.append_run(
                            workspace_id=workspace_id,
                            run_id=run_id,
                            event_type="memory.repeated_failure.updated",
                            payload={"memory_ref": f"workspace_memory:{workspace_id}", "repeated_failure_stats": repeated_stats},
                            actor="system",
                            summary="Repeated failure memory updated.",
                            source_ref=f"workspace_memory:{workspace_id}",
                            idempotency_key=f"memory.repeated_failure.updated:{workspace_id}:{run_id}",
                        )
                except Exception:
                    pass

    def _schedule_auto_repair_continuation_if_needed(self, run: RunRecord) -> None:
        try:
            max_depth = int(os.getenv("GROUNDED_AUTO_REPAIR_CONTINUATION_MAX", "1") or "0")
        except ValueError:
            max_depth = 1
        if max_depth <= 0 or self.background_task_service is None:
            return
        if run.status not in {"blocked", "failed"} or not run.draft_ready:
            return
        if str(run.current_stage or "") == "blocked_provider_quota":
            return
        budget_exhausted = (
            "budget exhausted" in str(run.failure_reason or "").lower()
            or "token budget exhausted" in str(run.failure_reason or "").lower()
            or bool((run.budget_status or {}).get("exhausted"))
            or str(run.current_stage or "") == "blocked_budget_exhausted"
        )
        repeated_no_progress = (
            run.failure_class == "generation.repeated_no_progress"
            or str(run.failure_signature or "").startswith("repair.no_progress:")
            or str(run.current_fix_phase or "") == "blocked_repair_continuation_needed"
        )
        reliability_gate_blocked = (
            run.failure_class == "reliability_gate.blocked"
            or str(run.failure_signature or "").startswith("reliability_gate.blocked:")
            or str(run.current_stage or "") == "blocked by reliability gate"
        )
        if not budget_exhausted and not repeated_no_progress and not reliability_gate_blocked:
            return
        if self.store.get("reports", f"auto_repair_continuation:{run.run_id}"):
            return
        depth = self._auto_repair_continuation_depth(run)
        if depth >= max_depth:
            self.store.upsert(
                "reports",
                f"auto_repair_continuation:{run.run_id}",
                {
                    "schema": "grounded.auto_repair_continuation.v1",
                    "status": "skipped",
                    "reason": "max_depth_reached",
                    "run_id": run.run_id,
                    "workspace_id": run.workspace_id,
                    "depth": depth,
                    "max_depth": max_depth,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return
        repair_cases = RepairCaseService(self.store).list_cases(run.run_id)
        active_case = repair_cases.get("active_case") if isinstance(repair_cases, dict) else None
        if not isinstance(active_case, dict):
            return
        prompt = (
            "Continue this failed generation from the active repair case. "
            "Do not restart product design and do not invent fallback product semantics. "
            "Patch only the evidence-backed target slice, then rerun the expected proof.\n"
            f"Original product prompt:\n{run.prompt[:3000]}\n"
            "Active repair case:\n"
            f"{json.dumps(active_case.get('repair_prompt') or active_case, ensure_ascii=False, default=str)[:5000]}"
        )
        task = self.background_task_service.create_task(
            workspace_id=run.workspace_id,
            task_type="repair_failed_run",
            title="Auto repair continuation",
            run_id=run.run_id,
            input_payload={
                "source_run_id": run.run_id,
                "prompt": prompt,
                "auto_continuation": True,
                "auto_continuation_depth": depth + 1,
                "repair_case_id": active_case.get("case_id"),
                "repair_cases_ref": RepairCaseService.index_ref(run.run_id),
            },
            owner="agent_auto_repair",
            max_attempts=1,
            auto_start=True,
        )
        run.current_stage = "auto_repair_queued"
        run.summary = "Strict-green validation did not pass yet. Auto repair continuation was queued from the active repair case."
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(
            run.linked_job_id,
            "repair_started",
            "Auto repair continuation queued from active repair case.",
            {
                "event": "auto_repair_continuation_queued",
                "run_id": run.run_id,
                "task_id": task.task_id,
                "repair_case_id": active_case.get("case_id"),
                "depth": depth + 1,
                "max_depth": max_depth,
            },
        )
        self.store.upsert(
            "reports",
            f"auto_repair_continuation:{run.run_id}",
            {
                "schema": "grounded.auto_repair_continuation.v1",
                "status": "scheduled",
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "task_id": task.task_id,
                "repair_case_id": active_case.get("case_id"),
                "depth": depth + 1,
                "max_depth": max_depth,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _auto_repair_continuation_depth(self, run: RunRecord) -> int:
        depth = 0
        seen: set[str] = set()
        current: RunRecord | None = run
        while current and current.resume_from_run_id and current.resume_from_run_id not in seen:
            seen.add(current.resume_from_run_id)
            report = self.store.get("reports", f"auto_repair_continuation:{current.resume_from_run_id}")
            if isinstance(report, dict) and report.get("status") == "scheduled":
                depth += 1
            try:
                current = self.get_run(current.resume_from_run_id)
            except KeyError:
                break
        return depth

    def _resolve_inherited_acceptance_contract(
        self,
        source_run: RunRecord | None,
        *,
        request: CreateRunRequest,
    ) -> tuple[RunRecord | None, dict[str, Any]]:
        if source_run is None or request.mode != "fix":
            return None, {}
        seen: set[str] = set()
        current: RunRecord | None = source_run
        while current is not None and current.run_id not in seen:
            seen.add(current.run_id)
            contract = self._required_acceptance_contract_for_run(current)
            if contract:
                return current, contract
            if not current.resume_from_run_id:
                break
            try:
                current = self.get_run(current.resume_from_run_id)
            except KeyError:
                break
        return None, {}

    def _resolve_inherited_prompt_contract(
        self,
        source_run: RunRecord | None,
        *,
        request: CreateRunRequest,
    ) -> tuple[RunRecord | None, dict[str, Any]]:
        if source_run is None or request.mode != "fix":
            return None, {}
        seen: set[str] = set()
        current: RunRecord | None = source_run
        while current is not None and current.run_id not in seen:
            seen.add(current.run_id)
            ref = current.prompt_contract_ref or f"prompt_contract:{current.workspace_id}:{current.run_id}"
            report = self.store.get("reports", ref)
            if isinstance(report, dict):
                contract = report.get("contract") if isinstance(report.get("contract"), dict) else report
                if isinstance(contract, dict) and (contract.get("acceptance_contract") or contract.get("sections")):
                    return current, dict(report)
            if not current.resume_from_run_id:
                break
            try:
                current = self.get_run(current.resume_from_run_id)
            except KeyError:
                break
        return None, {}

    def _required_acceptance_contract_for_run(self, run: RunRecord) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        prompt_report = self.store.get("reports", run.prompt_contract_ref or f"prompt_contract:{run.workspace_id}:{run.run_id}")
        if isinstance(prompt_report, dict):
            prompt_contract = prompt_report.get("contract") if isinstance(prompt_report.get("contract"), dict) else {}
            if isinstance(prompt_contract, dict) and isinstance(prompt_contract.get("acceptance_contract"), dict):
                candidates.append(dict(prompt_contract["acceptance_contract"]))
        report = self.store.get("reports", f"acceptance_contract:{run.workspace_id}:{run.run_id}")
        if isinstance(report, dict) and isinstance(report.get("contract"), dict):
            candidates.append(dict(report["contract"]))
        if isinstance(run.acceptance_contract, dict):
            candidates.append(dict(run.acceptance_contract))
        required = [candidate for candidate in candidates if bool(candidate.get("required"))]
        if not required:
            return {}
        required.sort(key=lambda item: 0 if isinstance(item.get("prompt_hints"), dict) else 1)
        return dict(required[0])

    def _resolve_intent(self, workspace: WorkspaceRecord, request: CreateRunRequest, *, resolved_role_scope: list[str] | None = None) -> str:
        if request.intent != "auto":
            return request.intent
        if request.mode == "fix":
            return "edit"
        role_scope = list(resolved_role_scope if resolved_role_scope is not None else self._resolve_target_role_scope(request))
        if role_scope and len(role_scope) == 1:
            return "role_only_change"
        has_existing_build = self._workspace_has_existing_build(workspace)
        if has_existing_build:
            return "edit"
        return "create"

    @classmethod
    def _resolve_target_role_scope(cls, request: CreateRunRequest) -> list[str]:
        explicit_scope = [role for role in request.target_role_scope if role in ROLE_SCOPE]
        if explicit_scope:
            return explicit_scope
        return []

    def _agent_quality_report(self, workspace_id: str) -> dict[str, Any]:
        payload = self.code_agent_runtime.current_report(workspace_id, "agent_quality")
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _validation_scope_for_run(run: RunRecord) -> str:
        if run.intent in {"create", "edit", "refine", "role_only_change"}:
            return "agentic"
        if run.mode in {"generate", "fix"}:
            return "agentic"
        return "full_build"

    @staticmethod
    def _agent_quality_from_execution(execution: CheckExecutionRecord) -> dict[str, Any]:
        role_coverage: dict[str, Any] = {}
        generated_tests: dict[str, Any] = {}
        flow_coverage: dict[str, Any] = {}
        neutral_template_findings: list[dict[str, Any]] = []
        for result in execution.results:
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            if result.name == "platform_invariants":
                diagnostics_role_coverage = diagnostics.get("role_coverage")
                diagnostics_generated_tests = diagnostics.get("generated_tests")
                diagnostics_neutral_findings = diagnostics.get("neutral_template_findings")
                if isinstance(diagnostics_role_coverage, dict) and diagnostics_role_coverage:
                    role_coverage = dict(diagnostics_role_coverage)
                if isinstance(diagnostics_generated_tests, dict) and diagnostics_generated_tests:
                    generated_tests.update(diagnostics_generated_tests)
                if isinstance(diagnostics_neutral_findings, list):
                    neutral_template_findings = [
                        dict(item)
                        for item in diagnostics_neutral_findings
                        if isinstance(item, dict)
                    ]
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}:
                generated_tests[result.name] = {
                    "status": result.status,
                    "details": result.details,
                    "command": result.command,
                }
            if result.name == "frontend_interaction_static_smoke":
                flow_coverage = {
                    "status": result.status,
                    "details": result.details,
                    "diagnostics": dict(diagnostics),
                    "logs": list(result.logs or []),
                }
            if result.name == "browser_flow_smoke":
                flow_coverage = {
                    **flow_coverage,
                    "browser_flow": {
                        "status": result.status,
                        "details": result.details,
                        "diagnostics": dict(diagnostics),
                        "logs": list(result.logs or []),
                    },
                }
        return {
            "workspace_id": execution.workspace_id,
            "role_coverage": role_coverage,
            "generated_tests": generated_tests,
            "flow_coverage": flow_coverage,
            "neutral_template_findings": neutral_template_findings,
        }

    def _store_agent_quality_from_execution(
        self,
        workspace_id: str,
        execution: CheckExecutionRecord,
    ) -> dict[str, Any]:
        payload = self._agent_quality_from_execution(execution)
        payload["workspace_id"] = workspace_id
        self.store.upsert("reports", f"agent_quality:{workspace_id}", payload)
        return payload

    def _resolve_generation_mode(
        self,
        workspace: WorkspaceRecord,
        request: CreateRunRequest,
        resolved_intent: str,
    ) -> GenerationMode:
        if request.mode == "fix":
            return request.generation_mode
        if request.generation_mode != GenerationMode.QUALITY:
            return request.generation_mode
        has_existing_build = self._workspace_has_existing_build(workspace)
        if resolved_intent in {"edit", "refine", "role_only_change"} and has_existing_build:
            return GenerationMode.BALANCED
        return request.generation_mode

    def _workspace_has_existing_build(self, workspace: WorkspaceRecord) -> bool:
        if not workspace.template_cloned or workspace.current_revision_id is None:
            return False
        if any(str(revision.source or "").strip() != "template_clone" for revision in workspace.revisions):
            return True
        source_dir = self.workspace_service.source_dir(workspace.workspace_id)
        try:
            status_output = self.workspace_service._git_output(source_dir, ["status", "--porcelain"])
        except Exception:
            return False
        paths: list[str] = []
        for line in status_output.splitlines():
            candidate = str(line[3:] if len(line) > 3 else "").strip()
            if " -> " in candidate:
                candidate = candidate.split(" -> ", 1)[-1].strip()
            if candidate:
                paths.append(candidate)
        return any(self._is_meaningful_source_path(path) for path in paths)

    @staticmethod
    def _resolve_model_profile(model_profile: str | None, generation_mode: GenerationMode) -> str:
        return resolve_model_profile(model_profile, generation_mode)

    def _build_change_plan(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        diff_text: str,
        prompt: str,
    ) -> CodeChangePlan:
        iteration_payload = self.code_agent_runtime.current_report(workspace_id, "iterations") or {}
        targets: list[CodeChangeTarget] = []
        seen_paths: set[str] = set()
        file_paths = self._paths_from_diff(diff_text)
        if not file_paths:
            file_paths = self._paths_from_iterations(iteration_payload.get("items", []))
        for file_path in file_paths:
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)
            targets.append(
                CodeChangeTarget(
                    file_path=file_path,
                    operation="replace",
                    reason="Touched by the draft workspace diff.",
                    risk="medium" if file_path.startswith("artifacts/") else "low",
                )
            )

        summary = f"Prepare draft code changes for prompt: {prompt[:120]}"
        risks = [
            "Generated app behavior must remain valid in the preview shell.",
            "Draft changes should not overwrite manual workspace edits outside the reviewed draft.",
        ]
        if diff_text.strip():
            risks.append("Existing workspace edits must be preserved and not overwritten unexpectedly.")
        acceptance_checks = [
            "Agent-authored changes satisfy the user prompt.",
            "Platform checks succeed on the draft workspace.",
            "Preview runtime starts and serves usable app pages.",
        ]
        return CodeChangePlan(
            workspace_id=workspace_id,
            run_id=run.run_id,
            intent=run.intent,
            summary=summary,
            target_role_scope=run.target_role_scope,
            targets=targets,
            risks=risks,
            acceptance_checks=acceptance_checks,
        )

    def _resolve_touched_files(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        change_plan: CodeChangePlan,
        request: CreateRunRequest,
    ) -> list[str]:
        paths = [
            target.file_path
            for target in change_plan.targets
            if target.file_path and self._is_meaningful_source_path(target.file_path)
        ]
        if request.mode == "fix":
            inherited = self._inherited_touched_files_for_fix(workspace_id=workspace_id, run=run)
            if inherited:
                paths = list(dict.fromkeys([*paths, *inherited]))
        if paths:
            return list(dict.fromkeys(paths))
        candidate_paths = [target.file_path for target in change_plan.targets if target.file_path]
        if request.mode == "fix":
            inherited = self._inherited_touched_files_for_fix(workspace_id=workspace_id, run=run)
            if inherited:
                candidate_paths = list(dict.fromkeys([*candidate_paths, *inherited]))
        return candidate_paths

    def _revision_commit_sha(self, workspace: WorkspaceRecord, revision_id: str | None) -> str | None:
        if not revision_id:
            return None
        for revision in workspace.revisions:
            if revision.revision_id == revision_id:
                return revision.commit_sha
        return None

    def _meaningful_paths_between_revisions(
        self,
        *,
        workspace_id: str,
        source_revision_id: str | None,
        result_revision_id: str | None,
    ) -> list[str]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_sha = self._revision_commit_sha(workspace, source_revision_id)
        result_sha = self._revision_commit_sha(workspace, result_revision_id)
        if not source_sha or not result_sha or source_sha == result_sha:
            return []
        try:
            diff_output = self.workspace_service._git_output(
                self.workspace_service.source_dir(workspace_id),
                ["diff", "--name-only", f"{source_sha}..{result_sha}"],
            )
        except Exception:
            return []
        paths = [line.strip() for line in diff_output.splitlines() if line.strip()]
        return [path for path in list(dict.fromkeys(paths)) if self._is_meaningful_source_path(path)]

    def _inherited_touched_files_for_fix(self, *, workspace_id: str, run: RunRecord) -> list[str]:
        if run.mode != "fix":
            return []
        source_run_id = str(run.resume_from_run_id or "").strip()
        if not source_run_id:
            return []
        try:
            source_run = self.get_run(source_run_id)
        except KeyError:
            return []

        inherited = [path for path in source_run.touched_files if self._is_meaningful_source_path(path)]
        if inherited:
            return list(dict.fromkeys(inherited))

        if self.workspace_service.draft_exists(workspace_id, source_run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=source_run_id)
            diff_paths = [
                path
                for path in self._paths_from_diff(diff_text)
                if self._is_meaningful_source_path(path)
            ]
            if diff_paths:
                return list(dict.fromkeys(diff_paths))
        return []

    def _mark_run_without_meaningful_diff(self, run: RunRecord, job: Any) -> None:
        message = "Draft produced no meaningful source changes to apply."
        run.summary = message
        run.failure_reason = message
        run.status = "failed"
        run.apply_status = "failed"
        run.outcome_kind = "noop_generation_failure"
        run.draft_status = "discarded" if self.workspace_service.draft_exists(run.workspace_id, run.run_id) else "none"
        run.draft_ready = False
        run.current_stage = "failed"
        run.progress_percent = self._terminal_failure_progress(run.progress_percent)
        run.current_fix_phase = job.current_fix_phase

        if self.workspace_service.draft_exists(run.workspace_id, run.run_id):
            self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        job.status = "failed"
        job.summary = message
        job.failure_reason = message
        self.code_agent_runtime.append_event(job, "job_failed", message, {"reason": "no_meaningful_diff"})

    @staticmethod
    def _should_wait_for_preview_refresh(request: CreateRunRequest, run: RunRecord) -> bool:
        mode = str(getattr(run.generation_mode, "value", run.generation_mode) or request.generation_mode or "").strip().lower()
        if mode in {"fast", "basic"}:
            return False
        if str(run.intent or "").strip().lower() not in {"edit", "refine", "role_only_change"}:
            return True
        probe = GenerateRequest(
            prompt=request.prompt,
            mode=request.mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            generation_mode=run.generation_mode,
            intent=run.intent,
            target_role_scope=run.target_role_scope,
            model_profile=run.model_profile,
            linked_run_id=run.run_id,
            resume_from_run_id=request.resume_from_run_id,
            error_context=request.error_context,
        )
        return WorkspaceCodeAgentRuntime._focused_edit_kind(probe) not in {"visual_style_edit", "small_copy_edit"}

    @staticmethod
    def _clear_successful_completion_metadata(*, run: RunRecord, job: Any | None = None) -> None:
        run.failure_reason = None
        run.failure_class = None
        run.failure_signature = None
        run.root_cause_summary = None
        run.current_fix_phase = None
        run.current_failing_command = None
        run.current_exit_code = None
        run.fix_targets = []
        run.handoff_from_failed_generate = None
        if job is not None:
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
            job.current_fix_phase = None
            job.current_failing_command = None
            job.current_exit_code = None
            job.fix_targets = []
            job.handoff_from_failed_generate = None

    def _apply_completed_draft(self, run: RunRecord, *, message: str) -> bool:
        allowed, _ = self.enforce_guardian_before_apply(run, source="pre_apply_guardian")
        if not allowed:
            return False
        apply_started_at = time.perf_counter()
        run.current_stage = "finalizing apply"
        run.progress_percent = max(run.progress_percent, 94)
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        self._append_job_event(run.linked_job_id, "apply_started", message)
        self._record_before_apply_checkpoint(run, source="auto_apply")
        revision = self.workspace_service.approve_draft(run.workspace_id, run.run_id, f"Auto-apply AI draft for run {run.run_id}")
        self.workspace_service.discard_draft(run.workspace_id, run.run_id)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.status = "completed"
        run.apply_status = "applied"
        run.outcome_kind = "applied"
        run.remaining_issues = []
        self._clear_successful_completion_metadata(run=run)
        run.draft_status = "approved"
        run.draft_ready = False
        run.current_stage = "completed"
        run.progress_percent = 100
        run.latency_breakdown["apply_ms"] = int((time.perf_counter() - apply_started_at) * 1000)
        revision_paths = self._meaningful_paths_between_revisions(
            workspace_id=run.workspace_id,
            source_revision_id=run.source_revision_id,
            result_revision_id=run.result_revision_id,
        )
        if revision_paths:
            run.touched_files = revision_paths
        self._append_job_event(
            run.linked_job_id,
            "apply_completed",
            "Generated draft was applied successfully.",
        )
        return True

    def _meaningful_paths_for_run(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        change_plan: CodeChangePlan,
        job: Any | None = None,
    ) -> list[str]:
        candidate_diff = (self.code_agent_runtime.current_report(workspace_id, "candidate_diff") or {}).get("diff", "")
        diff_text = candidate_diff
        if not diff_text and self.workspace_service.draft_exists(workspace_id, run.run_id):
            diff_text = self.workspace_service.diff(workspace_id, run_id=run.run_id)

        paths = self._paths_from_diff(diff_text)
        if not paths:
            paths = [target.file_path for target in change_plan.targets if target.file_path]
        if not paths and job is not None:
            apply_result = getattr(job, "apply_result", None) or {}
            if isinstance(apply_result, dict):
                changed_files = apply_result.get("changed_files")
                if isinstance(changed_files, list):
                    paths.extend(str(path) for path in changed_files if str(path).strip())
            fix_targets = getattr(job, "fix_targets", None)
            if isinstance(fix_targets, list):
                paths.extend(str(path) for path in fix_targets if str(path).strip())
        meaningful = [path for path in list(dict.fromkeys(paths)) if self._is_meaningful_source_path(path)]
        acceptance_required = bool((run.acceptance_contract or {}).get("required")) or run.mode in {"generate", "fix"}
        if acceptance_required:
            return [path for path in meaningful if self._is_product_runtime_source_path(path)]
        return meaningful

    @staticmethod
    def _is_meaningful_source_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return False
        path = PurePosixPath(normalized)
        if any(part in MEANINGFUL_DIFF_IGNORED_PARTS for part in path.parts):
            return False
        if path.name in MEANINGFUL_DIFF_IGNORED_NAMES:
            return False
        if path.name.endswith(MEANINGFUL_DIFF_IGNORED_SUFFIXES):
            return False
        if normalized.startswith("miniapp/app/generated/"):
            return False
        return True

    @staticmethod
    def _is_product_runtime_source_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized.startswith("miniapp/"):
            return False
        if normalized.startswith("miniapp/tests/") or normalized.startswith("miniapp/app/generated/"):
            return False
        if "/__pycache__/" in normalized or normalized.endswith(MEANINGFUL_DIFF_IGNORED_SUFFIXES):
            return False
        return normalized.startswith(("miniapp/app/", "miniapp/requirements.txt", "miniapp/Dockerfile"))

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE):
            candidate = match.group(1).strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            if candidate:
                paths.append(candidate)
        return paths

    @staticmethod
    def _paths_from_iterations(iterations: Any) -> list[str]:
        if not isinstance(iterations, list):
            return []
        paths: list[str] = []
        for iteration in iterations:
            if not isinstance(iteration, dict):
                continue
            file_changes = iteration.get("file_changes")
            if not isinstance(file_changes, list):
                continue
            for operation in file_changes:
                if not isinstance(operation, dict):
                    continue
                file_path = operation.get("file_path")
                if isinstance(file_path, str) and file_path.strip():
                    paths.append(file_path.strip())
        return list(dict.fromkeys(paths))

    @staticmethod
    def _build_checks_summary(
        validation_snapshot: Any,
        preview_status: str,
        *,
        gate_status: str | None = None,
    ) -> RunChecksSummary:
        issues = []
        validators = "pending"
        build = "pending"
        preview = "pending"
        if validation_snapshot:
            issues = list(getattr(validation_snapshot, "issues", []) or [])
            issue_codes = {
                str(issue.get("code") or "")
                for issue in issues
                if isinstance(issue, dict)
            }
            has_generation_block = any(code.startswith("generation.") for code in issue_codes)
            has_build_issue = any(code.startswith("build.") for code in issue_codes)
            has_preview_issue = any(code.startswith("preview.") for code in issue_codes)
            if getattr(validation_snapshot, "blocking", False) and has_generation_block:
                validators = "blocked"
            elif has_build_issue or has_preview_issue:
                validators = "passed"
            elif getattr(validation_snapshot, "platform_valid", False) or getattr(validation_snapshot, "checks_valid", False):
                validators = "passed"
            else:
                validators = "failed"
            build = "passed" if getattr(validation_snapshot, "build_valid", False) else "failed"
            if not getattr(validation_snapshot, "build_valid", False):
                preview = "skipped"
            elif preview_status == "running":
                preview = "passed"
            elif preview_status == "error":
                preview = "failed"
            else:
                preview = "pending"
        elif preview_status == "running":
            preview = "passed"
        elif preview_status == "error":
            preview = "failed"
        resolved_gate_status = gate_status
        if resolved_gate_status is None:
            if validators in {"failed", "blocked"} or build == "failed":
                resolved_gate_status = "failed"
            elif validators == "passed" and build == "passed":
                resolved_gate_status = "passed"
            else:
                resolved_gate_status = "pending"
        return RunChecksSummary(
            validators=validators,
            build=build,
            preview=preview,
            gate_status=resolved_gate_status,
            issues=issues,
        )

    @staticmethod
    def _copy_checks_summary(summary: RunChecksSummary | dict[str, Any], **updates: Any) -> RunChecksSummary:
        payload = summary.model_dump(mode="python") if hasattr(summary, "model_dump") else dict(summary or {})
        payload.update(updates)
        return RunChecksSummary.model_validate(payload)
