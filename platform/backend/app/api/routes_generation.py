from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_container
from app.models.domain import GenerateRequest, JobRecord
from app.services.container import ServiceContainer

router = APIRouter(tags=["generation"])


@router.post("/workspaces/{workspace_id}/generate", response_model=JobRecord)
def generate(
    workspace_id: str,
    request: GenerateRequest,
    container: ServiceContainer = Depends(get_container),
) -> JobRecord:
    try:
        return container.generation_service.generate(workspace_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str, container: ServiceContainer = Depends(get_container)) -> JobRecord:
    try:
        return container.generation_service.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, container: ServiceContainer = Depends(get_container)):
    try:
        job = container.generation_service.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    def event_stream():
        for event in job.events:
            yield f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/retry", response_model=JobRecord)
def retry_job(job_id: str, container: ServiceContainer = Depends(get_container)) -> JobRecord:
    try:
        return container.generation_service.retry(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

