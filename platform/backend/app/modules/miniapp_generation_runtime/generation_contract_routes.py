from __future__ import annotations

import re

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync
from app.models.domain import DraftFileOperation

from app.modules.miniapp_generation_runtime.generation_contract_api_routes_crud import (
    MiniappGenerationContractApiRoutesCrud,
)
from app.modules.miniapp_generation_runtime.generation_contract_api_routes_runtime import (
    MiniappGenerationContractApiRoutesRuntime,
)
from app.modules.miniapp_generation_runtime.generation_contract_api_routes_support import (
    MiniappGenerationContractApiRoutesSupport,
)
from app.modules.miniapp_generation_runtime.generation_contract_page_sources import (
    MiniappGenerationContractPageSources,
)
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractRoutes(MiniappGenerationRuntimeOwner):
    def _synchronize_minimal_workflow_route_contracts(
        self,
        workspace_id: str,
        draft_run_id: str,
        operations: list[DraftFileOperation],
        contract_sync_mode: str = "bootstrap_only",
    ) -> list[DraftFileOperation]:
        operation_map = {operation.file_path: operation for operation in operations}
        route_templates = {
            "miniapp/app/routes/client.py": MiniappGenerationContractPageSources._deterministic_client_page_route_source(),
            "miniapp/app/routes/specialist.py": MiniappGenerationContractPageSources._deterministic_specialist_page_route_source(),
            "miniapp/app/routes/manager.py": MiniappGenerationContractPageSources._deterministic_manager_page_route_source(),
            "miniapp/app/routes/requests.py": MiniappGenerationContractApiRoutesCrud._deterministic_requests_route_source(),
            "miniapp/app/routes/comments.py": MiniappGenerationContractApiRoutesCrud._deterministic_comments_route_source(),
            "miniapp/app/routes/assignments.py": MiniappGenerationContractApiRoutesCrud._deterministic_assignments_route_source(),
            "miniapp/app/routes/profiles.py": MiniappGenerationContractApiRoutesSupport._deterministic_profiles_route_source(),
            "miniapp/app/routes/runtime.py": MiniappGenerationContractApiRoutesRuntime._deterministic_runtime_route_source(),
            "miniapp/app/routes/users.py": MiniappGenerationContractApiRoutesSupport._deterministic_users_route_source(),
            "miniapp/app/routes/workload.py": MiniappGenerationContractApiRoutesSupport._deterministic_workload_route_source(),
            "miniapp/app/routes/time_slots.py": MiniappGenerationContractApiRoutesSupport._deterministic_time_slots_route_source(),
        }
        bootstrap_only_paths = {
            "miniapp/app/routes/client.py",
            "miniapp/app/routes/specialist.py",
            "miniapp/app/routes/manager.py",
            "miniapp/app/routes/runtime.py",
            "miniapp/app/routes/profiles.py",
        }
        for file_path, template in route_templates.items():
            content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            if content is None:
                if file_path in bootstrap_only_paths:
                    operation_map[file_path] = DraftFileOperation(
                        file_path=file_path,
                        operation="replace",
                        content=template,
                        reason="Pre-apply contract sync: bootstrap a missing runtime route module from the template contract.",
                    )
                continue
            if self._route_module_needs_stub(content) and file_path in bootstrap_only_paths:
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=template,
                    reason="Pre-apply contract sync: replace an empty route stub with a minimal bootstrap contract.",
                )
                continue
            if self._route_module_requires_db_backed_repair(file_path, content) and file_path in bootstrap_only_paths:
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=template,
                    reason="Pre-apply contract sync: restore a hard-invariant DB-backed route module when placeholder persistence leaked into the draft.",
                )
                continue
            if contract_sync_mode == "repair_invariants" and file_path.endswith("/runtime.py"):
                normalized = self._normalize_runtime_route_module_source(content)
                if normalized != content:
                    operation_map[file_path] = DraftFileOperation(
                        file_path=file_path,
                        operation="replace",
                        content=normalized,
                        reason="Pre-apply contract sync: normalize runtime route ownership without regenerating the whole app route layer.",
                    )
        return list(operation_map.values())

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
    def _deterministic_requests_route_source() -> str:
        return MiniappGenerationContractApiRoutesCrud._deterministic_requests_route_source()

    @staticmethod
    def _deterministic_comments_route_source() -> str:
        return MiniappGenerationContractApiRoutesCrud._deterministic_comments_route_source()

    @staticmethod
    def _deterministic_assignments_route_source() -> str:
        return MiniappGenerationContractApiRoutesCrud._deterministic_assignments_route_source()

    @staticmethod
    def _deterministic_profiles_route_source() -> str:
        return MiniappGenerationContractApiRoutesSupport._deterministic_profiles_route_source()

    @staticmethod
    def _deterministic_users_route_source() -> str:
        return MiniappGenerationContractApiRoutesSupport._deterministic_users_route_source()

    @staticmethod
    def _deterministic_workload_route_source() -> str:
        return MiniappGenerationContractApiRoutesSupport._deterministic_workload_route_source()

    @staticmethod
    def _deterministic_runtime_route_source() -> str:
        return MiniappGenerationContractApiRoutesRuntime._deterministic_runtime_route_source()

    @staticmethod
    def _deterministic_time_slots_route_source() -> str:
        return MiniappGenerationContractApiRoutesSupport._deterministic_time_slots_route_source()
