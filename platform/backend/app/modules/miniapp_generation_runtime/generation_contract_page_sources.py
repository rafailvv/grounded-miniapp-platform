from __future__ import annotations

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync


class MiniappGenerationContractPageSources:
    @staticmethod
    def _deterministic_client_page_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.routes.role_pages import resolve_role_page


router = APIRouter(prefix="/client", tags=["client"])

@router.get("", include_in_schema=False)
def client_root() -> FileResponse:
    return FileResponse(resolve_role_page("client", "/client"))


@router.get("/{page_path:path}", include_in_schema=False)
def client_nested_page(page_path: str) -> FileResponse:
    return FileResponse(resolve_role_page("client", f"/client/{page_path}"))
"""

    @staticmethod
    def _deterministic_specialist_page_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.routes.role_pages import resolve_role_page


router = APIRouter(prefix="/specialist", tags=["specialist"])


@router.get("", include_in_schema=False)
def specialist_root() -> FileResponse:
    return FileResponse(resolve_role_page("specialist", "/specialist"))


@router.get("/{page_path:path}", include_in_schema=False)
def specialist_nested_page(page_path: str) -> FileResponse:
    return FileResponse(resolve_role_page("specialist", f"/specialist/{page_path}"))
"""

    @staticmethod
    def _deterministic_manager_page_route_source() -> str:
        return """from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.routes.role_pages import resolve_role_page

router = APIRouter(prefix="/manager", tags=["manager"])


@router.get("", include_in_schema=False)
def manager_root() -> FileResponse:
    return FileResponse(resolve_role_page("manager", "/manager"))


@router.get("/{page_path:path}", include_in_schema=False)
def manager_nested_page(page_path: str) -> FileResponse:
    return FileResponse(resolve_role_page("manager", f"/manager/{page_path}"))
"""

    @staticmethod
    def _deterministic_main_runtime_source(route_modules: list[str]) -> str:
        return MiniappRuntimeContractSync.deterministic_main_runtime_source(route_modules)
