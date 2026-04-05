from __future__ import annotations

import hashlib
from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel
from app.models.domain import CodeChunkRecord, ContextPack, WorkspaceRecord
from app.services.code_index_service import CodeIndexService
from app.services.workspace_service import WorkspaceService


class ContextPackBuilder:
    def __init__(
        self,
        code_index_service: CodeIndexService,
        workspace_service: WorkspaceService,
        context_budget_manager: Any | None = None,
        prompt_state_manager: Any | None = None,
    ) -> None:
        self.code_index_service = code_index_service
        self.workspace_service = workspace_service
        self.context_budget_manager = context_budget_manager
        self.prompt_state_manager = prompt_state_manager

    def build(
        self,
        *,
        workspace: WorkspaceRecord,
        prompt: str,
        model_profile: str,
        generation_mode: GenerationMode = GenerationMode.BALANCED,
        active_paths: list[str] | None = None,
        target_files: list[str] | None = None,
        grounded_spec: GroundedSpecModel | None = None,
        execution_class: str | None = None,
        run_id: str | None = None,
    ) -> ContextPack:
        code_limit, doc_limit = self._retrieval_limits(generation_mode)
        preferred_paths = self._preferred_anchor_paths(
            grounded_spec=grounded_spec,
            execution_class=execution_class,
            target_files=target_files or [],
        )
        retrieval_active_paths = list(dict.fromkeys([*preferred_paths, *(active_paths or []), *(target_files or [])]))
        retrieval = self.code_index_service.retrieve(
            workspace_id=workspace.workspace_id,
            prompt=prompt,
            code_limit=code_limit,
            doc_limit=doc_limit,
            active_paths=retrieval_active_paths,
            recent_paths=self._recent_paths(workspace),
        )
        targeted_files: dict[str, str] = {}
        file_targets = list(target_files or [])
        if generation_mode == GenerationMode.FAST:
            file_targets = file_targets[:8]
        for file_path in file_targets:
            try:
                content = self.workspace_service.try_read_text_file(workspace.workspace_id, file_path, run_id=run_id)
            except FileNotFoundError:
                continue
            if content is None:
                continue
            targeted_files[file_path] = content
        retrieval_stats = dict(retrieval["stats"])  # type: ignore[arg-type]
        retrieval_stats["anchor_report"] = {
            "execution_class": execution_class or "shell_app",
            "preferred_anchor_paths": preferred_paths,
            "retrieval_active_paths": retrieval_active_paths[:24],
            "selected_code_paths": [chunk["path"] for chunk in retrieval["code"]],  # type: ignore[index]
            "target_file_sample": file_targets[:12],
        }
        stable_prefix = self.stable_prefix(workspace, model_profile)
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
            prompt_cache_key=self.prompt_cache_key(workspace, model_profile, stable_prefix),
            retrieval_stats=retrieval_stats,
        )

    @staticmethod
    def _workspace_summary(workspace: WorkspaceRecord) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        return (
            f"Workspace {workspace.name}. Target platform: {platform}. "
            f"Template cloned: {workspace.template_cloned}. Current revision: {workspace.current_revision_id or 'none'}."
        )

    @staticmethod
    def stable_prefix(workspace: WorkspaceRecord, model_profile: str) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        return (
            "You are editing a grounded mini-app workspace. "
            "Prefer minimal targeted changes, preserve role separation, and keep generated artifacts consistent. "
            f"Model profile: {model_profile}. Target platform: {platform}. "
            "Defer non-essential file reads. Use retrieved chunks before widening context."
        )

    @classmethod
    def prompt_cache_key(cls, workspace: WorkspaceRecord, model_profile: str, stable_prefix: str | None = None) -> str:
        stable_prefix = stable_prefix if stable_prefix is not None else cls.stable_prefix(workspace, model_profile)
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

    @staticmethod
    def _preferred_anchor_paths(
        *,
        grounded_spec: GroundedSpecModel | None,
        execution_class: str | None,
        target_files: list[str],
    ) -> list[str]:
        anchors: list[str] = []
        if execution_class in {"entity_workflow_app", "workflow_dashboard_app", "data_crud_app"}:
            anchors.extend(
                [
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                    "miniapp/app/generated/route_manifest.json",
                    "miniapp/app/generated/runtime_manifest.json",
                    "miniapp/app/generated/static_runtime_manifest.json",
                ]
            )
            anchors.extend(path for path in target_files if path.startswith("miniapp/app/routes/"))
            anchors.extend(
                path
                for path in target_files
                if path.startswith("miniapp/app/static/") and not path.endswith("/index.html")
            )
        else:
            anchors.extend(path for path in target_files if path.endswith("/index.html") or path.endswith("/profile.html"))

        if grounded_spec is not None:
            entity_names = {entity.name.strip().lower() for entity in grounded_spec.domain_entities if entity.name.strip()}
            api_paths = {api.path.strip("/") for api in grounded_spec.api_requirements if api.path.strip("/")}
            if grounded_spec.persistence_requirements or grounded_spec.api_requirements:
                anchors.extend(["miniapp/app/db.py", "miniapp/app/schemas.py"])
            for target in target_files:
                lowered = target.lower()
                if any(name and name in lowered for name in entity_names):
                    anchors.append(target)
                if any(path and path.split("/")[-1] in lowered for path in api_paths):
                    anchors.append(target)
            if execution_class in {"workflow_dashboard_app", "data_crud_app"}:
                for target in target_files:
                    lowered = target.lower()
                    if any(token in lowered for token in ("request", "detail", "list", "workload", "dashboard", "demo", "slot", "comment")):
                        anchors.append(target)
        return list(dict.fromkeys(path for path in anchors if path))
