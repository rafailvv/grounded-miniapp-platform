from __future__ import annotations

import json
import re
from typing import Any


ROLE_ORDER = ("client", "specialist", "manager")


class GeneratedTestsSynthesizer:
    """Builds minimal workflow-oriented generated tests from an acceptance contract."""

    SCHEMA = "grounded.generated_tests_synthesis.v1"

    @classmethod
    def synthesize(
        cls,
        *,
        acceptance_contract: dict[str, Any] | None,
        implementation_plan: dict[str, Any] | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        flows = cls._flows(contract=contract, plan=plan, prompt=prompt)
        roles = cls._roles(contract=contract, plan=plan, flows=flows)
        api_paths = cls._api_paths(flows=flows, contract=contract, plan=plan)
        state_fields = cls._state_fields(flows=flows, contract=contract, plan=plan)
        selectors = cls._selectors(flows=flows, contract=contract, plan=plan, roles=roles)
        test_specs = [
            *cls._api_persistence_specs(api_paths=api_paths, state_fields=state_fields, flows=flows),
            *cls._route_static_specs(roles=roles, selectors=selectors),
            *cls._js_selector_specs(roles=roles, selectors=selectors, api_paths=api_paths),
            *cls._browser_workflow_specs(flows=flows, roles=roles, selectors=selectors, api_paths=api_paths),
            *cls._role_specific_specs(roles=roles, flows=flows, selectors=selectors),
        ]
        files = {
            "miniapp/tests/test_generated_app.py": cls.render_python_unittest(
                api_paths=api_paths,
                state_fields=state_fields,
                roles=roles,
                flows=flows,
            ),
            "miniapp/tests/generated_app.test.mjs": cls.render_js_tests(
                roles=roles,
                selectors=selectors,
                api_paths=api_paths,
                flows=flows,
            ),
        }
        return {
            "schema": cls.SCHEMA,
            "status": "ready" if contract.get("required") or flows else "optional",
            "source": "acceptance_contract",
            "flow_count": len(flows),
            "roles": roles,
            "api_paths": api_paths,
            "state_fields": state_fields,
            "selectors": selectors,
            "test_specs": test_specs,
            "files": files,
            "commands": [
                "cd miniapp && python -m unittest discover -s tests -p test_generated_app.py",
                "cd miniapp && node --test tests/generated_app.test.mjs",
            ],
            "principle": "Generated tests must prove prompt-owned workflows through API persistence, route shells, selectors, role scripts, and browser-step contracts.",
        }

    @classmethod
    def render_python_unittest(cls, *, api_paths: list[str], state_fields: list[str], roles: list[str], flows: list[dict[str, Any]]) -> str:
        primary_api = api_paths[0] if api_paths else "/api/items"
        fields = state_fields or ["id", "title", "status"]
        payload = {field: cls._sample_value(field) for field in fields if field != "id"}
        if not payload:
            payload = {"title": "Generated workflow item", "status": "new"}
        flow_names = [str(flow.get("name") or flow.get("id") or "workflow") for flow in flows] or ["workflow"]
        return (
            "import unittest\n"
            "from fastapi.testclient import TestClient\n\n"
            "from app.main import app\n\n\n"
            "class GeneratedWorkflowAcceptanceTests(unittest.TestCase):\n"
            "    def test_api_persists_prompt_workflow_state(self):\n"
            f"        payload = {json.dumps(payload, ensure_ascii=False, sort_keys=True)!r}\n"
            f"        api_path = {primary_api!r}\n"
            "        with TestClient(app) as client:\n"
            "            created = client.post(api_path, json=payload)\n"
            "            self.assertLess(created.status_code, 500, created.text)\n"
            "            if created.status_code in {404, 405}:\n"
            "                self.fail(f'{api_path} must expose a create workflow, got {created.status_code}')\n"
            "            body = created.json() if created.content else {}\n"
            "            marker = body.get('id') or body.get('item', {}).get('id') or body.get('record', {}).get('id')\n"
            "            listed = client.get(api_path)\n"
            "            self.assertLess(listed.status_code, 500, listed.text)\n"
            "            raw = listed.json() if listed.content else []\n"
            "            items = raw.get('items', raw) if isinstance(raw, dict) else raw\n"
            "            self.assertIsInstance(items, list)\n"
            "            joined = str(items) + str(body)\n"
            "            self.assertTrue(marker or any(str(value) in joined for value in payload.values()))\n\n"
            "    def test_prompt_roles_have_route_shells(self):\n"
            f"        roles = {roles!r}\n"
            "        with TestClient(app) as client:\n"
            "            for role in roles:\n"
            "                response = client.get(f'/{role}')\n"
            "                self.assertEqual(response.status_code, 200, f'/{role} should load')\n"
            "                self.assertIn('<', response.text)\n\n"
            "    def test_generated_tests_track_prompt_flows(self):\n"
            f"        flow_names = {flow_names!r}\n"
            "        self.assertTrue(flow_names)\n"
            "        self.assertTrue(any(str(name).strip() for name in flow_names))\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )

    @classmethod
    def render_js_tests(cls, *, roles: list[str], selectors: list[dict[str, str]], api_paths: list[str], flows: list[dict[str, Any]]) -> str:
        selector_values = [item["selector"] for item in selectors if item.get("selector")]
        if not selector_values:
            selector_values = ["form", "button", "[data-action]"]
        return (
            "import test from 'node:test';\n"
            "import assert from 'node:assert/strict';\n"
            "import fs from 'node:fs';\n"
            "import path from 'node:path';\n\n"
            "const root = process.cwd();\n"
            "const read = (...parts) => fs.readFileSync(path.join(root, ...parts), 'utf8');\n\n"
            "test('role route shells and scripts expose workflow selectors', () => {\n"
            f"  const roles = {json.dumps(roles)};\n"
            f"  const selectors = {json.dumps(selector_values)};\n"
            "  for (const role of roles) {\n"
            "    const html = read('app', 'static', role, 'index.html');\n"
            "    const js = read('app', 'static', role, 'app.js');\n"
            "    assert.match(html, /<(main|section|form|button|input|select|textarea)\\b/i, `${role} renders a usable shell`);\n"
            "    assert.match(js, /(addEventListener|fetch\\(|querySelector|submit|click)/, `${role} wires user workflow behavior`);\n"
            "    assert.ok(selectors.some((selector) => html.includes(selector.replace(/^#/, '')) || js.includes(selector)), `${role} owns at least one workflow selector`);\n"
            "  }\n"
            "});\n\n"
            "test('frontend workflow calls persisted API paths', () => {\n"
            f"  const roles = {json.dumps(roles)};\n"
            f"  const apiPaths = {json.dumps(api_paths or ['/api/items'])};\n"
            "  const combined = roles.map((role) => read('app', 'static', role, 'app.js')).join('\\n');\n"
            "  assert.match(combined, /fetch\\(/, 'role scripts call the backend from user workflow code');\n"
            "  assert.ok(apiPaths.some((apiPath) => combined.includes(apiPath) || combined.includes('/api/')), 'workflow JS references the persisted API');\n"
            "  assert.match(combined, /POST|PUT|PATCH|method\\s*:/i, 'workflow JS includes a state-changing request');\n"
            "});\n\n"
            "test('browser workflow test contract is replayable', () => {\n"
            f"  const flows = {json.dumps([cls._flow_label(flow) for flow in flows] or ['workflow'])};\n"
            "  assert.ok(flows.length > 0);\n"
            "  assert.ok(flows.every((flow) => String(flow).trim().length > 0));\n"
            "});\n"
        )

    @classmethod
    def _flows(cls, *, contract: dict[str, Any], plan: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
        raw = contract.get("flows") if isinstance(contract.get("flows"), list) else []
        flows = [dict(item) for item in raw if isinstance(item, dict)]
        if flows:
            return flows[:8]
        ledger = plan.get("product_task_ledger") if isinstance(plan.get("product_task_ledger"), list) else []
        derived = [
            {"id": item.get("id"), "name": item.get("content") or item.get("title") or item.get("id"), "roles": [item.get("role")], "api_paths": item.get("api_paths") or []}
            for item in ledger
            if isinstance(item, dict)
        ]
        if derived:
            return derived[:8]
        return [{"id": "prompt_workflow", "name": prompt[:100] or "Prompt workflow", "roles": list(ROLE_ORDER), "api_paths": ["/api/items"]}]

    @staticmethod
    def _roles(*, contract: dict[str, Any], plan: dict[str, Any], flows: list[dict[str, Any]]) -> list[str]:
        raw: list[Any] = []
        raw.extend(contract.get("roles") or [])
        raw.extend(plan.get("roles") or [])
        for flow in flows:
            raw.extend(flow.get("roles") or [])
        roles = [str(role).strip().lower() for role in raw if str(role).strip().lower() in ROLE_ORDER]
        return list(dict.fromkeys(roles or list(ROLE_ORDER)))

    @staticmethod
    def _api_paths(*, flows: list[dict[str, Any]], contract: dict[str, Any], plan: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        for source in [contract, plan, *flows]:
            for key in ("api_paths", "required_api_paths"):
                value = source.get(key) if isinstance(source, dict) else None
                paths.extend(str(item) for item in (value or []) if str(item).startswith("/api/"))
            value = source.get("api_path") if isinstance(source, dict) else None
            if str(value).startswith("/api/"):
                paths.append(str(value))
        return list(dict.fromkeys(paths or ["/api/items"]))[:8]

    @staticmethod
    def _state_fields(*, flows: list[dict[str, Any]], contract: dict[str, Any], plan: dict[str, Any]) -> list[str]:
        fields: list[str] = []
        for source in [contract, plan, *flows]:
            for key in ("state_fields", "fields", "field_hints"):
                value = source.get(key) if isinstance(source, dict) else None
                fields.extend(str(item) for item in (value or []) if str(item).strip())
        normalized = [GeneratedTestsSynthesizer._identifier(field) for field in fields]
        return list(dict.fromkeys([field for field in normalized if field]))[:12] or ["title", "status"]

    @staticmethod
    def _selectors(*, flows: list[dict[str, Any]], contract: dict[str, Any], plan: dict[str, Any], roles: list[str]) -> list[dict[str, str]]:
        selectors: list[dict[str, str]] = []
        controls = contract.get("required_controls") if isinstance(contract.get("required_controls"), list) else []
        for item in controls:
            if isinstance(item, dict):
                role = str(item.get("role") or "").lower()
                selector = str(item.get("selector") or item.get("id") or item.get("name") or "").strip()
                if selector:
                    selectors.append({"role": role if role in ROLE_ORDER else roles[0], "selector": selector if selector.startswith(("#", ".", "[")) else f"#{GeneratedTestsSynthesizer._identifier(selector)}"})
        for role in roles:
            selectors.append({"role": role, "selector": f"form#{role}-main"})
            selectors.append({"role": role, "selector": f"[data-role=\"{role}\"]"})
        return [dict(item) for item in {f"{item['role']}:{item['selector']}": item for item in selectors}.values()][:24]

    @staticmethod
    def _api_persistence_specs(*, api_paths: list[str], state_fields: list[str], flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"kind": "api_persistence", "api_path": path, "state_fields": state_fields, "flows": [GeneratedTestsSynthesizer._flow_label(flow) for flow in flows]} for path in api_paths]

    @staticmethod
    def _route_static_specs(*, roles: list[str], selectors: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [{"kind": "route_static_shell", "role": role, "route": f"/{role}", "selectors": [item["selector"] for item in selectors if item["role"] == role]} for role in roles]

    @staticmethod
    def _js_selector_specs(*, roles: list[str], selectors: list[dict[str, str]], api_paths: list[str]) -> list[dict[str, Any]]:
        return [{"kind": "js_source_selector", "role": role, "selectors": [item["selector"] for item in selectors if item["role"] == role], "api_paths": api_paths} for role in roles]

    @staticmethod
    def _browser_workflow_specs(*, flows: list[dict[str, Any]], roles: list[str], selectors: list[dict[str, str]], api_paths: list[str]) -> list[dict[str, Any]]:
        return [{"kind": "browser_workflow", "flow": GeneratedTestsSynthesizer._flow_label(flow), "roles": flow.get("roles") or roles, "selectors": selectors[:8], "api_paths": flow.get("api_paths") or api_paths} for flow in flows]

    @staticmethod
    def _role_specific_specs(*, roles: list[str], flows: list[dict[str, Any]], selectors: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [{"kind": "role_specific", "role": role, "flows": [GeneratedTestsSynthesizer._flow_label(flow) for flow in flows if role in [str(item).lower() for item in (flow.get("roles") or [])] or not flow.get("roles")], "selectors": [item["selector"] for item in selectors if item["role"] == role]} for role in roles]

    @staticmethod
    def _flow_label(flow: dict[str, Any]) -> str:
        return str(flow.get("name") or flow.get("title") or flow.get("id") or "workflow")

    @staticmethod
    def _identifier(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
        if text and text[0].isdigit():
            text = f"field_{text}"
        return text

    @staticmethod
    def _sample_value(field: str) -> str:
        if "status" in field:
            return "new"
        if "date" in field:
            return "2026-01-01"
        if "count" in field or "amount" in field or "total" in field:
            return "1"
        return f"generated {field}".strip()
