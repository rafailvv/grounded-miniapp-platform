from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from subprocess import CalledProcessError

from app.core.config import Settings
from app.models.artifacts import ApplyPatchResult, PatchEnvelope, PatchOperationModel
from app.models.domain import DraftFileOperation, RevisionRecord, SaveFileRequest, WorkspaceRecord, utc_now
from app.repositories.state_store import StateStore
from app.services.workspace.log_service import WorkspaceLogService


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
    IGNORED_TREE_NAMES = {".DS_Store", "vite.config.js", "vite.config.d.ts"}

    def __init__(self, settings: Settings, store: StateStore, workspace_log_service: WorkspaceLogService) -> None:
        self.settings = settings
        self.store = store
        self.workspace_log_service = workspace_log_service
        self.code_index_service = None

    def attach_code_index_service(self, code_index_service) -> None:
        self.code_index_service = code_index_service

    def create_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        workspace_dir = self.settings.workspaces_dir / workspace.workspace_id
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self.store.upsert("workspaces", workspace.workspace_id, workspace.model_dump(mode="json"))
        self.workspace_log_service.append(
            workspace.workspace_id,
            source="workspace",
            message="Workspace created.",
            payload={"name": workspace.name, "target_platform": str(workspace.target_platform)},
        )
        return workspace

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        payload = self.store.get("workspaces", workspace_id)
        if not payload:
            raise KeyError(f"Workspace not found: {workspace_id}")
        return WorkspaceRecord.model_validate(payload)

    def rename_workspace(self, workspace_id: str, name: str) -> WorkspaceRecord:
        workspace = self.get_workspace(workspace_id)
        normalized_name = " ".join(str(name or "").split()).strip()
        if not normalized_name or workspace.name == normalized_name:
            return workspace
        workspace.name = normalized_name
        workspace.updated_at = utc_now()
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        self.workspace_log_service.append(
            workspace_id,
            source="workspace",
            message="Workspace renamed.",
            payload={"name": normalized_name},
        )
        return workspace

    def list_workspaces(self) -> list[WorkspaceRecord]:
        workspaces = [WorkspaceRecord.model_validate(item) for item in self.store.list("workspaces")]
        workspaces.sort(key=lambda workspace: workspace.updated_at, reverse=True)
        return workspaces

    def delete_workspace(self, workspace_id: str) -> None:
        self.get_workspace(workspace_id)

        workspace_root = self.workspace_root(workspace_id)
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
            if workspace_root.exists():
                raise RuntimeError(f"Workspace directory still exists after deletion: {workspace_root}")

        run_ids = {
            key
            for key, payload in self.store.items("runs")
            if payload.get("workspace_id") == workspace_id
        }
        job_ids = {
            key
            for key, payload in self.store.items("jobs")
            if payload.get("workspace_id") == workspace_id
        }

        self.store.delete_many("workspaces", [workspace_id])
        self.store.delete_many("previews", [workspace_id])

        for collection in ["documents", "chat_turns", "jobs", "runs"]:
            keys_to_delete = [
                key
                for key, payload in self.store.items(collection)
                if payload.get("workspace_id") == workspace_id
            ]
            self.store.delete_many(collection, keys_to_delete)

        export_keys_to_delete: list[str] = []
        for key, payload in self.store.items("exports"):
            if payload.get("workspace_id") != workspace_id:
                continue
            file_path = payload.get("file_path")
            if isinstance(file_path, str) and file_path:
                try:
                    Path(file_path).unlink(missing_ok=True)
                except OSError:
                    pass
            export_keys_to_delete.append(key)
        self.store.delete_many("exports", export_keys_to_delete)

        report_keys_to_delete = [
            key
            for key, payload in self.store.items("reports")
            if key.endswith(f":{workspace_id}")
            or payload.get("workspace_id") == workspace_id
            or payload.get("run_id") in run_ids
            or payload.get("job_id") in job_ids
        ]
        self.store.delete_many("reports", report_keys_to_delete)

        code_index_keys = [f"workspace:{workspace_id}", f"docs:{workspace_id}"]
        self.store.delete_many("code_indexes", code_index_keys)

        code_chunk_keys = [
            key
            for key, _ in self.store.items("code_chunks")
            if key.startswith(f"code:{workspace_id}:") or key.startswith(f"doc:{workspace_id}:")
        ]
        self.store.delete_many("code_chunks", code_chunk_keys)

        patch_apply_keys = [
            key
            for key, payload in self.store.items("patch_applies")
            if payload.get("workspace_id") == workspace_id or payload.get("run_id") in run_ids
        ]
        self.store.delete_many("patch_applies", patch_apply_keys)

    def prune_orphaned_state(self) -> dict[str, int]:
        workspace_ids = {key for key, _ in self.store.items("workspaces")}
        valid_job_ids = {
            key
            for key, payload in self.store.items("jobs")
            if payload.get("workspace_id") in workspace_ids
        }
        valid_run_ids = {
            key
            for key, payload in self.store.items("runs")
            if payload.get("workspace_id") in workspace_ids
        }
        deleted: dict[str, int] = {}

        def _delete(collection: str, keys: list[str]) -> None:
            if not keys:
                return
            self.store.delete_many(collection, keys)
            deleted[collection] = deleted.get(collection, 0) + len(keys)

        workspace_collections = ["documents", "jobs", "runs", "exports"]
        for collection in workspace_collections:
            keys = [
                key
                for key, payload in self.store.items(collection)
                if payload.get("workspace_id") not in workspace_ids
            ]
            _delete(collection, keys)

        preview_keys = [
            key
            for key, payload in self.store.items("previews")
            if key not in workspace_ids and payload.get("workspace_id") not in workspace_ids
        ]
        _delete("previews", preview_keys)

        report_keys = [
            key
            for key, payload in self.store.items("reports")
            if (
                (payload.get("workspace_id") and payload.get("workspace_id") not in workspace_ids)
                or (payload.get("run_id") and payload.get("run_id") not in valid_run_ids)
                or (payload.get("job_id") and payload.get("job_id") not in valid_job_ids)
                or any(
                    key.endswith(f":{workspace_id}")
                    for workspace_id in {
                        candidate
                        for candidate in [key.rsplit(":", 1)[-1]]
                        if candidate.startswith("ws_") and candidate not in workspace_ids
                    }
                )
            )
        ]
        _delete("reports", report_keys)

        code_index_keys = [
            key
            for key, _ in self.store.items("code_indexes")
            if (
                (key.startswith("workspace:") and key.split(":", 1)[1] not in workspace_ids)
                or (key.startswith("docs:") and key.split(":", 1)[1] not in workspace_ids)
            )
        ]
        _delete("code_indexes", code_index_keys)

        code_chunk_keys = []
        for key, _ in self.store.items("code_chunks"):
            parts = key.split(":", 2)
            if len(parts) < 3:
                continue
            _, chunk_workspace_id, _ = parts
            if chunk_workspace_id not in workspace_ids:
                code_chunk_keys.append(key)
        _delete("code_chunks", code_chunk_keys)

        patch_apply_keys = [
            key
            for key, payload in self.store.items("patch_applies")
            if (
                (payload.get("workspace_id") and payload.get("workspace_id") not in workspace_ids)
                or (payload.get("run_id") and payload.get("run_id") not in valid_run_ids)
            )
        ]
        _delete("patch_applies", patch_apply_keys)

        return deleted

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
        self.workspace_log_service.append(workspace_id, source="workspace", message="Canonical template cloned.")
        return workspace

    def reset_workspace(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self.clone_template(workspace_id)
        latest = workspace.revisions[-1]
        latest.source = "reset"
        latest.message = "Reset workspace to canonical template"
        self.store.upsert("workspaces", workspace_id, workspace.model_dump(mode="json"))
        self.workspace_log_service.append(workspace_id, source="workspace", message="Workspace reset to canonical template.")
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
        self.workspace_log_service.append(workspace_id, source="workspace", message="Workspace rolled back to previous revision.")
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
        self.workspace_log_service.append(
            workspace_id,
            source="workspace",
            message="Workspace revision reverted.",
            payload={"revision_id": revision_id},
        )
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

    def ensure_draft(self, workspace_id: str, run_id: str) -> Path:
        draft_source = self.draft_source_dir(workspace_id, run_id)
        if draft_source.exists():
            return draft_source
        return self.prepare_draft(workspace_id, run_id)

    def clone_draft(self, workspace_id: str, source_run_id: str, target_run_id: str) -> Path:
        source_draft = self.draft_source_dir(workspace_id, source_run_id)
        if not source_draft.exists():
            raise KeyError(f"Draft not found for run: {source_run_id}")
        target_draft = self.draft_source_dir(workspace_id, target_run_id)
        if target_draft.exists():
            shutil.rmtree(target_draft)
        target_draft.parent.mkdir(parents=True, exist_ok=True)
        self._copy_tree(source_draft, target_draft)
        self.workspace_log_service.append(
            workspace_id,
            source="workspace",
            message="Draft cloned for run resume.",
            payload={"source_run_id": source_run_id, "target_run_id": target_run_id},
        )
        return target_draft

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
        self.workspace_log_service.append(
            workspace_id,
            source="workspace",
            message="Draft approved and applied to source.",
            payload={"run_id": run_id, "revision_id": revision.revision_id},
        )
        return revision

    def discard_draft(self, workspace_id: str, run_id: str) -> None:
        draft_root = self.draft_root(workspace_id, run_id)
        if draft_root.exists():
            shutil.rmtree(draft_root, ignore_errors=True)
            self.workspace_log_service.append(
                workspace_id,
                source="workspace",
                message="Draft discarded.",
                payload={"run_id": run_id},
            )

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
        self.workspace_log_service.append(
            workspace_id,
            source="workspace",
            message="Source file saved.",
            payload={"relative_path": request.relative_path},
        )
        return revision

    def build_patch_envelope_for_draft(self, workspace_id: str, run_id: str, operations: list[DraftFileOperation]) -> PatchEnvelope:
        workspace = self.get_workspace(workspace_id)
        draft_source = self.draft_source_dir(workspace_id, run_id)
        prepared_ops: list[PatchOperationModel] = []
        for operation in operations:
            target_path = draft_source / self._safe_relative_path(operation.file_path)
            current_content = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""
            file_hash = self._file_hash(current_content) if target_path.exists() and target_path.is_file() else None
            if operation.operation == "patch":
                diff = self._ensure_unified_diff_paths(str(operation.diff or operation.content or ""), operation.file_path)
                op_name = "patch"
                content = None
            else:
                diff = self._unified_diff(current_content, operation.content or "", operation.file_path)
                op_name = "delete" if operation.operation == "delete" else ("create" if not target_path.exists() else "update")
                content = operation.content
            prepared_ops.append(
                PatchOperationModel(
                    operation_id=operation.operation_id,
                    op=op_name,
                    file_path=operation.file_path,
                    content=content,
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

    def try_read_text_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str | None:
        try:
            file_path = self._target_dir(workspace_id, run_id) / self._safe_relative_path(relative_path)
            return file_path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return None

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
        try:
            return self._git_output(source_dir, ["diff", "HEAD~1", "HEAD"])
        except CalledProcessError:
            return ""

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

        backups: dict[str, tuple[str, str | None]] = {}

        def backup_path(relative_path: str, target_path: Path) -> None:
            if relative_path in backups:
                return
            if target_path.exists() and target_path.is_file():
                backups[relative_path] = ("file", target_path.read_text(encoding="utf-8"))
            elif target_path.exists():
                backups[relative_path] = ("other", None)
            else:
                backups[relative_path] = ("missing", None)

        def rollback() -> None:
            for relative_path, (kind, content) in reversed(list(backups.items())):
                target_path = target_root / self._safe_relative_path(relative_path)
                if kind == "file":
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(content or "", encoding="utf-8")
                    continue
                if kind == "missing":
                    if target_path.exists():
                        if target_path.is_dir():
                            shutil.rmtree(target_path)
                        else:
                            target_path.unlink()

        def failed(status: str, reason: str) -> ApplyPatchResult:
            rollback()
            return ApplyPatchResult(
                workspace_id=workspace_id,
                run_id=run_id,
                base_revision_id=envelope.base_revision_id,
                status=status,
                conflict_reason=reason,
                changed_files=[],
            )

        for operation in envelope.ops:
            target_path = target_root / self._safe_relative_path(operation.file_path)
            existing_content = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""
            precondition_hash = (operation.precondition or {}).get("file_hash") if operation.precondition else None
            if precondition_hash is not None and self._file_hash(existing_content) != precondition_hash:
                return failed("conflict", f"Precondition hash mismatch for {operation.file_path}.")
            if operation.op == "delete":
                backup_path(operation.file_path, target_path)
                if target_path.exists():
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                    else:
                        target_path.unlink()
                    changed_files.append(operation.file_path)
                continue
            if operation.op == "patch":
                patch_diff = str(operation.diff or "")
                if not patch_diff.strip():
                    return failed("failed", f"Patch operation {operation.operation_id} is missing a unified diff.")
                backup_path(operation.file_path, target_path)
                if patch_diff.lstrip().startswith("*** Begin Patch"):
                    codex_result = self._apply_codex_update_patch(
                        existing_content,
                        patch_diff,
                        expected_path=operation.file_path,
                    )
                    if codex_result is None:
                        return failed("conflict", f"Patch operation {operation.operation_id} could not be applied as a Codex update patch.")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(codex_result, encoding="utf-8")
                    if operation.file_path not in changed_files:
                        changed_files.append(operation.file_path)
                    continue
                if self._is_line_free_hunk_patch(patch_diff):
                    hunk_result = self._apply_line_free_hunks(existing_content, patch_diff)
                    if hunk_result is None:
                        return failed("conflict", f"Patch operation {operation.operation_id} could not be applied as a line-free hunk patch.")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(hunk_result, encoding="utf-8")
                    if operation.file_path not in changed_files:
                        changed_files.append(operation.file_path)
                    continue
                patch_paths = self._paths_from_unified_diff(patch_diff)
                if not patch_paths:
                    return failed("failed", f"Patch operation {operation.operation_id} did not contain file paths.")
                expected_path = operation.file_path.strip().replace("\\", "/")
                if any(path != expected_path for path in patch_paths):
                    return failed("failed", f"Patch operation {operation.operation_id} touched paths outside {operation.file_path}.")
                for path in patch_paths:
                    self._safe_relative_path(path)
                    backup_path(path, target_root / self._safe_relative_path(path))
                check_result = subprocess.run(
                    ["git", "apply", "--check", "--whitespace=nowarn", "--"],
                    cwd=target_root,
                    input=patch_diff,
                    capture_output=True,
                    text=True,
                )
                if check_result.returncode != 0:
                    return failed("conflict", check_result.stderr.strip() or check_result.stdout.strip() or f"Patch operation {operation.operation_id} could not be applied.")
                apply_result = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", "--"],
                    cwd=target_root,
                    input=patch_diff,
                    capture_output=True,
                    text=True,
                )
                if apply_result.returncode != 0:
                    return failed("failed", apply_result.stderr.strip() or apply_result.stdout.strip() or f"Patch operation {operation.operation_id} failed during apply.")
                changed_files.extend(path for path in patch_paths if path not in changed_files)
                continue
            if operation.content is None:
                return failed("failed", f"Patch operation {operation.operation_id} is missing content.")
            backup_path(operation.file_path, target_path)
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
        if not (source_dir / ".git").exists():
            self._git_init(source_dir)
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
        output = self._normalize_draft_diff_paths(
            result.stdout,
            source_dir=source_dir,
            draft_source=draft_source,
        )
        return self._filter_ignored_draft_diff_blocks(output)

    @classmethod
    def _normalize_draft_diff_paths(cls, output: str, *, source_dir: Path, draft_source: Path) -> str:
        source = str(source_dir)
        draft = str(draft_source)
        replacements = (
            (f"a{source}", "a/source"),
            (f"b{source}", "b/source"),
            (f"a{draft}", "a/draft"),
            (f"b{draft}", "b/draft"),
            (source, "source"),
            (draft, "draft"),
        )
        normalized = output
        for previous, current in replacements:
            normalized = normalized.replace(previous, current)
        return normalized

    @classmethod
    def _filter_ignored_draft_diff_blocks(cls, output: str) -> str:
        if not output.strip():
            return output
        blocks: list[str] = []
        current: list[str] = []
        for line in output.splitlines(keepends=True):
            if line.startswith("diff --git ") and current:
                blocks.append("".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("".join(current))

        filtered: list[str] = []
        for block in blocks:
            if not block.startswith("diff --git "):
                filtered.append(block)
                continue
            paths = cls._paths_from_draft_diff_header(block.splitlines()[0])
            if paths and all(cls._is_ignored_workspace_path(Path(path)) for path in paths):
                continue
            filtered.append(block)
        return "".join(filtered)

    @staticmethod
    def _paths_from_draft_diff_header(header: str) -> list[str]:
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", header.strip())
        if not match:
            return []
        paths: list[str] = []
        for candidate in match.groups():
            normalized = candidate.strip().strip('"')
            if normalized in {"/dev/null", "dev/null"}:
                continue
            if normalized.startswith("source/"):
                normalized = normalized.split("source/", 1)[-1]
            elif normalized.startswith("draft/"):
                normalized = normalized.split("draft/", 1)[-1]
            if normalized:
                paths.append(normalized)
        return list(dict.fromkeys(paths))

    @classmethod
    def _paths_from_unified_diff(cls, diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff_text or "").splitlines():
            if line.startswith("diff --git "):
                paths.extend(cls._paths_from_draft_diff_header(line))
                continue
            if line.startswith("--- ") or line.startswith("+++ "):
                candidate = line[4:].strip().split("\t", 1)[0].strip().strip('"')
                if candidate in {"/dev/null", "dev/null"}:
                    continue
                if candidate.startswith("a/") or candidate.startswith("b/"):
                    candidate = candidate[2:]
                if candidate.startswith("source/") or candidate.startswith("draft/"):
                    candidate = candidate.split("/", 1)[1]
                if candidate:
                    paths.append(candidate)
        return list(dict.fromkeys(path for path in paths if path))

    @classmethod
    def _ensure_unified_diff_paths(cls, diff_text: str, relative_path: str) -> str:
        text = str(diff_text or "")
        if text.lstrip().startswith("*** Begin Patch"):
            return text
        if cls._paths_from_unified_diff(text):
            return text
        if cls._is_line_free_hunk_patch(text):
            return text
        if not re.search(r"^@@\s", text, flags=re.MULTILINE):
            return text
        normalized_path = str(relative_path or "").strip().replace("\\", "/").lstrip("./")
        if not normalized_path:
            return text
        body = text if text.endswith("\n") else f"{text}\n"
        return f"--- a/{normalized_path}\n+++ b/{normalized_path}\n{body}"

    @staticmethod
    def _is_line_free_hunk_patch(diff_text: str) -> bool:
        for line in str(diff_text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("--- ", "+++ ", "diff --git ")):
                continue
            if stripped.startswith("@@"):
                return re.match(r"^@@\s+-\d", stripped) is None
            return False
        return False

    @classmethod
    def _apply_codex_update_patch(cls, existing_content: str, patch_text: str, *, expected_path: str) -> str | None:
        lines = str(patch_text or "").splitlines()
        expected = str(expected_path or "").strip().replace("\\", "/").lstrip("./")
        active = False
        hunks: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            if line.startswith("*** Update File: "):
                path = line.split(":", 1)[1].strip().replace("\\", "/").lstrip("./")
                active = path == expected
                continue
            if line.startswith("*** End Patch") or line.startswith("*** Update File: "):
                break
            if not active:
                continue
            if line.startswith("@@"):
                if current:
                    hunks.append(current)
                current = []
                continue
            if line.startswith("***"):
                continue
            if current is not None:
                current.append(line)
        if current:
            hunks.append(current)
        if not hunks:
            return None

        updated = existing_content
        for hunk in hunks:
            next_content = cls._apply_line_free_hunk(updated, hunk)
            if next_content is None:
                return None
            updated = next_content
        return updated

    @classmethod
    def _apply_line_free_hunks(cls, existing_content: str, diff_text: str) -> str | None:
        hunks: list[list[str]] = []
        current: list[str] = []
        for line in str(diff_text or "").splitlines():
            if line.startswith(("--- ", "+++ ", "diff --git ")):
                continue
            if line.startswith("***"):
                continue
            if line.startswith("@@"):
                if current:
                    hunks.append(current)
                current = []
                continue
            current.append(line)
        if current:
            hunks.append(current)
        if not hunks:
            return None
        updated = existing_content
        for hunk in hunks:
            next_content = cls._apply_line_free_hunk(updated, hunk)
            if next_content is None:
                return None
            updated = next_content
        return updated

    @staticmethod
    def _apply_line_free_hunk(existing_content: str, hunk: list[str]) -> str | None:
        if WorkspaceService._route_addition_has_indented_after_context(hunk):
            return None
        old_lines: list[str] = []
        new_lines: list[str] = []
        for line in hunk:
            if not line:
                old_lines.append("")
                new_lines.append("")
                continue
            prefix = line[0]
            body = line[1:] if prefix in {" ", "+", "-"} else line
            if prefix in {" ", "-"}:
                old_lines.append(body)
            if prefix in {" ", "+"}:
                new_lines.append(body)
        old_text = "\n".join(old_lines)
        new_text = "\n".join(new_lines)
        if not old_text and new_text:
            addition = new_text if new_text.endswith("\n") else f"{new_text}\n"
            return f"{existing_content}{addition}"
        candidates = [old_text]
        if old_text and not old_text.endswith("\n"):
            candidates.insert(0, f"{old_text}\n")
        for candidate in candidates:
            if candidate and candidate in existing_content:
                replacement = f"{new_text}\n" if candidate.endswith("\n") and not new_text.endswith("\n") else new_text
                return existing_content.replace(candidate, replacement, 1)
        addition_fallback = WorkspaceService._apply_line_free_addition_fallback(existing_content, hunk)
        if addition_fallback is not None:
            return addition_fallback
        return None

    @classmethod
    def _route_addition_has_indented_after_context(cls, hunk: list[str]) -> bool:
        entries = cls._line_free_entries(hunk)
        for index, (prefix, body) in enumerate(entries):
            if prefix != "+" or not body.startswith("@router."):
                continue
            cursor = index
            while cursor < len(entries) and entries[cursor][0] == "+":
                cursor += 1
            while cursor < len(entries):
                next_prefix, next_body = entries[cursor]
                if next_prefix == " " and next_body.strip():
                    return next_body.startswith((" ", "\t"))
                cursor += 1
        return False

    @staticmethod
    def _line_free_entries(hunk: list[str]) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for line in hunk:
            if not line:
                entries.append((" ", ""))
                continue
            prefix = line[0] if line[0] in {" ", "+", "-"} else " "
            body = line[1:] if prefix in {" ", "+", "-"} else line
            entries.append((prefix, body))
        return entries

    @classmethod
    def _apply_line_free_addition_fallback(cls, existing_content: str, hunk: list[str]) -> str | None:
        entries = cls._line_free_entries(hunk)
        if not entries or any(prefix == "-" for prefix, _body in entries):
            return None

        updated = existing_content
        index = 0
        while index < len(entries):
            prefix, _body = entries[index]
            if prefix != "+":
                index += 1
                continue
            block_start = index
            while index < len(entries) and entries[index][0] == "+":
                index += 1
            block_end = index
            addition_lines = [body for _prefix, body in entries[block_start:block_end]]
            if not any(line.strip() for line in addition_lines):
                continue
            addition_text = cls._lines_to_text(addition_lines)
            if addition_text.strip() and addition_text in updated:
                continue

            before_context = [body for prefix, body in entries[:block_start] if prefix == " "]
            after_context = [body for prefix, body in entries[block_end:] if prefix == " "]
            route_decorator_addition = any(line.startswith("@router.") for line in addition_lines)
            inserted = cls._insert_addition_near_context(
                updated,
                addition_text,
                before_context=before_context,
                after_context=after_context,
                require_router_after_anchor=route_decorator_addition,
            )
            if inserted is None:
                return None
            updated = inserted
        return updated if updated != existing_content else None

    @classmethod
    def _insert_addition_near_context(
        cls,
        existing_content: str,
        addition_text: str,
        *,
        before_context: list[str],
        after_context: list[str],
        require_router_after_anchor: bool = False,
    ) -> str | None:
        if not addition_text:
            return None
        after_snippets = cls._context_snippets(after_context, from_end=False)
        if require_router_after_anchor:
            after_snippets = [snippet for snippet in after_snippets if cls._starts_with_router_decorator(snippet)]
        for snippet in after_snippets:
            position = existing_content.find(snippet)
            if position >= 0:
                return f"{existing_content[:position]}{addition_text}{existing_content[position:]}"
        if require_router_after_anchor:
            return None
        for snippet in cls._context_snippets(before_context, from_end=True):
            position = existing_content.rfind(snippet)
            if position >= 0:
                insertion_at = position + len(snippet)
                return f"{existing_content[:insertion_at]}{addition_text}{existing_content[insertion_at:]}"
        return None

    @staticmethod
    def _starts_with_router_decorator(snippet: str) -> bool:
        for line in str(snippet or "").splitlines():
            if not line.strip():
                continue
            return line.startswith("@router.")
        return False

    @classmethod
    def _context_snippets(cls, lines: list[str], *, from_end: bool) -> list[str]:
        meaningful_indexes = [idx for idx, line in enumerate(lines) if line.strip()]
        if not meaningful_indexes:
            return []
        if from_end:
            end = meaningful_indexes[-1] + 1
            starts = range(max(0, end - 8), end)
            candidates = [lines[start:end] for start in starts]
        else:
            start = meaningful_indexes[0]
            ends = range(min(len(lines), start + 8), start, -1)
            candidates = [lines[start:end] for end in ends]
        snippets = [cls._lines_to_text(candidate) for candidate in candidates if any(line.strip() for line in candidate)]
        return [snippet for snippet in snippets if snippet.strip()]

    @staticmethod
    def _lines_to_text(lines: list[str]) -> str:
        text = "\n".join(lines)
        return text if text.endswith("\n") else f"{text}\n"

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
                ".DS_Store",
                "vite.config.js",
                "vite.config.d.ts",
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
                        ".DS_Store",
                        "vite.config.js",
                        "vite.config.d.ts",
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
        if relative_path.name in cls.IGNORED_TREE_NAMES:
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
