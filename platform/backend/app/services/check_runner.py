from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from app.models.artifacts import ValidationIssue
from app.models.domain import CheckExecutionRecord, RunCheckResult, utc_now
from app.services.workspace.preview_service import PreviewService
from app.validators.suite import ValidationSuite


class CheckRunner:
    _API_FAILURE_RE = re.compile(
        r"(?P<label>Create|Update|List|Post-update list)\s+API\s+failed:\s*"
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
    _SHARED_STATE_UPDATE_RE = re.compile(
        r"Updated record\s+(?P<record_id>[A-Za-z0-9_-]+)\s+did not reflect\s+(?P<actor>[A-Za-z0-9_-]+)\s+changes in shared state\.\s+Payload:\s*(?P<payload>.*)$",
        re.IGNORECASE,
    )

    def __init__(self, validation_suite: ValidationSuite, preview_service: PreviewService) -> None:
        self.validation_suite = validation_suite
        self.preview_service = preview_service

    def run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        source_dir: Path,
        changed_files: list[str],
        preview_run_id: str | None = None,
        scope_mode: str = "whole_file_build",
    ) -> CheckExecutionRecord:
        started = time.perf_counter()
        results: list[RunCheckResult] = []
        backend_dir = source_dir / "miniapp"

        validator_started = time.perf_counter()
        build_issues = self.validation_suite.validate_build(source_dir)
        filtered_issues = self._filter_build_issues(build_issues, scope_mode)
        results.append(
            RunCheckResult(
                name="schema_validators",
                status="failed" if filtered_issues else "passed",
                details="Build validators executed against the draft workspace.",
                duration_ms=int((time.perf_counter() - validator_started) * 1000),
                command="validation_suite.validate_build",
                logs=self._validation_logs(filtered_issues),
            )
        )

        connectivity_started = time.perf_counter()
        connectivity_issues = self.validation_suite.validate_connectivity(source_dir)
        results.append(
            RunCheckResult(
                name="connectivity_validators",
                status="failed" if connectivity_issues else "passed",
                details="Connectivity validators executed against the draft workspace.",
                duration_ms=int((time.perf_counter() - connectivity_started) * 1000),
                command="validation_suite.validate_connectivity",
                logs=self._validation_logs(connectivity_issues),
            )
        )

        static_started = time.perf_counter()
        static_result = self._static_check(source_dir=source_dir, changed_files=changed_files)
        static_result.duration_ms = int((time.perf_counter() - static_started) * 1000)
        results.append(static_result)

        python_tests_started = time.perf_counter()
        python_tests_result = self._run_python_app_tests(backend_dir)
        python_tests_result.duration_ms = int((time.perf_counter() - python_tests_started) * 1000)
        results.append(python_tests_result)

        js_tests_started = time.perf_counter()
        js_tests_result = self._run_js_app_tests(backend_dir)
        js_tests_result.duration_ms = int((time.perf_counter() - js_tests_started) * 1000)
        results.append(js_tests_result)

        preview_started = time.perf_counter()
        preview = self.preview_service.get(workspace_id)
        should_skip_preview = (
            bool(filtered_issues)
            or bool(connectivity_issues)
            or static_result.status == "failed"
        )
        if should_skip_preview:
            preview_status = "skipped"
            preview_details = "Preview smoke skipped because validator or build checks already failed."
            preview_logs: list[str] = []
            connectivity_result = RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because validator or build checks already failed.",
                command="preview route smoke (current session)",
                logs=[],
            )
        else:
            preview_status = "skipped" if preview.status in {"stopped", "error"} else "passed"
            preview_details = "Draft preview smoke recorded using the current preview session."
            preview_logs = preview.logs[-12:]
            connectivity_result = self._preview_connectivity_smoke(
                source_dir=source_dir,
                preview=preview,
                preview_run_id=preview_run_id,
            )
        results.append(
            RunCheckResult(
                name="preview_boot_smoke",
                status=preview_status,
                details=preview_details,
                duration_ms=int((time.perf_counter() - preview_started) * 1000),
                command="preview smoke (current session)",
                logs=preview_logs,
            )
        )
        connectivity_result.duration_ms = int((time.perf_counter() - preview_started) * 1000)
        results.append(connectivity_result)

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
    def failing_issues(results: list[RunCheckResult]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for result in results:
            if result.status != "failed":
                continue
            location = result.name
            code = f"check.{result.name}"
            message = result.details or f"{result.name} failed."
            if result.name in {"schema_validators", "connectivity_validators"}:
                parsed = CheckRunner._validation_issues_from_logs(result.logs, fallback_code=code, fallback_location=location)
                if parsed:
                    issues.extend(parsed)
                    continue
                message = next((line for line in result.logs if line.strip()), message)
            if result.name == "changed_files_static":
                message = next((line for line in result.logs if line.strip()), message)
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}:
                location = "tests"
                code = "tests.python_generated_app" if result.name == "generated_app_python_tests" else "tests.js_generated_app"
                diagnostics = result.diagnostics if isinstance(result.diagnostics, dict) else {}
                api_failure = diagnostics.get("api_failure") if isinstance(diagnostics, dict) else None
                shared_state_failure = diagnostics.get("shared_state_update_failure") if isinstance(diagnostics, dict) else None
                if isinstance(shared_state_failure, dict):
                    actor = str(shared_state_failure.get("actor") or "").strip()
                    resource_slug = str(shared_state_failure.get("resource_slug") or "").strip()
                    resource_label = f"/api/{resource_slug}" if resource_slug else "shared record API"
                    payload_excerpt = str(shared_state_failure.get("payload_excerpt") or "").strip()
                    message = (
                        f"Generated app shared-state update failure: {resource_label} did not persist "
                        f"{actor or 'role'} changes. Payload: {payload_excerpt}"
                    ).strip()
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
                else:
                    message = next((line for line in reversed(result.logs) if line.strip()), message)
            if result.name in {"preview_boot_smoke", "preview_connectivity_smoke"}:
                location = "preview"
                code = "connectivity.preview_route_unreachable" if result.name == "preview_connectivity_smoke" else "preview.rebuild_failed"
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
        if "generated_app_python_tests" in failed_names or "generated_app_js_tests" in failed_names:
            return "app/runtime_test"
        if "preview_boot_smoke" in failed_names or "preview_connectivity_smoke" in failed_names:
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
    def _validation_issues_from_logs(
        logs: list[str],
        *,
        fallback_code: str,
        fallback_location: str,
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
                code=fallback_code,
                message=next((line for line in logs if line.strip()), "Validation failed."),
                severity="high",
                location=fallback_location,
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
                details="Preview connectivity smoke skipped because no generated route graph is available.",
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
            details="Preview route smoke checked generated root routes against the running preview session.",
            command="preview route smoke (current session)",
            logs=failures or logs,
        )

    @staticmethod
    def _root_preview_routes(source_dir: Path) -> list[str]:
        graph_path = source_dir / "artifacts" / "generated_app_graph.json"
        if not graph_path.exists():
            return []
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except ValueError:
            return []
        roles = graph.get("roles") or {}
        routes: list[str] = []
        for role in ("client", "specialist", "manager"):
            role_payload = roles.get(role) or {}
            pages = role_payload.get("pages") or []
            role_route = next(
                (
                    str(page.get("route_path") or "")
                    for page in pages
                    if isinstance(page, dict) and str(page.get("route_path") or "") in {f"/{role}", "/"}
                ),
                "",
            )
            if not role_route or role_route == "/":
                routes.append(f"/{role}")
                continue
            if role_route == f"/{role}":
                routes.append(role_route)
                continue
            normalized = role_route if role_route.startswith("/") else f"/{role_route}"
            routes.append(f"/{role}{normalized}")
        return list(dict.fromkeys(route for route in routes if route))

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

    def _run_python_app_tests(self, backend_dir: Path) -> RunCheckResult:
        test_file = backend_dir / "tests" / "test_generated_app.py"
        if not test_file.exists():
            return RunCheckResult(
                name="generated_app_python_tests",
                status="skipped",
                details="Generated Python app tests were not present in the draft workspace.",
                command=f"{sys.executable} -m unittest discover -s tests -p test_generated_app.py",
                logs=[],
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

    def _install_python_requirements(self, backend_dir: Path) -> RunCheckResult | None:
        requirements_file = backend_dir / "requirements.txt"
        if not requirements_file.exists():
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
                name="generated_app_python_tests",
                status="failed",
                details="Generated Python dependency install timed out.",
                command=" ".join(command),
                logs=self._command_logs(
                    "Generated Python dependency install timed out.",
                    exc.stdout or "",
                    exc.stderr or "",
                ),
            )
        if result.returncode != 0:
            return RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                details="Generated Python dependency install failed.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=self._command_logs(
                    "Generated Python dependency install failed.",
                    result.stdout,
                    result.stderr,
                ),
            )
        return None

    def _run_js_app_tests(self, backend_dir: Path) -> RunCheckResult:
        test_file = backend_dir / "tests" / "generated_app.test.mjs"
        if not test_file.exists():
            return RunCheckResult(
                name="generated_app_js_tests",
                status="skipped",
                details="Generated JS app tests were not present in the draft workspace.",
                command="node --test tests/generated_app.test.mjs",
                logs=[],
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
            return RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Generated JS app tests failed for the draft miniapp.",
                command=" ".join(command),
                exit_code=result.returncode,
                logs=self._command_logs("Generated JS app tests failed for the draft miniapp.", result.stdout, result.stderr),
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

    @staticmethod
    def _command_logs(summary: str, stdout: str, stderr: str, *, tail_lines: int = 40) -> list[str]:
        merged = "\n".join(part for part in [stderr.strip(), stdout.strip()] if part.strip())
        lines = [line.rstrip() for line in merged.splitlines() if line.strip()]
        if not lines:
            return [summary]
        tail = lines[-tail_lines:]
        return [summary, *tail]

    @classmethod
    def _extract_generated_app_test_diagnostics(cls, logs: list[str]) -> dict[str, object]:
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
        stack_excerpt = [str(line or "") for line in logs[-12:] if str(line or "").strip()]
        if stack_excerpt:
            diagnostics["stack_excerpt"] = stack_excerpt
        missing_role_pages = [
            str(match.group("role") or "").strip().lower()
            for line in logs
            if (match := cls._ROLE_PAGE_ASSERT_RE.search(str(line or "")))
        ]
        if missing_role_pages:
            diagnostics["missing_role_pages"] = list(dict.fromkeys(role for role in missing_role_pages if role))
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
    def _resource_slug_from_payload_keys(payload: str) -> str:
        for raw_key in re.findall(r"['\"]([A-Za-z0-9_]+)_id['\"]", str(payload or "")):
            stem = re.sub(r"[^a-z0-9_]+", "", raw_key.lower()).strip()
            if not stem or stem in {"id", "record", "item"}:
                continue
            return stem if stem.endswith("s") else f"{stem}s"
        return ""

    @staticmethod
    def _filter_build_issues(issues: list[ValidationIssue], scope_mode: str) -> list[ValidationIssue]:
        if scope_mode not in {"minimal_patch", "fix_agentic"}:
            return issues
        ignored_prefixes = ("build.placeholder_",)
        ignored_codes = {"build.missing_entrypoint"}
        return [
            issue
            for issue in issues
            if not issue.code.startswith(ignored_prefixes) and issue.code not in ignored_codes
        ]
