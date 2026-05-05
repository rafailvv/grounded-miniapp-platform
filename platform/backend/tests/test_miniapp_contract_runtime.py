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
    assert ("GET", "/api/ledgers") in routes
    assert ("POST", "/api/ledgers") in routes
    assert ("PATCH", "/api/ledgers/{item_id}/status") in routes
    assert "miniapp/app/generated/route_manifest.json" in contract.allowed_file_graph.contract_owned_paths


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
    assert second == []
    assert snapshot.status == "passed"
    assert preloaded_issues == []
    assert preloaded_findings == []
    assert "POST /api/records" in snapshot.declared_routes
    assert "/client" in snapshot.manifest_routes


def test_contract_shell_copy_uses_product_labels_not_internal_status_or_workflow_copy(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай приложение для заявок на аренду оборудования.",
        intent="create",
        generation_mode=GenerationMode.QUALITY,
        prompt_analysis=_analysis("заявка"),
    )
    MiniAppContractMaterializer.materialize(source, contract)

    specialist_js = (source / "miniapp/app/static/specialist/app.js").read_text(encoding="utf-8")
    specialist_html = (source / "miniapp/app/static/specialist/index.html").read_text(encoding="utf-8")

    assert "Отметить обработку" in specialist_js
    assert "statusLabel(item.status)" in specialist_js
    assert 'ready: "Готово к согласованию"' in specialist_js
    assert 'rejected: "Отклонена"' in specialist_js
    manager_js = (source / "miniapp/app/static/manager/app.js").read_text(encoding="utf-8")
    manager_html = (source / "miniapp/app/static/manager/index.html").read_text(encoding="utf-8")
    assert 'const DETAIL_FIELD_LABELS = {' in manager_js
    assert '<option value="approved">Одобрена</option>' in manager_html
    assert '<option value="rejected">Отклонена</option>' in manager_html
    assert "Save progress" not in specialist_js
    assert "workflow entries" not in specialist_js
    assert "Specialist" not in specialist_html
    assert "workflow" not in specialist_html
    assert "Title" not in specialist_html
    assert "Note" not in specialist_html
    assert 'item.status || "new"' not in specialist_js


def test_role_operation_fields_are_visible_across_roles_when_prompt_has_no_field_hints(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай приложение для согласования рабочего процесса.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_analysis("заявка"),
    )
    MiniAppContractMaterializer.materialize(source, contract)

    client_js = (source / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")
    manager_html = (source / "miniapp/app/static/manager/index.html").read_text(encoding="utf-8")

    assert 'name="managerDecision"' in manager_html
    assert 'name="managerComment"' in manager_html
    assert '"recordName": "Название записи"' in client_js
    assert '"specialistDecision": "Решение специалиста"' in client_js
    assert '"managerDecision": "Решение менеджера"' in client_js
    assert "ROLE === \"manager\" ? FIELD_LABELS" not in client_js


def test_internal_status_hint_stays_dedicated_control_not_detail_field(tmp_path: Path) -> None:
    source = _template_source(tmp_path)
    analysis = _analysis("заявка")
    analysis["field_hints"] = ["название", "статус", "комментарий"]
    analysis["role_field_hints"] = {
        "client": ["название", "комментарий"],
        "specialist": ["решение", "статус"],
        "manager": ["статус", "комментарий"],
    }
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt="Создай приложение для согласования заявки.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=analysis,
    )
    MiniAppContractMaterializer.materialize(source, contract)

    client_js = (source / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")
    manager_html = (source / "miniapp/app/static/manager/index.html").read_text(encoding="utf-8")

    assert '"status": "статус"' not in client_js
    assert 'id="contract-status-field" name="status"' in manager_html
    assert "statusLabel(item.status)" in client_js


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
    (source / "miniapp/app/routes/generated_contract.py").write_text("# route drift\n", encoding="utf-8")

    snapshot = MiniAppRouteRegistry.snapshot(source, contract)

    assert snapshot.status == "drift"
    assert any(issue["code"] == "registry.missing_backend_route" for issue in snapshot.drift_issues)
    assert any(recipe.suggested_patch_target == "miniapp/app/routes/generated_contract.py" for recipe in snapshot.repair_recipes)


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
