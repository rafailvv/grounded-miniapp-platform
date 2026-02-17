from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.container import ServiceContainer


def get_container(request: Request) -> ServiceContainer:
    return request.app.state.container


def raise_not_found(message: str) -> None:
    raise HTTPException(status_code=404, detail=message)

