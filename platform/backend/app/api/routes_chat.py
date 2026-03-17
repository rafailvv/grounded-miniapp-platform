from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_container
from app.models.domain import ChatTurnRecord, CreateChatTurnRequest
from app.services.container import ServiceContainer

router = APIRouter(tags=["chat"])


@router.post("/workspaces/{workspace_id}/chat/turns", response_model=ChatTurnRecord)
def create_chat_turn(
    workspace_id: str,
    request: CreateChatTurnRequest,
    container: ServiceContainer = Depends(get_container),
) -> ChatTurnRecord:
    turn = ChatTurnRecord(
        workspace_id=workspace_id,
        role=request.role,
        content=request.content,
        summary=request.summary,
        linked_job_id=request.linked_job_id,
        linked_run_id=request.linked_run_id,
    )
    container.store.upsert("chat_turns", turn.turn_id, turn.model_dump(mode="json"))
    return turn


@router.get("/workspaces/{workspace_id}/chat/turns", response_model=list[ChatTurnRecord])
def list_chat_turns(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> list[ChatTurnRecord]:
    return [
        ChatTurnRecord.model_validate(item)
        for item in container.store.list("chat_turns")
        if item["workspace_id"] == workspace_id
    ]


@router.get("/turns/{turn_id}/summary")
def get_turn_summary(turn_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, str | None]:
    payload = container.store.get("chat_turns", turn_id)
    if not payload:
        raise HTTPException(status_code=404, detail=f"Turn not found: {turn_id}")
    turn = ChatTurnRecord.model_validate(payload)
    return {"turn_id": turn.turn_id, "summary": turn.summary or turn.content[:160]}
