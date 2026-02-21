from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["validation"])


@router.get("/workspaces/{workspace_id}/spec/current")
def get_current_spec(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.generation_service.current_report(workspace_id, "spec")


@router.get("/workspaces/{workspace_id}/ir/current")
def get_current_ir(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.generation_service.current_report(workspace_id, "ir")


@router.get("/workspaces/{workspace_id}/validation/current")
def get_current_validation(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.generation_service.current_report(workspace_id, "validation")


@router.get("/workspaces/{workspace_id}/assumptions/current")
def get_current_assumptions(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.generation_service.current_report(workspace_id, "assumptions")


@router.get("/workspaces/{workspace_id}/traceability/current")
def get_current_traceability(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.generation_service.current_report(workspace_id, "traceability")


@router.post("/workspaces/{workspace_id}/validation/run")
def rerun_validation(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    spec = container.generation_service.current_report(workspace_id, "spec")
    ir = container.generation_service.current_report(workspace_id, "ir")
    return {
        "workspace_id": workspace_id,
        "has_spec": spec is not None,
        "has_ir": ir is not None,
        "validation": container.generation_service.current_report(workspace_id, "validation"),
        "assumptions": container.generation_service.current_report(workspace_id, "assumptions"),
        "traceability": container.generation_service.current_report(workspace_id, "traceability"),
    }
