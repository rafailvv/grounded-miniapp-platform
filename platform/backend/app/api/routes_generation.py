from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_container
from app.models.domain import CreateRunRequest, GenerateRequest, JobRecord
from app.services.container import ServiceContainer

router = APIRouter(tags=["generation"])


@router.post("/workspaces/{workspace_id}/generate", response_model=JobRecord)
def generate(
    workspace_id: str,
    request: GenerateRequest,
    container: ServiceContainer = Depends(get_container),
) -> JobRecord:
    try:
        run = container.run_service.create_run_sync(
            workspace_id,
            CreateRunRequest(
                prompt=request.prompt,
                mode=request.mode,
                intent=request.intent,
                apply_strategy="staged_auto_apply",
                target_role_scope=request.target_role_scope,
                model_profile=request.model_profile,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                generation_mode=request.generation_mode,
                resume_from_run_id=request.resume_from_run_id,
                error_context=request.error_context,
            ),
        )
        if not run.linked_job_id:
            raise HTTPException(status_code=500, detail="Run did not produce a linked job.")
        return container.generation_service.get_job(run.linked_job_id)
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
        job = container.generation_service.get_job(job_id)
        request = GenerateRequest(
            prompt=job.prompt,
            mode=job.mode,
            target_platform=job.target_platform,
            preview_profile=job.preview_profile,
            generation_mode=job.generation_mode,
            intent="edit" if job.mode == "fix" else "auto",
            model_profile=job.model_profile or "openai_code_fast",
            linked_run_id=job.linked_run_id,
            error_context=job.error_context,
        )
        if job.mode == "fix":
            return container.fix_orchestrator.generate(job.workspace_id, request)
        return container.generation_service.retry(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
