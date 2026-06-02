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
            archive.writestr("grounded-pr-babysitter.json", json.dumps(self._pr_babysitter_manifest(workspace_id), indent=2))
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
        manifest: dict[str, Any] = {"schema": "grounded.browser_proof_bundle.v1", "workspace_id": workspace_id, "run_count": len(runs), "reports": [], "screenshots": []}
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for run in runs:
                run_id = str(run.get("run_id") or "")
                candidate_keys = [
                    f"browser_proof:{run_id}",
                    f"browser_proof:{workspace_id}:{run_id}",
                    f"browser_replay_proof:{workspace_id}:{run_id}",
                    f"final_report:{run_id}",
                    f"gate:{run_id}",
                    f"run_artifacts:{run_id}",
                ]
                for key in candidate_keys:
                    payload = self.store.get("reports", key)
                    if payload is not None:
                        archive.writestr(f"reports/{key}.json", json.dumps(payload, indent=2, default=str))
                        manifest["reports"].append({"run_id": run_id, "key": key})
                        for screenshot_path in self._browser_proof_screenshot_paths(payload):
                            path = Path(screenshot_path)
                            if not path.is_file():
                                continue
                            archive_name = f"screenshots/{run_id}/{len(manifest['screenshots']) + 1:03d}-{path.name}"
                            archive.write(path, archive_name)
                            manifest["screenshots"].append({"run_id": run_id, "source": str(path), "archive_path": archive_name})
                        if key.startswith("browser_replay_proof:"):
                            for scenario in payload.get("scenarios") or []:
                                if not isinstance(scenario, dict):
                                    continue
                                scenario_id = str(scenario.get("scenario_id") or "scenario")
                                archive.writestr(f"replay/{run_id}/{scenario_id}.json", json.dumps(scenario, indent=2, default=str))
                                if scenario.get("playwright_spec"):
                                    archive.writestr(f"playwright/{run_id}/{scenario_id}.spec.ts", str(scenario.get("playwright_spec") or ""))
                                for index, snapshot in enumerate(scenario.get("dom_snapshot_refs") or [], start=1):
                                    archive.writestr(f"dom/{run_id}/{scenario_id}-{index:03d}.json", json.dumps(snapshot, indent=2, default=str))
                                if scenario.get("console_logs"):
                                    archive.writestr(f"logs/{run_id}/{scenario_id}-console.log", "\n".join(str(item) for item in scenario.get("console_logs") or []))
                                if scenario.get("network_logs"):
                                    archive.writestr(f"logs/{run_id}/{scenario_id}-network.log", "\n".join(str(item) for item in scenario.get("network_logs") or []))
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
        return self._store_export(workspace_id, "browser_proof_bundle", export_path)

    @classmethod
    def _browser_proof_screenshot_paths(cls, payload: Any) -> list[str]:
        paths: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    lowered = str(key or "").lower()
                    if lowered in {"screenshot", "screenshot_path", "image_path"} and isinstance(nested, str):
                        paths.append(nested)
                    elif lowered in {"screenshots", "images"} and isinstance(nested, list):
                        for item in nested:
                            if isinstance(item, str):
                                paths.append(item)
                            elif isinstance(item, dict):
                                visit(item)
                    elif lowered in {"proof", "browser_proof", "browser_flow_proof", "diagnostics", "browser_proof_steps", "role_workflows"}:
                        visit(nested)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)

        visit(payload)
        return list(dict.fromkeys(paths))

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
            "pr_babysitter": self._pr_babysitter_manifest(workspace_id),
        }

    def _pr_babysitter_manifest(self, workspace_id: str) -> dict[str, Any]:
        runs = [run for run in self.store.list("runs") if run.get("workspace_id") == workspace_id]
        latest_run = sorted(runs, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[:1]
        return {
            "schema": "grounded.pr_babysitter_export.v1",
            "workspace_id": workspace_id,
            "source": "exported_app",
            "run_id": latest_run[0].get("run_id") if latest_run else None,
            "watch_endpoint": f"/workspaces/{workspace_id}/pr-babysitter/watch",
            "snapshot_endpoint": f"/workspaces/{workspace_id}/pr-babysitter/snapshot",
            "required_input": {"pr": "auto | PR number | PR URL", "repo": "optional OWNER/REPO", "max_polls": 60, "poll_seconds": 60},
            "policy": {
                "poll_until": ["merged_or_closed", "user_help_required"],
                "process_review_before_ci_retry": True,
                "max_flaky_retries": 3,
                "green_mergeable_is_progress_not_terminal": True,
                "auto_fix_push_sequence": [
                    "diagnose logs/review",
                    "create focused repair run",
                    "export updated bundle",
                    "commit and push PR branch",
                    "resume watcher",
                ],
            },
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
