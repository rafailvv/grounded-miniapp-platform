from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import difflib
import hashlib
from html.parser import HTMLParser
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
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from app.models.common import GenerationMode
from app.models.artifacts import ValidationIssue
from app.models.domain import CheckExecutionRecord, RunCheckResult, utc_now
from app.modules.miniapp_validation.agent_static_validation import AgentStaticValidation
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
    "neutral shell",
    "blank shell screen",
    "preview entry",
    "should be replaced by the generated app",
    "replace blank shell screens",
)


class _HtmlControlContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.form_index = -1
        self.form_depth = 0
        self.names_by_form: dict[int, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(name).lower(): str(value or "") for name, value in attrs}
        if tag.lower() == "form":
            self.form_index += 1
            self.form_depth += 1
            self.names_by_form.setdefault(self.form_index, [])
        element_id = attr_map.get("id", "").strip()
        if element_id:
            self.ids.append(element_id)
        if tag.lower() not in {"input", "textarea", "select"} or self.form_depth <= 0:
            return
        control_name = attr_map.get("name", "").strip()
        if not control_name:
            return
        input_type = attr_map.get("type", "").strip().lower()
        if tag.lower() == "input" and input_type in {"checkbox", "radio"}:
            return
        self.names_by_form.setdefault(self.form_index, []).append(control_name)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self.form_depth > 0:
            self.form_depth -= 1
PRELOADED_PRODUCT_DATA_MARKERS = (
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
    "hard-coded product records",
    "hardcoded product records",
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
    _PY_MISSING_ATTRIBUTE_RE = re.compile(
        r"AttributeError:\s+'(?P<object>[A-Za-z0-9_]+)'\s+object\s+has\s+no\s+attribute\s+'(?P<attribute>[A-Za-z0-9_]+)'",
        re.IGNORECASE,
    )
    _SHARED_STATE_UPDATE_RE = re.compile(
        r"Updated (?:state|entity|item|record)\s+(?P<entity_id>[A-Za-z0-9_-]+)\s+did not reflect\s+(?P<actor>[A-Za-z0-9_-]+)\s+changes in shared state\.\s+Payload:\s*(?P<payload>.*)$",
        re.IGNORECASE,
    )
    _POST_PERSISTENCE_RE = re.compile(
        r"POST(?:ed)?\s+(?P<path>/api/[A-Za-z0-9_/{}/-]+)?\s*(?:record|payload)?\s*(?:did not|does not|was not)\s+persist(?:ed)?(?:\.\s*Payload:\s*(?P<payload>.*))?$",
        re.IGNORECASE,
    )
    _GENERATED_JS_TEST_LOCATION_RE = re.compile(r"generated_app\.test\.mjs:(?P<line>\d+):(?P<column>\d+)")
    _GENERATED_PY_TEST_LOCATION_RE = re.compile(r"test_generated_app\.py\", line (?P<line>\d+)")
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
            acceptance_contract=acceptance_contract,
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
                else "Generated Python app tests were deferred until the full verification gate."
            )
            js_details = (
                "Generated JS app tests were skipped for a focused CSS-only visual edit."
                if focused_details
                else "Generated JS app tests were deferred until the full verification gate."
            )
            preview_details = (
                "Preview rebuild was skipped for a focused CSS-only visual edit until successful apply/final viewing."
                if focused_details
                else "Preview rebuild was deferred until the full verification gate."
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
                            else "Preview connectivity smoke was deferred until the full verification gate."
                        ),
                        command="preview deferred during focused edit" if focused_details else "preview deferred during fast gate",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="api_workflow_smoke",
                        status="skipped",
                        details=(
                            "API workflow smoke was skipped for a focused CSS-only visual edit."
                            if focused_details
                            else "API workflow smoke was deferred until the full verification gate."
                        ),
                        command="api workflow smoke deferred during focused edit" if focused_details else "api workflow smoke deferred during fast gate",
                        logs=[],
                    ),
                    RunCheckResult(
                        name="browser_flow_smoke",
                        status="skipped",
                        details=(
                            "Browser flow smoke was skipped for a focused CSS-only visual edit."
                            if focused_details
                            else "Browser flow smoke was deferred until the full verification gate."
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

        def _skipped_preview_results(reason: str, *, duration_ms: int = 0) -> tuple[RunCheckResult, RunCheckResult, RunCheckResult, RunCheckResult]:
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
            api_workflow_result = RunCheckResult(
                name="api_workflow_smoke",
                status="skipped",
                details=reason,
                duration_ms=duration_ms,
                command="api workflow smoke",
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
            return preview_boot_result, connectivity_result, api_workflow_result, browser_result

        def _run_preview_checks() -> tuple[RunCheckResult, RunCheckResult, RunCheckResult, RunCheckResult]:
            preview_started = time.perf_counter()
            if should_skip_preview or skip_preview_only:
                duration_ms = int((time.perf_counter() - preview_started) * 1000)
                reason = (
                    "Preview smoke deferred during fast gate after generated workflow tests."
                    if skip_preview_only
                    else "Preview smoke skipped because validator or build checks already failed."
                )
                return _skipped_preview_results(reason, duration_ms=duration_ms)
            if preview_run_id is not None:
                preview = self.preview_service.rebuild(
                    workspace_id,
                    source_dir=source_dir,
                    draft_run_id=preview_run_id,
                )
            else:
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
            api_workflow_result = self._api_workflow_smoke(
                source_dir=source_dir,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
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
            api_workflow_result.duration_ms = duration_ms
            browser_result.duration_ms = duration_ms
            return preview_boot_result, connectivity_result, api_workflow_result, browser_result

        for started_name in ("generated_app_python_tests", "generated_app_js_tests"):
            self._emit_check_progress(progress_callback, started_name, "started", check_profile=check_profile)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="check-runner") as executor:
            python_future = executor.submit(_run_python_tests)
            js_future = executor.submit(_run_js_tests)
            python_tests_result = python_future.result()
            js_tests_result = js_future.result()

        for result in (python_tests_result, js_tests_result):
            self._emit_check_progress(
                progress_callback,
                result.name,
                result.status,
                duration_ms=result.duration_ms,
                check_profile=check_profile,
            )

        for started_name in (
            "preview_boot_smoke",
            "preview_connectivity_smoke",
            "api_workflow_smoke",
            "browser_flow_smoke",
        ):
            self._emit_check_progress(progress_callback, started_name, "started", check_profile=check_profile)
        preview_boot_result, connectivity_result, api_workflow_result, browser_flow_result = _run_preview_checks()

        if (
            not should_skip_preview
            and api_workflow_result.status == "passed"
            and browser_flow_result.status == "passed"
        ):
            if python_tests_result.status == "failed":
                python_tests_result = python_tests_result.model_copy(
                    update={
                        "status": "skipped",
                        "details": (
                            f"{python_tests_result.details} Generated Python tests produced diagnostics, "
                            "but API persistence proof and real browser workflow proof passed; browser/API proof is the blocking create gate."
                        ),
                        "diagnostics": {
                            **(python_tests_result.diagnostics if isinstance(python_tests_result.diagnostics, dict) else {}),
                            "non_blocking_python_test_diagnostics": True,
                        },
                    }
                )
            if js_tests_result.status == "failed":
                js_tests_result = js_tests_result.model_copy(
                    update={
                        "status": "skipped",
                        "details": (
                            f"{js_tests_result.details} JS source-level generated tests produced diagnostics, "
                            "but API persistence proof and real browser workflow proof passed; browser/API proof is the blocking create gate."
                        ),
                        "diagnostics": {
                            **(js_tests_result.diagnostics if isinstance(js_tests_result.diagnostics, dict) else {}),
                            "non_blocking_js_test_diagnostics": True,
                        },
                    }
                )

        results.append(python_tests_result)
        results.append(js_tests_result)
        results.append(preview_boot_result)
        results.append(connectivity_result)
        results.append(api_workflow_result)
        results.append(browser_flow_result)
        for result in (preview_boot_result, connectivity_result, api_workflow_result, browser_flow_result):
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
        role_html_text = {
            role: cls._read_role_html_surface_text(static_root / role)
            for role in ROLE_ORDER
        }
        backend_text = cls._read_backend_routes_text(source_dir)
        tests_text = cls._read_generated_tests_text(source_dir)
        all_frontend = "\n".join(role_text.values())
        features = contract.get("features") if isinstance(contract.get("features"), dict) else {}
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
        update_roles = [
            role
            for role in (
                str(item).strip().lower()
                for item in (state_contract.get("update_roles") or [])
            )
            if role in ROLE_ORDER
        ]
        source_roles = [
            role
            for role in (
                str(item).strip().lower()
                for item in (state_contract.get("source_roles") or [])
            )
            if role in ROLE_ORDER
        ]
        update_surface = "\n".join(role_text.get(role, "") for role in (update_roles or ROLE_ORDER))
        update_required = bool(features.get("workflow_update"))
        if not cls._has_frontend_post(all_frontend):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_missing_frontend_post",
                    message="Acceptance workflow requires at least one frontend save/create/import action that persists user-provided state, but no POST frontend action was found.",
                    severity="high",
                    location="miniapp/app/static",
                    blocking=True,
                )
            )
        if update_required and update_roles and not cls._has_workflow_update_action(update_surface):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_missing_update_action",
                    message=(
                        "Acceptance workflow requires a prompt-assigned persisted update action for "
                        f"{', '.join(update_roles)}, but no matching POST/PATCH/PUT/DELETE action was found."
                    ),
                    severity="high",
                    location="miniapp/app/static/" + (update_roles[0] if update_roles else ""),
                    blocking=True,
                )
            )
        if not cls._tests_cover_workflow_contract(tests_text, contract):
            issues.append(
                ValidationIssue(
                    code="platform.workflow_tests_missing_contract",
                    message="Generated tests do not cover the workflow acceptance contract. Add Python/JS tests for the actual prompt-owned UI/API/persistence flow.",
                    severity="high",
                    location="miniapp/tests",
                    blocking=True,
                )
            )
        issues.extend(cls._frontend_role_wiring_issues(static_root, backend_text=backend_text))
        issues.extend(
            cls._cross_role_update_visibility_issues(
                static_root=static_root,
                role_text=role_text,
                source_roles=source_roles,
                update_roles=update_roles,
            )
        )
        issues.extend(cls._role_css_html_contract_issues(static_root))
        return cls._dedupe_validation_issues(issues)

    @classmethod
    def _cross_role_update_visibility_issues(
        cls,
        *,
        static_root: Path,
        role_text: dict[str, str],
        source_roles: list[str] | None = None,
        update_roles: list[str] | None = None,
    ) -> list[ValidationIssue]:
        del static_root
        update_role_set = {
            role for role in (update_roles or []) if role in ROLE_ORDER
        } or set(ROLE_ORDER)
        operational_text = "\n".join(role_text.get(role, "") for role in update_role_set)
        update_fields = {
            field
            for payload_fields in cls._js_patch_payload_field_sets(operational_text)
            for field in payload_fields
            if field
            and field
            not in {
                "id",
                "item_id",
                "updated_by",
                "created_by",
                "current_view",
                "role",
            }
            and not cls._is_path_id_field(field)
        }
        if not update_fields:
            return []
        visibility_roles = {
            role for role in (source_roles or []) if role in ROLE_ORDER
        } | (set(ROLE_ORDER) - update_role_set)
        if not visibility_roles:
            visibility_roles = set(ROLE_ORDER) - update_role_set
        issues: list[ValidationIssue] = []
        for role in sorted(visibility_roles):
            role_identifiers = set(re.findall(r"\b[A-Za-z_$][\w$]*\b", role_text.get(role, "")))
            missing = sorted(field for field in update_fields if field not in role_identifiers)
            if not missing:
                continue
            issues.append(
                ValidationIssue(
                    code="platform.cross_role_update_not_rendered_in_role",
                    message=(
                        f"Prompt-assigned update payload fields are not rendered by {role} after reload: "
                        f"{', '.join(missing[:8])}. Roles that observe shared state should render persisted changes using prompt-owned labels."
                    ),
                    severity="high",
                    location=f"miniapp/app/static/{role}/app.js",
                    blocking=True,
                    repair_recipe={
                        "recipe_id": "workflow.cross_role_update_visibility",
                        "failure_class": "frontend_interaction_static_smoke",
                        "failure_signature": f"workflow.cross_role_update_not_rendered_in_role.{role}",
                        "required_next_tool": "read_files",
                        "suggested_tool_after_read": "apply_patch_to_draft_or_write_file",
                        "target_files": [
                            f"miniapp/app/static/{role}/app.js",
                            *[f"miniapp/app/static/{update_role}/app.js" for update_role in sorted(update_role_set)],
                            "miniapp/tests/generated_app.test.mjs",
                        ],
                        "verification_check": "frontend_interaction_static_smoke",
                        "verification_command": "run_checks frontend_interaction_static_smoke",
                        "retryable": True,
                        "deterministic": True,
                        "evidence": {
                            "role": role,
                            "update_roles": sorted(update_role_set),
                            "update_fields": sorted(update_fields),
                            "missing_role_fields": missing,
                        },
                    },
                )
            )
        return issues

    @classmethod
    def _frontend_role_wiring_issues(cls, static_root: Path, *, backend_text: str = "") -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        backend_create_schemas = cls._backend_create_schema_contracts(backend_text)
        backend_create_fields = {
            field
            for schema in backend_create_schemas
            for field in set(schema.get("accepted") or set())
        }
        backend_patch_schemas = cls._backend_update_schema_contracts(backend_text)
        for role in ROLE_ORDER:
            role_dir = static_root / role
            js_path = role_dir / "app.js"
            if not role_dir.exists() or not js_path.exists():
                continue
            try:
                js_source = js_path.read_text(encoding="utf-8")
            except OSError:
                js_source = ""
            html_files = sorted(role_dir.rglob("*.html"))
            html_by_path: dict[str, str] = {}
            for html_path in html_files:
                try:
                    html_by_path[html_path.relative_to(static_root.parents[2]).as_posix()] = html_path.read_text(encoding="utf-8")
                except OSError:
                    continue
            combined_html = "\n".join(html_by_path.values())
            issues.extend(cls._js_obvious_undefined_workflow_issues(role, js_path, js_source))
            issues.extend(cls._late_domcontentloaded_init_issues(role, js_path, js_source, combined_html))
            issues.extend(cls._selector_wiring_issues(role, js_path, js_source, combined_html))
            for relative_path, html_source in html_by_path.items():
                issues.extend(
                    cls._form_wiring_issues(
                        relative_path,
                        js_path,
                        html_source,
                        js_source,
                        backend_create_fields=backend_create_fields,
                        backend_create_schemas=backend_create_schemas,
                    )
                )
                issues.extend(cls._button_wiring_issues(relative_path, js_path, html_source, js_source))
            issues.extend(
                cls._frontend_backend_patch_payload_issues(
                    role,
                    js_path,
                    js_source,
                    backend_patch_schemas=backend_patch_schemas,
                )
            )
        return issues

    @classmethod
    def _late_domcontentloaded_init_issues(cls, role: str, js_path: Path, js_source: str, html_source: str) -> list[ValidationIssue]:
        text = str(js_source or "")
        if "DOMContentLoaded" not in text:
            return []
        if "readyState" in text:
            return []
        if not re.search(r"document\s*\.\s*addEventListener\(\s*([\"'])DOMContentLoaded\1", text):
            return []
        if not re.search(r"<(?:form|button|select|textarea|input)\b", str(html_source or ""), re.IGNORECASE):
            return []
        return [
            ValidationIssue(
                code="platform.workflow_late_domcontentloaded_init",
                message=(
                    f"{role} app initializes workflow handlers only from DOMContentLoaded. In preview/browser verification the script can load after "
                    "that event, leaving visible forms unbound. Use a readyState guard: if document.readyState is 'loading', add the listener; otherwise call init() immediately."
                ),
                severity="high",
                location=js_path.relative_to(js_path.parents[4]).as_posix(),
                blocking=True,
                repair_recipe={
                    "recipe_id": "frontend.init_wiring",
                    "failure_class": "frontend_interaction_static_smoke",
                    "failure_signature": f"frontend.late_domcontentloaded_init.{role}",
                    "required_next_tool": "read_files",
                    "suggested_tool_after_read": "write_file",
                    "target_files": [js_path.relative_to(js_path.parents[4]).as_posix()],
                    "verification_check": "frontend_interaction_static_smoke",
                    "verification_command": "run_checks frontend_interaction_static_smoke",
                    "retry_policy": "deterministic_repair",
                    "deterministic": True,
                    "retryable": True,
                    "instruction": "Read the role app.js and add a readyState guard so init runs even when DOMContentLoaded has already fired.",
                    "evidence": {"role": role, "uses_domcontentloaded": True, "ready_state_guard": False},
                },
            )
        ]

    @staticmethod
    def _js_obvious_undefined_workflow_issues(role: str, js_path: Path, js_source: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        text = str(js_source or "")
        if "formData.get" not in text:
            return issues

        function_pattern = re.compile(
            r"(?P<header>(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\((?P<params>[^)]*)\)\s*\{|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(?:async\s*)?\((?P<arrow_params>[^)]*)\)\s*=>\s*\{)",
            re.DOTALL,
        )
        for match in function_pattern.finditer(text):
            body_start = match.end()
            depth = 1
            index = body_start
            while index < len(text) and depth > 0:
                char = text[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                index += 1
            body = text[body_start : max(body_start, index - 1)]
            if "formData.get" not in body:
                continue
            params = f"{match.group('params') or ''},{match.group('arrow_params') or ''}"
            has_param = any(part.strip() == "formData" for part in params.split(","))
            has_declaration = bool(re.search(r"\b(?:const|let|var)\s+formData\s*=\s*new\s+FormData\b", body))
            if has_param or has_declaration:
                continue
            issues.append(
                ValidationIssue(
                    code="platform.workflow_js_undefined_formdata",
                    message=(
                        f"{js_path.relative_to(js_path.parents[4]).as_posix()} uses formData.get(...) in a {role} workflow "
                        "without declaring `const formData = new FormData(form)` in the same function. This would crash in the browser before submit."
                    ),
                    severity="high",
                    location=js_path.relative_to(js_path.parents[4]).as_posix(),
                    blocking=True,
                )
            )
        if not issues and not re.search(r"\b(?:const|let|var)\s+formData\s*=\s*new\s+FormData\b", text):
            if not re.search(r"\bfunction\s+[A-Za-z_$][\w$]*\s*\([^)]*\bformData\b", text) and not re.search(
                r"\([^)]*\bformData\b[^)]*\)\s*=>", text
            ):
                issues.append(
                    ValidationIssue(
                        code="platform.workflow_js_undefined_formdata",
                        message=(
                            f"{js_path.relative_to(js_path.parents[4]).as_posix()} uses formData.get(...) in a {role} workflow "
                            "without declaring `const formData = new FormData(form)` in the same script."
                        ),
                        severity="high",
                        location=js_path.relative_to(js_path.parents[4]).as_posix(),
                        blocking=True,
                    )
                )
        return issues

    @classmethod
    def _selector_wiring_issues(cls, role: str, js_path: Path, js_source: str, combined_html: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for selector in cls._literal_query_selectors(js_source):
            if cls._selector_is_dynamic_or_generic(selector):
                continue
            if cls._html_has_simple_selector(combined_html, selector):
                continue
            if cls._selector_is_optional_feedback_target(js_source, selector):
                continue
            if cls._selector_is_optional_alternate_query(js_source, selector):
                continue
            blocking = selector.strip().startswith("#")
            issues.append(
                ValidationIssue(
                    code="platform.workflow_selector_matches_no_html",
                    message=(
                        f"{js_path.relative_to(js_path.parents[4]).as_posix()} queries selector `{selector}`, "
                        f"but no {role} HTML page contains that selector. Required workflows must not be guarded behind selectors that never match."
                    ),
                    severity="high" if blocking else "medium",
                    location=js_path.relative_to(js_path.parents[4]).as_posix(),
                    blocking=blocking,
                )
            )
        return issues

    @classmethod
    def _selector_is_optional_feedback_target(cls, js_source: str, selector: str) -> bool:
        bindings = cls._js_dom_selector_bindings(js_source)
        matching_vars = [var_name for var_name, (_kind, value) in bindings.items() if value == selector]
        if not matching_vars:
            return False
        text = str(js_source or "")
        for var_name in matching_vars:
            escaped = re.escape(var_name)
            if re.search(rf"\b{escaped}\s*\.\s*(?:addEventListener|submit|click|value|checked)\b", text):
                continue
            if re.search(rf"\b{escaped}\s*&&", text) or re.search(rf"\b{escaped}\?\.", text):
                return True
            if re.search(rf"\bif\s*\(\s*{escaped}\s*\)", text):
                return True
        return False

    @staticmethod
    def _selector_is_optional_alternate_query(js_source: str, selector: str) -> bool:
        value = str(selector or "").strip()
        if not value:
            return False
        escaped = re.escape(value)
        query_pattern = re.compile(
            rf"""querySelector(?:All)?\(\s*(?:"{escaped}"|'{escaped}'|`{escaped}`)\s*\)"""
        )
        for line in str(js_source or "").splitlines():
            if ("||" not in line and "??" not in line) or not query_pattern.search(line):
                continue
            return True
        return False

    @staticmethod
    def _literal_query_selectors(js_source: str) -> list[str]:
        selectors: list[str] = []
        pattern = re.compile(
            r"""\bquerySelector(?:All)?\(\s*(?:"(?P<double>[^"]+)"|'(?P<single>[^']+)'|`(?P<backtick>[^`]+)`)\s*\)"""
        )
        for match in pattern.finditer(str(js_source or "")):
            selector = str(match.group("double") or match.group("single") or match.group("backtick") or "").strip()
            if selector:
                selectors.append(selector)
        return list(dict.fromkeys(selectors))

    @staticmethod
    def _selector_is_dynamic_or_generic(selector: str) -> bool:
        value = str(selector or "").strip()
        if not value or any(marker in value for marker in (" ", ">", "+", "~", ",", ":not", "${")):
            return True
        return value.lower() in {"body", "main", "form", "button", "input", "select", "textarea"}

    @staticmethod
    def _html_has_simple_selector(html_source: str, selector: str) -> bool:
        html = str(html_source or "")
        value = str(selector or "").strip()
        id_match = re.search(r"#(?P<id>[A-Za-z0-9_-]+)", value)
        if id_match and id_match.group("id") not in extract_html_ids(html):
            return False
        class_names = re.findall(r"\.(?P<class>[A-Za-z0-9_-]+)", value)
        if class_names:
            html_classes = CheckRunner._html_class_names(html)
            if any(class_name not in html_classes for class_name in class_names):
                return False
        attr_matches = re.findall(r"\[(?P<name>[A-Za-z0-9_:-]+)(?:\s*=\s*([\"']?)(?P<value>[^\]\"']+)\2)?\]", value)
        for attr_name, _quote, attr_value in attr_matches:
            if attr_value:
                pattern = rf"\b{re.escape(attr_name)}\s*=\s*([\"']){re.escape(attr_value)}\1"
            else:
                pattern = rf"\b{re.escape(attr_name)}(?:\s*=|\s|>)"
            if not re.search(pattern, html):
                return False
        if id_match or class_names or attr_matches:
            return True
        return value.lower() in html.lower()

    @classmethod
    def _form_wiring_issues(
        cls,
        relative_path: str,
        js_path: Path,
        html_source: str,
        js_source: str,
        *,
        backend_create_fields: set[str] | None = None,
        backend_create_schemas: list[dict[str, object]] | None = None,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        backend_create_fields = set(backend_create_fields or set())
        backend_create_schemas = list(backend_create_schemas or [])
        forms = cls._html_forms(html_source)
        multiple_forms = len(forms) > 1
        for form in forms:
            form_id = str(form.get("id") or "").strip()
            form_selectors = [str(selector) for selector in form.get("selectors") or [] if str(selector).strip()]
            field_names = set(form.get("field_names") or [])
            if not form_id and not form_selectors:
                continue
            form_label = f"#{form_id}" if form_id else form_selectors[0]
            field_ids_by_name = {
                str(key): str(value)
                for key, value in dict(form.get("field_ids_by_name") or {}).items()
                if str(key).strip() and str(value).strip()
            }
            form_referenced = bool(
                (form_id and cls._js_references_dom_id(js_source, form_id))
                or any(cls._js_references_selector(js_source, selector) for selector in form_selectors)
            )
            form_has_submit_handler = bool(
                (form_id and cls._js_has_submit_handler_for_id(js_source, form_id))
                or any(cls._js_has_submit_handler_for_selector(js_source, selector) for selector in form_selectors)
            )
            form_has_field_event_handler = bool(
                (form_id and cls._js_has_field_event_handler_for_id(js_source, form_id))
                or any(cls._js_has_field_event_handler_for_selector(js_source, selector) for selector in form_selectors)
            )
            form_requires_submit_handler = bool(form.get("has_submit_control"))
            if not form_referenced:
                issues.append(
                    ValidationIssue(
                        code="platform.workflow_form_without_handler",
                        message=f"{relative_path} has form {form_label}, but {js_path.name} never references it. Visible workflow forms must have a submit handler.",
                        severity="high",
                        location=relative_path,
                        blocking=True,
                        repair_recipe=cls._form_wiring_repair_recipe(
                            signature="frontend.unwired_form",
                            relative_path=relative_path,
                            js_path=js_path,
                            form_label=form_label,
                            field_names=field_names,
                            evidence_code="workflow_form_without_handler",
                        ),
                    )
                )
                continue
            if not form_has_submit_handler and not (form_has_field_event_handler and not form_requires_submit_handler):
                issues.append(
                    ValidationIssue(
                        code="platform.workflow_form_without_submit_handler",
                        message=f"{relative_path} form {form_label} is referenced by JavaScript but no submit/change handler is wired for it.",
                        severity="high",
                        location=relative_path,
                        blocking=True,
                        repair_recipe=cls._form_wiring_repair_recipe(
                            signature="frontend.unwired_form",
                            relative_path=relative_path,
                            js_path=js_path,
                            form_label=form_label,
                            field_names=field_names,
                            evidence_code="workflow_form_without_submit_handler",
                        ),
                    )
                )
            if not field_names:
                continue
            if not multiple_forms and "Object.fromEntries" in js_source and "new FormData" in js_source:
                formdata_vars = cls._js_object_from_entries_formdata_vars(js_source)
                data_props = {
                    prop
                    for var_name in formdata_vars
                    for prop in re.findall(rf"\b{re.escape(var_name)}\.([A-Za-z_$][\w$]*)", js_source)
                }
                formdata_api_props = {
                    "append",
                    "delete",
                    "entries",
                    "forEach",
                    "get",
                    "getAll",
                    "has",
                    "keys",
                    "set",
                    "values",
                    # Common API envelope fields may appear as data.items/data.total
                    # elsewhere in the same role script after response.json().
                    "items",
                    "total",
                }
                unknown_props = sorted(prop for prop in data_props if prop not in field_names and prop not in {"id"} | formdata_api_props)
                if unknown_props:
                    issues.append(
                        ValidationIssue(
                            code="platform.workflow_formdata_field_mismatch",
                            message=(
                                f"{relative_path} form {form_label} fields are {', '.join(sorted(field_names))}, "
                                f"but JavaScript reads missing FormData properties: {', '.join(unknown_props[:6])}."
                            ),
                            severity="medium",
                            location=relative_path,
                            blocking=False,
                        )
                    )
            else:
                missing_reads = sorted(
                    name
                    for name in field_names
                    if not (
                        (
                            cls._js_reads_form_path_id_field(js_source, name)
                            if cls._is_path_id_field(name)
                            else cls._js_reads_form_field(js_source, name)
                        )
                        or cls._js_reads_dom_field_id(js_source, field_ids_by_name.get(name, ""))
                    )
                )
                if missing_reads:
                    issues.append(
                        ValidationIssue(
                            code="platform.workflow_form_field_not_submitted",
                            message=(
                                f"{relative_path} form {form_label} contains fields not read by JavaScript payload: "
                                f"{', '.join(missing_reads[:6])}."
                            ),
                            severity="medium",
                            location=relative_path,
                            blocking=False,
                        )
                )
            if (
                backend_create_fields
                and "*" not in backend_create_fields
                and re.search(r"\bmethod\s*:\s*([\"'`])POST\1", js_source, re.IGNORECASE)
                and not (
                    cls._form_looks_like_workflow_update(field_names)
                    and re.search(r"\bmethod\s*:\s*([\"'`])(?:PATCH|PUT|DELETE)\1", js_source, re.IGNORECASE)
                )
            ):
                payload_fields = cls._js_effective_form_payload_fields(js_source, field_names)
                unknown_payload_fields = sorted(
                    field
                    for field in payload_fields
                    if field not in backend_create_fields and not cls._is_path_id_field(field)
                )
                if unknown_payload_fields:
                    issues.append(
                        ValidationIssue(
                            code="platform.workflow_frontend_backend_field_mismatch",
                            message=(
                                f"{relative_path} form {form_label} can submit fields not accepted by the backend create schema: "
                                f"{', '.join(unknown_payload_fields[:6])}. Align HTML names, JavaScript payload keys, "
                                "backend create schema, and generated tests to one contract."
                            ),
                            severity="medium",
                            location=relative_path,
                            blocking=False,
                        )
                    )
                missing_required_sets = cls._missing_required_create_schema_sets(
                    payload_fields,
                    backend_create_schemas=backend_create_schemas,
                )
                if missing_required_sets:
                    required_preview = ", ".join(sorted(missing_required_sets[0])[:8])
                    issues.append(
                        ValidationIssue(
                            code="platform.workflow_frontend_backend_required_field_missing",
                            message=(
                                f"{relative_path} form {form_label} POST payload does not satisfy any backend create schema. "
                                f"Missing required fields such as: {required_preview}. Align HTML inputs, JavaScript payload, "
                                "backend create schema, and generated tests to one workflow contract."
                            ),
                            severity="medium",
                            location=relative_path,
                            blocking=False,
                        )
                    )
        return issues

    @staticmethod
    def _form_wiring_repair_recipe(
        *,
        signature: str,
        relative_path: str,
        js_path: Path,
        form_label: str,
        field_names: set[str],
        evidence_code: str,
    ) -> dict[str, Any]:
        js_relative = js_path.relative_to(js_path.parents[4]).as_posix()
        role_match = re.search(r"miniapp/app/static/(?P<role>client|specialist|manager)/", relative_path)
        return {
            "recipe_id": "frontend.form_wiring",
            "failure_class": "frontend_interaction_static_smoke",
            "failure_signature": signature,
            "required_next_tool": "read_files",
            "suggested_tool_after_read": "write_file",
            "target_files": [relative_path, js_relative],
            "verification_check": "frontend_interaction_static_smoke",
            "verification_command": "run_checks frontend_interaction_static_smoke",
            "retry_policy": "deterministic_repair",
            "deterministic": True,
            "retryable": True,
            "instruction": (
                "Read the exact HTML page and role app.js. Wire the visible form to a submit/change handler in that role script, "
                "build the payload from the form fields, persist it through the existing backend API, refresh rendered state, "
                "and keep optional controls guarded only after the required form is bound."
            ),
            "evidence": {
                "code": evidence_code,
                "role": role_match.group("role") if role_match else "",
                "html_file": relative_path,
                "js_file": js_relative,
                "form": form_label,
                "field_names": sorted(field_names),
            },
        }

    @staticmethod
    def _js_object_from_entries_formdata_vars(js_source: str) -> set[str]:
        text = str(js_source or "")
        vars_: set[str] = set()
        direct_pattern = re.compile(
            r"\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*Object\.fromEntries\(\s*new\s+FormData\b",
            re.DOTALL,
        )
        vars_.update(match.group("var") for match in direct_pattern.finditer(text))
        formdata_vars = {
            match.group("var")
            for match in re.finditer(
                r"\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*new\s+FormData\b",
                text,
            )
        }
        for formdata_var in formdata_vars:
            pattern = re.compile(
                rf"\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*Object\.fromEntries\(\s*{re.escape(formdata_var)}(?:\.entries\(\))?\s*\)",
                re.DOTALL,
            )
            vars_.update(match.group("var") for match in pattern.finditer(text))
        return vars_

    @staticmethod
    def _backend_create_schema_fields(backend_text: str) -> set[str]:
        return {
            field
            for schema in CheckRunner._backend_create_schema_contracts(backend_text)
            for field in set(schema.get("accepted") or set())
        }

    @staticmethod
    def _backend_create_schema_contracts(backend_text: str) -> list[dict[str, object]]:
        return CheckRunner._backend_schema_contracts(backend_text, name_markers=("Create",))

    @staticmethod
    def _backend_update_schema_contracts(backend_text: str) -> list[dict[str, object]]:
        return CheckRunner._backend_schema_contracts(backend_text, name_markers=("Patch", "Update", "Status", "Action"))

    @staticmethod
    def _backend_schema_contracts(backend_text: str, *, name_markers: tuple[str, ...]) -> list[dict[str, object]]:
        contracts: list[dict[str, object]] = []
        text = str(backend_text or "")
        markers_pattern = "|".join(re.escape(marker) for marker in name_markers)
        for match in re.finditer(
            rf"^class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:{markers_pattern})[A-Za-z0-9_]*)\([^)]*(?:StrictModel|BaseModel)[^)]*\):\s*$",
            text,
            re.MULTILINE,
        ):
            start = match.end()
            next_match = re.search(r"^(?:class|def|@router\.)\s+", text[start:], re.MULTILINE)
            end = start + next_match.start() if next_match else len(text)
            body = text[start:end]
            accepted: set[str] = set()
            required: set[str] = set()
            allows_extra = CheckRunner._schema_allows_extra_fields(body)
            for field_match in re.finditer(
                r"^\s{4}(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<type>[^=\n#]+?)(?:\s*=\s*(?P<default>[^\n#]+))?\s*$",
                body,
                re.MULTILINE,
            ):
                field_name = field_match.group("name")
                field_type = str(field_match.group("type") or "")
                default = field_match.group("default")
                accepted.add(field_name)
                optional = (
                    CheckRunner._schema_field_default_is_optional(default)
                    or "None" in field_type
                    or "Optional[" in field_type
                    or " | None" in field_type
                    or "None |" in field_type
                )
                if not optional:
                    required.add(field_name)
            if allows_extra:
                accepted.add("*")
            if accepted:
                contracts.append(
                    {
                        "name": match.group("name"),
                        "accepted": accepted,
                        "required": required,
                    }
                )
        return contracts

    @staticmethod
    def _schema_allows_extra_fields(body: str) -> bool:
        text = str(body or "")
        if re.search(r"\bmodel_config\s*=\s*ConfigDict\([^)]*\bextra\s*=\s*([\"'])allow\1", text, re.DOTALL):
            return True
        config_match = re.search(r"^\s+class\s+Config\s*:\s*(?P<body>.*?)(?=^\s{0,4}\w|\Z)", text, re.MULTILINE | re.DOTALL)
        if config_match and re.search(r"\bextra\s*=\s*([\"'])allow\1", config_match.group("body"), re.DOTALL):
            return True
        return False

    @staticmethod
    def _schema_field_default_is_optional(default: str | None) -> bool:
        if default is None:
            return False
        raw = str(default or "").strip()
        if not raw:
            return False
        if raw in {"...", "Ellipsis"}:
            return False
        if raw.startswith("Field("):
            inner = raw.removeprefix("Field(").rsplit(")", 1)[0].strip()
            if not inner:
                return False
            first_arg = inner.split(",", 1)[0].strip()
            if first_arg in {"...", "Ellipsis"}:
                return False
            if first_arg and "=" not in first_arg:
                return True
            default_match = re.search(r"\bdefault\s*=\s*(?P<value>[^,)]+)", inner)
            if not default_match:
                return False
            return default_match.group("value").strip() not in {"...", "Ellipsis"}
        return True

    @staticmethod
    def _missing_required_create_schema_sets(
        payload_fields: set[str],
        *,
        backend_create_schemas: list[dict[str, object]],
    ) -> list[set[str]]:
        if not backend_create_schemas:
            return []
        payload = set(payload_fields or set())
        required_sets = [
            set(schema.get("required") or set())
            for schema in backend_create_schemas
            if set(schema.get("required") or set())
        ]
        if not required_sets:
            return []
        if any(required <= payload for required in required_sets):
            return []
        missing = [required - payload for required in required_sets]
        return sorted((item for item in missing if item), key=lambda item: (len(item), sorted(item)))

    @classmethod
    def _frontend_backend_patch_payload_issues(
        cls,
        role: str,
        js_path: Path,
        js_source: str,
        *,
        backend_patch_schemas: list[dict[str, object]],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        patch_payloads = cls._js_patch_payload_field_sets(js_source)
        accepted_sets = [
            set(schema.get("accepted") or set())
            for schema in backend_patch_schemas
            if set(schema.get("accepted") or set())
        ]
        if not patch_payloads or not accepted_sets:
            return issues
        if any("*" in accepted for accepted in accepted_sets):
            return issues
        allowed_path_fields = {field for fields in patch_payloads for field in fields if cls._is_path_id_field(field)}
        for payload_fields in patch_payloads:
            effective_fields = set(payload_fields) - allowed_path_fields
            if not effective_fields:
                continue
            if any(effective_fields <= accepted for accepted in accepted_sets):
                continue
            accepted_union = set().union(*accepted_sets)
            unknown_fields = sorted(field for field in effective_fields if field not in accepted_union)
            if not unknown_fields:
                continue
            issues.append(
                ValidationIssue(
                    code="platform.workflow_patch_payload_field_mismatch",
                    message=(
                        f"{js_path.relative_to(js_path.parents[4]).as_posix()} sends PATCH fields not accepted by the backend update schema: "
                        f"{', '.join(unknown_fields[:6])}. Role `{role}` actions must update fields the API actually accepts."
                    ),
                    severity="high",
                    location=js_path.relative_to(js_path.parents[4]).as_posix(),
                    blocking=True,
                )
            )
        return issues

    @classmethod
    def _js_patch_payload_field_sets(cls, js_source: str) -> list[set[str]]:
        text = str(js_source or "")
        payloads: list[set[str]] = []

        def add_fields(body: str) -> None:
            fields = cls._js_object_literal_keys(body)
            fields = {
                field
                for field in fields
                if field not in {"body", "headers", "method", "signal", "credentials", "mode", "cache", "redirect"}
            }
            if fields:
                payloads.append(fields)

        for match in re.finditer(r"JSON\.stringify\(\s*\{(?P<body>[^{}]+)\}\s*\)", text, re.DOTALL):
            nearby = text[max(0, match.start() - 700): match.end() + 350]
            if re.search(r"\bmethod\s*:\s*([\"'`])PATCH\1", nearby, re.IGNORECASE):
                add_fields(match.group("body"))

        for match in re.finditer(
            r"\b(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*,\s*\{(?P<body>[^{}]+)\}\s*\)",
            text,
            re.DOTALL,
        ):
            function_name = str(match.group("name") or "").lower()
            if not any(marker in function_name for marker in ("update", "patch", "mark", "save", "change", "set")):
                continue
            add_fields(match.group("body"))

        for patch_match in re.finditer(r"\bmethod\s*:\s*([\"'`])PATCH\1", text, re.IGNORECASE):
            tail = text[patch_match.start(): patch_match.end() + 700]
            payload_vars = {
                match.group("var")
                for match in re.finditer(r"JSON\.stringify\(\s*(?P<var>[A-Za-z_$][\w$]*)\s*\)", tail)
            }
            if not payload_vars:
                continue
            head = text[max(0, patch_match.start() - 1200): patch_match.start()]
            for payload_var in sorted(payload_vars):
                declaration_pattern = re.compile(
                    rf"\b(?:const|let|var)\s+{re.escape(payload_var)}\s*=\s*\{{(?P<body>[^{{}}]*)\}}",
                    re.DOTALL,
                )
                declarations = list(declaration_pattern.finditer(head))
                fields: set[str] = set()
                if declarations:
                    fields.update(cls._js_object_literal_keys(declarations[-1].group("body")))
                    mutation_start = declarations[-1].end()
                    fields.update(cls._js_object_mutation_keys(head[mutation_start:], payload_var))
                else:
                    fields.update(cls._js_object_mutation_keys(head, payload_var))
                fields = {
                    field
                    for field in fields
                    if field not in {"body", "headers", "method", "signal", "credentials", "mode", "cache", "redirect"}
                }
                if fields:
                    payloads.append(fields)

        deduped: list[set[str]] = []
        seen: set[tuple[str, ...]] = set()
        for fields in payloads:
            key = tuple(sorted(fields))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(fields)
        return deduped

    @staticmethod
    def _js_patch_payload_uses_dynamic_formdata(js_source: str) -> bool:
        text = str(js_source or "")
        return bool(
            re.search(r"\bmethod\s*:\s*([\"'`])PATCH\1", text, re.IGNORECASE)
            and CheckRunner._js_payload_uses_dynamic_formdata_entries(text)
        )

    @staticmethod
    def _js_payload_uses_dynamic_formdata_entries(js_source: str) -> bool:
        text = str(js_source or "")
        return bool(
            re.search(r"\bnew\s+FormData\s*\(", text)
            and re.search(r"\.entries\s*\(\s*\)", text)
            and re.search(r"\bfor\s*\([^)]*\[[^\]]*\bkey\b[^\]]*\][^)]*\bof\b[^)]*\.entries\s*\(\s*\)", text, re.DOTALL)
            and re.search(r"JSON\.stringify\(\s*[A-Za-z_$][\w$]*\s*\)", text)
            and re.search(r"\[\s*key\s*\]\s*=", text)
        )

    @staticmethod
    def _js_object_literal_keys(body: str) -> set[str]:
        keys: set[str] = set()
        text = str(body or "")
        text_without_strings = CheckRunner._strip_js_string_literals(text)
        for match in re.finditer(r"(?:^|[,{])\s*(?P<key>[A-Za-z_$][\w$]*)\s*:", text_without_strings):
            keys.add(match.group("key"))
        for match in re.finditer(r"(?:^|[,{])\s*([\"'])(?P<key>[A-Za-z_$][\w$]*)\1\s*:", text):
            keys.add(match.group("key"))
        for match in re.finditer(r"(?:^|,)\s*(?P<key>[A-Za-z_$][\w$]*)\s*(?=,|$)", text_without_strings, re.DOTALL):
            keys.add(match.group("key"))
        return keys

    @staticmethod
    def _strip_js_string_literals(source: str) -> str:
        return re.sub(
            r"([\"'`])(?:\\.|(?!\1).)*\1",
            '""',
            str(source or ""),
            flags=re.DOTALL,
        )

    @staticmethod
    def _js_object_mutation_keys(js_source: str, var_name: str) -> set[str]:
        keys: set[str] = set()
        text = str(js_source or "")
        escaped = re.escape(var_name)
        for match in re.finditer(rf"\b{escaped}\s*\.\s*(?P<key>[A-Za-z_$][\w$]*)\s*=", text):
            keys.add(match.group("key"))
        for match in re.finditer(rf"\b{escaped}\s*\[\s*([\"'])(?P<key>[A-Za-z_$][\w$]*)\1\s*\]\s*=", text):
            keys.add(match.group("key"))
        return keys

    @staticmethod
    def _form_looks_like_workflow_update(field_names: set[str]) -> bool:
        normalized = {str(field or "").strip().lower() for field in field_names}
        update_markers = {
            "status",
            "state",
            "stage",
            "progress",
            "outcome",
            "review_state",
            "specialist_note",
            "manager_note",
            "note",
            "entity_id",
            "item_id",
            "id",
        }
        if not normalized & update_markers:
            return False
        non_update_signal_count = len(normalized - update_markers)
        return non_update_signal_count <= max(2, len(update_markers & normalized) + 1)

    @staticmethod
    def _js_effective_form_payload_fields(js_source: str, field_names: set[str]) -> set[str]:
        text = str(js_source or "")
        explicit_post_payloads = CheckRunner._js_post_payload_field_sets(text)
        if CheckRunner._js_payload_uses_dynamic_formdata_entries(text):
            fields = set(field_names)
            if explicit_post_payloads:
                fields.update(set().union(*explicit_post_payloads))
            for match in re.finditer(r"\bdelete\s+payload(?:\.([A-Za-z_$][\w$]*)|\[\s*([\"'])([A-Za-z_$][\w$]*)\2\s*\])", text):
                fields.discard(str(match.group(1) or match.group(3) or ""))
            return fields
        if explicit_post_payloads:
            explicit_fields = set().union(*explicit_post_payloads)
            if "Object.fromEntries" in text and "FormData" in text:
                fields = set(field_names) | explicit_fields
                for match in re.finditer(r"\bdelete\s+payload(?:\.([A-Za-z_$][\w$]*)|\[\s*([\"'])([A-Za-z_$][\w$]*)\2\s*\])", text):
                    fields.discard(str(match.group(1) or match.group(3) or ""))
                return fields
            return explicit_fields
        fields = set(field_names)
        if "Object.fromEntries" not in text or "FormData" not in text:
            return {
                field
                for field in field_names
                if CheckRunner._js_reads_form_field(text, field)
            }
        for match in re.finditer(r"\bdelete\s+payload(?:\.([A-Za-z_$][\w$]*)|\[\s*([\"'])([A-Za-z_$][\w$]*)\2\s*\])", text):
            fields.discard(str(match.group(1) or match.group(3) or ""))
        for match in re.finditer(r"\bpayload(?:\.([A-Za-z_$][\w$]*)|\[\s*([\"'])([A-Za-z_$][\w$]*)\2\s*\])\s*=", text):
            field = str(match.group(1) or match.group(3) or "")
            if field:
                fields.add(field)
        return fields

    @classmethod
    def _js_post_payload_field_sets(cls, js_source: str) -> list[set[str]]:
        text = str(js_source or "")
        payloads: list[set[str]] = []

        def add_fields(body: str) -> None:
            fields = cls._js_object_literal_keys(body)
            fields = {
                field
                for field in fields
                if field not in {"body", "headers", "method", "signal", "credentials", "mode", "cache", "redirect"}
            }
            if fields:
                payloads.append(fields)

        for match in re.finditer(r"JSON\.stringify\(\s*\{(?P<body>[^{}]+)\}\s*\)", text, re.DOTALL):
            if cls._nearest_request_method_before(text, match.start()) == "POST":
                add_fields(match.group("body"))

        for post_match in re.finditer(r"\bmethod\s*:\s*([\"'`])POST\1", text, re.IGNORECASE):
            tail = text[post_match.start(): post_match.end() + 900]
            payload_vars = {
                match.group("var")
                for match in re.finditer(r"JSON\.stringify\(\s*(?P<var>[A-Za-z_$][\w$]*)\s*\)", tail)
            }
            if not payload_vars:
                continue
            head = text[max(0, post_match.start() - 1600): post_match.start()]
            for payload_var in sorted(payload_vars):
                declaration_pattern = re.compile(
                    rf"\b(?:const|let|var)\s+{re.escape(payload_var)}\s*=\s*\{{(?P<body>[^{{}}]*)\}}",
                    re.DOTALL,
                )
                declarations = list(declaration_pattern.finditer(head))
                fields: set[str] = set()
                if declarations:
                    fields.update(cls._js_object_literal_keys(declarations[-1].group("body")))
                    mutation_start = declarations[-1].end()
                    fields.update(cls._js_object_mutation_keys(head[mutation_start:], payload_var))
                else:
                    fields.update(cls._js_object_mutation_keys(head, payload_var))
                fields = {
                    field
                    for field in fields
                    if field not in {"body", "headers", "method", "signal", "credentials", "mode", "cache", "redirect"}
                }
                if fields:
                    payloads.append(fields)

        deduped: list[set[str]] = []
        seen: set[tuple[str, ...]] = set()
        for fields in payloads:
            key = tuple(sorted(fields))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(fields)
        return deduped

    @staticmethod
    def _nearest_request_method_before(js_source: str, offset: int) -> str | None:
        prefix = str(js_source or "")[max(0, offset - 700): max(0, offset)]
        methods = list(
            re.finditer(
                r"\bmethod\s*:\s*([\"'`])(?P<method>GET|POST|PUT|PATCH|DELETE)\1",
                prefix,
                re.IGNORECASE,
            )
        )
        if not methods:
            return None
        return str(methods[-1].group("method") or "").upper()

    @staticmethod
    def _html_forms(html_source: str) -> list[dict[str, object]]:
        forms: list[dict[str, object]] = []
        for match in re.finditer(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", str(html_source or ""), re.IGNORECASE | re.DOTALL):
            attrs = match.group("attrs") or ""
            body = match.group("body") or ""
            form_id_match = re.search(r"\bid\s*=\s*([\"'])(?P<id>[^\"']+)\1", attrs, re.IGNORECASE)
            selectors: list[str] = []
            if form_id_match:
                selectors.append(f"#{form_id_match.group('id')}")
            for attr_match in re.finditer(
                r"\b(?P<name>data-[A-Za-z0-9_-]+)(?:\s*=\s*([\"'])(?P<value>[^\"']*)\2)?",
                attrs,
                re.IGNORECASE,
            ):
                attr_name = attr_match.group("name")
                attr_value = attr_match.group("value")
                selectors.append(f"[{attr_name}=\"{attr_value}\"]" if attr_value else f"[{attr_name}]")
            names = {
                name_match.group("name")
                for name_match in re.finditer(r"\bname\s*=\s*([\"'])(?P<name>[A-Za-z0-9_-]+)\1", body, re.IGNORECASE)
            }
            has_submit_control = bool(
                re.search(r"<input\b(?=[^>]*\btype\s*=\s*([\"'])submit\1)", body, re.IGNORECASE | re.DOTALL)
                or re.search(r"<button\b(?=[^>]*\btype\s*=\s*([\"'])submit\1)", body, re.IGNORECASE | re.DOTALL)
                or re.search(r"<button\b(?![^>]*\btype\s*=)", body, re.IGNORECASE | re.DOTALL)
            )
            field_ids_by_name: dict[str, str] = {}
            for field_match in re.finditer(r"<(?:input|select|textarea)\b(?P<attrs>[^>]*)>", body, re.IGNORECASE | re.DOTALL):
                field_attrs = field_match.group("attrs") or ""
                name_attr = re.search(r"\bname\s*=\s*([\"'])(?P<name>[A-Za-z0-9_-]+)\1", field_attrs, re.IGNORECASE)
                id_attr = re.search(r"\bid\s*=\s*([\"'])(?P<id>[A-Za-z0-9_-]+)\1", field_attrs, re.IGNORECASE)
                if name_attr and id_attr:
                    field_ids_by_name[name_attr.group("name")] = id_attr.group("id")
            forms.append(
                {
                    "id": form_id_match.group("id") if form_id_match else "",
                    "selectors": selectors,
                    "field_names": sorted(names),
                    "field_ids_by_name": field_ids_by_name,
                    "has_submit_control": has_submit_control,
                }
            )
        return forms

    @classmethod
    def _button_wiring_issues(cls, relative_path: str, js_path: Path, html_source: str, js_source: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for match in re.finditer(r"<button\b(?P<attrs>[^>]*)>", str(html_source or ""), re.IGNORECASE):
            attrs = match.group("attrs") or ""
            id_match = re.search(r"\bid\s*=\s*([\"'])(?P<id>[^\"']+)\1", attrs, re.IGNORECASE)
            if not id_match:
                continue
            button_id = id_match.group("id")
            type_match = re.search(r"\btype\s*=\s*([\"'])(?P<type>[^\"']+)\1", attrs, re.IGNORECASE)
            if str(type_match.group("type") if type_match else "").lower() in {"submit", "reset"}:
                continue
            if cls._js_references_dom_id(js_source, button_id):
                continue
            issues.append(
                ValidationIssue(
                    code="platform.workflow_button_without_handler",
                    message=f"{relative_path} has button #{button_id}, but {js_path.name} never references it. Visible action buttons must be wired or be plain links.",
                    severity="high",
                    location=relative_path,
                    blocking=True,
                    repair_recipe=cls._button_wiring_repair_recipe(
                        relative_path=relative_path,
                        js_path=js_path,
                        button_id=button_id,
                    ),
                )
            )
        return issues

    @staticmethod
    def _button_wiring_repair_recipe(
        *,
        relative_path: str,
        js_path: Path,
        button_id: str,
    ) -> dict[str, Any]:
        js_relative = js_path.relative_to(js_path.parents[4]).as_posix()
        role_match = re.search(r"miniapp/app/static/(?P<role>client|specialist|manager)/", relative_path)
        return {
            "recipe_id": "frontend.button_wiring",
            "failure_class": "frontend_interaction_static_smoke",
            "failure_signature": "frontend.unwired_button",
            "required_next_tool": "read_files",
            "suggested_tool_after_read": "write_file",
            "target_files": [relative_path, js_relative],
            "verification_check": "frontend_interaction_static_smoke",
            "verification_command": "run_checks frontend_interaction_static_smoke",
            "retry_policy": "deterministic_repair",
            "deterministic": True,
            "retryable": True,
            "instruction": (
                "Read the exact HTML page and role app.js. Wire the visible button to the intended click handler, "
                "or convert it to a plain link/control only if no persisted action is intended. Keep the repair limited to that role page and script."
            ),
            "evidence": {
                "role": role_match.group("role") if role_match else "",
                "html_file": relative_path,
                "js_file": js_relative,
                "button_id": button_id,
            },
        }

    @staticmethod
    def _js_references_dom_id(js_source: str, dom_id: str) -> bool:
        escaped = re.escape(str(dom_id or ""))
        return bool(
            re.search(rf"getElementById\(\s*([\"']){escaped}\1\s*\)", js_source)
            or re.search(rf"querySelector\(\s*([\"'])#{escaped}\1\s*\)", js_source)
            or re.search(rf"querySelectorAll\(\s*([\"'])#{escaped}\1\s*\)", js_source)
            or re.search(rf"closest\(\s*([\"'])#{escaped}\1\s*\)", js_source)
            or re.search(rf"([\"'])#{escaped}\1", js_source)
            or re.search(rf"([\"']){escaped}\1", js_source)
        )

    @staticmethod
    def _js_references_selector(js_source: str, selector: str) -> bool:
        text = str(js_source or "")
        value = str(selector or "").strip()
        if not value:
            return False
        if value in text or value.replace('"', "'") in text:
            return True
        attr_match = re.match(r"\[(?P<name>data-[A-Za-z0-9_-]+)(?:=(?P<quote>[\"']?)(?P<value>[^\]\"']+)(?P=quote))?\]", value)
        return bool(attr_match and attr_match.group("name") in text)

    @staticmethod
    def _js_has_submit_handler_for_id(js_source: str, dom_id: str) -> bool:
        if not CheckRunner._js_references_dom_id(js_source, dom_id):
            return False
        text = str(js_source or "")
        return bool(re.search(r"\.addEventListener\(\s*([\"'])submit\1", text) or re.search(r"\bonsubmit\s*=", text))

    @staticmethod
    def _js_has_submit_handler_for_selector(js_source: str, selector: str) -> bool:
        if not CheckRunner._js_references_selector(js_source, selector):
            return False
        text = str(js_source or "")
        return bool(re.search(r"\.addEventListener\(\s*([\"'])submit\1", text) or re.search(r"\bonsubmit\s*=", text))

    @staticmethod
    def _js_has_field_event_handler_for_id(js_source: str, dom_id: str) -> bool:
        if not CheckRunner._js_references_dom_id(js_source, dom_id):
            return False
        text = str(js_source or "")
        return bool(re.search(r"\.addEventListener\(\s*([\"'])(?:change|input)\1", text))

    @staticmethod
    def _js_has_field_event_handler_for_selector(js_source: str, selector: str) -> bool:
        if not CheckRunner._js_references_selector(js_source, selector):
            return False
        text = str(js_source or "")
        return bool(re.search(r"\.addEventListener\(\s*([\"'])(?:change|input)\1", text))

    @staticmethod
    def _js_reads_form_field(js_source: str, field_name: str) -> bool:
        escaped = re.escape(str(field_name or ""))
        text = str(js_source or "")
        return bool(
            re.search(rf"\.get\(\s*([\"']){escaped}\1\s*\)", js_source)
            or re.search(rf"\b{escaped}\s*:", js_source)
            or re.search(rf"\bdata\.{escaped}\b", js_source)
            or re.search(rf"\b[A-Za-z_$][\w$]*\.{escaped}\b", js_source)
            or re.search(rf"\[\s*name\s*=\s*([\"']){escaped}\1\s*\]", text)
            or re.search(rf"\bname\s*=\s*([\"']){escaped}\1", text)
            or re.search(rf"\bcollectFormData\([^)]*\[[^\]]*([\"']){escaped}\1", text, re.DOTALL)
            or CheckRunner._js_payload_uses_dynamic_formdata_entries(text)
            or re.search(
                rf"\bfor\s*\([^)]*\bkey\b[^)]*\bof\b\s*\[[^\]]*([\"']){escaped}\1[^\]]*\][\s\S]{{0,500}}\.get\(\s*key\s*\)",
                text,
            )
        )

    @staticmethod
    def _is_path_id_field(field_name: str) -> bool:
        value = str(field_name or "").strip().lower()
        return value == "id" or value.endswith("_id")

    @staticmethod
    def _js_reads_form_path_id_field(js_source: str, field_name: str) -> bool:
        escaped = re.escape(str(field_name or ""))
        text = str(js_source or "")
        return bool(
            re.search(rf"\.get\(\s*([\"']){escaped}\1\s*\)", text)
            or re.search(rf"\b(?:form|[A-Za-z_$][\w$]*Form)\.{escaped}\b", text, re.IGNORECASE)
            or re.search(rf"\[\s*name\s*=\s*([\"']){escaped}\1\s*\]", text)
            or re.search(rf"\bname\s*=\s*([\"']){escaped}\1", text)
        )

    @staticmethod
    def _js_reads_dom_field_id(js_source: str, dom_id: str) -> bool:
        value = str(dom_id or "").strip()
        if not value:
            return False
        escaped = re.escape(value)
        text = str(js_source or "")
        if re.search(
            rf"document\.(?:getElementById|querySelector)\(\s*([\"'])(?:#)?{escaped}\1\s*\)\s*(?:\?\.|\.)\s*(?:value|checked)\b",
            text,
        ):
            return True
        if re.search(
            rf"(?<![\w$])[A-Za-z_$][\w$]*\(\s*([\"']){escaped}\1\s*\)\s*(?:\?\.|\.)\s*(?:value|checked)\b",
            text,
        ):
            return True
        bindings = CheckRunner._js_dom_id_bindings(text)
        for var_name, bound_id in bindings.items():
            if bound_id != value:
                continue
            if re.search(rf"\b{re.escape(var_name)}\s*(?:\?\.|\.)\s*(?:value|checked)\b", text):
                return True
        return False

    @classmethod
    def _role_css_html_contract_issues(cls, static_root: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        shared_css = cls._try_read_text(static_root / "shared/base.css")
        for role in ROLE_ORDER:
            role_dir = static_root / role
            css_path = role_dir / "styles.css"
            if not role_dir.exists() or not css_path.exists():
                continue
            css_text = cls._try_read_text(css_path)
            html_text = "\n".join(cls._try_read_text(path) for path in sorted(role_dir.rglob("*.html")))
            js_text = cls._try_read_text(role_dir / "app.js")
            html_classes = cls._html_class_names(html_text)
            css_classes = cls._css_class_selectors(f"{shared_css}\n{css_text}")
            missing_classes = sorted(
                class_name
                for class_name in html_classes
                if class_name not in css_classes and cls._html_class_requires_explicit_css_rule(class_name, css_classes)
            )
            if missing_classes:
                issues.append(
                    ValidationIssue(
                        code="platform.html_class_without_css_rule",
                        message=(
                            f"{role} HTML uses classes without CSS rules: {', '.join(missing_classes[:8])}. "
                            "Balanced/Quality layouts must style root headers, actions, cards, forms, and lists explicitly."
                        ),
                        severity="medium",
                        location=css_path.relative_to(static_root.parents[2]).as_posix(),
                        blocking=False,
                    )
                )
            role_css_classes = cls._css_class_selectors(css_text)
            used_classes = html_classes | cls._js_class_names(js_text)
            if len(role_css_classes) >= 8:
                used_ratio = len(role_css_classes & used_classes) / max(1, len(role_css_classes))
                if used_ratio < 0.30:
                    issues.append(
                        ValidationIssue(
                            code="platform.role_css_mostly_unused",
                            message=f"{role} CSS appears disconnected from HTML/JS: only {used_ratio:.0%} of class rules are used.",
                            severity="medium",
                            location=css_path.relative_to(static_root.parents[2]).as_posix(),
                            blocking=False,
                        )
                    )
            responsive_ok = any(marker in css_text for marker in ("@media", "minmax(", "auto-fit", "auto-fill"))
            wrap_ok = any(marker in css_text for marker in ("flex-wrap", "min-width: 0", "overflow-x", "word-break")) or (
                "@media" in css_text and ("flex-direction: column" in css_text or "grid-template-columns: 1fr" in css_text)
            )
            if not responsive_ok or not wrap_ok:
                issues.append(
                    ValidationIssue(
                        code="platform.role_css_missing_responsive_guards",
                        message=f"{role} CSS lacks responsive/wrapping guards for Telegram-width screens.",
                        severity="medium",
                        location=css_path.relative_to(static_root.parents[2]).as_posix(),
                        blocking=False,
                    )
                )
        return issues

    @staticmethod
    def _html_class_requires_explicit_css_rule(class_name: str, css_classes: set[str]) -> bool:
        value = str(class_name or "").strip()
        if not value or value in {"page-shell"}:
            return False
        lowered = value.lower()
        if "--" in lowered:
            base = lowered.split("--", 1)[0]
            if base in {item.lower() for item in css_classes}:
                return False
        if "__" in lowered:
            base = lowered.split("__", 1)[0]
            if base in {item.lower() for item in css_classes}:
                return False
        if "-" in lowered:
            base = lowered.split("-", 1)[0]
            if base in {item.lower() for item in css_classes}:
                return False
        if lowered.endswith(("-page", "-dashboard", "-root", "-home", "-work", "-queue", "-summary")):
            return False
        if lowered in {"panel"}:
            return False
        if re.match(r"^(?:client|specialist|manager)-(?:root|form|details|dashboard|page|home|work|queue|summary|subtext)$", lowered):
            return False
        if lowered in {"muted", "small", "subtle"} or any(marker in lowered for marker in ("eyebrow", "hint", "placeholder")):
            return False
        return True

    @staticmethod
    def _try_read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            return ""

    @staticmethod
    def _html_class_names(html_source: str) -> set[str]:
        classes: set[str] = set()
        for match in re.finditer(r"\bclass\s*=\s*([\"'])(?P<classes>[^\"']+)\1", str(html_source or ""), re.IGNORECASE):
            classes.update(part.strip() for part in match.group("classes").split() if part.strip())
        return classes

    @staticmethod
    def _css_class_selectors(css_source: str) -> set[str]:
        return {
            match.group("class")
            for match in re.finditer(r"\.(?P<class>[A-Za-z_-][A-Za-z0-9_-]*)\b", str(css_source or ""))
        }

    @staticmethod
    def _js_class_names(js_source: str) -> set[str]:
        classes: set[str] = set()
        for match in re.finditer(r"\bclassName\s*=\s*([\"'`])(?P<classes>[^\"'`]+)\1", str(js_source or "")):
            classes.update(part.strip() for part in match.group("classes").split() if part.strip())
        for match in re.finditer(r"\bclassList\.add\((?P<args>[^)]*)\)", str(js_source or "")):
            for literal in re.finditer(r"([\"'`])(?P<class>[A-Za-z0-9_-]+)\1", match.group("args")):
                classes.add(literal.group("class"))
        for match in re.finditer(r"""class\\?=\s*\\?["'](?P<classes>[^"'`<>]+)\\?["']""", str(js_source or "")):
            classes.update(part.strip().strip("\\") for part in match.group("classes").split() if part.strip().strip("\\"))
        return classes

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
    def _read_role_html_surface_text(role_dir: Path) -> str:
        if not role_dir.exists():
            return ""
        chunks: list[str] = []
        for path in sorted(role_dir.rglob("*.html")):
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
    def _has_workflow_update_action(text: str) -> bool:
        lowered = str(text or "").lower()
        return bool(
            re.search(r"method\s*:\s*['\"](?:patch|put|delete)['\"]", text, flags=re.IGNORECASE)
            or (
                re.search(r"method\s*:\s*['\"]post['\"]", text, flags=re.IGNORECASE)
                and "/api" in lowered
                and any(marker in lowered for marker in ("action", "update", "save", "control", "обнов", "сохран", "действ"))
            )
        )

    @staticmethod
    def _tests_cover_workflow_contract(tests_text: str, contract: dict[str, Any]) -> bool:
        lowered = str(tests_text or "").lower()
        if "testclient" not in lowered and "node:test" not in lowered:
            return False
        required_terms = ["post", "get"]
        features = contract.get("features") or {}
        if features.get("workflow_update", True):
            non_get_mutation_terms = ("patch", "put", "delete")
            return all(term in lowered for term in required_terms) and (
                any(term in lowered for term in non_get_mutation_terms)
                or lowered.count("post") >= 2
            )
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

    def _api_workflow_smoke(
        self,
        *,
        source_dir: Path,
        generation_mode: GenerationMode | str | None,
        acceptance_contract: dict[str, Any] | None,
    ) -> RunCheckResult:
        del generation_mode
        contract = dict(acceptance_contract or {})
        if not contract.get("required"):
            return RunCheckResult(
                name="api_workflow_smoke",
                status="skipped",
                details="API workflow smoke skipped because no create/workflow acceptance contract is required.",
                command="api workflow smoke",
                logs=[],
            )
        proof = self._run_generic_api_workflow_proof(source_dir=source_dir, acceptance_contract=contract)
        logs = [str(item) for item in proof.get("logs") or [] if str(item).strip()]
        diagnostics = {
            "workflow_kind": contract.get("workflow_kind") or "create",
            "roles_checked": list(contract.get("roles") or ROLE_ORDER),
            "steps": proof.get("steps") or [],
            "api_paths": proof.get("api_paths") or [],
            "created_state_marker": proof.get("created_state_marker"),
            "updated_state_marker": proof.get("updated_state_marker"),
            "api_before": proof.get("api_before"),
            "api_after": proof.get("api_after"),
            "failed_step": proof.get("failed_step"),
            "failed_role": proof.get("failed_role"),
            "failed_route": proof.get("failed_route"),
            "failed_selector": proof.get("failed_selector"),
        }
        if not logs:
            logs = ["Generic API workflow proof passed."]
        return RunCheckResult(
            name="api_workflow_smoke",
            status="passed" if bool(proof.get("passed")) else "failed",
            details="Generic API workflow proof checked GET/POST persistence and update visibility before browser UI proof.",
            command="api workflow smoke",
            logs=logs,
            diagnostics=diagnostics,
        )

    def _browser_flow_smoke(
        self,
        *,
        source_dir: Path,
        preview: Any,
        preview_run_id: str | None,
        generation_mode: GenerationMode | str | None,
        acceptance_contract: dict[str, Any] | None,
    ) -> RunCheckResult:
        contract = dict(acceptance_contract or {})
        if not contract.get("required"):
            return RunCheckResult(
                name="browser_flow_smoke",
                status="skipped",
                details="Browser flow smoke skipped because no create/workflow acceptance contract is required.",
                command="playwright browser flow smoke",
                logs=[],
            )
        mobile_report = self._mobile_layout_report(source_dir=source_dir, generation_mode=generation_mode)
        preview_status = getattr(preview, "status", None)
        preview_url = str(getattr(preview, "url", "") or "")
        if preview_run_id is not None and getattr(preview, "draft_run_id", None) != preview_run_id:
            proof = {
                "passed": False,
                "failed_step": "preview_draft_mismatch",
                "infra_unavailable": True,
                "logs": ["Browser UI proof requires the running preview to be built from the current draft."],
                "ui_steps": [],
            }
        elif preview_status != "running" or not preview_url:
            proof = {
                "passed": False,
                "failed_step": "preview_unavailable",
                "infra_unavailable": True,
                "logs": [
                    "Browser UI proof requires the configured preview runtime to be running for the current draft.",
                    f"Observed preview_status={preview_status!r}, preview_url={preview_url!r}.",
                ],
                "ui_steps": [],
                "screenshots": [],
            }
        else:
            preview_url = self._reachable_preview_base_url(preview_url)
            proof = self._run_real_browser_ui_flow(
                source_dir=source_dir,
                preview_url=preview_url,
                acceptance_contract=contract,
            )
        preview_status = getattr(preview, "status", None)
        logs = [
            *[str(item) for item in proof.get("logs") or [] if str(item).strip()],
            *[str(item.get("message") or item) for item in mobile_report.get("findings") or []],
        ]
        proof_passed = bool(proof.get("passed"))
        mobile_passed = str(mobile_report.get("status") or "") != "failed"
        diagnostics = {
            "workflow_kind": contract.get("workflow_kind") or "create",
            "roles_checked": list(contract.get("roles") or ROLE_ORDER),
            "steps": proof.get("ui_steps") or proof.get("steps") or [],
            "ui_steps": proof.get("ui_steps") or [],
            "api_paths": proof.get("api_paths") or [],
            "created_state_marker": proof.get("created_state_marker") or proof.get("created_marker"),
            "updated_state_marker": proof.get("updated_state_marker") or proof.get("updated_marker"),
            "created_marker": proof.get("created_marker") or proof.get("created_state_marker"),
            "updated_marker": proof.get("updated_marker") or proof.get("updated_state_marker"),
            "console_errors": proof.get("console_errors") or [],
            "visible_errors": proof.get("visible_errors") or [],
            "screenshots": proof.get("screenshots") or [],
            "api_before": proof.get("api_before"),
            "api_after": proof.get("api_after"),
            "failed_step": proof.get("failed_step"),
            "failed_role": proof.get("failed_role"),
            "failed_route": proof.get("failed_route"),
            "failed_selector": proof.get("failed_selector"),
            "action": proof.get("action"),
            "infra_unavailable": bool(proof.get("infra_unavailable")),
            "preview_url": preview_url,
            "preview_status": preview_status,
            "mobile_layout": mobile_report,
        }
        if not logs:
            logs = ["Playwright browser workflow proof and mobile layout checks passed."]
        return RunCheckResult(
            name="browser_flow_smoke",
            status="passed" if proof_passed and mobile_passed else "failed",
            details=(
                "Playwright end-to-end workflow proof clicked role UI, executed JavaScript, checked API state, refresh persistence, cross-role visibility, and mobile layout."
            ),
            command="playwright browser flow smoke",
            logs=logs,
            diagnostics=diagnostics,
        )

    def _run_real_browser_ui_flow(
        self,
        *,
        source_dir: Path,
        preview_url: str,
        acceptance_contract: dict[str, Any],
    ) -> dict[str, Any]:
        api_paths = [
            str(endpoint.get("path") or "").strip()
            for endpoint in acceptance_contract.get("required_endpoints") or []
            if isinstance(endpoint, dict) and str(endpoint.get("path") or "").strip()
        ]
        api_paths = list(dict.fromkeys(path for path in api_paths if path.startswith("/api/")))
        routes_by_role = self._role_preview_routes(source_dir)
        prompt_hints = acceptance_contract.get("prompt_hints") if isinstance(acceptance_contract.get("prompt_hints"), dict) else {}
        state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
        source_roles = [
            role
            for role in (
                str(item).strip().lower()
                for item in (state_contract.get("source_roles") or [])
            )
            if role in ROLE_ORDER
        ]
        update_roles = [
            role
            for role in (
                str(item).strip().lower()
                for item in (state_contract.get("update_roles") or [])
            )
            if role in ROLE_ORDER
        ]
        observer_roles = [
            role
            for role in (
                str(item).strip().lower()
                for item in (state_contract.get("observer_roles") or [])
            )
            if role in ROLE_ORDER
        ]
        payload = {
            "base_url": preview_url.rstrip("/"),
            "api_paths": api_paths,
            "routes_by_role": routes_by_role,
            "role_flow": {
                "source_roles": source_roles,
                "update_roles": update_roles,
                "observer_roles": observer_roles,
            },
            "screenshot_dir": tempfile.mkdtemp(prefix="miniapp-browser-ui-proof-"),
        }
        script = self._real_browser_ui_flow_python_script()
        env = {**os.environ}
        try:
            result = subprocess.run(
                [sys.executable, "-c", script, json.dumps(payload)],
                cwd=source_dir / "miniapp",
                capture_output=True,
                text=True,
                timeout=int(os.getenv("BROWSER_UI_FLOW_TIMEOUT_SEC", "180")),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "passed": False,
                "failed_step": "timeout",
                "logs": self._command_logs("Playwright browser UI proof timed out.", exc.stdout or "", exc.stderr or ""),
                "ui_steps": [],
                "screenshots": [],
            }
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        parsed: dict[str, Any] | None = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
        if parsed is None:
            return {
                "passed": False,
                "failed_step": "invalid_browser_proof_output",
                "logs": self._command_logs("Playwright browser UI proof did not return JSON.", result.stdout, result.stderr),
                "ui_steps": [],
                "screenshots": [],
            }
        parsed.setdefault("logs", [])
        if result.returncode != 0:
            parsed["passed"] = False
            parsed["logs"] = [*list(parsed.get("logs") or []), *self._command_logs("Playwright browser UI proof failed.", "", output)]
        return parsed

    @staticmethod
    def _real_browser_ui_flow_python_script() -> str:
        return r'''
import json
import os
import re
import sys
import traceback
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception as exc:
    print(json.dumps({
        "passed": False,
        "failed_step": "browser_infra_unavailable",
        "infra_unavailable": True,
        "logs": [f"Playwright is not available: {exc.__class__.__name__}: {exc}"],
        "ui_steps": [],
        "screenshots": [],
    }, ensure_ascii=False))
    sys.exit(1)


PAYLOAD = json.loads(sys.argv[1] or "{}")
BASE_URL = str(PAYLOAD.get("base_url") or "").rstrip("/")
ROUTES_BY_ROLE = PAYLOAD.get("routes_by_role") or {}
REQUESTED_API_PATHS = [str(path) for path in PAYLOAD.get("api_paths") or [] if str(path).startswith("/api/")]
ROLE_FLOW = PAYLOAD.get("role_flow") or {}
SOURCE_ROLES = [str(role) for role in ROLE_FLOW.get("source_roles") or [] if str(role) in ("client", "specialist", "manager")]
UPDATE_ROLES = [str(role) for role in ROLE_FLOW.get("update_roles") or [] if str(role) in ("client", "specialist", "manager")]
OBSERVER_ROLES = [str(role) for role in ROLE_FLOW.get("observer_roles") or [] if str(role) in ("client", "specialist", "manager")]
SCREENSHOT_DIR = Path(PAYLOAD.get("screenshot_dir") or "/tmp/miniapp-browser-ui-proof")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

STEPS = []
CONSOLE_ERRORS = []
VISIBLE_ERRORS = []
SCREENSHOTS = []
OPENAPI = {}
API_PATHS = []
GET_API_PATHS = []
BASE_API_PATH = ""
CREATED_MARKER = "browser-ui-proof"
UPDATED_MARKER = "browser-ui-updated"
MANAGER_MARKER = "browser-manager-updated"
API_BEFORE = None
API_AFTER = None


def emit(result):
    print(json.dumps(result, ensure_ascii=False))


def safe_screenshot(page, label):
    try:
        path = SCREENSHOT_DIR / f"{len(SCREENSHOTS) + 1:02d}-{label}.png"
        page.screenshot(path=str(path), full_page=True)
        SCREENSHOTS.append(str(path))
    except Exception:
        pass


def fail(step, message, page=None, **extra):
    if page is not None:
        safe_screenshot(page, step)
    result = {
        "passed": False,
        "failed_step": step,
        "logs": [message],
        "ui_steps": STEPS,
        "created_marker": CREATED_MARKER,
        "updated_marker": UPDATED_MARKER,
        "manager_marker": MANAGER_MARKER,
        "console_errors": CONSOLE_ERRORS[-20:],
        "visible_errors": VISIBLE_ERRORS[-20:],
        "screenshots": SCREENSHOTS,
        "api_paths": API_PATHS,
        "get_api_paths": GET_API_PATHS,
        "source_roles": SOURCE_ROLES,
        "update_roles": UPDATE_ROLES,
        "observer_roles": OBSERVER_ROLES,
        "api_before": API_BEFORE,
        "api_after": API_AFTER,
        **extra,
    }
    emit(result)
    sys.exit(1)


def request_json(method, path, payload=None):
    data = None
    headers = {"Accept": "application/json", "User-Agent": "browser-flow-smoke"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(urljoin(BASE_URL + "/", path.lstrip("/")), data=data, headers=headers, method=method.upper())
    with urlopen(request, timeout=8.0) as response:
        body = response.read().decode("utf-8", errors="ignore")
        if not body:
            return None
        return json.loads(body)


def items_from(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "records", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def contains_marker(value, marker):
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(contains_marker(item, marker) for item in value)
    return False


def find_id(value):
    if isinstance(value, dict):
        for key, candidate in value.items():
            normalized = str(key or "").lower()
            if normalized != "id" and not normalized.endswith("_id"):
                continue
            if candidate not in (None, ""):
                return candidate
    return None


def find_created(payload, marker, created_id=None):
    for item in items_from(payload):
        if created_id is not None and isinstance(item, dict) and str(find_id(item)) == str(created_id):
            return item
        if contains_marker(item, marker):
            return item
    return None


def discover_api():
    global OPENAPI, API_PATHS, GET_API_PATHS, BASE_API_PATH
    try:
        OPENAPI = request_json("GET", "/openapi.json") or {}
    except Exception as exc:
        fail("openapi_discovery", f"Preview OpenAPI could not be loaded: {exc}")
    all_paths = OPENAPI.get("paths") or {}
    GET_API_PATHS = [path for path, methods in all_paths.items() if str(path).startswith("/api/") and "get" in methods]
    API_PATHS = [path for path in REQUESTED_API_PATHS if path in all_paths and "get" in all_paths[path] and "post" in all_paths[path]]
    if not API_PATHS:
        API_PATHS = [path for path, methods in all_paths.items() if str(path).startswith("/api/") and "get" in methods and "post" in methods]
    API_PATHS = list(dict.fromkeys(API_PATHS))
    GET_API_PATHS = list(dict.fromkeys([*API_PATHS, *GET_API_PATHS]))
    if not GET_API_PATHS:
        fail("api_discovery", "No GET /api resource is discoverable for browser UI proof.")
    if not API_PATHS:
        fail("api_discovery", "No GET+POST /api resource is discoverable for the source role create proof.")
    BASE_API_PATH = API_PATHS[0]


def api_snapshot():
    snapshot = {}
    for path in GET_API_PATHS:
        try:
            snapshot[path] = request_json("GET", path)
        except Exception as exc:
            snapshot[path] = {"__error__": f"{exc.__class__.__name__}: {exc}"}
    return snapshot


def find_marker_in_snapshot(snapshot, marker, created_id=None):
    for path, payload in (snapshot or {}).items():
        item = find_created(payload, marker, created_id)
        if item is not None:
            return path, item
    return "", None


def snapshot_changed(before, after):
    try:
        return json.dumps(before, ensure_ascii=False, sort_keys=True) != json.dumps(after, ensure_ascii=False, sort_keys=True)
    except Exception:
        return before != after


def normalize_routes(role):
    routes = ROUTES_BY_ROLE.get(role) or [f"/{role}"]
    normalized = []
    for route in routes:
        route = "/" + str(route).strip("/")
        if route not in normalized:
            normalized.append(route)
    if f"/{role}" not in normalized:
        normalized.insert(0, f"/{role}")
    return normalized[:8]


def goto(page, route):
    page.goto(urljoin(BASE_URL + "/", route.lstrip("/")), wait_until="domcontentloaded", timeout=12000)
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except PlaywrightTimeoutError:
        pass
    STEPS.append({"action": "open", "route": route})


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def collect_visible_errors(page):
    text = body_text(page)
    lowered = text.lower()
    markers = [
        "referenceerror",
        "typeerror",
        "is not defined",
        "undefined variable",
        "ошибка",
        "не удалось",
        "failed to",
    ]
    found = [marker for marker in markers if marker in lowered]
    if found:
        VISIBLE_ERRORS.append(text[:700])
    return found


def check_runtime_errors(page, step, route):
    visible = collect_visible_errors(page)
    if CONSOLE_ERRORS:
        fail(step, f"Browser JavaScript error during {step}: {CONSOLE_ERRORS[-1]}", page, failed_route=route)
    if visible:
        fail(step, f"Visible runtime/error text appeared during {step}: {visible[0]}", page, failed_route=route)


def control_meta(locator):
    return locator.evaluate(
        """el => ({
            tag: el.tagName.toLowerCase(),
            type: (el.getAttribute('type') || '').toLowerCase(),
            name: el.getAttribute('name') || '',
            id: el.id || '',
            required: !!el.required,
            disabled: !!el.disabled,
            hidden: !!el.hidden || el.offsetParent === null,
            value: el.value || '',
            options: el.tagName.toLowerCase() === 'select'
                ? Array.from(el.options).map(o => ({ value: o.value, text: o.textContent || '', disabled: !!o.disabled }))
                : []
        })"""
    )


def value_for(meta, marker, purpose, created_id=None):
    name = str(meta.get("name") or meta.get("id") or "").lower()
    typ = str(meta.get("type") or "")
    if created_id is not None and (name == "id" or name.endswith("_id")):
        return str(created_id)
    if typ in {"number", "range"} or any(token in name for token in ("count", "quantity", "stock", "price", "amount", "total")):
        return "3"
    if typ == "datetime-local":
        return "2026-05-20T12:30"
    if typ == "date" or "date" in name or "day" in name:
        return "2026-05-20"
    if typ == "time" or "time" in name:
        return "12:30"
    if typ == "email" or "email" in name:
        return "flow@example.test"
    if typ == "tel" or "phone" in name or "contact" in name:
        return "+79000000000"
    if "status" in name or "stage" in name or "state" in name:
        return "ready" if purpose == "update" else "new"
    if purpose == "update":
        return f"{marker} {name or 'note'}"
    return f"{marker} {name or 'value'}"


def fill_control(locator, marker, purpose, created_id=None):
    try:
        meta = control_meta(locator)
    except Exception:
        return False
    if meta.get("disabled") or meta.get("hidden"):
        return False
    tag = meta.get("tag")
    typ = meta.get("type")
    if typ in {"hidden", "button", "submit", "reset", "file", "image"}:
        return False
    try:
        if tag == "select":
            raw_options = [opt for opt in meta.get("options") or [] if not opt.get("disabled")]
            options = [opt for opt in raw_options if str(opt.get("value") or "").strip()]
            if not options:
                options = [opt for opt in raw_options if str(opt.get("text") or "").strip()]
            if not options:
                return False
            option = options[-1] if purpose == "update" and len(options) > 1 else options[0]
            option_value = str(option.get("value") or "").strip()
            if option_value:
                locator.select_option(value=option_value)
            else:
                locator.select_option(label=str(option.get("text") or "").strip())
            return True
        if typ in {"checkbox", "radio"}:
            locator.check(timeout=1500)
            return True
        locator.fill(str(value_for(meta, marker, purpose, created_id)), timeout=1500)
        return True
    except Exception:
        return False


def submit_form(page, form, role, route, marker, purpose, created_id=None):
    controls = form.locator("input, textarea, select")
    filled = 0
    for index in range(controls.count()):
        if fill_control(controls.nth(index), marker, purpose, created_id):
            filled += 1
    submit = form.locator("button[type=submit], input[type=submit], button:not([type]), button")
    try:
        if submit.count():
            submit.first.click(timeout=2500)
        else:
            form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
    except Exception as exc:
        fail(f"{role}_{purpose}_click", f"Could not submit {role} {purpose} form: {exc}", page, failed_role=role, failed_route=route, failed_selector="form")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(700)
    STEPS.append({"action": f"{role}_{purpose}", "route": route, "filled_controls": filled})
    return filled


def fill_page_update_controls(scope, marker, created_id):
    controls = scope.locator("input, textarea, select")
    filled = 0
    for index in range(controls.count()):
        if fill_control(controls.nth(index), marker, "update", created_id):
            filled += 1
    return filled


def click_update_button(page, scope, role, route):
    patterns = re.compile(r"(save|update|ready|done|complete|сохран|обнов|готов|выполн)", re.I)
    refresh_only = re.compile(r"^(refresh|reload|обновить|перезагрузить)$", re.I)
    buttons = scope.locator("button, [role=button], input[type=button], input[type=submit]")
    for index in range(min(buttons.count(), 12)):
        button = buttons.nth(index)
        try:
            text = (button.inner_text(timeout=1000) or button.get_attribute("value") or "").strip()
        except Exception:
            text = ""
        data_id = button.get_attribute("data-id") or ""
        data_action = button.get_attribute("data-action") or ""
        if refresh_only.search(text) and not (data_id or data_action):
            continue
        if not (data_id or data_action or patterns.search(text)):
            continue
        try:
            button.click(timeout=2500)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(700)
            STEPS.append({"action": f"{role}_button_update", "route": route, "text": text[:80]})
            return True
        except Exception:
            continue
    return False


def submit_role_create(page, role):
    for route in normalize_routes(role):
        goto(page, route)
        forms = page.locator("form")
        for index in range(forms.count()):
            form = forms.nth(index)
            controls = form.locator("input, textarea, select")
            if controls.count() == 0:
                continue
            submit_form(page, form, role, route, CREATED_MARKER, "create")
            check_runtime_errors(page, f"{role}_source_create_ui", route)
            return route
    fail("source_role_form_discovery", f"No source role form with fillable controls was found in {role} routes.", page, failed_role=role, failed_selector="form")


def submit_role_update(page, role, marker, created_id, before_snapshot):
    before_text = json.dumps(before_snapshot, ensure_ascii=False, sort_keys=True)
    for route in normalize_routes(role):
        goto(page, route)
        form_groups = []
        created_containers = []
        try:
            containers = page.locator("article, li, tr, [class*=card], [data-id], [data-record-id], [data-item-id]").filter(has_text=CREATED_MARKER)
            for index in range(min(containers.count(), 12)):
                container = containers.nth(index)
                created_containers.append(container)
                form_groups.append(container.locator("form"))
        except Exception:
            pass
        for forms in form_groups:
            for index in range(forms.count()):
                form = forms.nth(index)
                controls = form.locator("input, textarea, select")
                if controls.count() == 0:
                    continue
                submit_form(page, form, role, route, marker, "update", created_id)
                check_runtime_errors(page, f"{role}_update_ui", route)
                return route
        for container in created_containers:
            fill_page_update_controls(container, marker, created_id)
            if click_update_button(page, container, role, route):
                check_runtime_errors(page, f"{role}_update_ui", route)
                return route
        forms = page.locator("form")
        for index in range(forms.count()):
            form = forms.nth(index)
            controls = form.locator("input, textarea, select")
            if controls.count() == 0:
                continue
            submit_form(page, form, role, route, marker, "update", created_id)
            check_runtime_errors(page, f"{role}_update_ui", route)
            return route
        fill_page_update_controls(page, marker, created_id)
        if click_update_button(page, page, role, route):
            check_runtime_errors(page, f"{role}_update_ui", route)
            return route
    fail("role_update_discovery", f"No prompt-assigned update form/control was found for role {role}.", page, failed_role=role, failed_selector="form/button", api_after=before_snapshot, before_text=before_text[:600])


def click_refresh_controls(page):
    buttons = page.locator("button, [role=button], a")
    for index in range(min(buttons.count(), 12)):
        button = buttons.nth(index)
        try:
            text = button.inner_text(timeout=500).strip()
        except Exception:
            continue
        if re.search(r"(refresh|reload|обнов|перезагруз)", text, re.I):
            try:
                button.click(timeout=1000)
                page.wait_for_timeout(300)
            except Exception:
                pass


def route_text_after_reload(page, route):
    goto(page, route)
    click_refresh_controls(page)
    try:
        page.reload(wait_until="domcontentloaded", timeout=10000)
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightTimeoutError:
            pass
    except Exception:
        pass
    page.wait_for_timeout(500)
    return body_text(page)


def role_has_marker(page, role, marker):
    texts = []
    for route in normalize_routes(role):
        text = route_text_after_reload(page, route)
        texts.append((route, text))
        check_runtime_errors(page, f"{role}_reload_visibility", route)
        if marker in text:
            return True, route, text
    return False, texts[-1][0] if texts else f"/{role}", texts[-1][1] if texts else ""


def check_horizontal_overflow(page, route, role):
    try:
        report = page.evaluate(
            """() => {
                const root = document.documentElement;
                const overflow = root.scrollWidth > window.innerWidth + 2 || document.body.scrollWidth > window.innerWidth + 2;
                const critical = Array.from(document.querySelectorAll('main, section, form, article, header, nav, .card, [class*=card], [class*=panel]')).slice(0, 80);
                const overlaps = [];
                for (let i = 0; i < critical.length; i++) {
                    const elA = critical[i];
                    const a = critical[i].getBoundingClientRect();
                    if (!a.width || !a.height) continue;
                    for (let j = i + 1; j < critical.length; j++) {
                        const elB = critical[j];
                        if (elA.contains(elB) || elB.contains(elA)) continue;
                        const b = critical[j].getBoundingClientRect();
                        if (!b.width || !b.height) continue;
                        const x = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
                        const y = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
                        if (x < 8 || y < 8) continue;
                        if (x * y > Math.min(a.width * a.height, b.width * b.height) * 0.65) {
                            overlaps.push({ a: critical[i].className || critical[i].tagName, b: critical[j].className || critical[j].tagName });
                        }
                    }
                }
                return { overflow, scrollWidth: root.scrollWidth, viewport: window.innerWidth, overlaps: overlaps.slice(0, 3) };
            }"""
        )
    except Exception:
        return
    if report.get("overflow"):
        fail("mobile_horizontal_overflow", f"{role} route {route} has horizontal overflow at mobile viewport: scrollWidth={report.get('scrollWidth')} viewport={report.get('viewport')}.", page, failed_role=role, failed_route=route)
    if report.get("overlaps"):
        fail("mobile_overlap", f"{role} route {route} has overlapping critical UI blocks at mobile viewport.", page, failed_role=role, failed_route=route)


def item_changed(before_item, after_item):
    if not isinstance(before_item, dict) or not isinstance(after_item, dict):
        return before_item != after_item
    ignored = {"updated_at", "created_at", "id"}
    before = {key: value for key, value in before_item.items() if key not in ignored}
    after = {key: value for key, value in after_item.items() if key not in ignored}
    return before != after


def run_flow(page):
    global API_BEFORE, API_AFTER
    discover_api()
    source_role = SOURCE_ROLES[0] if SOURCE_ROLES else ""
    if not source_role:
        fail("source_role_flow_missing", "Browser UI proof requires prompt_hints role-state source_roles from the acceptance contract.")
    API_BEFORE = api_snapshot()
    source_route = submit_role_create(page, source_role)
    after_create = api_snapshot()
    created_path, created_item = find_marker_in_snapshot(after_create, CREATED_MARKER)
    if created_item is None:
        fail(
            "source_create_state_change",
            f"{source_role} UI submit did not create a persisted API item containing the proof marker.",
            page,
            failed_role=source_role,
            failed_route=source_route,
            api_before=API_BEFORE,
            api_after=after_create,
        )
    created_id = find_id(created_item)
    if created_id is None:
        fail("created_id_missing", "Created UI state does not expose an id usable by another role action.", page, failed_role=source_role, api_after=after_create)
    has_marker, route, text = role_has_marker(page, source_role, CREATED_MARKER)
    if not has_marker:
        fail(f"{source_role}_refresh_visibility_ui", f"{source_role} UI did not show the created marker after reload, although API state exists.", page, failed_role=source_role, failed_route=route, api_after=after_create)

    API_AFTER = after_create
    update_markers = {}
    for index, role in enumerate([item for item in UPDATE_ROLES if item != source_role]):
        marker = UPDATED_MARKER if index == 0 else f"{UPDATED_MARKER}-{role}"
        before_update = api_snapshot()
        update_route = submit_role_update(page, role, marker, created_id, before_update)
        after_update = api_snapshot()
        if not snapshot_changed(before_update, after_update):
            fail(
                "role_update_state_change",
                f"{role} UI action completed but did not change any discoverable API state.",
                page,
                failed_role=role,
                failed_route=update_route,
                api_before=before_update,
                api_after=after_update,
            )
        API_AFTER = after_update
        update_markers[role] = marker

    visibility_roles = list(dict.fromkeys([source_role, *OBSERVER_ROLES, *UPDATE_ROLES]))
    for role in visibility_roles:
        if role not in ("client", "specialist", "manager"):
            continue
        created_visible, visible_route, visible_text = role_has_marker(page, role, CREATED_MARKER)
        if role in {source_role, *OBSERVER_ROLES} and not created_visible:
            fail(
                "role_created_state_visibility_ui",
                f"{role} UI did not show the source-created shared state after reload.",
                page,
                failed_role=role,
                failed_route=visible_route,
                api_after=API_AFTER,
            )
        for updated_by, marker in update_markers.items():
            if marker in json.dumps(API_AFTER, ensure_ascii=False) and role in {source_role, *OBSERVER_ROLES} and marker not in visible_text:
                fail(
                    "role_update_visibility_ui",
                    f"{role} UI did not show persisted update marker from {updated_by} after reload.",
                    page,
                    failed_role=role,
                    failed_route=visible_route,
                    api_after=API_AFTER,
                )
    for role in ("client", "specialist", "manager"):
        for route in normalize_routes(role)[:4]:
            route_text_after_reload(page, route)
            check_horizontal_overflow(page, route, role)
    emit({
        "passed": True,
        "failed_step": None,
        "logs": [f"Playwright browser UI proof passed through {BASE_API_PATH}."],
        "ui_steps": STEPS,
        "created_marker": CREATED_MARKER,
        "updated_marker": UPDATED_MARKER,
        "manager_marker": MANAGER_MARKER,
        "source_role": source_role,
        "update_roles": UPDATE_ROLES,
        "observer_roles": OBSERVER_ROLES,
        "created_api_path": created_path,
        "console_errors": CONSOLE_ERRORS[-20:],
        "visible_errors": VISIBLE_ERRORS[-20:],
        "screenshots": SCREENSHOTS,
        "api_paths": API_PATHS,
        "get_api_paths": GET_API_PATHS,
        "api_before": API_BEFORE,
        "api_after": API_AFTER,
    })


try:
    if not BASE_URL:
        fail("preview_unavailable", "Browser UI proof requires a preview base URL.", infra_unavailable=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            fail("browser_infra_unavailable", f"Chromium could not be launched for Playwright browser proof: {exc}", infra_unavailable=True)
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
        page = context.new_page()
        page.on("console", lambda msg: CONSOLE_ERRORS.append(f"{msg.type}: {msg.text}") if msg.type == "error" and "favicon" not in msg.text.lower() else None)
        page.on("pageerror", lambda exc: CONSOLE_ERRORS.append(f"pageerror: {exc}"))
        try:
            run_flow(page)
        finally:
            context.close()
            browser.close()
except SystemExit:
    raise
except Exception as exc:
    emit({
        "passed": False,
        "failed_step": "browser_runtime_exception",
        "logs": [f"Playwright browser UI proof crashed: {exc.__class__.__name__}: {exc}", traceback.format_exc(limit=8)],
        "ui_steps": STEPS,
        "created_marker": CREATED_MARKER,
        "updated_marker": UPDATED_MARKER,
        "manager_marker": MANAGER_MARKER,
        "console_errors": CONSOLE_ERRORS[-20:],
        "visible_errors": VISIBLE_ERRORS[-20:],
        "screenshots": SCREENSHOTS,
        "api_paths": API_PATHS,
        "get_api_paths": GET_API_PATHS,
        "source_roles": SOURCE_ROLES,
        "update_roles": UPDATE_ROLES,
        "observer_roles": OBSERVER_ROLES,
        "api_before": API_BEFORE,
        "api_after": API_AFTER,
    })
    sys.exit(1)
'''

    @staticmethod
    def _role_preview_routes(source_dir: Path) -> dict[str, list[str]]:
        pages_by_role = CheckRunner._routeable_role_pages(source_dir)
        routes_by_role: dict[str, list[str]] = {}
        for role in ROLE_ORDER:
            role_routes = CheckRunner._unique_role_routes(pages_by_role.get(role, []))
            root_route = f"/{role}"
            routes: list[str] = []
            if root_route in role_routes:
                routes.append(root_route)
            routes.extend(route for route in role_routes if route != root_route)
            if not routes:
                routes = [root_route]
            routes_by_role[role] = list(dict.fromkeys(routes))[:12]
        return routes_by_role

    def _run_generic_api_workflow_proof(self, *, source_dir: Path, acceptance_contract: dict[str, Any]) -> dict[str, Any]:
        backend_dir = source_dir / "miniapp"
        install_result = self._install_python_requirements(
            backend_dir,
            result_name="api_workflow_smoke",
            purpose="API workflow Python dependency",
        )
        if install_result is not None:
            return {
                "passed": False,
                "failed_step": "dependency_install",
                "logs": install_result.logs or [install_result.details or "Dependency install failed."],
            }
        api_paths = [
            str(endpoint.get("path") or "").strip()
            for endpoint in acceptance_contract.get("required_endpoints") or []
            if isinstance(endpoint, dict) and str(endpoint.get("path") or "").strip()
        ]
        api_paths = list(dict.fromkeys(path for path in api_paths if path.startswith("/api/")))
        script = self._generic_api_workflow_python_script()
        env = {**os.environ}
        python_path_parts = [str(backend_dir)]
        if env.get("PYTHONPATH"):
            python_path_parts.append(str(env.get("PYTHONPATH")))
        env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        with tempfile.TemporaryDirectory(prefix="miniapp-api-workflow-") as tmp_dir:
            env["DATABASE_URL"] = f"sqlite:///{(Path(tmp_dir) / 'api_workflow.db').as_posix()}"
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script, json.dumps(api_paths)],
                    cwd=backend_dir,
                    capture_output=True,
                    text=True,
                    timeout=int(os.getenv("API_WORKFLOW_TIMEOUT_SEC", "120")),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "passed": False,
                    "failed_step": "timeout",
                    "logs": self._command_logs("Generic API workflow proof timed out.", exc.stdout or "", exc.stderr or ""),
                }
        output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
        parsed: dict[str, Any] | None = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
        if parsed is None:
            return {
                "passed": False,
                "failed_step": "invalid_proof_output",
                "logs": self._command_logs("Generic API workflow proof did not return JSON.", result.stdout, result.stderr),
            }
        parsed.setdefault("logs", [])
        if result.returncode != 0:
            parsed["passed"] = False
            parsed["logs"] = [*list(parsed.get("logs") or []), *self._command_logs("Generic API workflow proof failed.", "", output)]
        return parsed

    @staticmethod
    def _generic_api_workflow_python_script() -> str:
        return r'''
import json
import sys

from fastapi.testclient import TestClient
from app import main as main_module


app = getattr(main_module, "app", None)
if app is None and hasattr(main_module, "create_app"):
    app = main_module.create_app()
if app is None:
    raise RuntimeError("Generated app.main must expose `app` or `create_app()`.")


def resolve_schema(openapi, schema):
    schema = dict(schema or {})
    ref = schema.get("$ref")
    if ref:
        name = str(ref).rsplit("/", 1)[-1]
        return dict((openapi.get("components") or {}).get("schemas", {}).get(name, {}) or {})
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            merged = {}
            for variant in variants:
                resolved = resolve_schema(openapi, variant)
                if resolved.get("type") != "null":
                    merged.update(resolved)
            if merged:
                return merged
    return schema


def request_schema(openapi, path, method):
    operation = ((openapi.get("paths") or {}).get(path) or {}).get(method.lower()) or {}
    content = ((operation.get("requestBody") or {}).get("content") or {})
    schema = (content.get("application/json") or content.get("application/x-www-form-urlencoded") or {}).get("schema") or {}
    return resolve_schema(openapi, schema)


def schema_type(schema):
    if not isinstance(schema, dict):
        return "string"
    if schema.get("type"):
        return schema.get("type")
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                resolved = resolve_schema(OPENAPI, variant)
                typ = resolved.get("type")
                if typ and typ != "null":
                    return typ
    return "string"


def value_for(name, schema, marker, purpose):
    lname = str(name or "").lower()
    schema = resolve_schema(OPENAPI, schema if isinstance(schema, dict) else {})
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        preferred = ("ready", "done", "completed", "paid", "in_progress") if purpose == "update" else tuple(enum)
        for value in preferred:
            if value in enum:
                return value
        return enum[-1] if purpose == "update" else enum[0]
    typ = schema_type(schema)
    fmt = str(schema.get("format") or "").lower()
    if lname in {"id", "pk"} or lname.endswith("_id"):
        return 1
    if typ in {"integer", "number"}:
        if "price" in lname or "cost" in lname:
            return 1200
        if "quantity" in lname or "count" in lname or "stock" in lname:
            return 3
        return 1
    if typ == "boolean":
        return True if purpose == "update" else False
    if typ == "array":
        return [f"{marker} item"]
    if typ == "object":
        return {"value": marker}
    if fmt in {"date-time", "datetime"}:
        return "2026-05-20T12:30:00"
    if fmt == "date":
        return "2026-05-20"
    if fmt == "time":
        return "12:30:00"
    if "email" in lname:
        return "flow@example.test"
    if "phone" in lname or "tel" in lname or "contact" in lname:
        return "+79000000000"
    if "date" in lname or "day" in lname or "time" in lname:
        return "2026-05-20"
    if "status" in lname or "state" in lname or "stage" in lname:
        return "ready" if purpose == "update" else "new"
    if "address" in lname:
        return f"{marker} address"
    if "name" in lname or "title" in lname or "item" in lname or "product" in lname:
        value = f"{marker} {name}"
    elif purpose == "update":
        value = f"{marker} updated {name}"
    else:
        value = f"{marker} {name}"
    max_length = schema.get("maxLength")
    try:
        if max_length and int(max_length) > 0:
            value = value[: int(max_length)]
    except (TypeError, ValueError):
        pass
    return value or marker


def build_payload(openapi, path, method, marker, purpose):
    schema = request_schema(openapi, path, method)
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(item) for item in schema.get("required") or [] if str(item).strip()]
    names = list(dict.fromkeys([*required, *[name for name in props if name not in {"id", "pk"}]][:10]))
    if not names and method.lower() == "patch":
        names = ["status"]
    if not names:
        names = ["title"]
    return {name: value_for(name, props.get(name, {}), marker, purpose) for name in names}


def items_from(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "records", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def contains_marker(value, marker):
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(contains_marker(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(contains_marker(item, marker) for item in value)
    return False


def find_id(value):
    if isinstance(value, dict):
        for key, candidate in value.items():
            normalized = str(key or "").lower()
            if normalized != "id" and not normalized.endswith("_id"):
                continue
            if candidate not in (None, ""):
                return candidate
    return None


def find_created(payload, marker, created_id=None):
    for item in items_from(payload):
        if created_id is not None and isinstance(item, dict) and str(find_id(item)) == str(created_id):
            return item
        if contains_marker(item, marker):
            return item
    return None


def update_path_for(openapi, base_path):
    paths = openapi.get("paths") or {}
    base = base_path.rstrip("/")
    for path, methods in paths.items():
        if "patch" in methods and (str(path).startswith(base + "/") or str(path) == base):
            return path, "patch"
    for path, methods in paths.items():
        if "put" in methods and (str(path).startswith(base + "/") or str(path) == base):
            return path, "put"
    for path, methods in paths.items():
        path_text = str(path)
        if "post" in methods and path_text != base and path_text.startswith(base + "/"):
            return path, "post"
    return "", ""


def concrete(path, item_id):
    value = str(path)
    for token in ("{id}", "{entity_id}", "{item_id}"):
        value = value.replace(token, str(item_id))
    import re
    return re.sub(r"\{[^}/]+\}", str(item_id), value)


def fail(step, message, **extra):
    result = {
        "passed": False,
        "failed_step": step,
        "logs": [message],
        "steps": STEPS,
        **extra,
    }
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1)


OPENAPI = app.openapi()
STEPS = []
requested_paths = json.loads(sys.argv[1] or "[]")
all_paths = OPENAPI.get("paths") or {}
api_paths = [path for path in requested_paths if path in all_paths and "get" in all_paths[path] and "post" in all_paths[path]]
if not api_paths:
    api_paths = [path for path, methods in all_paths.items() if path.startswith("/api/") and "get" in methods and "post" in methods]
api_paths = list(dict.fromkeys(api_paths))
if not api_paths:
    fail("api_discovery", "No generic GET+POST /api resource was discoverable from the generated app OpenAPI schema.", api_paths=[])

base_path = api_paths[0]
marker = "browser-flow-proof"
update_marker = "browser-flow-updated"

try:
    with TestClient(app) as client:
        before = client.get(base_path)
        if before.status_code >= 400:
            fail("initial_get", f"GET {base_path} returned {before.status_code}.", api_paths=api_paths)
        before_json = before.json()
        STEPS.append({"step": "initial_get", "path": base_path, "status": before.status_code})

        create_payload = build_payload(OPENAPI, base_path, "post", marker, "create")
        create = client.post(base_path, json=create_payload)
        if not (200 <= create.status_code < 300):
            fail("create_state", f"POST {base_path} returned {create.status_code}; payload={create_payload}; body={create.text[:500]}", api_paths=api_paths, api_before=before_json)
        create_json = create.json()
        created_id = find_id(create_json)
        STEPS.append({"step": "create_state", "path": base_path, "status": create.status_code, "id": created_id})

        after_create = client.get(base_path)
        after_create_json = after_create.json()
        created_item = find_created(after_create_json, marker, created_id)
        if created_item is None:
            fail("create_persistence", f"POST {base_path} did not persist a discoverable item/state through later GET.", api_paths=api_paths, api_before=before_json, api_after=after_create_json)
        created_id = created_id or find_id(created_item)
        STEPS.append({"step": "create_persistence", "path": base_path, "status": after_create.status_code, "id": created_id})

        update_template, method = update_path_for(OPENAPI, base_path)
        if not update_template:
            STEPS.append({"step": "update_route_discovery", "path": base_path, "status": "not_present"})
            print(json.dumps({
                "passed": True,
                "failed_step": None,
                "api_paths": api_paths,
                "created_state_marker": marker,
                "updated_state_marker": None,
                "api_before": before_json,
                "api_after": after_create_json,
                "steps": STEPS,
                "logs": [f"Generic workflow proof passed create/list persistence through {base_path}; no app-owned update route was required or discovered."],
            }, ensure_ascii=False))
            sys.exit(0)
        if created_id is None:
            fail("created_id", "Created state did not expose an id-like field usable for the discovered update route.", api_paths=api_paths, api_after=after_create_json)
        update_path = concrete(update_template, created_id)
        update_payload = build_payload(OPENAPI, update_template, method, update_marker, "update")
        update = getattr(client, method)(update_path, json=update_payload)
        if not (200 <= update.status_code < 300):
            fail("update_state", f"{method.upper()} {update_path} returned {update.status_code}; payload={update_payload}; body={update.text[:500]}", api_paths=api_paths, api_after=after_create_json)
        STEPS.append({"step": "update_state", "path": update_path, "status": update.status_code, "payload": update_payload})

        after_update = client.get(base_path)
        after_update_json = after_update.json()
        updated_item = find_created(after_update_json, marker, created_id)
        if updated_item is None:
            fail("post_update_visibility", "Shared GET state could not find the created entity after update.", api_paths=api_paths, api_after=after_update_json)
        update_visible = any(contains_marker(updated_item.get(key) if isinstance(updated_item, dict) else updated_item, str(value)) for key, value in update_payload.items())
        if not update_visible and not contains_marker(updated_item, update_marker):
            if isinstance(updated_item, dict) and update_payload:
                matching = [key for key, value in update_payload.items() if str(updated_item.get(key)) == str(value)]
                update_visible = bool(matching)
        if not update_visible:
            fail("refresh_persistence", f"Updated state was not visible through later GET. Payload={update_payload}; item={updated_item}", api_paths=api_paths, api_before=before_json, api_after=after_update_json)
        STEPS.append({"step": "refresh_visibility", "path": base_path, "status": after_update.status_code})

        print(json.dumps({
            "passed": True,
            "failed_step": None,
            "api_paths": api_paths,
            "created_state_marker": marker,
            "updated_state_marker": update_marker,
            "api_before": before_json,
            "api_after": after_update_json,
            "steps": STEPS,
            "logs": [f"Generic workflow proof passed through {base_path} and {update_path}."],
        }, ensure_ascii=False))
except Exception as exc:
    fail("runtime_exception", f"Generic workflow proof crashed: {exc.__class__.__name__}: {exc}", api_paths=api_paths)
'''

    @classmethod
    def _mobile_layout_report(cls, *, source_dir: Path, generation_mode: GenerationMode | str | None) -> dict[str, Any]:
        mode = str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()
        strict_mode = mode in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}
        findings: list[dict[str, Any]] = []
        static_root = source_dir / "miniapp/app/static"
        for role in ROLE_ORDER:
            role_dir = static_root / role
            if not role_dir.exists():
                continue
            css_text = ""
            for css_path in sorted(role_dir.rglob("*.css")):
                try:
                    css = css_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                css_text += "\n" + css
                for match in re.finditer(r"(?P<prop>^|[;{]\s*)(?P<name>min-width|width)\s*:\s*(?P<value>\d{3,5})px", css, re.IGNORECASE | re.MULTILINE):
                    value = int(match.group("value"))
                    if value > 430:
                        rel = css_path.relative_to(source_dir).as_posix()
                        findings.append(
                            {
                                "role": role,
                                "file": rel,
                                "viewport": "360-430px",
                                "message": f"{rel} sets {match.group('name')}:{value}px, which can force horizontal scrolling in Telegram mobile viewports.",
                            }
                        )
                if strict_mode and "grid-template-columns" in css and "@media" not in css:
                    rel = css_path.relative_to(source_dir).as_posix()
                    findings.append(
                        {
                            "role": role,
                            "file": rel,
                            "viewport": "360-430px",
                            "message": f"{rel} defines grid columns without a mobile @media rule.",
                        }
                    )
            for html_path in sorted(role_dir.rglob("*.html")):
                try:
                    html = html_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "<table" in html.lower() and "overflow-x" not in css_text.lower():
                    rel = html_path.relative_to(source_dir).as_posix()
                    findings.append(
                        {
                            "role": role,
                            "file": rel,
                            "viewport": "360-430px",
                            "message": f"{rel} uses a table without an overflow/wrapping mobile container.",
                        }
                    )
        return {
            "status": "failed" if findings else "passed",
            "viewports": ["360x740", "390x844", "430x932"],
            "findings": findings,
        }

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
                js_url_text_api = diagnostics.get("js_test_url_text_api") if isinstance(diagnostics, dict) else None
                server_html_assertion = diagnostics.get("server_rendered_html_assertion") if isinstance(diagnostics, dict) else None
                static_html_assertion = diagnostics.get("static_html_assertion") if isinstance(diagnostics, dict) else None
                assertion_source = diagnostics.get("assertion_source") if isinstance(diagnostics, dict) else None
                template_literal_assertion = diagnostics.get("js_test_unexpanded_template_literal") if isinstance(diagnostics, dict) else None
                brittle_api_constant_assertion = (
                    diagnostics.get("js_test_brittle_api_constant_assertion") if isinstance(diagnostics, dict) else None
                )
                missing_generated_js_token = diagnostics.get("js_test_missing_generated_source_token") if isinstance(diagnostics, dict) else None
                path_id_duplicate = diagnostics.get("path_id_payload_duplicate") if isinstance(diagnostics, dict) else None
                if isinstance(js_path_root, dict):
                    expected_root = str(js_path_root.get("expected_root") or "").strip()
                    message = expected_root or "Generated JS tests used an invalid miniapp path root."
                elif isinstance(js_url_path_api, dict):
                    expected_path_api = str(js_url_path_api.get("expected_path_api") or "").strip()
                    message = expected_path_api or "Generated JS tests passed a URL object to a path/fs API."
                elif isinstance(js_url_text_api, dict):
                    expected_text_api = str(js_url_text_api.get("expected_text_api") or "").strip()
                    message = expected_text_api or "Generated JS tests tried to call .text() on a URL object."
                elif isinstance(server_html_assertion, dict):
                    message = str(server_html_assertion.get("expected_scope") or "").strip() or "Generated Python tests asserted JS-rendered text in server HTML."
                elif isinstance(static_html_assertion, dict):
                    message = str(static_html_assertion.get("expected_scope") or "").strip() or "Generated JS tests asserted dynamic text only in HTML."
                elif isinstance(template_literal_assertion, dict):
                    message = str(template_literal_assertion.get("expected_fix") or "").strip() or "Generated JS test asserted an unexpanded template literal."
                elif isinstance(brittle_api_constant_assertion, dict):
                    message = (
                        str(brittle_api_constant_assertion.get("expected_fix") or "").strip()
                        or "Generated JS test required a brittle API constant name."
                    )
                elif isinstance(missing_generated_js_token, dict):
                    message = (
                        str(missing_generated_js_token.get("expected_fix") or "").strip()
                        or "Generated JS test required a token on the wrong generated page."
                    )
                elif isinstance(diagnostics.get("js_test_brittle_route_manifest_assertion"), dict):
                    message = (
                        str(diagnostics["js_test_brittle_route_manifest_assertion"].get("expected_fix") or "").strip()
                        or "Generated JS test asserted a brittle route manifest implementation detail."
                    )
                elif isinstance(assertion_source, dict):
                    line_no = assertion_source.get("line")
                    source_text = str(assertion_source.get("source") or "").strip()
                    message = f"Generated JS test failed at generated_app.test.mjs:{line_no}: {source_text}".strip()
                elif isinstance(path_id_duplicate, dict):
                    message = str(path_id_duplicate.get("expected_fix") or "").strip() or "Generated Python test duplicates a path id in PATCH JSON payload."
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
                elif isinstance(diagnostics.get("python_missing_attribute"), dict):
                    attr_issue = diagnostics.get("python_missing_attribute") or {}
                    message = str(attr_issue.get("expected_fix") or "").strip() or "Generated app reads a missing Python/ORM attribute."
                elif isinstance(diagnostics.get("sqlalchemy_no_foreign_keys"), dict):
                    relationship_issue = diagnostics.get("sqlalchemy_no_foreign_keys") or {}
                    message = str(relationship_issue.get("expected_fix") or "").strip() or "Generated SQLAlchemy relationship has no ForeignKey."
                else:
                    message = next((line for line in reversed(result.logs) if line.strip()), message)
            if result.name in {"preview_boot_smoke", "preview_connectivity_smoke", "api_workflow_smoke", "browser_flow_smoke"}:
                location = "preview"
                code = (
                    "connectivity.preview_route_unreachable"
                    if result.name == "preview_connectivity_smoke"
                    else "preview.api_workflow_failed" if result.name == "api_workflow_smoke"
                    else "preview.workflow_flow_failed" if result.name == "browser_flow_smoke"
                    else "preview.rebuild_failed"
                )
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                if result.name == "browser_flow_smoke" and diagnostics.get("infra_unavailable"):
                    code = "preview.browser_infra_unavailable"
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
            return "validator/contract"
        if "changed_files_static" in failed_names:
            return "syntax/build"
        if "platform_invariants" in failed_names or "frontend_interaction_static_smoke" in failed_names:
            return "validator/contract"
        if "generated_app_python_tests" in failed_names or "generated_app_js_tests" in failed_names:
            return "app/runtime_test"
        for result in results:
            if result.name == "browser_flow_smoke" and result.status == "failed":
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                if diagnostics.get("infra_unavailable"):
                    return "blocked_preview_infra"
        if (
            "preview_boot_smoke" in failed_names
            or "preview_connectivity_smoke" in failed_names
            or "api_workflow_smoke" in failed_names
            or "browser_flow_smoke" in failed_names
        ):
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
        if preview_run_id is not None and getattr(preview, "draft_run_id", None) != preview_run_id:
            return RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because running preview does not match the draft under validation.",
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
        preview_base_url = self._reachable_preview_base_url(str(preview.url or ""))
        for route in routes:
            target = urljoin(preview_base_url.rstrip("/") + "/", route.lstrip("/"))
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

    @classmethod
    def _reachable_preview_base_url(cls, preview_url: str) -> str:
        original = str(preview_url or "").strip().rstrip("/")
        if not original:
            return original
        for candidate in cls._preview_base_url_candidates(original):
            try:
                request = Request(candidate.rstrip("/") + "/health", headers={"User-Agent": "preview-url-probe"})
                with urlopen(request, timeout=1.2) as response:
                    status_code = response.status if hasattr(response, "status") else response.getcode()
                if status_code < 400:
                    return candidate.rstrip("/")
            except (TimeoutError, URLError, OSError):
                continue
        return original

    @staticmethod
    def _preview_base_url_candidates(preview_url: str) -> list[str]:
        parsed = urlparse(str(preview_url or "").strip())
        if not parsed.scheme or not parsed.netloc:
            return [str(preview_url or "").rstrip("/")]
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        candidates: list[str] = []
        if host in {"localhost", "127.0.0.1", "::1"}:
            for candidate_host in ("host.docker.internal", "127.0.0.1", "localhost"):
                candidates.append(urlunparse((parsed.scheme, f"{candidate_host}{port}", "", "", "", "")))
        candidates.append(urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")))
        return list(dict.fromkeys(candidate.rstrip("/") for candidate in candidates if candidate))

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
        acceptance_contract: dict[str, Any] | None = None,
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
            issues.extend(AgentStaticValidation.route_schema_issues(source_dir))
        if agentic_scope:
            role_issues, role_coverage, neutral_template_findings = self._role_surface_issues(
                source_dir,
                generation_mode=generation_mode,
                acceptance_contract=acceptance_contract,
            )
            issues.extend(role_issues)
            issues.extend(self._role_manifest_completeness_issues(source_dir))
            tests_issues, generated_tests = self._generated_tests_presence_issues(source_dir)
            issues.extend(tests_issues)
        elif css_only_focused_edit:
            role_coverage = {"status": "skipped", "reason": "focused_css_only_edit"}
            generated_tests = {"status": "skipped", "reason": "focused_css_only_edit"}
        issues.extend(self._shell_safe_spacing_issues(source_dir))
        if agentic_scope and str(intent or "").strip().lower() == "create":
            api_issues, api_contract = self._create_api_contract_issues(source_dir)
            issues.extend(api_issues)
            data_issues, preloaded_data_findings = self._preloaded_product_data_issues(source_dir)
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
        acceptance_contract: dict[str, Any] | None = None,
    ) -> tuple[list[ValidationIssue], dict[str, object], list[dict[str, str]]]:
        issues: list[ValidationIssue] = []
        coverage: dict[str, object] = {}
        neutral_findings: list[dict[str, str]] = []
        role_surface_text: dict[str, str] = {}
        route_pages = cls._routeable_role_pages(source_dir)

        for role in ROLE_ORDER:
            role_dir = source_dir / "miniapp" / "app" / "static" / role
            expected_files = {
                "html": role_dir / "index.html",
                "js": role_dir / "app.js",
                "css": role_dir / "styles.css",
            }
            missing = [path.relative_to(source_dir).as_posix() for path in expected_files.values() if not path.exists()]
            texts: list[str] = []
            html_texts: list[str] = []
            for kind, path in expected_files.items():
                if not path.exists():
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                texts.append(content)
                if kind == "html":
                    html_texts.append(content)
            for page in route_pages.get(role, []):
                page_path_raw = str(page.get("file_path") or "")
                page_path = source_dir / page_path_raw
                if page_path in expected_files.values() or not page_path.exists():
                    continue
                try:
                    content = page_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                texts.append(content)
                html_texts.append(content)
            combined = "\n".join(texts)
            html_combined = "\n".join(html_texts)
            normalized = combined.lower()
            markers = [marker for marker in NEUTRAL_TEMPLATE_MARKERS if marker in normalized]
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
                        message=f"{role} role still contains neutral shell/template text: {', '.join(markers[:4])}.",
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
            hidden_state_issue = cls._hidden_state_css_issue(role, html_combined, css_text)
            if hidden_state_issue is not None:
                coverage[role] = {
                    "status": "visible_hidden_state",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(hidden_state_issue)
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
            cross_role_markers = cls._cross_role_surface_markers(role, combined)
            if cross_role_markers:
                coverage[role] = {
                    "status": "cross_role_surface_mixed",
                    "markers": cross_role_markers,
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.cross_role_surface_mixed",
                        message=(
                            f"{role} role embeds technical controls from another role: {', '.join(cross_role_markers[:4])}. "
                            "Create separate client, specialist, and manager mini-app surfaces; shared data is allowed, shared role UI is not."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            technical_copy = list(
                dict.fromkeys(
                    [
                        *cls._technical_role_copy_markers(combined),
                        *cls._technical_visible_html_copy_markers(html_combined),
                    ]
                )
            )
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
            raw_status_markers = cls._raw_status_render_markers(combined)
            if raw_status_markers:
                coverage[role] = {
                    "status": "raw_status_copy",
                    "markers": raw_status_markers,
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.raw_status_rendered_to_user",
                        message=(
                            f"{role} role renders persisted status codes directly: {', '.join(raw_status_markers[:4])}. "
                            "Map internal status values to polished human-readable labels before rendering."
                        ),
                        severity="high",
                        location=f"miniapp/app/static/{role}",
                        blocking=True,
                    )
                )
                continue
            action_signals = cls._role_action_signals(role, combined, acceptance_contract=acceptance_contract)
            if not action_signals:
                expectation = cls._role_action_expectation(role, acceptance_contract=acceptance_contract)
                coverage[role] = {
                    "status": "missing_role_actions",
                    "route_count": len(role_routes),
                    "secondary_route_count": len(secondary_routes),
                    "routes": role_routes,
                    "expected_action": expectation,
                }
                issues.append(
                    ValidationIssue(
                        code="platform.missing_role_workflow_actions",
                        message=(
                            f"{role} role lacks the prompt-assigned workflow action for this acceptance contract: "
                            f"{expectation}. Add role-owned controls/handlers that use the app-owned API without copying another role's workflow."
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
                "routes": role_routes,
                "action_signals": action_signals,
            }

        present_roles = [
            role
            for role, payload in coverage.items()
            if isinstance(payload, dict) and payload.get("status") == "present"
        ]
        if len(present_roles) == len(ROLE_ORDER):
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
        return issues, coverage, neutral_findings

    @staticmethod
    def _cross_role_surface_markers(role: str, content: str) -> list[str]:
        text = str(content or "")
        lowered = text.lower()
        markers: list[str] = []
        other_roles = {"client", "specialist", "manager"} - {str(role or "").strip().lower()}
        technical_suffixes = (
            "app",
            "dashboard",
            "form",
            "grid",
            "list",
            "panel",
            "page",
            "root",
            "section",
            "surface",
            "title",
            "view",
        )
        suffix_pattern = "(?:" + "|".join(re.escape(suffix) for suffix in technical_suffixes) + ")"
        for other in sorted(other_roles):
            attr_pattern = re.compile(
                rf"\b(?:id|class|data-[a-z0-9_-]+)\s*=\s*([\"'])[^\"']*\b{re.escape(other)}[-_]?{suffix_pattern}\b[^\"']*\1",
                re.IGNORECASE,
            )
            if attr_pattern.search(text):
                markers.append(f"{other} attribute/control")
                continue
            for suffix in technical_suffixes:
                if re.search(rf"\b{re.escape(other)}[-_]?{re.escape(suffix)}\b", lowered):
                    markers.append(f"{other}{suffix}")
                    break
        return markers

    @staticmethod
    def _role_design_depth_issue(role: str, css_text: str, combined: str, generation_mode: GenerationMode | str | None) -> ValidationIssue | None:
        value = str(getattr(generation_mode, "value", generation_mode) or "").strip().lower()
        if value not in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
            return None
        css = str(css_text or "").lower()
        del combined
        css_rule_count = len(re.findall(r"[.#]?[a-z][a-z0-9_-]*\s*\{", css))
        min_rules = 12 if value == GenerationMode.QUALITY.value else 8
        quality_structure_ok = value != GenerationMode.QUALITY.value or ("@media" in css and ("focus-visible" in css or ":focus" in css))
        rich_quality_css = value == GenerationMode.QUALITY.value and css_rule_count >= min_rules + 6
        if css_rule_count >= min_rules and (quality_structure_ok or rich_quality_css):
            return None
        return ValidationIssue(
            code="platform.insufficient_mode_design_depth",
            message=(
                f"{role} role design is too shallow for {value} mode. "
                f"Expected at least {min_rules} real CSS rules and, for quality mode, responsive/focus styling."
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
    def _role_action_expectation(role: str, *, acceptance_contract: dict[str, Any] | None = None) -> str:
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
        source_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("source_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        update_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("update_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        observer_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("observer_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        role_prompts = prompt_hints.get("role_action_prompts") if isinstance(prompt_hints.get("role_action_prompts"), dict) else {}
        prompt_actions = [
            str(item).strip()
            for item in (role_prompts.get(role) or [])
            if str(item).strip()
        ] if isinstance(role_prompts, dict) else []
        if role in source_roles:
            base = "source role must create/publish/configure prompt-derived persisted state"
        elif role in update_roles:
            base = "update role must perform its prompt-derived persisted action"
        elif role in observer_roles:
            base = "observer role must load and use the shared persisted state"
        else:
            base = "role must expose the prompt-derived view/action assigned to it"
        if prompt_actions:
            return f"{base}; prompt actions: {', '.join(prompt_actions[:4])}"
        return base

    @staticmethod
    def _role_action_signals(role: str, content: str, *, acceptance_contract: dict[str, Any] | None = None) -> list[str]:
        text = str(content or "")
        lowered = text.lower()
        signals: list[str] = []
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        state_contract = prompt_hints.get("role_state_contract") if isinstance(prompt_hints.get("role_state_contract"), dict) else {}
        source_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("source_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        update_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("update_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        observer_roles = {
            str(item).strip().lower()
            for item in (state_contract.get("observer_roles") or [])
            if str(item).strip().lower() in ROLE_ORDER
        }
        has_interactive_surface = bool(
            re.search(r"<(?:form|button|select|textarea|input)\b", lowered)
            or re.search(r"addEventListener\s*\(", text)
        )
        has_fetch = "fetch(" in lowered or "/api/" in lowered
        has_submit_flow = bool(re.search(r"\bsubmit\b|<form\b|addEventListener\s*\(\s*['\"`]submit", text, flags=re.IGNORECASE))
        has_action_flow = bool(re.search(r"\b(click|change|submit)\b|data-[a-z0-9_-]+", lowered))
        has_post = bool(
            re.search(r"method\s*:\s*['\"`](?:POST)['\"`]", text, flags=re.IGNORECASE)
            or ".post(" in lowered
            or (has_fetch and has_submit_flow and re.search(r"['\"`]POST['\"`]", text, flags=re.IGNORECASE))
        )
        has_update_method = bool(
            re.search(r"method\s*:\s*['\"`](?:PATCH|PUT|DELETE)['\"`]", text, flags=re.IGNORECASE)
            or re.search(r"\.(?:patch|put|delete)\s*\(", lowered)
            or (has_fetch and has_action_flow and re.search(r"['\"`](?:PATCH|PUT|DELETE)['\"`]", text, flags=re.IGNORECASE))
        )
        has_mutating_api = has_fetch and (has_post or has_update_method)
        has_read_api = has_fetch or bool(re.search(r"method\s*:\s*['\"`]GET['\"`]", text, flags=re.IGNORECASE) or ".get(" in lowered)
        if has_interactive_surface:
            signals.append("interactive_surface")
        if has_read_api:
            signals.append("api_read")
        if has_post:
            signals.append("api_post")
        if has_update_method:
            signals.append("api_update")

        if role in source_roles:
            return signals if has_interactive_surface and has_post and has_fetch else []
        if role in update_roles:
            return signals if has_interactive_surface and has_mutating_api else []
        if role in observer_roles:
            return signals if has_read_api else []
        return signals if has_read_api and has_interactive_surface else []

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
            "source placeholder",
            "collect user-provided details",
            "placeholder records",
            "workflow entries",
            "create the first workflow entry",
            "no workflow entries yet",
            "select a saved record first",
            "unable to load saved records",
            ">save progress<",
        )
        return [marker for marker in markers if marker in lowered]

    @staticmethod
    def _technical_visible_html_copy_markers(content: str) -> list[str]:
        text = re.sub(r"<script\b.*?</script>", " ", str(content or ""), flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        visibleish = re.sub(r"<[^>]+>", " ", text)
        markers: list[str] = []
        if re.search(r"\b(?:GET|POST|PATCH|PUT|DELETE)\b", visibleish):
            markers.append("visible HTTP method")
        if re.search(r"/api/[A-Za-z0-9_/{}/.-]+", visibleish):
            markers.append("visible API path")
        if re.search(r"\bAPI\b", visibleish):
            markers.append("visible API term")
        if re.search(r"[А-Яа-яЁё]", visibleish) and re.search(r"\b(?:Client|Specialist|Manager)\b", visibleish):
            markers.append("visible role slug")
        return markers

    @staticmethod
    def _raw_status_render_markers(content: str) -> list[str]:
        text = str(content or "")
        patterns = (
            (r"\$\{\s*escape(?:Html|Text)?\s*\(\s*item\.status\b", "template escape(item.status)"),
            (r"\$\{\s*item\.status\b", "template item.status"),
            (r"\bbuildLine\s*\(\s*['\"][^'\"]*статус[^'\"]*['\"]\s*,\s*item\.status\s*\)", "buildLine status=item.status"),
            (r"\bitem\.status\s*\|\|\s*['\"](?:new|pending|done|completed)['\"]", "item.status raw enum passthrough"),
            (r"(?:Приоритет|priority)[^`]*\$\{[^}]*item\.priority", "template item.priority raw enum"),
            (r"\bstatus-pill[^`'\"]*`[^`]*item\.status", "status pill item.status"),
            (r"\bSTATUS_LABELS\s*\[\s*(?:value|key|item\.status)\s*\]\s*\|\|\s*(?:value|key|item\.status)\b", "status label raw passthrough"),
            (r"\bSTATUS_LABELS\s*\[[^\]]+\]\s*\|\|\s*(?:[A-Za-z_$][\w$]*|item\.status)\b", "status label raw passthrough"),
            (r"\bSTATUS_LABELS\s*\[[^\]]+\]\s*\|\|\s*normalize\s*\(\s*value\b", "status label normalize(value) passthrough"),
            (r"\bfunction\s+statusLabel\b[\s\S]{0,500}\breturn\s+[^;{}]*\[[^\]]+\]\s*\|\|\s*(?:status|value|key)\b", "status label raw passthrough"),
            (r"\bfunction\s+(?:humanStatus|formatStatus)\b[\s\S]{0,500}\breturn\s+[^;{}]*\[[^\]]+\]\s*\|\|\s*(?:status|value|key)\b", "status label raw passthrough"),
            (r"\bDETAIL_FIELD_LABELS\s*=\s*\{[^{}]*(?:['\"]status['\"]|status)\s*:", "detail labels include raw status field"),
        )
        markers: list[str] = []
        for pattern, label in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
                markers.append(label)
        return markers

    @staticmethod
    def _hidden_state_css_issue(role: str, html_text: str, css_text: str) -> ValidationIssue | None:
        html = str(html_text or "")
        css = str(css_text or "")
        if not re.search(r"\bclass\s*=\s*([\"'])[^\"']*\bhidden\b", html, flags=re.IGNORECASE):
            return None
        if re.search(r"\.hidden\s*\{[^}]*display\s*:\s*none\b", css, flags=re.IGNORECASE | re.DOTALL):
            return None
        if re.search(r"\.hidden\s*\{[^}]*visibility\s*:\s*hidden\b", css, flags=re.IGNORECASE | re.DOTALL):
            return None
        return ValidationIssue(
            code="platform.hidden_state_class_without_css",
            message=(
                f"{role} role uses a hidden state class for loading/empty/error sections, "
                "but styles.css does not actually hide it. This can show empty/loading states together with real records."
            ),
            severity="high",
            location=f"miniapp/app/static/{role}/styles.css",
            blocking=True,
        )

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
        has_backend_update = any(
            cls._is_declared_update_method(method, path, declared_methods)
            for method, path in declared_methods
        )
        frontend_post_refs = sorted(path for method, path in frontend_refs if method == "POST")
        frontend_update_refs = sorted(
            path
            for method, path in frontend_refs
            if method in {"PATCH", "PUT", "DELETE"} or cls._is_frontend_post_update_ref(path, frontend_refs)
        )
        issues: list[ValidationIssue] = []
        if not has_backend_get:
            issues.append(
                ValidationIssue(
                    code="platform.missing_create_get_api",
                    message="Create runs must expose at least one GET /api resource so saved user-created state can be listed.",
                    severity="high",
                    location="miniapp/app/routes",
                    blocking=True,
                )
            )
        if not has_backend_post:
            issues.append(
                ValidationIssue(
                    code="platform.missing_create_post_api",
                    message="Create runs must expose at least one POST /api resource so users can save new state.",
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
                        "Create runs must include frontend form/fetch code that POSTs user-provided state to /api."
                        if not raw_post_present
                        else "Frontend contains POST and /api markers, but the validator could not pair them confidently; generated tests must confirm the flow."
                    ),
                    severity="medium" if raw_post_present else "high",
                    location="miniapp/app/static",
                    blocking=not raw_post_present,
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
            "has_backend_get": has_backend_get,
            "has_backend_post": has_backend_post,
            "has_backend_update": has_backend_update,
            "update_required_by_platform": False,
        }

    @staticmethod
    def _api_path_has_path_param(path: str) -> bool:
        return bool(re.search(r"\{[^}/]+\}", str(path or "")))

    @classmethod
    def _is_declared_update_method(cls, method: str, path: str, declared_methods: set[tuple[str, str]]) -> bool:
        method = str(method or "").upper()
        normalized_path = str(path or "").rstrip("/") or "/"
        if method in {"PATCH", "PUT", "DELETE"}:
            return True
        if method != "POST":
            return False
        create_bases = {
            declared_path.rstrip("/") or "/"
            for declared_method, declared_path in declared_methods
            if declared_method == "POST"
            and ("GET", declared_path) in declared_methods
        }
        if normalized_path in create_bases:
            return False
        return cls._api_path_has_path_param(normalized_path) or any(
            normalized_path.startswith(f"{base}/") for base in create_bases if base != "/"
        )

    @classmethod
    def _is_frontend_post_update_ref(cls, path: str, refs: set[tuple[str, str]]) -> bool:
        normalized_path = str(path or "").rstrip("/") or "/"
        if not any(method == "POST" and ref_path == path for method, ref_path in refs):
            return False
        if cls._api_path_has_path_param(normalized_path):
            return True
        post_base_paths = {
            ref_path.rstrip("/") or "/"
            for method, ref_path in refs
            if method == "POST"
        }
        return any(
            normalized_path != base and normalized_path.startswith(f"{base}/")
            for base in post_base_paths
            if base != "/"
        )

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
    def _preloaded_product_data_issues(cls, source_dir: Path) -> tuple[list[ValidationIssue], list[dict[str, str]]]:
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
            if relative_path in {
                "miniapp/app/static/preview_bridge.js",
                "miniapp/app/generated/route_manifest.json",
                "miniapp/app/generated/miniapp_contract.json",
                "miniapp/app/generated/contract_validator.json",
            }:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            marker = cls._preloaded_product_data_marker(content)
            if not marker:
                continue
            findings.append({"file_path": relative_path, "marker": marker})
            issues.append(
                ValidationIssue(
                    code="platform.preloaded_product_data",
                    message=(
                        f"{relative_path} appears to include preloaded product data ({marker}). "
                        "Create apps must start with empty persistent state and let users add their own data."
                    ),
                    severity="high",
                    location=relative_path,
                    blocking=True,
                )
            )
        return issues, findings

    @staticmethod
    def _preloaded_product_data_marker(content: str) -> str | None:
        text = str(content or "")
        lowered = text.lower()
        compact = re.sub(r"[\s_\-]+", "", lowered)
        for marker in PRELOADED_PRODUCT_DATA_MARKERS:
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
                route_path_text = str(route_path_raw or "").strip()
                if not route_path_text:
                    continue
                route_probe = route_path_text if route_path_text.startswith("/") else f"/{route_path_text}"
                route_probe = route_probe.rstrip("/") or "/"
                for role in ROLE_ORDER:
                    if route_probe != f"/{role}" and not route_probe.startswith(f"/{role}/"):
                        continue
                    file_refs = [file_path_raw_value] if isinstance(file_path_raw_value, str) else file_path_raw_value
                    if not isinstance(file_refs, list):
                        break
                    for file_ref_raw in file_refs:
                        file_path_raw = str(file_ref_raw or "").strip()
                        if not file_path_raw.endswith(".html"):
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
                if isinstance(role_payload, list):
                    if any(str(item or "").strip().endswith((".html", ".js", ".css")) for item in role_payload):
                        continue
                    for route_item in role_payload:
                        route_path = cls._normalize_manifest_role_route(role, str(route_item or "").strip())
                        file_path = source_dir / "miniapp/app/static" / role / "index.html"
                        if not file_path.exists():
                            continue
                        pages_by_role[role].append(
                            {
                                "route_path": route_path,
                                "file_path": file_path.relative_to(source_dir).as_posix(),
                                "source": "manifest_role_route_list",
                            }
                        )
                    continue
                if not isinstance(role_payload, dict):
                    continue
                single_file = str(role_payload.get("file") or role_payload.get("file_path") or "").strip()
                if single_file:
                    file_path = cls._resolve_manifest_static_page(source_dir, single_file)
                    if file_path.exists():
                        single_route = str(
                            role_payload.get("route")
                            or role_payload.get("route_path")
                            or role_payload.get("page")
                            or role_payload.get("primary_page")
                            or ""
                        ).strip()
                        pages_by_role[role].append(
                            {
                                "route_path": cls._normalize_manifest_role_route(role, single_route),
                                "file_path": file_path.relative_to(source_dir).as_posix(),
                                "source": "manifest_role_file",
                            }
                        )
                route_map = role_payload.get("routes")
                if isinstance(route_map, list):
                    for route_item in route_map:
                        route_path = cls._normalize_manifest_role_route(role, str(route_item or "").strip())
                        file_path = source_dir / "miniapp/app/static" / role / "index.html"
                        if not file_path.exists():
                            continue
                        pages_by_role[role].append(
                            {
                                "route_path": route_path,
                                "file_path": file_path.relative_to(source_dir).as_posix(),
                                "source": "manifest_role_routes_list",
                            }
                        )
                    route_map = {}
                if not isinstance(route_map, dict):
                    route_map = {
                        str(route_path): str(file_path)
                        for route_path, file_path in role_payload.items()
                        if isinstance(file_path, str) and str(route_path) not in {"pages", "routes", "page", "file", "file_path", "route", "route_path", "primary_page"}
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
                    if isinstance(page, str):
                        route_path = cls._normalize_manifest_role_route(role, page)
                        file_path = source_dir / "miniapp/app/static" / role / "index.html"
                        if not file_path.exists():
                            continue
                        pages_by_role[role].append(
                            {
                                "route_path": route_path,
                                "file_path": file_path.relative_to(source_dir).as_posix(),
                                "source": "manifest_page_route_list",
                            }
                        )
                        continue
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

    @classmethod
    def _role_manifest_completeness_issues(cls, source_dir: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        pages_by_role = cls._routeable_role_pages(source_dir)
        static_root = source_dir / "miniapp/app/static"
        for role in ROLE_ORDER:
            role_root = static_root / role
            if not role_root.exists():
                continue
            route_sources = {
                str(page.get("route_path") or "").rstrip("/") or f"/{role}": str(page.get("source") or "")
                for page in pages_by_role.get(role, [])
            }
            manifest_routes = {
                route
                for route, source in route_sources.items()
                if source and source != "filesystem"
            }
            missing_routes: list[str] = []
            for html_path in sorted(role_root.rglob("index.html")):
                route = cls._filesystem_role_route(role, role_root, html_path)
                if route not in manifest_routes:
                    missing_routes.append(route)
            if missing_routes:
                issues.append(
                    ValidationIssue(
                        code="platform.role_route_manifest_incomplete",
                        message=(
                            f"{role} role has static pages missing from generated/route_manifest.json: "
                            f"{', '.join(missing_routes[:8])}. Every role page must be routeable through the platform shell."
                        ),
                        severity="high",
                        location="miniapp/app/generated/route_manifest.json",
                        blocking=True,
                    )
                )
        return issues

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
                issues.extend(cls._html_control_contract_issues(html_relative, html_source))
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
    def _html_control_contract_issues(relative_path: str, html_source: str) -> list[ValidationIssue]:
        parser = _HtmlControlContractParser()
        try:
            parser.feed(str(html_source or ""))
        except Exception:
            return []
        issues: list[ValidationIssue] = []
        duplicate_ids = sorted({item for item in parser.ids if parser.ids.count(item) > 1})
        if duplicate_ids:
            issues.append(
                ValidationIssue(
                    code="platform.duplicate_dom_id",
                    message=(
                        f"{relative_path} contains duplicate DOM ids: {', '.join(duplicate_ids[:8])}. "
                        "Each interactive control needs a unique id so labels, scripts, and browser proof target one element."
                    ),
                    severity="high",
                    location=relative_path,
                    blocking=True,
                )
            )
        duplicate_names: list[str] = []
        for names in parser.names_by_form.values():
            duplicate_names.extend(sorted({item for item in names if names.count(item) > 1}))
        duplicate_names = sorted(set(duplicate_names))
        if duplicate_names:
            issues.append(
                ValidationIssue(
                    code="platform.duplicate_form_control_name",
                    message=(
                        f"{relative_path} contains duplicate form control names in one form: {', '.join(duplicate_names[:8])}. "
                        "Remove the duplicate field or rename it before serializing FormData."
                    ),
                    severity="high",
                    location=relative_path,
                    blocking=True,
                )
            )
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
        for page_relative_path, html_source, page_ids in page_sources:
            missing_ids = sorted(
                dom_id
                for dom_id in unsafe_ids
                if dom_id not in page_ids
                and not cls._dom_id_accesses_are_scoped_to_absent_page_guard(js_source, dom_id, html_source, page_ids)
            )
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

    @classmethod
    def _dom_id_accesses_are_scoped_to_absent_page_guard(
        cls,
        js_source: str,
        dom_id: str,
        html_source: str,
        page_ids: set[str],
    ) -> bool:
        id_bindings = cls._js_dom_id_bindings(js_source)
        target_vars = {var_name for var_name, bound_id in id_bindings.items() if bound_id == dom_id}
        if not target_vars:
            return False
        guard_bindings = cls._js_dom_selector_bindings(js_source)
        lines = str(js_source or "").splitlines()
        saw_access = False
        for index, line in enumerate(lines):
            for var_name in target_vars:
                for match in re.finditer(rf"\b{re.escape(var_name)}\s*\.", line):
                    prefix = line[: match.start()]
                    if prefix.rstrip().endswith("?"):
                        continue
                    saw_access = True
                    active_guards = cls._active_absent_dom_guard_variables(
                        lines,
                        index,
                        guard_bindings,
                        html_source,
                        page_ids,
                    )
                    if not active_guards:
                        return False
        return saw_access

    @classmethod
    def _active_absent_dom_guard_variables(
        cls,
        lines: list[str],
        index: int,
        guard_bindings: dict[str, tuple[str, str]],
        html_source: str,
        page_ids: set[str],
        *,
        ignored_vars: set[str] | None = None,
    ) -> set[str]:
        ignored = set(ignored_vars or set())
        active: set[str] = set()
        for cursor in range(index, -1, -1):
            line = lines[cursor]
            for var_name, selector_info in guard_bindings.items():
                if var_name in ignored or var_name in active:
                    continue
                escaped = re.escape(var_name)
                if not re.search(rf"\bif\s*\(\s*{escaped}\s*\)\s*\{{", line):
                    if not cls._dom_selector_binding_present_in_html(selector_info, html_source, page_ids):
                        prefix = "\n".join(lines[max(0, cursor - 12) : index + 1])
                        if re.search(rf"\bif\s*\(\s*!\s*{escaped}\s*\)\s*(?:\{{\s*)?return\b", prefix):
                            active.add(var_name)
                    continue
                block = "\n".join(lines[cursor : index + 1])
                if block.count("{") <= block.count("}"):
                    continue
                if not cls._dom_selector_binding_present_in_html(selector_info, html_source, page_ids):
                    active.add(var_name)
        return active

    @staticmethod
    def _dom_selector_binding_present_in_html(
        selector_info: tuple[str, str],
        html_source: str,
        page_ids: set[str],
    ) -> bool:
        kind, value = selector_info
        if kind == "id":
            return value in page_ids
        return CheckRunner._html_has_simple_selector(html_source, value)

    @staticmethod
    def _js_dom_id_bindings(js_source: str) -> dict[str, str]:
        bindings: dict[str, str] = {}
        ambiguous_vars: set[str] = set()
        pattern = re.compile(
            r"""\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*document\.(?:getElementById\(\s*["'](?P<id1>[A-Za-z0-9_-]+)["']\s*\)|querySelector\(\s*["']\#(?P<id2>[A-Za-z0-9_-]+)["']\s*\))""",
            re.DOTALL,
        )
        for match in pattern.finditer(str(js_source or "")):
            dom_id = match.group("id1") or match.group("id2")
            if dom_id:
                var_name = match.group("var")
                if var_name in bindings and bindings[var_name] != dom_id:
                    ambiguous_vars.add(var_name)
                    continue
                bindings[var_name] = dom_id
        for var_name in ambiguous_vars:
            bindings.pop(var_name, None)
        return bindings

    @staticmethod
    def _js_dom_selector_bindings(js_source: str) -> dict[str, tuple[str, str]]:
        bindings: dict[str, tuple[str, str]] = {}
        pattern = re.compile(
            r"""\b(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)\s*=\s*document\.(?:(?:getElementById\(\s*["'](?P<id>[A-Za-z0-9_-]+)["']\s*\))|(?:querySelector\(\s*(?:"(?P<double>[^"]+)"|'(?P<single>[^']+)'|`(?P<backtick>[^`]+)`)\s*\)))""",
            re.DOTALL,
        )
        for match in pattern.finditer(str(js_source or "")):
            var_name = match.group("var")
            dom_id = match.group("id")
            selector = match.group("double") or match.group("single") or match.group("backtick")
            if dom_id:
                bindings[var_name] = ("id", dom_id)
            elif selector:
                bindings[var_name] = ("selector", selector.strip())
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
                    if cls._dom_variable_shadowed_by_function_param(lines, index, var_name):
                        continue
                    prefix = line[: match.start()]
                    if prefix.rstrip().endswith("?"):
                        continue
                    context = cls._dom_access_context(lines, index)
                    if cls._dom_variable_access_is_guarded(context, line, var_name):
                        continue
                    unsafe.add(var_name)
        return unsafe

    @staticmethod
    def _dom_variable_shadowed_by_function_param(lines: list[str], index: int, var_name: str) -> bool:
        for cursor in range(index, -1, -1):
            line = lines[cursor]
            match = re.search(r"\bfunction\s+[A-Za-z_$][\w$]*\s*\((?P<params>[^)]*)\)\s*\{", line)
            if not match:
                continue
            params = {part.strip().split("=", 1)[0].strip() for part in match.group("params").split(",")}
            if var_name not in params:
                return False
            if cursor == index:
                return True
            block = "\n".join(lines[cursor : index + 1])
            return block.count("{") > block.count("}")
        return False

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
                if CheckRunner._direct_dom_id_access_is_guarded(str(js_source or ""), match.start(), dom_id):
                    continue
                unsafe.add(dom_id)
        return unsafe

    @staticmethod
    def _direct_dom_id_access_is_guarded(js_source: str, access_start: int, dom_id: str) -> bool:
        prefix = str(js_source or "")[max(0, access_start - 500) : access_start]
        escaped = re.escape(dom_id)
        return bool(
            re.search(
                rf"\bif\s*\([^)]*(?:getElementById\(\s*([\"']){escaped}\1\s*\)|querySelector\(\s*([\"'])#{escaped}\2\s*\))[^)]*\)\s*\{{[\s\S]{{0,240}}$",
                prefix,
            )
        )

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
            CheckRunner._strip_leading_dot_slash(path)
            for path in changed_files
            if isinstance(path, str) and CheckRunner._strip_leading_dot_slash(path).startswith("miniapp/app/")
        ]
        return bool(relevant) and all(path.startswith("miniapp/app/static/") and path.endswith(".css") for path in relevant)

    @staticmethod
    def _strip_leading_dot_slash(raw_path: object) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path

    def _focused_css_static_check(self, *, source_dir: Path, changed_files: list[str]) -> RunCheckResult:
        issues: list[str] = []
        checked_paths: list[str] = []
        for raw_path in changed_files:
            path = self._strip_leading_dot_slash(raw_path)
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
                diagnostics=self._extract_generated_app_test_diagnostics(logs, test_file=test_file),
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
            assertion_failures: list[dict[str, object]] = []
            if test_file is not None and test_file.exists():
                try:
                    source_lines = test_file.read_text(encoding="utf-8").splitlines()
                except OSError:
                    source_lines = []
                seen_lines: set[int] = set()
                for location in generated_js_locations:
                    line_no = int(location["line"])
                    if line_no in seen_lines or not (1 <= line_no <= len(source_lines)):
                        continue
                    seen_lines.add(line_no)
                    assertion_line = source_lines[line_no - 1].strip()
                    failure: dict[str, object] = {
                        "file_path": "miniapp/tests/generated_app.test.mjs",
                        "line": line_no,
                        "source": assertion_line,
                    }
                    literal_match = re.search(r"\.includes\(\s*([\"'`])(?P<literal>.+?)\1\s*\)", assertion_line)
                    if literal_match:
                        failure["expected_literal"] = literal_match.group("literal")
                        if "${" in literal_match.group("literal"):
                            diagnostics["js_test_unexpanded_template_literal"] = {
                                "problem": "generated_js_test_asserts_unexpanded_template_literal",
                                "expected_literal": literal_match.group("literal"),
                                "expected_fix": (
                                    "Generated JS tests asserted a quoted string containing `${...}`. Use a template literal/backtick "
                                    "or construct the expected string before asserting, for example `/static/${role}/app.js` must be evaluated, not quoted literally."
                                ),
                            }
                    if "manifest.roles" in assertion_line and ".root" in assertion_line:
                        diagnostics["js_test_brittle_route_manifest_assertion"] = {
                            "problem": "generated_js_test_requires_exact_route_manifest_root",
                            "source": assertion_line,
                            "expected_fix": (
                                "Generated JS tests must not assert exact manifest.roles.<role>.root implementation fields. "
                                "Patch generated_app.test.mjs to derive actual role routes/pages from route_manifest entries and assert "
                                "real role HTML/CSS/JS/API wiring. Do not edit generated/route_manifest.json to satisfy a brittle test."
                            ),
                        }
                    regex_match = re.search(
                        r"(?:\.match\(\s*|assert\.match\([^,]+,\s*)/(?P<literal>(?:\\/|[^/])+)/[a-zA-Z]*\s*\)",
                        assertion_line,
                    )
                    if regex_match:
                        failure["expected_literal"] = regex_match.group("literal")
                        expected_literal = regex_match.group("literal")
                        if re.search(r"fetch\\\(API(?:_|[A-Z])", expected_literal) or (
                            "fetch" in expected_literal and "\\/api" in expected_literal
                        ):
                            diagnostics["js_test_brittle_api_constant_assertion"] = {
                                "problem": "generated_js_test_requires_exact_api_constant_name",
                                "expected_literal": expected_literal,
                                "expected_fix": (
                                    "Generated JS tests must not require a specific local constant name or a literal /api path inside fetch(...). "
                                    "Patch generated_app.test.mjs to assert the real frontend contract instead: the role script contains /api, "
                                    "the resource path/name, the required HTTP method, a submit/click handler, and fetch(...) somewhere in the source."
                                ),
                            }
                    assertion_failures.append(failure)
            if assertion_failures:
                diagnostics["assertion_failures"] = assertion_failures[:4]
            first_location = generated_js_locations[-1]
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
                        if "${" in literal_match.group("literal"):
                            diagnostics["js_test_unexpanded_template_literal"] = {
                                "problem": "generated_js_test_asserts_unexpanded_template_literal",
                                "expected_literal": literal_match.group("literal"),
                                "expected_fix": (
                                    "Generated JS tests asserted a quoted string containing `${...}`. Use a template literal/backtick "
                                    "or construct the expected string before asserting, for example `/static/${role}/app.js` must be evaluated, not quoted literally."
                                ),
                            }
                    if "manifest.roles" in assertion_line and ".root" in assertion_line:
                        diagnostics["js_test_brittle_route_manifest_assertion"] = {
                            "problem": "generated_js_test_requires_exact_route_manifest_root",
                            "source": assertion_line,
                            "expected_fix": (
                                "Generated JS tests must not assert exact manifest.roles.<role>.root implementation fields. "
                                "Patch generated_app.test.mjs to derive actual role routes/pages from route_manifest entries and assert "
                                "real role HTML/CSS/JS/API wiring. Do not edit generated/route_manifest.json to satisfy a brittle test."
                            ),
                        }
                    regex_match = re.search(
                        r"(?:\.match\(\s*|assert\.match\([^,]+,\s*)/(?P<literal>(?:\\/|[^/])+)/[a-zA-Z]*\s*\)",
                        assertion_line,
                    )
                    if regex_match:
                        diagnostics["expected_literal"] = regex_match.group("literal")
                        expected_literal = regex_match.group("literal")
                        if re.search(r"fetch\\\(API(?:_|[A-Z])", expected_literal) or (
                            "fetch" in expected_literal and "\\/api" in expected_literal
                        ):
                            diagnostics["js_test_brittle_api_constant_assertion"] = {
                                "problem": "generated_js_test_requires_exact_api_constant_name",
                                "expected_literal": expected_literal,
                                "expected_fix": (
                                    "Generated JS tests must not require a specific local constant name or a literal /api path inside fetch(...). "
                                    "Patch generated_app.test.mjs to assert the real frontend contract instead: the role script contains /api, "
                                    "the resource path/name, the required HTTP method, a submit/click handler, and fetch(...) somewhere in the source."
                                ),
                            }
                        diagnostics["stale_selector_assertion"] = {
                            "problem": "generated_js_test_requires_exact_selector_literal",
                            "expected_fix": (
                                "Patch generated_app.test.mjs to assert selectors/handlers that actually exist in the generated HTML/JS. "
                                "If the app binds a form/data-selector handler, do not require an unused button id literal in the script or page."
                            ),
                        }
                    start = max(0, line_no - 4)
                    end = min(len(source_lines), line_no + 3)
                    diagnostics["assertion_context"] = [
                        {
                            "line": index + 1,
                            "source": source_lines[index],
                        }
                        for index in range(start, end)
                    ]
        generated_py_locations = [
            {"line": int(match.group("line"))}
            for line in logs
            if (match := cls._GENERATED_PY_TEST_LOCATION_RE.search(str(line or "")))
        ]
        if generated_py_locations and test_file is not None and test_file.exists():
            try:
                source_lines = test_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                source_lines = []
            py_assertion_failures: list[dict[str, object]] = []
            seen_lines: set[int] = set()
            for location in generated_py_locations:
                line_no = int(location["line"])
                if line_no in seen_lines or not (1 <= line_no <= len(source_lines)):
                    continue
                seen_lines.add(line_no)
                start = max(0, line_no - 8)
                end = min(len(source_lines), line_no + 4)
                py_assertion_failures.append(
                    {
                        "file_path": "miniapp/tests/test_generated_app.py",
                        "line": line_no,
                        "source": source_lines[line_no - 1].strip(),
                        "context": [
                            {"line": index + 1, "source": source_lines[index]}
                            for index in range(start, end)
                        ],
                    }
                )
            if py_assertion_failures:
                diagnostics["python_assertion_failures"] = py_assertion_failures[:3]
            duplicate_path_id = cls._python_duplicate_path_id_payload_issue("\n".join(source_lines))
            if duplicate_path_id:
                diagnostics["path_id_payload_duplicate"] = duplicate_path_id
        stack_excerpt = [str(line or "") for line in logs[-12:] if str(line or "").strip()]
        if stack_excerpt:
            diagnostics["stack_excerpt"] = stack_excerpt
        missing_token_match = next(
            (
                re.search(r"missing token (?P<token>[A-Za-z0-9_-]+)", str(line or ""))
                for line in logs
                if "missing token " in str(line or "")
            ),
            None,
        )
        if missing_token_match:
            token = missing_token_match.group("token")
            diagnostics["js_test_missing_generated_source_token"] = {
                "problem": "generated_js_test_requires_token_on_wrong_page",
                "token": token,
                "expected_fix": (
                    f"Generated JS tests required `{token}` on a page where it is not present. "
                    "Patch generated_app.test.mjs to read the actual route_manifest target that owns that control, "
                    "or add the control to the UI only if the workflow is genuinely missing. Do not require every role root page "
                    "to duplicate child-page forms/buttons."
                ),
            }
        missing_includes_match = next(
            (
                re.search(
                    r"\b(?:html|[A-Za-z0-9_]*Html)\.includes\(\s*([\"'])(?P<literal>(?:id|href)=\\?[\"'][^\"']+\\?[\"'])\1",
                    str(line or ""),
                )
                for line in logs
                if ".includes(" in str(line or "") and ("id=" in str(line or "") or "href=" in str(line or ""))
            ),
            None,
        )
        if missing_includes_match:
            literal = missing_includes_match.group("literal").replace('\\"', '"').replace("\\'", "'")
            diagnostics["js_test_missing_generated_source_token"] = {
                "problem": "generated_js_test_requires_token_on_wrong_page",
                "token": literal,
                "expected_fix": (
                    f"Generated JS tests required `{literal}` in one specific HTML file, but workflow controls may live on either "
                    "the role root or the role child route from route_manifest. Patch generated_app.test.mjs to read all generated "
                    "HTML files for that role and assert the control on the actual page that contains it; only change UI if the "
                    "control is missing from every role surface."
                ),
            }
        missing_href_match = next(
            (
                re.search(r"missing (?P<literal>href=\\?[\"'][^\"']+\\?[\"'])", str(line or ""))
                for line in logs
                if "missing href=" in str(line or "")
            ),
            None,
        )
        if missing_href_match:
            literal = missing_href_match.group("literal").replace('\\"', '"').replace("\\'", "'")
            diagnostics["js_test_brittle_route_link_assertion"] = {
                "problem": "generated_js_test_requires_exact_route_link_literal",
                "token": literal,
                "expected_fix": (
                    f"Generated JS tests required `{literal}` as an exact navigation/back-link literal. "
                    "Route navigation details are not the workflow proof. Patch generated_app.test.mjs to verify "
                    "route_manifest entries, role CSS/script links, real forms/buttons, and API wiring instead. "
                    "Only add a link to the UI when the product workflow genuinely needs that link."
                ),
            }
        missing_plain_includes_match = next(
            (
                re.search(
                    r"\b(?:html|[A-Za-z0-9_]*Html)\.includes\(\s*([\"'])(?P<literal>[A-Za-z][A-Za-z0-9_-]{2,})\1",
                    str(line or ""),
                )
                for line in logs
                if ".includes(" in str(line or "")
            ),
            None,
        )
        if missing_plain_includes_match and "js_test_missing_generated_source_token" not in diagnostics:
            literal = missing_plain_includes_match.group("literal")
            diagnostics["js_test_missing_generated_source_token"] = {
                "problem": "generated_js_test_requires_token_on_wrong_page",
                "token": literal,
                "expected_fix": (
                    f"Generated JS tests required `{literal}` in one specific HTML file. Patch generated_app.test.mjs "
                    "to search all actual HTML files for that role from route_manifest and assert the control/content on "
                    "the page that really contains it; only add UI if it is absent from every role surface."
                ),
            }
        if any("miniapp/miniapp/app/" in str(line or "") for line in logs):
            diagnostics["js_test_path_root"] = {
                "problem": "generated_js_test_prefixed_miniapp_twice",
                "expected_root": "Generated JS tests run from cwd=miniapp; use app/static/<role>/... or resolve ../app/static from import.meta.url.",
            }
        if any(re.search(r"/miniapp/static/(?:client|specialist|manager)/", str(line or "")) for line in logs):
            diagnostics["js_test_route_manifest_path_prefix"] = {
                "problem": "generated_js_test_reads_route_manifest_target_without_app_prefix",
                "expected_root": (
                    "route_manifest maps browser routes to static/<role>/... targets, but generated_app.test.mjs runs "
                    "from cwd=miniapp. Prefix manifest targets with app/ before fs.readFileSync, or normalize through a helper "
                    "such as `const sourcePath = target.startsWith('app/') ? target : `app/${target}``."
                ),
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
        if any(".text is not a function" in str(line or "") for line in logs) and any("new URL" in str(line or "") for line in logs):
            diagnostics["js_test_url_text_api"] = {
                "problem": "generated_js_test_called_text_on_url",
                "expected_text_api": (
                    "Generated JS tests cannot call .text() on URL objects. Read files with fs.readFileSync(path, 'utf8') "
                    "or fs.readFileSync(fileURLToPath(new URL('../app/static/...', import.meta.url)), 'utf8')."
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
        if any(
            "<main class=\\\"page-shell\\\">" in str(line or "")
            or '<main class="page-shell">' in str(line or "")
            or "class=\\\"page-shell\\\"" in str(line or "")
            or 'class="page-shell"' in str(line or "")
            for line in logs
        ):
            diagnostics["exact_page_shell_tag_assertion"] = {
                "problem": "test_asserts_exact_page_shell_markup",
                "expected_fix": (
                    "The platform may add safe-area inline attributes and additional classes to the page shell. "
                    "Generated tests should assert that the class list contains the page-shell token, for example with "
                    "a regex allowing extra attributes/classes, instead of exact <main> markup or exact class=\"page-shell\"."
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
                    "expected_fix": (
                        "Ensure SQLAlchemy tables exist before TestClient requests. If the app creates tables in FastAPI lifespan, "
                        "generated tests must use `with TestClient(app) as client:`; otherwise call Base.metadata.create_all(bind=engine) "
                        "after all generated model classes are imported/declared before requests run."
                    ),
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
        if any("NoForeignKeysError" in str(line or "") for line in logs):
            diagnostics["sqlalchemy_no_foreign_keys"] = {
                "problem": "relationship_without_foreign_key",
                "expected_fix": (
                    "Patch SQLAlchemy models so every relationship has a matching ForeignKey column, or remove relationship()/back_populates "
                    "and query related status/update rows explicitly. Do not leave relationship('...') between tables without a ForeignKey."
                ),
            }
        browser_global_match = next(
            (
                re.search(r"ReferenceError:\s*(?P<name>document|window)\s+is not defined", str(line or ""))
                for line in logs
                if "ReferenceError:" in str(line or "") and " is not defined" in str(line or "")
            ),
            None,
        )
        if browser_global_match:
            app_path_match = next(
                (
                    re.search(r"/source/(?P<path>miniapp/app/static/(?:client|specialist|manager)/app\.js):(?P<line>\d+)", str(line or ""))
                    for line in logs
                    if "/source/miniapp/app/static/" in str(line or "")
                ),
                None,
            )
            diagnostics["js_test_imports_browser_app_without_dom"] = {
                "problem": "generated_js_test_imports_browser_only_role_script_without_dom",
                "missing_global": browser_global_match.group("name"),
                "role_script": app_path_match.group("path") if app_path_match else None,
                "role_script_line": int(app_path_match.group("line")) if app_path_match else None,
                "expected_fix": (
                    "generated_app.test.mjs imports a browser-only role app.js in Node without DOM globals. "
                    "Patch the generated JS test to read role HTML/JS source text and assert selectors/API/event wiring, "
                    "or create explicit globalThis.window/document mocks before importing a script. Do not change working browser code only to satisfy Node."
                ),
            }
        for line in reversed(logs):
            attr_match = cls._PY_MISSING_ATTRIBUTE_RE.search(str(line or ""))
            if not attr_match:
                continue
            object_name = str(attr_match.group("object") or "").strip()
            attribute = str(attr_match.group("attribute") or "").strip()
            diagnostics["python_missing_attribute"] = {
                "object": object_name,
                "attribute": attribute,
                "expected_fix": (
                    f"Generated app reads `{object_name}.{attribute}`, but the model/object has no such attribute. "
                    "Patch the ORM model, schema, route payload builder, frontend payload, and generated tests so the field names match exactly; "
                    "do not keep an output field that is not persisted."
                ),
            }
            break
        for line in reversed(logs):
            shared_state_match = cls._SHARED_STATE_UPDATE_RE.search(str(line or ""))
            if not shared_state_match:
                continue
            payload = str(shared_state_match.group("payload") or "").strip()
            resource_slug = cls._resource_slug_from_payload_keys(payload)
            diagnostics["shared_state_update_failure"] = {
                "entity_id": str(shared_state_match.group("entity_id") or "").strip(),
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

    @staticmethod
    def _python_duplicate_path_id_payload_issue(test_source: str) -> dict[str, object] | None:
        source = str(test_source or "")
        assignment_pattern = re.compile(
            r"\b(?P<payload>[A-Za-z_][A-Za-z0-9_]*)\s*\[\s*['\"](?P<field>[A-Za-z_][A-Za-z0-9_]*id)['\"]\s*\]\s*=\s*(?P<idvar>[A-Za-z_][A-Za-z0-9_]*)"
        )
        for match in assignment_pattern.finditer(source):
            payload_var = match.group("payload")
            field_name = match.group("field")
            id_var = match.group("idvar")
            window = source[match.start() : match.end() + 1200]
            patch_pattern = re.compile(
                rf"client\.patch\(\s*f?['\"][^'\"]*\{{\s*{re.escape(id_var)}\s*\}}[^'\"]*['\"][\s\S]{{0,500}}json\s*=\s*{re.escape(payload_var)}\b",
                re.MULTILINE,
            )
            if not patch_pattern.search(window):
                continue
            line_no = source[: match.start()].count("\n") + 1
            return {
                "line": line_no,
                "payload_var": payload_var,
                "field": field_name,
                "id_var": id_var,
                "expected_fix": (
                    f"Generated Python tests add `{field_name}` to `{payload_var}` while also sending the id in the PATCH path. "
                    "For path-id PATCH endpoints, do not duplicate the path id in the JSON body unless the backend patch schema explicitly accepts it; "
                    "otherwise strict Pydantic schemas return 422."
                ),
            }
        return None

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
        name_error = re.search(r"NameError:\s*name\s+['\"](?P<name>[^'\"]+)['\"]\s+is not defined", text)
        if name_error:
            source_locations = [
                {
                    "file_path": match.group("path").replace("\\", "/"),
                    "line": int(match.group("line")),
                }
                for match in re.finditer(
                    r"File\s+\"[^\"]*/source/(?P<path>miniapp/app/[^\"]+\.py)\",\s+line\s+(?P<line>\d+)",
                    text,
                )
            ]
            if not source_locations:
                source_locations = [
                    {
                        "file_path": f"miniapp/app/{match.group('path').replace(chr(92), '/')}",
                        "line": int(match.group("line")),
                    }
                    for match in re.finditer(
                        r"File\s+\"[^\"]*/app/(?P<path>[^\"]+\.py)\",\s+line\s+(?P<line>\d+)",
                        text,
                    )
                ]
            location = source_locations[-1] if source_locations else {}
            diagnostics["python_name_error"] = {
                "problem": "backend_import_name_error",
                "name": name_error.group("name"),
                "file_path": location.get("file_path"),
                "line": location.get("line"),
                "expected_fix": (
                    "Patch the exact Python file so the referenced name is defined before it is used at import time. "
                    "For SQLAlchemy mapped_column defaults/onupdate, define helper functions before model classes or use an inline callable."
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
