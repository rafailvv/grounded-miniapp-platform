from __future__ import annotations

from pathlib import Path

from app.models.domain import FixScopeEntry


class FixScopeBuilder:
    def __init__(self, *, file_exists) -> None:
        self._file_exists = file_exists

    def build_write_scope(
        self,
        *,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
        existing_scope: list[FixScopeEntry],
        allow_missing_scope_path,
    ) -> list[FixScopeEntry]:
        entries = {entry.file_path: entry for entry in existing_scope}
        for file_path in implicated_files:
            entries.setdefault(file_path, FixScopeEntry(file_path=file_path, reason="Directly implicated by the current failure evidence."))
            for companion in self.deterministic_companion_scope(file_path):
                if self._file_exists(workspace_id, run_id, companion) or allow_missing_scope_path(companion):
                    entries.setdefault(companion, FixScopeEntry(file_path=companion, reason="Included as a deterministic companion of the failing bundle."))
        for candidate in self.structural_scope_bundle(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            allow_missing_scope_path=allow_missing_scope_path,
        ):
            entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Included as part of the deterministic contract bundle."))
        if failure_class.startswith("preview_runtime") or failure_class.startswith("runtime") or failure_class.startswith("tooling"):
            for candidate in ("docker/docker-compose.yml", "miniapp/requirements.txt", "miniapp/app/main.py"):
                if self._file_exists(workspace_id, run_id, candidate) or allow_missing_scope_path(candidate):
                    entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Runtime or preview glue may be involved in the current failure."))
        return list(entries.values())

    @staticmethod
    def deterministic_companion_scope(file_path: str) -> list[str]:
        normalized = str(file_path or "").strip().replace("\\", "/")
        companions: list[str] = []
        if normalized.startswith("miniapp/app/static/"):
            path_obj = Path(normalized)
            if normalized.endswith("/index.html") or normalized.endswith("/styles.css") or normalized.endswith("/app.js"):
                base_dir = path_obj.parent
                companions.extend(
                    [
                        str(base_dir / "index.html").replace("\\", "/"),
                        str(base_dir / "styles.css").replace("\\", "/"),
                        str(base_dir / "app.js").replace("\\", "/"),
                    ]
                )
            elif not normalized.endswith("/index.html") and path_obj.suffix in {".html", ".css", ".js"}:
                base = path_obj.with_suffix("")
                companions.extend([f"{base}.html", f"{base}.css", f"{base}.js"])
        elif normalized in {"miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py", "miniapp/app/routes/profiles.py"}:
            companions.extend(
                [
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                    "miniapp/app/routes/profiles.py",
                ]
            )
        elif normalized.startswith("miniapp/app/routes/") and normalized.endswith(".py"):
            companions.extend(
                [
                    normalized,
                    "miniapp/app/main.py",
                    "miniapp/app/db.py",
                    "miniapp/app/schemas.py",
                ]
            )
        return list(dict.fromkeys(path for path in companions if path))

    def structural_scope_bundle(
        self,
        *,
        workspace_id: str,
        run_id: str,
        implicated_files: list[str],
        failure_class: str,
        allow_missing_scope_path,
    ) -> list[str]:
        bundle: list[str] = []
        for candidate in (
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/main.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
            "artifacts/generated_app_graph.json",
        ):
            if self._file_exists(workspace_id, run_id, candidate) or allow_missing_scope_path(candidate):
                bundle.append(candidate)
        for file_path in implicated_files:
            if file_path.startswith("miniapp/app/routes/"):
                bundle.append(file_path)
            if file_path.startswith("miniapp/app/static/") and file_path.endswith((".html", ".css", ".js")):
                parent = str(Path(file_path).parent)
                if self._file_exists(workspace_id, run_id, parent) or allow_missing_scope_path(file_path):
                    bundle.append(parent)
        if "route" in failure_class or "contract" in failure_class:
            routes_dir = "miniapp/app/routes"
            if self._file_exists(workspace_id, run_id, routes_dir):
                bundle.append(routes_dir)
        return list(dict.fromkeys(bundle))

    @staticmethod
    def merge_scope(
        current_scope: list[FixScopeEntry],
        next_scope: list[FixScopeEntry],
        scope_expansions: list[dict[str, object]],
        *,
        max_scope_expansions: int,
    ) -> list[FixScopeEntry]:
        merged = {entry.file_path: entry for entry in current_scope}
        for entry in next_scope:
            merged.setdefault(entry.file_path, entry)
        if len(scope_expansions) > max_scope_expansions:
            return current_scope
        return list(merged.values())

