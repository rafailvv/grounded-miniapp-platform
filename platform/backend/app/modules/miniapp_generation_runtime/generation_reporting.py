from __future__ import annotations

from typing import Any

from app.models.artifacts import MaterializationReport, TraceabilityReportEntry, TraceabilityReportModel
from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.models.domain import new_id
from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.modules.miniapp_generation_runtime.generation_paths import MiniappGenerationPaths
from app.services.miniapp_generation.artifact_builder import MiniappArtifactBuilder

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationReporting(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _build_stage_reports(
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> list[dict[str, Any]]:
        return MiniappArtifactBuilder(
            normalize_role_route_path=lambda role, route_path: MiniappGenerationPaths._normalize_role_route_path(role, route_path, index=0),
            absolute_role_route_path=MiniappGenerationPaths._absolute_role_route_path,
            default_page_asset_path=lambda file_path, asset_kind: MiniappGenerationPaths._default_page_asset_path(file_path, asset_kind=asset_kind),
            normalize_runtime_python_path=MiniappMaterializationService.normalize_runtime_python_path,
        ).build_stage_reports(page_graph=page_graph, role_scope=role_scope, realized_paths=realized_paths)

    @classmethod
    def _build_materialization_report(
        cls,
        *,
        execution_class: str,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> MaterializationReport:
        return MiniappMaterializationService(
            default_page_asset_path=lambda file_path, asset_kind: MiniappGenerationPaths._default_page_asset_path(file_path, asset_kind=asset_kind),
            workspace_file_tree=lambda workspace_id, run_id: [],
            build_stage_reports=lambda page_graph, role_scope, realized_paths: cls._build_stage_reports(
                page_graph=page_graph,
                role_scope=role_scope,
                realized_paths=realized_paths,
            ),
        ).build_materialization_report(
            execution_class=execution_class,
            page_graph=page_graph,
            role_scope=role_scope,
            realized_paths=realized_paths,
        )

    def _build_agent_traceability_report(
        self,
        workspace_id: str,
        grounded_spec: GroundedSpecModel,
        operations: list[DraftFileOperation],
    ) -> TraceabilityReportModel:
        entries = [
            TraceabilityReportEntry(
                trace_id=new_id("trace"),
                source_ref="prompt-source",
                source_kind="user_prompt",
                target_id=operation.file_path,
                target_type="file",
                mapping_note=f"Prompt-grounded edit for {operation.file_path}.",
            )
            for operation in operations
        ]
        for doc_ref in grounded_spec.doc_refs[:3]:
            entries.append(
                TraceabilityReportEntry(
                    trace_id=new_id("trace"),
                    source_ref=doc_ref.doc_ref_id,
                    source_kind=doc_ref.source_type,
                    target_id="grounded_spec",
                    target_type="planning_artifact",
                    mapping_note="Source material kept for planning/debug traceability.",
                )
            )
        return TraceabilityReportModel(report_id=new_id("trace"), workspace_id=workspace_id, entries=entries)

    @staticmethod
    def _build_agent_summary(
        *,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        operations: list[DraftFileOperation],
        generation_mode: GenerationMode,
        assistant_message: str,
    ) -> str:
        safe_message = MiniappGenerationReporting._safe_success_assistant_message(assistant_message)
        prefix = f"{safe_message} " if safe_message else ""
        return (
            f"{prefix}Built a {generation_mode.value} draft for {grounded_spec.target_platform} "
            f"with {len(operations)} file operations across {len(role_scope)} role views."
        )

    @staticmethod
    def _safe_success_assistant_message(assistant_message: str) -> str:
        """Keep successful run summaries user-facing, not provider/repair diagnostics."""

        message = " ".join(str(assistant_message or "").split()).strip()
        if not message:
            return ""
        internal_markers = (
            "automatic repair",
            "can only afford",
            "could not be generated",
            "fallback",
            "initial iteration diagnostics",
            "openrouter",
            "provider budget",
            "provider-budget",
            "repair attempt",
            "requires more credits",
            "timed out during whole-file generation",
        )
        lowered = message.lower()
        if any(marker in lowered for marker in internal_markers):
            return ""
        return message

    @staticmethod
    def _compile_code_summary(operations: list[DraftFileOperation], role_scope: list[str]) -> dict[str, int | str]:
        return {
            "file_count": len({operation.file_path for operation in operations}),
            "operation_count": len(operations),
            "role_count": len(role_scope),
            "iteration_count": 1,
        }
