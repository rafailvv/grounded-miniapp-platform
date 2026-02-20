from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["preview"])


@router.post("/workspaces/{workspace_id}/preview/start")
def start_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.start(workspace_id).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/rebuild")
def rebuild_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.rebuild(workspace_id).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/reset")
def reset_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.reset(workspace_id).model_dump(mode="json")


@router.get("/workspaces/{workspace_id}/preview/url")
def get_preview_url(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    preview = container.preview_service.get(workspace_id)
    return {"url": preview.url, "role_urls": container.preview_service.role_urls(workspace_id)}


@router.get("/workspaces/{workspace_id}/preview/logs")
def get_preview_logs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, list[str]]:
    return {"logs": container.preview_service.get(workspace_id).logs}


@router.get("/preview/{workspace_id}", response_class=HTMLResponse)
def render_preview(
    workspace_id: str,
    role: str = "client",
    container: ServiceContainer = Depends(get_container),
) -> HTMLResponse:
    try:
        html = container.preview_service.render_html(workspace_id, container.workspace_service.source_dir(workspace_id), role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(html)
