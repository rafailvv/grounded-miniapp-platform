from __future__ import annotations

from app.modules.miniapp_generation_runtime import (
    GenerationProgressReportingRuntime,
    MiniappGenerationCompletion,
    MiniappGenerationReporting,
    MiniappGenerationReportingCompaction,
    MiniappGenerationReportingRepair,
)


class CompatReportingMixins:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, object]:
        base = super()._compat_class_owner_map()
        return {
            **base,
            "_workspace_loop_completion_state": MiniappGenerationCompletion,
            "_build_stage_reports": MiniappGenerationReporting,
            "_build_materialization_report": MiniappGenerationReporting,
            "_build_agent_traceability_report": MiniappGenerationReporting,
            "_build_agent_summary": MiniappGenerationReporting,
            "_compile_code_summary": MiniappGenerationReporting,
            "_limit_text": MiniappGenerationReportingCompaction,
            "_bounded_file_contexts": MiniappGenerationReportingCompaction,
            "_compact_grounded_spec_for_codegen": MiniappGenerationReportingCompaction,
            "_compact_role_contract_for_codegen": MiniappGenerationReportingCompaction,
            "_compact_page_graph_for_codegen": MiniappGenerationReportingCompaction,
            "_stateful_page_contracts": MiniappGenerationReportingRepair,
            "_failure_signature_for_issues": MiniappGenerationReportingRepair,
            "_is_structural_contract_failure": MiniappGenerationReportingRepair,
            "_extract_failure_file_hints": MiniappGenerationReportingRepair,
            "_causal_surface_for_issues": MiniappGenerationReportingRepair,
            "_expand_structural_repair_targets": MiniappGenerationReportingRepair,
            "_repair_targets_for_attempt": MiniappGenerationReportingRepair,
            "_compact_file_contexts_for_repair": MiniappGenerationReportingCompaction,
            "_whole_file_parallel_group": GenerationProgressReportingRuntime,
            "_group_generation_clusters_for_execution": GenerationProgressReportingRuntime,
            "_run_progress_for_event": GenerationProgressReportingRuntime,
        }

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, object]:
        base = super()._compat_instance_owner_map()
        return {
            **base,
            "_workspace_loop_validation_snapshot_from_execution": "generation_completion",
            "_run_generation_workspace_loop": "generation_repair",
            "_repair_draft_after_failure": "generation_repair",
            "_append_event": "generation_progress_reporting",
            "_save_job": "generation_progress_reporting",
            "_store_report": "generation_progress_reporting",
            "_clear_trace": "generation_progress_reporting",
            "_append_trace": "generation_progress_reporting",
            "_sync_run_progress": "generation_progress_reporting",
            "_sync_generation_cluster_progress": "generation_progress_reporting",
            "_sync_generation_cluster_started": "generation_progress_reporting",
            "_sync_generation_batch_started": "generation_progress_reporting",
        }
