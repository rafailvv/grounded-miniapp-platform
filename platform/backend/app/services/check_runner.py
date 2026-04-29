from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import difflib
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.models.common import GenerationMode
from app.models.artifacts import ValidationIssue
from app.models.domain import CheckExecutionRecord, RunCheckResult, utc_now
from app.modules.miniapp_validation.generation_preflight_validation import GenerationPreflightValidation
from app.services.workspace.preview_service import PreviewService
from app.validators.static_analysis import (
    extract_declared_routes,
    extract_frontend_api_refs,
    extract_html_ids,
    extract_js_dom_ids,
    extract_script_refs,
    normalize_api_path,
    role_static_root,
)
from app.validators.suite import ValidationSuite
from app.services.workflow_acceptance import build_acceptance_contract


ROLE_ORDER = ("client", "specialist", "manager")
NEUTRAL_TEMPLATE_MARKERS = (
    "client preview",
    "specialist preview",
    "manager preview",
    "client surface",
    "specialist surface",
    "manager surface",
    "neutral starter",
    "starter screen",
    "preview entry",
    "should be replaced by the generated app",
    "replace starter screens",
)
ROLE_LINK_STOPWORDS = {
    "client",
    "specialist",
    "manager",
    "preview",
    "surface",
    "starter",
    "screen",
    "generated",
    "static",
    "script",
    "style",
    "button",
    "section",
    "header",
    "footer",
    "main",
    "role",
    "page",
    "app",
    "miniapp",
    "telegram",
    "пользователь",
    "клиент",
    "специалист",
    "менеджер",
    "страница",
    "экран",
    "приложение",
}
MIN_ROLE_ROUTE_PAGES = 3
PRELOADED_BUSINESS_DATA_MARKERS = (
    "mock data",
    "mock-data",
    "demo data",
    "demo-data",
    "sample data",
    "sample-data",
    "seed data",
    "seed-data",
    "fixture records",
    "fixture data",
    "preloaded records",
    "preloaded data",
    "hard-coded business records",
    "hardcoded business records",
    "initialrecords",
    "samplerecords",
    "demorecords",
    "seedrecords",
    "mockrecords",
    "мок-данн",
    "демо-данн",
    "тестовые данные",
)
CSS_PLACEHOLDER_MARKERS = (
    "generated client page styles can replace this file",
    "generated specialist page styles can replace this file",
    "generated manager page styles can replace this file",
    "styles can replace this file",
)
PAGE_SHELL_SAFE_TOP_MARKERS = (
    "var(--telegram-top-safe-offset)",
    "telegram-top-safe-offset",
    "safe-area-inset-top",
    "max(76px",
)


class CheckRunner:
    _API_FAILURE_RE = re.compile(
        r"(?P<label>Create|Update|List|Post-update list|Post-create list)\s+API\s+failed:\s*"
        r"(?:(?P<method>POST|PUT|PATCH|GET|DELETE)\s+)?"
        r"(?P<path>/api/[A-Za-z0-9_/{}/-]+)\s*->\s*(?P<status>\d+)"
        r"(?:;\s*payload=(?P<payload>.*?))?"
        r"(?:;\s*body=(?P<body>.*))?$",
        re.IGNORECASE,
    )
    _UNITTEST_FAIL_RE = re.compile(r"FAIL:\s+(?P<name>[A-Za-z0-9_]+)\s+\(")
    _UNITTEST_ERROR_RE = re.compile(r"ERROR:\s+(?P<name>[A-Za-z0-9_]+)\s+\(")
    _ROLE_PAGE_ASSERT_RE = re.compile(
        r"No(?: declared)? pages?(?: declared)?(?: for role)? (?P<role>client|specialist|manager)",
        re.IGNORECASE,
    )
    _SQLITE_MISSING_COLUMN_RE = re.compile(
        r"table\s+(?P<table>[A-Za-z0-9_]+)\s+has no column named\s+(?P<column>[A-Za-z0-9_]+)",
        re.IGNORECASE,
    )
    _SQLITE_MISSING_TABLE_RE = re.compile(
        r"no such table:\s+(?P<table>[A-Za-z0-9_]+)",
        re.IGNORECASE,
    )
    _SHARED_STATE_UPDATE_RE = re.compile(
        r"Updated record\s+(?P<record_id>[A-Za-z0-9_-]+)\s+did not reflect\s+(?P<actor>[A-Za-z0-9_-]+)\s+changes in shared state\.\s+Payload:\s*(?P<payload>.*)$",
        re.IGNORECASE,
    )
    _POST_PERSISTENCE_RE = re.compile(
        r"POST(?:ed)?\s+(?P<path>/api/[A-Za-z0-9_/{}/-]+)?\s*(?:record|payload)?\s*(?:did not|does not|was not)\s+persist(?:ed)?(?:\.\s*Payload:\s*(?P<payload>.*))?$",
        re.IGNORECASE,
    )
    _GENERATED_JS_TEST_LOCATION_RE = re.compile(r"generated_app\.test\.mjs:(?P<line>\d+):(?P<column>\d+)")
    _STATIC_JS_SYNTAX_LOCATION_RE = re.compile(
        r"(?P<path>.*?/miniapp/(?P<relative>app/static/[^:\s]+\.js)):(?P<line>\d+)"
    )
    _FASTAPI_SESSION_RESPONSE_FIELD_RE = re.compile(
        r"Invalid args for response field!.*sqlalchemy\.orm\.session\.Session",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, validation_suite: ValidationSuite, preview_service: PreviewService) -> None:
        self.validation_suite = validation_suite
        self.preview_service = preview_service
        self._python_requirements_cache: set[str] = set()
        self._python_requirements_cache_lock = threading.Lock()

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        source_dir: Path,
        changed_files: list[str],
        preview_run_id: str | None = None,
        scope_mode: str = "full_build",
        check_profile: str = "full",
        intent: str | None = None,
        generation_mode: GenerationMode | str | None = None,
        acceptance_contract: dict[str, Any] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> CheckExecutionRecord:
        started = time.perf_counter()
        results: list[RunCheckResult] = []
        backend_dir = source_dir / "miniapp"
        focused_edit_profile = check_profile == "focused_edit"
        focused_css_only_profile = focused_edit_profile and self._css_only_app_change(changed_files)

        self._emit_check_progress(progress_callback, "schema_validators", "started", check_profile=check_profile)
        validator_started = time.perf_counter()
        if focused_css_only_profile:
            build_issues = []
            filtered_issues = []
        else:
            build_issues = self.validation_suite.validate_build(source_dir)
            filtered_issues = self._filter_build_issues(build_issues, scope_mode)
        schema_blocking_issues = self._blocking_validation_issues(filtered_issues)
        schema_result = RunCheckResult(
            name="schema_validators",
            status="skipped" if focused_css_only_profile else "failed" if schema_blocking_issues else "passed",
            details=(
                "Build validators skipped for focused CSS-only visual edit."
                if focused_css_only_profile
                else "Build validators executed against the draft workspace."
            ),
            duration_ms=int((time.perf_counter() - validator_started) * 1000),
            command="validation_suite.validate_build",
            logs=self._validation_logs(filtered_issues),
        )
        results.append(schema_result)
        self._emit_check_progress(
            progress_callback,
            "schema_validators",
            schema_result.status,
            duration_ms=schema_result.duration_ms,
            issue_count=len(schema_blocking_issues),
            check_profile=check_profile,
        )

        self._emit_check_progress(progress_callback, "connectivity_validators", "started", check_profile=check_profile)
        connectivity_started = time.perf_counter()
        connectivity_issues = [] if focused_css_only_profile else self.validation_suite.validate_connectivity(source_dir)
        connectivity_blocking_issues = self._blocking_validation_issues(connectivity_issues)
        connectivity_result = RunCheckResult(
            name="connectivity_validators",
            status="skipped" if focused_css_only_profile else "failed" if connectivity_blocking_issues else "passed",
            details=(
                "Connectivity validators skipped for focused CSS-only visual edit."
                if focused_css_only_profile
                else "Connectivity validators executed against the draft workspace."
            ),
            duration_ms=int((time.perf_counter() - connectivity_started) * 1000),
            command="validation_suite.validate_connectivity",
            logs=self._validation_logs(connectivity_issues),
        )
        results.append(connectivity_result)
        self._emit_check_progress(
            progress_callback,
            "connectivity_validators",
            connectivity_result.status,
            duration_ms=connectivity_result.duration_ms,
            issue_count=len(connectivity_blocking_issues),
            check_profile=check_profile,
        )

        self._emit_check_progress(progress_callback, "changed_files_static", "started", changed_files=changed_files, check_profile=check_profile)
        static_started = time.perf_counter()
        static_result = (
            self._focused_css_static_check(source_dir=source_dir, changed_files=changed_files)
            if focused_edit_profile and self._css_only_app_change(changed_files)
            else self._static_check(source_dir=source_dir, changed_files=changed_files)
        )
        static_result.duration_ms = int((time.perf_counter() - static_started) * 1000)
        results.append(static_result)
        self._emit_check_progress(
            progress_callback,
            "changed_files_static",
            static_result.status,
            duration_ms=static_result.duration_ms,
            changed_files=changed_files,
            check_profile=check_profile,
        )

        self._emit_check_progress(progress_callback, "platform_invariants", "started", changed_files=changed_files, check_profile=check_profile)
        canonical_started = time.perf_counter()
        platform_smoke_result = self._platform_invariants_smoke(
            source_dir=source_dir,
            changed_files=changed_files,
            scope_mode="focused_edit" if focused_edit_profile else scope_mode,
            intent=intent,
            generation_mode=generation_mode,
        )
        platform_smoke_result.duration_ms = int((time.perf_counter() - canonical_started) * 1000)
        results.append(platform_smoke_result)
        self._emit_check_progress(
            progress_callback,
            "platform_invariants",
            platform_smoke_result.status,
            duration_ms=platform_smoke_result.duration_ms,
            changed_files=changed_files,
            issue_count=len(platform_smoke_result.logs or []) if platform_smoke_result.status == "failed" else 0,
            check_profile=check_profile,
        )

        self._emit_check_progress(progress_callback, "frontend_interaction_static_smoke", "started", check_profile=check_profile)
        flow_started = time.perf_counter()
        frontend_flow_result = self._frontend_interaction_static_smoke(
            source_dir=source_dir,
            changed_files=changed_files,
            intent=intent,
            generation_mode=generation_mode,
            acceptance_contract=acceptance_contract,
            focused_css_only=focused_css_only_profile,
        )
        frontend_flow_result.duration_ms = int((time.perf_counter() - flow_started) * 1000)
        results.append(frontend_flow_result)
        self._emit_check_progress(
            progress_callback,
            "frontend_interaction_static_smoke",
            frontend_flow_result.status,
            duration_ms=frontend_flow_result.duration_ms,
            issue_count=len(frontend_flow_result.logs or []) if frontend_flow_result.status == "failed" else 0,
            check_profile=check_profile,
        )

        should_skip_preview = (
            bool(filtered_issues)
            or bool(connectivity_issues)
            or static_result.status == "failed"
            or platform_smoke_result.status == "failed"
            or frontend_flow_result.status == "failed"
        )

        must_run_generated_tests = check_profile == "fast_gate" and self._requires_generated_workflow_tests(
            intent=intent,
            acceptance_contract=acceptance_contract,
        )

        if check_profile == "focused_edit" or (check_profile == "fast_gate" and not must_run_generated_tests):
            focused_details = check_profile == "focused_edit"
            python_details = (
                "Generated Python app tests were skipped for a focused CSS-only visual edit."
                if focused_details
                else "Generated Python app tests were deferred until follow-up verification."
            )
            js_details = (
                "Generated JS app tests were skipped for a focused CSS-only visual edit."
                if focused_details
                else "Generated JS app tests were deferred until follow-up verification."
            )
            preview_details = (
                "Preview rebuild was skipped for a focused CSS-only visual edit until successful apply/final viewing."
                if focused_details
                else "Preview rebuild was deferred until follow-up verification."
            )
            results.extend(
                [
                    RunCheckResult(
                        name="generated_app_python_tests",
                        status="skipped",
                        details=python_details,
                        command=f"{sys.executable} -m unittest discover -s tests -p test_generated_app.py",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="generated_app_js_tests",
                        status="skipped",
                        details=js_details,
                        command="node --test tests/generated_app.test.mjs",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="preview_boot_smoke",
                        status="skipped",
                        details=preview_details,
                        command="preview deferred during focused edit" if focused_details else "preview deferred during fast gate",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="preview_connectivity_smoke",
                        status="skipped",
                        details=(
                            "Preview connectivity smoke was skipped for a focused CSS-only visual edit."
                            if focused_details
                            else "Preview connectivity smoke was deferred until follow-up verification."
                        ),
                        command="preview deferred during focused edit" if focused_details else "preview deferred during fast gate",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="browser_flow_smoke",
                        status="skipped",
                        details=(
                            "Browser flow smoke was skipped for a focused CSS-only visual edit."
                            if focused_details
                            else "Browser flow smoke was deferred until follow-up verification."
                        ),
                        command="browser/preview flow deferred during focused edit" if focused_details else "browser/preview flow deferred during fast gate",
                        logs=[],
                    ),
                ]
            )
            completed_at = utc_now()
            return CheckExecutionRecord(
                workspace_id=workspace_id,
                run_id=run_id,
                changed_files=changed_files,
                results=results,
                started_at=utc_now(),
                completed_at=completed_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        require_generated_tests = scope_mode == "agentic"
        skip_preview_only = check_profile == "fast_gate" and must_run_generated_tests

        def _run_python_tests() -> RunCheckResult:
            python_tests_started = time.perf_counter()
            python_tests_result = self._run_python_app_tests(backend_dir, require_present=require_generated_tests)
            python_tests_result.duration_ms = int((time.perf_counter() - python_tests_started) * 1000)
            return python_tests_result

        def _run_js_tests() -> RunCheckResult:
            js_tests_started = time.perf_counter()
            js_tests_result = self._run_js_app_tests(backend_dir, require_present=require_generated_tests)
            js_tests_result.duration_ms = int((time.perf_counter() - js_tests_started) * 1000)
            return js_tests_result

        def _run_preview_checks() -> tuple[RunCheckResult, RunCheckResult, RunCheckResult]:
            preview_started = time.perf_counter()
            if should_skip_preview or skip_preview_only:
                duration_ms = int((time.perf_counter() - preview_started) * 1000)
                reason = (
                    "Preview smoke deferred during fast gate after generated workflow tests."
                    if skip_preview_only
                    else "Preview smoke skipped because validator or build checks already failed."
                )
                preview_boot_result = RunCheckResult(
                    name="preview_boot_smoke",
                    status="skipped",
                    details=reason,
                    duration_ms=duration_ms,
                    command="preview smoke (current session)",
                    logs=[],
                )
                connectivity_result = RunCheckResult(
                    name="preview_connectivity_smoke",
                    status="skipped",
                    details=reason,
                    duration_ms=duration_ms,
                    command="preview route smoke (current session)",
                    logs=[],
                )
                browser_result = RunCheckResult(
                    name="browser_flow_smoke",
                    status="skipped",
                    details=reason,
                    duration_ms=duration_ms,
                    command="browser/preview flow smoke",
                    logs=[],
                )
                return preview_boot_result, connectivity_result, browser_result
            preview = self.preview_service.get(workspace_id)
            preview_boot_result = RunCheckResult(
                name="preview_boot_smoke",
                status="skipped" if preview.status in {"stopped", "error"} else "passed",
                details="Draft preview smoke recorded using the current preview session.",
                command="preview smoke (current session)",
                logs=preview.logs[-12:],
            )
            connectivity_result = self._preview_connectivity_smoke(
                source_dir=source_dir,
                preview=preview,
                preview_run_id=preview_run_id,
            )
            browser_result = self._browser_flow_smoke(
                source_dir=source_dir,
                preview=preview,
                preview_run_id=preview_run_id,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
            )
            duration_ms = int((time.perf_counter() - preview_started) * 1000)
            preview_boot_result.duration_ms = duration_ms
            connectivity_result.duration_ms = duration_ms
            browser_result.duration_ms = duration_ms
            return preview_boot_result, connectivity_result, browser_result

        for started_name in ("generated_app_python_tests", "generated_app_js_tests", "preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke"):
            self._emit_check_progress(progress_callback, started_name, "started", check_profile=check_profile)

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="check-runner") as executor:
            python_future = executor.submit(_run_python_tests)
            js_future = executor.submit(_run_js_tests)
            preview_future = executor.submit(_run_preview_checks)
            python_tests_result = python_future.result()
            js_tests_result = js_future.result()
            preview_boot_result, connectivity_result, browser_flow_result = preview_future.result()

        results.append(python_tests_result)
        results.append(js_tests_result)
        results.append(preview_boot_result)
        results.append(connectivity_result)
        results.append(browser_flow_result)
        for result in (python_tests_result, js_tests_result, preview_boot_result, connectivity_result, browser_flow_result):
            self._emit_check_progress(
                progress_callback,
                result.name,
                result.status,
                duration_ms=result.duration_ms,
                check_profile=check_profile,
            )

        completed_at = utc_now()
        return CheckExecutionRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            changed_files=changed_files,
            results=results,
            started_at=utc_now(),
            completed_at=completed_at,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _emit_check_progress(
        callback: Callable[[str, dict[str, Any]], None] | None,
        check_step: str,
        status: str,
        **details: Any,
    ) -> None:
        if callback is None:
            return
        callback(check_step, {"check_step": check_step, "check_status": status, **details})

    @staticmethod
    def _requires_generated_workflow_tests(
        *,
        intent: str | None,
        acceptance_contract: dict[str, Any] | None,
    ) -> bool:
        intent_value = str(intent or "").strip().lower()
        return intent_value == "create" or bool((acceptance_contract or {}).get("required"))

    def _frontend_interaction_static_smoke(
        self,
        *,
        source_dir: Path,
        changed_files: list[str],
        intent: str | None,
        generation_mode: GenerationMode | str | None,
        acceptance_contract: dict[str, Any] | None,
        focused_css_only: bool = False,
    ) -> RunCheckResult:
        del changed_files
        if focused_css_only:
            return RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="skipped",
                details="Frontend interaction smoke skipped for focused CSS-only visual edit.",
                command="frontend interaction static smoke",
                logs=[],
            )
        contract = dict(acceptance_contract or {})
        if not contract:
            contract = build_acceptance_contract(
                prompt="",
                intent=intent,
                generation_mode=generation_mode,
                focused_edit_kind="",
            )
        if not contract.get("required"):
            return RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="skipped",
                details="Frontend interaction smoke skipped because no workflow acceptance contract is required.",
                command="frontend interaction static smoke",
                logs=[],
                diagnostics={"acceptance_contract_required": False},
            )

        issues = self._frontend_interaction_contract_issues(source_dir=source_dir, contract=contract)
        blocking = self._blocking_validation_issues(issues)
        return RunCheckResult(
            name="frontend_interaction_static_smoke",
            status="failed" if blocking else "passed",
            details="Frontend interaction smoke checked required buttons/forms, JavaScript handlers, API calls, and generated test coverage.",
            command="frontend interaction static smoke",
            logs=self._validation_logs(issues) if issues else ["Frontend interaction flow wiring passed."],
            diagnostics={
                "acceptance_contract_required": True,
                "flow_ids": [flow.get("id") for flow in contract.get("flows", []) if isinstance(flow, dict)],
                "features": dict(contract.get("features") or {}),
            },
        )

    @classmethod
    def _frontend_interaction_contract_issues(cls, *, source_dir: Path, contract: dict[str, Any]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        static_root = source_dir / "miniapp/app/static"
        role_text = {
            role: cls._read_role_surface_text(static_root / role)
            for role in ROLE_ORDER
        }
        backend_text = cls._read_backend_routes_text(source_dir)
        tests_text = cls._read_generated_tests_text(source_dir)
        all_frontend = "\n".join(role_text.values())
        if not cls._has_frontend_post(all_frontend):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_missing_frontend_post",
                    message="Acceptance workflow requires at least one frontend POST submit/fetch path, but no POST-capable frontend action was found.",
                    severity="high",
                    location="miniapp/app/static",
                    blocking=True,
                )
            )
        if not cls._has_status_update_action(role_text.get("specialist", "") + "\n" + role_text.get("manager", "")):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_missing_status_update",
                    message="Acceptance workflow requires specialist/manager processing actions, but no PATCH/PUT/status update action was found.",
                    severity="high",
                    location="miniapp/app/static",
                    blocking=True,
                )
            )
        if not cls._tests_cover_workflow_contract(tests_text, contract):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_tests_missing_contract",
                    message="Generated tests do not cover the workflow acceptance contract. Add Python/JS tests for the required form/API/status/cross-role flow.",
                    severity="high",
                    location="miniapp/tests",
                    blocking=True,
                )
            )
        return cls._dedupe_validation_issues(issues)

    @staticmethod
    def _read_role_surface_text(role_dir: Path) -> str:
        if not role_dir.exists():
            return ""
        chunks: list[str] = []
        for path in sorted(role_dir.rglob("*")):
            if path.suffix.lower() not in {".html", ".js"}:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        return "\n".join(chunks)

    @staticmethod
    def _read_backend_routes_text(source_dir: Path) -> str:
        routes_dir = source_dir / "miniapp/app/routes"
        chunks: list[str] = []
        for path in sorted(routes_dir.glob("*.py")) if routes_dir.exists() else []:
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        for path in (source_dir / "miniapp/app/main.py", source_dir / "miniapp/app/db.py", source_dir / "miniapp/app/schemas.py"):
            if not path.exists():
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        return "\n".join(chunks)

    @staticmethod
    def _read_generated_tests_text(source_dir: Path) -> str:
        chunks: list[str] = []
        tests_dir = source_dir / "miniapp/tests"
        for path in (tests_dir / "test_generated_app.py", tests_dir / "generated_app.test.mjs"):
            if not path.exists():
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        return "\n".join(chunks)

    @staticmethod
    def _has_frontend_post(text: str) -> bool:
        return bool(re.search(r"method\s*:\s*['\"]post['\"]", text, flags=re.IGNORECASE) or re.search(r"\bfetch\s*\(", text) and "post" in text.lower())

    @staticmethod
    def _has_status_update_action(text: str) -> bool:
        lowered = str(text or "").lower()
        return bool(
            re.search(r"method\s*:\s*['\"](?:patch|put)['\"]", text, flags=re.IGNORECASE)
            or ("/api/" in lowered and any(marker in lowered for marker in ("status", "confirm", "complete", "review", "approve", "статус", "подтверд", "готов", "провер")))
        )

    @staticmethod
    def _tests_cover_workflow_contract(tests_text: str, contract: dict[str, Any]) -> bool:
        lowered = str(tests_text or "").lower()
        if "testclient" not in lowered and "node:test" not in lowered:
            return False
        required_terms = ["post", "get"]
        if (contract.get("features") or {}).get("status_update"):
            required_terms.append("patch")
        return all(term in lowered for term in required_terms)

    @staticmethod
    def _backend_has_route(text: str, *, method: str, path: str) -> bool:
        method_name = method.strip().lower()
        escaped_path = re.escape(path.rstrip("/"))
        if bool(
            re.search(rf"@router\.{method_name}\(\s*['\"]{escaped_path}(?:/[^'\"]*)?['\"]", text, flags=re.IGNORECASE)
            or re.search(rf"\b{method_name}\s*=\s*['\"]{escaped_path}", text, flags=re.IGNORECASE)
        ):
            return True
        for prefix in re.findall(r"APIRouter\([^)]*prefix\s*=\s*['\"](?P<prefix>/[^'\"]*)['\"]", text, flags=re.IGNORECASE | re.DOTALL):
            prefix_value = str(prefix or "").rstrip("/")
            if not prefix_value or not path.startswith(prefix_value + "/"):
                continue
            relative_path = "/" + path[len(prefix_value):].strip("/")
            escaped_relative = re.escape(relative_path.rstrip("/"))
            if re.search(rf"@router\.{method_name}\(\s*['\"]{escaped_relative}(?:/[^'\"]*)?['\"]", text, flags=re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _text_has_fetch(text: str, path: str, *, method: str | None = None) -> bool:
        lowered = str(text or "").lower()
        path_lower = path.lower()
        if path_lower not in lowered:
            path_parts = [part for part in path_lower.strip("/").split("/") if part]
            has_composed_path = (
                len(path_parts) >= 2
                and path_parts[0] == "api"
                and "/api" in lowered
                and path_parts[-1] in lowered
                and ("fetch(" in lowered or "endpoint" in lowered or "api_base" in lowered)
            )
            if not has_composed_path:
                return False
        if method is None or method.upper() == "GET":
            return True
        method_name = method.lower()
        if re.search(rf"method\s*:\s*['\"]{method_name}['\"]", lowered, flags=re.IGNORECASE):
            return True
        if method_name == "GET":
            return True
        if path_lower not in lowered:
            return False
        return False

    def _browser_flow_smoke(
        self,
        *,
        source_dir: Path,
        preview: Any,
        preview_run_id: str | None,
        generation_mode: GenerationMode | str | None,
        acceptance_contract: dict[str, Any] | None,
    ) -> RunCheckResult:
        del source_dir
        mode = str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()
        contract = dict(acceptance_contract or {})
        if mode not in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value} or not contract.get("required"):
            return RunCheckResult(
                name="browser_flow_smoke",
                status="skipped",
                details="Browser flow smoke skipped because this run does not require Balanced/Quality workflow preview verification.",
                command="browser/preview flow smoke",
                logs=[],
            )
        if preview_run_id is not None:
            return RunCheckResult(
                name="browser_flow_smoke",
                status="skipped",
                details="Browser flow smoke skipped because preview is source-only and cannot validate the draft state.",
                command="browser/preview flow smoke",
                logs=[],
            )
        if getattr(preview, "status", None) != "running" or not getattr(preview, "url", None):
            return RunCheckResult(
                name="browser_flow_smoke",
                status="failed",
                details="Browser flow smoke requires a running preview for Balanced/Quality workflow verification.",
                command="browser/preview flow smoke",
                logs=[getattr(preview, "last_error", None) or "Preview is not running."],
            )
        return RunCheckResult(
            name="browser_flow_smoke",
            status="passed",
            details="Running preview is available for manual/browser flow verification; static interaction and generated tests covered the workflow contract.",
            command="browser/preview flow smoke",
            logs=[f"Preview available at {getattr(preview, 'url', '')}"],
            diagnostics={"flow_ids": [flow.get("id") for flow in contract.get("flows", []) if isinstance(flow, dict)]},
        )

    @staticmethod
    def failing_issues(results: list[RunCheckResult]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for result in results:
            if result.status != "failed":
                continue
            location = result.name
            code = f"check.{result.name}"
            message = result.details or f"{result.name} failed."
            if result.name in {"schema_validators", "connectivity_validators"}:
                parsed = CheckRunner._validation_issues_from_logs(result.logs, default_code=code, default_location=location)
                if parsed:
                    issues.extend(parsed)
                    continue
                message = next((line for line in result.logs if line.strip()), message)
            if result.name == "changed_files_static":
                message = next((line for line in result.logs if line.strip()), message)
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                session_dependency = diagnostics.get("fastapi_session_dependency_error") if isinstance(diagnostics, dict) else None
                if isinstance(session_dependency, dict):
                    message = str(session_dependency.get("expected_fix") or "").strip() or message
            if result.name == "platform_invariants":
                location = "miniapp/app"
                code = "platform.invariants"
                message = next((line for line in result.logs if line.strip()), message)
            if result.name == "frontend_interaction_static_smoke":
                location = "miniapp/app/static"
                code = "platform.frontend_interaction_static"
                message = next((line for line in result.logs if line.strip()), message)
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}:
                location = "tests"
                code = "tests.python_generated_app" if result.name == "generated_app_python_tests" else "tests.js_generated_app"
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                api_failure = diagnostics.get("api_failure") if isinstance(diagnostics, dict) else None
                shared_state_failure = diagnostics.get("shared_state_update_failure") if isinstance(diagnostics, dict) else None
                post_persistence_failure = diagnostics.get("post_persistence_failure") if isinstance(diagnostics, dict) else None
                js_path_root = diagnostics.get("js_test_path_root") if isinstance(diagnostics, dict) else None
                js_url_path_api = diagnostics.get("js_test_url_path_api") if isinstance(diagnostics, dict) else None
                server_html_assertion = diagnostics.get("server_rendered_html_assertion") if isinstance(diagnostics, dict) else None
                static_html_assertion = diagnostics.get("static_html_assertion") if isinstance(diagnostics, dict) else None
                assertion_source = diagnostics.get("assertion_source") if isinstance(diagnostics, dict) else None
                if isinstance(js_path_root, dict):
                    expected_root = str(js_path_root.get("expected_root") or "").strip()
                    message = expected_root or "Generated JS tests used an invalid miniapp path root."
                elif isinstance(js_url_path_api, dict):
                    expected_path_api = str(js_url_path_api.get("expected_path_api") or "").strip()
                    message = expected_path_api or "Generated JS tests passed a URL object to a path/fs API."
                elif isinstance(server_html_assertion, dict):
                    message = str(server_html_assertion.get("expected_scope") or "").strip() or "Generated Python tests asserted JS-rendered text in server HTML."
                elif isinstance(static_html_assertion, dict):
                    message = str(static_html_assertion.get("expected_scope") or "").strip() or "Generated JS tests asserted dynamic text only in HTML."
                elif isinstance(assertion_source, dict):
                    line_no = assertion_source.get("line")
                    source_text = str(assertion_source.get("source") or "").strip()
                    message = f"Generated JS test failed at generated_app.test.mjs:{line_no}: {source_text}".strip()
                elif isinstance(shared_state_failure, dict):
                    actor = str(shared_state_failure.get("actor") or "").strip()
                    resource_slug = str(shared_state_failure.get("resource_slug") or "").strip()
                    resource_label = f"/api/{resource_slug}" if resource_slug else "shared record API"
                    payload_excerpt = str(shared_state_failure.get("payload_excerpt") or "").strip()
                    message = (
                        f"Generated app shared-state update failure: {resource_label} did not persist "
                        f"{actor or 'role'} changes. Payload: {payload_excerpt}"
                    ).strip()
                elif isinstance(post_persistence_failure, dict):
                    path = str(post_persistence_failure.get("path") or "").strip()
                    resource_slug = str(post_persistence_failure.get("resource_slug") or "").strip()
                    resource_label = path or (f"/api/{resource_slug}" if resource_slug else "created record API")
                    message = f"Generated app POST persistence failure: {resource_label} did not persist the created record."
                elif isinstance(api_failure, dict):
                    method = str(api_failure.get("method") or "").strip().upper()
                    path = str(api_failure.get("path") or "").strip()
                    status = api_failure.get("status_code")
                    route_label = " ".join(part for part in [method, path] if part).strip()
                    message = f"Generated app API failure: {route_label} -> {status}".strip()
                elif isinstance(diagnostics.get("missing_role_pages"), list) and diagnostics.get("missing_role_pages"):
                    roles = ", ".join(str(role).strip() for role in diagnostics.get("missing_role_pages") if str(role).strip())
                    message = f"Generated app role pages missing for: {roles}".strip()
                elif isinstance(diagnostics.get("sqlite_missing_column"), dict):
                    sqlite_issue = diagnostics.get("sqlite_missing_column") or {}
                    table_name = str(sqlite_issue.get("table") or "").strip()
                    column_name = str(sqlite_issue.get("column") or "").strip()
                    if table_name and column_name:
                        message = f"Generated app DB schema mismatch: table {table_name} is missing column {column_name}".strip()
                elif isinstance(diagnostics.get("sqlite_missing_table"), dict):
                    sqlite_issue = diagnostics.get("sqlite_missing_table") or {}
                    table_name = str(sqlite_issue.get("table") or "").strip()
                    if table_name:
                        message = f"Generated app DB schema missing table {table_name}; create tables before TestClient requests run.".strip()
                else:
                    message = next((line for line in reversed(result.logs) if line.strip()), message)
            if result.name in {"preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke"}:
                location = "preview"
                code = (
                    "connectivity.preview_route_unreachable"
                    if result.name == "preview_connectivity_smoke"
                    else "preview.workflow_flow_failed" if result.name == "browser_flow_smoke"
                    else "preview.rebuild_failed"
                )
                message = next((line for line in reversed(result.logs) if line.strip()), message)
            issues.append(
                ValidationIssue(
                    code=code,
                    message=message,
                    severity="high",
                    location=location,
                    blocking=True,
                )
            )
        return issues

    @staticmethod
    def classify_failure(results: list[RunCheckResult]) -> str | None:
        if CheckRunner.has_tooling_failure(results):
            return "tooling/runtime_misconfiguration"
        failed_names = {result.name for result in results if result.status == "failed"}
        if "schema_validators" in failed_names or "connectivity_validators" in failed_names:
            return "validator/domain_constraint"
        if "changed_files_static" in failed_names:
            return "syntax/build"
        if "platform_invariants" in failed_names or "prompt_alignment_smoke" in failed_names or "frontend_interaction_static_smoke" in failed_names:
            return "validator/domain_constraint"
        if "generated_app_python_tests" in failed_names or "generated_app_js_tests" in failed_names:
            return "app/runtime_test"
        if "preview_boot_smoke" in failed_names or "preview_connectivity_smoke" in failed_names or "browser_flow_smoke" in failed_names:
            return "runtime_preview_boot"
        return None

    @staticmethod
    def has_tooling_failure(results: list[RunCheckResult]) -> bool:
        markers = (
            "npm is not available in the miniapp runtime",
            "frontend build tooling is unavailable",
            "node.js/npm is missing",
            "node.js is missing for generated app tests",
        )
        for result in results:
            haystack = "\n".join([result.details or "", *result.logs]).lower()
            if any(marker in haystack for marker in markers):
                return True
        return False

    @staticmethod
    def _validation_logs(issues: list[ValidationIssue]) -> list[str]:
        return [json.dumps(issue.model_dump(mode="json"), ensure_ascii=False) for issue in issues]

    @staticmethod
    def _blocking_validation_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        return [issue for issue in issues if getattr(issue, "blocking", True)]

    @staticmethod
    def _validation_issues_from_logs(
        logs: list[str],
        *,
        default_code: str,
        default_location: str,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for line in logs:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                issues.append(ValidationIssue.model_validate(payload))
            except Exception:
                continue
        if issues:
            return issues
        if not logs:
            return []
        return [
            ValidationIssue(
                code=default_code,
                message=next((line for line in logs if line.strip()), "Validation failed."),
                severity="high",
                location=default_location,
                blocking=True,
            )
        ]

    def _preview_connectivity_smoke(self, *, source_dir: Path, preview, preview_run_id: str | None) -> RunCheckResult:
        if preview.status != "running" or not preview.url:
            return RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because no running preview session is available.",
                command="preview route smoke (current session)",
                logs=[],
            )
        if preview_run_id is not None:
            return RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because preview is source-only and does not validate draft state.",
                command="preview route smoke (current session)",
                logs=[],
            )
        routes = self._root_preview_routes(source_dir)
        if not routes:
            return RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because no preview routes are available.",
                command="preview route smoke (current session)",
                logs=[],
            )
        failures: list[str] = []
        logs: list[str] = []
        for route in routes:
            target = urljoin(preview.url.rstrip("/") + "/", route.lstrip("/"))
            final_failure: str | None = None
            for attempt in range(1, 4):
                try:
                    request = Request(target, headers={"User-Agent": "connectivity-smoke"})
                    with urlopen(request, timeout=2.0) as response:
                        status_code = response.status if hasattr(response, "status") else response.getcode()
                        body = response.read().decode("utf-8", errors="ignore")
                    if status_code >= 400:
                        final_failure = f"{route} returned HTTP {status_code}."
                    else:
                        normalized_body = body.lower()
                        if len(normalized_body.strip()) < 40 or "not found" in normalized_body or "<title>404" in normalized_body:
                            final_failure = f"{route} returned unusable preview content."
                        else:
                            suffix = f" after {attempt} attempt(s)." if attempt > 1 else "."
                            logs.append(f"{route} returned usable preview content{suffix}")
                            final_failure = None
                            break
                except (TimeoutError, URLError, OSError) as exc:
                    final_failure = f"{route} could not be opened in preview: {exc}"
                if final_failure is None:
                    break
                if attempt < 3:
                    time.sleep(0.35 * attempt)
            if final_failure:
                failures.append(final_failure)
        return RunCheckResult(
            name="preview_connectivity_smoke",
            status="failed" if failures else "passed",
            details="Preview route smoke checked generated role routes against the running preview session.",
            command="preview route smoke (current session)",
            logs=failures or logs,
            diagnostics={"routes_checked": routes},
        )

    @staticmethod
    def _root_preview_routes(source_dir: Path) -> list[str]:
        routes: list[str] = []
        pages_by_role = CheckRunner._routeable_role_pages(source_dir)
        for role in ROLE_ORDER:
            role_routes = CheckRunner._unique_role_routes(pages_by_role.get(role, []))
            root_route = f"/{role}"
            if root_route in role_routes:
                routes.append(root_route)
            routes.extend(route for route in role_routes if route != root_route)
        return list(dict.fromkeys(route for route in routes if route))[:18]

    def _platform_invariants_smoke(
        self,
        *,
        source_dir: Path,
        changed_files: list[str],
        scope_mode: str,
        intent: str | None = None,
        generation_mode: GenerationMode | str | None = None,
    ) -> RunCheckResult:
        relevant_changed = [
            str(path)
            for path in changed_files
            if isinstance(path, str)
            and path.startswith("miniapp/app/")
        ]
        agentic_scope = scope_mode == "agentic"
        focused_scope = scope_mode == "focused_edit"
        css_only_focused_edit = focused_scope and self._css_only_app_change(relevant_changed)
        if not relevant_changed and not agentic_scope and not focused_scope:
            return RunCheckResult(
                name="platform_invariants",
                status="skipped",
                details="Platform invariant smoke skipped because no app files changed.",
                command="platform invariant smoke",
                logs=[],
            )
        issues: list[ValidationIssue] = []
        role_coverage: dict[str, object] = {}
        neutral_template_findings: list[dict[str, str]] = []
        generated_tests: dict[str, object] = {}
        api_contract: dict[str, object] = {}
        preloaded_data_findings: list[dict[str, str]] = []
        if any(
            path.startswith("miniapp/app/routes/")
            or path in {"miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py"}
            or path.startswith("miniapp/app/generated/")
            for path in relevant_changed
        ):
            issues.extend(GenerationPreflightValidation.preflight_profile_schema_issues(source_dir))
            issues.extend(GenerationPreflightValidation.preflight_route_schema_issues(source_dir))
        if agentic_scope:
            role_issues, role_coverage, neutral_template_findings = self._role_surface_issues(
                source_dir,
                generation_mode=generation_mode,
            )
            issues.extend(role_issues)
            tests_issues, generated_tests = self._generated_tests_presence_issues(source_dir)
            issues.extend(tests_issues)
        elif css_only_focused_edit:
            role_coverage = {"status": "skipped", "reason": "focused_css_only_edit"}
            generated_tests = {"status": "skipped", "reason": "focused_css_only_edit"}
        issues.extend(self._shell_safe_spacing_issues(source_dir))
        if agentic_scope and str(intent or "").strip().lower() == "create":
            api_issues, api_contract = self._create_api_contract_issues(source_dir)
            issues.extend(api_issues)
            data_issues, preloaded_data_findings = self._preloaded_business_data_issues(source_dir)
            issues.extend(data_issues)
        if not css_only_focused_edit:
            dom_contract_files = list(relevant_changed)
            if agentic_scope and str(intent or "").strip().lower() == "create":
                dom_contract_files.extend(self._role_script_paths(source_dir))
            issues.extend(self._dom_contract_issues(source_dir=source_dir, changed_files=dom_contract_files))
        issues = self._dedupe_validation_issues(issues)
        blocking_issues = self._blocking_validation_issues(issues)
        return RunCheckResult(
            name="platform_invariants",
            status="failed" if blocking_issues else "passed",
            details="Platform invariant smoke validated lightweight route/schema and DOM invariants for the edited surface.",
            command="platform invariant smoke",
            logs=self._validation_logs(issues) if issues else ["Platform invariant smoke passed."],
            diagnostics={
                "role_coverage": role_coverage,
                "multipage_coverage": self._multipage_coverage_from_roles(role_coverage),
                "neutral_template_findings": neutral_template_findings,
                "generated_tests": generated_tests,
                "api_contract": api_contract,
                "preloaded_data_findings": preloaded_data_findings,
            },
        )

    @classmethod
    def _role_surface_issues(
        cls,
        source_dir: Path,
        *,
        generation_mode: GenerationMode | str | None = None,
    ) -> tuple[list[ValidationIssue], dict[str, object], list[dict[str, str]]]:
        issues: list[ValidationIssue] = []
        coverage: dict[str, object] = {}
        neutral_findings: list[dict[str, str]] = []
        role_tokens: dict[str, set[str]] = {}
        role_surface_text: dict[str, str] = {}
        route_pages = cls._routeable_role_pages(source_dir)
        min_role_route_pages = cls._min_role_route_pages(generation_mode)

        for role in ROLE_ORDER:
            role_dir = source_dir / "miniapp" / "app" / "static" / role
            expected_files = {
                "html": role_dir / "index.html",
                "js": role_dir / "app.js",
                "css": role_dir / "styles.css",
            }
            missing = [path.relative_to(source_dir).as_posix() for path in expected_files.values() if not path.exists()]
            texts: list[str] = []
            for path in expected_files.values():
                if not path.exists():
                    continue
                try:
                    texts.append(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
            for page in route_pages.get(role, []):
                page_path_raw = str(page.get("file_path") or "")
                page_path = source_dir / page_path_raw
                if page_path in expected_files.values() or not page_path.exists():
                    continue
                try:
                    texts.append(page_path.read_text(encoding="utf-8"))
                except OSError:
                    continue
            combined = "\n".join(texts)
            normalized = combined.lower()
            markers = [marker for marker in NEUTRAL_TEMPLATE_MARKERS if marker in normalized]
            role_tokens[role] = cls._domain_tokens(combined)
            role_routes = cls._unique_role_routes(route_pages.get(role, []))
            secondary_routes = [route for route in role_routes if route != f"/{role}"]
            if missing:
                coverage[role] = {"status": "missing", "missing_files": missing}
                issues.append(
                    ValidationIssue(
                        code="platform.missing_role_surface",
                        message=f"{role} role surface is incomplete: missing {', '.join(missing)}.",
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            if markers:
                finding = {
                    "role": role,
                    "file_path": f"miniapp/app/static/{role}",
                    "markers": ", ".join(markers[:4]),
                }
                neutral_findings.append(finding)
                coverage[role] = {
                    "status": "neutral_template",
                    "markers": markers[:4],
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.neutral_role_template",
                        message=f"{role} role still contains neutral starter/template text: {', '.join(markers[:4])}.",
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            css_text = ""
            try:
                css_text = expected_files["css"].read_text(encoding="utf-8")
            except OSError:
                css_text = ""
            css_marker = cls._css_placeholder_marker(css_text)
            if css_marker:
                coverage[role] = {
                    "status": "placeholder_css",
                    "marker": css_marker,
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.placeholder_role_css",
                        message=f"{role} role CSS is still a placeholder. Generate a real static/{role}/styles.css file.",
                        severity="high",
                        location=f"miniapp/app/static/{role}/styles.css",
                        blocking=True,
                    )
                )
                continue
            shell_spacing_issue = cls._role_css_shell_spacing_issue(role, css_text)
            if shell_spacing_issue is not None:
                coverage[role] = {
                    "status": "unsafe_shell_spacing",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(shell_spacing_issue)
                continue
            design_depth_issue = cls._role_design_depth_issue(role, css_text, combined, generation_mode)
            if design_depth_issue is not None:
                coverage[role] = {
                    "status": "insufficient_mode_design",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(design_depth_issue)
                continue
            cross_role_links = cls._cross_role_links(role, combined)
            if cross_role_links:
                coverage[role] = {
                    "status": "cross_role_navigation",
                    "links": cross_role_links,
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.cross_role_navigation",
                        message=(
                            f"{role} role links to other role apps: {', '.join(cross_role_links[:4])}. "
                            "Role apps must be isolated; only the platform shell should switch roles."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            technical_copy = cls._technical_role_copy_markers(combined)
            if technical_copy:
                coverage[role] = {
                    "status": "technical_role_copy",
                    "markers": technical_copy,
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.technical_role_copy",
                        message=(
                            f"{role} role contains technical/generated copy: {', '.join(technical_copy[:4])}. "
                            "Use polished user-facing text in the user's language."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            if len(role_routes) < min_role_route_pages or len(secondary_routes) < min_role_route_pages - 1:
                coverage[role] = {
                    "status": "single_page",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                    "required_route_count": min_role_route_pages,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.single_page_role_surface",
                        message=(
                            f"{role} role is too single-page. Generate at least {min_role_route_pages} routeable pages "
                            f"for this role: /{role} plus at least {min_role_route_pages - 1} domain-specific child pages."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            action_signals = cls._role_action_signals(role, combined)
            if not action_signals:
                coverage[role] = {
                    "status": "missing_role_actions",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "required_route_count": min_role_route_pages,
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.missing_role_workflow_actions",
                        message=(
                            f"{role} role lacks its own workflow actions. Client must create records, "
                            "specialist must process/update work, and manager must expose dashboard/oversight actions."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            role_surface_text[role] = combined
            coverage[role] = {
                "status": "present",
                "files": [path.relative_to(source_dir).as_posix() for path in expected_files.values()],
                "route_count": len(role_routes),
                "secondary_route_count": len(secondary_routes),
                "required_route_count": min_role_route_pages,
                "routes": role_routes,
                "domain_token_count": len(role_tokens[role]),
                "action_signals": action_signals,
            }

        present_roles = [
            role
            for role, payload in coverage.items()
            if isinstance(payload, dict) and payload.get("status") == "present"
        ]
        if len(present_roles) == len(ROLE_ORDER):
            shared_tokens = set.intersection(*(role_tokens[role] for role in ROLE_ORDER))
            if not shared_tokens:
                issues.append(
                    ValidationIssue(
                        code="platform.disconnected_role_surfaces",
                        message="Client, specialist, and manager surfaces do not share visible prompt-derived content. Use one connected business context across all roles.",
                        severity="high",
                        location="miniapp/app/static",
                        blocking=True,
                    )
                )
            if cls._role_surfaces_too_similar(role_surface_text):
                issues.append(
                    ValidationIssue(
                        code="platform.identical_role_surfaces",
                        message="Client, specialist, and manager surfaces are too similar. Generate three separate role apps with different workflows, actions, and styling.",
                        severity="high",
                        location="miniapp/app/static",
                        blocking=True,
                    )
                )
            coverage["shared_domain_tokens"] = sorted(shared_tokens)[:12]
        return issues, coverage, neutral_findings

    @staticmethod
    def _min_role_route_pages(generation_mode: GenerationMode | str | None) -> int:
        value = str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()
        if value == GenerationMode.QUALITY.value:
            return 4
        return 2 if value == GenerationMode.FAST.value else MIN_ROLE_ROUTE_PAGES

    @staticmethod
    def _role_design_depth_issue(role: str, css_text: str, combined: str, generation_mode: GenerationMode | str | None) -> ValidationIssue | None:
        value = str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()
        if value not in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
            return None
        css = str(css_text or "").lower()
        surface = f"{css}\n{str(combined or '').lower()}"
        css_rule_count = len(re.findall(r"[.#]?[a-z][a-z0-9_-]*\s*\{", css))
        state_tokens = {
            "badge",
            "button",
            "card",
            "dashboard",
            "empty",
            "error",
            "form",
            "grid",
            "input",
            "list",
            "loading",
            "metric",
            "success",
            "status",
        }
        token_hits = {token for token in state_tokens if token in surface}
        min_rules = 12 if value == GenerationMode.QUALITY.value else 8
        min_hits = 7 if value == GenerationMode.QUALITY.value else 5
        quality_structure_ok = value != GenerationMode.QUALITY.value or ("@media" in css and ("focus-visible" in css or ":focus" in css))
        rich_quality_css = value == GenerationMode.QUALITY.value and css_rule_count >= min_rules + 6 and len(token_hits) >= min_hits + 1
        if css_rule_count >= min_rules and len(token_hits) >= min_hits and (quality_structure_ok or rich_quality_css):
            return None
        return ValidationIssue(
            code="platform.insufficient_mode_design_depth",
            message=(
                f"{role} role design is too shallow for {value} mode. "
                f"Expected at least {min_rules} real CSS rules, richer role state classes, "
                "and for quality mode responsive/focus styling."
            ),
            severity="high",
            location=f"miniapp/app/static/{role}/styles.css",
            blocking=True,
        )

    @staticmethod
    def _css_placeholder_marker(content: str) -> str | None:
        lowered = str(content or "").lower()
        stripped = re.sub(r"/\*|\*/|\s+", " ", lowered).strip()
        if not stripped:
            return "empty css"
        for marker in CSS_PLACEHOLDER_MARKERS:
            if marker in lowered:
                return marker
        meaningful_tokens = re.findall(r"[.#]?[A-Za-z][A-Za-z0-9_-]*\s*\{", content or "")
        return None if len(meaningful_tokens) >= 3 else "insufficient css rules"

    @classmethod
    def _shell_safe_spacing_issues(cls, source_dir: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        base_path = source_dir / "miniapp/app/static/shared/base.css"
        if base_path.exists():
            try:
                base_css = base_path.read_text(encoding="utf-8")
            except OSError:
                base_css = ""
            normalized_base = re.sub(r"\s+", "", base_css.lower())
            expected_safe_top = "padding-top:max(76px,calc(var(--telegram-top-safe-offset)+12px))"
            if (
                ".page-shell" not in base_css
                or "padding-top" not in normalized_base
                or not any(marker.lower().replace(" ", "") in normalized_base for marker in PAGE_SHELL_SAFE_TOP_MARKERS)
                or expected_safe_top not in normalized_base
                or "!important" not in normalized_base
            ):
                issues.append(
                    ValidationIssue(
                        code="platform.shell_safe_top_spacing_missing",
                        message=(
                            "Shared shell CSS must keep the Telegram top safe spacing on .page-shell. "
                            "Use padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px)) !important."
                        ),
                        severity="high",
                        location="miniapp/app/static/shared/base.css",
                        blocking=True,
                    )
                )
        for role in ROLE_ORDER:
            css_path = source_dir / "miniapp/app/static" / role / "styles.css"
            if not css_path.exists():
                continue
            try:
                css_text = css_path.read_text(encoding="utf-8")
            except OSError:
                continue
            issue = cls._role_css_shell_spacing_issue(role, css_text)
            if issue is not None:
                issues.append(issue)
        return issues

    @classmethod
    def _role_css_shell_spacing_issue(cls, role: str, content: str) -> ValidationIssue | None:
        for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", str(content or ""), flags=re.MULTILINE):
            selectors = match.group("selectors")
            if not cls._css_selectors_target_page_shell(selectors):
                continue
            declarations = re.findall(r"(?P<prop>padding(?:-top)?)\s*:\s*(?P<value>[^;{}]+)", match.group("body"), flags=re.IGNORECASE)
            if not declarations:
                continue
            has_safe_top = False
            has_unsafe_top = False
            for prop, value in declarations:
                prop_name = prop.strip().lower()
                css_value = value.strip().lower()
                safe_value = any(marker.lower() in css_value for marker in PAGE_SHELL_SAFE_TOP_MARKERS)
                if prop_name in {"padding", "padding-top"} and safe_value:
                    has_safe_top = True
                elif prop_name in {"padding", "padding-top"}:
                    has_unsafe_top = True
            if has_unsafe_top and not has_safe_top:
                return ValidationIssue(
                    code="platform.role_css_collapses_shell_safe_top_spacing",
                    message=(
                        f"{role} role CSS overrides .page-shell padding without preserving the Telegram top safe spacing. "
                        "Move layout spacing to inner elements or use the shared safe top expression."
                    ),
                    severity="high",
                    location=f"miniapp/app/static/{role}/styles.css",
                    blocking=True,
                )
        return None

    @staticmethod
    def _css_selectors_target_page_shell(selectors: str) -> bool:
        for selector in str(selectors or "").split(","):
            selector_text = selector.strip()
            if ".page-shell" not in selector_text:
                continue
            match = re.search(r"\.page-shell\b", selector_text)
            if not match:
                continue
            tail = selector_text[match.end():]
            if not tail.strip():
                return True
            if tail[0] in {".", "#", "[", ":"} and not re.search(r"[\s>+~]", tail):
                return True
        return False

    @staticmethod
    def _role_action_signals(role: str, content: str) -> list[str]:
        text = str(content or "")
        lowered = text.lower()
        signals: list[str] = []
        if role == "client":
            if "<form" in lowered:
                signals.append("form")
            if re.search(r"method\s*:\s*['\"]post['\"]", text, flags=re.IGNORECASE) or ".post(" in lowered:
                signals.append("post")
            return signals if {"form", "post"}.issubset(set(signals)) else []
        if role == "specialist":
            has_update_method = bool(re.search(r"method\s*:\s*['\"](?:patch|put|delete)['\"]", text, flags=re.IGNORECASE))
            has_status_post = bool(
                re.search(r"method\s*:\s*['\"]post['\"]", text, flags=re.IGNORECASE)
                and re.search(r"(status_updates|status|статус|готов|очеред)", lowered)
            )
            if has_update_method or has_status_post:
                signals.append("status_update")
            if re.search(r"\b(confirm|done|complete|assign|process|status|queue)\b", lowered) or re.search(
                r"(статус|готов|очеред|заказ|обнов|кондитер|работ)", lowered
            ):
                signals.append("operations")
            return signals if len(signals) >= 2 else []
        if role == "manager":
            if re.search(r"\b(metric|dashboard|summary|total|workload|overview|count)\b", lowered):
                signals.append("dashboard")
            has_manager_control = bool(
                re.search(r"\b(review|approve|escalate|assign|control|oversight|refresh|filter|audit)\b", lowered)
                or re.search(r"\b(manager-oversight|manager-control|manager-review)\b", lowered)
                or re.search(r"(контрол|обнов|провер|соглас|назнач|отч[её]т|фильтр)", lowered)
            )
            has_user_action_surface = bool(
                re.search(r"method\s*:\s*['\"](?:patch|put|delete)['\"]", text, flags=re.IGNORECASE)
                or re.search(r"addEventListener\s*\(", text)
                or "<button" in lowered
            )
            if re.search(r"method\s*:\s*['\"](?:patch|put|delete)['\"]", text, flags=re.IGNORECASE) or (has_manager_control and has_user_action_surface):
                signals.append("oversight_action")
            return signals if len(signals) >= 2 else []
        return signals

    @staticmethod
    def _cross_role_links(role: str, content: str) -> list[str]:
        other_roles = [candidate for candidate in ROLE_ORDER if candidate != role]
        links: list[str] = []
        for match in re.finditer(r"\bhref\s*=\s*['\"](?P<href>/[^'\"]*)['\"]", str(content or ""), flags=re.IGNORECASE):
            href = str(match.group("href") or "").strip()
            if any(href == f"/{other}" or href.startswith(f"/{other}/") for other in other_roles):
                links.append(href)
        return list(dict.fromkeys(links))

    @staticmethod
    def _technical_role_copy_markers(content: str) -> list[str]:
        lowered = str(content or "").lower()
        markers = (
            "client app",
            "specialist app",
            "manager app",
            "source request",
            "collect user-provided details",
            "record records",
        )
        return [marker for marker in markers if marker in lowered]

    @staticmethod
    def _role_surfaces_too_similar(role_text: dict[str, str]) -> bool:
        if any(role not in role_text for role in ROLE_ORDER):
            return False
        normalized = {
            role: CheckRunner._normalize_role_surface_text(text)
            for role, text in role_text.items()
        }
        pairs = [
            ("client", "specialist"),
            ("client", "manager"),
            ("specialist", "manager"),
        ]
        high_similarity = 0
        for left, right in pairs:
            if not normalized[left] or not normalized[right]:
                continue
            ratio = difflib.SequenceMatcher(None, normalized[left], normalized[right]).ratio()
            if ratio >= 0.92:
                high_similarity += 1
        return high_similarity >= 2

    @staticmethod
    def _normalize_role_surface_text(content: str) -> str:
        text = re.sub(r"<script[\s\S]*?</script>", " ", str(content or ""), flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.lower()
        text = re.sub(r"\b(client|specialist|manager|клиент|специалист|менеджер)\b", "role", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:5000]

    @classmethod
    def _create_api_contract_issues(cls, source_dir: Path) -> tuple[list[ValidationIssue], dict[str, object]]:
        declared_methods = cls._declared_api_methods(source_dir / "miniapp/app/routes")
        static_root = source_dir / "miniapp/app/static"
        frontend_refs = cls._frontend_api_refs(static_root)
        frontend_raw_methods = cls._frontend_raw_api_methods(static_root)
        has_backend_get = any(method == "GET" for method, _path in declared_methods)
        has_backend_post = any(method == "POST" for method, _path in declared_methods)
        has_backend_update = any(method in {"PATCH", "PUT", "DELETE"} for method, _path in declared_methods)
        frontend_post_refs = sorted(path for method, path in frontend_refs if method == "POST")
        frontend_update_refs = sorted(path for method, path in frontend_refs if method in {"PATCH", "PUT", "DELETE"})
        issues: list[ValidationIssue] = []
        if not has_backend_get:
            issues.append(
                ValidationIssue(
                    code="platform.missing_create_get_api",
                    message="Create runs must expose at least one GET /api resource so saved user records can be listed.",
                    severity="high",
                    location="miniapp/app/routes",
                    blocking=True,
                )
            )
        if not has_backend_post:
            issues.append(
                ValidationIssue(
                    code="platform.missing_create_post_api",
                    message="Create runs must expose at least one POST /api resource so users can save new records.",
                    severity="high",
                    location="miniapp/app/routes",
                    blocking=True,
                )
            )
        if not frontend_post_refs:
            raw_post_present = "POST" in frontend_raw_methods
            issues.append(
                ValidationIssue(
                    code="platform.frontend_missing_post_api",
                    message=(
                        "Create runs must include frontend form/fetch code that POSTs user-provided records to /api."
                        if not raw_post_present
                        else "Frontend contains POST and /api markers, but the validator could not pair them confidently; generated tests must confirm the flow."
                    ),
                    severity="medium" if raw_post_present else "high",
                    location="miniapp/app/static",
                    blocking=not raw_post_present,
                )
            )
        if not has_backend_update:
            issues.append(
                ValidationIssue(
                    code="platform.missing_create_update_api",
                    message="Create runs must expose at least one PATCH/PUT/DELETE /api endpoint so specialist or manager roles can update saved work.",
                    severity="high",
                    location="miniapp/app/routes",
                    blocking=True,
                )
            )
        if not frontend_update_refs:
            raw_update_present = bool(frontend_raw_methods.intersection({"PATCH", "PUT", "DELETE"}))
            issues.append(
                ValidationIssue(
                    code="platform.frontend_missing_update_api",
                    message=(
                        "Create runs must include specialist or manager frontend actions that update saved records through /api."
                        if not raw_update_present
                        else "Frontend contains update-method and /api markers, but the validator could not pair them confidently; generated tests must confirm the flow."
                    ),
                    severity="medium" if raw_update_present else "high",
                    location="miniapp/app/static",
                    blocking=not raw_update_present,
                )
            )
        return issues, {
            "declared_methods": [
                {"method": method, "path": path}
                for method, path in sorted(declared_methods)
            ],
            "frontend_refs": [
                {"method": method, "path": path}
                for method, path in sorted(frontend_refs)
            ],
            "frontend_post_refs": frontend_post_refs,
            "frontend_update_refs": frontend_update_refs,
            "frontend_raw_methods": sorted(frontend_raw_methods),
        }

    @staticmethod
    def _declared_api_methods(routes_root: Path) -> set[tuple[str, str]]:
        return extract_declared_routes(routes_root, api_only=True)

    @classmethod
    def _frontend_api_refs(cls, static_root: Path) -> set[tuple[str, str]]:
        refs: set[tuple[str, str]] = set()
        if not static_root.exists():
            return refs
        for path in static_root.rglob("*"):
            if path.suffix.lower() not in {".html", ".js"} or path.name == "preview_bridge.js":
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            refs.update(cls._extract_frontend_api_refs(content))
        return refs

    @staticmethod
    def _frontend_raw_api_methods(static_root: Path) -> set[str]:
        methods: set[str] = set()
        if not static_root.exists():
            return methods
        for path in static_root.rglob("*"):
            if path.suffix.lower() not in {".html", ".js"} or path.name == "preview_bridge.js":
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "/api/" not in content and "/api" not in content:
                continue
            for match in re.finditer(r"method\s*:\s*['\"](?P<method>POST|PUT|PATCH|DELETE)['\"]", content, re.IGNORECASE):
                methods.add(match.group("method").upper())
            for match in re.finditer(r"\.(?P<method>post|put|patch|delete)\s*\(", content, re.IGNORECASE):
                methods.add(match.group("method").upper())
        return methods

    @staticmethod
    def _extract_frontend_api_refs(content: str) -> set[tuple[str, str]]:
        return extract_frontend_api_refs(content)

    @staticmethod
    def _normalize_api_ref_path(value: str) -> str:
        return normalize_api_path(value)

    @classmethod
    def _preloaded_business_data_issues(cls, source_dir: Path) -> tuple[list[ValidationIssue], list[dict[str, str]]]:
        app_root = source_dir / "miniapp/app"
        findings: list[dict[str, str]] = []
        issues: list[ValidationIssue] = []
        if not app_root.exists():
            return issues, findings
        scan_suffixes = {".py", ".js", ".html", ".json"}
        for path in app_root.rglob("*"):
            if path.suffix.lower() not in scan_suffixes:
                continue
            relative_path = path.relative_to(source_dir).as_posix()
            if relative_path in {"miniapp/app/static/preview_bridge.js", "miniapp/app/generated/route_manifest.json"}:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            marker = cls._preloaded_business_data_marker(content)
            if not marker:
                continue
            findings.append({"file_path": relative_path, "marker": marker})
            issues.append(
                ValidationIssue(
                    code="platform.preloaded_business_data",
                    message=(
                        f"{relative_path} appears to include preloaded business data ({marker}). "
                        "Create apps must start with empty persistent state and let users add records."
                    ),
                    severity="high",
                    location=relative_path,
                    blocking=True,
                )
            )
        return issues, findings

    @staticmethod
    def _preloaded_business_data_marker(content: str) -> str | None:
        text = str(content or "")
        lowered = text.lower()
        compact = re.sub(r"[\s_\-]+", "", lowered)
        for marker in PRELOADED_BUSINESS_DATA_MARKERS:
            normalized_marker = marker.lower()
            compact_marker = re.sub(r"[\s_\-]+", "", normalized_marker)
            if normalized_marker in lowered or compact_marker in compact:
                return marker
        marker_match = re.search(
            r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*(?:Mock|Demo|Sample|Seed|Fixture|Preloaded)[A-Za-z_$\d]*\s*=",
            text,
        )
        if marker_match:
            return marker_match.group(0).strip()
        python_marker = re.search(
            r"\b(?:MOCK|DEMO|SAMPLE|SEED|FIXTURE|PRELOADED)_[A-Z0-9_]*\s*=",
            text,
        )
        if python_marker:
            return python_marker.group(0).strip()
        return None

    @staticmethod
    def _multipage_coverage_from_roles(role_coverage: dict[str, object]) -> dict[str, object]:
        coverage: dict[str, object] = {}
        for role in ROLE_ORDER:
            payload = role_coverage.get(role)
            if not isinstance(payload, dict):
                continue
            coverage[role] = {
                "status": payload.get("status"),
                "route_count": payload.get("route_count"),
                "secondary_route_count": payload.get("secondary_route_count"),
                "required_route_count": payload.get("required_route_count") or MIN_ROLE_ROUTE_PAGES,
                "routes": payload.get("routes") or [],
            }
        return coverage

    @classmethod
    def _routeable_role_pages(cls, source_dir: Path) -> dict[str, list[dict[str, str]]]:
        pages_by_role: dict[str, list[dict[str, str]]] = {role: [] for role in ROLE_ORDER}
        manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        except ValueError:
            manifest = {}
        top_level_routes = manifest.get("routes") if isinstance(manifest, dict) else {}
        if isinstance(top_level_routes, dict):
            for route_path_raw, file_path_raw_value in top_level_routes.items():
                route_path_text = str(route_path_raw or "").strip()
                file_path_raw = str(file_path_raw_value or "").strip()
                if not route_path_text or not file_path_raw:
                    continue
                for role in ROLE_ORDER:
                    route_probe = route_path_text if route_path_text.startswith("/") else f"/{route_path_text}"
                    route_probe = route_probe.rstrip("/") or f"/{role}"
                    if route_probe != f"/{role}" and not route_probe.startswith(f"/{role}/"):
                        continue
                    normalized_route = cls._normalize_role_route(role, route_probe)
                    file_path = cls._resolve_manifest_static_page(source_dir, file_path_raw)
                    if not file_path.exists():
                        continue
                    pages_by_role[role].append(
                        {
                            "route_path": normalized_route,
                            "file_path": file_path.relative_to(source_dir).as_posix(),
                            "source": "manifest_top_routes",
                        }
                    )
                    break
        roles = manifest.get("roles") if isinstance(manifest, dict) else {}
        if isinstance(roles, dict):
            for route_path_raw, file_path_raw_value in roles.items():
                if not isinstance(file_path_raw_value, str):
                    continue
                route_path_text = str(route_path_raw or "").strip()
                file_path_raw = str(file_path_raw_value or "").strip()
                if not route_path_text or not file_path_raw:
                    continue
                route_probe = route_path_text if route_path_text.startswith("/") else f"/{route_path_text}"
                route_probe = route_probe.rstrip("/") or "/"
                for role in ROLE_ORDER:
                    if route_probe != f"/{role}" and not route_probe.startswith(f"/{role}/"):
                        continue
                    file_path = cls._resolve_manifest_static_page(source_dir, file_path_raw)
                    if not file_path.exists():
                        continue
                    pages_by_role[role].append(
                        {
                            "route_path": cls._normalize_role_route(role, route_probe),
                            "file_path": file_path.relative_to(source_dir).as_posix(),
                            "source": "manifest_roles_route_map",
                        }
                    )
                    break
            for role in ROLE_ORDER:
                role_payload = roles.get(role) or roles.get(f"/{role}")
                if not isinstance(role_payload, dict):
                    continue
                route_map = role_payload.get("routes")
                if not isinstance(route_map, dict):
                    route_map = {
                        str(route_path): str(file_path)
                        for route_path, file_path in role_payload.items()
                        if isinstance(file_path, str) and str(route_path) not in {"pages", "routes"}
                    }
                if isinstance(route_map, dict):
                    for route_path_raw, file_path_raw_value in route_map.items():
                        file_path_raw = str(file_path_raw_value or "").strip()
                        if not file_path_raw:
                            continue
                        file_path = cls._resolve_manifest_static_page(source_dir, file_path_raw)
                        if not file_path.exists():
                            continue
                        route_path = cls._normalize_manifest_role_route(role, str(route_path_raw or "").strip())
                        pages_by_role[role].append(
                            {
                                "route_path": route_path,
                                "file_path": file_path.relative_to(source_dir).as_posix(),
                                "source": "manifest_routes",
                            }
                        )
                pages = role_payload.get("pages")
                if not isinstance(pages, list):
                    continue
                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    file_path_raw = str(page.get("file_path") or "").strip()
                    if not file_path_raw:
                        continue
                    file_path = cls._resolve_manifest_static_page(source_dir, file_path_raw)
                    if not file_path.exists():
                        continue
                    route_path = cls._normalize_role_route(role, str(page.get("route_path") or "").strip())
                    pages_by_role[role].append(
                        {
                            "route_path": route_path,
                            "file_path": file_path.relative_to(source_dir).as_posix(),
                            "source": "manifest",
                        }
                    )

        static_root = source_dir / "miniapp/app/static"
        for role in ROLE_ORDER:
            role_root = static_root / role
            if not role_root.exists():
                continue
            for html_path in sorted(role_root.rglob("index.html")):
                route_path = cls._filesystem_role_route(role, role_root, html_path)
                pages_by_role[role].append(
                    {
                        "route_path": route_path,
                        "file_path": html_path.relative_to(source_dir).as_posix(),
                        "source": "filesystem",
                    }
                )
        return {
            role: cls._dedupe_role_pages(pages)
            for role, pages in pages_by_role.items()
        }

    @staticmethod
    def _resolve_manifest_static_page(source_dir: Path, file_path_raw: str) -> Path:
        normalized = str(file_path_raw or "").strip().replace("\\", "/").lstrip("/")
        if normalized.startswith("miniapp/app/"):
            return source_dir / normalized
        if normalized.startswith("app/"):
            return source_dir / "miniapp" / normalized
        if normalized.startswith("static/"):
            return source_dir / "miniapp/app" / normalized
        return source_dir / normalized

    @staticmethod
    def _normalize_manifest_role_route(role: str, route_path: str) -> str:
        route = str(route_path or "").strip()
        if not route or route in {"root", "index", "/"}:
            return f"/{role}"
        if route.startswith("/"):
            return CheckRunner._normalize_role_route(role, route)
        return f"/{role}/{route}".rstrip("/")

    @staticmethod
    def _normalize_role_route(role: str, route_path: str) -> str:
        normalized = str(route_path or "").strip()
        if not normalized or normalized in {"/", f"/{role}/root"}:
            return f"/{role}"
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        normalized = normalized.rstrip("/") or f"/{role}"
        if normalized == f"/{role}/root":
            return f"/{role}"
        if normalized == f"/{role}" or normalized.startswith(f"/{role}/"):
            return normalized
        return f"/{role}{normalized}"

    @staticmethod
    def _filesystem_role_route(role: str, role_root: Path, html_path: Path) -> str:
        relative = html_path.relative_to(role_root).as_posix()
        slug = relative.removesuffix("/index.html").removesuffix("index.html").strip("/")
        return f"/{role}/{slug}".rstrip("/") if slug else f"/{role}"

    @staticmethod
    def _dedupe_role_pages(pages: list[dict[str, str]]) -> list[dict[str, str]]:
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for page in pages:
            route_path = str(page.get("route_path") or "").rstrip("/") or "/"
            file_path = str(page.get("file_path") or "")
            key = (route_path, file_path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(page)
        return deduped

    @staticmethod
    def _unique_role_routes(pages: list[dict[str, str]]) -> list[str]:
        routes: list[str] = []
        seen: set[str] = set()
        for page in pages:
            route = str(page.get("route_path") or "").rstrip("/") or "/"
            if route in seen:
                continue
            seen.add(route)
            routes.append(route)
        return routes

    @staticmethod
    def _domain_tokens(text: str) -> set[str]:
        cleaned = re.sub(r"<script\b.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<style\b.*?</style>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_-]{3,}", cleaned)
        }
        return {
            token
            for token in tokens
            if token not in ROLE_LINK_STOPWORDS
            and not token.startswith(("data-", "aria-", "http", "html", "body", "class", "const", "function"))
        }

    @staticmethod
    def _generated_tests_presence_issues(source_dir: Path) -> tuple[list[ValidationIssue], dict[str, object]]:
        expected = {
            "python": source_dir / "miniapp" / "tests" / "test_generated_app.py",
            "js": source_dir / "miniapp" / "tests" / "generated_app.test.mjs",
        }
        status: dict[str, object] = {}
        issues: list[ValidationIssue] = []
        for kind, path in expected.items():
            relative_path = path.relative_to(source_dir).as_posix()
            if path.exists():
                status[kind] = {"status": "present", "file_path": relative_path}
                continue
            status[kind] = {"status": "missing", "file_path": relative_path}
            issues.append(
                ValidationIssue(
                    code="platform.missing_generated_app_tests",
                    message=f"Generated {kind} app test file is missing: {relative_path}.",
                    severity="high",
                    location=relative_path,
                    blocking=True,
                )
            )
        return issues, status

    @classmethod
    def _dom_contract_issues(cls, *, source_dir: Path, changed_files: list[str]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        js_files = [
            str(path)
            for path in changed_files
            if path.startswith("miniapp/app/static/") and path.endswith(".js")
        ]
        for relative_path in js_files:
            js_path = source_dir / relative_path
            if not js_path.exists():
                continue
            html_candidates = [
                js_path.with_name("index.html"),
                js_path.with_suffix(".html"),
            ]
            role_root = cls._role_static_root_for_js(source_dir, relative_path)
            if role_root is not None and role_root.exists():
                html_candidates.extend(sorted(role_root.rglob("index.html")))
            existing_html_paths = list(dict.fromkeys(candidate for candidate in html_candidates if candidate.exists()))
            if not existing_html_paths:
                continue
            try:
                js_source = js_path.read_text(encoding="utf-8")
            except OSError:
                continue
            html_sources: list[str] = []
            all_page_sources: list[tuple[str, str, set[str]]] = []
            page_sources: list[tuple[str, str, set[str]]] = []
            for html_path in existing_html_paths:
                try:
                    html_source = html_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                html_sources.append(html_source)
                html_relative = html_path.relative_to(source_dir).as_posix()
                html_ids = extract_html_ids(html_source)
                all_page_sources.append((html_relative, html_source, html_ids))
                if cls._html_references_script(html_relative, html_source, relative_path):
                    page_sources.append((html_relative, html_source, html_ids))
            available_dom_ids = extract_html_ids("\n".join(html_sources))
            bindings = cls._js_dom_id_bindings(js_source)
            unsafe_variables = cls._unsafe_js_dom_variables(js_source, set(bindings))
            unsafe_dom_ids = {bindings[var_name] for var_name in unsafe_variables if var_name in bindings}
            unsafe_dom_ids.update(cls._unsafe_direct_dom_ids(js_source))
            missing_ids = sorted(unsafe_dom_ids - available_dom_ids)
            if missing_ids:
                issues.append(
                    ValidationIssue(
                        code="platform.missing_dom_id",
                        message=f"{relative_path} references DOM ids not found in any matching role HTML page: {', '.join(missing_ids)}.",
                        severity="high",
                        location=relative_path,
                        blocking=True,
                    )
                )
            if not page_sources:
                page_sources = all_page_sources
            issues.extend(cls._unchecked_page_dom_issues(relative_path, js_source, page_sources))
        return issues

    @staticmethod
    def _role_static_root_for_js(source_dir: Path, relative_path: str) -> Path | None:
        return role_static_root(source_dir, relative_path)

    @staticmethod
    def _role_script_paths(source_dir: Path) -> list[str]:
        static_root = source_dir / "miniapp/app/static"
        if not static_root.exists():
            return []
        paths: list[str] = []
        for role in ROLE_ORDER:
            role_root = static_root / role
            if not role_root.exists():
                continue
            for script_path in sorted(role_root.rglob("*.js")):
                paths.append(script_path.relative_to(source_dir).as_posix())
        return list(dict.fromkeys(paths))

    @classmethod
    def _unchecked_page_dom_issues(
        cls,
        script_relative_path: str,
        js_source: str,
        page_sources: list[tuple[str, str, set[str]]],
    ) -> list[ValidationIssue]:
        bindings = cls._js_dom_id_bindings(js_source)
        unsafe_variables = cls._unsafe_js_dom_variables(js_source, set(bindings))
        unsafe_ids = {bindings[var_name] for var_name in unsafe_variables if var_name in bindings}
        unsafe_ids.update(cls._unsafe_direct_dom_ids(js_source))
        if not unsafe_ids:
            return []
        issues: list[ValidationIssue] = []
        for page_relative_path, _html_source, page_ids in page_sources:
            missing_ids = sorted(dom_id for dom_id in unsafe_ids if dom_id not in page_ids)
            if not missing_ids:
                continue
            issues.append(
                ValidationIssue(
                    code="platform.unchecked_page_dom_id",
                    message=(
                        f"{script_relative_path} is loaded by {page_relative_path} but dereferences DOM ids "
                        f"not present on that page without an obvious guard: {', '.join(missing_ids[:6])}. "
                        "Guard page-specific elements before property access/event listeners or split page scripts."
                    ),
                    severity="high",
                    location=script_relative_path,
                    blocking=True,
                )
            )
        return issues

    @staticmethod
    def _js_dom_id_bindings(js_source: str) -> dict[str, str]:
        bindings: dict[str, str] = {}
        pattern = re.compile(
            r"""\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*document\.(?:getElementById\(\s*["'](?P<id1>[A-Za-z0-9_-]+)["']\s*\)|querySelector\(\s*["']\#(?P<id2>[A-Za-z0-9_-]+)["']\s*\))""",
            re.DOTALL,
        )
        for match in pattern.finditer(str(js_source or "")):
            dom_id = match.group("id1") or match.group("id2")
            if dom_id:
                bindings[match.group("var")] = dom_id
        return bindings

    @classmethod
    def _unsafe_js_dom_variables(cls, js_source: str, variables: set[str]) -> set[str]:
        unsafe: set[str] = set()
        if not variables:
            return unsafe
        lines = str(js_source or "").splitlines()
        for index, line in enumerate(lines):
            for var_name in variables:
                for match in re.finditer(rf"\b{re.escape(var_name)}\s*\.", line):
                    prefix = line[: match.start()]
                    if prefix.rstrip().endswith("?"):
                        continue
                    context = cls._dom_access_context(lines, index)
                    if cls._dom_variable_access_is_guarded(context, line, var_name):
                        continue
                    unsafe.add(var_name)
        return unsafe

    @staticmethod
    def _dom_access_context(lines: list[str], index: int) -> str:
        start = max(0, index - 80)
        return "\n".join(lines[start : index + 1])

    @staticmethod
    def _dom_variable_access_is_guarded(context: str, line: str, var_name: str) -> bool:
        escaped = re.escape(var_name)
        if re.search(rf"\b{escaped}\s*&&", line):
            return True
        if re.search(rf"\bif\s*\(\s*{escaped}\b", line):
            return True
        if re.search(rf"\bif\s*\([^)]*!\s*{escaped}\b[^)]*\)\s*return\b", context):
            return True
        if re.search(rf"\bif\s*\([^)]*!\s*{escaped}\b[^)]*\)\s*\{{[\s\S]{{0,160}}\breturn\b", context):
            return True
        if re.search(rf"\bif\s*\(\s*!\s*{escaped}\s*\)\s*return\b", context):
            return True
        if re.search(rf"\bif\s*\([^)]*\b{escaped}\b[^)]*\)\s*\{{[\s\S]{{0,400}}\b{escaped}\s*\.", context):
            return True
        return bool(re.search(rf"\bif\s*\(\s*{escaped}\b[\s\S]{{0,160}}\b{escaped}\s*\.", context))

    @staticmethod
    def _unsafe_direct_dom_ids(js_source: str) -> set[str]:
        unsafe: set[str] = set()
        pattern = re.compile(
            r"""document\.(?:getElementById\(\s*["'](?P<id1>[A-Za-z0-9_-]+)["']\s*\)|querySelector\(\s*["']\#(?P<id2>[A-Za-z0-9_-]+)["']\s*\))\s*\.""",
            re.DOTALL,
        )
        for match in pattern.finditer(str(js_source or "")):
            dom_id = match.group("id1") or match.group("id2")
            if dom_id:
                unsafe.add(dom_id)
        return unsafe

    @classmethod
    def _html_references_script(cls, html_relative_path: str, html_source: str, script_relative_path: str) -> bool:
        for ref in extract_script_refs(html_source):
            resolved = cls._resolve_static_ref(ref, source_path=html_relative_path)
            if resolved == script_relative_path:
                return True
        return False

    @staticmethod
    def _resolve_static_ref(raw_ref: str, *, source_path: str) -> str | None:
        ref = str(raw_ref or "").strip().split("?", 1)[0].split("#", 1)[0]
        if not ref or ref.startswith(("http://", "https://", "//", "data:")):
            return None
        if ref.startswith("/static/"):
            return f"miniapp/app{ref}"
        if ref.startswith("static/"):
            return f"miniapp/app/{ref}"
        if ref.startswith("/"):
            return None
        source_parent = Path(source_path).parent.as_posix()
        resolved = posixpath.normpath(posixpath.join(source_parent, ref))
        return resolved if resolved.startswith("miniapp/app/static/") else None

    @staticmethod
    def _dedupe_validation_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        deduped: list[ValidationIssue] = []
        seen: set[tuple[str, str]] = set()
        for issue in issues:
            key = (issue.code, issue.location)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped

    @staticmethod
    def _css_only_app_change(changed_files: list[str]) -> bool:
        relevant = [
            str(path).replace("\\", "/").lstrip("./")
            for path in changed_files
            if isinstance(path, str) and str(path).replace("\\", "/").lstrip("./").startswith("miniapp/app/")
        ]
        return bool(relevant) and all(path.startswith("miniapp/app/static/") and path.endswith(".css") for path in relevant)

    def _focused_css_static_check(self, *, source_dir: Path, changed_files: list[str]) -> RunCheckResult:
        issues: list[str] = []
        checked_paths: list[str] = []
        for raw_path in changed_files:
            path = str(raw_path or "").replace("\\", "/").lstrip("./")
            if not path.startswith("miniapp/app/static/") or not path.endswith(".css"):
                continue
            full_path = source_dir / path
            if not full_path.exists():
                if path == "miniapp/app/static/shared/base.css":
                    continue
                issues.append(f"{path}: CSS file is missing.")
                continue
            try:
                content = full_path.read_text(encoding="utf-8")
            except OSError as exc:
                issues.append(f"{path}: could not read CSS file: {exc}.")
                continue
            checked_paths.append(path)
            stripped = content.strip()
            if not stripped:
                issues.append(f"{path}: CSS file is empty.")
            if any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>")):
                issues.append(f"{path}: unresolved merge/apply conflict marker.")
            if stripped.count("{") != stripped.count("}"):
                issues.append(f"{path}: unbalanced CSS braces.")
        if issues:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Focused CSS static check failed.",
                command="focused CSS static check",
                logs=issues,
            )
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Focused CSS static check passed.",
            command="focused CSS static check",
            logs=[f"Checked CSS files: {', '.join(checked_paths) or 'none'}."],
        )

    def _static_check(self, *, source_dir: Path, changed_files: list[str]) -> RunCheckResult:
        frontend_dir = source_dir / "frontend"
        backend_dir = source_dir / "miniapp"
        logs: list[str] = []
        executed = False

        if (frontend_dir / "package.json").exists():
            executed = True
            frontend_result = self._run_frontend_build(frontend_dir)
            logs.extend(frontend_result.logs)
            if frontend_result.status == "failed":
                return frontend_result

        if (backend_dir / "app").exists():
            executed = True
            backend_result = self._run_backend_compile(backend_dir)
            logs.extend(backend_result.logs)
            if backend_result.status == "failed":
                return backend_result
            js_syntax_result = self._run_static_js_syntax_check(backend_dir)
            logs.extend(js_syntax_result.logs)
            if js_syntax_result.status == "failed":
                return js_syntax_result
            install_result = self._install_python_requirements(
                backend_dir,
                result_name="changed_files_static",
                purpose="Backend import-smoke dependency",
            )
            if install_result is not None:
                logs.extend(install_result.logs)
                if install_result.status == "failed":
                    return install_result
            import_result = self._run_backend_import_smoke(backend_dir)
            logs.extend(import_result.logs)
            if import_result.status == "failed":
                return import_result

        if executed:
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="Full draft compile checks passed.",
                logs=logs or ["Draft compile checks passed."],
            )

        generated = any(path.startswith("artifacts/") for path in changed_files)
        if generated:
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="Generated artifact consistency smoke passed.",
                logs=["Generated artifacts changed; consistency smoke passed."],
            )
        return RunCheckResult(
            name="changed_files_static",
            status="skipped",
            details="No changed-file static checks were required.",
        )

    def _run_python_app_tests(self, backend_dir: Path, *, require_present: bool = False) -> RunCheckResult:
        test_file = backend_dir / "tests" / "test_generated_app.py"
        if not test_file.exists():
            return RunCheckResult(
                name="generated_app_python_tests",
                status="failed" if require_present else "skipped",
                details=(
                    "Generated Python app tests are required for agentic create/edit runs but were not present in the draft workspace."
                    if require_present
                    else "Generated Python app tests were not present in the draft workspace."
                ),
                command=f"{sys.executable} -m unittest discover -s tests -p test_generated_app.py",
                logs=[],
                diagnostics={"missing_test_file": "tests/test_generated_app.py"} if require_present else {},
            )
        try:
            test_source = test_file.read_text(encoding="utf-8")
        except OSError:
            test_source = ""
        if (
            re.search(r"^\s*def\s+test_[A-Za-z0-9_]*\s*\(", test_source, flags=re.MULTILINE)
            and "unittest.TestCase" not in test_source
        ):
            return RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                details="Generated Python app tests are not unittest-discoverable.",
                command=f"{sys.executable} -m unittest discover -s tests -p test_generated_app.py",
                exit_code=5,
                logs=[
                    "Generated Python app tests use top-level pytest-style test functions.",
                    "The platform runs python -m unittest discover, so define import unittest and a unittest.TestCase subclass with test_* methods.",
                    "Without a unittest.TestCase class, unittest reports NO TESTS RAN and the create run cannot complete.",
                ],
                diagnostics={"unittest_discovery_failure": "pytest_style_top_level_functions"},
            )
        install_result = self._install_python_requirements(backend_dir)
        if install_result is not None:
            return install_result
        env = {**os.environ}
        python_path_parts = [str(backend_dir)]
        existing_python_path = env.get("PYTHONPATH")
        if existing_python_path:
            python_path_parts.append(existing_python_path)
        env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_generated_app.py"]
        with tempfile.TemporaryDirectory(prefix="miniapp-generated-tests-") as tmp_dir:
            env["DATABASE_URL"] = f"sqlite:///{(Path(tmp_dir) / 'app.db').as_posix()}"
            try:
                result = subprocess.run(
                    command,
                    cwd=backend_dir,
                    capture_output=True,
                    text=True,
                    timeout=int(os.getenv("GENERATED_APP_PYTHON_TEST_TIMEOUT_SEC", "240")),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                return RunCheckResult(
                    name="generated_app_python_tests",
                    status="failed",
                    details="Generated Python app tests timed out.",
                    command=" ".join(command),
                    logs=self._command_logs("Generated Python app tests timed out.", exc.stdout or "", exc.stderr or ""),
                )
        if result.returncode != 0:
            logs = self._command_logs("Generated Python app tests failed for the draft miniapp.", result.stdout, result.stderr)
            return RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                details="Generated Python app tests failed for the draft miniapp.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=logs,
                diagnostics=self._extract_generated_app_test_diagnostics(logs),
            )
        return RunCheckResult(
            name="generated_app_python_tests",
            status="passed",
            details="Generated Python app tests passed.",
            command=" ".join(command),
            exit_code=result.returncode,
            logs=["Generated Python app tests passed."],
        )

    def _install_python_requirements(
        self,
        backend_dir: Path,
        *,
        result_name: str = "generated_app_python_tests",
        purpose: str = "Generated Python dependency",
    ) -> RunCheckResult | None:
        requirements_file = backend_dir / "requirements.txt"
        if not requirements_file.exists():
            return None
        try:
            digest = hashlib.sha256(requirements_file.read_bytes()).hexdigest()
        except OSError:
            digest = str(requirements_file)
        cache_key = f"{sys.executable}:{digest}"
        with self._python_requirements_cache_lock:
            if cache_key in self._python_requirements_cache:
                return None
        command = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", "requirements.txt"]
        env = {
            **os.environ,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        try:
            result = subprocess.run(
                command,
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("GENERATED_APP_PYTHON_INSTALL_TIMEOUT_SEC", "600")),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return RunCheckResult(
                name=result_name,
                status="failed",
                details=f"{purpose} install timed out.",
                command=" ".join(command),
                logs=self._command_logs(
                    f"{purpose} install timed out.",
                    exc.stdout or "",
                    exc.stderr or "",
                ),
            )
        if result.returncode != 0:
            return RunCheckResult(
                name=result_name,
                status="failed",
                details=f"{purpose} install failed.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=self._command_logs(
                    f"{purpose} install failed.",
                    result.stdout,
                    result.stderr,
                ),
            )
        with self._python_requirements_cache_lock:
            self._python_requirements_cache.add(cache_key)
        return None

    def _run_js_app_tests(self, backend_dir: Path, *, require_present: bool = False) -> RunCheckResult:
        test_file = backend_dir / "tests" / "generated_app.test.mjs"
        if not test_file.exists():
            return RunCheckResult(
                name="generated_app_js_tests",
                status="failed" if require_present else "skipped",
                details=(
                    "Generated JS app tests are required for agentic create/edit runs but were not present in the draft workspace."
                    if require_present
                    else "Generated JS app tests were not present in the draft workspace."
                ),
                command="node --test tests/generated_app.test.mjs",
                logs=[],
                diagnostics={"missing_test_file": "tests/generated_app.test.mjs"} if require_present else {},
            )
        node_binary = shutil.which("node") or shutil.which("nodejs")
        if not node_binary:
            return RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Node.js is missing for generated app tests.",
                command="node --test tests/generated_app.test.mjs",
                logs=[
                    "Node.js is missing for generated app tests.",
                    "Install Node.js in the platform runtime so generated JS tests can run.",
                ],
            )
        command = [node_binary, "--test", "tests/generated_app.test.mjs"]
        try:
            result = subprocess.run(
                command,
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("GENERATED_APP_JS_TEST_TIMEOUT_SEC", "240")),
            )
        except subprocess.TimeoutExpired as exc:
            return RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Generated JS app tests timed out.",
                command=" ".join(command),
                logs=self._command_logs("Generated JS app tests timed out.", exc.stdout or "", exc.stderr or ""),
            )
        if result.returncode != 0:
            logs = self._command_logs("Generated JS app tests failed for the draft miniapp.", result.stdout, result.stderr)
            return RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Generated JS app tests failed for the draft miniapp.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=logs,
                diagnostics=self._extract_generated_app_test_diagnostics(logs, test_file=test_file),
            )
        return RunCheckResult(
            name="generated_app_js_tests",
            status="passed",
            details="Generated JS app tests passed.",
            command=" ".join(command),
            exit_code=result.returncode,
            logs=["Generated JS app tests passed."],
        )

    def _run_frontend_build(self, frontend_dir: Path) -> RunCheckResult:
        npm_binary = os.getenv("FRONTEND_NPM_BINARY") or shutil.which("npm")
        env = {
            **os.environ,
            "CI": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_update_notifier": "false",
        }

        install_timeout = int(os.getenv("FRONTEND_INSTALL_TIMEOUT_SEC", "900"))
        build_timeout = int(os.getenv("FRONTEND_BUILD_TIMEOUT_SEC", "900"))

        if not npm_binary:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Frontend build tooling is unavailable in the miniapp runtime.",
                command="npm run build",
                logs=[
                    "Frontend build tooling is unavailable in the miniapp runtime.",
                    "npm was not found on PATH.",
                    "Install Node.js/npm in the platform miniapp runtime and rebuild the miniapp container.",
                ],
            )

        try:
            self._reset_frontend_build_state(frontend_dir)
            install_cmd = (
                [npm_binary, "ci", "--no-audit", "--no-fund"]
                if (frontend_dir / "package-lock.json").exists()
                else [npm_binary, "install", "--no-audit", "--no-fund"]
            )
            install_result = subprocess.run(
                install_cmd,
                cwd=frontend_dir,
                capture_output=True,
                text=True,
                timeout=install_timeout,
                env=env,
            )
            if install_result.returncode != 0:
                return RunCheckResult(
                    name="changed_files_static",
                    status="failed",
                    details="Frontend dependency install failed before build.",
                    command=" ".join(install_cmd),
                    exit_code=install_result.returncode,
                    logs=self._command_logs(
                        "Frontend dependency install failed before build.",
                        install_result.stdout,
                        install_result.stderr,
                    ),
                )

            build_result = subprocess.run(
                [npm_binary, "run", "build"],
                cwd=frontend_dir,
                capture_output=True,
                text=True,
                timeout=build_timeout,
                env=env,
            )
            if build_result.returncode != 0:
                return RunCheckResult(
                    name="changed_files_static",
                    status="failed",
                    details="npm run build failed for the draft frontend.",
                    command=f"{npm_binary} run build",
                    exit_code=build_result.returncode,
                    logs=self._command_logs(
                        "npm run build failed for the draft frontend.",
                        build_result.stdout,
                        build_result.stderr,
                    ),
                )
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="npm run build passed for the draft frontend.",
                command=f"{npm_binary} run build",
                exit_code=build_result.returncode,
                logs=["npm run build passed for the draft frontend."],
            )
        except FileNotFoundError:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="npm is not available in the miniapp runtime.",
                command="npm run build",
                logs=["npm is not available in the miniapp runtime."],
            )
        except subprocess.TimeoutExpired as exc:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="npm run build timed out for the draft frontend.",
                command=f"{npm_binary} run build",
                logs=self._command_logs(
                    "npm run build timed out for the draft frontend.",
                    exc.stdout or "",
                    exc.stderr or "",
                ),
            )

    @staticmethod
    def _reset_frontend_build_state(frontend_dir: Path) -> None:
        for artifact_name in ("node_modules", "dist", "build", ".vite"):
            artifact_path = frontend_dir / artifact_name
            if artifact_path.is_dir():
                shutil.rmtree(artifact_path, ignore_errors=True)
        for pattern in ("*.tsbuildinfo",):
            for artifact_path in frontend_dir.glob(pattern):
                try:
                    artifact_path.unlink()
                except OSError:
                    pass

    def _run_backend_compile(self, backend_dir: Path) -> RunCheckResult:
        app_dir = backend_dir / "app"
        py_files = sorted(str(path.relative_to(backend_dir)) for path in app_dir.rglob("*.py"))
        if not py_files:
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="No miniapp Python files required compilation.",
                command="python -m py_compile",
                logs=["No miniapp Python files required compilation."],
            )
        try:
            command = [sys.executable, "-m", "py_compile", *py_files]
            result = subprocess.run(
                command,
                cwd=backend_dir,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("BACKEND_COMPILE_TIMEOUT_SEC", "180")),
            )
        except subprocess.TimeoutExpired as exc:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Backend py_compile timed out.",
                command=f"{sys.executable} -m py_compile {' '.join(py_files)}",
                logs=self._command_logs("Backend py_compile timed out.", exc.stdout or "", exc.stderr or ""),
            )
        if result.returncode != 0:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Backend py_compile failed for the draft miniapp.",
                command=f"{sys.executable} -m py_compile {' '.join(py_files)}",
                exit_code=result.returncode,
                logs=self._command_logs("Backend py_compile failed for the draft miniapp.", result.stdout, result.stderr),
            )
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Backend py_compile passed for the draft miniapp.",
            command=f"{sys.executable} -m py_compile {' '.join(py_files)}",
            exit_code=result.returncode,
            logs=["Backend py_compile passed for the draft miniapp."],
        )

    def _run_static_js_syntax_check(self, backend_dir: Path) -> RunCheckResult:
        static_dir = backend_dir / "app" / "static"
        js_files = sorted(str(path.relative_to(backend_dir)) for path in static_dir.rglob("*.js")) if static_dir.exists() else []
        if not js_files:
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="No static JavaScript files required syntax checking.",
                command="node --check",
                logs=["No static JavaScript files required syntax checking."],
            )
        node_binary = shutil.which("node") or shutil.which("nodejs")
        if not node_binary:
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Node.js is missing for static JavaScript syntax checks.",
                command="node --check",
                logs=[
                    "Node.js is missing for static JavaScript syntax checks.",
                    "Install Node.js in the platform runtime so generated browser scripts can be validated before apply.",
                ],
            )
        for js_file in js_files:
            command = [node_binary, "--check", js_file]
            try:
                result = subprocess.run(
                    command,
                    cwd=backend_dir,
                    capture_output=True,
                    text=True,
                    timeout=int(os.getenv("STATIC_JS_CHECK_TIMEOUT_SEC", "45")),
                )
            except subprocess.TimeoutExpired as exc:
                return RunCheckResult(
                    name="changed_files_static",
                    status="failed",
                    details="Static JavaScript syntax check timed out.",
                    command=" ".join(command),
                    logs=self._command_logs("Static JavaScript syntax check timed out.", exc.stdout or "", exc.stderr or ""),
                )
            if result.returncode != 0:
                logs = self._command_logs("Static JavaScript syntax check failed for the draft miniapp.", result.stdout, result.stderr)
                return RunCheckResult(
                    name="changed_files_static",
                    status="failed",
                    details="Static JavaScript syntax check failed for the draft miniapp.",
                    command=" ".join(command),
                    exit_code=result.returncode,
                    logs=logs,
                    diagnostics=self._extract_static_js_syntax_diagnostics(logs, backend_dir=backend_dir, command_path=js_file),
                )
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Static JavaScript syntax checks passed for the draft miniapp.",
            command=f"{node_binary} --check {' '.join(js_files)}",
            logs=["Static JavaScript syntax checks passed for the draft miniapp."],
        )

    def _run_backend_import_smoke(self, backend_dir: Path) -> RunCheckResult:
        if not (backend_dir / "app" / "main.py").exists():
            return RunCheckResult(
                name="changed_files_static",
                status="skipped",
                details="Backend import smoke skipped because app/main.py is missing.",
                command=f"{sys.executable} -c import app.main",
                logs=[],
            )
        env = {**os.environ}
        python_path_parts = [str(backend_dir)]
        existing_python_path = env.get("PYTHONPATH")
        if existing_python_path:
            python_path_parts.append(existing_python_path)
        env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        with tempfile.TemporaryDirectory(prefix="miniapp-import-smoke-") as tmp_dir:
            env["DATABASE_URL"] = f"sqlite:///{(Path(tmp_dir) / 'app.db').as_posix()}"
            command = [
                sys.executable,
                "-c",
                "import importlib; importlib.import_module('app.main')",
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=backend_dir,
                    capture_output=True,
                    text=True,
                    timeout=int(os.getenv("BACKEND_IMPORT_SMOKE_TIMEOUT_SEC", "45")),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                return RunCheckResult(
                    name="changed_files_static",
                    status="failed",
                    details="Backend import smoke timed out.",
                    command=" ".join(command),
                    logs=self._command_logs("Backend import smoke timed out.", exc.stdout or "", exc.stderr or ""),
                )
        if result.returncode != 0:
            logs = self._command_logs("Backend import smoke failed for the draft miniapp.", result.stdout, result.stderr)
            return RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Backend import smoke failed for the draft miniapp.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=logs,
                diagnostics=self._extract_backend_import_diagnostics(logs),
            )
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Backend import smoke passed for the draft miniapp.",
            command=" ".join(command),
            exit_code=result.returncode,
            logs=["Backend import smoke passed for the draft miniapp."],
        )

    @staticmethod
    def _command_logs(summary: str, stdout: str, stderr: str, *, tail_lines: int = 40) -> list[str]:
        merged = "\n".join(part for part in [stderr.strip(), stdout.strip()] if part.strip())
        lines = [line.rstrip() for line in merged.splitlines() if line.strip()]
        if not lines:
            return [summary]
        tail = lines[-tail_lines:]
        return [summary, *tail]

    @classmethod
    def _extract_static_js_syntax_diagnostics(
        cls,
        logs: list[str],
        *,
        backend_dir: Path | None = None,
        command_path: str | None = None,
    ) -> dict[str, object]:
        diagnostics: dict[str, object] = {}
        file_path = ""
        line_number: int | None = None
        for line in logs:
            match = cls._STATIC_JS_SYNTAX_LOCATION_RE.search(str(line or ""))
            if not match:
                continue
            file_path = f"miniapp/{match.group('relative')}"
            line_number = int(match.group("line"))
            break
        if not file_path and command_path:
            file_path = f"miniapp/{str(command_path).strip().lstrip('./')}"
        syntax_line = next((str(line) for line in logs if line.strip().startswith("SyntaxError:")), "")
        snippet_lines: list[str] = []
        if line_number is not None and backend_dir is not None and file_path:
            target = backend_dir.parent / file_path
            try:
                source_lines = target.read_text(encoding="utf-8").splitlines()
            except OSError:
                source_lines = []
            if source_lines:
                start = max(line_number - 3, 1)
                end = min(line_number + 3, len(source_lines))
                snippet_lines = [
                    f"{number}: {source_lines[number - 1]}"
                    for number in range(start, end + 1)
                ]
        if file_path:
            diagnostics["static_js_syntax_error"] = {
                "file_path": file_path,
                "line": line_number,
                "syntax_error": syntax_line,
                "snippet": snippet_lines,
                "required_action": "Patch this exact JavaScript file so node --check passes; do not spend the next turn rewriting unrelated pages.",
            }
        return diagnostics

    @classmethod
    def _extract_generated_app_test_diagnostics(cls, logs: list[str], test_file: Path | None = None) -> dict[str, object]:
        diagnostics: dict[str, object] = {}
        failing_test = next(
            (
                match.group("name")
                for line in logs
                if (match := cls._UNITTEST_FAIL_RE.search(str(line or "")))
                or (match := cls._UNITTEST_ERROR_RE.search(str(line or "")))
            ),
            None,
        )
        if failing_test:
            diagnostics["failing_test_name"] = failing_test
        generated_js_locations = [
            {
                "line": int(match.group("line")),
                "column": int(match.group("column")),
            }
            for line in logs
            if (match := cls._GENERATED_JS_TEST_LOCATION_RE.search(str(line or "")))
        ]
        if generated_js_locations:
            first_location = generated_js_locations[0]
            diagnostics["failing_test_location"] = {
                "file_path": "miniapp/tests/generated_app.test.mjs",
                **first_location,
            }
            if test_file is not None and test_file.exists():
                try:
                    source_lines = test_file.read_text(encoding="utf-8").splitlines()
                except OSError:
                    source_lines = []
                line_no = int(first_location["line"])
                if 1 <= line_no <= len(source_lines):
                    assertion_line = source_lines[line_no - 1].strip()
                    diagnostics["assertion_source"] = {
                        "file_path": "miniapp/tests/generated_app.test.mjs",
                        "line": line_no,
                        "source": assertion_line,
                    }
                    literal_match = re.search(r"\.includes\(\s*([\"'`])(?P<literal>.+?)\1\s*\)", assertion_line)
                    if literal_match:
                        diagnostics["expected_literal"] = literal_match.group("literal")
                    start = max(0, line_no - 4)
                    end = min(len(source_lines), line_no + 3)
                    diagnostics["assertion_context"] = [
                        {
                            "line": index + 1,
                            "source": source_lines[index],
                        }
                        for index in range(start, end)
                    ]
        stack_excerpt = [str(line or "") for line in logs[-12:] if str(line or "").strip()]
        if stack_excerpt:
            diagnostics["stack_excerpt"] = stack_excerpt
        if any("miniapp/miniapp/app/" in str(line or "") for line in logs):
            diagnostics["js_test_path_root"] = {
                "problem": "generated_js_test_prefixed_miniapp_twice",
                "expected_root": "Generated JS tests run from cwd=miniapp; use app/static/<role>/... or resolve ../app/static from import.meta.url.",
            }
        if any(
            "ERR_INVALID_ARG_TYPE" in str(line or "") and "Received an instance of URL" in str(line or "")
            for line in logs
        ) or any("path.resolve(new URL" in str(line or "") for line in logs):
            diagnostics["js_test_url_path_api"] = {
                "problem": "generated_js_test_passed_url_to_path_api",
                "expected_path_api": (
                    "In generated_app.test.mjs, path/fs APIs need strings. Use path.join(process.cwd(), 'app/static/...') "
                    "or fileURLToPath(new URL('../app/static/...', import.meta.url)); never pass a URL object directly to path.resolve/path.join/fs."
                ),
            }
        if any("not found in '<!doctype html>" in str(line or "").lower() for line in logs):
            diagnostics["server_rendered_html_assertion"] = {
                "problem": "test_asserts_js_rendered_text_in_server_html",
                "expected_scope": (
                    "FastAPI TestClient sees HTML before browser JavaScript runs. "
                    "Assert route/static shell in Python tests, include source text in HTML, or move JS-rendered item checks to generated_app.test.mjs."
                ),
            }
        if any("assert(html.includes(" in str(line or "") for line in logs):
            diagnostics["static_html_assertion"] = {
                "problem": "js_test_asserts_dynamic_text_only_in_html",
                "expected_scope": (
                    "If role data is rendered by JavaScript, generated_app.test.mjs should read the role JS/shared data source as well as HTML."
                ),
            }
        missing_role_pages = [
            str(match.group("role") or "").strip().lower()
            for line in logs
            if (match := cls._ROLE_PAGE_ASSERT_RE.search(str(line or "")))
        ]
        if missing_role_pages:
            diagnostics["missing_role_pages"] = list(dict.fromkeys(role for role in missing_role_pages if role))
        for line in reversed(logs):
            table_match = cls._SQLITE_MISSING_TABLE_RE.search(str(line or ""))
            if table_match:
                diagnostics["sqlite_missing_table"] = {
                    "table": str(table_match.group("table") or "").strip(),
                    "expected_fix": "If generated SQLAlchemy models are defined in route modules, call Base.metadata.create_all(bind=engine) after all model classes are imported/declared before TestClient requests run.",
                }
                break
        for line in reversed(logs):
            sqlite_match = cls._SQLITE_MISSING_COLUMN_RE.search(str(line or ""))
            if sqlite_match:
                diagnostics["sqlite_missing_column"] = {
                    "table": str(sqlite_match.group("table") or "").strip(),
                    "column": str(sqlite_match.group("column") or "").strip(),
                }
                break
        for line in reversed(logs):
            shared_state_match = cls._SHARED_STATE_UPDATE_RE.search(str(line or ""))
            if not shared_state_match:
                continue
            payload = str(shared_state_match.group("payload") or "").strip()
            resource_slug = cls._resource_slug_from_payload_keys(payload)
            diagnostics["shared_state_update_failure"] = {
                "record_id": str(shared_state_match.group("record_id") or "").strip(),
                "actor": str(shared_state_match.group("actor") or "").strip().lower(),
                "resource_slug": resource_slug or None,
                "payload_excerpt": payload[:700],
            }
            break
        for line in reversed(logs):
            persistence_match = cls._POST_PERSISTENCE_RE.search(str(line or ""))
            if not persistence_match:
                continue
            path = str(persistence_match.group("path") or "").strip()
            payload = str(persistence_match.group("payload") or "").strip()
            diagnostics["post_persistence_failure"] = {
                "path": path or None,
                "resource_slug": cls._resource_slug_from_api_path(path) or cls._resource_slug_from_payload_keys(payload) or None,
                "payload_excerpt": payload[:700] or None,
                "expected_behavior": "A generated create API must persist the POSTed record so a later GET returns it.",
            }
            break
        for line in reversed(logs):
            match = cls._API_FAILURE_RE.search(str(line or ""))
            if not match:
                continue
            path = str(match.group("path") or "").strip()
            segments = [segment for segment in path.strip("/").split("/") if segment]
            resource_slug = segments[1] if len(segments) >= 2 and segments[0] == "api" else ""
            diagnostics["api_failure"] = {
                "label": str(match.group("label") or "").strip().lower().replace(" ", "_"),
                "method": str(match.group("method") or "").strip().upper() or None,
                "path": path,
                "status_code": int(match.group("status") or 0),
                "request_payload": str(match.group("payload") or "").strip() or None,
                "response_body": str(match.group("body") or "").strip() or None,
                "resource_slug": resource_slug or None,
            }
            break
        return diagnostics

    @classmethod
    def _extract_backend_import_diagnostics(cls, logs: list[str]) -> dict[str, object]:
        text = "\n".join(str(line or "") for line in logs)
        diagnostics: dict[str, object] = {}
        if cls._FASTAPI_SESSION_RESPONSE_FIELD_RE.search(text):
            diagnostics["fastapi_session_dependency_error"] = {
                "problem": "FastAPI treated a SQLAlchemy Session parameter as a request/response field.",
                "expected_fix": (
                    "Patch generated route functions so every parameter typed Session uses "
                    "Depends(get_db_session) or Depends(get_db). Do not use next(get_db_session()), "
                    "SessionLocal(), @contextmanager, or a live Session object as a default argument."
                ),
            }
        return diagnostics

    @staticmethod
    def _resource_slug_from_api_path(path: str) -> str:
        segments = [segment for segment in str(path or "").strip("/").split("/") if segment]
        if len(segments) >= 2 and segments[0] == "api":
            return segments[1]
        return ""

    @staticmethod
    def _resource_slug_from_payload_keys(payload: str) -> str:
        for raw_key in re.findall(r"['\"]([A-Za-z0-9_]+)_id['\"]", str(payload or "")):
            stem = re.sub(r"[^a-z0-9_]+", "", raw_key.lower()).strip()
            if not stem or stem in {"id", "record", "item"}:
                continue
            return stem if stem.endswith("s") else f"{stem}s"
        return ""

    @staticmethod
    def _filter_build_issues(issues: list[ValidationIssue], scope_mode: str) -> list[ValidationIssue]:
        if scope_mode not in {"minimal_patch", "role_partial_build", "fix_agentic", "agentic"}:
            return issues
        ignored_prefixes = ("build.placeholder_",)
        ignored_codes = {"build.missing_entrypoint"}
        return [
            issue
            for issue in issues
            if not issue.code.startswith(ignored_prefixes) and issue.code not in ignored_codes
        ]
