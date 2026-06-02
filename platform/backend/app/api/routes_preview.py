from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.deps import get_container
from app.api.routes_public_apps import _public_app_url_for_request, _role_urls_for_app
from app.models.sandbox import SandboxPreviewLifecycle
from app.services.container import ServiceContainer

router = APIRouter(tags=["preview"])


def _flatten_service_logs(logs_by_service: dict[str, list[str]]) -> list[str]:
    return [
        line
        for service, lines in logs_by_service.items()
        for line in ([f"=== {service} ==="] + lines + [""])
    ]


def _local_preview_logs(preview_logs: list[str], api_log: list[str]) -> dict[str, list[str]]:
    lines = [line for line in api_log[-160:] if str(line).strip()]
    if lines:
        return {"local-preview": lines}
    runtime_lines = [
        line
        for line in preview_logs[-80:]
        if str(line).strip() and ("uvicorn" in line or "health" in line or "runtime" in line or "Preview" in line)
    ]
    return {"local-preview": runtime_lines} if runtime_lines else {}


@router.post("/workspaces/{workspace_id}/preview/start")
def start_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.ensure_started(workspace_id).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/ensure")
def ensure_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.ensure_started(workspace_id).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/rebuild")
def rebuild_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.rebuild_async(workspace_id, force=True).model_dump(mode="json")


@router.post("/workspaces/{workspace_id}/preview/reset")
def reset_preview(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    return container.preview_service.reset(workspace_id).model_dump(mode="json")


@router.get("/workspaces/{workspace_id}/preview/url")
def get_preview_url(
    workspace_id: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    preview = container.preview_service.get(workspace_id)
    app_url = _public_app_url_for_request(request, workspace_id, container.preview_service.public_app_url(workspace_id, preview))
    return {
        "url": app_url or preview.url,
        "runtime_url": preview.url,
        "role_urls": _role_urls_for_app(app_url) if app_url else container.preview_service.public_role_urls(workspace_id, preview),
        "runtime_mode": preview.runtime_mode,
        "status": preview.status,
        "stage": preview.stage,
        "progress_percent": preview.progress_percent,
        "draft_run_id": preview.draft_run_id,
        "latency_breakdown": preview.latency_breakdown,
        "last_error": preview.last_error,
        "runtime_boundary": container.preview_service.runtime_boundary(workspace_id),
    }


@router.get("/workspaces/{workspace_id}/preview/runtime-boundary", response_model=SandboxPreviewLifecycle)
def get_preview_runtime_boundary(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> SandboxPreviewLifecycle:
    return SandboxPreviewLifecycle.model_validate(container.preview_service.runtime_boundary(workspace_id))


@router.get("/workspaces/{workspace_id}/preview/logs")
def get_preview_logs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    preview = container.preview_service.peek(workspace_id)
    container_logs: dict[str, list[str]] = {}
    if preview.runtime_mode == "docker" and preview.proxy_port is not None:
        try:
            source_dir = container.workspace_service.source_dir(workspace_id)
            container_logs = container.runtime_manager.collect_container_logs(workspace_id, source_dir, preview.proxy_port)
        except Exception as exc:
            container_logs = {"preview_runtime": [f"Unable to collect preview container logs: {exc}"]}
    elif preview.runtime_mode == "local":
        api_log = container.workspace_log_service.read_lines(workspace_id, kind="api")
        container_logs = _local_preview_logs(preview.logs, api_log)
    return {"logs": preview.logs, "mini_app_logs": container_logs}


@router.get("/workspaces/{workspace_id}/logs")
def get_workspace_logs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    job = container.workspace_code_agent_runtime.latest_job_for_workspace(workspace_id)
    preview = container.preview_service.peek(workspace_id)
    platform_log = container.workspace_log_service.read_lines(workspace_id, kind="platform")
    api_log = container.workspace_log_service.read_lines(workspace_id, kind="api")
    container_logs: dict[str, list[str]] = {}
    if preview.runtime_mode == "docker" and preview.proxy_port is not None:
        try:
            source_dir = container.workspace_service.source_dir(workspace_id)
            container_logs = container.runtime_manager.collect_container_logs(workspace_id, source_dir, preview.proxy_port)
        except Exception as exc:
            container_logs = {"preview_runtime": [f"Unable to collect preview container logs: {exc}"]}
    elif preview.runtime_mode == "local":
        container_logs = _local_preview_logs(preview.logs, api_log)
    workspace_event_lines = [
        (
            f"- [{event.created_at.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{event.event_type}: {event.message}"
            + (f" | {event.details}" if event.details else "")
        )
        for event in (job.events if job else [])
    ]
    workspace_logs = [
        *workspace_event_lines,
        *(["", "=== platform.log ===", *platform_log] if platform_log else []),
        *(["", "=== api.log ===", *api_log] if api_log else []),
    ]
    mini_app_logs = _flatten_service_logs(container_logs)
    validation = container.workspace_code_agent_runtime.current_report(workspace_id, "validation")
    iterations = container.workspace_code_agent_runtime.current_report(workspace_id, "iterations")
    candidate_diff = container.workspace_code_agent_runtime.current_report(workspace_id, "candidate_diff")
    check_results = container.workspace_code_agent_runtime.current_report(workspace_id, "check_results")
    trace = container.workspace_code_agent_runtime.current_report(workspace_id, "trace")
    fix_case = container.workspace_code_agent_runtime.current_report(workspace_id, "fix_case")
    fix_runtime = container.workspace_code_agent_runtime.current_report(workspace_id, "fix_runtime")

    return {
        "workspace_id": workspace_id,
        "job": job.model_dump(mode="json") if job else None,
        "events": [event.model_dump(mode="json") for event in job.events] if job else [],
        "workspace_logs": workspace_logs,
        "preview": {
            "status": preview.status,
            "stage": preview.stage,
            "progress_percent": preview.progress_percent,
            "runtime_mode": preview.runtime_mode,
            "url": preview.url,
            "logs": preview.logs,
            "draft_run_id": preview.draft_run_id,
            "latency_breakdown": preview.latency_breakdown,
            "last_error": preview.last_error,
            "mini_app_logs": mini_app_logs,
        },
        "reports": {
            "trace": trace,
            "validation": validation,
            "iterations": iterations,
            "candidate_diff": candidate_diff,
            "check_results": check_results,
            "fix_case": fix_case,
            "fix_runtime": fix_runtime,
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
        del run_id
        source_dir = container.workspace_service.source_dir(workspace_id)
        html = container.preview_service.render_html(workspace_id, source_dir, role)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(html)
