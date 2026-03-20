from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from app.core.config import Settings
from app.models.domain import CodeChunkRecord, DocumentRecord, IndexStatusRecord, WorkspaceRecord, utc_now
from app.repositories.state_store import StateStore

EMBEDDING_SIZE = 24
TEXT_FILE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".md",
    ".css",
    ".html",
    ".yml",
    ".yaml",
}


class CodeIndexService:
    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store

    def index_workspace(self, workspace: WorkspaceRecord, source_dir: Path) -> IndexStatusRecord:
        revision_id = workspace.current_revision_id or "unversioned"
        existing = self.get_workspace_status(workspace.workspace_id)
        if existing.status == "ready" and existing.revision_id == revision_id and existing.chunk_count > 0:
            return existing

        file_count = sum(1 for _ in self._iter_workspace_files(source_dir))
        status = IndexStatusRecord(
            workspace_id=workspace.workspace_id,
            revision_id=revision_id,
            status="ready",
            chunk_count=file_count,
            indexed_at=utc_now(),
        )
        self.store.upsert("code_indexes", f"workspace:{workspace.workspace_id}", status.model_dump(mode="json"))
        return status

    def index_documents(self, workspace_id: str, documents: list[DocumentRecord]) -> IndexStatusRecord:
        revision_id = self._doc_revision_id(documents)
        existing = self.get_document_status(workspace_id)
        if existing.status == "ready" and existing.revision_id == revision_id and existing.chunk_count > 0:
            return existing

        status = IndexStatusRecord(
            workspace_id=workspace_id,
            revision_id=revision_id,
            status="ready",
            chunk_count=len(documents),
            indexed_at=utc_now(),
        )
        self.store.upsert("code_indexes", f"docs:{workspace_id}", status.model_dump(mode="json"))
        return status

    def get_workspace_status(self, workspace_id: str) -> IndexStatusRecord:
        payload = self.store.get("code_indexes", f"workspace:{workspace_id}")
        if not payload:
            return IndexStatusRecord(workspace_id=workspace_id)
        return IndexStatusRecord.model_validate(payload)

    def get_document_status(self, workspace_id: str) -> IndexStatusRecord:
        payload = self.store.get("code_indexes", f"docs:{workspace_id}")
        if not payload:
            return IndexStatusRecord(workspace_id=workspace_id)
        return IndexStatusRecord.model_validate(payload)

    def get_chunks(self, workspace_id: str, *, kind: str = "code") -> list[CodeChunkRecord]:
        prefix = f"{kind}:{workspace_id}:"
        return [
            CodeChunkRecord.model_validate(value)
            for key, value in self.store.items("code_chunks")
            if key.startswith(prefix)
        ]

    def retrieve(
        self,
        *,
        workspace_id: str,
        prompt: str,
        code_limit: int = 6,
        doc_limit: int = 4,
        active_paths: list[str] | None = None,
        recent_paths: list[str] | None = None,
    ) -> dict[str, object]:
        active = set(active_paths or [])
        recent = set(recent_paths or [])
        query_embedding = self._embedding(prompt)
        query_terms = self._tokenize(prompt)
        status = self.get_workspace_status(workspace_id)
        revision_id = status.revision_id or "unversioned"
        source_dir = self.settings.workspaces_dir / workspace_id / "source"
        candidate_paths = self._candidate_workspace_paths(
            source_dir=source_dir,
            query_terms=query_terms,
            active_paths=active,
            recent_paths=recent,
            limit=max(code_limit * 8, 24),
        )
        candidate_chunks: list[CodeChunkRecord] = []
        for relative_path in candidate_paths:
            file_path = source_dir / relative_path
            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            candidate_chunks.extend(
                self._chunk_text(
                    workspace_id=workspace_id,
                    revision_id=revision_id,
                    relative_path=relative_path,
                    content=content,
                    kind="code",
                    source_type="workspace_code",
                )
            )
        code = self._rank_chunks(candidate_chunks, query_terms, query_embedding, active, recent)[:code_limit]
        docs: list[CodeChunkRecord] = []
        return {
            "code": [chunk.model_dump(mode="json") for chunk in code],
            "docs": [chunk.model_dump(mode="json") for chunk in docs],
            "stats": {
                "workspace_chunk_count": len(candidate_chunks),
                "doc_chunk_count": 0,
                "code_hits": len(code),
                "doc_hits": len(docs),
                "query_terms": len(query_terms),
                "candidate_files": len(candidate_paths),
            },
        }

    def _store_index(self, workspace_id: str, revision_id: str, chunks: list[CodeChunkRecord]) -> IndexStatusRecord:
        self._replace_kind_chunks(workspace_id, "code", chunks)
        status = IndexStatusRecord(
            workspace_id=workspace_id,
            revision_id=revision_id,
            status="ready",
            chunk_count=len(chunks),
            indexed_at=utc_now(),
        )
        self.store.upsert("code_indexes", f"workspace:{workspace_id}", status.model_dump(mode="json"))
        return status

    def _store_doc_index(self, workspace_id: str, revision_id: str, chunks: list[CodeChunkRecord]) -> IndexStatusRecord:
        self._replace_kind_chunks(workspace_id, "doc", chunks)
        status = IndexStatusRecord(
            workspace_id=workspace_id,
            revision_id=revision_id,
            status="ready",
            chunk_count=len(chunks),
            indexed_at=utc_now(),
        )
        self.store.upsert("code_indexes", f"docs:{workspace_id}", status.model_dump(mode="json"))
        return status

    def _replace_kind_chunks(self, workspace_id: str, kind: str, chunks: list[CodeChunkRecord]) -> None:
        prefix = f"{kind}:{workspace_id}:"
        for key, _ in self.store.items("code_chunks"):
            if key.startswith(prefix):
                self.store.delete("code_chunks", key)
        for chunk in chunks:
            self.store.upsert("code_chunks", f"{kind}:{workspace_id}:{chunk.chunk_id}", chunk.model_dump(mode="json"))

    def _iter_workspace_files(self, source_dir: Path) -> list[Path]:
        ignored_dirs = {
            ".git",
            "node_modules",
            "dist",
            "build",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".next",
            ".vite",
            "coverage",
        }
        files: list[Path] = []
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in ignored_dirs for part in file_path.parts):
                continue
            if file_path.suffix.lower() not in TEXT_FILE_SUFFIXES:
                continue
            files.append(file_path)
        return files

    def _candidate_workspace_paths(
        self,
        *,
        source_dir: Path,
        query_terms: set[str],
        active_paths: set[str],
        recent_paths: set[str],
        limit: int,
    ) -> list[str]:
        ranked: list[tuple[float, str]] = []
        for file_path in self._iter_workspace_files(source_dir):
            relative_path = str(file_path.relative_to(source_dir))
            score = self._path_score(relative_path, query_terms)
            if relative_path in active_paths:
                score += 1.25
            if relative_path in recent_paths:
                score += 0.75
            if score <= 0:
                continue
            ranked.append((score, relative_path))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [path for _, path in ranked[:limit]]

    def _path_score(self, relative_path: str, query_terms: set[str]) -> float:
        if not query_terms:
            return 0.0
        lowered = relative_path.lower()
        path_terms = self._tokenize(relative_path)
        overlap = len(query_terms & path_terms)
        score = overlap * 0.5
        score += sum(0.15 for term in query_terms if term in lowered)
        basename = Path(relative_path).name.lower()
        if any(term in basename for term in query_terms):
            score += 0.35
        return score

    def _chunk_text(
        self,
        *,
        workspace_id: str,
        revision_id: str,
        relative_path: str,
        content: str,
        kind: str,
        source_type: str,
    ) -> list[CodeChunkRecord]:
        lines = content.splitlines()
        if not lines:
            return []
        blocks = self._semantic_blocks(lines, relative_path)
        return [
            self._build_chunk(
                workspace_id=workspace_id,
                revision_id=revision_id,
                relative_path=relative_path,
                text="\n".join(lines[start - 1 : end]),
                start_line=start,
                end_line=end,
                kind=kind,
                source_type=source_type,
            )
            for start, end in blocks
            if start <= end and "\n".join(lines[start - 1 : end]).strip()
        ]

    def _build_chunk(
        self,
        *,
        workspace_id: str,
        revision_id: str,
        relative_path: str,
        text: str,
        start_line: int,
        end_line: int,
        kind: str,
        source_type: str,
    ) -> CodeChunkRecord:
        symbols = sorted(set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text)))[:24]
        imports = sorted(set(re.findall(r"(?:from|import)\s+([A-Za-z0-9_./-]+)", text)))[:12]
        chunk_hash = hashlib.sha256(f"{relative_path}:{start_line}:{end_line}:{text}".encode("utf-8")).hexdigest()
        summary = text.splitlines()[0].strip()[:120] if text.strip() else relative_path
        return CodeChunkRecord(
            workspace_id=workspace_id,
            revision_id=revision_id,
            path=relative_path,
            language=self._language_for_path(relative_path),
            kind=kind,  # type: ignore[arg-type]
            start_line=start_line,
            end_line=end_line,
            text=text,
            symbols=symbols,
            imports=imports,
            chunk_hash=chunk_hash,
            summary=summary,
            embedding=self._embedding(text),
            source_type=source_type,
        )

    def _rank_chunks(
        self,
        chunks: list[CodeChunkRecord],
        query_terms: set[str],
        query_embedding: list[float],
        active_paths: set[str],
        recent_paths: set[str],
    ) -> list[CodeChunkRecord]:
        ranked: list[CodeChunkRecord] = []
        for chunk in chunks:
            lexical = self._lexical_score(query_terms, chunk)
            dense = self._cosine(query_embedding, chunk.embedding)
            boost = 0.0
            if chunk.path in active_paths:
                boost += 0.35
            if chunk.path in recent_paths:
                boost += 0.2
            if any(term in chunk.path.lower() for term in query_terms):
                boost += 0.15
            score = lexical * 0.55 + dense * 0.35 + boost
            if score <= 0:
                continue
            ranked.append(chunk.model_copy(update={"score": round(score, 5)}))
        ranked.sort(key=lambda item: ((item.score or 0.0), item.path, item.start_line), reverse=True)
        return ranked

    @staticmethod
    def _semantic_blocks(lines: list[str], relative_path: str) -> list[tuple[int, int]]:
        suffix = Path(relative_path).suffix.lower()
        pattern: re.Pattern[str] | None = None
        if suffix == ".py":
            pattern = re.compile(r"^(?:class|def)\s+[A-Za-z_][A-Za-z0-9_]*")
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            pattern = re.compile(
                r"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+[A-Za-z_][A-Za-z0-9_]*"
            )
        elif suffix in {".md", ".json", ".css", ".html", ".yml", ".yaml"}:
            return CodeIndexService._window_blocks(lines, window=40)
        starts = [index + 1 for index, line in enumerate(lines) if pattern and pattern.search(line.strip())]
        if not starts:
            return CodeIndexService._window_blocks(lines, window=80)
        blocks: list[tuple[int, int]] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
            if end - start < 3 and blocks:
                prev_start, _ = blocks.pop()
                blocks.append((prev_start, end))
            else:
                blocks.append((start, end))
        return blocks

    @staticmethod
    def _window_blocks(lines: list[str], *, window: int) -> list[tuple[int, int]]:
        return [(start, min(len(lines), start + window - 1)) for start in range(1, len(lines) + 1, window)]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {token.lower() for token in re.findall(r"[A-Za-z0-9_./-]{3,}", text)}

    def _lexical_score(self, query_terms: set[str], chunk: CodeChunkRecord) -> float:
        if not query_terms:
            return 0.0
        haystack = set(chunk.symbols) | set(chunk.imports) | self._tokenize(chunk.path) | self._tokenize(chunk.text[:800])
        overlap = len({term for term in query_terms if term in {item.lower() for item in haystack}})
        return overlap / max(len(query_terms), 1)

    @staticmethod
    def _embedding(text: str) -> list[float]:
        buckets = [0.0] * EMBEDDING_SIZE
        for token in re.findall(r"[A-Za-z0-9_./-]{2,}", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = digest[0] % EMBEDDING_SIZE
            buckets[index] += 1.0
        norm = math.sqrt(sum(value * value for value in buckets)) or 1.0
        return [round(value / norm, 6) for value in buckets]

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right:
            return 0.0
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _language_for_path(relative_path: str) -> str:
        suffix = Path(relative_path).suffix.lower()
        return {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".js": "javascript",
            ".jsx": "jsx",
            ".md": "markdown",
            ".json": "json",
            ".css": "css",
            ".html": "html",
            ".yml": "yaml",
            ".yaml": "yaml",
        }.get(suffix, "text")

    @staticmethod
    def _doc_revision_id(documents: list[DocumentRecord]) -> str:
        digest = hashlib.sha256()
        for document in documents:
            digest.update(document.document_id.encode("utf-8"))
            digest.update(document.content.encode("utf-8"))
        return digest.hexdigest()[:16]
