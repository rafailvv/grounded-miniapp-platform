from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from app.models.common import GenerationMode, StrictModel
from app.models.domain import new_id
from app.services.workflow_acceptance import extract_prompt_planning_hints, normalized_generation_mode
from app.validators.static_analysis import extract_declared_routes, extract_frontend_api_refs, normalize_api_path


ROLE_ORDER = ("client", "specialist", "manager")
RESERVED_WORKFLOW_FIELD_KEYS = {
    "id",
    "item_id",
    "created_by",
    "updated_by",
}


class MiniAppEndpoint(StrictModel):
    endpoint_id: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    purpose: str
    resource: str
    role: Literal["client", "specialist", "manager", "system"] = "system"
    request_fields: list[str] = Field(default_factory=list)
    response_shape: str = "json"


class MiniAppResource(StrictModel):
    resource_id: str
    slug: str
    name: str
    display_name: str
    fields: list[str] = Field(default_factory=list)
    field_labels: dict[str, str] = Field(default_factory=dict)
    role_field_labels: dict[str, dict[str, str]] = Field(default_factory=dict)
    role_actions: dict[str, list[str]] = Field(default_factory=dict)
    source_roles: list[str] = Field(default_factory=list)
    update_roles: list[str] = Field(default_factory=list)
    observer_roles: list[str] = Field(default_factory=list)
    status_values: list[str] = Field(default_factory=list)
    endpoints: list[MiniAppEndpoint] = Field(default_factory=list)


class MiniAppScreen(StrictModel):
    screen_id: str
    role: Literal["client", "specialist", "manager"]
    route_path: str
    file_path: str
    script_path: str
    style_path: str
    navigation_label: str
    purpose: str
    contract_owned: bool = False


class AllowedFileGraph(StrictModel):
    contract_owned_paths: list[str] = Field(default_factory=list)
    writable_globs: list[str] = Field(default_factory=list)
    readonly_paths: list[str] = Field(default_factory=list)
    blocked_globs: list[str] = Field(default_factory=list)


class RepairRecipe(StrictModel):
    recipe_id: str = Field(default_factory=lambda: new_id("repair"))
    issue_code: str
    severity: Literal["low", "medium", "high", "critical"] = "high"
    frontend_ref: str | None = None
    expected_route: str | None = None
    declared_routes: list[str] = Field(default_factory=list)
    manifest_routes: list[str] = Field(default_factory=list)
    why_mismatch: str
    suggested_patch_target: str
    auto_fixable: bool = True
    validator_may_be_stale: bool = False


class RouteRegistrySnapshot(StrictModel):
    snapshot_id: str = Field(default_factory=lambda: new_id("registry"))
    contract_id: str | None = None
    declared_routes: list[str] = Field(default_factory=list)
    frontend_refs: list[str] = Field(default_factory=list)
    manifest_routes: list[str] = Field(default_factory=list)
    contract_routes: list[str] = Field(default_factory=list)
    drift_issues: list[dict[str, Any]] = Field(default_factory=list)
    repair_recipes: list[RepairRecipe] = Field(default_factory=list)
    regenerated_files: list[str] = Field(default_factory=list)
    status: Literal["passed", "drift", "missing_contract"] = "passed"


class MiniAppContract(StrictModel):
    contract_id: str = Field(default_factory=lambda: new_id("miniapp_contract"))
    version: Literal["grounded.miniapp.contract.v1"] = "grounded.miniapp.contract.v1"
    workspace_id: str
    run_id: str
    prompt_summary: str
    generation_mode: str
    intent: str
    roles: list[Literal["client", "specialist", "manager"]] = Field(default_factory=lambda: list(ROLE_ORDER))
    resources: list[MiniAppResource] = Field(default_factory=list)
    endpoints: list[MiniAppEndpoint] = Field(default_factory=list)
    screens: list[MiniAppScreen] = Field(default_factory=list)
    allowed_file_graph: AllowedFileGraph
    acceptance_summary: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)


class MiniAppContractCompiler:
    @classmethod
    def compile(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        prompt: str,
        intent: str | None,
        generation_mode: GenerationMode | str | None,
        acceptance_contract: dict[str, Any] | None = None,
        implementation_plan: dict[str, Any] | None = None,
        prompt_analysis: dict[str, Any] | None = None,
    ) -> MiniAppContract:
        mode_value = normalized_generation_mode(generation_mode) or GenerationMode.BALANCED.value
        intent_value = str(intent or "").strip().lower() or "create"
        contract_hints = (acceptance_contract or {}).get("prompt_hints") if isinstance((acceptance_contract or {}).get("prompt_hints"), dict) else None
        hints = contract_hints or extract_prompt_planning_hints(prompt, prompt_analysis=prompt_analysis)
        slug_source = str(hints.get("resource_hint") or "").strip()
        resources: list[MiniAppResource] = []
        if slug_source:
            slug_source = cls._resource_display_source(slug_source)
            slug = cls._plural_slug(slug_source)
            display_name = cls._display_name(slug_source)
            resources = [
                cls._resource(
                    slug=slug,
                    display_name=display_name,
                    field_hints=[str(item) for item in hints.get("field_hints") or [] if str(item).strip()],
                    role_field_hints={
                        role: [str(item) for item in (items or []) if str(item).strip()]
                        for role, items in dict(hints.get("role_field_hints") or {}).items()
                    },
                    role_actions={
                        role: [str(item) for item in (items or []) if str(item).strip()]
                        for role, items in dict(hints.get("role_action_prompts") or {}).items()
                    },
                    role_state_contract=dict(hints.get("role_state_contract") or {}),
                )
            ]
        screens = cls._screens()
        endpoints = [endpoint for resource in resources for endpoint in resource.endpoints]
        allowed_graph = AllowedFileGraph(
            contract_owned_paths=MiniAppContractMaterializer.contract_owned_paths(),
            writable_globs=[
                "miniapp/app/static/client/**",
                "miniapp/app/static/specialist/**",
                "miniapp/app/static/manager/**",
                "miniapp/app/static/shared/**",
                "miniapp/app/routes/**",
                "miniapp/app/schemas.py",
                "miniapp/app/db.py",
                "miniapp/app/main.py",
            ],
            readonly_paths=[
                "miniapp/app/routes/role_pages.py",
                "miniapp/app/routes/role_routes.py",
            ],
            blocked_globs=[
                "miniapp/app/generated/**",
            ],
        )
        contract = MiniAppContract(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt_summary=str(hints.get("prompt_summary") or prompt or "")[:1200],
            generation_mode=mode_value,
            intent=intent_value,
            resources=resources,
            endpoints=endpoints,
            screens=screens,
            allowed_file_graph=allowed_graph,
            acceptance_summary=cls.acceptance_summary_for(
                endpoints=endpoints,
                base_contract=acceptance_contract,
                implementation_plan=implementation_plan,
                mode_value=mode_value,
            ),
        )
        contract.artifacts = {
            "miniapp_contract": "miniapp/app/generated/miniapp_contract.json",
            "route_manifest": "miniapp/app/generated/route_manifest.json",
            "validator_metadata": "miniapp/app/generated/contract_validator.json",
        }
        return contract

    @staticmethod
    def acceptance_summary_for(
        *,
        endpoints: list[MiniAppEndpoint],
        base_contract: dict[str, Any] | None,
        implementation_plan: dict[str, Any] | None,
        mode_value: str,
    ) -> dict[str, Any]:
        contract = dict(base_contract or {})
        contract["required"] = True
        contract["generation_mode"] = mode_value
        contract["roles"] = list(ROLE_ORDER)
        contract["required_endpoints"] = [
            {"method": endpoint.method, "path": endpoint.path, "purpose": endpoint.purpose}
            for endpoint in endpoints
        ]
        contract.setdefault("features", {})
        contract["features"] = {
            **dict(contract.get("features") or {}),
            "prompt_contract_v1": True,
            "platform_product_scaffold": False,
        }
        contract.setdefault("page_contract", {})
        contract["page_contract"] = {
            **dict(contract.get("page_contract") or {}),
            "route_manifest_required": True,
            "single_source_of_truth": "user_prompt_and_code",
        }
        if implementation_plan is not None:
            implementation_plan["prompt_contract_v1"] = {
                "enabled": True,
                "metadata_only": True,
                "materialized_runtime": False,
                "materialized_tests": False,
                "contract_owned_paths": MiniAppContractMaterializer.contract_owned_paths(),
            }
            implementation_plan.setdefault("api_contract", {})
            implementation_plan["api_contract"] = {
                **dict(implementation_plan.get("api_contract") or {}),
                "required_endpoints": contract["required_endpoints"],
                "api_routes_owned_by_agent": True,
                "single_source_of_truth": "user_prompt_and_code",
            }
        return contract

    @classmethod
    def _resource(
        cls,
        *,
        slug: str,
        display_name: str,
        field_hints: list[str] | None = None,
        role_field_hints: dict[str, list[str]] | None = None,
        role_actions: dict[str, list[str]] | None = None,
        role_state_contract: dict[str, Any] | None = None,
    ) -> MiniAppResource:
        role_field_labels, field_labels = cls._role_field_labels(role_field_hints or {}, field_hints or [])
        role_actions = cls._normalized_role_actions(role_actions or {})
        source_roles, update_roles, observer_roles, status_values = cls._resource_role_state(
            role_field_labels=role_field_labels,
            role_actions=role_actions,
            role_state_contract=role_state_contract or {},
        )
        endpoints: list[MiniAppEndpoint] = []
        all_role_labels: dict[str, str] = {}
        for role in ROLE_ORDER:
            all_role_labels.update(dict(role_field_labels.get(role) or {}))
        return MiniAppResource(
            resource_id=slug,
            slug=slug,
            name=slug,
            display_name=display_name,
            fields=list(dict.fromkeys([*field_labels.keys(), *all_role_labels.keys()])),
            field_labels=field_labels,
            role_field_labels=role_field_labels,
            role_actions=role_actions,
            source_roles=source_roles,
            update_roles=update_roles,
            observer_roles=observer_roles,
            status_values=status_values,
            endpoints=endpoints,
        )

    @staticmethod
    def _normalized_roles(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for item in values:
            role = str(item or "").strip().lower()
            if role in ROLE_ORDER and role not in result:
                result.append(role)
        return result

    @classmethod
    def _normalized_role_actions(cls, values: dict[str, list[str]]) -> dict[str, list[str]]:
        actions: dict[str, list[str]] = {role: [] for role in ROLE_ORDER}
        for role in ROLE_ORDER:
            seen: set[str] = set()
            for item in (values.get(role) or [])[:4]:
                action = " ".join(str(item or "").split()).strip(" .:-")
                if len(action) < 2 or action in seen:
                    continue
                seen.add(action)
                actions[role].append(action[:120])
        return actions

    @classmethod
    def _resource_role_state(
        cls,
        *,
        role_field_labels: dict[str, dict[str, str]],
        role_actions: dict[str, list[str]],
        role_state_contract: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        source_roles = cls._normalized_roles(role_state_contract.get("source_roles"))
        update_roles = cls._normalized_roles(role_state_contract.get("update_roles"))
        observer_roles = cls._normalized_roles(role_state_contract.get("observer_roles"))
        status_values = [
            " ".join(str(item or "").split()).strip(" .:-")[:80]
            for item in (role_state_contract.get("status_values") or [])
            if " ".join(str(item or "").split()).strip(" .:-")
        ][:8]

        roles_with_fields = [
            role for role in ROLE_ORDER if any(str(value).strip() for value in (role_field_labels.get(role) or {}).values())
        ]
        roles_with_actions = [
            role for role in ROLE_ORDER if any(str(value).strip() for value in (role_actions.get(role) or []))
        ]
        if not source_roles:
            source_roles = (roles_with_fields or roles_with_actions)[:1]
        if not update_roles and source_roles:
            if roles_with_fields or roles_with_actions:
                candidates = list(dict.fromkeys([*roles_with_actions, *roles_with_fields]))
                update_roles = [role for role in candidates if role not in source_roles] or list(source_roles)
        if not observer_roles:
            observer_roles = [role for role in ROLE_ORDER if role not in {*source_roles, *update_roles}]
        return source_roles, update_roles, observer_roles, status_values

    @classmethod
    def _role_field_labels(cls, role_field_hints: dict[str, list[str]], field_hints: list[str]) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        role_labels: dict[str, dict[str, str]] = {role: {} for role in ROLE_ORDER}
        combined: dict[str, str] = {}
        normalized_to_name: dict[str, str] = {}

        def add_label(role: str | None, raw_label: str, index: int) -> None:
            label = " ".join(str(raw_label or "").split()).strip(" .:-")
            if len(label) < 2:
                return
            normalized = cls._semantic_label_key(label)
            base_name = cls._field_name(label) or f"field_{index}"
            if base_name in RESERVED_WORKFLOW_FIELD_KEYS:
                return
            if normalized in normalized_to_name:
                name = normalized_to_name[normalized]
            else:
                name = base_name
                suffix = 2
                while name in combined:
                    name = f"{base_name}_{suffix}"
                    suffix += 1
                normalized_to_name[normalized] = name
                combined[name] = label[:80]
            if role in ROLE_ORDER:
                role_labels.setdefault(str(role), {})[name] = combined[name]

        index = 1
        for role in ROLE_ORDER:
            for raw_label in (role_field_hints.get(role) or [])[:12]:
                add_label(role, raw_label, index)
                index += 1
        for raw_label in field_hints[:12]:
            before = set(combined)
            add_label(None, raw_label, index)
            if set(combined) != before:
                index += 1
        return role_labels, combined

    @staticmethod
    def _semantic_label_key(label: str) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", str(label or "").lower())

    @classmethod
    def _field_labels(cls, field_hints: list[str]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for index, raw_label in enumerate(field_hints[:12], start=1):
            label = " ".join(str(raw_label or "").split()).strip(" .:-")
            if len(label) < 2:
                continue
            base_name = cls._field_name(label) or f"field_{index}"
            name = base_name
            suffix = 2
            while name in labels:
                name = f"{base_name}_{suffix}"
                suffix += 1
            labels[name] = label[:80]
        return labels

    @classmethod
    def _field_name(cls, label: str) -> str:
        slug = cls._slug(label).replace("-", "_")
        parts = [part for part in slug.split("_") if part]
        if not parts:
            return ""
        name = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
        if name and name[0].isdigit():
            name = f"field{name}"
        return name[:48]

    @staticmethod
    def _screens() -> list[MiniAppScreen]:
        return [
            MiniAppScreen(
                screen_id=f"{role}.root",
                role=role,  # type: ignore[arg-type]
                route_path=f"/{role}",
                file_path=f"static/{role}/index.html",
                script_path=f"static/{role}/app.js",
                style_path=f"static/{role}/styles.css",
                navigation_label=role.title(),
                purpose=f"{role} role root workflow",
            )
            for role in ROLE_ORDER
        ]

    @staticmethod
    def _slug(value: str) -> str:
        value = MiniAppContractCompiler._transliterate(str(value or "").lower())
        slug = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
        return slug or "item"

    @classmethod
    def _plural_slug(cls, value: str) -> str:
        slug = cls._slug(value)
        if slug.endswith("s"):
            return slug
        if slug.endswith("y") and len(slug) > 2:
            return f"{slug[:-1]}ies"
        return f"{slug}s"

    @staticmethod
    def _display_name(value: str) -> str:
        cleaned = re.sub(r"[-_]+", " ", str(value or "item")).strip()
        return cleaned[:1].upper() + cleaned[1:] if cleaned else "Item"

    @staticmethod
    def _resource_display_source(value: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "item")).strip(" .:-")
        cleaned = re.sub(r"^[Bb]2[Bb][-_ ]*", "", cleaned).strip()
        lowered = cleaned.lower()
        replacements = (
            ("цию", "ция"),
            ("сию", "сия"),
            ("ию", "ия"),
            ("ку", "ка"),
            ("гу", "га"),
            ("чу", "ча"),
            ("шу", "ша"),
            ("ью", "ья"),
        )
        for suffix, replacement in replacements:
            if lowered.endswith(suffix) and len(cleaned) > len(suffix) + 1:
                return f"{cleaned[:-len(suffix)]}{replacement}"
        if lowered.endswith("у") and len(cleaned) > 4:
            return f"{cleaned[:-1]}а"
        return cleaned or "item"

    @staticmethod
    def _transliterate(value: str) -> str:
        table = {
            "а": "a",
            "б": "b",
            "в": "v",
            "г": "g",
            "д": "d",
            "е": "e",
            "ё": "e",
            "ж": "zh",
            "з": "z",
            "и": "i",
            "й": "y",
            "к": "k",
            "л": "l",
            "м": "m",
            "н": "n",
            "о": "o",
            "п": "p",
            "р": "r",
            "с": "s",
            "т": "t",
            "у": "u",
            "ф": "f",
            "х": "h",
            "ц": "c",
            "ч": "ch",
            "ш": "sh",
            "щ": "shch",
            "ъ": "",
            "ы": "y",
            "ь": "",
            "э": "e",
            "ю": "yu",
            "я": "ya",
        }
        return "".join(table.get(char, char) for char in value)


class MiniAppContractMaterializer:
    GENERATED_FILES = (
        "miniapp/app/generated/miniapp_contract.json",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/generated/contract_validator.json",
    )

    @classmethod
    def contract_owned_paths(cls) -> list[str]:
        return list(cls.GENERATED_FILES)

    @classmethod
    def materialize(
        cls,
        source_dir: Path,
        contract: MiniAppContract,
        *,
        include_role_shell: bool = False,
    ) -> list[str]:
        del include_role_shell
        changed: list[str] = []
        generated_root = source_dir / "miniapp/app/generated"
        generated_root.mkdir(parents=True, exist_ok=True)

        writes = {
            "miniapp/app/generated/miniapp_contract.json": cls._render_json(contract.model_dump(mode="json")),
            "miniapp/app/generated/route_manifest.json": cls._render_route_manifest(source_dir, contract),
            "miniapp/app/generated/contract_validator.json": cls._render_json(cls._validator_metadata(contract)),
        }
        for relative_path, content in writes.items():
            if cls._write_if_changed(source_dir / relative_path, content):
                changed.append(relative_path)
        return list(dict.fromkeys(changed))

    @classmethod
    def _render_route_manifest(cls, source_dir: Path, contract: MiniAppContract) -> str:
        existing: dict[str, Any] = {}
        manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, ValueError):
                existing = {}
        roles: dict[str, Any] = {}
        top_routes: dict[str, str] = {}
        for screen in contract.screens:
            role_payload = roles.setdefault(screen.role, {"pages": [], "routes": {}})
            page = {
                "id": screen.screen_id.rsplit(".", 1)[-1],
                "route_path": screen.route_path,
                "file_path": screen.file_path,
                "script_path": screen.script_path,
                "style_path": screen.style_path,
                "navigation_label": screen.navigation_label,
            }
            role_payload["pages"].append(page)
            role_payload["routes"][screen.route_path] = screen.file_path
            top_routes[screen.route_path] = screen.file_path
        cls._merge_filesystem_role_pages(source_dir, roles, top_routes)
        rendered = {
            **existing,
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "source": "miniapp_contract",
            "routes": {**dict(existing.get("routes") or {}), **top_routes},
            "roles": roles,
            "shared": existing.get("shared") if isinstance(existing.get("shared"), dict) else {},
        }
        return cls._render_json(rendered)

    @staticmethod
    def _merge_filesystem_role_pages(source_dir: Path, roles: dict[str, Any], top_routes: dict[str, str]) -> None:
        static_root = source_dir / "miniapp/app/static"
        if not static_root.exists():
            return
        for role in ROLE_ORDER:
            role_root = static_root / role
            if not role_root.exists():
                continue
            role_payload = roles.setdefault(role, {"pages": [], "routes": {}})
            existing_routes = {
                str(page.get("route_path") or "")
                for page in role_payload.get("pages") or []
                if isinstance(page, dict)
            }
            for html_path in sorted(role_root.rglob("index.html")):
                if not html_path.is_file():
                    continue
                rel_to_role = html_path.relative_to(role_root).as_posix()
                if rel_to_role == "index.html":
                    route_path = f"/{role}"
                    page_id = "root"
                    label = role.title()
                else:
                    slug = rel_to_role.removesuffix("/index.html").strip("/")
                    route_path = f"/{role}/{slug}".rstrip("/")
                    page_id = slug.replace("/", "-") or "page"
                    label = slug.replace("_", " ").replace("-", " ").title() or "Page"
                file_ref = html_path.relative_to(source_dir / "miniapp/app").as_posix()
                role_payload.setdefault("routes", {})[route_path] = file_ref
                top_routes[route_path] = file_ref
                if route_path in existing_routes:
                    continue
                page = {
                    "id": page_id,
                    "route_path": route_path,
                    "file_path": file_ref,
                    "navigation_label": label,
                }
                script_ref = html_path.with_name("app.js")
                style_ref = html_path.with_name("styles.css")
                if script_ref.exists():
                    page["script_path"] = script_ref.relative_to(source_dir / "miniapp/app").as_posix()
                if style_ref.exists():
                    page["style_path"] = style_ref.relative_to(source_dir / "miniapp/app").as_posix()
                role_payload.setdefault("pages", []).append(page)

    @staticmethod
    def _validator_metadata(contract: MiniAppContract) -> dict[str, Any]:
        return {
            "contract_id": contract.contract_id,
            "version": contract.version,
            "contract_routes": [
                {"method": endpoint.method, "path": endpoint.path, "endpoint_id": endpoint.endpoint_id}
                for endpoint in contract.endpoints
            ],
            "contract_owned_paths": contract.allowed_file_graph.contract_owned_paths,
        }

    @staticmethod
    def _render_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        original = None
        if path.exists():
            try:
                original = path.read_text(encoding="utf-8")
            except OSError:
                original = None
        if original == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True


class MiniAppRouteRegistry:
    @classmethod
    def sync_contract_owned_files(cls, source_dir: Path, contract: MiniAppContract) -> list[str]:
        return MiniAppContractMaterializer.materialize(source_dir, contract, include_role_shell=False)

    @classmethod
    def snapshot(
        cls,
        source_dir: Path,
        contract: MiniAppContract | None,
        *,
        regenerated_files: list[str] | None = None,
    ) -> RouteRegistrySnapshot:
        if contract is None:
            return RouteRegistrySnapshot(status="missing_contract")
        declared = cls._declared_route_strings(source_dir)
        frontend = cls._frontend_ref_strings(source_dir)
        manifest = cls._manifest_route_strings(source_dir)
        contract_routes = [cls._route_key(endpoint.method, endpoint.path) for endpoint in contract.endpoints]
        issues: list[dict[str, Any]] = []
        recipes: list[RepairRecipe] = []

        for endpoint in contract.endpoints:
            expected = cls._route_key(endpoint.method, endpoint.path)
            if expected not in declared:
                issue = {
                    "code": "registry.missing_backend_route",
                    "expected": expected,
                    "location": "miniapp/app/routes",
                }
                issues.append(issue)
                recipes.append(
                    RepairRecipe(
                        issue_code="registry.missing_backend_route",
                        expected_route=expected,
                        declared_routes=declared,
                        manifest_routes=manifest,
                        why_mismatch="The typed miniapp contract declares an API route that FastAPI route parsing cannot find.",
                        suggested_patch_target="miniapp/app/routes",
                        auto_fixable=True,
                    )
                )
        for screen in contract.screens:
            if screen.route_path not in manifest:
                issues.append(
                    {
                        "code": "registry.missing_manifest_route",
                        "expected": screen.route_path,
                        "location": "miniapp/app/generated/route_manifest.json",
                    }
                )
                recipes.append(
                    RepairRecipe(
                        issue_code="registry.missing_manifest_route",
                        expected_route=screen.route_path,
                        declared_routes=declared,
                        manifest_routes=manifest,
                        why_mismatch="The role screen exists in MiniAppContract but is absent from route_manifest.json.",
                        suggested_patch_target="miniapp/app/generated/route_manifest.json",
                        auto_fixable=True,
                    )
                )
        for ref in frontend:
            method, path = ref.split(" ", 1)
            if not cls._frontend_ref_has_backend(method, path, declared):
                issues.append(
                    {
                        "code": "registry.frontend_backend_drift",
                        "frontend_ref": ref,
                        "location": "miniapp/app/static",
                    }
                )
                recipes.append(
                    RepairRecipe(
                        issue_code="registry.frontend_backend_drift",
                        frontend_ref=ref,
                        declared_routes=declared,
                        manifest_routes=manifest,
                        why_mismatch="Frontend JavaScript references an API path that is not declared by backend routes.",
                        suggested_patch_target="miniapp/app/routes",
                        auto_fixable=False,
                    )
                )
        return RouteRegistrySnapshot(
            contract_id=contract.contract_id,
            declared_routes=declared,
            frontend_refs=frontend,
            manifest_routes=manifest,
            contract_routes=contract_routes,
            drift_issues=issues,
            repair_recipes=recipes,
            regenerated_files=regenerated_files or [],
            status="drift" if issues else "passed",
        )

    @staticmethod
    def load_contract(source_dir: Path) -> MiniAppContract | None:
        path = source_dir / "miniapp/app/generated/miniapp_contract.json"
        if not path.exists():
            return None
        try:
            return MiniAppContract.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @classmethod
    def _declared_route_strings(cls, source_dir: Path) -> list[str]:
        routes = extract_declared_routes(source_dir / "miniapp/app/routes", api_only=True)
        return sorted(cls._route_key(method, path) for method, path in routes)

    @classmethod
    def _frontend_ref_strings(cls, source_dir: Path) -> list[str]:
        refs: set[tuple[str, str]] = set()
        for root in [source_dir / "miniapp/app/static", source_dir / "miniapp/app/generated"]:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.suffix.lower() not in {".js", ".html"}:
                    continue
                try:
                    refs.update(extract_frontend_api_refs(path.read_text(encoding="utf-8")))
                except OSError:
                    continue
        return sorted(cls._route_key(method, path) for method, path in refs)

    @staticmethod
    def _manifest_route_strings(source_dir: Path) -> list[str]:
        path = source_dir / "miniapp/app/generated/route_manifest.json"
        if not path.exists():
            return []
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        routes: set[str] = set()
        top = manifest.get("routes") if isinstance(manifest, dict) else {}
        if isinstance(top, dict):
            routes.update(str(route) for route in top if str(route).strip())
        roles = manifest.get("roles") if isinstance(manifest, dict) else {}
        if isinstance(roles, dict):
            for role, payload in roles.items():
                if isinstance(payload, dict):
                    route_map = payload.get("routes")
                    if isinstance(route_map, dict):
                        routes.update(str(route) for route in route_map if str(route).strip())
                    for page in payload.get("pages") or []:
                        if isinstance(page, dict) and page.get("route_path"):
                            routes.add(str(page["route_path"]))
                elif isinstance(payload, str):
                    routes.add(str(role))
        return sorted(routes)

    @classmethod
    def _frontend_ref_has_backend(cls, method: str, path: str, declared: list[str]) -> bool:
        normalized = normalize_api_path(path)
        same_method = [item.split(" ", 1)[1] for item in declared if item.startswith(f"{method.upper()} ")]
        return any(cls._paths_match(normalized, declared_path) for declared_path in same_method)

    @staticmethod
    def _paths_match(frontend_path: str, declared_path: str) -> bool:
        left = frontend_path.strip("/").split("/")
        right = declared_path.strip("/").split("/")
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if a == b or (a.startswith("{") and a.endswith("}")) or (b.startswith("{") and b.endswith("}")):
                continue
            return False
        return True

    @staticmethod
    def _route_key(method: str, path: str) -> str:
        return f"{str(method or 'GET').upper()} {normalize_api_path(path)}"
