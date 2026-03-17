from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_auth,
    routes_chat,
    routes_documents,
    routes_export,
    routes_files,
    routes_generation,
    routes_preview,
    routes_runs,
    routes_validation,
    routes_workspaces,
)
from app.services.container import build_container


def create_app(*, repo_root: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Grounded Mini-App Platform",
        version="0.1.0",
        description="Research-first grounded mini-app generation platform.",
    )
    app.state.container = build_container(repo_root=repo_root, data_dir=data_dir)
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
        llm = app.state.container.openrouter_client.configuration()
        return {
            "llm": {
                "enabled": llm["enabled"],
                "provider": "openrouter" if llm["enabled"] else None,
                "models": llm["models"],
                "task_profiles": llm["task_profiles"],
            },
            "defaults": {
                "generation_mode": "quality",
                "model_profile": llm["default_coding_profile"],
            },
            "default_coding_profile": llm["default_coding_profile"],
            "supports_staged_apply": True,
            "research_artifacts_enabled": True,
        }

    app.include_router(routes_auth.router)
    app.include_router(routes_workspaces.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_generation.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_validation.router)
    app.include_router(routes_files.router)
    app.include_router(routes_preview.router)
    app.include_router(routes_export.router)
    return app


app = create_app()
