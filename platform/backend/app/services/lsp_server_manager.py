from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

from app.models.lsp import LspServerState


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


def encode_lsp_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def decode_lsp_messages(buffer: bytes) -> tuple[list[dict[str, Any]], bytes]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(buffer):
        header_end = buffer.find(b"\r\n\r\n", index)
        separator_len = 4
        if header_end < 0:
            header_end = buffer.find(b"\n\n", index)
            separator_len = 2
        if header_end < 0:
            break
        header = buffer[index:header_end].decode("ascii", errors="ignore")
        length = None
        for line in header.splitlines():
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    length = None
                break
        if length is None:
            index = header_end + separator_len
            continue
        body_start = header_end + separator_len
        body_end = body_start + length
        if body_end > len(buffer):
            break
        try:
            payload = json.loads(buffer[body_start:body_end].decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            messages.append(payload)
        index = body_end
    return messages, buffer[index:]


@dataclass
class ManagedLspServer:
    key: str
    workspace_id: str
    run_id: str | None
    language: str
    root: Path
    command: list[str]
    process: subprocess.Popen[bytes] | None = None
    initialized: bool = False
    status: str = "stopped"
    message: str = ""
    started_at: str | None = None
    updated_at: str | None = None
    _seq: int = 0
    _responses: dict[int, dict[str, Any]] = field(default_factory=dict)
    _notifications: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    _reader: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, *, timeout: float = 8.0) -> LspServerState:
        if self.process is not None and self.process.poll() is None and self.initialized:
            return self.state(fallback_used=False)
        if not self.command:
            self.status = "unavailable"
            self.message = "language server command not found"
            self.updated_at = _now()
            return self.state(fallback_used=True)
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.status = "failed"
            self.message = str(exc)
            self.updated_at = _now()
            return self.state(fallback_used=True)
        self.status = "starting"
        self.started_at = self.started_at or _now()
        self.updated_at = _now()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        response = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": _uri(self.root),
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": True},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    }
                },
                "workspaceFolders": [{"uri": _uri(self.root), "name": self.root.name}],
            },
            timeout=timeout,
        )
        if response.get("error"):
            self.status = "failed"
            self.message = str(response.get("error"))[:500]
            return self.state(fallback_used=True)
        self.notify("initialized", {})
        self.initialized = True
        self.status = "running"
        self.message = "initialized"
        self.updated_at = _now()
        return self.state(fallback_used=False)

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 6.0) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            request_id = self._seq
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._responses.pop(request_id, None)
            if response is not None:
                return response
            if self.process is None or self.process.poll() is not None:
                return {"error": {"message": "language server exited"}}
            time.sleep(0.02)
        return {"error": {"message": f"{method} timed out"}}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def open_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(path),
                    "languageId": self._language_id(path),
                    "version": 1,
                    "text": text,
                }
            },
        )

    def collect_diagnostics(self, *, timeout: float = 1.5) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        notifications: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                message = self._notifications.get(timeout=0.05)
            except queue.Empty:
                continue
            if message.get("method") == "textDocument/publishDiagnostics":
                notifications.append(message)
        return notifications

    def shutdown(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.request("shutdown", {}, timeout=2.0)
                self.notify("exit", {})
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        finally:
            self.status = "stopped"
            self.initialized = False
            self.updated_at = _now()

    def state(self, *, fallback_used: bool) -> LspServerState:
        return LspServerState(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            language=self.language,
            status=self.status,
            command=list(self.command),
            root_uri=_uri(self.root),
            pid=self.process.pid if self.process is not None and self.process.poll() is None else None,
            initialized=self.initialized,
            fallback_used=fallback_used,
            message=self.message,
            started_at=self.started_at,
            updated_at=self.updated_at or _now(),
        )

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise BrokenPipeError("language server is not running")
        self.process.stdin.write(encode_lsp_message(payload))
        self.process.stdin.flush()

    def _read_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        buffer = b""
        while self.process.poll() is None:
            chunk = self.process.stdout.read(1)
            if not chunk:
                break
            buffer += chunk
            messages, buffer = decode_lsp_messages(buffer)
            for message in messages:
                if "id" in message and ("result" in message or "error" in message):
                    try:
                        self._responses[int(message["id"])] = message
                    except (TypeError, ValueError):
                        pass
                else:
                    self._notifications.put(message)

    @staticmethod
    def _language_id(path: Path) -> str:
        return {
            ".py": "python",
            ".js": "javascript",
            ".mjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
        }.get(path.suffix.lower(), "plaintext")


class LspServerManager:
    def __init__(self, *, command_overrides: dict[str, list[str]] | None = None) -> None:
        self.command_overrides = command_overrides or {}
        self._servers: dict[str, ManagedLspServer] = {}
        self._lock = threading.Lock()

    def server(self, *, workspace_id: str, run_id: str | None, root: Path, language: str) -> ManagedLspServer:
        key = self._key(workspace_id=workspace_id, run_id=run_id, root=root, language=language)
        with self._lock:
            existing = self._servers.get(key)
            if existing is not None:
                return existing
            server = ManagedLspServer(
                key=key,
                workspace_id=workspace_id,
                run_id=run_id,
                language=language,
                root=root,
                command=self._command(root=root, language=language),
            )
            self._servers[key] = server
            return server

    def ensure(self, *, workspace_id: str, run_id: str | None, root: Path, language: str) -> LspServerState:
        return self.server(workspace_id=workspace_id, run_id=run_id, root=root, language=language).start()

    def states(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        states = [
            server.state(fallback_used=server.status != "running").model_dump(mode="json", by_alias=True)
            for server in self._servers.values()
            if workspace_id is None or server.workspace_id == workspace_id
        ]
        return {"schema": "grounded.lsp_servers.v1", "status": "ok", "items": states}

    def restart(self, *, workspace_id: str, run_id: str | None = None) -> dict[str, Any]:
        restarted: list[dict[str, Any]] = []
        for key, server in list(self._servers.items()):
            if server.workspace_id != workspace_id:
                continue
            if run_id is not None and server.run_id != run_id:
                continue
            server.shutdown()
            restarted.append(server.start().model_dump(mode="json", by_alias=True))
        return {"schema": "grounded.lsp_restart.v1", "status": "ok", "items": restarted}

    def shutdown_all(self) -> None:
        for server in list(self._servers.values()):
            server.shutdown()

    def _command(self, *, root: Path, language: str) -> list[str]:
        if language in self.command_overrides:
            return list(self.command_overrides[language])
        if language == "python":
            binary = self._find_binary(root, "pyright-langserver")
            return [binary, "--stdio"] if binary else []
        if language == "typescript":
            binary = self._find_binary(root, "typescript-language-server")
            return [binary, "--stdio"] if binary else []
        return []

    @staticmethod
    def _find_binary(root: Path, name: str) -> str | None:
        env_name = f"GROUND_LSP_{name.upper().replace('-', '_')}"
        if os.getenv(env_name):
            return os.getenv(env_name)
        for candidate in (
            root / "node_modules" / ".bin" / name,
            root / "miniapp" / "node_modules" / ".bin" / name,
            root / "miniapp" / "app" / "node_modules" / ".bin" / name,
            Path.cwd() / "platform" / "frontend" / "node_modules" / ".bin" / name,
        ):
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which(name)

    @staticmethod
    def _key(*, workspace_id: str, run_id: str | None, root: Path, language: str) -> str:
        return f"{workspace_id}:{run_id or 'source'}:{language}:{root.resolve()}"
