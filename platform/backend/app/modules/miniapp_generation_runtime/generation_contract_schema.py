from __future__ import annotations

import re
from pathlib import Path

from app.modules.miniapp_generation_runtime.generation_plan_runtime import FORBIDDEN_ROUTE_MODULE_STEMS
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner
from app.models.domain import DraftFileOperation


class MiniappGenerationContractSchema(MiniappGenerationRuntimeOwner):
    _FEATURE_ROUTE_EXCLUDED_STEMS = FORBIDDEN_ROUTE_MODULE_STEMS | {
        "client",
        "specialist",
        "manager",
        "profiles",
        "runtime",
        "users",
        "workload",
        "time_slots",
        "comments",
        "assignments",
        "role_pages",
        "health",
    }

    @staticmethod
    def _normalized_imported_schema_names(content: str) -> set[str]:
        imported_names: set[str] = set()
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+\((.*?)\)", content, flags=re.DOTALL):
            parts = [part.strip() for part in match.group(1).replace("\n", " ").split(",") if part.strip()]
            for part in parts:
                imported_names.add(part.split(" as ", 1)[0].strip())
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+([A-Za-z0-9_, ]+)", content):
            parts = [part.strip() for part in match.group(1).split(",") if part.strip()]
            for part in parts:
                imported_names.add(part.split(" as ", 1)[0].strip())
        return imported_names

    @staticmethod
    def _normalize_request_schema_contract(schemas_content: str) -> str:
        updated = str(schemas_content or "")
        if "class RequestRead" not in updated:
            return updated
        updated = re.sub(
            r"(^\s*owner_role:\s*AppRole\s*\|\s*None)\s*$",
            r"\1 = None",
            updated,
            flags=re.MULTILINE,
        )
        return updated

    @staticmethod
    def _route_path_candidates(resource_stem: str) -> set[str]:
        normalized = str(resource_stem or "").strip().strip("/").lower()
        if not normalized:
            return set()
        dashed = normalized.replace("_", "-")
        return {normalized, dashed}

    @classmethod
    def _needs_canonical_resource_route_repair(cls, content: str, resource_stem: str) -> bool:
        normalized = str(content or "")
        lowered = normalized.lower()
        if not normalized.strip():
            return True
        if "/api/submissions/{table}" in lowered:
            return True
        if any(
            marker in lowered
            for marker in (
                "create table if not exists requests",
                "insert or replace into requests",
                "select * from requests",
                "from app.db import engine",
                "from sqlalchemy import text",
            )
        ):
            return True
        path_candidates = cls._route_path_candidates(resource_stem)
        if not path_candidates:
            return True
        has_list = any(f'@router.get("/{candidate}")' in normalized for candidate in path_candidates)
        has_create = any(f'@router.post("/{candidate}")' in normalized for candidate in path_candidates)
        has_detail = any(
            re.search(rf'@router\.get\("/{re.escape(candidate)}/\{{[A-Za-z_][A-Za-z0-9_]*\}}"\)', normalized)
            for candidate in path_candidates
        )
        has_update = any(
            re.search(rf'@router\.(?:patch|put)\("/{re.escape(candidate)}/\{{[A-Za-z_][A-Za-z0-9_]*\}}"\)', normalized)
            for candidate in path_candidates
        )
        has_put = any(
            re.search(rf'@router\.put\("/{re.escape(candidate)}/\{{[A-Za-z_][A-Za-z0-9_]*\}}"\)', normalized)
            for candidate in path_candidates
        )
        if not (has_list and has_create and has_detail and has_update and has_put):
            return True
        return any(
            re.search(pattern, normalized, flags=re.MULTILINE)
            for pattern in (
                r"^class\s+[A-Za-z0-9_]*Record\s*\(Base\):",
                r"^class\s+[A-Za-z0-9_]*Create\s*\(",
                r"^class\s+[A-Za-z0-9_]*Update\s*\(",
                r"from\s+pydantic\s+import\s+BaseModel",
                r"\bid=str\(uuid4\(\)\)",
                r"\bowner_specialist_id\b",
            )
        )

    @staticmethod
    def _needs_profile_contract_repair(profiles_content: str) -> bool:
        return any(marker in str(profiles_content or "") for marker in ("DEFAULT_PROFILES", "_get_or_create(", "_get_or_create_profile_record("))

    @staticmethod
    def _needs_route_schema_contract_repair(imported_names: set[str], schemas_content: str) -> bool:
        updated_schemas = str(schemas_content or "")
        return (
            ("RequestDetail" in imported_names and "class RequestDetail" not in updated_schemas)
            or ("RoleProfile" in imported_names and "class RoleProfile" not in updated_schemas)
            or ("RoleProfile" in imported_names and "AppRole =" not in updated_schemas)
        )

    @staticmethod
    def _schema_prefixes_declared_by_schemas(schemas_content: str) -> list[str]:
        suffixes = ("Create", "Update", "Read", "Summary", "Detail", "ListResponse")
        prefixes: list[str] = []
        for match in re.finditer(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", str(schemas_content or ""), flags=re.MULTILINE):
            name = match.group(1)
            for suffix in suffixes:
                if name.endswith(suffix) and len(name) > len(suffix):
                    prefix = name[: -len(suffix)]
                    if prefix and prefix not in prefixes:
                        prefixes.append(prefix)
                    break
        return prefixes

    @classmethod
    def _route_schema_prefix_is_missing(cls, route_content: str, schemas_content: str) -> bool:
        route = str(route_content or "")
        schemas = str(schemas_content or "")
        match = re.search(r"^SCHEMA_PREFIX\s*=\s*[\"']([^\"']+)[\"']", route, flags=re.MULTILINE)
        if not match:
            return False
        current_prefix = match.group(1)
        if any(f"class {current_prefix}{suffix}" in schemas for suffix in ("Create", "Update", "Read", "Summary", "Detail", "ListResponse")):
            return False
        return bool(cls._schema_prefixes_declared_by_schemas(schemas))

    @classmethod
    def _patch_route_schema_prefix_candidates(cls, route_content: str, schemas_content: str) -> str:
        prefixes = cls._schema_prefixes_declared_by_schemas(schemas_content)
        if not prefixes:
            return route_content
        candidates_literal = ", ".join(repr(prefix) for prefix in prefixes[:8])
        replacement = (
            "def _candidate_schema_names() -> list[str]:\n"
            f"    candidates = [SCHEMA_PREFIX, {candidates_literal}]\n"
            "    seen: list[str] = []\n"
            "    for candidate in candidates:\n"
            "        normalized = str(candidate or \"\").strip()\n"
            "        if normalized and normalized not in seen:\n"
            "            seen.append(normalized)\n"
            "    return seen\n"
        )
        patched = re.sub(
            r"def\s+_candidate_schema_names\(\)\s*->\s*list\[str\]:\n(?:    .+\n)+?(?=\n\ndef\s+)",
            replacement,
            str(route_content or ""),
            count=1,
        )
        if patched != route_content:
            return patched
        return str(route_content or "").replace(
            "def _candidate_schema_names() -> list[str]:\n    return [SCHEMA_PREFIX]\n",
            replacement,
            1,
        )

    def _synchronize_profile_schema_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "repair_invariants",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        profiles_path = "miniapp/app/routes/profiles.py"
        schemas_path = "miniapp/app/schemas.py"
        profiles_content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, profiles_path)
        schemas_content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, schemas_path)
        if not profiles_content or not schemas_content:
            return operations
        if "from app.schemas import" not in profiles_content or "RoleProfile" not in profiles_content:
            return operations
        updated_schemas = schemas_content
        if "AppRole =" not in updated_schemas:
            if "from typing import Literal" in updated_schemas:
                updated_schemas = updated_schemas.replace("from typing import Literal", "from typing import Literal", 1)
            else:
                updated_schemas = "from typing import Literal\n" + updated_schemas
            updated_schemas += "\n\nAppRole = Literal[\"client\", \"specialist\", \"manager\"]\n"
        if "class RoleProfile" not in updated_schemas:
            if "from datetime import datetime" not in updated_schemas:
                updated_schemas = "from datetime import datetime\n" + updated_schemas
            if "from pydantic import" not in updated_schemas:
                updated_schemas = updated_schemas + "\nfrom pydantic import BaseModel\n"
            elif "BaseModel" not in updated_schemas:
                updated_schemas = updated_schemas.replace("from pydantic import ", "from pydantic import BaseModel, ", 1)
            updated_schemas += (
                "\n\nclass RoleProfile(BaseModel):\n"
                "    first_name: str\n"
                "    last_name: str\n"
                "    email: str | None = None\n"
                "    phone: str | None = None\n"
                "    photo_url: str | None = None\n"
                "    updated_at: datetime\n"
            )
        if updated_schemas == schemas_content:
            return operations
        operation_map[schemas_path] = DraftFileOperation(
            file_path=schemas_path,
            operation="replace",
            content=updated_schemas,
            reason="Pre-apply contract sync: keep schemas.py compatible with routes/profiles.py imports.",
        )
        return list(operation_map.values())

    def _synchronize_db_session_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        return self.runtime_contract_sync.synchronize_db_session_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )

    def _synchronize_runtime_route_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        return self.runtime_contract_sync.synchronize_runtime_route_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )

    def _synchronize_backend_dependency_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        return self.runtime_contract_sync.synchronize_backend_dependency_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )

    def _synchronize_main_runtime_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        return self.runtime_contract_sync.synchronize_main_runtime_contract(
            workspace_id=workspace_id,
            draft_run_id=draft_run_id,
            operations=operations,
        )

    def _synchronize_route_schema_contract(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "repair_invariants",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        schemas_path = "miniapp/app/schemas.py"
        db_path = "miniapp/app/db.py"
        schemas_content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, schemas_path)
        db_content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, db_path)
        if not schemas_content:
            return operations
        normalized_schemas = self._normalize_request_schema_contract(schemas_content)
        if normalized_schemas != schemas_content:
            operation_map[schemas_path] = DraftFileOperation(
                file_path=schemas_path,
                operation="replace",
                content=normalized_schemas,
                reason="Pre-apply contract sync: normalize optional request schema fields so route responses serialize without response-model drift.",
            )
            schemas_content = normalized_schemas
        imported_names: set[str] = set()
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
                continue
            content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            if self._route_schema_prefix_is_missing(content, schemas_content):
                patched_content = self._patch_route_schema_prefix_candidates(content, schemas_content)
                if patched_content != content:
                    operation_map[file_path] = DraftFileOperation(
                        file_path=file_path,
                        operation="replace",
                        content=patched_content,
                        reason="Pre-apply contract sync: let the resource route reuse the actual schema prefix declared in schemas.py.",
                    )
                    content = patched_content
            imported_names.update(self._normalized_imported_schema_names(content))
        if not self._needs_route_schema_contract_repair(imported_names, schemas_content):
            return list(operation_map.values())
        updated_schemas = schemas_content
        if "RequestDetail" in imported_names and "class RequestDetail" not in updated_schemas:
            if "Field" not in updated_schemas and "from pydantic import" in updated_schemas:
                updated_schemas = updated_schemas.replace("from pydantic import BaseModel, ConfigDict", "from pydantic import BaseModel, ConfigDict, Field")
            updated_schemas += (
                "\n\nclass RequestDetail(RequestSummary):\n"
                "    client_id: str\n"
                "    description: str | None = None\n"
                "    preferred_time_slots: List[TimeSlot] = Field(default_factory=list)\n"
                "    created_at: datetime | None = None\n"
                "    attachments: List[AttachmentMeta] = Field(default_factory=list)\n"
                "    comments: List[CommentOut] = Field(default_factory=list)\n"
                "    assignments: List[dict] = Field(default_factory=list)\n"
            )
        if updated_schemas == schemas_content:
            return operations
        operation_map[schemas_path] = DraftFileOperation(
            file_path=schemas_path,
            operation="replace",
            content=updated_schemas,
            reason="Pre-apply contract sync: keep schemas.py compatible with route imports before full checks.",
        )
        return list(operation_map.values())

    def _operation_or_workspace_content(
        self,
        workspace_id: str,
        draft_run_id: str,
        operation_map: dict[str, DraftFileOperation],
        file_path: str,
    ) -> str | None:
        operation = operation_map.get(file_path)
        if operation and operation.content is not None:
            return operation.content
        try:
            return self.workspace_service.read_file(workspace_id, file_path, run_id=draft_run_id)
        except Exception:
            return None
