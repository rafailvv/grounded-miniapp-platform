from __future__ import annotations

import json
import re
from typing import Any

from app.models.domain import DraftAction


class AcceptanceTestMaterializer:
    """Materializes browser replay proof scenarios as workspace-owned tests."""

    SCHEMA = "grounded.acceptance_tests.v1"
    TEST_RUNNER_PATH = "miniapp/tests/test_acceptance.py"
    ACCEPTANCE_DIR = "miniapp/tests/acceptance"

    @classmethod
    def materialize(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        replay_proof: dict[str, Any] | None,
        replay_source_ref: str | None = None,
    ) -> dict[str, Any]:
        proof = replay_proof if isinstance(replay_proof, dict) else {}
        scenarios = [dict(item) for item in proof.get("scenarios") or [] if isinstance(item, dict)]
        if not scenarios:
            return {
                "schema": cls.SCHEMA,
                "workspace_id": workspace_id,
                "run_id": run_id,
                "status": "empty",
                "acceptance_tests_ref": cls.ref(workspace_id, run_id),
                "acceptance_test_files": [],
                "acceptance_replay_source_ref": replay_source_ref,
                "file_changes": [],
            }

        used: set[str] = set()
        scenario_entries: list[dict[str, Any]] = []
        file_changes: list[DraftAction] = []
        for index, scenario in enumerate(scenarios, start=1):
            scenario_id = cls._unique_slug(str(scenario.get("scenario_id") or f"scenario_{index}"), used=used, fallback=f"scenario_{index}")
            path = f"{cls.ACCEPTANCE_DIR}/{scenario_id}.spec.ts"
            metadata = cls._scenario_metadata(scenario, scenario_id=scenario_id)
            scenario_entries.append({**metadata, "path": path})
            file_changes.append(
                DraftAction(
                    file_path=path,
                    operation="replace",
                    content=cls.render_typescript_spec(scenario=scenario, metadata=metadata, workspace_id=workspace_id, run_id=run_id),
                    reason="Materialize browser replay proof as a project acceptance Playwright spec.",
                )
            )

        runner = cls.render_python_runner(scenarios=scenario_entries)
        file_changes.insert(
            0,
            DraftAction(
                file_path=cls.TEST_RUNNER_PATH,
                operation="replace",
                content=runner,
                reason="Materialize replayable acceptance test runner for browser proof scenarios.",
            ),
        )
        files = [change.file_path for change in file_changes]
        return {
            "schema": cls.SCHEMA,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "ready",
            "acceptance_tests_ref": cls.ref(workspace_id, run_id),
            "acceptance_test_files": files,
            "acceptance_replay_source_ref": replay_source_ref,
            "scenario_count": len(scenario_entries),
            "scenarios": scenario_entries,
            "commands": ["cd miniapp && python -m unittest discover -s tests -p test_acceptance.py"],
            "file_changes": file_changes,
        }

    @staticmethod
    def ref(workspace_id: str, run_id: str) -> str:
        return f"acceptance_tests:{workspace_id}:{run_id}"

    @classmethod
    def render_typescript_spec(cls, *, scenario: dict[str, Any], metadata: dict[str, Any], workspace_id: str, run_id: str) -> str:
        source = str(scenario.get("playwright_spec") or "").strip()
        if not source:
            source = cls._fallback_typescript_spec(metadata)
        header = [
            "// Generated from browser replay proof. Keep this file with the product workspace.",
            f"// source-run: {run_id}",
            f"// workspace: {workspace_id}",
            f"// acceptance-scenario: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}",
            "// replay: npx playwright test tests/acceptance",
            "",
        ]
        return "\n".join(header) + source.rstrip() + "\n"

    @staticmethod
    def render_python_runner(*, scenarios: list[dict[str, Any]]) -> str:
        manifest = repr(scenarios)
        return f'''from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent
ACCEPTANCE_DIR = ROOT / "acceptance"
MANIFEST = {manifest}


class ReplayableAcceptanceTests(unittest.TestCase):
    def test_acceptance_specs_are_materialized(self):
        self.assertTrue(ACCEPTANCE_DIR.exists(), "tests/acceptance directory must exist")
        specs = sorted(ACCEPTANCE_DIR.glob("*.spec.ts"))
        self.assertTrue(specs, "at least one replayable acceptance spec is required")
        manifest_paths = {{item["path"] for item in MANIFEST}}
        actual_paths = {{f"miniapp/tests/acceptance/{{path.name}}" for path in specs}}
        self.assertTrue(manifest_paths.issubset(actual_paths), f"missing specs: {{sorted(manifest_paths - actual_paths)}}")

    def test_acceptance_scenarios_have_replay_contracts(self):
        for item in MANIFEST:
            with self.subTest(scenario=item.get("scenario_id")):
                self.assertTrue(str(item.get("scenario_id") or "").strip())
                self.assertTrue(str(item.get("route") or "").startswith("/"), item)
                self.assertIsInstance(item.get("viewport"), dict)
                self.assertGreater(int(item.get("step_count") or 0), 0)
                selectors = item.get("selectors") or []
                actions = item.get("actions") or []
                self.assertTrue(selectors or actions, item)
                source = (ROOT.parent / item["path"].removeprefix("miniapp/")).read_text(encoding="utf-8")
                match = re.search(r"^// acceptance-scenario: (.+)$", source, flags=re.MULTILINE)
                self.assertIsNotNone(match, "spec must carry replay metadata")
                embedded = json.loads(match.group(1))
                self.assertEqual(embedded["scenario_id"], item["scenario_id"])

    def test_optional_live_replay_when_base_url_is_configured(self):
        base_url = os.getenv("ACCEPTANCE_BASE_URL", "").rstrip("/")
        if not base_url:
            self.skipTest("ACCEPTANCE_BASE_URL is not set; static replay validation already passed.")
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.skipTest(f"Playwright is unavailable: {{exc}}")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for item in MANIFEST:
                    viewport = item.get("viewport") or {{"width": 390, "height": 844}}
                    page = browser.new_page(viewport={{"width": int(viewport.get("width") or 390), "height": int(viewport.get("height") or 844)}})
                    page.goto(base_url + str(item.get("route") or "/"))
                    for selector in item.get("selectors") or []:
                        page.locator(selector).first.wait_for(state="visible", timeout=5000)
                    page.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
'''

    @staticmethod
    def _scenario_metadata(scenario: dict[str, Any], *, scenario_id: str) -> dict[str, Any]:
        steps = [dict(item) for item in scenario.get("steps") or [] if isinstance(item, dict)]
        route = str(scenario.get("route") or (steps[0].get("route") if steps else "") or "/")
        if not route.startswith("/"):
            route = f"/{route}"
        selectors = [str(step.get("selector")) for step in steps if str(step.get("selector") or "").strip()]
        actions = [str(step.get("action")) for step in steps if str(step.get("action") or "").strip()]
        viewport = scenario.get("viewport") if isinstance(scenario.get("viewport"), dict) else {}
        return {
            "scenario_id": scenario_id,
            "status": str(scenario.get("status") or "unknown"),
            "role": scenario.get("role"),
            "route": route,
            "viewport": viewport or {"width": 390, "height": 844},
            "step_count": len(steps),
            "selectors": list(dict.fromkeys(selectors)),
            "actions": list(dict.fromkeys(actions)),
        }

    @staticmethod
    def _fallback_typescript_spec(metadata: dict[str, Any]) -> str:
        route = json.dumps(str(metadata.get("route") or "/"))
        selector = json.dumps(str((metadata.get("selectors") or ["body"])[0]))
        scenario_id = str(metadata.get("scenario_id") or "acceptance")
        return "\n".join(
            [
                "import { test, expect } from '@playwright/test';",
                "",
                f"test('replay {scenario_id}', async ({{ page }}) => {{",
                f"  await page.goto({route});",
                f"  await expect(page.locator({selector}).first()).toBeVisible();",
                "});",
                "",
            ]
        )

    @staticmethod
    def _unique_slug(value: str, *, used: set[str], fallback: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_").lower() or fallback
        candidate = base
        index = 2
        while candidate in used:
            candidate = f"{base}_{index}"
            index += 1
        used.add(candidate)
        return candidate
