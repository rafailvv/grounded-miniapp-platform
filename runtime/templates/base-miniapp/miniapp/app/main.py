from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.health import router as health_router
from app.routes.profiles import router as profiles_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ROLES = ("client", "specialist", "manager")

app = FastAPI()
app.include_router(health_router)
app.include_router(profiles_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


@app.get("/{role}", include_in_schema=False)
def role_page(role: str) -> FileResponse:
    if role not in ROLES:
        raise KeyError(role)
    return FileResponse(STATIC_DIR / role / "index.html")


@app.get("/{role}/profile", include_in_schema=False)
def role_profile_page(role: str) -> FileResponse:
    if role not in ROLES:
        raise KeyError(role)
    return FileResponse(STATIC_DIR / role / "profile.html")


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})
