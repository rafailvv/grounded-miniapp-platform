from __future__ import annotations

from app.models.artifacts import PatchOperationModel
from app.services.workspace_service import WorkspaceService


class PatchService:
    def __init__(self, workspace_service: WorkspaceService) -> None:
        self.workspace_service = workspace_service

    def apply(self, *, workspace_id: str, operations: list[PatchOperationModel]) -> str:
        revision = self.workspace_service.apply_patch_operations(
            workspace_id,
            operations,
            "Apply generated artifact plan",
        )
        return revision.revision_id

