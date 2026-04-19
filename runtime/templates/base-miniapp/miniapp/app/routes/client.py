from __future__ import annotations

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
