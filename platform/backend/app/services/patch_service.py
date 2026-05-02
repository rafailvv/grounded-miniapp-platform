from __future__ import annotations

from app.models.artifacts import ApplyPatchResult, PatchEnvelope, PatchOperationModel
from app.services.workspace.service import WorkspaceService


class PatchService:
    def __init__(self, workspace_service: WorkspaceService) -> None:
        self.workspace_service = workspace_service

    def apply(self, *, workspace_id: str, patch_actions: list[PatchOperationModel], base_revision_id: str | None = None) -> ApplyPatchResult:
        result = self.workspace_service.apply_patch_envelope(
            workspace_id,
            PatchEnvelope(
                workspace_id=workspace_id,
                base_revision_id=base_revision_id,
                summary="Apply generated artifact plan",
                ops=patch_actions,
            ),
            message="Apply generated artifact plan",
        )
        return result
