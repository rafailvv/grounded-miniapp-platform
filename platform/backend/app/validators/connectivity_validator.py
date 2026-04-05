from __future__ import annotations

import json
from pathlib import Path
import posixpath
import re

from app.models.artifacts import ValidationIssue


ROLE_NAMES = {"client", "specialist", "manager"}
UI_WIRING_MARKERS = (
    "fetch(",
    "xmlhttprequest",
    "axios.",
    "addeventlistener(\"submit\"",
    "addeventlistener('submit'",
    ".onsubmit",
    "formdata(",
    "await fetch",
    "/api/",
)
PLACEHOLDER_DYNAMIC_MARKERS = (
    "no items yet",
    "loading content",
    "something went wrong",
    "coming soon",
    "placeholder",
)


class ConnectivityValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        graph = self._read_json(workspace_path / "artifacts" / "generated_app_graph.json")
        if not isinstance(graph, dict):
            return []
        roles = graph.get("roles")
        if not isinstance(roles, dict):
            return []

        grounded_spec = self._read_json(workspace_path / "artifacts" / "grounded_spec.json")
        api_requirements = grounded_spec.get("api_requirements") if isinstance(grounded_spec, dict) else []
        api_requirements = api_requirements if isinstance(api_requirements, list) else []

        issues: list[ValidationIssue] = []
        static_root = workspace_path / "miniapp" / "app" / "static"
        routes_root = workspace_path / "miniapp" / "app" / "routes"
        route_stems = {path.stem for path in routes_root.glob("*.py") if path.name != "__init__.py"}

        for role, role_payload in roles.items():
            if not isinstance(role_payload, dict):
                continue
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                dependencies = self._normalize_tokens(page.get("data_dependencies") or [])
                if not dependencies:
                    continue
                file_path = str(page.get("file_path") or "")
                if not file_path:
                    continue
                page_file = workspace_path / file_path
                if not page_file.exists():
                    continue
                surface_content = self._page_surface_content(workspace_path, file_path)
                api_refs = self._extract_api_refs(surface_content)
                expected_route_stems = self._expected_route_stems(page, dependencies, api_requirements, api_refs)

                if api_refs:
                    for ref in sorted(api_refs):
                        if ref not in route_stems:
                            issues.append(
                                ValidationIssue(
                                    code="connectivity.missing_backend_route",
                                    message=f"{file_path} references /api/{ref} but miniapp/app/routes/{ref}.py is missing.",
                                    severity="high",
                                    location=f"miniapp/app/routes/{ref}.py",
                                )
                            )
                elif expected_route_stems and not any(stem in route_stems for stem in expected_route_stems):
                    missing_stem = sorted(expected_route_stems)[0]
                    issues.append(
                        ValidationIssue(
                            code="connectivity.missing_backend_route",
                            message=f"{file_path} declares dynamic data dependencies but no matching backend route module was found.",
                            severity="high",
                            location=f"miniapp/app/routes/{missing_stem}.py",
                        )
                    )

                lowered_surface = surface_content.lower()
                has_dynamic_runtime_wiring = bool(api_refs) or any(marker in lowered_surface for marker in UI_WIRING_MARKERS)
                if not has_dynamic_runtime_wiring:
                    issues.append(
                        ValidationIssue(
                            code="connectivity.unwired_page_dependency",
                            message=f"{file_path} declares dynamic dependencies but does not include request, API, or submit wiring.",
                            severity="high",
                            location=file_path,
                        )
                    )

                loading_state = str(page.get("loading_state") or "").strip()
                if loading_state and has_dynamic_runtime_wiring and not self._contains_state(lowered_surface, loading_state, state_kind="loading"):
                    issues.append(
                        ValidationIssue(
                            code="connectivity.missing_ui_loading_state",
                            message=f"{file_path} is missing its planned loading state for dynamic data.",
                            severity="high",
                            location=file_path,
                        )
                    )

                error_state = str(page.get("error_state") or "").strip()
                if error_state and has_dynamic_runtime_wiring and not self._contains_state(lowered_surface, error_state, state_kind="error"):
                    issues.append(
                        ValidationIssue(
                            code="connectivity.missing_ui_error_state",
                            message=f"{file_path} is missing its planned error state for dynamic data.",
                            severity="high",
                            location=file_path,
                        )
                    )

                if self._looks_like_placeholder_dynamic_page(lowered_surface, api_refs):
                    issues.append(
                        ValidationIssue(
                            code="connectivity.placeholder_dynamic_page",
                            message=f"{file_path} still looks like a static placeholder despite declared dynamic dependencies.",
                            severity="high",
                            location=file_path,
                        )
                    )

        if static_root.exists():
            for file_path in static_root.rglob("*"):
                if file_path.suffix not in {".html", ".js"}:
                    continue
                relative = str(file_path.relative_to(workspace_path))
                content = file_path.read_text(encoding="utf-8")
                for endpoint in self._extract_api_refs(content):
                    if endpoint not in route_stems:
                        issues.append(
                            ValidationIssue(
                                code="connectivity.missing_backend_route",
                                message=f"{relative} references /api/{endpoint} but the matching route module is missing.",
                                severity="high",
                                location=f"miniapp/app/routes/{endpoint}.py",
                            )
                        )
                for asset_path in self._extract_static_asset_refs(content, source_path=relative):
                    if (workspace_path / asset_path).exists():
                        continue
                    issues.append(
                        ValidationIssue(
                            code="connectivity.missing_static_asset",
                            message=f"{relative} references {self._public_static_asset_path(asset_path)} but the static asset is missing.",
                            severity="high",
                            location=asset_path,
                        )
                    )
        return self._dedupe_issues(issues)

    @staticmethod
    def _read_json(path: Path) -> dict | list | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _page_surface_content(workspace_path: Path, file_path: str) -> str:
        page_file = workspace_path / file_path
        page_content = page_file.read_text(encoding="utf-8")
        content_parts = [page_content]
        appended_assets: set[str] = set()
        for asset_path in ConnectivityValidator._extract_static_asset_refs(page_content, source_path=file_path):
            asset_file = workspace_path / asset_path
            if not asset_file.exists() or asset_path in appended_assets:
                continue
            content_parts.append(asset_file.read_text(encoding="utf-8"))
            appended_assets.add(asset_path)
        match = re.search(r"miniapp/app/static/([^/]+)/", file_path)
        if match is None:
            return "\n".join(content_parts)
        role = match.group(1)
        role_dir = workspace_path / "miniapp" / "app" / "static" / role
        for candidate in ("app.js", "profile.js"):
            target = role_dir / candidate
            target_relative = str(target.relative_to(workspace_path))
            if target.exists() and target_relative not in appended_assets:
                content_parts.append(target.read_text(encoding="utf-8"))
                appended_assets.add(target_relative)
        return "\n".join(content_parts)

    @staticmethod
    def _extract_api_refs(content: str) -> set[str]:
        refs: set[str] = set()
        for match in re.finditer(r"['\"]?/api/([a-zA-Z0-9_-]+)(?:[/'\"?)]|$)", content):
            refs.add(match.group(1).lower())
        return refs

    @staticmethod
    def _extract_static_asset_refs(content: str, *, source_path: str) -> set[str]:
        refs: set[str] = set()
        patterns = (
            r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css)(?:[?#][^"']*)?)["']""",
            r"""(?:import|from)\s*(?:\(\s*)?["']([^"']+\.(?:js|css)(?:[?#][^"']*)?)["']""",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, content, flags=re.IGNORECASE):
                resolved = ConnectivityValidator._resolve_static_asset_ref(match.group(1), source_path=source_path)
                if resolved:
                    refs.add(resolved)
        return refs

    @staticmethod
    def _resolve_static_asset_ref(raw_ref: str, *, source_path: str) -> str | None:
        candidate = raw_ref.strip().split("?", 1)[0].split("#", 1)[0]
        if not candidate or candidate.startswith(("http://", "https://", "//", "data:")):
            return None
        if candidate.startswith("/static/"):
            resolved = f"miniapp/app{candidate}"
        elif candidate.startswith("static/"):
            resolved = f"miniapp/app/{candidate}"
        elif candidate.startswith("/"):
            return None
        else:
            source_parent = Path(source_path).parent.as_posix()
            resolved = posixpath.normpath(posixpath.join(source_parent, candidate))
        if not resolved.startswith("miniapp/app/static/"):
            return None
        if Path(resolved).suffix.lower() not in {".js", ".css"}:
            return None
        return resolved

    @staticmethod
    def _public_static_asset_path(relative_path: str) -> str:
        if relative_path.startswith("miniapp/app/static/"):
            return f"/static/{relative_path.removeprefix('miniapp/app/static/')}"
        return relative_path

    @staticmethod
    def _normalize_tokens(values: list[str] | tuple[str, ...]) -> set[str]:
        tokens: set[str] = set()
        for value in values:
            for token in re.split(r"[^a-zA-Z0-9]+", str(value).lower()):
                token = token.strip()
                if len(token) < 3 or token in ROLE_NAMES:
                    continue
                tokens.add(token[:-1] if token.endswith("s") and len(token) > 4 else token)
        return tokens

    @classmethod
    def _expected_route_stems(
        cls,
        page: dict,
        dependencies: set[str],
        api_requirements: list[dict],
        api_refs: set[str],
    ) -> set[str]:
        if api_refs:
            return set(api_refs)
        stems: set[str] = set()
        stems.update(dependencies)
        page_tokens = cls._normalize_tokens(
            [
                str(page.get("route_path") or ""),
                str(Path(str(page.get("file_path") or "")).stem),
                str(page.get("title") or ""),
                str(page.get("description") or ""),
            ]
        )
        stems.update(page_tokens)
        for requirement in api_requirements:
            if not isinstance(requirement, dict):
                continue
            path = str(requirement.get("path") or "")
            if "/api/" not in path:
                continue
            candidate_stems = cls._normalize_tokens(
                [
                    path,
                    str(requirement.get("name") or ""),
                    str(requirement.get("purpose") or ""),
                ]
            )
            if candidate_stems & (dependencies | page_tokens):
                path_match = re.search(r"/api/([a-zA-Z0-9_-]+)", path)
                if path_match:
                    stems.add(path_match.group(1).lower())
        return {stem for stem in stems if stem not in {"api", "data", "page", "state"}}

    @staticmethod
    def _contains_state(content: str, state_text: str, *, state_kind: str) -> bool:
        normalized_state = re.sub(r"\s+", " ", state_text.lower()).strip()
        normalized_content = re.sub(r"\s+", " ", content)
        if normalized_state and normalized_state in normalized_content:
            return True
        semantic_patterns = (
            rf'data-ui-state\s*=\s*["\']{state_kind}["\']',
            rf'id\s*=\s*["\'][^"\']*{state_kind}[^"\']*["\']',
            rf'class\s*=\s*["\'][^"\']*{state_kind}[^"\']*["\']',
            rf'getelementbyid\(\s*["\'][^"\']*{state_kind}[^"\']*["\']\s*\)',
            rf'queryselector\(\s*["\'][^"\']*{state_kind}[^"\']*["\']\s*\)',
            rf'queryselectorall\(\s*["\'][^"\']*{state_kind}[^"\']*["\']\s*\)',
        )
        return any(re.search(pattern, normalized_content) for pattern in semantic_patterns)

    @staticmethod
    def _looks_like_placeholder_dynamic_page(content: str, api_refs: set[str]) -> bool:
        if api_refs:
            return False
        if any(marker in content for marker in UI_WIRING_MARKERS):
            return False
        return any(marker in content for marker in PLACEHOLDER_DYNAMIC_MARKERS)

    @staticmethod
    def _dedupe_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        deduped: dict[tuple[str, str, str], ValidationIssue] = {}
        for issue in issues:
            deduped[(issue.code, issue.location, issue.message)] = issue
        return list(deduped.values())
