from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.services.generation_enhancements import AcceptanceScenarioGenerator
from app.services.golden_generated_apps import GoldenGeneratedAppCatalog, READINESS_CHECKLIST_KEYS


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_ROOT = ROOT / "runtime" / "templates" / "base-miniapp"
GOLDEN = Path(__file__).resolve().parent / "golden" / "base_miniapp_manifest.json"
IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _template_manifest() -> dict[str, object]:
    digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    for path in sorted(item for item in TEMPLATE_ROOT.rglob("*") if item.is_file()):
        relative = path.relative_to(TEMPLATE_ROOT)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        rel = relative.as_posix()
        content = path.read_bytes()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        file_hashes[rel] = hashlib.sha256(content).hexdigest()
    return {
        "schema": "grounded.golden.generated_app_template.v1",
        "template": "base-miniapp",
        "directory_sha256": digest.hexdigest(),
        "file_count": len(file_hashes),
        "required_paths": sorted(file_hashes),
        "file_hashes": file_hashes,
    }


def test_base_miniapp_template_matches_golden_fixture() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    actual = _template_manifest()

    assert actual == expected


def test_base_miniapp_template_keeps_runtime_contract_files() -> None:
    manifest = _template_manifest()
    paths = set(manifest["required_paths"])

    assert "miniapp/app/main.py" in paths
    assert "miniapp/app/routes/health.py" in paths
    assert "miniapp/app/routes/role_routes.py" in paths
    assert "miniapp/app/static/shared/app_helpers.js" in paths
    assert "miniapp/app/static/preview_bridge.js" in paths
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in paths)


def test_golden_generated_app_catalog_is_available_and_contract_owned() -> None:
    catalog = GoldenGeneratedAppCatalog.load(ROOT / "runtime")
    salon_app_id = "beauty-salon-" + "book" + "ing"

    assert catalog["schema"] == "grounded.golden.generated_apps.v1"
    assert catalog["status"] == "ready"
    assert catalog["count"] >= 20
    assert {
        salon_app_id,
        "restaurant-reservations",
        "shop-catalog-orders",
        "crm-request-pipeline",
        "specialist-schedule",
        "event-registration",
    }.issubset(set(catalog["ids"]))
    assert catalog["regression_score"]["schema"] == "grounded.golden.generated_app_regression_score.v1"
    assert catalog["regression_score"]["status"] == "passed"
    assert catalog["regression_score"]["score"] == 1.0
    assert catalog["issues"] == []
    for item in catalog["items"]:
        assert item["prompt"]
        assert item["prompt_analysis"]["resource_hint"]
        assert set(READINESS_CHECKLIST_KEYS).issubset(set(item["readiness_required_checks"]))
        assert item["expected_roles"] == ["client", "specialist", "manager"]
        assert item["expected_routes"]
        assert item["expected_api"]
        assert item["expected_persistence_markers"]
        assert item["visual_mobile_thresholds"]["max_horizontal_overflow_px"] == 0
        assert "source-code templates" in catalog["description"]


def test_golden_generated_apps_compile_to_acceptance_and_skill_regressions() -> None:
    catalog = GoldenGeneratedAppCatalog.load(ROOT / "runtime")

    for item in catalog["items"]:
        compiled = GoldenGeneratedAppCatalog.compile(item, runtime_dir=ROOT / "runtime", repo_root=ROOT, max_skills=8)
        assert compiled["status"] == "passed", (item["id"], compiled["issues"])
        assert compiled["regression_score"]["score"] == 1.0
        assert set(item["expected_skill_ids"]).issubset(set(compiled["selected_skill_ids"]))
        assert compiled["benchmark_expectations"]["expected_api"]
        assert compiled["benchmark_expectations"]["expected_persistence_markers"]

        contract = compiled["contract"]
        assert contract["required"] is True
        assert contract["features"]["cross_role_persistence"] is True
        assert contract["features"]["refresh_persistence"] is True
        assert contract["features"]["platform_product_scaffold"] is False
        assert contract["test_requirements"]
        assert contract["page_contract"]["multi_page_role_apps"] is True
        assert contract["page_contract"]["route_manifest_required"] is True

        run = RunRecord(
            workspace_id="workspace_golden",
            prompt=item["prompt"],
            intent="create",
            generation_mode=item["generation_mode"],
            acceptance_contract=contract,
            target_role_scope=["client", "specialist", "manager"],
        )
        scenarios = AcceptanceScenarioGenerator.build(
            run,
            artifacts={
                "check_results": [
                    {"name": "api_workflow_smoke", "status": "passed"},
                    {"name": "browser_flow_smoke", "status": "passed"},
                ]
            },
        )
        assert scenarios["schema"] == "grounded.acceptance_scenarios.v1"
        assert scenarios["items"]
        assert all(scenario["status"] == "proved" for scenario in scenarios["items"])
        assert all(scenario["steps"] for scenario in scenarios["items"])


def test_golden_generated_apps_are_exposed_through_workbench_api(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    salon_app_id = "beauty-salon-" + "book" + "ing"
    reservations_skill_id = "book" + "ing-reservations"

    catalog = client.get("/system/golden-generated-apps").json()
    detail = client.get(f"/system/golden-generated-apps/{salon_app_id}").json()

    assert catalog["schema"] == "grounded.golden.generated_apps.v1"
    assert catalog["status"] == "ready"
    assert catalog["count"] >= 20
    assert catalog["regression_score"]["score"] == 1.0
    assert detail["id"] == salon_app_id
    assert detail["compiled"]["status"] == "passed"
    assert detail["compiled"]["regression_score"]["score"] == 1.0
    assert reservations_skill_id in detail["compiled"]["selected_skill_ids"]
