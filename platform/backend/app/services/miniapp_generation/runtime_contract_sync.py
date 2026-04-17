from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from app.models.domain import DraftFileOperation
from app.services.workspace.service import WorkspaceService


ReadContentFn = Callable[[str, str, dict[str, DraftFileOperation], str], str | None]


class MiniappRuntimeContractSync:
    def __init__(
        self,
        *,
        workspace_service: WorkspaceService,
        read_content: ReadContentFn,
    ) -> None:
        self.workspace_service = workspace_service
        self.read_content = read_content

    def synchronize(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        ensured = self.synchronize_db_session_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )
        ensured = self.synchronize_runtime_route_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
        )
        ensured = self.synchronize_main_runtime_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
        )
        ensured = self.synchronize_backend_dependency_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
        )
        ensured = self.synchronize_frontend_navigation_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=ensured,
        )
        return ensured

    def synchronize_db_session_contract(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        db_path = "miniapp/app/db.py"
        db_content = self.read_content(workspace_id, draft_run_id, operation_map, db_path)
        if not db_content or "def get_db(" in db_content:
            return operations
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
                continue
            content = self.read_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content or "get_db" not in content:
                continue
            if "from app.db import" not in content and "Depends(get_db)" not in content:
                continue
            operation_map[db_path] = DraftFileOperation(
                file_path=db_path,
                operation="replace",
                content=db_content.rstrip()
                + "\n\n\ndef get_db():\n"
                + "    session = SessionLocal()\n"
                + "    try:\n"
                + "        yield session\n"
                + "    finally:\n"
                + "        session.close()\n",
                reason="Pre-apply contract sync: ensure db.py exports get_db when routes depend on it.",
            )
            return list(operation_map.values())
        return operations

    def synchronize_runtime_route_contract(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
                continue
            content = self.read_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content or "/api/runtime/" not in content:
                continue
            updated = (
                self.normalize_runtime_route_module_source(content)
                if file_path == "miniapp/app/routes/runtime.py"
                else self.strip_noncanonical_runtime_route_handlers(content)
            )
            if updated == content:
                continue
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="replace",
                content=updated,
                reason="Pre-apply contract sync: keep runtime manifest endpoints global and sample-aware.",
            )
        return list(operation_map.values())

    def synchronize_main_runtime_contract(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        main_path = "miniapp/app/main.py"
        current_main = self.read_content(workspace_id, draft_run_id, operation_map, main_path)
        if current_main is None:
            return operations
        route_root = self.workspace_service.draft_source_dir(workspace_id, draft_run_id) / "miniapp/app/routes"
        route_modules: set[str] = set()
        if route_root.exists():
            for file_path in route_root.glob("*.py"):
                stem = file_path.stem
                if stem not in {"__init__", "health", "profiles"}:
                    route_modules.add(stem)
        for file_path in operation_map:
            if not file_path.startswith("miniapp/app/routes/") or not file_path.endswith(".py"):
                continue
            stem = Path(file_path).stem
            if stem not in {"__init__", "health", "profiles"}:
                route_modules.add(stem)
        desired = self.deterministic_main_runtime_source(sorted(route_modules))
        if desired == current_main:
            return operations
        operation_map[main_path] = DraftFileOperation(
            file_path=main_path,
            operation="replace",
            content=desired,
            reason="Pre-apply contract sync: keep main.py manifest-aware and include all canonical backend routers.",
        )
        return list(operation_map.values())

    def synchronize_backend_dependency_contract(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
                continue
            content = self.read_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            updated = re.sub(
                r"Depends\(\s*lambda:\s*get_actor_context\(\)\s*\)",
                "Depends(get_actor_context)",
                content,
            )
            if "Depends(" in updated and "from fastapi import" in updated:
                updated = self.ensure_fastapi_import_symbol(updated, "Depends")
            if updated == content:
                continue
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="replace",
                content=updated,
                reason="Pre-apply contract sync: use FastAPI dependency injection directly for get_actor_context.",
            )
        return list(operation_map.values())

    def synchronize_frontend_navigation_contract(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        for file_path in list(operation_map):
            if not (
                file_path.startswith("miniapp/app/static/")
                and (file_path.endswith(".html") or file_path.endswith(".js"))
            ):
                continue
            content = self.read_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            updated = self.canonicalize_local_role_links_in_text(content)
            if updated == content:
                continue
            operation_map[file_path] = DraftFileOperation(
                file_path=file_path,
                operation="replace",
                content=updated,
                reason="Pre-apply contract sync: canonicalize local role navigation links before generated route checks.",
            )
        return list(operation_map.values())

    @staticmethod
    def canonicalize_local_role_links_in_text(content: str) -> str:
        updated = str(content or "")
        role_root_pattern = re.compile(
            r'(?P<quote>["\'`])(?P<path>/(?:client|specialist|manager)(?:/[A-Za-z0-9_{}:-]+)*)/(?P<suffix>(?:[?#][^"\'`]*)?)(?P=quote)'
        )

        def _replace(match: re.Match[str]) -> str:
            quote = match.group("quote")
            path = match.group("path")
            suffix = match.group("suffix") or ""
            return f"{quote}{path}{suffix}{quote}"

        return role_root_pattern.sub(_replace, updated)

    @staticmethod
    def strip_noncanonical_runtime_route_handlers(content: str) -> str:
        lines = str(content or "").splitlines()
        if not lines:
            return str(content or "")
        runtime_decorator = re.compile(r'^\s*@router\.(?:get|post|put|patch|delete)\(["\']/api/runtime/')
        definition_line = re.compile(r'^\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\(')
        kept: list[str] = []
        index = 0
        removed_any = False
        while index < len(lines):
            line = lines[index]
            if runtime_decorator.match(line):
                removed_any = True
                while index < len(lines) and runtime_decorator.match(lines[index]):
                    index += 1
                if index < len(lines) and definition_line.match(lines[index]):
                    index += 1
                    while index < len(lines):
                        current = lines[index]
                        if current.strip() == "":
                            index += 1
                            continue
                        if not current.startswith((" ", "\t")):
                            break
                        index += 1
                    continue
            kept.append(line)
            index += 1
        updated = "\n".join(kept)
        if str(content or "").endswith("\n"):
            updated += "\n"
        if removed_any:
            updated = re.sub(r"\n{3,}", "\n\n", updated)
        return updated

    @classmethod
    def normalize_runtime_route_module_source(cls, content: str) -> str:
        updated = str(content or "")
        if "/api/runtime/" not in updated:
            return updated
        helper_block = (
            '\n\ndef _normalize_runtime_role(role: str) -> str:\n'
            '    normalized = str(role or "").strip().strip("/")\n'
            '    if normalized == "sample":\n'
            '        return "client"\n'
            '    return normalized\n'
        )
        if "_normalize_runtime_role" not in updated:
            if "ALLOWED_ROLES = {" in updated:
                updated = re.sub(r"(ALLOWED_ROLES\s*=\s*\{[^\n]+\}\n)", r"\1" + helper_block, updated, count=1)
            else:
                updated = helper_block.lstrip("\n") + "\n" + updated
        replacement = (
            "def _validate_role(role: str) -> str:\n"
            "    normalized = _normalize_runtime_role(role)\n"
            "    if normalized not in ALLOWED_ROLES:\n"
            "        raise HTTPException(status_code=404, detail=\"Role not supported\")\n"
            "    return normalized\n"
        )
        updated = cls._replace_top_level_function(updated, "_validate_role", replacement)
        updated = re.sub(r"(?m)^(\s*)_validate_role\(role\)\s*$", r"\1role = _validate_role(role)", updated)
        updated = updated.replace(
            '    raise HTTPException(status_code=404, detail="Action not supported")\n',
            '    return {\n'
            '        "next_path": f"/{role}/",\n'
            '        "message": "No-op runtime action.",\n'
            '        "action_id": action_id,\n'
            '        "role": role,\n'
            '    }\n',
        )
        return updated

    @staticmethod
    def deterministic_main_runtime_source(route_modules: list[str]) -> str:
        route_import_lines = "\n".join(
            f"from app.routes.{module} import router as {module}_router"
            for module in route_modules
        )
        include_lines = "\n".join(f"app.include_router({module}_router)" for module in route_modules)
        if route_import_lines:
            route_import_lines = f"{route_import_lines}\n"
        if include_lines:
            include_lines = f"{include_lines}\n"
        return f"""from __future__ import annotations

import json
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routes.health import router as health_router
from app.routes.profiles import router as profiles_router
{route_import_lines}BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
GENERATED_DIR = BASE_DIR / "generated"
ROUTE_MANIFEST_PATH = GENERATED_DIR / "route_manifest.json"
RUNTIME_MANIFEST_PATH = GENERATED_DIR / "runtime_manifest.json"
ROLES = ("client", "specialist", "manager")

app = FastAPI()
app.include_router(health_router)
app.include_router(profiles_router)
{include_lines}app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/client", status_code=307)


def _load_route_manifest() -> dict:
    if not ROUTE_MANIFEST_PATH.exists():
        return {{}}
    try:
        return json.loads(ROUTE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {{}}


def _load_runtime_manifest() -> dict:
    if not RUNTIME_MANIFEST_PATH.exists():
        return {{}}
    try:
        return json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {{}}


def _normalize_runtime_role(role: str) -> str:
    normalized = (role or "").strip().strip("/")
    if normalized == "sample":
        return "client"
    return normalized


def _runtime_manifest_response(role: str, requested_role: str | None = None) -> JSONResponse:
    requested_role = requested_role or role
    role = _normalize_runtime_role(role)
    if role not in ROLES:
        return JSONResponse(status_code=404, content={{"detail": f"unknown role: {{requested_role}}"}})
    runtime_manifest_payload = _load_runtime_manifest()
    route_manifest_payload = _load_route_manifest()
    return JSONResponse(
        content={{
            "role": role,
            "requested_role": requested_role,
            "runtime": ((runtime_manifest_payload.get("roles") or {{}}).get(role) or {{}}),
            "routes": ((route_manifest_payload.get("roles") or {{}}).get(role) or {{}}),
            "version": runtime_manifest_payload.get("version") or "generated",
        }}
    )


def _canonicalize_role_path(path: str) -> str:
    normalized = str(path or "").strip() or "/"
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized or "/"


def _route_matches(pattern: str, actual: str) -> bool:
    normalized_pattern = re.sub(r"\\{{[^/]+\\}}", "[^/]+", pattern)
    normalized_pattern = re.sub(r":[^/]+", "[^/]+", normalized_pattern)
    return re.fullmatch(normalized_pattern, actual) is not None


def _resolve_declared_page_file(role: str, actual_path: str) -> Path | None:
    actual_path = _canonicalize_role_path(actual_path)
    route_manifest = _load_route_manifest()
    pages = (((route_manifest.get("roles") or {{}}).get(role) or {{}}).get("pages") or [])
    for page in pages:
        if not isinstance(page, dict):
            continue
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip()
        if not route_path or not file_path:
            continue
        if not _route_matches(route_path, actual_path):
            continue
        normalized_file_path = file_path.replace("\\\\", "/")
        if normalized_file_path.startswith("miniapp/app/"):
            resolved = BASE_DIR.parent / normalized_file_path.removeprefix("miniapp/app/")
        elif normalized_file_path.startswith("app/"):
            resolved = BASE_DIR.parent / normalized_file_path.removeprefix("app/")
        else:
            resolved = BASE_DIR / normalized_file_path
        if resolved.exists():
            return resolved
    return None


def _resolve_role_page(role: str, actual_path: str) -> Path:
    actual_path = _canonicalize_role_path(actual_path)
    if role not in ROLES:
        raise KeyError(role)
    declared_page = _resolve_declared_page_file(role, actual_path)
    if declared_page is not None:
        return declared_page
    if actual_path == f"/{{role}}":
        page_file = STATIC_DIR / role / "index.html"
        if page_file.exists():
            return page_file
    if actual_path == f"/{{role}}/profile":
        page_file = STATIC_DIR / role / "profile" / "index.html"
        if page_file.exists():
            return page_file
    slug_parts = [segment for segment in actual_path.removeprefix(f"/{{role}}").split("/") if segment]
    if len(slug_parts) == 1:
        page_file = STATIC_DIR / role / slug_parts[0] / "index.html"
        if page_file.exists():
            return page_file
    if len(slug_parts) >= 2:
        dynamic_candidate = STATIC_DIR / role / f"{{slug_parts[0]}}_detail" / "index.html"
        if dynamic_candidate.exists():
            return dynamic_candidate
    raise KeyError(actual_path)


@app.get("/api/runtime/client/manifest")
def runtime_manifest_client() -> JSONResponse:
    return _runtime_manifest_response("client", "client")


@app.get("/api/runtime/specialist/manifest")
def runtime_manifest_specialist() -> JSONResponse:
    return _runtime_manifest_response("specialist", "specialist")


@app.get("/api/runtime/manager/manifest")
def runtime_manifest_manager() -> JSONResponse:
    return _runtime_manifest_response("manager", "manager")


@app.get("/api/runtime/sample/manifest")
def runtime_manifest_sample() -> JSONResponse:
    return _runtime_manifest_response("client", "sample")


@app.get("/api/runtime/{{role}}/manifest")
def runtime_manifest(role: str) -> JSONResponse:
    return _runtime_manifest_response(role, role)


@app.get("/{{role}}", include_in_schema=False)
def role_page(role: str) -> FileResponse:
    return FileResponse(_resolve_role_page(role, f"/{{role}}"))


@app.get("/{{role}}/", include_in_schema=False)
def role_page_trailing_slash(role: str) -> FileResponse:
    return FileResponse(_resolve_role_page(role, f"/{{role}}/"))


@app.get("/{{role}}/{{page_path:path}}", include_in_schema=False)
def role_nested_page(role: str, page_path: str) -> FileResponse:
    return FileResponse(_resolve_role_page(role, f"/{{role}}/{{page_path}}"))


@app.exception_handler(KeyError)
def key_error_handler(_, exc: KeyError) -> JSONResponse:
    return JSONResponse(status_code=404, content={{"detail": str(exc)}})
"""

    @staticmethod
    def ensure_fastapi_import_symbol(content: str, symbol: str) -> str:
        updated = str(content or "")
        import_pattern = re.compile(r"from fastapi import ([^\n]+)")
        match = import_pattern.search(updated)
        if not match:
            return updated
        symbols = [item.strip() for item in match.group(1).split(",") if item.strip()]
        if symbol in symbols:
            return updated
        symbols.append(symbol)
        ordered_symbols: list[str] = []
        for item in symbols:
            if item not in ordered_symbols:
                ordered_symbols.append(item)
        replacement = f"from fastapi import {', '.join(ordered_symbols)}"
        return import_pattern.sub(replacement, updated, count=1)

    @classmethod
    def _replace_top_level_function(cls, source: str, function_name: str, replacement: str) -> str:
        lines = str(source or "").splitlines()
        if not lines:
            return replacement.rstrip() + "\n"
        pattern = re.compile(rf"^def {re.escape(function_name)}\(")
        start_index: int | None = None
        for index, line in enumerate(lines):
            if pattern.match(line):
                start_index = index
                break
        if start_index is None:
            insert_at = 0
            for index, line in enumerate(lines):
                if line.startswith("@router.") or line.startswith("router =") or line.startswith("def "):
                    insert_at = index
                    break
            new_lines = lines[:insert_at] + replacement.strip("\n").splitlines() + [""] + lines[insert_at:]
            return cls._restore_trailing_newline(source, new_lines)
        end_index = start_index + 1
        while end_index < len(lines):
            current = lines[end_index]
            if current.strip() == "":
                end_index += 1
                continue
            if not current.startswith((" ", "\t")):
                break
            end_index += 1
        new_lines = lines[:start_index] + replacement.strip("\n").splitlines() + lines[end_index:]
        return cls._restore_trailing_newline(source, new_lines)

    @staticmethod
    def _restore_trailing_newline(original: str, lines: list[str]) -> str:
        updated = "\n".join(lines)
        if str(original or "").endswith("\n"):
            updated += "\n"
        return updated
