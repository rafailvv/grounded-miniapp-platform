from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.ai.model_registry import VISUAL_PATCH_MODEL
from app.models.domain import ChatTurnRecord, DraftFileOperation, GenerateRequest, JobRecord, ValidationSnapshot
from app.services.miniapp_generation.constants import ROLE_ORDER
from app.validators.build_validator import BuildValidator


class VisualFastLaneFallback(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualPatchTargets:
    writable_paths: list[str]
    context_paths: list[str]
    roles: list[str]
    global_patch: bool


class MiniappVisualPatchFastLane:
    _STATIC_PREFIX = "miniapp/app/static/"
    _STATIC_EXTENSIONS = {".html", ".css", ".js"}
    _MAX_WRITABLE_FILES = 12
    _MAX_CONTEXT_CHARS = 80000
    _MAX_CHANGED_DIFF_LINES = 700

    _VISUAL_PATTERNS = (
        r"\b(?:color|colour|background|theme|dark|light|style|css|layout|spacing|margin|padding|radius|border|shadow|font|size|width|height|align|center|move|position|header|avatar|logo|image|icon|label|copy|text|rename|bigger|smaller)\b",
        r"\b(?:button|card|badge|chip|title|subtitle|hero|profile)\b",
        r"(?:цвет|фон|стил|визуал|отступ|размер|шрифт|кнопк|карточк|аватар|логотип|картин|иконк|текст|надпис|переимен|перемест|располож|выровн|профил)",
    )
    _HARD_NEGATIVE_PATTERNS = (
        r"\b(?:api|backend|database|db|schema|endpoint|route|fastapi|sql|sqlite|docker|build|test|pytest|traceback|stack trace|runtime error|import error)\b",
        r"\b(?:persist|persistence|save|stored|crud|record|data model|shared state|status|approve|reject|assign|return equipment|availability|conflict)\b",
        r"\b(?:separate page|new page|navigate|navigation|click item|list-to-detail|list to detail|page flow)\b",
        r"(?:апи|бекенд|бэкенд|база|схем|маршрут|эндпоинт|докер|сборк|тест|ошибк|трейсбек|рантайм|импорт)",
        r"(?:сохран|персист|статус|подтверд|отклон|назнач|вернуть|доступност|конфликт|детальн(?:ая|ую) страниц|отдельн(?:ая|ую) страниц|навигац)",
    )
    _TECHNICAL_FIX_NEGATIVE_PATTERNS = (
        r"\b(?:failed|blocked|build failed|tests? failed|schema failed|api error|loading data fails|unable to load|could not load|try again|refresh page)\b",
        r"(?:не загруз|не работает api|failed|blocked|ошибка сборки|тест.*упал|не может загруз|попробуйте снова|refresh)",
    )
    _GLOBAL_PATTERNS = (
        r"\b(?:everywhere|all roles|all pages|global|shared|common|whole app)\b",
        r"(?:везде|во всех ролях|на всех страницах|глобально|общий|общая)",
    )
    _DETAIL_VISUAL_PATTERNS = (
        r"\b(?:detail|details|profile)\b",
        r"(?:детал|профил)",
    )
    _GENERIC_FIX_PROMPTS = (
        "analyze the reported failure and apply the smallest safe fix.",
        "analyze the reported failure and apply the smallest safe fix",
    )

    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def should_attempt(
        cls,
        *,
        prompt: str,
        intent: str,
        run_mode: str,
        role_scope: list[str],
        error_context: Any | None = None,
    ) -> bool:
        lowered = cls._normalize_prompt(prompt)
        if not lowered:
            return False
        negative_scan = cls._strip_preservation_clauses(lowered)
        if intent == "create":
            return False
        if intent not in {"auto", "edit", "refine", "role_only_change"}:
            return False
        if not cls._matches_any(lowered, cls._VISUAL_PATTERNS):
            return False
        if cls._matches_any(negative_scan, cls._HARD_NEGATIVE_PATTERNS):
            return False
        raw_error = str(getattr(error_context, "raw_error", "") or "")
        if raw_error.strip():
            return False
        if run_mode == "fix" and cls._matches_any(negative_scan, cls._TECHNICAL_FIX_NEGATIVE_PATTERNS):
            return False
        explicit_roles = cls._mentioned_roles(lowered)
        if len(role_scope) > 1 and not cls._matches_any(lowered, cls._GLOBAL_PATTERNS):
            scoped_explicit_roles = [role for role in explicit_roles if role in role_scope]
            if len(scoped_explicit_roles) != 1:
                return False
        explicit_page = bool(re.search(r"\b(?:page|screen|section|block|header|profile)\b|(?:страниц|экран|раздел|блок|хедер|профил)", lowered))
        if not role_scope and not explicit_roles and not explicit_page and not cls._matches_any(lowered, cls._GLOBAL_PATTERNS):
            return False
        return True

    @classmethod
    def _visual_prompt_for_request(cls, request: GenerateRequest) -> str:
        prompt = str(getattr(request, "prompt", "") or "").strip()
        raw_error = str(getattr(getattr(request, "error_context", None), "raw_error", "") or "").strip()
        if raw_error and cls._is_generic_fix_prompt(prompt):
            return raw_error
        return prompt

    @classmethod
    def _is_generic_fix_prompt(cls, prompt: str) -> bool:
        normalized = cls._normalize_prompt(prompt)
        return normalized in cls._GENERIC_FIX_PROMPTS

    @classmethod
    def _strip_preservation_clauses(cls, lowered: str) -> str:
        clauses = re.split(r"(?<=[.;!?])\s+|[;\n]+", lowered)
        filtered: list[str] = []
        for clause in clauses:
            if re.search(r"\b(?:keep|preserve|leave|without changing)\b|(?:сохран|остав|не меня)", clause) and re.search(
                r"\b(?:existing|current|same|unchanged|working)\b|(?:текущ|существ|как есть|работ)",
                clause,
            ):
                continue
            filtered.append(clause)
        return " ".join(filtered)

    def try_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        role_scope: list[str],
        started_at: float,
        draft_source: Path | None = None,
        run_mode: str,
    ) -> JobRecord | None:
        visual_prompt = self._visual_prompt_for_request(request)
        if not self.should_attempt(
            prompt=visual_prompt,
            intent=request.intent,
            run_mode=run_mode,
            role_scope=role_scope,
            error_context=None if visual_prompt != request.prompt else request.error_context,
        ):
            return None

        if draft_source is None:
            draft_source = self.service.workspace_service.prepare_draft(workspace_id, run_id)
            self._append_event(job, "draft_prepared", "Prepared draft workspace for a fast visual patch.")
        targets = self._resolve_targets(workspace_id=workspace_id, run_id=run_id, prompt=visual_prompt, role_scope=role_scope)
        if targets is None:
            return None

        patch_applied = False
        try:
            self._append_event(
                job,
                "fast_visual_patch",
                "Applying a fast visual patch.",
                {"target_files": targets.writable_paths, "model": VISUAL_PATCH_MODEL},
            )
            result = self._request_visual_patch(
                workspace_id=workspace_id,
                run_id=run_id,
                prompt=visual_prompt,
                targets=targets,
            )
            operations = self._operations_from_payload(
                workspace_id=workspace_id,
                run_id=run_id,
                payload=dict(result.get("payload") or {}),
                targets=targets,
            )
            original_contents = {
                operation.file_path: self.service.workspace_service.try_read_text_file(workspace_id, operation.file_path, run_id=run_id) or ""
                for operation in operations
            }
            self._guard_operations(operations=operations, original_contents=original_contents, targets=targets)
            envelope = self.service.workspace_service.build_patch_envelope_for_draft(workspace_id, run_id, operations)
            apply_result = self.service.workspace_service.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
            if apply_result.status != "applied":
                raise VisualFastLaneFallback(apply_result.conflict_reason or "Visual patch could not be applied.")
            patch_applied = True
            self._quick_check(
                workspace_id=workspace_id,
                run_id=run_id,
                operations=operations,
                original_contents=original_contents,
                draft_source=draft_source,
            )
            return self._complete_job(
                workspace_id=workspace_id,
                run_id=run_id,
                request=request,
                job=job,
                started_at=started_at,
                model=str(result.get("model") or VISUAL_PATCH_MODEL),
                operations=operations,
                summary=str((result.get("payload") or {}).get("summary") or "Fast visual patch applied."),
                apply_result=apply_result,
            )
        except VisualFastLaneFallback as exc:
            if patch_applied:
                self.service.workspace_service.prepare_draft(workspace_id, run_id)
            self._append_trace(
                workspace_id,
                "fast_visual_patch_fallback",
                "Fast visual patch fell back to the normal pipeline.",
                {"reason": str(exc), "run_mode": run_mode},
            )
            return None
        except Exception as exc:
            if patch_applied:
                self.service.workspace_service.prepare_draft(workspace_id, run_id)
            self._append_trace(
                workspace_id,
                "fast_visual_patch_fallback",
                "Fast visual patch errored and fell back to the normal pipeline.",
                {"reason": str(exc), "run_mode": run_mode, "exception_type": type(exc).__name__},
            )
            return None

    @staticmethod
    def _normalize_prompt(prompt: str) -> str:
        return re.sub(r"\s+", " ", str(prompt or "").strip().lower())

    @classmethod
    def _matches_any(cls, lowered: str, patterns: tuple[str, ...]) -> bool:
        return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)

    @classmethod
    def _mentioned_roles(cls, lowered: str) -> list[str]:
        role_markers = {
            "client": (r"\bclient\b", r"\bcustomer\b", r"(?:клиент|пользователь|сотрудник)"),
            "specialist": (r"\bspecialist\b", r"\boperator\b", r"(?:специалист|оператор)"),
            "manager": (r"\bmanager\b", r"\badmin\b", r"(?:менеджер|руководител|админ)"),
        }
        roles: list[str] = []
        for role in ROLE_ORDER:
            if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in role_markers[role]):
                roles.append(role)
        return roles

    @classmethod
    def _is_global_patch(cls, lowered: str) -> bool:
        return cls._matches_any(lowered, cls._GLOBAL_PATTERNS)

    @classmethod
    def _is_static_path(cls, path: str) -> bool:
        normalized = str(path or "").strip().lstrip("./")
        if ".." in Path(normalized).parts:
            return False
        if not normalized.startswith(cls._STATIC_PREFIX):
            return False
        if normalized == "miniapp/app/static/preview_bridge.js":
            return False
        return Path(normalized).suffix in cls._STATIC_EXTENSIONS

    def _resolve_targets(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        role_scope: list[str],
    ) -> VisualPatchTargets | None:
        lowered = self._normalize_prompt(prompt)
        tree_paths = [
            str(item.get("path") or "").strip().lstrip("./")
            for item in self.service.workspace_service.file_tree(workspace_id, run_id=run_id)
            if item.get("type") == "file"
        ]
        static_paths = [path for path in tree_paths if self._is_static_path(path)]
        if not static_paths:
            return None
        global_patch = self._is_global_patch(lowered)
        explicit_roles = self._mentioned_roles(lowered)
        scoped_roles = [role for role in ROLE_ORDER if role in role_scope]
        if len(scoped_roles) > 1 and explicit_roles and not global_patch:
            roles = [role for role in explicit_roles if role in scoped_roles]
        elif scoped_roles:
            roles = scoped_roles
        else:
            roles = explicit_roles
        if global_patch and not roles:
            roles = [role for role in ROLE_ORDER if any(path.startswith(f"miniapp/app/static/{role}/") for path in static_paths)]
        if not roles:
            return None
        if len(role_scope) > 1 and not global_patch:
            if len(roles) != 1:
                return None

        include_detailish = self._matches_any(lowered, self._DETAIL_VISUAL_PATTERNS)
        wants_copy = bool(re.search(r"\b(?:text|label|copy|rename|title|subtitle)\b|(?:текст|надпис|переимен|заголов)", lowered))
        wants_js = wants_copy or bool(re.search(r"\b(?:button|icon|avatar|logo|image)\b|(?:кнопк|иконк|аватар|логотип|картин)", lowered))
        writable: list[str] = []
        for role in roles:
            role_prefix = f"miniapp/app/static/{role}/"
            role_paths = [path for path in static_paths if path.startswith(role_prefix)]
            root_triplet = [f"{role_prefix}{name}" for name in ("index.html", "styles.css", "app.js") if f"{role_prefix}{name}" in role_paths]
            if global_patch:
                writable.extend([path for path in root_triplet if path.endswith((".html", ".css")) or wants_js])
            else:
                writable.extend(root_triplet)
            if include_detailish:
                writable.extend(
                    path
                    for path in role_paths
                    if any(part in path.lower() for part in ("detail", "details", "profile"))
                )
        if global_patch and "miniapp/app/static/shared/base.css" in static_paths:
            writable.append("miniapp/app/static/shared/base.css")
        writable = list(dict.fromkeys(path for path in writable if self._is_static_path(path)))
        if not writable or len(writable) > self._MAX_WRITABLE_FILES:
            return None
        context = list(writable)
        if "miniapp/app/static/shared/base.css" in static_paths and "miniapp/app/static/shared/base.css" not in context:
            context.append("miniapp/app/static/shared/base.css")
        total_chars = 0
        for path in context:
            total_chars += len(self.service.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id) or "")
        if total_chars > self._MAX_CONTEXT_CHARS:
            return None
        return VisualPatchTargets(
            writable_paths=writable,
            context_paths=list(dict.fromkeys(context)),
            roles=roles,
            global_patch=global_patch,
        )

    def _request_visual_patch(
        self,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        targets: VisualPatchTargets,
    ) -> dict[str, Any]:
        file_contexts: list[dict[str, Any]] = []
        for path in targets.context_paths:
            content = self.service.workspace_service.try_read_text_file(workspace_id, path, run_id=run_id)
            if content is None:
                continue
            file_contexts.append(
                {
                    "path": path,
                    "writable": path in targets.writable_paths,
                    "content": content,
                }
            )
        if not file_contexts:
            raise VisualFastLaneFallback("No static file context is available for the visual patch.")
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["replace"]},
                            "content": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["file_path", "operation", "content", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "operations"],
            "additionalProperties": False,
        }
        system_prompt = (
            "You are applying a fast visual-only frontend patch to an existing mini-app. "
            "Only change static HTML/CSS/JS presentation for the requested role or page. "
            "Do not change API paths, data fetching semantics, persistence, schemas, routes, generated artifacts, tests, or backend logic. "
            "Return full-file replacements only for writable files. If the request needs real behavior, data, routes, or page creation, return no operations."
        )
        user_prompt = json.dumps(
            {
                "user_request": prompt,
                "roles": targets.roles,
                "global_patch": targets.global_patch,
                "writable_files": targets.writable_paths,
                "files": file_contexts,
            },
            ensure_ascii=False,
        )
        return self.service.openrouter_client.generate_structured(
            role="code_edit",
            schema_name="visual_patch_fast_lane_v1",
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_override=VISUAL_PATCH_MODEL,
            fallback_model_override=VISUAL_PATCH_MODEL,
            responses_tuning_override={"reasoning": {"effort": "low"}},
        )

    def _operations_from_payload(
        self,
        *,
        workspace_id: str,
        run_id: str,
        payload: dict[str, Any],
        targets: VisualPatchTargets,
    ) -> list[DraftFileOperation]:
        raw_operations = payload.get("operations")
        if not isinstance(raw_operations, list) or not raw_operations:
            raise VisualFastLaneFallback("Visual patch model returned no operations.")
        operations: list[DraftFileOperation] = []
        for raw_operation in raw_operations:
            if not isinstance(raw_operation, dict):
                raise VisualFastLaneFallback("Visual patch model returned an invalid operation.")
            file_path = str(raw_operation.get("file_path") or "").strip().lstrip("./")
            if file_path not in targets.writable_paths:
                raise VisualFastLaneFallback(f"Visual patch attempted to edit a non-writable file: {file_path}")
            if raw_operation.get("operation") != "replace":
                raise VisualFastLaneFallback("Visual patch attempted a non-replace operation.")
            if self.service.workspace_service.try_read_text_file(workspace_id, file_path, run_id=run_id) is None:
                raise VisualFastLaneFallback(f"Visual patch attempted to replace a missing file: {file_path}")
            content = raw_operation.get("content")
            if not isinstance(content, str) or not content.strip():
                raise VisualFastLaneFallback(f"Visual patch returned empty content for {file_path}.")
            operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=content,
                    reason=str(raw_operation.get("reason") or "Fast visual-only patch."),
                )
            )
        return operations

    def _guard_operations(
        self,
        *,
        operations: list[DraftFileOperation],
        original_contents: dict[str, str],
        targets: VisualPatchTargets,
    ) -> None:
        if len(operations) > self._MAX_WRITABLE_FILES:
            raise VisualFastLaneFallback("Visual patch touched too many files.")
        changed_any = False
        diff_lines = 0
        for operation in operations:
            if operation.operation != "replace" or operation.file_path not in targets.writable_paths:
                raise VisualFastLaneFallback(f"Forbidden visual patch operation for {operation.file_path}.")
            if not self._is_static_path(operation.file_path):
                raise VisualFastLaneFallback(f"Forbidden non-static visual patch file: {operation.file_path}.")
            original = original_contents.get(operation.file_path, "")
            updated = operation.content or ""
            if updated != original:
                changed_any = True
            diff_lines += sum(
                1
                for line in difflib.unified_diff(original.splitlines(), updated.splitlines(), lineterm="")
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
        if not changed_any:
            raise VisualFastLaneFallback("Visual patch was a no-op.")
        if diff_lines > self._MAX_CHANGED_DIFF_LINES:
            raise VisualFastLaneFallback("Visual patch diff is too broad for fast-lane.")

    def _quick_check(
        self,
        *,
        workspace_id: str,
        run_id: str,
        operations: list[DraftFileOperation],
        original_contents: dict[str, str],
        draft_source: Path,
    ) -> None:
        issues: list[str] = []
        changed_map = {operation.file_path: operation.content or "" for operation in operations}
        for path, content in changed_map.items():
            for issue in BuildValidator._static_ui_text_artifact_issues(content, path):
                issues.append(f"{path}: {issue.message}")
            if self._introduced_visible_loading_or_error(path, original_contents.get(path, ""), content):
                issues.append(f"{path}: visual patch introduced visible loading/error placeholder copy.")
            suffix = Path(path).suffix
            if suffix == ".css":
                css_issue = self._css_issue(content)
                if css_issue:
                    issues.append(f"{path}: {css_issue}")
            elif suffix == ".js":
                js_issue = self._js_issue(content)
                if js_issue:
                    issues.append(f"{path}: {js_issue}")
        issues.extend(self._dom_id_issues(workspace_id=workspace_id, run_id=run_id, changed_map=changed_map))
        if issues:
            raise VisualFastLaneFallback("; ".join(issues[:5]))
        if not draft_source.exists():
            raise VisualFastLaneFallback("Draft source disappeared during fast visual patch.")

    @staticmethod
    def _introduced_visible_loading_or_error(path: str, original: str, updated: str) -> bool:
        if Path(path).suffix not in {".html", ".js"}:
            return False
        markers = (
            "loading all",
            "loading data",
            "loading...",
            "unable to load data",
            "could not load",
            "please refresh",
            "try again",
        )
        original_lower = original.lower()
        updated_lower = updated.lower()
        return any(marker in updated_lower and marker not in original_lower for marker in markers)

    @staticmethod
    def _css_issue(content: str) -> str | None:
        if "<script" in content.lower() or "</" in content.lower():
            return "CSS contains HTML/script fragments."
        if content.count("{") != content.count("}"):
            return "CSS braces are unbalanced."
        return None

    @staticmethod
    def _js_issue(content: str) -> str | None:
        node_path = shutil.which("node")
        if not node_path:
            return None
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".js", delete=True) as handle:
            handle.write(content)
            handle.flush()
            result = subprocess.run(
                [node_path, "--check", handle.name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        if result.returncode != 0:
            return (result.stderr or result.stdout or "JavaScript syntax check failed.").strip().splitlines()[0][:240]
        return None

    def _dom_id_issues(
        self,
        *,
        workspace_id: str,
        run_id: str,
        changed_map: dict[str, str],
    ) -> list[str]:
        issues: list[str] = []
        for path, content in changed_map.items():
            if not path.endswith(".js"):
                continue
            html_path = str(Path(path).with_name("index.html"))
            html_content = changed_map.get(html_path)
            if html_content is None:
                html_content = self.service.workspace_service.try_read_text_file(workspace_id, html_path, run_id=run_id)
            if not html_content:
                continue
            html_ids = BuildValidator._extract_html_ids(html_content)
            script_ids = BuildValidator._extract_js_dom_ids(content)
            missing = sorted(script_ids - html_ids)
            if missing:
                issues.append(f"{path}: JS references missing DOM ids in {html_path}: {', '.join(missing[:6])}")
        return issues

    def _complete_job(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        started_at: float,
        model: str,
        operations: list[DraftFileOperation],
        summary: str,
        apply_result: Any,
    ) -> JobRecord:
        changed_files = [operation.file_path for operation in operations]
        snapshot = ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=False,
            issues=[],
        )
        job.status = "completed"
        job.outcome_kind = "applied"
        job.failure_reason = None
        job.failure_class = None
        job.failure_signature = None
        job.root_cause_summary = None
        job.current_fix_phase = "completed" if job.mode == "fix" else None
        job.validation_snapshot = snapshot
        job.summary = summary.strip() or "Fast visual patch applied."
        job.llm_model = model
        job.fix_targets = changed_files
        job.compile_summary = {
            "mode": "fast_visual_patch",
            "files": len(changed_files),
            "changed_files": len(changed_files),
        }
        job.latency_breakdown["total_ms"] = int((time.perf_counter() - started_at) * 1000)
        if hasattr(apply_result, "model_dump"):
            job.apply_result = apply_result.model_dump(mode="json")
        else:
            job.apply_result = dict(apply_result or {})
        job.artifacts = {
            "candidate_diff": "reports/candidate_diff",
            "validation": "reports/validation",
            "fast_visual_patch": "reports/fast_visual_patch",
        }
        diff_text = self.service.workspace_service.diff(workspace_id, run_id=run_id)
        self.service._store_report(
            f"candidate_diff:{workspace_id}",
            {"workspace_id": workspace_id, "run_id": run_id, "diff": diff_text, "files": changed_files},
        )
        self.service._store_report(f"validation:{workspace_id}", snapshot.model_dump(mode="json"))
        self.service._store_report(
            f"fast_visual_patch:{workspace_id}",
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "model": model,
                "files": changed_files,
                "summary": job.summary,
                "checks": "quick_scoped",
            },
        )
        assistant_turn = ChatTurnRecord(
            workspace_id=workspace_id,
            role="assistant",
            content=job.summary,
            summary=job.summary,
            linked_job_id=job.job_id,
            linked_run_id=request.linked_run_id,
        )
        self.service.store.upsert("chat_turns", assistant_turn.turn_id, assistant_turn.model_dump(mode="json"))
        self._append_event(job, "checks_completed", "Fast visual checks completed.", {"files": changed_files})
        self._append_event(job, "job_completed", "Fast visual patch completed successfully.", {"files": changed_files})
        return job

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.service._append_event(job, event_type, message, details)

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict[str, Any] | None = None) -> None:
        self.service._append_trace(workspace_id, stage, message, payload)
