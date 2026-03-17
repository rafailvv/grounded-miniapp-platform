from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.ai.openrouter_client import OpenRouterClient
from app.models.domain import (
    CodeChangePlan,
    CodeChangeTarget,
    CreateRunRequest,
    GenerateRequest,
    RunChecksSummary,
    RunRecord,
    WorkspaceRecord,
)
from app.repositories.state_store import StateStore
from app.services.generation_service import GenerationService
from app.services.preview_service import PreviewService
from app.services.workspace_service import WorkspaceService

ROLE_SCOPE = {"client", "specialist", "manager"}


class RunService:
    def __init__(
        self,
        store: StateStore,
        workspace_service: WorkspaceService,
        generation_service: GenerationService,
        preview_service: PreviewService,
        openrouter_client: OpenRouterClient,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.generation_service = generation_service
        self.preview_service = preview_service
        self.openrouter_client = openrouter_client

    def create_run(self, workspace_id: str, request: CreateRunRequest) -> RunRecord:
        workspace = self.workspace_service.get_workspace(workspace_id)
        resolved_intent = self._resolve_intent(workspace, request)
        run = RunRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            intent=resolved_intent,
            apply_strategy=request.apply_strategy,
            target_role_scope=[role for role in request.target_role_scope if role in ROLE_SCOPE],
            model_profile=request.model_profile,
            llm_provider="openrouter" if self.openrouter_client.enabled else None,
            source_revision_id=workspace.current_revision_id,
            status="running",
            apply_status="pending",
        )
        self._save_run(run)

        job = self.generation_service.generate(
            workspace_id,
            GenerateRequest(
                prompt=request.prompt,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                generation_mode=request.generation_mode,
                intent=resolved_intent,
                model_profile=request.model_profile,
                linked_run_id=run.run_id,
            ),
        )

        current_workspace = self.workspace_service.get_workspace(workspace_id)
        preview = self.preview_service.get(workspace_id)
        change_plan = self._build_change_plan(
            workspace_id=workspace_id,
            run=run,
            artifact_plan=self.generation_service.current_report(workspace_id, "artifact_plan"),
            diff_text=self.workspace_service.diff(workspace_id),
            prompt=request.prompt,
        )

        run.linked_job_id = job.job_id
        run.llm_provider = job.llm_provider
        run.llm_model = job.llm_model
        run.summary = job.summary
        run.failure_reason = job.failure_reason
        run.result_revision_id = current_workspace.current_revision_id
        run.checks_summary = self._build_checks_summary(job.validation_snapshot, preview.status)
        run.touched_files = [target.file_path for target in change_plan.targets]
        run.artifacts = {
            "grounded_spec": f"/workspaces/{workspace_id}/spec/current",
            "app_ir": f"/workspaces/{workspace_id}/ir/current",
            "run_artifacts": f"/runs/{run.run_id}/artifacts",
            "preview_url": preview.url or "",
            "traceability": f"/workspaces/{workspace_id}/traceability/current",
        }
        run.updated_at = datetime.now(timezone.utc)

        if job.status == "completed":
            run.status = "completed"
            run.apply_status = "applied"
        elif job.status == "blocked":
            run.status = "blocked"
            run.apply_status = "blocked"
        else:
            run.status = "failed"
            run.apply_status = "failed"

        self._save_run(run)
        self._store_run_artifacts(run, change_plan, job, preview)
        return run

    def list_runs(self, workspace_id: str) -> list[RunRecord]:
        runs = [
            RunRecord.model_validate(item)
            for item in self.store.list("runs")
            if item["workspace_id"] == workspace_id
        ]
        runs.sort(key=lambda item: item.created_at, reverse=True)
        return runs

    def get_run(self, run_id: str) -> RunRecord:
        payload = self.store.get("runs", run_id)
        if not payload:
            raise KeyError(f"Run not found: {run_id}")
        return RunRecord.model_validate(payload)

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_artifacts:{run_id}")
        if not payload:
            raise KeyError(f"Artifacts not found for run: {run_id}")
        return payload

    def apply_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run.apply_strategy != "manual_approve":
            return run
        run.apply_status = "applied" if run.status == "completed" else run.apply_status
        run.updated_at = datetime.now(timezone.utc)
        self._save_run(run)
        return run

    def _save_run(self, run: RunRecord) -> None:
        self.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    def _store_run_artifacts(self, run: RunRecord, change_plan: CodeChangePlan, job: Any, preview: Any) -> None:
        workspace_id = run.workspace_id
        payload = {
            "run": run.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "grounded_spec": self.generation_service.current_report(workspace_id, "spec"),
            "app_ir": self.generation_service.current_report(workspace_id, "ir"),
            "validation": self.generation_service.current_report(workspace_id, "validation"),
            "assumptions": self.generation_service.current_report(workspace_id, "assumptions"),
            "traceability": self.generation_service.current_report(workspace_id, "traceability"),
            "artifact_plan": self.generation_service.current_report(workspace_id, "artifact_plan"),
            "trace": self.generation_service.current_report(workspace_id, "trace"),
            "code_change_plan": change_plan.model_dump(mode="json"),
            "diff": self.workspace_service.diff(workspace_id),
            "preview": {
                "status": preview.status,
                "runtime_mode": preview.runtime_mode,
                "url": preview.url,
                "role_urls": self.preview_service.role_urls(workspace_id),
                "logs": preview.logs,
            },
        }
        self.store.upsert("reports", f"run_artifacts:{run.run_id}", payload)

    def _resolve_intent(self, workspace: WorkspaceRecord, request: CreateRunRequest) -> str:
        if request.intent != "auto":
            return request.intent
        prompt = request.prompt.lower()
        if request.target_role_scope and len(request.target_role_scope) == 1:
            return "role_only_change"
        if any(token in prompt for token in ("refine", "polish", "improve", "tighten", "cleanup")):
            return "refine"
        has_existing_build = workspace.template_cloned and workspace.current_revision_id is not None and len(workspace.revisions) > 1
        if has_existing_build or any(token in prompt for token in ("change", "update", "edit", "modify", "rewrite", "fix")):
            return "edit"
        return "create"

    def _build_change_plan(
        self,
        *,
        workspace_id: str,
        run: RunRecord,
        artifact_plan: dict[str, Any] | None,
        diff_text: str,
        prompt: str,
    ) -> CodeChangePlan:
        targets: list[CodeChangeTarget] = []
        seen_paths: set[str] = set()
        operations = artifact_plan.get("operations", []) if isinstance(artifact_plan, dict) else []
        for operation in operations:
            file_path = str(operation.get("file_path", "")).strip()
            if not file_path or file_path in seen_paths:
                continue
            seen_paths.add(file_path)
            targets.append(
                CodeChangeTarget(
                    file_path=file_path,
                    operation=str(operation.get("op", "update")),
                    reason=str(operation.get("explanation", "Generated as part of the run.")),
                    risk="medium" if file_path.startswith("artifacts/") else "low",
                )
            )

        if not targets and diff_text.strip():
            for file_path in self._paths_from_diff(diff_text):
                if file_path in seen_paths:
                    continue
                seen_paths.add(file_path)
                targets.append(
                    CodeChangeTarget(
                        file_path=file_path,
                        operation="update",
                        reason="Touched by the applied workspace diff.",
                        risk="medium",
                    )
                )

        summary = (
            str(artifact_plan.get("summary"))
            if isinstance(artifact_plan, dict) and artifact_plan.get("summary")
            else f"Plan code changes for prompt: {prompt[:120]}"
        )
        risks = [
            "Role-specific behavior must remain valid for client, specialist, and manager previews.",
            "Generated artifacts should remain traceable to prompt and retrieved documents.",
        ]
        if diff_text.strip():
            risks.append("Existing workspace edits must be preserved and not overwritten unexpectedly.")
        acceptance_checks = [
            "GroundedSpec and AppIR validators pass or provide explicit blocking issues.",
            "Build validation succeeds on the updated workspace.",
            "Preview runtime starts and exposes role-specific URLs.",
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

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^\+\+\+ b/(.+)$", diff_text, flags=re.MULTILINE):
            candidate = match.group(1).strip()
            if candidate != "/dev/null":
                paths.append(candidate)
        return paths

    @staticmethod
    def _build_checks_summary(validation_snapshot: Any, preview_status: str) -> RunChecksSummary:
        issues = []
        validators = "pending"
        build = "pending"
        if validation_snapshot:
            issues = list(getattr(validation_snapshot, "issues", []) or [])
            if getattr(validation_snapshot, "blocking", False):
                validators = "blocked"
            elif getattr(validation_snapshot, "grounded_spec_valid", False) and getattr(validation_snapshot, "app_ir_valid", False):
                validators = "passed"
            else:
                validators = "failed"
            build = "passed" if getattr(validation_snapshot, "build_valid", False) else "failed"
        preview = "passed" if preview_status == "running" else "failed" if preview_status == "error" else "pending"
        return RunChecksSummary(validators=validators, build=build, preview=preview, issues=issues)
