from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.routes.role_pages import resolve_role_page


router = APIRouter(tags=["role-pages"])


def serve_role_page(role: str, page_path: str = "") -> FileResponse:
    suffix = f"/{page_path}" if page_path else ""
    return FileResponse(resolve_role_page(role, f"/{role}{suffix}"))


@router.get("/client", include_in_schema=False)
def client_root() -> FileResponse:
    return serve_role_page("client")


@router.get("/client/{page_path:path}", include_in_schema=False)
def client_page(page_path: str) -> FileResponse:
    return serve_role_page("client", page_path)


@router.get("/specialist", include_in_schema=False)
def specialist_root() -> FileResponse:
    return serve_role_page("specialist")


@router.get("/specialist/{page_path:path}", include_in_schema=False)
def specialist_page(page_path: str) -> FileResponse:
    return serve_role_page("specialist", page_path)


@router.get("/manager", include_in_schema=False)
def manager_root() -> FileResponse:
    return serve_role_page("manager")


@router.get("/manager/{page_path:path}", include_in_schema=False)
def manager_page(page_path: str) -> FileResponse:
    return serve_role_page("manager", page_path)
