from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["validation"])


@router.get("/workspaces/{workspace_id}/validation/current")
def get_current_validation(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict | None:
    return container.workspace_code_agent_runtime.current_report(workspace_id, "validation")


@router.post("/workspaces/{workspace_id}/validation/run")
def rerun_validation(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "validation": container.workspace_code_agent_runtime.current_report(workspace_id, "validation"),
        "checks": container.workspace_code_agent_runtime.current_report(workspace_id, "check_results"),
        "trace": container.workspace_code_agent_runtime.current_report(workspace_id, "trace"),
    }
