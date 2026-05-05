from __future__ import annotations

import shutil
from pathlib import Path

from app.models.common import GenerationMode
from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.services.check_runner import CheckRunner
from app.services.miniapp_contract import (
    MiniAppContractCompiler,
    MiniAppContractMaterializer,
    MiniAppRouteRegistry,
)


def _template_source(tmp_path: Path) -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    shutil.copytree(repo_root / "runtime/templates/base-miniapp", source)
    return source


def _analysis(resource: str) -> dict:
    return {
        "resource_hint": resource,
        "field_hints": [],
        "role_field_hints": {"client": [], "specialist": [], "manager": []},
        "role_action_prompts": {"client": [], "specialist": [], "manager": []},
    }


def test_fast_contract_compiler_creates_deterministic_resource_and_routes() -> None:
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Create a ledger app for client specialist manager",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_analysis("ledger"),
    )

    routes = {(endpoint.method, endpoint.path) for endpoint in contract.endpoints}

    assert contract.version == "grounded.miniapp.contract.v1"
    assert routes == set()
    assert contract.resources[0].slug == "ledgers"
    assert "miniapp/app/generated/route_manifest.json" in contract.allowed_file_graph.contract_owned_paths
    assert all("miniapp/app/routes/" not in path for path in contract.allowed_file_graph.contract_owned_paths)
    assert "miniapp/tests/test_generated_app.py" not in contract.allowed_file_graph.blocked_globs


def test_materializer_is_idempotent_and_registry_passes(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Create a record app",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_analysis("record"),
    )

    first = MiniAppContractMaterializer.materialize(source, contract)
    second = MiniAppContractMaterializer.materialize(source, contract)
    snapshot = MiniAppRouteRegistry.snapshot(source, contract)
    preloaded_issues, preloaded_findings = CheckRunner._preloaded_domain_data_issues(source)

    assert "miniapp/app/generated/miniapp_contract.json" in first
    assert all("miniapp/app/routes/" not in path for path in first)
    assert "miniapp/tests/test_generated_app.py" not in first
    assert second == []
    assert snapshot.status == "passed"
    assert preloaded_issues == []
    assert preloaded_findings == []
    assert "/client" in snapshot.manifest_routes


def test_contract_materializer_does_not_write_product_role_shell(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    original_client_html = (source / "miniapp/app/static/client/index.html").read_text(encoding="utf-8")
    original_manager_html = (source / "miniapp/app/static/manager/index.html").read_text(encoding="utf-8")
    original_manager_js = (source / "miniapp/app/static/manager/app.js").read_text(encoding="utf-8")
    analysis = _analysis("товар")
    analysis["role_field_hints"] = {
        "client": [],
        "specialist": [],
        "manager": ["название товара", "цена", "остаток"],
    }
    analysis["role_action_prompts"] = {
        "client": ["смотрит каталог"],
        "specialist": [],
        "manager": ["выкладывает товары"],
    }
    analysis["role_state_contract"] = {
        "source_roles": ["manager"],
        "update_roles": ["manager"],
        "observer_roles": ["client", "specialist"],
        "status_values": ["опубликован", "скрыт"],
    }
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай интернет-магазин: менеджер сам выкладывает товары, клиент смотрит каталог.",
        intent="create",
        generation_mode=GenerationMode.QUALITY,
        prompt_analysis=analysis,
    )
    MiniAppContractMaterializer.materialize(source, contract)

    client_html = (source / "miniapp/app/static/client/index.html").read_text(encoding="utf-8")
    manager_js = (source / "miniapp/app/static/manager/app.js").read_text(encoding="utf-8")
    manager_html = (source / "miniapp/app/static/manager/index.html").read_text(encoding="utf-8")

    assert contract.resources[0].source_roles == ["manager"]
    assert client_html == original_client_html
    assert manager_html == original_manager_html
    assert manager_js == original_manager_js
    assert "nazvanieTovara" not in manager_html
    assert "выкладывает товары" not in manager_html
    assert "DETAIL_FIELD_LABELS" not in manager_js
    assert "Одобрена" not in manager_html
    assert "Отклонена" not in manager_html
    assert "Контроль шаблонных записей" not in manager_html


def test_contract_compiler_does_not_invent_resource_when_llm_analysis_has_none() -> None:
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай полезное мобильное приложение.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis={**_analysis(""), "resource_hint": None},
    )

    assert contract.resources == []
    assert contract.endpoints == []


def test_status_hint_remains_prompt_owned_metadata_field(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    analysis = _analysis("документ")
    analysis["field_hints"] = ["название", "статус", "комментарий"]
    analysis["role_field_hints"] = {
        "client": ["название", "комментарий"],
        "specialist": ["решение", "статус"],
        "manager": ["статус", "комментарий"],
    }
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай приложение для согласования документа.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=analysis,
    )
    MiniAppContractMaterializer.materialize(source, contract)

    resource = contract.resources[0]

    assert "status" in resource.field_labels
    assert "status" in resource.role_field_labels["specialist"]
    assert "status" in resource.role_field_labels["manager"]


def test_registry_returns_repair_recipe_for_backend_contract_drift(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Create a task app",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_analysis("task"),
    )
    MiniAppContractMaterializer.materialize(source, contract)
    (source / "miniapp/app/static/client/app.js").write_text(
        'window.setupPreviewBridge?.("client");\nfetch("/api/tasks", { method: "GET" });\n',
        encoding="utf-8",
    )

    snapshot = MiniAppRouteRegistry.snapshot(source, contract)

    assert snapshot.status == "drift"
    assert any(issue["code"] == "registry.frontend_backend_drift" for issue in snapshot.drift_issues)
    assert any(recipe.suggested_patch_target == "miniapp/app/routes" for recipe in snapshot.repair_recipes)


def test_registry_sync_adds_filesystem_child_pages_to_route_manifest(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Create a ledger app",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_analysis("ledger"),
    )
    MiniAppContractMaterializer.materialize(source, contract)
    child_page = source / "miniapp/app/static/client/history/index.html"
    child_page.parent.mkdir(parents=True, exist_ok=True)
    child_page.write_text("<!doctype html><title>History</title>", encoding="utf-8")

    changed = MiniAppRouteRegistry.sync_contract_owned_files(source, contract)
    snapshot = MiniAppRouteRegistry.snapshot(source, contract, regenerated_files=changed)

    assert "miniapp/app/generated/route_manifest.json" in changed
    assert "/client/history" in snapshot.manifest_routes
    assert snapshot.status == "passed"


def test_contract_owned_files_are_blocked_from_llm_writes() -> None:
    invalid = AgentEditValidator._first_invalid_file_change(
        [
            DraftAction(
                file_path="miniapp/app/generated/route_manifest.json",
                operation="replace",
                content="{}",
                reason="model tried to rewrite generated contract output",
            )
        ]
    )

    assert invalid is not None
    assert "protected" in invalid[1] or "relative path" in invalid[1]
