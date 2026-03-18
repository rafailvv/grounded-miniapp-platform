from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.store import register_submission

router = APIRouter()


@router.post("/api/submissions")
def create_submission(payload: dict[str, Any]) -> dict[str, Any]:
    return register_submission(payload)
