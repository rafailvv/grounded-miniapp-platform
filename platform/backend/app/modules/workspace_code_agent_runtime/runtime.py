from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.ai.model_registry import models_for_role, resolve_model_profile
from app.ai.openai_client import OpenAIClient
from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    BackgroundTaskRecord,
    CheckExecutionRecord,
    DraftAction,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    RunRecord,
    ValidationSnapshot,
)
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_command_policy import command_policy_snapshot
from app.modules.miniapp_agent_loop.agent_coordinator import AgentCoordinator
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.agent_tool_call_loop import AgentToolCallLoop
from app.modules.miniapp_agent_loop.agent_tool_changes import (
    file_changes_from_mutating_tool_calls,
    is_mutating_agent_tool_call,
)
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.agent_worker_branch_loop import AgentWorkerBranchLoop, WorkerBranchResult
from app.modules.miniapp_agent_loop.agent_worker_runtime import AgentWorkerRuntime
from app.modules.miniapp_agent_loop.agent_worker_tasks import AgentWorkerTaskPlanner
from app.modules.miniapp_agent_loop.product_workers import (
    SECRET_RE,
    canonical_worker_id,
    ownership_for_worker,
    path_is_allowed,
    select_memory_items,
    stale_path_checks,
    worker_refs,
)
from app.modules.miniapp_agent_loop.agent_kernel import compact_agent_memory
from app.modules.miniapp_agent_loop.agent_memory_store import AgentMemoryStore
from app.modules.miniapp_agent_loop.agent_scratchpad import AgentScratchpad
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.context_pressure import AgentContextPressureAnalyzer
from app.modules.miniapp_agent_loop.environment_snapshot import AgentEnvironmentSnapshot
from app.modules.miniapp_agent_loop.rollout_trace import RolloutTraceRecorder
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.agent_tool_runtime import (
    normalize_tool_calls,
    truncate_tool_text,
    validate_workspace_command,
)
from app.modules.miniapp_agent_loop.types import AgentLoopCallbacks, AgentLoopResult, AgentTurnPlan
from app.modules.miniapp_agent_loop.turn_diff_tracker import AgentTurnDiffTracker
from app.modules.miniapp_agent_loop.verification_worker import VerificationWorker
from app.modules.workspace_code_agent_runtime.budget import (
    completion_budget_for_mode,
    completion_budget_status,
    token_usage_total,
)
from app.modules.workspace_code_agent_runtime.artifact_reporter import WorkspaceAgentArtifactReporter
from app.modules.workspace_code_agent_runtime.browser_replay import BrowserProofReplay
from app.modules.workspace_code_agent_runtime.check_orchestrator import WorkspaceAgentCheckOrchestrator
from app.modules.workspace_code_agent_runtime.completion_gate import WorkspaceAgentCompletionGate
from app.modules.workspace_code_agent_runtime.prompt_contract import agent_system_prompt
from app.modules.workspace_code_agent_runtime.process_recovery import AgentProcessRecovery
from app.modules.workspace_code_agent_runtime.tool_executor import AgentToolExecutor
from app.repositories.platform_db import PlatformDb
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.miniapp_contract import MiniAppContractCompiler, MiniAppContractMaterializer, MiniAppRouteRegistry
from app.services.platform_shell import BASE_STYLESHEET_HREF, BASE_STYLESHEET_PATH, PAGE_SHELL_INLINE_STYLE
from app.services.run_compaction import RunCompactionService
from app.services.run_protocol import RunProtocolService, diff_sha256
from app.services.trace_bundle import TraceBundleWriter
from app.services.generation_enhancements import SkillPackCatalog
from app.services.workflow_acceptance import (
    build_acceptance_contract,
    build_implementation_plan,
    orchestration_metadata_for_contract,
)
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.services.workspace.service import WorkspaceService
from app.validators.static_analysis import extract_declared_routes

logger = logging.getLogger(__name__)

ROLE_ORDER = ("client", "specialist", "manager")
PLATFORM_SHELL_TEMPLATE_PATHS = (
    "miniapp/app/main.py",
    "miniapp/app/routes/health.py",
    "miniapp/app/routes/role_pages.py",
    "miniapp/app/routes/role_routes.py",
)
QUALITY_FIDELITY = {
    GenerationMode.FAST: "fast_app",
    GenerationMode.QUALITY: "quality_app",
    GenerationMode.BALANCED: "balanced_app",
    GenerationMode.BASIC: "basic_app",
}
FOCUSED_VISUAL_WRITE_LIMIT = 4
FOCUSED_VISUAL_CONTENT_MAX_LENGTH = 12000
TOOL_RESULT_SPILL_THRESHOLD_CHARS = 6000
PATCH_FIRST_EXISTING_FILE_CHAR_LIMIT = 2500
PATCH_FIRST_EXISTING_FILE_LINE_LIMIT = 120
READ_ONLY_WRITE_PREFIXES = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".next/",
    ".vite/",
)
INITIAL_CONTEXT_PATHS = (
    "miniapp/tests/test_generated_app.py",
    "miniapp/tests/generated_app.test.mjs",
    "README.md",
    "docs/agent-guidelines.md",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/static/shared/base.css",
    "miniapp/app/static/client/index.html",
    "miniapp/app/static/client/app.js",
)
class WorkspaceCodeAgentRuntime:
    """Single agentic code path for create, edit, fix, refine, and visual changes."""

    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        check_runner: CheckRunner,
        preview_service: PreviewService,
        runtime_manager: PreviewRuntimeManager,
        openai_client: OpenAIClient,
        workspace_log_service: WorkspaceLogService,
        agent_tool_call_loop: AgentToolCallLoop,
        context_pack_builder: Any | None = None,
        platform_db: PlatformDb | None = None,
        run_protocol_service: RunProtocolService | None = None,
        run_compaction_service: RunCompactionService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.check_runner = check_runner
        self.preview_service = preview_service
        self.runtime_manager = runtime_manager
        self.openai_client = openai_client
        self.workspace_log_service = workspace_log_service
        self.agent_tool_call_loop = agent_tool_call_loop
        self.artifact_reporter = WorkspaceAgentArtifactReporter(store)
        self.check_orchestrator = WorkspaceAgentCheckOrchestrator()
        self.completion_gate = WorkspaceAgentCompletionGate(workspace_service)
        self.browser_replay = BrowserProofReplay()
        self.process_recovery = AgentProcessRecovery()
        self.file_state_cache = AgentFileStateCache()
        self.turn_diff_tracker = AgentTurnDiffTracker()
        self.worker_manager = AgentWorkerManager()
        self.worker_runtime = AgentWorkerRuntime()
        self.worker_branch_loop = AgentWorkerBranchLoop(
            openai_client=openai_client,
            workspace_service=workspace_service,
            read_artifact=lambda ref: self.store.get("reports", ref),
        )
        self.memory_store = AgentMemoryStore()
        self.hook_manager = AgentHookManager()
        self.context_pressure = AgentContextPressureAnalyzer()
        self.rollout_trace = RolloutTraceRecorder()
        self.transcript_store = AgentTranscriptStore()
        self.process_manager = AgentProcessManager()
        self.trace_bundle_writers: dict[str, TraceBundleWriter] = {}
        self.tool_batch_summaries: dict[str, list[dict[str, object]]] = {}
        self.context_pressure_history: dict[str, list[dict[str, Any]]] = {}
        self.scratchpads: dict[str, AgentScratchpad] = {}
        self.coordinators: dict[str, AgentCoordinator] = {}
        self.environment_snapshots: dict[str, dict[str, object]] = {}
        self.verification_reports: dict[str, dict[str, object]] = {}
        self.tool_executor = AgentToolExecutor(
            workspace_service=workspace_service,
            file_state_cache=self.file_state_cache,
            process_manager=self.process_manager,
            read_artifact=lambda ref: self.store.get("reports", ref),
        )
        self.context_pack_builder = context_pack_builder
        self.platform_db = platform_db
        self.run_protocol_service = run_protocol_service
        self.run_compaction_service = run_compaction_service

    def get_job(self, job_id: str) -> JobRecord:
        payload = self.store.get("jobs", job_id)
        if not payload:
            raise KeyError(f"Job not found: {job_id}")
        return JobRecord.model_validate(payload)

    def latest_job_for_workspace(self, workspace_id: str) -> JobRecord | None:
        latest: JobRecord | None = None
        for payload in self.store.list("jobs"):
            if str(payload.get("workspace_id") or "") != workspace_id:
                continue
            candidate = JobRecord.model_validate(payload)
            if latest is None or candidate.updated_at > latest.updated_at:
                latest = candidate
        return latest

    def current_report(self, workspace_id: str, report_type: str) -> dict[str, Any] | None:
        payload = self.store.get("reports", f"{report_type}:{workspace_id}")
        return dict(payload) if isinstance(payload, dict) else None

    def _stored_acceptance_contract_for_runtime(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        stored_run: dict[str, Any] | None,
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        candidates.extend(self._acceptance_contract_candidates(workspace_id=workspace_id, run_id=run_id, run_payload=stored_run))
        source_run_id = str(request.resume_from_run_id or "").strip()
        seen: set[str] = set()
        while source_run_id and source_run_id not in seen:
            seen.add(source_run_id)
            source_payload = self.store.get("runs", source_run_id)
            if not isinstance(source_payload, dict):
                break
            source_workspace = str(source_payload.get("workspace_id") or workspace_id)
            candidates.extend(
                self._acceptance_contract_candidates(
                    workspace_id=source_workspace,
                    run_id=source_run_id,
                    run_payload=source_payload,
                )
            )
            source_run_id = str(source_payload.get("resume_from_run_id") or "").strip()
        required = [candidate for candidate in candidates if bool(candidate.get("required"))]
        if required:
            required.sort(key=lambda item: 0 if isinstance(item.get("prompt_hints"), dict) else 1)
            return dict(required[0])
        for candidate in candidates:
            if candidate.get("blocking") or str(candidate.get("status") or "").startswith("blocked_"):
                return dict(candidate)
        return {}

    def _acceptance_contract_candidates(
        self,
        *,
        workspace_id: str,
        run_id: str,
        run_payload: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        report = self.store.get("reports", f"acceptance_contract:{workspace_id}:{run_id}")
        if isinstance(report, dict) and isinstance(report.get("contract"), dict):
            candidates.append(dict(report["contract"]))
        if isinstance(run_payload, dict) and isinstance(run_payload.get("acceptance_contract"), dict):
            candidates.append(dict(run_payload["acceptance_contract"]))
        return candidates

    def _contract_prompt_for_runtime_contract(
        self,
        *,
        workspace_id: str,
        request: GenerateRequest,
        stored_run: dict[str, Any] | None,
        stored_plan: dict[str, Any],
        acceptance_contract: dict[str, Any],
    ) -> str:
        candidate_run_ids: list[str] = []
        repair_continuation = stored_plan.get("repair_continuation") if isinstance(stored_plan.get("repair_continuation"), dict) else {}
        for value in (
            acceptance_contract.get("contract_source_run_id"),
            acceptance_contract.get("inherited_from_run_id"),
            repair_continuation.get("contract_source_run_id"),
            repair_continuation.get("source_run_id"),
            request.resume_from_run_id,
        ):
            run_id = str(value or "").strip()
            if run_id and run_id not in candidate_run_ids:
                candidate_run_ids.append(run_id)
        if isinstance(stored_run, dict):
            prompt = str(stored_run.get("prompt") or "").strip()
            if prompt and not candidate_run_ids:
                return prompt
        for run_id in candidate_run_ids:
            payload = self.store.get("runs", run_id)
            if not isinstance(payload, dict):
                continue
            if str(payload.get("workspace_id") or workspace_id) != workspace_id:
                continue
            prompt = str(payload.get("prompt") or "").strip()
            if prompt:
                return prompt
        return request.prompt

    def retry_from_job(self, job_id: str, *, should_stop: Callable[[], bool] | None = None) -> JobRecord:
        job = self.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            mode=job.mode,
            target_platform=job.target_platform,
            preview_profile=job.preview_profile,
            generation_mode=job.generation_mode,
            intent="edit" if job.mode == "fix" else "auto",
            target_role_scope=[],
            model_profile=job.model_profile or "",
            linked_run_id=job.linked_run_id,
            error_context=job.error_context,
        )
        return self.generate(job.workspace_id, request, should_stop=should_stop)

    def append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self._append_event(job, event_type, message, details)

    def validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        return self._validation_snapshot_from_execution(execution)

    def generate(
        self,
        workspace_id: str,
        request: GenerateRequest,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> JobRecord:
        started_at = time.perf_counter()
        workspace = self.workspace_service.get_workspace(workspace_id)
        generation_mode = self._generation_mode(request.generation_mode)
        model_profile = resolve_model_profile(request.model_profile, generation_mode)
        role_scope = self._effective_role_scope(request)
        run_id = request.linked_run_id or f"agent_{int(time.time() * 1000)}"
        job = JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            mode=request.mode,
            status="running",
            generation_mode=generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace.current_revision_id,
            fidelity=QUALITY_FIDELITY[generation_mode],  # type: ignore[arg-type]
            llm_enabled=self.openai_client.enabled,
            llm_provider=(self.openai_client.configuration().get("routing") or {}).get("provider") if self.openai_client.enabled else None,
            model_profile=model_profile,
            linked_run_id=run_id,
            error_context=request.error_context,
            current_fix_phase="agent_loop",
            execution_class="shell_app",
            completion_budget=completion_budget_for_mode(generation_mode),
        )
        writer = self._trace_bundle_writer(job)
        job.trace_bundle_ref = f"trace_bundle:{workspace_id}:{run_id}"
        self._store_report(job.trace_bundle_ref, {"workspace_id": workspace_id, "run_id": run_id, **writer.index(status="recording")})
        self._clear_agent_reports(workspace_id)
        self._clear_trace(workspace_id)
        self._save_job(job)
        self._append_event(job, "job_started", "Workspace code agent started.", {"run_id": run_id, "mode": request.mode})
        self._record_hook(
            job=job,
            hook="session_start",
            status="completed",
            payload={"run_id": run_id, "workspace_id": workspace_id},
        )
        self._record_hook(
            job=job,
            hook="before_run",
            status="completed",
            payload={"run_id": run_id, "mode": request.mode, "generation_mode": generation_mode.value},
        )

        if not self.openai_client.enabled:
            job.status = "blocked"
            job.outcome_kind = "blocked_generation"
            job.summary = "Workspace code agent requires OpenAI."
            job.failure_reason = "Set OPENAI_API_KEY before generating or editing a workspace."
            job.failure_class = "generation.llm_required"
            job.current_fix_phase = "failed"
            job.latency_breakdown["agent_total_ms"] = int((time.perf_counter() - started_at) * 1000)
            self._append_event(job, "job_failed", job.summary, {"failure_class": job.failure_class})
            self._record_hook(
                job=job,
                hook="after_run",
                status="failed",
                payload={"status": job.status, "failure_class": job.failure_class},
            )
            self._finalize_trace_bundle(job)
            self._save_job(job)
            return job

        draft_source = self._prepare_draft(workspace_id=workspace_id, run_id=run_id, request=request)
        self._restore_file_state_cache_from_resume(
            workspace_id=workspace_id,
            run_id=run_id,
            source_run_id=str(request.resume_from_run_id or "").strip(),
        )
        self._store_report(
            f"spec:{workspace_id}:{run_id}",
            {
                "workspace_id": workspace_id,
                "source": "user_prompt",
                "prompt": request.prompt,
                "runtime": "workspace_code_agent",
                "prompt_policy": "Prompt semantics are authoritative; no app category is assumed.",
            },
        )
        self._store_report(
            f"execution_class:{workspace_id}:{run_id}",
            {"workspace_id": workspace_id, "run_id": run_id, "execution_class": "shell_app", "runtime": "workspace_code_agent"},
        )

        with self.openai_client.routing_context(model_profile=model_profile, generation_mode=generation_mode):
            loop_result = self._run_loop(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request.model_copy(update={"model_profile": model_profile, "target_role_scope": role_scope}),
                job=job,
                draft_source=draft_source,
                role_scope=role_scope,
                generation_mode=generation_mode,
                loop_started_at=started_at,
                should_stop=should_stop,
            )
        return self._finalize_job(
            job=job,
            loop_result=loop_result,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )

    def _prepare_draft(self, *, workspace_id: str, run_id: str, request: GenerateRequest) -> Path:
        source_run_id = str(request.resume_from_run_id or "").strip()
        if source_run_id and source_run_id != run_id and self.workspace_service.draft_exists(workspace_id, source_run_id):
            return self.workspace_service.clone_draft(workspace_id, source_run_id, run_id)
        if self.workspace_service.draft_exists(workspace_id, run_id):
            return self.workspace_service.ensure_draft(workspace_id, run_id)
        return self.workspace_service.prepare_draft(workspace_id, run_id)

    def _restore_file_state_cache_from_resume(self, *, workspace_id: str, run_id: str, source_run_id: str) -> None:
        if not source_run_id or source_run_id == run_id:
            return
        checkpoint = self.store.get("reports", f"resume_checkpoint:{workspace_id}:{source_run_id}")
        if not isinstance(checkpoint, dict):
            return
        ref = str(checkpoint.get("file_state_cache_ref") or "").strip()
        if not ref:
            return
        snapshot = self.store.get("reports", ref)
        if not isinstance(snapshot, dict):
            return
        result = self.file_state_cache.restore_snapshot(run_id=run_id, snapshot=snapshot)
        self._store_report(
            f"file_state_cache_restore:{workspace_id}:{run_id}",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "source_run_id": source_run_id,
                "source_file_state_cache_ref": ref,
                **result,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def _effective_role_scope(request: GenerateRequest) -> list[str]:
        explicit_scope = [role for role in request.target_role_scope if role in ROLE_ORDER]
        if request.intent == "create" or (request.intent == "auto" and request.mode == "generate" and not explicit_scope):
            return list(ROLE_ORDER)
        return explicit_scope or list(ROLE_ORDER)

    @classmethod
    def _focused_edit_kind(cls, request: GenerateRequest) -> str:
        intent = str(request.intent or "").strip().lower()
        if intent not in {"edit", "refine", "role_only_change"}:
            return "standard"
        return "behavior_edit"

    @staticmethod
    def _focused_visual_css_paths(role_scope: list[str] | tuple[str, ...] | None = None) -> list[str]:
        paths: list[str] = ["miniapp/app/static/shared/base.css"]
        for role in role_scope or ROLE_ORDER:
            if role in ROLE_ORDER:
                paths.append(f"miniapp/app/static/{role}/styles.css")
        return list(dict.fromkeys(paths))

    def _focused_visual_initial_context(self, workspace_id: str, run_id: str, *, role_scope: list[str]) -> dict[str, str]:
        contexts: dict[str, str] = {}
        for path in self._focused_visual_css_paths(role_scope):
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is not None:
                contexts[path] = content
        return contexts

    def _stabilize_platform_shell(
        self,
        workspace_id: str,
        run_id: str,
        draft_source: Any,
        changed_files: list[str],
    ) -> list[str]:
        del workspace_id, run_id, changed_files
        source_dir = Path(draft_source)
        stabilized: list[str] = []
        template_dir = getattr(getattr(self.workspace_service, "settings", None), "template_dir", None)
        template_root = Path(template_dir) if template_dir else None
        if template_root is not None:
            for relative in PLATFORM_SHELL_TEMPLATE_PATHS:
                template_path = template_root / relative
                target_path = source_dir / relative
                if not template_path.exists():
                    continue
                try:
                    template_text = template_path.read_text(encoding="utf-8")
                    current_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
                except OSError:
                    continue
                if current_text != template_text:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(template_text, encoding="utf-8")
                    stabilized.append(relative)

        base_path = source_dir / BASE_STYLESHEET_PATH
        if base_path.exists():
            try:
                original = base_path.read_text(encoding="utf-8")
            except OSError:
                original = ""
            updated = self._ensure_base_shell_safe_spacing(original)
            if updated != original:
                base_path.write_text(updated, encoding="utf-8")
                stabilized.append(BASE_STYLESHEET_PATH)

        static_root = source_dir / "miniapp/app/static"
        if static_root.exists():
            for css_path in sorted(static_root.glob("*/styles.css")):
                try:
                    original = css_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                updated = self._ensure_role_shell_safe_spacing(original)
                if updated != original:
                    css_path.write_text(updated, encoding="utf-8")
                    stabilized.append(css_path.relative_to(source_dir).as_posix())
            for html_path in sorted(static_root.rglob("index.html")):
                try:
                    original = html_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                updated = self._ensure_html_platform_shell(original)
                if updated != original:
                    html_path.write_text(updated, encoding="utf-8")
                    stabilized.append(html_path.relative_to(source_dir).as_posix())
            if self._sync_route_manifest_from_static_pages(source_dir):
                stabilized.append("miniapp/app/generated/route_manifest.json")
        return list(dict.fromkeys(stabilized))

    @staticmethod
    def _sync_route_manifest_from_static_pages(source_dir: Path) -> bool:
        """Keep platform routing metadata aligned with generated role pages.

        This does not generate product UI or product data. It only makes every
        existing static role page reachable through the mini-app shell so
        tests, preview, and browser proof operate on the same route graph.
        """
        static_root = source_dir / "miniapp/app/static"
        if not static_root.exists():
            return False
        generated_dir = source_dir / "miniapp/app/generated"
        manifest_path = generated_dir / "route_manifest.json"
        existing: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError):
                existing = {}
        roles_payload: dict[str, Any] = {}
        for role in ROLE_ORDER:
            role_root = static_root / role
            if not role_root.exists():
                continue
            pages: list[dict[str, str]] = []
            routes: dict[str, str] = {}
            for html_path in sorted(role_root.rglob("index.html")):
                if not html_path.is_file():
                    continue
                rel_to_role = html_path.relative_to(role_root).as_posix()
                if rel_to_role == "index.html":
                    route_path = f"/{role}"
                    page_id = "root"
                    label = "Главная"
                else:
                    slug = rel_to_role.removesuffix("/index.html").strip("/")
                    route_path = f"/{role}/{slug}".rstrip("/")
                    page_id = slug.replace("/", "-") or "page"
                    label = slug.replace("_", " ").replace("-", " ").title() or "Page"
                file_ref = html_path.relative_to(source_dir / "miniapp/app").as_posix()
                page = {
                    "id": page_id,
                    "route_path": route_path,
                    "file_path": file_ref,
                    "navigation_label": label,
                }
                script_ref = html_path.with_name("app.js")
                style_ref = html_path.with_name("styles.css")
                if script_ref.exists():
                    page["script_path"] = script_ref.relative_to(source_dir / "miniapp/app").as_posix()
                if style_ref.exists():
                    page["style_path"] = style_ref.relative_to(source_dir / "miniapp/app").as_posix()
                pages.append(page)
                routes[route_path] = file_ref
            if pages:
                roles_payload[role] = {"pages": pages, "routes": routes}
        if not roles_payload:
            return False
        updated = {
            **existing,
            "roles": roles_payload,
            "shared": existing.get("shared") if isinstance(existing.get("shared"), dict) else {},
        }
        rendered = json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        original = ""
        if manifest_path.exists():
            try:
                original = manifest_path.read_text(encoding="utf-8")
            except OSError:
                original = ""
        if rendered == original:
            return False
        generated_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(rendered, encoding="utf-8")
        return True

    def _enforce_patch_first_file_changes(
        self,
        file_changes: list[DraftAction],
        *,
        request: GenerateRequest,
        workspace_id: str,
        run_id: str,
    ) -> list[DraftAction]:
        if str(request.intent or "").strip().lower() == "create":
            return list(file_changes)
        normalized: list[DraftAction] = []
        for operation in file_changes:
            if operation.operation not in {"create", "replace"}:
                normalized.append(operation)
                continue
            new_content = operation.content
            if new_content is None:
                normalized.append(operation)
                continue
            current_content = self.workspace_service.try_read_text_file(workspace_id, operation.file_path, run_id=run_id)
            if current_content is None:
                normalized.append(operation)
                continue
            if self._patch_first_allows_full_replace(operation, current_content):
                normalized.append(operation)
                continue
            diff = self._unified_diff_for_existing_file(operation.file_path, current_content, new_content)
            if not diff.strip():
                normalized.append(operation)
                continue
            normalized.append(
                DraftAction(
                    operation_id=operation.operation_id,
                    file_path=operation.file_path,
                    operation="patch",
                    diff=diff,
                    content=None,
                    reason=f"{operation.reason} Converted to patch-first diff for existing file.",
                )
            )
        return normalized

    @staticmethod
    def _patch_first_allows_full_replace(operation: DraftAction, current_content: str) -> bool:
        reason = str(operation.reason or "").lower()
        if any(marker in reason for marker in ("apply conflict", "patch conflict", "hunk", "rejected", "precondition")):
            return True
        if len(current_content) <= PATCH_FIRST_EXISTING_FILE_CHAR_LIMIT:
            return True
        if len(current_content.splitlines()) <= PATCH_FIRST_EXISTING_FILE_LINE_LIMIT:
            return True
        return False

    @staticmethod
    def _unified_diff_for_existing_file(file_path: str, before: str, after: str) -> str:
        before_lines = str(before or "").splitlines(keepends=True)
        after_lines = str(after or "").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                lineterm="\n",
            )
        )

    @staticmethod
    def _ensure_base_shell_safe_spacing(content: str) -> str:
        text = str(content or "")
        normalized = re.sub(r"\s+", "", text.lower())
        expected_safe_top = "padding-top:max(76px,calc(var(--telegram-top-safe-offset)+12px))"
        if ".page-shell" in text and expected_safe_top in normalized and "!important" in normalized:
            return text
        guard = (
            "\n\n/* Platform invariant: generated role CSS must not remove Telegram header clearance. */\n"
            ".page-shell {\n"
            "  padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px)) !important;\n"
            "}\n"
        )
        return text.rstrip() + guard

    @staticmethod
    def _ensure_role_shell_safe_spacing(content: str) -> str:
        text = str(content or "")
        safe_top = "padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px)) !important;"

        def rewrite(match: re.Match[str]) -> str:
            selectors = match.group("selectors")
            body = match.group("body")
            if ".page-shell" not in selectors:
                return match.group(0)
            if "telegram-top-safe-offset" in body:
                return match.group(0)
            if not re.search(r"\bpadding(?:-top)?\s*:", body, re.IGNORECASE):
                return match.group(0)
            body_text = body.rstrip()
            separator = "\n  " if "\n" in body_text else " "
            return f"{selectors}{{{body_text.rstrip(';')};{separator}{safe_top} }}"

        return re.sub(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", rewrite, text, flags=re.MULTILINE)

    @staticmethod
    def _ensure_page_shell_inline_safe_spacing(content: str) -> str:
        def update_tag(match: re.Match[str]) -> str:
            tag = match.group(1)
            close = match.group("close")
            style_match = re.search(r"""\sstyle=(?P<quote>["'])(?P<value>.*?)(?P=quote)""", tag, flags=re.IGNORECASE | re.DOTALL)
            if not style_match:
                return f'{tag} style="{PAGE_SHELL_INLINE_STYLE}"{close}'
            style_value = style_match.group("value")
            if re.search(r"padding-top\s*:", style_value, flags=re.IGNORECASE):
                updated_style = re.sub(
                    r"padding-top\s*:[^;]+;?",
                    PAGE_SHELL_INLINE_STYLE,
                    style_value,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                separator = "" if not style_value.strip() or style_value.rstrip().endswith(";") else "; "
                updated_style = f"{style_value.rstrip()}{separator}{PAGE_SHELL_INLINE_STYLE}"
            start, end = style_match.span("value")
            return f"{tag[:start]}{updated_style}{tag[end:]}{close}"

        return re.sub(
            r"""(<main\b(?=[^>]*\bclass=(["'])[^"']*\bpage-shell\b[^"']*\2)[^>]*)(?P<close>>)""",
            update_tag,
            str(content or ""),
            flags=re.IGNORECASE,
        )

    @classmethod
    def _ensure_html_platform_shell(cls, content: str) -> str:
        text = cls._ensure_base_stylesheet_link(str(content or ""))
        text = cls._ensure_page_shell_root(text)
        return cls._ensure_preview_bridge_script(text)

    @staticmethod
    def _ensure_base_stylesheet_link(content: str) -> str:
        text = str(content or "")
        if BASE_STYLESHEET_HREF in text:
            return text
        link = f'    <link rel="stylesheet" href="{BASE_STYLESHEET_HREF}" />\n'
        role_style = re.search(
            r"^[ \t]*<link\b[^>]+href=[\"']/static/(?:client|specialist|manager)/styles\.css[\"'][^>]*>\s*$",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if role_style:
            return text[: role_style.start()] + link + text[role_style.start() :]
        head_close = re.search(r"</head\s*>", text, flags=re.IGNORECASE)
        if head_close:
            return text[: head_close.start()] + link + text[head_close.start() :]
        return link + text

    @classmethod
    def _ensure_page_shell_root(cls, content: str) -> str:
        text = str(content or "")
        if "page-shell" in text:
            return cls._ensure_page_shell_inline_safe_spacing(text)

        def update_main(match: re.Match[str]) -> str:
            tag = match.group(0)
            class_match = re.search(r"""\bclass=(?P<quote>["'])(?P<value>.*?)(?P=quote)""", tag, flags=re.IGNORECASE | re.DOTALL)
            if class_match:
                class_value = class_match.group("value").strip()
                updated_class = f"{class_value} page-shell".strip()
                start, end = class_match.span("value")
                tag = f"{tag[:start]}{updated_class}{tag[end:]}"
            else:
                tag = tag[:-1].rstrip() + ' class="page-shell">'
            return tag

        if re.search(r"<main\b", text, flags=re.IGNORECASE):
            updated = re.sub(r"<main\b[^>]*>", update_main, text, count=1, flags=re.IGNORECASE | re.DOTALL)
            return cls._ensure_page_shell_inline_safe_spacing(updated)

        body_match = re.search(r"<body\b[^>]*>", text, flags=re.IGNORECASE)
        body_close = re.search(r"</body\s*>", text, flags=re.IGNORECASE)
        if body_match and body_close and body_close.start() >= body_match.end():
            inner = text[body_match.end() : body_close.start()]
            wrapped = (
                f'\n    <main class="page-shell" style="{PAGE_SHELL_INLINE_STYLE}">'
                f"{inner.rstrip()}\n    </main>\n  "
            )
            return text[: body_match.end()] + wrapped + text[body_close.start() :]
        return (
            f'<main class="page-shell" style="{PAGE_SHELL_INLINE_STYLE}">\n'
            f"{text.rstrip()}\n"
            "</main>\n"
        )

    @staticmethod
    def _ensure_preview_bridge_script(content: str) -> str:
        text = str(content or "")
        if "/static/preview_bridge.js" in text:
            return text
        script = '    <script src="/static/preview_bridge.js" defer></script>\n'
        app_script = re.search(r"^[ \t]*<script\b[^>]+src=[\"']/static/[^\"']+/app\.js[\"'][^>]*></script>\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
        if app_script:
            return text[: app_script.start()] + script + text[app_script.start() :]
        body_close = re.search(r"</body\s*>", text, flags=re.IGNORECASE)
        if body_close:
            return text[: body_close.start()] + script + text[body_close.start() :]
        return text.rstrip() + "\n" + script

    def _run_loop(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        draft_source: Path,
        role_scope: list[str],
        generation_mode: GenerationMode,
        loop_started_at: float,
        should_stop: Callable[[], bool] | None,
    ) -> AgentLoopResult:
        focused_edit_kind = self._focused_edit_kind(request)
        focused_visual_edit = focused_edit_kind == "visual_style_edit"
        create_intent = str(request.intent or "").strip().lower() == "create"
        stored_run = self.store.get("runs", run_id) if run_id else None
        stored_acceptance_contract = self._stored_acceptance_contract_for_runtime(
            workspace_id=workspace_id,
            run_id=run_id,
            request=request,
            stored_run=stored_run if isinstance(stored_run, dict) else None,
        )
        stored_plan = (
            dict(stored_run.get("implementation_plan") or {})
            if isinstance(stored_run, dict) and isinstance(stored_run.get("implementation_plan"), dict)
            else {}
        )
        prompt_analysis: dict[str, Any] | None = None
        stored_contract_is_authoritative = bool(
            stored_acceptance_contract.get("required")
            or stored_acceptance_contract.get("blocking")
            or str(stored_acceptance_contract.get("status") or "").startswith("blocked_")
            or isinstance(stored_acceptance_contract.get("prompt_hints"), dict)
        )
        requires_prompt_analysis = (
            create_intent
            or focused_edit_kind == "behavior_workflow_edit"
            or generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
        ) and not stored_contract_is_authoritative
        if requires_prompt_analysis:
            prompt_analysis = self.openai_client.analyze_miniapp_prompt(
                prompt=request.prompt,
                generation_mode=generation_mode,
                model_profile=request.model_profile,
            )
        acceptance_contract = (
            stored_acceptance_contract
            if stored_contract_is_authoritative
            else build_acceptance_contract(
                prompt=request.prompt,
                intent=str(request.intent or ""),
                generation_mode=generation_mode,
                focused_edit_kind=focused_edit_kind,
                prompt_analysis=prompt_analysis,
            )
        )
        contract_prompt = self._contract_prompt_for_runtime_contract(
            workspace_id=workspace_id,
            request=request,
            stored_run=stored_run if isinstance(stored_run, dict) else None,
            stored_plan=stored_plan,
            acceptance_contract=acceptance_contract,
        )
        orchestration = orchestration_metadata_for_contract(
            contract=acceptance_contract,
            generation_mode=generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        implementation_plan = stored_plan or build_implementation_plan(
            prompt=request.prompt,
            intent=str(request.intent or ""),
            generation_mode=generation_mode,
            acceptance_contract=acceptance_contract,
            orchestration=orchestration,
            prompt_analysis=prompt_analysis,
        )
        miniapp_contract = None
        contract_materialized_paths: list[str] = []
        contract_runtime_fast_path = False
        if acceptance_contract.get("required"):
            miniapp_contract = MiniAppContractCompiler.compile(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=contract_prompt,
                intent=str(request.intent or ""),
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                implementation_plan=implementation_plan,
                prompt_analysis=prompt_analysis,
            )
            acceptance_contract = miniapp_contract.acceptance_summary
        job.acceptance_contract = acceptance_contract
        job.implementation_plan = implementation_plan
        job.orchestration_phases = list(orchestration.get("phases") or [])
        job.worker_summaries = list(orchestration.get("worker_summaries") or [])
        job.flow_coverage = {
            "status": "blocked_contract_missing" if acceptance_contract.get("blocking") or str(acceptance_contract.get("status") or "").startswith("blocked_") else "planned" if acceptance_contract.get("required") else "not_required",
            "required_flows": [flow.get("id") for flow in acceptance_contract.get("flows", []) if isinstance(flow, dict)],
        }
        if acceptance_contract.get("blocking") or str(acceptance_contract.get("status") or "").startswith("blocked_"):
            return self.results.blocked(
                summary="Run blocked because prompt-derived product contract is missing.",
                failure_reason=str(acceptance_contract.get("reason") or "Prompt-derived product contract is missing."),
                failure_class="acceptance_contract_missing",
                failure_signature="acceptance.blocked_contract_missing",
                root_cause_summary="Generation stopped before edits to avoid platform-invented product semantics.",
                current_phase="planning",
                latest_execution=None,
                latest_preview_details={},
                latest_apply_result=None,
                iterations=[],
                repair_iterations=[],
                all_file_changes=[],
                last_assistant_message="",
                turn_history=[
                    {
                        "turn": 0,
                        "result": "blocked_contract_missing",
                        "issues": list(acceptance_contract.get("issues") or []),
                    }
                ],
            )
        artifact_run_id = run_id or job.job_id
        self.tool_batch_summaries[artifact_run_id] = []
        self.context_pressure_history[artifact_run_id] = []
        coordinator = AgentCoordinator(run_id=artifact_run_id, generation_mode=generation_mode, implementation_plan=implementation_plan)
        scratchpad = AgentScratchpad(run_id=artifact_run_id)
        scratchpad.set_plan(implementation_plan, coordinator.snapshot().get("todo_plan", []))  # type: ignore[arg-type]
        scratchpad.set_route_ui_manifest(
            {
                "roles": list(ROLE_ORDER),
                "flows": list(acceptance_contract.get("flows") or []),
                "api_contract": implementation_plan.get("api_contract") or {},
                "ui_contract": implementation_plan.get("ui_contract") or {},
            }
        )
        self.coordinators[artifact_run_id] = coordinator
        self.scratchpads[artifact_run_id] = scratchpad
        coordinator.start_phase("planning", "Implementation plan and role workflow contract prepared.")
        self.rollout_trace.append(artifact_run_id, "plan", {"generation_mode": str(generation_mode), "intent": str(request.intent or "")})
        job.scratchpad_ref = f"scratchpad:{workspace_id}:{artifact_run_id}"
        job.agent_transcript_ref = f"agent_transcript:{workspace_id}:{artifact_run_id}"
        job.tool_result_messages_ref = f"tool_result_messages:{workspace_id}:{artifact_run_id}"
        job.resume_checkpoint_ref = f"resume_checkpoint:{workspace_id}:{artifact_run_id}"
        job.command_policy_ref = f"command_policy:{workspace_id}:{artifact_run_id}"
        job.context_pressure_ref = f"context_pressure:{workspace_id}:{artifact_run_id}"
        job.hook_trace_ref = f"hook_trace:{workspace_id}:{artifact_run_id}"
        job.semantic_graph_ref = f"semantic_graph:{workspace_id}:{artifact_run_id}"
        job.worker_prefix_ref = f"worker_prefix:{workspace_id}:{artifact_run_id}"
        if miniapp_contract is not None:
            job.miniapp_contract_ref = f"miniapp_contract:{workspace_id}:{artifact_run_id}"
            job.contract_compile_ref = f"contract_compile:{workspace_id}:{artifact_run_id}"
            job.route_registry_ref = f"route_registry:{workspace_id}:{artifact_run_id}"
            job.repair_recipes_ref = f"repair_recipes:{workspace_id}:{artifact_run_id}"
            contract_materialized_paths = MiniAppContractMaterializer.materialize(
                draft_source,
                miniapp_contract,
                include_role_shell=False,
            )
            contract_runtime_fast_path = False
            route_registry_snapshot = MiniAppRouteRegistry.snapshot(
                draft_source,
                miniapp_contract,
                regenerated_files=contract_materialized_paths,
            )
            self._store_report(
                job.miniapp_contract_ref,
                {"workspace_id": workspace_id, "run_id": run_id, "contract": miniapp_contract.model_dump(mode="json")},
            )
            self._store_report(
                job.contract_compile_ref,
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "status": "compiled_metadata_only",
                    "contract_id": miniapp_contract.contract_id,
                    "materialized_paths": contract_materialized_paths,
                    "materialized_runtime": False,
                    "materialized_tests": False,
                },
            )
            self._store_report(
                job.route_registry_ref,
                {"workspace_id": workspace_id, "run_id": run_id, "snapshot": route_registry_snapshot.model_dump(mode="json")},
            )
            self._store_report(
                job.repair_recipes_ref,
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "items": [item.model_dump(mode="json") for item in route_registry_snapshot.repair_recipes],
                },
            )
        restored_process_view = self.process_recovery.restore_view(self.store.get("reports", job.resume_checkpoint_ref))
        if restored_process_view.get("stale_processes"):
            self._store_report(
                f"process_recovery:{workspace_id}:{artifact_run_id}",
                {"workspace_id": workspace_id, "run_id": run_id, **restored_process_view},
            )
            self._append_event(
                job,
                "tool_progress",
                "Recovered process checkpoint; stale running processes will be rerun if needed.",
                {"process_recovery_ref": f"process_recovery:{workspace_id}:{artifact_run_id}", **restored_process_view},
            )
        self._configure_transcript_persistence(job, artifact_run_id, restore_existing=True)
        self._store_report(job.scratchpad_ref, {"workspace_id": workspace_id, "run_id": run_id, **scratchpad.snapshot()})
        self._store_report(
            job.resume_checkpoint_ref,
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "phase": "planning",
                "agent_transcript_ref": job.agent_transcript_ref,
                "scratchpad_ref": job.scratchpad_ref,
                "todo_plan": coordinator.snapshot().get("todo_plan", []),
                "process_summary": self.process_recovery.checkpoint(self.process_manager.snapshot()),
            },
        )
        self._store_report(job.command_policy_ref, {"workspace_id": workspace_id, "run_id": run_id, **command_policy_snapshot()})
        self._store_report(job.context_pressure_ref, {"workspace_id": workspace_id, "run_id": run_id, "items": []})
        self._store_report(job.hook_trace_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.hook_manager.snapshot(artifact_run_id)})
        semantic_graph = semantic_scan(root=draft_source, targets=["miniapp/app", "miniapp/tests"])
        self._store_report(job.semantic_graph_ref, {"workspace_id": workspace_id, "run_id": run_id, "graph": semantic_graph})
        environment_snapshot = AgentEnvironmentSnapshot.capture(
            draft_source=draft_source,
            command_policy=validate_workspace_command,
        )
        self.environment_snapshots[artifact_run_id] = environment_snapshot
        job.environment_snapshot_ref = f"environment_snapshot:{workspace_id}:{artifact_run_id}"
        self._store_report(
            job.environment_snapshot_ref,
            {"workspace_id": workspace_id, "run_id": run_id, "snapshot": environment_snapshot},
        )
        worker_mailbox = self.worker_manager.mailbox_for_plan(
            generation_mode=generation_mode,
            implementation_plan=implementation_plan,
        )
        worker_tasks = AgentWorkerTaskPlanner.worker_tasks(
            generation_mode=generation_mode,
            implementation_plan=implementation_plan,
        )
        if not bool(worker_mailbox.get("enabled")):
            worker_tasks = []
        worker_prefix = self._worker_prefix_payload(
            implementation_plan=implementation_plan,
            acceptance_contract=acceptance_contract,
            semantic_graph=semantic_graph,
            current_diff_summary="",
        )
        self._store_report(
            job.worker_prefix_ref,
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "prefix": worker_prefix,
                "contract": (
                    "Worker branches are disabled for this mode; prompt-contract metadata is stable and mutations are serialized through the coordinator."
                    if not bool(worker_mailbox.get("enabled"))
                    else "Worker branches share this stable prefix; only owned task directive differs."
                ),
            },
        )
        job.worker_mailbox_ref = f"worker_mailbox:{workspace_id}:{artifact_run_id}"
        self._store_report(
            job.worker_mailbox_ref,
            {"workspace_id": workspace_id, "run_id": run_id, "mailbox": worker_mailbox, "worker_tasks": worker_tasks},
        )
        worker_tasks = self._prepare_product_worker_artifacts(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_run_id=artifact_run_id,
            job=job,
            generation_mode=generation_mode,
            worker_tasks=worker_tasks,
            implementation_plan=implementation_plan,
            acceptance_contract=acceptance_contract,
            semantic_graph=semantic_graph,
            worker_prefix=worker_prefix,
            draft_source=draft_source,
            enabled=bool(worker_mailbox.get("enabled")),
        )
        self._store_report(
            job.worker_mailbox_ref,
            {"workspace_id": workspace_id, "run_id": run_id, "mailbox": worker_mailbox, "worker_tasks": worker_tasks},
        )
        writer_worker_tasks = [
            task
            for task in worker_tasks
            if canonical_worker_id(str(task.get("worker_id") or "")).strip() not in {"mobile_polish_worker"}
        ]
        worker_drafts = self.worker_runtime.prepare_workspace_branches(
            workspace_id=workspace_id,
            run_id=artifact_run_id,
            generation_mode=generation_mode,
            workspace_service=self.workspace_service,
            worker_specs=writer_worker_tasks,
        )
        job.worker_drafts_ref = f"worker_drafts:{workspace_id}:{artifact_run_id}"
        job.worker_merge_ref = f"worker_merge:{workspace_id}:{artifact_run_id}"
        self._store_report(job.worker_drafts_ref, {"workspace_id": workspace_id, "run_id": run_id, **worker_drafts})
        self._store_report(job.worker_merge_ref, {"workspace_id": workspace_id, "run_id": run_id, "merge_reports": []})
        if worker_drafts.get("enabled"):
            for worker in worker_drafts.get("workers") or []:
                if isinstance(worker, dict):
                    self._append_event(
                        job,
                        "worker_started",
                        f"Prepared isolated worker draft for {worker.get('worker_id')}.",
                        {"worker_id": worker.get("worker_id"), "owner_scope": worker.get("owner_scope"), "artifact_ref": job.worker_drafts_ref},
                    )
        self._append_activity(
            job,
            "planning",
            "Coordinator prepared todo plan and worker drafts.",
            {
                "phase": "planning",
                "status": "completed",
                "summary": "Plan, scratchpad, command policy, and worker draft metadata are ready.",
                "artifact_ref": job.scratchpad_ref,
            },
            save=False,
        )
        if acceptance_contract.get("required") or request.mode == "generate":
            self._store_report(
                f"acceptance_contract:{workspace_id}:{run_id}",
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "contract": acceptance_contract,
                    "implementation_plan": implementation_plan,
                    "orchestration": orchestration,
                },
            )
            self._store_report(
                f"implementation_plan:{workspace_id}:{run_id}",
                {"workspace_id": workspace_id, "run_id": run_id, "implementation_plan": implementation_plan},
            )
            self._append_event(
                job,
                "spec_extract_started",
                "Created implementation plan and role workflow acceptance contract.",
                {
                    "run_id": run_id,
                    "workflow_kind": focused_edit_kind,
                    "orchestration_enabled": bool(orchestration.get("enabled")),
                    "flow_ids": [flow.get("id") for flow in acceptance_contract.get("flows", []) if isinstance(flow, dict)],
                    "primary_entities": list(implementation_plan.get("primary_entities") or []),
                    "environment_snapshot_ref": job.environment_snapshot_ref,
                    "worker_mailbox_ref": job.worker_mailbox_ref,
                },
            )
        coordinator.complete_phase("planning", "Implementation plan, scratchpad, policy, and worker metadata persisted.")
        scratchpad.set_next_action(
            action="inspect draft files and semantic maps before patching",
            reason="planning_complete",
            payload={"semantic_graph_ref": job.semantic_graph_ref, "worker_prefix_ref": job.worker_prefix_ref},
        )
        initial_context = (
            self._focused_visual_initial_context(workspace_id, run_id, role_scope=role_scope)
            if focused_visual_edit
            else self._initial_file_context(workspace_id, run_id, role_scope=role_scope)
        )
        coordinator.complete_phase("reading", "Initial draft files and semantic maps selected.")
        scratchpad.append_worker_note(
            "coordinator",
            "Initial draft context selected.",
            {"file_count": len(initial_context), "files": list(initial_context.keys())[:24]},
        )
        scratchpad.set_next_action(action="produce the smallest useful draft patch", reason="reading_complete")
        self._store_report(job.scratchpad_ref, {"workspace_id": workspace_id, "run_id": run_id, **scratchpad.snapshot(), **coordinator.snapshot()})
        tool_results: list[dict[str, object]] = []
        if focused_visual_edit:
            tool_results.append(self._focused_visual_edit_budget_result(role_scope=role_scope))
        last_changed_files: list[str] = (
            self._focused_visual_css_paths(role_scope)
            if focused_visual_edit
            else (contract_materialized_paths or ["miniapp", "docs", "README.md"])
        )
        if contract_materialized_paths and create_intent and not focused_visual_edit:
            coordinator.complete_phase("editing", "Prompt-contract metadata was written; product backend, UI, and tests remain agent-owned.")
            scratchpad.set_next_action(
                action="build the prompt-derived product backend, role UI, tests, then run proof",
                reason="prompt_contract_metadata_materialized",
                payload={"paths": contract_materialized_paths[:24]},
            )
            self._append_event(
                job,
                "patch_apply_completed",
                "Prompt-contract metadata materialized without product scaffold.",
                {
                    "turn": 0,
                    "files": contract_materialized_paths[:24],
                    "file_count": len(contract_materialized_paths),
                },
            )
        cached_no_diff_checks: tuple[CheckExecutionRecord, dict[str, Any]] | None = None
        branch_initial_file_changes: list[DraftAction] = []
        branch_initial_message = "Workspace code agent initialized."
        if (
            worker_drafts.get("enabled")
            and bool(acceptance_contract.get("required"))
            and not contract_runtime_fast_path
            and not focused_visual_edit
            and writer_worker_tasks
        ):
            branch_initial_file_changes, branch_changed_files, branch_message = self._run_parallel_worker_branches(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                request=request,
                job=job,
                generation_mode=generation_mode,
                worker_tasks=writer_worker_tasks,
                worker_drafts=worker_drafts,
                worker_prefix=worker_prefix,
            )
            if branch_changed_files:
                stabilized = self._stabilize_platform_shell(workspace_id, run_id, draft_source, branch_changed_files)
                if stabilized:
                    self.file_state_cache.invalidate(run_id, stabilized)
                    branch_changed_files = list(dict.fromkeys([*branch_changed_files, *stabilized]))
                last_changed_files = branch_changed_files
                branch_initial_message = branch_message

        def _execute_checks(changed_files: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            nonlocal last_changed_files, cached_no_diff_checks
            last_changed_files = list(changed_files or last_changed_files)
            if miniapp_contract is not None and job.route_registry_ref and job.repair_recipes_ref:
                registry_regenerated = MiniAppRouteRegistry.sync_contract_owned_files(draft_source, miniapp_contract)
                if registry_regenerated:
                    self.file_state_cache.invalidate(run_id, registry_regenerated)
                    last_changed_files = list(dict.fromkeys([*last_changed_files, *registry_regenerated]))
                registry_snapshot = MiniAppRouteRegistry.snapshot(
                    draft_source,
                    miniapp_contract,
                    regenerated_files=registry_regenerated,
                )
                self._store_report(
                    job.route_registry_ref,
                    {"workspace_id": workspace_id, "run_id": run_id, "snapshot": registry_snapshot.model_dump(mode="json")},
                )
                self._store_report(
                    job.repair_recipes_ref,
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "items": [item.model_dump(mode="json") for item in registry_snapshot.repair_recipes],
                    },
                )
            has_draft_diff = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
            self._append_event(
                job,
                "running_checks",
                "Starting validation checks.",
                {
                    "attempt": 1 if has_draft_diff else 0,
                    "has_file_edits": has_draft_diff,
                    "changed_files": list(last_changed_files),
                },
            )
            self._record_hook(
                job=job,
                hook="before_checks",
                status="started",
                payload={"changed_files": list(last_changed_files), "has_draft_diff": has_draft_diff},
            )
            if not has_draft_diff and cached_no_diff_checks is not None:
                self._record_hook(
                    job=job,
                    hook="before_checks",
                    status="completed",
                    payload={"changed_files": list(last_changed_files), "cached": True, "has_draft_diff": False},
                )
                return cached_no_diff_checks
            check_plan = self.check_orchestrator.plan(
                focused_visual_edit=focused_visual_edit,
                create_intent=create_intent,
                acceptance_required=bool(acceptance_contract.get("required")),
                generation_mode=generation_mode,
                has_draft_diff=has_draft_diff,
            )

            def _check_progress_callback(check_step: str, payload: dict[str, Any]) -> None:
                event_type = self._check_progress_event_type(check_step)
                details = {
                    **payload,
                    "attempt": check_plan.check_attempt,
                    "has_file_edits": has_draft_diff,
                    "has_draft_diff": has_draft_diff,
                    "changed_files": list(last_changed_files),
                }
                self._append_event(job, event_type, self._check_progress_message(check_step, payload), details)

            execution = self.check_runner.run(
                workspace_id=workspace_id,
                run_id=run_id,
                source_dir=draft_source,
                changed_files=list(last_changed_files),
                preview_run_id=run_id,
                scope_mode=check_plan.scope_mode,
                check_profile=check_plan.check_profile,
                intent=str(request.intent or ""),
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                progress_callback=_check_progress_callback,
            )
            if self.check_orchestrator.should_run_final_gate(
                check_profile=check_plan.check_profile,
                execution=execution,
                has_draft_diff=bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
                create_intent=str(request.intent or "").strip().lower() == "create",
                acceptance_required=bool(acceptance_contract.get("required")),
            ):
                self._append_event(
                    job,
                    "final_checks_started",
                    "Running final generated app checks.",
                    {"attempt": 1, "has_file_edits": True, "changed_files": list(last_changed_files)},
                )
                execution = self.check_runner.run(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    source_dir=draft_source,
                    changed_files=list(last_changed_files),
                    preview_run_id=run_id,
                    scope_mode="agentic",
                    check_profile="full",
                    intent=str(request.intent or ""),
                    generation_mode=generation_mode,
                    acceptance_contract=acceptance_contract,
                    progress_callback=_check_progress_callback,
                )
            execution.completed_at = datetime.now(timezone.utc)
            self.transcript_store.append_check_snapshot(
                artifact_run_id,
                failed_count=sum(1 for item in execution.results if item.status == "failed"),
                result_names=[item.name for item in execution.results],
            )
            self._store_transcript_snapshot(job, artifact_run_id)
            self._store_agent_quality_report(workspace_id, run_id, execution)
            failed_checks = [item for item in execution.results if item.status in {"failed", "blocked"}]
            self._record_hook(
                job=job,
                hook="before_checks",
                status="completed" if not failed_checks else "failed",
                payload={
                    "changed_files": list(last_changed_files),
                    "failed_checks": [item.name for item in failed_checks],
                    "check_count": len(execution.results),
                },
            )
            if failed_checks:
                self._record_hook(
                    job=job,
                    hook="on_check_failed",
                    status="completed",
                    payload={
                        "failed_checks": [
                            {"name": item.name, "status": item.status, "details": item.details}
                            for item in failed_checks[:8]
                        ],
                        "changed_files": list(last_changed_files),
                    },
                )
            preview = self.preview_service.get(workspace_id)
            preview_details = self.check_orchestrator.preview_details(preview)
            if any(item.status == "failed" for item in execution.results) and not focused_visual_edit:
                self._collect_preview_diagnostics(workspace_id, preview_details)
            if not has_draft_diff:
                cached_no_diff_checks = (execution, preview_details)
            return execution, preview_details

        def _plan_turn(
            *,
            attempt: int,
            latest_execution: CheckExecutionRecord,
            latest_preview_details: dict[str, object],
            validation_snapshot: ValidationSnapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
            agent_memory: dict[str, Any] | None = None,
            repair_packets: list[dict[str, Any]] | None = None,
            next_forced_action: dict[str, Any] | None = None,
            diagnostics_delta: dict[str, Any] | None = None,
        ) -> AgentTurnPlan:
            del validation_snapshot
            extra_file_context: dict[str, str] = {}
            local_tool_results = list(tool_results)
            seen_tool_calls: set[str] = set()
            self_blocked_correction_sent = False
            empty_fatal_correction_sent = False
            output_cap_correction_sent = False
            context_length_correction_sent = False
            tool_budget_correction_sent = False
            invalid_mutation_correction_sent = False
            create_repair_turn = create_intent and bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
            self._add_failed_generated_test_context(
                workspace_id=workspace_id,
                run_id=run_id,
                latest_execution=latest_execution,
                extra_file_context=extra_file_context,
                tool_results=local_tool_results,
            )
            self._add_build_validator_failure_context(
                workspace_id=workspace_id,
                run_id=run_id,
                latest_execution=latest_execution,
                extra_file_context=extra_file_context,
                tool_results=local_tool_results,
            )
            self._add_static_js_failure_context(
                workspace_id=workspace_id,
                run_id=run_id,
                latest_execution=latest_execution,
                extra_file_context=extra_file_context,
                tool_results=local_tool_results,
            )
            for tool_round in range(self._tool_round_limit(generation_mode) + 4):
                llm_payload = self._request_agent_turn(
                    job=job,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request=request,
                    attempt=attempt,
                    tool_round=tool_round,
                    context_mode=context_mode,
                    repeated_no_progress=repeated_no_progress,
                    latest_execution=latest_execution,
                    latest_preview_details=latest_preview_details,
                    initial_context=initial_context,
                    extra_file_context=extra_file_context,
                    tool_results=local_tool_results,
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=latest_diff_summary,
                    agent_memory=agent_memory,
                    repair_packets=repair_packets or [],
                    next_forced_action=next_forced_action or {},
                    diagnostics_delta=diagnostics_delta or {},
                )
                if "error" in llm_payload:
                    if self._is_provider_quota_error(str(llm_payload.get("error") or "")):
                        return AgentTurnPlan(
                            outcome="fatal_invalid_response",
                            assistant_message=str(llm_payload.get("error") or ""),
                            diagnosis=str(llm_payload.get("error") or ""),
                            files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                            failure_class="provider.insufficient_quota",
                            failure_signature="provider.insufficient_quota",
                            root_cause_summary="OpenAI provider quota is exhausted for the selected code generation model.",
                        )
                    if self._is_output_cap_error(str(llm_payload.get("error") or "")):
                        if not output_cap_correction_sent:
                            correction = self._output_cap_correction_result(llm_payload, request=request)
                            local_tool_results.append(correction)
                            tool_results.append(correction)
                            output_cap_correction_sent = True
                            self._append_event(
                                job,
                                "repair_iteration",
                                "Agent exceeded the model output cap. Retrying with smaller tool calls.",
                                {"attempt": attempt, "tool_round": tool_round, "reason": "output_cap"},
                            )
                            continue
                        return AgentTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent response exceeded the model output cap.",
                            diagnosis=(
                                "The previous response was too large. Retry with one small apply_patch_to_draft "
                                "or write_file tool call for the next concrete failing slice."
                            ),
                            files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                            metadata={"error": str(llm_payload.get("error") or ""), "retry_reason": "max_output_tokens"},
                        )
                    if self._is_context_length_error(str(llm_payload.get("error") or "")):
                        if not context_length_correction_sent:
                            correction = self._context_length_correction_result(llm_payload, request=request)
                            local_tool_results = [correction]
                            tool_results.append(correction)
                            context_length_correction_sent = True
                            context_mode = "minimal"
                            extra_file_context = {}
                            self.transcript_store.clear_model_context(artifact_run_id)
                            self._append_event(
                                job,
                                "compact_boundary",
                                "Agent context exceeded the model window. Retrying with compact repair context only.",
                                {"attempt": attempt, "tool_round": tool_round, "reason": "context_length_exceeded"},
                            )
                            continue
                        return AgentTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent context exceeded the model window.",
                            diagnosis=(
                                "The previous model request exceeded the context window. Continue with one compact tool call "
                                "against the current failing packet; do not request broad file bundles."
                            ),
                            files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                            metadata={"error": str(llm_payload.get("error") or ""), "retry_reason": "context_length_exceeded"},
                        )
                    return AgentTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(llm_payload.get("error") or ""),
                        diagnosis=str(llm_payload.get("error") or ""),
                        files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                        failure_class="generation.agent_invalid_response",
                        failure_signature="generation.agent_invalid_response",
                        root_cause_summary=str(llm_payload.get("error") or ""),
                    )
                raw_tool_calls = self._agent_tool_calls(llm_payload.get("tool_calls") or [])
                mutating_file_changes, mutating_tool_trace = self._file_changes_from_mutating_tool_calls(
                    raw_tool_calls,
                    draft_source=draft_source,
                    run_id=artifact_run_id,
                )
                failed_mutating_tool_results = [
                    item
                    for item in mutating_tool_trace
                    if isinstance(item, dict) and str(item.get("status") or "") == "failed" and item.get("repair_packet")
                ]
                if failed_mutating_tool_results:
                    local_tool_results.extend(failed_mutating_tool_results)
                    tool_results.extend(failed_mutating_tool_results)
                    self.transcript_store.append_tool_results(artifact_run_id, failed_mutating_tool_results)
                    self._store_transcript_snapshot(job, artifact_run_id)
                    self._append_event(
                        job,
                        "repair_iteration",
                        "Agent exact edit tool failed; retrying with typed edit failure packet.",
                        {"attempt": attempt, "tool_round": tool_round, "repair_packets": [item.get("repair_packet") for item in failed_mutating_tool_results]},
                    )
                    continue
                if mutating_file_changes:
                    invalid_mutation = AgentEditValidator._first_invalid_file_change(mutating_file_changes)
                    if invalid_mutation and not invalid_mutation_correction_sent:
                        code, message = invalid_mutation
                        repair_packet = AgentEditValidator.repair_packet_for_issue(
                            code=code,
                            message=message,
                            file_changes=mutating_file_changes,
                            attempt=attempt,
                            evidence={"tool_round": tool_round, "tool_trace": mutating_tool_trace},
                        )
                        failed_tool_results = [
                            {
                                "tool": "mutating_tool_validation",
                                "tool_use_id": str(item.get("tool_use_id") or f"mutating_tool_validation_{index}"),
                                "status": "failed",
                                "failure_class": repair_packet.get("failure_class"),
                                "failure_signature": repair_packet.get("failure_signature"),
                                "error_code": code,
                                "message": message,
                                "file_path": str(item.get("file_path") or ""),
                                "repair_packet": repair_packet,
                                "required_next_action": (
                                    "Retry the same concrete fix with valid mutating tool calls. "
                                    "apply_patch_to_draft requires file_path + a unified diff in `diff`; "
                                    "write_file requires file_path + complete file `content`; "
                                    "merge duplicate edits for the same file into one tool call. "
                                    "Do not use pattern-only edits."
                                ),
                            }
                            for index, item in enumerate(mutating_tool_trace)
                        ] or [
                            {
                                "tool": "mutating_tool_validation",
                                "tool_use_id": "mutating_tool_validation",
                                "status": "failed",
                                "failure_class": repair_packet.get("failure_class"),
                                "failure_signature": repair_packet.get("failure_signature"),
                                "error_code": code,
                                "message": message,
                                "repair_packet": repair_packet,
                            }
                        ]
                        local_tool_results.extend(failed_tool_results)
                        tool_results.extend(failed_tool_results)
                        self.transcript_store.append_tool_results(artifact_run_id, failed_tool_results)
                        self._store_transcript_snapshot(job, artifact_run_id)
                        invalid_mutation_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent write tool call was invalid; retrying inside the same tool loop with exact write-tool contract.",
                            {"attempt": attempt, "tool_round": tool_round, "error_code": code, "message": message, "repair_packet": repair_packet},
                        )
                        continue
                    read_state_packets = self._read_state_repair_packets(
                        run_id=run_id,
                        draft_source=draft_source,
                        file_changes=mutating_file_changes,
                        attempt=attempt,
                    )
                    if read_state_packets:
                        read_state_results = [
                            {
                                "tool": "mutating_tool_read_state",
                                "tool_use_id": f"read_state_{attempt}_{index}",
                                "status": "warning",
                                "failure_class": packet.get("failure_class"),
                                "failure_signature": packet.get("failure_signature"),
                                "file_path": packet.get("file_path"),
                                "repair_packet": packet,
                                "required_next_action": "If this edit fails or repeats, read the target file before the next write.",
                            }
                            for index, packet in enumerate(read_state_packets, start=1)
                        ]
                        local_tool_results.extend(read_state_results)
                        tool_results.extend(read_state_results)
                        self.transcript_store.append_tool_results(artifact_run_id, read_state_results)
                    raw_tool_calls = [
                        item for item in raw_tool_calls if not self._is_mutating_agent_tool_call(item)
                    ]
                    local_tool_results.extend(mutating_tool_trace)
                    tool_results.extend(mutating_tool_trace)
                    self.transcript_store.append_tool_results(artifact_run_id, mutating_tool_trace)
                    self._store_transcript_snapshot(job, artifact_run_id)
                    if raw_tool_calls:
                        new_context, executed_results = self._execute_tool_calls(
                            workspace_id=workspace_id,
                            run_id=run_id,
                            draft_source=draft_source,
                            tool_calls=raw_tool_calls,
                            execute_checks=_execute_checks,
                            job=job,
                        )
                        extra_file_context.update(new_context)
                        local_tool_results.extend(executed_results)
                        tool_results.extend(executed_results)
                    return AgentTurnPlan(
                        outcome="changes_ready",
                        assistant_message=str(
                            llm_payload.get("assistant_message")
                            or llm_payload.get("diagnosis")
                            or "Agent prepared draft writes through mutating tools."
                        ),
                        diagnosis=str(llm_payload.get("diagnosis") or ""),
                        file_changes=mutating_file_changes,
                        files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                        expected_verification=str(llm_payload.get("expected_verification") or ""),
                        rationale_by_file={str(key): str(value) for key, value in dict(llm_payload.get("rationale_by_file") or {}).items()},
                        metadata={
                            "tool_results": list(local_tool_results),
                            "acceptance_checks": list(llm_payload.get("acceptance_checks") or []),
                            "tool_call_contract": "mutating tool calls are converted to internal draft changes before apply",
                            "repair_packets": read_state_packets,
                        },
                    )
                if raw_tool_calls:
                    signature = json.dumps(raw_tool_calls, ensure_ascii=True, sort_keys=True)
                    if signature in seen_tool_calls:
                        return AgentTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent repeated an already satisfied tool request.",
                            diagnosis="The requested diagnostic context is already available; the next turn must use apply_patch_to_draft or write_file.",
                            files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                            metadata={"tool_calls": raw_tool_calls},
                        )
                    seen_tool_calls.add(signature)
                    new_context, executed_results = self._execute_tool_calls(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        draft_source=draft_source,
                        tool_calls=raw_tool_calls,
                        execute_checks=_execute_checks,
                        job=job,
                    )
                    extra_file_context.update(new_context)
                    local_tool_results.extend(executed_results)
                    tool_results.extend(executed_results)
                    if tool_round < self._tool_round_limit(generation_mode):
                        continue
                    if not tool_budget_correction_sent:
                        correction = self._tool_budget_correction_result(raw_tool_calls, request=request)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        tool_budget_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent requested more diagnostic tools than the current lane allows. Retrying with write-tool-only instructions.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "tool_budget_exhausted"},
                        )
                        continue
                    return AgentTurnPlan(
                        outcome="needs_context",
                        assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent requested more tools than the turn budget allows."),
                        files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                        metadata={"tool_calls": raw_tool_calls},
                    )
                if not empty_fatal_correction_sent and self._is_empty_fatal_agent_response(llm_payload):
                    correction = self._empty_fatal_correction_result(llm_payload)
                    local_tool_results.append(correction)
                    tool_results.append(correction)
                    empty_fatal_correction_sent = True
                    self._append_event(
                        job,
                        "repair_iteration",
                        "Agent returned text without tool calls. Retrying with corrected task instructions.",
                        {"attempt": attempt, "tool_round": tool_round, "reason": "empty_tool_step"},
                    )
                    continue
                return AgentTurnPlan(
                    outcome="no_op",
                    assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                    diagnosis=str(llm_payload.get("diagnosis") or "Agent did not call a tool."),
                    files_read=list({*initial_context.keys(), *extra_file_context.keys()}),
                    metadata={"raw_response": llm_payload, "required_next_action": "use_tool_call"},
                )
            return AgentTurnPlan(
                outcome="no_op",
                assistant_message="Agent turn ended without producing edits.",
                diagnosis="Agent turn ended without producing edits.",
                files_read=list(initial_context.keys()),
            )

        def _apply_change_sync(file_changes: list[DraftAction]) -> list[DraftAction]:
            synced = self._enforce_patch_first_file_changes(
                file_changes,
                request=request,
                workspace_id=workspace_id,
                run_id=run_id,
            )
            merge_report = self.worker_runtime.merge_report(artifact_run_id, synced)
            branch_results = self.worker_runtime.record_branch_results(artifact_run_id, synced)
            job.worker_merge_ref = f"worker_merge:{workspace_id}:{artifact_run_id}"
            self._store_report(job.worker_merge_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.worker_runtime.snapshot(artifact_run_id)})
            for branch in branch_results:
                ref = f"worker_branch:{workspace_id}:{artifact_run_id}:{branch.get('worker_id')}"
                job.worker_branch_refs = list(dict.fromkeys([*job.worker_branch_refs, ref]))
                self._store_report(ref, {"workspace_id": workspace_id, "run_id": run_id, "branch": branch})
            ownership_ok = bool((merge_report.get("ownership") if isinstance(merge_report.get("ownership"), dict) else {}).get("ok"))
            if not ownership_ok:
                scratchpad.append_failed_fix(
                    "worker_merge_conflict",
                    "Worker merge guard found conflicting draft changes.",
                    merge_report,
                )
                self._append_event(
                    job,
                    "worker_failed",
                    "Worker merge guard found conflicting draft changes.",
                    {"worker_id": "merge", "merge_report": merge_report, "artifact_ref": job.worker_merge_ref},
                )
            else:
                self._append_event(
                    job,
                    "worker_completed",
                    "Worker draft changes passed ownership merge guard.",
                    {"worker_id": "merge", "merge_report": merge_report, "artifact_ref": job.worker_merge_ref},
                )
            return synced

        def _before_apply(turn: int, file_changes: list[DraftAction]) -> None:
            paths = [operation.file_path for operation in file_changes]
            self._record_hook(
                job=job,
                hook="pre_apply_patch",
                status="started",
                payload={"turn": turn, "paths": paths, "file_change_count": len(file_changes)},
            )
            self._record_hook(
                job=job,
                hook="before_apply",
                status="started",
                payload={"turn": turn, "paths": paths, "file_change_count": len(file_changes)},
            )
            self.turn_diff_tracker.capture_baseline(
                workspace_service=self.workspace_service,
                workspace_id=workspace_id,
                run_id=run_id,
                turn=turn,
                paths=paths,
            )
            self._record_rollout_trace(
                job,
                artifact_run_id=artifact_run_id,
                event_type="turn_diff_before",
                payload={
                    "turn": turn,
                    "paths": paths,
                    "file_hashes": self._file_content_hashes(workspace_id=workspace_id, run_id=run_id, paths=paths),
                },
            )
            self.transcript_store.append_file_changes(artifact_run_id, turn=turn, file_changes=file_changes)
            self._store_transcript_snapshot(job, artifact_run_id)
            self.rollout_trace.append(artifact_run_id, "patch_apply_started", {"turn": turn, "paths": paths})

        def _after_apply(turn: int, file_changes: list[DraftAction], apply_result: Any, paths: list[str]) -> None:
            self.file_state_cache.invalidate(run_id, paths)
            record = self.turn_diff_tracker.record_result(
                workspace_service=self.workspace_service,
                workspace_id=workspace_id,
                run_id=run_id,
                turn=turn,
                paths=paths,
                apply_result=apply_result,
                owner_for_path=self._worker_owner_for_path,
            )
            job.turn_diff_ref = f"turn_diff:{workspace_id}:{artifact_run_id}"
            self._store_report(
                job.turn_diff_ref,
                {"workspace_id": workspace_id, "run_id": run_id, **self.turn_diff_tracker.snapshot(run_id)},
            )
            scratchpad.append_worker_note(
                "draft_apply",
                f"Applied turn {turn} draft changes.",
                {"paths": paths, "turn_diff_ref": job.turn_diff_ref, "changed_summary": record.summary()},
            )
            coordinator.complete_phase("editing", f"Applied turn {turn} draft changes.")
            scratchpad.set_next_action(
                action="run static, API, generated, browser, and mobile proof",
                reason="patch_applied",
                payload={"turn": turn, "paths": paths, "turn_diff_ref": job.turn_diff_ref},
            )
            self.rollout_trace.append(artifact_run_id, "patch_apply_completed", record.summary())
            self._record_rollout_trace(
                job,
                artifact_run_id=artifact_run_id,
                event_type="turn_diff_after",
                payload={
                    "turn": turn,
                    "paths": paths,
                    "apply_status": getattr(apply_result, "status", None),
                    "turn_diff_ref": job.turn_diff_ref,
                    "diff_summary": record.summary(),
                    "file_hashes": self._file_content_hashes(workspace_id=workspace_id, run_id=run_id, paths=paths),
                },
            )
            self._record_hook(
                job=job,
                hook="post_apply_patch",
                status="completed" if getattr(apply_result, "status", "") == "applied" else "failed",
                payload={"turn": turn, "paths": paths, "apply_status": getattr(apply_result, "status", None), "turn_diff_ref": job.turn_diff_ref},
            )
            self._record_hook(
                job=job,
                hook="after_apply",
                status="completed" if getattr(apply_result, "status", "") == "applied" else "failed",
                payload={"turn": turn, "paths": paths, "apply_status": getattr(apply_result, "status", None), "turn_diff_ref": job.turn_diff_ref},
            )
            job.semantic_graph_ref = f"semantic_graph:{workspace_id}:{artifact_run_id}"
            self._store_report(
                job.semantic_graph_ref,
                {"workspace_id": workspace_id, "run_id": run_id, "graph": semantic_scan(root=draft_source, targets=["miniapp/app", "miniapp/tests"])},
            )

        def _post_apply_stabilize(
            stabilize_workspace_id: str,
            stabilize_run_id: str,
            apply_result: Any,
            paths: list[str],
        ) -> list[str]:
            del apply_result
            changed = self._stabilize_platform_shell(stabilize_workspace_id, stabilize_run_id, draft_source, paths)
            self.file_state_cache.invalidate(stabilize_run_id, changed)
            return changed

        def _verify_completion(
            latest_execution: CheckExecutionRecord | None,
            latest_preview_details: dict[str, Any],
        ) -> dict[str, Any]:
            self._append_event(
                job,
                "worker_started",
                "Fresh verifier worker started after validation checks.",
                {"worker_id": "mobile_polish_worker", "phase": "browser_verifying"},
            )
            report = VerificationWorker.verify(
                latest_execution=latest_execution,
                preview_details=latest_preview_details,
                acceptance_contract=acceptance_contract,
                require_browser_proof=bool((create_intent or acceptance_contract.get("required")) and not focused_visual_edit),
            ).model_dump()
            browser_replay_packet = self.browser_replay.failed_step_packet(latest_execution)
            if browser_replay_packet:
                browser_replay_ref = f"browser_replay:{workspace_id}:{artifact_run_id}:latest"
                self._store_report(
                    browser_replay_ref,
                    {"workspace_id": workspace_id, "run_id": run_id, "packet": browser_replay_packet},
                )
                job.browser_step_refs = list(dict.fromkeys([*job.browser_step_refs, browser_replay_ref]))
                report["browser_replay_ref"] = browser_replay_ref
                report["browser_replay_packet"] = browser_replay_packet
                scratchpad.set_next_action(
                    action="repair failed browser step, rerun that step, then rerun full proof",
                    reason="browser_proof_failed",
                    payload={"browser_replay_ref": browser_replay_ref, "packet": browser_replay_packet},
                )
                job.repair_issue_signatures.append(
                    {
                        "check": "browser_flow_smoke",
                        "signature": str(browser_replay_packet.get("failed_step") or browser_replay_packet.get("failed_selector") or "browser_step_failed"),
                        "repair_packet_ref": browser_replay_ref,
                    }
                )
            report["worker_id"] = "mobile_polish_worker"
            report["fresh_context"] = True
            self.verification_reports[artifact_run_id] = report
            coordinator.complete_phase("checking", "Validation checks reached strict green before verifier.")
            if report.get("status") == "passed":
                coordinator.complete_phase("browser_verifying", "Browser and mobile proof passed.")
            else:
                coordinator.start_phase("repairing", "Verifier found unresolved proof issues.")
                self._append_activity(
                    job,
                    "verifier_nudge",
                    "Verifier requires targeted repair before completion",
                    {
                        "phase": "repairing",
                        "status": "failed",
                        "issue_count": len(report.get("issues") or []),
                    },
                    save=False,
                )
            if report.get("status") == "passed" and not coordinator.ready_to_finalize():
                missing = coordinator.incomplete_required_todos()
                report = {
                    **report,
                    "status": "failed",
                    "summary": "Coordinator todo gate is incomplete.",
                    "issues": [
                        *list(report.get("issues") or []),
                        {
                            "kind": "todo_plan_incomplete",
                            "details": "A create/workflow run cannot complete until planning, reading, editing, checking, and browser proof todo items are completed.",
                            "missing": missing,
                        },
                    ],
                }
                coordinator.start_phase("repairing", "Coordinator todo gate requires targeted repair before completion.")
            elif report.get("status") == "passed":
                coordinator.complete_phase("completed", "Run passed strict checks, browser proof, and verifier gate.")
            job.verification_report_ref = f"verification_report:{workspace_id}:{artifact_run_id}"
            job.verifier_review_ref = f"verifier_review:{workspace_id}:{artifact_run_id}"
            self._store_report(
                job.verification_report_ref,
                {"workspace_id": workspace_id, "run_id": run_id, "report": report},
            )
            self._store_report(
                job.verifier_review_ref,
                {"workspace_id": workspace_id, "run_id": run_id, "review": report},
            )
            browser_step_ref = f"browser_steps:{workspace_id}:{artifact_run_id}:mobile_polish_worker"
            job.browser_step_refs = list(dict.fromkeys([*job.browser_step_refs, browser_step_ref]))
            self._store_report(
                browser_step_ref,
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "worker_id": "mobile_polish_worker",
                    "status": report.get("status"),
                    "issues": report.get("issues", []),
                    "proof": report,
                },
            )
            self.transcript_store.append_browser_proof(artifact_run_id, report)
            self._store_transcript_snapshot(job, artifact_run_id)
            self._store_report(job.scratchpad_ref, {"workspace_id": workspace_id, "run_id": run_id, **scratchpad.snapshot(), **coordinator.snapshot()})
            self.rollout_trace.append(artifact_run_id, "verification_worker", report)
            self._record_hook(
                job=job,
                hook="post_browser_verify",
                status="completed" if report.get("status") == "passed" else "failed",
                payload={"worker_id": "mobile_polish_worker", "verification_report_ref": job.verification_report_ref},
            )
            self._append_event(
                job,
                "worker_completed" if report.get("status") == "passed" else "worker_failed",
                "Fresh verifier worker completed.",
                {"worker_id": "mobile_polish_worker", "status": report.get("status"), "artifact_ref": job.verification_report_ref},
            )
            return report

        def _record_compact_boundary(payload: dict[str, Any]) -> None:
            failed_signatures = [
                str(item)
                for item in (payload.get("memory") or {}).get("failed_signatures", [])
                if str(item).strip()
            ] if isinstance(payload.get("memory"), dict) else []
            boundary = scratchpad.record_compact_boundary(
                plan=implementation_plan,
                diff_summary=str(payload.get("latest_diff_summary") or ""),
                failed_signatures=failed_signatures,
                next_action="repair the smallest failing workflow slice and rerun exact proof",
            )
            self.transcript_store.clear_model_context(artifact_run_id)
            signature = str(payload.get("latest_failure_signature") or "").strip()
            if signature:
                self.memory_store.record_failure(
                    artifact_run_id,
                    signature,
                    "Latest repair boundary recorded for the next agent turn.",
                    {"turn": payload.get("turn"), "boundary": boundary},
                )
            stale_checks = self.memory_store.verify_stale_claims(artifact_run_id, draft_source)
            job.agent_memory = {
                **dict(payload.get("memory") or {}),
                "store_ref": f"agent_memory_store:{workspace_id}:{artifact_run_id}",
                "stale_checks": stale_checks[-12:],
            }
            job.scratchpad_ref = f"scratchpad:{workspace_id}:{artifact_run_id}"
            job.memory_ref = f"agent_memory_store:{workspace_id}:{artifact_run_id}"
            self._store_report(job.scratchpad_ref, {"workspace_id": workspace_id, "run_id": run_id, **scratchpad.snapshot()})
            self._store_report(job.memory_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.memory_store.snapshot(artifact_run_id)})
            self._append_activity(
                job,
                "compact_boundary",
                "Stored compact repair boundary",
                {
                    "phase": "repairing",
                    "status": "completed",
                    "artifact_ref": job.scratchpad_ref,
                    "summary": "Plan, diff summary, failure signatures, and next action were compacted.",
                },
                save=False,
            )

        def _completion_state_with_trace(results: list[RunCheckResult], preview_details: dict[str, Any], validation_snapshot: ValidationSnapshot | None = None) -> dict[str, object]:
            state = self._completion_state(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                results=results,
                preview_details=preview_details,
                validation_snapshot=validation_snapshot,
                acceptance_contract=acceptance_contract,
            )
            self._record_rollout_trace(
                job,
                artifact_run_id=artifact_run_id,
                event_type="final_acceptance_gate_decision",
                payload={
                    "status": state.get("status") or ("blocked" if state.get("blocking") else "passed"),
                    "blocking": state.get("blocking"),
                    "reasons": state.get("reasons") or state.get("issues") or [],
                    "result_names": [result.name for result in results],
                    "failed_checks": [result.name for result in results if result.status == "failed"],
                    "acceptance_required": bool((acceptance_contract or {}).get("required") or request.mode == "generate"),
                    "validation_snapshot": validation_snapshot.model_dump(mode="json") if validation_snapshot is not None else None,
                },
            )
            self._record_skill_usage_telemetry(
                job=job,
                artifact_run_id=artifact_run_id,
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                results=results,
                run_status="blocked" if state.get("blocking") else "completed",
            )
            return state

        callbacks = AgentLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self._validation_snapshot_from_execution,
            completion_state=_completion_state_with_trace,
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_change_sync=_apply_change_sync,
            verify_completion=_verify_completion,
            before_apply=_before_apply,
            after_apply=_after_apply,
            post_apply_stabilize=_post_apply_stabilize,
            append_event=self._append_event,
            append_activity=self._append_activity,
            append_trace=self._append_trace,
            store_report=self._store_report,
            record_compact_boundary=_record_compact_boundary,
            allow_optimistic_completion=False,
            skip_initial_checks=focused_visual_edit,
            stop_if_requested=should_stop,
            budget_status=lambda attempt: completion_budget_status(
                job=job,
                mode=generation_mode,
                started_at=loop_started_at,
                attempt=attempt,
            ),
        )
        return self.agent_tool_call_loop.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            initial_file_changes=branch_initial_file_changes,
            initial_assistant_message=branch_initial_message,
            initial_files_read=list(initial_context.keys()),
            initial_changed_files=last_changed_files,
            callbacks=callbacks,
        )

    def _prepare_product_worker_artifacts(
        self,
        *,
        workspace_id: str,
        run_id: str,
        artifact_run_id: str,
        job: JobRecord,
        generation_mode: GenerationMode,
        worker_tasks: list[dict[str, Any]],
        implementation_plan: dict[str, Any],
        acceptance_contract: dict[str, Any],
        semantic_graph: dict[str, Any],
        worker_prefix: dict[str, Any],
        draft_source: Path,
        enabled: bool,
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        workspace_memory = self.store.get("reports", f"workspace_memory:{workspace_id}") or {"items": []}
        run_memory = self.store.get("reports", f"memory_stage1:{workspace_id}:{run_id}") or {"items": []}
        latest_repair_packet = (job.repair_iterations[-1].model_dump(mode="json") if job.repair_iterations else {})
        for task in worker_tasks:
            if not isinstance(task, dict):
                continue
            worker_id = canonical_worker_id(str(task.get("worker_id") or ""))
            if not worker_id:
                continue
            refs = worker_refs(workspace_id, artifact_run_id, worker_id)
            ownership = dict(task.get("ownership") or ownership_for_worker(worker_id))
            selected_memory, rejected_memory = select_memory_items(
                {"items": [*(workspace_memory.get("items") or []), *(run_memory.get("items") or [])]},
                worker_id=worker_id,
            )
            memory_snapshot = {
                "schema": "grounded.worker_memory_snapshot.v1",
                "workspace_id": workspace_id,
                "run_id": run_id,
                "artifact_run_id": artifact_run_id,
                "worker_id": worker_id,
                "worker_type": worker_id,
                "items": selected_memory,
                "rejected": rejected_memory,
                "source_refs": {
                    "workspace_memory": f"workspace_memory:{workspace_id}",
                    "run_memory": f"memory_stage1:{workspace_id}:{run_id}",
                },
                "stale_check": stale_path_checks(draft_source, {"items": selected_memory}),
                "created_at": utc_now().isoformat(),
            }
            context_payload = {
                "schema": "grounded.worker_context.v1",
                "workspace_id": workspace_id,
                "run_id": run_id,
                "artifact_run_id": artifact_run_id,
                "worker_id": worker_id,
                "worker_type": worker_id,
                "generation_mode": str(generation_mode.value),
                "ownership": ownership,
                "contract_slice": self._worker_contract_slice(
                    worker_id=worker_id,
                    implementation_plan=implementation_plan,
                    acceptance_contract=acceptance_contract,
                ),
                "shared_state_contract": (implementation_plan.get("role_state_contract") or implementation_plan.get("prompt_hints", {}).get("role_state_contract") or {}),
                "semantic_slice": self._worker_semantic_slice(worker_id=worker_id, semantic_graph=semantic_graph),
                "memory_snapshot_ref": refs["memory_snapshot_ref"],
                "latest_repair_packet": latest_repair_packet,
                "backend_contract": worker_prefix.get("backend_contract") or {},
                "created_at": utc_now().isoformat(),
            }
            output_payload = {
                "schema": "grounded.worker_output.v1",
                "workspace_id": workspace_id,
                "run_id": run_id,
                "artifact_run_id": artifact_run_id,
                "worker_id": worker_id,
                "worker_type": worker_id,
                "status": "context_ready" if enabled else "available_disabled",
                "badge": "context_ready" if enabled else "available_disabled",
                "ownership": ownership,
                "changed_files": [],
                "diff_ref": None,
                "proof_refs": [],
                "issues": [],
                "self_check": {"status": "pending" if enabled else "skipped"},
                "token_usage": {},
                "summary": "Worker context prepared." if enabled else "Worker available but disabled by gate.",
                "merge_recommendation": "pending" if enabled else "deferred",
                "context_ref": refs["context_ref"],
                "memory_snapshot_ref": refs["memory_snapshot_ref"],
                "created_at": utc_now().isoformat(),
            }
            task_id = ""
            if enabled:
                task_id = self._record_worker_background_task(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    worker_id=worker_id,
                    context_ref=refs["context_ref"],
                    output_ref=refs["output_ref"],
                )
                output_payload["task_id"] = task_id
            self._store_report(refs["memory_snapshot_ref"], memory_snapshot)
            self._store_report(refs["context_ref"], context_payload)
            self._store_report(refs["output_ref"], output_payload)
            enriched_task = dict(task)
            enriched_task.update(
                {
                    "worker_id": worker_id,
                    "worker_type": worker_id,
                    "ownership": ownership,
                    "context_ref": refs["context_ref"],
                    "memory_snapshot_ref": refs["memory_snapshot_ref"],
                    "output_ref": refs["output_ref"],
                    "merge_decision_ref": refs["merge_decision_ref"],
                    "task_id": task_id or None,
                }
            )
            enriched.append(enriched_task)
        return enriched

    def _record_worker_background_task(
        self,
        *,
        workspace_id: str,
        run_id: str,
        worker_id: str,
        context_ref: str,
        output_ref: str,
    ) -> str:
        task = BackgroundTaskRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            type="worker_branch",
            title=f"{worker_id} branch",
            owner=worker_id,
            input={"worker_id": worker_id, "context_ref": context_ref, "output_ref": output_ref},
            linked_refs={"context_ref": context_ref, "output_ref": output_ref},
        )
        task.status = "running"
        task.started_at = utc_now()
        task.updated_at = utc_now()
        self.store.upsert("background_tasks", task.task_id, task.model_dump(mode="json"))
        self._append_worker_task_output(task.task_id, "task_created", "Worker branch task created.", {"worker_id": worker_id})
        self._append_worker_task_output(task.task_id, "context_ready", "Worker isolated context is ready.", {"context_ref": context_ref, "output_ref": output_ref})
        return task.task_id

    def _append_worker_task_output(self, task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        key = f"background_task_output:{task_id}"
        output = self.store.get("reports", key) or {"schema": "grounded.background_task_output.v1", "task_id": task_id, "items": []}
        items = [item for item in output.get("items") or [] if isinstance(item, dict)]
        items.append(
            {
                "sequence": len(items) + 1,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "created_at": utc_now().isoformat(),
            }
        )
        output["items"] = items[-1000:]
        output["updated_at"] = utc_now().isoformat()
        self.store.upsert("reports", key, output)

    @staticmethod
    def _worker_contract_slice(
        *,
        worker_id: str,
        implementation_plan: dict[str, Any],
        acceptance_contract: dict[str, Any],
    ) -> dict[str, Any]:
        role = ownership_for_worker(worker_id).get("role")
        flows = [
            flow
            for flow in (acceptance_contract.get("flows") or [])
            if isinstance(flow, dict) and (not role or role in [str(item) for item in flow.get("roles") or []])
        ]
        return {
            "primary_entities": list(implementation_plan.get("primary_entities") or [])[:8],
            "role": role,
            "flows": flows[:6],
            "role_actions": (acceptance_contract.get("role_actions") or {}).get(role, []) if role else acceptance_contract.get("role_actions") or {},
            "browser_steps": acceptance_contract.get("browser_steps") or [],
            "api_smoke": acceptance_contract.get("api_smoke") or [],
            "persisted_markers": acceptance_contract.get("persisted_markers") or [],
        }

    @staticmethod
    def _worker_semantic_slice(*, worker_id: str, semantic_graph: dict[str, Any]) -> dict[str, Any]:
        ownership = ownership_for_worker(worker_id)
        allowed = [str(item) for item in ownership.get("allowed_paths") or []]
        graph = semantic_graph.get("graph") if isinstance(semantic_graph, dict) else semantic_graph
        files = []
        if isinstance(graph, dict):
            for item in graph.get("files") or graph.get("nodes") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or item.get("file") or "")
                if any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed):
                    files.append(item)
        return {"allowed_prefixes": allowed, "files": files[:80]}

    def _update_worker_output_artifact(
        self,
        *,
        workspace_id: str,
        run_id: str,
        artifact_run_id: str,
        worker_id: str,
        result: WorkerBranchResult,
        branch_ref: str,
    ) -> None:
        canonical = canonical_worker_id(worker_id)
        refs = worker_refs(workspace_id, artifact_run_id, canonical)
        current = self.store.get("reports", refs["output_ref"]) or {}
        status = "changes_ready" if result.status == "changes_ready" else "failed" if result.status == "failed" else "completed"
        payload = {
            **(current if isinstance(current, dict) else {}),
            "schema": "grounded.worker_output.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "artifact_run_id": artifact_run_id,
            "worker_id": canonical,
            "worker_type": canonical,
            "status": status,
            "badge": status,
            "ownership": ownership_for_worker(canonical),
            "changed_files": list(result.changed_files),
            "diff_ref": branch_ref,
            "proof_refs": list((current or {}).get("proof_refs") or []),
            "issues": ([{"message": result.error, "severity": "high"}] if result.error else []),
            "self_check": {
                "status": "failed" if result.status == "failed" else "ready_for_merge",
                "owned_paths_only": all(path_is_allowed(canonical, path) for path in result.changed_files),
                "file_count": len(result.changed_files),
            },
            "token_usage": dict(result.token_usage),
            "summary": result.assistant_message[:1200] or (
                f"{canonical} produced {len(result.changed_files)} changed file(s)."
                if result.changed_files
                else f"{canonical} finished without branch changes."
            ),
            "merge_recommendation": "needs_repair" if result.status == "failed" else "accept" if result.status == "changes_ready" else "deferred",
            "branch_ref": branch_ref,
            "updated_at": utc_now().isoformat(),
        }
        self._store_report(refs["output_ref"], payload)
        task_id = str(payload.get("task_id") or "")
        if task_id:
            self._append_worker_task_output(task_id, status, payload["summary"], {"worker_id": canonical, "output_ref": refs["output_ref"], "branch_ref": branch_ref})
            task_payload = self.store.get("background_tasks", task_id)
            if isinstance(task_payload, dict):
                task_payload["status"] = "failed" if result.status == "failed" else "completed"
                task_payload["output_summary"] = payload["summary"]
                task_payload["completed_at"] = utc_now().isoformat()
                task_payload["updated_at"] = utc_now().isoformat()
                task_payload["linked_refs"] = {**(task_payload.get("linked_refs") or {}), "branch_ref": branch_ref, "output_ref": refs["output_ref"]}
                self.store.upsert("background_tasks", task_id, task_payload)

    def _store_worker_merge_decision(
        self,
        *,
        workspace_id: str,
        run_id: str,
        artifact_run_id: str,
        merge_report: dict[str, Any],
        results: list[WorkerBranchResult],
        status: str,
        apply_result: dict[str, Any] | None = None,
    ) -> None:
        decisions: list[dict[str, Any]] = []
        ownership = merge_report.get("ownership") if isinstance(merge_report.get("ownership"), dict) else {}
        conflict_paths = {
            str(item.get("path") or "")
            for item in ownership.get("conflicts", [])
            if isinstance(item, dict)
        }
        forbidden_paths = {
            str(item.get("path") or "")
            for item in ownership.get("forbidden", [])
            if isinstance(item, dict)
        }
        for result in results:
            worker_id = canonical_worker_id(result.worker_id)
            changed = list(result.changed_files)
            blocked_paths = sorted({path for path in changed if path in conflict_paths or path in forbidden_paths})
            if result.status == "failed":
                decision = "needs_repair"
            elif blocked_paths:
                decision = "rejected"
            elif status in {"merged", "partial"} and result.status == "changes_ready":
                decision = "accepted"
            elif result.status == "changes_ready":
                decision = "deferred"
            else:
                decision = "deferred"
            decisions.append(
                {
                    "worker_id": worker_id,
                    "worker_type": worker_id,
                    "decision": decision,
                    "changed_files": changed,
                    "blocked_paths": blocked_paths,
                    "output_ref": worker_refs(workspace_id, artifact_run_id, worker_id)["output_ref"],
                    "repair_worker_required": decision in {"needs_repair", "rejected"},
                }
            )
        payload = {
            "schema": "grounded.worker_manager_merge_decision.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "artifact_run_id": artifact_run_id,
            "status": status,
            "decisions": decisions,
            "merge_report": merge_report,
            "apply_result": apply_result or {},
            "repair_worker": self._repair_worker_packet_from_decisions(run_id=run_id, decisions=decisions),
            "created_at": utc_now().isoformat(),
        }
        ref = f"worker_manager_merge_decision:{workspace_id}:{artifact_run_id}"
        self._store_report(ref, payload)

    @staticmethod
    def _repair_worker_packet_from_decisions(*, run_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        repair_items = [item for item in decisions if item.get("repair_worker_required")]
        if not repair_items:
            return {}
        return {
            "worker_id": "repair_worker",
            "worker_type": "repair_worker",
            "run_id": run_id,
            "owned_failures": repair_items,
            "status": "planned",
            "next_action": "Start repair_worker with the blocked owned paths and latest proof failure.",
        }

    def _run_parallel_worker_branches(
        self,
        *,
        workspace_id: str,
        run_id: str,
        artifact_run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        generation_mode: GenerationMode,
        worker_tasks: list[dict[str, Any]],
        worker_drafts: dict[str, Any],
        worker_prefix: dict[str, Any],
    ) -> tuple[list[DraftAction], list[str], str]:
        workers_by_id = {
            str(worker.get("worker_id") or ""): worker
            for worker in worker_drafts.get("workers") or []
            if isinstance(worker, dict)
        }
        tasks = [
            task
            for task in worker_tasks
            if str(task.get("worker_id") or "").strip() in workers_by_id
        ]
        if not tasks:
            return [], [], "Workspace code agent initialized."
        backend_tasks = [task for task in tasks if canonical_worker_id(str(task.get("worker_id") or "").strip()) == "backend_api_worker"]
        remaining_tasks = [task for task in tasks if canonical_worker_id(str(task.get("worker_id") or "").strip()) != "backend_api_worker"]
        if backend_tasks and remaining_tasks:
            backend_changes, backend_changed_files, backend_message = self._run_parallel_worker_branches(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                request=request,
                job=job,
                generation_mode=generation_mode,
                worker_tasks=backend_tasks,
                worker_drafts=worker_drafts,
                worker_prefix=worker_prefix,
            )
            updated_worker_prefix = dict(worker_prefix)
            updated_worker_prefix["backend_contract"] = self._backend_contract_snapshot(
                self.workspace_service.draft_source_dir(workspace_id, run_id)
            )
            refreshed_workers: list[dict[str, Any]] = []
            for task in remaining_tasks:
                worker_id = str(task.get("worker_id") or "").strip()
                existing = dict(workers_by_id.get(worker_id) or {})
                branch_run_id = str(existing.get("branch_run_id") or f"{artifact_run_id}__worker__{worker_id}")
                try:
                    source_dir = self.workspace_service.clone_draft(workspace_id, run_id, branch_run_id)
                    existing["source_dir"] = str(source_dir)
                except Exception as exc:
                    self._append_event(
                        job,
                        "worker_failed",
                        f"Worker {worker_id} could not refresh its branch after backend/API merge.",
                        {"worker_id": worker_id, "branch_run_id": branch_run_id, "error": str(exc)},
                    )
                existing.setdefault("worker_id", worker_id)
                existing.setdefault("branch_run_id", branch_run_id)
                existing.setdefault("owner_scope", str(task.get("owner_scope") or worker_id))
                refreshed_workers.append(existing)
            remaining_changes, remaining_changed_files, remaining_message = self._run_parallel_worker_branches(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                request=request,
                job=job,
                generation_mode=generation_mode,
                worker_tasks=remaining_tasks,
                worker_drafts={"enabled": True, "workers": refreshed_workers},
                worker_prefix=updated_worker_prefix,
            )
            combined_changes = [*backend_changes, *remaining_changes]
            combined_changed_files = list(dict.fromkeys([*backend_changed_files, *remaining_changed_files]))
            message = (
                "Backend/API worker established the contract first; role/test workers forked from the updated draft. "
                f"{backend_message} {remaining_message}"
            ).strip()
            return combined_changes, combined_changed_files, message
        self._append_activity(
            job,
            "worker_started",
            "Starting isolated worker branches",
            {"worker_count": len(tasks), "status": "started"},
            save=False,
        )
        for task in tasks:
            worker_id = str(task.get("worker_id") or "").strip()
            self._append_event(
                job,
                "worker_started",
                f"Worker {worker_id} started an isolated tool loop.",
                {
                    "worker_id": worker_id,
                    "branch_run_id": workers_by_id.get(worker_id, {}).get("branch_run_id"),
                    "transcript_ref": workers_by_id.get(worker_id, {}).get("transcript_ref"),
                },
            )
        results: list[WorkerBranchResult] = []
        result_queue: queue.Queue[WorkerBranchResult] = queue.Queue()
        timeout_seconds = self._worker_branch_timeout_seconds(generation_mode)
        started_at = time.monotonic()
        threads: dict[str, threading.Thread] = {}

        def run_worker(worker_id: str, task: dict[str, Any], worker: dict[str, Any]) -> None:
            try:
                result = self.worker_branch_loop.run(
                    workspace_id=workspace_id,
                    parent_run_id=artifact_run_id,
                    branch_run_id=str(worker.get("branch_run_id") or ""),
                    branch_source=Path(str(worker.get("source_dir") or "")),
                    generation_mode=generation_mode,
                    model_profile=request.model_profile,
                    user_prompt=request.prompt,
                    worker_task=task,
                    worker_prefix=worker_prefix,
                    max_steps=6 if generation_mode == GenerationMode.FAST else 6 if generation_mode == GenerationMode.BALANCED else 8,
                )
            except Exception as exc:
                result = WorkerBranchResult(
                    worker_id=worker_id,
                    owner_scope=str(workers_by_id.get(worker_id, {}).get("owner_scope") or worker_id),
                    branch_run_id=str(workers_by_id.get(worker_id, {}).get("branch_run_id") or ""),
                    source_dir=str(workers_by_id.get(worker_id, {}).get("source_dir") or ""),
                    status="failed",
                    error=str(exc),
                )
            result_queue.put(result)

        def record_result(result: WorkerBranchResult) -> None:
            results.append(result)
            worker_id = canonical_worker_id(result.worker_id)
            ref = f"worker_branch:{workspace_id}:{artifact_run_id}:{worker_id}"
            job.worker_branch_refs = list(dict.fromkeys([*job.worker_branch_refs, ref]))
            self._store_report(ref, {"workspace_id": workspace_id, "run_id": run_id, "branch": result.as_dict()})
            self._update_worker_output_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                worker_id=worker_id,
                result=result,
                branch_ref=ref,
            )
            if result.token_usage:
                job.token_usage = self._merge_run_token_usage(
                    job.token_usage if isinstance(job.token_usage, dict) else {},
                    result.token_usage,
                )
                job.cache_stats = self._merge_cache_stats(job.cache_stats, result.token_usage)
            event_type = "worker_completed" if result.status == "changes_ready" else "worker_failed" if result.status == "failed" else "worker_completed"
            message = (
                f"Worker {worker_id} produced branch changes."
                if result.status == "changes_ready"
                else f"Worker {worker_id} finished without branch changes."
                if result.status == "no_changes"
                else f"Worker {worker_id} failed its branch loop."
            )
            self._append_event(
                job,
                event_type,
                message,
                {
                    "worker_id": worker_id,
                    "branch_run_id": result.branch_run_id,
                    "status": result.status,
                    "changed_files": list(result.changed_files),
                    "file_change_count": len(result.file_changes),
                    "token_usage": dict(result.token_usage),
                    "model": result.model,
                    "error": result.error,
                    "artifact_ref": ref,
                },
            )

        for task in tasks:
            worker_id = str(task.get("worker_id") or "").strip()
            worker = workers_by_id[worker_id]
            thread = threading.Thread(
                target=run_worker,
                args=(worker_id, task, worker),
                name=f"agent-worker-{worker_id}",
                daemon=True,
            )
            threads[worker_id] = thread
            thread.start()

        pending = set(threads)
        while pending:
            remaining = max(0.0, timeout_seconds - (time.monotonic() - started_at))
            if remaining <= 0:
                break
            try:
                result = result_queue.get(timeout=min(5.0, remaining))
            except queue.Empty:
                self._append_activity(
                    job,
                    "worker_started",
                    "Waiting for isolated worker branches",
                    {
                        "worker_count": len(tasks),
                        "pending_workers": sorted(pending),
                        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                        "status": "running",
                    },
                    save=False,
                )
                continue
            pending.discard(result.worker_id)
            record_result(result)

        for worker_id in sorted(pending):
            worker = workers_by_id.get(worker_id, {})
            result = WorkerBranchResult(
                worker_id=canonical_worker_id(worker_id),
                owner_scope=str(worker.get("owner_scope") or worker_id),
                branch_run_id=str(worker.get("branch_run_id") or ""),
                source_dir=str(worker.get("source_dir") or ""),
                status="failed",
                error=f"Worker branch timed out after {timeout_seconds} seconds; coordinator will continue with available diffs and repair the missing slice.",
            )
            record_result(result)
        file_changes = [change for result in results if result.status == "changes_ready" for change in result.file_changes]
        if not file_changes:
            self._store_worker_merge_decision(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                merge_report={"run_id": artifact_run_id, "status": "no_changes", "ownership": {"ok": True, "conflicts": [], "forbidden": []}},
                results=results,
                status="deferred",
            )
            self._store_report(
                job.worker_merge_ref or f"worker_merge:{workspace_id}:{artifact_run_id}",
                {"workspace_id": workspace_id, "run_id": run_id, **self.worker_runtime.snapshot(artifact_run_id)},
            )
            return [], [], "Worker branches completed without mergeable changes; coordinator will continue."
        merge_report = self.worker_runtime.merge_report(artifact_run_id, file_changes)
        job.worker_merge_ref = job.worker_merge_ref or f"worker_merge:{workspace_id}:{artifact_run_id}"
        self._store_worker_merge_decision(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_run_id=artifact_run_id,
            merge_report=merge_report,
            results=results,
            status="conflict" if not bool((merge_report.get("ownership") if isinstance(merge_report.get("ownership"), dict) else {}).get("ok")) else "ready",
        )
        if not bool((merge_report.get("ownership") if isinstance(merge_report.get("ownership"), dict) else {}).get("ok")):
            conflict_paths = {
                str(conflict.get("path") or "")
                for conflict in (merge_report.get("ownership") if isinstance(merge_report.get("ownership"), dict) else {}).get("conflicts", [])
                if isinstance(conflict, dict) and str(conflict.get("path") or "").strip()
            }
            mergeable_changes = [
                change
                for change in file_changes
                if str(change.file_path or "").strip() not in conflict_paths
            ]
            if mergeable_changes:
                mergeable_report = self.worker_runtime.merge_report(artifact_run_id, mergeable_changes)
                envelope = self.workspace_service.build_patch_envelope_for_file_changes(workspace_id, run_id, mergeable_changes)
                apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
                merge_payload = {
                    "merge_report": merge_report,
                    "mergeable_report": mergeable_report,
                    "partial_apply_result": apply_result.model_dump(mode="json"),
                    "conflict_paths": sorted(conflict_paths),
                    "branch_results": [result.as_dict() for result in results],
                }
                self._store_report(job.worker_merge_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.worker_runtime.snapshot(artifact_run_id), **merge_payload})
                if apply_result.status == "applied":
                    self._store_worker_merge_decision(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        artifact_run_id=artifact_run_id,
                        merge_report=merge_report,
                        results=results,
                        status="partial",
                        apply_result=apply_result.model_dump(mode="json"),
                    )
                    branch_results = self.worker_runtime.record_branch_results(artifact_run_id, mergeable_changes)
                    for branch in branch_results:
                        ref = f"worker_branch_merge:{workspace_id}:{artifact_run_id}:{branch.get('worker_id')}"
                        job.worker_branch_refs = list(dict.fromkeys([*job.worker_branch_refs, ref]))
                        self._store_report(ref, {"workspace_id": workspace_id, "run_id": run_id, "branch": branch})
                    changed_files = list(dict.fromkeys([change.file_path for change in mergeable_changes if change.file_path]))
                    self._append_event(
                        job,
                        "worker_failed",
                        "Worker branch merge had conflicts; applied non-conflicting worker diffs and queued conflicting files for coordinator repair.",
                        {
                            "worker_id": "merge",
                            "changed_files": changed_files,
                            "conflict_paths": sorted(conflict_paths),
                            "artifact_ref": job.worker_merge_ref,
                        },
                    )
                    return (
                        mergeable_changes,
                        changed_files,
                        "Applied non-conflicting worker branch diffs; coordinator must repair conflicting files.",
                    )
            self._store_report(job.worker_merge_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.worker_runtime.snapshot(artifact_run_id)})
            self._append_event(
                job,
                "worker_failed",
                "Worker branch merge found conflicting owned changes.",
                {"worker_id": "merge", "merge_report": merge_report, "artifact_ref": job.worker_merge_ref},
            )
            return [], [], "Worker branches need coordinator repair after merge conflict."
        envelope = self.workspace_service.build_patch_envelope_for_file_changes(workspace_id, run_id, file_changes)
        apply_result = self.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
        merge_payload = {
            "merge_report": merge_report,
            "apply_result": apply_result.model_dump(mode="json"),
            "branch_results": [result.as_dict() for result in results],
        }
        self._store_report(job.worker_merge_ref, {"workspace_id": workspace_id, "run_id": run_id, **self.worker_runtime.snapshot(artifact_run_id), **merge_payload})
        if apply_result.status != "applied":
            self._store_worker_merge_decision(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                merge_report=merge_report,
                results=results,
                status="rejected",
                apply_result=apply_result.model_dump(mode="json"),
            )
            self._append_event(
                job,
                "worker_failed",
                "Worker branch merge patch could not be applied to the coordinator draft.",
                {"worker_id": "merge", "apply_result": apply_result.model_dump(mode="json"), "artifact_ref": job.worker_merge_ref},
            )
            return [], [], "Worker branches need coordinator repair after merge apply conflict."
        branch_results = self.worker_runtime.record_branch_results(artifact_run_id, file_changes)
        self._store_worker_merge_decision(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_run_id=artifact_run_id,
            merge_report=merge_report,
            results=results,
            status="merged",
            apply_result=apply_result.model_dump(mode="json"),
        )
        for branch in branch_results:
            ref = f"worker_branch_merge:{workspace_id}:{artifact_run_id}:{branch.get('worker_id')}"
            job.worker_branch_refs = list(dict.fromkeys([*job.worker_branch_refs, ref]))
            self._store_report(ref, {"workspace_id": workspace_id, "run_id": run_id, "branch": branch})
        changed_files = list(dict.fromkeys([change.file_path for change in file_changes if change.file_path]))
        self._append_activity(
            job,
            "worker_completed",
            "Merged isolated worker branch diffs",
            {"worker_count": len(results), "changed_files": changed_files, "status": "completed"},
            save=False,
        )
        self._append_event(
            job,
            "worker_completed",
            "Merged isolated worker branch diffs into the coordinator draft.",
            {"worker_id": "merge", "changed_files": changed_files, "file_change_count": len(file_changes), "artifact_ref": job.worker_merge_ref},
        )
        return file_changes, changed_files, "Isolated worker branches produced the initial coordinator draft."

    def _request_agent_turn(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        tool_round: int,
        context_mode: str,
        repeated_no_progress: int,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, object],
        initial_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
        agent_memory: dict[str, Any] | None = None,
        repair_packets: list[dict[str, Any]] | None = None,
        next_forced_action: dict[str, Any] | None = None,
        diagnostics_delta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        turn_started_at = time.perf_counter()
        turn_started_iso = datetime.now(timezone.utc).isoformat()
        protocol_turn_id = f"turn_{attempt}_{tool_round}"
        self._append_event(
            job,
            "agent_turn_started",
            "Workspace code agent is planning the next code edit.",
            {
                "attempt": attempt,
                "tool_round": tool_round,
                "context_mode": context_mode,
                "has_draft_diff": bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
            },
        )
        if self.run_protocol_service is not None:
            self.run_protocol_service.append_event(
                run_id=run_id,
                workspace_id=workspace_id,
                session_id=request.session_id,
                event_type="turn_started",
                status="started",
                turn_id=protocol_turn_id,
                message="Workspace code agent turn started.",
                payload={
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "repeated_no_progress": repeated_no_progress,
                },
                refs={
                    "checkpoint_ref": job.resume_checkpoint_ref,
                    "trace_bundle_ref": job.trace_bundle_ref,
                },
                source_event_type="agent_turn_started",
            )
        try:
            generation_mode = self._generation_mode(request.generation_mode)
            intent_value = str(request.intent or "").strip().lower()
            current_draft_diff = self.workspace_service.diff(workspace_id, run_id=run_id).strip()
            has_draft_diff = bool(current_draft_diff)
            create_turn = intent_value == "create"
            create_repair_turn = create_turn and has_draft_diff
            browser_step_repair = create_repair_turn and self._browser_step_repair_needed(latest_execution)
            workflow_slice_repair = (
                create_repair_turn
                and not browser_step_repair
                and self._workflow_slice_repair_needed(latest_execution)
            )
            generated_tests_repair = (
                create_repair_turn
                and not browser_step_repair
                and not workflow_slice_repair
                and self._generated_tests_repair_needed(latest_execution)
            )
            isolate_compact_repair = create_repair_turn and (generated_tests_repair or browser_step_repair or repeated_no_progress > 0)
            fast_create_turn = generation_mode == GenerationMode.FAST and create_turn
            focused_edit_kind = self._focused_edit_kind(request)
            focused_visual_edit = focused_edit_kind == "visual_style_edit"
            edit_turn = intent_value in {"edit", "refine", "role_only_change"}
            compact_edit_turn = edit_turn and focused_edit_kind in {"small_copy_edit", "behavior_edit", "standard"}
            agentic_workflow_turn = (
                focused_edit_kind == "behavior_workflow_edit"
                and generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
            ) or (
                create_turn
                and generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
            )
            primary_model = models_for_role(
                "agent_turn",
                model_profile=request.model_profile,
                generation_mode=generation_mode,
            )
            force_replace_only = bool(
                create_repair_turn
                and last_turn_summary
                and "full-file replace actions for only the conflicted files" in str(last_turn_summary).lower()
            )
            content_max_length = (
                FOCUSED_VISUAL_CONTENT_MAX_LENGTH
                if focused_visual_edit
                else 18000 if force_replace_only
                else 9000 if generated_tests_repair
                else 7000 if browser_step_repair
                else 18000 if workflow_slice_repair and generation_mode == GenerationMode.FAST
                else 22000 if workflow_slice_repair and generation_mode == GenerationMode.BALANCED
                else 26000 if workflow_slice_repair
                else 6000 if create_repair_turn and generation_mode == GenerationMode.FAST
                else 6000 if create_repair_turn and generation_mode == GenerationMode.BALANCED
                else 8000 if create_repair_turn
                else 24000 if agentic_workflow_turn
                else 9000 if compact_edit_turn
                else 9000 if fast_create_turn else 18000
            )
            del content_max_length
            user_prompt = self._agent_user_prompt(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                attempt=attempt,
                tool_round=tool_round,
                context_mode=context_mode,
                repeated_no_progress=repeated_no_progress,
                latest_execution=latest_execution,
                latest_preview_details=latest_preview_details,
                initial_context=initial_context,
                extra_file_context=extra_file_context,
                tool_results=tool_results,
                last_turn_summary=last_turn_summary,
                latest_diff_summary=latest_diff_summary,
                agent_memory=agent_memory,
                repair_packets=repair_packets or [],
                next_forced_action=next_forced_action or {},
                diagnostics_delta=diagnostics_delta or {},
            )
            user_prompt = self._attach_context_pressure(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                prompt_payload=user_prompt,
                attempt=attempt,
                tool_round=tool_round,
            )
            self._record_rollout_trace(
                job,
                artifact_run_id=run_id or job.job_id,
                event_type="prompt_context_pack",
                payload=self._prompt_context_trace_payload(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request=request,
                    attempt=attempt,
                    tool_round=tool_round,
                    context_mode=context_mode,
                    prompt_payload=user_prompt,
                    agent_memory=agent_memory or {},
                    latest_execution=latest_execution,
                    latest_diff_summary=latest_diff_summary,
                ),
            )
            self._append_event(
                job,
                "agent_turn_started",
                "Workspace code agent prepared the model context.",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "phase": "context_ready",
                    "has_draft_diff": has_draft_diff,
                    "prompt_payload_mode": "compact_generated_test_repair" if generated_tests_repair else "compact_browser_repair" if browser_step_repair else "compact_repair" if create_repair_turn else "standard",
                    "force_replace_only": force_replace_only,
                },
            )
            self._append_event(
                job,
                "agent_turn_started",
                "Workspace code agent is selecting the next tool call.",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "phase": "model_request",
                    "has_draft_diff": has_draft_diff,
                    "prompt_payload_mode": "compact_generated_test_repair" if generated_tests_repair else "compact_browser_repair" if browser_step_repair else "compact_repair" if create_repair_turn else "standard",
                    "force_replace_only": force_replace_only,
                },
            )
            artifact_run_id = run_id or job.job_id
            transcript_context = self.transcript_store.next_model_context(artifact_run_id)
            if isolate_compact_repair and (
                transcript_context.get("previous_response_id")
                or transcript_context.get("tool_result_messages")
                or transcript_context.get("post_compact_messages")
            ):
                self.transcript_store.clear_model_context(artifact_run_id)
                self._append_event(
                    job,
                    "compact_boundary",
                    "Compact repair is isolated from the previous model transcript to reduce token load.",
                    {
                        "attempt": attempt,
                        "tool_round": tool_round,
                        "reason": "generated_test_repair" if generated_tests_repair else "browser_step_repair" if browser_step_repair else "repeated_no_progress_repair",
                    },
                )
                transcript_context = self.transcript_store.next_model_context(artifact_run_id)
            pending_tool_results = list(transcript_context.get("tool_result_messages") or [])
            pending_post_compact_messages = list(transcript_context.get("post_compact_messages") or [])
            if pending_post_compact_messages:
                user_prompt = self._with_post_compact_messages(user_prompt, pending_post_compact_messages)
            available_tools = AgentToolRegistry.openai_tools()
            forced_tool_names = {
                str(item)
                for item in (next_forced_action or {}).get("forced_tool_names", [])
                if str(item).strip()
            }
            if not forced_tool_names:
                nested_forced = (next_forced_action or {}).get("next_forced_action")
                if isinstance(nested_forced, dict):
                    forced_tool_names = {
                        str(item)
                        for item in nested_forced.get("allowed_tools", [])
                        if str(item).strip()
                    }
            if forced_tool_names:
                available_tools = AgentToolRegistry.openai_tools(forced_tool_names)
            if generated_tests_repair or browser_step_repair or (
                create_repair_turn
                and (tool_round > self._tool_round_limit(generation_mode) or repeated_no_progress > 0)
            ):
                available_tools = AgentToolRegistry.openai_tools(forced_tool_names or {"apply_patch_to_draft", "write_file"})
            response = self.openai_client.generate_agent_tool_step(
                tools=available_tools,
                system_prompt=self._agent_system_prompt(),
                user_prompt=user_prompt,
                prompt_cache_key=self._prompt_cache_key(workspace_id, run_id, request.prompt),
                stable_prefix="workspace_code_agent_tool_loop_v1",
                model_override=primary_model,
                responses_tuning_override=self._agent_turn_tuning(
                    generation_mode,
                    intent=str(request.intent or ""),
                    focused_edit_kind=focused_edit_kind,
                    repair_turn=create_repair_turn,
                    generated_tests_repair=generated_tests_repair,
                    browser_step_repair=browser_step_repair,
                ),
                previous_response_id=str(transcript_context.get("previous_response_id") or "") or None,
                tool_result_messages=pending_tool_results,
            )
            job.llm_model = str(response.get("model") or "")
            turn_cache_stats = response.get("cache_stats") or {}
            job.cache_stats = self._merge_cache_stats(job.cache_stats, turn_cache_stats)
            payload = response.get("payload")
            parsed_payload = payload if isinstance(payload, dict) else {}
            raw_tool_calls = parsed_payload.get("tool_calls")
            tool_call_count = len(raw_tool_calls) if isinstance(raw_tool_calls, list) else 0
            duration_ms = int((time.perf_counter() - turn_started_at) * 1000)
            normalized_tool_calls = [item for item in raw_tool_calls if isinstance(item, dict)] if isinstance(raw_tool_calls, list) else []
            self.transcript_store.append_model_turn(
                artifact_run_id,
                attempt=attempt,
                tool_round=tool_round,
                response_id=str(parsed_payload.get("response_id") or ""),
                assistant_message=str(parsed_payload.get("assistant_message") or ""),
                tool_calls=normalized_tool_calls,
                model=job.llm_model,
                usage={
                    "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                    "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                    "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                    "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                },
                consumed_tool_result_count=len(pending_tool_results),
                consumed_post_compact_count=len(pending_post_compact_messages),
                consumed_post_compact_refs=[
                    str(item.get("post_compact_message_ref") or item.get("ref") or "")
                    for item in pending_post_compact_messages
                    if isinstance(item, dict)
                ],
            )
            for compact_message in pending_post_compact_messages:
                if not isinstance(compact_message, dict) or self.run_compaction_service is None:
                    continue
                boundary_id_value = str(compact_message.get("boundary_id") or "").strip()
                if not boundary_id_value:
                    continue
                try:
                    self.run_compaction_service.mark_post_compact_consumed(
                        run_id=run_id,
                        boundary_id=boundary_id_value,
                        turn_id=protocol_turn_id,
                    )
                except KeyError:
                    logger.debug("post_compact_message_missing run_id=%s boundary_id=%s", run_id, boundary_id_value)
            self.transcript_store.append_tool_calls(artifact_run_id, normalized_tool_calls)
            self._record_rollout_trace(
                job,
                artifact_run_id=artifact_run_id,
                event_type="model_prompt_response",
                payload={
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "response_id": str(parsed_payload.get("response_id") or ""),
                    "model": job.llm_model,
                    "assistant_summary": str(parsed_payload.get("assistant_message") or "")[:1200],
                    "tool_call_count": tool_call_count,
                    "token_usage": {
                        "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                        "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                        "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                        "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                    },
                },
            )
            job.active_tool_uses = [
                {
                    "tool_use_id": str(item.get("tool_use_id") or ""),
                    "tool": str(item.get("tool") or ""),
                    "status": "requested",
                    "attempt": attempt,
                    "tool_round": tool_round,
                }
                for item in normalized_tool_calls
            ]
            self._store_transcript_snapshot(job, artifact_run_id)
            self._store_resume_checkpoint(
                job,
                artifact_run_id,
                phase="model_turn",
                extra={
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "pending_tool_results": len(pending_tool_results),
                    "consumed_post_compact_count": len(pending_post_compact_messages),
                },
            )
            protocol_bookmark: dict[str, Any] | None = None
            if self.run_protocol_service is not None:
                try:
                    protocol_bookmark = self.run_protocol_service.create_bookmark(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        turn_id=protocol_turn_id,
                        response_id=str(parsed_payload.get("response_id") or "") or None,
                        checkpoint_ref=job.resume_checkpoint_ref,
                        trace_bundle_ref=job.trace_bundle_ref,
                        diff_sha256_value=diff_sha256(self.workspace_service.diff(workspace_id, run_id=run_id)),
                        tool_result_count=len(pending_tool_results),
                        latest_check_ref=f"latest_check_execution:{run_id}",
                        todo_state_ref=job.scratchpad_ref,
                        metadata={
                            "attempt": attempt,
                            "tool_round": tool_round,
                            "context_mode": context_mode,
                            "model": job.llm_model,
                        },
                    )
                    self.run_protocol_service.append_event(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        session_id=request.session_id,
                        event_type="model_delta",
                        status="completed",
                        turn_id=protocol_turn_id,
                        message="Workspace code agent produced a model response.",
                        payload={
                            "response_id": str(parsed_payload.get("response_id") or "") or None,
                            "model": job.llm_model,
                            "assistant_summary": str(parsed_payload.get("assistant_message") or "")[:600],
                            "tool_call_count": tool_call_count,
                            "token_usage": {
                                "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                                "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                                "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                                "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                            },
                        },
                        refs={
                            "checkpoint_ref": job.resume_checkpoint_ref,
                            "trace_bundle_ref": job.trace_bundle_ref,
                        },
                        bookmark_id=str(protocol_bookmark.get("bookmark_id") or "") if protocol_bookmark else None,
                        source_event_type="iteration_ready",
                    )
                    for tool_call in normalized_tool_calls:
                        self.run_protocol_service.append_event(
                            run_id=run_id,
                            workspace_id=workspace_id,
                            session_id=request.session_id,
                            event_type="tool_requested",
                            status="started",
                            turn_id=protocol_turn_id,
                            message=f"Tool requested: {tool_call.get('tool') or 'unknown'}",
                            payload={
                                "tool": tool_call.get("tool"),
                                "tool_use_id": tool_call.get("tool_use_id"),
                                "mode": tool_call.get("mode"),
                                "targets": tool_call.get("targets") if isinstance(tool_call.get("targets"), list) else [],
                            },
                            bookmark_id=str(protocol_bookmark.get("bookmark_id") or "") if protocol_bookmark else None,
                            source_event_type="tool_call",
                        )
                except Exception:
                    logger.exception("run_protocol_turn_record_failed run_id=%s turn_id=%s", run_id, protocol_turn_id)
            self._append_agent_diagnostic(
                workspace_id,
                {
                    "run_id": run_id,
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "started_at": turn_started_iso,
                    "duration_ms": duration_ms,
                    "status": "completed",
                    "model": job.llm_model,
                    "response_id": str(parsed_payload.get("response_id") or ""),
                    "tool_call_count": tool_call_count,
                    "consumed_tool_result_count": len(pending_tool_results),
                    "tool_calls": [
                        {
                            "tool": str(item.get("tool") or ""),
                            "tool_use_id": str(item.get("tool_use_id") or ""),
                            "mode": str(item.get("mode") or ""),
                            "targets": [str(target) for target in item.get("targets") or []] if isinstance(item, dict) else [],
                        }
                        for item in raw_tool_calls
                        if isinstance(item, dict)
                    ] if isinstance(raw_tool_calls, list) else [],
                    "token_usage": {
                        "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                        "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                        "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                        "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                    },
                },
            )
            self._append_event(
                job,
                "iteration_ready",
                "Workspace code agent returned an agent turn.",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "response_id": str(parsed_payload.get("response_id") or ""),
                    "tool_call_count": tool_call_count,
                    "model": job.llm_model,
                    "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                    "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                    "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                    "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                    "has_draft_diff": bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
                },
            )
            if self.run_protocol_service is not None:
                self.run_protocol_service.append_event(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    session_id=request.session_id,
                    event_type="turn_completed",
                    status="completed",
                    turn_id=protocol_turn_id,
                    message="Workspace code agent turn completed.",
                    payload={"attempt": attempt, "tool_round": tool_round, "tool_call_count": tool_call_count},
                    bookmark_id=str(protocol_bookmark.get("bookmark_id") or "") if protocol_bookmark else None,
                    source_event_type="iteration_ready",
                )
            return parsed_payload if parsed_payload else {"error": "Agent returned no tool-step payload."}
        except Exception as exc:
            error_text = str(exc)
            self._append_agent_diagnostic(
                workspace_id,
                {
                    "run_id": run_id,
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "started_at": turn_started_iso,
                    "duration_ms": int((time.perf_counter() - turn_started_at) * 1000),
                    "status": "error",
                    "error": error_text,
                    "error_class": exc.__class__.__name__,
                },
            )
            if self._is_output_cap_error(error_text):
                logger.warning(
                    "workspace_code_agent_turn_output_cap workspace_id=%s run_id=%s attempt=%s tool_round=%s error=%s",
                    workspace_id,
                    run_id,
                    attempt,
                    tool_round,
                    error_text,
                )
            elif self._is_provider_quota_error(error_text):
                logger.warning(
                    "workspace_code_agent_turn_provider_quota workspace_id=%s run_id=%s attempt=%s tool_round=%s error=%s",
                    workspace_id,
                    run_id,
                    attempt,
                    tool_round,
                    error_text,
                )
            else:
                logger.exception("workspace_code_agent_turn_failed workspace_id=%s run_id=%s", workspace_id, run_id)
            if self.run_protocol_service is not None:
                self.run_protocol_service.append_event(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    session_id=request.session_id,
                    event_type="turn_completed",
                    status="failed",
                    turn_id=protocol_turn_id,
                    message="Workspace code agent turn failed.",
                    payload={"attempt": attempt, "tool_round": tool_round, "error": error_text, "error_class": exc.__class__.__name__},
                    source_event_type="agent_turn_failed",
                )
            return {"error": f"Workspace code agent turn failed: {error_text}"}

    @staticmethod
    def _is_provider_quota_error(error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        return "insufficient_quota" in lowered or "exceeded your current quota" in lowered

    @staticmethod
    def _agent_system_prompt() -> str:
        return agent_system_prompt()

    def _agent_user_prompt(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        tool_round: int,
        context_mode: str,
        repeated_no_progress: int,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, object],
        initial_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
        agent_memory: dict[str, Any] | None = None,
        repair_packets: list[dict[str, Any]] | None = None,
        next_forced_action: dict[str, Any] | None = None,
        diagnostics_delta: dict[str, Any] | None = None,
    ) -> str:
        file_tree = [
            path
            for path in self.workspace_service.file_tree(workspace_id, run_id=run_id)
            if self._agent_model_visible_path(path)
        ]
        generation_mode = self._generation_mode(request.generation_mode)
        intent_value = str(request.intent or "").strip().lower()
        current_draft_diff = self.workspace_service.diff(workspace_id, run_id=run_id).strip()
        has_draft_diff = bool(current_draft_diff)
        focused_edit_kind = self._focused_edit_kind(request)
        focused_visual_edit = focused_edit_kind == "visual_style_edit"
        focused_edit_files = self._focused_visual_css_paths(request.target_role_scope or ROLE_ORDER) if focused_visual_edit else []
        compact_repair_prompt = has_draft_diff and (
            intent_value == "create" or focused_edit_kind == "behavior_workflow_edit"
        )
        browser_step_repair = compact_repair_prompt and self._browser_step_repair_needed(latest_execution)
        workflow_slice_repair = (
            compact_repair_prompt
            and not browser_step_repair
            and self._workflow_slice_repair_needed(latest_execution)
        )
        missing_generated_tests_repair = (
            compact_repair_prompt
            and not browser_step_repair
            and not workflow_slice_repair
            and self._missing_generated_tests_repair_needed(latest_execution)
        )
        stale_generated_tests_repair = (
            compact_repair_prompt
            and not browser_step_repair
            and not workflow_slice_repair
            and self._stale_generated_tests_repair_needed(latest_execution)
        )
        generated_tests_repair = missing_generated_tests_repair or stale_generated_tests_repair
        stored_run = self.store.get("runs", run_id) if run_id else None
        stored_acceptance_contract = (
            dict(stored_run.get("acceptance_contract") or {})
            if isinstance(stored_run, dict) and isinstance(stored_run.get("acceptance_contract"), dict)
            else {}
        )
        prompt_analysis: dict[str, Any] | None = None
        requires_prompt_analysis = (
            intent_value == "create"
            or focused_edit_kind == "behavior_workflow_edit"
            or request.generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
        )
        if requires_prompt_analysis and not isinstance(stored_acceptance_contract.get("prompt_hints"), dict):
            prompt_analysis = self.openai_client.analyze_miniapp_prompt(
                prompt=request.prompt,
                generation_mode=request.generation_mode,
                model_profile=request.model_profile,
            )
        acceptance_contract = (
            stored_acceptance_contract
            if isinstance(stored_acceptance_contract.get("prompt_hints"), dict)
            else build_acceptance_contract(
                prompt=request.prompt,
                intent=intent_value,
                generation_mode=request.generation_mode,
                focused_edit_kind=focused_edit_kind,
                prompt_analysis=prompt_analysis,
            )
        )
        orchestration = orchestration_metadata_for_contract(
            contract=acceptance_contract,
            generation_mode=request.generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        stored_plan = (
            dict(stored_run.get("implementation_plan") or {})
            if isinstance(stored_run, dict) and isinstance(stored_run.get("implementation_plan"), dict)
            else {}
        )
        implementation_plan = stored_plan or build_implementation_plan(
            prompt=request.prompt,
            intent=intent_value,
            generation_mode=request.generation_mode,
            acceptance_contract=acceptance_contract,
            orchestration=orchestration,
        )
        focused_rules: list[str] = []
        if focused_visual_edit:
            focused_rules.extend(
                [
                    "Focused visual_style_edit lane: change only CSS/style files listed in focused_edit_files. Do not edit HTML, JavaScript, backend routes, route_manifest.json, generated tests, docs, or unrelated files.",
                    f"Use one compact mutating tool batch with at most {FOCUSED_VISUAL_WRITE_LIMIT} CSS writes. Prefer write_file for CSS if a hunk patch would be ambiguous or has already conflicted.",
                "Do not add product behavior, data, tests, API calls, navigation, or role copy for a pure style/color/spacing prompt.",
                    "CSS may use hex/rgb/hsl values for requested colors; the literal user color word does not need to appear in generated CSS.",
                    "Keep the existing top safe spacing and existing selectors/data-testid hooks intact while changing the visual styling.",
                ]
            )
        repair_context: dict[str, str] = {}
        if compact_repair_prompt:
            diff_paths = self._paths_from_diff(current_draft_diff) or self._paths_from_diff(str(latest_diff_summary or ""))
            repair_context_paths = (
                self._generated_test_repair_paths(latest_execution, diff_paths=diff_paths)
                if generated_tests_repair
                else self._browser_step_repair_paths(latest_execution, diff_paths=diff_paths)
                if browser_step_repair
                else self._repair_context_paths(
                    failed_paths=self._target_files_from_execution(latest_execution),
                    diff_paths=diff_paths,
                )
            )
            for path in repair_context_paths:
                if not self._agent_model_visible_path(path):
                    continue
                content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
                if content is not None:
                    repair_context[path] = content
            if generated_tests_repair:
                for path, content in extra_file_context.items():
                    normalized = self._strip_leading_dot_slash(path)
                    if normalized.startswith("miniapp/") and self._agent_model_visible_path(normalized) and normalized not in repair_context:
                        repair_context[normalized] = content
        file_context_payload = (
            self._compact_file_contexts(
                repair_context,
                max_files=12 if generated_tests_repair else 5 if browser_step_repair else 10 if repeated_no_progress else 14 if workflow_slice_repair else 8,
                max_chars=2600 if generated_tests_repair else 1800 if browser_step_repair else 2200 if repeated_no_progress else 3200 if workflow_slice_repair else 2200,
            )
            if compact_repair_prompt
            else {
                **self._compact_file_contexts(
                    {
                        path: content
                        for path, content in initial_context.items()
                        if self._agent_model_visible_path(path)
                    },
                    max_files=8 if focused_visual_edit else 14,
                    max_chars=9000 if focused_visual_edit else 6000,
                ),
                **self._compact_file_contexts(
                    {
                        path: content
                        for path, content in extra_file_context.items()
                        if self._agent_model_visible_path(path)
                    },
                    max_files=4 if focused_visual_edit else 12,
                ),
            }
        )
        forced_registry_tools: set[str] | None = None
        if isinstance(next_forced_action, dict):
            forced_registry_tools = {
                str(item)
                for item in (next_forced_action or {}).get("forced_tool_names", [])
                if str(item).strip()
            }
            if not forced_registry_tools:
                nested_forced = (next_forced_action or {}).get("next_forced_action")
                if isinstance(nested_forced, dict):
                    forced_registry_tools = {
                        str(item)
                        for item in nested_forced.get("allowed_tools", [])
                        if str(item).strip()
                    }
        if not forced_registry_tools and (
            generated_tests_repair or browser_step_repair or (compact_repair_prompt and repeated_no_progress > 0)
        ):
            forced_registry_tools = {"apply_patch_to_draft", "write_file"}
        active_repair_case_payload = self._active_repair_case_payload(repair_packets or [])
        active_repair_next_action = (
            active_repair_case_payload.get("next_action")
            if isinstance(active_repair_case_payload.get("next_action"), dict)
            else {}
        )
        payload = {
            "task": "Edit the draft workspace to satisfy the user prompt and pass platform invariant checks.",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "prompt_payload_mode": "compact_generated_test_repair" if generated_tests_repair else "compact_browser_repair" if browser_step_repair else "compact_repair" if compact_repair_prompt else "focused_visual" if focused_visual_edit else "standard",
            "mode": request.mode,
            "intent": request.intent,
            "generation_mode": str(getattr(request.generation_mode, "value", request.generation_mode) or ""),
            "focused_edit_kind": focused_edit_kind,
            "focused_edit_files": focused_edit_files,
            "acceptance_contract": (
                self._compact_acceptance_contract(acceptance_contract)
                if compact_repair_prompt
                else acceptance_contract
            ),
            "implementation_plan": (
                self._compact_jsonish(implementation_plan, max_chars=900, max_items=8)
                if compact_repair_prompt
                else implementation_plan
            ),
            "worker_branching": (
                {
                    "enabled": False,
                    "reason": "focused_generated_test_repair" if generated_tests_repair else "focused_browser_step_repair" if browser_step_repair else "compact_repair_after_no_progress",
                }
                if generated_tests_repair or browser_step_repair or (compact_repair_prompt and repeated_no_progress > 0)
                else self._worker_branching_prompt_payload(
                    generation_mode=generation_mode,
                    implementation_plan=implementation_plan,
                )
                if generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
                else {"enabled": False, "mode": "single_agent_loop"}
            ),
            "orchestration": (
                self._compact_orchestration_metadata(orchestration)
                if compact_repair_prompt
                else orchestration
            ),
            "attempt": attempt,
            "tool_round": tool_round,
            "context_mode": context_mode,
            "repeated_no_progress": repeated_no_progress,
            "agent_memory": (
                self._compact_jsonish(agent_memory or {}, max_chars=700, max_items=5)
                if browser_step_repair or generated_tests_repair
                else agent_memory or {}
            ),
            "repair_packets": self._compact_jsonish(
                repair_packets or [],
                max_chars=2400 if compact_repair_prompt else 3600,
                max_items=6,
            ),
            "active_repair_case": self._compact_jsonish(
                active_repair_case_payload,
                max_chars=2200 if compact_repair_prompt else 3200,
                max_items=8,
            ),
            "next_forced_action": self._compact_jsonish(
                next_forced_action or {},
                max_chars=1200,
                max_items=8,
            ),
            "diagnostics_delta": self._compact_jsonish(
                diagnostics_delta or {},
                max_chars=2200 if compact_repair_prompt else 3200,
                max_items=8,
            ),
            "environment_snapshot": self._compact_jsonish(
                self.environment_snapshots.get(run_id) or {},
                max_chars=700,
                max_items=6,
            ),
            "latest_turn_diff": self._compact_jsonish(
                self.turn_diff_tracker.latest_summary(run_id),
                max_chars=900,
                max_items=6,
            ),
            "first_blocking_issue": (
                self._first_blocking_issue_from_execution(latest_execution)
                if compact_repair_prompt
                else {}
            ),
            "tool_registry": self._agent_tool_registry_payload(
                forced_registry_tools
            ),
            "repair_focus": (
                str(active_repair_next_action.get("instruction") or active_repair_next_action.get("action") or "")
                if active_repair_next_action
                else
                str((next_forced_action or {}).get("repair_focus") or "")
                if isinstance(next_forced_action, dict) and next_forced_action.get("active")
                else
                self._browser_step_repair_focus(latest_execution)
                if browser_step_repair
                else
                self._generated_test_repair_focus(latest_execution, missing=missing_generated_tests_repair)
                if generated_tests_repair
                else
                "Create or replace both missing generated test files now: miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs. In the same coherent patch, also fix the first API/frontend schema mismatch from latest_checks if one is listed. Do not spend this turn only on CSS, copy, or a single frontend mismatch while generated tests are absent."
                if missing_generated_tests_repair
                else
                "Repeated no-progress repair: patch only first_blocking_issue in source files. Do not edit generated tests unless first_blocking_issue itself is a generated test. For role child-page form/control issues, update the role app.js to be view-aware and bind that child page's visible controls."
                if compact_repair_prompt and repeated_no_progress > 0
                else
                "Repair the connected workflow slice from latest_checks in one coherent mutating tool batch touching only the named backend/role/test files; keep selectors, payloads, routes, and generated tests aligned together."
                if workflow_slice_repair
                else "Patch only the first concrete blocking issue from latest_checks. Use 1-2 apply_patch_to_draft/write_file calls touching only the named file(s); do not repair all failures in one turn."
                if compact_repair_prompt
                else ""
            ),
            "user_prompt": request.prompt,
            "error_context": request.error_context.model_dump(mode="json") if request.error_context else None,
            "role_scope": list(request.target_role_scope or ROLE_ORDER),
            "file_tree": file_tree[:30] if generated_tests_repair else file_tree[:25] if browser_step_repair else file_tree[:40] if compact_repair_prompt and repeated_no_progress > 0 else file_tree[:80] if compact_repair_prompt else file_tree[:120] if focused_visual_edit else file_tree[:240],
            "file_contexts": file_context_payload,
            "context_pack": (
                {}
                if focused_visual_edit or compact_repair_prompt
                else self._context_pack_payload(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    request=request,
                    latest_execution=latest_execution,
                    latest_diff_summary=latest_diff_summary,
                    context_mode=context_mode,
                    attempt=attempt,
                )
            ),
            "latest_checks": (
                self._compact_generated_test_repair_checks(latest_execution)
                if generated_tests_repair
                else self._compact_repair_checks(latest_execution)
                if compact_repair_prompt
                else self._compact_checks(latest_execution)
            ),
            "browser_repair_packet": (
                self._browser_step_repair_packet(latest_execution)
                if browser_step_repair
                else {}
            ),
            "generated_test_repair_packet": (
                self._generated_test_repair_packet(latest_execution)
                if generated_tests_repair
                else {}
            ),
            "preview": self._compact_preview_details(latest_preview_details) if compact_repair_prompt else latest_preview_details,
            "latest_diff_summary": (
                truncate_tool_text(str(latest_diff_summary or ""), max_chars=1400 if generated_tests_repair else 1200 if browser_step_repair else 4000)
                if compact_repair_prompt
                else latest_diff_summary
            ),
            "last_turn_summary": (
                truncate_tool_text(str(last_turn_summary or ""), max_chars=900 if generated_tests_repair else 700 if browser_step_repair else 1800)
                if compact_repair_prompt
                else last_turn_summary
            ),
            "tool_results": self._compact_tool_results(
                tool_results,
                workspace_id=workspace_id,
                run_id=run_id,
                max_items=2 if generated_tests_repair else 1 if browser_step_repair else 4 if compact_repair_prompt else 8,
            ),
            "rules": (
                self._compact_repair_rules(
                    generation_mode=generation_mode,
                    focused_edit_kind=focused_edit_kind,
                    workflow_slice_repair=workflow_slice_repair,
                    missing_generated_tests_repair=missing_generated_tests_repair,
                    stale_generated_tests_repair=stale_generated_tests_repair,
                    browser_step_repair=browser_step_repair,
                    repeated_no_progress=repeated_no_progress,
                )
                if compact_repair_prompt
                else [
                    *focused_rules,
                    f"Keep each turn applyable: use mutating tools for a compact coherent edit, or request only the specific read-only tools needed for the next patch.",
                    "Use the implementation_plan and acceptance_contract as the product contract. Derive entities, fields, routes, labels, and role actions from the user's prompt and current code, not from platform templates.",
                    "Do not satisfy the contract with fixed sample nouns or a platform-invented workflow; implement the concrete entities, controls, and persisted state described by the LLM-derived acceptance contract and the current code.",
                    "Each role surface must satisfy its own LLM-derived role responsibilities. Do not satisfy a role with only a role-agnostic data-refresh page when the contract assigns concrete actions.",
                    "Use implementation_plan.product_scale_contract.min_role_routes and implementation_plan.routeable_screen_plan as prompt-derived minimum scope. Add child pages when the prompt has enough role actions/resources to require them; do not collapse a broad business workflow into one dashboard-only page.",
                    "Create/workflow completion requires real UI controls, JavaScript handlers, backend persistence, generated tests, cross-role visibility, refresh persistence, and browser/mobile proof.",
                    "Build three isolated role surfaces in the miniapp shell. Role responsibilities come only from the LLM-derived acceptance contract; do not link role roots to each other and do not force unrequested workflow semantics.",
                    "For multi-page role apps, shared static/<role>/app.js must initialize per page: use body[data-view] or route, guard optional DOM nodes from other pages, and bind every visible child-page form/button/control to persisted API behavior.",
                    "The source role must display its original submitted state and any persisted prompt-derived update fields after reload; observer roles should show the shared state without duplicate source controls.",
                    "User-facing UI copy must be polished product language: do not render raw API paths, the word API, HTTP methods, internal route names, role slugs, or enum codes like `new`/`preparing`; map persisted values to human-readable labels and keep label/value pairs visually separated.",
                    "In async JavaScript form handlers, capture DOM nodes before any await, for example `const form = event.currentTarget`, then use `form.reset()` after awaited API calls. Do not read `event.currentTarget` after await because browsers clear it after dispatch.",
                    "Do not initialize workflow bindings only with `document.addEventListener('DOMContentLoaded', init)`. Use a readyState guard (`if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();`) so forms still bind when scripts load after DOMContentLoaded in preview/browser verification.",
                    "Fast should be compact and working for the prompt's actual breadth; Balanced should add moderate workflow/design depth; Quality should first get the workflow green, then add a polished mobile design pass. Do not add arbitrary pages/resources, but do satisfy the prompt-derived minimum pages and resources in product_scale_contract.",
                    "Mobile-first: target Telegram widths around 360-430px, use one consistent light neutral product visual system across all roles unless the user explicitly asks for a dark theme, preserve safe top spacing/preview bridge, and avoid horizontal scroll or overlapping cards/forms/actions.",
                    "Generated source must start empty: no mock, seed, demo, sample, fixture, preloaded, or hard-coded product records. Empty states and validation test payloads are allowed.",
                    "Generated tests must verify the actual app contract: persisted API behavior, real role HTML/JS selectors, prompt-assigned actions, and no stale UI-only controls. Python generated tests must be unittest-discoverable: import unittest, define a unittest.TestCase subclass, and put assertions inside test_* methods. FastAPI generated tests should use `with TestClient(app) as client:` so lifespan/table setup runs, or explicitly create tables after generated ORM models are imported. If a Python test reads a JSON store file, normalize `raw.get('items', raw)` when raw is a dict before asserting count. JS generated tests run from cwd=miniapp, so read app/static/... and app/generated/... paths, not miniapp/app/... paths. Node has no browser DOM: do not import browser-only role app.js files unless the test creates explicit window/document mocks first; prefer source-text assertions for selectors, API calls, routes, and handlers. Do not write pytest-only top-level test functions. Patch tests only when they are stale with the app contract.",
                    "Generated Python tests must not import product/reset helpers from platform shell modules such as app.routes.health, app.routes.role_pages, or app.routes.role_routes; import from the app-owned route module or reset the generated DB after importing app-owned route models.",
                    "For edit/refine/fix/repair, patch existing files with small unified diffs. Use full-file replace only for new files, tiny files, create-mode work, or a file that repeatedly conflicts.",
                    "If checks/browser proof fail, repair the concrete failing slice from latest_checks/tool_results: align selectors, payload fields, API routes, rendered state, and tests together. Do not rewrite unrelated files.",
                    "Read-only tools never write files. Mutating tools are serialized through the draft edit validator. run_command is limited to safe diagnostics such as unittest, py_compile, node tests/checks, rg, sed, and ls.",
                    "If you have enough context, call apply_patch_to_draft or write_file. Do not stop unless the blocker is exact and external.",
                ]
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _active_repair_case_payload(repair_packets: list[dict[str, Any]]) -> dict[str, Any]:
        for packet in repair_packets:
            if not isinstance(packet, dict):
                continue
            prompt = packet.get("repair_prompt")
            if not isinstance(prompt, dict):
                continue
            sections = prompt.get("sections") if isinstance(prompt.get("sections"), dict) else {}
            next_action = prompt.get("next_action") if isinstance(prompt.get("next_action"), dict) else sections.get("next_action")
            return {
                "case_id": prompt.get("case_id") or packet.get("repair_case_id"),
                "failure": prompt.get("failure") or sections.get("failure") or packet.get("failure_signature"),
                "target_files": prompt.get("target_files") or sections.get("target_files") or packet.get("target_files") or [],
                "allowed_edit_slice": prompt.get("allowed_edit_slice") or sections.get("allowed_edit_slice") or [],
                "next_action": next_action if isinstance(next_action, dict) else {},
                "expected_proof": prompt.get("expected_proof") or sections.get("expected_proof") or [],
                "attempt_count": prompt.get("attempt_count") or packet.get("attempt_count") or 0,
                "forbidden_repeat_action": prompt.get("forbidden_repeat_action") or packet.get("forbidden_repeat_action") or "",
            }
        return {}

    @staticmethod
    def _agent_tool_registry_payload(allowed: set[str] | None = None) -> dict[str, dict[str, object]]:
        payload: dict[str, dict[str, object]] = {}
        for name in sorted(AgentToolRegistry.names()):
            if allowed is not None and name not in allowed:
                continue
            spec = AgentToolRegistry.spec(name)
            if spec is None:
                continue
            payload[name] = {
                "kind": spec.kind,
                "concurrency_safe": spec.concurrency_safe,
                "timeout_seconds": spec.timeout_seconds,
                "output_cap_chars": spec.output_cap_chars,
                "activity": spec.activity,
                "progress_label": spec.progress_label,
            }
        return payload

    @staticmethod
    def _worker_prefix_payload(
        *,
        implementation_plan: dict[str, Any],
        acceptance_contract: dict[str, Any],
        semantic_graph: dict[str, Any],
        current_diff_summary: str,
    ) -> dict[str, Any]:
        return {
            "system_contract": {
                "agent_style": "plan, inspect, patch, verify, repair",
                "source_of_truth": "user prompt, current code, validation, and browser proof",
                "product_binding": "none",
                "completion_gate": "strict checks plus real browser/mobile proof",
            },
            "implementation_plan": WorkspaceCodeAgentRuntime._compact_jsonish(implementation_plan, max_chars=1400, max_items=10),
            "acceptance_contract": WorkspaceCodeAgentRuntime._compact_acceptance_contract(acceptance_contract),
            "semantic_graph": WorkspaceCodeAgentRuntime._compact_jsonish(semantic_graph.get("graph") if isinstance(semantic_graph, dict) else {}, max_chars=1400, max_items=10),
            "current_diff_summary": current_diff_summary,
            "worker_directive_slot": "<owned worker directive is appended separately>",
        }

    @staticmethod
    def _worker_branching_prompt_payload(
        *,
        generation_mode: GenerationMode,
        implementation_plan: dict[str, Any],
    ) -> dict[str, Any]:
        mailbox = AgentWorkerManager.mailbox_for_plan(
            generation_mode=generation_mode,
            implementation_plan=implementation_plan,
        )
        if not bool(mailbox.get("enabled")):
            return {
                "enabled": False,
                "mode": str(getattr(generation_mode, "value", generation_mode) or ""),
                "reason": mailbox.get("disabled_reason") or "serial_contract_runtime_writes",
                "write_coordination": mailbox.get("write_coordination") or "serial_contract_runtime_writes",
                "contract": "Use read/search/check tools freely, but keep all draft mutations in the coordinator loop.",
            }
        return WorkspaceCodeAgentRuntime._compact_jsonish(
            {
                "mailbox": mailbox,
                "worker_tasks": AgentWorkerTaskPlanner.worker_tasks(
                    generation_mode=generation_mode,
                    implementation_plan=implementation_plan,
                ),
            },
            max_chars=2600,
            max_items=12,
        )

    def _context_pack_payload(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        latest_execution: CheckExecutionRecord,
        latest_diff_summary: str | None,
        context_mode: str,
        attempt: int,
    ) -> dict[str, Any]:
        builder = getattr(self, "context_pack_builder", None)
        if builder is None:
            return {}
        generation_mode = self._generation_mode(request.generation_mode)
        is_initial_fast_create = (
            generation_mode == GenerationMode.FAST
            and str(request.intent or "").strip().lower() == "create"
            and int(attempt or 0) <= 1
            and context_mode == "minimal"
            and not self.workspace_service.diff(workspace_id, run_id=run_id).strip()
        )
        if is_initial_fast_create:
            return {}
        target_files = self._target_files_from_execution(latest_execution)
        if not target_files and latest_diff_summary:
            target_files = self._paths_from_diff(str(latest_diff_summary or ""))[:8]
        try:
            workspace = self.workspace_service.get_workspace(workspace_id)
            pack = builder.build(
                workspace=workspace,
                prompt=request.prompt,
                model_profile=request.model_profile,
                generation_mode=generation_mode,
                active_paths=target_files,
                target_files=target_files,
                execution_class="shell_app",
                run_id=run_id,
                intent=str(request.intent or ""),
            )
        except Exception as exc:
            logger.warning("context_pack_build_failed workspace_id=%s run_id=%s error=%s", workspace_id, run_id, exc)
            return {"error": str(exc)[:400]}
        return {
            "workspace_summary": pack.workspace_summary,
            "recent_diff": truncate_tool_text(pack.recent_diff, max_chars=3000),
            "selected_code": [
                {
                    "path": chunk.path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "summary": chunk.summary,
                    "excerpt": truncate_tool_text(chunk.text, max_chars=1400),
                }
                for chunk in pack.code_chunks[:6]
            ],
            "targeted_files": self._compact_file_contexts(pack.targeted_files, max_files=6, max_chars=3000),
            "retrieval_stats": {
                "selected_code_paths": (pack.retrieval_stats.get("anchor_report") or {}).get("selected_code_paths", []),
                "target_file_sample": (pack.retrieval_stats.get("anchor_report") or {}).get("target_file_sample", []),
                "budget": pack.retrieval_stats.get("budget") or {},
            },
        }

    @staticmethod
    def _repair_context_paths(*, failed_paths: list[str], diff_paths: list[str]) -> list[str]:
        ordered: list[str] = []
        context_paths = failed_paths or diff_paths
        route_paths = [
            path
            for path in diff_paths
            if path.startswith("miniapp/app/routes/")
            and path.endswith(".py")
            and path not in {"miniapp/app/routes/app_api.py", "miniapp/app/routes/role_pages.py", "miniapp/app/routes/role_routes.py"}
        ]
        supporting_paths: list[str] = []
        if any(path.endswith("miniapp/tests/test_generated_app.py") or path == "miniapp/tests/test_generated_app.py" for path in context_paths):
            supporting_paths.extend([*route_paths, "miniapp/app/routes/api.py", "miniapp/app/schemas.py", "miniapp/app/db.py"])
            supporting_paths.extend(
                [
                    path
                    for path in diff_paths
                    if path.startswith("miniapp/app/static/")
                    and path.endswith((".html", ".js"))
                ][:9]
            )
        if any(path.endswith("miniapp/tests/generated_app.test.mjs") or path == "miniapp/tests/generated_app.test.mjs" for path in context_paths):
            supporting_paths.extend([path for path in diff_paths if path.startswith("miniapp/app/static/") and path.endswith((".html", ".js"))][:4])
        if any("/routes/" in path for path in context_paths):
            supporting_paths.extend([*route_paths, "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/tests/test_generated_app.py"])
        for role in ROLE_ORDER:
            role_prefix = f"miniapp/app/static/{role}/"
            if any(path.startswith(role_prefix) and path.endswith((".html", "app.js", "styles.css")) for path in context_paths):
                supporting_paths.extend(
                    [
                        path
                        for path in diff_paths
                        if path.startswith(role_prefix) and path.endswith(".html")
                    ][:4]
                )
                supporting_paths.extend(
                    [
                        f"{role_prefix}app.js",
                        f"{role_prefix}styles.css",
                        *route_paths,
                        "miniapp/app/routes/api.py",
                        "miniapp/app/schemas.py",
                        "miniapp/tests/test_generated_app.py",
                        "miniapp/tests/generated_app.test.mjs",
                    ]
                )
        for path in [*context_paths, *supporting_paths]:
            normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            if normalized and normalized.startswith("miniapp/") and normalized not in ordered:
                ordered.append(normalized)

        return ordered[:16]

    @staticmethod
    def _compact_acceptance_contract(contract: dict[str, Any]) -> dict[str, Any]:
        flows = contract.get("flows") if isinstance(contract, dict) else []
        compact_flows: list[dict[str, object]] = []
        for flow in flows if isinstance(flows, list) else []:
            if not isinstance(flow, dict):
                continue
            compact_flows.append(
                {
                    "id": flow.get("id"),
                    "roles": flow.get("roles"),
                    "requirements": list(flow.get("requirements") or [])[:5],
                    "required_tests": list(flow.get("required_tests") or [])[:3],
                }
            )
        return {
            "required": bool(contract.get("required")) if isinstance(contract, dict) else False,
            "intent": contract.get("intent") if isinstance(contract, dict) else "",
            "generation_mode": contract.get("generation_mode") if isinstance(contract, dict) else "",
            "workflow_kind": contract.get("workflow_kind") if isinstance(contract, dict) else "",
            "roles": contract.get("roles") if isinstance(contract, dict) else [],
            "features": contract.get("features") if isinstance(contract, dict) else {},
            "required_endpoints": list(contract.get("required_endpoints") or [])[:6] if isinstance(contract, dict) else [],
            "required_controls": list(contract.get("required_controls") or [])[:10] if isinstance(contract, dict) else [],
            "flows": compact_flows[:4],
            "test_requirements": list(contract.get("test_requirements") or [])[:8] if isinstance(contract, dict) else [],
        }

    @staticmethod
    def _compact_orchestration_metadata(orchestration: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(orchestration, dict):
            return {}
        workers = orchestration.get("worker_summaries") if isinstance(orchestration.get("worker_summaries"), list) else []
        return {
            "enabled": bool(orchestration.get("enabled")),
            "mode": orchestration.get("mode"),
            "workflow_kind": orchestration.get("workflow_kind"),
            "execution_style": orchestration.get("execution_style"),
            "isolated_worker_drafts": bool(orchestration.get("isolated_worker_drafts")),
            "phases": [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                }
                for item in (orchestration.get("phases") or [])[:5]
                if isinstance(item, dict)
            ],
            "workers": [
                {
                    "worker": item.get("worker"),
                    "ownership": item.get("ownership"),
                }
                for item in workers[:6]
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _compact_jsonish(value: Any, *, max_chars: int = 1000, max_items: int = 6, depth: int = 0) -> Any:
        if isinstance(value, str):
            return truncate_tool_text(value, max_chars=max_chars)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 3:
            return truncate_tool_text(json.dumps(value, ensure_ascii=False, default=str), max_chars=max_chars)
        if isinstance(value, list):
            compact = [
                WorkspaceCodeAgentRuntime._compact_jsonish(item, max_chars=max_chars, max_items=max_items, depth=depth + 1)
                for item in value[:max_items]
            ]
            if len(value) > max_items:
                compact.append({"truncated_items": len(value) - max_items})
            return compact
        if isinstance(value, dict):
            compact_dict: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= max_items:
                    compact_dict["truncated_keys"] = len(value) - max_items
                    break
                compact_dict[str(key)] = WorkspaceCodeAgentRuntime._compact_jsonish(
                    item,
                    max_chars=max_chars,
                    max_items=max_items,
                    depth=depth + 1,
                )
            return compact_dict
        return truncate_tool_text(str(value), max_chars=max_chars)

    @staticmethod
    def _browser_step_repair_needed(execution: CheckExecutionRecord) -> bool:
        return any(
            result.name == "browser_flow_smoke" and result.status == "failed"
            for result in execution.results
        )

    @staticmethod
    def _browser_step_repair_packet(execution: CheckExecutionRecord) -> dict[str, object]:
        for result in execution.results:
            if result.name != "browser_flow_smoke" or result.status != "failed":
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            packet = {
                key: WorkspaceCodeAgentRuntime._compact_jsonish(diagnostics.get(key), max_chars=700, max_items=5)
                for key in (
                    "failed_step",
                    "failed_role",
                    "failed_route",
                    "failed_selector",
                    "action",
                    "console_errors",
                    "visible_errors",
                    "api_before",
                    "api_after",
                    "ui_steps",
                    "mobile_overflow",
                    "screenshots",
                )
                if key in diagnostics
            }
            packet["details"] = truncate_tool_text(str(result.details or ""), max_chars=500)
            packet["logs"] = [truncate_tool_text(str(line), max_chars=500) for line in result.logs[-3:]]
            packet["repair_scope"] = (
                "Repair the real UI workflow step that failed in browser proof. "
                "Prefer the failed role HTML/JS/CSS files; touch backend only when the packet shows API state did not change."
            )
            return packet
        return {}

    @staticmethod
    def _browser_step_repair_focus(execution: CheckExecutionRecord) -> str:
        packet = WorkspaceCodeAgentRuntime._browser_step_repair_packet(execution)
        failed_step = packet.get("failed_step") or "unknown_step"
        failed_role = packet.get("failed_role") or "unknown_role"
        failed_route = packet.get("failed_route") or "unknown_route"
        failed_selector = packet.get("failed_selector") or "unknown_selector"
        return (
            "Focused browser-proof repair: fix only the failing real UI step "
            f"{failed_step!r} for role {failed_role!r} on route {failed_route!r} selector {failed_selector!r}. "
            "Patch the smallest relevant role UI/JS/CSS slice so the browser action changes persisted state, "
            "the affected role renders it after reload, and downstream roles observe it. "
            "Do not rebuild worker branches, add product templates, or rewrite unrelated files."
        )

    @staticmethod
    def _browser_step_repair_paths(execution: CheckExecutionRecord, *, diff_paths: list[str]) -> list[str]:
        failed_names = {str(result.name or "") for result in execution.results if result.status == "failed"}
        diagnostics: dict[str, Any] = {}
        details_blob = ""
        for result in execution.results:
            if result.name != "browser_flow_smoke" or result.status != "failed":
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            details_blob = " ".join(
                [
                    str(result.details or ""),
                    " ".join(str(line or "") for line in result.logs[-3:]),
                    json.dumps(diagnostics, ensure_ascii=False, default=str)[:1200],
                ]
            ).lower()
            break
        role = str(diagnostics.get("failed_role") or "").strip()
        route = str(diagnostics.get("failed_route") or "").strip()
        route_role_match = re.search(r"/(client|specialist|manager)(?:/|$)", route)
        if role not in ROLE_ORDER and route_role_match:
            role = route_role_match.group(1)
        roles: list[str] = [role] if role in ROLE_ORDER else []
        if not roles:
            for path in diff_paths:
                match = re.search(r"miniapp/app/static/(client|specialist|manager)/", str(path or ""))
                if match and match.group(1) not in roles:
                    roles.append(match.group(1))
            roles = roles[:1] or ["client"]

        ordered: list[str] = []
        for item_role in roles:
            ordered.extend(
                [
                    f"miniapp/app/static/{item_role}/index.html",
                    f"miniapp/app/static/{item_role}/app.js",
                    f"miniapp/app/static/{item_role}/styles.css",
                ]
            )

        api_failed = bool({"api_workflow_smoke", "connectivity_validators"} & failed_names)
        api_signal = any(term in details_blob for term in (" api", "/api", "fetch", "post", "patch", "put", "state did not", "state unchanged"))
        if api_failed or api_signal:
            ordered.extend(
                [
                    path
                    for path in diff_paths
                    if path.startswith("miniapp/app/routes/") and path.endswith(".py")
                    and path not in {"miniapp/app/routes/app_api.py", "miniapp/app/routes/role_pages.py", "miniapp/app/routes/role_routes.py"}
                ]
            )
            ordered.extend(["miniapp/app/routes/api.py", "miniapp/app/schemas.py"])
        if "generated_app_js_tests" in failed_names:
            ordered.append("miniapp/tests/generated_app.test.mjs")
        if "generated_app_python_tests" in failed_names or api_failed:
            ordered.append("miniapp/tests/test_generated_app.py")

        for path in diff_paths:
            normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            if normalized.startswith("miniapp/app/static/") and normalized.endswith((".html", ".js", ".css")):
                match = re.search(r"miniapp/app/static/(client|specialist|manager)/", normalized)
                if match and match.group(1) in roles:
                    ordered.append(normalized)

        compact: list[str] = []
        for path in ordered:
            normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            if normalized.startswith("miniapp/") and normalized not in compact:
                compact.append(normalized)
        return compact[:7]

    @staticmethod
    def _generated_tests_repair_needed(execution: CheckExecutionRecord) -> bool:
        return (
            WorkspaceCodeAgentRuntime._missing_generated_tests_repair_needed(execution)
            or WorkspaceCodeAgentRuntime._stale_generated_tests_repair_needed(execution)
        )

    @staticmethod
    def _stale_generated_tests_repair_needed(execution: CheckExecutionRecord) -> bool:
        for result in execution.results:
            if result.status != "failed" or result.name not in {"generated_app_python_tests", "generated_app_js_tests"}:
                continue
            details = str(result.details or "").lower()
            logs_blob = "\n".join(str(line or "") for line in result.logs or []).lower()
            if (
                "required" in details
                or "not present" in details
                or "missing_generated_app_tests" in logs_blob
                or not result.logs
            ):
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            if diagnostics.get("assertion_failures") or diagnostics.get("failing_test_location") or diagnostics.get("assertion_source"):
                return True
            if diagnostics.get("protected_platform_shell_import"):
                return True
            stale_markers = (
                "assertionerror",
                "cannot import name",
                "app.routes.health",
                "expected:",
                "actual:",
                "operator:",
                "diff:",
                "test failed",
                "fail:",
                "✖ failing tests",
            )
            if any(marker in logs_blob for marker in stale_markers):
                return True
        return False

    @staticmethod
    def _generated_test_repair_packet(execution: CheckExecutionRecord) -> dict[str, object]:
        failed: list[dict[str, object]] = []
        for result in execution.results:
            if result.status != "failed" or result.name not in {"generated_app_python_tests", "generated_app_js_tests"}:
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            failed.append(
                {
                    "name": result.name,
                    "details": truncate_tool_text(str(result.details or ""), max_chars=500),
                    "command": truncate_tool_text(str(result.command or ""), max_chars=220),
                    "exit_code": result.exit_code,
                    "diagnostics": WorkspaceCodeAgentRuntime._compact_jsonish(diagnostics, max_chars=900, max_items=6),
                    "logs": [truncate_tool_text(str(line), max_chars=700) for line in result.logs[-10:]],
                }
            )
        return {
            "failed_generated_tests": failed,
            "repair_scope": (
                "Generated acceptance tests must match the actual current app contract. "
                "Patch generated tests when their selectors/HTML expectations are stale; patch app code only when the failed assertion reveals a real behavior/API bug."
            ),
        }

    @staticmethod
    def _generated_test_repair_focus(execution: CheckExecutionRecord, *, missing: bool = False) -> str:
        failed_names = [
            str(result.name or "")
            for result in execution.results
            if result.status == "failed" and result.name in {"generated_app_python_tests", "generated_app_js_tests"}
        ]
        if missing:
            return (
                "Generated-test repair: create the missing generated Python and JS acceptance tests from the actual current routes, schemas, "
                "role HTML, and role JS. Do not invent a separate test-only contract. Python tests must not import product/reset helpers from "
                "platform shell modules such as app.routes.health, app.routes.role_pages, or app.routes.role_routes."
            )
        return (
            "Generated-test repair: fix stale generated acceptance tests before returning to browser proof. "
            f"Failed checks: {', '.join(failed_names) or 'generated tests'}. "
            "Compare the test expectations with the current app files in file_contexts. Prefer patching only miniapp/tests/* when tests assert old selectors, old class names, old labels, or old routes. "
            "Patch app code only if the assertion proves the actual workflow contract is broken. Python tests must not import product/reset helpers from platform shell modules; "
            "import from the app-owned route module that defines the resource or reset the generated DB after importing app-owned route models. Keep browser-flow repair for the next validation round."
        )

    @staticmethod
    def _generated_test_repair_paths(execution: CheckExecutionRecord, *, diff_paths: list[str]) -> list[str]:
        failed_names = {str(result.name or "") for result in execution.results if result.status == "failed"}
        ordered: list[str] = []
        if "generated_app_js_tests" in failed_names:
            ordered.append("miniapp/tests/generated_app.test.mjs")
        if "generated_app_python_tests" in failed_names:
            ordered.append("miniapp/tests/test_generated_app.py")

        static_paths = [
            path
            for path in diff_paths
            if path.startswith("miniapp/app/static/")
            and path.endswith((".html", ".js"))
        ]
        route_paths = [
            path
            for path in diff_paths
            if path.startswith("miniapp/app/routes/")
            and path.endswith(".py")
            and path not in {"miniapp/app/routes/app_api.py", "miniapp/app/routes/role_pages.py", "miniapp/app/routes/role_routes.py"}
        ]
        ordered.extend(static_paths[:9])
        ordered.extend(route_paths[:6])
        if "generated_app_python_tests" in failed_names or route_paths:
            ordered.extend(["miniapp/app/schemas.py", "miniapp/app/db.py"])

        compact: list[str] = []
        for path in ordered:
            normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            if normalized.startswith("miniapp/") and normalized not in compact:
                compact.append(normalized)
        return compact[:18]

    @staticmethod
    def _workflow_slice_repair_needed(execution: CheckExecutionRecord) -> bool:
        failed_results = [result for result in execution.results if result.status == "failed"]
        failed_names = {str(result.name or "") for result in failed_results}
        if not failed_names:
            return False
        workflow_checks = {"connectivity_validators", "frontend_interaction_static_smoke", "browser_flow_smoke"}
        has_workflow_check = bool(workflow_checks & failed_names)
        has_generated_check = bool({"generated_app_python_tests", "generated_app_js_tests"} & failed_names)
        generated_tests_disagree = {"generated_app_python_tests", "generated_app_js_tests"}.issubset(failed_names)
        has_route_or_static_contract = bool({"changed_files_static", "platform_invariants"} & failed_names)
        frontend_issue_count = 0
        route_schema_issue = False
        schema_payload_issue = False
        role_update_issue = False
        for result in failed_results:
            if result.name == "frontend_interaction_static_smoke":
                frontend_issue_count = len(result.logs or [])
            if result.name == "platform_invariants" and any("preflight.route_schema_contract" in str(line or "") for line in result.logs or []):
                route_schema_issue = True
            if result.name == "platform_invariants" and any("platform.frontend_missing_update_api" in str(line or "") for line in result.logs or []):
                role_update_issue = True
            if result.name == "generated_app_python_tests" and any(" 422" in str(line or "") or "422 !=" in str(line or "") or "!= 422" in str(line or "") for line in result.logs or []):
                schema_payload_issue = True
        return (
            (has_workflow_check and has_generated_check)
            or (has_route_or_static_contract and has_generated_check)
            or generated_tests_disagree
            or route_schema_issue
            or role_update_issue
            or schema_payload_issue
            or {"connectivity_validators", "frontend_interaction_static_smoke"}.issubset(failed_names)
            or "browser_flow_smoke" in failed_names
            or frontend_issue_count >= 3
        )

    @staticmethod
    def _missing_generated_tests_repair_needed(execution: CheckExecutionRecord) -> bool:
        for result in execution.results:
            if result.status != "failed":
                continue
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"} and (
                "required" in str(result.details or "").lower()
                or "not present" in str(result.details or "").lower()
                or not result.logs
            ):
                return True
            if result.name == "platform_invariants" and any(
                "missing_generated_app_tests" in str(line or "") for line in result.logs or []
            ):
                return True
        return False

    @staticmethod
    def _compact_repair_checks(execution: CheckExecutionRecord) -> list[dict[str, object]]:
        failed_results = [result for result in execution.results if result.status != "passed"]
        priority = {
            "changed_files_static": 0,
            "platform_invariants": 1,
            "frontend_interaction_static_smoke": 2,
            "browser_flow_smoke": 3,
            "generated_app_python_tests": 3,
            "generated_app_js_tests": 4,
            "connectivity_validators": 5,
        }
        source_results = (
            sorted(failed_results, key=lambda result: priority.get(str(result.name), 20))
            if failed_results
            else execution.results[-3:]
        )
        payload: list[dict[str, object]] = []
        for result in source_results[:5]:
            browser_failure_packet = {}
            if result.name == "browser_flow_smoke" and isinstance(result.diagnostics, dict):
                browser_failure_packet = {
                    key: WorkspaceCodeAgentRuntime._compact_jsonish(result.diagnostics.get(key), max_chars=900, max_items=6)
                    for key in (
                        "failed_step",
                        "failed_role",
                        "failed_route",
                        "failed_selector",
                        "action",
                        "console_errors",
                        "visible_errors",
                        "screenshots",
                        "api_before",
                        "api_after",
                        "ui_steps",
                    )
                    if key in result.diagnostics
                }
                diagnostics = browser_failure_packet
            else:
                diagnostics = WorkspaceCodeAgentRuntime._compact_jsonish(result.diagnostics, max_chars=900, max_items=6)
            payload.append(
                {
                    "name": result.name,
                    "status": result.status,
                    "details": truncate_tool_text(str(result.details or ""), max_chars=420),
                    "command": truncate_tool_text(str(result.command or ""), max_chars=180),
                    "exit_code": result.exit_code,
                    "logs": [truncate_tool_text(str(line), max_chars=700) for line in result.logs[-5:]],
                    "diagnostics": diagnostics,
                    "browser_failure_packet": browser_failure_packet,
                }
            )
        return payload

    @staticmethod
    def _compact_generated_test_repair_checks(execution: CheckExecutionRecord) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for result in execution.results:
            if result.status != "failed" or result.name not in {"generated_app_python_tests", "generated_app_js_tests"}:
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            payload.append(
                {
                    "name": result.name,
                    "status": result.status,
                    "details": truncate_tool_text(str(result.details or ""), max_chars=500),
                    "command": truncate_tool_text(str(result.command or ""), max_chars=220),
                    "exit_code": result.exit_code,
                    "logs": [truncate_tool_text(str(line), max_chars=1000) for line in result.logs[-12:]],
                    "diagnostics": WorkspaceCodeAgentRuntime._compact_jsonish(diagnostics, max_chars=1200, max_items=8),
                    "repair_boundary": (
                        "Only generated test failures are included in this turn. "
                        "Other API/browser failures are intentionally withheld until generated tests pass, so do not infer or chase them now."
                    ),
                }
            )
        return payload

    @staticmethod
    def _compact_preview_details(preview_details: dict[str, object]) -> dict[str, object]:
        if not isinstance(preview_details, dict):
            return {}
        compact: dict[str, object] = {}
        for key in (
            "status",
            "stage",
            "current_stage",
            "progress_percent",
            "preview_url",
            "error",
            "failure_reason",
            "preview_refresh_status",
        ):
            if key in preview_details:
                compact[key] = WorkspaceCodeAgentRuntime._compact_jsonish(preview_details.get(key), max_chars=700, max_items=4)
        for key in ("logs", "container_logs"):
            value = preview_details.get(key)
            if isinstance(value, list) and value:
                compact[key] = [truncate_tool_text(str(line), max_chars=500) for line in value[-4:]]
            elif isinstance(value, str) and value.strip():
                compact[key] = truncate_tool_text(value, max_chars=1200)
        return compact

    def _compact_tool_results(
        self,
        tool_results: list[dict[str, object]],
        *,
        max_items: int = 4,
        workspace_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, object]]:
        compact: list[dict[str, object]] = []
        for item in tool_results[-max_items:]:
            if isinstance(item, dict):
                compact.append(
                    self._compact_tool_result(
                        item,
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                )
        return compact

    def _compact_tool_result(
        self,
        item: dict[str, object],
        *,
        workspace_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        serialized = self._tool_result_json(item)
        should_spill = (
            bool(workspace_id)
            and bool(run_id)
            and len(serialized) > TOOL_RESULT_SPILL_THRESHOLD_CHARS
        )
        if not should_spill:
            return WorkspaceCodeAgentRuntime._compact_jsonish(item, max_chars=900, max_items=7)
        if self.run_compaction_service is not None:
            compact = self.run_compaction_service.microcompact_tool_result(
                workspace_id=str(workspace_id),
                run_id=str(run_id),
                tool_result=dict(item),
                serialized=serialized,
            )
            return {
                "tool": compact.get("tool"),
                "status": compact.get("status"),
                "persisted_output_ref": compact.get("microcompact_ref"),
                "microcompact_ref": compact.get("microcompact_ref"),
                "digest": compact.get("digest"),
                "original_chars": compact.get("original_chars"),
                "preview": compact.get("output"),
                "has_more": True,
            }
        report_key = self._store_large_tool_result(
            workspace_id=str(workspace_id),
            run_id=str(run_id),
            item=item,
            serialized=serialized,
        )
        if not report_key:
            return WorkspaceCodeAgentRuntime._compact_jsonish(item, max_chars=900, max_items=7)
        return {
            "tool": item.get("tool") or item.get("name") or item.get("type"),
            "status": item.get("status") or item.get("outcome"),
            "persisted_output_ref": report_key,
            "original_chars": len(serialized),
            "preview": truncate_tool_text(serialized, max_chars=1800),
            "has_more": True,
        }

    def _store_large_tool_result(
        self,
        *,
        workspace_id: str,
        run_id: str,
        item: dict[str, object],
        serialized: str,
    ) -> str | None:
        if getattr(self, "store", None) is None:
            return None
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        report_key = f"tool_result:{workspace_id}:{run_id}:{digest[:24]}"
        payload = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "digest": digest,
            "original_chars": len(serialized),
            "preview": truncate_tool_text(serialized, max_chars=2400),
            "tool_result": item,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._store_report(report_key, payload)
        except Exception as exc:
            logger.warning(
                "large_tool_result_store_failed workspace_id=%s run_id=%s error=%s",
                workspace_id,
                run_id,
                exc,
            )
            return None
        return report_key

    def _attach_context_pressure(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        prompt_payload: str,
        attempt: int,
        tool_round: int,
    ) -> str:
        try:
            payload = json.loads(prompt_payload)
        except json.JSONDecodeError:
            payload = {"raw_prompt": prompt_payload}
        artifact_run_id = run_id or job.job_id
        pressure = self.context_pressure.analyze_payload(payload)
        transcript_pressure = self.context_pressure.analyze_transcript(
            self.transcript_store.snapshot(artifact_run_id),
            current_file_contexts=payload.get("file_contexts") if isinstance(payload.get("file_contexts"), dict) else {},
        )
        if transcript_pressure.get("duplicate_file_reads"):
            pressure["duplicate_file_reads"] = transcript_pressure.get("duplicate_file_reads")
            pressure["duplicate_read_token_estimate"] = transcript_pressure.get("duplicate_read_token_estimate")
        transcript_suggestions = [
            item for item in transcript_pressure.get("suggestions") or [] if isinstance(item, dict)
        ]
        if transcript_suggestions:
            pressure["suggestions"] = [
                *[item for item in pressure.get("suggestions") or [] if isinstance(item, dict)],
                *transcript_suggestions,
            ]
            pressure["compact_recommended"] = True
        pressure.update({"attempt": attempt, "tool_round": tool_round, "created_at": datetime.now(timezone.utc).isoformat()})
        self.context_pressure_history.setdefault(artifact_run_id, []).append(pressure)
        job.context_pressure_ref = job.context_pressure_ref or f"context_pressure:{workspace_id}:{artifact_run_id}"
        self._store_report(
            job.context_pressure_ref,
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "items": self.context_pressure_history.get(artifact_run_id, [])[-200:],
            },
        )
        payload["context_pressure"] = {
            "total_tokens_estimate": pressure.get("total_tokens_estimate"),
            "pressure_ratio": pressure.get("pressure_ratio"),
            "compact_recommended": pressure.get("compact_recommended"),
            "suggestions": pressure.get("suggestions"),
            "duplicate_file_reads": pressure.get("duplicate_file_reads"),
        }
        if pressure.get("duplicate_file_reads"):
            payload["read_cache_hints"] = {
                "avoid_re_reading": [
                    item.get("path")
                    for item in pressure.get("duplicate_file_reads") or []
                    if isinstance(item, dict) and item.get("path")
                ][:8],
                "rule": "Use cached file_contexts/current diff for these paths unless they were mutated after the last read or a precise missing line range is required.",
            }
        if pressure.get("compact_recommended"):
            self._record_compact_boundary(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_run_id=artifact_run_id,
                pressure=pressure,
                attempt=attempt,
                tool_round=tool_round,
                reason="context_pressure",
            )
            self._append_activity(
                job,
                "context_suggestion",
                "Context pressure suggests compacting next turn",
                {
                    "phase": "compacting",
                    "status": "recommended",
                    "artifact_ref": job.context_pressure_ref,
                    "summary": "Use failing files, current diff, and proof packet instead of broad context.",
                    "total_tokens_estimate": pressure.get("total_tokens_estimate"),
                    "pressure_ratio": pressure.get("pressure_ratio"),
                },
                save=False,
            )
            self.rollout_trace.append(artifact_run_id, "context_suggestion", pressure)
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _with_post_compact_messages(user_prompt: str, messages: list[dict[str, Any]]) -> str:
        compact_messages: list[dict[str, Any]] = []
        for item in messages[-3:]:
            if not isinstance(item, dict):
                continue
            compact_messages.append(
                {
                    "boundary_id": item.get("boundary_id"),
                    "post_compact_message_ref": item.get("post_compact_message_ref") or item.get("ref"),
                    "status": item.get("status"),
                    "message": str(item.get("message") or "")[:12000],
                    "refs": item.get("refs") if isinstance(item.get("refs"), dict) else {},
                }
            )
        if not compact_messages:
            return user_prompt
        try:
            payload = json.loads(user_prompt)
            if isinstance(payload, dict):
                payload["post_compact_messages"] = compact_messages
                payload["post_compact_instruction"] = (
                    "Use post_compact_messages as the active continuity context for this turn. "
                    "They replace omitted prior tool results at compact boundaries."
                )
                return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
        return (
            f"{user_prompt}\n\n"
            "POST-COMPACT CONTEXT:\n"
            f"{json.dumps(compact_messages, ensure_ascii=False, default=str)}"
        )

    def _record_compact_boundary(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        artifact_run_id: str,
        pressure: dict[str, Any],
        attempt: int,
        tool_round: int,
        reason: str,
    ) -> None:
        if self.run_compaction_service is None or not run_id:
            return
        boundary_id = f"compact_{attempt}_{tool_round}_{hashlib.sha256(json.dumps(pressure, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()[:8]}"
        boundary_ref = f"run_compaction_boundary:{run_id}:{boundary_id}"
        if isinstance(self.store.get("reports", boundary_ref), dict):
            return
        run_payload = self.store.get("runs", run_id)
        if not isinstance(run_payload, dict):
            return
        try:
            run = RunRecord.model_validate(run_payload)
            checkpoint = self.store.get("reports", job.resume_checkpoint_ref) if job.resume_checkpoint_ref else {}
            artifacts = self.store.get("reports", f"run_artifacts:{run_id}") or {}
            compact_payload = self.run_compaction_service.compact_run(
                run=run,
                artifacts=artifacts if isinstance(artifacts, dict) else {},
                checkpoint=checkpoint if isinstance(checkpoint, dict) else {},
                context_pressure=pressure,
                reason=reason,
                source="autocompact",
                boundary_id=boundary_id,
            )
            reset = self.transcript_store.compact_model_context(
                artifact_run_id,
                reason=reason,
                preserve_response_id=True,
            )
            post_compact_message_ref = compact_payload.get("post_compact_message_ref")
            post_compact_message: dict[str, Any] | None = None
            if post_compact_message_ref:
                post_compact_message = self.store.get("reports", str(post_compact_message_ref))
                if isinstance(post_compact_message, dict):
                    self.transcript_store.append_post_compact_message(artifact_run_id, post_compact_message)
            job.compaction_summaries = [
                *list(job.compaction_summaries or []),
                {
                    "schema": "grounded.run_compaction_summary.v1",
                    "boundary_id": boundary_id,
                    "compaction_ref": compact_payload.get("compaction_ref"),
                    "post_compact_message_ref": post_compact_message_ref,
                    "reason": reason,
                    "pressure_ratio": pressure.get("pressure_ratio"),
                    "pending_tool_result_count": reset.get("pending_tool_result_count"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            ][-20:]
            self._store_transcript_snapshot(job, artifact_run_id)
            self._append_event(
                job,
                "compact_boundary",
                "Run context compacted at context-pressure boundary.",
                {
                    "boundary_id": boundary_id,
                    "artifact_ref": compact_payload.get("compaction_ref"),
                    "boundary_ref": compact_payload.get("boundary_ref"),
                    "post_compact_message_ref": post_compact_message_ref,
                    "reason": reason,
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "pressure_ratio": pressure.get("pressure_ratio"),
                    "pending_tool_result_count": reset.get("pending_tool_result_count"),
                    "previous_response_id": reset.get("previous_response_id"),
                },
            )
            self._store_resume_checkpoint(
                job,
                artifact_run_id,
                phase="compact_boundary",
                extra={
                    "compaction_ref": compact_payload.get("compaction_ref"),
                    "compact_boundary_ref": compact_payload.get("boundary_ref"),
                    "compact_boundary_id": boundary_id,
                    "post_compact_message_ref": post_compact_message_ref,
                    "context_pressure": pressure,
                    "pending_tool_result_count": reset.get("pending_tool_result_count"),
                    "pending_post_compact_count": 1 if post_compact_message else 0,
                    "previous_response_id": reset.get("previous_response_id"),
                    "latest_check_ref": f"latest_check_execution:{run_id}",
                    "budget_status": job.budget_status,
                    "completion_budget": job.completion_budget,
                },
            )
        except Exception:
            logger.exception("run_compaction_boundary_failed workspace_id=%s run_id=%s", workspace_id, run_id)

    def _record_hook(
        self,
        *,
        job: JobRecord,
        hook: str,
        status: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> None:
        artifact_run_id = job.linked_run_id or job.job_id
        outcome_payload: dict[str, Any] | None = None
        try:
            if status == "started":
                event = self.hook_manager.record(artifact_run_id, hook, status=status, payload=payload or {})  # type: ignore[arg-type]
            else:
                outcome = self.hook_manager.run(artifact_run_id, hook, payload={**dict(payload or {}), "requested_status": status})  # type: ignore[arg-type]
                event = outcome.event
                outcome_payload = outcome.as_dict()
                if outcome.should_block:
                    status = "failed"
        except Exception:
            return
        job.hook_trace_ref = job.hook_trace_ref or f"hook_trace:{job.workspace_id}:{artifact_run_id}"
        hook_report = {
            "workspace_id": job.workspace_id,
            "run_id": job.linked_run_id,
            **self.hook_manager.snapshot(artifact_run_id),
            "activity_hooks": [
                item
                for item in job.agent_activity_events
                if str(item.get("type") or "") in {"hook_started", "hook_completed"}
            ][-500:],
        }
        if outcome_payload is not None:
            hook_report["latest_outcome"] = outcome_payload
        self._store_report(job.hook_trace_ref, hook_report)
        self._append_activity(
            job,
            "hook_completed" if status != "started" else "hook_started",
            f"{hook.replace('_', ' ')} {status}",
            {
                "hook": hook,
                "status": status,
                "artifact_ref": job.hook_trace_ref,
                "outcome": outcome_payload,
                **dict(payload or {}),
            },
            save=False,
        )
        self.rollout_trace.append(artifact_run_id, f"hook_{hook}", event)

    @staticmethod
    def _tool_result_json(item: dict[str, object]) -> str:
        try:
            return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return json.dumps(
                WorkspaceCodeAgentRuntime._compact_jsonish(item, max_chars=4000, max_items=20),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )

    @staticmethod
    def _compact_repair_rules(
        *,
        generation_mode: GenerationMode,
        focused_edit_kind: str,
        workflow_slice_repair: bool = False,
        missing_generated_tests_repair: bool = False,
        stale_generated_tests_repair: bool = False,
        browser_step_repair: bool = False,
        repeated_no_progress: int = 0,
    ) -> list[str]:
        del generation_mode, focused_edit_kind
        rules = [
            "This is a compact code-agent repair turn for the current draft. Do not rebuild the whole app unless the failing file is missing.",
            "Read the latest check/browser diagnostics as the source of truth, patch the smallest connected slice, then let validation run again.",
            "Prefer apply_patch_to_draft for existing files. Use write_file only for a new/missing/tiny file or after a repeated apply conflict on that same file.",
            "Keep the app prompt-owned and prompt-derived: do not introduce platform templates, seed/demo/mock product records, or fixed resource/page names.",
            "For create/workflow failures, keep HTML controls, JavaScript handlers, API routes/schemas, persistence, and generated tests aligned to one actual contract.",
            "Browser-flow failures are product failures: make the UI action change persisted state, make other roles observe it, and make reload preserve it.",
            "A role that creates shared state must render the persisted fields relevant to its contract. If the contract assigns later changes to another role, those changes must become visible to the relevant roles after refresh.",
            "Generated tests should verify the actual app contract. Python generated tests must be unittest-discoverable: import unittest, define a unittest.TestCase subclass, and put assertions inside test_* methods; use FastAPI TestClient as a context manager when app lifespan creates tables; if reading a JSON store file, normalize dict envelopes with `raw.get('items', raw)` before asserting count; never replace tests with pytest-only top-level functions. JS generated tests run from cwd=miniapp, so path reads should be app/static/... and app/generated/.... Node has no browser DOM: do not import browser-only role app.js files unless the test creates explicit window/document mocks first; prefer source-text assertions for selectors, API calls, routes, and handlers. Patch stale/brittle test expectations only when the app behavior is already correct.",
            "Generated Python tests must not import product/reset helpers from platform shell modules such as app.routes.health, app.routes.role_pages, or app.routes.role_routes; import from the app-owned route module or reset the generated DB after importing app-owned route models.",
            "Mobile layout fixes must target 360-430px width: no horizontal scroll, no overlapping critical cards/forms/actions, and readable wrapping.",
            "For multi-page role apps, a shared static/<role>/app.js must be view-aware: branch by body[data-view] or route, guard optional DOM nodes that exist only on other pages, and bind every visible form/button/control on root and child pages.",
        ]
        if repeated_no_progress > 0:
            rules.append(
                "This failure signature repeated. Do not patch broad slices or generated tests first. Patch only the first blocking source issue from first_blocking_issue, preferably by replacing the one failing role app.js or child-page HTML file when a hunk patch keeps missing the handler."
            )
        if workflow_slice_repair and not browser_step_repair:
            rules.append(
                "Repair the complete connected workflow slice in one focused patch: relevant backend route/schema, one or more role HTML/JS/CSS files, and generated tests if they are stale."
            )
        if browser_step_repair:
            rules.append(
                "This is a browser-step repair, not a rebuild. Use one small patch for the failed role UI/JS/CSS; only touch backend/schema when the browser packet shows API state did not change. Do not patch generated tests unless they are stale relative to working app behavior."
            )
        if missing_generated_tests_repair:
            rules.append(
                "Create missing generated Python and JS tests from the actual current routes, selectors, schemas, and role files; Python tests must be unittest-discoverable with a unittest.TestCase subclass, not pytest-only top-level functions, and use TestClient context manager when lifespan initializes tables. JS tests run from cwd=miniapp and must use app/... relative paths. Do not invent a separate test-only contract."
            )
        if stale_generated_tests_repair:
            rules.append(
                "This turn is for stale generated acceptance tests. Compare miniapp/tests/* expectations against current role HTML/JS and backend routes. Prefer patching only generated tests when they assert old selectors, class names, labels, paths, or API shapes; patch app code only if the test failure proves a real workflow bug. Do not chase browser proof in the same turn."
            )
        return rules

    @staticmethod
    def _first_blocking_issue_from_execution(execution: CheckExecutionRecord) -> dict[str, object]:
        priority = {
            "changed_files_static": 0,
            "connectivity_validators": 1,
            "platform_invariants": 2,
            "frontend_interaction_static_smoke": 3,
            "browser_flow_smoke": 4,
            "generated_app_python_tests": 5,
            "generated_app_js_tests": 6,
        }
        for result in sorted(
            [item for item in execution.results if item.status == "failed"],
            key=lambda item: priority.get(str(item.name), 20),
        ):
            for line in result.logs or []:
                try:
                    issue = json.loads(str(line))
                except ValueError:
                    issue = None
                if not isinstance(issue, dict):
                    continue
                blocking = bool(issue.get("blocking")) or str(issue.get("severity") or "").lower() in {"high", "critical"}
                if not blocking:
                    continue
                location = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(
                    str(issue.get("location") or issue.get("path") or issue.get("file_path") or "")
                )
                role_match = re.search(r"miniapp/app/static/(?P<role>client|specialist|manager)/", location)
                role = role_match.group("role") if role_match else ""
                code = str(issue.get("code") or "")
                next_action = (
                    "Patch the named role UI source, not generated tests: make the role app.js page-aware, reference the visible form/control from the child page, attach the needed submit/click/change handler, send the persisted API payload, refresh rendered state, and guard optional DOM nodes from other pages."
                    if code.startswith("platform.workflow_")
                    else "Patch the named source file or directly connected source file before changing generated tests."
                )
                return {
                    "check": result.name,
                    "code": code,
                    "message": str(issue.get("message") or "")[:1000],
                    "location": location,
                    "role": role,
                    "required_next_action": next_action,
                }
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            location = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(
                str(diagnostics.get("location") or diagnostics.get("path") or diagnostics.get("file_path") or "")
            )
            diagnostic_code = str(diagnostics.get("code") or result.name)
            for nested_key in ("static_js_syntax_error", "python_name_error"):
                nested = diagnostics.get(nested_key)
                if isinstance(nested, dict) and nested.get("file_path"):
                    location = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(str(nested.get("file_path") or ""))
                    diagnostic_code = nested_key
                    break
            if location:
                return {
                    "check": result.name,
                    "code": diagnostic_code,
                    "message": str(result.details or "")[:1000],
                    "location": location,
                    "required_next_action": "Patch the named source file and the smallest connected workflow slice.",
                }
        return {}

    @staticmethod
    def _target_files_from_execution(execution: CheckExecutionRecord) -> list[str]:
        paths: list[str] = []
        for result in execution.results:
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            candidates = [
                diagnostics.get("file_path"),
                diagnostics.get("path"),
                diagnostics.get("location"),
            ]
            for diagnostic_key in ("static_js_syntax_error", "python_name_error"):
                diagnostic_payload = diagnostics.get(diagnostic_key)
                if isinstance(diagnostic_payload, dict):
                    candidates.append(diagnostic_payload.get("file_path"))
            if result.name == "generated_app_python_tests":
                candidates.append("miniapp/tests/test_generated_app.py")
                candidates.extend(
                    [
                        "miniapp/app/routes/api.py",
                        "miniapp/app/schemas.py",
                        "miniapp/app/db.py",
                    ]
                )
            if result.name == "generated_app_js_tests":
                candidates.append("miniapp/tests/generated_app.test.mjs")
            if result.name == "browser_flow_smoke":
                candidates.extend(
                    [
                        "miniapp/app/routes/api.py",
                        "miniapp/app/schemas.py",
                        "miniapp/app/db.py",
                        "miniapp/tests/test_generated_app.py",
                        "miniapp/tests/generated_app.test.mjs",
                    ]
                )
                failed_role = str(diagnostics.get("failed_role") or "").strip()
                roles = [failed_role] if failed_role in ROLE_ORDER else list(ROLE_ORDER)
                for role in roles:
                    candidates.extend(
                        [
                            f"miniapp/app/static/{role}/index.html",
                            f"miniapp/app/static/{role}/app.js",
                            f"miniapp/app/static/{role}/styles.css",
                        ]
                    )
            for issue in diagnostics.get("issues") or []:
                if isinstance(issue, dict):
                    candidates.extend([issue.get("location"), issue.get("path"), issue.get("file_path")])
            for line in result.logs or []:
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    candidates.extend([payload.get("location"), payload.get("path"), payload.get("file_path")])
                    code = str(payload.get("code") or "")
                    location = str(payload.get("location") or payload.get("path") or payload.get("file_path") or "").replace("\\", "/")
                    if code == "platform.frontend_missing_update_api" or location.rstrip("/") == "miniapp/app/static":
                        for role in ROLE_ORDER:
                            candidates.extend(
                                [
                                    f"miniapp/app/static/{role}/index.html",
                                    f"miniapp/app/static/{role}/app.js",
                                    f"miniapp/app/static/{role}/styles.css",
                                ]
                            )
                        candidates.extend(
                            [
                                "miniapp/app/routes/api.py",
                                "miniapp/app/schemas.py",
                                "miniapp/tests/test_generated_app.py",
                                "miniapp/tests/generated_app.test.mjs",
                            ]
                        )
                    role_match_from_location = re.search(r"miniapp/app/static/(?P<role>client|specialist|manager)/", location)
                    if code.startswith("platform.workflow_") and role_match_from_location:
                        role = role_match_from_location.group("role")
                        candidates.extend(
                            [
                                f"miniapp/app/static/{role}/app.js",
                                "miniapp/app/schemas.py",
                                "miniapp/app/routes/api.py",
                            ]
                        )
                for static_match in re.finditer(
                    r"readStatic\(\s*([\"'])(?P<path>(?:client|specialist|manager)/[^\"']+\.(?:html|js|css))\1",
                    str(line or ""),
                ):
                    candidates.append(f"miniapp/app/static/{static_match.group('path')}")
                role_match = re.search(r"\bconst\s+role\s*=\s*([\"'])(?P<role>client|specialist|manager)\1", str(line or ""))
                if role_match:
                    role = role_match.group("role")
                    candidates.append(f"miniapp/app/static/{role}/app.js")
                for match in re.finditer(r"(?:^|[/\\])(?P<path>miniapp[/\\][A-Za-z0-9_./\\-]+\.(?:py|js|mjs|html|css|json))", str(line or "")):
                    candidates.append(match.group("path"))
            for candidate in candidates:
                path = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(candidate)
                if path and path.startswith("miniapp/") and path not in paths:
                    paths.append(path)
        return paths[:16]

    def _execute_tool_calls(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_calls: list[dict[str, Any]],
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
        job: JobRecord | None = None,
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        def append_activity(kind: str, label: str, details: dict[str, Any] | None = None) -> None:
            if job is None:
                return
            self._append_activity(job, kind, label, details or {}, save=False)

        def append_batch_summary(summary: dict[str, object]) -> None:
            artifact_run_id = run_id or (job.job_id if job is not None else run_id)
            self.tool_batch_summaries.setdefault(artifact_run_id, []).append(summary)
            if job is not None:
                job.tool_batch_summaries_ref = f"tool_batch_summaries:{workspace_id}:{artifact_run_id}"
                self._store_report(
                    job.tool_batch_summaries_ref,
                    {
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "items": self.tool_batch_summaries.get(artifact_run_id, [])[-500:],
                    },
                )
            self.rollout_trace.append(artifact_run_id, "tool_batch", dict(summary))

        loaded_context, tool_results = self.tool_executor.execute(
            workspace_id=workspace_id,
            run_id=run_id,
            draft_source=draft_source,
            tool_calls=tool_calls,
            execute_checks=execute_checks,
            append_activity=append_activity if job is not None else None,
            append_batch_summary=append_batch_summary,
        )
        if job is not None:
            artifact_run_id = run_id or job.job_id
            self.transcript_store.append_tool_results(artifact_run_id, tool_results)
            for item in tool_results:
                if not isinstance(item, dict):
                    continue
                success = item.get("success")
                status = str(item.get("status") or item.get("semantic_status") or "").lower()
                exit_code = item.get("exit_code")
                failed = success is False or status in {"failed", "blocked", "forbidden", "error"} or (isinstance(exit_code, int) and exit_code != 0)
                if failed:
                    self._record_rollout_trace(
                        job,
                        artifact_run_id=artifact_run_id,
                        event_type="tool_failed_reason",
                        payload={
                            "tool": item.get("tool"),
                            "tool_use_id": item.get("tool_use_id"),
                            "status": status or item.get("status"),
                            "semantic_status": item.get("semantic_status"),
                            "exit_code": exit_code,
                            "policy_decision": item.get("policy_decision"),
                            "error": item.get("error") or item.get("stderr_tail") or item.get("details"),
                            "command": item.get("command"),
                        },
                    )
            completed_ids = {str(item.get("tool_use_id") or "") for item in tool_results if isinstance(item, dict)}
            if completed_ids and job.active_tool_uses:
                job.active_tool_uses = [
                    {
                        **item,
                        "status": "completed" if str(item.get("tool_use_id") or "") in completed_ids else item.get("status", "requested"),
                    }
                    for item in job.active_tool_uses
                ]
            job.active_processes = list(self.process_manager.snapshot().get("active_processes") or [])
            self._store_transcript_snapshot(job, artifact_run_id)
            self._store_resume_checkpoint(
                job,
                artifact_run_id,
                phase="tool_results",
                extra={"tool_result_count": len(tool_results)},
            )
        return loaded_context, tool_results

    def _store_resume_checkpoint(
        self,
        job: JobRecord,
        artifact_run_id: str,
        *,
        phase: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        job.resume_checkpoint_ref = job.resume_checkpoint_ref or f"resume_checkpoint:{job.workspace_id}:{artifact_run_id}"
        coordinator = self.coordinators.get(artifact_run_id)
        scratchpad = self.scratchpads.get(artifact_run_id)
        repair_state_ref = f"repair_state:{job.workspace_id}:{artifact_run_id}"
        diagnostics_delta_ref = f"diagnostics_delta:{job.workspace_id}:{artifact_run_id}"
        repair_state = self.store.get("reports", repair_state_ref)
        repair_packets = [
            item
            for item in (list((repair_state or {}).get("latest_repair_packets") or []) if isinstance(repair_state, dict) else [])
            if isinstance(item, dict)
        ]
        try:
            latest_diff_summary = self.workspace_service.diff(job.workspace_id, run_id=artifact_run_id)[:4000]
        except Exception:
            latest_diff_summary = ""
        checkpoint = {
            "workspace_id": job.workspace_id,
            "run_id": job.linked_run_id,
            "job_id": job.job_id,
            "phase": phase,
            "status": job.status,
            "current_stage": getattr(job, "current_stage", None) or job.current_fix_phase,
            "agent_transcript_ref": job.agent_transcript_ref,
            "tool_result_messages_ref": job.tool_result_messages_ref,
            "scratchpad_ref": job.scratchpad_ref,
            "file_change_history_ref": job.file_change_history_ref,
            "replay_trace_ref": job.replay_trace_ref,
            "todo_plan": coordinator.snapshot().get("todo_plan", []) if coordinator else [],
            "scratchpad": scratchpad.snapshot() if scratchpad else {},
            "process_summary": self.process_recovery.checkpoint(self.process_manager.snapshot()),
            "repair_state_ref": repair_state_ref,
            "repair_packets": repair_packets,
            "last_edit_failure_packets": [
                item
                for item in repair_packets
                if str(item.get("failure_class") or "").startswith("generation.invalid_edit_operation")
            ],
            "next_forced_action": dict((repair_state or {}).get("next_forced_action") or {}) if isinstance(repair_state, dict) else {},
            "failure_signatures": [
                str(item.get("failure_signature") or item.get("signature") or "")
                for item in repair_packets
            ],
            "repair_attempts": int(
                max(
                    [int(item.get("repeated_count") or 0) for item in repair_packets]
                    or [0]
                )
            ),
            "latest_diff_summary": latest_diff_summary,
            "diagnostics_delta_ref": diagnostics_delta_ref,
            "diagnostics_delta": self.store.get("reports", diagnostics_delta_ref),
            "file_state_cache_ref": job.file_state_cache_ref,
            "compaction_ref": f"run_compaction:{artifact_run_id}",
            "compact_boundaries_ref": f"run_compaction_boundaries:{artifact_run_id}",
            "latest_check_ref": f"latest_check_execution:{artifact_run_id}",
            "budget_status": job.budget_status,
            "completion_budget": job.completion_budget,
            "previous_response_id": self.transcript_store.snapshot(artifact_run_id).get("last_response_id"),
            "pending_tool_result_count": self.transcript_store.snapshot(artifact_run_id).get("pending_tool_result_count"),
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        checkpoint.update(extra or {})
        self._store_report(job.resume_checkpoint_ref, checkpoint)

    def _configure_transcript_persistence(
        self,
        job: JobRecord,
        artifact_run_id: str,
        *,
        restore_existing: bool = False,
    ) -> None:
        job.agent_transcript_ref = job.agent_transcript_ref or f"agent_transcript:{job.workspace_id}:{artifact_run_id}"
        job.tool_result_messages_ref = job.tool_result_messages_ref or f"tool_result_messages:{job.workspace_id}:{artifact_run_id}"
        if self.transcript_store.is_configured(artifact_run_id):
            return
        existing = self.store.get("reports", job.agent_transcript_ref) if restore_existing else None

        def write(snapshot: dict[str, Any]) -> None:
            self._store_report(
                job.agent_transcript_ref or f"agent_transcript:{job.workspace_id}:{artifact_run_id}",
                {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **snapshot},
            )
            self._store_report(
                job.tool_result_messages_ref or f"tool_result_messages:{job.workspace_id}:{artifact_run_id}",
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    "items": snapshot.get("all_tool_result_messages", []),
                    "pending_items": snapshot.get("tool_result_messages", []),
                    "transcript_ref": job.agent_transcript_ref,
                },
            )

        self.transcript_store.configure_persistence(
            artifact_run_id,
            writer=write,
            existing=existing if isinstance(existing, dict) else None,
        )
        if self.run_compaction_service is not None:
            self.transcript_store.configure_microcompact(
                artifact_run_id,
                writer=lambda result, serialized: self.run_compaction_service.microcompact_tool_result(
                    workspace_id=job.workspace_id,
                    run_id=artifact_run_id,
                    tool_result=result,
                    serialized=serialized,
                ),
            )

    def _store_transcript_snapshot(self, job: JobRecord, artifact_run_id: str) -> None:
        self._configure_transcript_persistence(job, artifact_run_id)
        snapshot = self.transcript_store.snapshot(artifact_run_id)
        self._store_report(
            job.agent_transcript_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **snapshot},
        )
        self._store_report(
            job.tool_result_messages_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": snapshot.get("all_tool_result_messages", []),
                "pending_items": snapshot.get("tool_result_messages", []),
                "transcript_ref": job.agent_transcript_ref,
            },
        )

    @staticmethod
    def _agent_turn_tuning(
        generation_mode: GenerationMode,
        *,
        intent: str = "",
        focused_edit_kind: str = "",
        repair_turn: bool = False,
        generated_tests_repair: bool = False,
        browser_step_repair: bool = False,
    ) -> dict[str, Any]:
        if generated_tests_repair:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 12000}
        if browser_step_repair:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 7000}
        if focused_edit_kind == "visual_style_edit":
            return {"reasoning": {"effort": "low"}, "max_output_tokens": FOCUSED_VISUAL_CONTENT_MAX_LENGTH}
        if focused_edit_kind in {"small_copy_edit", "behavior_edit"}:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 12000}
        if str(intent or "").lower() in {"edit", "refine", "role_only_change"}:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 14000}
        if str(intent or "").lower() == "create":
            if repair_turn:
                if generation_mode == GenerationMode.FAST:
                    return {"reasoning": {"effort": "low"}, "max_output_tokens": 18000}
                if generation_mode == GenerationMode.QUALITY:
                    return {"reasoning": {"effort": "low"}, "max_output_tokens": 26000}
                return {"reasoning": {"effort": "low"}, "max_output_tokens": 22000}
            if generation_mode == GenerationMode.FAST:
                return {"reasoning": {"effort": "low"}, "max_output_tokens": 28000}
            if generation_mode == GenerationMode.QUALITY:
                return {"reasoning": {"effort": "high"}, "max_output_tokens": 52000}
            return {
                "reasoning": {"effort": "medium"},
                "max_output_tokens": 36000,
            }
        if generation_mode == GenerationMode.FAST:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 24000}
        if generation_mode == GenerationMode.QUALITY:
            return {"reasoning": {"effort": "high"}, "max_output_tokens": 42000}
        return {"reasoning": {"effort": "medium"}, "max_output_tokens": 32000}

    @staticmethod
    def _is_output_cap_error(error: str) -> bool:
        text = str(error or "").lower()
        return "max_output_token" in text or "max_output_tokens" in text or "output cap" in text or "too large to return as valid json" in text

    @staticmethod
    def _is_context_length_error(error: str) -> bool:
        text = str(error or "").lower()
        return "context_length_exceeded" in text or "exceeds the context window" in text or "context window" in text

    @staticmethod
    def _context_length_correction_result(payload: dict[str, Any], *, request: GenerateRequest) -> dict[str, object]:
        return {
            "tool": "context_length_correction",
            "tool_use_id": "context_length_correction",
            "status": "failed",
            "previous_error": str(payload.get("error") or "")[:1200],
            "required_next_action": (
                "The previous request exceeded the context window. Use only the compact failure packet, current diff summary, "
                "and exact failing files. Call one small read_files/search_files or one small apply_patch_to_draft/write_file; "
                "do not request broad workspace context or rewrite unrelated app slices."
            ),
            "intent": str(request.intent or ""),
        }

    @staticmethod
    def _output_cap_correction_result(payload: dict[str, Any], *, request: GenerateRequest) -> dict[str, object]:
        create_task = str(request.intent or "").lower() == "create"
        if create_task:
            next_action = (
                "The previous answer was too large. Call one very small apply_patch_to_draft or write_file tool now. "
                "If this is a repair for an existing draft, patch the smallest coherent failing workflow slice and touch only the concrete failed files/checks; do not rebuild the whole app. "
                "If this is the first implementation, write a compact but complete prompt-derived app surface, avoid inline styles and long comments, and do not request more context unless a specific required file is absent. "
                "Keep the app working, not static-only: align backend API routes, form/fetch frontend code, and generated tests around the same persisted fields. "
                "Do not add mock data, seed data, demo data, sample data, fixture records, preloaded records, or hard-coded product records."
            )
        else:
            next_action = (
                "The previous answer was too large. Call 1-2 focused apply_patch_to_draft/write_file tools for the requested edit. "
                "Prefer write_file for the single visible role file that needs the change instead of fragile multi-file hunks. "
                "Do not request more context unless a required file is absent."
            )
        return {
            "tool": "output_cap_correction",
            "contract": "The model step exceeded the output cap; tools cannot recover this automatically.",
            "required_next_action": next_action,
            "previous_error": str(payload.get("error") or "")[:1200],
        }

    @staticmethod
    def _tool_budget_correction_result(tool_calls: list[dict[str, Any]], *, request: GenerateRequest) -> dict[str, object]:
        create_task = str(request.intent or "").strip().lower() == "create"
        return {
            "tool": "tool_budget_correction",
            "contract": "This agent turn has reached its diagnostic tool budget. Continue with the context and validation packet already provided.",
            "required_next_action": (
                "Call apply_patch_to_draft or write_file now. Do not request more read-only tools in the next answer. "
                "Use the file_contexts and latest_checks already provided. "
                + (
                    "For create, produce compact writes that advance the prompt-derived app contract across backend, role UI, frontend behavior, route manifest when needed, and generated tests. Keep HTML concise and do not include large inline style blocks or preloaded product records."
                    if create_task
                    else "For edit/fix, patch the smallest complete set of files needed for the requested behavior."
                )
            ),
            "ignored_tool_calls": [
                {
                    "tool": str(item.get("tool") or ""),
                    "targets": [str(target) for target in item.get("targets") or []] if isinstance(item, dict) else [],
                    "reason": str(item.get("reason") or "")[:400] if isinstance(item, dict) else "",
                }
                for item in tool_calls
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _focused_visual_edit_budget_result(*, role_scope: list[str]) -> dict[str, object]:
        return {
            "tool": "focused_visual_edit_budget",
            "contract": (
                "This is a small visual/style edit. It must use the focused CSS lane instead of the full create/workflow "
                "generation loop."
            ),
            "focused_edit_files": WorkspaceCodeAgentRuntime._focused_visual_css_paths(role_scope),
            "required_next_action": (
                "Use apply_patch_to_draft or write_file for 1-4 CSS-only edits. Prefer write_file with the full resulting CSS file "
                "when a hunk patch is ambiguous. Do not edit backend, JavaScript, HTML, route manifests, generated tests, "
                "or docs. Do not request tools when the listed CSS files are already provided in file_contexts."
            ),
        }

    @staticmethod
    def _is_self_blocked_tool_contract_response(payload: dict[str, Any]) -> bool:
        if payload.get("tool_calls"):
            return False
        text = " ".join(
            str(payload.get(key) or "")
            for key in ("assistant_message", "diagnosis", "expected_verification")
        ).lower()
        tool_markers = (
            "run_checks",
            "apply_patch",
            "tool",
            "script",
            "python",
            "command",
            "shell",
            "write",
            "apply",
            "edit",
            "rewrite",
            "file changes",
            "инструмент",
            "редакт",
            "запис",
            "файл",
        )
        blocked_markers = (
            "cannot",
            "could not",
            "unable",
            "never returned",
            "no response",
            "did not produce",
            "without the ability",
            "no more",
            "not provided",
            "not recognized",
            "unrecognized",
            "нельзя",
            "невозможно",
            "не могу",
            "нужен доступ",
            "требуется",
            "без возможности",
        )
        return any(marker in text for marker in tool_markers) and any(marker in text for marker in blocked_markers)

    @staticmethod
    def _tool_contract_correction_result(payload: dict[str, Any]) -> dict[str, object]:
        return {
            "tool": "tool_contract_correction",
            "contract": "Read-only tools inspect context/checks. apply_patch_to_draft and write_file are the only write tools and are validated before apply.",
            "required_next_action": "Call apply_patch_to_draft/write_file for concrete files, or request read-only context only if specific files are still missing.",
            "previous_outcome": str(payload.get("outcome") or ""),
            "previous_diagnosis": str(payload.get("diagnosis") or payload.get("assistant_message") or "")[:1200],
        }

    @staticmethod
    def _is_empty_fatal_agent_response(payload: dict[str, Any]) -> bool:
        if payload.get("tool_calls"):
            return False
        text = " ".join(str(payload.get(key) or "") for key in ("assistant_message", "diagnosis")).strip().lower()
        return (
            not text
            or "no analysis performed" in text
            or "can't help with that" in text
            or "can’t help with that" in text
            or "cannot help with that" in text
            or "not able to help with that" in text
            or "unable to help with that" in text
            or "cannot generate the required response" in text
            or "can't generate the required response" in text
            or "can’t generate the required response" in text
            or "missing or malformed" in text
            or "please rerun" in text
            or "please re-run" in text
            or "could you please rerun" in text
            or "need to inspect" in text
            or "i need to inspect" in text
        )

    @staticmethod
    def _empty_fatal_correction_result(payload: dict[str, Any]) -> dict[str, object]:
        return {
            "tool": "fatal_response_correction",
            "contract": "The user is asking for ordinary workspace code generation/editing. This is allowed platform work.",
            "required_next_action": "Do not return fatal_invalid_response without a concrete blocker. Use apply_patch_to_draft/write_file or request read-only context.",
            "previous_outcome": str(payload.get("outcome") or ""),
            "previous_message": str(payload.get("diagnosis") or payload.get("assistant_message") or "")[:1200],
        }

    @staticmethod
    def _agent_tool_calls(raw_tool_calls: list[Any]) -> list[dict[str, Any]]:
        allowed_tools = {
            "list_files",
            "read_files",
            "search_files",
            "inspect_diff",
            "read_artifact_ref",
            "semantic_scan",
            "run_checks",
            "run_command",
            "apply_patch_to_draft",
            "write_file",
            "browser_verify",
        }
        return [
            item
            for item in normalize_tool_calls(raw_tool_calls)
            if str(item.get("tool") or "").strip().lower() in allowed_tools
        ]

    @staticmethod
    def _is_mutating_agent_tool_call(request_item: dict[str, Any]) -> bool:
        return is_mutating_agent_tool_call(request_item)

    def _file_changes_from_mutating_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        draft_source: Path | None = None,
        run_id: str = "",
    ) -> tuple[list[DraftAction], list[dict[str, object]]]:
        def read_text_file(relative_path: str) -> str | None:
            if draft_source is None:
                return None
            normalized = str(relative_path or "").replace("\\", "/")
            if not normalized.startswith("miniapp/") or normalized.startswith(("/", "~")) or ".." in normalized.split("/"):
                return None
            try:
                return (draft_source / normalized).read_text(encoding="utf-8")
            except OSError:
                return None

        return file_changes_from_mutating_tool_calls(
            tool_calls,
            read_text_file=read_text_file if draft_source is not None else None,
            file_freshness=(
                lambda relative_path: self.file_state_cache.freshness(
                    run_id=run_id,
                    root=draft_source,
                    path=relative_path,
                )
            )
            if draft_source is not None
            else None,
            find_similar_path=(lambda relative_path: self._find_similar_draft_path(draft_source, relative_path)) if draft_source is not None else None,
        )

    @staticmethod
    def _find_similar_draft_path(draft_source: Path, requested_path: str) -> str | None:
        normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(requested_path)
        requested_name = Path(normalized).name
        if not requested_name:
            return None
        root = draft_source / "miniapp"
        if not root.exists():
            return None
        for path in sorted(root.rglob(requested_name)):
            if path.is_file():
                return path.relative_to(draft_source).as_posix()
        requested_stem = Path(normalized).stem
        requested_suffix = Path(normalized).suffix
        if not requested_stem:
            return None
        for path in sorted(root.rglob(f"*{requested_suffix}")):
            if path.is_file() and path.stem == requested_stem:
                return path.relative_to(draft_source).as_posix()
        return None

    def _read_state_repair_packets(
        self,
        *,
        run_id: str,
        draft_source: Path,
        file_changes: list[DraftAction],
        attempt: int,
    ) -> list[dict[str, object]]:
        packets: list[dict[str, object]] = []
        for change in file_changes[:8]:
            path = self._strip_leading_dot_slash(change.file_path)
            if not path or str(change.operation) == "create":
                continue
            freshness = self.file_state_cache.freshness(run_id=run_id, root=draft_source, path=path)
            status = str(freshness.get("status") or "")
            if status not in {"unread", "stale"}:
                continue
            message = (
                f"{path} changed after the last read; read it again before patching."
                if status == "stale"
                else f"{path} was not read in this run before the edit; read it before repeating this repair."
            )
            packets.append(
                AgentEditValidator.repair_packet_for_read_state(
                    status=status,
                    file_path=path,
                    message=message,
                    attempt=attempt,
                    evidence={"freshness": freshness, "operation": change.operation},
                )
            )
        return packets

    @staticmethod
    def _strip_leading_dot_slash(raw_path: object) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path

    @staticmethod
    def _agent_model_visible_path(raw_path: object) -> bool:
        path = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(raw_path)
        return bool(path) and not AgentEditValidator.is_protected_path(path)

    def _store_agent_quality_report(self, workspace_id: str, run_id: str, execution: CheckExecutionRecord) -> None:
        role_coverage: dict[str, Any] = {}
        generated_tests: dict[str, Any] = {}
        neutral_template_findings: list[dict[str, Any]] = []
        for result in execution.results:
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            if result.name == "platform_invariants":
                role_coverage = dict(diagnostics.get("role_coverage") or role_coverage)
                generated_tests = dict(diagnostics.get("generated_tests") or generated_tests)
                neutral_template_findings = list(diagnostics.get("neutral_template_findings") or neutral_template_findings)
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}:
                generated_tests[result.name] = {
                    "status": result.status,
                    "details": result.details,
                    "command": result.command,
                }
        self._store_report(
            f"agent_quality:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "role_coverage": role_coverage,
                "generated_tests": generated_tests,
                "neutral_template_findings": neutral_template_findings,
            },
        )
        self._store_report(
            f"agent_quality:{workspace_id}:{run_id}",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "role_coverage": role_coverage,
                "generated_tests": generated_tests,
                "neutral_template_findings": neutral_template_findings,
            },
        )

    def _combined_changed_content(self, *, workspace_id: str, run_id: str, changed_paths: list[str], path_prefix: str) -> str:
        combined: list[str] = []
        for path in changed_paths:
            if not path.startswith(path_prefix):
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content:
                combined.append(content[:12000])
        return "\n".join(combined)

    def _completion_state(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        validation_snapshot: ValidationSnapshot | None,
        acceptance_contract: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        del preview_details
        return self.completion_gate.completion_state(
            workspace_id=workspace_id,
            run_id=run_id,
            request_mode=request.mode,
            results=results,
            validation_snapshot=validation_snapshot,
            generation_mode=str(getattr(request.generation_mode, "value", request.generation_mode)),
            intent=request.intent,
            acceptance_contract=acceptance_contract,
            focused_visual_edit=self._focused_edit_kind(request) == "css_only_visual",
        )

    def _validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        return self.completion_gate.validation_snapshot_from_execution(execution)

    def _finalize_job(self, *, job: JobRecord, loop_result: AgentLoopResult, elapsed_ms: int) -> JobRecord:
        job.status = loop_result.status
        job.outcome_kind = loop_result.outcome_kind
        job.summary = loop_result.summary
        job.failure_reason = loop_result.failure_reason
        job.failure_class = loop_result.failure_class
        job.failure_signature = loop_result.failure_signature
        job.root_cause_summary = loop_result.root_cause_summary
        job.current_fix_phase = loop_result.current_phase
        job.remaining_issues = [] if loop_result.status == "completed" else list(loop_result.remaining_issues)
        if loop_result.status != "completed":
            if job.failure_class == "provider.insufficient_quota":
                job.failure_reason = (
                    job.root_cause_summary
                    or job.failure_reason
                    or "OpenAI provider quota is exhausted for the selected code generation model."
                )
            else:
                job.failure_reason = self._specific_failure_reason(
                    default=job.failure_reason,
                    remaining_issues=job.remaining_issues,
                    latest_execution=loop_result.latest_execution,
                )
            latest_has_failed_checks = bool(
                loop_result.latest_execution is not None
                and any(result.status == "failed" for result in loop_result.latest_execution.results)
            )
            if job.root_cause_summary and not latest_has_failed_checks:
                job.failure_reason = job.failure_reason or job.root_cause_summary
        job.repair_iterations = [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in loop_result.repair_iterations]
        job.apply_result = loop_result.latest_apply_result
        if loop_result.latest_execution is not None:
            job.executed_checks = [item.model_dump(mode="json") for item in loop_result.latest_execution.results]
            job.validation_snapshot = self._validation_snapshot_from_execution(loop_result.latest_execution)
            flow_result = next(
                (item for item in loop_result.latest_execution.results if item.name == "frontend_interaction_static_smoke"),
                None,
            )
            if flow_result is not None:
                browser_result = next(
                    (item for item in loop_result.latest_execution.results if item.name == "browser_flow_smoke"),
                    None,
                )
                browser_diagnostics = dict(browser_result.diagnostics or {}) if browser_result is not None else {}
                job.flow_coverage = {
                    **dict(job.flow_coverage or {}),
                    "status": flow_result.status,
                    "diagnostics": dict(flow_result.diagnostics or {}),
                    "logs": list(flow_result.logs or []),
                    "browser_flow": {
                        "status": browser_result.status if browser_result is not None else "missing",
                        "diagnostics": browser_diagnostics,
                        "logs": list(browser_result.logs or []) if browser_result is not None else [],
                    },
                }
                if browser_result is not None:
                    job.browser_flow_proof = browser_diagnostics
                    if isinstance(browser_diagnostics.get("mobile_layout"), dict):
                        job.mobile_layout_report = dict(browser_diagnostics.get("mobile_layout") or {})
                    if browser_result.status == "failed":
                        job.repair_issue_signatures.append(
                            {
                                "check": "browser_flow_smoke",
                                "signature": self._failure_signature(loop_result.latest_execution),
                                "logs": list(browser_result.logs or [])[-5:],
                            }
                        )
        if loop_result.status == "completed":
            job.outcome_kind = "applied"
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
            job.validation_snapshot = ValidationSnapshot(
                platform_valid=True,
                checks_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        job.agent_turns = list(loop_result.turn_history)
        tool_trace_items: list[dict[str, Any]] = []
        large_tool_output_refs: list[str] = []
        process_output_items: list[dict[str, Any]] = []
        semantic_graph_items: list[dict[str, Any]] = []
        artifact_read_items: list[dict[str, Any]] = []
        artifact_run_id = job.linked_run_id or job.job_id
        for turn in loop_result.turn_history:
            metadata = turn.get("metadata") if isinstance(turn, dict) else {}
            for item in (metadata.get("tool_results") if isinstance(metadata, dict) else []) or []:
                if isinstance(item, dict):
                    if item.get("tool") == "run_command":
                        process_output_items.append(
                            {
                                "tool_use_id": item.get("tool_use_id"),
                                "command": item.get("command"),
                                "argv": item.get("argv"),
                                "cwd": item.get("cwd"),
                                "exit_code": item.get("exit_code"),
                                "semantic_status": item.get("semantic_status"),
                                "success": item.get("success"),
                                "duration_ms": item.get("duration_ms"),
                                "stdout_head": item.get("stdout_head"),
                                "stdout_tail": item.get("stdout_tail"),
                                "stderr_head": item.get("stderr_head"),
                                "stderr_tail": item.get("stderr_tail"),
                                "stdout_omitted_chars": item.get("stdout_omitted_chars"),
                                "stderr_omitted_chars": item.get("stderr_omitted_chars"),
                                "policy_decision": item.get("policy_decision"),
                            }
                        )
                    if item.get("tool") == "semantic_scan":
                        semantic_graph_items.append(dict(item))
                    if item.get("tool") == "read_artifact_ref":
                        artifact_read_items.append(
                            {
                                "tool_use_id": item.get("tool_use_id"),
                                "artifact_ref": item.get("artifact_ref"),
                                "found": item.get("found"),
                            }
                        )
                    compact_item = self._compact_tool_result(
                        item,
                        workspace_id=job.workspace_id,
                        run_id=artifact_run_id,
                    )
                    ref = compact_item.get("persisted_output_ref") if isinstance(compact_item, dict) else None
                    if ref:
                        large_tool_output_refs.append(str(ref))
                    tool_trace_items.append(compact_item)
        patch_history = [
            {
                "file_path": operation.file_path,
                "change_type": operation.operation,
                "reason": operation.reason,
                "owner": self._worker_owner_for_path(operation.file_path),
                "has_content": bool(operation.content),
                "has_diff": bool(operation.diff),
            }
            for operation in loop_result.all_file_changes
        ]
        job.compaction_summaries = [
            *list(job.compaction_summaries or []),
            compact_agent_memory(
                turn_history=loop_result.turn_history,
                file_change_count=len(loop_result.all_file_changes),
                last_assistant_message=truncate_tool_text(loop_result.last_assistant_message, max_chars=1200),
            )
        ][-20:]
        job.agent_transcript_ref = job.agent_transcript_ref or f"agent_transcript:{job.workspace_id}:{artifact_run_id}"
        job.tool_trace_ref = f"tool_trace:{job.workspace_id}:{artifact_run_id}"
        job.file_change_history_ref = f"file_change_history:{job.workspace_id}:{artifact_run_id}"
        job.large_tool_outputs_ref = f"large_tool_outputs:{job.workspace_id}:{artifact_run_id}" if large_tool_output_refs else None
        job.browser_proof_ref = f"browser_proof:{job.workspace_id}:{artifact_run_id}" if job.browser_flow_proof else None
        job.file_state_cache_ref = f"file_state_cache:{job.workspace_id}:{artifact_run_id}"
        job.turn_diff_ref = job.turn_diff_ref or f"turn_diff:{job.workspace_id}:{artifact_run_id}"
        job.environment_snapshot_ref = job.environment_snapshot_ref or f"environment_snapshot:{job.workspace_id}:{artifact_run_id}"
        job.tool_batch_summaries_ref = job.tool_batch_summaries_ref or f"tool_batch_summaries:{job.workspace_id}:{artifact_run_id}"
        job.worker_mailbox_ref = job.worker_mailbox_ref or f"worker_mailbox:{job.workspace_id}:{artifact_run_id}"
        job.scratchpad_ref = job.scratchpad_ref or f"scratchpad:{job.workspace_id}:{artifact_run_id}"
        job.memory_ref = job.memory_ref or f"agent_memory_store:{job.workspace_id}:{artifact_run_id}"
        job.worker_drafts_ref = job.worker_drafts_ref or f"worker_drafts:{job.workspace_id}:{artifact_run_id}"
        job.worker_merge_ref = job.worker_merge_ref or f"worker_merge:{job.workspace_id}:{artifact_run_id}"
        job.trace_bundle_ref = f"trace_bundle:{job.workspace_id}:{artifact_run_id}"
        job.trace_reducer_ref = f"trace_reducer:{job.workspace_id}:{artifact_run_id}"
        job.exec_trace_ref = f"exec_trace:{job.workspace_id}:{artifact_run_id}" if process_output_items else None
        job.process_outputs_ref = f"process_outputs:{job.workspace_id}:{artifact_run_id}" if process_output_items else None
        job.tool_result_messages_ref = job.tool_result_messages_ref or f"tool_result_messages:{job.workspace_id}:{artifact_run_id}"
        job.artifact_read_trace_ref = f"artifact_read_trace:{job.workspace_id}:{artifact_run_id}" if artifact_read_items else None
        job.active_processes = list(self.process_manager.snapshot().get("active_processes") or [])
        job.resume_checkpoint_ref = job.resume_checkpoint_ref or f"resume_checkpoint:{job.workspace_id}:{artifact_run_id}"
        worker_branch_refs = [
            *job.worker_branch_refs,
            *[
                ref
                for ref in [job.worker_drafts_ref, job.worker_merge_ref, job.worker_mailbox_ref, job.worker_prefix_ref]
                if ref
            ],
        ]
        job.worker_branch_refs = list(
            dict.fromkeys(
                worker_branch_refs
            )
        )
        job.verifier_review_ref = job.verifier_review_ref or job.verification_report_ref
        job.context_pressure_ref = job.context_pressure_ref or f"context_pressure:{job.workspace_id}:{artifact_run_id}"
        job.hook_trace_ref = job.hook_trace_ref or f"hook_trace:{job.workspace_id}:{artifact_run_id}"
        job.semantic_graph_ref = job.semantic_graph_ref or f"semantic_graph:{job.workspace_id}:{artifact_run_id}"
        job.worker_prefix_ref = job.worker_prefix_ref or f"worker_prefix:{job.workspace_id}:{artifact_run_id}"
        job.replay_trace_ref = f"replay_trace:{job.workspace_id}:{artifact_run_id}"
        job.command_policy_ref = job.command_policy_ref or f"command_policy:{job.workspace_id}:{artifact_run_id}"
        job.verification_report_ref = job.verification_report_ref or f"verification_report:{job.workspace_id}:{artifact_run_id}"
        job.rollout_trace_ref = f"rollout_trace:{job.workspace_id}:{artifact_run_id}"
        self._store_transcript_snapshot(job, artifact_run_id)
        self._store_report(job.tool_trace_ref, {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "items": tool_trace_items})
        if job.artifact_read_trace_ref:
            self._store_report(
                job.artifact_read_trace_ref,
                {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "items": artifact_read_items},
            )
        self._store_report(job.file_change_history_ref, {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "items": patch_history})
        self._store_report(
            job.file_state_cache_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.file_state_cache.snapshot(artifact_run_id)},
        )
        self._store_report(
            job.turn_diff_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.turn_diff_tracker.snapshot(artifact_run_id)},
        )
        self._store_report(
            job.environment_snapshot_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "snapshot": self.environment_snapshots.get(artifact_run_id, {}),
            },
        )
        self._store_report(
            job.tool_batch_summaries_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": self.tool_batch_summaries.get(artifact_run_id, []),
            },
        )
        self._store_report(
            job.verification_report_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "report": self.verification_reports.get(artifact_run_id, {}),
            },
        )
        self._store_report(
            job.scratchpad_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.scratchpads.get(artifact_run_id, AgentScratchpad(run_id=artifact_run_id)).snapshot()},
        )
        self._store_report(
            job.memory_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.memory_store.snapshot(artifact_run_id)},
        )
        self._store_report(
            job.worker_drafts_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.worker_runtime.snapshot(artifact_run_id)},
        )
        self._store_report(
            job.worker_merge_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.worker_runtime.snapshot(artifact_run_id)},
        )
        self._store_report(
            job.command_policy_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **command_policy_snapshot()},
        )
        trace_snapshot = self.rollout_trace.snapshot(artifact_run_id)
        self._store_report(
            job.rollout_trace_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **trace_snapshot},
        )
        self._store_report(
            job.trace_bundle_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "events": trace_snapshot.get("events", [])},
        )
        self._store_report(
            job.trace_reducer_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "graph": trace_snapshot.get("graph", []),
                "reducer": trace_snapshot.get("reducer", {}),
            },
        )
        self._store_resume_checkpoint(job, artifact_run_id, phase="final_artifacts")
        if job.verifier_review_ref:
            self._store_report(
                job.verifier_review_ref,
                self.store.get("reports", job.verifier_review_ref)
                or self.store.get("reports", job.verification_report_ref)
                or {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "review": {}},
            )
        if job.exec_trace_ref:
            self._store_report(
                job.exec_trace_ref,
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    "items": process_output_items,
                    "deterministic_env": ["NO_COLOR", "TERM", "PAGER", "GIT_PAGER", "PYTHONIOENCODING", "LC_ALL", "LANG"],
                },
            )
        if job.process_outputs_ref:
            self._store_report(
                job.process_outputs_ref,
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    "items": process_output_items,
                },
            )
        self._store_report(
            job.context_pressure_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": self.context_pressure_history.get(artifact_run_id, []),
            },
        )
        self._store_report(
            job.hook_trace_ref,
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **self.hook_manager.snapshot(artifact_run_id)},
        )
        current_semantic_report = self.store.get("reports", job.semantic_graph_ref) if job.semantic_graph_ref else None
        self._store_report(
            job.semantic_graph_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": semantic_graph_items[-20:],
                "latest": semantic_graph_items[-1]
                if semantic_graph_items
                else (current_semantic_report.get("graph") if isinstance(current_semantic_report, dict) else {}),
            },
        )
        self._store_report(
            job.worker_prefix_ref,
            self.store.get("reports", job.worker_prefix_ref)
            or {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "prefix": {}},
        )
        self._store_report(
            job.replay_trace_ref,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "raw_event_ref": job.trace_bundle_ref,
                "reducer_ref": job.trace_reducer_ref,
                "graph": trace_snapshot.get("graph", []),
                "summary": trace_snapshot.get("reducer", {}),
            },
        )
        if job.large_tool_outputs_ref:
            self._store_report(
                job.large_tool_outputs_ref,
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    "refs": list(dict.fromkeys(large_tool_output_refs)),
                },
            )
        if job.browser_proof_ref:
            self._store_report(
                job.browser_proof_ref,
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    "proof": job.browser_flow_proof,
                    "mobile_layout_report": job.mobile_layout_report,
                },
            )
        self._store_report(
            f"agent_activity:{job.workspace_id}:{artifact_run_id}",
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": list(job.agent_activity_events),
            },
        )
        self._store_report(
            f"compaction_summaries:{job.workspace_id}:{artifact_run_id}",
            {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, "items": job.compaction_summaries},
        )
        job.latency_breakdown["agent_total_ms"] = elapsed_ms
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)
        self._store_report(f"remaining_issues:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.remaining_issues})
        if job.validation_snapshot is not None:
            self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        if loop_result.latest_preview_details:
            self._store_report(f"fix_runtime:{job.workspace_id}", {"workspace_id": job.workspace_id, **loop_result.latest_preview_details})
        event_type = "job_completed" if job.status == "completed" else "job_failed"
        self._record_hook(
            job=job,
            hook="after_run",
            status="completed" if job.status == "completed" else "failed",
            payload={"status": job.status, "outcome_kind": job.outcome_kind, "failure_class": job.failure_class},
        )
        self._append_event(job, event_type, job.summary or ("Workspace code agent completed." if job.status == "completed" else "Workspace code agent failed."))
        self._finalize_trace_bundle(job)
        self._save_job(job)
        return job

    @staticmethod
    def _worker_owner_for_path(path: str) -> str:
        normalized = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
        if normalized.startswith("miniapp/app/static/client/"):
            return "client_surface_worker"
        if normalized.startswith("miniapp/app/static/specialist/"):
            return "specialist_surface_worker"
        if normalized.startswith("miniapp/app/static/manager/"):
            return "manager_surface_worker"
        if normalized.startswith("miniapp/tests/"):
            return "test_verifier_worker"
        if normalized.startswith("miniapp/app/routes/") or normalized in {
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
        }:
            return "backend_api_worker"
        if (
            normalized == "miniapp/app/main.py"
            or normalized.startswith("miniapp/app/generated/")
            or normalized.startswith("miniapp/app/static/shared/")
        ):
            return "shared_runtime"
        return "shared"

    @staticmethod
    def _specific_failure_reason(
        *,
        default: str | None,
        remaining_issues: list[dict[str, Any]],
        latest_execution: CheckExecutionRecord | None,
    ) -> str | None:
        def _important_line(logs: list[Any]) -> str:
            specific_markers = (
                "operationalerror",
                "assertionerror",
                "syntaxerror",
                "modulenotfounderror",
                "importerror",
                "no such table",
            )
            nonspecific_markers = (
                "failed",
                "error:",
                "traceback",
            )
            clean_logs = [" ".join(str(line or "").split()).strip() for line in logs if str(line or "").strip()]
            for line in reversed(clean_logs):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict) and (payload.get("blocking") is True or str(payload.get("severity") or "").lower() == "high"):
                    return line[:320]
            for line in reversed(clean_logs):
                lowered = line.lower()
                if any(marker in lowered for marker in specific_markers):
                    return line[:320]
            for line in reversed(clean_logs):
                lowered = line.lower()
                if any(marker in lowered for marker in nonspecific_markers):
                    return line[:320]
            return clean_logs[-1][:320] if clean_logs else ""

        for issue in remaining_issues:
            check = str(issue.get("check") or issue.get("code") or "validation").strip()
            logs = issue.get("logs")
            if isinstance(logs, list):
                line = _important_line(logs)
                if line:
                    return f"{check}: {line}"
            message = str(issue.get("message") or "").strip()
            if message:
                return f"{check}: {message[:320]}"
        if latest_execution is not None:
            for result in latest_execution.results:
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                if result.name == "browser_flow_smoke" and result.status == "failed" and diagnostics.get("infra_unavailable"):
                    return (
                        "browser_flow_smoke: Playwright browser proof could not run because browser "
                        "verification infrastructure is unavailable."
                    )
            for result in latest_execution.results:
                if result.status != "failed":
                    continue
                line = _important_line(list(result.logs or []))
                if line:
                    return f"{result.name}: {line}"
                if result.details:
                    return f"{result.name}: {result.details[:320]}"
        return default

    @staticmethod
    def _failure_signature(execution: CheckExecutionRecord | None) -> str:
        if execution is None:
            return "checks:none"
        parts: list[str] = []
        for result in execution.results:
            if result.status != "failed":
                continue
            key_line = ""
            for line in reversed(list(result.logs or [])):
                text = " ".join(str(line or "").split()).strip()
                if text:
                    key_line = text[:160]
                    break
            parts.append(f"{result.name}:{key_line or str(result.details or '')[:160]}")
        if not parts:
            return "checks:passed"
        return "|".join(parts[:4])

    def _initial_file_context(self, workspace_id: str, run_id: str, *, role_scope: list[str] | None = None) -> dict[str, str]:
        contexts: dict[str, str] = {}
        role_paths: list[str] = []
        for role in role_scope or []:
            if role in ROLE_ORDER:
                role_paths.extend(
                    [
                        f"miniapp/app/static/{role}/index.html",
                        f"miniapp/app/static/{role}/app.js",
                        f"miniapp/app/static/{role}/styles.css",
                    ]
                )
        for path in [*role_paths, *INITIAL_CONTEXT_PATHS]:
            if path in contexts:
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is not None:
                contexts[path] = content
        return contexts

    def _add_failed_generated_test_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        latest_execution: CheckExecutionRecord,
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
    ) -> None:
        failed_tests = [
            result
            for result in latest_execution.results
            if result.status == "failed" and result.name in {"generated_app_python_tests", "generated_app_js_tests"}
        ]
        if not failed_tests:
            return
        for path in ("miniapp/tests/test_generated_app.py", "miniapp/tests/generated_app.test.mjs"):
            if path in extra_file_context:
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is not None:
                extra_file_context[path] = content
        related_fixture_paths: list[str] = []
        js_test_content = extra_file_context.get("miniapp/tests/generated_app.test.mjs") or ""
        for path in self._generated_js_test_fixture_paths(js_test_content):
            if path in extra_file_context:
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is None:
                continue
            extra_file_context[path] = content
            related_fixture_paths.append(path)
            if len(related_fixture_paths) >= 16:
                break
        if any(item.get("tool") == "generated_test_failure_context" for item in tool_results if isinstance(item, dict)):
            return
        tool_results.append(
            {
                "tool": "generated_test_failure_context",
                "contract": (
                    "Generated tests are app acceptance tests. On an edit, either preserve the app selectors/text they still validly assert, "
                    "or update the generated test file in the same patch when the requested behavior intentionally changes the expectation."
                ),
                "required_next_action": (
                    "Use apply_patch_to_draft/write_file so the next generated test result changes: restore missing selectors/ids in app code, "
                    "or patch miniapp/tests/test_generated_app.py / miniapp/tests/generated_app.test.mjs together with the app change. "
                    "Do not keep returning single app-file patches that leave the same generated test failure."
                ),
                "failed_tests": [
                    {
                        "name": result.name,
                        "details": result.details,
                        "command": result.command,
                        "logs": list(result.logs or [])[-40:],
                        "diagnostics": result.diagnostics if isinstance(result.diagnostics, dict) else {},
                    }
                    for result in failed_tests
                ],
                "related_fixture_files_loaded": related_fixture_paths,
            }
        )

    @staticmethod
    def _generated_js_test_fixture_paths(source: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"['\"]app/((?:static|generated)/[^'\"]+)['\"]", str(source or "")):
            path = f"miniapp/app/{match.group(1).strip().lstrip('/')}"
            if path not in paths:
                paths.append(path)
        return paths

    def _add_static_js_failure_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        latest_execution: CheckExecutionRecord,
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
    ) -> None:
        failures: list[dict[str, object]] = []
        for result in latest_execution.results:
            if result.status != "failed" or result.name != "changed_files_static":
                continue
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            diagnostic_payload = diagnostics.get("static_js_syntax_error")
            context_kind = "javascript_syntax"
            if not isinstance(diagnostic_payload, dict):
                diagnostic_payload = diagnostics.get("python_name_error")
                context_kind = "python_import"
            if not isinstance(diagnostic_payload, dict):
                continue
            file_path = self._strip_leading_dot_slash(diagnostic_payload.get("file_path"))
            if file_path:
                content = self.workspace_service.try_read_text_file(workspace_id, file_path, run_id=run_id)
                if content is not None:
                    extra_file_context[file_path] = content
            failures.append(
                {
                    "name": result.name,
                    "details": result.details,
                    "command": result.command,
                    "logs": list(result.logs or [])[-20:],
                    "diagnostics": diagnostic_payload,
                    "context_kind": context_kind,
                }
            )
        if not failures:
            return
        if any(item.get("tool") in {"static_js_failure_context", "static_build_failure_context"} for item in tool_results if isinstance(item, dict)):
            return
        tool_results.append(
            {
                "tool": "static_build_failure_context",
                "contract": "changed_files_static is a blocking platform import/syntax check for generated source.",
                "required_next_action": (
                    "Patch the exact file_path from diagnostics so the static/import check passes. "
                    "For JavaScript, make node --check pass. For Python import NameError, define the referenced name before import-time use. "
                    "Do not spend the next turn rewriting unrelated pages or tests while this blocking static failure remains."
                ),
                "failures": failures,
            }
        )

    def _add_build_validator_failure_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        latest_execution: CheckExecutionRecord,
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
    ) -> None:
        actionable_codes = {
            "build.duplicate_static_route",
            "build.broken_static_ref",
            "build.missing_static_asset",
            "build.missing_static_page",
            "build.page_missing_preview_bridge",
            "build.page_missing_shell_style_link",
            "build.page_missing_shell_root",
            "platform.missing_generated_app_tests",
            "platform.missing_create_get_api",
            "platform.missing_create_post_api",
            "platform.frontend_missing_post_api",
            "platform.preloaded_product_data",
            "connectivity.missing_backend_route",
        }
        issues: list[dict[str, object]] = []
        for result in latest_execution.results:
            if result.status != "failed" or result.name not in {"schema_validators", "connectivity_validators", "platform_invariants"}:
                continue
            for line in result.logs or []:
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                code = str(payload.get("code") or "")
                if code not in actionable_codes:
                    continue
                issues.append(
                    {
                        "check": result.name,
                        "code": code,
                        "location": str(payload.get("location") or ""),
                        "message": str(payload.get("message") or ""),
                        "repair_recipe": payload.get("repair_recipe") if isinstance(payload.get("repair_recipe"), dict) else None,
                    }
                )
        if not issues:
            return
        target_paths: list[str] = []
        for issue in issues:
            recipe = issue.get("repair_recipe") if isinstance(issue.get("repair_recipe"), dict) else {}
            if isinstance(recipe, dict):
                frontend_ref = str(recipe.get("frontend_ref") or "").strip()
                if frontend_ref.startswith("miniapp/"):
                    target_paths.append(frontend_ref.split(":", 1)[0].strip())
                suggested = str(recipe.get("suggested_patch_target") or "").strip()
                if suggested:
                    target_paths.append(suggested)
            target_paths.append(str(issue.get("location") or ""))
        for path in ["miniapp/app/routes/api.py", "miniapp/app/schemas.py", *target_paths]:
            if not path or path in extra_file_context:
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is not None:
                extra_file_context[path] = content
        if any(item.get("tool") == "build_validator_failure_context" for item in tool_results if isinstance(item, dict)):
            return
        tool_results.append(
            {
                "tool": "build_validator_failure_context",
                "contract": "Build validator failures are platform invariants for routeable generated pages.",
                "required_next_action": (
                    "Patch only the exact failing route/page contract. For build.missing_static_page, either create the exact missing HTML file "
                    "or remove that exact route_manifest entry. For build.page_missing_preview_bridge, add "
                    "<script src=\"/static/preview_bridge.js\" defer></script> to the exact page. "
                    "For build.duplicate_static_route, keep one canonical route_manifest entry per route and make it point to the static page that should serve that route. "
                    "For build.broken_static_ref or build.missing_static_asset, either create the referenced asset or remove the script/link tag from the exact page. "
                    "For platform.missing_generated_app_tests, create miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs. "
                    "For platform.missing_create_get_api/platform.missing_create_post_api, create or register backend /api read/write routes for the prompt-derived persisted workflow. "
                    "For platform.frontend_missing_post_api, add the prompt-required control/fetch code that writes user-provided state to the matching /api route. "
                    "For platform.preloaded_product_data, remove preloaded product records and start from empty persistent state. "
                    "For connectivity.missing_backend_route, patch either the exact frontend API reference from repair_recipe.frontend_ref or the matching FastAPI route; do not rewrite unrelated UI/tests first. "
                    "Do not rewrite unrelated role pages."
                ),
                "issues": issues[:24],
            }
        )

    @staticmethod
    def _compact_file_contexts(file_contexts: dict[str, str], *, max_files: int, max_chars: int = 6000) -> dict[str, str]:
        compact: dict[str, str] = {}
        for path, content in list(file_contexts.items())[:max_files]:
            text = str(content or "")
            if len(text) > max_chars:
                text = f"{text[: max_chars // 2]}\n...[truncated {len(text) - max_chars} chars]...\n{text[-max_chars // 2 :]}"
            compact[path] = text
        return compact

    @staticmethod
    def _compact_checks(execution: CheckExecutionRecord) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for result in execution.results:
            payload.append(
                {
                    "name": result.name,
                    "status": result.status,
                    "details": result.details,
                    "command": result.command,
                    "exit_code": result.exit_code,
                    "logs": result.logs[-10:],
                    "diagnostics": result.diagnostics,
                }
            )
        return payload

    def _collect_preview_diagnostics(self, workspace_id: str, preview_details: dict[str, Any]) -> None:
        preview = self.preview_service.get(workspace_id)
        if preview.proxy_port is None:
            return
        source_dir = self.workspace_service.source_dir(workspace_id)
        preview_details["containers"] = self.runtime_manager.inspect_containers(workspace_id, source_dir, preview.proxy_port)
        preview_details["container_logs"] = self.runtime_manager.collect_container_logs(workspace_id, source_dir, preview.proxy_port)

    def _clear_agent_reports(self, workspace_id: str) -> None:
        for key in (
            "validation",
            "check_results",
            "iterations",
            "candidate_diff",
            "patch",
            "fix_case",
            "fix_runtime",
            "remaining_issues",
            "agent_diagnostics",
            "agent_activity",
        ):
            self.store.delete("reports", f"{key}:{workspace_id}")

    def _append_agent_diagnostic(self, workspace_id: str, entry: dict[str, Any]) -> None:
        report_key = f"agent_diagnostics:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "items": []}
        items = list(current.get("items", [])) if isinstance(current, dict) else []
        items.append({**entry, "recorded_at": datetime.now(timezone.utc).isoformat()})
        self._store_report(report_key, {"workspace_id": workspace_id, "items": items[-80:]})

    def _append_activity(
        self,
        job: JobRecord,
        activity_type: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        save: bool = True,
    ) -> None:
        normalized_type = self._normalize_activity_type(activity_type)
        payload = self._enriched_event_details(details or {})
        event = {
            "type": normalized_type,
            "message": " ".join(str(message or "").split()).strip()[:220] or normalized_type.replace("_", " "),
            "details": WorkspaceCodeAgentRuntime._compact_jsonish(payload, max_chars=900, max_items=8),
            "sequence": len(job.agent_activity_events) + 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        for key in ("batch_id", "worker", "owner_scope", "summary", "duration_ms", "status"):
            if key in payload:
                event[key] = payload.get(key)
        for key in ("tool_use_id", "worker_id", "phase", "elapsed_ms", "artifact_ref", "hook", "semantic_status"):
            if key in payload:
                event[key] = payload.get(key)
        job.agent_activity_events.append(event)
        job.agent_activity_events = job.agent_activity_events[-160:]
        report_key = f"agent_activity:{job.workspace_id}:{job.linked_run_id or job.job_id}"
        current = self.store.get("reports", report_key) or {
            "workspace_id": job.workspace_id,
            "run_id": job.linked_run_id,
            "items": [],
        }
        items = list(current.get("items", [])) if isinstance(current, dict) else []
        items.append(event)
        self._store_report(
            report_key,
            {
                "workspace_id": job.workspace_id,
                "run_id": job.linked_run_id,
                "items": items[-500:],
            },
        )
        self._append_trace_bundle_event(job, f"activity.{normalized_type}", event)
        if save:
            self._sync_activity_to_run(job)
            self._save_job(job)

    @staticmethod
    def _normalize_activity_type(activity_type: str) -> str:
        allowed = {
            "planning",
            "reading",
            "searching",
            "running_command",
            "editing",
            "applying_patch",
            "checking",
            "browser_verifying",
            "repairing",
            "compacting",
            "compact_boundary",
            "tool_progress",
            "tool_use_summary",
            "process_started",
            "command_output_delta",
            "process_completed",
            "process_failed",
            "context_suggestion",
            "hook_started",
            "hook_completed",
            "verifier_nudge",
            "worker_started",
            "worker_completed",
            "worker_failed",
            "completed",
        }
        value = str(activity_type or "").strip().lower()
        return value if value in allowed else "planning"

    def _activity_from_event(self, event_type: str, message: str, details: dict[str, Any]) -> tuple[str, str] | None:
        if "check_step" in details:
            step = str(details.get("check_step") or "")
            if step in {"preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke"}:
                return "browser_verifying", message
            return "checking", message
        phase = str(details.get("phase") or "").strip().lower()
        if event_type == "spec_extract_started":
            return "planning", message
        if event_type == "agent_turn_started":
            if phase == "context_ready":
                return "reading", message
            if phase == "model_request":
                return "editing", message
            return "planning", message
        if event_type in {
            "tool_progress",
            "tool_use_summary",
            "process_started",
            "command_output_delta",
            "process_completed",
            "process_failed",
            "context_suggestion",
            "hook_started",
            "hook_completed",
            "verifier_nudge",
        }:
            return event_type, message
        if event_type in {"worker_started", "worker_completed", "worker_failed"}:
            return event_type, message
        if event_type == "compact_boundary":
            return "compact_boundary", message
        if event_type in {"running_checks", "build_started", "frontend_build_started", "backend_compile_started", "final_checks_started"}:
            return "checking", message
        if event_type == "preview_validation_started":
            return "browser_verifying", message
        if event_type in {"patch_apply_started", "patch_apply_completed"}:
            return "applying_patch", message
        if event_type == "repair_iteration":
            return "repairing", message
        if event_type in {"job_completed", "job_failed"}:
            return "completed", message
        return None

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        details = self._enriched_event_details(details or {})
        self.rollout_trace.append(
            job.linked_run_id or job.job_id,
            event_type,
            {"message": message, "details": WorkspaceCodeAgentRuntime._compact_jsonish(details, max_chars=900, max_items=8)},
        )
        self._append_trace_bundle_event(job, event_type, {"message": message, "details": details})
        self._persist_run_event(job, event_type, message, details)
        job.events.append(JobEvent(event_type=event_type, message=message, details=details))
        activity = self._activity_from_event(event_type, message, details)
        if activity is not None:
            activity_type, activity_message = activity
            self._append_activity(job, activity_type, activity_message, details, save=False)
        if event_type == "iteration_ready":
            job.token_usage = self._merge_run_token_usage(
                job.token_usage if isinstance(job.token_usage, dict) else {},
                details,
            )
            if isinstance(job.budget_status, dict):
                job.budget_status = {
                    **job.budget_status,
                    "total_tokens": token_usage_total(job.token_usage),
                }
        job.updated_at = datetime.now(timezone.utc)
        noisy_check_event = self._is_noisy_check_progress_event(event_type, details)
        if not noisy_check_event:
            self._sync_run_progress(job, event_type, message, details)
        if not noisy_check_event or self._should_flush_noisy_event(job):
            self._save_job(job)
        if not noisy_check_event:
            self.workspace_log_service.append(job.workspace_id, source=f"agent.{event_type}", message=message, payload=details)

    def _persist_run_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any]) -> None:
        if self.platform_db is None or not job.linked_run_id:
            return
        sdk_event_type = self._sdk_event_type(event_type, details)
        payload = {
            "schema": "grounded.run_event.v1",
            "job_id": job.job_id,
            "workspace_id": job.workspace_id,
            "status": job.status,
            "apply_status": getattr(job, "apply_status", None),
            "current_stage": getattr(job, "current_stage", None),
            "progress_percent": getattr(job, "progress_percent", None),
            "source_event_type": event_type,
            "message": message,
            "details": WorkspaceCodeAgentRuntime._compact_jsonish(details, max_chars=1400, max_items=10),
        }
        try:
            event = self.platform_db.append_run_event(job.linked_run_id, sdk_event_type, payload)
            if self.run_protocol_service is not None:
                session_id = None
                run_payload = self.store.get("runs", job.linked_run_id)
                if isinstance(run_payload, dict):
                    session_id = str(run_payload.get("session_id") or "") or None
                self.run_protocol_service.record_from_source_event(
                    run_id=job.linked_run_id,
                    workspace_id=job.workspace_id,
                    session_id=session_id,
                    source_event_type=event_type,
                    message=message,
                    details=payload["details"] if isinstance(payload.get("details"), dict) else {},
                    job_status=job.status,
                )
            if event_type in self._RUN_STATE_SNAPSHOT_EVENTS:
                self.platform_db.insert_run_state_snapshot(
                    run_id=job.linked_run_id,
                    reason=event_type,
                    payload={
                        "schema": "grounded.run_state_snapshot.v1",
                        "event": event,
                        "job": {
                            "job_id": job.job_id,
                            "status": job.status,
                            "apply_status": getattr(job, "apply_status", None),
                            "current_stage": getattr(job, "current_stage", None),
                            "progress_percent": getattr(job, "progress_percent", None),
                            "failure_class": job.failure_class,
                            "failure_signature": job.failure_signature,
                            "token_usage": job.token_usage,
                        },
                    },
                )
        except Exception:
            logger.exception("Failed to persist run event %s for run %s.", event_type, job.linked_run_id)

    _RUN_STATE_SNAPSHOT_EVENTS = {
        "job_started",
        "iteration_ready",
        "repair_iteration",
        "patch_apply_completed",
        "checks_completed",
        "preview_validation_started",
        "job_completed",
        "job_failed",
    }

    @staticmethod
    def _sdk_event_type(event_type: str, details: dict[str, Any]) -> str:
        if event_type == "job_started":
            return "run.started"
        if event_type in {"job_completed", "job_failed"}:
            return "run.completed"
        if event_type == "agent_turn_started":
            return "turn.started"
        if event_type in {"tool_use_summary", "tool_batch", "tool_call_contract"}:
            return "tool.completed"
        if event_type in {"running_checks", "checks_completed", "final_checks_started"} or "check_step" in details:
            return "check.completed"
        if event_type in {"repair_iteration", "browser_repair_packet", "generated_test_repair_packet", "browser_replay_packet"}:
            return "repair.packet"
        if event_type in {"preview_validation_started", "browser_proof_failed", "browser_step_repair"}:
            return "browser.step"
        if event_type in {"patch_apply_started", "patch_apply_completed", "apply_started", "apply_completed"}:
            return "gate.changed"
        if event_type == "iteration_ready":
            return "usage"
        return event_type.replace("_", ".")

    @staticmethod
    def _is_noisy_check_progress_event(event_type: str, details: dict[str, Any]) -> bool:
        if "check_step" not in details:
            return False
        status = str(details.get("check_status") or "").strip().lower()
        if status == "failed":
            return False
        if event_type in {
            "build_started",
            "frontend_build_started",
            "backend_compile_started",
            "final_checks_started",
            "preview_validation_started",
            "running_checks",
        }:
            return status in {"started", "passed", "skipped"}
        return False

    @staticmethod
    def _should_flush_noisy_event(job: JobRecord) -> bool:
        return len(job.events) % 10 == 0

    @staticmethod
    def _enriched_event_details(details: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(details)
        for key in ("files", "changed_files", "file_change_files"):
            raw_files = enriched.get(key)
            if isinstance(raw_files, list) and raw_files:
                files = [str(path) for path in raw_files if str(path).strip()]
                enriched.setdefault("file_count", len(set(files)))
                enriched.setdefault("file_summary", WorkspaceCodeAgentRuntime._compact_file_list(files))
                enriched.setdefault("file_groups", WorkspaceCodeAgentRuntime._file_group_counts(files))
                break
        return enriched

    @staticmethod
    def _compact_file_list(files: list[str], *, limit: int = 4) -> str:
        compact: list[str] = []
        for path in files:
            value = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            for prefix in ("miniapp/app/", "miniapp/", "app/"):
                if value.startswith(prefix):
                    value = value[len(prefix):]
                    break
            if value and value not in compact:
                compact.append(value)
        if not compact:
            return ""
        shown = compact[:limit]
        suffix = f" +{len(compact) - limit}" if len(compact) > limit else ""
        return ", ".join(shown) + suffix

    @staticmethod
    def _file_group_counts(files: list[str]) -> dict[str, int]:
        groups = {"backend": 0, "client": 0, "specialist": 0, "manager": 0, "tests": 0, "styles": 0, "other": 0}
        seen = set()
        for raw_path in files:
            path = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(raw_path)
            if not path or path in seen:
                continue
            seen.add(path)
            if path.endswith(".css"):
                groups["styles"] += 1
            if path.startswith("miniapp/tests/"):
                groups["tests"] += 1
            elif path.startswith("miniapp/app/static/client/"):
                groups["client"] += 1
            elif path.startswith("miniapp/app/static/specialist/"):
                groups["specialist"] += 1
            elif path.startswith("miniapp/app/static/manager/"):
                groups["manager"] += 1
            elif path.startswith("miniapp/app/routes/") or path in {
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
            }:
                groups["backend"] += 1
            elif path == "miniapp/app/main.py":
                groups["other"] += 1
            else:
                groups["other"] += 1
        return {key: value for key, value in groups.items() if value}

    @staticmethod
    def _check_progress_event_type(check_step: str) -> str:
        step = str(check_step or "").strip()
        if step == "schema_validators":
            return "build_started"
        if step in {"connectivity_validators", "platform_invariants"}:
            return "frontend_build_started"
        if step == "changed_files_static":
            return "backend_compile_started"
        if step == "lsp_static_diagnostics":
            return "backend_compile_started"
        if step in {"generated_app_python_tests", "generated_app_js_tests"}:
            return "final_checks_started"
        if step == "frontend_interaction_static_smoke":
            return "final_checks_started"
        if step in {"preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke"}:
            return "preview_validation_started"
        return "running_checks"

    @staticmethod
    def _check_progress_message(check_step: str, payload: dict[str, Any]) -> str:
        step_label = {
            "schema_validators": "schema and route manifest",
            "connectivity_validators": "frontend API connectivity",
            "changed_files_static": "static files and backend imports",
            "lsp_static_diagnostics": "language diagnostics",
            "platform_invariants": "role workflow invariants",
            "frontend_interaction_static_smoke": "frontend interaction flow wiring",
            "generated_app_python_tests": "generated Python persistence tests",
            "generated_app_js_tests": "generated JS frontend tests",
            "preview_boot_smoke": "preview boot smoke",
            "preview_connectivity_smoke": "preview route smoke",
            "browser_flow_smoke": "browser flow smoke",
        }.get(str(check_step or ""), str(check_step or "validation step").replace("_", " "))
        status = str(payload.get("check_status") or "").strip().lower()
        if status == "started":
            return f"Checking {step_label}."
        if status == "skipped":
            return f"Skipped {step_label}."
        if status == "passed":
            return f"{step_label.capitalize()} passed."
        if status == "failed":
            return f"{step_label.capitalize()} failed."
        return f"{step_label.capitalize()} {status or 'completed'}."

    def _save_job(self, job: JobRecord) -> None:
        if len(job.events) >= StateStore.JOB_EVENT_SHARD_MIN_COUNT:
            job.storage_version = StateStore.STORAGE_VERSION
            job.event_storage_ref = self.store.expected_storage_ref("job_events", job.job_id)
            job.event_count = len(job.events)
        self.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    def write_process_stdin(self, process_id: str, data: str) -> dict[str, Any]:
        ok = self.process_manager.write_stdin(process_id, data)
        return {"process_id": process_id, "ok": ok}

    def terminate_process(self, process_id: str) -> dict[str, Any]:
        ok = self.process_manager.terminate(process_id)
        return {"process_id": process_id, "ok": ok}

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: str = "stdout",
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        return self.process_manager.read_output(process_id, stream=stream, start=start, end=end)

    def _store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.artifact_reporter.store_report(key, payload)

    def _trace_bundle_writer(self, job: JobRecord) -> TraceBundleWriter:
        run_id = job.linked_run_id or job.job_id
        writer = self.trace_bundle_writers.get(run_id)
        if writer is None:
            writer = TraceBundleWriter(root=self.store.path.parent, workspace_id=job.workspace_id, run_id=run_id)
            self.trace_bundle_writers[run_id] = writer
        return writer

    def _append_trace_bundle_event(self, job: JobRecord, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if not (job.linked_run_id or job.job_id):
            return
        try:
            writer = self._trace_bundle_writer(job)
            event = writer.record(event_type, payload or {})
            job.trace_bundle_ref = job.trace_bundle_ref or f"trace_bundle:{job.workspace_id}:{job.linked_run_id or job.job_id}"
            self._store_report(
                job.trace_bundle_ref,
                {
                    "workspace_id": job.workspace_id,
                    "run_id": job.linked_run_id,
                    **writer.index(status="recording"),
                    "latest_event": event,
                },
            )
        except Exception:
            logger.exception("Failed to append trace bundle event %s for job %s.", event_type, job.job_id)

    def _record_rollout_trace(self, job: JobRecord, *, artifact_run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.rollout_trace.append(artifact_run_id, event_type, payload)
            self._append_trace_bundle_event(job, event_type, payload)
        except Exception:
            logger.exception("Failed to record rollout trace event %s for job %s.", event_type, job.job_id)

    def _prompt_context_trace_payload(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        tool_round: int,
        context_mode: str,
        prompt_payload: str,
        agent_memory: dict[str, Any],
        latest_execution: CheckExecutionRecord,
        latest_diff_summary: str | None,
    ) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        try:
            value = json.loads(prompt_payload)
            parsed = value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            parsed = {"raw_prompt_excerpt": prompt_payload[:2000]}
        context_pack = parsed.get("context_pack") if isinstance(parsed.get("context_pack"), dict) else {}
        memory_trace = self._memory_injection_trace(workspace_id=workspace_id, run_id=run_id, agent_memory=agent_memory)
        active_paths = self._target_files_from_execution(latest_execution)
        if not active_paths and latest_diff_summary:
            active_paths = self._paths_from_diff(str(latest_diff_summary or ""))[:8]
        return {
            "schema": "grounded.rollout_memory_trace.v1",
            "attempt": attempt,
            "tool_round": tool_round,
            "context_mode": context_mode,
            "prompt_sha256": hashlib.sha256(prompt_payload.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt_payload),
            "prompt_keys": sorted(str(key) for key in parsed.keys())[:80],
            "prompt_excerpt": prompt_payload[:4000],
            "context_pack": {
                "present": bool(context_pack),
                "workspace_summary_chars": len(str(context_pack.get("workspace_summary") or "")) if context_pack else 0,
                "selected_code_paths": [
                    str(item.get("path") or "")
                    for item in (context_pack.get("selected_code") or [])
                    if isinstance(item, dict)
                ][:20],
                "targeted_file_paths": [
                    str(item.get("path") or "")
                    for item in (context_pack.get("targeted_files") or [])
                    if isinstance(item, dict)
                ][:20],
                "retrieval_stats": context_pack.get("retrieval_stats") or {},
            },
            "skills": self._selected_skill_trace(
                workspace_id=workspace_id,
                prompt=request.prompt,
                intent=str(request.intent or ""),
                generation_mode=str(getattr(request.generation_mode, "value", request.generation_mode)),
                paths=active_paths,
                failure_class=str(getattr(request, "failure_class", "") or ""),
            ),
            "memory": memory_trace,
            "rules_count": len(parsed.get("rules") or []) if isinstance(parsed.get("rules"), list) else 0,
            "tool_registry_count": len(parsed.get("tool_registry") or {}) if isinstance(parsed.get("tool_registry"), dict) else 0,
            "repair_focus": parsed.get("repair_focus") or "",
        }

    def _selected_skill_trace(
        self,
        *,
        workspace_id: str,
        prompt: str,
        intent: str,
        generation_mode: str,
        paths: list[str],
        failure_class: str,
    ) -> dict[str, Any]:
        del workspace_id
        builder = getattr(self, "context_pack_builder", None)
        if builder is not None and hasattr(builder, "skill_search_for_context"):
            search = builder.skill_search_for_context(
                prompt=prompt,
                intent=intent,
                generation_mode=generation_mode,
                paths=paths,
                failure_class=failure_class,
            )
        else:
            search = {"selected": [], "skipped": [], "reason": "settings_unavailable"}
        selected = list(search.get("selected") or [])
        skipped = list(search.get("skipped") or [])[:20]
        return {
            "schema": search.get("schema") or "grounded.skill_search.v1",
            "budget": search.get("budget") or {},
            "explicit_mentions": search.get("explicit_mentions") or [],
            "prefetch": search.get("prefetch") or {},
            "selected": [
                {
                    "id": item.get("id"),
                    "source": item.get("source"),
                    "activation_reason": item.get("activation_reason"),
                    "activation_score": item.get("activation_score"),
                    "allowedTools": item.get("allowedTools") or [],
                    "validation": item.get("validation") or item.get("validation_hints") or [],
                }
                for item in selected
            ],
            "skipped": skipped,
        }

    def _memory_injection_trace(self, *, workspace_id: str, run_id: str, agent_memory: dict[str, Any]) -> dict[str, Any]:
        workspace_payload = self.store.get("reports", f"workspace_memory:{workspace_id}") or {}
        stage1_payload = self.store.get("reports", f"memory_stage1:{workspace_id}:{run_id}") or {}
        injected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for source, payload in (
            ("workspace_memory", workspace_payload),
            ("memory_stage1", stage1_payload),
            ("agent_memory", agent_memory or {}),
        ):
            items = payload.get("items") or payload.get("memories") or []
            if isinstance(payload.get("recent_failures"), list):
                items = [*list(items if isinstance(items, list) else []), *payload.get("recent_failures")]
            for index, item in enumerate(items if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or item.get("summary") or item.get("signature") or "")
                entry = {
                    "source": source,
                    "index": index,
                    "kind": item.get("kind") or item.get("type") or "memory",
                    "text_excerpt": text[:240],
                    "citation": item.get("citation") or item.get("citations") or item.get("source_ref"),
                }
                if SECRET_RE.search(text) or SECRET_RE.search(str(item)):
                    skipped.append({**entry, "reason": "secret_like_material"})
                elif item.get("status") == "superseded":
                    skipped.append({**entry, "reason": "superseded"})
                elif (item.get("stale_check") or {}).get("status") == "stale":
                    skipped.append({**entry, "reason": "stale_reference"})
                else:
                    injected.append({**entry, "reason": "active_context"})
        return {
            "injected": injected[:30],
            "skipped": skipped[:30],
            "source_refs": {
                "workspace_memory": f"workspace_memory:{workspace_id}",
                "memory_stage1": f"memory_stage1:{workspace_id}:{run_id}",
                "agent_memory": f"agent_memory_store:{workspace_id}:{run_id}",
            },
        }

    def _record_skill_usage_telemetry(
        self,
        *,
        job: JobRecord,
        artifact_run_id: str,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        results: list[RunCheckResult],
        run_status: str,
    ) -> None:
        builder = getattr(self, "context_pack_builder", None)
        paths = [path for result in results for path in self._target_files_from_result_payload(result)][:16]
        if builder is not None and hasattr(builder, "skill_search_for_context"):
            search = builder.skill_search_for_context(
                prompt=request.prompt,
                intent=str(request.intent or ""),
                generation_mode=str(getattr(request.generation_mode, "value", request.generation_mode)),
                paths=paths,
                failure_class=str(job.failure_class or ""),
            )
            selected = list(search.get("selected") or [])
        else:
            selected = []
        telemetry = SkillPackCatalog.usage_telemetry(
            selected=selected,
            check_results=[result.model_dump(mode="json") for result in results],
            run_status=run_status,
        )
        telemetry_ref = f"skill_usage:{workspace_id}:{artifact_run_id}"
        telemetry["workspace_id"] = workspace_id
        telemetry["run_id"] = run_id
        telemetry["search_selected_ids"] = [item.get("id") for item in selected]
        self._store_report(telemetry_ref, telemetry)
        self._record_rollout_trace(
            job,
            artifact_run_id=artifact_run_id,
            event_type="skill_usage_telemetry",
            payload={"telemetry_ref": telemetry_ref, "items": telemetry.get("items", [])},
        )

    def _file_content_hashes(self, *, workspace_id: str, run_id: str, paths: list[str]) -> dict[str, dict[str, Any]]:
        hashes: dict[str, dict[str, Any]] = {}
        for path in paths:
            normalized = str(path or "").strip().replace("\\", "/")
            if not normalized:
                continue
            text = self.workspace_service.try_read_text_file(workspace_id, normalized, run_id=run_id)
            hashes[normalized] = {
                "exists": text is not None,
                "sha256": hashlib.sha256(str(text or "").encode("utf-8")).hexdigest() if text is not None else None,
                "chars": len(str(text or "")) if text is not None else 0,
            }
        return hashes

    @staticmethod
    def _target_files_from_result_payload(result: RunCheckResult) -> list[str]:
        paths: list[str] = []
        diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
        for key in ("target_files", "changed_files", "files", "paths"):
            value = diagnostics.get(key)
            if isinstance(value, list):
                paths.extend(str(item) for item in value if isinstance(item, str))
            elif isinstance(value, str):
                paths.append(value)
        for line in [result.details, result.command, *list(result.logs or [])]:
            for match in re.finditer(r"miniapp/[A-Za-z0-9_./-]+\\.(?:py|js|mjs|html|css|json)", str(line or "")):
                paths.append(match.group(0))
        normalized: list[str] = []
        for path in paths:
            value = WorkspaceCodeAgentRuntime._strip_leading_dot_slash(path)
            if value and value not in normalized:
                normalized.append(value)
        return normalized[:12]

    def _finalize_trace_bundle(self, job: JobRecord) -> None:
        try:
            writer = self._trace_bundle_writer(job)
            state = writer.reduce()
            job.trace_bundle_ref = job.trace_bundle_ref or f"trace_bundle:{job.workspace_id}:{job.linked_run_id or job.job_id}"
            job.trace_reducer_ref = job.trace_reducer_ref or f"trace_reducer:{job.workspace_id}:{job.linked_run_id or job.job_id}"
            self._store_report(job.trace_bundle_ref, {"workspace_id": job.workspace_id, "run_id": job.linked_run_id, **writer.index(status="reduced"), "state": state})
            self._store_report(job.trace_reducer_ref, state)
        except Exception:
            logger.exception("Failed to finalize trace bundle for job %s.", job.job_id)

    def _clear_trace(self, workspace_id: str) -> None:
        self._store_report(f"trace:{workspace_id}", {"workspace_id": workspace_id, "entries": []})

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        report_key = f"trace:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "entries": []}
        entries = list(current.get("entries", []))
        entries.append({"stage": stage, "message": message, "payload": payload or {}, "created_at": datetime.now(timezone.utc).isoformat()})
        current["entries"] = entries
        self._store_report(report_key, current)
        self.workspace_log_service.append(workspace_id, source=f"agent.trace.{stage}", message=message, payload=payload or {})

    def _sync_activity_to_run(self, job: JobRecord) -> None:
        if not job.linked_run_id:
            return
        payload = self.store.get("runs", job.linked_run_id)
        if not payload:
            return
        payload["agent_activity_events"] = list(job.agent_activity_events)
        payload["storage_version"] = StateStore.STORAGE_VERSION
        self.store.upsert("runs", job.linked_run_id, payload)

    def _sync_run_progress(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any]) -> None:
        if not job.linked_run_id:
            return
        payload = self.store.get("runs", job.linked_run_id)
        if not payload:
            return
        stage, progress = self._run_progress_for_event(event_type, details=details, message=message)
        existing_progress = int(payload.get("progress_percent", 0))
        payload["linked_job_id"] = job.job_id
        payload["storage_version"] = StateStore.STORAGE_VERSION
        if job.event_storage_ref:
            payload["event_storage_ref"] = job.event_storage_ref
        if self._should_update_run_stage(event_type, progress=progress, existing_progress=existing_progress):
            payload["current_stage"] = stage
        if event_type == "job_failed":
            payload["progress_percent"] = 100
        else:
            payload["progress_percent"] = max(existing_progress, progress)
        iteration_count = self._current_iteration_count(job.workspace_id)
        if iteration_count:
            payload["iteration_count"] = iteration_count
        if event_type == "patch_apply_completed":
            changed_files = details.get("changed_files")
            if isinstance(changed_files, list):
                existing_files = payload.get("touched_files")
                merged_files = [
                    str(path)
                    for path in [*(existing_files if isinstance(existing_files, list) else []), *changed_files]
                    if str(path).strip()
                ]
                payload["touched_files"] = list(dict.fromkeys(merged_files))
        if event_type == "iteration_ready":
            payload["token_usage"] = self._merge_run_token_usage(
                payload.get("token_usage") if isinstance(payload.get("token_usage"), dict) else {},
                details,
            )
        payload["summary"] = job.summary
        payload["failure_reason"] = job.failure_reason
        payload["failure_class"] = job.failure_class
        payload["failure_signature"] = job.failure_signature
        payload["root_cause_summary"] = job.root_cause_summary
        payload["current_fix_phase"] = job.current_fix_phase
        payload["orchestration_phases"] = list(job.orchestration_phases)
        payload["implementation_plan"] = dict(job.implementation_plan)
        payload["agent_turns"] = list(job.agent_turns)
        payload["agent_activity_events"] = list(job.agent_activity_events)
        payload["agent_memory"] = dict(job.agent_memory)
        payload["agent_transcript_ref"] = job.agent_transcript_ref
        payload["tool_trace_ref"] = job.tool_trace_ref
        payload["file_change_history_ref"] = job.file_change_history_ref
        payload["browser_proof_ref"] = job.browser_proof_ref
        payload["large_tool_outputs_ref"] = job.large_tool_outputs_ref
        payload["file_state_cache_ref"] = job.file_state_cache_ref
        payload["turn_diff_ref"] = job.turn_diff_ref
        payload["environment_snapshot_ref"] = job.environment_snapshot_ref
        payload["tool_batch_summaries_ref"] = job.tool_batch_summaries_ref
        payload["worker_mailbox_ref"] = job.worker_mailbox_ref
        payload["scratchpad_ref"] = job.scratchpad_ref
        payload["memory_ref"] = job.memory_ref
        payload["worker_drafts_ref"] = job.worker_drafts_ref
        payload["worker_merge_ref"] = job.worker_merge_ref
        payload["trace_bundle_ref"] = job.trace_bundle_ref
        payload["trace_reducer_ref"] = job.trace_reducer_ref
        payload["command_policy_ref"] = job.command_policy_ref
        payload["verification_report_ref"] = job.verification_report_ref
        payload["rollout_trace_ref"] = job.rollout_trace_ref
        payload["exec_trace_ref"] = job.exec_trace_ref
        payload["process_outputs_ref"] = job.process_outputs_ref
        payload["context_pressure_ref"] = job.context_pressure_ref
        payload["tool_result_messages_ref"] = job.tool_result_messages_ref
        payload["active_processes"] = list(job.active_processes)
        payload["resume_checkpoint_ref"] = job.resume_checkpoint_ref
        payload["worker_branch_refs"] = list(job.worker_branch_refs)
        payload["verifier_review_ref"] = job.verifier_review_ref
        payload["browser_step_refs"] = list(job.browser_step_refs)
        payload["hook_trace_ref"] = job.hook_trace_ref
        payload["semantic_graph_ref"] = job.semantic_graph_ref
        payload["worker_prefix_ref"] = job.worker_prefix_ref
        payload["replay_trace_ref"] = job.replay_trace_ref
        payload["compaction_summaries"] = list(job.compaction_summaries)
        payload["acceptance_contract"] = dict(job.acceptance_contract)
        payload["worker_summaries"] = list(job.worker_summaries)
        payload["flow_coverage"] = dict(job.flow_coverage)
        payload["browser_flow_proof"] = dict(job.browser_flow_proof)
        payload["repair_issue_signatures"] = list(job.repair_issue_signatures)
        payload["mobile_layout_report"] = dict(job.mobile_layout_report)
        payload["completion_budget"] = dict(job.completion_budget)
        budget_status = dict(job.budget_status)
        token_usage = payload.get("token_usage") if isinstance(payload.get("token_usage"), dict) else {}
        try:
            usage_total = int(token_usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            usage_total = 0
        if usage_total:
            budget_status["total_tokens"] = usage_total
        payload["budget_status"] = budget_status
        payload["fix_targets"] = list(job.fix_targets)
        payload["remaining_issues"] = list(job.remaining_issues)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.upsert("runs", job.linked_run_id, payload)

    @staticmethod
    def _should_update_run_stage(event_type: str, *, progress: int, existing_progress: int) -> bool:
        if event_type in {"job_completed", "job_failed"}:
            return True
        if event_type in {
            "agent_turn_started",
            "agent_build_started",
            "agent_build_completed",
            "agent_build_failed",
            "tool_progress",
            "tool_use_summary",
            "process_started",
            "command_output_delta",
            "process_completed",
            "context_suggestion",
            "hook_started",
            "hook_completed",
            "verifier_nudge",
            "compact_boundary",
            "worker_started",
            "worker_completed",
            "worker_failed",
            "iteration_ready",
            "repair_iteration",
            "scope_expanded",
            "patch_apply_started",
            "patch_apply_completed",
            "build_started",
            "frontend_build_started",
            "backend_compile_started",
            "final_checks_started",
            "preview_validation_started",
            "checks_completed",
        }:
            return True
        return progress >= existing_progress

    def _current_iteration_count(self, workspace_id: str) -> int:
        payload = self.store.get("reports", f"iterations:{workspace_id}") or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        return len(items) if isinstance(items, list) else 0

    @staticmethod
    def _merge_run_token_usage(existing: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
        def _int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        last_turn = {
            "input_tokens": _int(turn.get("input_tokens")),
            "output_tokens": _int(turn.get("output_tokens")),
            "reasoning_tokens": _int(turn.get("reasoning_tokens")),
            "total_tokens": _int(turn.get("total_tokens")),
        }
        if not last_turn["total_tokens"]:
            last_turn["total_tokens"] = last_turn["input_tokens"] + last_turn["output_tokens"]
        merged = {
            "input_tokens": _int(existing.get("input_tokens")) + last_turn["input_tokens"],
            "output_tokens": _int(existing.get("output_tokens")) + last_turn["output_tokens"],
            "reasoning_tokens": _int(existing.get("reasoning_tokens")) + last_turn["reasoning_tokens"],
            "total_tokens": _int(existing.get("total_tokens")) + last_turn["total_tokens"],
            "turn_count": _int(existing.get("turn_count")) + 1,
            "last_turn": last_turn,
        }
        return merged

    @classmethod
    def _run_progress_for_event(
        cls,
        event_type: str,
        *,
        details: dict[str, Any] | None = None,
        message: str = "",
    ) -> tuple[str, int]:
        details = details or {}
        attempt = cls._safe_int(details.get("attempt"), default=0)
        is_after_patch = bool(details.get("has_file_edits") or details.get("has_draft_diff"))
        if details.get("check_step"):
            return cls._check_step_stage(details), cls._check_step_progress(details)
        progress_map = {
            "job_started": ("Starting code agent", 3),
            "spec_extract_started": ("Planning workflow contract", 8),
            "agent_build_started": ("Running code agent", 20),
            "agent_build_completed": ("Merged agent patches", 52),
            "agent_build_failed": ("Repairing agent patch", 38),
            "worker_started": ("Worker draft started", 24),
            "worker_completed": ("Worker draft completed", 48),
            "worker_failed": ("Repairing worker draft", 39),
            "process_started": ("Running diagnostic command", 31),
            "command_output_delta": ("Reading command output", 33),
            "process_completed": ("Diagnostic command completed", 35),
            "tool_progress": ("Tool is running", 36),
            "tool_use_summary": ("Tool batch completed", 34),
            "context_suggestion": ("Compacting agent context", 37),
            "hook_started": ("Preparing agent tool", 30),
            "hook_completed": ("Agent tool lifecycle updated", 35),
            "verifier_nudge": ("Preparing verification proof", 79),
            "compact_boundary": ("Compacting repair context", 43),
            "draft_prepared": ("Prepared draft patch", 52),
            "running_checks": ("Running validation checks" if is_after_patch else "Checking workspace shell", 59 if is_after_patch else cls._pre_patch_progress(attempt, 7)),
            "build_started": ("Checking schema and route manifest" if is_after_patch else "Validating workspace shell", 61 if is_after_patch else cls._pre_patch_progress(attempt, 9)),
            "frontend_build_started": ("Checking generated routes and API links" if is_after_patch else "Checking frontend baseline", 64 if is_after_patch else cls._pre_patch_progress(attempt, 11)),
            "backend_compile_started": ("Checking static files and backend imports" if is_after_patch else "Checking backend baseline", 67 if is_after_patch else cls._pre_patch_progress(attempt, 13)),
            "checks_completed": (cls._checks_stage(details), cls._checks_completed_progress(details) if is_after_patch else cls._pre_patch_progress(attempt, 16)),
            "agent_turn_started": (cls._agent_turn_stage(details), cls._agent_turn_progress(details)),
            "iteration_ready": (cls._iteration_ready_stage(details), cls._iteration_ready_progress(details)),
            "scope_expanded": ("Reading more workspace context", cls._context_expanded_progress(details)),
            "patch_apply_started": (cls._files_stage("Applying patch", details, key="files"), cls._patch_apply_progress(details, completed=False)),
            "patch_apply_completed": (cls._files_stage("Patch applied", details, key="changed_files"), cls._patch_apply_progress(details, completed=True)),
            "repair_iteration": (cls._repair_stage(details), cls._repair_progress(details)),
            "final_checks_started": ("Running final checks", 84),
            "apply_started": ("Applying to workspace", 92),
            "apply_completed": ("Applied to workspace", 98),
            "preview_rebuild_completed": ("Preview refreshed", 98),
            "job_completed": ("Complete", 99),
            "job_failed": ("Failed", 100),
        }
        if event_type in progress_map:
            return progress_map[event_type]
        clean_message = " ".join(str(message or "").split()).strip()
        return clean_message[:80] if clean_message else "Processing", 18

    @staticmethod
    def _pre_patch_progress(attempt: int, base: int) -> int:
        return min(42, base + max(0, int(attempt or 0)) * 6)

    @staticmethod
    def _safe_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _checks_stage(cls, details: dict[str, Any]) -> str:
        failed_checks = details.get("failed_checks")
        if isinstance(failed_checks, list) and failed_checks:
            return f"Checks found {len(failed_checks)} issue{'s' if len(failed_checks) != 1 else ''}: {', '.join(str(item) for item in failed_checks[:3])}"
        return "Checks passed"

    @classmethod
    def _checks_completed_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        return min(94, 82 + max(0, attempt - 1) * 4)

    @classmethod
    def _check_step_stage(cls, details: dict[str, Any]) -> str:
        step = str(details.get("check_step") or "").strip()
        status = str(details.get("check_status") or "").strip().lower()
        labels = {
            "schema_validators": "schema and route manifest",
            "connectivity_validators": "frontend API connectivity",
            "changed_files_static": "static files and backend imports",
            "platform_invariants": "role workflow invariants",
            "frontend_interaction_static_smoke": "frontend interaction flow wiring",
            "generated_app_python_tests": "Python persistence tests",
            "generated_app_js_tests": "JS frontend tests",
            "preview_boot_smoke": "preview boot smoke",
            "preview_connectivity_smoke": "preview route smoke",
            "browser_flow_smoke": "browser flow smoke",
        }
        label = labels.get(step, step.replace("_", " ") or "validation step")
        prefix = {
            "started": "Checking",
            "passed": "Passed",
            "failed": "Failed",
            "skipped": "Skipped",
        }.get(status, "Finished")
        suffix = cls._duration_suffix(details)
        return f"{prefix} {label}{suffix}"

    @classmethod
    def _check_step_progress(cls, details: dict[str, Any]) -> int:
        attempt = cls._safe_int(details.get("attempt"), default=0)
        is_after_patch = bool(details.get("has_file_edits") or details.get("has_draft_diff"))
        step = str(details.get("check_step") or "").strip()
        status = str(details.get("check_status") or "").strip().lower()
        if not is_after_patch:
            order = {
                "schema_validators": 9,
                "connectivity_validators": 11,
                "changed_files_static": 13,
                "platform_invariants": 15,
                "frontend_interaction_static_smoke": 16,
                "generated_app_python_tests": 17,
                "generated_app_js_tests": 18,
                "preview_boot_smoke": 19,
                "preview_connectivity_smoke": 20,
                "browser_flow_smoke": 21,
            }
            return cls._pre_patch_progress(attempt, order.get(step, 12))
        order = {
            "schema_validators": 61,
            "connectivity_validators": 64,
            "changed_files_static": 67,
            "platform_invariants": 70,
            "frontend_interaction_static_smoke": 73,
            "generated_app_python_tests": 76,
            "generated_app_js_tests": 78,
            "preview_boot_smoke": 80,
            "preview_connectivity_smoke": 82,
            "browser_flow_smoke": 84,
        }
        progress = order.get(step, 66) + (1 if status in {"passed", "failed", "skipped"} else 0)
        if attempt > 1:
            progress += min(10, (attempt - 1) * 4)
        return min(94, progress)

    @staticmethod
    def _duration_suffix(details: dict[str, Any]) -> str:
        duration_ms = WorkspaceCodeAgentRuntime._safe_int(details.get("duration_ms"), default=0)
        if duration_ms <= 0:
            return ""
        if duration_ms < 1000:
            return f" ({duration_ms} ms)"
        return f" ({duration_ms / 1000:.1f}s)"

    @classmethod
    def _agent_turn_stage(cls, details: dict[str, Any]) -> str:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        tool_round = cls._safe_int(details.get("tool_round"), default=0)
        phase = str(details.get("phase") or "").strip().lower()
        if phase == "context_ready":
            return f"Reading context for edit {attempt}"
        if phase == "model_request":
            return f"Editing draft {attempt}"
        if tool_round > 0:
            return f"Reading context for turn {attempt}"
        return f"Planning code edit {attempt}"

    @classmethod
    def _agent_turn_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        tool_round = cls._safe_int(details.get("tool_round"), default=0)
        phase = str(details.get("phase") or "").strip().lower()
        if bool(details.get("has_draft_diff")):
            if phase == "context_ready":
                return min(93, 86 + max(0, attempt - 2) * 3 + tool_round)
            if phase == "model_request":
                return min(93, 88 + max(0, attempt - 2) * 3 + tool_round)
            return min(93, 84 + max(0, attempt - 2) * 3 + tool_round)
        base = 10 + (attempt - 1) * 6 + tool_round * 3
        if phase == "context_ready":
            return min(48, base + 5)
        if phase == "model_request":
            return min(52, base + 12)
        return min(45, base)

    @classmethod
    def _iteration_ready_stage(cls, details: dict[str, Any]) -> str:
        file_change_count = cls._safe_int(details.get("file_change_count", details.get("file_change_count")), default=0)
        tool_call_count = cls._safe_int(details.get("tool_call_count"), default=0)
        outcome = str(details.get("outcome") or "").strip().lower()
        if file_change_count > 0:
            return f"Prepared {file_change_count} file edit{'s' if file_change_count != 1 else ''}"
        if tool_call_count > 0:
            return f"Requested {tool_call_count} context read{'s' if tool_call_count != 1 else ''}"
        if outcome in {"no_progress", "no_op"}:
            return "No file edits returned"
        return "Model response received"

    @classmethod
    def _iteration_ready_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        tool_round = cls._safe_int(details.get("tool_round"), default=0)
        file_change_count = cls._safe_int(details.get("file_change_count", details.get("file_change_count")), default=0)
        tool_call_count = cls._safe_int(details.get("tool_call_count"), default=0)
        if bool(details.get("has_draft_diff")):
            if file_change_count > 0:
                return min(94, 87 + max(0, attempt - 2) * 3)
            if tool_call_count > 0:
                return min(93, 85 + max(0, attempt - 2) * 3)
            return min(92, 84 + max(0, attempt - 2) * 3)
        base = 38 + max(0, attempt - 1) * 5 + tool_round * 2
        if file_change_count > 0:
            return min(51, base + 7)
        if tool_call_count > 0:
            return min(50, base + 3)
        return min(50, base + 2)

    @classmethod
    def _repair_stage(cls, details: dict[str, Any]) -> str:
        reason = str(details.get("reason") or "").strip()
        outcome = str(details.get("outcome") or "").strip()
        if reason == "self_blocked_no_file_changes":
            return "Correcting edit contract"
        if outcome in {"needs_context", "no_op"}:
            return "No file edits yet; reading more context"
        return "Refining patch with more context"

    @classmethod
    def _repair_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        outcome = str(details.get("outcome") or "").strip()
        if bool(details.get("has_draft_diff")):
            return min(93, 85 + max(0, attempt - 2) * 3)
        if outcome in {"needs_context", "no_op"}:
            return min(50, 42 + max(0, attempt - 1) * 4)
        return min(50, 44 + max(0, attempt - 1) * 4)

    @classmethod
    def _patch_apply_progress(cls, details: dict[str, Any], *, completed: bool) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        if attempt <= 1 or bool(details.get("first_patch")) or not bool(details.get("has_draft_diff")):
            return 58 if completed else 52
        return min(94, (89 if completed else 87) + max(0, attempt - 2) * 3)

    @classmethod
    def _context_expanded_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        tool_round = cls._safe_int(details.get("tool_round"), default=0)
        if bool(details.get("has_draft_diff")):
            return min(93, 86 + max(0, attempt - 2) * 3 + tool_round)
        return min(50, 44 + max(0, attempt - 1) * 4 + tool_round)

    @staticmethod
    def _files_stage(prefix: str, details: dict[str, Any], *, key: str) -> str:
        raw_files = details.get(key)
        files = [str(item) for item in raw_files if str(item).strip()] if isinstance(raw_files, list) else []
        if not files:
            return prefix
        unique_count = len(set(files))
        return f"{prefix} • {unique_count} file{'s' if unique_count != 1 else ''}"

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff_text or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
            if not match:
                continue
            for candidate in match.groups():
                normalized = candidate.strip().strip('"')
                if normalized.startswith("source/") or normalized.startswith("draft/"):
                    normalized = normalized.split("/", 1)[1]
                if normalized and normalized not in paths:
                    paths.append(normalized)
        return paths

    @staticmethod
    def _generation_mode(value: GenerationMode | str | None) -> GenerationMode:
        if isinstance(value, GenerationMode):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return GenerationMode(value.strip())
            except Exception:
                pass
        return GenerationMode.BALANCED

    @staticmethod
    def _tool_round_limit(generation_mode: GenerationMode) -> int:
        env_name = {
            GenerationMode.FAST: "WORKSPACE_AGENT_FAST_TOOL_ROUND_LIMIT",
            GenerationMode.BALANCED: "WORKSPACE_AGENT_BALANCED_TOOL_ROUND_LIMIT",
            GenerationMode.QUALITY: "WORKSPACE_AGENT_QUALITY_TOOL_ROUND_LIMIT",
        }.get(generation_mode, "WORKSPACE_AGENT_BALANCED_TOOL_ROUND_LIMIT")
        env_value = os.getenv(env_name) or os.getenv("WORKSPACE_AGENT_TOOL_ROUND_LIMIT")
        if env_value:
            try:
                return max(1, min(10, int(env_value)))
            except ValueError:
                pass
        if generation_mode == GenerationMode.FAST:
            return 2
        if generation_mode == GenerationMode.QUALITY:
            return 6
        return 4

    @staticmethod
    def _worker_branch_timeout_seconds(generation_mode: GenerationMode) -> float:
        env_value = os.getenv("WORKSPACE_AGENT_WORKER_TIMEOUT_SECONDS")
        if env_value:
            try:
                return max(60.0, float(env_value))
            except ValueError:
                pass
        if generation_mode == GenerationMode.FAST:
            return 300.0
        if generation_mode == GenerationMode.QUALITY:
            return 720.0
        return 480.0

    @staticmethod
    def _backend_contract_snapshot(source_dir: Path) -> dict[str, Any]:
        routes_root = source_dir / "miniapp/app/routes"
        declared_routes = [
            {"method": method, "path": path}
            for method, path in sorted(extract_declared_routes(routes_root, api_only=True))
        ]
        excerpts: dict[str, str] = {}
        route_relatives: list[str] = []
        for path in sorted(routes_root.glob("*.py")) if routes_root.exists() else []:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "/api" in text or "@router." in text:
                relative = str(path.relative_to(source_dir)).replace("\\", "/")
                if relative == "miniapp/app/routes/app_api.py":
                    continue
                route_relatives.append(relative)
        for relative in [*route_relatives, "miniapp/app/routes/api.py", "miniapp/app/schemas.py"]:
            path = source_dir / relative
            if not path.exists():
                continue
            if relative in excerpts:
                continue
            try:
                excerpts[relative] = truncate_tool_text(path.read_text(encoding="utf-8"), max_chars=5000)
            except OSError:
                continue
        return {
            "declared_api_routes": declared_routes,
            "source_excerpts": excerpts,
            "instruction": "UI and generated-test workers must use these actual backend routes and schema field names exactly; do not invent alternate /api paths.",
        }

    @staticmethod
    def _prompt_cache_key(workspace_id: str, run_id: str, prompt: str) -> str:
        prompt = re.sub(
            r"^\s*(?:(?:[01]?\d|2[0-3])[:.][0-5]\d(?:\s*(?:am|pm))?|(?:1[0-2]|0?[1-9])[:.][0-5]\d\s*(?:am|pm))\s+",
            "",
            str(prompt or "").strip(),
            flags=re.IGNORECASE,
        )
        prompt_key = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.strip().lower())[:80].strip("_") or "prompt"
        return f"workspace_code_agent:{workspace_id}:{run_id}:{prompt_key}"

    @staticmethod
    def _merge_cache_stats(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing or {})
        for key, value in (incoming or {}).items():
            if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
                merged[key] = merged[key] + value
            else:
                merged[key] = value
        return merged
