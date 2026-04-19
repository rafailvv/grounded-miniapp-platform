from __future__ import annotations

import re
from pathlib import Path

from app.modules.miniapp_generation_runtime.generation_contract_api_routes_support import MiniappGenerationContractApiRoutesSupport
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner
from app.models.domain import DraftFileOperation


class MiniappGenerationContractSchema(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _needs_canonical_bookingrequests_route_repair(content: str) -> bool:
        normalized = str(content or "")
        lowered = normalized.lower()
        if not normalized.strip():
            return True
        if "/api/submissions/{table}" in lowered:
            return True
        if "@router.post(\"\")" not in normalized or "@router.get(\"\")" not in normalized or "@router.put(\"/{item_id}\")" not in normalized:
            return True
        return any(
            marker in normalized
            for marker in (
                "class BookingRequestRecord(Base):",
                "class BookingRequestCreate(",
                "class BookingRequestUpdate(",
                "class BookingRequestItem(",
                "class BookingRequestList(",
                "class BookingRequestCreateResponse(",
                "BookingRequestList,",
                "id=str(uuid4())",
                "owner_specialist_id",
                "owner_role",
                "issued_at",
                "returned_at",
                "bookingrequest_id=record.bookingrequest_id",
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
        if self._needs_profile_contract_repair(profiles_content):
            operation_map[profiles_path] = DraftFileOperation(
                file_path=profiles_path,
                operation="replace",
                content=MiniappGenerationContractApiRoutesSupport._deterministic_profiles_route_source(),
                reason="Pre-apply contract sync: strip placeholder profile persistence and restore DB-backed empty-profile behavior.",
            )
            profiles_content = operation_map[profiles_path].content
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
        imported_names: set[str] = set()
        for file_path in list(operation_map):
            if not (file_path.startswith("miniapp/app/routes/") and file_path.endswith(".py")):
                continue
            content = self._operation_or_workspace_content(workspace_id, draft_run_id, operation_map, file_path)
            if not content:
                continue
            if (
                Path(file_path).stem == "bookingrequests"
                and db_content
                and "class BookingRequestRecord" in db_content
                and "class BookingRequestCreate" in schemas_content
                and "class BookingRequestUpdate" in schemas_content
                and (
                    "class BookingRequestListResponse" in schemas_content
                    or "class BookingRequestList" in schemas_content
                )
                and (
                    "class BookingRequestRead" in schemas_content
                    or "class BookingRequest" in schemas_content
                )
                and self._needs_canonical_bookingrequests_route_repair(content)
            ):
                operation_map[file_path] = DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=MiniappGenerationContractApiRoutesSupport._deterministic_bookingrequests_route_source(),
                    reason="Pre-apply contract sync: keep bookingrequests.py aligned with db.py and schemas.py instead of declaring inline ORM and Pydantic models.",
                )
                continue
            for match in re.finditer(r"from\s+app\.schemas\s+import\s+\((.*?)\)", content, flags=re.DOTALL):
                imported_names.update({part.strip() for part in match.group(1).replace("\n", " ").split(",") if part.strip()})
            for match in re.finditer(r"from\s+app\.schemas\s+import\s+([A-Za-z0-9_, ]+)", content):
                imported_names.update({part.strip() for part in match.group(1).split(",") if part.strip()})
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
