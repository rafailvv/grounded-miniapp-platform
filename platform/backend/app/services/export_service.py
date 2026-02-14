from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

from app.core.config import Settings
from app.models.domain import ExportRecord
from app.repositories.state_store import StateStore
from app.services.workspace_service import WorkspaceService


class ExportService:
    def __init__(self, settings: Settings, store: StateStore, workspace_service: WorkspaceService) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service

    def export_zip(self, workspace_id: str) -> ExportRecord:
        source_dir = self.workspace_service.source_dir(workspace_id)
        export_path = self.settings.exports_dir / f"{workspace_id}.zip"
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in source_dir.rglob("*"):
                if file_path.is_file() and ".git" not in file_path.parts:
                    archive.write(file_path, file_path.relative_to(source_dir))
        export = ExportRecord(workspace_id=workspace_id, export_type="zip", file_path=str(export_path))
        self.store.upsert("exports", export.export_id, export.model_dump(mode="json"))
        return export

    def export_git_patch(self, workspace_id: str) -> ExportRecord:
        source_dir = self.workspace_service.source_dir(workspace_id)
        export_path = self.settings.exports_dir / f"{workspace_id}.patch"
        revisions = self.workspace_service.get_workspace(workspace_id).revisions
        if len(revisions) < 2:
            export_path.write_text("", encoding="utf-8")
        else:
            result = subprocess.run(
                ["git", "diff", "HEAD~1", "HEAD"],
                cwd=source_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            export_path.write_text(result.stdout, encoding="utf-8")
        export = ExportRecord(workspace_id=workspace_id, export_type="git_patch", file_path=str(export_path))
        self.store.upsert("exports", export.export_id, export.model_dump(mode="json"))
        return export

    def get_export(self, export_id: str) -> ExportRecord:
        payload = self.store.get("exports", export_id)
        if not payload:
            raise KeyError(f"Export not found: {export_id}")
        return ExportRecord.model_validate(payload)

