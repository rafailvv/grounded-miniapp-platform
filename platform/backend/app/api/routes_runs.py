from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_container
from app.models.domain import CreateRunRequest, RunRecord
from app.services.container import ServiceContainer

router = APIRouter(tags=["runs"])


@router.post("/workspaces/{workspace_id}/runs", response_model=RunRecord)
def create_run(
    workspace_id: str,
    request: CreateRunRequest,
    container: ServiceContainer = Depends(get_container),
) -> RunRecord:
    try:
        return container.run_service.create_run(workspace_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/runs", response_model=list[RunRecord])
def list_runs(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> list[RunRecord]:
    return container.run_service.list_runs(workspace_id)


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.run_service.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/artifacts")
def get_run_artifacts(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict:
    try:
        return container.run_service.get_run_artifacts(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/iterations")
def get_run_iterations(run_id: str, container: ServiceContainer = Depends(get_container)) -> list[dict]:
    try:
        return container.run_service.get_run_iterations(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/apply", response_model=RunRecord)
@router.post("/runs/{run_id}/approve", response_model=RunRecord)
def apply_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.run_service.apply_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/discard", response_model=RunRecord)
def discard_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.run_service.discard_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/stop", response_model=RunRecord)
def stop_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.run_service.stop_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/rollback", response_model=RunRecord)
def rollback_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.run_service.rollback_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
