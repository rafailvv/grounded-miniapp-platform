from __future__ import annotations

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
    role_html_ids,
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
        items: list[dict[str, Any]] = []
        tool_status: dict[str, Any] = {}

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

        items.extend(cls._selector_diagnostics(root=root, js_files=js_files[:120]))
        items.extend(cls._api_route_diagnostics(root=root, js_files=js_files[:120]))

        if include_optional_tools:
            cls._optional_python_tools(root=root, items=items, tool_status=tool_status)
        else:
            tool_status["ruff"] = {"status": "skipped"}
            tool_status["pyright"] = {"status": "skipped"}

        error_count = sum(1 for item in items if item.get("severity") == "error")
        warning_count = sum(1 for item in items if item.get("severity") == "warning")
        return {
            "schema": "grounded.lsp_diagnostics.v1",
            "tool": "lsp.diagnostics",
            "status": "failed" if error_count else "passed",
            "items": items[:200],
            "tool_status": tool_status,
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
            html_ids = role_html_ids(root, relative)
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
            result = subprocess.run([pyright, str(app_dir)], text=True, capture_output=True, timeout=16)
            tool_status["pyright"] = {"status": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode}
            if result.returncode != 0:
                items.append(cls._diagnostic_item(root=root, source="pyright", severity="warning", file_path=app_dir, message=cls._bounded(result.stdout or result.stderr, 1800)))
        else:
            tool_status["pyright"] = {"status": "unavailable"}

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
