from __future__ import annotations

from pathlib import Path
import posixpath
import re

from app.models.artifacts import ValidationIssue


class ConnectivityValidator:
    def validate(self, workspace_path: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        static_root = workspace_path / "miniapp" / "app" / "static"
        routes_root = workspace_path / "miniapp" / "app" / "routes"
        route_stems = {
            self._normalize_route_stem(path.stem)
            for path in routes_root.glob("*.py")
            if path.name != "__init__.py"
        }

        if static_root.exists():
            for file_path in static_root.rglob("*"):
                if file_path.suffix not in {".html", ".js"}:
                    continue
                relative = str(file_path.relative_to(workspace_path))
                content = file_path.read_text(encoding="utf-8")
                for endpoint in self._extract_api_refs(content):
                    normalized_endpoint = self._normalize_route_stem(endpoint)
                    if normalized_endpoint not in route_stems:
                        issues.append(
                            ValidationIssue(
                                code="connectivity.missing_backend_route",
                                message=f"{relative} references /api/{endpoint} but the matching route module is missing.",
                                severity="high",
                                location=f"miniapp/app/routes/{normalized_endpoint}.py",
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
    def _extract_api_refs(content: str) -> set[str]:
        refs: set[str] = set()
        for match in re.finditer(r"['\"]?/api/([a-zA-Z0-9_-]+)(?:[/'\"?)]|$)", content):
            refs.add(ConnectivityValidator._normalize_route_stem(match.group(1).lower()))
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
    def _dedupe_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        deduped: dict[tuple[str, str, str], ValidationIssue] = {}
        for issue in issues:
            key = (issue.code, issue.location, issue.message)
            deduped.setdefault(key, issue)
        return list(deduped.values())
