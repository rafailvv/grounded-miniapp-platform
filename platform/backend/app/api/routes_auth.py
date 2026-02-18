from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/session")
def get_auth_session() -> dict[str, object]:
    return {
        "mode": "single_user_research",
        "authenticated": True,
        "principal": "local_research_operator",
    }

