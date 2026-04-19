from __future__ import annotations

import json
from typing import Any


class ArtifactPythonTestsMixin:
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
        response = client.delete(path)
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


def _extract_record_id(payload):
    if isinstance(payload, dict):
        for key in ("id", "request_id", "submission_id", "task_id", "item_id", "record_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            candidate = _extract_record_id(value)
            if candidate:
                return candidate
    if isinstance(payload, list):
        for item in payload:
            candidate = _extract_record_id(item)
            if candidate:
                return candidate
    return None


def _contains_status(payload, expected_status: str) -> bool:
    if payload == expected_status:
        return True
    if isinstance(payload, dict):
        return any(_contains_status(value, expected_status) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_status(item, expected_status) for item in payload)
    return False


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
            payload[name] = "submitted"
        elif lowered in {"comment", "note", "message"}:
            payload[name] = "Generated comment"
        elif lowered in {"specialist_id", "assignee_id", "owner_id", "user_id"}:
            payload[name] = "sample-user"
        elif "time" in lowered or "date" in lowered:
            payload[name] = "2026-04-17T10:00:00Z"
        else:
            payload[name] = "sample"
    if not payload:
        path_tokens = str(path).lower()
        if any(token in path_tokens for token in ("booking", "reservation", "equipment", "request", "requests", "loan")):
            payload = {
                "item_type": "Laptop",
                "item_label": "ThinkPad T14",
                "start_date": "2026-04-17T10:00:00Z",
                "end_date": "2026-04-18T18:00:00Z",
                "reason": "Resource needed for an internal team session.",
            }
    if not payload and method in {"POST", "PUT", "PATCH"}:
        payload = {"name": "sample", "status": "submitted"}
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

    def test_declared_role_routes_render_shell_contract(self) -> None:
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            self.assertTrue(pages, f"No declared pages for role {role}")
            for page in pages:
                if not isinstance(page, dict):
                    continue
                route_path = _sample_route_path(str(page.get("route_path") or "").strip())
                self.assertTrue(route_path.startswith("/"), f"Route path must be absolute: {route_path}")
                response = self.client.get(route_path)
                self.assertEqual(response.status_code, 200, f"{route_path} did not render successfully")
                content = response.text
                self.assertIn("/static/shared/base.css", content, f"{route_path} must keep the shared shell stylesheet")
                self.assertIn("/static/preview_bridge.js", content, f"{route_path} must keep the preview bridge runtime")
                self.assertIn("page-shell", content, f"{route_path} must render the shared page-shell root")
                self.assertIn("padding-top: max(76px", content, f"{route_path} must preserve the 76px shell safe-area baseline")

    def test_local_route_refs_resolve_inside_generated_route_manifest(self) -> None:
        declared_paths = {
            _normalize_route_ref(str(page.get("route_path") or ""))
            for role in ROLES
            for page in (((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or [])
            if isinstance(page, dict)
        }
        for role in ROLES:
            pages = ((self.route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                file_path = str(page.get("file_path") or "")
                if not file_path:
                    continue
                html = (MINIAPP_DIR / file_path.removeprefix("miniapp/")).read_text(encoding="utf-8")
                for route_ref in _extract_local_route_refs(html):
                    normalized = _normalize_route_ref(route_ref)
                    self.assertTrue(
                        any(_route_pattern_matches(candidate, normalized) for candidate in declared_paths),
                        f"{file_path} links to undeclared local route {route_ref}",
                    )
                    response = self.client.get(normalized)
                    self.assertLess(response.status_code, 400, f"{file_path} links to local route {route_ref} that does not resolve")

    def test_role_journey_round_trip_persists_shared_record(self) -> None:
        workflow_tokens = {"request", "requests", "record", "records", "order", "orders", "task", "tasks", "submission", "submissions", "item", "items"}
        create_requirement = _pick_workflow_api(self.workflow_api_requirements, methods={"POST"}, tokens_any=workflow_tokens)
        self.assertIsNotNone(create_requirement, "Generated app must expose a POST workflow API for creating persisted records.")
        preferred_resource = _resource_slug(str(create_requirement.get("path") or ""))
        list_requirement = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"GET"},
            tokens_any=workflow_tokens,
            preferred_resource=preferred_resource,
        )
        update_requirement = _pick_workflow_api(
            self.workflow_api_requirements,
            methods={"PUT", "PATCH"},
            tokens_any=workflow_tokens,
            preferred_resource=preferred_resource,
        )
        self.assertIsNotNone(list_requirement, "Generated app must expose a GET workflow API for reading persisted records.")
        self.assertIsNotNone(update_requirement, "Generated app must expose a PUT or PATCH workflow API for updating persisted records.")

        create_path = _sample_route_path(str(create_requirement.get("path") or ""))
        create_payload = _payload_for_api(create_requirement, create_path, str(create_requirement.get("method") or "POST")) or {}
        create_allowed_fields = {
            str(field.get("name") or "").strip()
            for field in create_requirement.get("fields") or []
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        }
        for key, value in {"title": "Generated request", "status": "new", "comment": "Created from generated app test"}.items():
            if key in create_allowed_fields and key not in create_payload:
                create_payload[key] = value
        create_response = _request_and_assert(self.client, str(create_requirement.get("method") or "POST"), create_path, create_payload)
        self.assertLess(create_response.status_code, 400, f"Create API failed: {create_path} -> {create_response.status_code}")
        created_payload = _response_json(create_response)
        created_id = _extract_record_id(created_payload)
        self.assertTrue(created_id, f"Create API {create_path} must return a persisted record id. Payload: {created_payload}")

        list_path = _sample_route_path(str(list_requirement.get("path") or ""))
        list_response = _request_and_assert(self.client, "GET", list_path)
        self.assertLess(list_response.status_code, 400, f"List API failed: {list_path} -> {list_response.status_code}")
        listed_payload = _response_json(list_response)
        self.assertTrue(_payload_contains_value(listed_payload, created_id), f"List API {list_path} did not expose the created record id {created_id}. Payload: {listed_payload}")

        update_path = _resolve_path_params(str(update_requirement.get("path") or ""), {"id": created_id, "request_id": created_id, "record_id": created_id, "item_id": created_id})
        update_payload = _payload_for_api(update_requirement, update_path, str(update_requirement.get("method") or "PATCH"), created_id=created_id) or {}
        update_allowed_fields = {
            str(field.get("name") or "").strip()
            for field in update_requirement.get("fields") or []
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        }
        for key, value in {"status": "in_progress", "comment": "Updated by specialist"}.items():
            if (not update_allowed_fields and key == "status") or key in update_allowed_fields:
                update_payload[key] = value
        update_response = _request_and_assert(self.client, str(update_requirement.get("method") or "PATCH"), update_path, update_payload)
        self.assertLess(update_response.status_code, 400, f"Update API failed: {update_path} -> {update_response.status_code}")
        updated_payload = _response_json(update_response)
        self.assertTrue(
            _payload_contains_value(updated_payload, created_id) or _contains_status(updated_payload, "in_progress"),
            f"Update API {update_path} did not acknowledge the persisted record update. Payload: {updated_payload}",
        )

        post_update_list_response = _request_and_assert(self.client, "GET", list_path)
        self.assertLess(post_update_list_response.status_code, 400, f"Post-update list API failed: {list_path} -> {post_update_list_response.status_code}")
        post_update_payload = _response_json(post_update_list_response)
        self.assertTrue(_payload_contains_value(post_update_payload, created_id), f"Updated record {created_id} disappeared from list API {list_path}. Payload: {post_update_payload}")
        self.assertTrue(
            _contains_status(post_update_payload, "in_progress") or _payload_contains_value(post_update_payload, "Updated by specialist"),
            f"Updated record {created_id} did not reflect specialist changes in shared state. Payload: {post_update_payload}",
        )

    def test_backend_route_modules_import(self) -> None:
        for file_path in EXPECTED_BACKEND_TARGETS:
            if not file_path.startswith("miniapp/app/routes/") or not file_path.endswith(".py"):
                continue
            module_name = file_path.removeprefix("miniapp/").removesuffix(".py").replace("/", ".")
            importlib.import_module(module_name)


if __name__ == "__main__":
    unittest.main()
'''
        return template.replace("__ROLES_LITERAL__", roles_literal).replace("__BACKEND_TARGETS_LITERAL__", backend_targets_literal)
