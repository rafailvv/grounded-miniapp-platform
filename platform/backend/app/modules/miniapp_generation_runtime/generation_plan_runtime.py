from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES

FORBIDDEN_ROUTE_MODULE_STEMS = {
    "__init__",
    "auth",
    "auth_telegram",
    "attachment",
    "attachments",
    "event",
    "events",
    "login",
    "me",
    "notification",
    "notifications",
    "polling",
    "push",
    "realtime",
    "session",
    "sessions",
    "sse",
    "telegram_auth",
    "upload",
    "uploads",
    "webhook",
    "webhooks",
    "websocket",
    "worklog",
}

if TYPE_CHECKING:
    from app.services.miniapp_generation.service import GenerationService


class MiniappGenerationPlanRuntime:
    _GENERIC_ENTITY_STEMS = {"workflowrequests", "records", "submissions"}

    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def prepare_runtime_plan(
        self,
        *,
        workspace_id: str,
        draft_source,
        grounded_spec: GroundedSpecModel,
        entity_contract: dict[str, Any] | None,
        role_scope: list[str],
        plan_result: dict[str, Any],
    ) -> dict[str, Any]:
        service = self.service
        service._normalize_runtime_python_paths_in_plan(plan_result)
        if isinstance(plan_result.get("page_graph"), dict):
            plan_result["page_graph"] = service.generation_targeting.sanitize_page_graph_role_entries(
                dict(plan_result["page_graph"])
            )
        plan_result["target_files"] = list(dict.fromkeys(plan_result.get("target_files") or []))
        plan_result["backend_targets"] = service._sanitize_backend_targets(
            list(dict.fromkeys(plan_result.get("backend_targets") or []))
        )
        inferred_backend_targets = list(
            dict.fromkeys(
                [
                    *self.detect_missing_backend_contract_targets_from_page_graph(
                        page_graph=plan_result.get("page_graph") or {},
                        current_target_files=plan_result["target_files"],
                        backend_targets=plan_result["backend_targets"],
                        entity_contract=entity_contract,
                    ),
                    *self.detect_missing_backend_contract_targets_from_spec(
                        grounded_spec=grounded_spec,
                        page_graph=plan_result.get("page_graph") or {},
                        current_target_files=plan_result["target_files"],
                        backend_targets=plan_result["backend_targets"],
                        entity_contract=entity_contract,
                    ),
                ]
            )
        )
        if inferred_backend_targets:
            plan_result["backend_targets"] = service._sanitize_backend_targets(
                [
                    *plan_result["backend_targets"],
                    *inferred_backend_targets,
                ]
            )
            plan_result["target_files"] = list(
                dict.fromkeys(
                    [
                        *plan_result["target_files"],
                        *plan_result["backend_targets"],
                    ]
                )
            )
        plan_result["files_to_read"] = list(dict.fromkeys(plan_result.get("files_to_read") or []))
        plan_result["shared_files"] = list(dict.fromkeys(plan_result.get("shared_files") or []))
        plan_result["target_files"] = service._sanitize_planner_target_files(
            target_files=plan_result["target_files"],
            backend_targets=plan_result["backend_targets"],
            page_graph=plan_result["page_graph"],
        )
        target_set = set(plan_result["target_files"])
        plan_result["backend_targets"] = [path for path in plan_result["backend_targets"] if path in target_set]
        plan_result["shared_files"] = [path for path in plan_result["shared_files"] if path in target_set]
        plan_result["files_to_read"] = list(
            dict.fromkeys(
                [
                    *[
                        path
                        for path in plan_result["files_to_read"]
                        if path in target_set or path in DESIGN_REFERENCE_FILES
                    ],
                    *[path for path in DESIGN_REFERENCE_FILES if (draft_source / path).exists()],
                ]
            )
        )
        plan_result["generation_clusters"] = service._build_generation_clusters(plan_result["target_files"])
        page_graph_roles = plan_result.get("page_graph", {}).get("roles") if isinstance(plan_result.get("page_graph"), dict) else {}
        if isinstance(page_graph_roles, dict):
            plan_result["execution_plan"] = service._build_execution_plan(
                role_scope=role_scope,
                roles=page_graph_roles,
                shared_files=plan_result["shared_files"],
                backend_targets=plan_result["backend_targets"],
                target_files=plan_result["target_files"],
                generation_clusters=plan_result["generation_clusters"],
            )
        return plan_result

    @classmethod
    def detect_missing_backend_contract_targets_from_page_graph(
        cls,
        *,
        page_graph: dict[str, Any],
        current_target_files: list[str],
        backend_targets: list[str],
        entity_contract: dict[str, Any] | None = None,
    ) -> list[str]:
        endpoint_names: set[str] = set()
        for role_payload in (page_graph.get("roles") or {}).values():
            if not isinstance(role_payload, dict):
                continue
            for page in role_payload.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                for dependency in page.get("data_dependencies") or []:
                    if not isinstance(dependency, str):
                        continue
                    endpoint_names.update(cls.endpoint_names_from_dependency_text(dependency))
        if not endpoint_names:
            return []
        existing_targets = set(current_target_files) | set(backend_targets)
        inferred: list[str] = []
        router_path = "miniapp/app/main.py"
        for contract_path in ("miniapp/app/db.py", "miniapp/app/schemas.py"):
            if contract_path not in existing_targets:
                inferred.append(contract_path)
        for endpoint_name in sorted(endpoint_names):
            normalized_endpoint_name = cls.normalize_endpoint_name_for_entity_contract(
                endpoint_name,
                entity_contract=entity_contract,
            )
            if cls.is_forbidden_endpoint_name(normalized_endpoint_name):
                continue
            inferred_path = cls.route_module_path_for_endpoint_name(normalized_endpoint_name)
            if inferred_path not in existing_targets:
                inferred.append(inferred_path)
            if router_path not in existing_targets:
                inferred.append(router_path)
        entity_route_file = cls.entity_route_file(entity_contract)
        if entity_route_file and entity_route_file not in existing_targets and endpoint_names:
            inferred.append(entity_route_file)
        return list(dict.fromkeys(inferred))

    @classmethod
    def detect_missing_backend_contract_targets_from_spec(
        cls,
        *,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, Any],
        current_target_files: list[str],
        backend_targets: list[str],
        entity_contract: dict[str, Any] | None = None,
    ) -> list[str]:
        existing_targets = set(current_target_files) | set(backend_targets)
        inferred: list[str] = []
        endpoint_names: set[str] = set()
        for requirement in grounded_spec.api_requirements:
            path = str(requirement.path or "").strip()
            if not path:
                continue
            for match in re.finditer(r"/api/([a-zA-Z0-9_-]+)", path):
                endpoint_names.add(match.group(1).strip().lower())
        if grounded_spec.api_requirements:
            for contract_path in ("miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py"):
                if contract_path not in existing_targets:
                    inferred.append(contract_path)
        has_profile_pages = any(
            isinstance(page, dict) and str(page.get("route_path") or "").rstrip("/") == "/profile"
            for role_payload in (page_graph.get("roles") or {}).values()
            if isinstance(role_payload, dict)
            for page in (role_payload.get("pages") or [])
        )
        if has_profile_pages and "miniapp/app/routes/profiles.py" not in existing_targets:
            inferred.append("miniapp/app/routes/profiles.py")
        for endpoint_name in sorted(endpoint_names):
            normalized_endpoint_name = cls.normalize_endpoint_name_for_entity_contract(
                endpoint_name,
                entity_contract=entity_contract,
            )
            if cls.is_forbidden_endpoint_name(normalized_endpoint_name):
                continue
            inferred_path = cls.route_module_path_for_endpoint_name(normalized_endpoint_name)
            if inferred_path not in existing_targets:
                inferred.append(inferred_path)
        entity_route_file = cls.entity_route_file(entity_contract)
        if entity_route_file and entity_route_file not in existing_targets and grounded_spec.api_requirements:
            inferred.append(entity_route_file)
        return list(dict.fromkeys(inferred))

    @staticmethod
    def endpoint_names_from_dependency_text(dependency: str) -> set[str]:
        endpoint_names: set[str] = set()
        for match in re.finditer(r"/api/([a-zA-Z0-9_-]+)", dependency):
            endpoint_names.add(match.group(1).strip().lower())
        for match in re.finditer(r"(?:GET|POST|PUT|PATCH|DELETE)\s+/([a-zA-Z0-9_-]+)", dependency, flags=re.IGNORECASE):
            endpoint_name = match.group(1).strip().lower()
            if endpoint_name in {"api", "client", "specialist", "manager", "profile", "profiles", "health", "auth", "login", "me"}:
                continue
            endpoint_names.add(endpoint_name)
        return endpoint_names

    @staticmethod
    def _snake_case_filename(name: str) -> str:
        parts = name.split(".")
        stem = parts[0]
        suffix = f".{'.'.join(parts[1:])}" if len(parts) > 1 else ""
        if stem.startswith("__") and stem.endswith("__"):
            return f"{stem.lower()}{suffix.lower()}"
        normalized = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
        normalized = re.sub(r"_+", "_", normalized)
        return f"{normalized or 'file'}{suffix.lower()}"

    @staticmethod
    def normalize_runtime_python_path(path: str) -> str:
        return MiniappMaterializationService.normalize_runtime_python_path(path)

    @classmethod
    def route_module_path_for_endpoint_name(cls, endpoint_name: str) -> str:
        filename = cls.canonical_endpoint_name(endpoint_name)
        return cls.normalize_runtime_python_path(f"miniapp/app/routes/{filename}.py")

    @classmethod
    def entity_route_file(cls, entity_contract: dict[str, Any] | None) -> str | None:
        route_file = str((entity_contract or {}).get("route_file") or "").strip().replace("\\", "/")
        if not route_file.startswith("miniapp/app/routes/") or not route_file.endswith(".py"):
            return None
        return cls.normalize_runtime_python_path(route_file)

    @classmethod
    def normalize_endpoint_name_for_entity_contract(
        cls,
        endpoint_name: str,
        *,
        entity_contract: dict[str, Any] | None,
    ) -> str:
        normalized = cls.canonical_endpoint_name(endpoint_name)
        if not entity_contract:
            return normalized
        route_file = cls.entity_route_file(entity_contract)
        if not route_file:
            return normalized
        entity_stem = route_file.rsplit("/", 1)[-1].removesuffix(".py").lower()
        contract_api_stem = str((entity_contract or {}).get("api_path") or "").removeprefix("/api/").split("/", 1)[0].strip().lower()
        if normalized in cls._GENERIC_ENTITY_STEMS:
            return entity_stem
        if contract_api_stem and normalized == contract_api_stem:
            return entity_stem
        return normalized

    @classmethod
    def is_forbidden_endpoint_name(cls, endpoint_name: str) -> bool:
        normalized = cls.canonical_endpoint_name(endpoint_name)
        return normalized in FORBIDDEN_ROUTE_MODULE_STEMS or normalized in {"health", "profiles", "auth", "login", "me"}

    @classmethod
    def canonical_endpoint_name(cls, endpoint_name: str) -> str:
        normalized = cls._snake_case_filename((endpoint_name or "").strip()).lower()
        return normalized
