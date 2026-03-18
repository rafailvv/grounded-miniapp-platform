from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from fastapi import FastAPI

from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.profiles import router as profiles_router
from app.routes.runtime import router as runtime_router
from app.routes.submissions import router as submissions_router

BASE_ROUTE_MODULES = {
    "auth",
    "health",
    "profiles",
    "runtime",
    "submissions",
}

ROUTERS = (
    health_router,
    auth_router,
    runtime_router,
    profiles_router,
    submissions_router,
)


def register_routes(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)

    routes_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(routes_dir)]):
        name = module_info.name
        if name.startswith("_") or name in BASE_ROUTE_MODULES:
            continue
        module = importlib.import_module(f"app.routes.{name}")
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
