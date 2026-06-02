from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import time
from typing import Any

from app.models.domain import BackgroundTaskRecord, CreateRunRequest
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.repositories.state_store import StateStore
from app.services.check_runner import CheckRunner
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.pr_babysitter import PrBabysitterService
from app.services.repair_cases import RepairCaseService
from app.services.workspace.preview_service import PreviewService
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService
from app.services.lsp_context import LspContextService


TERMINAL_STATUSES = {"completed", "failed", "blocked", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


class BackgroundTaskService:
    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        run_service: RunService,
        preview_service: PreviewService,
        check_runner: CheckRunner,
        pr_babysitter_service: PrBabysitterService | None = None,
        lsp_context_service: LspContextService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.run_service = run_service
        self.preview_service = preview_service
        self.check_runner = check_runner
        self.pr_babysitter_service = pr_babysitter_service
        self.lsp_context_service = lsp_context_service
        self._workers: dict[str, threading.Thread] = {}

    def create_task(
        self,
        *,
        workspace_id: str,
        task_type: str,
        title: str | None = None,
        run_id: str | None = None,
        parent_task_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
        owner: str = "agent",
        max_attempts: int = 1,
        auto_start: bool = True,
    ) -> BackgroundTaskRecord:
        self.workspace_service.get_workspace(workspace_id)
        if run_id:
            self.run_service.get_run(run_id)
        task = BackgroundTaskRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            parent_task_id=parent_task_id,
            type=task_type,  # type: ignore[arg-type]
            title=title or self._default_title(task_type),
            owner=owner or "agent",
            input=dict(input_payload or {}),
            max_attempts=max(1, int(max_attempts or 1)),
        )
        self._save(task)
        self._append_output(task.task_id, "task_created", "Task created.", {"type": task.type, "run_id": task.run_id})
        if auto_start:
            self._start_worker(task.task_id)
        return self.get_task(task.task_id)

    def list_tasks(
        self,
        *,
        workspace_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        items = [BackgroundTaskRecord.model_validate(item) for item in self.store.list("background_tasks")]
        if workspace_id:
            items = [item for item in items if item.workspace_id == workspace_id]
        if run_id:
            items = [
                item
                for item in items
                if item.run_id == run_id
                or str(item.input.get("source_run_id") or "") == run_id
                or str(item.linked_refs.get("source_run_id") or "") == run_id
            ]
        if status:
            items = [item for item in items if item.status == status]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return {
            "schema": "grounded.background_tasks.v1",
            "status": "ok",
            "items": [item.model_dump(mode="json") for item in items],
        }

    def get_task(self, task_id: str) -> BackgroundTaskRecord:
        payload = self.store.get("background_tasks", task_id)
        if not isinstance(payload, dict):
            raise KeyError(f"Task not found: {task_id}")
        return BackgroundTaskRecord.model_validate(payload)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> BackgroundTaskRecord:
        task = self.get_task(task_id)
        if "title" in payload:
            task.title = str(payload.get("title") or task.title).strip() or task.title
        if "status" in payload:
            status = str(payload.get("status") or "").strip()
            if status not in {"queued", "running", "stopping", "completed", "failed", "blocked", "cancelled"}:
                raise ValueError(f"Unsupported task status: {status}")
            task.status = status  # type: ignore[assignment]
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            task.linked_refs = {**task.linked_refs, "metadata": metadata}
        task.updated_at = _now()
        self._save(task)
        self._append_output(task.task_id, "progress", "Task updated.", {"status": task.status})
        return task

    def stop_task(self, task_id: str) -> BackgroundTaskRecord:
        task = self.get_task(task_id)
        self.store.upsert(
            "reports",
            f"background_task_stop:{task_id}",
            {"task_id": task_id, "requested": True, "requested_at": _now_iso()},
        )
        if task.run_id:
            try:
                self.run_service.stop_run(task.run_id)
            except KeyError:
                pass
        if task.status in {"queued"}:
            task.status = "cancelled"
            task.completed_at = _now()
            self._append_output(task.task_id, "task_cancelled", "Queued task cancelled.", {})
        elif task.status in {"running"}:
            task.status = "stopping"
            self._append_output(task.task_id, "progress", "Stop requested.", {})
        task.updated_at = _now()
        self._save(task)
        return task

    def retry_task(self, task_id: str) -> BackgroundTaskRecord:
        task = self.get_task(task_id)
        if task.attempt >= task.max_attempts:
            raise ValueError("Task has no retry attempts remaining.")
        retry = BackgroundTaskRecord(
            workspace_id=task.workspace_id,
            run_id=task.run_id,
            parent_task_id=task.task_id,
            type=task.type,
            title=task.title,
            owner=task.owner,
            input=dict(task.input),
            attempt=task.attempt + 1,
            max_attempts=task.max_attempts,
            linked_refs={**task.linked_refs, "retry_of": task.task_id},
        )
        self._save(retry)
        self._append_output(retry.task_id, "task_created", "Retry task created.", {"retry_of": task.task_id, "attempt": retry.attempt})
        self._start_worker(retry.task_id)
        return self.get_task(retry.task_id)

    def requeue_task(self, task_id: str) -> BackgroundTaskRecord:
        task = self.get_task(task_id)
        if task.status not in {"failed", "blocked", "cancelled"}:
            raise ValueError("Only failed, blocked, or cancelled tasks can be requeued.")
        task.status = "queued"
        task.error = None
        task.completed_at = None
        task.updated_at = _now()
        self.store.delete("reports", f"background_task_stop:{task_id}")
        self._save(task)
        self._append_output(task.task_id, "progress", "Task requeued.", {"attempt": task.attempt})
        self._start_worker(task.task_id)
        return self.get_task(task.task_id)

    def output(self, task_id: str, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        self.get_task(task_id)
        payload = self.store.get("reports", f"background_task_output:{task_id}") or {}
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        start = max(0, int(cursor or 0))
        size = min(500, max(1, int(limit or 100)))
        selected = items[start : start + size]
        return {
            "schema": "grounded.background_task_output.v1",
            "task_id": task_id,
            "items": selected,
            "next_cursor": start + len(selected),
            "has_more": start + len(selected) < len(items),
        }

    def real_tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.list_tasks(run_id=run_id)["items"]

    def _start_worker(self, task_id: str) -> None:
        if task_id in self._workers and self._workers[task_id].is_alive():
            return
        worker = threading.Thread(target=self._execute, args=(task_id,), daemon=True)
        self._workers[task_id] = worker
        worker.start()

    def _execute(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if self._stop_requested(task.task_id):
            self._cancel(task, "Task stopped before start.")
            return
        task.status = "running"
        task.started_at = task.started_at or _now()
        task.updated_at = _now()
        self._save(task)
        self._append_output(task.task_id, "task_started", "Task started.", {"type": task.type})
        try:
            result = self._execute_type(task)
            if self._stop_requested(task.task_id):
                self._cancel(task, "Task stopped.")
                return
            task = self.get_task(task.task_id)
            task.status = "completed"
            task.output_summary = str(result.get("summary") or "Task completed.")
            task.linked_refs = {**task.linked_refs, **{k: v for k, v in result.items() if k != "summary"}}
            task.completed_at = _now()
            task.updated_at = _now()
            self._save(task)
            self._append_output(task.task_id, "task_completed", task.output_summary or "Task completed.", result)
        except Exception as exc:
            task = self.get_task(task_id)
            task.status = "failed"
            task.error = str(exc)
            task.completed_at = _now()
            task.updated_at = _now()
            self._save(task)
            self._append_output(task.task_id, "task_failed", str(exc), {"error": str(exc), "error_type": type(exc).__name__})

    def _execute_type(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        if task.type == "generate_product":
            return self._generate_product(task)
        if task.type == "repair_failed_run":
            return self._repair_failed_run(task)
        if task.type == "browser_verify":
            return self._browser_verify(task)
        if task.type == "lsp_diagnostics":
            return self._lsp_diagnostics(task)
        if task.type == "memory_consolidate":
            return self._memory_consolidate(task)
        if task.type == "preview_rebuild":
            return self._preview_rebuild(task)
        if task.type == "pr_ci_babysit":
            return self._pr_ci_babysit(task)
        if task.type == "worker_branch":
            return {
                "summary": "Worker branch task recorded; execution is controlled by the run worker branch gate.",
                "worker_branch": task.input.get("worker_id") or "unknown",
            }
        raise ValueError(f"Unsupported background task type: {task.type}")

    def _generate_product(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        request_payload = dict(task.input.get("run_request") or task.input)
        request_payload.setdefault("prompt", task.input.get("prompt") or task.title)
        request = CreateRunRequest.model_validate(request_payload)
        run = self.run_service.create_run(task.workspace_id, request)
        task.run_id = run.run_id
        task.linked_refs = {**task.linked_refs, "run_id": run.run_id}
        self._save(task)
        self._append_output(task.task_id, "artifact", "Run created.", {"run_id": run.run_id, "status": run.status})
        return {"summary": "Generate product run started.", "run_id": run.run_id}

    def _repair_failed_run(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        source_run_id = str(task.input.get("source_run_id") or task.run_id or "").strip()
        if not source_run_id:
            raise ValueError("repair_failed_run requires source_run_id or run_id.")
        source = self.run_service.get_run(source_run_id)
        prompt = str(task.input.get("prompt") or "").strip()
        if not prompt:
            repair_cases = RepairCaseService(self.store).list_cases(source_run_id)
            active_case = repair_cases.get("active_case") if isinstance(repair_cases, dict) else None
            if isinstance(active_case, dict):
                prompt = (
                    "Repair the source run from this active repair case. Do not broaden the task.\n"
                    f"Original product prompt:\n{source.prompt[:3000]}\n"
                    "Active repair case:\n"
                    f"{json.dumps(active_case.get('repair_prompt') or active_case, ensure_ascii=False, default=str)[:5000]}"
                )
        if not prompt:
            prompt = f"Repair failed run {source_run_id}: {source.failure_reason or source.summary or source.prompt}"
        request = CreateRunRequest(
            prompt=prompt,
            mode="fix",
            intent="edit",
            resume_from_run_id=source_run_id,
            target_role_scope=source.target_role_scope,
            model_profile=source.model_profile,
            generation_mode=source.generation_mode,
        )
        run = self.run_service.create_run(task.workspace_id, request)
        task.run_id = run.run_id
        task.linked_refs = {**task.linked_refs, "source_run_id": source_run_id, "run_id": run.run_id}
        self._save(task)
        self._append_output(task.task_id, "artifact", "Repair run created.", {"run_id": run.run_id, "source_run_id": source_run_id})
        return {"summary": "Repair run started.", "run_id": run.run_id, "source_run_id": source_run_id}

    def _browser_verify(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        run_id = str(task.run_id or task.input.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("browser_verify requires run_id.")
        run = self.run_service.get_run(run_id)
        source_dir = (
            self.workspace_service.draft_source_dir(run.workspace_id, run.run_id)
            if self.workspace_service.draft_exists(run.workspace_id, run.run_id)
            else self.workspace_service.source_dir(run.workspace_id)
        )
        record = self.check_runner.run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            source_dir=source_dir,
            changed_files=list(run.touched_files or []),
            preview_run_id=run.run_id,
            scope_mode="full_build",
            check_profile="full",
            intent=run.intent,
            generation_mode=run.generation_mode,
            acceptance_contract=run.acceptance_contract,
            progress_callback=lambda phase, payload: self._append_output(task.task_id, "progress", phase, payload),
        )
        ref = f"background_task_check:{task.task_id}"
        self.store.upsert("reports", ref, record.model_dump(mode="json"))
        return {"summary": f"Browser/check verification {record.status}.", "check_ref": ref, "check_status": record.status}

    def _lsp_diagnostics(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        run_id = str(task.run_id or task.input.get("run_id") or "").strip() or None
        files = [str(item).strip() for item in (task.input.get("files") or []) if str(item or "").strip()]
        changed_only = bool(task.input.get("changed_only"))
        source_dir = (
            self.workspace_service.draft_source_dir(task.workspace_id, run_id)
            if run_id and self.workspace_service.draft_exists(task.workspace_id, run_id)
            else self.workspace_service.source_dir(task.workspace_id)
        )
        changed_files: list[str] = []
        if run_id:
            try:
                changed_files = self._paths_from_diff(self.workspace_service.diff(task.workspace_id, run_id=run_id))
            except KeyError:
                changed_files = []
        self._append_output(task.task_id, "progress", "LSP diagnostics started.", {"run_id": run_id, "changed_only": changed_only, "files": files})
        if self.lsp_context_service is not None:
            payload = self.lsp_context_service.diagnostics(
                workspace_id=task.workspace_id,
                run_id=run_id,
                changed_only=changed_only,
                files=files or None,
            )
            self._append_output(
                task.task_id,
                "diagnostic_stream",
                f"LSP diagnostics completed: {payload.get('status')}",
                {
                    "status": payload.get("status"),
                    "engine": payload.get("engine"),
                    "diagnostics_ref": payload.get("diagnostics_ref"),
                    "error_count": payload.get("error_count", 0),
                    "warning_count": payload.get("warning_count", 0),
                },
            )
            task_ref = f"lsp_diagnostics_task:{task.task_id}"
            self.store.upsert("reports", task_ref, payload)
            return {
                "summary": f"LSP diagnostics {payload.get('status')}.",
                "diagnostics_ref": payload.get("diagnostics_ref") or f"lsp_diagnostics:{task.workspace_id}:{run_id or 'source'}",
                "task_diagnostics_ref": task_ref,
                "diagnostics_status": payload.get("status"),
                "error_count": payload.get("error_count", 0),
                "warning_count": payload.get("warning_count", 0),
            }
        report = LspToolService.diagnostics(
            root=source_dir,
            targets=files or None,
            changed_files=changed_files,
            changed_only=changed_only,
            progress_callback=lambda phase, payload: self._append_output(task.task_id, "diagnostic_stream", f"LSP phase {phase}: {payload.get('status')}", payload),
        )
        route_graph = LspToolService.route_graph(root=source_dir, targets=files or None)
        payload = {
            **report,
            "workspace_id": task.workspace_id,
            "run_id": run_id,
            "sources": sorted({str(item.get("source") or "unknown") for item in report.get("items") or []} or {"none"}),
            "symbols": LspToolService.symbol_context(root=source_dir, query="", targets=files or None).get("items", []),
            "route_graph": route_graph,
            "async_task_id": task.task_id,
        }
        ref = f"lsp_diagnostics:{task.workspace_id}:{run_id or 'source'}"
        task_ref = f"lsp_diagnostics_task:{task.task_id}"
        self.store.upsert("reports", ref, payload)
        self.store.upsert("reports", task_ref, payload)
        return {
            "summary": f"LSP diagnostics {payload.get('status')}.",
            "diagnostics_ref": ref,
            "task_diagnostics_ref": task_ref,
            "diagnostics_status": payload.get("status"),
            "error_count": payload.get("error_count", 0),
            "warning_count": payload.get("warning_count", 0),
        }

    def _memory_consolidate(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        stage1 = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"memory_stage1:{task.workspace_id}:") and isinstance(payload, dict)
        ]
        current = self.store.get("reports", f"workspace_memory:{task.workspace_id}") or {"workspace_id": task.workspace_id, "items": []}
        consolidated = WorkspaceMemoryPipeline.consolidate(
            task.workspace_id,
            stage1,
            current,
            workspace_root=self.workspace_service.source_dir(task.workspace_id),
        )
        consolidated["stale_check"] = WorkspaceMemoryPipeline.stale_check(self.workspace_service.source_dir(task.workspace_id), consolidated)
        self.store.upsert("reports", f"workspace_memory:{task.workspace_id}", consolidated)
        summary_ref = f"memory_consolidation:{task.workspace_id}"
        pipeline = consolidated.get("pipeline") if isinstance(consolidated.get("pipeline"), dict) else {}
        summary = {
            "schema": "grounded.memory_consolidation.v1",
            "workspace_id": task.workspace_id,
            "status": "consolidated",
            "stage1_count": len(stage1),
            "raw_count": int(pipeline.get("stage1_items", 0) or 0),
            "active_count": int(pipeline.get("active_count", 0) or 0),
            "stale_count": int(pipeline.get("stale_count", 0) or 0),
            "expired_count": int(pipeline.get("expired_count", 0) or 0),
            "superseded_count": int(pipeline.get("superseded_count", 0) or 0),
            "deduped_count": int(pipeline.get("deduped_count", 0) or 0),
            "repeated_failure_stats": pipeline.get("repeated_failure_stats") or WorkspaceMemoryPipeline.repeated_failure_stats(consolidated.get("items") or []),
            "updated_at": _now_iso(),
        }
        self.store.upsert("reports", summary_ref, summary)
        return {"summary": "Workspace memory consolidated.", "memory_ref": f"workspace_memory:{task.workspace_id}", "consolidation_ref": summary_ref}

    def _preview_rebuild(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        source_dir = None
        draft_run_id = str(task.input.get("draft_run_id") or task.run_id or "").strip() or None
        if draft_run_id and self.workspace_service.draft_exists(task.workspace_id, draft_run_id):
            source_dir = self.workspace_service.draft_source_dir(task.workspace_id, draft_run_id)
        preview = self.preview_service.rebuild(task.workspace_id, source_dir=source_dir, draft_run_id=draft_run_id)
        return {"summary": f"Preview rebuild {preview.status}.", "preview_id": preview.preview_id, "preview_status": preview.status, "preview_url": preview.url}

    def _pr_ci_babysit(self, task: BackgroundTaskRecord) -> dict[str, Any]:
        if self.pr_babysitter_service is None:
            raise ValueError("PR babysitter service is unavailable.")
        ref = f"pr_babysitter_task:{task.task_id}"
        max_polls = _bounded_int(task.input.get("max_polls"), default=30, minimum=1, maximum=720)
        poll_value = task.input.get("poll_seconds")
        if poll_value is None:
            poll_value = task.input.get("poll_interval_seconds")
        poll_seconds = _bounded_int(poll_value, default=60, minimum=0, maximum=3600)
        stop_when_ready = bool(task.input.get("stop_when_ready"))
        stop_actions = {"stop_pr_closed", "stop_user_help_required", "stop_exhausted_retries"}
        report: dict[str, Any] | None = None
        history: list[dict[str, Any]] = []
        terminal_reason: str | None = None

        for poll_index in range(max_polls):
            if self._stop_requested(task.task_id):
                terminal_reason = "stop_requested"
                break
            report = self.pr_babysitter_service.snapshot(
                workspace_id=task.workspace_id,
                pr=str(task.input.get("pr") or "auto"),
                repo=str(task.input.get("repo") or "").strip() or None,
                run_id=task.run_id or str(task.input.get("run_id") or "").strip() or None,
                export_id=str(task.input.get("export_id") or "").strip() or None,
                max_flaky_retries=int(task.input.get("max_flaky_retries") or 3),
                retry_failed_now=bool(task.input.get("retry_failed_now") or task.input.get("auto_retry")),
            )
            actions = [str(action) for action in (report.get("actions") or [])]
            history.append(
                {
                    "poll": poll_index + 1,
                    "status": report.get("status"),
                    "actions": actions,
                    "checks": report.get("checks") or {},
                    "pr": report.get("pr") or {},
                    "updated_at": report.get("updated_at"),
                }
            )
            self.store.upsert("reports", ref, {**report, "task_id": task.task_id, "poll": poll_index + 1, "max_polls": max_polls, "watch_history": history})
            self._append_output(
                task.task_id,
                "pr_snapshot",
                f"PR babysitter poll {poll_index + 1}/{max_polls}: {report.get('status')}.",
                {"report_ref": ref, "actions": actions, "checks": report.get("checks") or {}, "pr": report.get("pr") or {}, "poll": poll_index + 1, "max_polls": max_polls},
            )
            action_set = set(actions)
            terminal = sorted(action_set & stop_actions)
            if terminal:
                terminal_reason = terminal[0]
                break
            if stop_when_ready and "ready_to_merge" in action_set:
                terminal_reason = "ready_to_merge"
                break
            if poll_index + 1 >= max_polls:
                terminal_reason = "poll_horizon_reached"
                break
            self._append_output(task.task_id, "progress", "PR babysitter waiting for next poll.", {"poll_seconds": poll_seconds, "next_poll": poll_index + 2})
            deadline = time.monotonic() + poll_seconds
            while time.monotonic() < deadline:
                if self._stop_requested(task.task_id):
                    terminal_reason = "stop_requested"
                    break
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if terminal_reason == "stop_requested":
                break

        if report is None:
            raise ValueError("PR babysitter stopped before collecting a snapshot.")
        watch = {"polls_completed": len(history), "max_polls": max_polls, "poll_seconds": poll_seconds, "terminal_reason": terminal_reason or "unknown"}
        return {
            "summary": f"PR babysitter {report.get('status')} ({watch['terminal_reason']}).",
            "pr_babysitter_ref": ref,
            "actions": report.get("actions") or [],
            "pr": report.get("pr") or {},
            "watch": watch,
        }

    def _cancel(self, task: BackgroundTaskRecord, message: str) -> None:
        task.status = "cancelled"
        task.output_summary = message
        task.completed_at = _now()
        task.updated_at = _now()
        self._save(task)
        self._append_output(task.task_id, "task_cancelled", message, {})

    def _stop_requested(self, task_id: str) -> bool:
        payload = self.store.get("reports", f"background_task_stop:{task_id}") or {}
        return bool(payload.get("requested"))

    def _save(self, task: BackgroundTaskRecord) -> None:
        task.updated_at = _now()
        self.store.upsert("background_tasks", task.task_id, task.model_dump(mode="json"))

    def _append_output(self, task_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        key = f"background_task_output:{task_id}"
        output = self.store.get("reports", key) or {"schema": "grounded.background_task_output.v1", "task_id": task_id, "items": []}
        items = [item for item in output.get("items") or [] if isinstance(item, dict)]
        items.append(
            {
                "sequence": len(items) + 1,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "created_at": _now_iso(),
            }
        )
        output["items"] = items[-1000:]
        output["updated_at"] = _now_iso()
        self.store.upsert("reports", key, output)

    @staticmethod
    def _paths_from_diff(diff: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff or "").splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    paths.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
            elif line.startswith("+++ b/"):
                paths.append(line[6:])
        return list(dict.fromkeys(paths))

    @staticmethod
    def _default_title(task_type: str) -> str:
        labels = {
            "generate_product": "Generate product",
            "repair_failed_run": "Repair failed run",
            "browser_verify": "Browser verification",
            "lsp_diagnostics": "LSP diagnostics",
            "memory_consolidate": "Consolidate memory",
            "worker_branch": "Worker branch",
            "preview_rebuild": "Preview rebuild",
            "pr_ci_babysit": "PR/CI babysitter",
        }
        return labels.get(task_type, str(task_type or "Background task"))
