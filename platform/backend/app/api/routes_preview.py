from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["preview"])


@router.post("/workspaces/{workspace_id}/preview/start")
def start_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.ensure_started(workspace_id).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/ensure")
def ensure_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.ensure_started(workspace_id).model_dump(mode="json")


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
        "draft_run_id": preview.draft_run_id,
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
    spec = container.generation_service.current_report(workspace_id, "spec")
    iterations = container.generation_service.current_report(workspace_id, "iterations")
    candidate_diff = container.generation_service.current_report(workspace_id, "candidate_diff")
    check_results = container.generation_service.current_report(workspace_id, "check_results")
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
            "draft_run_id": preview.draft_run_id,
        },
        "reports": {
            "trace": trace,
            "validation": validation,
            "assumptions": assumptions,
            "traceability": traceability,
            "iterations": iterations,
            "candidate_diff": candidate_diff,
            "check_results": check_results,
            "spec_summary": {
                "product_goal": spec.get("product_goal") if spec else None,
                "actors": len(spec.get("actors", [])) if spec else 0,
                "flows": len(spec.get("user_flows", [])) if spec else 0,
                "api_requirements": len(spec.get("api_requirements", [])) if spec else 0,
            },
        },
    }


@router.get("/preview/{workspace_id}", response_class=HTMLResponse)
def render_preview(
    workspace_id: str,
    role: str = "client",
    run_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> HTMLResponse:
    try:
        source_dir = (
            container.workspace_service.draft_source_dir(workspace_id, run_id)
            if run_id and container.workspace_service.draft_exists(workspace_id, run_id)
            else container.workspace_service.source_dir(workspace_id)
        )
        html = container.preview_service.render_html(workspace_id, source_dir, role)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(html)
