from __future__ import annotations

import threading

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


@router.get("/workspaces", response_model=list[WorkspaceRecord])
def list_workspaces(container: ServiceContainer = Depends(get_container)) -> list[WorkspaceRecord]:
    return container.workspace_service.list_workspaces()


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRecord)
def get_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        return container.workspace_service.get_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/clone-template", response_model=WorkspaceRecord)
def clone_template(
    workspace_id: str,
    container: ServiceContainer = Depends(get_container),
) -> WorkspaceRecord:
    try:
        workspace = container.workspace_service.clone_template(workspace_id)
        threading.Thread(
            target=container.code_index_service.index_workspace,
            args=(workspace, container.workspace_service.source_dir(workspace_id)),
            daemon=True,
        ).start()
        threading.Thread(
            target=container.preview_service.ensure_started,
            args=(workspace_id,),
            daemon=True,
        ).start()
        return workspace
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/reset", response_model=WorkspaceRecord)
def reset_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        workspace = container.workspace_service.reset_workspace(workspace_id)
        threading.Thread(
            target=container.code_index_service.index_workspace,
            args=(workspace, container.workspace_service.source_dir(workspace_id)),
            daemon=True,
        ).start()
        threading.Thread(
            target=container.preview_service.ensure_started,
            args=(workspace_id,),
            daemon=True,
        ).start()
        return workspace
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/rollback", response_model=WorkspaceRecord)
def rollback_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> WorkspaceRecord:
    try:
        workspace = container.workspace_service.rollback_last_revision(workspace_id)
        container.preview_service.rebuild(workspace_id)
        return workspace
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/index")
def index_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    try:
        workspace = container.workspace_service.get_workspace(workspace_id)
        status = container.code_index_service.index_workspace(workspace, container.workspace_service.source_dir(workspace_id))
        documents = container.document_service.list_documents(workspace_id)
        if documents:
            container.code_index_service.index_documents(workspace_id, documents)
        return status.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/index/status")
def index_status(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        return {
            "workspace": container.code_index_service.get_workspace_status(workspace_id).model_dump(mode="json"),
            "documents": container.code_index_service.get_document_status(workspace_id).model_dump(mode="json"),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    try:
        try:
            container.preview_service.reset(workspace_id)
        except Exception:
            pass
        container.workspace_service.delete_workspace(workspace_id)
        return {"deleted": workspace_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
