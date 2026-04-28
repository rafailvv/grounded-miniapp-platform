from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timezone
import difflib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from app.ai.model_registry import models_for_role, resolve_model_profile
from app.ai.openai_client import OpenAIClient
from app.models.artifacts import ValidationIssue
from app.models.common import GenerationMode
from app.models.domain import (
    CheckExecutionRecord,
    DraftFileOperation,
    FixAttemptRecord,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    ValidationSnapshot,
)
from app.modules.miniapp_agent_loop.engine import WorkspaceLoopEngine
from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    normalize_tool_requests,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
    truncate_tool_text,
)
from app.modules.miniapp_agent_loop.types import WorkspaceLoopCallbacks, WorkspaceLoopResult, WorkspaceLoopTurnPlan
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.platform_shell import BASE_STYLESHEET_PATH, PAGE_SHELL_INLINE_STYLE
from app.services.workflow_acceptance import (
    build_acceptance_contract,
    is_behavior_workflow_prompt,
    orchestration_metadata_for_contract,
)
from app.services.workspace.log_service import WorkspaceLogService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.runtime_manager import PreviewRuntimeManager
from app.services.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)

ROLE_ORDER = ("client", "specialist", "manager")
QUALITY_FIDELITY = {
    GenerationMode.FAST: "fast_app",
    GenerationMode.QUALITY: "quality_app",
    GenerationMode.BALANCED: "balanced_app",
    GenerationMode.BASIC: "basic_app",
}
AGENT_TURN_OPERATION_LIMIT = 20
AGENTIC_WORKFLOW_OPERATION_LIMIT = 32
FAST_PARALLEL_WORKER_COUNT = 5
FAST_PARALLEL_WORKER_OPERATION_LIMIT = 5
FAST_PARALLEL_WORKER_CONTENT_MAX_LENGTH = 14000
FOCUSED_VISUAL_OPERATION_LIMIT = 4
FOCUSED_VISUAL_CONTENT_MAX_LENGTH = 12000
FOCUSED_VISUAL_MAX_ATTEMPTS = 2
PATCH_FIRST_EXISTING_FILE_CHAR_LIMIT = 2500
PATCH_FIRST_EXISTING_FILE_LINE_LIMIT = 120
FOCUSED_VISUAL_STYLE_MARKERS = (
    "background",
    "border",
    "color",
    "colors",
    "css",
    "font",
    "palette",
    "padding",
    "spacing",
    "style",
    "theme",
    "typography",
    "visual",
    "акцент",
    "визуал",
    "внешний вид",
    "отступ",
    "палитр",
    "размер",
    "стил",
    "тем",
    "фон",
    "цвет",
    "шрифт",
    "фиолет",
    "синий",
    "синю",
    "синее",
    "сини",
    "красн",
    "зелен",
    "зелён",
    "желт",
    "жёлт",
    "черн",
    "чёрн",
    "бел",
    "серый",
    "серую",
    "серое",
    "серые",
)
FOCUSED_COPY_EDIT_MARKERS = (
    "copy",
    "heading",
    "label",
    "rename",
    "text",
    "title",
    "заголов",
    "лейбл",
    "назван",
    "надпис",
    "переимен",
    "слово",
    "текст",
)
BEHAVIOR_EDIT_MARKERS = (
    "/api",
    "api",
    "app.js",
    "backend",
    "database",
    "endpoint",
    "fetch",
    "javascript",
    "method:",
    "patch",
    "post",
    "put",
    "route",
    "server",
    "status",
    "бекенд",
    "бэк",
    "логик",
    "маршрут",
    "сервер",
    "статус",
    "эндпоинт",
)
VISUAL_EDIT_NEGATES_LOGIC_MARKERS = (
    "do not change logic",
    "don't change logic",
    "no logic changes",
    "without changing logic",
    "без изменения логики",
    "логику не меняй",
    "не менять логику",
    "не меняй логику",
    "не трогай логику",
)
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
SEED_CONTEXT_PATHS = (
    "miniapp/tests/test_generated_app.py",
    "miniapp/tests/generated_app.test.mjs",
    "README.md",
    "docs/agent-guidelines.md",
    "miniapp/app/main.py",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/routes/role_routes.py",
    "miniapp/app/routes/role_pages.py",
    "miniapp/app/static/shared/base.css",
    "miniapp/app/static/client/index.html",
    "miniapp/app/static/client/app.js",
    "miniapp/app/generated/route_manifest.json",
)
PROMPT_SEMANTIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "build",
    "business",
    "create",
    "for",
    "from",
    "i",
    "make",
    "miniapp",
    "need",
    "owner",
    "please",
    "sell",
    "selling",
    "small",
    "that",
    "the",
    "this",
    "to",
    "want",
    "with",
    "без",
    "будет",
    "вам",
    "ваш",
    "для",
    "его",
    "есть",
    "занимаюсь",
    "занимается",
    "заниматься",
    "как",
    "который",
    "маленький",
    "малого",
    "мне",
    "мини",
    "небольшого",
    "небольшой",
    "нужно",
    "обычно",
    "они",
    "оно",
    "продаю",
    "продавать",
    "продажи",
    "приложение",
    "сделай",
    "соцсети",
    "соцсетях",
    "создай",
    "так",
    "там",
    "через",
    "хочу",
    "чтобы",
    "это",
    "этот",
    "владелец",
    "я",
}
CYRILLIC_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
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
        workspace_loop_engine: WorkspaceLoopEngine,
        context_pack_builder: Any | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.check_runner = check_runner
        self.preview_service = preview_service
        self.runtime_manager = runtime_manager
        self.openai_client = openai_client
        self.workspace_log_service = workspace_log_service
        self.workspace_loop_engine = workspace_loop_engine
        self.context_pack_builder = context_pack_builder

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
        )
        self._clear_agent_reports(workspace_id)
        self._clear_trace(workspace_id)
        self._save_job(job)
        self._append_event(job, "job_started", "Workspace code agent started.", {"run_id": run_id, "mode": request.mode})

        if not self.openai_client.enabled:
            job.status = "blocked"
            job.outcome_kind = "blocked_generation"
            job.summary = "Workspace code agent requires OpenAI."
            job.failure_reason = "Set OPENAI_API_KEY before generating or editing a workspace."
            job.failure_class = "generation.llm_required"
            job.current_fix_phase = "failed"
            job.latency_breakdown["agent_total_ms"] = int((time.perf_counter() - started_at) * 1000)
            self._append_event(job, "job_failed", job.summary, {"failure_class": job.failure_class})
            self._save_job(job)
            return job

        draft_source = self._prepare_draft(workspace_id=workspace_id, run_id=run_id, request=request)
        self._store_report(
            f"spec:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "source": "user_prompt",
                "prompt": request.prompt,
                "runtime": "workspace_code_agent",
            "domain_policy": "Prompt semantics are authoritative; no app category is assumed.",
            },
        )
        self._store_report(
            f"execution_class:{workspace_id}",
            {"workspace_id": workspace_id, "execution_class": "shell_app", "runtime": "workspace_code_agent"},
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
        prompt = str(request.prompt or "").strip().lower()
        if not prompt:
            return "behavior_edit"
        if is_behavior_workflow_prompt(prompt):
            return "behavior_workflow_edit"
        if cls._contains_any_marker(prompt, FOCUSED_VISUAL_STYLE_MARKERS) and cls._contains_any_marker(prompt, VISUAL_EDIT_NEGATES_LOGIC_MARKERS):
            return "visual_style_edit"
        if cls._contains_any_marker(prompt, BEHAVIOR_EDIT_MARKERS):
            return "behavior_edit"
        if cls._contains_any_marker(prompt, FOCUSED_VISUAL_STYLE_MARKERS):
            return "visual_style_edit"
        if cls._contains_any_marker(prompt, FOCUSED_COPY_EDIT_MARKERS):
            return "small_copy_edit"
        return "behavior_edit"

    @staticmethod
    def _contains_any_marker(text: str, markers: tuple[str, ...]) -> bool:
        return any(marker in text for marker in markers)

    @staticmethod
    def _focused_visual_css_paths(role_scope: list[str] | tuple[str, ...] | None = None) -> list[str]:
        paths: list[str] = ["miniapp/app/static/shared/base.css"]
        for role in role_scope or ROLE_ORDER:
            if role in ROLE_ORDER:
                paths.append(f"miniapp/app/static/{role}/styles.css")
        return list(dict.fromkeys(paths))

    def _focused_visual_seed_context(self, workspace_id: str, run_id: str, *, role_scope: list[str]) -> dict[str, str]:
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
            for html_path in sorted(static_root.rglob("index.html")):
                try:
                    original = html_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "page-shell" not in original:
                    continue
                updated = self._ensure_page_shell_inline_safe_spacing(original)
                if updated != original:
                    html_path.write_text(updated, encoding="utf-8")
                    stabilized.append(html_path.relative_to(source_dir).as_posix())
        return list(dict.fromkeys(stabilized))

    def _enforce_patch_first_operations(
        self,
        operations: list[DraftFileOperation],
        *,
        request: GenerateRequest,
        workspace_id: str,
        run_id: str,
    ) -> list[DraftFileOperation]:
        if str(request.intent or "").strip().lower() == "create":
            return list(operations)
        normalized: list[DraftFileOperation] = []
        for operation in operations:
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
                DraftFileOperation(
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
    def _patch_first_allows_full_replace(operation: DraftFileOperation, current_content: str) -> bool:
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
        should_stop: Callable[[], bool] | None,
    ) -> WorkspaceLoopResult:
        focused_edit_kind = self._focused_edit_kind(request)
        focused_visual_edit = focused_edit_kind == "visual_style_edit"
        acceptance_contract = build_acceptance_contract(
            prompt=request.prompt,
            intent=str(request.intent or ""),
            generation_mode=generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        orchestration = orchestration_metadata_for_contract(
            contract=acceptance_contract,
            generation_mode=generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        job.acceptance_contract = acceptance_contract
        job.orchestration_phases = list(orchestration.get("phases") or [])
        job.worker_summaries = list(orchestration.get("worker_summaries") or [])
        job.flow_coverage = {
            "status": "planned" if acceptance_contract.get("required") else "not_required",
            "required_flows": [flow.get("id") for flow in acceptance_contract.get("flows", []) if isinstance(flow, dict)],
        }
        if acceptance_contract.get("required"):
            self._store_report(
                f"acceptance_contract:{workspace_id}",
                {"workspace_id": workspace_id, "run_id": run_id, "contract": acceptance_contract, "orchestration": orchestration},
            )
            self._append_event(
                job,
                "spec_extract_started",
                "Extracted role workflow acceptance contract.",
                {
                    "run_id": run_id,
                    "workflow_kind": focused_edit_kind,
                    "orchestration_enabled": bool(orchestration.get("enabled")),
                    "flow_ids": [flow.get("id") for flow in acceptance_contract.get("flows", []) if isinstance(flow, dict)],
                },
            )
        seed_context = (
            self._focused_visual_seed_context(workspace_id, run_id, role_scope=role_scope)
            if focused_visual_edit
            else self._seed_file_context(workspace_id, run_id, role_scope=role_scope)
        )
        tool_results: list[dict[str, object]] = []
        if generation_mode == GenerationMode.FAST and str(request.intent or "").lower() == "create":
            tool_results.append(self._fast_create_budget_result())
        if focused_visual_edit:
            tool_results.append(self._focused_visual_edit_budget_result(role_scope=role_scope))
        last_changed_files: list[str] = (
            self._focused_visual_css_paths(role_scope) if focused_visual_edit else ["miniapp", "docs", "README.md"]
        )
        cached_no_diff_checks: tuple[CheckExecutionRecord, dict[str, Any]] | None = None

        def _execute_checks(changed_files: list[str]) -> tuple[CheckExecutionRecord, dict[str, Any]]:
            nonlocal last_changed_files, cached_no_diff_checks
            last_changed_files = list(changed_files or last_changed_files)
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
            if not has_draft_diff and cached_no_diff_checks is not None:
                return cached_no_diff_checks
            check_profile = (
                "focused_edit"
                if focused_visual_edit
                else "full" if generation_mode == GenerationMode.QUALITY else "fast_gate"
            )
            scope_mode = "focused_edit" if focused_visual_edit else "agentic"
            check_attempt = 1 if has_draft_diff else 0

            def _check_progress_callback(check_step: str, payload: dict[str, Any]) -> None:
                event_type = self._check_progress_event_type(check_step)
                details = {
                    **payload,
                    "attempt": check_attempt,
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
                scope_mode=scope_mode,
                check_profile=check_profile,
                intent=str(request.intent or ""),
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
                progress_callback=_check_progress_callback,
            )
            if (
                check_profile == "fast_gate"
                and self._fast_gate_passed(execution.results)
                and self.workspace_service.diff(workspace_id, run_id=run_id).strip()
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
            prompt_smoke = self._prompt_alignment_smoke(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=request.prompt,
                intent=str(request.intent or ""),
                focused_edit_kind=focused_edit_kind,
            )
            execution.results.append(prompt_smoke)
            execution.completed_at = datetime.now(timezone.utc)
            self._store_agent_quality_report(workspace_id, execution)
            preview = self.preview_service.get(workspace_id)
            preview_details = {
                "status": "skipped",
                "stage": getattr(preview, "stage", "idle"),
                "progress_percent": getattr(preview, "progress_percent", 0),
                "logs": list(getattr(preview, "logs", [])),
                "last_error": getattr(preview, "last_error", None),
                "containers": [],
                "container_logs": {},
            }
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
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            extra_file_context: dict[str, str] = {}
            local_tool_results = list(tool_results)
            seen_tool_requests: set[str] = set()
            self_blocked_correction_sent = False
            generic_fatal_correction_sent = False
            output_cap_correction_sent = False
            tool_budget_correction_sent = False
            create_patch_coverage_correction_sent = False
            fast_parallel_attempted = False
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
            if (
                generation_mode == GenerationMode.FAST
                and str(request.intent or "").strip().lower() == "create"
                and self.workspace_service.diff(workspace_id, run_id=run_id).strip()
                and self._fast_create_should_use_fallback_repair(latest_execution)
            ):
                fallback_operations = self._fast_create_fallback_operations_with_cleanup(
                    request,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                fallback_result = self._fast_create_fallback_result(request)
                self._mark_fast_create_fallback_job(job)
                local_tool_results.append(fallback_result)
                tool_results.append(fallback_result)
                self._append_event(
                    job,
                    "repair_iteration",
                    "Fast create used the compact platform fallback to repair generated-test API persistence failures.",
                    {"attempt": attempt, "reason": "fast_create_generated_test_fallback"},
                )
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message="Fast create fallback repaired the generated API persistence path.",
                    diagnosis="Generated app tests exposed a persistence/schema failure; Fast mode replaced the app with a compact file-backed GET/POST implementation.",
                    operations=fallback_operations,
                    files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    expected_verification="Run generated Python/JS tests and preview checks.",
                    rationale_by_file={operation.file_path: operation.reason for operation in fallback_operations},
                    metadata={"tool_results": list(local_tool_results), "fallback": "fast_create_generated_test_repair"},
                )
            for tool_round in range(self._tool_round_limit(generation_mode) + 4):
                if (
                    not fast_parallel_attempted
                    and tool_round == 0
                    and self._should_use_fast_parallel_build(
                        request=request,
                        generation_mode=generation_mode,
                        focused_edit_kind=focused_edit_kind,
                        attempt=attempt,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        acceptance_contract=acceptance_contract,
                    )
                ):
                    fast_parallel_attempted = True
                    parallel_plan = self._request_fast_parallel_plan(
                        job=job,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        request=request,
                        attempt=attempt,
                        context_mode=context_mode,
                        latest_execution=latest_execution,
                        latest_preview_details=latest_preview_details,
                        seed_context=seed_context,
                        extra_file_context=extra_file_context,
                        tool_results=local_tool_results,
                        last_turn_summary=last_turn_summary,
                        latest_diff_summary=latest_diff_summary,
                        acceptance_contract=acceptance_contract,
                    )
                    if parallel_plan is not None:
                        return parallel_plan
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
                    seed_context=seed_context,
                    extra_file_context=extra_file_context,
                    tool_results=local_tool_results,
                    last_turn_summary=last_turn_summary,
                    latest_diff_summary=latest_diff_summary,
                )
                if "error" in llm_payload:
                    if self._is_output_cap_error(str(llm_payload.get("error") or "")):
                        fast_first_create_patch = (
                            generation_mode == GenerationMode.FAST
                            and str(request.intent or "").strip().lower() == "create"
                            and not self.workspace_service.diff(workspace_id, run_id=run_id).strip()
                        )
                        if fast_first_create_patch:
                            fallback_operations = self._fast_create_fallback_operations_with_cleanup(
                                request,
                                workspace_id=workspace_id,
                                run_id=run_id,
                            )
                            fallback_result = self._fast_create_fallback_result(request)
                            self._mark_fast_create_fallback_job(job)
                            local_tool_results.append(fallback_result)
                            tool_results.append(fallback_result)
                            self._append_event(
                                job,
                                "repair_iteration",
                                "Fast create used the compact platform fallback after the first model response exceeded the output cap.",
                                {"attempt": attempt, "tool_round": tool_round, "reason": "fast_create_output_cap_fallback"},
                            )
                            return WorkspaceLoopTurnPlan(
                                outcome="patch_ready",
                                assistant_message="Fast create fallback prepared a compact working app after the model output cap.",
                                diagnosis="The first Fast create model response exceeded max_output_tokens, so the platform emitted a compact persisted app scaffold using the prompt domain.",
                                operations=fallback_operations,
                                files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                                expected_verification="Run validators plus generated Python/JS tests; verify GET starts empty, POST saves, and GET returns the created record.",
                                rationale_by_file={operation.file_path: operation.reason for operation in fallback_operations},
                                metadata={"tool_results": list(local_tool_results), "fallback": "fast_create_output_cap"},
                            )
                        if not output_cap_correction_sent:
                            correction = self._output_cap_correction_result(llm_payload, request=request)
                            local_tool_results.append(correction)
                            tool_results.append(correction)
                            output_cap_correction_sent = True
                            self._append_event(
                                job,
                                "repair_iteration",
                                "Agent exceeded the structured output cap. Retrying with a smaller patch contract.",
                                {"attempt": attempt, "tool_round": tool_round, "reason": "output_cap"},
                            )
                            continue
                        return WorkspaceLoopTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent response exceeded the structured output cap.",
                            diagnosis=(
                                "The previous response was too large to return as valid JSON. "
                                "Retry with a smaller operation set, prefer compact replace operations, and keep this turn applyable."
                            ),
                            files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                            metadata={"error": str(llm_payload.get("error") or ""), "retry_reason": "max_output_tokens"},
                        )
                    return WorkspaceLoopTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(llm_payload.get("error") or ""),
                        diagnosis=str(llm_payload.get("error") or ""),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        failure_class="generation.agent_invalid_response",
                        failure_signature="generation.agent_invalid_response",
                        root_cause_summary=str(llm_payload.get("error") or ""),
                    )
                outcome = str(llm_payload.get("outcome") or "").strip().lower()
                raw_tool_requests = self._agent_tool_requests(llm_payload.get("tool_requests") or [])
                if outcome == "tool_request" or raw_tool_requests:
                    signature = json.dumps(raw_tool_requests, ensure_ascii=True, sort_keys=True)
                    if signature in seen_tool_requests:
                        return WorkspaceLoopTurnPlan(
                            outcome="needs_context",
                            assistant_message="Agent repeated an already satisfied tool request.",
                            diagnosis="The requested diagnostic context is already available; the next turn must patch code.",
                            files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                            metadata={"tool_requests": raw_tool_requests},
                        )
                    seen_tool_requests.add(signature)
                    is_fast_first_create_patch = (
                        generation_mode == GenerationMode.FAST
                        and str(request.intent or "").strip().lower() == "create"
                        and not self.workspace_service.diff(workspace_id, run_id=run_id).strip()
                    )
                    if is_fast_first_create_patch and not tool_budget_correction_sent:
                        correction = self._tool_budget_correction_result(raw_tool_requests, request=request)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        tool_budget_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Fast create skipped diagnostic reads before the first create patch and requested patch-only output.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "fast_first_patch_no_tools"},
                        )
                        continue
                    new_context, executed_results = self._execute_tool_requests(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        draft_source=draft_source,
                        tool_requests=raw_tool_requests,
                        execute_checks=_execute_checks,
                    )
                    extra_file_context.update(new_context)
                    local_tool_results.extend(executed_results)
                    tool_results.extend(executed_results)
                    if tool_round < self._tool_round_limit(generation_mode):
                        continue
                    if not tool_budget_correction_sent:
                        correction = self._tool_budget_correction_result(raw_tool_requests, request=request)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        tool_budget_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent requested more diagnostic tools than Fast mode allows. Retrying with patch-only instructions.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "tool_budget_exhausted"},
                        )
                        continue
                    return WorkspaceLoopTurnPlan(
                        outcome="needs_context",
                        assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent requested more tools than the turn budget allows."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        metadata={"tool_requests": raw_tool_requests},
                    )
                if outcome == "fatal_invalid_response":
                    if (
                        not self_blocked_correction_sent
                        and self._is_self_blocked_tool_contract_response(llm_payload)
                    ):
                        correction = self._tool_contract_correction_result(llm_payload)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        self_blocked_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent misunderstood the read-only tool contract. Retrying with corrected tool instructions.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "self_blocked_tool_contract"},
                        )
                        continue
                    if (
                        not generic_fatal_correction_sent
                        and self._is_empty_fatal_agent_response(llm_payload)
                    ):
                        correction = self._empty_fatal_correction_result(llm_payload)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        generic_fatal_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent returned an empty fatal response. Retrying with corrected task instructions.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "empty_fatal_response"},
                        )
                        continue
                    return WorkspaceLoopTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(llm_payload.get("assistant_message") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent declared a fatal invalid response."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    )
                operations = self._coerce_operations(llm_payload.get("operations") or [])
                if not operations:
                    if (
                        not self_blocked_correction_sent
                        and self._is_self_blocked_tool_contract_response(llm_payload)
                    ):
                        correction = self._tool_contract_correction_result(llm_payload)
                        local_tool_results.append(correction)
                        tool_results.append(correction)
                        self_blocked_correction_sent = True
                        self._append_event(
                            job,
                            "repair_iteration",
                            "Agent misunderstood how file edits are applied. Retrying with the operations contract.",
                            {"attempt": attempt, "tool_round": tool_round, "reason": "self_blocked_no_operations"},
                        )
                        continue
                    return WorkspaceLoopTurnPlan(
                        outcome="no_op",
                        assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or ""),
                        diagnosis=str(llm_payload.get("diagnosis") or "Agent did not return file edits."),
                        files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                        metadata={"raw_response": llm_payload},
                    )
                has_product_draft = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
                existing_paths, existing_text_by_path = self._create_coverage_existing_state(
                    workspace_id=workspace_id,
                    run_id=run_id,
                ) if has_product_draft else (set(), {})
                create_coverage_gap = self._create_patch_coverage_gap(
                    operations,
                    request=request,
                    existing_paths=existing_paths,
                    existing_text_by_path=existing_text_by_path,
                )
                if create_coverage_gap and not create_patch_coverage_correction_sent:
                    correction = self._create_patch_coverage_correction_result(create_coverage_gap)
                    local_tool_results.append(correction)
                    tool_results.append(correction)
                    create_patch_coverage_correction_sent = True
                    self._append_event(
                        job,
                        "repair_iteration",
                        "Agent create patch missed required role/test coverage. Retrying before applying a partial app.",
                        {"attempt": attempt, "tool_round": tool_round, "missing": create_coverage_gap},
                    )
                    continue
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message=str(llm_payload.get("assistant_message") or llm_payload.get("diagnosis") or "Agent prepared code edits."),
                    diagnosis=str(llm_payload.get("diagnosis") or ""),
                    operations=operations,
                    files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    expected_verification=str(llm_payload.get("expected_verification") or ""),
                    rationale_by_file={str(key): str(value) for key, value in dict(llm_payload.get("rationale_by_file") or {}).items()},
                    metadata={
                        "tool_results": list(local_tool_results),
                        "acceptance_checks": list(llm_payload.get("acceptance_checks") or []),
                    },
                )
            return WorkspaceLoopTurnPlan(
                outcome="no_op",
                assistant_message="Agent turn ended without producing edits.",
                diagnosis="Agent turn ended without producing edits.",
                files_read=list(seed_context.keys()),
            )

        callbacks = WorkspaceLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self._validation_snapshot_from_execution,
            completion_state=lambda results, preview_details, validation_snapshot=None: self._completion_state(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                results=results,
                preview_details=preview_details,
                validation_snapshot=validation_snapshot,
            ),
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=lambda operations: self._enforce_patch_first_operations(
                operations,
                request=request,
                workspace_id=workspace_id,
                run_id=run_id,
            ),
            post_apply_stabilize=self._stabilize_platform_shell,
            append_event=self._append_event,
            append_trace=self._append_trace,
            store_report=self._store_report,
            allow_optimistic_completion=True,
            skip_initial_checks=focused_visual_edit,
            stop_if_requested=should_stop,
        )
        return self.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=role_scope,
            generation_mode=generation_mode,
            max_attempts=FOCUSED_VISUAL_MAX_ATTEMPTS if focused_visual_edit else self._max_attempts(generation_mode),
            initial_operations=[],
            initial_assistant_message="Workspace code agent initialized.",
            initial_files_read=list(seed_context.keys()),
            initial_changed_files=last_changed_files,
            callbacks=callbacks,
        )

    def _should_use_fast_parallel_build(
        self,
        *,
        request: GenerateRequest,
        generation_mode: GenerationMode,
        focused_edit_kind: str,
        attempt: int,
        workspace_id: str,
        run_id: str,
        acceptance_contract: dict[str, Any],
    ) -> bool:
        if generation_mode != GenerationMode.FAST:
            return False
        if int(attempt or 0) > 1:
            return False
        if not acceptance_contract.get("required"):
            return False
        if str(request.intent or "").strip().lower() not in {"create", "edit", "refine", "role_only_change"}:
            return False
        if focused_edit_kind not in {"standard", "behavior_workflow_edit"} and str(request.intent or "").strip().lower() != "create":
            return False
        return not bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())

    def _request_fast_parallel_plan(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        context_mode: str,
        latest_execution: CheckExecutionRecord,
        latest_preview_details: dict[str, object],
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
        acceptance_contract: dict[str, Any],
    ) -> WorkspaceLoopTurnPlan | None:
        del latest_execution, latest_preview_details, last_turn_summary, latest_diff_summary
        started = time.perf_counter()
        blueprint = self._fast_parallel_blueprint(acceptance_contract)
        workers = self._fast_parallel_workers(blueprint)
        self._append_event(
            job,
            "parallel_build_started",
            "Fast parallel build started.",
            {
                "attempt": attempt,
                "worker_count": len(workers),
                "execution_style": "fast_parallel_workers",
                "has_draft_diff": False,
            },
        )
        self._store_report(
            f"parallel_build:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "mode": "fast",
                "blueprint": blueprint,
                "workers": [
                    {
                        "worker": worker["worker"],
                        "ownership": worker["ownership"],
                        "responsibility": worker["responsibility"],
                    }
                    for worker in workers
                ],
            },
        )
        results: list[dict[str, Any]] = []
        try:
            with ThreadPoolExecutor(max_workers=min(FAST_PARALLEL_WORKER_COUNT, len(workers)), thread_name_prefix="fast-agent-worker") as executor:
                future_to_worker = {}
                for worker in workers:
                    ctx = copy_context()
                    future = executor.submit(
                        ctx.run,
                        self._request_fast_parallel_worker,
                        job,
                        workspace_id,
                        run_id,
                        request,
                        attempt,
                        context_mode,
                        seed_context,
                        extra_file_context,
                        tool_results,
                        acceptance_contract,
                        blueprint,
                        worker,
                    )
                    future_to_worker[future] = worker
                for future in as_completed(future_to_worker):
                    worker = future_to_worker[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "worker": worker["worker"],
                            "status": "error",
                            "error": str(exc),
                            "error_class": exc.__class__.__name__,
                            "payload": {},
                            "operations": [],
                            "cache_stats": {},
                            "duration_ms": 0,
                        }
                    results.append(result)
        except Exception as exc:
            self._append_event(
                job,
                "parallel_build_failed",
                "Fast parallel build failed before worker merge; falling back to single agent turn.",
                {"attempt": attempt, "error": str(exc), "error_class": exc.__class__.__name__},
            )
            return None

        for result in sorted(results, key=lambda item: str(item.get("worker") or "")):
            cache_stats = result.get("cache_stats") if isinstance(result.get("cache_stats"), dict) else {}
            if cache_stats:
                job.cache_stats = self._merge_cache_stats(job.cache_stats, cache_stats)
            if result.get("model") and not job.llm_model:
                job.llm_model = str(result.get("model") or "")
            self._append_agent_diagnostic(
                workspace_id,
                {
                    "run_id": run_id,
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "worker": result.get("worker"),
                    "context_mode": context_mode,
                    "duration_ms": int(result.get("duration_ms") or 0),
                    "status": result.get("status"),
                    "model": result.get("model"),
                    "outcome": result.get("outcome"),
                    "operation_count": len(result.get("operations") or []),
                    "operation_files": [operation.file_path for operation in result.get("operations") or []],
                    "error": result.get("error"),
                    "token_usage": {
                        "input_tokens": int(cache_stats.get("input_tokens") or 0),
                        "output_tokens": int(cache_stats.get("output_tokens") or 0),
                        "reasoning_tokens": int(cache_stats.get("reasoning_tokens") or 0),
                        "total_tokens": int(cache_stats.get("total_tokens") or 0),
                    },
                },
            )
            self._append_event(
                job,
                "iteration_ready",
                f"Fast parallel worker {result.get('worker')} returned.",
                {
                    "attempt": attempt,
                    "worker": result.get("worker"),
                    "outcome": str(result.get("outcome") or result.get("status") or ""),
                    "operation_count": len(result.get("operations") or []),
                    "model": result.get("model") or "",
                    "input_tokens": int(cache_stats.get("input_tokens") or 0),
                    "output_tokens": int(cache_stats.get("output_tokens") or 0),
                    "reasoning_tokens": int(cache_stats.get("reasoning_tokens") or 0),
                    "total_tokens": int(cache_stats.get("total_tokens") or 0),
                    "has_draft_diff": False,
                },
            )

        merged_operations, merge_error = self._merge_fast_parallel_worker_operations(results)
        if merge_error:
            self._append_event(
                job,
                "parallel_build_failed",
                "Fast parallel worker merge failed; selecting the fastest safe fallback.",
                {"attempt": attempt, "reason": merge_error},
            )
            if str(request.intent or "").strip().lower() == "create":
                fallback_operations = self._fast_create_fallback_operations_with_cleanup(
                    request,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                fallback_result = self._fast_create_fallback_result(request)
                self._mark_fast_create_fallback_job(job)
                tool_results.append(fallback_result)
                self._append_event(
                    job,
                    "repair_iteration",
                    "Fast create used the compact platform fallback after parallel worker merge failed.",
                    {"attempt": attempt, "reason": "fast_parallel_merge_failed", "merge_error": merge_error},
                )
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message="Fast parallel workers partially generated the app, then platform fallback completed the create contract.",
                    diagnosis="At least one Fast parallel worker failed or conflicted, so Fast mode applied a compact persisted fallback instead of spending another full LLM turn.",
                    operations=fallback_operations,
                    files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    expected_verification="Run validators plus generated Python/JS tests for persistence and role workflow coverage.",
                    rationale_by_file={operation.file_path: operation.reason for operation in fallback_operations},
                    metadata={"tool_results": list(tool_results), "fallback": "fast_parallel_merge_failed"},
                )
            tool_results.append(
                {
                    "tool": "fast_parallel_build",
                    "status": "failed",
                    "reason": merge_error,
                    "required_next_action": "Return a single coherent patch_ready response that satisfies the same acceptance_contract.",
                }
            )
            return None
        create_gap = self._create_patch_coverage_gap(merged_operations, request=request)
        if create_gap:
            self._append_event(
                job,
                "parallel_build_failed",
                "Fast parallel worker merge missed create coverage; applying compact fallback.",
                {"attempt": attempt, "missing": create_gap},
            )
            if str(request.intent or "").strip().lower() == "create":
                fallback_operations = self._fast_create_fallback_operations_with_cleanup(
                    request,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                fallback_result = self._fast_create_fallback_result(request)
                self._mark_fast_create_fallback_job(job)
                tool_results.append(self._create_patch_coverage_correction_result(create_gap))
                tool_results.append(fallback_result)
                self._append_event(
                    job,
                    "repair_iteration",
                    "Fast create used the compact platform fallback after parallel coverage was incomplete.",
                    {"attempt": attempt, "reason": "fast_parallel_coverage_gap", "missing": create_gap},
                )
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready",
                    assistant_message="Fast parallel workers missed required create coverage, then platform fallback completed the app contract.",
                    diagnosis="Parallel workers returned a partial app; Fast mode applied a compact persisted fallback instead of spending another full LLM turn.",
                    operations=fallback_operations,
                    files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
                    expected_verification="Run validators plus generated Python/JS tests for persistence and role workflow coverage.",
                    rationale_by_file={operation.file_path: operation.reason for operation in fallback_operations},
                    metadata={"tool_results": list(tool_results), "fallback": "fast_parallel_coverage_gap"},
                )
            tool_results.append(self._create_patch_coverage_correction_result(create_gap))
            return None
        duration_ms = int((time.perf_counter() - started) * 1000)
        job.flow_coverage = {
            **dict(job.flow_coverage or {}),
            "status": "parallel_patch_ready",
            "parallel_worker_count": len(workers),
            "merged_file_count": len({operation.file_path for operation in merged_operations}),
        }
        self._append_event(
            job,
            "parallel_build_completed",
            "Fast parallel build produced a merged patch.",
            {
                "attempt": attempt,
                "duration_ms": duration_ms,
                "worker_count": len(workers),
                "operation_count": len(merged_operations),
                "operation_files": [operation.file_path for operation in merged_operations],
            },
        )
        return WorkspaceLoopTurnPlan(
            outcome="patch_ready",
            assistant_message="Fast parallel workers produced a merged product patch.",
            diagnosis="Fast built backend, client, specialist, manager, and generated tests in parallel ownership lanes, then merged non-conflicting operations.",
            operations=merged_operations,
            files_read=list({*seed_context.keys(), *extra_file_context.keys()}),
            expected_verification="Run static validators, frontend interaction smoke, generated Python/JS tests, and preview checks.",
            rationale_by_file={operation.file_path: operation.reason for operation in merged_operations},
            metadata={
                "parallel_build": True,
                "worker_count": len(workers),
                "worker_summaries": [
                    {
                        "worker": result.get("worker"),
                        "status": result.get("status"),
                        "operation_count": len(result.get("operations") or []),
                    }
                    for result in sorted(results, key=lambda item: str(item.get("worker") or ""))
                ],
            },
        )

    def _request_fast_parallel_worker(
        self,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        context_mode: str,
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        acceptance_contract: dict[str, Any],
        blueprint: dict[str, Any],
        worker: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        generation_mode = self._generation_mode(request.generation_mode)
        schema = self._agent_turn_schema(
            operation_limit=int(worker.get("operation_limit") or FAST_PARALLEL_WORKER_OPERATION_LIMIT),
            content_max_length=int(worker.get("content_max_length") or FAST_PARALLEL_WORKER_CONTENT_MAX_LENGTH),
            allow_tool_requests=False,
            allowed_outcomes=["patch_ready", "fatal_invalid_response"],
        )
        model = models_for_role("agent_turn", model_profile=request.model_profile, generation_mode=generation_mode)
        response = self.openai_client.generate_agent_turn(
            schema_name=f"fast_parallel_{worker['worker']}_v1",
            schema=schema,
            system_prompt=self._agent_system_prompt(),
            user_prompt=self._fast_parallel_worker_prompt(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                attempt=attempt,
                context_mode=context_mode,
                seed_context=seed_context,
                extra_file_context=extra_file_context,
                tool_results=tool_results,
                acceptance_contract=acceptance_contract,
                blueprint=blueprint,
                worker=worker,
            ),
            prompt_cache_key=self._prompt_cache_key(workspace_id, run_id, f"{request.prompt}:{worker['worker']}"),
            stable_prefix="workspace_code_agent_fast_parallel_v1",
            model_override=model,
            responses_tuning_override={"reasoning": {"effort": "low"}, "max_output_tokens": int(worker.get("max_output_tokens") or 14000)},
        )
        payload = response.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        parsed = payload if isinstance(payload, dict) else {}
        operations = self._coerce_operations(parsed.get("operations") or [])
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "worker": worker["worker"],
            "status": "completed",
            "outcome": str(parsed.get("outcome") or ""),
            "model": str(response.get("model") or ""),
            "payload": parsed,
            "operations": operations,
            "cache_stats": response.get("cache_stats") if isinstance(response.get("cache_stats"), dict) else {},
            "duration_ms": duration_ms,
        }

    def _fast_parallel_worker_prompt(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        attempt: int,
        context_mode: str,
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        acceptance_contract: dict[str, Any],
        blueprint: dict[str, Any],
        worker: dict[str, Any],
    ) -> str:
        payload = {
            "task": "Fast parallel worker: return only operations for your ownership zone. Other workers are running in parallel.",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "mode": request.mode,
            "intent": request.intent,
            "generation_mode": "fast",
            "attempt": attempt,
            "context_mode": context_mode,
            "worker": {
                "id": worker["worker"],
                "responsibility": worker["responsibility"],
                "ownership": worker["ownership"],
                "required_files": worker["required_files"],
            },
            "user_prompt": request.prompt,
            "acceptance_contract": acceptance_contract,
            "fast_parallel_blueprint": blueprint,
            "file_contexts": self._compact_file_contexts(
                {**seed_context, **extra_file_context},
                max_files=16,
                max_chars=4500,
            ),
            "tool_results": tool_results[-4:],
            "rules": [
                "Return outcome=patch_ready with create/replace/patch operations only inside your ownership list.",
                "Do not edit files owned by another worker. The merge layer rejects overlapping file paths.",
                "Use the exact required_files paths for this worker unless the prompt and blueprint require an additional file inside your ownership zone.",
                "Keep Fast compact but complete. Do not add mock/seed/demo/sample/preloaded business records.",
                "Generated UI must be usable with forms/buttons/fetch and must start from an empty state.",
                "Use one consistent light visual system and preserve preview_bridge/page-shell safe spacing.",
                "If you cannot satisfy your ownership zone, return fatal_invalid_response with a concrete diagnosis.",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _fast_parallel_blueprint(acceptance_contract: dict[str, Any]) -> dict[str, Any]:
        features = acceptance_contract.get("features") if isinstance(acceptance_contract.get("features"), dict) else {}
        commerce = bool(features.get("commerce_catalog_cart_order"))
        client_child = "catalog" if commerce else "request"
        specialist_child = "inventory" if commerce else "queue"
        manager_child = "overview"
        return {
            "roles": list(ROLE_ORDER),
            "commerce_flow": commerce,
            "backend_route_file": "miniapp/app/routes/app_api.py",
            "route_manifest": "miniapp/app/generated/route_manifest.json",
            "main_file": "miniapp/app/main.py",
            "resources": ["products", "orders"] if commerce else ["records"],
            "api_paths": ["/api/products", "/api/orders"] if commerce else ["/api/records"],
            "role_files": {
                "client": {
                    "root": "miniapp/app/static/client/index.html",
                    "child": f"miniapp/app/static/client/{client_child}/index.html",
                    "child_route": f"/client/{client_child}",
                    "app_js": "miniapp/app/static/client/app.js",
                    "css": "miniapp/app/static/client/styles.css",
                },
                "specialist": {
                    "root": "miniapp/app/static/specialist/index.html",
                    "child": f"miniapp/app/static/specialist/{specialist_child}/index.html",
                    "child_route": f"/specialist/{specialist_child}",
                    "app_js": "miniapp/app/static/specialist/app.js",
                    "css": "miniapp/app/static/specialist/styles.css",
                },
                "manager": {
                    "root": "miniapp/app/static/manager/index.html",
                    "child": f"miniapp/app/static/manager/{manager_child}/index.html",
                    "child_route": f"/manager/{manager_child}",
                    "app_js": "miniapp/app/static/manager/app.js",
                    "css": "miniapp/app/static/manager/styles.css",
                },
            },
            "test_files": [
                "miniapp/tests/test_generated_app.py",
                "miniapp/tests/generated_app.test.mjs",
            ],
        }

    @staticmethod
    def _fast_parallel_workers(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
        role_files = blueprint.get("role_files") if isinstance(blueprint.get("role_files"), dict) else {}
        return [
            {
                "worker": "backend_api",
                "responsibility": "Create shared persistence, API routes, main.py registration, and route_manifest for all role pages.",
                "ownership": [
                    "miniapp/app/routes/**",
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                    "miniapp/app/generated/route_manifest.json",
                ],
                "required_files": [
                    blueprint.get("backend_route_file"),
                    blueprint.get("main_file"),
                    blueprint.get("route_manifest"),
                ],
                "operation_limit": 4,
                "content_max_length": 15000,
                "max_output_tokens": 15000,
            },
            *[
                {
                    "worker": f"{role}_ui",
                    "responsibility": f"Create the {role} role app root, one child page, role JavaScript, and role CSS with role-specific workflow actions.",
                    "ownership": [f"miniapp/app/static/{role}/**"],
                    "required_files": [
                        role_payload.get("root"),
                        role_payload.get("child"),
                        role_payload.get("app_js"),
                        role_payload.get("css"),
                    ],
                    "operation_limit": 4,
                    "content_max_length": 14000,
                    "max_output_tokens": 14000,
                }
                for role, role_payload in role_files.items()
                if role in ROLE_ORDER and isinstance(role_payload, dict)
            ],
            {
                "worker": "generated_tests",
                "responsibility": "Create generated Python and JS tests that cover the complete acceptance contract and cross-role flow.",
                "ownership": ["miniapp/tests/**"],
                "required_files": list(blueprint.get("test_files") or []),
                "operation_limit": 2,
                "content_max_length": 16000,
                "max_output_tokens": 16000,
            },
        ]

    @classmethod
    def _merge_fast_parallel_worker_operations(cls, results: list[dict[str, Any]]) -> tuple[list[DraftFileOperation], str | None]:
        operations: list[DraftFileOperation] = []
        seen_paths: dict[str, str] = {}
        operation_index_by_path: dict[str, int] = {}
        worker_count = 0
        for result in results:
            worker = str(result.get("worker") or "").strip()
            if not worker:
                return [], "A parallel worker result did not include a worker id."
            if result.get("status") != "completed":
                return [], f"{worker} failed: {result.get('error') or result.get('status')}"
            if str(result.get("outcome") or "").strip().lower() != "patch_ready":
                return [], f"{worker} returned {result.get('outcome') or 'no outcome'} instead of patch_ready."
            raw_operations = result.get("operations")
            if not isinstance(raw_operations, list) or not raw_operations:
                return [], f"{worker} returned no operations."
            worker_count += 1
            ownership = cls._ownership_for_parallel_worker_result(result)
            for operation in raw_operations:
                if not isinstance(operation, DraftFileOperation):
                    return [], f"{worker} returned an invalid operation."
                path = str(operation.file_path or "").replace("\\", "/").lstrip("./")
                if not cls._path_matches_any_ownership(path, ownership):
                    return [], f"{worker} attempted to edit {path} outside ownership {ownership}."
                previous_worker = seen_paths.get(path)
                if previous_worker and previous_worker != worker:
                    return [], f"{path} was edited by both {previous_worker} and {worker}."
                seen_paths[path] = worker
                if path in operation_index_by_path:
                    operations[operation_index_by_path[path]] = operation
                    continue
                operation_index_by_path[path] = len(operations)
                operations.append(operation)
        if worker_count < FAST_PARALLEL_WORKER_COUNT:
            return [], f"Expected {FAST_PARALLEL_WORKER_COUNT} Fast parallel workers, got {worker_count}."
        return operations, None

    @staticmethod
    def _ownership_for_parallel_worker_result(result: dict[str, Any]) -> list[str]:
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        worker = str(result.get("worker") or "").strip()
        if isinstance(payload, dict):
            worker_payload = payload.get("worker")
            if isinstance(worker_payload, dict) and isinstance(worker_payload.get("ownership"), list):
                ownership = [str(item) for item in worker_payload.get("ownership") if str(item).strip()]
                if ownership:
                    return ownership
        if worker == "backend_api":
            return [
                "miniapp/app/routes/**",
                "miniapp/app/main.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
                "miniapp/app/generated/route_manifest.json",
            ]
        if worker == "generated_tests":
            return ["miniapp/tests/**"]
        for role in ROLE_ORDER:
            if worker == f"{role}_ui":
                return [f"miniapp/app/static/{role}/**"]
        return []

    @staticmethod
    def _path_matches_any_ownership(path: str, ownership: list[str]) -> bool:
        normalized = str(path or "").replace("\\", "/").lstrip("./")
        for raw_pattern in ownership:
            pattern = str(raw_pattern or "").replace("\\", "/").lstrip("./")
            if not pattern:
                continue
            if pattern.endswith("/**") and normalized.startswith(pattern[:-3].rstrip("/") + "/"):
                return True
            if normalized == pattern:
                return True
        return False

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
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> dict[str, Any]:
        turn_started_at = time.perf_counter()
        turn_started_iso = datetime.now(timezone.utc).isoformat()
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
        try:
            generation_mode = self._generation_mode(request.generation_mode)
            fast_create_turn = generation_mode == GenerationMode.FAST and str(request.intent or "").strip().lower() == "create"
            fast_first_create_patch = fast_create_turn and not self.workspace_service.diff(workspace_id, run_id=run_id).strip()
            focused_edit_kind = self._focused_edit_kind(request)
            focused_visual_edit = focused_edit_kind == "visual_style_edit"
            edit_turn = str(request.intent or "").strip().lower() in {"edit", "refine", "role_only_change"}
            compact_edit_turn = edit_turn and focused_edit_kind in {"small_copy_edit", "behavior_edit", "standard"}
            agentic_workflow_turn = (
                focused_edit_kind == "behavior_workflow_edit"
                and generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
            ) or (
                str(request.intent or "").strip().lower() == "create"
                and generation_mode in {GenerationMode.BALANCED, GenerationMode.QUALITY}
            )
            primary_model = models_for_role(
                "agent_turn",
                model_profile=request.model_profile,
                generation_mode=generation_mode,
            )
            operation_limit = (
                FOCUSED_VISUAL_OPERATION_LIMIT
                if focused_visual_edit
                else AGENTIC_WORKFLOW_OPERATION_LIMIT if agentic_workflow_turn
                else 8 if compact_edit_turn
                else 20 if fast_create_turn else AGENT_TURN_OPERATION_LIMIT
            )
            content_max_length = (
                FOCUSED_VISUAL_CONTENT_MAX_LENGTH
                if focused_visual_edit
                else 24000 if agentic_workflow_turn
                else 9000 if compact_edit_turn
                else 9000 if fast_create_turn else 18000
            )
            allow_tool_requests = False if focused_visual_edit else not fast_first_create_patch
            allowed_outcomes = (
                ["patch_ready", "fatal_invalid_response"]
                if focused_visual_edit
                else ["patch_ready"] if fast_first_create_patch else None
            )
            agent_schema = self._agent_turn_schema(
                operation_limit=operation_limit,
                content_max_length=content_max_length,
                allow_tool_requests=allow_tool_requests,
                allowed_outcomes=allowed_outcomes,
            )
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
                seed_context=seed_context,
                extra_file_context=extra_file_context,
                tool_results=tool_results,
                last_turn_summary=last_turn_summary,
                latest_diff_summary=latest_diff_summary,
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
                    "has_draft_diff": bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
                },
            )
            self._append_event(
                job,
                "agent_turn_started",
                "Workspace code agent is generating the structured edit.",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "context_mode": context_mode,
                    "phase": "model_request",
                    "has_draft_diff": bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
                },
            )
            response = self.openai_client.generate_agent_turn(
                schema_name="workspace_code_agent_turn_v1",
                schema=agent_schema,
                system_prompt=self._agent_system_prompt(),
                user_prompt=user_prompt,
                prompt_cache_key=self._prompt_cache_key(workspace_id, run_id, request.prompt),
                stable_prefix="workspace_code_agent_runtime_v2",
                model_override=primary_model,
                responses_tuning_override=self._agent_turn_tuning(
                    generation_mode,
                    intent=str(request.intent or ""),
                    focused_edit_kind=focused_edit_kind,
                ),
            )
            job.llm_model = str(response.get("model") or "")
            turn_cache_stats = response.get("cache_stats") or {}
            job.cache_stats = self._merge_cache_stats(job.cache_stats, turn_cache_stats)
            payload = response.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            parsed_payload = payload if isinstance(payload, dict) else {}
            raw_operations = parsed_payload.get("operations")
            raw_tool_requests = parsed_payload.get("tool_requests")
            operation_count = len(raw_operations) if isinstance(raw_operations, list) else 0
            tool_request_count = len(raw_tool_requests) if isinstance(raw_tool_requests, list) else 0
            duration_ms = int((time.perf_counter() - turn_started_at) * 1000)
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
                    "outcome": str(parsed_payload.get("outcome") or ""),
                    "operation_count": operation_count,
                    "operation_files": [
                        str(item.get("file_path") or "")
                        for item in raw_operations
                        if isinstance(item, dict) and str(item.get("file_path") or "").strip()
                    ] if isinstance(raw_operations, list) else [],
                    "tool_request_count": tool_request_count,
                    "tool_requests": [
                        {
                            "tool": str(item.get("tool") or ""),
                            "mode": str(item.get("mode") or ""),
                            "targets": [str(target) for target in item.get("targets") or []] if isinstance(item, dict) else [],
                        }
                        for item in raw_tool_requests
                        if isinstance(item, dict)
                    ] if isinstance(raw_tool_requests, list) else [],
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
                "Workspace code agent returned a structured turn.",
                {
                    "attempt": attempt,
                    "tool_round": tool_round,
                    "outcome": str(parsed_payload.get("outcome") or ""),
                    "operation_count": operation_count,
                    "tool_request_count": tool_request_count,
                    "model": job.llm_model,
                    "input_tokens": int(turn_cache_stats.get("input_tokens") or 0),
                    "output_tokens": int(turn_cache_stats.get("output_tokens") or 0),
                    "reasoning_tokens": int(turn_cache_stats.get("reasoning_tokens") or 0),
                    "total_tokens": int(turn_cache_stats.get("total_tokens") or 0),
                    "has_draft_diff": bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip()),
                },
            )
            return payload if isinstance(payload, dict) else {"error": "Agent returned a non-object payload."}
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
            else:
                logger.exception("workspace_code_agent_turn_failed workspace_id=%s run_id=%s", workspace_id, run_id)
            return {"error": f"Workspace code agent turn failed: {error_text}"}

    @staticmethod
    def _agent_turn_schema(
        *,
        operation_limit: int = AGENT_TURN_OPERATION_LIMIT,
        content_max_length: int = 18000,
        allow_tool_requests: bool = True,
        allowed_outcomes: list[str] | None = None,
    ) -> dict[str, Any]:
        outcome_values = allowed_outcomes or ["patch_ready", "tool_request", "no_progress", "fatal_invalid_response"]
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {"type": "string", "enum": outcome_values},
                "assistant_message": {"type": "string"},
                "diagnosis": {"type": "string"},
                "tool_requests": {
                    "type": "array",
                    "maxItems": 12 if allow_tool_requests else 0,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tool": {"type": "string", "enum": ["list_files", "read_files", "search_files", "inspect_diff", "run_checks", "run_command"]},
                            "mode": {"type": "string", "enum": ["exact", "final"]},
                            "targets": {"type": "array", "items": {"type": "string"}},
                            "pattern": {"type": "string"},
                            "command": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["tool", "targets", "reason"],
                    },
                },
                "operations": {
                    "type": "array",
                    "maxItems": operation_limit,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "file_path": {"type": "string", "maxLength": 220},
                            "operation": {"type": "string", "enum": ["create", "replace", "delete", "patch"]},
                            "content": {"type": ["string", "null"], "maxLength": content_max_length},
                            "diff": {"type": ["string", "null"], "maxLength": content_max_length},
                            "reason": {"type": "string", "maxLength": 600},
                        },
                        "required": ["file_path", "operation", "reason"],
                    },
                },
                "expected_verification": {"type": "string"},
                "rationale_by_file": {"type": "object", "additionalProperties": {"type": "string"}},
                "acceptance_checks": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "outcome",
                "assistant_message",
                "diagnosis",
                "tool_requests",
                "operations",
                "expected_verification",
                "rationale_by_file",
                "acceptance_checks",
            ],
        }

    @staticmethod
    def _agent_system_prompt() -> str:
        return (
            "You are a universal workspace code agent for a Telegram mini-app platform. "
            "Work like a coding agent: inspect files, return source edits as operations, request read-only validation checks, and converge to a working app. "
            "The user's prompt is the only source of requested behavior. Do not impose any generic queue, ticketing, lifecycle, or CRUD business model unless the prompt explicitly asks for it. "
            "Existing template docs and files are technical shell context only. They must not override the user's domain. "
            "Preserve the FastAPI + static-file shell, preview bridge, and role-root routing unless the user asks otherwise. "
            "Every generated HTML route page, including role roots and child pages such as static/client/details/index.html, must include <script src=\"/static/preview_bridge.js\" defer></script>. Role root pages must also keep the role app script. "
            "Generate normal light-mode interfaces by default: light backgrounds, dark readable text, clear contrast, restrained neutral colors, and no dark theme unless the user explicitly asks for dark mode. Do not give roles different color palettes; use one consistent light visual system across client, specialist, and manager. "
            "Preserve the top safe spacing from the template shell; generated role CSS must not collapse the top padding of .page-shell. Do not put plain .page-shell padding or padding-top in role CSS unless it keeps max(76px, calc(var(--telegram-top-safe-offset) + 12px)). Put role layout spacing on inner wrappers when possible. "
            "Visible generated UI copy must use the user's language. Do not paste raw prompt excerpts into the interface, and do not show technical role labels such as Client app, Specialist app, Manager app, source request, or Workspace suffixes unless the user explicitly asks for them. "
            "For create tasks, build three separate role apps inside one miniapp shell: client, specialist, and manager. They share backend state, but their UI logic, actions, text, CSS, and workflow focus must be visibly different. "
            "For create tasks and workflow behavior edits, first satisfy the extracted acceptance contract: roles, data resources, buttons, forms, APIs, cross-role visibility, and refresh persistence must all be covered by generated tests. "
            "When a prompt says a catalog, cart, checkout, button, post-submit visibility, or cross-role flow is broken, repair the whole workflow instead of making a narrow local patch. "
            "Role apps must be isolated inside the UI: do not add in-app links from client to specialist/manager, from specialist to client/manager, or from manager to client/specialist. The platform shell chooses the role entry; each role app may only link to its own child pages. "
            "Create tasks must be multi-page: each role root is a dashboard/hub and each role must expose child pages for the mode budget. Fast requires at least one domain-specific child page per role; Balanced and Quality require at least two. "
            "Use miniapp/app/generated/route_manifest.json to declare role routes for generated child pages. Prefer a compact routes map, either top-level \"routes\": {\"/client/details\": \"static/client/details/index.html\"} or per-role \"routes\" maps. "
            "Never declare a route_manifest route unless its target file already exists or is created/replaced in the same operations array. "
            "The three role surfaces must be different but connected parts of one prompt-derived business context: client creates and reviews user-submitted records, specialist processes work and changes operational status, and manager sees dashboard metrics, workload, and oversight controls. "
            "Never leave a role as a neutral starter, generic preview, blank page, or placeholder surface. "
            "For create tasks, never ship static-only mockups. Every generated app must be usable: users can fill forms, submit actions, and see saved records through backend APIs. "
            "When one role app.js is loaded by multiple pages for that role, page-specific DOM selectors must be null-safe: guard missing elements before property access/event listeners or split the code by page so one child page cannot break another page's catalog/list/form loading. "
            "Do not add mock data, seed data, demo data, sample data, fixture records, preloaded records, or hard-coded business records to generated app source. Start with empty persistent state and domain-specific empty states; generated tests may create their own test payloads. "
            "Every create task must create or replace real per-role CSS files at static/client/styles.css, static/specialist/styles.css, and static/manager/styles.css; placeholder CSS or missing role CSS is invalid. Child pages must link the matching role CSS. "
            "In Fast create mode, build a compact working MVP with three distinct role apps, one key child page per role, one persistent backend resource exposed through GET and POST APIs, a minimal status/update endpoint, frontend fetch/form/status code, compact role CSS, and generated tests that prove POST then GET persistence and status update. Do not add extra child pages, extra resources, or large visual systems in Fast. Do not spend Fast first-patch operations on role_routes.py, generated/models.py, or shared/base.css. "
            "In Balanced create mode, build deeper separate client/specialist/manager role apps with two or three connected persistent resources, or one resource plus meaningful status/update workflows, so roles share real state instead of copied static content. Balanced design quality must be visibly stronger than Fast: polished light-mode product UI, clear hierarchy, responsive role dashboards, refined forms/lists/status badges, and real CSS classes for spacing, typography, buttons, cards, and states without decorative bloat. "
            "In Quality create mode, build detailed separate client/specialist/manager role apps with several API endpoints, create/list/update behavior where useful, robust empty/error states, richer role-specific CSS, and broader generated acceptance tests. Quality design quality must be top-tier and product-ready: highly refined layout, typography, spacing, accessible form states, empty/loading/error/success states, responsive data presentation, role-specific dashboards, and cohesive light visual design implemented in CSS, not just described in text. "
            "For every create task, write generated app tests for both backend and frontend surfaces: miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs. "
            "Create-task Python tests must verify backend API behavior: GET starts empty, POST creates a user-provided record, and a later GET returns that record. "
            "Generated tests must validate the requested behavior, all role roots, and shared role content without relying on network calls or non-template dependencies. "
            "For edit tasks, preserve existing selectors, ids, and data-testid attributes that generated tests assert unless the requested behavior intentionally replaces them; when behavior changes test expectations, update the generated test file in the same patch. "
            "Python generated tests run through FastAPI TestClient and see server-rendered HTML before browser JavaScript executes; do not assert JS-rendered item text there unless the text is also present in HTML fallback/source. Use Python tests for route status, preview bridge/static shell, and backend APIs when present. "
            "JS generated tests are responsible for frontend source/data assertions; read role HTML/JS/shared data files directly with node:test, node:assert, and fs/path. "
            "node:test does not export expect; generated_app.test.mjs must use import test from \"node:test\" and import assert from \"node:assert\" with assert.ok/assert.equal/assert.match. "
            "Before writing generated_app.test.mjs, ensure every exact phrase asserted by includes() or match() is literally present in the file being read; prefer stable headings, route links, app title, and data-testid values over paraphrased expectations. "
            "Generated JS tests execute from the miniapp directory, so file paths inside those tests must start with app/static/... or be resolved from import.meta.url to ../app/static; do not use miniapp/app/... in generated tests. "
            "In generated JS tests, path/fs APIs require string paths: use path.join(process.cwd(), 'app/static/...') or fileURLToPath(new URL('../app/static/...', import.meta.url)); never pass a URL object directly to path.resolve, path.join, or fs. "
            "In Fast create mode, if the provided file_contexts already include the role HTML shells and tests directory context, return a compact patch_ready response immediately instead of planning an exhaustive app architecture. "
            "Tools are diagnostic only. They cannot write files, execute arbitrary scripts, install packages, fetch networks, or apply changes. "
            "run_checks is a read-only platform validation snapshot for the current draft; run_command is diagnostic-only and limited to safe test/search/read commands. Neither tool can rewrite files. "
            "All code changes must be returned in the operations array of the same structured JSON response. "
            "Use hunk patches when small edits are enough. Use full-file create/replace only when creating or substantially rewriting a file. "
            "Do not edit generated app tests or generated manifests to hide failures. For edits, update tests only when the requested product behavior changes; for fixes, repair app code unless a test expectation is clearly stale because of the requested change. "
            "Return only the structured JSON payload requested by the schema."
        )

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
        seed_context: dict[str, str],
        extra_file_context: dict[str, str],
        tool_results: list[dict[str, object]],
        last_turn_summary: str | None,
        latest_diff_summary: str | None,
    ) -> str:
        file_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        focused_edit_kind = self._focused_edit_kind(request)
        focused_visual_edit = focused_edit_kind == "visual_style_edit"
        focused_edit_files = self._focused_visual_css_paths(request.target_role_scope or ROLE_ORDER) if focused_visual_edit else []
        acceptance_contract = build_acceptance_contract(
            prompt=request.prompt,
            intent=str(request.intent or ""),
            generation_mode=request.generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        orchestration = orchestration_metadata_for_contract(
            contract=acceptance_contract,
            generation_mode=request.generation_mode,
            focused_edit_kind=focused_edit_kind,
        )
        focused_rules: list[str] = []
        if focused_visual_edit:
            focused_rules.extend(
                [
                    "Focused visual_style_edit lane: change only CSS/style files listed in focused_edit_files. Do not edit HTML, JavaScript, backend routes, route_manifest.json, generated tests, docs, or unrelated files.",
                    f"Return one compact patch_ready response with at most {FOCUSED_VISUAL_OPERATION_LIMIT} operations and no tool requests. Prefer full-file replace for CSS if a hunk patch would be ambiguous or has already conflicted.",
                    "Do not add product behavior, data, tests, API calls, navigation, or role copy for a pure style/color/spacing prompt.",
                    "CSS may use hex/rgb/hsl values for requested colors; the literal user color word does not need to appear in generated CSS.",
                    "Keep the existing top safe spacing and existing selectors/data-testid hooks intact while changing the visual styling.",
                ]
            )
        payload = {
            "task": "Edit the draft workspace to satisfy the user prompt and pass platform invariant checks.",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "mode": request.mode,
            "intent": request.intent,
            "generation_mode": str(getattr(request.generation_mode, "value", request.generation_mode) or ""),
            "focused_edit_kind": focused_edit_kind,
            "focused_edit_files": focused_edit_files,
            "acceptance_contract": acceptance_contract,
            "orchestration": orchestration,
            "attempt": attempt,
            "tool_round": tool_round,
            "context_mode": context_mode,
            "repeated_no_progress": repeated_no_progress,
            "user_prompt": request.prompt,
            "error_context": request.error_context.model_dump(mode="json") if request.error_context else None,
            "role_scope": list(request.target_role_scope or ROLE_ORDER),
            "fast_create_required_file_set": (
                [
                    "miniapp/app/static/client/index.html",
                    "miniapp/app/static/client/request/index.html",
                    "miniapp/app/static/client/app.js",
                    "miniapp/app/static/client/styles.css",
                    "miniapp/app/static/specialist/index.html",
                    "miniapp/app/static/specialist/queue/index.html",
                    "miniapp/app/static/specialist/app.js",
                    "miniapp/app/static/specialist/styles.css",
                    "miniapp/app/static/manager/index.html",
                    "miniapp/app/static/manager/overview/index.html",
                    "miniapp/app/static/manager/app.js",
                    "miniapp/app/static/manager/styles.css",
                    "miniapp/app/generated/route_manifest.json",
                    "miniapp/app/routes/<domain_resource>.py",
                    "miniapp/app/main.py",
                    "miniapp/tests/test_generated_app.py",
                    "miniapp/tests/generated_app.test.mjs",
                ]
                if self._generation_mode(request.generation_mode) == GenerationMode.FAST
                and str(request.intent or "").strip().lower() == "create"
                else []
            ),
            "file_tree": file_tree[:120] if focused_visual_edit else file_tree[:240],
            "file_contexts": {
                **self._compact_file_contexts(
                    seed_context,
                    max_files=8 if focused_visual_edit else 22 if context_mode == "full_bundle" else 14,
                    max_chars=9000 if focused_visual_edit else 6000,
                ),
                **self._compact_file_contexts(extra_file_context, max_files=4 if focused_visual_edit else 12),
            },
            "context_pack": (
                {}
                if focused_visual_edit
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
            "latest_checks": self._compact_checks(latest_execution),
            "preview": latest_preview_details,
            "latest_diff_summary": latest_diff_summary,
            "last_turn_summary": last_turn_summary,
            "tool_results": tool_results[-8:],
            "rules": [
                *focused_rules,
                f"Outside focused edit lanes, keep each turn applyable: return up to {AGENT_TURN_OPERATION_LIMIT} independent file operations together when they are part of the same coherent change.",
                "For create and behavior_workflow_edit tasks, treat acceptance_contract as blocking product scope: every listed flow must have UI controls, JavaScript handlers, backend API persistence, cross-role visibility, refresh persistence, and generated tests.",
                "If focused_edit_kind is behavior_workflow_edit, do not make a narrow one-file cosmetic fix. Repair the full user workflow across backend, client, specialist, manager, and generated tests so the scenario can run end to end.",
                "For Balanced and Quality workflow runs, follow the orchestration ownership lanes: backend/API, client UI, specialist UI, manager UI, and generated tests should be reasoned as separate workstreams, then merged without overlapping ownership conflicts.",
                "If acceptance_contract contains commerce_catalog_cart_order, implement the complete flow: specialist creates products, client catalog loads products, add-to-cart has an effective click handler and cart state, checkout POSTs an order, and specialist/manager can see and update that saved order.",
                "For Fast create tasks, the first model answer should usually be outcome=patch_ready, not tool_request or no_progress, because the template file_contexts already provide the shell.",
                "For Fast create tasks, output one compact working MVP pass instead of a giant app: up to 20 concise operations covering route_manifest.json, three role roots, one key child page per role, role app.js files, role styles.css files, one backend API route module, main.py router registration, generated tests, and form/fetch/status code. Avoid verbose comments, inline styles, large fixtures, and repeated tool reads for files already shown in file_contexts.",
                "Fast size contract: no extra pages beyond one child per role, no extra API resources, compact but real per-role CSS, no long narrative copy. Keep each HTML file roughly under 160 lines and keep CSS focused on layout, forms, lists, cards, buttons, and status badges. Use one consistent neutral light palette across roles.",
                "For Fast create tasks, use the fast_create_required_file_set as the patch skeleton. Do not spend operations on miniapp/app/routes/role_routes.py, miniapp/app/generated/models.py, or miniapp/app/static/shared/base.css in the first create patch.",
                "For Fast create tasks, include exactly the backend needed for one persistent resource with GET, POST, and minimal PATCH/status update; do not fall back to frontend-only/static-only pages.",
                "For Balanced create tasks, include deeper separate client/specialist/manager apps with two or three connected persistent resources, or one resource plus meaningful status/update endpoints, so roles share real saved state.",
                "Balanced design quality: make the UI materially more polished than Fast with clean light-mode hierarchy, responsive dashboards, refined forms/lists/status badges, and meaningful CSS classes for spacing, typography, buttons, cards, and states.",
                "For Quality create tasks, include detailed separate role workflows, several backend endpoints, create/list/update behavior where useful, richer role-specific CSS, and broader generated Python/JS acceptance tests.",
                "Quality design quality: make the app look production-ready with highly refined layout, typography, spacing, responsive data presentation, accessible interaction states, empty/loading/error/success states, and cohesive light visual design implemented in CSS.",
                "For Fast create tasks, do not stop after only three role hub pages. Include one key child page per role in the same first create patch whenever possible.",
                "For every create task, distribute the first create patch evenly: replace all three role roots, create required child pages for each role before adding extra pages to any one role, create route_manifest.json, and create both generated test files. A client-only first patch will be rejected.",
                "For Fast create tasks, each role must have at least two routeable pages: /<role> plus at least one /<role>/<slug> page. Balanced and Quality should have at least three routeable pages per role.",
                "Do not declare child/profile/settings routes unless you also create those exact files in the same response or they already exist. If staging the work, wait to add child routes until creating their files.",
                "Do not copy a fixed page pattern such as profile/settings unless the user explicitly asked for it; child pages should come from the user's request.",
                "Each role root should visibly link only to its own child pages and implement role-specific workflow rather than reusing the same list page for all roles. Do not include links between client, specialist, and manager role roots.",
                "If a role app.js is shared across role child pages, initialize each page only when its DOM elements exist. Do not call render/list/cart/form functions that dereference missing elements on another child page.",
                "For create tasks, do not include mock data, seed data, demo data, sample data, fixture records, preloaded records, or hard-coded business records in generated app source.",
                "For create tasks, app state starts empty. Use domain-specific empty-state copy plus forms/buttons so users can add their own records.",
                "For Fast create tasks, keep each generated HTML/JS/CSS/test file compact and use zero preloaded business records.",
                "For Fast create tasks, create the required role CSS files for client, specialist, and manager, but keep them compact and consistent instead of building a large visual system.",
                "Use light mode by default for all generated UI: light surfaces, dark text, accessible contrast, and no dark backgrounds unless the user explicitly requested dark mode.",
                "For broad create tasks, batch independent backend/static/style files in one response when the changes are clear; otherwise patch the most important working slice first and continue after checks.",
                "For create tasks, generate client, specialist, and manager role roots even if the prompt mentions only one audience.",
                "Each role root must be domain-specific, non-placeholder, and connected to the same product content; specialist is required and cannot remain the starter screen.",
                "For create tasks, include dependency-free generated tests in miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs.",
                "For create tasks, generated Python tests must cover persistent API behavior: GET returns an empty list first, POST creates a user-supplied record, and a later GET returns the created record.",
                "Python generated tests should use unittest plus FastAPI TestClient to verify role routes and backend/API behavior when backend state exists.",
                "JS generated tests should use node:test plus node:assert plus fs/path only to verify role HTML/JS content, shared domain labels, role-specific selectors/actions, and absence of neutral template text; do not import or call expect.",
                "In generated_app.test.mjs, only assert exact strings that literally appear in the file being read; do not paraphrase expected UI text.",
                "Do not make Python TestClient tests assert text that is only rendered by browser JavaScript; either include the text in HTML fallback/source or move that assertion to generated_app.test.mjs.",
                "When role content is populated from a shared JS data file, JS generated tests should read that data/source file directly and assert the shared item names there.",
                "Generated JS tests run with cwd=miniapp, so read app/static/<role>/... paths or resolve from import.meta.url to ../app/static; never prefix paths with miniapp/app/ inside the test.",
                "In generated_app.test.mjs, prefer path.join(process.cwd(), 'app/static/<role>/index.html') for fixture paths. If using import.meta.url, wrap new URL(...) with fileURLToPath(...) before passing it to path/fs APIs.",
                "For edit/refine tasks, update generated tests when the requested behavior changes so the new behavior is covered.",
                "For edit/refine tasks, preserve existing ids/selectors/data-testid values that tests or existing scripts rely on, unless you update the affected generated test in the same response.",
                "For edit/refine/fix/repair tasks, use patch-first operations for existing files: return unified hunk diffs instead of whole-file replace whenever the target file already exists.",
                "For edit/refine/fix/repair tasks, full-file replace is only acceptable for new files, tiny existing files, create-mode work, or one conflicted file after a patch apply conflict.",
                "For edit/refine tasks, keep the patch focused on one visible slice in the requested role files and usually return 1-2 operations: one small app-code patch plus one generated-test patch when tests need new expectations.",
                "For edit/refine tasks, keep the response compact; prefer a hunk patch under 200 changed lines over a whole-file rewrite for feature additions.",
                "For edit/refine tasks, do not rewrite the whole app or unrelated files; make the smallest complete visible change that satisfies the prompt.",
                "If generated_app_python_tests or generated_app_js_tests failed, read the failure logs in latest_checks and patch the app or generated tests so that exact failure changes on the next attempt; do not repeat app-only patches that leave the same generated test failure.",
                "If role_scope contains only client, prioritize miniapp/app/static/client files and do not change backend unless the prompt explicitly asks for API/data changes.",
                "If role_scope contains only manager, prioritize miniapp/app/static/manager files and do not change backend unless the prompt explicitly asks for API/data changes.",
                "Every generated HTML route page must include /static/preview_bridge.js; role root pages should place it before the role app script. If a check reports page_missing_preview_bridge, patch only the missing script tag instead of rewriting the whole app.",
                "If a check reports build.missing_static_page for route_manifest, either create that exact HTML file or remove the route from route_manifest; do not rewrite unrelated pages.",
                "Prefer targeted patch operations over full-file replace when the file already exists; the runtime may convert large existing-file replaces into unified diffs.",
                "If the latest turn reports a patch apply conflict, return a full-file replace for that conflicted file unless the exact corrected hunk is obvious.",
                "If you need more context, request list_files/read_files/search_files/inspect_diff/run_checks/run_command.",
                "Tools are read-only diagnostics. They cannot write files, install packages, fetch networks, or apply edits.",
                "run_checks only returns a platform validation snapshot for the current draft. run_command can only run safe diagnostics such as python -m unittest, python -m py_compile, node --test, node --check, rg, sed, and ls.",
                "If you have enough context, return outcome=patch_ready with operations.",
                "All writes must be represented as operations. Never wait for a tool to write code for you.",
                "Every create or replace operation must include full resulting file content.",
                "Every patch operation must include a unified diff for exactly file_path.",
                "Do not create generic queue, ticketing, lifecycle, or CRUD language unless the prompt asks for it.",
                "Do not return no_progress unless you can explain the exact unresolved blocker.",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

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
    def _target_files_from_execution(execution: CheckExecutionRecord) -> list[str]:
        paths: list[str] = []
        for result in execution.results:
            diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
            candidates = [
                diagnostics.get("file_path"),
                diagnostics.get("path"),
                diagnostics.get("location"),
            ]
            syntax_error = diagnostics.get("static_js_syntax_error")
            if isinstance(syntax_error, dict):
                candidates.append(syntax_error.get("file_path"))
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
            for candidate in candidates:
                path = str(candidate or "").strip().replace("\\", "/").lstrip("./")
                if path and path.startswith("miniapp/") and path not in paths:
                    paths.append(path)
        return paths[:16]

    def _execute_tool_requests(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        tool_requests: list[dict[str, Any]],
        execute_checks: Callable[[list[str]], tuple[CheckExecutionRecord, dict[str, Any]]],
    ) -> tuple[dict[str, str], list[dict[str, object]]]:
        loaded_context: dict[str, str] = {}
        tool_results: list[dict[str, object]] = []
        workspace_tree = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        for request_item in tool_requests:
            tool_name = str(request_item.get("tool") or "").strip().lower()
            targets = [str(item or "").strip().lstrip("./") for item in request_item.get("targets") or [] if str(item or "").strip()]
            reason = str(request_item.get("reason") or "").strip()
            if tool_name == "list_files":
                tool_results.append({**list_workspace_files(workspace_tree=workspace_tree, targets=targets), "reason": reason})
                continue
            if tool_name == "read_files":
                for target in targets[:16]:
                    content = self.workspace_service.try_read_text_file(workspace_id, target, run_id=run_id)
                    if content is not None:
                        loaded_context[target] = content
                tool_results.append(
                    {
                        "tool": "read_files",
                        "targets": targets,
                        "files": summarize_read_file_payloads(file_contents=loaded_context),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "search_files":
                pattern = str(request_item.get("pattern") or "").strip()
                tool_results.append(
                    {
                        **search_workspace_files(
                            workspace_tree=workspace_tree,
                            read_text_file=lambda relative_path: self.workspace_service.try_read_text_file(
                                workspace_id,
                                relative_path,
                                run_id=run_id,
                            ),
                            pattern=pattern,
                            targets=targets,
                        ),
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "inspect_diff":
                tool_results.append({**self._inspect_diff(workspace_id=workspace_id, run_id=run_id, targets=targets), "reason": reason})
                continue
            if tool_name == "run_checks":
                mode = str(request_item.get("mode") or "exact").strip().lower()
                execution, preview_details = execute_checks(targets or ["miniapp"])
                failed_checks = [
                    {
                        "name": result.name,
                        "details": result.details,
                        "command": result.command,
                        "logs": result.logs[-8:],
                    }
                    for result in execution.results
                    if result.status == "failed"
                ]
                tool_results.append(
                    {
                        "tool": "run_checks",
                        "contract": "read_only_validation_snapshot",
                        "writes_files": False,
                        "executes_arbitrary_commands": False,
                        "mode": mode,
                        "targets": targets or ["miniapp"],
                        "failed_checks": failed_checks,
                        "preview": preview_details,
                        "reason": reason,
                    }
                )
                continue
            if tool_name == "run_command":
                command = str(request_item.get("command") or "").strip()
                tool_results.append(
                    {
                        **run_workspace_command(
                            draft_source=draft_source,
                            command=command,
                            timeout_seconds=25,
                        ),
                        "reason": reason,
                    }
                )
        return loaded_context, tool_results

    @staticmethod
    def _agent_turn_tuning(
        generation_mode: GenerationMode,
        *,
        intent: str = "",
        focused_edit_kind: str = "",
    ) -> dict[str, Any]:
        if focused_edit_kind == "visual_style_edit":
            return {"reasoning": {"effort": "low"}, "max_output_tokens": FOCUSED_VISUAL_CONTENT_MAX_LENGTH}
        if focused_edit_kind in {"small_copy_edit", "behavior_edit"}:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 12000}
        if str(intent or "").lower() in {"edit", "refine", "role_only_change"}:
            return {"reasoning": {"effort": "low"}, "max_output_tokens": 14000}
        if str(intent or "").lower() == "create":
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
    def _output_cap_correction_result(payload: dict[str, Any], *, request: GenerateRequest) -> dict[str, object]:
        create_task = str(request.intent or "").lower() == "create"
        if create_task:
            next_action = (
                "The previous answer was too large. Return outcome=patch_ready now with a compact, complete first implementation. "
                "Use no more than 12 concise file operations in this recovery turn, avoid inline styles and long comments, and do not request more context unless a specific required file is absent. "
                "Keep the app working, not static-only: include one compact backend API route module with GET/POST persistence, main.py router registration, form/fetch frontend code, and generated tests. "
                "Do not add mock data, seed data, demo data, sample data, fixture records, preloaded records, or hard-coded business records."
            )
        else:
            next_action = (
                "The previous answer was too large. Return outcome=patch_ready now with 1-2 focused operations for the requested edit. "
                "Prefer full-file replace for the single visible role file that needs the change instead of fragile multi-file hunks. "
                "Do not request more context unless a required file is absent."
            )
        return {
            "tool": "output_cap_correction",
            "contract": "The structured JSON response exceeded the model output cap; tools cannot recover this automatically.",
            "required_next_action": next_action,
            "previous_error": str(payload.get("error") or "")[:1200],
        }

    @staticmethod
    def _tool_budget_correction_result(tool_requests: list[dict[str, Any]], *, request: GenerateRequest) -> dict[str, object]:
        create_task = str(request.intent or "").strip().lower() == "create"
        return {
            "tool": "tool_budget_correction",
            "contract": "Fast mode has enough template and validation context to start editing without additional diagnostic reads.",
            "required_next_action": (
                "Return outcome=patch_ready now. Do not request more tools in the next answer. "
                "Use the file_contexts and latest_checks already provided. "
                + (
                    "For create, produce compact file operations now. If this is the first create patch, create all required Fast role routes, one backend GET/POST resource, main.py router registration, form/fetch frontend behavior, route_manifest.json, and generated tests in no more than 12 concise operations. Keep HTML concise and do not include large inline style blocks or preloaded business records."
                    if create_task
                    else "For edit/fix, patch the smallest complete set of files needed for the requested behavior."
                )
            ),
            "ignored_tool_requests": [
                {
                    "tool": str(item.get("tool") or ""),
                    "targets": [str(target) for target in item.get("targets") or []] if isinstance(item, dict) else [],
                    "reason": str(item.get("reason") or "")[:400] if isinstance(item, dict) else "",
                }
                for item in tool_requests
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _fast_create_fallback_result(request: GenerateRequest) -> dict[str, object]:
        spec = WorkspaceCodeAgentRuntime._fast_create_domain_spec(request.prompt)
        return {
            "tool": "fast_create_platform_fallback",
            "contract": "Fast create must return a working persisted app even when the model exceeds the output cap.",
            "domain": spec["title"],
            "required_next_action": "Apply the compact platform fallback operations, then run validators and generated app tests.",
        }

    @staticmethod
    def _mark_fast_create_fallback_job(job: JobRecord) -> None:
        job.llm_model = job.llm_model or "platform_fast_fallback"
        if not isinstance(job.token_usage, dict) or not job.token_usage:
            job.token_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "turn_count": 0,
                "last_turn": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                },
            }

    @staticmethod
    def _fast_create_should_use_fallback_repair(latest_execution: CheckExecutionRecord) -> bool:
        markers = (
            "no such table",
            "operationalerror",
            "post exists but",
            "was not persisted",
            "did not persist",
            "missing_create_post_api",
            "missing_create_get_api",
            "missing_create_update_api",
            "frontend_missing_post_api",
            "frontend_missing_update_api",
            "placeholder_role_css",
            "identical_role_surfaces",
            "missing_role_workflow_actions",
        )
        for result in latest_execution.results:
            if result.status != "failed":
                continue
            if result.name not in {"generated_app_python_tests", "platform_invariants", "connectivity_validators"}:
                continue
            text = "\n".join([str(result.details or ""), *[str(line or "") for line in result.logs]]).lower()
            if any(marker in text for marker in markers):
                return True
        return False

    def _fast_create_fallback_operations_with_cleanup(
        self,
        request: GenerateRequest,
        *,
        workspace_id: str,
        run_id: str,
    ) -> list[DraftFileOperation]:
        operations = list(self._fast_create_fallback_operations(request))
        keep_paths = {operation.file_path for operation in operations if operation.file_path.startswith("miniapp/app/routes/")}
        protected_paths = {
            "miniapp/app/routes/__init__.py",
            "miniapp/app/routes/health.py",
            "miniapp/app/routes/role_pages.py",
            "miniapp/app/routes/role_routes.py",
        }
        try:
            existing_files = self.workspace_service.file_tree(workspace_id, run_id=run_id)
        except Exception:
            existing_files = []
        for item in existing_files:
            path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("./")
            if item.get("type") != "file":
                continue
            if not re.fullmatch(r"miniapp/app/routes/[^/]+\.py", path):
                continue
            if path in keep_paths or path in protected_paths:
                continue
            operations.append(
                DraftFileOperation(
                    file_path=path,
                    operation="delete",
                    reason="Remove stale generated route module before applying the compact Fast fallback.",
                )
            )
        return operations

    @staticmethod
    def _fast_create_fallback_operations(request: GenerateRequest) -> list[DraftFileOperation]:
        if WorkspaceCodeAgentRuntime._fast_create_uses_commerce_flow(request.prompt):
            return WorkspaceCodeAgentRuntime._fast_commerce_fallback_operations(request)
        spec = WorkspaceCodeAgentRuntime._fast_create_domain_spec(request.prompt)
        resource = str(spec["resource"])
        api_path = f"/api/{resource}"
        route_file = f"miniapp/app/routes/{resource}.py"

        route_manifest = {
            "routes": {
                "/client/request": "static/client/request/index.html",
                "/specialist/queue": "static/specialist/queue/index.html",
                "/manager/overview": "static/manager/overview/index.html",
            }
        }

        def op(path: str, content: str, reason: str) -> DraftFileOperation:
            return DraftFileOperation(file_path=path, operation="replace", content=content, reason=reason)

        return [
            op("miniapp/app/static/client/index.html", WorkspaceCodeAgentRuntime._fast_client_html(spec, api_path), "Create client role form and saved-record list."),
            op("miniapp/app/static/client/request/index.html", WorkspaceCodeAgentRuntime._fast_client_child_html(spec, api_path), "Create client child page."),
            op("miniapp/app/static/client/app.js", WorkspaceCodeAgentRuntime._fast_client_js(spec, api_path), "Create client-only form and saved-record logic."),
            op("miniapp/app/static/client/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("client"), "Create real client role styles."),
            op("miniapp/app/static/specialist/index.html", WorkspaceCodeAgentRuntime._fast_specialist_html(spec, api_path), "Create specialist role queue and status workflow."),
            op("miniapp/app/static/specialist/queue/index.html", WorkspaceCodeAgentRuntime._fast_specialist_child_html(spec, api_path), "Create specialist child page."),
            op("miniapp/app/static/specialist/app.js", WorkspaceCodeAgentRuntime._fast_specialist_js(spec, api_path), "Create specialist status action logic."),
            op("miniapp/app/static/specialist/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("specialist"), "Create real specialist role styles."),
            op("miniapp/app/static/manager/index.html", WorkspaceCodeAgentRuntime._fast_manager_html(spec, api_path), "Create manager role dashboard and workload overview."),
            op("miniapp/app/static/manager/overview/index.html", WorkspaceCodeAgentRuntime._fast_manager_child_html(spec, api_path), "Create manager child page."),
            op("miniapp/app/static/manager/app.js", WorkspaceCodeAgentRuntime._fast_manager_js(spec, api_path), "Create manager dashboard summary logic."),
            op("miniapp/app/static/manager/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("manager"), "Create real manager role styles."),
            op("miniapp/app/generated/route_manifest.json", json.dumps(route_manifest, indent=2, ensure_ascii=False) + "\n", "Declare Fast child routes."),
            op(route_file, WorkspaceCodeAgentRuntime._fast_api_route_py(spec, api_path), "Create persistent GET/POST API resource."),
            op("miniapp/app/main.py", WorkspaceCodeAgentRuntime._fast_main_py(resource), "Register the generated API route."),
            op("miniapp/tests/test_generated_app.py", WorkspaceCodeAgentRuntime._fast_python_test_py(spec, api_path, resource), "Test API persistence and role routes."),
            op("miniapp/tests/generated_app.test.mjs", WorkspaceCodeAgentRuntime._fast_js_test_mjs(spec, api_path), "Test frontend source and role pages."),
        ]

    @staticmethod
    def _fast_create_uses_commerce_flow(prompt: str) -> bool:
        contract = build_acceptance_contract(
            prompt=prompt,
            intent="create",
            generation_mode=GenerationMode.FAST,
            focused_edit_kind="standard",
        )
        return bool((contract.get("features") or {}).get("commerce_catalog_cart_order"))

    @staticmethod
    def _fast_commerce_fallback_operations(request: GenerateRequest) -> list[DraftFileOperation]:
        spec = WorkspaceCodeAgentRuntime._fast_create_domain_spec(request.prompt)
        route_manifest = {
            "routes": {
                "/client/catalog": "static/client/catalog/index.html",
                "/specialist/inventory": "static/specialist/inventory/index.html",
                "/manager/overview": "static/manager/overview/index.html",
            }
        }

        def op(path: str, content: str, reason: str) -> DraftFileOperation:
            return DraftFileOperation(file_path=path, operation="replace", content=content, reason=reason)

        return [
            op("miniapp/app/static/client/index.html", WorkspaceCodeAgentRuntime._fast_commerce_client_html(spec, child=False), "Create client catalog, cart, and checkout surface."),
            op("miniapp/app/static/client/catalog/index.html", WorkspaceCodeAgentRuntime._fast_commerce_client_html(spec, child=True), "Create client catalog child page."),
            op("miniapp/app/static/client/app.js", WorkspaceCodeAgentRuntime._fast_commerce_client_js(spec), "Create catalog loading, add-to-cart, and checkout persistence."),
            op("miniapp/app/static/client/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("client"), "Create real client role styles."),
            op("miniapp/app/static/specialist/index.html", WorkspaceCodeAgentRuntime._fast_commerce_specialist_html(spec, child=False), "Create specialist inventory and order queue surface."),
            op("miniapp/app/static/specialist/inventory/index.html", WorkspaceCodeAgentRuntime._fast_commerce_specialist_html(spec, child=True), "Create specialist inventory child page."),
            op("miniapp/app/static/specialist/app.js", WorkspaceCodeAgentRuntime._fast_commerce_specialist_js(), "Create product POST flow and order status workflow."),
            op("miniapp/app/static/specialist/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("specialist"), "Create real specialist role styles."),
            op("miniapp/app/static/manager/index.html", WorkspaceCodeAgentRuntime._fast_commerce_manager_html(spec, child=False), "Create manager dashboard surface."),
            op("miniapp/app/static/manager/overview/index.html", WorkspaceCodeAgentRuntime._fast_commerce_manager_html(spec, child=True), "Create manager dashboard child page."),
            op("miniapp/app/static/manager/app.js", WorkspaceCodeAgentRuntime._fast_commerce_manager_js(), "Create management metrics and order review workflow."),
            op("miniapp/app/static/manager/styles.css", WorkspaceCodeAgentRuntime._fast_role_css("manager"), "Create real manager role styles."),
            op("miniapp/app/generated/route_manifest.json", json.dumps(route_manifest, indent=2, ensure_ascii=False) + "\n", "Declare commerce child routes."),
            op("miniapp/app/routes/commerce.py", WorkspaceCodeAgentRuntime._fast_commerce_route_py(), "Create persistent products and orders APIs."),
            op("miniapp/app/main.py", WorkspaceCodeAgentRuntime._fast_commerce_main_py(), "Register the commerce API route."),
            op("miniapp/tests/test_generated_app.py", WorkspaceCodeAgentRuntime._fast_commerce_python_test_py(spec), "Test product/order persistence and status updates."),
            op("miniapp/tests/generated_app.test.mjs", WorkspaceCodeAgentRuntime._fast_commerce_js_test_mjs(spec), "Test commerce frontend source and role pages."),
        ]

    @staticmethod
    def _fast_commerce_client_html(spec: dict[str, Any], *, child: bool) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        lang = WorkspaceCodeAgentRuntime._html_escape(spec["copy"]["lang"])
        back_href = "/client" if child else "/client/catalog"
        nav_text = "Назад" if child and lang == "ru" else "Каталог" if lang == "ru" else "Back" if child else "Catalog"
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/client/styles.css" />
</head>
<body>
  <main class="page-shell client-app">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="{back_href}">{nav_text}</a></nav>
    </header>
    <section class="records-panel">
      <h2>{'Каталог товаров' if lang == 'ru' else 'Product catalog'}</h2>
      <p id="catalog-empty">{'Пока нет товаров. Они появятся после добавления сотрудником.' if lang == 'ru' else 'No products yet. They will appear after staff adds them.'}</p>
      <ul id="catalog-list"></ul>
    </section>
    <section class="action-panel">
      <h2>{'Корзина и оформление' if lang == 'ru' else 'Cart and checkout'}</h2>
      <ul id="cart-list"></ul>
      <p id="cart-empty">{'Корзина пуста.' if lang == 'ru' else 'Cart is empty.'}</p>
      <form id="checkout-form" class="request-form">
        <label>{'Имя' if lang == 'ru' else 'Name'}<input name="customer_name" autocomplete="name" required /></label>
        <label>{'Телефон' if lang == 'ru' else 'Phone'}<input name="phone" autocomplete="tel" required /></label>
        <label>{'Адрес или комментарий' if lang == 'ru' else 'Address or note'}<textarea name="delivery_note" required></textarea></label>
        <button class="primary-action" type="submit">{'Оформить заказ' if lang == 'ru' else 'Place order'}</button>
      </form>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/client/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_commerce_specialist_html(spec: dict[str, Any], *, child: bool) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        lang = WorkspaceCodeAgentRuntime._html_escape(spec["copy"]["lang"])
        back_href = "/specialist" if child else "/specialist/inventory"
        nav_text = "Назад" if child and lang == "ru" else "Склад" if lang == "ru" else "Back" if child else "Inventory"
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/specialist/styles.css" />
</head>
<body>
  <main class="page-shell specialist-app">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="{back_href}">{nav_text}</a></nav>
    </header>
    <section class="action-panel">
      <h2>{'Добавить товар' if lang == 'ru' else 'Add product'}</h2>
      <form id="product-form" class="request-form">
        <label>{'Название' if lang == 'ru' else 'Name'}<input name="name" required /></label>
        <label>{'Цена' if lang == 'ru' else 'Price'}<input name="price" type="number" min="0" step="1" required /></label>
        <label>{'Остаток' if lang == 'ru' else 'Stock'}<input name="stock" type="number" min="0" step="1" required /></label>
        <label>{'Описание' if lang == 'ru' else 'Description'}<textarea name="description" required></textarea></label>
        <button class="primary-action" type="submit">{'Сохранить товар' if lang == 'ru' else 'Save product'}</button>
      </form>
    </section>
    <section class="records-panel">
      <h2>{'Товары на складе' if lang == 'ru' else 'Inventory'}</h2>
      <p id="products-empty">{'Пока нет товаров.' if lang == 'ru' else 'No products yet.'}</p>
      <ul id="products-list"></ul>
    </section>
    <section class="queue-panel">
      <h2>{'Новые заказы' if lang == 'ru' else 'Orders'}</h2>
      <p id="orders-empty">{'Пока нет заказов.' if lang == 'ru' else 'No orders yet.'}</p>
      <ul id="orders-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/specialist/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_commerce_manager_html(spec: dict[str, Any], *, child: bool) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        lang = WorkspaceCodeAgentRuntime._html_escape(spec["copy"]["lang"])
        back_href = "/manager" if child else "/manager/overview"
        nav_text = "Назад" if child and lang == "ru" else "Сводка" if lang == "ru" else "Back" if child else "Summary"
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/manager/styles.css" />
</head>
<body>
  <main class="page-shell manager-app">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="{back_href}">{nav_text}</a></nav>
    </header>
    <section class="metrics-panel">
      <h2>{'Сводка продаж' if lang == 'ru' else 'Sales summary'}</h2>
      <div id="metrics" class="metric-grid"></div>
    </section>
    <section class="manager-list-panel">
      <h2>{'Заказы' if lang == 'ru' else 'Orders'}</h2>
      <p id="orders-empty">{'Пока нет заказов.' if lang == 'ru' else 'No orders yet.'}</p>
      <ul id="orders-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/manager/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_commerce_client_js(spec: dict[str, Any]) -> str:
        lang = spec["copy"]["lang"]
        return f'''const role = "client";
window.setupPreviewBridge?.(role);

const productsApi = "/api/products";
const ordersApi = "/api/orders";
let products = [];
let cart = [];

const catalogList = document.getElementById("catalog-list");
const catalogEmpty = document.getElementById("catalog-empty");
const cartList = document.getElementById("cart-list");
const cartEmpty = document.getElementById("cart-empty");
const checkoutForm = document.getElementById("checkout-form");

async function loadProducts() {{
  const response = await fetch(productsApi);
  products = await response.json();
  if (catalogEmpty) catalogEmpty.hidden = products.length > 0;
  if (!catalogList) return;
  catalogList.innerHTML = products.map((product) => `
    <li class="record-card product-card">
      <div><strong>${{product.name}}</strong><span>${{product.description || ""}}</span><small>${{product.price}} · ${{product.stock}}</small></div>
      <button type="button" class="add-to-cart" data-add-to-cart data-id="${{product.id}}">{'В корзину' if lang == 'ru' else 'Add to cart'}</button>
    </li>
  `).join("");
}}

function renderCart() {{
  if (cartEmpty) cartEmpty.hidden = cart.length > 0;
  if (!cartList) return;
  cartList.innerHTML = cart.map((item) => `
    <li class="record-card cart-item">
      <strong>${{item.name}}</strong>
      <button type="button" data-decrease data-id="${{item.id}}">-</button>
      <span>${{item.quantity}}</span>
      <button type="button" data-increase data-id="${{item.id}}">+</button>
    </li>
  `).join("");
}}

function addToCart(productId) {{
  const product = products.find((item) => item.id === productId);
  if (!product) return;
  const existing = cart.find((item) => item.id === productId);
  if (existing) existing.quantity += 1;
  else cart.push({{ id: product.id, name: product.name, price: Number(product.price || 0), quantity: 1 }});
  renderCart();
}}

catalogList?.addEventListener("click", (event) => {{
  const button = event.target.closest("[data-add-to-cart]");
  if (!button) return;
  addToCart(button.dataset.id);
}});

cartList?.addEventListener("click", (event) => {{
  const up = event.target.closest("[data-increase]");
  const down = event.target.closest("[data-decrease]");
  const control = up || down;
  if (!control) return;
  const item = cart.find((entry) => entry.id === control.dataset.id);
  if (!item) return;
  item.quantity += up ? 1 : -1;
  cart = cart.filter((entry) => entry.quantity > 0);
  renderCart();
}});

checkoutForm?.addEventListener("submit", async (event) => {{
  event.preventDefault();
  if (cart.length === 0) return;
  const customer = Object.fromEntries(new FormData(event.currentTarget).entries());
  await fetch(ordersApi, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ ...customer, items: cart, status: "new" }}),
  }});
  cart = [];
  event.currentTarget.reset();
  renderCart();
}});

loadProducts();
renderCart();
'''

    @staticmethod
    def _fast_commerce_specialist_js() -> str:
        return '''const role = "specialist";
window.setupPreviewBridge?.(role);

const productsApi = "/api/products";
const ordersApi = "/api/orders";
const productForm = document.getElementById("product-form");
const productsList = document.getElementById("products-list");
const productsEmpty = document.getElementById("products-empty");
const ordersList = document.getElementById("orders-list");
const ordersEmpty = document.getElementById("orders-empty");

async function loadProducts() {
  const response = await fetch(productsApi);
  const products = await response.json();
  if (productsEmpty) productsEmpty.hidden = products.length > 0;
  if (!productsList) return;
  productsList.innerHTML = products.map((product) => `
    <li class="record-card product-card">
      <div><strong>${product.name}</strong><span>${product.description || ""}</span></div>
      <span class="status-pill">${product.stock} шт.</span>
    </li>
  `).join("");
}

async function loadOrders() {
  const response = await fetch(ordersApi);
  const orders = await response.json();
  if (ordersEmpty) ordersEmpty.hidden = orders.length > 0;
  if (!ordersList) return;
  ordersList.innerHTML = orders.map((order) => `
    <li class="record-card specialist-task">
      <div><strong>${order.customer_name || "Покупатель"}</strong><span>${(order.items || []).map((item) => `${item.name} x${item.quantity}`).join(", ")}</span></div>
      <span class="status-pill">${order.status || "new"}</span>
      <button type="button" data-status-action data-id="${order.id}" data-next="assembling">Собирается</button>
      <button type="button" data-status-action data-id="${order.id}" data-next="done">Готово</button>
    </li>
  `).join("");
}

productForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  await fetch(productsApi, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  event.currentTarget.reset();
  await loadProducts();
});

ordersList?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-status-action]");
  if (!button) return;
  await fetch(`${ordersApi}/${button.dataset.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: button.dataset.next }),
  });
  await loadOrders();
});

loadProducts();
loadOrders();
'''

    @staticmethod
    def _fast_commerce_manager_js() -> str:
        return '''const role = "manager";
window.setupPreviewBridge?.(role);

const productsApi = "/api/products";
const ordersApi = "/api/orders";
const metrics = document.getElementById("metrics");
const ordersList = document.getElementById("orders-list");
const ordersEmpty = document.getElementById("orders-empty");

async function loadDashboard() {
  const [productsResponse, ordersResponse] = await Promise.all([
    fetch(productsApi),
    fetch(ordersApi),
  ]);
  const products = await productsResponse.json();
  const orders = await ordersResponse.json();
  const total = orders.reduce((sum, order) => sum + (order.items || []).reduce((itemSum, item) => itemSum + Number(item.price || 0) * Number(item.quantity || 0), 0), 0);
  const active = orders.filter((order) => !["done", "reviewed"].includes(order.status || "new")).length;
  if (metrics) metrics.innerHTML = [
    ["total", orders.length],
    ["products", products.length],
    ["active", active],
    ["summary", total],
  ].map(([label, value]) => `<article class="metric-card"><strong>${value}</strong><span>${label}</span></article>`).join("");
  if (ordersEmpty) ordersEmpty.hidden = orders.length > 0;
  if (!ordersList) return;
  ordersList.innerHTML = orders.map((order) => `
    <li class="record-card manager-record">
      <div><strong>${order.customer_name || "Покупатель"}</strong><span>${order.phone || ""}</span></div>
      <span class="status-pill">${order.status || "new"}</span>
      <button type="button" data-manager-action data-id="${order.id}">Проверено</button>
    </li>
  `).join("");
}

ordersList?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-manager-action]");
  if (!button) return;
  await fetch(`${ordersApi}/${button.dataset.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status: "reviewed" }),
  });
  await loadDashboard();
});

loadDashboard();
'''

    @staticmethod
    def _fast_commerce_route_py() -> str:
        return '''from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException


router = APIRouter(tags=["commerce"])
GENERATED_DIR = Path(__file__).resolve().parent.parent / "generated"
PRODUCTS_FILE = GENERATED_DIR / "products.json"
ORDERS_FILE = GENERATED_DIR / "orders.json"


def _read_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_list(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/api/products")
def list_products() -> list[dict]:
    return _read_list(PRODUCTS_FILE)


@router.post("/api/products")
def create_product(payload: dict) -> dict:
    products = _read_list(PRODUCTS_FILE)
    product = {
        "id": uuid4().hex,
        "name": str(payload.get("name") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "price": float(payload.get("price") or 0),
        "stock": int(payload.get("stock") or 0),
        "status": "active",
    }
    if not product["name"]:
        raise HTTPException(status_code=422, detail="name is required")
    products.append(product)
    _write_list(PRODUCTS_FILE, products)
    return product


@router.patch("/api/products/{product_id}")
def update_product(product_id: str, payload: dict) -> dict:
    products = _read_list(PRODUCTS_FILE)
    for product in products:
        if str(product.get("id")) == product_id:
            for key in ("name", "description", "price", "stock", "status"):
                if key in payload:
                    product[key] = payload[key]
            _write_list(PRODUCTS_FILE, products)
            return product
    raise HTTPException(status_code=404, detail="product not found")


@router.get("/api/orders")
def list_orders() -> list[dict]:
    return _read_list(ORDERS_FILE)


@router.post("/api/orders")
def create_order(payload: dict) -> dict:
    orders = _read_list(ORDERS_FILE)
    order = {"id": uuid4().hex, "status": payload.get("status") or "new", **payload}
    orders.append(order)
    _write_list(ORDERS_FILE, orders)
    return order


@router.patch("/api/orders/{order_id}")
def update_order(order_id: str, payload: dict) -> dict:
    orders = _read_list(ORDERS_FILE)
    for order in orders:
        if str(order.get("id")) == order_id:
            order["status"] = str(payload.get("status") or order.get("status") or "new")
            _write_list(ORDERS_FILE, orders)
            return order
    raise HTTPException(status_code=404, detail="order not found")
'''

    @staticmethod
    def _fast_commerce_main_py() -> str:
        return '''from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.commerce import router as commerce_router
from app.routes.health import router as health_router
from app.routes.role_routes import router as role_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
app.include_router(role_router)
app.include_router(commerce_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_preview_caching(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"GET", "HEAD"} and (request.url.path.startswith("/static/") or not request.url.path.startswith("/api/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@app.exception_handler(HTTPException)
def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": _json_safe(exc.detail)}, headers=exc.headers)


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def value_error_handler(_, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})
'''

    @staticmethod
    def _fast_commerce_python_test_py(spec: dict[str, Any]) -> str:
        title = spec["title"]
        return f'''import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.routes.commerce import ORDERS_FILE, PRODUCTS_FILE


class GeneratedCommerceAppTest(unittest.TestCase):
    def setUp(self):
        PRODUCTS_FILE.unlink(missing_ok=True)
        ORDERS_FILE.unlink(missing_ok=True)
        self.client = TestClient(app)

    def test_products_cart_orders_persist_across_roles(self):
        self.assertEqual(self.client.get("/api/products").json(), [])
        self.assertEqual(self.client.get("/api/orders").json(), [])
        product = self.client.post("/api/products", json={{"name": "{title} product", "price": 2500, "stock": 7, "description": "Catalog item"}})
        self.assertEqual(product.status_code, 200)
        product_payload = product.json()
        self.assertEqual(self.client.get("/api/products").json()[0]["name"], product_payload["name"])
        order = self.client.post("/api/orders", json={{"customer_name": "Buyer", "phone": "+79990000000", "items": [{{"id": product_payload["id"], "name": product_payload["name"], "quantity": 2, "price": 2500}}]}})
        self.assertEqual(order.status_code, 200)
        order_payload = order.json()
        self.assertEqual(len(self.client.get("/api/orders").json()), 1)
        updated = self.client.patch(f"/api/orders/{{order_payload['id']}}", json={{"status": "done"}})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get("/api/orders").json()[0]["status"], "done")

    def test_role_routes(self):
        for path in ["/client", "/client/catalog", "/specialist", "/specialist/inventory", "/manager", "/manager/overview"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("{title}", response.text)


if __name__ == "__main__":
    unittest.main()
'''

    @staticmethod
    def _fast_commerce_js_test_mjs(spec: dict[str, Any]) -> str:
        title_literal = json.dumps(spec["title"])
        return f'''import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const read = (filePath) => fs.readFileSync(path.join(root, filePath), 'utf8');

test('commerce role pages are separate and styled', () => {{
  for (const filePath of [
    'app/static/client/index.html',
    'app/static/client/catalog/index.html',
    'app/static/specialist/index.html',
    'app/static/specialist/inventory/index.html',
    'app/static/manager/index.html',
    'app/static/manager/overview/index.html'
  ]) {{
    const html = read(filePath);
    assert.ok(html.includes({title_literal}));
    assert.match(html, /\\/static\\/preview_bridge\\.js/);
    assert.match(html, /\\/static\\/(client|specialist|manager)\\/styles\\.css/);
  }}
}});

test('products orders cart workflow is wired in frontend source', () => {{
  const clientJs = read('app/static/client/app.js');
  assert.ok(clientJs.includes('/api/products'));
  assert.ok(clientJs.includes('/api/orders'));
  assert.match(clientJs, /add-to-cart|addToCart|корзин/i);
  assert.match(clientJs, /method:\\s*"POST"/);
  const specialistJs = read('app/static/specialist/app.js');
  assert.ok(specialistJs.includes('/api/products'));
  assert.ok(specialistJs.includes('/api/orders'));
  assert.match(specialistJs, /method:\\s*"POST"/);
  assert.match(specialistJs, /method:\\s*"PATCH"/);
  const managerJs = read('app/static/manager/app.js');
  assert.ok(managerJs.includes('/api/orders'));
  assert.match(managerJs, /metric|summary|dashboard|total/i);
}});
'''

    @staticmethod
    def _fast_create_domain_spec(prompt: str) -> dict[str, Any]:
        language = WorkspaceCodeAgentRuntime._prompt_language(prompt)
        copy = WorkspaceCodeAgentRuntime._fast_copy(language)
        tokens = WorkspaceCodeAgentRuntime._prompt_semantic_tokens(prompt, limit=4)
        title_base = WorkspaceCodeAgentRuntime._human_title_from_tokens(tokens, language) if tokens else str(copy["fallback_title"])
        resource = WorkspaceCodeAgentRuntime._resource_slug_from_prompt_tokens(tokens)
        return {
            "language": language,
            "copy": copy,
            "title": title_base,
            "resource": resource,
            "record_label": str(copy["entry_label"]),
            "action_label": str(copy["create_action"]),
        }

    @staticmethod
    def _prompt_language(prompt: str) -> str:
        text = str(prompt or "")
        cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        return "ru" if cyrillic > latin else "en"

    @staticmethod
    def _human_title_from_tokens(tokens: list[str], language: str) -> str:
        raw_title = " ".join(tokens[:3]).replace("_", " ").strip()
        if not raw_title:
            return "Бизнес" if language == "ru" else "Business"
        if language == "ru":
            raw_title = re.sub(r"\bинтернет-магазина\b", "интернет-магазин", raw_title, flags=re.IGNORECASE)
            return raw_title[:1].upper() + raw_title[1:]
        return raw_title.title()

    @staticmethod
    def _fast_copy(language: str) -> dict[str, str]:
        if language == "ru":
            return {
                "lang": "ru",
                "fallback_title": "Бизнес",
                "entry_label": "запись",
                "create_action": "Сохранить",
                "client_nav": "Оформление",
                "client_heading": "Новая запись",
                "client_intro": "Заполните данные. После сохранения они появятся у команды.",
                "name_label": "Имя",
                "phone_label": "Телефон",
                "preferred_time_label": "Удобное время",
                "details_label": "Детали",
                "client_records_heading": "Мои записи",
                "client_empty": "Пока нет сохраненных записей.",
                "specialist_nav": "Рабочий список",
                "specialist_heading": "Работа с записями",
                "specialist_empty": "Пока нет новых записей.",
                "manager_nav": "Сводка",
                "manager_heading": "Сводка",
                "manager_records_heading": "Все записи",
                "manager_empty": "Пока нет данных для сводки.",
                "back": "Назад",
                "client_fallback": "Клиент",
                "time_pending": "Время не указано",
                "no_details": "Детали не указаны",
                "status_label": "Статус",
                "status_new": "новая",
                "confirm_action": "В работу",
                "done_action": "Готово",
                "review_action": "Проверено",
                "metric_total": "Всего",
                "metric_new": "Новые",
                "metric_confirmed": "В работе",
                "metric_done": "Готово",
            }
        return {
            "lang": "en",
            "fallback_title": "Business",
            "entry_label": "entry",
            "create_action": "Save",
            "client_nav": "New entry",
            "client_heading": "New entry",
            "client_intro": "Fill in the details. After saving, the team will see them.",
            "name_label": "Name",
            "phone_label": "Phone",
            "preferred_time_label": "Preferred time",
            "details_label": "Details",
            "client_records_heading": "My entries",
            "client_empty": "No saved entries yet.",
            "specialist_nav": "Work list",
            "specialist_heading": "Work list",
            "specialist_empty": "No new entries yet.",
            "manager_nav": "Summary",
            "manager_heading": "Summary",
            "manager_records_heading": "All entries",
            "manager_empty": "No data for the summary yet.",
            "back": "Back",
            "client_fallback": "Client",
            "time_pending": "Time pending",
            "no_details": "No details",
            "status_label": "Status",
            "status_new": "new",
            "confirm_action": "Confirm",
            "done_action": "Done",
            "review_action": "Reviewed",
            "metric_total": "Total",
            "metric_new": "New",
            "metric_confirmed": "In progress",
            "metric_done": "Done",
        }

    @staticmethod
    def _prompt_semantic_tokens(prompt: str, *, limit: int = 8) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for raw_token in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_-]{2,}", str(prompt or "")):
            token = raw_token.strip("-_").lower()
            if not token or token in PROMPT_SEMANTIC_STOPWORDS or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= limit:
                break
        return tokens

    @staticmethod
    def _resource_slug_from_prompt_tokens(tokens: list[str]) -> str:
        slug_parts: list[str] = []
        for token in tokens[:3]:
            ascii_token = token.lower().translate(CYRILLIC_TRANSLIT)
            ascii_token = re.sub(r"[^a-z0-9]+", "_", ascii_token).strip("_")
            if ascii_token:
                slug_parts.append(ascii_token)
        slug = "_".join(slug_parts)[:60].strip("_") or "records"
        if not re.match(r"^[a-z_]", slug):
            slug = f"records_{slug}"
        if slug in {"class", "def", "from", "import", "return", "for", "while", "if", "else", "try"}:
            slug = f"{slug}_records"
        return slug

    @staticmethod
    def _prompt_token_matches_text(token: str, text: str) -> bool:
        normalized = str(token or "").lower()
        haystack = str(text or "").lower()
        if not normalized:
            return False
        if normalized in haystack:
            return True
        return len(normalized) >= 6 and normalized[:5] in haystack

    @staticmethod
    def _html_escape(value: str) -> str:
        return (
            str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    @staticmethod
    def _fast_client_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        action = WorkspaceCodeAgentRuntime._html_escape(copy["create_action"])
        nav = WorkspaceCodeAgentRuntime._html_escape(copy["client_nav"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["client_heading"])
        intro = WorkspaceCodeAgentRuntime._html_escape(copy["client_intro"])
        name_label = WorkspaceCodeAgentRuntime._html_escape(copy["name_label"])
        phone_label = WorkspaceCodeAgentRuntime._html_escape(copy["phone_label"])
        preferred_time_label = WorkspaceCodeAgentRuntime._html_escape(copy["preferred_time_label"])
        details_label = WorkspaceCodeAgentRuntime._html_escape(copy["details_label"])
        records_heading = WorkspaceCodeAgentRuntime._html_escape(copy["client_records_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["client_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/client/styles.css" />
</head>
<body>
  <main class="page-shell client-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/client/request">{nav}</a></nav>
    </header>
    <section class="action-panel client-request-panel">
      <h2>{heading}</h2>
      <p>{intro}</p>
      <form id="record-form" class="request-form">
        <label>{name_label}<input name="client_name" autocomplete="name" required /></label>
        <label>{phone_label}<input name="phone" autocomplete="tel" required /></label>
        <label>{preferred_time_label}<input name="preferred_time" required /></label>
        <label>{details_label}<textarea name="details" required></textarea></label>
        <button class="primary-action" type="submit">{action}</button>
      </form>
    </section>
    <section class="records-panel client-records">
      <h2>{records_heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/client/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_specialist_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        nav = WorkspaceCodeAgentRuntime._html_escape(copy["specialist_nav"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["specialist_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["specialist_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/specialist/styles.css" />
</head>
<body>
  <main class="page-shell specialist-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/specialist/queue">{nav}</a></nav>
    </header>
    <section class="queue-panel">
      <h2>{heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/specialist/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_manager_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        nav = WorkspaceCodeAgentRuntime._html_escape(copy["manager_nav"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["manager_heading"])
        records_heading = WorkspaceCodeAgentRuntime._html_escape(copy["manager_records_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["manager_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/manager/styles.css" />
</head>
<body>
  <main class="page-shell manager-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/manager/overview">{nav}</a></nav>
    </header>
    <section class="metrics-panel">
      <h2>{heading}</h2>
      <div id="metrics" class="metric-grid"></div>
    </section>
    <section class="manager-list-panel">
      <h2>{records_heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/manager/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_client_child_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        back = WorkspaceCodeAgentRuntime._html_escape(copy["back"])
        action = WorkspaceCodeAgentRuntime._html_escape(copy["create_action"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["client_heading"])
        intro = WorkspaceCodeAgentRuntime._html_escape(copy["client_intro"])
        name_label = WorkspaceCodeAgentRuntime._html_escape(copy["name_label"])
        phone_label = WorkspaceCodeAgentRuntime._html_escape(copy["phone_label"])
        preferred_time_label = WorkspaceCodeAgentRuntime._html_escape(copy["preferred_time_label"])
        details_label = WorkspaceCodeAgentRuntime._html_escape(copy["details_label"])
        records_heading = WorkspaceCodeAgentRuntime._html_escape(copy["client_records_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["client_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/client/styles.css" />
</head>
<body>
  <main class="page-shell client-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/client">{back}</a></nav>
    </header>
    <section class="action-panel client-request-panel">
      <h2>{heading}</h2>
      <p>{intro}</p>
      <form id="record-form" class="request-form">
        <label>{name_label}<input name="client_name" autocomplete="name" required /></label>
        <label>{phone_label}<input name="phone" autocomplete="tel" required /></label>
        <label>{preferred_time_label}<input name="preferred_time" required /></label>
        <label>{details_label}<textarea name="details" required></textarea></label>
        <button class="primary-action" type="submit">{action}</button>
      </form>
    </section>
    <section class="records-panel client-records">
      <h2>{records_heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/client/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_specialist_child_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        back = WorkspaceCodeAgentRuntime._html_escape(copy["back"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["specialist_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["specialist_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/specialist/styles.css" />
</head>
<body>
  <main class="page-shell specialist-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/specialist">{back}</a></nav>
    </header>
    <section class="queue-panel">
      <h2>{heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/specialist/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_manager_child_html(spec: dict[str, Any], api_path: str) -> str:
        title = WorkspaceCodeAgentRuntime._html_escape(spec["title"])
        copy = spec["copy"]
        lang = WorkspaceCodeAgentRuntime._html_escape(copy["lang"])
        back = WorkspaceCodeAgentRuntime._html_escape(copy["back"])
        heading = WorkspaceCodeAgentRuntime._html_escape(copy["manager_heading"])
        records_heading = WorkspaceCodeAgentRuntime._html_escape(copy["manager_records_heading"])
        empty = WorkspaceCodeAgentRuntime._html_escape(copy["manager_empty"])
        return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <link rel="stylesheet" href="/static/shared/base.css" />
  <link rel="stylesheet" href="/static/manager/styles.css" />
</head>
<body>
  <main class="page-shell manager-app" data-api-path="{api_path}">
    <header class="role-hero">
      <h1>{title}</h1>
      <nav class="role-nav"><a href="/manager">{back}</a></nav>
    </header>
    <section class="metrics-panel">
      <h2>{heading}</h2>
      <div id="metrics" class="metric-grid"></div>
    </section>
    <section class="manager-list-panel">
      <h2>{records_heading}</h2>
      <p id="empty-state">{empty}</p>
      <ul id="records-list"></ul>
    </section>
  </main>
  <script src="/static/preview_bridge.js" defer></script>
  <script src="/static/manager/app.js" defer></script>
</body>
</html>
"""

    @staticmethod
    def _fast_client_js(spec: dict[str, Any], api_path: str) -> str:
        copy = spec["copy"]
        client_fallback = json.dumps(copy["client_fallback"], ensure_ascii=False)
        time_pending = json.dumps(copy["time_pending"], ensure_ascii=False)
        status_label = json.dumps(copy["status_label"], ensure_ascii=False)
        status_new = json.dumps(copy["status_new"], ensure_ascii=False)
        return f'''const role = "client";
window.setupPreviewBridge?.(role);

const apiPath = "{api_path}";
const form = document.getElementById("record-form");
const emptyState = document.getElementById("empty-state");
const recordsList = document.getElementById("records-list");

async function loadClientRecords() {{
  const response = await fetch(apiPath);
  const records = await response.json();
  if (emptyState) emptyState.hidden = records.length > 0;
  if (!recordsList) return;
  recordsList.innerHTML = records.map((item) => `
    <li class="record-card client-record">
      <strong>${{item.client_name || {client_fallback}}}</strong>
      <span>${{item.preferred_time || {time_pending}}}</span>
      <small>${{ {status_label} }}: ${{item.status || {status_new}}}</small>
    </li>
  `).join("");
}}

form?.addEventListener("submit", async (event) => {{
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  await fetch(apiPath, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(payload),
  }});
  event.currentTarget.reset();
  await loadClientRecords();
}});

loadClientRecords();
'''

    @staticmethod
    def _fast_specialist_js(spec: dict[str, Any], api_path: str) -> str:
        copy = spec["copy"]
        client_fallback = json.dumps(copy["client_fallback"], ensure_ascii=False)
        no_details = json.dumps(copy["no_details"], ensure_ascii=False)
        status_new = json.dumps(copy["status_new"], ensure_ascii=False)
        confirm_action = json.dumps(copy["confirm_action"], ensure_ascii=False)
        done_action = json.dumps(copy["done_action"], ensure_ascii=False)
        return f'''const role = "specialist";
window.setupPreviewBridge?.(role);

const apiPath = "{api_path}";
const emptyState = document.getElementById("empty-state");
const recordsList = document.getElementById("records-list");

async function updateStatus(id, status) {{
  await fetch(`${{apiPath}}/${{id}}`, {{
    method: "PATCH",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ status }}),
  }});
  await loadSpecialistQueue();
}}

async function loadSpecialistQueue() {{
  const response = await fetch(apiPath);
  const records = await response.json();
  if (emptyState) emptyState.hidden = records.length > 0;
  if (!recordsList) return;
  recordsList.innerHTML = records.map((item) => `
    <li class="record-card specialist-task">
      <div><strong>${{item.client_name || {client_fallback}}}</strong><span>${{item.details || {no_details}}}</span></div>
      <span class="status-pill">${{item.status || {status_new}}}</span>
      <button data-status-action data-id="${{item.id}}" data-next="confirmed">${{ {confirm_action} }}</button>
      <button data-status-action data-id="${{item.id}}" data-next="done">${{ {done_action} }}</button>
    </li>
  `).join("");
}}

recordsList?.addEventListener("click", (event) => {{
  const button = event.target.closest("[data-status-action]");
  if (!button) return;
  updateStatus(button.dataset.id, button.dataset.next);
}});

loadSpecialistQueue();
'''

    @staticmethod
    def _fast_manager_js(spec: dict[str, Any], api_path: str) -> str:
        copy = spec["copy"]
        client_fallback = json.dumps(copy["client_fallback"], ensure_ascii=False)
        time_pending = json.dumps(copy["time_pending"], ensure_ascii=False)
        status_new = json.dumps(copy["status_new"], ensure_ascii=False)
        review_action = json.dumps(copy["review_action"], ensure_ascii=False)
        metric_total = json.dumps(copy["metric_total"], ensure_ascii=False)
        metric_new = json.dumps(copy["metric_new"], ensure_ascii=False)
        metric_confirmed = json.dumps(copy["metric_confirmed"], ensure_ascii=False)
        metric_done = json.dumps(copy["metric_done"], ensure_ascii=False)
        return f'''const role = "manager";
window.setupPreviewBridge?.(role);

const apiPath = "{api_path}";
const metrics = document.getElementById("metrics");
const emptyState = document.getElementById("empty-state");
const recordsList = document.getElementById("records-list");

async function markReviewed(id) {{
  await fetch(`${{apiPath}}/${{id}}`, {{
    method: "PATCH",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ status: "reviewed" }}),
  }});
  await loadManagerDashboard();
}}

async function loadManagerDashboard() {{
  const response = await fetch(apiPath);
  const records = await response.json();
  const open = records.filter((item) => (item.status || "new") === "new").length;
  const confirmed = records.filter((item) => item.status === "confirmed").length;
  const done = records.filter((item) => item.status === "done").length;
  if (metrics) metrics.innerHTML = [
    [{metric_total}, records.length],
    [{metric_new}, open],
    [{metric_confirmed}, confirmed],
    [{metric_done}, done],
  ].map(([label, value]) => `<article class="metric-card"><strong>${{value}}</strong><span>${{label}}</span></article>`).join("");
  if (emptyState) emptyState.hidden = records.length > 0;
  if (!recordsList) return;
  recordsList.innerHTML = records.map((item) => `
    <li class="record-card manager-record">
      <div><strong>${{item.client_name || {client_fallback}}}</strong><span>${{item.preferred_time || {time_pending}}}</span></div>
      <span class="status-pill">${{item.status || {status_new}}}</span>
      <button data-manager-action data-id="${{item.id}}">${{ {review_action} }}</button>
    </li>
  `).join("");
}}

recordsList?.addEventListener("click", (event) => {{
  const button = event.target.closest("[data-manager-action]");
  if (!button) return;
  markReviewed(button.dataset.id);
}});

loadManagerDashboard();
'''

    @staticmethod
    def _fast_role_css(role: str) -> str:
        accent = "#2563eb"
        surface = "#f8fafc"
        ink = "#172033"
        return f'''.page-shell.{role}-app {{
  max-width: 1040px;
  margin: 0 auto;
  padding: max(76px, calc(var(--telegram-top-safe-offset) + 24px)) 24px 48px;
  color: {ink};
  background: #ffffff;
}}
.role-hero {{
  display: grid;
  gap: 12px;
  padding: 20px;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: {surface};
}}
.eyebrow {{
  margin: 0;
  color: {accent};
  font-weight: 700;
  text-transform: uppercase;
  font-size: 12px;
}}
.role-nav {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}}
.role-nav a, button, .primary-action {{
  border: 1px solid {accent};
  border-radius: 6px;
  padding: 10px 12px;
  color: {accent};
  background: #ffffff;
  text-decoration: none;
  font-weight: 700;
}}
.primary-action, button:hover {{
  color: #ffffff;
  background: {accent};
}}
.action-panel, .records-panel, .queue-panel, .metrics-panel, .manager-list-panel, .detail-panel {{
  margin-top: 18px;
  padding: 18px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #ffffff;
}}
.request-form {{
  display: grid;
  gap: 12px;
}}
label {{
  display: grid;
  gap: 6px;
  font-weight: 700;
}}
input, textarea {{
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 10px;
  font: inherit;
}}
#records-list {{
  display: grid;
  gap: 10px;
  padding: 0;
  list-style: none;
}}
.record-card {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
  border: 1px solid #d8dee8;
  border-radius: 8px;
  background: #f8fafc;
}}
.metric-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
}}
.metric-card {{
  padding: 14px;
  border-radius: 8px;
  background: {surface};
}}
.metric-card strong {{
  display: block;
  font-size: 24px;
  color: {accent};
}}
.status-pill {{
  border-radius: 999px;
  padding: 5px 9px;
  background: {surface};
  color: {accent};
  font-weight: 700;
}}
@media (max-width: 720px) {{
  .page-shell.{role}-app {{ padding: max(76px, calc(var(--telegram-top-safe-offset) + 18px)) 14px 36px; }}
  .record-card {{ align-items: stretch; flex-direction: column; }}
}}
'''

    @staticmethod
    def _fast_api_route_py(spec: dict[str, str], api_path: str) -> str:
        resource = spec["resource"]
        return f'''from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException


router = APIRouter(tags=["{resource}"])
DATA_FILE = Path(__file__).resolve().parent.parent / "generated" / "{resource}.json"


def _read_records() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_records(records: list[dict]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("{api_path}")
def list_records() -> list[dict]:
    return _read_records()


@router.post("{api_path}")
def create_record(payload: dict) -> dict:
    records = _read_records()
    record = {{"id": uuid4().hex, "status": "new", **payload}}
    records.append(record)
    _write_records(records)
    return record


@router.patch("{api_path}/{{record_id}}")
def update_record_status(record_id: str, payload: dict) -> dict:
    records = _read_records()
    status = str(payload.get("status") or "").strip()
    if not status:
        raise HTTPException(status_code=422, detail="status is required")
    for record in records:
        if str(record.get("id")) == record_id:
            record["status"] = status
            _write_records(records)
            return record
    raise HTTPException(status_code=404, detail="record not found")
'''

    @staticmethod
    def _fast_main_py(resource: str) -> str:
        return f'''from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.health import router as health_router
from app.routes.role_routes import router as role_router
from app.routes.{resource} import router as generated_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
app.include_router(role_router)
app.include_router(generated_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_preview_caching(request: Request, call_next):
    response = await call_next(request)
    if request.method in {{"GET", "HEAD"}} and (request.url.path.startswith("/static/") or not request.url.path.startswith("/api/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {{str(key): _json_safe(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@app.exception_handler(HTTPException)
def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={{"detail": _json_safe(exc.detail)}}, headers=exc.headers)


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={{"detail": str(exc)}})


@app.exception_handler(ValueError)
def value_error_handler(_, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={{"detail": str(exc)}})
'''

    @staticmethod
    def _fast_python_test_py(spec: dict[str, str], api_path: str, resource: str) -> str:
        title = spec["title"]
        return f'''import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.routes.{resource} import DATA_FILE


class GeneratedAppTest(unittest.TestCase):
    def setUp(self):
        DATA_FILE.unlink(missing_ok=True)
        self.client = TestClient(app)

    def test_api_persistence(self):
        self.assertEqual(self.client.get("{api_path}").json(), [])
        payload = {{"client_name": "Benchmark Client", "phone": "+79990000000", "preferred_time": "2026-05-05 10:00", "details": "{title} request"}}
        created = self.client.post("{api_path}", json=payload)
        self.assertEqual(created.status_code, 200)
        created_payload = created.json()
        records = self.client.get("{api_path}").json()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["client_name"], payload["client_name"])
        self.assertEqual(records[0]["status"], "new")
        updated = self.client.patch(f"{api_path}/{{created_payload['id']}}", json={{"status": "confirmed"}})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["status"], "confirmed")
        self.assertEqual(self.client.get("{api_path}").json()[0]["status"], "confirmed")

    def test_role_routes(self):
        for path in ["/client", "/client/request", "/specialist", "/specialist/queue", "/manager", "/manager/overview"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("{title}", response.text)


if __name__ == "__main__":
    unittest.main()
'''

    @staticmethod
    def _fast_js_test_mjs(spec: dict[str, str], api_path: str) -> str:
        title = spec["title"]
        title_literal = json.dumps(title)
        api_literal = json.dumps(api_path)
        return f'''import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const apiPath = {api_literal};
const read = (filePath) => fs.readFileSync(path.join(root, filePath), 'utf8');

test('role pages contain domain content and preview bridge', () => {{
  for (const filePath of [
    'app/static/client/index.html',
    'app/static/client/request/index.html',
    'app/static/specialist/index.html',
    'app/static/specialist/queue/index.html',
    'app/static/manager/index.html',
    'app/static/manager/overview/index.html'
  ]) {{
    const html = read(filePath);
    assert.ok(html.includes({title_literal}));
    assert.match(html, /\\/static\\/preview_bridge\\.js/);
    assert.match(html, /\\/static\\/(client|specialist|manager)\\/styles\\.css/);
  }}
}});

test('role apps have separate frontend actions and styles', () => {{
  const html = read('app/static/client/index.html');
  assert.match(html, /record-form/);
  const clientJs = read('app/static/client/app.js');
  assert.ok(clientJs.includes('fetch(apiPath'));
  assert.match(clientJs, /method:\\s*"POST"/);
  const specialistJs = read('app/static/specialist/app.js');
  assert.match(specialistJs, /method:\\s*"PATCH"/);
  assert.match(specialistJs, /data-status-action/);
  const managerJs = read('app/static/manager/app.js');
  assert.match(managerJs, /metric-card/);
  assert.match(managerJs, /data-manager-action/);
  for (const role of ['client', 'specialist', 'manager']) {{
    const css = read(`app/static/${{role}}/styles.css`);
    assert.match(css, new RegExp(`\\\\.${{role}}-app`));
    assert.match(css, /record-card|metric-card|request-form/);
  }}
}});
'''

    def _create_coverage_existing_state(self, *, workspace_id: str, run_id: str) -> tuple[set[str], dict[str, str]]:
        paths: set[str] = set()
        text_by_path: dict[str, str] = {}
        for item in self.workspace_service.file_tree(workspace_id, run_id=run_id):
            path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("./")
            if not path or item.get("type") != "file":
                continue
            if not (
                path.startswith("miniapp/app/static/")
                or path.startswith("miniapp/app/routes/")
                or path in {
                    "miniapp/app/main.py",
                    "miniapp/app/generated/route_manifest.json",
                    "miniapp/tests/test_generated_app.py",
                    "miniapp/tests/generated_app.test.mjs",
                }
            ):
                continue
            paths.add(path)
            if path.endswith((".py", ".js", ".html", ".json", ".css", ".mjs")):
                content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
                if content is not None:
                    text_by_path[path] = content
        return paths, text_by_path

    @staticmethod
    def _create_patch_coverage_gap(
        operations: list[DraftFileOperation],
        *,
        request: GenerateRequest,
        existing_paths: set[str] | None = None,
        existing_text_by_path: dict[str, str] | None = None,
    ) -> list[str]:
        if str(request.intent or "").strip().lower() != "create":
            return []
        existing_paths = {
            str(path or "").strip().replace("\\", "/").lstrip("./")
            for path in existing_paths or set()
            if str(path or "").strip()
        }
        touched = {
            *existing_paths,
            *{str(operation.file_path or "").strip().replace("\\", "/").lstrip("./") for operation in operations},
        }
        operation_text_by_path = {
            str(operation.file_path or "").strip().replace("\\", "/").lstrip("./"): "\n".join(
                part
                for part in (str(operation.content or ""), str(operation.diff or ""))
                if part
            )
            for operation in operations
        }
        operation_text_by_path = {**(existing_text_by_path or {}), **operation_text_by_path}
        missing: list[str] = []
        required_files = [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/specialist/index.html",
            "miniapp/app/static/specialist/app.js",
            "miniapp/app/static/specialist/styles.css",
            "miniapp/app/static/manager/index.html",
            "miniapp/app/static/manager/app.js",
            "miniapp/app/static/manager/styles.css",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/main.py",
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
        ]
        for path in required_files:
            if path not in touched:
                missing.append(path)
        generation_mode = WorkspaceCodeAgentRuntime._generation_mode(request.generation_mode)
        required_child_pages = 1 if generation_mode == GenerationMode.FAST else 2
        for role in ("client", "specialist", "manager"):
            child_pages = {
                path
                for path in touched
                if re.fullmatch(rf"miniapp/app/static/{role}/[^/]+/index\.html", path)
            }
            if len(child_pages) < required_child_pages:
                missing.append(
                    f"miniapp/app/static/{role}/<{'one-child-page' if required_child_pages == 1 else 'two-child-pages'}>/index.html"
                )
            css_path = f"miniapp/app/static/{role}/styles.css"
            css_text = operation_text_by_path.get(css_path, "")
            if "generated " in css_text.lower() and "styles can replace this file" in css_text.lower():
                missing.append(f"{css_path} real role CSS")
            role_html_text = "\n".join(
                operation_text_by_path.get(path, "")
                for path in touched
                if path.startswith(f"miniapp/app/static/{role}/") and path.endswith(".html")
            )
            if f"/static/{role}/styles.css" not in role_html_text:
                missing.append(f"miniapp/app/static/{role}/html link to role CSS")
        backend_route_paths = {
            path
            for path in touched
            if re.fullmatch(r"miniapp/app/routes/[^/]+\.py", path)
            and not path.endswith(("/__init__.py", "/health.py", "/role_pages.py", "/role_routes.py"))
        }
        backend_text = "\n".join(operation_text_by_path.get(path, "") for path in sorted(backend_route_paths))
        if not backend_route_paths:
            missing.append("miniapp/app/routes/<domain_resource>.py")
        if not WorkspaceCodeAgentRuntime._has_backend_api_method(backend_text, "get"):
            missing.append("backend GET /api/<resource>")
        if not WorkspaceCodeAgentRuntime._has_backend_api_method(backend_text, "post"):
            missing.append("backend POST /api/<resource>")
        has_update_api = any(
            WorkspaceCodeAgentRuntime._has_backend_api_method(backend_text, method)
            for method in ("put", "patch", "delete")
        )
        if not has_update_api:
            missing.append("backend status/update endpoint")
        frontend_text = "\n".join(
            text
            for path, text in operation_text_by_path.items()
            if path.startswith("miniapp/app/static/") and path.endswith((".html", ".js"))
        )
        if "/api/" not in frontend_text or not re.search(r"method\s*:\s*['\"]POST['\"]", frontend_text, re.IGNORECASE):
            missing.append("frontend form/fetch POST /api/<resource>")
        if not re.search(r"method\s*:\s*['\"](?:PATCH|PUT|DELETE)['\"]", frontend_text, re.IGNORECASE):
            missing.append("frontend specialist/manager status update action")
        python_test_text = operation_text_by_path.get("miniapp/tests/test_generated_app.py", "")
        if ".post(" not in python_test_text or ".get(" not in python_test_text:
            missing.append("miniapp/tests/test_generated_app.py API persistence coverage")
        if not any(token in python_test_text for token in (".patch(", ".put(", ".delete(")):
            missing.append("miniapp/tests/test_generated_app.py API status/update coverage")
        api_resources = WorkspaceCodeAgentRuntime._api_resource_stems(backend_text)
        if generation_mode == GenerationMode.BALANCED and len(api_resources) < 2 and not has_update_api:
            missing.append("balanced create: second API resource or update/status endpoint")
        if generation_mode == GenerationMode.QUALITY and (len(api_resources) < 2 or not has_update_api):
            missing.append("quality create: multiple API resources plus update/status endpoint")
        return missing

    @staticmethod
    def _api_resource_stems(source: str) -> set[str]:
        stems: set[str] = set()
        for match in re.finditer(r"['\"](?P<path>/api/[A-Za-z0-9_/{}/-]+)", str(source or "")):
            segments = [segment for segment in match.group("path").strip("/").split("/") if segment]
            if len(segments) >= 2 and segments[0] == "api":
                stem = segments[1].strip("{}")
                if stem:
                    stems.add(stem)
        return stems

    @staticmethod
    def _has_backend_api_method(source: str, method: str) -> bool:
        text = str(source or "")
        method_pattern = rf"@(?:router|api)\.{re.escape(method)}\(\s*['\"](?P<path>[^'\"]*)['\"]"
        for match in re.finditer(method_pattern, text, flags=re.IGNORECASE):
            route_path = str(match.group("path") or "")
            if route_path.startswith("/api/"):
                return True
            prefix_match = re.search(r"APIRouter\(\s*prefix\s*=\s*['\"](?P<prefix>/api(?:/[^'\"]*)?)['\"]", text)
            if prefix_match:
                return True
        return False

    @staticmethod
    def _create_patch_coverage_correction_result(missing: list[str]) -> dict[str, object]:
        return {
            "tool": "create_patch_coverage_correction",
            "contract": (
                "Create runs must not apply a partial product slice that leaves another role as the starter template "
                "or omits generated tests. The first patch must cover the required app surface evenly."
            ),
            "required_next_action": (
                "Return outcome=patch_ready now with one compact operations array that covers all required create surfaces. "
                "Do not request more tools. Replace all three role roots, create the required child pages for every role before adding extra pages to any one role, "
                "create/update route_manifest.json, create real app.js and styles.css files for each role, create a backend route module with GET, POST, and status/update persistence, register it in main.py, add frontend form/fetch/status behavior that uses the API, and create both generated test files. "
                "A client-only first patch is invalid; frontend-only or static-only first patches are invalid too. Keep pages concise, light-mode, and free of preloaded business records."
            ),
            "missing_required_coverage": list(missing),
        }

    @staticmethod
    def _fast_create_budget_result() -> dict[str, object]:
        return {
            "tool": "fast_create_budget",
            "contract": (
                "Fast create should finish in one compact patch whenever possible. "
                "It still must be a working app with backend persistence, not a frontend-only mockup."
            ),
            "required_next_action": (
                "Return outcome=patch_ready in the first answer with a compact working MVP, up to 20 concise operations: route_manifest.json, three separate role root apps, one child page per role, role app.js files, role styles.css files, one backend API route module, main.py router registration, form/fetch/status code, and generated tests. "
                "Final Fast success requires at least two routeable pages per role: /client and /client/<slug>; same for specialist and manager. "
                "Final success also requires at least one POST-capable /api resource used by the frontend plus a status/update endpoint, with generated Python tests proving GET starts empty, POST creates a record, GET returns it, PATCH/status update changes it, and GET returns the updated state. "
                "Choose child-page names and flows from the user's request instead of from a fixed profile/settings/page template. "
                "Put enough prompt-derived text in the role HTML itself so backend TestClient can see meaningful requested content without browser JavaScript. "
                "Keep /static/preview_bridge.js in every generated HTML route page, including every child page. Use app/static paths inside JS tests because tests run from cwd=miniapp. "
                "Use string paths in JS tests, for example path.join(process.cwd(), 'app/static/client/index.html'); do not pass URL objects to path.resolve or fs. "
                "Use node:assert in JS tests; node:test does not export expect. "
                "Do not emit extra child pages, extra API resources, mock data, seed data, demo data, sample data, fixture records, preloaded records, hard-coded business records, long fixtures, or inline <style> blocks. Per-role CSS files are required and should be compact but real, using a consistent neutral light palette across roles. "
                "If route_manifest.json is absent or empty, create it; do not request it via tools just to inspect it."
            ),
        }

    @staticmethod
    def _focused_visual_edit_budget_result(*, role_scope: list[str]) -> dict[str, object]:
        return {
            "tool": "focused_visual_edit_budget",
            "contract": (
                "This is a small visual/style edit. It must use the focused CSS lane instead of the full create/product "
                "generation loop."
            ),
            "focused_edit_files": WorkspaceCodeAgentRuntime._focused_visual_css_paths(role_scope),
            "required_next_action": (
                "Return outcome=patch_ready with 1-4 CSS-only operations. Prefer replace with the full resulting CSS file "
                "when a hunk patch is ambiguous. Do not edit backend, JavaScript, HTML, route manifests, generated tests, "
                "or docs. Do not request tools when the listed CSS files are already provided in file_contexts."
            ),
        }

    @staticmethod
    def _is_self_blocked_tool_contract_response(payload: dict[str, Any]) -> bool:
        if str(payload.get("outcome") or "").strip().lower() not in {"fatal_invalid_response", "no_progress"}:
            return False
        if payload.get("operations"):
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
            "contract": "Tools are read-only diagnostics and cannot write files, run shell commands, execute Python scripts, or apply edits.",
            "required_next_action": "Return outcome=patch_ready with operations that create/replace/patch/delete files, or request read-only context only if specific files are still missing.",
            "previous_outcome": str(payload.get("outcome") or ""),
            "previous_diagnosis": str(payload.get("diagnosis") or payload.get("assistant_message") or "")[:1200],
        }

    @staticmethod
    def _is_empty_fatal_agent_response(payload: dict[str, Any]) -> bool:
        if str(payload.get("outcome") or "").strip().lower() != "fatal_invalid_response":
            return False
        if payload.get("operations") or payload.get("tool_requests"):
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
            "required_next_action": "Do not return fatal_invalid_response without a concrete blocker. Return patch_ready operations or request read-only context.",
            "previous_outcome": str(payload.get("outcome") or ""),
            "previous_message": str(payload.get("diagnosis") or payload.get("assistant_message") or "")[:1200],
        }

    @staticmethod
    def _agent_tool_requests(raw_tool_requests: list[Any]) -> list[dict[str, Any]]:
        allowed_tools = {"list_files", "read_files", "search_files", "inspect_diff", "run_checks", "run_command"}
        return [
            item
            for item in normalize_tool_requests(raw_tool_requests)
            if str(item.get("tool") or "").strip().lower() in allowed_tools
        ]

    def _inspect_diff(self, *, workspace_id: str, run_id: str, targets: list[str]) -> dict[str, object]:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        paths = self._paths_from_diff(diff_text)
        normalized_targets = [target.rstrip("/") for target in targets if target.strip()]
        if normalized_targets:
            selected_chunks: list[str] = []
            current_chunk: list[str] = []
            current_path = ""
            for line in diff_text.splitlines():
                if line.startswith("diff --git "):
                    if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                        selected_chunks.append("\n".join(current_chunk))
                    current_chunk = [line]
                    current_path = ""
                    match = re.match(r"^diff --git a/(.+?) b/(.+)$", line.strip())
                    if match:
                        current_path = match.group(2).strip().strip('"')
                    continue
                if current_chunk:
                    current_chunk.append(line)
            if current_chunk and self._path_matches_targets(current_path, normalized_targets):
                selected_chunks.append("\n".join(current_chunk))
            diff_text = "\n".join(selected_chunks)
            paths = [path for path in paths if self._path_matches_targets(path, normalized_targets)]
        return {
            "tool": "inspect_diff",
            "targets": targets,
            "paths": paths,
            "diff": truncate_tool_text(diff_text, max_chars=12000),
        }

    @staticmethod
    def _path_matches_targets(path: str, targets: list[str]) -> bool:
        normalized = path.strip().lstrip("./")
        return any(normalized == target or normalized.startswith(target.rstrip("/") + "/") for target in targets)

    def _coerce_operations(self, raw_operations: list[Any]) -> list[DraftFileOperation]:
        operations: list[DraftFileOperation] = []
        for item in raw_operations:
            if not isinstance(item, dict):
                continue
            operation = DraftFileOperation.model_validate(item)
            file_path = operation.file_path.strip().replace("\\", "/").lstrip("./")
            raw_patch = str(operation.diff or operation.content or "") if operation.operation == "patch" else ""
            codex_patch_paths = self._codex_update_patch_paths(raw_patch)
            if len(codex_patch_paths) == 1:
                file_path = codex_patch_paths[0]
            if not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
                raise ValueError(f"Agent returned an unsafe file path: {operation.file_path}")
            if any(file_path == prefix.rstrip("/") or file_path.startswith(prefix) for prefix in READ_ONLY_WRITE_PREFIXES):
                raise ValueError(f"Agent attempted to edit a read-only/generated surface: {file_path}")
            if operation.operation in {"create", "replace"} and operation.content is None:
                raise ValueError(f"Agent returned {operation.operation} for {file_path} without content.")
            if operation.operation == "patch" and not str(operation.diff or operation.content or "").strip():
                raise ValueError(f"Agent returned patch for {file_path} without a unified diff.")
            if operation.operation == "patch":
                raw_content = str(operation.content or "")
                if operation.diff and raw_content.strip() and raw_content != str(operation.diff) and not self._looks_like_unified_diff(raw_content):
                    operations.append(
                        DraftFileOperation(
                            operation_id=operation.operation_id,
                            file_path=file_path,
                            operation="replace",
                            content=raw_content,
                            diff=None,
                            reason=operation.reason,
                        )
                    )
                    continue
                if not self._looks_like_unified_diff(raw_patch):
                    operations.append(
                        DraftFileOperation(
                            operation_id=operation.operation_id,
                            file_path=file_path,
                            operation="replace",
                            content=raw_patch,
                            diff=None,
                            reason=operation.reason,
                        )
                    )
                    continue
            operations.append(
                DraftFileOperation(
                    operation_id=operation.operation_id,
                    file_path=file_path,
                    operation=operation.operation,
                    content=None if operation.operation == "patch" else operation.content,
                    diff=operation.diff or (operation.content if operation.operation == "patch" else None),
                    reason=operation.reason,
                )
            )
        return operations

    @staticmethod
    def _looks_like_unified_diff(text: str) -> bool:
        value = str(text or "")
        return bool(
            re.search(r"^@@\s", value, flags=re.MULTILINE)
            or re.search(r"^(---|\+\+\+)\s+", value, flags=re.MULTILINE)
            or re.search(r"^diff --git\s+", value, flags=re.MULTILINE)
            or re.search(r"^\*\*\* Update File:\s+", value, flags=re.MULTILINE)
        )

    @staticmethod
    def _codex_update_patch_paths(text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^\*\*\* Update File:\s+(.+)$", str(text or ""), flags=re.MULTILINE):
            path = match.group(1).strip().replace("\\", "/").lstrip("./")
            if path:
                paths.append(path)
        return list(dict.fromkeys(paths))

    def _prompt_alignment_smoke(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str = "",
        focused_edit_kind: str = "",
    ) -> RunCheckResult:
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id)
        if not diff_text.strip():
            return RunCheckResult(
                name="prompt_alignment_smoke",
                status="skipped",
                details="Prompt alignment smoke skipped because the draft has no product changes yet.",
                command="prompt alignment static smoke",
                logs=[],
            )
        if focused_edit_kind == "visual_style_edit":
            return RunCheckResult(
                name="prompt_alignment_smoke",
                status="skipped",
                details="Prompt alignment smoke skipped for focused visual/style edits; CSS can encode requested colors as design values rather than literal prompt words.",
                command="prompt alignment static smoke",
                logs=[],
            )
        if str(intent or "").strip().lower() in {"edit", "refine", "role_only_change"}:
            changed_paths = self._paths_from_diff(diff_text)
            if changed_paths and all(path.startswith("miniapp/app/static/") and path.endswith(".css") for path in changed_paths):
                return RunCheckResult(
                    name="prompt_alignment_smoke",
                    status="skipped",
                    details="Prompt alignment smoke skipped for CSS-only edits.",
                    command="prompt alignment static smoke",
                    logs=[],
                )
        prompt_lower = prompt.lower()
        targeted_fix_requested = any(
            marker in prompt_lower
            for marker in (
                "fix",
                "bug",
                "error",
                "404",
                "crash",
                "endpoint",
                "route",
                "not found",
                "ошиб",
                "почин",
                "фикс",
                "не работает",
            )
        )
        if targeted_fix_requested:
            return RunCheckResult(
                name="prompt_alignment_smoke",
                status="skipped",
                details="Prompt alignment smoke skipped for targeted fix prompts.",
                command="prompt alignment static smoke",
                logs=[],
            )
        changed_paths = self._paths_from_diff(diff_text)
        combined = []
        for path in changed_paths:
            if not path.startswith("miniapp/app/"):
                continue
            content = self.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content:
                combined.append(f"\n/* {path} */\n{content[:12000]}")
        haystack = "\n".join(combined).lower()
        issues: list[str] = []
        prompt_tokens = self._prompt_semantic_tokens(prompt, limit=10)
        if prompt_tokens:
            matched_tokens = [token for token in prompt_tokens if self._prompt_token_matches_text(token, haystack)]
            required_matches = 1 if len(prompt_tokens) < 4 else 2
            if len(matched_tokens) < required_matches:
                missing_tokens = [token for token in prompt_tokens if not self._prompt_token_matches_text(token, haystack)][:5]
                issues.append(
                    "Changed app files do not include enough prompt-specific language "
                    f"(matched {len(matched_tokens)}/{required_matches}; missing examples: {', '.join(missing_tokens)})."
                )
        return RunCheckResult(
            name="prompt_alignment_smoke",
            status="failed" if issues else "passed",
            details="Prompt alignment smoke checks that generated files reuse meaningful terms from the user prompt without assuming a predefined app category.",
            command="prompt alignment static smoke",
            logs=issues or ["Prompt alignment smoke passed."],
        )

    def _store_agent_quality_report(self, workspace_id: str, execution: CheckExecutionRecord) -> None:
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
    ) -> dict[str, object]:
        del preview_details
        failed = [result for result in results if result.status == "failed"]
        has_diff = bool(self.workspace_service.diff(workspace_id, run_id=run_id).strip())
        no_product_diff = request.mode in {"generate", "fix"} and not has_diff
        remaining_issues = [
            {
                "kind": "check_failure",
                "check": result.name,
                "details": result.details,
                "logs": result.logs[-8:],
                "blocking": True,
            }
            for result in failed
        ]
        if no_product_diff:
            remaining_issues.append(
                {
                    "kind": "prompt_alignment",
                    "check": "meaningful_diff",
                    "details": "Generation must create a prompt-specific draft diff before completion.",
                    "blocking": True,
                }
            )
        if validation_snapshot is not None:
            remaining_issues.extend(
                issue
                for issue in validation_snapshot.issues
                if isinstance(issue, dict) and issue.get("blocking", False)
            )
        complete = not failed and not no_product_diff
        return {
            "strict_green": complete,
            "optimistic_complete": complete,
            "preview_ok": True,
            "validators_ok": not any(result.status == "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"}),
            "build_ok": not any(result.status == "failed" for result in results if result.name == "changed_files_static"),
            "canonical_smoke_ok": not any(result.status == "failed" for result in results if result.name == "platform_invariants"),
            "remaining_issues": remaining_issues,
        }

    def _validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        prompt_failed = any(item.status == "failed" for item in execution.results if item.name == "prompt_alignment_smoke")
        return ValidationSnapshot(
            platform_valid=not bool(issues),
            prompt_alignment_valid=not prompt_failed,
            checks_valid=not bool(issues),
            build_valid=not build_failed,
            blocking=bool(issues),
            issues=issues,
        )

    @staticmethod
    def _fast_gate_passed(results: list[RunCheckResult]) -> bool:
        return not any(result.status == "failed" for result in results)

    def _finalize_job(self, *, job: JobRecord, loop_result: WorkspaceLoopResult, elapsed_ms: int) -> JobRecord:
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
            job.failure_reason = self._specific_failure_reason(
                default=job.failure_reason,
                remaining_issues=job.remaining_issues,
                latest_execution=loop_result.latest_execution,
            )
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
                job.flow_coverage = {
                    **dict(job.flow_coverage or {}),
                    "status": flow_result.status,
                    "diagnostics": dict(flow_result.diagnostics or {}),
                    "logs": list(flow_result.logs or []),
                }
        if loop_result.status == "completed":
            job.outcome_kind = "applied"
            job.failure_reason = None
            job.failure_class = None
            job.failure_signature = None
            job.root_cause_summary = None
            job.validation_snapshot = ValidationSnapshot(
                platform_valid=True,
                prompt_alignment_valid=True,
                checks_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        job.fix_attempts = [
            FixAttemptRecord(
                run_id=job.linked_run_id or "",
                attempt=int(turn.get("attempt") or 0),
                diagnosis=str(turn.get("diagnosis") or turn.get("assistant_message") or ""),
                files_changed=[str(path) for path in turn.get("files_changed") or []],
                implicated_files=[str(path) for path in turn.get("fix_targets") or []],
                failure_signature=str(turn.get("failure_signature") or "") or None,
                result="patched" if str(turn.get("result")) == "patched" else "failed",
            ).model_dump(mode="json")
            for turn in loop_result.turn_history
        ]
        job.latency_breakdown["agent_total_ms"] = elapsed_ms
        job.updated_at = datetime.now(timezone.utc)
        self._save_job(job)
        self._store_report(f"fix_attempts:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.fix_attempts})
        self._store_report(f"remaining_issues:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.remaining_issues})
        if job.validation_snapshot is not None:
            self._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        if loop_result.latest_preview_details:
            self._store_report(f"fix_runtime:{job.workspace_id}", {"workspace_id": job.workspace_id, **loop_result.latest_preview_details})
        event_type = "job_completed" if job.status == "completed" else "job_failed"
        self._append_event(job, event_type, job.summary or ("Workspace code agent completed." if job.status == "completed" else "Workspace code agent failed."))
        return job

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
            generic_markers = (
                "failed",
                "error:",
                "traceback",
            )
            clean_logs = [" ".join(str(line or "").split()).strip() for line in logs if str(line or "").strip()]
            for line in reversed(clean_logs):
                lowered = line.lower()
                if any(marker in lowered for marker in specific_markers):
                    return line[:320]
            for line in reversed(clean_logs):
                lowered = line.lower()
                if any(marker in lowered for marker in generic_markers):
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
                if result.status != "failed":
                    continue
                line = _important_line(list(result.logs or []))
                if line:
                    return f"{result.name}: {line}"
                if result.details:
                    return f"{result.name}: {result.details[:320]}"
        return default

    def _seed_file_context(self, workspace_id: str, run_id: str, *, role_scope: list[str] | None = None) -> dict[str, str]:
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
        for path in [*role_paths, *SEED_CONTEXT_PATHS]:
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
                    "Generated tests are product acceptance tests. On an edit, either preserve the app selectors/text they still validly assert, "
                    "or update the generated test file in the same patch when the requested behavior intentionally changes the expectation."
                ),
                "required_next_action": (
                    "Return operations that make the next generated test result different: restore missing selectors/ids in app code, "
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
            syntax_error = diagnostics.get("static_js_syntax_error")
            if not isinstance(syntax_error, dict):
                continue
            file_path = str(syntax_error.get("file_path") or "").strip().lstrip("./")
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
                    "diagnostics": syntax_error,
                }
            )
        if not failures:
            return
        if any(item.get("tool") == "static_js_failure_context" for item in tool_results if isinstance(item, dict)):
            return
        tool_results.append(
            {
                "tool": "static_js_failure_context",
                "contract": "changed_files_static is a blocking platform syntax check for generated JavaScript.",
                "required_next_action": (
                    "Patch the exact file_path from diagnostics so node --check passes. "
                    "Use a targeted hunk patch or full-file replace for that JavaScript file. "
                    "Do not spend the next turn rewriting unrelated pages or tests while this syntax error remains."
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
            "build.broken_static_ref",
            "build.missing_static_asset",
            "build.missing_static_page",
            "build.page_missing_preview_bridge",
            "build.page_missing_shell_style_link",
            "build.page_missing_shell_root",
            "platform.missing_generated_app_tests",
            "platform.single_page_role_surface",
            "platform.missing_create_get_api",
            "platform.missing_create_post_api",
            "platform.frontend_missing_post_api",
            "platform.preloaded_business_data",
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
                    }
                )
        if not issues:
            return
        for path in ["miniapp/app/generated/route_manifest.json", *[str(issue.get("location") or "") for issue in issues]]:
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
                    "For build.broken_static_ref or build.missing_static_asset, either create the referenced asset or remove the script/link tag from the exact page. "
                    "For platform.missing_generated_app_tests, create miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs. "
                    "For platform.single_page_role_surface, create the missing child pages for that exact role and add them to route_manifest.json. "
                    "For platform.missing_create_get_api/platform.missing_create_post_api, create or register a backend /api route with persistent GET and POST behavior. "
                    "For platform.frontend_missing_post_api, add form/fetch code that POSTs user-provided records to /api. "
                    "For platform.preloaded_business_data, remove hard-coded business records and start from empty persistent state. "
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
            "fix_attempts",
            "scope_expansions",
            "fix_runtime",
            "remaining_issues",
            "agent_diagnostics",
        ):
            self.store.delete("reports", f"{key}:{workspace_id}")

    def _append_agent_diagnostic(self, workspace_id: str, entry: dict[str, Any]) -> None:
        report_key = f"agent_diagnostics:{workspace_id}"
        current = self.store.get("reports", report_key) or {"workspace_id": workspace_id, "items": []}
        items = list(current.get("items", [])) if isinstance(current, dict) else []
        items.append({**entry, "recorded_at": datetime.now(timezone.utc).isoformat()})
        self._store_report(report_key, {"workspace_id": workspace_id, "items": items[-80:]})

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        details = self._enriched_event_details(details or {})
        job.events.append(JobEvent(event_type=event_type, message=message, details=details))
        if event_type == "iteration_ready":
            job.token_usage = self._merge_run_token_usage(
                job.token_usage if isinstance(job.token_usage, dict) else {},
                details,
            )
        job.updated_at = datetime.now(timezone.utc)
        noisy_check_event = self._is_noisy_check_progress_event(event_type, details)
        if not noisy_check_event:
            self._sync_run_progress(job, event_type, message, details)
        if not noisy_check_event or self._should_flush_noisy_event(job):
            self._save_job(job)
        if not noisy_check_event:
            self.workspace_log_service.append(job.workspace_id, source=f"agent.{event_type}", message=message, payload=details)

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
        for key in ("files", "changed_files", "operation_files"):
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
            value = str(path or "").replace("\\", "/").lstrip("./")
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
            path = str(raw_path or "").replace("\\", "/").lstrip("./")
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
                "miniapp/app/main.py",
                "miniapp/app/db.py",
                "miniapp/app/schemas.py",
            }:
                groups["backend"] += 1
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

    def _store_report(self, key: str, payload: dict[str, Any]) -> None:
        self.store.upsert("reports", key, payload)

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
            payload["progress_percent"] = min(98, max(progress, min(existing_progress, 98)))
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
        payload["acceptance_contract"] = dict(job.acceptance_contract)
        payload["worker_summaries"] = list(job.worker_summaries)
        payload["flow_coverage"] = dict(job.flow_coverage)
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
            "parallel_build_started",
            "parallel_build_completed",
            "parallel_build_failed",
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
            "job_started": ("Starting code agent", 4),
            "spec_extract_started": ("Extracting workflow contract", 6),
            "parallel_build_started": ("Running Fast parallel workers", 24),
            "parallel_build_completed": ("Merged Fast parallel workers", 52),
            "parallel_build_failed": ("Fast parallel build fallback", 38),
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
            "job_failed": ("Failed", 98),
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
            return f"Prepared edit context {attempt}"
        if phase == "model_request":
            return f"Generating code edit {attempt}"
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
        base = 24 + (attempt - 1) * 6 + tool_round * 3
        if phase == "context_ready":
            return min(48, base + 6)
        if phase == "model_request":
            return min(49, base + 12)
        return min(45, base)

    @classmethod
    def _iteration_ready_stage(cls, details: dict[str, Any]) -> str:
        operation_count = cls._safe_int(details.get("operation_count"), default=0)
        tool_request_count = cls._safe_int(details.get("tool_request_count"), default=0)
        outcome = str(details.get("outcome") or "").strip().lower()
        if operation_count > 0:
            return f"Prepared {operation_count} file edit{'s' if operation_count != 1 else ''}"
        if tool_request_count > 0:
            return f"Requested {tool_request_count} context read{'s' if tool_request_count != 1 else ''}"
        if outcome in {"no_progress", "no_op"}:
            return "No file edits returned"
        return "Model response received"

    @classmethod
    def _iteration_ready_progress(cls, details: dict[str, Any]) -> int:
        attempt = max(1, cls._safe_int(details.get("attempt"), default=1))
        tool_round = cls._safe_int(details.get("tool_round"), default=0)
        operation_count = cls._safe_int(details.get("operation_count"), default=0)
        tool_request_count = cls._safe_int(details.get("tool_request_count"), default=0)
        if bool(details.get("has_draft_diff")):
            if operation_count > 0:
                return min(94, 87 + max(0, attempt - 2) * 3)
            if tool_request_count > 0:
                return min(93, 85 + max(0, attempt - 2) * 3)
            return min(92, 84 + max(0, attempt - 2) * 3)
        base = 38 + max(0, attempt - 1) * 5 + tool_round * 2
        if operation_count > 0:
            return min(51, base + 7)
        if tool_request_count > 0:
            return min(50, base + 3)
        return min(50, base + 2)

    @classmethod
    def _repair_stage(cls, details: dict[str, Any]) -> str:
        reason = str(details.get("reason") or "").strip()
        outcome = str(details.get("outcome") or "").strip()
        if reason == "self_blocked_no_operations":
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
    def _max_attempts(generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            return 5
        if generation_mode == GenerationMode.QUALITY:
            return 8
        return 6

    @staticmethod
    def _tool_round_limit(generation_mode: GenerationMode) -> int:
        if generation_mode == GenerationMode.FAST:
            return 1
        if generation_mode == GenerationMode.QUALITY:
            return 4
        return 3

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
