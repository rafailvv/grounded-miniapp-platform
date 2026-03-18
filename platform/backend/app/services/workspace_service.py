from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.config import Settings
from app.models.artifacts import PatchOperationModel
from app.models.domain import DraftFileOperation, RevisionRecord, SaveFileRequest, WorkspaceRecord
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
        self._copy_tree(self.settings.template_dir, source_dir)
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

    def rollback_last_revision(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self.get_workspace(workspace_id)
        if len(workspace.revisions) < 2:
            raise ValueError("No previous revision is available for rollback.")

        previous_revision = workspace.revisions[-2]
        source_dir = self.source_dir(workspace_id)
        self._restore_tree_from_commit(source_dir, previous_revision.commit_sha)
        commit_sha = self._git_commit(source_dir, f"Rollback workspace to {previous_revision.revision_id}")
        revision = RevisionRecord(commit_sha=commit_sha, message=f"Rollback to {previous_revision.revision_id}", source="reset")
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return workspace

    def revert_revision(self, workspace_id: str, revision_id: str, message: str) -> RevisionRecord:
        workspace = self.get_workspace(workspace_id)
        target_revision = next((revision for revision in workspace.revisions if revision.revision_id == revision_id), None)
        if target_revision is None:
            raise KeyError(f"Revision not found: {revision_id}")
        if workspace.current_revision_id != revision_id:
            raise ValueError("Only the latest applied revision can be rolled back safely.")

        source_dir = self.source_dir(workspace_id)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Grounded MiniApp Platform",
                "-c",
                "user.email=grounded@example.local",
                "revert",
                "--no-edit",
                target_revision.commit_sha,
            ],
            cwd=source_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_sha = self._git_output(source_dir, ["rev-parse", "HEAD"]).strip()
        revision = RevisionRecord(commit_sha=commit_sha, message=message, source="rollback")
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return revision

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

    def prepare_draft(self, workspace_id: str, run_id: str) -> Path:
        draft_source = self.draft_source_dir(workspace_id, run_id)
        if draft_source.exists():
            shutil.rmtree(draft_source)
        draft_source.parent.mkdir(parents=True, exist_ok=True)
        self._copy_tree(self.source_dir(workspace_id), draft_source)
        return draft_source

    def apply_draft_operations(self, workspace_id: str, run_id: str, operations: list[DraftFileOperation]) -> Path:
        draft_source = self.draft_source_dir(workspace_id, run_id)
        if not draft_source.exists():
            self.prepare_draft(workspace_id, run_id)
        for operation in operations:
            target_path = draft_source / self._safe_relative_path(operation.file_path)
            if operation.operation == "delete":
                if target_path.exists():
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                    else:
                        target_path.unlink()
                continue
            if operation.content is None:
                raise ValueError(f"Draft operation {operation.operation_id} is missing content.")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(operation.content, encoding="utf-8")
        return draft_source

    def approve_draft(self, workspace_id: str, run_id: str, message: str) -> RevisionRecord:
        source_dir = self.source_dir(workspace_id)
        draft_source = self.draft_source_dir(workspace_id, run_id)
        if not draft_source.exists():
            raise KeyError(f"Draft not found for run: {run_id}")
        self._replace_workspace_contents_from_draft(source_dir, draft_source)
        commit_sha = self._git_commit(source_dir, message)
        revision = RevisionRecord(commit_sha=commit_sha, message=message, source="ai_patch")
        workspace = self.get_workspace(workspace_id)
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return revision

    def discard_draft(self, workspace_id: str, run_id: str) -> None:
        draft_root = self.draft_root(workspace_id, run_id)
        if draft_root.exists():
            shutil.rmtree(draft_root, ignore_errors=True)

    def save_file(self, workspace_id: str, request: SaveFileRequest) -> RevisionRecord | None:
        source_dir = self.source_dir(workspace_id) if not request.run_id else self.draft_source_dir(workspace_id, request.run_id)
        relative_path = self._safe_relative_path(request.relative_path)
        file_path = source_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(request.content, encoding="utf-8")
        if request.run_id:
            return None
        commit_sha = self._git_commit(source_dir, f"Manual edit: {relative_path}")
        revision = RevisionRecord(commit_sha=commit_sha, message=f"Manual edit: {relative_path}", source="manual_edit")
        workspace = self.get_workspace(workspace_id)
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        return revision

    def read_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str:
        file_path = self._target_dir(workspace_id, run_id) / self._safe_relative_path(relative_path)
        return file_path.read_text(encoding="utf-8")

    def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:
        source_dir = self._target_dir(workspace_id, run_id)
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

    def diff(self, workspace_id: str, run_id: str | None = None) -> str:
        if run_id:
            return self._diff_against_draft(workspace_id, run_id)
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

    def draft_root(self, workspace_id: str, run_id: str) -> Path:
        return self.workspace_root(workspace_id) / "drafts" / run_id

    def draft_source_dir(self, workspace_id: str, run_id: str) -> Path:
        return self.draft_root(workspace_id, run_id) / "source"

    def draft_exists(self, workspace_id: str, run_id: str) -> bool:
        return self.draft_source_dir(workspace_id, run_id).exists()

    def _target_dir(self, workspace_id: str, run_id: str | None) -> Path:
        if run_id:
            draft_dir = self.draft_source_dir(workspace_id, run_id)
            if not draft_dir.exists():
                raise FileNotFoundError(f"Draft not found for run: {run_id}")
            return draft_dir
        return self.source_dir(workspace_id)

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

    def _diff_against_draft(self, workspace_id: str, run_id: str) -> str:
        source_dir = self.source_dir(workspace_id)
        draft_source = self.draft_source_dir(workspace_id, run_id)
        result = subprocess.run(
            ["git", "diff", "--no-index", "--", str(source_dir), str(draft_source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Unable to diff draft.")
        output = result.stdout
        output = output.replace(str(source_dir), "source")
        output = output.replace(str(draft_source), "draft")
        return output

    @staticmethod
    def _copy_tree(source_dir: Path, destination_dir: Path) -> None:
        shutil.copytree(source_dir, destination_dir, ignore=shutil.ignore_patterns(".git"), symlinks=True)

    @staticmethod
    def _replace_workspace_contents_from_draft(source_dir: Path, draft_source_dir: Path) -> None:
        for child in source_dir.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in draft_source_dir.iterdir():
            destination = source_dir / child.name
            if child.is_symlink():
                destination.symlink_to(child.readlink(), target_is_directory=child.is_dir())
            elif child.is_dir():
                shutil.copytree(child, destination, symlinks=True)
            else:
                shutil.copy2(child, destination)

    def _restore_tree_from_commit(self, source_dir: Path, commit_sha: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            archive = subprocess.run(
                ["git", "archive", commit_sha],
                cwd=source_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["tar", "-x", "-f", "-", "-C", str(temp_path)],
                input=archive.stdout,
                check=True,
                capture_output=True,
            )
            self._replace_workspace_contents_from_draft(source_dir, temp_path)


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
