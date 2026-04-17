from __future__ import annotations

from collections import Counter
import json
from typing import Any, Callable

from app.models.artifacts import MaterializationReport
from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation, utc_now
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps


ROLE_ORDER = ("client", "specialist", "manager")


class MiniappArtifactBuilder:
    def __init__(
        self,
        *,
        normalize_role_route_path: Callable[[str, str], str],
        absolute_role_route_path: Callable[[str, str], str],
        default_page_asset_path: Callable[[str, str], str],
        normalize_runtime_python_path: Callable[[str], str],
    ) -> None:
        self._normalize_role_route_path = normalize_role_route_path
        self._absolute_role_route_path = absolute_role_route_path
        self._default_page_asset_path = default_page_asset_path
        self._normalize_runtime_python_path = normalize_runtime_python_path

    def ensure_runtime_artifact_operations(
        self,
        *,
        grounded_spec: GroundedSpecModel,
        page_graph: dict[str, Any],
        role_scope: list[str],
        generation_mode: GenerationMode,
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        route_manifest = self.route_manifest_from_page_graph(page_graph, role_scope)
        runtime_manifest = self.runtime_manifest_from_page_graph(route_manifest, grounded_spec, generation_mode)
        required_artifacts = {
            "miniapp/app/generated/route_manifest.json": (route_manifest, "Persist the canonical route manifest for the generated role pages."),
            "miniapp/app/generated/runtime_manifest.json": (runtime_manifest, "Persist the lightweight runtime manifest for the generated role pages."),
        }
        ensured_operations = [operation for operation in operations if operation.file_path not in required_artifacts]
        for file_path, (payload, reason) in required_artifacts.items():
            ensured_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=json_dumps(payload),
                    reason=reason,
                )
            )
        return ensured_operations

    def ensure_app_level_test_operations(
        self,
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
        operations: list[DraftFileOperation],
    ) -> list[DraftFileOperation]:
        required_tests = {
            "miniapp/tests/test_generated_app.py": self.python_app_level_test_content(page_graph=page_graph, role_scope=role_scope),
            "miniapp/tests/generated_app.test.mjs": self.js_app_level_test_content(page_graph=page_graph, role_scope=role_scope),
        }
        ensured_operations = [operation for operation in operations if operation.file_path not in required_tests]
        for file_path, content in required_tests.items():
            ensured_operations.append(
                DraftFileOperation(
                    file_path=file_path,
                    operation="replace",
                    content=content,
                    reason="Provide deterministic generated app-level tests for the synthesized miniapp workspace.",
                )
            )
        return ensured_operations

    def python_app_level_test_content(self, *, page_graph: dict[str, Any], role_scope: list[str]) -> str:
        roles_literal = ", ".join(repr(role) for role in role_scope)
        backend_targets = [
            self._normalize_runtime_python_path(str(path))
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        ]
        backend_targets_literal = json.dumps(sorted(dict.fromkeys(backend_targets)), ensure_ascii=True, indent=2)
        template = r'''from __future__ import annotations

import importlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

MINIAPP_DIR = Path(__file__).resolve().parents[1]
if str(MINIAPP_DIR) not in sys.path:
    sys.path.insert(0, str(MINIAPP_DIR))

ROLES = (__ROLES_LITERAL__,)
EXPECTED_BACKEND_TARGETS = __BACKEND_TARGETS_LITERAL__


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _extract_static_asset_refs(html: str) -> list[str]:
    return re.findall(r"(?:src|href)=[\"\\'](/static/[^\"\\']+)[\"\\']", html)


def _strip_route_template_expressions(content: str) -> str:
    return re.sub(r"\$\{[^}]+\}", "sample", str(content or ""))


def _extract_local_route_refs(content: str) -> set[str]:
    content = _strip_route_template_expressions(content)
    refs: set[str] = set()
    for match in re.finditer(r"(?:href|location(?:\\.href)?)\\s*=\\s*[\"\\'](/(?!api/|static/|/)[^\"\\'#?]*)", content):
        refs.add(match.group(1))
    for match in re.finditer(r"[\"\\'](/(?:client|specialist|manager)[^\"\\'#?]*)[\"\\']", content):
        refs.add(match.group(1))
    return refs


def _extract_js_dom_ids(source: str) -> set[str]:
    ids: set[str] = set()
    for match in re.finditer(r"getElementById\(\s*[\"\\']([^\"\\']+)[\"\\']\s*\)", source):
        ids.add(match.group(1))
    for match in re.finditer(r"querySelector\(\s*[\"\\']#([^\"\\']+)[\"\\']\s*\)", source):
        ids.add(match.group(1))
    return ids


def _normalize_route_ref(route_ref: str) -> str:
    normalized = route_ref.strip()
    normalized = re.sub(r"\$\{[^/]+\}", "sample", normalized)
    normalized = re.sub(r"\{[^/]+\}", "sample", normalized)
    normalized = re.sub(r":[^/]+", "sample", normalized)
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized or "/"


def _response_json(response):
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def _request_and_assert(client: TestClient, method: str, path: str, payload: dict | None = None):
    method = method.upper()
    if method == "GET":
        response = client.get(path)
    elif method == "POST":
        response = client.post(path, json=payload or {})
    elif method == "PUT":
        response = client.put(path, json=payload or {})
    elif method == "PATCH":
        response = client.patch(path, json=payload or {})
    elif method == "DELETE":
        response = client.delete(path, json=payload or {})
    else:
        raise AssertionError(f"Unsupported method {method} for path {path}")
    return response


def _sample_route_path(path: str) -> str:
    normalized = path
    normalized = re.sub(r"\$[{{][^/]+[}}]", "sample", normalized)
    normalized = re.sub(r"{{[^/]+}}", "sample", normalized)
    normalized = re.sub(r":[^/]+", "sample", normalized)
    return normalized


def _route_pattern_matches(pattern: str, actual: str) -> bool:
    normalized_pattern = re.sub(r"\{[^/]+\}", "[^/]+", pattern)
    normalized_pattern = re.sub(r":[^/]+", "[^/]+", normalized_pattern)
    return re.fullmatch(normalized_pattern, actual) is not None


def _resolve_path_params(path: str, replacements: dict[str, str]) -> str:
    resolved = path
    for key, value in replacements.items():
        resolved = re.sub(r"{{" + re.escape(key) + r"}}", value, resolved)
        resolved = re.sub(r":" + re.escape(key) + r"\b", value, resolved)
    resolved = re.sub(r"\$[{{][^/]+[}}]", "sample", resolved)
    resolved = re.sub(r"{{[^/]+}}", "sample", resolved)
    resolved = re.sub(r":[^/]+", "sample", resolved)
    return resolved


def _payload_contains_value(payload, target_value: str) -> bool:
    if payload == target_value:
        return True
    if isinstance(payload, dict):
        return any(_payload_contains_value(value, target_value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_value(item, target_value) for item in payload)
    return False


def _extract_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "records", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _resource_slug(path: str) -> str:
    parts = [segment for segment in path.split("/") if segment and not segment.startswith("{") and not segment.startswith(":")]
    api_parts = [segment for segment in parts if segment != "api"]
    return api_parts[0] if api_parts else ""


def _pick_workflow_api(requirements, *, methods, tokens_any, preferred_resource=None):
    methods = {method.upper() for method in methods}
    tokens_any = {token.lower() for token in tokens_any}
    preferred_resource = (preferred_resource or "").lower().strip() or None
    matches = []
    for requirement in requirements:
        method = str(requirement.get("method") or "").upper()
        path = str(requirement.get("path") or "")
        if method not in methods or not path.startswith("/api/"):
            continue
        lowered_path = path.lower()
        if not any(token in lowered_path for token in tokens_any):
            continue
        resource = _resource_slug(path)
        score = 0
        if preferred_resource and resource == preferred_resource:
            score += 10
        if "{" in path or ":" in path:
            score += 2
        matches.append((score, requirement))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _payload_for_api(requirement: dict, path: str, method: str, *, created_id: str | None = None) -> dict | None:
    method = method.upper()
    if method == "GET":
        return None
    payload = {}
    for field in requirement.get("fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in {"id", "request_id", "submission_id", "task_id", "item_id", "record_id"} and created_id:
            payload[name] = created_id
        elif lowered in {"status", "state"}:
            payload[name] = "open"
        elif lowered in {"comment", "note", "message"}:
            payload[name] = "Generated comment"
        elif lowered in {"specialist_id", "assignee_id", "owner_id", "user_id"}:
            payload[name] = "sample-user"
        elif "time" in lowered or "date" in lowered:
            payload[name] = "2026-04-17T10:00:00Z"
        else:
            payload[name] = "sample"
    if not payload and method in {"POST", "PUT", "PATCH"}:
        payload = {"name": "sample", "status": "open"}
    return payload


def _workflow_api_requirements(grounded_spec: dict) -> list[dict]:
    return [
        item
        for item in grounded_spec.get("api_requirements", [])
        if isinstance(item, dict)
        and str(item.get("path") or "").startswith("/api/")
        and not str(item.get("path") or "").startswith("/api/runtime/")
    ]


class GeneratedMiniAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._test_db_dir = TemporaryDirectory()
        cls._original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(cls._test_db_dir.name) / 'generated_test.db').as_posix()}"
        for module_name in [name for name in list(sys.modules) if name == "app" or name.startswith("app.")]:
            sys.modules.pop(module_name, None)
        from app.main import app

        cls._client_context = TestClient(app)
        cls.client = cls._client_context.__enter__()
        cls.route_manifest = _load_json(MINIAPP_DIR / "app" / "generated" / "route_manifest.json")
        cls.runtime_manifest = _load_json(MINIAPP_DIR / "app" / "generated" / "runtime_manifest.json")
        cls.grounded_spec = _load_json(MINIAPP_DIR.parent / "artifacts" / "grounded_spec.json") if (MINIAPP_DIR.parent / "artifacts" / "grounded_spec.json").exists() else {}
        cls.workflow_api_requirements = _workflow_api_requirements(cls.grounded_spec)

    @classmethod
    def tearDownClass(cls) -> None:
        client_context = getattr(cls, "_client_context", None)
        if client_context is not None:
            client_context.__exit__(None, None, None)
        original_database_url = getattr(cls, "_original_database_url", None)
        if original_database_url:
            os.environ["DATABASE_URL"] = original_database_url
        else:
            os.environ.pop("DATABASE_URL", None)
        test_db_dir = getattr(cls, "_test_db_dir", None)
        if test_db_dir is not None:
            test_db_dir.cleanup()

    def test_generated_manifests_exist(self) -> None:
        self.assertIsInstance(self.route_manifest.get("roles"), dict)
        self.assertIsInstance(self.runtime_manifest.get("roles"), dict)

    def test_role_pages_and_static_assets_exist(self) -> None:
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            self.assertTrue(pages, f"No pages declared for role {role}")
            file_paths = [str(page.get("file_path") or "") for page in pages if isinstance(page, dict)]
            self.assertEqual(len(set(file_paths)), len(file_paths), f"Role {role} reuses the same file across multiple routes: {file_paths}")
            for page in pages:
                if not isinstance(page, dict):
                    continue
                file_path = str(page.get("file_path") or "")
                style_path = str(page.get("style_path") or "")
                script_path = str(page.get("script_path") or "")
                self.assertTrue(file_path.startswith("miniapp/"), f"Unexpected page file path {file_path}")
                absolute_page_path = MINIAPP_DIR / file_path.removeprefix("miniapp/")
                self.assertTrue(absolute_page_path.exists(), f"Missing page file {file_path}")
                self.assertTrue(style_path.startswith("miniapp/"), f"Unexpected page style path {style_path}")
                self.assertTrue(script_path.startswith("miniapp/"), f"Unexpected page script path {script_path}")
                self.assertTrue((MINIAPP_DIR / style_path.removeprefix("miniapp/")).exists(), f"Missing page style {style_path}")
                self.assertTrue((MINIAPP_DIR / script_path.removeprefix("miniapp/")).exists(), f"Missing page script {script_path}")
                html = absolute_page_path.read_text(encoding="utf-8")
                self.assertIn("<html", html.lower(), f"Page {file_path} does not look like HTML")
                self.assertGreater(len(html.strip()), 80, f"Page {file_path} is unexpectedly short")
                self.assertIn("/static/shared/base.css", html, f"Page {file_path} must reference /static/shared/base.css")
                self.assertIn("/" + style_path.removeprefix("miniapp/app/").replace("\\", "/"), html, f"Page {file_path} must reference its own CSS file")
                self.assertIn("/" + script_path.removeprefix("miniapp/app/").replace("\\", "/"), html, f"Page {file_path} must reference its own JS file")
                self.assertNotRegex(html, r">\\s*Refresh\\s*<", f"Page {file_path} should not render a manual refresh action")
                script_text = (MINIAPP_DIR / script_path.removeprefix("miniapp/")).read_text(encoding="utf-8")
                for dom_id in _extract_js_dom_ids(script_text):
                    self.assertIn(f'id="{dom_id}"', html, f"Page {file_path} is missing DOM id {dom_id} referenced by {script_path}")
                for asset in _extract_static_asset_refs(html):
                    asset_path = MINIAPP_DIR / "app" / "static" / asset.removeprefix("/static/")
                    self.assertTrue(asset_path.exists(), f"Missing referenced asset {asset} from {file_path}")

    def test_planned_api_refs_map_to_runtime_routes(self) -> None:
        expected_api_paths = {
            str(item.get("path") or "")
            for item in self.workflow_api_requirements
            if str(item.get("path") or "").startswith("/api/")
        }
        script = """
import json
from app.main import app
routes = []
for route in app.routes:
    path = getattr(route, 'path', None)
    methods = sorted(getattr(route, 'methods', []) or [])
    if path:
        routes.append({'path': path, 'methods': methods})
print(json.dumps(routes))
"""
        from subprocess import run
        import sys

        result = run([sys.executable, "-c", script], cwd=MINIAPP_DIR, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, f"Failed to inspect FastAPI routes\n{result.stderr or result.stdout}")
        registered_routes = json.loads(result.stdout)
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for target_path in [page.get("file_path"), page.get("style_path"), page.get("script_path")]:
                    absolute_path = MINIAPP_DIR / str(target_path or "").removeprefix("miniapp/")
                    if not absolute_path.exists():
                        continue
                    for api_ref in re.findall(r"[\"\\'](/api/[a-zA-Z0-9_/:{}-]+)", absolute_path.read_text(encoding="utf-8")):
                        expected_api_paths.add(api_ref)
        for api_path in expected_api_paths:
            matches = [
                item
                for item in registered_routes
                if _route_pattern_matches(str(item.get("path") or ""), api_path)
                or _sample_route_path(str(item.get("path") or "")) == _sample_route_path(api_path)
            ]
            self.assertTrue(matches, f"No registered backend route matches planned API path {api_path}")

    def test_local_page_links_render(self) -> None:
        declared_routes = set()
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            for page in pages:
                if isinstance(page, dict):
                    declared_routes.add(_normalize_route_ref(str(page.get("route_path") or "")))
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                sources = [
                    MINIAPP_DIR / str(target_path or "").removeprefix("miniapp/")
                    for target_path in [page.get("file_path"), page.get("script_path")]
                ]
                refs: set[str] = set()
                for source_path in sources:
                    if not source_path.exists():
                        continue
                    refs.update(_extract_local_route_refs(source_path.read_text(encoding="utf-8")))
                for route_ref in refs:
                    self.assertIn(_normalize_route_ref(route_ref), declared_routes, f"Route {route_ref} referenced by {page.get('file_path')} is not declared in route_manifest.json")

    def test_grounded_workflow_roles_have_pages(self) -> None:
        actor_by_id = {
            str(actor.get("actor_id") or ""): str(actor.get("role") or "").lower()
            for actor in self.grounded_spec.get("actors", [])
            if isinstance(actor, dict)
        }
        for flow in self.grounded_spec.get("user_flows", []):
            if not isinstance(flow, dict):
                continue
            for step in flow.get("steps", []):
                if not isinstance(step, dict):
                    continue
                role = actor_by_id.get(str(step.get("actor_id") or ""))
                if not role or role not in ROLES:
                    continue
                pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
                self.assertTrue(pages, f"Workflow role {role} from flow {flow.get('name') or flow.get('flow_id') or 'unknown'} has no declared pages")

    def test_detected_workflow_lifecycle_executes(self) -> None:
        resource = None
        list_api = next(
            (
                item
                for item in self.workflow_api_requirements
                if str(item.get("method") or "").upper() == "GET"
                and "{" not in str(item.get("path") or "")
                and ":" not in str(item.get("path") or "")
            ),
            None,
        )
        if list_api:
            resource = _resource_slug(str(list_api.get("path") or ""))
            list_path = str(list_api.get("path") or "")
            list_response = _request_and_assert(self.client, "GET", list_path)
            self.assertNotEqual(list_response.status_code, 404, f"Workflow list route missing at runtime: {list_path}")
            self.assertLess(list_response.status_code, 500, f"Workflow list route failed: {list_response.text}")
            list_payload = _response_json(list_response)
        else:
            list_payload = {}

        create_api = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"POST"},
            tokens_any={"create", "new", "request", "submission", "task", "booking", "reserve"},
            preferred_resource=resource,
        )
        created_id = None
        if create_api:
            create_path = str(create_api.get("path") or "")
            create_payload = _payload_for_api(create_api, create_path, "POST") or {}
            create_response = _request_and_assert(self.client, "POST", create_path, payload=create_payload)
            self.assertNotEqual(create_response.status_code, 404, f"Workflow create route missing at runtime: {create_path}")
            self.assertLess(create_response.status_code, 500, f"Workflow create failed: {create_response.text}")
            create_body = _response_json(create_response)
            for candidate_key in ("id", "request_id", "submission_id", "task_id", "item_id", "record_id"):
                candidate_value = create_body.get(candidate_key) if isinstance(create_body, dict) else None
                if isinstance(candidate_value, (str, int)):
                    created_id = str(candidate_value)
                    break
            if created_id:
                items = _extract_items(list_payload)
                if items:
                    self.assertTrue(
                        any(_payload_contains_value(item, created_id) for item in items),
                        f"Created workflow record {created_id} is not visible in list payload {list_payload}",
                    )

        if not created_id:
            return

        path_replacements = {
            "id": created_id,
            "request_id": created_id,
            "submission_id": created_id,
            "task_id": created_id,
            "item_id": created_id,
            "record_id": created_id,
        }
        detail_api = next(
            (
                item
                for item in self.workflow_api_requirements
                if item.get("method") == "GET"
                and "{" in str(item.get("path") or "")
                and (not resource or _resource_slug(str(item.get("path") or "")) == resource)
            ),
            None,
        )
        if detail_api:
            detail_path = _resolve_path_params(str(detail_api.get("path") or ""), path_replacements)
            detail_response = _request_and_assert(self.client, "GET", detail_path)
            self.assertNotEqual(detail_response.status_code, 404, f"Workflow detail route missing at runtime: {detail_path}")
            self.assertLess(detail_response.status_code, 500, f"Workflow detail failed: {detail_response.text}")

        assign_api = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"POST", "PUT", "PATCH"},
            tokens_any={"assign", "owner", "specialist", "assignee", "claim"},
            preferred_resource=resource or None,
        )
        if assign_api:
            assign_path = _resolve_path_params(str(assign_api.get("path") or ""), path_replacements)
            assign_method = str(assign_api.get("method") or "PATCH")
            assign_payload = _payload_for_api(assign_api, assign_path, assign_method, created_id=created_id) or {}
            assign_response = _request_and_assert(self.client, assign_method, assign_path, payload=assign_payload)
            self.assertNotEqual(assign_response.status_code, 404, f"Workflow assignment route missing at runtime: {assign_path}")
            self.assertLess(assign_response.status_code, 500, f"Workflow assignment failed: {assign_response.text}")

        status_api = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"PUT", "PATCH", "POST"},
            tokens_any={"status", "progress", "complete", "update"},
            preferred_resource=resource or None,
        )
        if status_api:
            status_path = _resolve_path_params(str(status_api.get("path") or ""), path_replacements)
            status_method = str(status_api.get("method") or "PATCH")
            status_payload = _payload_for_api(status_api, status_path, status_method, created_id=created_id) or {}
            status_payload.setdefault("status", "in_progress")
            status_response = _request_and_assert(self.client, status_method, status_path, payload=status_payload)
            self.assertNotEqual(status_response.status_code, 404, f"Workflow status route missing at runtime: {status_path}")
            self.assertLess(status_response.status_code, 500, f"Workflow status update failed: {status_response.text}")

        comment_api = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"POST", "PUT", "PATCH"},
            tokens_any={"comment", "note", "notes", "message", "history"},
            preferred_resource=resource or None,
        )
        if comment_api:
            comment_path = _resolve_path_params(str(comment_api.get("path") or ""), path_replacements)
            comment_method = str(comment_api.get("method") or "POST")
            comment_payload = _payload_for_api(comment_api, comment_path, comment_method, created_id=created_id) or {}
            if "comment" not in {key.lower() for key in comment_payload}:
                comment_payload["comment"] = "Workflow comment"
            comment_response = _request_and_assert(self.client, comment_method, comment_path, payload=comment_payload)
            self.assertNotEqual(comment_response.status_code, 404, f"Workflow comment route missing at runtime: {comment_path}")
            self.assertLess(comment_response.status_code, 500, f"Workflow comment failed: {comment_response.text}")

    def test_backend_route_modules_import(self) -> None:
        for file_path in EXPECTED_BACKEND_TARGETS:
            if not file_path.startswith("miniapp/app/routes/") or not file_path.endswith(".py"):
                continue
            module_name = file_path.removeprefix("miniapp/").removesuffix(".py").replace("/", ".")
            importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
'''
        return (
            template
            .replace("__ROLES_LITERAL__", roles_literal)
            .replace("__BACKEND_TARGETS_LITERAL__", backend_targets_literal)
        )

    def js_app_level_test_content(self, *, page_graph: dict[str, Any], role_scope: list[str]) -> str:
        roles_literal = json.dumps(list(role_scope), ensure_ascii=True)
        template = r"""import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const MINIAPP_DIR = path.resolve(__dirname, '..');
const APP_DIR = path.join(MINIAPP_DIR, 'app');
const ROUTE_MANIFEST_PATH = path.join(APP_DIR, 'generated', 'route_manifest.json');
const RUNTIME_MANIFEST_PATH = path.join(APP_DIR, 'generated', 'runtime_manifest.json');
const GROUNDED_SPEC_PATH = path.join(MINIAPP_DIR, '..', 'artifacts', 'grounded_spec.json');
const ROLES = __ROLES_LITERAL__;

function loadJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function collectFiles(rootDir, extension) {
  const items = [];
  for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
    const absolutePath = path.join(rootDir, entry.name);
    if (entry.isDirectory()) {
      items.push(...collectFiles(absolutePath, extension));
      continue;
    }
    if (absolutePath.endsWith(extension)) {
      items.push(absolutePath);
    }
  }
  return items;
}

function extractStaticAssets(html) {
  const assets = [];
  const regex = /(?:src|href)=["'](\/static\/[^"']+)["']/g;
  for (const match of html.matchAll(regex)) {
    assets.push(match[1]);
  }
  return assets;
}

function extractApiRefs(content) {
  const refs = new Set();
  const regex = /["'](\/api\/[a-zA-Z0-9_\-/:{}]+)/g;
  for (const match of content.matchAll(regex)) {
    refs.add(match[1]);
  }
  return refs;
}

function stripRouteTemplateExpressions(content) {
  return String(content ?? '').replace(/\$\{[^}]+\}/g, 'sample');
}

function extractLocalRouteRefs(content) {
  content = stripRouteTemplateExpressions(content);
  const refs = new Set();
  const patterns = [
    /(?:href|location(?:\.href)?)\s*=\s*["'](\/(?:client|specialist|manager)(?:\/[^"'#?]*)?)["']/g,
    /["'](\/(?:client|specialist|manager)[^"'#?]*)["']/g,
  ];
  for (const pattern of patterns) {
    for (const match of content.matchAll(pattern)) {
      refs.add(match[1]);
    }
  }
  return refs;
}

function extractHtmlIds(html) {
  const ids = new Set();
  const regex = /id=["']([^"']+)["']/g;
  for (const match of html.matchAll(regex)) {
    ids.add(match[1]);
  }
  return ids;
}

function extractJsDomIds(content) {
  const ids = new Set();
  const patterns = [
    /getElementById\(["']([^"']+)["']\)/g,
    /querySelector\(["']#([^"']+)["']\)/g,
  ];
  for (const pattern of patterns) {
    for (const match of content.matchAll(pattern)) {
      ids.add(match[1]);
    }
  }
  return ids;
}

function normalizeRoutePath(value) {
  return String(value ?? '')
    .replace(/[$][{][^/]+[}]/g, 'sample')
    .replace(/\$\{[^}]+\}/g, 'sample')
    .replace(/\{[^/]+\}/g, 'sample')
    .replace(/:[^/]+/g, 'sample');
}

function routePatternMatches(pattern, actual) {
  const normalizedPattern = String(pattern ?? '')
    .replace(/\{[^/]+\}/g, '[^/]+')
    .replace(/:[^/]+/g, '[^/]+');
  return new RegExp(`^${normalizedPattern}$`).test(actual);
}

function collectRegisteredRoutes() {
  const script = `
import json
from app.main import app
routes = []
for route in app.routes:
    path = getattr(route, 'path', None)
    methods = sorted(getattr(route, 'methods', []) or [])
    if path:
        routes.append({'path': path, 'methods': methods})
print(json.dumps(routes))
`;
  const env = { ...process.env, PYTHONPATH: [MINIAPP_DIR, process.env.PYTHONPATH || ''].filter(Boolean).join(path.delimiter) };
  const result = spawnSync(process.env.PYTHON || 'python3', ['-c', script], {
    cwd: MINIAPP_DIR,
    encoding: 'utf8',
    env,
  });
  assert.equal(result.status, 0, `Failed to inspect FastAPI routes\n${result.stderr || result.stdout}`);
  return JSON.parse(result.stdout);
}

test('generated manifests exist', () => {
  assert.equal(fs.existsSync(ROUTE_MANIFEST_PATH), true, `Missing ${ROUTE_MANIFEST_PATH}`);
  assert.equal(fs.existsSync(RUNTIME_MANIFEST_PATH), true, `Missing ${RUNTIME_MANIFEST_PATH}`);
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  const runtimeManifest = loadJson(RUNTIME_MANIFEST_PATH);
  assert.equal(typeof routeManifest.roles, 'object');
  assert.equal(typeof runtimeManifest.roles, 'object');
});

test('role pages and static assets exist', () => {
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  for (const role of ROLES) {
    const pages = routeManifest.roles?.[role]?.pages ?? [];
    assert.ok(pages.length > 0, `No pages declared for role ${role}`);
    const filePaths = pages.map((page) => String(page.file_path ?? ''));
    assert.equal(new Set(filePaths).size, filePaths.length, `Role ${role} reuses the same file across multiple routes: ${filePaths.join(', ')}`);
    for (const page of pages) {
      const filePath = String(page.file_path ?? '');
      const stylePath = String(page.style_path ?? '');
      const scriptPath = String(page.script_path ?? '');
      assert.ok(filePath.startsWith('miniapp/'), `Unexpected page file path ${filePath}`);
      const absolutePagePath = path.join(MINIAPP_DIR, filePath.replace(/^miniapp\//, ''));
      assert.ok(fs.existsSync(absolutePagePath), `Missing page file ${filePath}`);
      assert.ok(stylePath.startsWith('miniapp/'), `Unexpected page style path ${stylePath}`);
      assert.ok(scriptPath.startsWith('miniapp/'), `Unexpected page script path ${scriptPath}`);
      assert.ok(fs.existsSync(path.join(MINIAPP_DIR, stylePath.replace(/^miniapp\//, ''))), `Missing page style ${stylePath}`);
      assert.ok(fs.existsSync(path.join(MINIAPP_DIR, scriptPath.replace(/^miniapp\//, ''))), `Missing page script ${scriptPath}`);
      const html = fs.readFileSync(absolutePagePath, 'utf8');
      assert.ok(html.toLowerCase().includes('<html'), `Page ${filePath} does not look like HTML`);
      assert.ok(html.trim().length > 80, `Page ${filePath} is unexpectedly short`);
      assert.ok(html.includes('/static/shared/base.css'), `Page ${filePath} must reference /static/shared/base.css`);
      assert.ok(html.includes('/' + stylePath.replace(/^miniapp\/app\//, '').replace(/\\/g, '/')), `Page ${filePath} must reference its own CSS file`);
      assert.ok(html.includes('/' + scriptPath.replace(/^miniapp\/app\//, '').replace(/\\/g, '/')), `Page ${filePath} must reference its own JS file`);
      assert.equal(/>\s*Refresh\s*</.test(html), false, `Page ${filePath} should not render a manual refresh action`);
      for (const asset of extractStaticAssets(html)) {
        const absoluteAssetPath = path.join(APP_DIR, asset.replace(/^\/static\//, 'static/'));
        assert.ok(fs.existsSync(absoluteAssetPath), `Missing referenced asset ${asset} from ${filePath}`);
      }
      const htmlIds = extractHtmlIds(html);
      const scriptSource = fs.readFileSync(path.join(MINIAPP_DIR, scriptPath.replace(/^miniapp\//, '')), 'utf8');
      const missingIds = [...extractJsDomIds(scriptSource)].filter((id) => !htmlIds.has(id));
      assert.deepEqual(missingIds, [], `Page ${filePath} is missing DOM ids required by ${scriptPath}: ${missingIds.join(', ')}`);
    }
  }
});

test('planned api references map to registered backend routes', () => {
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  const groundedSpec = fs.existsSync(GROUNDED_SPEC_PATH) ? loadJson(GROUNDED_SPEC_PATH) : {};
  const expectedApiPaths = new Set(
    (groundedSpec.api_requirements ?? [])
      .map((item) => String(item?.path ?? ''))
      .filter((value) => value.startsWith('/api/'))
  );
  for (const role of ROLES) {
    const pages = routeManifest.roles?.[role]?.pages ?? [];
    for (const page of pages) {
      for (const targetPath of [page.file_path, page.style_path, page.script_path]) {
        const absolutePath = path.join(MINIAPP_DIR, String(targetPath ?? '').replace(/^miniapp\//, ''));
        if (!fs.existsSync(absolutePath)) {
          continue;
        }
        for (const apiRef of extractApiRefs(fs.readFileSync(absolutePath, 'utf8'))) {
          expectedApiPaths.add(apiRef);
        }
      }
    }
  }
  const registeredRoutes = collectRegisteredRoutes();
  for (const apiPath of expectedApiPaths) {
    const normalized = normalizeRoutePath(apiPath);
    const matches = registeredRoutes.filter((item) => {
      const pattern = String(item.path ?? '');
      return routePatternMatches(pattern, apiPath) || normalizeRoutePath(pattern) === normalized;
    });
    assert.ok(matches.length > 0, `No registered backend route matches planned API path ${apiPath}`);
  }
});

test('page-local navigation targets resolve to declared routes', () => {
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  const declaredRoutes = new Set();
  for (const role of ROLES) {
    const pages = routeManifest.roles?.[role]?.pages ?? [];
    for (const page of pages) {
      declaredRoutes.add(normalizeRoutePath(String(page.route_path ?? '')));
    }
  }
  for (const role of ROLES) {
    const pages = routeManifest.roles?.[role]?.pages ?? [];
    for (const page of pages) {
      const sources = [page.file_path, page.script_path]
        .map((targetPath) => path.join(MINIAPP_DIR, String(targetPath ?? '').replace(/^miniapp\//, '')))
        .filter((absolutePath) => fs.existsSync(absolutePath))
        .map((absolutePath) => fs.readFileSync(absolutePath, 'utf8'));
      const refs = new Set();
      for (const source of sources) {
        for (const routeRef of extractLocalRouteRefs(source)) {
          refs.add(routeRef);
        }
      }
      for (const routeRef of refs) {
        assert.ok(declaredRoutes.has(normalizeRoutePath(routeRef)), `Route ${routeRef} referenced by ${page.file_path} is not declared in route_manifest.json`);
      }
    }
  }
});

test('grounded workflow roles map to declared page surfaces', () => {
  const routeManifest = loadJson(ROUTE_MANIFEST_PATH);
  const groundedSpec = fs.existsSync(GROUNDED_SPEC_PATH) ? loadJson(GROUNDED_SPEC_PATH) : {};
  const actorById = new Map((groundedSpec.actors ?? []).map((actor) => [String(actor.actor_id ?? ''), String(actor.role ?? '').toLowerCase()]));
  for (const flow of groundedSpec.user_flows ?? []) {
    for (const step of flow.steps ?? []) {
      const role = actorById.get(String(step.actor_id ?? ''));
      if (!role || !ROLES.includes(role)) {
        continue;
      }
      const pages = routeManifest.roles?.[role]?.pages ?? [];
      assert.ok(pages.length > 0, `Workflow role ${role} from flow ${flow.name ?? flow.flow_id ?? 'unknown'} has no declared pages`);
    }
  }
});

test('generated javascript files parse', () => {
  const jsFiles = collectFiles(path.join(APP_DIR, 'static'), '.js');
  assert.ok(jsFiles.length > 0, 'No generated JavaScript files found');
  for (const filePath of jsFiles) {
    const result = spawnSync(process.execPath, ['--check', filePath], { encoding: 'utf8' });
    assert.equal(result.status, 0, `node --check failed for ${filePath}\n${result.stderr || result.stdout}`);
  }
});
"""
        return template.replace("__ROLES_LITERAL__", roles_literal)

    def route_manifest_from_page_graph(self, page_graph: dict[str, Any], role_scope: list[str]) -> dict[str, Any]:
        roles: dict[str, Any] = {}
        role_payloads = page_graph.get("roles") or {}
        for role in role_scope:
            payload = role_payloads.get(role) or {}
            pages: list[dict[str, Any]] = []
            for index, page in enumerate(payload.get("pages") or []):
                if not isinstance(page, dict):
                    continue
                route_path = str(page.get("route_path") or f"/{role}").strip() or f"/{role}"
                normalized_role_route = self._normalize_role_route_path(role, route_path)
                normalized_route = self._absolute_role_route_path(role, normalized_role_route)
                page_kind = str(page.get("page_kind") or "").strip().lower()
                file_path = str(page.get("file_path") or f"miniapp/app/static/{role}/index.html")
                default_style_path = self._default_page_asset_path(file_path, "css")
                default_script_path = self._default_page_asset_path(file_path, "js")
                style_path = str(page.get("style_path") or default_style_path)
                script_path = str(page.get("script_path") or default_script_path)
                if not style_path.startswith(f"miniapp/app/static/{role}/"):
                    style_path = default_style_path
                if not script_path.startswith(f"miniapp/app/static/{role}/"):
                    script_path = default_script_path
                pages.append(
                    {
                        "page_id": str(page.get("page_id") or f"{role}_{index + 1}"),
                        "route_path": normalized_route,
                        "file_path": file_path,
                        "style_path": style_path,
                        "script_path": script_path,
                        "page_kind": page_kind or ("profile" if normalized_route.endswith("/profile") else "page"),
                        "navigation_label": str(page.get("navigation_label") or page.get("title") or "Open"),
                        "title": str(page.get("title") or page.get("navigation_label") or role.title()),
                        "handoff_paths": [str(path) for path in (page.get("handoff_paths") or []) if isinstance(path, str)],
                        "is_entry": bool(page.get("is_entry") or normalized_route == f"/{role}"),
                    }
                )
            roles[role] = {"entry_path": str(payload.get("entry_path") or f"/{role}"), "pages": pages}
        return {"roles": roles}

    def runtime_manifest_from_page_graph(
        self,
        route_manifest: dict[str, Any],
        grounded_spec: GroundedSpecModel,
        generation_mode: GenerationMode,
    ) -> dict[str, Any]:
        roles: dict[str, Any] = {}
        for role in ROLE_ORDER:
            role_payload = ((route_manifest.get("roles") or {}).get(role) or {})
            pages = [page for page in (role_payload.get("pages") or []) if isinstance(page, dict)]
            routes = []
            screens = {}
            navigation = []
            route_tree = []
            for index, page in enumerate(pages):
                route_path = str(page.get("route_path") or f"/{role}")
                screen_id = str(page.get("page_id") or f"{role}_{index + 1}")
                title = str(page.get("title") or page.get("navigation_label") or role.title())
                page_kind = str(page.get("page_kind") or "page")
                route_tree.append(route_path)
                routes.append(
                    {
                        "route_id": f"{role}_route_{index + 1}",
                        "role": role,
                        "path": route_path,
                        "screen_id": screen_id,
                        "label": str(page.get("navigation_label") or title),
                        "is_entry": bool(page.get("is_entry")),
                    }
                )
                screens[screen_id] = {
                    "screen_id": screen_id,
                    "path": route_path,
                    "title": title,
                    "subtitle": grounded_spec.product_goal[:160],
                    "kind": page_kind,
                    "page_purpose": title,
                    "handoff_paths": list(page.get("handoff_paths") or []),
                    "components": [],
                    "actions": [],
                    "sections": [],
                    "state_key": f"{role}:{screen_id}",
                }
                navigation.append({"path": route_path, "label": str(page.get("navigation_label") or title), "is_entry": bool(page.get("is_entry"))})
            roles[role] = {
                "entry_path": str(role_payload.get("entry_path") or f"/{role}"),
                "route_tree": route_tree,
                "routes": routes,
                "screens": screens,
                "action_model": [],
                "navigation": navigation,
            }
        return {
            "app": {
                "title": grounded_spec.product_goal[:80],
                "goal": grounded_spec.product_goal,
                "generation_mode": generation_mode.value,
                "ui_variant": "generated",
                "layout_variant": "stacked",
                "theme": {"accent": "#2d7ff9", "surface": "#ffffff", "card": "#f8fbff", "border": "#d8e4f7"},
                "platform": grounded_spec.target_platform,
                "route_count": sum(len(role_payload["routes"]) for role_payload in roles.values()),
                "screen_count": sum(len(role_payload["screens"]) for role_payload in roles.values()),
            },
            "roles": roles,
        }

    def build_stage_reports(
        self,
        *,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> list[dict[str, Any]]:
        planned_backend = {
            str(path)
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        }
        role_page_paths = {
            str(page.get("file_path"))
            for role in role_scope
            for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("file_path"), str)
        }
        required_manifests = {
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        }
        backend_hits = sorted(path for path in planned_backend if path in realized_paths)
        page_hits = sorted(path for path in role_page_paths if path in realized_paths)
        manifest_hits = sorted(path for path in required_manifests if path in realized_paths)
        return [
            {
                "stage": "runtime_backend_foundation",
                "planned_files": sorted(planned_backend),
                "created_files": backend_hits,
                "completed": bool(planned_backend) and len(backend_hits) >= max(1, min(len(planned_backend), 3)),
            },
            {
                "stage": "workflow_page_surfaces",
                "planned_files": sorted(role_page_paths),
                "created_files": page_hits,
                "completed": len(page_hits) >= max(len(role_scope), len(role_page_paths) // 2 if role_page_paths else 0),
            },
            {
                "stage": "integration_contract_completion",
                "planned_files": sorted(required_manifests),
                "created_files": manifest_hits,
                "completed": "miniapp/app/generated/route_manifest.json" in manifest_hits
                and "artifacts/generated_app_graph.json" in manifest_hits,
            },
        ]

    def build_materialization_report(
        self,
        *,
        execution_class: str,
        page_graph: dict[str, Any],
        role_scope: list[str],
        realized_paths: set[str],
    ) -> MaterializationReport:
        normalized_realized_paths = {
            self._normalize_runtime_python_path(str(path))
            for path in realized_paths
            if isinstance(path, str)
        }
        planned_pages = [
            self._normalize_runtime_python_path(str(page.get("file_path")))
            for role in role_scope
            for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
            if isinstance(page, dict) and isinstance(page.get("file_path"), str)
        ]
        expected_backend_files = [
            self._normalize_runtime_python_path(str(path))
            for path in [*(page_graph.get("backend_targets") or []), *(page_graph.get("shared_files") or [])]
            if isinstance(path, str) and path.startswith("miniapp/app/")
        ]
        expected_manifests = [
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        ]
        missing_files = [path for path in planned_pages if path not in normalized_realized_paths]
        missing_backend_files = [path for path in expected_backend_files if path not in normalized_realized_paths]
        role_unique_page_counts: dict[str, int] = {}
        duplicate_page_file_roles: dict[str, list[str]] = {}
        role_page_counts = {
            role: sum(
                1
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict)
                and self._normalize_runtime_python_path(str(page.get("file_path") or "")) in normalized_realized_paths
            )
            for role in role_scope
        }
        for role in role_scope:
            role_pages = [
                self._normalize_runtime_python_path(str(page.get("file_path") or ""))
                for page in (page_graph.get("roles", {}).get(role, {}) or {}).get("pages", [])
                if isinstance(page, dict) and isinstance(page.get("file_path"), str)
            ]
            role_unique_page_counts[role] = len(set(role_pages))
            duplicates = sorted(path for path, count in Counter(role_pages).items() if count > 1 and path)
            if duplicates:
                duplicate_page_file_roles[role] = duplicates
        backend_surface_ok = bool(expected_backend_files) and not missing_backend_files
        page_surface_ok = (
            not missing_files
            and not duplicate_page_file_roles
            and all(count >= 2 for count in role_page_counts.values())
        )
        manifest_surface_ok = all(path in normalized_realized_paths for path in expected_manifests)
        collapsed_surface = not page_surface_ok and all(count <= 2 for count in role_page_counts.values())
        return MaterializationReport(
            execution_class=execution_class,  # type: ignore[arg-type]
            planned_files=sorted(dict.fromkeys(planned_pages)),
            created_files=sorted(normalized_realized_paths),
            missing_files=sorted(dict.fromkeys(missing_files)),
            expected_backend_files=sorted(dict.fromkeys(expected_backend_files)),
            missing_backend_files=sorted(dict.fromkeys(missing_backend_files)),
            backend_surface_ok=backend_surface_ok,
            page_surface_ok=page_surface_ok,
            manifest_surface_ok=manifest_surface_ok,
            collapsed_surface=collapsed_surface,
            role_page_counts=role_page_counts,
            role_unique_page_counts=role_unique_page_counts,
            duplicate_page_file_roles=duplicate_page_file_roles,
            stage_reports=self.build_stage_reports(page_graph=page_graph, role_scope=role_scope, realized_paths=normalized_realized_paths),
        )
