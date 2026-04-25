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
