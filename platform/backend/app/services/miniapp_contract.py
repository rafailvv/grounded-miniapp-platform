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
    ) -> MiniAppContract:
        mode_value = normalized_generation_mode(generation_mode) or GenerationMode.BALANCED.value
        intent_value = str(intent or "").strip().lower() or "create"
        hints = extract_prompt_planning_hints(prompt)
        terms = [str(item) for item in hints.get("prompt_terms") or [] if str(item).strip()]
        slug_source = next((term for term in terms if term not in {"client", "manager", "specialist"}), "items")
        slug = cls._plural_slug(slug_source)
        display_name = cls._display_name(slug_source)
        resource = cls._resource(slug=slug, display_name=display_name)
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
    def _resource(cls, *, slug: str, display_name: str) -> MiniAppResource:
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
                request_fields=["title", "note"],
            ),
            MiniAppEndpoint(
                endpoint_id=f"{slug}.update_status",
                method="PATCH",
                path=f"/api/{slug}/{{item_id}}/status",
                purpose="Persist a specialist or manager status update",
                resource=slug,
                role="specialist",
                request_fields=["status", "note"],
            ),
        ]
        return MiniAppResource(
            resource_id=slug,
            slug=slug,
            name=slug,
            display_name=display_name,
            fields=["id", "title", "note", "status", "created_by", "updated_by"],
            endpoints=endpoints,
        )

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
        slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
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
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api", tags=["contract-runtime"])
_NEXT_ID = count(1)
_ITEMS: list[dict[str, Any]] = []


class ContractItemCreate(BaseModel):
    title: str = Field(default="{title}", min_length=1)
    note: str = ""
    created_by: str = "client"


class ContractItemStatusUpdate(BaseModel):
    status: str = Field(default="updated", min_length=1)
    note: str = ""
    updated_by: str = "specialist"


@router.get("/{slug}")
def list_contract_items() -> list[dict[str, Any]]:
    return list(_ITEMS)


@router.post("/{slug}", status_code=201)
def create_contract_item(payload: ContractItemCreate) -> dict[str, Any]:
    item = {{
        "id": str(next(_NEXT_ID)),
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
    for item in _ITEMS:
        if str(item.get("id")) == str(item_id):
            item["status"] = payload.status
            item["note"] = payload.note or item.get("note", "")
            item["updated_by"] = payload.updated_by or "specialist"
            return item
    raise HTTPException(status_code=404, detail="Contract item not found")
'''

    @staticmethod
    def _render_python_tests(contract: MiniAppContract) -> str:
        resource = contract.resources[0]
        path = f"/api/{resource.slug}"
        return f'''from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class GeneratedContractRuntimeTest(unittest.TestCase):
    def test_contract_api_persists_create_and_status_update(self) -> None:
        with TestClient(app) as client:
            before = client.get({json.dumps(path)})
            self.assertEqual(before.status_code, 200)
            create = client.post({json.dumps(path)}, json={{"title": "Contract item", "note": "created"}})
            self.assertEqual(create.status_code, 201)
            created = create.json()
            self.assertEqual(created["status"], "new")
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
            files[f"miniapp/app/static/{role}/styles.css"] = cls._role_css(role=role)
        return files

    @staticmethod
    def _role_html(*, role: str, resource: MiniAppResource) -> str:
        title = {
            "client": f"Create {resource.display_name}",
            "specialist": f"Process {resource.display_name}",
            "manager": f"Review {resource.display_name}",
        }.get(role, resource.display_name)
        form = (
            '''
        <form id="contract-create-form" class="contract-form">
          <label>Title <input id="contract-title" name="title" required /></label>
          <label>Note <textarea id="contract-note" name="note"></textarea></label>
          <button type="submit">Save</button>
        </form>'''
            if role == "client"
            else '''
        <div class="contract-actions">
          <button id="contract-refresh" type="button">Refresh</button>
        </div>'''
        )
        return f'''<!doctype html>
<html lang="en">
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
          <p class="contract-eyebrow">{role.title()}</p>
          <h1>{title}</h1>
          <p id="contract-status" class="contract-copy">Loading saved workflow state.</p>
        </header>
{form}
        <section class="contract-list" aria-live="polite">
          <h2>Shared records</h2>
          <div id="contract-items"></div>
        </section>
      </section>
    </main>
    <script src="/static/preview_bridge.js" defer></script>
    <script src="/static/{role}/app.js" defer></script>
  </body>
</html>
'''

    @staticmethod
    def _role_js(*, role: str, resource: MiniAppResource) -> str:
        slug = resource.slug
        update_status = "processed" if role == "specialist" else "reviewed"
        create_handler = ""
        if role == "client":
            create_handler = f'''
const form = document.getElementById("contract-create-form");
if (form) {{
  form.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const formData = new FormData(form);
    await requestJson(API_BASE, {{
      method: "POST",
      body: JSON.stringify({{
        title: String(formData.get("title") || "{resource.display_name}"),
        note: String(formData.get("note") || ""),
        created_by: ROLE,
      }}),
    }});
    form.reset();
    await loadItems();
  }});
}}
'''
        update_handler = ""
        if role in {"specialist", "manager"}:
            update_handler = f'''
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
const statusNode = document.getElementById("contract-status");
const itemsRoot = document.getElementById("contract-items");

window.setupPreviewBridge?.(ROLE);

async function requestJson(path, options = {{}}) {{
  const response = await fetch(path, {{
    headers: {{ "Content-Type": "application/json", ...(options.headers || {{}}) }},
    ...options,
  }});
  if (!response.ok) throw new Error(`Request failed: ${{response.status}}`);
  return response.json();
}}

async function loadItems() {{
  try {{
    const items = await requestJson(API_BASE);
    renderItems(items);
    if (statusNode) statusNode.textContent = items.length ? `${{items.length}} saved records` : "No saved records yet.";
  }} catch (error) {{
    if (statusNode) statusNode.textContent = error.message || "Unable to load saved records.";
  }}
}}

function renderItems(items) {{
  if (!itemsRoot) return;
  if (!items.length) {{
    itemsRoot.innerHTML = '<p class="contract-empty">Create the first record to start the shared workflow.</p>';
    return;
  }}
  itemsRoot.innerHTML = items.map((item) => `
    <article class="contract-card">
      <strong>${{item.title || "{resource.display_name}"}}</strong>
      <span>${{item.status || "new"}}</span>
      <p>${{item.note || ""}}</p>
      {('<button type="button" data-update-id="${item.id}">Update status</button>' if role in {"specialist", "manager"} else '')}
    </article>
  `).join("");
}}
{create_handler}
{update_handler}
loadItems();
'''

    @staticmethod
    def _role_css(*, role: str) -> str:
        accent = {"client": "#0f766e", "specialist": "#2563eb", "manager": "#7c3aed"}.get(role, "#0f766e")
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
.contract-actions {{
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
textarea {{
  border: 1px solid var(--contract-border);
  border-radius: 8px;
  padding: 11px 12px;
  font: inherit;
  background: white;
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
