from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


def _relative_files(root: Path, targets: list[str], suffixes: tuple[str, ...], max_files: int = 80) -> list[Path]:
    paths: list[Path] = []
    normalized_targets = [target.rstrip("/") for target in targets if target.strip()]
    for path in sorted(root.rglob("*")):
        if len(paths) >= max_files:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if normalized_targets and not any(relative == target or relative.startswith(target + "/") for target in normalized_targets):
            continue
        if any(part in {"node_modules", "__pycache__", ".git", "dist", "build"} for part in path.parts):
            continue
        paths.append(path)
    return paths


def _scan_python(path: Path, root: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return {"path": path.relative_to(root).as_posix(), "error": str(exc)[:240]}
    imports: list[str] = []
    routes: list[dict[str, object]] = []
    definitions: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node.name)
            decorators = getattr(node, "decorator_list", [])
            for decorator in decorators:
                text = ast.unparse(decorator) if hasattr(ast, "unparse") else ""
                if re.search(r"\.(get|post|patch|put|delete)\(", text):
                    routes.append({"name": node.name, "decorator": text[:220], "line": getattr(node, "lineno", None)})
    return {
        "path": path.relative_to(root).as_posix(),
        "imports": sorted(set(imports))[:20],
        "definitions": definitions[:30],
        "routes": routes[:30],
    }


def _scan_html(path: Path, root: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path.relative_to(root).as_posix(), "error": str(exc)[:240]}
    forms: list[dict[str, object]] = []
    for match in re.finditer(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", text, flags=re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs")
        body = match.group("body")
        form_id = re.search(r"\bid=(['\"])(?P<id>.*?)\1", attrs, flags=re.IGNORECASE)
        names = re.findall(r"\bname=(['\"])(.*?)\1", body, flags=re.IGNORECASE)
        buttons = re.findall(r"<button\b[^>]*>(.*?)</button>", body, flags=re.IGNORECASE | re.DOTALL)
        forms.append(
            {
                "id": form_id.group("id") if form_id else "",
                "field_names": [name for _, name in names][:20],
                "button_count": len(buttons),
            }
        )
    ids = re.findall(r"\bid=(['\"])(.*?)\1", text, flags=re.IGNORECASE)
    classes = re.findall(r"\bclass=(['\"])(.*?)\1", text, flags=re.IGNORECASE)
    return {
        "path": path.relative_to(root).as_posix(),
        "forms": forms[:20],
        "ids": [value for _, value in ids][:40],
        "classes": sorted({part for _, value in classes for part in value.split()})[:60],
        "scripts": re.findall(r"<script\b[^>]*src=(['\"])(.*?)\1", text, flags=re.IGNORECASE)[:12],
        "stylesheets": re.findall(r"<link\b[^>]*href=(['\"])(.*?)\1", text, flags=re.IGNORECASE)[:12],
    }


def _scan_js(path: Path, root: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path.relative_to(root).as_posix(), "error": str(exc)[:240]}
    selectors = re.findall(r"(?:querySelector|getElementById)\((['\"])(.*?)\1\)", text)
    listeners = re.findall(r"\.addEventListener\((['\"])(.*?)\1", text)
    fetches = re.findall(r"fetch\(([^,\n)]+)", text)
    return {
        "path": path.relative_to(root).as_posix(),
        "selectors": [value for _, value in selectors][:50],
        "event_listeners": [value for _, value in listeners][:30],
        "fetch_refs": [value.strip(" `\"'")[:160] for value in fetches[:30]],
    }


def _scan_css(path: Path, root: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path.relative_to(root).as_posix(), "error": str(exc)[:240]}
    classes = sorted(set(re.findall(r"\.([A-Za-z_-][A-Za-z0-9_-]*)\s*[{,:]", text)))[:120]
    media_queries = re.findall(r"@media[^{]+", text, flags=re.IGNORECASE)[:20]
    fixed_width_risks = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if any(prop in lowered for prop in ("width:", "min-width:", "grid-template-columns:")) and re.search(r"\b(?:[5-9]\d{2,}|[1-9]\d{3,})px\b", lowered):
            fixed_width_risks.append({"line": line_no, "text": line.strip()[:220]})
    return {
        "path": path.relative_to(root).as_posix(),
        "classes": classes,
        "media_queries": media_queries,
        "has_mobile_rules": bool(re.search(r"max-width\s*:\s*(?:4[0-9]{2}|3[0-9]{2})px", text, flags=re.IGNORECASE)),
        "has_overflow_guard": "overflow-x" in text.lower() or "min-width: 0" in text.lower(),
        "fixed_width_risks": fixed_width_risks[:20],
    }


def _scan_generated_test(path: Path, root: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": path.relative_to(root).as_posix(), "error": str(exc)[:240]}
    return {
        "path": path.relative_to(root).as_posix(),
        "api_refs": sorted(set(re.findall(r"['\"](/api/[^'\"]+)['\"]", text)))[:40],
        "selector_refs": sorted(set(re.findall(r"['\"](#?[A-Za-z][A-Za-z0-9_-]+)['\"]", text)))[:80],
        "mentions_browser": any(token in text.lower() for token in ("playwright", "page.", "locator", "click(")),
        "assertion_count": len(re.findall(r"\b(?:assert|expect)\b", text)),
    }


def _build_graph(scan: dict[str, Any]) -> dict[str, Any]:
    routes = [
        route
        for item in scan.get("python", [])
        if isinstance(item, dict)
        for route in item.get("routes", [])
        if isinstance(route, dict)
    ]
    forms = [
        {"path": item.get("path"), **form}
        for item in scan.get("html", [])
        if isinstance(item, dict)
        for form in item.get("forms", [])
        if isinstance(form, dict)
    ]
    selectors = sorted(
        set(
            selector
            for item in scan.get("javascript", [])
            if isinstance(item, dict)
            for selector in item.get("selectors", [])
            if isinstance(selector, str)
        )
    )
    fetch_refs = sorted(
        set(
            ref
            for item in scan.get("javascript", [])
            if isinstance(item, dict)
            for ref in item.get("fetch_refs", [])
            if isinstance(ref, str)
        )
    )
    html_classes = sorted(
        set(
            class_name
            for item in scan.get("html", [])
            if isinstance(item, dict)
            for class_name in item.get("classes", [])
            if isinstance(class_name, str)
        )
    )
    css_classes = sorted(
        set(
            class_name
            for item in scan.get("css", [])
            if isinstance(item, dict)
            for class_name in item.get("classes", [])
            if isinstance(class_name, str)
        )
    )
    missing_css_classes = [class_name for class_name in html_classes if class_name not in css_classes][:80]
    return {
        "route_count": len(routes),
        "form_count": len(forms),
        "selector_count": len(selectors),
        "fetch_count": len(fetch_refs),
        "routes": routes[:40],
        "forms": forms[:40],
        "selectors": selectors[:80],
        "fetch_refs": fetch_refs[:40],
        "missing_css_classes": missing_css_classes,
        "mobile_risks": [
            {"path": item.get("path"), "fixed_width_risks": item.get("fixed_width_risks")}
            for item in scan.get("css", [])
            if isinstance(item, dict) and item.get("fixed_width_risks")
        ][:20],
    }


def semantic_scan(*, root: Path, targets: list[str]) -> dict[str, object]:
    python_files = _relative_files(root, targets, (".py",), max_files=50)
    html_files = _relative_files(root, targets, (".html",), max_files=80)
    js_files = _relative_files(root, targets, (".js", ".mjs"), max_files=80)
    css_files = _relative_files(root, targets, (".css",), max_files=80)
    test_files = [path for path in _relative_files(root, ["miniapp/tests"], (".py", ".js", ".mjs"), max_files=20)]
    scan = {
        "tool": "semantic_scan",
        "targets": targets,
        "python": [_scan_python(path, root) for path in python_files],
        "html": [_scan_html(path, root) for path in html_files],
        "javascript": [_scan_js(path, root) for path in js_files],
        "css": [_scan_css(path, root) for path in css_files],
        "generated_tests": [_scan_generated_test(path, root) for path in test_files],
    }
    scan["graph"] = _build_graph(scan)
    return scan
