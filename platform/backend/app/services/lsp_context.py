from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
from typing import Any

from app.models.lsp import LspContextReport, LspDiagnosticReportV2, LspReferenceReport, LspRouteGraphReport, LspSymbolIndex
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService, SOURCE_SUFFIXES
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.lsp_server_manager import LspServerManager
from app.services.workspace.service import WorkspaceService


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


class LspContextService:
    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        server_manager: LspServerManager | None = None,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.server_manager = server_manager or LspServerManager()
        self.event_journal_service = event_journal_service

    def diagnostics(
        self,
        *,
        workspace_id: str,
        run_id: str | None = None,
        changed_only: bool = False,
        files: list[str] | None = None,
        include_optional_tools: bool = True,
    ) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        changed_files = self._changed_files(workspace_id, run_id)
        static = LspToolService.diagnostics(
            root=root,
            targets=files,
            changed_files=changed_files,
            changed_only=changed_only,
            include_optional_tools=include_optional_tools,
        )
        selected = self._selected_files(root=root, files=files, changed_files=changed_files, changed_only=changed_only)
        real_items, server_status = self._real_diagnostics(workspace_id=workspace_id, run_id=run_id, root=root, files=selected)
        items = self._dedupe_items([*real_items, *list(static.get("items") or [])])
        error_count = sum(1 for item in items if item.get("severity") == "error")
        warning_count = sum(1 for item in items if item.get("severity") == "warning")
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        diagnostics_ref = self._diagnostics_ref(workspace_id, run_id, changed_only=changed_only)
        existing_report = self.store.get("reports", diagnostics_ref)
        route_graph = self.route_graph(workspace_id=workspace_id, run_id=run_id, targets=files, persist=True)
        symbols = self.symbol_context(workspace_id=workspace_id, run_id=run_id, query="", targets=files, persist=True)
        payload = {
            **static,
            "schema": "grounded.lsp_diagnostics.v1",
            "v2_schema": "grounded.lsp_diagnostics.v2",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "failed" if error_count else "passed",
            "engine": "real_lsp+static" if not fallback_used else "static",
            "server_status": server_status,
            "fallback_used": fallback_used,
            "diagnostics_ref": diagnostics_ref,
            "route_graph_ref": route_graph.get("route_graph_ref"),
            "symbol_index_ref": symbols.get("symbol_index_ref"),
            "items": items[:240],
            "jumps": self._jumps(items),
            "error_count": error_count,
            "warning_count": warning_count,
            "changed_only": changed_only,
            "changed_files": changed_files,
            "targets": files or [],
            "sources": sorted({str(item.get("source") or "unknown") for item in items} or {"none"}),
            "symbols": symbols.get("items") or [],
            "route_graph": route_graph,
            "next_sequence": self._next_sequence(diagnostics_ref),
            "updated_at": _now(),
        }
        if isinstance(existing_report, dict):
            for key in ("gate_required", "policy"):
                if key in existing_report and key not in payload:
                    payload[key] = existing_report[key]
        self.store.upsert("reports", diagnostics_ref, payload)
        self._record_run_event(workspace_id, run_id, "lsp.diagnostics.updated", payload, source_ref=diagnostics_ref)
        return payload

    def symbol_context(
        self,
        *,
        workspace_id: str,
        run_id: str | None = None,
        query: str = "",
        targets: list[str] | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        static = LspToolService.symbol_context(root=root, query=query, targets=targets)
        server_status = self._server_status_for_targets(workspace_id=workspace_id, run_id=run_id, root=root, targets=targets)
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        ref = self._symbol_ref(workspace_id, run_id)
        payload = {
            **static,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "engine": "real_lsp+static" if not fallback_used else "static",
            "server_status": server_status,
            "fallback_used": fallback_used,
            "symbol_index_ref": ref,
            "jumps": self._jumps(static.get("items") or []),
            "next_sequence": self._next_sequence(ref),
        }
        if persist:
            self.store.upsert("reports", ref, payload)
            self._record_run_event(workspace_id, run_id, "lsp.symbols.indexed", payload, source_ref=ref)
        return payload

    def definition(self, *, workspace_id: str, run_id: str | None = None, symbol: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        static = LspToolService.definition(root=root, symbol=symbol, targets=targets)
        server_status = self._server_status_for_targets(workspace_id=workspace_id, run_id=run_id, root=root, targets=targets)
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        return {
            **static,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "engine": "real_lsp+static" if not fallback_used else "static",
            "server_status": server_status,
            "fallback_used": fallback_used,
            "jumps": self._jumps(static.get("items") or []),
            "next_sequence": 1,
        }

    def find_references(self, *, workspace_id: str, run_id: str | None = None, symbol: str = "", targets: list[str] | None = None) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        static = LspToolService.find_references(root=root, symbol=symbol, targets=targets)
        server_status = self._server_status_for_targets(workspace_id=workspace_id, run_id=run_id, root=root, targets=targets)
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        ref = self._references_ref(workspace_id, run_id, symbol)
        payload = {
            **static,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "engine": "real_lsp+static" if not fallback_used else "static",
            "server_status": server_status,
            "fallback_used": fallback_used,
            "references_ref": ref,
            "jumps": self._jumps(static.get("items") or []),
            "next_sequence": self._next_sequence(ref),
        }
        self.store.upsert("reports", ref, payload)
        return payload

    def route_static_context(self, *, workspace_id: str, run_id: str | None = None, targets: list[str] | None = None) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        payload = LspToolService.route_static_context(root=root, targets=targets)
        server_status = self._server_status_for_targets(workspace_id=workspace_id, run_id=run_id, root=root, targets=targets)
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        return {**payload, "workspace_id": workspace_id, "run_id": run_id, "engine": "real_lsp+static" if not fallback_used else "static", "server_status": server_status, "fallback_used": fallback_used, "next_sequence": 1}

    def route_graph(self, *, workspace_id: str, run_id: str | None = None, targets: list[str] | None = None, persist: bool = False) -> dict[str, Any]:
        root = self._root(workspace_id, run_id)
        static = LspToolService.route_graph(root=root, targets=targets)
        server_status = self._server_status_for_targets(workspace_id=workspace_id, run_id=run_id, root=root, targets=targets)
        fallback_used = not any((state.get("status") == "running" for state in server_status.values()))
        ref = self._route_graph_ref(workspace_id, run_id)
        payload = {
            **static,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "engine": "real_lsp+static" if not fallback_used else "static",
            "server_status": server_status,
            "fallback_used": fallback_used,
            "route_graph_ref": ref,
            "jumps": self._jumps([*list(static.get("nodes") or []), *list(static.get("edges") or [])]),
            "next_sequence": self._next_sequence(ref),
        }
        if persist:
            self.store.upsert("reports", ref, payload)
            self._record_run_event(workspace_id, run_id, "lsp.route_graph.updated", payload, source_ref=ref)
        return payload

    def context(self, *, workspace_id: str, run_id: str | None = None, files: list[str] | None = None) -> dict[str, Any]:
        diagnostics = self.diagnostics(workspace_id=workspace_id, run_id=run_id, files=files)
        symbols = self.symbol_context(workspace_id=workspace_id, run_id=run_id, query="", targets=files, persist=True)
        route_graph = self.route_graph(workspace_id=workspace_id, run_id=run_id, targets=files, persist=True)
        ref = self._context_ref(workspace_id, run_id)
        fallback_used = bool(diagnostics.get("fallback_used") and symbols.get("fallback_used") and route_graph.get("fallback_used"))
        payload = LspContextReport(
            workspace_id=workspace_id,
            run_id=run_id,
            engine="real_lsp+static" if not fallback_used else "static",
            server_status=diagnostics.get("server_status") or {},
            fallback_used=fallback_used,
            lsp_context_ref=ref,
            diagnostics_ref=diagnostics.get("diagnostics_ref"),
            symbol_index_ref=symbols.get("symbol_index_ref"),
            route_graph_ref=route_graph.get("route_graph_ref"),
            diagnostics=diagnostics,
            symbols=symbols,
            route_graph=route_graph,
            items=list(diagnostics.get("items") or []),
            jumps=self._jumps([*list(diagnostics.get("items") or []), *list(symbols.get("items") or [])]),
            next_sequence=self._next_sequence(ref),
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", ref, payload)
        return payload

    def servers(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        return self.server_manager.states(workspace_id=workspace_id)

    def restart(self, *, workspace_id: str, run_id: str | None = None) -> dict[str, Any]:
        return self.server_manager.restart(workspace_id=workspace_id, run_id=run_id)

    def _real_diagnostics(self, *, workspace_id: str, run_id: str | None, root: Path, files: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items: list[dict[str, Any]] = []
        status: dict[str, Any] = {}
        by_language = {
            "python": [path for path in files if path.suffix.lower() == ".py"][:40],
            "typescript": [path for path in files if path.suffix.lower() in {".js", ".mjs", ".ts", ".tsx"}][:40],
        }
        for language, paths in by_language.items():
            if not paths:
                continue
            server = self.server_manager.server(workspace_id=workspace_id, run_id=run_id, root=root, language=language)
            state = server.start()
            status[language] = state.model_dump(mode="json", by_alias=True)
            if state.status != "running":
                self._record_run_event(workspace_id, run_id, "lsp.server.failed", status[language], source_ref=None)
                continue
            self._record_run_event(workspace_id, run_id, "lsp.server.started", status[language], source_ref=None)
            for path in paths:
                server.open_file(path)
            for notification in server.collect_diagnostics(timeout=1.2):
                items.extend(self._diagnostics_from_notification(root=root, notification=notification, source=f"{language}_lsp"))
        return items, status

    def _server_status_for_targets(self, *, workspace_id: str, run_id: str | None, root: Path, targets: list[str] | None) -> dict[str, Any]:
        files = self._selected_files(root=root, files=targets, changed_files=[], changed_only=False)
        languages = set()
        if any(path.suffix.lower() == ".py" for path in files):
            languages.add("python")
        if any(path.suffix.lower() in {".js", ".mjs", ".ts", ".tsx"} for path in files):
            languages.add("typescript")
        status: dict[str, Any] = {}
        for language in sorted(languages):
            state = self.server_manager.ensure(workspace_id=workspace_id, run_id=run_id, root=root, language=language)
            status[language] = state.model_dump(mode="json", by_alias=True)
        return status

    def _selected_files(self, *, root: Path, files: list[str] | None, changed_files: list[str], changed_only: bool) -> list[Path]:
        if changed_only:
            raw = changed_files
        else:
            raw = files or []
        if raw:
            result: list[Path] = []
            for item in raw:
                candidate = root / str(item).strip().lstrip("./")
                if candidate.is_file() and candidate.suffix.lower() in SOURCE_SUFFIXES:
                    result.append(candidate)
                elif candidate.is_dir():
                    result.extend(path for path in candidate.rglob("*") if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES)
            return sorted(dict.fromkeys(result), key=lambda path: LspToolService._relative(root, path))[:120]
        return LspToolService._selected_files(root=root)[:120]

    @staticmethod
    def _diagnostics_from_notification(*, root: Path, notification: dict[str, Any], source: str) -> list[dict[str, Any]]:
        params = notification.get("params") if isinstance(notification.get("params"), dict) else {}
        text_document = params.get("textDocument") if isinstance(params.get("textDocument"), dict) else {}
        uri = str(params.get("uri") or text_document.get("uri") or "")
        try:
            path = Path(uri.removeprefix("file://"))
        except Exception:
            path = root
        items: list[dict[str, Any]] = []
        for diagnostic in params.get("diagnostics") or []:
            if not isinstance(diagnostic, dict):
                continue
            range_payload = diagnostic.get("range") if isinstance(diagnostic.get("range"), dict) else {}
            start = range_payload.get("start") if isinstance(range_payload.get("start"), dict) else {}
            severity_value = int(diagnostic.get("severity") or 1)
            severity = "error" if severity_value == 1 else "warning" if severity_value == 2 else "info"
            line = int(start.get("line") or 0) + 1
            column = int(start.get("character") or 0) + 1
            items.append(
                {
                    "source": source,
                    "severity": severity,
                    "path": LspToolService._relative(root, path),
                    "file": LspToolService._relative(root, path),
                    "line": line,
                    "column": column,
                    "message": str(diagnostic.get("message") or "Language server diagnostic."),
                    "code": str(diagnostic.get("code") or ""),
                    "jump": {"path": LspToolService._relative(root, path), "line": line, "column": column, "label": f"{LspToolService._relative(root, path)}:{line}"},
                }
            )
        return items

    def _root(self, workspace_id: str, run_id: str | None) -> Path:
        self.workspace_service.get_workspace(workspace_id)
        return self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id and self.workspace_service.draft_exists(workspace_id, run_id) else self.workspace_service.source_dir(workspace_id)

    def _changed_files(self, workspace_id: str, run_id: str | None) -> list[str]:
        if not run_id:
            return []
        touched: list[str] = []
        try:
            run = self.store.get("runs", run_id) or {}
            run_touched = run.get("touched_files") if isinstance(run, dict) else []
            touched.extend(str(item) for item in run_touched or [] if str(item).strip())
        except Exception:
            pass
        try:
            diff = self.workspace_service.diff(workspace_id, run_id=run_id)
            if isinstance(diff, dict):
                touched.extend(self._paths_from_diff(str(diff.get("diff") or diff.get("unified_diff") or "")))
        except Exception:
            pass
        return list(dict.fromkeys(touched))

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith(("+++ b/", "--- a/")):
                continue
            path = line[6:].strip()
            if path and path != "/dev/null":
                paths.append(path)
        return paths

    @staticmethod
    def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, int, int, str]] = set()
        result: list[dict[str, Any]] = []
        for item in items:
            key = (str(item.get("source") or ""), str(item.get("path") or item.get("file") or ""), int(item.get("line") or 0), int(item.get("column") or 0), str(item.get("message") or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _jumps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        jumps: list[dict[str, Any]] = []
        for item in items:
            jump = item.get("jump") if isinstance(item.get("jump"), dict) else None
            if jump and jump not in jumps:
                jumps.append(jump)
        return jumps[:120]

    def _record_run_event(self, workspace_id: str, run_id: str | None, event_type: str, payload: dict[str, Any], *, source_ref: str | None) -> None:
        if self.event_journal_service is None or not run_id:
            return
        try:
            self.event_journal_service.append_run(
                workspace_id=workspace_id,
                run_id=run_id,
                event_type=event_type,
                payload={
                    "status": payload.get("status"),
                    "engine": payload.get("engine"),
                    "fallback_used": payload.get("fallback_used"),
                    "diagnostics_ref": payload.get("diagnostics_ref"),
                    "symbol_index_ref": payload.get("symbol_index_ref"),
                    "route_graph_ref": payload.get("route_graph_ref"),
                    "error_count": payload.get("error_count"),
                    "warning_count": payload.get("warning_count"),
                },
                actor="system",
                summary=event_type,
                source_ref=source_ref,
            )
        except Exception:
            return

    def _next_sequence(self, ref: str) -> int:
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            return 1
        return int(payload.get("next_sequence") or 1) + 1

    @staticmethod
    def _diagnostics_ref(workspace_id: str, run_id: str | None, *, changed_only: bool) -> str:
        suffix = f":{run_id}" if run_id else ":source"
        return f"lsp_diagnostics:{workspace_id}{suffix}{':changed' if changed_only else ''}"

    @staticmethod
    def _context_ref(workspace_id: str, run_id: str | None) -> str:
        return f"lsp_context:{workspace_id}:{run_id or 'source'}"

    @staticmethod
    def _symbol_ref(workspace_id: str, run_id: str | None) -> str:
        return f"lsp_symbol_index:{workspace_id}:{run_id or 'source'}"

    @staticmethod
    def _route_graph_ref(workspace_id: str, run_id: str | None) -> str:
        return f"lsp_route_graph:{workspace_id}:{run_id or 'source'}"

    @staticmethod
    def _references_ref(workspace_id: str, run_id: str | None, symbol: str) -> str:
        digest = hashlib.sha256(str(symbol or "").encode("utf-8")).hexdigest()[:12]
        return f"lsp_references:{workspace_id}:{run_id or 'source'}:{digest}"
