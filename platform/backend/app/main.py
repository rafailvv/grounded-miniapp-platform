from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_auth,
    routes_chat,
    routes_documents,
    routes_export,
    routes_files,
    routes_preview,
    routes_public_apps,
    routes_rpc,
    routes_runs,
    routes_validation,
    routes_workbench,
    routes_workspaces,
)
from app.api.errors import http_exception_handler, unhandled_exception_handler, validation_exception_handler
from app.models.model_manager import ModelManagerStatus
from app.services.container import build_container


def configure_logging() -> None:
    level_name = os.getenv("PLATFORM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(max(logging.WARNING, level))


def create_app(*, repo_root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Upmini AI Studio",
        version="0.1.0",
        description="AI studio for generating, repairing, validating, and publishing Upmini mini-apps.",
    )
    app.state.container = build_container(repo_root=repo_root, data_dir=data_dir)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/system/configuration")
    def system_configuration() -> dict[str, object]:
        llm = app.state.container.openai_client.configuration()
        return {
            "llm": {
                "enabled": llm["enabled"],
                "provider": (llm.get("routing") or {}).get("provider") if isinstance(llm.get("routing"), dict) else None,
                "models": llm["models"],
                "task_profiles": llm["task_profiles"],
                "routing": llm.get("routing"),
                "provider_routing": llm.get("provider_routing"),
                "model_manager": llm.get("model_manager"),
                "model_catalog": llm.get("model_catalog"),
            },
            "defaults": {
                "generation_mode": "balanced",
                "model_profile": llm["default_coding_profile"],
            },
            "default_coding_profile": llm["default_coding_profile"],
            "supports_staged_apply": True,
            "research_artifacts_enabled": True,
        }

    @app.get("/system/models", response_model=ModelManagerStatus)
    def system_models() -> dict[str, object]:
        return app.state.container.model_manager_service.status().model_dump(mode="json", by_alias=True)

    app.include_router(routes_auth.router)
    app.include_router(routes_workspaces.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_validation.router)
    app.include_router(routes_files.router)
    app.include_router(routes_preview.router)
    app.include_router(routes_public_apps.router)
    app.include_router(routes_rpc.router)
    app.include_router(routes_export.router)
    app.include_router(routes_workbench.router)
    return app


app = create_app()
