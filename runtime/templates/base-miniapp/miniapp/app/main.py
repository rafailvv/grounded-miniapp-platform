from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.client import router as client_router
from app.routes.health import router as health_router
from app.routes.manager import router as manager_router
from app.routes.profiles import router as profiles_router
from app.routes.specialist import router as specialist_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
app.include_router(profiles_router)
app.include_router(client_router)
app.include_router(specialist_router)
app.include_router(manager_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def disable_preview_caching(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"GET", "HEAD"} and (request.url.path.startswith("/static/") or not request.url.path.startswith("/api/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})
