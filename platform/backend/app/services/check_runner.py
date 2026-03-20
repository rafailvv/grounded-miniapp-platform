from __future__ import annotations

import json
import os
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
from app.services.preview_service import PreviewService
from app.validators.suite import ValidationSuite


class CheckRunner:
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

        preview_started = time.perf_counter()
        preview = self.preview_service.get(workspace_id)
        should_skip_preview = bool(filtered_issues) or bool(connectivity_issues) or static_result.status == "failed"
        if should_skip_preview:
            preview_status = "skipped"
            preview_details = "Preview smoke skipped because validator/build checks already failed."
            preview_logs: list[str] = []
            connectivity_result = RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because validator/build checks already failed.",
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
        if "preview_boot_smoke" in failed_names or "preview_connectivity_smoke" in failed_names:
            return "runtime_preview_boot"
        return None

    @staticmethod
    def has_tooling_failure(results: list[RunCheckResult]) -> bool:
        markers = (
            "npm is not available in the miniapp runtime",
            "frontend build tooling is unavailable",
            "node.js/npm is missing",
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
        if preview_run_id is not None and preview.draft_run_id not in {None, preview_run_id}:
            return RunCheckResult(
                name="preview_connectivity_smoke",
                status="skipped",
                details="Preview connectivity smoke skipped because the running preview session does not match this draft.",
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
            try:
                request = Request(target, headers={"User-Agent": "connectivity-smoke"})
                with urlopen(request, timeout=2.0) as response:
                    status_code = response.status if hasattr(response, "status") else response.getcode()
                    body = response.read().decode("utf-8", errors="ignore")
                if status_code >= 400:
                    failures.append(f"{route} returned HTTP {status_code}.")
                    continue
                normalized_body = body.lower()
                if len(normalized_body.strip()) < 40 or "not found" in normalized_body or "<title>404" in normalized_body:
                    failures.append(f"{route} returned unusable preview content.")
                    continue
                logs.append(f"{route} returned usable preview content.")
            except (TimeoutError, URLError, OSError) as exc:
                failures.append(f"{route} could not be opened in preview: {exc}")
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
            root_route = next(
                (
                    str(page.get("route_path") or "")
                    for page in pages
                    if isinstance(page, dict) and str(page.get("route_path") or "") in {f"/{role}", "/"}
                ),
                "",
            )
            routes.append(root_route or f"/{role}")
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
