from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_container
from app.models.domain import SaveFileRequest
from app.services.container import ServiceContainer

router = APIRouter(tags=["files"])


@router.get("/workspaces/{workspace_id}/files/tree")
def get_file_tree(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> list[dict[str, str]]:
    try:
        return container.workspace_service.file_tree(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/files/content")
def get_file_content(
    workspace_id: str,
    path: str = Query(...),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, str]:
    try:
        return {"path": path, "content": container.workspace_service.read_file(workspace_id, path)}
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/files/save")
def save_file(
    workspace_id: str,
    request: SaveFileRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, str]:
    try:
        revision = container.workspace_service.save_file(workspace_id, request)
        return {"revision_id": revision.revision_id, "commit_sha": revision.commit_sha}
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/diff")
def get_diff(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    try:
        return {"diff": container.workspace_service.diff(workspace_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

