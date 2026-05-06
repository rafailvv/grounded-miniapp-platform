from __future__ import annotations

import ast
import json
import posixpath
import re
from pathlib import Path
from typing import Any

from app.models.artifacts import ValidationIssue
from app.services.platform_shell import BASE_STYLESHEET_HREF, PAGE_SHELL_CLASS, PREVIEW_BRIDGE_SRC
from app.validators.static_analysis import extract_html_ids, extract_js_dom_ids, extract_script_refs, role_surface_dom_ids


class BuildValidator:
    _ROUTE_DECORATOR_RE = re.compile(
        r"@router\.(?P<method>get|post|put|patch|delete)\(\s*['\"](?P<path>[^'\"]*)['\"]",
        re.IGNORECASE,
    )
    _ROUTER_PREFIX_RE = re.compile(r"APIRouter\(\s*prefix\s*=\s*['\"](?P<prefix>[^'\"]*)['\"]")

    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        issues.extend(self._required_shell_issues(workspace_path))
        issues.extend(self._static_shell_issues(workspace_path))
        issues.extend(self._route_module_import_issues(workspace_path))
        issues.extend(self._duplicate_route_issues(workspace_path))
        return self._dedupe(issues)

    @staticmethod
    def _required_shell_issues(workspace_path: Path) -> list[ValidationIssue]:
        required_files = [
            workspace_path / "miniapp" / "app" / "main.py",
            workspace_path / "miniapp" / "requirements.txt",
            workspace_path / "docker" / "docker-compose.yml",
        ]
        issues: list[ValidationIssue] = []
        for file_path in required_files:
            if file_path.exists():
                continue
            issues.append(
                ValidationIssue(
                    code="build.missing_entrypoint",
                    message=f"Required shell asset or entrypoint is missing: {file_path.name}",
                    severity="high",
                    location=str(file_path.relative_to(workspace_path)),
                )
            )
        return issues

    def _static_shell_issues(self, workspace_path: Path) -> list[ValidationIssue]:
        manifest_path = workspace_path / "miniapp/app/generated/route_manifest.json"
        if not manifest_path.exists():
            return [
                ValidationIssue(
                    code="build.missing_route_manifest",
                    message="Generated route manifest is missing.",
                    severity="high",
                    location="miniapp/app/generated/route_manifest.json",
                )
            ]
        manifest = self._read_json(manifest_path)
        pages = self._manifest_pages(workspace_path, manifest if isinstance(manifest, dict) else {})
        if not pages:
            pages = self._filesystem_static_pages(workspace_path)
        issues: list[ValidationIssue] = []
        seen_routes: dict[str, str] = {}
        for page in pages:
            route_path = str(page.get("route_path") or "").strip() or "/"
            route_key = route_path.rstrip("/") or "/"
            file_key = self._canonical_manifest_file_ref(str(page.get("file_path") or ""))
            existing_file = seen_routes.get(route_key)
            if existing_file is not None and existing_file != file_key:
                issues.append(
                    ValidationIssue(
                        code="build.duplicate_static_route",
                        message=(
                            f"Duplicate static route declared: {route_path} maps to both "
                            f"{existing_file or '<missing>'} and {file_key or '<missing>'}"
                        ),
                        severity="high",
                        location="miniapp/app/generated/route_manifest.json",
                    )
                )
                continue
            if existing_file is not None:
                continue
            seen_routes[route_key] = file_key
            issues.extend(self._static_page_issues(workspace_path, page))
        return issues

    @staticmethod
    def _canonical_manifest_file_ref(file_ref: str) -> str:
        normalized = str(file_ref or "").strip().replace("\\", "/").lstrip("/")
        for prefix in ("miniapp/app/", "app/"):
            if normalized.startswith(prefix):
                normalized = normalized.removeprefix(prefix)
        return posixpath.normpath(normalized) if normalized else ""

    @classmethod
    def _manifest_pages(cls, workspace_path: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        top_level_routes = manifest.get("routes")
        if isinstance(top_level_routes, dict):
            for route_path, file_path in top_level_routes.items():
                route = str(route_path or "").strip()
                file_ref = str(file_path or "").strip()
                if route and file_ref:
                    pages.append({"route_path": route, "file_path": file_ref})
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), dict) else {}
        for route_path, file_path in roles.items():
            route = str(route_path or "").strip()
            if isinstance(file_path, str):
                file_ref = str(file_path or "").strip()
                if route and file_ref:
                    pages.append({"route_path": route, "file_path": file_ref})
                continue
            if isinstance(file_path, list):
                for file_ref_raw in file_path:
                    file_ref = str(file_ref_raw or "").strip()
                    if route and file_ref.endswith(".html"):
                        pages.append({"route_path": route, "file_path": file_ref})
        for role_raw, payload in roles.items():
            if not isinstance(payload, dict):
                continue
            role = cls._normalize_manifest_role_key(str(role_raw or ""))
            single_file = str(payload.get("file") or payload.get("file_path") or "").strip()
            if single_file:
                single_route = str(payload.get("route") or payload.get("route_path") or payload.get("page") or payload.get("primary_page") or "").strip()
                pages.append(
                    {
                        "role": role,
                        "route_path": cls._normalize_manifest_role_route(role, single_route),
                        "file_path": single_file,
                    }
                )
            route_map = payload.get("routes")
            if isinstance(route_map, list):
                for route_item in route_map:
                    route = cls._normalize_manifest_role_route(role, str(route_item or "").strip())
                    if route:
                        pages.append({"role": role, "route_path": route, "file_path": f"static/{role}/index.html"})
                route_map = {}
            if not isinstance(route_map, dict):
                route_map = {
                    str(route_path): str(file_path)
                    for route_path, file_path in payload.items()
                    if isinstance(file_path, str) and str(route_path) not in {"pages", "routes", "page", "file", "file_path", "route", "route_path", "primary_page"}
                }
            if isinstance(route_map, dict):
                for route_path, file_path in route_map.items():
                    route = cls._normalize_manifest_role_route(role, str(route_path or "").strip())
                    file_ref = str(file_path or "").strip()
                    if route and file_ref:
                        pages.append({"role": role, "route_path": route, "file_path": file_ref})
            for page in payload.get("pages") or []:
                if isinstance(page, str):
                    pages.append(
                        {
                            "role": role,
                            "route_path": cls._normalize_manifest_role_route(role, page),
                            "file_path": f"static/{role}/index.html",
                        }
                    )
                    continue
                if isinstance(page, dict):
                    pages.append({"role": role, **page})
        shared = manifest.get("shared")
        if isinstance(shared, dict):
            for page in shared.get("pages") or []:
                if isinstance(page, dict):
                    pages.append(dict(page))
        if pages:
            return pages
        return cls._filesystem_static_pages(workspace_path)

    @staticmethod
    def _normalize_manifest_role_key(role_raw: str) -> str:
        role = str(role_raw or "").strip().strip("/")
        return role or role_raw

    @staticmethod
    def _normalize_manifest_role_route(role: str, route_path: str) -> str:
        route = str(route_path or "").strip()
        if not route or route in {"root", "index", "/"}:
            return f"/{role}" if role else route
        if route.startswith("/"):
            return route.rstrip("/") or f"/{role}"
        return f"/{role}/{route}".rstrip("/") if role else f"/{route}".rstrip("/")

    @staticmethod
    def _filesystem_static_pages(workspace_path: Path) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        static_root = workspace_path / "miniapp/app/static"
        if not static_root.exists():
            return pages
        for html_path in sorted(static_root.rglob("index.html")):
            if "shared" in html_path.parts:
                continue
            relative_path = html_path.relative_to(workspace_path).as_posix()
            static_relative = html_path.relative_to(static_root).as_posix()
            route_path = "/" + static_relative.removesuffix("/index.html").removesuffix("index.html").strip("/")
            pages.append(
                {
                    "route_path": route_path or "/",
                    "file_path": relative_path,
                    "style_path": relative_path.replace("index.html", "styles.css"),
                    "script_path": relative_path.replace("index.html", "app.js"),
                }
            )
        return pages

    def _static_page_issues(self, workspace_path: Path, page: dict[str, Any]) -> list[ValidationIssue]:
        file_path_raw = self._workspace_static_path(str(page.get("file_path") or "").strip().replace("\\", "/"))
        if not file_path_raw:
            return []
        file_path = workspace_path / file_path_raw
        if not file_path.exists():
            return [
                ValidationIssue(
                    code="build.missing_static_page",
                    message=f"Static page is missing: {Path(file_path_raw).name}",
                    severity="high",
                    location=file_path_raw,
                )
            ]
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            return [
                ValidationIssue(
                    code="build.static_page_unreadable",
                    message=f"Static page could not be read: {exc}",
                    severity="high",
                    location=file_path_raw,
                )
            ]
        issues: list[ValidationIssue] = []
        if BASE_STYLESHEET_HREF not in content:
            issues.append(self._issue("build.page_missing_shell_style_link", "Page does not reference the shared shell stylesheet.", file_path_raw))
        if PREVIEW_BRIDGE_SRC not in content:
            issues.append(self._issue("build.page_missing_preview_bridge", "Page does not reference the preview bridge runtime.", file_path_raw))
        if PAGE_SHELL_CLASS not in content:
            issues.append(self._issue("build.page_missing_shell_root", "Page does not include the platform shell root container.", file_path_raw))
        issues.extend(self._asset_reference_issues(workspace_path, file_path_raw, content, page))
        issues.extend(self._dom_reference_issues(workspace_path, file_path_raw, content, page))
        return issues

    def _asset_reference_issues(
        self,
        workspace_path: Path,
        file_path_raw: str,
        html_content: str,
        page: dict[str, Any],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for key, label in (("style_path", "CSS"), ("script_path", "JS")):
            asset_path_raw = self._workspace_static_path(str(page.get(key) or "").strip().replace("\\", "/"))
            if not asset_path_raw:
                continue
            href = self._static_asset_href(asset_path_raw)
            if href and href not in html_content:
                issues.append(self._issue("build.page_missing_asset_ref", f"Page does not reference its {label} asset.", file_path_raw))
            if asset_path_raw and not (workspace_path / asset_path_raw).exists():
                issues.append(self._issue("build.missing_static_asset", f"Referenced {label} asset is missing: {Path(asset_path_raw).name}", asset_path_raw))
        for ref in self._html_static_refs(html_content):
            asset_path = workspace_path / f"miniapp/app{ref}"
            if not asset_path.exists():
                issues.append(self._issue("build.broken_static_ref", f"Static asset reference is broken: {ref}", file_path_raw))
        return issues

    @staticmethod
    def _workspace_static_path(path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
        if normalized.startswith("miniapp/app/"):
            return normalized
        if normalized.startswith("app/static/"):
            return f"miniapp/{normalized}"
        if normalized.startswith("static/"):
            return f"miniapp/app/{normalized}"
        return normalized

    def _dom_reference_issues(
        self,
        workspace_path: Path,
        file_path_raw: str,
        html_content: str,
        page: dict[str, Any],
    ) -> list[ValidationIssue]:
        script_paths = self._script_paths_for_page(file_path_raw, html_content, page)
        if not script_paths:
            return []
        issues: list[ValidationIssue] = []
        page_ids = extract_html_ids(html_content)
        for script_path_raw in script_paths:
            script_path = workspace_path / script_path_raw
            if not script_path.exists():
                continue
            try:
                script_content = script_path.read_text(encoding="utf-8")
            except OSError:
                continue
            role_ids = role_surface_dom_ids(workspace_path, script_path_raw)
            html_ids = role_ids or page_ids
            script_ids = self._extract_unsafe_direct_dom_ids(script_content)
            missing_ids = sorted(script_ids - html_ids)
            if not missing_ids:
                continue
            issues.append(
                self._issue(
                    "build.page_script_dom_contract",
                    f"Page role surface is missing DOM ids required by {Path(script_path_raw).name}: {', '.join(missing_ids[:6])}",
                    file_path_raw,
                )
            )
        return issues

    @classmethod
    def _script_paths_for_page(cls, file_path_raw: str, html_content: str, page: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        script_path_raw = cls._workspace_static_path(str(page.get("script_path") or "").strip().replace("\\", "/"))
        if script_path_raw:
            paths.append(script_path_raw)
        for ref in extract_script_refs(html_content):
            resolved = cls._resolve_static_ref(ref, source_path=file_path_raw)
            if resolved and resolved.endswith(".js"):
                paths.append(resolved)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _resolve_static_ref(raw_ref: str, *, source_path: str) -> str | None:
        ref = str(raw_ref or "").strip().split("?", 1)[0].split("#", 1)[0]
        if not ref or ref.startswith(("http://", "https://", "//", "data:")):
            return None
        if ref.startswith("/static/"):
            return f"miniapp/app{ref}"
        if ref.startswith("static/"):
            return f"miniapp/app/{ref}"
        if ref.startswith("/"):
            return None
        source_parent = Path(source_path).parent.as_posix()
        resolved = posixpath.normpath(posixpath.join(source_parent, ref))
        return resolved if resolved.startswith("miniapp/app/static/") else None

    @classmethod
    def _route_module_import_issues(cls, workspace_path: Path) -> list[ValidationIssue]:
        routes_dir = workspace_path / "miniapp/app/routes"
        if not routes_dir.exists():
            return []
        issues: list[ValidationIssue] = []
        for route_file in routes_dir.glob("*.py"):
            module_name = route_file.stem
            try:
                content = route_file.read_text(encoding="utf-8")
                ast.parse(content)
            except SyntaxError as exc:
                issues.append(cls._issue("build.route_syntax_error", f"{route_file.name} has invalid Python syntax: {exc.msg}", str(route_file.relative_to(workspace_path))))
                continue
            except OSError:
                continue
            self_import_markers = (f"from app.routes.{module_name} import ", f"import app.routes.{module_name}")
            if any(marker in content for marker in self_import_markers):
                issues.append(cls._issue("build.route_self_import", f"Route module {route_file.name} imports itself.", str(route_file.relative_to(workspace_path))))
        return issues

    @classmethod
    def _duplicate_route_issues(cls, workspace_path: Path) -> list[ValidationIssue]:
        routes_dir = workspace_path / "miniapp/app/routes"
        if not routes_dir.exists():
            return []
        seen: dict[tuple[str, str], str] = {}
        issues: list[ValidationIssue] = []
        for route_file in routes_dir.glob("*.py"):
            try:
                content = route_file.read_text(encoding="utf-8")
            except OSError:
                continue
            prefix_match = cls._ROUTER_PREFIX_RE.search(content)
            prefix = (prefix_match.group("prefix") if prefix_match else "").rstrip("/")
            for match in cls._ROUTE_DECORATOR_RE.finditer(content):
                method = match.group("method").upper()
                route_path = match.group("path")
                full_path = cls._join_route(prefix, route_path)
                key = (method, full_path)
                location = str(route_file.relative_to(workspace_path))
                if key in seen:
                    issues.append(cls._issue("build.duplicate_runtime_route", f"Duplicate route {method} {full_path} also declared in {seen[key]}.", location))
                else:
                    seen[key] = location
        return issues

    @staticmethod
    def _join_route(prefix: str, route_path: str) -> str:
        route_path = route_path.strip()
        if not prefix:
            return route_path or "/"
        if route_path in {"", "/"}:
            return prefix or "/"
        return f"{prefix}/{route_path.lstrip('/')}"

    @staticmethod
    def _static_asset_href(asset_path_raw: str) -> str:
        normalized = str(asset_path_raw or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        prefix = "miniapp/app/"
        if normalized.startswith(prefix):
            return "/" + normalized[len(prefix):]
        if normalized.startswith("app/"):
            return "/" + normalized
        if normalized.startswith("/"):
            return normalized
        return "/" + normalized

    @staticmethod
    def _html_static_refs(content: str) -> list[str]:
        refs = re.findall(r"""(?:href|src)=["'](/static/[^"']+)["']""", content)
        return list(dict.fromkeys(refs))

    @staticmethod
    def _extract_html_ids(content: str) -> set[str]:
        return extract_html_ids(content)

    @staticmethod
    def _extract_js_dom_ids(content: str) -> set[str]:
        return extract_js_dom_ids(content)

    @staticmethod
    def _extract_unsafe_direct_dom_ids(content: str) -> set[str]:
        unsafe: set[str] = set()
        pattern = re.compile(
            r"""document\.(?:getElementById\(\s*["'](?P<id1>[A-Za-z0-9_-]+)["']\s*\)|querySelector\(\s*["']\#(?P<id2>[A-Za-z0-9_-]+)["']\s*\))\s*\.""",
            re.DOTALL,
        )
        for match in pattern.finditer(str(content or "")):
            dom_id = match.group("id1") or match.group("id2")
            if dom_id:
                unsafe.add(dom_id)
        return unsafe

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | list[Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _issue(code: str, message: str, location: str) -> ValidationIssue:
        return ValidationIssue(code=code, message=message, severity="high", location=location, blocking=True)

    @staticmethod
    def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        deduped: list[ValidationIssue] = []
        seen: set[tuple[str, str, str]] = set()
        for issue in issues:
            key = (issue.code, issue.location, issue.message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped
