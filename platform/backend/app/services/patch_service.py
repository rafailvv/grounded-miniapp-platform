from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Any

from app.models.artifacts import ApplyPatchResult, PatchEnvelope, PatchOperationModel
from app.services.workspace.service import WorkspaceService


class PatchService:
    def __init__(self, workspace_service: WorkspaceService) -> None:
        self.workspace_service = workspace_service

    def preflight(
        self,
        *,
        workspace_id: str,
        patch_actions: list[PatchOperationModel],
        run_id: str | None = None,
        base_revision_id: str | None = None,
    ) -> dict[str, Any]:
        workspace = self.workspace_service.get_workspace(workspace_id)
        target_root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        conflicts: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        sandbox_paths: list[str] = []
        if base_revision_id and workspace.current_revision_id and base_revision_id != workspace.current_revision_id:
            conflicts.append(
                {
                    "code": "stale_base_revision",
                    "message": "Patch base revision is stale.",
                    "base_revision_id": base_revision_id,
                    "current_revision_id": workspace.current_revision_id,
                }
            )
        for operation in patch_actions:
            normalized = str(operation.file_path or "").strip().replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            path = Path(normalized)
            file_report: dict[str, Any] = {
                "operation_id": operation.operation_id,
                "path": normalized,
                "op": operation.op,
                "exists": False,
                "current_hash": None,
                "reverse_diff": "",
                "line_anchor_count": 0,
            }
            if not normalized or path.is_absolute() or ".." in path.parts:
                conflicts.append({"code": "path_escape", "message": "Patch path must stay within workspace.", "path": normalized})
                files.append(file_report)
                continue
            if self.workspace_service._is_ignored_workspace_path(path):
                conflicts.append({"code": "ignored_path", "message": "Patch targets an ignored/generated path.", "path": normalized})
                files.append(file_report)
                continue
            target = target_root / path
            current = ""
            if target.exists() and target.is_file():
                file_report["exists"] = True
                try:
                    current = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    conflicts.append({"code": "binary_file", "message": "Patch target is not UTF-8 text.", "path": normalized})
                    files.append(file_report)
                    continue
                file_report["current_hash"] = hashlib.sha256(current.encode("utf-8")).hexdigest()
            precondition_hash = (operation.precondition or {}).get("file_hash") if operation.precondition else None
            if precondition_hash is not None and precondition_hash != file_report["current_hash"]:
                conflicts.append({"code": "precondition_hash_mismatch", "message": "File hash precondition does not match.", "path": normalized})
            if operation.op in {"create", "update"} and operation.content is not None:
                file_report["reverse_diff"] = "".join(
                    difflib.unified_diff(
                        str(operation.content or "").splitlines(keepends=True),
                        current.splitlines(keepends=True),
                        fromfile=f"b/{normalized}",
                        tofile=f"a/{normalized}",
                    )
                )
            if operation.diff:
                file_report["line_anchor_count"] = sum(1 for line in str(operation.diff).splitlines() if line.startswith("@@"))
            files.append(file_report)
            sandbox_paths.append(normalized)
        sandbox_report = self.workspace_service.sandbox_service.preflight_apply(
            target_root,
            sandbox_paths,
            profile="agent_draft_write" if run_id else "source_apply_gate",
            operation="apply",
            allow_generated=run_id is None,
        ).model_dump(mode="json")
        if sandbox_report.get("status") == "blocked":
            for violation in sandbox_report.get("violations") or []:
                if isinstance(violation, dict):
                    conflicts.append({"code": violation.get("code"), "message": violation.get("message"), "path": violation.get("path")})
        return {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "base_revision_id": base_revision_id,
            "status": "passed" if not conflicts else "conflict",
            "conflicts": conflicts,
            "files": files,
            "sandbox_report": sandbox_report,
            "deterministic": True,
            "validation_before_apply": True,
        }

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
