from __future__ import annotations

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
