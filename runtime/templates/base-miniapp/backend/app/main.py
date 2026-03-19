from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.dependencies import get_profile_service


def create_app() -> FastAPI:
    app = FastAPI(title="Base Mini-App Backend", version="2.0.0")
    app.include_router(api_router)

    @app.on_event("startup")
    def startup() -> None:
        get_profile_service().init_storage()

    @app.exception_handler(KeyError)
    def key_error_handler(_, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app


app = create_app()
