from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.models.grounded_spec import GroundedSpecModel
from app.modules.miniapp_materialization.materialization import MiniappMaterializationService
from app.services.miniapp_generation.constants import DESIGN_REFERENCE_FILES

CANONICAL_ENDPOINT_ALIASES = {
    "submission": "requests",
    "submissions": "requests",
    "booking": "requests",
    "bookings": "requests",
    "appointment": "requests",
    "appointments": "requests",
    "task": "requests",
    "tasks": "requests",
    "assignee": "assignments",
    "owners": "assignments",
    "owner": "assignments",
    "notes": "comments",
    "note": "comments",
    "specialist": "users",
    "specialists": "users",
}

FORBIDDEN_ROUTE_MODULE_STEMS = {
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
    def __init__(self, service: "GenerationService") -> None:
        self.service = service

    def prepare_runtime_plan(
        self,
        *,
        workspace_id: str,
        draft_source,
        grounded_spec: GroundedSpecModel,
        role_scope: list[str],
        plan_result: dict[str, Any],
    ) -> dict[str, Any]:
        service = self.service
        service._normalize_runtime_python_paths_in_plan(plan_result)
        service._expand_page_asset_targets_in_plan(plan_result)
        plan_result["target_files"] = list(dict.fromkeys(plan_result.get("target_files") or []))
        plan_result["backend_targets"] = service._sanitize_backend_targets(
            list(dict.fromkeys(plan_result.get("backend_targets") or []))
        )
        plan_result["files_to_read"] = list(dict.fromkeys(plan_result.get("files_to_read") or []))
        plan_result["shared_files"] = list(dict.fromkeys(plan_result.get("shared_files") or []))
        plan_result["target_files"] = service._sanitize_planner_target_files(
            target_files=plan_result["target_files"],
            backend_targets=plan_result["backend_targets"],
            page_graph=plan_result["page_graph"],
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
        proactive_contract_targets = self.detect_missing_backend_contract_targets_from_page_graph(
            page_graph=plan_result["page_graph"],
            current_target_files=plan_result["target_files"],
            backend_targets=plan_result["backend_targets"],
        )
        if proactive_contract_targets:
            plan_result["target_files"] = list(dict.fromkeys([*plan_result["target_files"], *proactive_contract_targets]))
            plan_result["backend_targets"] = service._sanitize_backend_targets(
                list(dict.fromkeys([*plan_result["backend_targets"], *proactive_contract_targets]))
            )
            if isinstance(plan_result.get("page_graph"), dict):
                existing_backend_targets = list(plan_result["page_graph"].get("backend_targets") or [])
                plan_result["page_graph"]["backend_targets"] = service._sanitize_backend_targets(
                    list(dict.fromkeys([*existing_backend_targets, *proactive_contract_targets]))
                )
            plan_result["generation_clusters"] = service._build_generation_clusters(plan_result["target_files"])
            service._append_trace(
                workspace_id,
                "planner_contract_completed",
                "Planner targets were proactively expanded to include backend contract files before code generation.",
                {"added_targets": proactive_contract_targets},
            )
        spec_contract_targets = self.detect_missing_backend_contract_targets_from_spec(
            grounded_spec=grounded_spec,
            page_graph=plan_result["page_graph"],
            current_target_files=plan_result["target_files"],
            backend_targets=plan_result["backend_targets"],
        )
        if spec_contract_targets:
            plan_result["target_files"] = list(dict.fromkeys([*plan_result["target_files"], *spec_contract_targets]))
            plan_result["backend_targets"] = service._sanitize_backend_targets(
                list(dict.fromkeys([*plan_result["backend_targets"], *spec_contract_targets]))
            )
            service._append_trace(
                workspace_id,
                "spec_contract_completed",
                "Grounded spec targets were proactively expanded to include required backend contract files before code generation.",
                {"added_targets": spec_contract_targets},
            )
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
            if cls.is_forbidden_endpoint_name(endpoint_name):
                continue
            inferred_path = cls.route_module_path_for_endpoint_name(endpoint_name)
            if inferred_path not in existing_targets:
                inferred.append(inferred_path)
            if router_path not in existing_targets:
                inferred.append(router_path)
        return list(dict.fromkeys(inferred))

    @classmethod
    def detect_missing_backend_contract_targets_from_spec(
        cls,
        *,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, Any],
        current_target_files: list[str],
        backend_targets: list[str],
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
            if cls.is_forbidden_endpoint_name(endpoint_name):
                continue
            inferred_path = cls.route_module_path_for_endpoint_name(endpoint_name)
            if inferred_path not in existing_targets:
                inferred.append(inferred_path)
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
    def is_forbidden_endpoint_name(cls, endpoint_name: str) -> bool:
        normalized = cls.canonical_endpoint_name(endpoint_name)
        return normalized in FORBIDDEN_ROUTE_MODULE_STEMS or normalized in {"health", "profiles", "auth", "login", "me"}

    @classmethod
    def canonical_endpoint_name(cls, endpoint_name: str) -> str:
        normalized = cls._snake_case_filename((endpoint_name or "").strip()).lower()
        return CANONICAL_ENDPOINT_ALIASES.get(normalized, normalized)
