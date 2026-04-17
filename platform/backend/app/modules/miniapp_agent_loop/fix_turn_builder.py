from __future__ import annotations

from typing import Any

from app.models.domain import ContainerStatusRecord
from app.modules.miniapp_agent_loop.fix_prompt_builder import FixPromptBuilder
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext


class FixTurnBuilder:
    def __init__(self, prompt_builder: FixPromptBuilder) -> None:
        self.prompt_builder = prompt_builder

    def build_turn_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        attempt: int,
        request,
        check_execution,
        preview_details: dict[str, Any],
        prior_attempts,
        existing_scope,
        memory_context: str | None,
        augment_failure_evidence,
        implicated_files,
        specialized_failure_class,
        classify_failure_text,
        root_cause_summary,
        failure_signature,
        error_excerpt,
        first_failing_command,
        write_scope,
    ) -> FixTurnContext:
        raw_error = request.error_context.raw_error if request.error_context else request.prompt
        combined_text = "\n".join(
            [
                raw_error,
                *[item.details or "" for item in check_execution.results],
                *[line for result in check_execution.results for line in result.logs],
                *(preview_details.get("logs") or []),
            ]
        )
        combined_text = augment_failure_evidence(combined_text, check_execution.results)
        implicated = implicated_files(workspace_id, run_id, combined_text, existing_scope)
        failure_class = specialized_failure_class(
            workspace_id=workspace_id,
            run_id=run_id,
            results=check_execution.results,
            combined_text=combined_text,
            implicated_files=implicated,
        )
        if not failure_class:
            failure_class = classify_failure_text(combined_text) or "build/runtime"
        root_cause = root_cause_summary(check_execution.results, preview_details, raw_error)
        signature = failure_signature(failure_class, root_cause)
        scope = write_scope(workspace_id, run_id, implicated, failure_class, existing_scope)
        excerpt = error_excerpt(check_execution.results, preview_details, raw_error)
        container_statuses = [
            ContainerStatusRecord.model_validate(item)
            for item in preview_details.get("containers", [])
            if isinstance(item, dict)
        ]
        return FixTurnContext(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt=attempt,
            failure_class=failure_class,
            failure_signature=signature,
            failing_command=first_failing_command(check_execution.results),
            root_cause_summary=root_cause,
            exact_error_excerpt=excerpt,
            implicated_files=implicated,
            container_statuses=container_statuses,
            container_logs=preview_details.get("container_logs", {}),
            write_scope=scope,
            attempt_history=[item.model_dump(mode="json") for item in prior_attempts[-4:]],
            executed_checks=check_execution.results,
            memory_context=memory_context,
        )

    def build_prompt_context(
        self,
        *,
        workspace_id: str,
        run_id: str,
        fix_turn: FixTurnContext,
        scope_entries,
        context_mode: str,
        collect_file_contexts,
        merge_additional_context_paths,
        deterministic_contract_seed_paths,
        current_diff_summary,
        additional_paths: list[str] | None = None,
    ) -> FixPromptContext:
        full_files = context_mode in {"expanded", "full_bundle"} or self.prompt_builder.needs_full_context_first(fix_turn)
        budget = 32000 if full_files else 12000
        file_contexts = collect_file_contexts(
            workspace_id,
            run_id,
            scope_entries,
            fix_turn=fix_turn,
            budget_override=budget,
            full_files=full_files,
        )
        extra_paths = list(additional_paths or [])
        if context_mode == "full_bundle":
            extra_paths.extend(deterministic_contract_seed_paths(workspace_id, run_id, fix_turn, scope_entries))
        if extra_paths:
            file_contexts = merge_additional_context_paths(
                workspace_id,
                run_id,
                file_contexts,
                extra_paths,
                budget_override=budget,
            )
        return FixPromptContext(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt=fix_turn.attempt,
            failure_class=fix_turn.failure_class,
            failure_signature=fix_turn.failure_signature,
            root_cause_summary=fix_turn.root_cause_summary,
            exact_error_excerpt=fix_turn.exact_error_excerpt,
            context_mode=context_mode,  # type: ignore[arg-type]
            failing_checks=[
                {
                    "name": item.name,
                    "status": item.status,
                    "details": item.details,
                    "logs": item.logs[-12:],
                }
                for item in fix_turn.executed_checks
                if item.status == "failed"
            ],
            normalized_critical_issues=self.prompt_builder.normalized_critical_issues(
                fix_turn.executed_checks,
                failure_class=fix_turn.failure_class,
            ),
            failing_file_paths=list(fix_turn.implicated_files),
            deterministic_companions=[entry.file_path for entry in scope_entries],
            expected_contract=self.prompt_builder.expected_contract_snapshot(fix_turn),
            file_contexts=file_contexts,
            read_only_surfaces=self.prompt_builder.read_only_surfaces(),
            previous_attempt_summary=self.prompt_builder.previous_attempt_summary(fix_turn),
            previous_diff_summary=current_diff_summary(workspace_id, run_id),
        )
