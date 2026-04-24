from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.ai.model_registry import default_profile_for_generation_mode
from app.models.domain import (
    CheckExecutionRecord,
    FixAttemptRecord,
    JobRecord,
    RepairIterationRecord,
    RunCheckResult,
    ValidationSnapshot,
    utc_now,
)
from app.modules.miniapp_agent_loop.fix_types import FixTurnContext
from app.modules.miniapp_agent_loop.types import WorkspaceLoopResult
from app.services.check_runner import CheckRunner

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixExecutionRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def _refresh_derived_app_tests(
        self,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
    ) -> None:
        generation_service = getattr(self.service, "generation_service", None)
        if generation_service is None:
            return
        try:
            page_graph = self.service._page_graph_for_run(workspace_id, run_id)
        except Exception:
            page_graph = {}
        role_scope = [
            str(role)
            for role in ((page_graph.get("roles") or {}).keys() if isinstance(page_graph, dict) else [])
            if str(role) in {"client", "specialist", "manager"}
        ] or ["client", "specialist", "manager"]
        artifact_builder = generation_service.artifact_builder
        entity_contract_report = generation_service.current_report(workspace_id, "entity_contract") or {}
        entity_contract = dict(entity_contract_report.get("entity_contract") or {})
        test_files = {
            "miniapp/tests/test_generated_app.py": artifact_builder.python_app_level_test_content(
                page_graph=page_graph,
                role_scope=role_scope,
                entity_contract=entity_contract,
            ),
            "miniapp/tests/generated_app.test.mjs": artifact_builder.js_app_level_test_content(
                page_graph=page_graph,
                role_scope=role_scope,
            ),
        }
        for relative_path, content in test_files.items():
            target_path = draft_source / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")

    def execute_exact_checks(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        changed_files: list[str],
    ) -> tuple[CheckExecutionRecord, dict[str, Any]]:
        self.service._append_event(job, "frontend_build_started", "Running exact frontend/build verification.")
        self.service._append_event(job, "backend_compile_started", "Running exact miniapp compile verification.")
        use_fast_gate = job.generation_mode != GenerationMode.QUALITY
        if not use_fast_gate:
            self._refresh_derived_app_tests(
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
            )
        execution = self.service.check_runner.run(
            workspace_id=workspace_id,
            run_id=run_id,
            source_dir=draft_source,
            changed_files=changed_files,
            preview_run_id=run_id,
            scope_mode="fix_agentic",
            check_profile="fast_gate" if use_fast_gate else "full",
        )
        results = [item for item in execution.results if item.name not in {"preview_boot_smoke", "preview_connectivity_smoke"}]
        preview_details: dict[str, Any] = {"status": "skipped", "containers": [], "container_logs": {}, "logs": [], "last_error": None}
        static_failure = any(item.status == "failed" for item in results if item.name in {"schema_validators", "connectivity_validators", "changed_files_static"})
        if not static_failure:
            results.append(
                RunCheckResult(
                    name="preview_boot_smoke",
                    status="skipped",
                    details="Preview rebuild is deferred during fix verification after static/build checks passed.",
                    command="preview deferred during fix",
                    exit_code=0,
                    logs=[],
                )
            )
            results.append(
                RunCheckResult(
                    name="preview_connectivity_smoke",
                    status="skipped",
                    details="Preview connectivity smoke is deferred during fix verification.",
                    command="preview deferred during fix",
                    exit_code=0,
                    logs=[],
                )
            )
        else:
            preview = self.service.preview_service.get(workspace_id)
            container_logs = {}
            containers: list[dict[str, Any]] = []
            if preview.proxy_port is not None:
                log_source = (
                    self.service.workspace_service.draft_source_dir(workspace_id, preview.draft_run_id)
                    if preview.draft_run_id and self.service.workspace_service.draft_exists(workspace_id, preview.draft_run_id)
                    else self.service.workspace_service.source_dir(workspace_id)
                )
                container_logs = self.service.runtime_manager.collect_container_logs(workspace_id, log_source, preview.proxy_port)
                containers = self.service.runtime_manager.inspect_containers(workspace_id, log_source, preview.proxy_port)
            results.append(
                RunCheckResult(
                    name="preview_boot_smoke",
                    status="skipped",
                    details="Preview rebuild was skipped because compile/build checks are still failing.",
                    command="docker compose up -d --build",
                    logs=["Preview rebuild was skipped because compile/build checks are still failing."],
                )
            )
            results.append(
                RunCheckResult(
                    name="preview_connectivity_smoke",
                    status="skipped",
                    details="Preview route smoke was skipped because compile/build checks are still failing.",
                    command="preview route smoke (current session)",
                    logs=["Preview route smoke was skipped because compile/build checks are still failing."],
                )
            )
            preview_details = {
                "status": preview.status,
                "stage": preview.stage,
                "progress_percent": preview.progress_percent,
                "logs": list(preview.logs),
                "last_error": preview.last_error,
                "containers": containers,
                "container_logs": container_logs,
            }
        execution.results = results
        execution.completed_at = utc_now()
        return execution, preview_details

    def execute_final_checks(
        self,
        *,
        job: JobRecord,
        workspace_id: str,
        run_id: str,
        draft_source: Path,
        changed_files: list[str],
    ) -> tuple[CheckExecutionRecord, dict[str, Any]]:
        self.service._append_event(job, "final_checks_started", "Running final full verification before completing fix.")
        self._refresh_derived_app_tests(
            workspace_id=workspace_id,
            run_id=run_id,
            draft_source=draft_source,
        )
        execution = self.service.check_runner.run(
            workspace_id=workspace_id,
            run_id=run_id,
            source_dir=draft_source,
            changed_files=changed_files,
            preview_run_id=run_id,
            scope_mode="whole_file_build",
        )
        preview = self.service.preview_service.get(workspace_id)
        container_logs = {}
        containers: list[dict[str, Any]] = []
        if preview.proxy_port is not None:
            log_source = (
                self.service.workspace_service.draft_source_dir(workspace_id, preview.draft_run_id)
                if preview.draft_run_id and self.service.workspace_service.draft_exists(workspace_id, preview.draft_run_id)
                else self.service.workspace_service.source_dir(workspace_id)
            )
            container_logs = self.service.runtime_manager.collect_container_logs(workspace_id, log_source, preview.proxy_port)
            containers = self.service.runtime_manager.inspect_containers(workspace_id, log_source, preview.proxy_port)
        preview_details = {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "logs": list(preview.logs),
            "last_error": preview.last_error,
            "containers": containers,
            "container_logs": container_logs,
        }
        execution.completed_at = utc_now()
        return execution, preview_details

    @staticmethod
    def final_check_changed_files(
        latest_apply_result: dict[str, Any] | None,
        fix_turn: FixTurnContext,
        scope_entries,
    ) -> list[str]:
        if latest_apply_result and latest_apply_result.get("changed_files"):
            return [str(path) for path in latest_apply_result.get("changed_files") or []]
        if fix_turn.implicated_files:
            return list(fix_turn.implicated_files)
        return [entry.file_path for entry in scope_entries]

    def role_scope_for_fix_request(self, workspace_id: str, run_id: str, request) -> list[str]:
        explicit_scope = [role for role in request.target_role_scope if role in {"client", "specialist", "manager"}]
        if explicit_scope:
            return explicit_scope
        page_graph = self.service._page_graph_for_run(workspace_id, run_id)
        graph_scope = [str(role) for role in (page_graph.get("roles") or {}).keys() if role in {"client", "specialist", "manager"}]
        return graph_scope or ["client", "specialist", "manager"]

    def finalize_loop_job(
        self,
        *,
        job: JobRecord,
        loop_result: WorkspaceLoopResult,
        scope_expansions: list[dict[str, Any]],
        elapsed_ms: int,
    ) -> JobRecord:
        job.status = loop_result.status
        job.outcome_kind = loop_result.outcome_kind
        job.summary = loop_result.summary
        job.failure_reason = loop_result.failure_reason
        if loop_result.failure_class is not None:
            job.failure_class = loop_result.failure_class
        if loop_result.failure_signature is not None:
            job.failure_signature = loop_result.failure_signature
        if loop_result.root_cause_summary is not None:
            job.root_cause_summary = loop_result.root_cause_summary
        baseline_failure_class = (
            self.service._classify_failure_text(job.error_context.raw_error)
            if job.error_context and job.error_context.raw_error
            else None
        )
        job.failure_class = self.service._prefer_failure_class(job.failure_class, baseline_failure_class)
        job.current_fix_phase = loop_result.current_phase
        job.remaining_issues = [] if loop_result.status == "completed" else list(loop_result.remaining_issues)
        if loop_result.latest_execution is not None:
            job.validation_snapshot = self.validation_snapshot_from_execution(loop_result.latest_execution)
        if loop_result.status == "completed":
            job.outcome_kind = "applied"
            job.validation_snapshot = ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            )
        fix_attempts: list[FixAttemptRecord] = []
        for turn in loop_result.turn_history:
            fix_attempts.append(
                FixAttemptRecord(
                    run_id=job.linked_run_id or "",
                    attempt=int(turn.get("attempt") or 0),
                    diagnosis=str(turn.get("diagnosis") or turn.get("assistant_message") or ""),
                    commands=[result.command for result in (loop_result.latest_execution.results if loop_result.latest_execution else []) if result.command],
                    exit_codes={result.name: result.exit_code for result in (loop_result.latest_execution.results if loop_result.latest_execution else [])},
                    files_changed=[str(path) for path in turn.get("files_changed") or []],
                    implicated_files=[str(path) for path in turn.get("fix_targets") or []],
                    failure_signature=str(turn.get("failure_signature") or "") or None,
                    result="patched" if str(turn.get("result")) == "patched" else "failed",
                    rationale_by_file={str(k): str(v) for k, v in dict(turn.get("metadata") or {}).items() if isinstance(v, str)},
                    expected_verification=None,
                )
            )
        return self.finalize_job(
            job,
            fix_attempts=fix_attempts,
            repair_iterations=loop_result.repair_iterations,
            scope_expansions=scope_expansions,
            latest_execution=loop_result.latest_execution,
            latest_preview_details=loop_result.latest_preview_details,
            latest_apply_result=loop_result.latest_apply_result,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def is_fix_success(results: list[RunCheckResult], preview_details: dict[str, Any]) -> bool:
        validators_ok = all(result.status != "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"})
        build_ok = all(result.status != "failed" for result in results if result.name == "changed_files_static")
        canonical_smoke_ok = all(
            result.status != "failed"
            for result in results
            if result.name == "workflow_canonical_smoke"
        )
        app_test_results = [
            result
            for result in results
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"}
        ]
        app_tests_ok = all(result.status != "failed" for result in app_test_results)
        app_tests_complete = not any(result.status == "skipped" for result in app_test_results)
        preview_result = next((result for result in results if result.name == "preview_boot_smoke"), None)
        preview_connectivity_result = next((result for result in results if result.name == "preview_connectivity_smoke"), None)
        preview_deferred = (
            preview_result is not None
            and preview_result.status == "skipped"
            and preview_connectivity_result is not None
            and preview_connectivity_result.status == "skipped"
            and preview_details.get("status") == "skipped"
        )
        preview_ok = (
            preview_result is not None
            and preview_result.status == "passed"
            and preview_connectivity_result is not None
            and preview_connectivity_result.status == "passed"
            and preview_details.get("status") == "running"
        )
        return validators_ok and build_ok and canonical_smoke_ok and app_tests_ok and app_tests_complete and (preview_ok or preview_deferred)

    @classmethod
    def completion_state_from_results(
        cls,
        results: list[RunCheckResult],
        preview_details: dict[str, Any],
        *,
        validation_snapshot: ValidationSnapshot | None,
    ) -> dict[str, Any]:
        strict_green = cls.is_fix_success(results, preview_details)
        preview_result = next((result for result in results if result.name == "preview_boot_smoke"), None)
        preview_connectivity_result = next((result for result in results if result.name == "preview_connectivity_smoke"), None)
        preview_ok = (
            preview_result is not None
            and preview_connectivity_result is not None
            and preview_result.status != "failed"
            and preview_connectivity_result.status != "failed"
        )
        non_blocking_validation_codes: set[str] = set()
        validation_failures = [
            issue
            for issue in (validation_snapshot.issues if validation_snapshot is not None else [])
            if isinstance(issue, dict) and issue.get("blocking", False)
        ]
        only_non_blocking_validator_tail = bool(validation_failures) and all(
            str(issue.get("code") or "") in non_blocking_validation_codes for issue in validation_failures
        )
        validators_ok = all(result.status != "failed" for result in results if result.name in {"schema_validators", "connectivity_validators"})
        if only_non_blocking_validator_tail:
            validators_ok = True
        build_ok = all(result.status != "failed" for result in results if result.name == "changed_files_static")
        canonical_smoke_ok = all(
            result.status != "failed"
            for result in results
            if result.name == "workflow_canonical_smoke"
        )
        app_test_failures = [
            result
            for result in results
            if result.name in {"generated_app_python_tests", "generated_app_js_tests"} and result.status == "failed"
        ]
        non_test_failures = [
            result
            for result in results
            if result.status == "failed"
            and result.name not in {"generated_app_python_tests", "generated_app_js_tests"}
            and not (only_non_blocking_validator_tail and result.name == "schema_validators")
        ]
        remaining_issues = cls.remaining_issues_from_results(
            app_test_failures=app_test_failures,
            validation_snapshot=validation_snapshot,
            preview_details=preview_details,
        )
        if only_non_blocking_validator_tail:
            remaining_issues.extend(
                {
                    "kind": "validation_issue",
                    "code": issue.get("code"),
                    "message": issue.get("message"),
                    "location": issue.get("location"),
                    "blocking": False,
                }
                for issue in validation_failures
            )
        return {
            "strict_green": strict_green,
            "optimistic_complete": validators_ok and build_ok and canonical_smoke_ok and not non_test_failures and not app_test_failures,
            "preview_ok": preview_ok,
            "validators_ok": validators_ok,
            "build_ok": build_ok,
            "canonical_smoke_ok": canonical_smoke_ok,
            "non_test_failures": non_test_failures,
            "app_test_failures": app_test_failures,
            "remaining_issues": remaining_issues,
        }

    @staticmethod
    def remaining_issues_from_results(
        *,
        app_test_failures: list[RunCheckResult],
        validation_snapshot: ValidationSnapshot | None,
        preview_details: dict[str, Any],
    ) -> list[dict[str, Any]]:
        remaining: list[dict[str, Any]] = []
        for result in app_test_failures:
            remaining.append(
                {
                    "kind": "generated_test_failure",
                    "location": "tests",
                    "blocking": False,
                    "check": result.name,
                    "details": result.details,
                    "logs": result.logs[-8:],
                }
            )
        if validation_snapshot is not None:
            for issue in validation_snapshot.issues:
                if not isinstance(issue, dict) or issue.get("blocking", False):
                    continue
                remaining.append({"kind": "validation_issue", "code": issue.get("code"), "message": issue.get("message"), "location": issue.get("location")})
        if preview_details.get("status") == "error" and preview_details.get("last_error"):
            remaining.append({"kind": "preview_warning", "message": preview_details.get("last_error")})
        return remaining

    def validation_snapshot_from_execution(self, execution: CheckExecutionRecord) -> ValidationSnapshot:
        issues = [issue.model_dump(mode="json") for issue in CheckRunner.failing_issues(execution.results)]
        build_failed = any(item.status == "failed" for item in execution.results if item.name == "changed_files_static")
        return ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=not build_failed,
            blocking=bool(issues),
            issues=issues,
        )

    def finalize_job(
        self,
        job: JobRecord,
        *,
        fix_attempts: list[FixAttemptRecord],
        repair_iterations: list[RepairIterationRecord],
        scope_expansions: list[dict[str, Any]],
        latest_execution: CheckExecutionRecord | None,
        latest_preview_details: dict[str, Any],
        latest_apply_result: dict[str, Any] | None,
        elapsed_ms: int,
    ) -> JobRecord:
        job.fix_attempts = [item.model_dump(mode="json") for item in fix_attempts]
        job.repair_iterations = [item.model_dump(mode="json") for item in repair_iterations]
        job.scope_expansions = list(scope_expansions)
        job.apply_result = latest_apply_result
        if latest_execution is not None:
            job.executed_checks = [item.model_dump(mode="json") for item in latest_execution.results]
        job.container_statuses = latest_preview_details.get("containers", job.container_statuses)
        job.updated_at = datetime.now(timezone.utc)
        job.latency_breakdown["fix_total_ms"] = elapsed_ms
        self.service._save_job(job)
        if self.service.artifact_recorder is not None:
            self.service.artifact_recorder.store_workspace_report(
                job.workspace_id,
                "cache_diagnostics",
                {
                    "workspace_id": job.workspace_id,
                    "cache_stats": job.cache_stats,
                    "repair_iterations": len(job.repair_iterations),
                    "fix_attempts": len(job.fix_attempts),
                },
            )
        if self.service.session_engine is not None:
            self.service.session_engine.record_phase(
                workspace_id=job.workspace_id,
                phase="repair",
                generation_mode=str(job.generation_mode),
                model_profile=job.model_profile or default_profile_for_generation_mode(job.generation_mode),
                run_mode="fix",
                details={
                    "fix_attempts": len(fix_attempts),
                    "scope_expansions": len(scope_expansions),
                    "status": job.status,
                },
            )
        self.service._store_report(f"fix_attempts:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": job.fix_attempts})
        self.service._store_report(f"scope_expansions:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": scope_expansions})
        self.service._store_report(f"remaining_issues:{job.workspace_id}", {"workspace_id": job.workspace_id, "items": list(job.remaining_issues)})
        if job.validation_snapshot is not None:
            self.service._store_report(f"validation:{job.workspace_id}", job.validation_snapshot.model_dump(mode="json"))
        if latest_preview_details:
            self.service._store_report(
                f"fix_runtime:{job.workspace_id}",
                {
                    "workspace_id": job.workspace_id,
                    "containers": latest_preview_details.get("containers", []),
                    "container_logs": latest_preview_details.get("container_logs", {}),
                    "status": latest_preview_details.get("status"),
                    "stage": latest_preview_details.get("stage"),
                    "last_error": latest_preview_details.get("last_error"),
                },
            )
        return job
