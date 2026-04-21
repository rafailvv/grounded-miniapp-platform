from __future__ import annotations

from pathlib import PurePosixPath

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
        for candidate in self.structural_scope_bundle(
            workspace_id=workspace_id,
            run_id=run_id,
            implicated_files=implicated_files,
            failure_class=failure_class,
            allow_missing_scope_path=allow_missing_scope_path,
        ):
            entries.setdefault(candidate, FixScopeEntry(file_path=candidate, reason="Included because the current failure evidence points to this adjacent structural file."))
        return list(entries.values())

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
        needs_route_registration_context = any(path.startswith("miniapp/app/routes/") and path.endswith(".py") for path in implicated_files)
        if needs_route_registration_context or ("route" in failure_class or "contract" in failure_class):
            structural_candidates = ["miniapp/app/db.py", "miniapp/app/schemas.py"]
            if failure_class in {
                "backend_framework_mismatch",
                "runtime_manifest_route_missing",
                "router_not_registered",
                "db_dependency_export_missing",
            }:
                structural_candidates.insert(0, "miniapp/app/main.py")
            for candidate in structural_candidates:
                if candidate in implicated_files:
                    continue
                if self._file_exists(workspace_id, run_id, candidate) or allow_missing_scope_path(candidate):
                    bundle.append(candidate)
        for file_path in implicated_files:
            if file_path.startswith("miniapp/app/routes/"):
                bundle.append(file_path)
            bundle.extend(
                self._static_page_scope_bundle(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    file_path=file_path,
                    allow_missing_scope_path=allow_missing_scope_path,
                )
            )
        return list(dict.fromkeys(bundle))

    def _static_page_scope_bundle(
        self,
        *,
        workspace_id: str,
        run_id: str,
        file_path: str,
        allow_missing_scope_path,
    ) -> list[str]:
        normalized = str(file_path or "").strip().replace("\\", "/")
        if not normalized.startswith("miniapp/app/static/"):
            return []
        path = PurePosixPath(normalized)
        if path.name not in {"index.html", "styles.css", "app.js"}:
            return []
        page_dir = path.parent
        candidates: list[str] = []

        def _add_triplet(base_dir: PurePosixPath) -> None:
            for name in ("index.html", "styles.css", "app.js"):
                candidate = str(base_dir / name)
                if self._file_exists(workspace_id, run_id, candidate) or allow_missing_scope_path(candidate):
                    candidates.append(candidate)

        _add_triplet(page_dir)
        parts = page_dir.parts
        if len(parts) >= 5:
            role = parts[3]
            role_route = f"miniapp/app/routes/{role}.py"
            if self._file_exists(workspace_id, run_id, role_route) or allow_missing_scope_path(role_route):
                candidates.append(role_route)
        page_stem = page_dir.name
        if page_stem.endswith("_detail"):
            _add_triplet(page_dir.with_name(page_stem[: -len("_detail")]))
        elif page_stem not in {"client", "specialist", "manager", "profile"}:
            _add_triplet(page_dir.with_name(f"{page_stem}_detail"))
        return list(dict.fromkeys(candidates))

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
