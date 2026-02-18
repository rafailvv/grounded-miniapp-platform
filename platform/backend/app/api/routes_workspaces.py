from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_container
from app.models.domain import CreateWorkspaceRequest, WorkspaceRecord
from app.services.container import ServiceContainer

router = APIRouter(tags=["workspaces"])


@router.post("/workspaces", response_model=WorkspaceRecord)
def create_workspace(
    request: CreateWorkspaceRequest,
    container: ServiceContainer = Depends(get_container),
) -> WorkspaceRecord:
    workspace = WorkspaceRecord(
        name=request.name,
        description=request.description,
        target_platform=request.target_platform,
        preview_profile=request.preview_profile,
        path=str(container.settings.workspaces_dir / "pending"),
    )
    workspace.path = str(container.settings.workspaces_dir / workspace.workspace_id)
    return container.workspace_service.create_workspace(workspace)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRecord)
def get_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        return container.workspace_service.get_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/clone-template", response_model=WorkspaceRecord)
def clone_template(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        return container.workspace_service.clone_template(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/reset", response_model=WorkspaceRecord)
def reset_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        return container.workspace_service.reset_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

