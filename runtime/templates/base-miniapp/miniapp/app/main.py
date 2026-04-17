from __future__ import annotations

import json
from pathlib import Path
import re

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.health import router as health_router
from app.routes.profiles import router as profiles_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
GENERATED_DIR = BASE_DIR / "generated"
ROUTE_MANIFEST_PATH = GENERATED_DIR / "route_manifest.json"
ROLES = ("client", "specialist", "manager")

app = FastAPI()
app.include_router(health_router)
app.include_router(profiles_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_preview_caching(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"GET", "HEAD"} and (request.url.path.startswith("/static/") or not request.url.path.startswith("/api/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


def _load_route_manifest() -> dict:
    if not ROUTE_MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(ROUTE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _route_matches(pattern: str, actual: str) -> bool:
    normalized_pattern = re.sub(r"\{[^/]+\}", "[^/]+", pattern)
    normalized_pattern = re.sub(r":[^/]+", "[^/]+", normalized_pattern)
    return re.fullmatch(normalized_pattern, actual) is not None


def _resolve_declared_page_file(role: str, actual_path: str) -> Path | None:
    route_manifest = _load_route_manifest()
    pages = (((route_manifest.get("roles") or {}).get(role) or {}).get("pages") or [])
    for page in pages:
        if not isinstance(page, dict):
            continue
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip()
        if not route_path or not file_path:
            continue
        if not _route_matches(route_path, actual_path):
            continue
        normalized_file_path = file_path.replace("\\", "/")
        if normalized_file_path.startswith("miniapp/app/"):
            resolved = BASE_DIR.parent / normalized_file_path.removeprefix("miniapp/app/")
        elif normalized_file_path.startswith("app/"):
            resolved = BASE_DIR.parent / normalized_file_path.removeprefix("app/")
        else:
            resolved = BASE_DIR / normalized_file_path
        if resolved.exists():
            return resolved
    return None


def _resolve_role_page(role: str, actual_path: str) -> Path:
    if role not in ROLES:
        raise KeyError(role)
    declared_page = _resolve_declared_page_file(role, actual_path)
    if declared_page is not None:
        return declared_page
    if actual_path == f"/{role}":
        page_file = STATIC_DIR / role / "index.html"
        if page_file.exists():
            return page_file
    if actual_path == f"/{role}/profile":
        page_file = STATIC_DIR / role / "profile" / "index.html"
        if page_file.exists():
            return page_file
    slug_parts = [segment for segment in actual_path.removeprefix(f"/{role}").split("/") if segment]
    if len(slug_parts) == 1:
        page_file = STATIC_DIR / role / slug_parts[0] / "index.html"
        if page_file.exists():
            return page_file
    raise KeyError(actual_path)


@app.get("/{role}", include_in_schema=False)
def role_page(role: str) -> FileResponse:
    return FileResponse(_resolve_role_page(role, f"/{role}"))


@app.get("/{role}/{page_path:path}", include_in_schema=False)
def role_nested_page(role: str, page_path: str) -> FileResponse:
    return FileResponse(_resolve_role_page(role, f"/{role}/{page_path}"))


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})
