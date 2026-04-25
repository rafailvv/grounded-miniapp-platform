from __future__ import annotations

import re
from typing import Any

from app.modules.miniapp_contract.runtime_contract_normalization import MiniappRuntimeContractNormalization
from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationContractRoutes(MiniappGenerationRuntimeOwner):
    @staticmethod
    def _strip_response_model_argument(content: str, model_name: str) -> str:
        updated = str(content or "")
        if not updated or not model_name:
            return updated
        patterns = (
            rf",\s*response_model\s*=\s*{re.escape(model_name)}",
            rf"response_model\s*=\s*{re.escape(model_name)}\s*,\s*",
            rf"response_model\s*=\s*{re.escape(model_name)}",
        )
        for pattern in patterns:
            updated = re.sub(pattern, "", updated)
        updated = re.sub(r"\(\s*,", "(", updated)
        updated = re.sub(r",\s*\)", ")", updated)
        return updated

    @staticmethod
    def _ensure_assignment_alias(content: str, alias_name: str, target_name: str) -> str:
        updated = str(content or "")
        alias_name = str(alias_name or "").strip()
        target_name = str(target_name or "").strip()
        if not updated or not alias_name or not target_name:
            return updated
        alias_line = f"{alias_name} = {target_name}"
        if re.search(rf"(?m)^{re.escape(alias_line)}$", updated):
            return updated
        schema_import_match = re.search(r"(?m)^from app\.schemas import [^\n]+$", updated)
        if schema_import_match is not None:
            insert_at = schema_import_match.end()
            return updated[:insert_at] + f"\n{alias_line}" + updated[insert_at:]
        return alias_line + "\n" + updated

    @staticmethod
    def _ensure_main_router_import(content: str, module_name: str, alias_name: str) -> str:
        updated = str(content or "")
        if not updated or not module_name or not alias_name:
            return updated
        import_line = f"from app.routes.{module_name} import router as {alias_name}"
        if re.search(rf"(?m)^{re.escape(import_line)}$", updated):
            return updated
        last_route_import = None
        for candidate in re.finditer(r"(?m)^from app\.routes\.[^\n]+$", updated):
            last_route_import = candidate
        if last_route_import is not None:
            insert_at = last_route_import.end()
            return updated[:insert_at] + "\n" + import_line + updated[insert_at:]
        last_import = None
        for candidate in re.finditer(r"(?m)^(?:from\s+\S+\s+import\s+[^\n]+|import\s+[^\n]+)$", updated):
            last_import = candidate
        if last_import is not None:
            insert_at = last_import.end()
            return updated[:insert_at] + "\n" + import_line + updated[insert_at:]
        return import_line + "\n" + updated

    @staticmethod
    def _ensure_main_router_include(content: str, alias_name: str) -> str:
        updated = str(content or "")
        include_line = f"app.include_router({alias_name})"
        if not updated or not alias_name or include_line in updated:
            return updated
        lines = updated.splitlines()
        insert_index: int | None = None
        for index, line in enumerate(lines):
            if line.strip().startswith("app.include_router("):
                insert_index = index + 1
        if insert_index is None:
            for index, line in enumerate(lines):
                if re.match(r"^\s*app\s*=\s*FastAPI\(", line):
                    insert_index = index + 1
                    break
        if insert_index is None:
            lines.append(include_line)
        else:
            lines.insert(insert_index, include_line)
        updated = "\n".join(lines)
        if str(content or "").endswith("\n"):
            updated += "\n"
        return updated

    @classmethod
    def _normalize_main_app_router_includes(
        cls,
        content: str,
        *,
        route_modules: list[str],
    ) -> str:
        updated = str(content or "")
        for module_name in [str(item or "").strip() for item in route_modules if str(item or "").strip()]:
            alias_name = f"{module_name}_router"
            updated = cls._ensure_main_router_import(updated, module_name, alias_name)
            updated = cls._ensure_main_router_include(updated, alias_name)
        return updated

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

    @staticmethod
    def _ensure_module_import_symbols(content: str, module_path: str, symbols: list[str]) -> str:
        updated = str(content or "")
        requested = [str(symbol or "").strip() for symbol in symbols if str(symbol or "").strip()]
        if not updated or not requested:
            return updated
        import_pattern = re.compile(rf"(?m)^from {re.escape(module_path)} import ([^\n]+)$")
        match = import_pattern.search(updated)
        if match:
            existing = [item.strip() for item in match.group(1).split(",") if item.strip()]
            merged: list[str] = []
            for item in [*existing, *requested]:
                if item not in merged:
                    merged.append(item)
            replacement = f"from {module_path} import {', '.join(merged)}"
            return updated[: match.start()] + replacement + updated[match.end() :]
        import_line = f"from {module_path} import {', '.join(dict.fromkeys(requested))}\n"
        last_import_match = None
        for candidate in re.finditer(r"(?m)^(?:from\s+\S+\s+import\s+[^\n]+|import\s+[^\n]+)$", updated):
            last_import_match = candidate
        if last_import_match is not None:
            insert_at = last_import_match.end()
            return updated[:insert_at] + "\n" + import_line + updated[insert_at:]
        return import_line + updated

    @classmethod
    def _normalize_entity_route_module_source(
        cls,
        content: str,
        entity_contract: dict[str, Any] | None,
    ) -> str:
        updated = MiniappRuntimeContractNormalization.normalize_future_annotations_import(content)
        contract = dict(entity_contract or {})
        api_path = str(contract.get("api_path") or "").strip()
        if api_path:
            updated = MiniappRuntimeContractNormalization.normalize_router_prefix(updated, api_path)
        schema_prefix = str(contract.get("schema_prefix") or "").strip()
        if not schema_prefix:
            return updated
        create_name = f"{schema_prefix}Create"
        update_name = f"{schema_prefix}Update"
        read_name = str(contract.get("read_schema_name") or f"{schema_prefix}Read").strip()
        list_name = str(contract.get("list_schema_name") or f"{schema_prefix}ListResponse").strip()
        status_name = f"{schema_prefix}Status"
        record_name = f"{schema_prefix}Record"
        payload_name = f"{schema_prefix}Payload"
        update_payload_name = f"{schema_prefix}UpdatePayload"
        base_model_class_names = MiniappRuntimeContractNormalization.top_level_base_model_class_names(updated)
        if base_model_class_names:
            import_symbols: list[str] = []
            schema_backed_names: set[str] = set()
            alias_assignments_to_strip = [read_name, payload_name, update_payload_name]

            if read_name in base_model_class_names or record_name in base_model_class_names or re.search(
                rf"(?m)^{re.escape(read_name)}\s*=\s*{re.escape(record_name)}\s*$",
                updated,
            ):
                import_symbols.append(read_name)
                schema_backed_names.update({read_name, record_name})
            if list_name in base_model_class_names:
                import_symbols.append(list_name)
                schema_backed_names.add(list_name)
            if create_name in base_model_class_names or payload_name in base_model_class_names:
                import_symbols.append(f"{create_name} as {payload_name}" if payload_name in base_model_class_names else create_name)
                schema_backed_names.update({create_name, payload_name})
            if update_name in base_model_class_names or update_payload_name in base_model_class_names:
                import_symbols.append(f"{update_name} as {update_payload_name}" if update_payload_name in base_model_class_names else update_name)
                schema_backed_names.update({update_name, update_payload_name})

            if import_symbols:
                updated = cls._ensure_module_import_symbols(updated, "app.schemas", import_symbols)
            updated = MiniappRuntimeContractNormalization.strip_top_level_class_definitions(updated, base_model_class_names)
            updated = MiniappRuntimeContractNormalization.strip_top_level_assignments(updated, alias_assignments_to_strip)
            updated = re.sub(r"(?m)^from pydantic import [^\n]*BaseModel[^\n]*\n?", "", updated)
            if record_name in schema_backed_names:
                updated = cls._ensure_assignment_alias(updated, record_name, read_name)
            for wrapper_name in [name for name in base_model_class_names if name not in schema_backed_names]:
                updated = cls._strip_response_model_argument(updated, wrapper_name)
                updated = re.sub(
                    rf"->\s*{re.escape(wrapper_name)}\b",
                    "-> dict[str, object]",
                    updated,
                )
                updated = updated.replace(f"{wrapper_name}(", "dict(")
            default_status = next(
                (
                    str(value).strip().lower()
                    for value in (contract.get("status_literals") or [])
                    if str(value).strip()
                ),
                "open",
            )
            updated = re.sub(
                rf"\bstatus\s*=\s*{re.escape(status_name)}\.[A-Za-z_][A-Za-z0-9_]*\.value",
                f'status="{default_status}"',
                updated,
            )
            updated = re.sub(
                rf"\b{re.escape(status_name)}\.[A-Za-z_][A-Za-z0-9_]*\.value\b",
                f'"{default_status}"',
                updated,
            )
            updated = re.sub(r"\b([A-Za-z_$][\w$.]*)\.status\.value\b", r"\1.status", updated)
            updated = re.sub(r"\n{3,}", "\n\n", updated)
        return updated

    @classmethod
    def _normalize_runtime_manifest_route_source(cls, content: str) -> str:
        updated = MiniappRuntimeContractNormalization.normalize_runtime_route_module_source(content)
        updated = MiniappRuntimeContractNormalization.normalize_router_prefix(updated, "/api/runtime")
        base_model_class_names = MiniappRuntimeContractNormalization.top_level_base_model_class_names(updated)
        if not base_model_class_names:
            return updated
        updated = MiniappRuntimeContractNormalization.strip_top_level_class_definitions(updated, base_model_class_names)
        updated = re.sub(r"(?m)^from pydantic import [^\n]*BaseModel[^\n]*\n?", "", updated)
        for class_name in base_model_class_names:
            updated = cls._strip_response_model_argument(updated, class_name)
            updated = updated.replace(f"list[{class_name}]", "list[dict[str, object]]")
            updated = re.sub(
                rf"->\s*{re.escape(class_name)}\b",
                "-> dict[str, object]",
                updated,
            )
            updated = updated.replace(f"{class_name}(", "dict(")
        updated = re.sub(r"\n{3,}", "\n\n", updated)
        return updated
