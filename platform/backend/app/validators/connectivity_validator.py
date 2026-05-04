from __future__ import annotations

from pathlib import Path
import posixpath
import re

from app.models.artifacts import ValidationIssue
from app.validators.static_analysis import extract_declared_routes, extract_frontend_api_refs, normalize_api_path


class ConnectivityValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        static_root = workspace_path / "miniapp" / "app" / "static"
        routes_root = workspace_path / "miniapp" / "app" / "routes"
        api_route_paths = self._api_route_paths(routes_root)

        if static_root.exists():
            for file_path in static_root.rglob("*"):
                if file_path.suffix not in {".html", ".js"}:
                    continue
                relative = str(file_path.relative_to(workspace_path))
                content = file_path.read_text(encoding="utf-8")
                for method, endpoint in self._extract_api_refs(content):
                    normalized_endpoint = self._normalize_api_path(endpoint)
                    if not self._api_path_is_declared(method, normalized_endpoint, api_route_paths):
                        issues.append(
                            ValidationIssue(
                                code="connectivity.missing_backend_route",
                                message=f"{relative} references {method} {normalized_endpoint} but the matching backend route is missing.",
                                severity="high",
                                location="miniapp/app/routes",
                                repair_recipe={
                                    "frontend_ref": f"{relative}: {method} {normalized_endpoint}",
                                    "expected_route": f"{method} {normalized_endpoint}",
                                    "declared_routes": [f"{declared_method} {declared_path}" for declared_method, declared_path in sorted(api_route_paths)],
                                    "why_mismatch": "Frontend fetch reference has no matching FastAPI route by method/path.",
                                    "suggested_patch_target": "miniapp/app/routes/generated_contract.py",
                                    "auto_fixable": False,
                                    "validator_may_be_stale": False,
                                },
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
    def _extract_api_refs(content: str) -> set[tuple[str, str]]:
        return extract_frontend_api_refs(content)

    @classmethod
    def _api_route_paths(cls, routes_root: Path) -> set[tuple[str, str]]:
        return extract_declared_routes(routes_root, api_only=True)

    @classmethod
    def _api_path_is_declared(cls, referenced_method: str, referenced_path: str, declared_paths: set[tuple[str, str]]) -> bool:
        method = str(referenced_method or "GET").upper()
        same_method_paths = {path for declared_method, path in declared_paths if declared_method == method}
        if method == "HEAD":
            same_method_paths.update(path for declared_method, path in declared_paths if declared_method == "GET")
        if any(cls._api_paths_match(referenced_path, declared_path) for declared_path in same_method_paths):
            return True
        referenced = referenced_path.rstrip("/")
        if referenced.count("/") < 2:
            return False
        return any(declared_path.startswith(f"{referenced}/") for declared_path in same_method_paths)

    @staticmethod
    def _api_paths_match(referenced_path: str, declared_path: str) -> bool:
        referenced_parts = referenced_path.strip("/").split("/")
        declared_parts = declared_path.strip("/").split("/")
        if len(referenced_parts) != len(declared_parts):
            return False
        for referenced, declared in zip(referenced_parts, declared_parts):
            if declared.startswith("{") and declared.endswith("}"):
                continue
            if referenced.startswith("{") and referenced.endswith("}"):
                continue
            if referenced != declared:
                return False
        return True

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
    def _normalize_route_stem(value: str) -> str:
        normalized = str(value or "").strip().strip("/").lower().replace("-", "_")
        if normalized.startswith("api/"):
            normalized = normalized.split("/", 1)[1]
        if "/" in normalized:
            normalized = normalized.split("/", 1)[0]
        if normalized.endswith("s") and len(normalized) > 4:
            singular = normalized[:-1]
            if singular not in {"status", "news"}:
                normalized = singular
        return normalized

    @staticmethod
    def _normalize_api_path(value: str) -> str:
        return normalize_api_path(value)

    @staticmethod
    def _dedupe_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        deduped: dict[tuple[str, str, str], ValidationIssue] = {}
        for issue in issues:
            key = (issue.code, issue.location, issue.message)
            deduped.setdefault(key, issue)
        return list(deduped.values())
