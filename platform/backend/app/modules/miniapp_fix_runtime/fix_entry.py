from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.models.common import GenerationMode
from app.models.domain import GenerateRequest, JobRecord, ValidationSnapshot
from app.modules.miniapp_agent_loop.types import WorkspaceLoopCallbacks, WorkspaceLoopTurnPlan
from app.modules.miniapp_agent_loop.tool_agent_runtime import (
    list_workspace_files,
    run_workspace_command,
    search_workspace_files,
    summarize_read_file_payloads,
)
from app.services.check_runner import CheckRunner

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixEntryRuntime:
    MAX_TOOL_ROUNDS = 7
    COMMAND_TIMEOUT_SECONDS = 20

    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    @staticmethod
    def _tool_request_signature(tool_requests: list[dict[str, object]]) -> str:
        normalized_items: list[dict[str, object]] = []
        for item in tool_requests:
            normalized_items.append(
                {
                    "tool": str(item.get("tool") or "").strip().lower(),
                    "mode": str(item.get("mode") or "").strip().lower(),
                    "targets": sorted(
                        {
                            str(target or "").strip().lstrip("./")
                            for target in list(item.get("targets") or [])
                            if str(target or "").strip()
                        }
                    ),
                    "pattern": str(item.get("pattern") or "").strip(),
                    "command": str(item.get("command") or "").strip(),
                }
            )
        return str(normalized_items)

    @staticmethod
    def _duplicate_tool_request_feedback(tool_requests: list[dict[str, object]]) -> dict[str, object]:
        requested_targets = [
            str(target or "").strip().lstrip("./")
            for item in tool_requests
            for target in list(item.get("targets") or [])
            if str(target or "").strip()
        ]
        return {
            "tool": "tool_request_feedback",
            "targets": list(dict.fromkeys(requested_targets)),
            "error": (
                "The same tool request was already executed in this repair attempt. "
                "Use the current file_contexts and prior tool_results to return operations "
                "or outcome=no_progress instead of requesting the same files again."
            ),
        }

    @staticmethod
    def _read_request_already_satisfied(
        tool_requests: list[dict[str, object]],
        file_contexts: dict[str, str],
    ) -> bool:
        saw_read = False
        for item in tool_requests:
            tool_name = str(item.get("tool") or "").strip().lower()
            if tool_name != "read_files":
                return False
            saw_read = True
            targets = [
                str(target or "").strip().lstrip("./")
                for target in list(item.get("targets") or [])
                if str(target or "").strip()
            ]
            if not targets:
                return False
            for target in targets:
                content = str(file_contexts.get(target) or "")
                if not content.strip() or content.startswith("FILE_MISSING:"):
                    return False
        return saw_read

    def generate_with_workspace_loop(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request: GenerateRequest,
        job: JobRecord,
        draft_source: Path,
        started_at: float,
        role_scope: list[str],
        effective_mode: GenerationMode,
        memory_context: str | None,
        should_stop: Callable[[], bool] | None,
    ) -> JobRecord:
        del role_scope
        scope_entries = []
        scope_expansions: list[dict[str, object]] = []
        pending_scope_requests: list[str] = []

        def _call_plan_patch(*, prompt_context):
            planner = self.service._plan_patch
            parameters = inspect.signature(planner).parameters
            if "prompt_context" in parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                return planner(job=job, prompt_context=prompt_context)
            if "repair_packet" in parameters:
                return planner(job=job, repair_packet=prompt_context)
            return planner(job=job, prompt_context=prompt_context)

        def _execute_checks(changed_files: list[str]):
            return self.service._execute_exact_checks(
                job=job,
                workspace_id=workspace_id,
                run_id=run_id,
                draft_source=draft_source,
                changed_files=changed_files or ["miniapp"],
            )

        def _execute_check_action(mode: str, changed_files: list[str]):
            normalized_mode = str(mode or "exact").strip().lower()
            if normalized_mode == "final":
                return self.service._execute_final_checks(
                    job=job,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    draft_source=draft_source,
                    changed_files=changed_files or ["miniapp"],
                )
            return _execute_checks(changed_files)

        def _search_workspace(*, pattern: str, targets: list[str]) -> dict[str, object]:
            source_dir = self.service.workspace_service.draft_source_dir(workspace_id, run_id)
            result = search_workspace_files(
                workspace_tree=self.service.workspace_service.file_tree(workspace_id, run_id),
                read_text_file=lambda relative_path: self.service.workspace_service.try_read_text_file(
                    workspace_id,
                    relative_path,
                    run_id=run_id,
                ),
                pattern=pattern,
                targets=targets,
            )
            return {**result, "workspace_root": str(source_dir)}

        def _list_workspace_files(*, targets: list[str]) -> dict[str, object]:
            return list_workspace_files(
                workspace_tree=self.service.workspace_service.file_tree(workspace_id, run_id),
                targets=targets,
            )

        def _run_workspace_command(*, command: str) -> dict[str, object]:
            return run_workspace_command(
                draft_source=draft_source,
                command=command,
                timeout_seconds=self.COMMAND_TIMEOUT_SECONDS,
            )

        def _adopt_requested_scope(
            requested_paths: list[str],
            *,
            fix_turn,
            reason: str,
        ) -> list[str]:
            approved: list[str] = []
            for path in requested_paths:
                normalized = str(path or "").strip().lstrip("./")
                if not normalized or normalized in pending_scope_requests:
                    continue
                if self.service._is_read_only_generated_surface(normalized):
                    continue
                if normalized.startswith("miniapp/tests/") and not self.service._allow_test_file_writes_for_failure(fix_turn.failure_class or ""):
                    continue
                if not (
                    self.service._can_expand_for_file(normalized, fix_turn.implicated_files)
                    or self.service._file_exists(workspace_id, run_id, normalized)
                    or self.service._allow_missing_scope_path(normalized)
                ):
                    continue
                approved.append(normalized)
            if not approved:
                return []
            pending_scope_requests.extend(approved)
            scope_expansions.append(
                {
                    "attempt": fix_turn.attempt,
                    "files": list(approved),
                    "reason": reason,
                }
            )
            return approved

        def _execute_tool_requests(
            *,
            fix_turn,
            tool_requests: list[dict[str, object]],
        ) -> tuple[list[str], list[dict[str, object]]]:
            additional_paths: list[str] = []
            tool_results: list[dict[str, object]] = []
            for request_item in tool_requests:
                tool_name = str(request_item.get("tool") or "").strip().lower()
                targets = [
                    str(item or "").strip().lstrip("./")
                    for item in list(request_item.get("targets") or [])
                    if str(item or "").strip()
                ]
                reason = str(request_item.get("reason") or "").strip()
                if tool_name == "read_files":
                    approved = _adopt_requested_scope(
                        targets,
                        fix_turn=fix_turn,
                        reason=reason or "Repair agent requested additional file reads.",
                    )
                    loaded_contents: dict[str, str] = {}
                    for path in approved:
                        if path not in additional_paths:
                            additional_paths.append(path)
                        content = self.service.workspace_service.try_read_text_file(
                            workspace_id,
                            path,
                            run_id=run_id,
                        )
                        if content is not None:
                            loaded_contents[path] = content
                    tool_results.append(
                        {
                            "tool": "read_files",
                            "targets": list(targets),
                            "approved_targets": list(approved),
                            "files": summarize_read_file_payloads(file_contents=loaded_contents),
                            "reason": reason,
                        }
                    )
                    continue
                if tool_name == "list_files":
                    tool_results.append(
                        {
                            **_list_workspace_files(targets=targets),
                            "reason": reason,
                        }
                    )
                    continue
                if tool_name == "run_checks":
                    requested_changed_files = list(targets or ["miniapp"])
                    mode = str(request_item.get("mode") or "exact").strip().lower()
                    execution, preview_details = _execute_check_action(mode, requested_changed_files)
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
                            "mode": mode,
                            "targets": requested_changed_files,
                            "reason": reason,
                            "failed_checks": failed_checks,
                            "preview_logs": list((preview_details.get("logs") or [])[-8:]),
                        }
                    )
                    continue
                if tool_name == "search_files":
                    pattern = str(request_item.get("pattern") or "").strip()
                    tool_results.append(
                        {
                            **_search_workspace(pattern=pattern, targets=targets),
                            "reason": reason,
                        }
                    )
                    continue
                if tool_name == "run_command":
                    command = str(request_item.get("command") or "").strip()
                    tool_results.append(
                        {
                            **_run_workspace_command(command=command),
                            "reason": reason,
                        }
                    )
            return additional_paths, tool_results

        def _plan_turn(
            *,
            attempt: int,
            latest_execution,
            latest_preview_details: dict[str, object],
            validation_snapshot: ValidationSnapshot,
            context_mode: str,
            repeated_no_progress: int,
            last_turn_summary: str | None,
            latest_diff_summary: str | None,
        ) -> WorkspaceLoopTurnPlan:
            del validation_snapshot
            fix_turn = self.service._build_fix_case(
                workspace_id=workspace_id,
                run_id=run_id,
                attempt=attempt,
                request=request,
                check_execution=latest_execution,
                preview_details=latest_preview_details,
                prior_attempts=[],
                existing_scope=scope_entries,
                memory_context=memory_context,
            )
            next_scope = self.service._build_write_scope(
                workspace_id,
                run_id,
                list(dict.fromkeys([*fix_turn.implicated_files, *pending_scope_requests])),
                fix_turn.failure_class or "build/runtime",
                scope_entries,
            )
            scope_entries[:] = self.service._merge_scope(scope_entries, next_scope, scope_expansions)
            job.failure_class = self.service._prefer_failure_class(job.failure_class, fix_turn.failure_class)
            job.failure_signature = fix_turn.failure_signature
            job.root_cause_summary = fix_turn.root_cause_summary
            job.fix_targets = list(fix_turn.implicated_files)
            job.validation_snapshot = self.service._validation_snapshot_from_execution(latest_execution)
            self.service._store_report(
                f"fix_case:{workspace_id}",
                {
                    "workspace_id": fix_turn.workspace_id,
                    "run_id": fix_turn.run_id,
                    "attempt": fix_turn.attempt,
                    "failure_class": fix_turn.failure_class,
                    "failure_signature": fix_turn.failure_signature,
                    "root_cause_summary": fix_turn.root_cause_summary,
                    "implicated_files": list(fix_turn.implicated_files),
                    "write_scope": [entry.model_dump(mode="json") for entry in fix_turn.write_scope],
                },
            )
            self.service._append_event(
                job,
                "triage_completed",
                fix_turn.root_cause_summary or "Fix evidence packet prepared.",
                {
                    "attempt": attempt,
                    "failure_class": fix_turn.failure_class,
                    "failure_signature": fix_turn.failure_signature,
                    "implicated_files": fix_turn.implicated_files,
                    "context_mode": context_mode,
                    "repeated_no_progress": repeated_no_progress,
                },
            )
            extra_paths_for_attempt: list[str] = []
            tool_results_for_attempt: list[dict[str, object]] = []
            last_prompt_context = None
            seen_tool_request_signatures: set[str] = set()
            api_diag_test_reread_count = 0
            for tool_round in range(self.MAX_TOOL_ROUNDS + 1):
                prompt_context = self.service._build_repair_packet(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    fix_turn=fix_turn,
                    scope_entries=scope_entries,
                    context_mode=context_mode,
                    additional_paths=extra_paths_for_attempt,
                    tool_results=tool_results_for_attempt,
                    repair_base=job.repair_base,
                )
                last_prompt_context = prompt_context
                if last_turn_summary or latest_diff_summary:
                    prompt_context.previous_attempt_summary = last_turn_summary or prompt_context.previous_attempt_summary
                    prompt_context.previous_diff_summary = latest_diff_summary or prompt_context.previous_diff_summary
                self.service._append_event(
                    job,
                    "repair_planned",
                    "Prepared repair packet for the current failure bundle.",
                    {
                        "attempt": attempt,
                        "scope": [entry.file_path for entry in scope_entries],
                        "context_mode": prompt_context.context_mode,
                        "tool_round": tool_round,
                    },
                )
                llm_result = _call_plan_patch(prompt_context=prompt_context)
                repair_outcome = self.service._repair_outcome_from_response(
                    llm_result=llm_result,
                    prompt_context=prompt_context,
                    fix_turn=fix_turn,
                    scope_entries=scope_entries,
                    scope_expansions=scope_expansions,
                )
                if repair_outcome.outcome == "tool_request":
                    normalized_tool_requests = list(repair_outcome.tool_requests)
                    read_test_only = bool(normalized_tool_requests) and all(
                        str(item.get("tool") or "").strip().lower() == "read_files"
                        and all(
                            str(target or "").strip().lstrip("./") == "miniapp/tests/test_generated_app.py"
                            for target in list(item.get("targets") or [])
                            if str(target or "").strip()
                        )
                        for item in normalized_tool_requests
                    )
                    tool_request_signature = self._tool_request_signature(normalized_tool_requests)
                    duplicate_request = bool(tool_request_signature) and tool_request_signature in seen_tool_request_signatures
                    already_satisfied_read = self._read_request_already_satisfied(
                        normalized_tool_requests,
                        prompt_context.file_contexts,
                    )
                    if read_test_only and prompt_context.api_failure_diagnostics:
                        api_diag_test_reread_count += 1
                        requested_paths = []
                        executed_tool_results = [
                            {
                                "tool": "tool_request_feedback",
                                "targets": ["miniapp/tests/test_generated_app.py"],
                                "error": (
                                    "The failing request is already structured in api_failure_diagnostics. "
                                    "Patch the implicated route/schema/db cluster or return outcome=no_progress."
                                ),
                            }
                        ]
                        if api_diag_test_reread_count >= 2:
                            return WorkspaceLoopTurnPlan(
                                outcome="no_op",
                                assistant_message="The fix loop is rereading miniapp/tests/test_generated_app.py instead of patching the implicated route/schema/db cluster.",
                                diagnosis=(
                                    "Structured API failure diagnostics are already available. "
                                    "Patch the implicated route/schema/db files or escalate the repair base."
                                ),
                                files_read=list(prompt_context.file_contexts.keys()),
                                failure_class=fix_turn.failure_class,
                                failure_signature=fix_turn.failure_signature,
                                root_cause_summary=fix_turn.root_cause_summary,
                                fix_targets=list(fix_turn.implicated_files),
                                metadata={"tool_requests": list(repair_outcome.tool_requests), "stall_reason": "repeated_test_reread"},
                            )
                    elif duplicate_request or already_satisfied_read:
                        requested_paths = []
                        executed_tool_results = [
                            self._duplicate_tool_request_feedback(normalized_tool_requests),
                        ]
                    else:
                        if tool_request_signature:
                            seen_tool_request_signatures.add(tool_request_signature)
                        requested_paths, executed_tool_results = _execute_tool_requests(
                            fix_turn=fix_turn,
                            tool_requests=normalized_tool_requests,
                        )
                    for path in requested_paths:
                        if path not in extra_paths_for_attempt:
                            extra_paths_for_attempt.append(path)
                    tool_results_for_attempt.extend(executed_tool_results)
                    self.service._append_event(
                        job,
                        "scope_expanded",
                        repair_outcome.diagnosis or "Repair agent requested explicit tool actions.",
                        {
                            "attempt": attempt,
                            "tool_round": tool_round,
                            "tool_requests": list(repair_outcome.tool_requests),
                        },
                    )
                    if tool_round < self.MAX_TOOL_ROUNDS and (requested_paths or executed_tool_results):
                        continue
                    return WorkspaceLoopTurnPlan(
                        outcome="needs_context",
                        assistant_message=str(repair_outcome.diagnosis or ""),
                        diagnosis=repair_outcome.validation_error or repair_outcome.diagnosis,
                        files_read=list(prompt_context.file_contexts.keys()),
                        failure_class=fix_turn.failure_class,
                        failure_signature=fix_turn.failure_signature,
                        root_cause_summary=fix_turn.root_cause_summary,
                        fix_targets=list(fix_turn.implicated_files),
                        metadata={"tool_requests": list(repair_outcome.tool_requests)},
                    )
                if repair_outcome.outcome == "fatal_invalid_response":
                    return WorkspaceLoopTurnPlan(
                        outcome="fatal_invalid_response",
                        assistant_message=str(repair_outcome.diagnosis or ""),
                        diagnosis=repair_outcome.validation_error or repair_outcome.diagnosis,
                        files_read=list(prompt_context.file_contexts.keys()),
                        failure_class=fix_turn.failure_class,
                        failure_signature=fix_turn.failure_signature,
                        root_cause_summary=fix_turn.root_cause_summary,
                        fix_targets=list(fix_turn.implicated_files),
                        metadata={"raw_response": repair_outcome.raw_response},
                    )
                return WorkspaceLoopTurnPlan(
                    outcome="patch_ready" if repair_outcome.outcome == "patch_ready" else "no_op",
                    assistant_message=str(repair_outcome.diagnosis or ""),
                    diagnosis=repair_outcome.validation_error or repair_outcome.diagnosis,
                    operations=list(repair_outcome.operations),
                    files_read=list(prompt_context.file_contexts.keys()),
                    failure_class=fix_turn.failure_class,
                    failure_signature=fix_turn.failure_signature,
                    root_cause_summary=fix_turn.root_cause_summary,
                    fix_targets=list(fix_turn.implicated_files),
                    expected_verification=repair_outcome.expected_verification,
                    rationale_by_file=dict(repair_outcome.rationale_by_file),
                    metadata={"tool_results": list(tool_results_for_attempt)},
                )
            return WorkspaceLoopTurnPlan(
                outcome="no_op",
                assistant_message="Repair agent exhausted the tool-request budget without producing a patch.",
                diagnosis="Repair agent exhausted the tool-request budget without producing a patch.",
                files_read=list(last_prompt_context.file_contexts.keys()) if last_prompt_context is not None else [],
                failure_class=fix_turn.failure_class,
                failure_signature=fix_turn.failure_signature,
                root_cause_summary=fix_turn.root_cause_summary,
                fix_targets=list(fix_turn.implicated_files),
                metadata={"tool_results": list(tool_results_for_attempt)},
            )

        callbacks = WorkspaceLoopCallbacks(
            execute_checks=_execute_checks,
            build_validation_snapshot=self.service._validation_snapshot_from_execution,
            completion_state=self.service._completion_state_from_results,
            has_tooling_failure=CheckRunner.has_tooling_failure,
            plan_turn=_plan_turn,
            apply_contract_sync=lambda operations: self.service.generation_service._run_pre_apply_contract_pass(
                workspace_id=workspace_id,
                draft_run_id=run_id,
                page_graph=self.service._page_graph_for_run(workspace_id, run_id),
                role_scope=self.service._role_scope_for_fix_request(workspace_id, run_id, request),
                generation_mode=effective_mode,
                operations=list(operations),
                contract_sync_mode="repair_invariants",
            )
            if self.service.generation_service is not None
            else list(operations),
            post_apply_stabilize=lambda current_workspace_id, _run_id, current_draft_source, _changed_files: (
                self.service.generation_service.generation_normal_loop.stabilize_draft_contract_from_source(
                    workspace_id=current_workspace_id,
                    draft_source=current_draft_source,
                )
                if self.service.generation_service is not None
                else []
            ),
            append_event=self.service._append_event,
            append_trace=self.service._append_trace,
            store_report=self.service._store_report,
            stop_if_requested=should_stop,
        )
        loop_result = self.service.workspace_loop_engine.run(
            workspace_id=workspace_id,
            run_id=run_id,
            job=job,
            draft_source=draft_source,
            role_scope=self.service._role_scope_for_fix_request(workspace_id, run_id, request),
            generation_mode=effective_mode,
            max_attempts=self.service.MAX_ATTEMPTS,
            initial_operations=[],
            initial_assistant_message="Fix workspace loop initialized.",
            initial_files_read=[],
            initial_changed_files=["miniapp"],
            callbacks=callbacks,
        )
        return self.service._finalize_loop_job(
            job=job,
            loop_result=loop_result,
            scope_expansions=scope_expansions,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )
