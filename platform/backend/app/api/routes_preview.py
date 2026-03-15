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
    return {
        "url": preview.url,
        "role_urls": container.preview_service.role_urls(workspace_id),
        "runtime_mode": preview.runtime_mode,
        "status": preview.status,
    }


@router.get("/workspaces/{workspace_id}/preview/logs")
def get_preview_logs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, list[str]]:
    return {"logs": container.preview_service.get(workspace_id).logs}


@router.get("/workspaces/{workspace_id}/logs")
def get_workspace_logs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    job = container.generation_service.latest_job_for_workspace(workspace_id)
    preview = container.preview_service.get(workspace_id)
    validation = container.generation_service.current_report(workspace_id, "validation")
    assumptions = container.generation_service.current_report(workspace_id, "assumptions")
    traceability = container.generation_service.current_report(workspace_id, "traceability")
    artifact_plan = container.generation_service.current_report(workspace_id, "artifact_plan")
    spec = container.generation_service.current_report(workspace_id, "spec")
    ir = container.generation_service.current_report(workspace_id, "ir")
    trace = container.generation_service.current_report(workspace_id, "trace")

    return {
        "workspace_id": workspace_id,
        "job": job.model_dump(mode="json") if job else None,
        "events": [event.model_dump(mode="json") for event in job.events] if job else [],
        "preview": {
            "status": preview.status,
            "runtime_mode": preview.runtime_mode,
            "url": preview.url,
            "logs": preview.logs,
        },
        "reports": {
            "trace": trace,
            "validation": validation,
            "assumptions": assumptions,
            "traceability": traceability,
            "artifact_plan": artifact_plan,
            "spec_summary": {
                "product_goal": spec.get("product_goal") if spec else None,
                "actors": len(spec.get("actors", [])) if spec else 0,
                "flows": len(spec.get("user_flows", [])) if spec else 0,
                "api_requirements": len(spec.get("api_requirements", [])) if spec else 0,
            },
            "ir_summary": {
                "screens": len(ir.get("screens", [])) if ir else 0,
                "route_groups": len(ir.get("route_groups", [])) if ir else 0,
                "integrations": len(ir.get("integrations", [])) if ir else 0,
                "actions": sum(len(screen.get("actions", [])) for screen in ir.get("screens", [])) if ir else 0,
            },
        },
    }


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
