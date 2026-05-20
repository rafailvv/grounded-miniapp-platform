from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.validators.static_analysis import (
    extract_declared_routes,
    extract_frontend_api_refs,
    extract_js_dom_ids,
    normalize_api_path,
    role_surface_dom_ids,
)


IGNORED_PARTS = {".git", "node_modules", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache"}
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".ts", ".tsx", ".html", ".css"}


class LspToolService:
    """Deterministic LSP-like source intelligence for agent tools and Workbench."""

    @classmethod
    def diagnostics(
        cls,
        *,
        root: Path,
        targets: list[str] | None = None,
        changed_files: list[str] | None = None,
        changed_only: bool = False,
        include_optional_tools: bool = True,
    ) -> dict[str, Any]:
        selected_files = cls._selected_files(root=root, targets=targets, changed_files=changed_files, changed_only=changed_only)
        py_files = [path for path in selected_files if path.suffix == ".py"]
        js_files = [path for path in selected_files if path.suffix in {".js", ".mjs"}]
        ts_files = [path for path in selected_files if path.suffix in {".ts", ".tsx"}]
        items: list[dict[str, Any]] = []
        tool_status: dict[str, Any] = {}
        diagnostic_stream: list[dict[str, Any]] = []

        def stream(phase: str, status: str, **details: Any) -> None:
            diagnostic_stream.append({"phase": phase, "status": status, "created_at": datetime.now(timezone.utc).isoformat(), **details})

        backend_dir = root / "miniapp"
        for py_file in py_files[:120]:
            try:
                command_path = py_file.relative_to(backend_dir) if backend_dir in py_file.parents else py_file
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(command_path)],
                    cwd=backend_dir if backend_dir.exists() else root,
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
            except Exception as exc:
                items.append(cls._diagnostic_item(root=root, source="python_compile", severity="error", file_path=py_file, message=str(exc)))
                continue
            if result.returncode != 0:
                text = result.stderr or result.stdout
                line = cls._line_from_python_compile(text)
                items.append(
                    cls._diagnostic_item(
                        root=root,
                        source="python_compile",
                        severity="error",
                        file_path=py_file,
                        line=line,
                        message=cls._first_meaningful_line(text) or "Python compile failed.",
                        details=cls._bounded(text, 1600),
                    )
                )
        tool_status["python_compile"] = {"status": "checked", "file_count": min(len(py_files), 120), "truncated": len(py_files) > 120}
        stream("python_compile", "completed", file_count=min(len(py_files), 120), issue_count=len([item for item in items if item.get("source") == "python_compile"]))

        node_binary = shutil.which("node") or shutil.which("nodejs")
        if node_binary:
            for js_file in js_files[:120]:
                try:
                    command_path = js_file.relative_to(backend_dir) if backend_dir in js_file.parents else js_file
                    result = subprocess.run(
                        [node_binary, "--check", str(command_path)],
                        cwd=backend_dir if backend_dir.exists() else root,
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                except Exception as exc:
                    items.append(cls._diagnostic_item(root=root, source="node_check", severity="error", file_path=js_file, message=str(exc)))
                    continue
                if result.returncode != 0:
                    text = result.stderr or result.stdout
                    items.append(
                        cls._diagnostic_item(
                            root=root,
                            source="node_check",
                            severity="error",
                            file_path=js_file,
                            line=cls._line_from_node_check(text),
                            message=cls._first_meaningful_line(text) or "JavaScript syntax check failed.",
                            details=cls._bounded(text, 1600),
                        )
                    )
            tool_status["node_check"] = {"status": "checked", "file_count": min(len(js_files), 120), "truncated": len(js_files) > 120}
        else:
            tool_status["node_check"] = {"status": "unavailable", "message": "node was not found on PATH"}
        stream("node_check", tool_status["node_check"]["status"], file_count=min(len(js_files), 120), issue_count=len([item for item in items if item.get("source") == "node_check"]))

        selector_items = cls._selector_diagnostics(root=root, js_files=js_files[:120])
        items.extend(selector_items)
        stream("selector_static", "completed", issue_count=len(selector_items))
        api_items = cls._api_route_diagnostics(root=root, js_files=js_files[:120])
        items.extend(api_items)
        stream("api_route_static", "completed", issue_count=len(api_items))

        if include_optional_tools:
            cls._optional_python_tools(root=root, items=items, tool_status=tool_status)
            cls._optional_typescript_tools(root=root, ts_files=ts_files, items=items, tool_status=tool_status)
        else:
            tool_status["ruff"] = {"status": "skipped"}
            tool_status["pyright"] = {"status": "skipped"}
            tool_status["tsserver"] = {"status": "skipped"}
            tool_status["tsc"] = {"status": "skipped"}
        stream("pyright", tool_status.get("pyright", {}).get("status", "unknown"), issue_count=len([item for item in items if item.get("source") == "pyright"]))
        stream("tsserver", tool_status.get("tsserver", {}).get("status", "unknown"))
        stream("tsc", tool_status.get("tsc", {}).get("status", "unknown"), issue_count=len([item for item in items if item.get("source") == "tsc"]))

        error_count = sum(1 for item in items if item.get("severity") == "error")
        warning_count = sum(1 for item in items if item.get("severity") == "warning")
        stream("complete", "failed" if error_count else "passed", error_count=error_count, warning_count=warning_count)
        return {
            "schema": "grounded.lsp_diagnostics.v1",
            "tool": "lsp.diagnostics",
            "engine": "grounded.lsp.v2",
            "status": "failed" if error_count else "passed",
            "items": items[:200],
            "tool_status": tool_status,
            "diagnostic_stream": diagnostic_stream,
            "error_count": error_count,
            "warning_count": warning_count,
            "changed_only": changed_only,
            "changed_files": changed_files or [],
            "targets": targets or [],
        }

    @classmethod
    def symbol_context(cls, *, root: Path, query: str = "", targets: list[str] | None = None, limit: int = 80) -> dict[str, Any]:
        query_lower = str(query or "").strip().lower()
        symbols: list[dict[str, Any]] = []
        for path in cls._selected_files(root=root, targets=targets):
            if path.suffix.lower() not in {".py", ".js", ".mjs", ".ts", ".tsx"}:
                continue
            for symbol in cls._symbols_for_file(root=root, path=path):
                if query_lower and query_lower not in symbol["name"].lower() and query_lower not in symbol["path"].lower():
                    continue
                symbols.append(symbol)
                if len(symbols) >= limit:
                    break
            if len(symbols) >= limit:
                break
        return {"schema": "grounded.lsp_symbol_context.v1", "tool": "lsp.symbol_context", "query": query, "items": symbols}

    @classmethod
    def find_references(cls, *, root: Path, symbol: str, targets: list[str] | None = None, limit: int = 80) -> dict[str, Any]:
        needle = str(symbol or "").strip()
        if not needle:
            return {"schema": "grounded.lsp_find_references.v1", "tool": "lsp.find_references", "symbol": needle, "items": []}
        pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(needle)}(?![A-Za-z0-9_$])")
        items: list[dict[str, Any]] = []
        for path in cls._selected_files(root=root, targets=targets):
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                match = pattern.search(line)
                if not match:
                    continue
                items.append(
                    {
                        "path": cls._relative(root, path),
                        "line": line_no,
                        "column": match.start() + 1,
                        "excerpt": line.strip()[:240],
                        "jump": cls._jump(root, path, line_no, match.start() + 1),
                    }
                )
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        return {"schema": "grounded.lsp_find_references.v1", "tool": "lsp.find_references", "symbol": needle, "items": items}

    @classmethod
    def definition(cls, *, root: Path, symbol: str, targets: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        needle = str(symbol or "").strip()
        if not needle:
            return {"schema": "grounded.lsp_definition.v1", "tool": "lsp.definition", "symbol": needle, "items": []}
        exact: list[dict[str, Any]] = []
        fuzzy: list[dict[str, Any]] = []
        for path in cls._selected_files(root=root, targets=targets):
            if path.suffix.lower() not in {".py", ".js", ".mjs", ".ts", ".tsx"}:
                continue
            for symbol_item in cls._symbols_for_file(root=root, path=path):
                if symbol_item["name"] == needle:
                    exact.append({**symbol_item, "definition_kind": "exact"})
                elif needle.lower() in symbol_item["name"].lower():
                    fuzzy.append({**symbol_item, "definition_kind": "fuzzy"})
                if len(exact) >= limit:
                    break
            if len(exact) >= limit:
                break
        items = (exact + fuzzy)[:limit]
        return {"schema": "grounded.lsp_definition.v1", "tool": "lsp.definition", "symbol": needle, "items": items}

    @classmethod
    def route_static_context(cls, *, root: Path, targets: list[str] | None = None) -> dict[str, Any]:
        route_root = root / "miniapp" / "app" / "routes"
        declared = sorted({" ".join(route) for route in extract_declared_routes(route_root, api_only=False)})
        api_declared = sorted({" ".join(route) for route in extract_declared_routes(route_root, api_only=True)})
        scan = semantic_scan(root=root, targets=targets or ["miniapp/app"])
        frontend_refs: list[dict[str, Any]] = []
        for path in cls._selected_files(root=root, targets=targets or ["miniapp/app/static"]):
            if path.suffix.lower() not in {".js", ".mjs"}:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for method, api_path in sorted(extract_frontend_api_refs(content)):
                normalized = (method.upper(), normalize_api_path(api_path))
                frontend_refs.append(
                    {
                        "method": normalized[0],
                        "path": normalized[1],
                        "file": cls._relative(root, path),
                        "declared": f"{normalized[0]} {normalized[1]}" in api_declared,
                    }
                )
        return {
            "schema": "grounded.lsp_route_static_context.v1",
            "tool": "lsp.route_static_context",
            "routes": declared[:120],
            "api_routes": api_declared[:120],
            "frontend_api_refs": frontend_refs[:120],
            "semantic_graph": scan.get("graph") or {},
        }

    @classmethod
    def route_graph(cls, *, root: Path, targets: list[str] | None = None) -> dict[str, Any]:
        context = cls.route_static_context(root=root, targets=targets)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()

        def add_node(node: dict[str, Any]) -> None:
            node_id = str(node.get("id") or "")
            if not node_id or node_id in node_ids:
                return
            node_ids.add(node_id)
            nodes.append(node)

        for route in context.get("routes") or []:
            route_text = str(route)
            add_node({"id": f"route:{route_text}", "kind": "backend_route", "label": route_text, "route": route_text, "api": route_text.startswith(("GET /api", "POST /api", "PUT /api", "PATCH /api", "DELETE /api"))})
        for api_route in context.get("api_routes") or []:
            add_node({"id": f"api:{api_route}", "kind": "api_route", "label": str(api_route), "route": str(api_route), "api": True})
        for ref in context.get("frontend_api_refs") or []:
            method = str(ref.get("method") or "GET").upper()
            api_path = str(ref.get("path") or "")
            file_path = str(ref.get("file") or "")
            call_id = f"frontend:{file_path}:{method} {api_path}"
            route_id = f"api:{method} {api_path}"
            declared = bool(ref.get("declared"))
            add_node({"id": call_id, "kind": "frontend_api_call", "label": f"{file_path} -> {method} {api_path}", "file": file_path, "method": method, "path": api_path})
            if not declared:
                add_node({"id": route_id, "kind": "missing_api_route", "label": f"{method} {api_path}", "route": f"{method} {api_path}", "api": True, "missing": True})
            edges.append({"from": call_id, "to": route_id, "kind": "calls", "status": "resolved" if declared else "missing", "file": file_path, "method": method, "path": api_path})
        missing_edges = [edge for edge in edges if edge.get("status") == "missing"]
        return {
            "schema": "grounded.lsp_route_graph.v1",
            "tool": "lsp.route_graph",
            "nodes": nodes[:180],
            "edges": edges[:240],
            "missing_edges": missing_edges[:80],
            "summary": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "missing_edge_count": len(missing_edges),
                "api_route_count": len(context.get("api_routes") or []),
                "frontend_ref_count": len(context.get("frontend_api_refs") or []),
            },
            "static_context": context,
        }

    @classmethod
    def _selected_files(
        cls,
        *,
        root: Path,
        targets: list[str] | None = None,
        changed_files: list[str] | None = None,
        changed_only: bool = False,
    ) -> list[Path]:
        if changed_only:
            candidate_paths = [root / str(path).strip().lstrip("./") for path in (changed_files or []) if str(path or "").strip()]
            return sorted(path for path in candidate_paths if path.is_file() and cls._is_visible(root, path))
        normalized_targets = [str(target or "").strip().replace("\\", "/").rstrip("/") for target in (targets or []) if str(target or "").strip()]
        candidates: list[Path] = []
        if normalized_targets:
            for target in normalized_targets:
                target_path = root / target.lstrip("./")
                if target_path.is_file() and cls._is_visible(root, target_path):
                    candidates.append(target_path)
                elif target_path.is_dir():
                    candidates.extend(path for path in target_path.rglob("*") if path.is_file() and cls._is_visible(root, path))
        else:
            candidates = [path for path in root.rglob("*") if path.is_file() and cls._is_visible(root, path)]
        return sorted(dict.fromkeys(path for path in candidates if path.suffix.lower() in SOURCE_SUFFIXES), key=lambda item: cls._relative(root, item))[:400]

    @classmethod
    def _selector_diagnostics(cls, *, root: Path, js_files: list[Path]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for js_file in js_files:
            relative = cls._relative(root, js_file)
            try:
                js_content = js_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            html_ids = role_surface_dom_ids(root, relative)
            if not html_ids:
                continue
            for dom_id in sorted(extract_js_dom_ids(js_content) - html_ids)[:20]:
                line, column = cls._line_column_for_text(js_content, f"#{dom_id}")
                items.append(
                    cls._diagnostic_item(
                        root=root,
                        source="selector_static",
                        severity="error",
                        file_path=js_file,
                        line=line,
                        column=column,
                        message=f"JavaScript references missing DOM id #{dom_id}.",
                        code="missing_dom_id",
                        data={"selector": f"#{dom_id}"},
                    )
                )
        return items

    @classmethod
    def _api_route_diagnostics(cls, *, root: Path, js_files: list[Path]) -> list[dict[str, Any]]:
        declared_routes = extract_declared_routes(root / "miniapp" / "app" / "routes", api_only=True)
        items: list[dict[str, Any]] = []
        for js_file in js_files:
            try:
                js_content = js_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for method, api_path in sorted(extract_frontend_api_refs(js_content)):
                normalized = (method.upper(), normalize_api_path(api_path))
                if normalized in declared_routes or cls._compatible_template(normalized, declared_routes):
                    continue
                line, column = cls._line_column_for_text(js_content, api_path)
                items.append(
                    cls._diagnostic_item(
                        root=root,
                        source="api_route_static",
                        severity="error",
                        file_path=js_file,
                        line=line,
                        column=column,
                        message=f"Frontend calls undeclared API route {normalized[0]} {normalized[1]}.",
                        code="missing_backend_route",
                        data={"method": normalized[0], "path": normalized[1]},
                    )
                )
        return items

    @classmethod
    def _optional_python_tools(cls, *, root: Path, items: list[dict[str, Any]], tool_status: dict[str, Any]) -> None:
        app_dir = root / "miniapp" / "app"
        ruff = shutil.which("ruff")
        if ruff and app_dir.exists():
            result = subprocess.run([ruff, "check", str(app_dir)], text=True, capture_output=True, timeout=12)
            tool_status["ruff"] = {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode}
            if result.returncode != 0:
                items.append(cls._diagnostic_item(root=root, source="ruff", severity="warning", file_path=app_dir, message=cls._bounded(result.stdout or result.stderr, 1800)))
        else:
            tool_status["ruff"] = {"status": "unavailable"}
        pyright = shutil.which("pyright")
        if pyright and app_dir.exists():
            result = subprocess.run([pyright, str(app_dir), "--outputjson"], text=True, capture_output=True, timeout=16)
            tool_status["pyright"] = {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "mode": "pyright_json"}
            if result.returncode != 0:
                parsed_items = cls._pyright_json_diagnostics(root=root, raw=result.stdout or result.stderr)
                if parsed_items:
                    items.extend(parsed_items)
                else:
                    items.append(cls._diagnostic_item(root=root, source="pyright", severity="warning", file_path=app_dir, message=cls._bounded(result.stdout or result.stderr, 1800)))
        else:
            tool_status["pyright"] = {"status": "unavailable"}

    @classmethod
    def _optional_typescript_tools(cls, *, root: Path, ts_files: list[Path], items: list[dict[str, Any]], tool_status: dict[str, Any]) -> None:
        tsserver = shutil.which("tsserver")
        tool_status["tsserver"] = {"status": "available" if tsserver else "unavailable", "mode": "protocol_status"}
        tsc = shutil.which("tsc")
        tsconfig = cls._find_tsconfig(root)
        if not tsc:
            tool_status["tsc"] = {"status": "unavailable"}
            return
        if tsconfig is None:
            tool_status["tsc"] = {"status": "skipped", "reason": "tsconfig.json not found", "file_count": len(ts_files)}
            return
        result = subprocess.run([tsc, "--noEmit", "--pretty", "false", "--project", str(tsconfig)], cwd=root, text=True, capture_output=True, timeout=18)
        tool_status["tsc"] = {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "project": cls._relative(root, tsconfig)}
        if result.returncode != 0:
            items.extend(cls._tsc_diagnostics(root=root, raw=result.stdout or result.stderr))

    @classmethod
    def _find_tsconfig(cls, root: Path) -> Path | None:
        for candidate in (root / "miniapp" / "tsconfig.json", root / "tsconfig.json", root / "miniapp" / "app" / "tsconfig.json"):
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def _pyright_json_diagnostics(cls, *, root: Path, raw: str) -> list[dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            return []
        diagnostics = payload.get("generalDiagnostics") if isinstance(payload, dict) else []
        if not isinstance(diagnostics, list):
            return []
        items: list[dict[str, Any]] = []
        for diagnostic in diagnostics[:120]:
            if not isinstance(diagnostic, dict):
                continue
            file_path = cls._diagnostic_path(root, str(diagnostic.get("file") or ""))
            range_payload = diagnostic.get("range") if isinstance(diagnostic.get("range"), dict) else {}
            start = range_payload.get("start") if isinstance(range_payload.get("start"), dict) else {}
            line = int(start.get("line") or 0) + 1
            column = int(start.get("character") or 0) + 1
            severity = str(diagnostic.get("severity") or "warning")
            if severity == "information":
                severity = "info"
            items.append(
                cls._diagnostic_item(
                    root=root,
                    source="pyright",
                    severity="error" if severity == "error" else "warning" if severity == "warning" else "info",
                    file_path=file_path,
                    line=line,
                    column=column,
                    message=str(diagnostic.get("message") or "Pyright diagnostic."),
                    code=str(diagnostic.get("rule") or diagnostic.get("code") or ""),
                    data={"rule": diagnostic.get("rule"), "severity": diagnostic.get("severity")},
                )
            )
        return items

    @classmethod
    def _tsc_diagnostics(cls, *, root: Path, raw: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pattern = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s+(?P<severity>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.+)$")
        for line_text in str(raw or "").splitlines()[:160]:
            match = pattern.match(line_text.strip())
            if not match:
                continue
            file_path = cls._diagnostic_path(root, match.group("file"))
            items.append(
                cls._diagnostic_item(
                    root=root,
                    source="tsc",
                    severity="error" if match.group("severity") == "error" else "warning",
                    file_path=file_path,
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    message=match.group("message"),
                    code=match.group("code"),
                )
            )
        return items

    @staticmethod
    def _diagnostic_path(root: Path, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return root / raw_path

    @classmethod
    def _symbols_for_file(cls, *, root: Path, path: Path) -> list[dict[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        patterns = [
            ("python_function", re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)),
            ("python_class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:(]", re.M)),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)),
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
            ("const", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)),
        ]
        symbols: list[dict[str, Any]] = []
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                line, column = cls._line_column_for_offset(text, match.start(1))
                symbols.append(
                    {
                        "path": cls._relative(root, path),
                        "kind": kind,
                        "name": match.group(1),
                        "line": line,
                        "column": column,
                        "excerpt": cls._line_at(text, line),
                        "jump": cls._jump(root, path, line, column),
                    }
                )
        return sorted(symbols, key=lambda item: (item["path"], item["line"], item["name"]))[:80]

    @staticmethod
    def _compatible_template(ref: tuple[str, str], declared_routes: set[tuple[str, str]]) -> bool:
        method, path = ref
        ref_parts = [part for part in path.split("/") if part]
        for declared_method, declared_path in declared_routes:
            if method != declared_method:
                continue
            declared_parts = [part for part in declared_path.split("/") if part]
            if len(ref_parts) != len(declared_parts):
                continue
            if all(left == right or (right.startswith("{") and right.endswith("}")) for left, right in zip(ref_parts, declared_parts)):
                return True
        return False

    @staticmethod
    def _diagnostic_item(
        *,
        root: Path,
        source: str,
        severity: str,
        file_path: Path,
        message: str,
        line: int | None = None,
        column: int | None = None,
        code: str = "",
        details: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        relative = LspToolService._relative(root, file_path)
        payload: dict[str, Any] = {
            "source": source,
            "severity": severity,
            "path": relative,
            "file": relative,
            "message": message,
            "jump": LspToolService._jump(root, file_path, line or 1, column or 1),
        }
        if line:
            payload["line"] = line
        if column:
            payload["column"] = column
        if code:
            payload["code"] = code
        if details:
            payload["details"] = details
        if data:
            payload["data"] = data
        return payload

    @staticmethod
    def _jump(root: Path, path: Path, line: int, column: int = 1) -> dict[str, Any]:
        relative = LspToolService._relative(root, path)
        return {"path": relative, "line": max(1, int(line or 1)), "column": max(1, int(column or 1)), "label": f"{relative}:{max(1, int(line or 1))}"}

    @staticmethod
    def _is_visible(root: Path, path: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        return not any(part in IGNORED_PARTS for part in relative.parts)

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _first_meaningful_line(text: str) -> str:
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:300]
        return ""

    @staticmethod
    def _bounded(text: str, limit: int) -> str:
        value = str(text or "").strip()
        return value[:limit]

    @staticmethod
    def _line_from_python_compile(text: str) -> int | None:
        match = re.search(r'File\s+"[^"]+",\s+line\s+(\d+)', str(text or ""))
        return int(match.group(1)) if match else None

    @staticmethod
    def _line_from_node_check(text: str) -> int | None:
        match = re.search(r":(\d+)\s*$", str(text or "").splitlines()[0] if str(text or "").splitlines() else "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _line_column_for_text(text: str, needle: str) -> tuple[int | None, int | None]:
        index = str(text or "").find(str(needle or ""))
        if index < 0:
            return None, None
        return LspToolService._line_column_for_offset(text, index)

    @staticmethod
    def _line_column_for_offset(text: str, offset: int) -> tuple[int, int]:
        prefix = text[: max(0, offset)]
        line = prefix.count("\n") + 1
        column = len(prefix.rsplit("\n", 1)[-1]) + 1
        return line, column

    @staticmethod
    def _line_at(text: str, line: int) -> str:
        lines = str(text or "").splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()[:240]
        return ""
