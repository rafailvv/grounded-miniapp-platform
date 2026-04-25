from __future__ import annotations

import re

from app.modules.miniapp_contract.runtime_contract_normalization import MiniappRuntimeContractNormalization
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractRoutes(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _route_module_needs_stub(content: str) -> bool:
        normalized = str(content or "").strip()
        if not normalized:
            return True
        mutable_store_pattern = re.compile(
            r"^(?P<name>(?:REQUESTS|COMMENTS|ASSIGNMENTS|TIME_SLOTS|SPECIALISTS|USERS|PROFILE_STORE|[A-Z][A-Z0-9_]*(?:_STORE|_CACHE|_TABLE|_ITEMS)))\s*(?::[^=]+)?=\s*(?:\{|\[)",
            flags=re.MULTILINE,
        )
        compact = re.sub(r"\s+", " ", normalized)
        if compact in {"from fastapi import APIRouter router = APIRouter()", "router = APIRouter()"} or ("router = APIRouter()" in compact and "@router." not in compact):
            return True
        if mutable_store_pattern.search(normalized):
            return True
        if "from pydantic import BaseModel" in normalized or "from pydantic import BaseModel," in normalized:
            return True
        return False

    @staticmethod
    def _route_module_requires_db_backed_repair(file_path: str, content: str) -> bool:
        normalized_path = str(file_path or "").strip().replace("\\", "/")
        normalized = str(content or "")
        if normalized_path.endswith("/runtime.py"):
            return any(marker in normalized for marker in ("DEMO_REQUESTS", "/actions/{action_id}", "runtime_action("))
        if normalized_path.endswith("/profiles.py"):
            return any(marker in normalized for marker in ("DEFAULT_PROFILES", "_get_or_create(", "_get_or_create_profile_record("))
        return False

    @staticmethod
    def _strip_noncanonical_runtime_route_handlers(content: str) -> str:
        return MiniappRuntimeContractNormalization.strip_noncanonical_runtime_route_handlers(content)

    @staticmethod
    def _normalize_runtime_route_module_source(content: str) -> str:
        return MiniappRuntimeContractNormalization.normalize_runtime_route_module_source(content)
