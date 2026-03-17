from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.models.artifacts import PatchOperationModel
from app.models.domain import RevisionRecord, SaveFileRequest, WorkspaceRecord
from app.repositories.state_store import StateStore


class WorkspaceService:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store

    def create_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        workspace_dir = self.settings.workspaces_dir / workspace.workspace_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert("workspaces", workspace.workspace_id, workspace.model_dump(mode="json"))
        return workspace

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        payload = self.store.get("workspaces", workspace_id)
        if not payload:
            raise KeyError(f"Workspace not found: {workspace_id}")
        return WorkspaceRecord.model_validate(payload)

    def list_workspaces(self) -> list[WorkspaceRecord]:
        workspaces = [WorkspaceRecord.model_validate(item) for item in self.store.list("workspaces")]
        workspaces.sort(key=lambda workspace: workspace.updated_at, reverse=True)
        return workspaces

    def delete_workspace(self, workspace_id: str) -> None:
        self.get_workspace(workspace_id)

        workspace_root = self.workspace_root(workspace_id)
        if workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)

        self.store.delete("workspaces", workspace_id)
        self.store.delete("previews", workspace_id)

        for collection in ["documents", "chat_turns", "jobs", "runs", "exports"]:
            for key, payload in self.store.items(collection):
                if payload.get("workspace_id") != workspace_id:
                    continue
                if collection == "exports":
                    file_path = payload.get("file_path")
                    if isinstance(file_path, str) and file_path:
                        try:
                            Path(file_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                self.store.delete(collection, key)

        for report_key, _ in self.store.items("reports"):
            if report_key.endswith(f":{workspace_id}"):
                self.store.delete("reports", report_key)

    def clone_template(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self.get_workspace(workspace_id)
        workspace_root = self.workspace_root(workspace_id)
        source_dir = workspace_root / "source"
        if source_dir.exists():
            shutil.rmtree(source_dir)
        shutil.copytree(self.settings.template_dir, source_dir)
        self._git_init(source_dir)
        commit_sha = self._git_commit(source_dir, "Clone canonical template")
        revision = RevisionRecord(commit_sha=commit_sha, message="Clone canonical template", source="template_clone")
        workspace.template_cloned = True
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return workspace

    def reset_workspace(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self.clone_template(workspace_id)
        latest = workspace.revisions[-1]
        latest.source = "reset"
        latest.message = "Reset workspace to canonical template"
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return workspace

    def apply_patch_operations(self, workspace_id: str, operations: list[PatchOperationModel], message: str) -> RevisionRecord:
        source_dir = self.source_dir(workspace_id)
        for operation in operations:
            target_path = source_dir / operation.file_path
            if operation.op == "delete":
                if target_path.exists():
                    target_path.unlink()
                continue
            if operation.content is None:
                raise ValueError(f"Patch operation {operation.operation_id} is missing content.")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(operation.content, encoding="utf-8")
        commit_sha = self._git_commit(source_dir, message)
        revision = RevisionRecord(commit_sha=commit_sha, message=message, source="ai_patch")
        workspace = self.get_workspace(workspace_id)
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return revision

    def save_file(self, workspace_id: str, request: SaveFileRequest) -> RevisionRecord:
        source_dir = self.source_dir(workspace_id)
        relative_path = self._safe_relative_path(request.relative_path)
        file_path = source_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(request.content, encoding="utf-8")
        commit_sha = self._git_commit(source_dir, f"Manual edit: {relative_path}")
        revision = RevisionRecord(commit_sha=commit_sha, message=f"Manual edit: {relative_path}", source="manual_edit")
        workspace = self.get_workspace(workspace_id)
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return revision

    def read_file(self, workspace_id: str, relative_path: str) -> str:
        file_path = self.source_dir(workspace_id) / self._safe_relative_path(relative_path)
        return file_path.read_text(encoding="utf-8")

    def file_tree(self, workspace_id: str) -> list[dict[str, str]]:
        source_dir = self.source_dir(workspace_id)
        tree: list[dict[str, str]] = []
        for path in sorted(source_dir.rglob("*")):
            if ".git" in path.parts:
                continue
            tree.append(
                {
                    "path": str(path.relative_to(source_dir)),
                    "type": "directory" if path.is_dir() else "file",
                }
            )
        return tree

    def diff(self, workspace_id: str) -> str:
        source_dir = self.source_dir(workspace_id)
        revisions = self.get_workspace(workspace_id).revisions
        if len(revisions) < 2:
            return ""
        return self._git_output(source_dir, ["diff", "HEAD~1", "HEAD"])

    def workspace_root(self, workspace_id: str) -> Path:
        return self.settings.workspaces_dir / workspace_id

    def source_dir(self, workspace_id: str) -> Path:
        source_dir = self.workspace_root(workspace_id) / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        return source_dir

    def _safe_relative_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("File paths must stay within the workspace.")
        return candidate

    def _git_init(self, source_dir: Path) -> None:
        if (source_dir / ".git").exists():
            shutil.rmtree(source_dir / ".git")
        subprocess.run(["git", "init"], cwd=source_dir, check=True, capture_output=True, text=True)

    def _git_commit(self, source_dir: Path, message: str) -> str:
        subprocess.run(["git", "add", "."], cwd=source_dir, check=True, capture_output=True, text=True)
        status = self._git_output(source_dir, ["status", "--short"])
        if not status.strip():
            return self._git_output(source_dir, ["rev-parse", "HEAD"]).strip()
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Grounded MiniApp Platform",
                "-c",
                "user.email=grounded@example.local",
                "commit",
                "-m",
                message,
            ],
            cwd=source_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return self._git_output(source_dir, ["rev-parse", "HEAD"]).strip()

    def _git_output(self, source_dir: Path, args: list[str]) -> str:
        result = subprocess.run(["git", *args], cwd=source_dir, check=True, capture_output=True, text=True)
        return result.stdout


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
