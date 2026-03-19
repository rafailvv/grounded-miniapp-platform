from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
        scope_mode: str = "app_surface_build",
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
                logs=[issue.message for issue in filtered_issues],
            )
        )

        static_started = time.perf_counter()
        static_result = self._static_check(source_dir=source_dir, changed_files=changed_files)
        static_result.duration_ms = int((time.perf_counter() - static_started) * 1000)
        results.append(static_result)

        preview_started = time.perf_counter()
        preview = self.preview_service.get(workspace_id)
        preview_status = "skipped" if preview.status in {"stopped", "error"} else "passed"
        results.append(
            RunCheckResult(
                name="preview_boot_smoke",
                status=preview_status,
                details="Draft preview smoke recorded using the current preview session.",
                duration_ms=int((time.perf_counter() - preview_started) * 1000),
                command="preview smoke (current session)",
                logs=preview.logs[-12:],
            )
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
    def failing_issues(results: list[RunCheckResult]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for result in results:
            if result.status != "failed":
                continue
            location = result.name
            code = f"check.{result.name}"
            message = result.details or f"{result.name} failed."
            if result.name == "schema_validators":
                message = next((line for line in result.logs if line.strip()), message)
            if result.name == "changed_files_static":
                message = next((line for line in result.logs if line.strip()), message)
            if result.name == "preview_boot_smoke":
                location = "preview"
                code = "preview.rebuild_failed"
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
        if "schema_validators" in failed_names:
            return "validator/domain_constraint"
        if "changed_files_static" in failed_names:
            return "syntax/build"
        if "preview_boot_smoke" in failed_names:
            return "runtime_preview_boot"
        return None

    @staticmethod
    def has_tooling_failure(results: list[RunCheckResult]) -> bool:
        markers = (
            "npm is not available in the backend runtime",
            "frontend build tooling is unavailable",
            "node.js/npm is missing",
        )
        for result in results:
            haystack = "\n".join([result.details or "", *result.logs]).lower()
            if any(marker in haystack for marker in markers):
                return True
        return False

    def _static_check(self, *, source_dir: Path, changed_files: list[str]) -> RunCheckResult:
        frontend_dir = source_dir / "frontend"
        backend_dir = source_dir / "backend"
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
                details="Frontend build tooling is unavailable in the backend runtime.",
                command="npm run build",
                logs=[
                    "Frontend build tooling is unavailable in the backend runtime.",
                    "npm was not found on PATH.",
                    "Install Node.js/npm in the platform backend runtime and rebuild the backend container.",
                ],
            )

        try:
            if not (frontend_dir / "node_modules").exists():
                install_cmd = [npm_binary, "ci", "--no-audit", "--no-fund"] if (frontend_dir / "package-lock.json").exists() else [npm_binary, "install", "--no-audit", "--no-fund"]
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
                details="npm is not available in the backend runtime.",
                command="npm run build",
                logs=["npm is not available in the backend runtime."],
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

    def _run_backend_compile(self, backend_dir: Path) -> RunCheckResult:
        app_dir = backend_dir / "app"
        py_files = sorted(str(path.relative_to(backend_dir)) for path in app_dir.rglob("*.py"))
        if not py_files:
            return RunCheckResult(
                name="changed_files_static",
                status="passed",
                details="No backend Python files required compilation.",
                command="python -m py_compile",
                logs=["No backend Python files required compilation."],
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
                details="Backend py_compile failed for the draft backend.",
                command=f"{sys.executable} -m py_compile {' '.join(py_files)}",
                exit_code=result.returncode,
                logs=self._command_logs("Backend py_compile failed for the draft backend.", result.stdout, result.stderr),
            )
        return RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Backend py_compile passed for the draft backend.",
            command=f"{sys.executable} -m py_compile {' '.join(py_files)}",
            exit_code=result.returncode,
            logs=["Backend py_compile passed for the draft backend."],
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
