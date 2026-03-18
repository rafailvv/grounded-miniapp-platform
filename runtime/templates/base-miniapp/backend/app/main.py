from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.routes import register_routes
from app.store import ensure_state, ensure_store_data

app = FastAPI(title="Base Mini-App Backend", version="2.0.0")
register_routes(app)


@app.on_event("startup")
def startup() -> None:
    ensure_state()
    ensure_store_data()


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})
