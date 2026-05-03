from __future__ import annotations

import subprocess
import zipfile
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.models.domain import ExportRecord
from app.repositories.state_store import StateStore
from app.services.workspace.service import WorkspaceService


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

    def export_deploy_bundle(self, workspace_id: str) -> ExportRecord:
        source_dir = self.workspace_service.source_dir(workspace_id)
        export_path = self.settings.exports_dir / f"{workspace_id}-deploy-bundle.zip"
        manifest = self._workspace_manifest(workspace_id, bundle_type="deploy_bundle")
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in source_dir.rglob("*"):
                if file_path.is_file() and ".git" not in file_path.parts:
                    archive.write(file_path, f"source/{file_path.relative_to(source_dir)}")
            archive.writestr("grounded-manifest.json", json.dumps(manifest, indent=2))
            archive.writestr("docker-validation-report.json", json.dumps(self._docker_validation_report(workspace_id), indent=2))
        return self._store_export(workspace_id, "deploy_bundle", export_path)

    def export_docker_validation_report(self, workspace_id: str) -> ExportRecord:
        export_path = self.settings.exports_dir / f"{workspace_id}-docker-validation-report.json"
        export_path.write_text(json.dumps(self._docker_validation_report(workspace_id), indent=2), encoding="utf-8")
        return self._store_export(workspace_id, "docker_validation_report", export_path)

    def export_manifest(self, workspace_id: str) -> ExportRecord:
        export_path = self.settings.exports_dir / f"{workspace_id}-manifest.json"
        export_path.write_text(json.dumps(self._workspace_manifest(workspace_id, bundle_type="manifest"), indent=2), encoding="utf-8")
        return self._store_export(workspace_id, "manifest", export_path)

    def export_browser_proof_bundle(self, workspace_id: str) -> ExportRecord:
        export_path = self.settings.exports_dir / f"{workspace_id}-browser-proof.zip"
        runs = [run for run in self.store.list("runs") if run.get("workspace_id") == workspace_id]
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"workspace_id": workspace_id, "run_count": len(runs)}, indent=2))
            for run in runs:
                run_id = str(run.get("run_id") or "")
                for key in [f"browser_proof:{run_id}", f"run_artifacts:{run_id}"]:
                    payload = self.store.get("reports", key)
                    if payload is not None:
                        archive.writestr(f"reports/{key}.json", json.dumps(payload, indent=2, default=str))
        return self._store_export(workspace_id, "browser_proof_bundle", export_path)

    def get_export(self, export_id: str) -> ExportRecord:
        payload = self.store.get("exports", export_id)
        if not payload:
            raise KeyError(f"Export not found: {export_id}")
        return ExportRecord.model_validate(payload)

    def _store_export(self, workspace_id: str, export_type: str, export_path: Path) -> ExportRecord:
        export = ExportRecord(workspace_id=workspace_id, export_type=export_type, file_path=str(export_path))  # type: ignore[arg-type]
        self.store.upsert("exports", export.export_id, export.model_dump(mode="json"))
        return export

    def _workspace_manifest(self, workspace_id: str, *, bundle_type: str) -> dict[str, Any]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)
        files = [
            str(path.relative_to(source_dir)).replace("\\", "/")
            for path in source_dir.rglob("*")
            if path.is_file() and ".git" not in path.parts
        ]
        return {
            "schema_version": "grounded.export.v1",
            "bundle_type": bundle_type,
            "workspace": workspace.model_dump(mode="json"),
            "file_count": len(files),
            "files": sorted(files),
        }

    def _docker_validation_report(self, workspace_id: str) -> dict[str, Any]:
        source_dir = self.workspace_service.source_dir(workspace_id)
        compose_file = source_dir / "docker" / "docker-compose.yml"
        dockerfile_count = len(list(source_dir.rglob("Dockerfile")))
        return {
            "schema_version": "grounded.docker_report.v1",
            "workspace_id": workspace_id,
            "compose_file_present": compose_file.exists(),
            "dockerfile_count": dockerfile_count,
            "recommended_command": "docker compose -f docker/docker-compose.yml config",
            "status": "ready" if compose_file.exists() else "missing_compose",
        }
