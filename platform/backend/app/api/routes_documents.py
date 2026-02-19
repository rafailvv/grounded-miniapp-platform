from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_container
from app.models.domain import DocumentRecord, SaveDocumentRequest
from app.services.container import ServiceContainer

router = APIRouter(tags=["documents"])


@router.post("/workspaces/{workspace_id}/documents", response_model=DocumentRecord)
def create_document(
    workspace_id: str,
    request: SaveDocumentRequest,
    container: ServiceContainer = Depends(get_container),
) -> DocumentRecord:
    document = DocumentRecord(
        workspace_id=workspace_id,
        file_name=request.file_name,
        file_path=request.file_path,
        source_type=request.source_type,
        content=request.content,
    )
    return container.document_service.save_document(document)


@router.get("/workspaces/{workspace_id}/documents", response_model=list[DocumentRecord])
def list_documents(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> list[DocumentRecord]:
    return container.document_service.list_documents(workspace_id)


@router.post("/documents/{document_id}/index", response_model=DocumentRecord)
def index_document(document_id: str, container: ServiceContainer = Depends(get_container)) -> DocumentRecord:
    try:
        return container.document_service.index(document_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/documents/{document_id}/chunks")
def get_document_chunks(document_id: str, container: ServiceContainer = Depends(get_container)) -> list[dict]:
    try:
        return [chunk.model_dump(mode="json") for chunk in container.document_service.get_chunks(document_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

