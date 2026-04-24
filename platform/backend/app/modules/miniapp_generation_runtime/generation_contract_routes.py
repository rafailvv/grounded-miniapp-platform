from __future__ import annotations

import re
from pathlib import Path

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync
from app.models.domain import DraftFileOperation

from app.modules.miniapp_generation_runtime.generation_contract_api_routes_runtime import (
    MiniappGenerationContractApiRoutesRuntime,
)
from app.modules.miniapp_generation_runtime.generation_contract_api_routes_crud import (
    MiniappGenerationContractApiRoutesCrud,
)
from app.modules.miniapp_generation_runtime.generation_contract_api_routes_support import (
    MiniappGenerationContractApiRoutesSupport,
)
from app.modules.miniapp_generation_runtime.generation_contract_page_sources import (
    MiniappGenerationContractPageSources,
)
from app.modules.miniapp_generation_runtime.generation_plan_runtime import FORBIDDEN_ROUTE_MODULE_STEMS
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractRoutes(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _template_app_root() -> Path:
        return Path(__file__).resolve().parents[5] / "runtime" / "templates" / "base-miniapp" / "miniapp" / "app"

    @classmethod
    def _template_source_for_path(cls, file_path: str) -> str | None:
        normalized = str(file_path or "").strip().replace("\\", "/")
        template_root = cls._template_app_root()
        mapping = {
            "miniapp/app/main.py": template_root / "main.py",
            "miniapp/app/db.py": template_root / "db.py",
            "miniapp/app/schemas.py": template_root / "schemas.py",
            "miniapp/app/generated/__init__.py": template_root / "generated" / "__init__.py",
            "miniapp/app/generated/route_manifest.json": template_root / "generated" / "route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json": template_root / "generated" / "runtime_manifest.json",
            "miniapp/app/generated/static_runtime_manifest.json": template_root / "generated" / "static_runtime_manifest.json",
            "miniapp/app/generated/role_seed.json": template_root / "generated" / "role_seed.json",
            "miniapp/app/generated/role_experience.json": template_root / "generated" / "role_experience.json",
        }
        source_path = mapping.get(normalized)
        if source_path is None or not source_path.exists():
            return None
        return source_path.read_text(encoding="utf-8")

    @staticmethod
    def _deterministic_route_source_for_path(file_path: str) -> str | None:
        normalized = str(file_path or "").strip().replace("\\", "/")
        mapping = {
            "miniapp/app/routes/client.py": MiniappGenerationContractPageSources._deterministic_client_page_route_source,
            "miniapp/app/routes/specialist.py": MiniappGenerationContractPageSources._deterministic_specialist_page_route_source,
            "miniapp/app/routes/manager.py": MiniappGenerationContractPageSources._deterministic_manager_page_route_source,
            "miniapp/app/routes/profiles.py": MiniappGenerationContractApiRoutesSupport._deterministic_profiles_route_source,
            "miniapp/app/routes/runtime.py": MiniappGenerationContractApiRoutesRuntime._deterministic_runtime_route_source,
            "miniapp/app/routes/users.py": MiniappGenerationContractApiRoutesSupport._deterministic_users_route_source,
            "miniapp/app/routes/workload.py": MiniappGenerationContractApiRoutesSupport._deterministic_workload_route_source,
            "miniapp/app/routes/time_slots.py": MiniappGenerationContractApiRoutesSupport._deterministic_time_slots_route_source,
            "miniapp/app/routes/comments.py": MiniappGenerationContractApiRoutesCrud._deterministic_comments_route_source,
            "miniapp/app/routes/assignments.py": MiniappGenerationContractApiRoutesCrud._deterministic_assignments_route_source,
        }
        source_builder = mapping.get(normalized)
        if source_builder is not None:
            return source_builder()
        if normalized.startswith("miniapp/app/routes/") and normalized.endswith(".py"):
            stem = Path(normalized).stem.lower()
            if stem not in FORBIDDEN_ROUTE_MODULE_STEMS and stem not in {
                "__init__",
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
            }:
                return MiniappGenerationContractApiRoutesCrud._deterministic_resource_route_source(stem)
        return MiniappGenerationContractRoutes._template_source_for_path(normalized)

    def _synchronize_minimal_workflow_route_contracts(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "bootstrap_only",
    ) -> list[DraftFileOperation]:
        del workspace_id, draft_run_id, contract_sync_mode
        return list(operations)

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
        return MiniappRuntimeContractSync.strip_noncanonical_runtime_route_handlers(content)

    @staticmethod
    def _normalize_runtime_route_module_source(content: str) -> str:
        return MiniappRuntimeContractSync.normalize_runtime_route_module_source(content)

    @staticmethod
    def _deterministic_client_page_route_source() -> str:
        return MiniappGenerationContractPageSources._deterministic_client_page_route_source()

    @staticmethod
    def _deterministic_specialist_page_route_source() -> str:
        return MiniappGenerationContractPageSources._deterministic_specialist_page_route_source()

    @staticmethod
    def _deterministic_manager_page_route_source() -> str:
        return MiniappGenerationContractPageSources._deterministic_manager_page_route_source()

    @staticmethod
    def _deterministic_main_runtime_source(route_modules: list[str]) -> str:
        return MiniappGenerationContractPageSources._deterministic_main_runtime_source(route_modules)

    @staticmethod
    def _deterministic_profiles_route_source() -> str:
        return MiniappGenerationContractApiRoutesSupport._deterministic_profiles_route_source()

    @staticmethod
    def _deterministic_runtime_route_source() -> str:
        return MiniappGenerationContractApiRoutesRuntime._deterministic_runtime_route_source()
