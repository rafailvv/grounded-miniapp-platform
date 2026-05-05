from __future__ import annotations

from html import escape
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
ROLE_LABELS_RU = {
    "client": "Клиент",
    "specialist": "Специалист",
    "manager": "Менеджер",
}
STANDARD_ROLE_FIELD_LABELS = {
    "client": {
        "recordName": "Название записи",
        "clientComment": "Комментарий клиента",
    },
    "specialist": {
        "specialistDecision": "Решение специалиста",
        "specialistComment": "Комментарий специалиста",
    },
    "manager": {
        "managerDecision": "Решение менеджера",
        "managerComment": "Комментарий менеджера",
    },
}
RESERVED_WORKFLOW_FIELD_KEYS = {
    "id",
    "item_id",
    "title",
    "note",
    "status",
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
        slug_source = str(hints.get("resource_hint") or "").strip() or "item"
        slug_source = cls._resource_display_source(slug_source)
        slug = cls._plural_slug(slug_source)
        display_name = cls._display_name(slug_source)
        resource = cls._resource(
            slug=slug,
            display_name=display_name,
            field_hints=[str(item) for item in hints.get("field_hints") or [] if str(item).strip()],
            role_field_hints={
                role: [str(item) for item in (items or []) if str(item).strip()]
                for role, items in dict(hints.get("role_field_hints") or {}).items()
            },
        )
        screens = cls._screens()
        endpoints = list(resource.endpoints)
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
                "miniapp/tests/test_generated_app.py",
                "miniapp/tests/generated_app.test.mjs",
                "miniapp/app/routes/generated_contract.py",
            ],
        )
        contract = MiniAppContract(
            workspace_id=workspace_id,
            run_id=run_id,
            prompt_summary=str(hints.get("prompt_summary") or prompt or "")[:1200],
            generation_mode=mode_value,
            intent=intent_value,
            resources=[resource],
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
            "api_client": "miniapp/app/generated/api_client.js",
            "route_manifest": "miniapp/app/generated/route_manifest.json",
            "backend_route": "miniapp/app/routes/generated_contract.py",
            "python_tests": "miniapp/tests/test_generated_app.py",
            "js_tests": "miniapp/tests/generated_app.test.mjs",
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
        contract["features"] = {**dict(contract.get("features") or {}), "contract_runtime_v1": True}
        contract.setdefault("page_contract", {})
        contract["page_contract"] = {
            **dict(contract.get("page_contract") or {}),
            "route_manifest_required": True,
            "single_source_of_truth": "miniapp_contract",
        }
        if implementation_plan is not None:
            implementation_plan["contract_runtime_v1"] = {
                "enabled": True,
                "materialized_tests": True,
                "contract_owned_paths": MiniAppContractMaterializer.contract_owned_paths(),
            }
            implementation_plan.setdefault("api_contract", {})
            implementation_plan["api_contract"] = {
                **dict(implementation_plan.get("api_contract") or {}),
                "required_endpoints": contract["required_endpoints"],
                "single_source_of_truth": "miniapp_contract",
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
    ) -> MiniAppResource:
        role_field_labels, field_labels = cls._role_field_labels(role_field_hints or {}, field_hints or [])
        client_labels = role_field_labels.get("client") or field_labels
        update_labels = {
            **dict(role_field_labels.get("specialist") or {}),
            **dict(role_field_labels.get("manager") or {}),
        }
        create_fields = [*client_labels.keys(), "title", "note", "created_by"] if client_labels else ["title", "note"]
        update_fields = list(dict.fromkeys(["status", "note", "updated_by", *update_labels.keys()]))
        endpoints = [
            MiniAppEndpoint(
                endpoint_id=f"{slug}.list",
                method="GET",
                path=f"/api/{slug}",
                purpose="List persisted shared records",
                resource=slug,
                role="system",
            ),
            MiniAppEndpoint(
                endpoint_id=f"{slug}.create",
                method="POST",
                path=f"/api/{slug}",
                purpose="Create a persisted record from the client role",
                resource=slug,
                role="client",
                request_fields=create_fields,
            ),
            MiniAppEndpoint(
                endpoint_id=f"{slug}.update_status",
                method="PATCH",
                path=f"/api/{slug}/{{item_id}}/status",
                purpose="Persist a specialist or manager status update",
                resource=slug,
                role="specialist",
                request_fields=update_fields,
            ),
        ]
        return MiniAppResource(
            resource_id=slug,
            slug=slug,
            name=slug,
            display_name=display_name,
            fields=list(dict.fromkeys(["id", "title", "note", "status", "created_by", "updated_by", *field_labels.keys()])),
            field_labels=field_labels,
            role_field_labels=role_field_labels,
            endpoints=endpoints,
        )

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
        "miniapp/app/generated/api_client.js",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/generated/contract_validator.json",
        "miniapp/app/routes/generated_contract.py",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
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
        include_role_shell: bool = True,
    ) -> list[str]:
        changed: list[str] = []
        generated_root = source_dir / "miniapp/app/generated"
        generated_root.mkdir(parents=True, exist_ok=True)
        tests_root = source_dir / "miniapp/tests"
        tests_root.mkdir(parents=True, exist_ok=True)
        routes_root = source_dir / "miniapp/app/routes"
        routes_root.mkdir(parents=True, exist_ok=True)

        writes = {
            "miniapp/app/generated/miniapp_contract.json": cls._render_json(contract.model_dump(mode="json")),
            "miniapp/app/generated/api_client.js": cls._render_api_client(contract),
            "miniapp/app/generated/route_manifest.json": cls._render_route_manifest(source_dir, contract),
            "miniapp/app/generated/contract_validator.json": cls._render_json(cls._validator_metadata(contract)),
            "miniapp/app/routes/generated_contract.py": cls._render_backend_route(contract),
            "miniapp/tests/test_generated_app.py": cls._render_python_tests(contract),
            "miniapp/tests/generated_app.test.mjs": cls._render_js_tests(contract),
        }
        for relative_path, content in writes.items():
            if cls._write_if_changed(source_dir / relative_path, content):
                changed.append(relative_path)
        if cls._ensure_main_includes_generated_route(source_dir):
            changed.append("miniapp/app/main.py")
        if include_role_shell:
            for relative_path, content in cls._role_shell_files(contract).items():
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
    def _render_api_client(contract: MiniAppContract) -> str:
        resource = contract.resources[0]
        slug = resource.slug
        return f'''const CONTRACT_RESOURCE = {json.dumps(slug)};
const CONTRACT_API_BASE = `/api/${{CONTRACT_RESOURCE}}`;

export async function listContractItems() {{
  return requestJson(CONTRACT_API_BASE);
}}

export async function createContractItem(payload) {{
  return requestJson(CONTRACT_API_BASE, {{
    method: "POST",
    body: JSON.stringify(payload || {{}}),
  }});
}}

export async function updateContractItemStatus(itemId, payload) {{
  return requestJson(`${{CONTRACT_API_BASE}}/${{encodeURIComponent(itemId)}}/status`, {{
    method: "PATCH",
    body: JSON.stringify(payload || {{}}),
  }});
}}

async function requestJson(path, options = {{}}) {{
  const response = await fetch(path, {{
    headers: {{ "Content-Type": "application/json", ...(options.headers || {{}}) }},
    ...options,
  }});
  if (!response.ok) {{
    throw new Error(`Request failed: ${{response.status}} ${{path}}`);
  }}
  return response.json();
}}
'''

    @staticmethod
    def _render_backend_route(contract: MiniAppContract) -> str:
        resource = contract.resources[0]
        slug = resource.slug
        title = resource.display_name
        return f'''from __future__ import annotations

from itertools import count
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api", tags=["contract-runtime"])
_NEXT_ID = count(1)
_ITEMS: list[dict[str, Any]] = []


class ContractItemCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str = Field(default="{title}", min_length=1)
    note: str = ""
    created_by: str = "client"


class ContractItemStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = Field(default="updated", min_length=1)
    note: str = ""
    updated_by: str = "specialist"


@router.get("/{slug}")
def list_contract_items() -> list[dict[str, Any]]:
    return list(_ITEMS)


@router.post("/{slug}", status_code=201)
def create_contract_item(payload: ContractItemCreate) -> dict[str, Any]:
    payload_data = payload.model_dump()
    prompt_fields = {{
        key: value
        for key, value in payload_data.items()
        if key not in {{"title", "note", "created_by"}}
    }}
    item = {{
        "id": str(next(_NEXT_ID)),
        **prompt_fields,
        "title": payload.title,
        "note": payload.note,
        "status": "new",
        "created_by": payload.created_by or "client",
        "updated_by": "",
    }}
    _ITEMS.append(item)
    return item


@router.patch("/{slug}/{{item_id}}/status")
def update_contract_item_status(item_id: str, payload: ContractItemStatusUpdate) -> dict[str, Any]:
    payload_data = payload.model_dump()
    for item in _ITEMS:
        if str(item.get("id")) == str(item_id):
            item["status"] = payload.status
            item["note"] = payload.note or item.get("note", "")
            item["updated_by"] = payload.updated_by or "specialist"
            for key, value in payload_data.items():
                if key not in {{"status", "note", "updated_by"}}:
                    item[key] = value
            return item
    raise HTTPException(status_code=404, detail="Contract item not found")
'''

    @staticmethod
    def _render_python_tests(contract: MiniAppContract) -> str:
        resource = contract.resources[0]
        path = f"/api/{resource.slug}"
        create_labels = dict((resource.role_field_labels or {}).get("client") or resource.field_labels or {})
        prompt_payload = {
            field: f"{label} value"
            for field, label in list(create_labels.items())[:4]
        }
        create_payload = {
            **prompt_payload,
            "title": "Contract item",
            "note": "created",
        }
        return f'''from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class GeneratedContractRuntimeTest(unittest.TestCase):
    def test_contract_api_persists_create_and_status_update(self) -> None:
        with TestClient(app) as client:
            before = client.get({json.dumps(path)})
            self.assertEqual(before.status_code, 200)
            create_payload = {json.dumps(create_payload, ensure_ascii=False)}
            create = client.post({json.dumps(path)}, json=create_payload)
            self.assertEqual(create.status_code, 201)
            created = create.json()
            self.assertEqual(created["status"], "new")
            for key, value in create_payload.items():
                self.assertEqual(created.get(key), value)
            after = client.get({json.dumps(path)})
            self.assertEqual(after.status_code, 200)
            self.assertTrue(any(str(item.get("id")) == str(created["id"]) for item in after.json()))
            update = client.patch(f"{path}/{{created['id']}}/status", json={{"status": "processed", "updated_by": "specialist"}})
            self.assertEqual(update.status_code, 200)
            self.assertEqual(update.json()["status"], "processed")


if __name__ == "__main__":
    unittest.main()
'''

    @staticmethod
    def _render_js_tests(contract: MiniAppContract) -> str:
        resource = contract.resources[0]
        path = f"/api/{resource.slug}"
        role_routes = [screen.route_path for screen in contract.screens]
        return f'''import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const appRoot = path.join(process.cwd(), "app");

test("generated contract files expose route manifest and API client", () => {{
  const contract = JSON.parse(fs.readFileSync(path.join(appRoot, "generated", "miniapp_contract.json"), "utf8"));
  const manifest = JSON.parse(fs.readFileSync(path.join(appRoot, "generated", "route_manifest.json"), "utf8"));
  const apiClient = fs.readFileSync(path.join(appRoot, "generated", "api_client.js"), "utf8");
  assert.equal(contract.version, "grounded.miniapp.contract.v1");
  assert.match(apiClient, /fetch\\(/);
  assert.match(apiClient, /method: "POST"/);
  assert.match(apiClient, /method: "PATCH"/);
  assert.ok(contract.endpoints.some((endpoint) => endpoint.method === "POST" && endpoint.path === {json.dumps(path)}));
  for (const route of {json.dumps(role_routes)}) {{
    assert.ok(manifest.routes[route], `missing route manifest entry for ${{route}}`);
  }}
}});
'''

    @classmethod
    def _role_shell_files(cls, contract: MiniAppContract) -> dict[str, str]:
        resource = contract.resources[0]
        files: dict[str, str] = {}
        for role in ROLE_ORDER:
            files[f"miniapp/app/static/{role}/index.html"] = cls._role_html(role=role, resource=resource)
            files[f"miniapp/app/static/{role}/app.js"] = cls._role_js(role=role, resource=resource)
            files[f"miniapp/app/static/{role}/styles.css"] = cls._role_css(role=role, generation_mode=contract.generation_mode)
        return files

    @staticmethod
    def _standard_role_field_labels(role: str) -> dict[str, str]:
        labels = STANDARD_ROLE_FIELD_LABELS.get(role)
        if labels is not None:
            return dict(labels)
        return {"workComment": "Комментарий"}

    @classmethod
    def _role_field_labels_for_ui(cls, resource: MiniAppResource, role: str) -> dict[str, str]:
        labels = dict((resource.role_field_labels or {}).get(role) or {})
        if role == "client" and not labels:
            labels = dict(resource.field_labels or {})
        if not labels:
            labels = cls._standard_role_field_labels(role)
        return labels

    @classmethod
    def _detail_field_labels_for_ui(cls, resource: MiniAppResource) -> dict[str, str]:
        labels: dict[str, str] = {}
        labels.update(dict(resource.field_labels or {}))
        for role in ROLE_ORDER:
            labels.update(cls._role_field_labels_for_ui(resource, role))
        return labels

    @classmethod
    def _prompt_form_controls(cls, resource: MiniAppResource, *, role: str = "client") -> str:
        labels = cls._role_field_labels_for_ui(resource, role)
        controls: list[str] = []
        for index, (name, label) in enumerate(labels.items()):
            safe_name = escape(name, quote=True)
            safe_label = escape(label, quote=True)
            required = " required" if index == 0 else ""
            lowered = f"{name} {label}".lower()
            if any(marker in lowered for marker in ("comment", "note", "opis", "kommentar", "predpochten", "message")):
                controls.append(
                    f'          <label>{safe_label} <textarea id="contract-{safe_name}" name="{safe_name}"{required}></textarea></label>'
                )
            else:
                input_type = "date" if any(marker in lowered for marker in ("date", "data", "datum", "srok")) else "text"
                controls.append(
                    f'          <label>{safe_label} <input id="contract-{safe_name}" name="{safe_name}" type="{input_type}"{required} /></label>'
                )
        return "\n".join(controls)

    @staticmethod
    def _role_html(*, role: str, resource: MiniAppResource) -> str:
        cyrillic = bool(re.search(r"[А-Яа-яЁё]", resource.display_name))
        role_label = ROLE_LABELS_RU.get(role, role.title()) if cyrillic else role.title()
        if cyrillic:
            title = {
                "client": f"Создать запись: {resource.display_name}",
                "specialist": f"Обработка: {resource.display_name}",
                "manager": f"Контроль: {resource.display_name}",
            }.get(role, resource.display_name)
            saved_copy = "Сохраненные записи доступны после перезагрузки."
            save_label = "Сохранить"
            refresh_label = "Обновить"
            select_label = "Запись"
            update_label = "Сохранить изменения"
            status_label = "Статус"
            status_options_by_role = {
                "manager": (
                    '<option value="reviewed">На согласовании</option>\n'
                    '            <option value="ready">Готово к согласованию</option>\n'
                    '            <option value="approved">Одобрена</option>\n'
                    '            <option value="rejected">Отклонена</option>'
                ),
                "specialist": (
                    '<option value="processed">В работе</option>\n'
                    '            <option value="reviewed">Проверена</option>\n'
                    '            <option value="ready">Готово к согласованию</option>'
                ),
            }
            status_options = status_options_by_role.get(
                role,
                '<option value="processed">В работе</option>\n'
                '            <option value="reviewed">Проверена</option>\n'
                '            <option value="ready">Готово к согласованию</option>',
            )
            list_title = {
                "client": "Сохраненные записи",
                "specialist": "Очередь обработки",
                "manager": "Контроль заявок",
            }.get(role, "Сохраненные записи")
            loading_copy = "Загружаем сохраненные записи..."
            error_copy = "Не удалось загрузить данные. Попробуйте обновить список."
            success_copy = "Данные обновлены."
            metrics_label = "Метрики заявок"
        else:
            title = {
                "client": f"Create record: {resource.display_name}",
                "specialist": f"Process {resource.display_name}",
                "manager": f"Review {resource.display_name}",
            }.get(role, resource.display_name)
            saved_copy = "Saved records are available after reload."
            save_label = "Save"
            refresh_label = "Refresh"
            select_label = "Record"
            update_label = "Save changes"
            status_label = "Status"
            status_options_by_role = {
                "manager": (
                    '<option value="reviewed">Reviewed</option>\n'
                    '            <option value="ready">Ready</option>\n'
                    '            <option value="approved">Approved</option>\n'
                    '            <option value="rejected">Rejected</option>'
                ),
                "specialist": (
                    '<option value="processed">Processed</option>\n'
                    '            <option value="reviewed">Reviewed</option>\n'
                    '            <option value="ready">Ready</option>'
                ),
            }
            status_options = status_options_by_role.get(
                role,
                '<option value="processed">Processed</option>\n'
                '            <option value="reviewed">Reviewed</option>\n'
                '            <option value="ready">Ready</option>',
            )
            list_title = f"{resource.display_name} records"
            loading_copy = "Loading saved records..."
            error_copy = "Could not load records. Try refreshing the list."
            success_copy = "Data updated."
            metrics_label = "Request metrics"
        form = (
            f'''
        <form id="contract-create-form" class="contract-form">
{MiniAppContractMaterializer._prompt_form_controls(resource, role="client")}
          <button type="submit">{save_label}</button>
        </form>'''
            if role == "client"
            else f'''
        <form id="contract-update-form" class="contract-form">
          <label>{select_label} <select id="contract-item-select" name="item_id"></select></label>
{MiniAppContractMaterializer._prompt_form_controls(resource, role=role)}
          <label>{status_label} <select id="contract-status-field" name="status">
            {status_options}
          </select></label>
          <button type="submit">{update_label}</button>
        </form>
        <div class="contract-actions">
          <button id="contract-refresh" type="button">{refresh_label}</button>
        </div>'''
        )
        return f'''<!doctype html>
<html lang="{'ru' if cyrillic else 'en'}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
    <link rel="stylesheet" href="/static/shared/base.css" />
    <link rel="stylesheet" href="/static/{role}/styles.css" />
  </head>
  <body data-role="{role}">
    <main class="page-shell">
      <section class="page" data-role="{role}">
        <header class="contract-header">
          <p class="contract-eyebrow">{role_label}</p>
          <h1>{title}</h1>
          <p id="contract-status" class="contract-copy">{saved_copy}</p>
        </header>
        <div id="contract-loading" class="contract-state contract-loading" hidden>{loading_copy}</div>
        <div id="contract-error" class="contract-state contract-error" hidden>{error_copy}</div>
        <div id="contract-success" class="contract-state contract-success" hidden>{success_copy}</div>
{f'        <section id="contract-metrics" class="metrics-grid" aria-label="{escape(metrics_label)}"></section>' if role == "manager" else ''}
{form}
        <section class="contract-list" aria-live="polite">
          <h2>{escape(list_title)}</h2>
          <div id="contract-items"></div>
        </section>
      </section>
    </main>
    <script src="/static/preview_bridge.js" defer></script>
    <script src="/static/{role}/app.js" defer></script>
  </body>
</html>
'''

    @classmethod
    def _role_js(cls, *, role: str, resource: MiniAppResource) -> str:
        slug = resource.slug
        update_status = "processed" if role == "specialist" else "reviewed"
        quick_action_label = "Отметить обработку" if role == "specialist" else "Сохранить решение"
        client_field_labels = cls._role_field_labels_for_ui(resource, "client")
        role_field_labels = cls._role_field_labels_for_ui(resource, role)
        detail_field_labels = cls._detail_field_labels_for_ui(resource)
        primary_field = next(iter(client_field_labels.keys()), "")
        create_handler = ""
        if role == "client":
            create_handler = f'''
const form = document.getElementById("contract-create-form");
if (form) {{
  form.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const formData = new FormData(form);
    const payload = {{}};
    for (const [key, value] of formData.entries()) {{
      payload[key] = String(value || "");
    }}
    const primaryValue = PRIMARY_FIELD ? String(payload[PRIMARY_FIELD] || "") : "";
    payload.title = payload.title || primaryValue || {json.dumps(resource.display_name)};
    payload.note = payload.note || Object.entries(FIELD_LABELS)
      .map(([key, label]) => payload[key] ? `${{label}}: ${{payload[key]}}` : "")
      .filter(Boolean)
      .join(" · ");
    payload.created_by = ROLE;
    await requestJson(API_BASE, {{
      method: "POST",
      body: JSON.stringify(payload),
    }});
    form.reset();
    await loadItems();
  }});
}}
'''
        update_handler = ""
        if role in {"specialist", "manager"}:
            update_handler = f'''
const updateForm = document.getElementById("contract-update-form");
if (updateForm) {{
  updateForm.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const formData = new FormData(updateForm);
    const itemId = String(formData.get("item_id") || "");
    if (!itemId) {{
      setState("error", "Сначала выберите сохраненную запись.");
      return;
    }}
    const payload = {{ status: String(formData.get("status") || {json.dumps(update_status)}), updated_by: ROLE }};
    for (const [key, value] of formData.entries()) {{
      if (key !== "item_id" && key !== "status") payload[key] = String(value || "");
    }}
    payload.note = payload.note || Object.entries(ROLE_FIELD_LABELS)
      .map(([key, label]) => payload[key] ? `${{label}}: ${{payload[key]}}` : "")
      .filter(Boolean)
      .join(" · ");
    await requestJson(`${{API_BASE}}/${{encodeURIComponent(itemId)}}/status`, {{
      method: "PATCH",
      body: JSON.stringify(payload),
    }});
    updateForm.reset();
    await loadItems();
  }});
}}
itemsRoot?.addEventListener("click", async (event) => {{
  const button = event.target.closest("[data-update-id]");
  if (!button) return;
  await requestJson(`${{API_BASE}}/${{button.dataset.updateId}}/status`, {{
    method: "PATCH",
    body: JSON.stringify({{ status: "{update_status}", updated_by: ROLE }}),
  }});
  await loadItems();
}});
document.getElementById("contract-refresh")?.addEventListener("click", loadItems);
'''
        return f'''const ROLE = {json.dumps(role)};
const API_BASE = {json.dumps(f"/api/{slug}")};
const FIELD_LABELS = {json.dumps(client_field_labels, ensure_ascii=False)};
const ROLE_FIELD_LABELS = {json.dumps(role_field_labels, ensure_ascii=False)};
const CLIENT_FIELD_LABELS = {json.dumps(client_field_labels, ensure_ascii=False)};
const DETAIL_FIELD_LABELS = {json.dumps(detail_field_labels, ensure_ascii=False)};
const PRIMARY_FIELD = {json.dumps(primary_field)};
const STATUS_LABELS = {{
  new: "Новая",
  pending: "На проверке",
  processing: "В работе",
  processed: "В работе",
  reviewed: "Проверена",
  ready: "Готово к согласованию",
  approved: "Одобрена",
  rejected: "Отклонена",
  done: "Готово",
  completed: "Завершена",
  confirmed: "Подтверждена",
  paid: "Оплачена",
}};
const statusNode = document.getElementById("contract-status");
const loadingNode = document.getElementById("contract-loading");
const errorNode = document.getElementById("contract-error");
const successNode = document.getElementById("contract-success");
const metricsRoot = document.getElementById("contract-metrics");
const itemsRoot = document.getElementById("contract-items");
const itemSelect = document.getElementById("contract-item-select");

window.setupPreviewBridge?.(ROLE);

async function requestJson(path, options = {{}}) {{
  const response = await fetch(path, {{
    headers: {{ "Content-Type": "application/json", ...(options.headers || {{}}) }},
    ...options,
  }});
  if (!response.ok) throw new Error(`Запрос завершился ошибкой: ${{response.status}}`);
  return response.json();
}}

async function loadItems() {{
  setState("loading", "Загружаем сохраненные записи...");
  try {{
    const items = await requestJson(API_BASE);
    renderMetrics(items);
    renderItems(items);
    renderItemOptions(items);
    setState("success", items.length ? `Найдено записей: ${{items.length}}.` : "Пока нет сохраненных записей.");
  }} catch (error) {{
    setState("error", error.message || "Не удалось загрузить сохраненные записи.");
  }}
}}

function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }}[char]));
}}

function itemTitle(item) {{
  return item.title || (PRIMARY_FIELD ? item[PRIMARY_FIELD] : "") || {json.dumps(resource.display_name)};
}}

function humanizeStatus(value) {{
  const text = String(value || "").replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Новая";
}}

function statusLabel(value) {{
  const key = String(value || "new").trim();
  return STATUS_LABELS[key] || "Статус уточняется";
}}

function setState(state, message = "") {{
  if (loadingNode) loadingNode.hidden = state !== "loading";
  if (errorNode) {{
    errorNode.hidden = state !== "error";
    if (state === "error" && message) errorNode.textContent = message;
  }}
  if (successNode) {{
    successNode.hidden = state !== "success";
    if (state === "success" && message) successNode.textContent = message;
  }}
  if (statusNode && message) statusNode.textContent = message;
}}

function renderMetrics(items) {{
  if (!metricsRoot) return;
  const total = items.length;
  const approved = items.filter((item) => item.status === "approved").length;
  const inReview = items.filter((item) => ["processed", "reviewed", "ready"].includes(item.status)).length;
  metricsRoot.innerHTML = [
    ["Всего", total],
    ["На согласовании", inReview],
    ["Одобрено", approved],
  ].map(([label, value]) => `<article class="metric-card"><span>${{escapeHtml(label)}}</span><strong>${{escapeHtml(value)}}</strong></article>`).join("");
}}

function itemDetails(item) {{
  const rows = Object.entries(DETAIL_FIELD_LABELS)
    .filter(([key]) => item[key])
    .map(([key, label]) => `<p><b>${{escapeHtml(label)}}</b>: ${{escapeHtml(item[key])}}</p>`);
  if (rows.length) return rows.join("");
  return `<p>${{escapeHtml(item.note || "")}}</p>`;
}}

function renderItemOptions(items) {{
  if (!itemSelect) return;
  itemSelect.innerHTML = items.map((item) => `<option value="${{escapeHtml(item.id)}}">${{escapeHtml(itemTitle(item))}}</option>`).join("");
}}

function renderItems(items) {{
  if (!itemsRoot) return;
  if (!items.length) {{
    itemsRoot.innerHTML = '<p class="contract-empty">Пока нет сохраненных записей. Создайте первую запись через форму.</p>';
    return;
  }}
  itemsRoot.innerHTML = items.map((item) => `
    <article class="contract-card">
      <strong>${{escapeHtml(itemTitle(item))}}</strong>
      <span>${{escapeHtml(statusLabel(item.status))}}</span>
      ${{itemDetails(item)}}
      {('<button type="button" data-update-id="${item.id}">' + quick_action_label + '</button>' if role in {"specialist", "manager"} else '')}
    </article>
  `).join("");
}}
{create_handler}
{update_handler}
loadItems();
'''

    @staticmethod
    def _role_css(*, role: str, generation_mode: GenerationMode | str | None = None) -> str:
        accent = {"client": "#0f766e", "specialist": "#2563eb", "manager": "#7c3aed"}.get(role, "#0f766e")
        mode_value = normalized_generation_mode(generation_mode)
        balanced_css = ""
        if mode_value in {GenerationMode.BALANCED.value, GenerationMode.QUALITY.value}:
            balanced_css = '''

.page {
  padding: 22px 18px 30px;
}

.contract-header {
  padding: 18px;
  border: 1px solid var(--contract-border);
  border-radius: 8px;
  background: var(--contract-panel);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.06);
}

.contract-header::before {
  content: "";
  display: block;
  width: 48px;
  height: 4px;
  margin-bottom: 12px;
  border-radius: 999px;
  background: var(--contract-accent);
}

.contract-header h1 {
  font-size: 24px;
  line-height: 1.12;
}

.contract-form {
  padding: 16px;
  border: 1px solid var(--contract-border);
  border-radius: 8px;
  background: #ffffff;
  box-shadow: 0 14px 34px rgba(15, 23, 42, 0.07);
}

.contract-form label {
  color: #26313b;
  font-size: 13px;
}

.contract-form button,
.contract-actions button {
  width: 100%;
}

.contract-list h2 {
  font-size: 18px;
  line-height: 1.2;
}

.contract-card {
  border-left: 4px solid var(--contract-accent);
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.06);
}

.contract-card p {
  margin-bottom: 0;
  line-height: 1.45;
}

.contract-card b {
  color: var(--contract-ink);
}

.metric-card {
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.05);
}
'''
        quality_css = ""
        if mode_value == GenerationMode.QUALITY.value:
            quality_css = '''

body {
  background: linear-gradient(180deg, #f7faf9 0%, #eef3f7 48%, #f7f8fb 100%);
}

.page {
  width: min(100%, 480px);
  min-height: calc(100vh - 24px);
  display: grid;
  gap: 16px;
}

.contract-header {
  padding: 20px;
  border-color: color-mix(in srgb, var(--contract-accent) 22%, var(--contract-border));
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--contract-accent) 10%, white), #ffffff 46%),
    #ffffff;
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.10);
}

.contract-eyebrow {
  width: max-content;
  padding: 5px 9px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--contract-accent) 12%, white);
}

.contract-header h1 {
  font-size: 26px;
  line-height: 1.08;
  font-weight: 800;
}

.contract-copy {
  font-size: 14px;
  line-height: 1.5;
}

.contract-form {
  gap: 13px;
  padding: 18px;
  border-color: color-mix(in srgb, var(--contract-accent) 14%, var(--contract-border));
  box-shadow: 0 20px 46px rgba(15, 23, 42, 0.10);
}

input,
textarea,
select {
  background: #fbfcfd;
  border-color: #cfd8e3;
}

.contract-list h2 {
  margin-bottom: 2px;
  letter-spacing: 0;
}

.contract-card {
  gap: 10px;
  padding: 16px;
  border-color: color-mix(in srgb, var(--contract-accent) 16%, var(--contract-border));
  background: #ffffff;
  box-shadow: 0 16px 38px rgba(15, 23, 42, 0.09);
}

.contract-card strong {
  font-size: 18px;
  line-height: 1.18;
}

.contract-card span {
  justify-self: start;
  border: 1px solid color-mix(in srgb, var(--contract-accent) 22%, white);
}

.contract-card p {
  padding-top: 7px;
  border-top: 1px solid #eef2f6;
}

.metric-card {
  padding: 14px;
  border-color: color-mix(in srgb, var(--contract-accent) 16%, var(--contract-border));
  background: #ffffff;
}

.metric-card strong {
  font-size: 24px;
}

.contract-card button {
  justify-self: start;
  width: auto;
  min-width: 160px;
}

button {
  box-shadow: 0 10px 24px color-mix(in srgb, var(--contract-accent) 20%, transparent);
}

@media (max-width: 420px) {
  .contract-header,
  .contract-form,
  .contract-card {
    box-shadow: 0 10px 24px rgba(15, 23, 42, 0.07);
  }
}
'''
        return f''':root {{
  color-scheme: light;
  --contract-accent: {accent};
  --contract-ink: #172026;
  --contract-muted: #5c6670;
  --contract-panel: #ffffff;
  --contract-border: #d8dee4;
}}

body {{
  margin: 0;
  background: #f5f7f8;
  color: var(--contract-ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}

.page {{
  width: min(100%, 440px);
  margin: 0 auto;
  padding: 20px;
  box-sizing: border-box;
}}

.contract-header,
.contract-form,
.contract-list,
.contract-actions,
.metrics-grid {{
  display: grid;
  gap: 12px;
  margin-bottom: 16px;
}}

.contract-eyebrow {{
  margin: 0;
  color: var(--contract-accent);
  font-weight: 700;
  text-transform: uppercase;
  font-size: 12px;
}}

h1, h2, p {{
  margin-top: 0;
}}

.contract-copy,
.contract-empty,
.contract-card p {{
  color: var(--contract-muted);
}}

label {{
  display: grid;
  gap: 6px;
  font-weight: 600;
}}

input,
textarea,
select {{
  border: 1px solid var(--contract-border);
  border-radius: 8px;
  padding: 11px 12px;
  font: inherit;
  background: white;
  min-width: 0;
}}

input:focus-visible,
textarea:focus-visible,
select:focus-visible,
button:focus-visible {{
  outline: 3px solid color-mix(in srgb, var(--contract-accent) 28%, transparent);
  outline-offset: 2px;
}}

button {{
  min-height: 44px;
  border: 0;
  border-radius: 8px;
  background: var(--contract-accent);
  color: white;
  padding: 10px 14px;
  font: inherit;
  font-weight: 700;
}}

[hidden] {{
  display: none !important;
}}

.contract-state {{
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--contract-border);
  background: white;
  font-weight: 700;
}}

.contract-loading {{
  color: var(--contract-muted);
}}

.contract-error {{
  border-color: #fecaca;
  background: #fef2f2;
  color: #991b1b;
}}

.contract-success {{
  border-color: #bbf7d0;
  background: #f0fdf4;
  color: #166534;
}}

.metrics-grid {{
  grid-template-columns: repeat(3, minmax(0, 1fr));
}}

.metric-card {{
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--contract-border);
  border-radius: 8px;
  background: var(--contract-panel);
}}

.metric-card span {{
  display: block;
  color: var(--contract-muted);
  font-size: 12px;
}}

.metric-card strong {{
  display: block;
  margin-top: 4px;
  color: var(--contract-accent);
  font-size: 22px;
}}

#contract-items {{
  display: grid;
  gap: 10px;
}}

.contract-card {{
  display: grid;
  gap: 8px;
  padding: 14px;
  background: var(--contract-panel);
  border: 1px solid var(--contract-border);
  border-radius: 8px;
}}

.contract-card span {{
  width: max-content;
  border-radius: 999px;
  padding: 4px 9px;
  background: color-mix(in srgb, var(--contract-accent) 12%, white);
  color: var(--contract-accent);
  font-size: 12px;
  font-weight: 700;
}}

@media (max-width: 420px) {{
  .page {{
    padding: 16px;
  }}

  .metrics-grid {{
    grid-template-columns: 1fr;
  }}

  .contract-card {{
    overflow-wrap: anywhere;
  }}
}}
{balanced_css}
{quality_css}
'''

    @staticmethod
    def _ensure_main_includes_generated_route(source_dir: Path) -> bool:
        main_path = source_dir / "miniapp/app/main.py"
        if not main_path.exists():
            return False
        original = main_path.read_text(encoding="utf-8")
        updated = original
        import_line = "from app.routes.generated_contract import router as generated_contract_router\n"
        include_line = "app.include_router(generated_contract_router)\n"
        if import_line not in updated:
            anchor = "from app.routes.role_routes import router as role_router\n"
            updated = updated.replace(anchor, anchor + import_line) if anchor in updated else import_line + updated
        if include_line not in updated:
            anchor = "app.include_router(role_router)\n"
            updated = updated.replace(anchor, anchor + include_line) if anchor in updated else updated + "\n" + include_line
        if updated == original:
            return False
        main_path.write_text(updated, encoding="utf-8")
        return True

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
                    "location": "miniapp/app/routes/generated_contract.py",
                }
                issues.append(issue)
                recipes.append(
                    RepairRecipe(
                        issue_code="registry.missing_backend_route",
                        expected_route=expected,
                        declared_routes=declared,
                        manifest_routes=manifest,
                        why_mismatch="The typed miniapp contract declares an API route that FastAPI route parsing cannot find.",
                        suggested_patch_target="miniapp/app/routes/generated_contract.py",
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
                        suggested_patch_target="miniapp/app/routes/generated_contract.py",
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
