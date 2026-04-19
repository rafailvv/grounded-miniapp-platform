from __future__ import annotations

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
