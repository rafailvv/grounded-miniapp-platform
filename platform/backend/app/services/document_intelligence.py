from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.core.config import Settings
from app.models.domain import DocumentChunkRecord, DocumentRecord
from app.models.grounded_spec import DocRef
from app.repositories.state_store import StateStore
from app.services.platform_adapters import get_platform_adapter


class DocumentIntelligenceService:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store

    def save_document(self, document: DocumentRecord) -> DocumentRecord:
        self.store.upsert("documents", document.document_id, document.model_dump(mode="json"))
        return document

    def get_document(self, document_id: str) -> DocumentRecord:
        payload = self.store.get("documents", document_id)
        if not payload:
            raise KeyError(f"Document not found: {document_id}")
        return DocumentRecord.model_validate(payload)

    def list_documents(self, workspace_id: str) -> list[DocumentRecord]:
        return [
            DocumentRecord.model_validate(item)
            for item in self.store.list("documents")
            if item["workspace_id"] == workspace_id
        ]

    def index(self, document_id: str) -> DocumentRecord:
        document = self.get_document(document_id)
        document.chunks = self._chunk_document(document.content)
        document.indexed = True
        self.store.upsert("documents", document.document_id, document.model_dump(mode="json"))
        return document

    def get_chunks(self, document_id: str) -> list[DocumentChunkRecord]:
        return self.get_document(document_id).chunks

    def retrieve(
        self,
        *,
        workspace_id: str,
        prompt: str,
        target_platform: str,
        limit: int = 8,
    ) -> list[DocRef]:
        query_terms = self._tokenize(prompt)
        refs: list[DocRef] = []
        for document in self.list_documents(workspace_id):
            if not document.indexed:
                continue
            refs.extend(self._refs_from_document(document, query_terms))
        refs.extend(self._refs_from_bundled_dir(self.settings.template_dir / "docs", "project_doc", query_terms))
        adapter = get_platform_adapter(target_platform)
        refs.extend(
            self._refs_from_bundled_dir(
                self.settings.runtime_dir / "platform-docs" / adapter.doc_dir_name,
                "platform_doc",
                query_terms,
            )
        )
        refs.append(
            DocRef(
                doc_ref_id="prompt-source",
                source_type="user_prompt",
                file_path="prompt",
                chunk_id="prompt-0",
                section_title="User prompt",
                snippet=prompt,
                relevance=1.0,
            )
        )
        refs.sort(key=lambda item: item.relevance, reverse=True)
        return refs[:limit]

    def ensure_required_corpora(self, target_platform: str) -> list[str]:
        issues: list[str] = []
        adapter = get_platform_adapter(target_platform)
        template_docs = list((self.settings.template_dir / "docs").glob("*"))
        platform_docs = list((self.settings.runtime_dir / "platform-docs" / adapter.doc_dir_name).glob("*"))
        if not template_docs:
            issues.append("Canonical template documentation is missing.")
        if not platform_docs:
            issues.append(f"Bundled platform corpus is missing for {target_platform}.")
        return issues

    def _refs_from_document(self, document: DocumentRecord, query_terms: set[str]) -> list[DocRef]:
        refs: list[DocRef] = []
        for chunk in document.chunks:
            score = self._score(chunk.content, query_terms)
            if score <= 0:
                continue
            refs.append(
                DocRef(
                    doc_ref_id=f"{document.document_id}:{chunk.chunk_id}",
                    source_type=document.source_type,
                    file_path=document.file_path,
                    chunk_id=chunk.chunk_id,
                    section_title=chunk.section_title,
                    snippet=chunk.content[:280],
                    relevance=score,
                )
            )
        return refs

    def _refs_from_bundled_dir(
        self,
        directory: Path,
        source_type: str,
        query_terms: set[str],
    ) -> list[DocRef]:
        refs: list[DocRef] = []
        for file_path in sorted(directory.rglob("*")):
            if not file_path.is_file():
                continue
            content = file_path.read_text(encoding="utf-8")
            for chunk in self._chunk_document(content):
                score = self._score(chunk.content, query_terms)
                if score <= 0:
                    continue
                refs.append(
                    DocRef(
                        doc_ref_id=f"{source_type}:{file_path.name}:{chunk.chunk_id}",
                        source_type=source_type,  # type: ignore[arg-type]
                        file_path=str(file_path.relative_to(self.settings.repo_root)),
                        chunk_id=chunk.chunk_id,
                        section_title=chunk.section_title,
                        snippet=chunk.content[:280],
                        relevance=score,
                    )
                )
        return refs

    def _chunk_document(self, content: str) -> list[DocumentChunkRecord]:
        sections = [section.strip() for section in content.split("\n\n") if section.strip()]
        return [
            DocumentChunkRecord(
                section_title=self._section_title(section),
                content=section,
                semantic_role="section",
            )
            for section in sections
        ]

    @staticmethod
    def _section_title(section: str) -> str:
        first_line = section.splitlines()[0].strip()
        return first_line.lstrip("# ").strip()[:80]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {token.lower() for token in text.replace("/", " ").replace("_", " ").split() if len(token) > 2}

    def _score(self, content: str, query_terms: set[str]) -> float:
        content_terms = self._tokenize(content)
        if not content_terms:
            return 0.0
        overlap = len(content_terms & query_terms)
        return overlap / max(len(query_terms), 1)

