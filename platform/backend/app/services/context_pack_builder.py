from __future__ import annotations

import hashlib

from app.models.common import GenerationMode
from app.models.domain import CodeChunkRecord, ContextPack, WorkspaceRecord
from app.services.code_index_service import CodeIndexService
from app.services.workspace_service import WorkspaceService


class ContextPackBuilder:
    def __init__(self, code_index_service: CodeIndexService, workspace_service: WorkspaceService) -> None:
        self.code_index_service = code_index_service
        self.workspace_service = workspace_service

    def build(
        self,
        *,
        workspace: WorkspaceRecord,
        prompt: str,
        model_profile: str,
        generation_mode: GenerationMode = GenerationMode.BALANCED,
        active_paths: list[str] | None = None,
        target_files: list[str] | None = None,
        run_id: str | None = None,
    ) -> ContextPack:
        code_limit, doc_limit = self._retrieval_limits(generation_mode)
        retrieval = self.code_index_service.retrieve(
            workspace_id=workspace.workspace_id,
            prompt=prompt,
            code_limit=code_limit,
            doc_limit=doc_limit,
            active_paths=active_paths or target_files or [],
            recent_paths=self._recent_paths(workspace),
        )
        targeted_files: dict[str, str] = {}
        file_targets = list(target_files or [])
        if generation_mode == GenerationMode.FAST:
            file_targets = file_targets[:8]
        for file_path in file_targets:
            try:
                targeted_files[file_path] = self.workspace_service.read_file(workspace.workspace_id, file_path, run_id=run_id)
            except FileNotFoundError:
                continue
        stable_prefix = self._stable_prefix(workspace, model_profile)
        return ContextPack(
            workspace_id=workspace.workspace_id,
            revision_id=workspace.current_revision_id,
            prompt=prompt,
            system_prefix=stable_prefix,
            workspace_summary=self._workspace_summary(workspace),
            current_task=prompt.strip(),
            recent_diff=self.workspace_service.diff(workspace.workspace_id, run_id=run_id) if workspace.template_cloned else "",
            code_chunks=[CodeChunkRecord.model_validate(item) for item in retrieval["code"]],  # type: ignore[index]
            doc_chunks=[CodeChunkRecord.model_validate(item) for item in retrieval["docs"]],  # type: ignore[index]
            targeted_files=targeted_files,
            prompt_cache_key=self._prompt_cache_key(workspace, model_profile, stable_prefix),
            retrieval_stats=dict(retrieval["stats"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _workspace_summary(workspace: WorkspaceRecord) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        return (
            f"Workspace {workspace.name}. Target platform: {platform}. "
            f"Template cloned: {workspace.template_cloned}. Current revision: {workspace.current_revision_id or 'none'}."
        )

    @staticmethod
    def _stable_prefix(workspace: WorkspaceRecord, model_profile: str) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        return (
            "You are editing a grounded mini-app workspace. "
            "Prefer minimal targeted changes, preserve role separation, and keep generated artifacts consistent. "
            f"Model profile: {model_profile}. Target platform: {platform}. "
            "Defer non-essential file reads. Use retrieved chunks before widening context."
        )

    @staticmethod
    def _prompt_cache_key(workspace: WorkspaceRecord, model_profile: str, stable_prefix: str) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        material = f"{workspace.workspace_id}:{platform}:{model_profile}:{stable_prefix}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _recent_paths(workspace: WorkspaceRecord) -> list[str]:
        return [revision.message.split(": ", 1)[-1] for revision in workspace.revisions[-5:] if ": " in revision.message]

    @staticmethod
    def _retrieval_limits(generation_mode: GenerationMode) -> tuple[int, int]:
        if generation_mode == GenerationMode.FAST:
            return 3, 2
        if generation_mode == GenerationMode.BALANCED:
            return 5, 3
        return 6, 4
