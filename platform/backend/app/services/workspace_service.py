from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from app.core.config import Settings
from app.models.artifacts import ApplyPatchResult, PatchEnvelope, PatchOperationModel
from app.models.domain import DraftFileOperation, RevisionRecord, SaveFileRequest, WorkspaceRecord
from app.repositories.state_store import StateStore


class WorkspaceService:
    IGNORED_TREE_PARTS = {
        ".git",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".vite",
        ".cache",
    }
    IGNORED_TREE_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")

    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store
        self.code_index_service = None

    def attach_code_index_service(self, code_index_service) -> None:
        self.code_index_service = code_index_service

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
        self._refresh_indexes_async(workspace)
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
        self._refresh_indexes_async(workspace)
        return revision

    def apply_patch_operations(self, workspace_id: str, operations: list[PatchOperationModel], message: str) -> RevisionRecord:
        workspace = self.get_workspace(workspace_id)
        envelope = PatchEnvelope(
            workspace_id=workspace_id,
            base_revision_id=workspace.current_revision_id,
            summary=message,
            risk_level="medium",
            ops=operations,
        )
        result = self.apply_patch_envelope(workspace_id, envelope, message=message)
        if result.status != "applied" or not result.revision_id:
            raise ValueError(result.conflict_reason or "Patch operations could not be applied.")
        workspace = self.get_workspace(workspace_id)
        revision = next(rev for rev in workspace.revisions if rev.revision_id == result.revision_id)
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
        envelope = self.build_patch_envelope_for_draft(workspace_id, run_id, operations)
        result = self.apply_patch_envelope_to_draft(workspace_id, run_id, envelope)
        if result.status != "applied":
            raise ValueError(result.conflict_reason or "Draft patch could not be applied.")
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
        self._refresh_indexes_async(workspace)
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
        self._refresh_indexes_async(workspace)
        return revision

    def build_patch_envelope_for_draft(self, workspace_id: str, run_id: str, operations: list[DraftFileOperation]) -> PatchEnvelope:
        workspace = self.get_workspace(workspace_id)
        draft_source = self.draft_source_dir(workspace_id, run_id)
        prepared_ops: list[PatchOperationModel] = []
        for operation in operations:
            target_path = draft_source / self._safe_relative_path(operation.file_path)
            current_content = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""
            file_hash = self._file_hash(current_content) if target_path.exists() and target_path.is_file() else None
            diff = self._unified_diff(current_content, operation.content or "", operation.file_path)
            prepared_ops.append(
                PatchOperationModel(
                    operation_id=operation.operation_id,
                    op="delete" if operation.operation == "delete" else ("create" if not target_path.exists() else "update"),
                    file_path=operation.file_path,
                    content=operation.content,
                    diff=diff,
                    explanation=operation.reason,
                    trace_refs=[],
                    precondition={"file_hash": file_hash, "max_fuzz": 0},
                )
            )
        return PatchEnvelope(
            workspace_id=workspace_id,
            base_revision_id=workspace.current_revision_id,
            summary=f"Draft patch for run {run_id}",
            risk_level="medium",
            ops=prepared_ops,
            post_actions={"run": ["validators", "preview_smoke"]},
            ui={"title": "Draft patch", "summary": f"{len(prepared_ops)} file operations"},
        )

    def apply_patch_envelope(self, workspace_id: str, envelope: PatchEnvelope, *, message: str) -> ApplyPatchResult:
        workspace = self.get_workspace(workspace_id)
        source_dir = self.source_dir(workspace_id)
        if envelope.base_revision_id and workspace.current_revision_id and envelope.base_revision_id != workspace.current_revision_id:
            return ApplyPatchResult(
                workspace_id=workspace_id,
                base_revision_id=envelope.base_revision_id,
                status="conflict",
                conflict_reason="Patch base revision is stale.",
            )
        result = self._apply_envelope_to_target(source_dir, workspace_id, None, envelope)
        if result.status != "applied":
            return result
        commit_sha = self._git_commit(source_dir, message)
        revision = RevisionRecord(commit_sha=commit_sha, message=message, source="ai_patch")
        workspace.current_revision_id = revision.revision_id
        workspace.revisions.append(revision)
        workspace.updated_at = revision.created_at
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        self._refresh_indexes(workspace)
        return result.model_copy(update={"revision_id": revision.revision_id})

    def apply_patch_envelope_to_draft(self, workspace_id: str, run_id: str, envelope: PatchEnvelope) -> ApplyPatchResult:
        draft_source = self.draft_source_dir(workspace_id, run_id)
        if not draft_source.exists():
            self.prepare_draft(workspace_id, run_id)
        return self._apply_envelope_to_target(draft_source, workspace_id, run_id, envelope)

    def read_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str:
        file_path = self._target_dir(workspace_id, run_id) / self._safe_relative_path(relative_path)
        return file_path.read_text(encoding="utf-8")

    def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:
        source_dir = self._target_dir(workspace_id, run_id)
        tree: list[dict[str, str]] = []
        for path in sorted(source_dir.rglob("*")):
            relative_path = path.relative_to(source_dir)
            if self._is_ignored_workspace_path(relative_path):
                continue
            tree.append(
                {
                    "path": str(relative_path),
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

    def _apply_envelope_to_target(
        self,
        target_root: Path,
        workspace_id: str,
        run_id: str | None,
        envelope: PatchEnvelope,
    ) -> ApplyPatchResult:
        changed_files: list[str] = []
        for operation in envelope.ops:
            target_path = target_root / self._safe_relative_path(operation.file_path)
            existing_content = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""
            precondition_hash = (operation.precondition or {}).get("file_hash") if operation.precondition else None
            if precondition_hash is not None and self._file_hash(existing_content) != precondition_hash:
                return ApplyPatchResult(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    base_revision_id=envelope.base_revision_id,
                    status="conflict",
                    conflict_reason=f"Precondition hash mismatch for {operation.file_path}.",
                    changed_files=changed_files,
                )
            if operation.op == "delete":
                if target_path.exists():
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                    else:
                        target_path.unlink()
                    changed_files.append(operation.file_path)
                continue
            if operation.content is None:
                return ApplyPatchResult(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    base_revision_id=envelope.base_revision_id,
                    status="failed",
                    conflict_reason=f"Patch operation {operation.operation_id} is missing content.",
                    changed_files=changed_files,
                )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(operation.content, encoding="utf-8")
            changed_files.append(operation.file_path)
        result = ApplyPatchResult(
            workspace_id=workspace_id,
            run_id=run_id,
            base_revision_id=envelope.base_revision_id,
            status="applied",
            changed_files=changed_files,
        )
        self.store.upsert(
            "patch_applies",
            result.apply_id,
            result.model_dump(mode="json"),
        )
        return result

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
        shutil.copytree(
            source_dir,
            destination_dir,
            ignore=shutil.ignore_patterns(
                ".git",
                "node_modules",
                "dist",
                "build",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".next",
                ".vite",
                ".cache",
                "*.pyc",
                "*.pyo",
                "*.tsbuildinfo",
            ),
            symlinks=True,
        )

    @classmethod
    def _replace_workspace_contents_from_draft(cls, source_dir: Path, draft_source_dir: Path) -> None:
        for child in source_dir.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in draft_source_dir.iterdir():
            if cls._is_ignored_workspace_path(Path(child.name)):
                continue
            destination = source_dir / child.name
            if child.is_symlink():
                destination.symlink_to(child.readlink(), target_is_directory=child.is_dir())
            elif child.is_dir():
                shutil.copytree(
                    child,
                    destination,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(
                        "node_modules",
                        "dist",
                        "build",
                        "__pycache__",
                        ".pytest_cache",
                        ".mypy_cache",
                        ".ruff_cache",
                        ".next",
                        ".vite",
                        ".cache",
                        "*.pyc",
                        "*.pyo",
                        "*.tsbuildinfo",
                    ),
                )
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

    def _refresh_indexes(self, workspace: WorkspaceRecord) -> None:
        if self.code_index_service is None or not workspace.template_cloned:
            return
        self.code_index_service.index_workspace(workspace, self.source_dir(workspace.workspace_id))

    def _refresh_indexes_async(self, workspace: WorkspaceRecord) -> None:
        thread = threading.Thread(target=self._refresh_indexes, args=(workspace,), daemon=True)
        thread.start()

    @classmethod
    def _is_ignored_workspace_path(cls, relative_path: Path) -> bool:
        if any(part in cls.IGNORED_TREE_PARTS for part in relative_path.parts):
            return True
        return relative_path.name.endswith(cls.IGNORED_TREE_SUFFIXES)

    @staticmethod
    def _file_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _unified_diff(previous: str, current: str, relative_path: str) -> str:
        return "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
            )
        )


def json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
