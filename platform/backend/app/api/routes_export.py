from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["export"])


@router.post("/workspaces/{workspace_id}/export/zip")
def export_zip(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_zip(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.post("/workspaces/{workspace_id}/export/git-patch")
def export_git_patch(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_git_patch(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.post("/workspaces/{workspace_id}/export/deploy-bundle")
def export_deploy_bundle(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_deploy_bundle(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.post("/workspaces/{workspace_id}/export/docker-validation-report")
def export_docker_validation_report(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_docker_validation_report(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.post("/workspaces/{workspace_id}/export/manifest")
def export_manifest(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_manifest(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.post("/workspaces/{workspace_id}/export/browser-proof-bundle")
def export_browser_proof_bundle(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str]:
    export = container.export_service.export_browser_proof_bundle(workspace_id)
    return {"export_id": export.export_id, "file_path": export.file_path}


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, container: ServiceContainer = Depends(get_container)) -> FileResponse:
    try:
        export = container.export_service.get_export(export_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(export.file_path, filename=export.file_path.split("/")[-1])
