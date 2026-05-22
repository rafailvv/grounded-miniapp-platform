from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.ai.openai_client import OpenAIClient
from app.ai.model_registry import CODEX_MINI_MODEL, models_for_role
from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord, CreateRunRequest, DraftAction, RunCheckResult, WorkspaceRecord, utc_now
from app.models.hooks import HookContext
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_command_policy import DEFAULT_COMMAND_POLICY, AgentCommandPolicy, decide_workspace_command
from app.modules.miniapp_agent_loop.agent_coordinator import AgentCoordinator
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_kernel import agent_tool_kind, compact_agent_memory, plan_agent_tool_batches
from app.modules.miniapp_agent_loop.agent_memory_store import AgentMemoryStore
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager, HeadTailOutputBuffer
from app.modules.miniapp_agent_loop.agent_scratchpad import AgentScratchpad
from app.modules.miniapp_agent_loop.agent_tool_call_loop import AgentToolCallLoop
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.agent_tool_changes import file_changes_from_mutating_tool_calls
from app.modules.miniapp_agent_loop.tool_router import ToolRouter, ToolRouterContext
from app.modules.miniapp_agent_loop.tool_orchestrator import ToolOrchestrationRequest, ToolOrchestrator
from app.modules.miniapp_agent_loop.diagnostics_delta import AgentDiagnosticsDelta
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.agent_worker_branch_loop import AgentWorkerBranchLoop
from app.modules.miniapp_agent_loop.agent_worker_runtime import AgentWorkerRuntime
from app.modules.miniapp_agent_loop.agent_worker_tasks import AgentWorkerTaskPlanner
from app.models.artifacts import ApplyPatchResult
from app.modules.miniapp_agent_loop.context_pressure import AgentContextPressureAnalyzer
from app.modules.miniapp_agent_loop.context_packer import AgentContextPacker
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.lsp_tools import LspToolService
from app.modules.miniapp_agent_loop.repair_packets import RepairTransitionPolicy
from app.modules.miniapp_agent_loop.rollout_trace import RolloutTraceRecorder
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.agent_tool_runtime import validate_workspace_command
from app.services.tool_protocol import TOOL_EXECUTION_DEFAULTS, TOOL_RISK_DEFAULTS, tool_registry_contract
from app.modules.miniapp_agent_loop.turn_diff_tracker import AgentTurnDiffTracker
from app.modules.miniapp_agent_loop.types import AgentTurnPlan
from app.modules.miniapp_agent_loop.guardian_review import GuardianReview
from app.modules.miniapp_agent_loop.verification_worker import VerificationWorker
from app.modules.workspace_code_agent_runtime.browser_replay import BrowserProofReplay
from app.modules.workspace_code_agent_runtime.check_orchestrator import WorkspaceAgentCheckOrchestrator
from app.modules.workspace_code_agent_runtime.process_recovery import AgentProcessRecovery
from app.modules.workspace_code_agent_runtime.prompt_contract import agent_system_prompt
from app.modules.workspace_code_agent_runtime.runtime import WorkspaceCodeAgentRuntime
from app.services.check_runner import CheckRunner
from app.services.hook_policy_service import HookPolicyService
from app.services.miniapp_contract import MiniAppContractCompiler
from app.services.repair_catalog import RepairCatalog
from app.services.workspace.run_service import RunService
from app.services.workflow_acceptance import build_acceptance_contract, build_implementation_plan, extract_prompt_planning_hints
from app.services.generated_tests_synthesizer import GeneratedTestsSynthesizer
from app.repositories.state_store import StateStore


def _prompt_analysis(
    *,
    resource: str = "ресурс",
    client: list[str] | None = None,
    specialist: list[str] | None = None,
    manager: list[str] | None = None,
    screen_plan: dict | None = None,
) -> dict:
    client_fields = client or []
    specialist_fields = specialist or []
    manager_fields = manager or []
    return {
        "prompt_summary": "structured test analysis",
        "resource_hint": resource,
        "field_hints": [*client_fields, *specialist_fields, *manager_fields],
        "role_field_hints": {
            "client": client_fields,
            "specialist": specialist_fields,
            "manager": manager_fields,
        },
        "role_action_prompts": {
            "client": ["; ".join(client_fields)] if client_fields else [],
            "specialist": ["; ".join(specialist_fields)] if specialist_fields else [],
            "manager": ["; ".join(manager_fields)] if manager_fields else [],
        },
        "routeable_screen_plan": screen_plan or {},
    }


def test_browser_infra_failure_does_not_mask_repairable_app_failures() -> None:
    results = [
        RunCheckResult(
            name="generated_app_python_tests",
            status="failed",
            details="Generated tests failed.",
            logs=["AssertionError"],
        ),
        RunCheckResult(
            name="browser_flow_smoke",
            status="failed",
            details="Browser proof failed.",
            diagnostics={"infra_unavailable": True},
        ),
    ]

    assert AgentToolCallLoop._has_browser_infra_failure(results) is False


def test_compact_agent_memory_keeps_repair_outcomes_and_changed_files() -> None:
    memory = compact_agent_memory(
        turn_history=[
            {"outcome": "needs_context", "failure_signature": "platform.first"},
            {"outcome": "changes_ready", "result": "applied", "files_changed": ["miniapp/app/static/manager/app.js"]},
        ],
        file_change_count=1,
        last_assistant_message="patched manager",
    )

    assert memory["recent_outcomes"] == ["needs_context", "changes_ready"]
    assert memory["latest_changed_files"] == ["miniapp/app/static/manager/app.js"]
    assert memory["no_edit_turn_count"] == 1


def test_browser_infra_failure_blocks_when_no_repairable_failures_remain() -> None:
    results = [
        RunCheckResult(name="api_workflow_smoke", status="passed", details="API proof passed."),
        RunCheckResult(
            name="browser_flow_smoke",
            status="failed",
            details="Browser proof failed.",
            diagnostics={"infra_unavailable": True},
        ),
    ]

    assert AgentToolCallLoop._has_browser_infra_failure(results) is True


def test_generated_test_failures_gate_browser_preview_proof() -> None:
    assert CheckRunner._generated_tests_failed(
        RunCheckResult(name="generated_app_python_tests", status="failed", details="Python tests failed."),
        RunCheckResult(name="generated_app_js_tests", status="passed", details="JS tests passed."),
    )
    assert not CheckRunner._generated_tests_failed(
        RunCheckResult(name="generated_app_python_tests", status="passed", details="Python tests passed."),
        RunCheckResult(name="generated_app_js_tests", status="skipped", details="JS tests skipped."),
    )


def _contract_with_analysis(
    prompt: str,
    *,
    generation_mode: GenerationMode = GenerationMode.FAST,
    resource: str = "ресурс",
    client: list[str] | None = None,
    specialist: list[str] | None = None,
    manager: list[str] | None = None,
    screen_plan: dict | None = None,
) -> dict:
    return build_acceptance_contract(
        prompt=prompt,
        intent="create",
        generation_mode=generation_mode,
        prompt_analysis=_prompt_analysis(
            resource=resource,
            client=client,
            specialist=specialist,
            manager=manager,
            screen_plan=screen_plan,
        ),
    )


def test_code_agent_defaults_to_mini_for_all_generation_modes() -> None:
    for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY, GenerationMode.PRODUCTION):
        assert models_for_role("agent_turn", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("repair", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("summarize", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL


def test_quality_create_prompt_with_error_states_stays_quality_mode() -> None:
    service = object.__new__(RunService)
    workspace = WorkspaceRecord(name="test", path="/tmp/ws")
    request = CreateRunRequest(
        prompt="Создай приложение. Quality: добавь empty/loading/error states.",
        intent="create",
        generation_mode=GenerationMode.QUALITY,
    )

    assert service._resolve_generation_mode(workspace, request, "create") == GenerationMode.QUALITY


def test_balanced_quality_runs_require_acceptance_contract_with_prompt_analysis() -> None:
    contract = build_acceptance_contract(
        prompt="Improve the operations flow",
        intent="edit",
        generation_mode=GenerationMode.BALANCED,
        prompt_analysis=_prompt_analysis(resource="operations", client=["name"]),
    )

    assert contract["required"] is True
    assert contract["workflow_kind"] == "product_quality_run"
    assert contract["api_contract"]["resource_hint"] == "operations"
    assert contract["flows"][0]["steps"][0]["kind"] == "prompt_state_source"
    assert contract["flows"][0]["steps"][0]["entity"] == "operations"
    legacy_step_kind = "create" + "_or_update"
    assert not any(step.get("kind") == legacy_step_kind for flow in contract["flows"] for step in flow.get("steps", []))


def test_acceptance_contract_uses_role_actions_as_source_when_fields_are_absent() -> None:
    contract = build_acceptance_contract(
        prompt="Manager publishes the operating plan for everyone to read.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis={
            "prompt_summary": "manager-owned operating plan",
            "resource_hint": "operating plan",
            "field_hints": [],
            "role_field_hints": {"client": [], "specialist": [], "manager": []},
            "role_action_prompts": {"client": [], "specialist": [], "manager": ["publish operating plan"]},
        },
    )

    assert not contract.get("blocking")
    assert contract["flows"][0]["steps"][0]["role"] == "manager"
    assert contract["flows"][0]["steps"][0]["action"] == "publish operating plan"


def test_acceptance_contract_blocks_without_prompt_derived_resource() -> None:
    contract = build_acceptance_contract(
        prompt="Создай полезное мобильное приложение.",
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=_prompt_analysis(resource="", client=["название"]),
    )

    assert contract["status"] == "blocked_contract_missing"
    assert contract["blocking"] is True
    assert "missing_prompt_derived_resource" in contract["issues"]
    assert contract["flows"] == []
    assert contract["required_endpoints"] == []


def test_lsp_static_diagnostics_reports_structured_python_issue(tmp_path: Path) -> None:
    app_dir = tmp_path / "miniapp" / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    runner = object.__new__(CheckRunner)

    result = runner._lsp_static_diagnostics(source_dir=tmp_path)

    assert result.name == "lsp_static_diagnostics"
    assert result.status == "failed"
    assert result.diagnostics["items"][0]["source"] == "python_compile"
    assert result.diagnostics["items"][0]["file"] == "miniapp/app/main.py"


def test_lsp_static_diagnostics_checks_role_dom_ids(tmp_path: Path) -> None:
    static_dir = tmp_path / "miniapp" / "app" / "static" / "client"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main id='client-root'></main>\n", encoding="utf-8")
    (static_dir / "app.js").write_text(
        "document.querySelector('#missing-control')?.addEventListener('click', () => {});\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._lsp_static_diagnostics(source_dir=tmp_path)

    assert result.name == "lsp_static_diagnostics"
    assert result.status == "failed"
    assert any(
        item["source"] == "selector_static" and item["code"] == "missing_dom_id"
        for item in result.diagnostics["items"]
    )


def test_lsp_static_diagnostics_accepts_role_ids_rendered_from_js(tmp_path: Path) -> None:
    static_dir = tmp_path / "miniapp" / "app" / "static" / "manager"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main id='app'></main>\n", encoding="utf-8")
    (static_dir / "app.js").write_text(
        """
function renderPage() {
  return `<form id="manager-update-form"><button type="submit">Save</button></form>`;
}
document.getElementById("app").innerHTML = renderPage();
document.getElementById("manager-update-form")?.addEventListener("submit", () => {});
""",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._lsp_static_diagnostics(source_dir=tmp_path)

    assert result.name == "lsp_static_diagnostics"
    assert result.status == "passed"
    assert not [
        item
        for item in result.diagnostics["items"]
        if item.get("source") == "selector_static" and item.get("code") == "missing_dom_id"
    ]


def test_frontend_selector_wiring_accepts_role_markup_rendered_from_js(tmp_path: Path) -> None:
    js_path = tmp_path / "source" / "miniapp" / "app" / "static" / "manager" / "app.js"
    js_path.parent.mkdir(parents=True)
    js_source = """
function render() {
  const app = document.getElementById("app");
  app.innerHTML = `<section><div id="manager-list" class="items"></div></section>`;
  app.querySelector("#manager-list")?.addEventListener("click", () => {});
}
"""

    issues = CheckRunner._selector_wiring_issues(
        "manager",
        js_path,
        js_source,
        "<main id='app'></main>",
    )

    assert issues == []


def test_frontend_selector_wiring_flags_selector_missing_from_static_and_generated_markup(tmp_path: Path) -> None:
    js_path = tmp_path / "source" / "miniapp" / "app" / "static" / "manager" / "app.js"
    js_path.parent.mkdir(parents=True)
    js_source = """
function render() {
  const app = document.getElementById("app");
  app.innerHTML = `<section><div id="manager-list" class="items"></div></section>`;
  app.querySelector("#missing-manager-list")?.addEventListener("click", () => {});
}
"""

    issues = CheckRunner._selector_wiring_issues(
        "manager",
        js_path,
        js_source,
        "<main id='app'></main>",
    )

    assert [issue.code for issue in issues] == ["platform.workflow_selector_matches_no_html"]
    assert issues[0].blocking is True


def test_late_domcontentloaded_init_detects_js_rendered_controls(tmp_path: Path) -> None:
    js_path = tmp_path / "source" / "miniapp" / "app" / "static" / "specialist" / "app.js"
    js_path.parent.mkdir(parents=True)
    js_source = """
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("app").innerHTML = `<form id="specialist-form"><button type="submit">Save</button></form>`;
  document.getElementById("specialist-form")?.addEventListener("submit", () => {});
});
"""

    issues = CheckRunner._late_domcontentloaded_init_issues(
        "specialist",
        js_path,
        js_source,
        "<main id='app'></main>",
    )

    assert [issue.code for issue in issues] == ["platform.workflow_late_domcontentloaded_init"]
    assert issues[0].blocking is True


def test_runtime_database_artifacts_are_blocking_and_cleaned(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    app_dir = source_dir / "miniapp" / "app" / "generated"
    app_dir.mkdir(parents=True)
    db_path = app_dir / "app.db"
    wal_path = app_dir / "app.db-wal"
    db_path.write_bytes(b"sqlite-runtime-data")
    wal_path.write_bytes(b"sqlite-runtime-wal")

    issues = CheckRunner._runtime_database_artifact_issues(source_dir)
    removed = CheckRunner._cleanup_runtime_database_artifacts(source_dir)

    assert [issue.code for issue in issues] == [
        "platform.runtime_database_artifact",
        "platform.runtime_database_artifact",
    ]
    assert removed == ["miniapp/app/generated/app.db", "miniapp/app/generated/app.db-wal"]
    assert not db_path.exists()
    assert not wal_path.exists()


def test_js_rendered_form_blocks_visible_control_value_not_used(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    role_dir = source_dir / "miniapp" / "app" / "static" / "manager"
    role_dir.mkdir(parents=True)
    (role_dir / "index.html").write_text("<main id='app'></main>", encoding="utf-8")
    (role_dir / "app.js").write_text(
        """
const root = document.getElementById("app");
root.innerHTML = `<form id="manager-form">
  <input id="manager-selected" name="selected_note" required />
  <textarea id="manager-note"></textarea>
  <button type="submit">Save</button>
</form>`;
document.getElementById("manager-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await fetch("/api/items/1", { method: "PATCH", body: JSON.stringify({ selected_note: document.getElementById("manager-note").value }) });
});
""",
        encoding="utf-8",
    )

    issues = CheckRunner._frontend_role_wiring_issues(source_dir / "miniapp/app/static")

    assert "platform.workflow_form_field_value_not_used" in [issue.code for issue in issues]


def test_js_rendered_form_accepts_formdata_value_use(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    role_dir = source_dir / "miniapp" / "app" / "static" / "client"
    role_dir.mkdir(parents=True)
    (role_dir / "index.html").write_text("<main id='app'></main>", encoding="utf-8")
    (role_dir / "app.js").write_text(
        """
const root = document.getElementById("app");
root.innerHTML = `<form id="client-form">
  <input id="client-title" name="title" required />
  <button type="submit">Save</button>
</form>`;
document.getElementById("client-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  await fetch("/api/items", { method: "POST", body: JSON.stringify(payload) });
});
""",
        encoding="utf-8",
    )

    issues = CheckRunner._frontend_role_wiring_issues(source_dir / "miniapp/app/static")

    assert "platform.workflow_form_field_value_not_used" not in [issue.code for issue in issues]


def test_lsp_tool_service_reports_jumpable_changed_file_diagnostics(tmp_path: Path) -> None:
    app_dir = tmp_path / "miniapp" / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (app_dir / "ok.py").write_text("def ok():\n    return True\n", encoding="utf-8")

    report = LspToolService.diagnostics(
        root=tmp_path,
        changed_only=True,
        changed_files=["miniapp/app/main.py"],
        include_optional_tools=False,
    )

    assert report["tool"] == "lsp.diagnostics"
    assert report["status"] == "failed"
    assert report["items"][0]["path"] == "miniapp/app/main.py"
    assert report["items"][0]["jump"]["line"] >= 1


def test_lsp_tool_service_accepts_role_ids_rendered_from_js(tmp_path: Path) -> None:
    static_dir = tmp_path / "miniapp" / "app" / "static" / "specialist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main id='app'></main>\n", encoding="utf-8")
    (static_dir / "app.js").write_text(
        """
function render() {
  document.getElementById("app").innerHTML = `<form id="specialist-form"><select id="specialist-item-id"></select></form>`;
}
document.getElementById("specialist-form")?.addEventListener("submit", () => {});
document.getElementById("specialist-item-id")?.addEventListener("change", () => {});
""",
        encoding="utf-8",
    )

    report = LspToolService.diagnostics(
        root=tmp_path,
        targets=["miniapp/app/static/specialist/app.js"],
        include_optional_tools=False,
    )

    assert report["status"] == "passed"
    assert not [
        item
        for item in report["items"]
        if item.get("source") == "selector_static" and item.get("code") == "missing_dom_id"
    ]


def test_lsp_tool_service_symbol_reference_and_route_context(tmp_path: Path) -> None:
    routes = tmp_path / "miniapp" / "app" / "routes"
    client = tmp_path / "miniapp" / "app" / "static" / "client"
    routes.mkdir(parents=True)
    client.mkdir(parents=True)
    (routes / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/items')\n"
        "@router.get('')\n"
        "def list_items():\n"
        "    return []\n",
        encoding="utf-8",
    )
    (client / "app.js").write_text("function loadItems() { return fetch('/api/items'); }\nloadItems();\n", encoding="utf-8")

    symbols = LspToolService.symbol_context(root=tmp_path, query="list_items")
    refs = LspToolService.find_references(root=tmp_path, symbol="loadItems")
    definition = LspToolService.definition(root=tmp_path, symbol="list_items")
    route_context = LspToolService.route_static_context(root=tmp_path)
    route_graph = LspToolService.route_graph(root=tmp_path)

    assert symbols["items"][0]["name"] == "list_items"
    assert len(refs["items"]) == 2
    assert definition["schema"] == "grounded.lsp_definition.v1"
    assert definition["items"][0]["name"] == "list_items"
    assert "GET /api/items" in route_context["api_routes"]
    assert route_context["frontend_api_refs"][0]["declared"] is True
    assert route_graph["schema"] == "grounded.lsp_route_graph.v1"
    assert route_graph["summary"]["missing_edge_count"] == 0
    assert any(edge["status"] == "resolved" for edge in route_graph["edges"])


def test_lsp_tool_service_uses_real_tool_outputs_and_tsserver_protocol(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    app_dir = tmp_path / "miniapp" / "app"
    static_dir = app_dir / "static" / "client"
    bin_dir.mkdir()
    static_dir.mkdir(parents=True)
    (app_dir / "main.py").write_text("def ok():\n    return True\n", encoding="utf-8")
    (static_dir / "app.ts").write_text("const value: string = 1;\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"noEmit":true,"strict":true},"include":["miniapp/app/static/**/*.ts"]}\n', encoding="utf-8")

    def fake_tool(name: str, body: str) -> None:
        path = bin_dir / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)

    fake_tool(
        "ruff",
        "import json, sys\n"
        "print(json.dumps([{'filename':'miniapp/app/main.py','location':{'row':1,'column':5},'code':'F401','message':'unused import'}]))\n"
        "sys.exit(1)\n",
    )
    fake_tool(
        "pyright",
        "import json, pathlib, sys\n"
        "root = pathlib.Path.cwd()\n"
        "print(json.dumps({'generalDiagnostics':[{'file':str(root/'miniapp/app/main.py'),'range':{'start':{'line':0,'character':0}},'severity':'error','message':'unknown symbol','rule':'reportUnknownVariableType'}]}))\n"
        "sys.exit(1)\n",
    )
    fake_tool(
        "tsc",
        "import sys\n"
        "print('miniapp/app/static/client/app.ts(1,7): error TS2322: Type number is not assignable to string')\n"
        "sys.exit(2)\n",
    )
    fake_tool(
        "tsserver",
        "import json, sys\n"
        "payload = {'seq':1,'type':'response','command':'semanticDiagnosticsSync','request_seq':4,'success':True,'body':[{'start':{'line':1,'offset':7},'text':'Type mismatch','code':2322,'category':'error'}]}\n"
        "raw = json.dumps(payload).encode()\n"
        "sys.stdout.buffer.write(f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    report = LspToolService.diagnostics(root=tmp_path)
    sources = {item["source"] for item in report["items"]}

    assert {"ruff", "pyright", "tsc", "tsserver"}.issubset(sources)
    assert report["tool_status"]["ruff"]["status"] == "failed"
    assert report["tool_status"]["pyright"]["status"] == "failed"
    assert report["tool_status"]["tsc"]["status"] == "failed"
    assert report["tool_status"]["tsserver"]["status"] == "failed"


def test_lsp_tool_service_detects_api_method_mismatch_and_route_definitions(tmp_path: Path) -> None:
    routes = tmp_path / "miniapp" / "app" / "routes"
    client = tmp_path / "miniapp" / "app" / "static" / "client"
    routes.mkdir(parents=True)
    client.mkdir(parents=True)
    (routes / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api/items')\n"
        "@router.get('')\n"
        "def list_items():\n"
        "    return []\n",
        encoding="utf-8",
    )
    (client / "app.js").write_text("async function save() { return fetch('/api/items', { method: 'POST' }); }\n", encoding="utf-8")

    diagnostics = LspToolService.diagnostics(root=tmp_path, include_optional_tools=False)
    definition = LspToolService.definition(root=tmp_path, symbol="/api/items")
    refs = LspToolService.find_references(root=tmp_path, symbol="/api/items")
    route_graph = LspToolService.route_graph(root=tmp_path)

    assert any(item["source"] == "api_route_static" and item["code"] == "method_mismatch" for item in diagnostics["items"])
    assert definition["items"][0]["kind"] == "api_route"
    assert any(item["path"].endswith("client/app.js") for item in refs["items"])
    assert route_graph["summary"]["api_mismatch_count"] == 1
    assert route_graph["edges"][0]["status"] == "method_mismatch"


def test_lsp_agent_tools_are_model_visible_aliases_with_canonical_protocol() -> None:
    tool_names = {tool["name"] for tool in AgentToolRegistry.openai_tools()}

    assert "lsp_diagnostics" in tool_names
    assert "lsp_symbol_context" in tool_names
    assert "lsp_definition" in tool_names
    assert "lsp_find_references" in tool_names
    assert "lsp_route_graph" in tool_names
    assert "lsp_route_static_context" in tool_names
    assert AgentToolRegistry.kind("lsp.diagnostics") == "read_only"


def test_prompt_planning_hints_extract_role_fields_from_colon_and_action_sentences() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент описывает объект: название, локация, тип, количество, бюджет, срок, контакт и комментарий. "
        "Специалист видит объект, указывает вариант, доступность, стоимость, срок исполнения, альтернативу и рабочее состояние. "
        "Менеджер контролирует бюджет и срочность, назначает приоритет, фиксирует лимит или замену, оставляет управленческий комментарий.",
        prompt_analysis=_prompt_analysis(
            resource="объект",
            client=["название", "локация", "тип", "количество", "бюджет", "срок", "контакт", "комментарий"],
            specialist=["вариант", "доступность", "стоимость", "срок исполнения", "альтернативу", "рабочее состояние"],
            manager=["приоритет", "лимит", "управленческий комментарий"],
        ),
    )

    assert hints["resource_hint"] == "объект"
    assert hints["role_field_hints"]["client"][:4] == [
        "название",
        "локация",
        "тип",
        "количество",
    ]
    assert "вариант" in hints["role_field_hints"]["specialist"]
    assert "доступность" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "утверждает лимит или замену" not in hints["role_field_hints"]["client"]


def test_prompt_planning_hints_do_not_turn_workflow_actions_into_form_fields() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент описывает рабочий пакет: название, направление, тема, количество участников, бюджет, контакт. "
        "Специалист готовит рабочий пакет: план, ответственный, стоимость, доступные даты, рабочий статус. "
        "Менеджер: руководитель согласует: приоритет, лимит бюджета, итоговое решение. "
        "Нужно приложение с ролями /client, /specialist, /manager: создание карточки, список карточек, выбор карточки специалистом и менеджером, обновление состояния, сохранение данных после перезагрузки.",
        prompt_analysis=_prompt_analysis(
            resource="рабочий пакет",
            client=["название", "направление", "тема", "количество участников", "бюджет", "контакт"],
            specialist=["план", "ответственный", "стоимость", "доступные даты", "рабочий статус"],
            manager=["приоритет", "лимит бюджета", "итоговое решение"],
        ),
    )

    all_fields = [
        field
        for fields in hints["role_field_hints"].values()
        for field in fields
    ]

    assert "название" in hints["role_field_hints"]["client"]
    assert "план" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "название" not in hints["role_field_hints"]["manager"]
    assert "контакт" not in hints["role_field_hints"]["manager"]
    assert "создание карточки" not in all_fields
    assert "список карточек" not in all_fields
    assert "выбор карточки специалистом" not in all_fields
    assert "менеджером" not in all_fields


def test_prompt_planning_hints_skip_actor_action_and_mode_instruction_fields() -> None:
    hints = extract_prompt_planning_hints(
        "Клиент описывает рабочий пакет, указывает название, количество участников, тему, желаемые даты, формат, бюджет и комментарий. "
        "Специалист видит рабочий пакет, подбирает план, ответственного, длительность, стоимость, ресурсы и рабочий статус. "
        "Менеджер видит все карточки и результат специалиста, назначает приоритет, фиксирует лимит бюджета, выбирает финальные даты и оставляет управленческий комментарий. "
        "Balanced режим: сделай дизайн лучше fast, метрики для менеджера, состояния empty/loading/error/success. "
        "Техническую инструкцию про generated metadata не добавляй как поле.",
        prompt_analysis=_prompt_analysis(
            resource="рабочий пакет",
            client=["название", "количество участников", "тему", "желаемые даты", "формат", "бюджет", "комментарий"],
            specialist=["план", "ответственного", "длительность", "стоимость", "ресурсы", "рабочий статус"],
            manager=["приоритет", "лимит бюджета", "финальные даты", "управленческий комментарий"],
        ),
    )

    all_fields = {
        field
        for fields in hints["role_field_hints"].values()
        for field in fields
    }
    manager_fields = set(hints["role_field_hints"]["manager"])

    assert "Клиент описывает рабочий пакет" not in all_fields
    assert "Специалист видит рабочий пакет" not in all_fields
    assert "Менеджер видит все карточки" not in all_fields
    assert "рабочий пакет" not in all_fields
    assert "название" in hints["role_field_hints"]["client"]
    assert "план" in hints["role_field_hints"]["specialist"]
    assert "приоритет" in hints["role_field_hints"]["manager"]
    assert "лимит бюджета" in hints["role_field_hints"]["manager"]
    assert not any("отклоняет" in field for field in all_fields)
    assert not any("Balanced режим" in field for field in manager_fields)
    assert not any("generated metadata" in field for field in all_fields)


def test_agent_prompt_is_tool_loop_contract_not_domain_template() -> None:
    prompt = agent_system_prompt()

    assert "plan, inspect, patch the draft, run checks/browser proof" in prompt
    assert "The user prompt is the only product source" in prompt
    assert "three separate multi-page role apps" in prompt
    assert "Do not add mock data, seed data, demo data, sample data" in prompt
    assert "never create mapped attributes named metadata" in prompt
    assert "do not assert brittle implementation literals" in prompt
    assert "Never use visible technical placeholders" in prompt
    assert "Do not leave empty static/<role>/<child>/ directories" in prompt
    assert "do not patch it directly" in prompt
    assert "manifest.roles.<role>.root" in prompt
    assert "return the persisted fields at the top level" in prompt
    assert "product_scale_contract" in prompt
    assert "product_task_ledger" in prompt
    assert "completion_audit_contract" in prompt
    assert "normalize list-vs-envelope shape" in prompt
    assert "miniapp/app/generated/miniapp_contract.json" in prompt
    assert "keep names consistent across backend, JS payloads, renderers, and tests" in prompt


def test_implementation_plan_has_prompt_derived_routeable_screen_intents() -> None:
    prompt = (
        "Я владелец процесса. Клиент должен выбрать формат, дату, "
        "количество участников и дополнительные параметры, специалист должен подготовить план, "
        "обновлять статус подготовки и оставлять комментарии, менеджер должен видеть "
        "загрузку команды, выручку и позиции, где есть задержки."
    )
    screen_plan_payload = {
        "multi_page_recommended": True,
        "roles": {
            "client": [{"intent": "overview"}, {"intent": "create_or_configure"}],
            "specialist": [{"intent": "overview"}, {"intent": "detail_or_update"}],
            "manager": [{"intent": "overview"}, {"intent": "summary_or_insight"}],
        },
    }
    contract = _contract_with_analysis(
        prompt,
        generation_mode=GenerationMode.BALANCED,
        resource="операция",
        client=["формат", "дату", "количество участников", "дополнительные параметры"],
        specialist=["план", "статус подготовки", "комментарии"],
        manager=["загрузка команды", "выручка", "задержки"],
        screen_plan=screen_plan_payload,
    )
    plan = build_implementation_plan(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
    )

    screen_plan = plan["routeable_screen_plan"]

    assert screen_plan["multi_page_recommended"] is True
    assert screen_plan["no_fixed_page_count"] is True
    assert screen_plan["route_names_owned_by_agent"] is True
    assert any(item["intent"] == "create_or_configure" for item in screen_plan["roles"]["client"])
    assert any(item["intent"] == "detail_or_update" for item in screen_plan["roles"]["specialist"])
    assert any(item["intent"] == "summary_or_insight" for item in screen_plan["roles"]["manager"])


def test_ordinary_business_prompt_promotes_prompt_scale_to_role_pages() -> None:
    prompt = (
        "Сделай мини-приложение для моей студии маникюра. "
        "Клиенты должны видеть услуги, цены и свободное время, записываться на удобную дату и получать подтверждение. "
        "Мастера должны видеть свои записи на день и отмечать, что клиент пришел. "
        "Администратор должен видеть все записи, добавлять услуги, менять цены и отменять запись."
    )
    prompt_analysis = {
        "prompt_summary": "мини-приложение для студии маникюра",
        "resource_hint": "запись",
        "resource_hints": ["услуги", "цены", "свободное время", "запись"],
        "business_capabilities": ["просмотр услуг и цен", "запись клиента", "день мастера", "администрирование услуг и записей"],
        "field_hints": ["услуги", "цены", "свободное время", "дата", "подтверждение", "клиент пришел"],
        "role_field_hints": {
            "client": ["услуги", "цены", "свободное время", "дата", "подтверждение"],
            "specialist": ["записи на день", "клиент пришел"],
            "manager": ["записи", "услуги", "цены"],
        },
        "role_action_prompts": {
            "client": ["видеть услуги", "видеть цены", "видеть свободное время", "записываться на удобную дату", "получать подтверждение"],
            "specialist": ["видеть свои записи на день", "отмечать, что клиент пришел"],
            "manager": ["видеть все записи", "добавлять услуги", "менять цены", "отменять запись"],
        },
        "role_state_contract": {
            "source_roles": ["manager"],
            "update_roles": ["manager"],
            "observer_roles": ["client", "specialist"],
        },
        "routeable_screen_plan": {
            "multi_page_recommended": True,
            "roles": {
                "client": [{"intent": "list_or_read", "purpose": "показать услуги, цены и свободное время"}],
                "specialist": [],
                "manager": [],
            },
        },
    }
    contract = build_acceptance_contract(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis=prompt_analysis,
    )
    plan = build_implementation_plan(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=contract,
    )

    screen_plan = plan["routeable_screen_plan"]
    scale_contract = plan["product_scale_contract"]
    ledger = plan["product_task_ledger"]
    audit = plan["completion_audit_contract"]

    assert "client" in plan["role_state_contract"]["source_roles"]
    assert "specialist" in plan["role_state_contract"]["update_roles"]
    assert len(screen_plan["roles"]["client"]) >= 3
    assert len(screen_plan["roles"]["specialist"]) >= 2
    assert len(screen_plan["roles"]["manager"]) >= 3
    assert scale_contract["scale"] == "full_product"
    assert scale_contract["min_role_routes"]["client"] >= 3
    assert scale_contract["min_role_routes"]["specialist"] >= 2
    assert scale_contract["min_role_routes"]["manager"] >= 3
    assert any(item["id"] == "manager.role_surface" and item["expected_min_routes"] >= 3 for item in ledger)
    manager_item = next(item for item in ledger if item["id"] == "manager.role_surface")
    assert "miniapp/app/static/manager/app.js" in manager_item["owned_paths"]
    assert manager_item["depends_on"] == ["planner.plan_ready"]
    assert "browser_flow_smoke" in manager_item["proof_checks"]
    assert any(item["id"] == "shared_state.persistence_api" for item in ledger)
    proof_item = next(item for item in ledger if item["id"] == "proof.generated_and_browser")
    assert "manager.role_surface" in proof_item["depends_on"]
    assert "shared_state.persistence_api" in proof_item["depends_on"]
    assert audit["status"] == "required"
    assert "verify each ledger item has product runtime source changes, not only tests or generated metadata" in audit["audit_steps"]

    execution_contract = WorkspaceCodeAgentRuntime._product_execution_contract_payload(plan)
    assert "product_task_ledger" in execution_contract
    assert "completion_audit_contract" in execution_contract


def test_generated_tests_synthesizer_builds_workflow_oriented_python_and_js_tests() -> None:
    contract = {
        "required": True,
        "roles": ["client", "manager"],
        "flows": [
            {
                "id": "create_task",
                "name": "Client creates task for manager review",
                "roles": ["client", "manager"],
                "api_paths": ["/api/tasks"],
                "state_fields": ["id", "title", "status"],
            }
        ],
        "required_controls": [{"role": "client", "selector": "#client-main-form"}],
    }
    synthesis = GeneratedTestsSynthesizer.synthesize(
        acceptance_contract=contract,
        implementation_plan={"roles": ["client", "manager"]},
        prompt="Client creates task and manager reviews persisted status.",
    )

    kinds = {item["kind"] for item in synthesis["test_specs"]}
    python_source = synthesis["files"]["miniapp/tests/test_generated_app.py"]
    js_source = synthesis["files"]["miniapp/tests/generated_app.test.mjs"]

    assert synthesis["schema"] == "grounded.generated_tests_synthesis.v1"
    assert {"api_persistence", "route_static_shell", "js_source_selector", "browser_workflow", "role_specific"}.issubset(kinds)
    assert "/api/tasks" in synthesis["api_paths"]
    assert {"title", "status"}.issubset(set(synthesis["state_fields"]))
    assert "TestClient(app)" in python_source
    assert "client.post(api_path" in python_source
    assert "client.get(api_path)" in python_source
    assert "querySelector" in js_source
    assert "fetch\\(" in js_source
    assert "#client-main-form" in js_source


def test_implementation_plan_embeds_generated_tests_synthesis() -> None:
    contract = build_acceptance_contract(
        prompt="Client creates task, manager reviews status.",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        prompt_analysis={
            "resource_hint": "task",
            "resource_hints": ["task"],
            "role_action_prompts": {"client": ["creates task"], "manager": ["reviews status"]},
            "role_field_hints": {"client": ["title"], "manager": ["status"]},
            "role_state_contract": {"source_roles": ["client"], "observer_roles": ["manager"]},
        },
    )
    plan = build_implementation_plan(
        prompt="Client creates task, manager reviews status.",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
    )

    synthesis = plan["generated_tests_synthesis"]
    assert synthesis["status"] == "ready"
    assert synthesis["files"]["miniapp/tests/test_generated_app.py"]
    assert synthesis["files"]["miniapp/tests/generated_app.test.mjs"]
    assert any(item["kind"] == "browser_workflow" for item in synthesis["test_specs"])


def test_acceptance_contract_carries_prompt_field_hints() -> None:
    prompt = "Клиент указывает компанию, адрес, дату, бюджет и комментарий. Менеджер видит выручку."

    contract = _contract_with_analysis(
        prompt,
        client=["компанию", "адрес", "дату", "бюджет", "комментарий"],
        manager=["выручку"],
    )

    assert contract["api_contract"]["field_hints"][:3] == ["компанию", "адрес", "дату"]
    assert contract["api_contract"]["role_field_hints"]["client"][:3] == ["компанию", "адрес", "дату"]
    assert "prompt_hints" in contract


def test_acceptance_contract_splits_role_owned_fields_and_resource_hint() -> None:
    prompt = (
        "Клиент создает проект, указывает компанию, телефон и бюджет. "
        "Специалист назначает материалы, рассчитывает стоимость и добавляет комментарий цеха. "
        "Менеджер может пометить приоритет и добавить управленческий комментарий."
    )

    contract = _contract_with_analysis(
        prompt,
        resource="проект",
        client=["компанию", "телефон", "бюджет"],
        specialist=["материалы", "стоимость", "комментарий цеха"],
        manager=["приоритет", "управленческий комментарий"],
    )

    role_fields = contract["api_contract"]["role_field_hints"]
    assert role_fields["client"] == ["компанию", "телефон", "бюджет"]
    assert "материалы" in role_fields["specialist"]
    assert "управленческий комментарий" in role_fields["manager"]
    assert contract["api_contract"]["resource_hint"] == "проект"


def test_acceptance_contract_does_not_merge_followup_visibility_sentence_into_client_fields() -> None:
    prompt = (
        "Клиент создает проект, указывает компанию, бюджет и комментарий. "
        "Видит статус, расчет стоимости и дату визита после перезагрузки. "
        "Специалист рассчитывает стоимость и добавляет комментарий бригады."
    )

    contract = _contract_with_analysis(
        prompt,
        resource="проект",
        client=["компанию", "бюджет", "комментарий"],
        specialist=["стоимость", "комментарий бригады"],
    )

    assert contract["api_contract"]["role_field_hints"]["client"] == ["компанию", "бюджет", "комментарий"]
    assert "стоимость" in contract["api_contract"]["role_field_hints"]["specialist"]


def test_cross_role_update_visibility_requires_client_renderer(tmp_path: Path) -> None:
    static_root = tmp_path / "miniapp/app/static"
    (static_root / "client").mkdir(parents=True)
    role_text = {
        "client": "function itemDetails(item) { return item.status + item.materials; }",
        "specialist": 'fetch("/api/items/1/update", { method: "PATCH", body: JSON.stringify({ status: "production", calculated_price: "1000", ready_date: "2026-06-05", workshop_comment: "ready" }) });',
        "manager": "",
    }

    issues = CheckRunner._cross_role_update_visibility_issues(
        static_root=static_root,
        role_text=role_text,
        source_roles=["client"],
        update_roles=["specialist"],
    )

    assert issues
    assert issues[0].code == "platform.cross_role_update_not_rendered_in_role"
    assert set(issues[0].repair_recipe["evidence"]["missing_role_fields"]) >= {
        "calculated_price",
        "ready_date",
        "workshop_comment",
    }


def test_cross_role_update_visibility_accepts_prompt_owned_label_fields(tmp_path: Path) -> None:
    static_root = tmp_path / "miniapp/app/static"
    (static_root / "client").mkdir(parents=True)
    role_text = {
        "client": "function itemDetails(item) { return item.prep_status_label + item.manager_choice_label; }",
        "specialist": 'fetch("/api/items/1", { method: "PATCH", body: JSON.stringify({ prep_status: "ready", prep_status_label: "Готово" }) });',
        "manager": 'fetch("/api/items/1", { method: "PATCH", body: JSON.stringify({ manager_choice: "book", manager_choice_label: "Выбрано" }) });',
    }

    issues = CheckRunner._cross_role_update_visibility_issues(
        static_root=static_root,
        role_text=role_text,
        source_roles=["client"],
        update_roles=["specialist", "manager"],
    )

    assert issues == []


def test_python_generated_test_diagnostic_detects_json_store_envelope_assertion() -> None:
    test_source = """
import json

class GeneratedAppPythonTests(unittest.TestCase):
    def test_store(self):
        persisted = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted), 1)
"""

    diagnostic = CheckRunner._python_json_store_shape_assertion_issue(test_source)

    assert diagnostic is not None
    assert diagnostic["json_var"] == "persisted"
    assert "raw.get('items', raw)" in diagnostic["expected_fix"]


def test_contract_compiler_preserves_prompt_metadata_without_product_shell() -> None:
    prompt = (
        "Клиент указывает название, бюджет и комментарий. "
        "Специалист добавляет план, ответственного и стоимость. "
        "Менеджер видит результат специалиста, фиксирует лимит и итоговое решение."
    )
    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.QUALITY,
        acceptance_contract=_contract_with_analysis(
            prompt,
            generation_mode=GenerationMode.QUALITY,
            resource="рабочий пакет",
            client=["название", "бюджет", "комментарий"],
            specialist=["план", "ответственный", "стоимость"],
            manager=["лимит", "итоговое решение"],
        ),
    )

    resource = contract.resources[0]

    assert resource.role_actions["manager"] == ["лимит; итоговое решение"]
    assert all("miniapp/app/routes/" not in path for path in contract.allowed_file_graph.contract_owned_paths)
    assert "miniapp/tests/test_generated_app.py" not in contract.allowed_file_graph.blocked_globs
    assert contract.acceptance_summary["features"]["platform_product_scaffold"] is False


def test_role_surface_blocks_generic_workflow_copy_and_raw_status_rendering() -> None:
    technical = CheckRunner._technical_role_copy_markers(
        '<p>No workflow entries yet.</p><button type="button">Save progress</button>'
    )
    raw_status = CheckRunner._raw_status_render_markers(
        """
        function render(item) {
          return `<span>${escapeHtml(item.status || "new")}</span>`;
          return `<p><strong>Приоритет:</strong> ${escapeHtml(item.priority)}</p>`;
        }
        buildLine('Статус', item.status);
        const DETAIL_FIELD_LABELS = { status: "статус", title: "название" };
        function statusLabel(status) { return STATUS_LABELS[status] || status || "Новый"; }
        function humanStatus(status) {
          const labels = { ready: "Готово" };
          return labels[status] || status || "Новая";
        }
        """
    )

    assert "no workflow entries yet" in technical
    assert ">save progress<" in technical
    assert "template escape(item.status)" in raw_status
    assert "template item.priority raw enum" in raw_status
    assert "buildLine status=item.status" in raw_status
    assert "detail labels include raw status field" in raw_status
    assert "status label raw passthrough" in raw_status

    safe_status = CheckRunner._raw_status_render_markers(
        """
        const STATUS_LABELS = { new: "Новый", ready: "Готов" };
        function statusLabel(value) {
          return STATUS_LABELS[String(value || "").toLowerCase()] || "На обработке";
        }
        function render(item) {
          return `<span>${escapeHtml(statusLabel(item.status))}</span>`;
        }
        """
    )
    assert safe_status == []

    conditional_status = CheckRunner._raw_status_render_markers(
        """
        function humanStatus(status) {
          return status === "active" ? "Работает" : "Пауза";
        }
        function render(item) {
          return `<span>${item.status === "active" ? "Работает" : "Пауза"}</span>`;
          return `<b>${humanStatus(item.status)}</b>`;
        }
        """
    )
    assert conditional_status == []


def test_hidden_state_class_requires_effective_css_rule() -> None:
    issue = CheckRunner._hidden_state_css_issue(
        "client",
        '<div id="contract-empty" class="state hidden">Пока нет записей</div>',
        ".state { display: grid; }",
    )
    ok = CheckRunner._hidden_state_css_issue(
        "client",
        '<div id="contract-empty" class="state hidden">Пока нет записей</div>',
        ".hidden { display: none; }",
    )

    assert issue is not None
    assert issue.code == "platform.hidden_state_class_without_css"
    assert ok is None


def test_html_control_contract_blocks_duplicate_ids_and_form_names() -> None:
    issues = CheckRunner._html_control_contract_issues(
        "miniapp/app/static/specialist/index.html",
        """
        <form>
          <label>Результат <textarea id="contract-result" name="result"></textarea></label>
          <label>Результат <input id="contract-result" name="result" type="text" /></label>
          <label><input name="options" type="checkbox" /> Разрешенный повтор для группы</label>
          <label><input name="options" type="checkbox" /> Разрешенный повтор для группы</label>
        </form>
        """,
    )

    codes = {issue.code for issue in issues}
    assert "platform.duplicate_dom_id" in codes
    assert "platform.duplicate_form_control_name" in codes


def test_browser_flow_visibility_reads_visible_form_control_values() -> None:
    script = CheckRunner._real_browser_ui_flow_python_script()

    assert 'querySelectorAll("input, textarea, select")' in script
    assert 'type === "hidden"' in script
    assert "selectedIndex" in script
    assert 'page.locator("body").inner_text' in script


def test_role_surface_blocks_visible_http_api_copy_without_blocking_js_methods() -> None:
    markers = CheckRunner._technical_visible_html_copy_markers(
        '<p>PATCH сохраняет изменения</p><p>/api/vizitas</p><p>Через защищённое API</p><p>Client</p><script>fetch("/api/vizitas", { method: "PATCH" })</script>'
    )
    js_markers = CheckRunner._technical_visible_html_copy_markers(
        '<script>fetch("/api/vizitas", { method: "PATCH" })</script>'
    )

    assert markers == ["visible HTTP method", "visible API path", "visible API term", "visible role slug"]
    assert js_markers == []


def test_frontend_form_field_reads_accept_dynamic_formdata_entries_payload() -> None:
    js_source = '''
      const form = document.getElementById("main-create-form");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const payload = {};
        for (const [key, value] of formData.entries()) {
          payload[key] = String(value || "");
        }
        payload.created_by = ROLE;
        await fetch(API_BASE, { method: "POST", body: JSON.stringify(payload) });
      });
    '''

    assert CheckRunner._js_reads_form_field(js_source, "kompaniya")
    assert CheckRunner._js_effective_form_payload_fields(js_source, {"kompaniya", "byudzhet"}) == {
        "kompaniya",
        "byudzhet",
        "created_by",
    }


def test_miniapp_contract_uses_prompt_fields_instead_of_items_slug() -> None:
    prompt = "Клиент указывает компанию, адрес и бюджет для корпоративного ланча."
    acceptance_contract = _contract_with_analysis(
        prompt,
        resource="корпоративный ланч",
        client=["компанию", "адрес", "бюджет для корпоративного ланча"],
    )
    implementation_plan = build_implementation_plan(
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
    )

    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
        implementation_plan=implementation_plan,
    )

    resource = contract.resources[0]
    assert resource.slug != "items"
    assert set(resource.field_labels.values()) >= {"компанию", "адрес", "бюджет для корпоративного ланча"}
    assert any(field.startswith("komp") for field in resource.field_labels)


def test_miniapp_contract_uses_role_scoped_fields_and_business_resource_name() -> None:
    prompt = (
        "Клиент создает проект, указывает компанию, телефон и бюджет. "
        "Специалист добавляет комментарий цеха и дату готовности."
    )
    acceptance_contract = _contract_with_analysis(
        prompt,
        resource="проект",
        client=["компанию", "телефон", "бюджет"],
        specialist=["комментарий цеха", "дату готовности"],
    )

    contract = MiniAppContractCompiler.compile(
        workspace_id="ws_test",
        run_id="run_test",
        prompt=prompt,
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract=acceptance_contract,
    )

    resource = contract.resources[0]
    assert "proekt" in resource.slug
    assert resource.display_name == "Проект"
    assert set(resource.role_field_labels["client"].values()) == {"компанию", "телефон", "бюджет"}
    assert "комментарий цеха" in set(resource.role_field_labels["specialist"].values())
    assert {(endpoint.method, endpoint.path) for endpoint in resource.endpoints} == {
        ("GET", "/api/proekts"),
        ("POST", "/api/proekts"),
        ("PATCH", "/api/proekts/{item_id}"),
    }
    assert "kommentariyCeha" not in resource.role_field_labels["client"]


def test_frontend_backend_payload_validator_accepts_pydantic_extra_allow(tmp_path: Path) -> None:
    backend_text = '''
from pydantic import BaseModel, ConfigDict

class ContractItemStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "updated"
    updated_by: str = "manager"
'''
    schemas = CheckRunner._backend_update_schema_contracts(backend_text)
    js_path = tmp_path / "a/b/c/d/manager/app.js"
    js_path.parent.mkdir(parents=True)
    js_path.write_text("", encoding="utf-8")

    issues = CheckRunner._frontend_backend_patch_payload_issues(
        "manager",
        js_path,
        'fetch("/api/items/1/update", { method: "PATCH", body: JSON.stringify({ priority: "Высокий", updated_by: ROLE }) });',
        backend_patch_schemas=schemas,
    )

    assert schemas[0]["accepted"] >= {"*", "status", "updated_by"}
    assert issues == []


def test_platform_shell_stabilizer_adds_shell_assets_to_plain_html() -> None:
    html = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="/static/client/styles.css">'
        "</head><body><section>Content</section>"
        '<script defer src="/static/client/app.js"></script>'
        "</body></html>"
    )

    updated = WorkspaceCodeAgentRuntime._ensure_html_platform_shell(html)

    assert '/static/shared/base.css' in updated
    assert '/static/preview_bridge.js' in updated
    assert 'class="page-shell"' in updated
    assert "telegram-top-safe-offset" in updated


def test_platform_shell_stabilizer_restores_entrypoint_from_template(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    shutil.copytree(repo_root / "runtime/templates/base-miniapp", source)
    broken_main = source / "miniapp/app/main.py"
    broken_main.write_text(
        "from fastapi import FastAPI\nfrom .routes.role_pages import router as role_pages_router\napp = FastAPI()\napp.include_router(role_pages_router)\n",
        encoding="utf-8",
    )
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = SimpleNamespace(settings=SimpleNamespace(template_dir=repo_root / "runtime/templates/base-miniapp"))

    stabilized = runtime._stabilize_platform_shell("ws_1", "run_1", source, ["miniapp/app/main.py"])

    assert "miniapp/app/main.py" in stabilized
    assert broken_main.read_text(encoding="utf-8") == (repo_root / "runtime/templates/base-miniapp/miniapp/app/main.py").read_text(encoding="utf-8")


def test_repair_context_paths_exclude_platform_shell_and_legacy_api_alias() -> None:
    paths = WorkspaceCodeAgentRuntime._repair_context_paths(
        failed_paths=["miniapp/tests/test_generated_app.py"],
        diff_paths=[
            "miniapp/app/main.py",
            "miniapp/app/routes/api.py",
            "miniapp/app/routes/app_api.py",
            "miniapp/app/routes/role_routes.py",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/static/client/app.js",
        ],
    )

    assert "miniapp/app/main.py" not in paths
    assert "miniapp/app/routes/app_api.py" not in paths
    assert "miniapp/app/routes/role_routes.py" not in paths
    assert "miniapp/app/generated/route_manifest.json" not in paths
    assert "miniapp/app/routes/api.py" in paths


def test_tool_round_limits_allow_real_agent_inspection_cycles(monkeypatch) -> None:
    monkeypatch.delenv("WORKSPACE_AGENT_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_FAST_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_BALANCED_TOOL_ROUND_LIMIT", raising=False)
    monkeypatch.delenv("WORKSPACE_AGENT_QUALITY_TOOL_ROUND_LIMIT", raising=False)

    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.FAST) >= 2
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.BALANCED) >= 4
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.QUALITY) >= 6
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.PRODUCTION) >= 6

    monkeypatch.setenv("WORKSPACE_AGENT_FAST_TOOL_ROUND_LIMIT", "3")
    assert WorkspaceCodeAgentRuntime._tool_round_limit(GenerationMode.FAST) == 3


def test_agent_tools_batch_reads_and_serialize_mutations() -> None:
    plan = plan_agent_tool_batches(
        [
            {"tool": "read_files", "targets": ["miniapp/app/main.py"]},
            {"tool": "semantic_scan", "targets": ["miniapp/app"]},
            {"tool": "browser_verify", "targets": ["/client"]},
            {"tool": "apply_patch_to_draft", "targets": ["miniapp/app/main.py"], "diff": "@@\n-old\n+new\n"},
            {"tool": "write_file", "targets": ["miniapp/app/static/client/app.js"], "content": "console.log(1);\n"},
        ]
    )

    assert agent_tool_kind("read_files") == "read_only"
    assert agent_tool_kind("apply_patch_to_draft") == "mutating"
    assert AgentToolRegistry.spec("browser_verify") is not None
    assert AgentToolRegistry.spec("browser_verify").kind == "verification"  # type: ignore[union-attr]
    assert [item["tool"] for item in plan.read_only_requests] == ["read_files", "semantic_scan", "browser_verify"]
    assert [item["tool"] for item in plan.mutating_requests] == ["apply_patch_to_draft", "write_file"]
    assert [[item["tool"] for item in batch.requests] for batch in plan.ordered_batches] == [
        ["read_files", "semantic_scan"],
        ["browser_verify"],
        ["apply_patch_to_draft"],
        ["write_file"],
    ]


def test_agent_registry_exposes_real_openai_tool_contract() -> None:
    tools = AgentToolRegistry.openai_tools()
    encoded = json.dumps(tools)
    names = {str(tool["name"]) for tool in tools}

    assert any(tool["name"] == "apply_patch_to_draft" for tool in tools)
    assert "tool_search" in names
    assert AgentToolRegistry.spec("browser_verify") is not None
    assert "browser_verify" not in names
    assert all("." not in name and name != "browser_verify" for name in names)
    assert ("tool_" + "requests") not in encoded
    assert all(tool["type"] == "function" for tool in tools)


class _RouterWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def file_tree(self, workspace_id: str, *, run_id: str) -> list[dict[str, str]]:
        del workspace_id, run_id
        return [
            {"path": str(path.relative_to(self.root)).replace("\\", "/"), "type": "file"}
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        ]

    def try_read_text_file(self, workspace_id: str, relative_path: str, *, run_id: str) -> str | None:
        del workspace_id, run_id
        target = self.root / relative_path
        return target.read_text(encoding="utf-8") if target.exists() else None

    def diff(self, workspace_id: str, *, run_id: str) -> str:
        del workspace_id, run_id
        return ""


def _router_context(
    tmp_path: Path,
    *,
    mode: str = "default",
    hook_manager: AgentHookManager | None = None,
    output_artifact_writer=None,
) -> ToolRouterContext:
    root = tmp_path
    (root / "miniapp/app/static/client").mkdir(parents=True, exist_ok=True)
    (root / "miniapp/app/static/client/app.js").write_text("console.log('ok');\n", encoding="utf-8")

    def execute_checks(_: list[str]) -> tuple[CheckExecutionRecord, dict[str, object]]:
        return (
            CheckExecutionRecord(
                workspace_id="ws_router",
                run_id="run_router",
                changed_files=[],
                results=[RunCheckResult(name="router_check", status="passed", details="ok")],
                started_at=utc_now(),
                completed_at=utc_now(),
                duration_ms=0,
            ),
            {"preview": "ok"},
        )

    return ToolRouterContext(
        workspace_id="ws_router",
        run_id="run_router",
        draft_source=root,
        workspace_service=_RouterWorkspace(root),  # type: ignore[arg-type]
        execute_checks=execute_checks,
        hook_manager=hook_manager,
        mode=mode,
        output_artifact_writer=output_artifact_writer,
    )


def test_tool_registry_contract_exposes_governed_canonical_specs() -> None:
    contract = tool_registry_contract()
    tools = {item["canonical"]: item for item in contract["tools"]}

    assert contract["schema"] == "grounded.tool_registry_contract.v2"
    assert contract["alias_index"]["run_command"] == "shell.exec"
    assert contract["json_schema_tools"]["input_schema_field"] == "input_schema"
    assert contract["parallel_execution_policy"]["parallel_safe_field"] == "parallel_safe"
    for spec in tools.values():
        assert spec["input_schema"]["type"] == "object"
        assert spec["output_schema"]["type"] == "object"
        assert spec["capabilities"]
        assert "read" in spec["allowed_paths"]
        assert spec["side_effect_class"] in {"none", "read_workspace", "write_draft", "execute_process", "verification", "external_browser", "approval_request", "unknown"}
        assert isinstance(spec["parallel_safe"], bool)
        assert spec["risk"] in {"safe", "read_only", "mutating", "network", "destructive", "forbidden", "unknown"}
        assert spec["approval_class"] in {"none", "policy", "human", "forbidden"}
        assert isinstance(spec["concurrency_safe"], bool)
        assert spec["timeout_seconds"] > 0
        assert spec["output_cap_chars"] > 0
        assert spec["artifact_spill_policy"] in {"never", "on_truncation", "always"}
        assert spec["result_summarization"]["mode"]
        assert spec["retry_policy"]["max_attempts"] >= 1
        assert spec["failure_signatures"]["format"]
    assert "read_files" in tools["file.read"]["aliases"]
    assert tools["shell.exec"]["approval_class"] == "policy"
    assert tools["shell.exec"]["side_effect_class"] == "execute_process"
    assert tools["file.write"]["approval_class"] == "human"
    assert tools["file.write"]["side_effect_class"] == "write_draft"
    assert tools["file.read"]["parallel_safe"] is True


def test_tool_router_normalizes_alias_and_returns_typed_envelope(tmp_path: Path) -> None:
    router = ToolRouter(_router_context(tmp_path))

    result = router.route_batch(
        [
            {
                "tool": "read_files",
                "tool_use_id": "read_1",
                "targets": ["miniapp/app/static/client/app.js"],
                "reason": "inspect",
            }
        ]
    )

    read_result = result.model_results[1]
    envelope = read_result["envelope"]
    assert result.loaded_context["miniapp/app/static/client/app.js"] == "console.log('ok');\n"
    assert read_result["tool_use_id"] == "read_1"
    assert envelope["tool_call_id"] == "read_1"  # type: ignore[index]
    assert envelope["tool"] == "file.read"  # type: ignore[index]
    assert envelope["status"] == "completed"  # type: ignore[index]
    assert envelope["approval"]["class"] == "none"  # type: ignore[index]
    assert envelope["capabilities"]  # type: ignore[index]
    assert envelope["parallel_safe"] is True  # type: ignore[index]
    assert envelope["side_effect_class"] == "read_workspace"  # type: ignore[index]
    assert envelope["result_summary"]["counts"]["files_count"] == 1  # type: ignore[index]


def test_tool_router_spills_large_result_to_output_artifact_writer(tmp_path: Path, monkeypatch) -> None:
    for index in range(40):
        target = tmp_path / f"miniapp/app/static/client/generated/very-long-component-name-{index}.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"console.log({index});\n", encoding="utf-8")
    monkeypatch.setitem(
        TOOL_EXECUTION_DEFAULTS,
        "file.list",
        {**TOOL_EXECUTION_DEFAULTS["file.list"], "output_cap_chars": 260},
    )
    written: list[dict[str, object]] = []

    def writer(payload: dict[str, object]) -> dict[str, object] | None:
        written.append(payload)
        return {
            "ref": "exec_output:ws_router:run_router:tool:list_1:tool:abc",
            "artifact_id": "tool:list_1:tool:abc",
            "stream": payload["stream"],
            "sha256": "abc",
            "chars": len(str(payload.get("content") or "")),
            "omitted_chars": (payload.get("head_tail") or {}).get("omitted_chars") if isinstance(payload.get("head_tail"), dict) else 0,
            "truncated_full": False,
        }

    router = ToolRouter(_router_context(tmp_path, output_artifact_writer=writer))
    result = router.route_batch([{"tool": "list_files", "tool_use_id": "list_1", "reason": "inventory"}])
    payload = result.model_results[1]
    envelope = payload["envelope"]

    assert envelope["truncation"]["truncated"] is True  # type: ignore[index]
    assert envelope["truncation"]["spilled"] is True  # type: ignore[index]
    assert envelope["artifacts"][0]["kind"] == "tool_result"  # type: ignore[index]
    assert payload["artifact_ref"] == "exec_output:ws_router:run_router:tool:list_1:tool:abc"
    assert written[0]["stream"] == "tool"
    assert written[0]["metadata"]["tool"] == "file.list"  # type: ignore[index]
    assert "very-long-component-name" in str(written[0]["content"])


def test_tool_router_schema_validation_reports_extra_and_missing_args(tmp_path: Path) -> None:
    router = ToolRouter(_router_context(tmp_path))

    extra = router.route_batch([{"tool": "read_files", "tool_use_id": "bad_1", "targets": [], "reason": "x", "unexpected": True}])
    missing = router.route_batch([{"tool": "run_command", "tool_use_id": "bad_2", "reason": "x"}])

    assert extra.model_results[1]["status"] == "failed"
    assert extra.model_results[1]["envelope"]["error"]["code"] == "tool_schema_invalid"  # type: ignore[index]
    assert extra.model_results[1]["envelope"]["tool"] == "file.read"  # type: ignore[index]
    assert missing.model_results[1]["status"] == "failed"
    assert missing.model_results[1]["envelope"]["error"]["details"]["missing"] == ["command"]  # type: ignore[index]
    assert missing.model_results[1]["envelope"]["failure_signature"].startswith("shell.exec:tool_schema_invalid:")  # type: ignore[index]
    assert missing.model_results[1]["envelope"]["result_summary"]["error_code"] == "tool_schema_invalid"  # type: ignore[index]


def test_tool_router_mode_filtering_and_forced_tools() -> None:
    default_names = {tool["name"] for tool in ToolRouter.allowed_openai_tools()}
    mutation_names = {tool["name"] for tool in ToolRouter.allowed_openai_tools(mode="mutation_required", forced_allowed={"write_file"})}
    forced_browser_names = {tool["name"] for tool in ToolRouter.allowed_openai_tools(forced_allowed={"browser_verify"})}

    assert "read_files" in default_names
    assert "tool_search" in default_names
    assert "ask_user" not in default_names
    assert "browser_verify" not in default_names
    assert mutation_names == {"write_file"}
    assert forced_browser_names == {"browser_verify"}


def test_tool_router_manifest_v2_surfaces_capabilities_paths_and_retry_policy() -> None:
    manifest = ToolRouter.manifest()
    tools = {item["canonical"]: item for item in manifest["tools"]}

    assert manifest["schema"] == "grounded.tool_router.manifest.v2"
    assert manifest["parallel_execution_policy"]["parallel_safe_field"] == "parallel_safe"
    assert manifest["result_policy"]["failure_signature_field"] == "failure_signature"
    assert tools["file.read"]["capabilities"]
    assert tools["file.read"]["parallel_safe"] is True
    assert tools["file.write"]["allowed_paths"]["write"]
    assert tools["file.write"]["retry_policy"]["first_retry_tool"] == "read_files"
    assert tools["shell.exec"]["side_effect_class"] == "execute_process"


def test_tool_router_dynamic_tool_search_discovers_deferred_capabilities(tmp_path: Path) -> None:
    router = ToolRouter(_router_context(tmp_path))

    result = router.route_batch(
        [
            {
                "tool": "tool_search",
                "tool_use_id": "search_1",
                "query": "deploy to vercel with database and payments",
                "reason": "Find optional tools without exposing every connector.",
            }
        ]
    )
    payload = result.model_results[1]
    domains = {str(item.get("domain")) for item in payload["items"]}  # type: ignore[index]

    assert result.envelopes[0]["status"] == "completed"
    assert result.model_results[1]["status"] == "ready"
    assert result.envelopes[0]["tool"] == "tool.search"
    assert {"deploy", "vercel", "database", "payments"}.issubset(domains)
    assert payload["summary"]["deferred_count"] >= 3  # type: ignore[index]
    assert "browser_verify" not in {tool["name"] for tool in ToolRouter.allowed_openai_tools()}


def test_tool_router_disallowed_and_hook_block_return_failed_envelopes(tmp_path: Path, monkeypatch) -> None:
    disallowed = ToolRouter(_router_context(tmp_path, mode="read_only")).route_batch(
        [{"tool": "write_file", "tool_use_id": "write_1", "file_path": "miniapp/app/static/client/app.js", "content": "changed", "reason": "x"}]
    )
    assert disallowed.model_results[1]["status"] == "failed"
    assert disallowed.model_results[1]["envelope"]["error"]["code"] == "tool_not_allowed"  # type: ignore[index]

    hook_manager = AgentHookManager()
    monkeypatch.setitem(TOOL_RISK_DEFAULTS, "file.read", "forbidden")
    blocked = ToolRouter(_router_context(tmp_path, hook_manager=hook_manager)).route_batch(
        [{"tool": "read_files", "tool_use_id": "read_blocked", "targets": ["miniapp/app/static/client/app.js"], "reason": "x"}]
    )

    assert blocked.model_results[1]["status"] == "failed"
    assert blocked.model_results[1]["envelope"]["error"]["code"] == "tool_hook_blocked"  # type: ignore[index]
    assert hook_manager.snapshot("run_router")["counts"]["pre_tool_use:failed"] == 1


def test_tool_router_post_hook_adds_context_without_changing_envelope_shape(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = HookPolicyService(store)
    service.update_workspace_policy(
        "ws_router",
        {
            "rules": [
                {
                    "rule_id": "post_read_context",
                    "conditions": {"hook": "post_tool_use", "canonical_tool": "file.read"},
                    "actions": [{"action": "add_context", "text": "Read output should guide the next patch.", "priority": 5}],
                }
            ]
        },
    )
    hook_manager = AgentHookManager(policy_service=service)

    result = ToolRouter(_router_context(tmp_path, hook_manager=hook_manager)).route_batch(
        [{"tool": "read_files", "tool_use_id": "read_1", "targets": ["miniapp/app/static/client/app.js"], "reason": "inspect"}]
    )
    model_result = result.model_results[1]
    envelope = model_result["envelope"]

    assert model_result["status"] == "completed"
    assert model_result["hook_contexts"] == ["Read output should guide the next patch."]
    assert envelope["tool"] == "file.read"  # type: ignore[index]
    assert envelope["status"] == "completed"  # type: ignore[index]
    assert envelope["hook_contexts"] == ["Read output should guide the next patch."]  # type: ignore[index]


def test_tool_router_mutating_tools_defer_without_writing_files(tmp_path: Path) -> None:
    target = tmp_path / "miniapp/app/static/client/app.js"
    router = ToolRouter(_router_context(tmp_path))

    result = router.route_batch(
        [
            {
                "tool": "write_file",
                "tool_use_id": "write_1",
                "file_path": "miniapp/app/static/client/app.js",
                "content": "console.log('changed');\n",
                "reason": "replace",
            }
        ]
    )

    write_result = result.model_results[1]
    envelope = write_result["envelope"]
    assert write_result["status"] == "deferred"
    assert envelope["tool"] == "file.write"  # type: ignore[index]
    assert envelope["approval"]["required"] is True  # type: ignore[index]
    assert result.deferred_changes[0].file_path == "miniapp/app/static/client/app.js"
    assert target.read_text(encoding="utf-8") == "console.log('ok');\n"


def _orchestrator_request(*, allowed: bool = True, deferred: bool = False, max_attempts: int = 1) -> ToolOrchestrationRequest:
    request = SimpleNamespace(tool="read_files", canonical_tool="file.read", tool_call_id="tool_1")
    decision = SimpleNamespace(
        allowed=allowed,
        reason="allowed" if allowed else "blocked by mode",
        approval_class="none",
        sandbox_profile="analysis_readonly",
        retry_policy={"max_attempts": max_attempts},
        deferred=deferred,
    )
    return ToolOrchestrationRequest(
        request=request,
        decision=decision,
        protocol_input={"mode": "default"},
        workspace_id="ws_router",
        run_id="run_router",
    )


def _orchestrator_result(*, status: str = "completed", error_code: str | None = None):
    error = None
    if error_code:
        error = {"code": error_code, "message": error_code, "retryable": True, "details": {}}
    envelope = {
        "tool": "file.read",
        "status": status,
        "error": error,
        "retry": {"max_attempts": 1},
        "result_summary": {},
        "failure_class": error_code,
        "failure_signature": f"file.read:{error_code}:test" if error_code else None,
    }
    return SimpleNamespace(envelope=envelope, model_result={"status": status, "envelope": envelope})


def test_tool_orchestrator_records_attempts_for_allowed_tool() -> None:
    events: list[dict[str, object]] = []
    orchestrator = ToolOrchestrator(emit_activity=lambda kind, label, details: events.append({"kind": kind, "label": label, "details": details}))

    result = orchestrator.run(
        _orchestrator_request(),
        schema_error=lambda: None,
        pre_hook_error=lambda: None,
        post_hook=lambda router_result, failed: None,
        execute=lambda: _orchestrator_result(),
        failed_result=lambda error: _orchestrator_result(status="failed", error_code=error["code"]),
    )

    assert result.router_result.envelope["status"] == "completed"
    assert result.router_result.envelope["retry"]["attempt"] == 1
    assert result.router_result.envelope["retry"]["attempts"][0]["status"] == "completed"
    assert result.router_result.envelope["orchestration"]["attempt_count"] == 1
    assert [event["kind"] for event in events] == [
        "tool_orchestration_started",
        "tool_attempt_started",
        "tool_attempt_completed",
        "tool_orchestration_completed",
    ]


def test_tool_orchestrator_disallowed_tool_uses_compatible_failure_shape() -> None:
    orchestrator = ToolOrchestrator()

    result = orchestrator.run(
        _orchestrator_request(allowed=False),
        schema_error=lambda: None,
        pre_hook_error=lambda: None,
        post_hook=lambda router_result, failed: None,
        execute=lambda: _orchestrator_result(),
        failed_result=lambda error: _orchestrator_result(status="failed", error_code=error["code"]),
    )

    assert result.router_result.envelope["status"] == "failed"
    assert result.router_result.envelope["error"]["code"] == "tool_not_allowed"
    assert result.router_result.model_result["orchestration_attempts"][0]["failure_class"] == "tool_not_allowed"


def test_tool_orchestrator_hook_block_runs_failure_post_hook() -> None:
    post_calls: list[bool] = []
    orchestrator = ToolOrchestrator()

    result = orchestrator.run(
        _orchestrator_request(),
        schema_error=lambda: None,
        pre_hook_error=lambda: {"code": "tool_hook_blocked", "message": "blocked", "retryable": False, "details": {}},
        post_hook=lambda router_result, failed: post_calls.append(failed),
        execute=lambda: _orchestrator_result(),
        failed_result=lambda error: _orchestrator_result(status="failed", error_code=error["code"]),
    )

    assert result.router_result.envelope["error"]["code"] == "tool_hook_blocked"
    assert post_calls == [True]


def test_tool_orchestrator_governance_retry_is_limited_to_max_attempts() -> None:
    calls = {"count": 0}
    orchestrator = ToolOrchestrator()

    def execute():
        calls["count"] += 1
        return _orchestrator_result(status="failed", error_code="sandbox_preflight_blocked")

    result = orchestrator.run(
        _orchestrator_request(deferred=True, max_attempts=2),
        schema_error=lambda: None,
        pre_hook_error=lambda: None,
        post_hook=lambda router_result, failed: None,
        execute=execute,
        failed_result=lambda error: _orchestrator_result(status="failed", error_code=error["code"]),
    )

    assert calls["count"] == 2
    assert len(result.attempts) == 2
    assert result.router_result.envelope["retry"]["governance_retry_only"] is True
    assert [attempt["error_code"] for attempt in result.router_result.envelope["retry"]["attempts"]] == [
        "sandbox_preflight_blocked",
        "sandbox_preflight_blocked",
    ]


def test_role_surface_issues_accepts_generation_mode(tmp_path: Path) -> None:
    css = """
    .dashboard { display: grid; }
    .card { padding: 12px; }
    .button { min-height: 44px; }
    .form { display: grid; }
    .input { border: 1px solid #ccd; }
    .list { display: grid; }
    .status { font-weight: 700; }
    .metric { font-variant-numeric: tabular-nums; }
    """
    for role in ("client", "specialist", "manager"):
        role_dir = tmp_path / "miniapp" / "app" / "static" / role
        role_dir.mkdir(parents=True)
        (role_dir / "index.html").write_text(
            f"<main><section class='dashboard'><h1>{role} workflow app</h1><button class='button'>Save</button></section></main>",
            encoding="utf-8",
        )
        (role_dir / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
        (role_dir / "styles.css").write_text(css, encoding="utf-8")

    issues, coverage, _neutral = CheckRunner._role_surface_issues(tmp_path, generation_mode=GenerationMode.BALANCED)

    assert isinstance(issues, list)
    assert set(coverage) == {"client", "specialist", "manager"}


def test_prompt_scale_requires_routeable_role_pages(tmp_path: Path) -> None:
    contract = build_acceptance_contract(
        prompt=(
            "Клиент записывается и получает подтверждение. "
            "Мастер видит записи дня и отмечает приход. "
            "Администратор добавляет услуги, меняет цены и отменяет записи."
        ),
        intent="create",
        generation_mode=GenerationMode.FAST,
        prompt_analysis={
            "prompt_summary": "студия маникюра",
            "resource_hint": "запись",
            "field_hints": ["услуга", "цена", "слот", "приход"],
            "role_field_hints": {
                "client": ["услуга", "слот", "подтверждение"],
                "specialist": ["записи дня", "приход"],
                "manager": ["услуги", "цены", "отмена"],
            },
            "role_action_prompts": {
                "client": ["записываться", "получать подтверждение"],
                "specialist": ["видеть записи дня", "отмечать приход"],
                "manager": ["добавлять услуги", "менять цены", "отменять записи"],
            },
            "routeable_screen_plan": {"multi_page_recommended": True, "roles": {"client": [], "specialist": [], "manager": []}},
        },
    )
    css = ".shell { display: grid; } .card { padding: 12px; } .button { min-height: 44px; }"
    role_payloads = {
        "client": (
            "Записаться",
            "document.querySelector('form')?.addEventListener('submit', () => fetch('/api/zapis', { method: 'POST' }));",
        ),
        "specialist": (
            "День мастера",
            "document.querySelector('button')?.addEventListener('click', () => fetch('/api/zapis/1', { method: 'PATCH' }));",
        ),
        "manager": (
            "Управление",
            "document.querySelector('form')?.addEventListener('submit', () => fetch('/api/zapis', { method: 'POST' })); fetch('/api/zapis/1', { method: 'PATCH' });",
        ),
    }
    for role, (title, script) in role_payloads.items():
        role_dir = tmp_path / "miniapp" / "app" / "static" / role
        role_dir.mkdir(parents=True)
        (role_dir / "index.html").write_text(
            f"<main class='shell'><h1>{title}</h1><form><input name='name' /><button class='button'>Сохранить</button></form></main>",
            encoding="utf-8",
        )
        (role_dir / "app.js").write_text(script, encoding="utf-8")
        (role_dir / "styles.css").write_text(css, encoding="utf-8")

    issues, coverage, _neutral = CheckRunner._role_surface_issues(
        tmp_path,
        generation_mode=GenerationMode.FAST,
        acceptance_contract=contract,
    )

    assert any(issue.code == "platform.prompt_scale_route_pages_missing" for issue in issues)
    assert coverage["client"]["expected_route_count"] >= 2  # type: ignore[index]


def test_routeable_child_pages_must_be_wired_as_distinct_views(tmp_path: Path) -> None:
    css = ".shell { display: grid; } .card { padding: 12px; } .button { min-height: 44px; }"
    for role in ("client", "specialist", "manager"):
        role_dir = tmp_path / "miniapp" / "app" / "static" / role
        role_dir.mkdir(parents=True)
        (role_dir / "index.html").write_text(
            "<body><main class='shell'><form><input name='title' /><button class='button'>Сохранить</button></form></main></body>",
            encoding="utf-8",
        )
        (role_dir / "app.js").write_text(
            "document.querySelector('form')?.addEventListener('submit', () => fetch('/api/items', { method: 'POST' }));",
            encoding="utf-8",
        )
        (role_dir / "styles.css").write_text(css, encoding="utf-8")

    manager_root = tmp_path / "miniapp" / "app" / "static" / "manager"
    services_dir = manager_root / "services"
    services_dir.mkdir()
    (services_dir / "index.html").write_text(
        "<body data-view='services'><main><form><input name='price' /><button>Добавить услугу</button></form></main></body>",
        encoding="utf-8",
    )
    stylists_dir = manager_root / "stylists"
    stylists_dir.mkdir()
    (stylists_dir / "index.html").write_text(
        "<body data-view='stylists'><main><form><input name='name' /><button>Добавить мастера</button></form></main></body>",
        encoding="utf-8",
    )

    issues, coverage, _neutral = CheckRunner._role_surface_issues(
        tmp_path,
        generation_mode=GenerationMode.FAST,
    )

    assert any(issue.code == "platform.routeable_role_views_not_wired" for issue in issues)
    assert coverage["manager"]["status"] == "routeable_views_not_wired"  # type: ignore[index]


def test_role_action_signals_follow_prompt_role_flow() -> None:
    contract = {
        "required": True,
        "prompt_hints": {
            "role_state_contract": {
                "source_roles": ["manager"],
                "update_roles": ["specialist"],
                "observer_roles": ["client"],
            },
            "role_action_prompts": {
                "manager": ["publishes the shared board"],
                "client": ["filters and reads the shared board"],
                "specialist": ["updates fulfillment state"],
            },
        },
    }

    manager_source = """
    <form id="publish-form"><input name="name"><button>Publish</button></form>
    <script>fetch("/api/board", { method: "POST", body: JSON.stringify({ name: "x" }) })</script>
    """
    client_observer = """
    <select id="category-filter"></select>
    <section id="shared-board"></section>
    <script>fetch("/api/board").then(renderBoard)</script>
    """
    client_without_mutation = """
    <form id="filter-form"><input name="query"><button>Search</button></form>
    <script>fetch("/api/board").then(renderBoard)</script>
    """

    assert CheckRunner._role_action_signals("manager", manager_source, acceptance_contract=contract)
    assert CheckRunner._role_action_signals("client", client_observer, acceptance_contract=contract)
    assert not CheckRunner._role_action_signals(
        "specialist",
        client_without_mutation,
        acceptance_contract=contract,
    )
    dynamic_method_source = """
    <form id="publish-form"><input name="name"><button>Publish</button></form>
    <script>
      const method = state.editingId ? "PATCH" : "POST";
      publishForm.addEventListener("submit", () => fetch("/api/items", { method }));
    </script>
    """
    assert CheckRunner._role_action_signals("manager", dynamic_method_source, acceptance_contract=contract)


def test_openai_tool_step_extracts_response_function_calls() -> None:
    parsed = OpenAIClient._extract_response_tool_step(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Reading first."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_files",
                    "arguments": '{"targets":["miniapp/app/main.py"],"reason":"inspect"}',
                },
            ],
        }
    )

    assert parsed["assistant_message"] == "Reading first."
    assert parsed["tool_calls"][0]["tool"] == "read_files"  # type: ignore[index]
    assert parsed["tool_calls"][0]["tool_use_id"] == "call_1"  # type: ignore[index]


def test_openai_tool_result_messages_are_model_conversation_items() -> None:
    messages = [{"tool_use_id": "call_1", "tool": "read_files", "output": {"ok": True}}]

    response_items = OpenAIClient._responses_tool_result_items(messages)
    chat_items = OpenAIClient._chat_tool_result_messages(messages)

    assert response_items == [{"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}]
    assert chat_items[0]["role"] == "assistant"
    assert chat_items[0]["tool_calls"][0]["id"] == "call_1"  # type: ignore[index]
    assert chat_items[1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}


def test_agent_transcript_tracks_pending_tool_results_and_reduced_graph() -> None:
    transcript = AgentTranscriptStore()
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Read files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "read_files", "files": []}])

    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"
    assert snapshot["counts"]["model_turn"] == 1
    assert snapshot["counts"]["tool_result"] == 1
    assert snapshot["reduced_graph"][0]["event_type"] == "model_turn"


def test_agent_transcript_does_not_feed_unlinked_internal_results_to_model() -> None:
    transcript = AgentTranscriptStore()

    transcript.append_tool_results("run_1", [{"tool": "internal_repair_packet", "output": {"next": "patch"}}])
    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["tool_result_messages"] == []
    assert snapshot["counts"]["tool_result_unlinked"] == 1


def test_agent_transcript_drops_partial_tool_outputs_before_responses_resume() -> None:
    transcript = AgentTranscriptStore()
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Read files.",
        tool_calls=[
            {"tool_use_id": "call_1", "tool": "read_files"},
            {"tool_use_id": "call_2", "tool": "search_files"},
        ],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "read_files", "files": []}])

    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["previous_response_id"] is None
    assert context["tool_result_messages"] == []
    assert snapshot["counts"]["tool_result_context_incomplete"] == 1
    assert snapshot["last_response_id"] is None


def test_agent_transcript_persists_and_restores_pending_tool_results() -> None:
    writes: list[dict[str, object]] = []
    transcript = AgentTranscriptStore()
    transcript.configure_persistence("run_1", writer=lambda payload: writes.append(payload))
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Search files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "search_files"}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "search_files", "matches": []}])

    restored = AgentTranscriptStore()
    restored.restore("run_1", writes[-1])
    context = restored.next_model_context("run_1")

    assert len(writes) >= 3
    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"


def test_agent_edit_validator_rejects_unsafe_or_invalid_file_changes() -> None:
    unsafe = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[DraftAction(file_path="../miniapp/app/main.py", operation="replace", content="x", reason="bad")],
        )
    )
    invalid_patch = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[
                DraftAction(
                    file_path="miniapp/app/static/client/app.js",
                    operation="patch",
                    diff="not a unified diff",
                    reason="bad",
                )
            ],
        )
    )

    assert unsafe.failure_signature == "generation.invalid_edit_operation:unsafe_path"
    assert invalid_patch.failure_signature == "generation.invalid_edit_operation:invalid_patch_diff"
    assert unsafe.metadata["repair_packets"][0]["code"] == "unsafe_path"
    assert invalid_patch.metadata["repair_packets"][0]["required_next_tool"] in {"read_files", "write_file"}


def test_agent_edit_validator_protects_generated_artifacts_without_retrying_them() -> None:
    plan = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[
                DraftAction(
                    file_path="miniapp/app/generated/route_manifest.json",
                    operation="replace",
                    content="{}",
                    reason="bad",
                ),
                DraftAction(
                    file_path="miniapp/app/static/manager/app.js",
                    operation="replace",
                    content="console.log('ok');",
                    reason="source repair",
                ),
            ],
        )
    )

    packet = plan.metadata["repair_packets"][0]

    assert plan.failure_signature == "generation.invalid_edit_operation:protected_path"
    assert packet["code"] == "protected_path"
    assert packet["retryable"] is False
    assert "miniapp/app/generated/route_manifest.json" not in packet["target_files"]
    assert packet["target_files"] == ["miniapp/app/static/manager/app.js"]


def test_protected_route_repair_targets_allowed_role_sources() -> None:
    plan = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[
                DraftAction(
                    file_path="miniapp/app/routes/role_routes.py",
                    operation="replace",
                    content="bad",
                    reason="bad",
                )
            ],
        )
    )
    packet = plan.metadata["repair_packets"][0]
    decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 1},
        latest_files_read=[],
    )

    assert packet["code"] == "protected_path"
    assert "miniapp/app/routes/role_routes.py" in packet["forbidden_target_files"]
    assert "miniapp/app/routes/role_routes.py" not in packet["target_files"]
    assert "miniapp/app/static/client/app.js" in packet["target_files"]
    assert decision.active is True
    assert decision.forced_tool_names == ["read_files"]
    assert "miniapp/app/routes/role_routes.py" in decision.next_forced_action["forbidden_target_files"]


def test_agent_file_state_cache_reports_freshness(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True)
    target.write_text("console.log('a');\n", encoding="utf-8")
    cache = AgentFileStateCache()

    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "unread"
    assert cache.read(run_id="run_1", root=root, path="miniapp/app/static/client/app.js", read_text=lambda path: (root / path).read_text(encoding="utf-8"))
    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "fresh"
    target.write_text("console.log('b');\n", encoding="utf-8")
    assert cache.freshness(run_id="run_1", root=root, path="miniapp/app/static/client/app.js")["status"] == "stale"


def test_repair_transition_policy_forces_read_then_write() -> None:
    packet = AgentEditValidator.repair_packet_for_issue(
        code="invalid_patch_diff",
        message="bad patch",
        file_changes=[
            DraftAction(
                file_path="miniapp/app/static/client/app.js",
                operation="patch",
                diff="bad",
                reason="test",
            )
        ],
        repeated_count=2,
    )

    read_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=[],
    )
    write_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=["miniapp/app/static/client/app.js"],
    )

    assert read_decision.active is True
    assert read_decision.forced_tool_names == ["read_files"]
    assert write_decision.forced_tool_names == ["write_file"]


def test_repair_transition_policy_allows_patch_after_non_patch_conflict_read() -> None:
    packet = AgentEditValidator.repair_packet_for_issue(
        code="old_string_not_found",
        message="old string missing",
        file_changes=[
            DraftAction(
                file_path="miniapp/app/static/client/app.js",
                operation="patch",
                diff="bad",
                reason="test",
            )
        ],
        repeated_count=2,
    )

    write_decision = RepairTransitionPolicy.decide(
        repair_packets=[packet],
        repeated_failure_signatures={str(packet["failure_signature"]): 2},
        latest_files_read=["miniapp/app/static/client/app.js"],
    )

    assert write_decision.forced_tool_names == ["write_file", "apply_patch_to_draft"]


def test_repair_catalog_returns_operational_packets() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "check": "frontend_interaction_static_smoke",
            "details": "workflow_patch_payload_field_mismatch: frontend sends PATCH fields not accepted",
            "paths": ["miniapp/app/static/manager/app.js"],
        }
    )

    assert packet["signature"] == "workflow.payload_schema_mismatch"
    assert packet["required_next_tool"] == "read_files"
    assert packet["verification_command"]
    assert packet["deterministic"] is True


def test_repair_catalog_uncatalogued_failure_creates_evidence_case_packet() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "check": "custom_quality_check",
            "details": "A prompt-specific invariant assertion failed.",
            "paths": ["miniapp/app/main.py"],
        }
    )

    assert packet["signature"] == "repair.uncatalogued_repair_case:custom_quality_check"
    assert packet["failure_class"] == "custom_quality_check"
    assert packet["required_next_tool"] == "read_files"
    assert packet["retry_policy"] == "evidence_driven_repair_case"
    assert "evidence-driven repair case" in packet["instruction"]


def test_repair_catalog_embedded_connectivity_recipe_has_precise_targets() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "kind": "check_failure",
            "check": "connectivity_validators",
            "evidence": {
                "logs": [
                    json.dumps(
                        {
                            "code": "connectivity.missing_backend_route",
                            "message": "miniapp/app/static/specialist/app.js references GET /api/flows/{param} but the matching backend route is missing.",
                            "severity": "high",
                            "location": "miniapp/app/routes",
                            "blocking": True,
                            "repair_recipe": {
                                "frontend_ref": "miniapp/app/static/specialist/app.js: GET /api/flows/{param}",
                                "expected_route": "GET /api/flows/{param}",
                                "suggested_patch_target": "miniapp/app/routes",
                            },
                        }
                    )
                ]
            },
        }
    )

    assert packet["signature"] == "connectivity.missing_backend_route"
    assert "miniapp/app/static/specialist/app.js" in packet["target_files"]
    assert "miniapp/app/routes/api.py" in packet["target_files"]
    assert packet["next_forced_action"]["target_files"] == packet["target_files"]


def test_runtime_syncs_missing_api_route_from_frontend_contract(tmp_path: Path) -> None:
    source = tmp_path / "source"
    routes = source / "miniapp/app/routes"
    routes.mkdir(parents=True)
    (routes / "__init__.py").write_text("", encoding="utf-8")
    tests = source / "miniapp/tests"
    tests.mkdir(parents=True)
    (tests / "test_generated_app.py").write_text(
        "import app.routes.health as health\n\nold = health.STORE_PATH\n",
        encoding="utf-8",
    )

    class FakeWorkspaceService:
        def draft_source_dir(self, workspace_id: str, run_id: str) -> Path:
            return source

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str) -> str | None:
            target = source / path
            return target.read_text(encoding="utf-8") if target.exists() else None

    runtime = WorkspaceCodeAgentRuntime.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()
    changes = [
        DraftAction(
            file_path="miniapp/app/static/client/app.js",
            operation="create",
            content="await fetch('/api/zapis', { method: 'POST', body: JSON.stringify({}) });\n",
            reason="client creates a reservation",
        )
    ]

    synced = WorkspaceCodeAgentRuntime._sync_required_api_route_file_changes(
        runtime,
        changes,
        workspace_id="ws",
        run_id="run",
        acceptance_contract={"required_endpoints": [{"method": "GET", "path": "/api/zapis"}]},
    )

    route = next(change for change in synced if change.file_path == "miniapp/app/routes/zapis.py")
    assert 'router = APIRouter(prefix="/api/zapis"' in str(route.content)
    assert "STORE_PATH" in str(route.content)
    test_change = next(change for change in synced if change.file_path == "miniapp/tests/test_generated_app.py")
    assert "import app.routes.zapis as health" in str(test_change.content)


def test_frontend_form_wiring_issue_emits_exact_repair_packet(tmp_path: Path) -> None:
    static_root = tmp_path / "miniapp/app/static"
    manager_dir = static_root / "manager"
    specialist_dir = static_root / "specialist"
    client_dir = static_root / "client"
    tests_dir = tmp_path / "miniapp/tests"
    for path in (manager_dir, specialist_dir, client_dir, tests_dir):
        path.mkdir(parents=True, exist_ok=True)
    (manager_dir / "index.html").write_text(
        '<form id="entry-form"><input name="title"><button type="submit">Save</button></form>',
        encoding="utf-8",
    )
    (manager_dir / "app.js").write_text(
        "async function saveOther(){ await fetch('/api/entries', {method: 'POST', body: JSON.stringify({title: 'x'})}); }\n",
        encoding="utf-8",
    )
    (specialist_dir / "index.html").write_text("<button id='mark'>Mark</button>", encoding="utf-8")
    (specialist_dir / "app.js").write_text(
        "async function mark(id){ await fetch(`/api/entries/${id}`, {method: 'PATCH', body: JSON.stringify({review_note: 'done'})}); }\n",
        encoding="utf-8",
    )
    (client_dir / "index.html").write_text("<main></main>", encoding="utf-8")
    (client_dir / "app.js").write_text("async function load(){ await fetch('/api/entries'); }\n", encoding="utf-8")
    (tests_dir / "generated_app.test.mjs").write_text("import test from 'node:test'; // post get update\n", encoding="utf-8")

    issues = CheckRunner._frontend_interaction_contract_issues(
        source_dir=tmp_path,
        contract={
            "required": True,
            "features": {"workflow_update": True},
            "prompt_hints": {
                "role_state_contract": {
                    "source_roles": ["manager"],
                    "update_roles": ["specialist"],
                    "observer_roles": ["client"],
                }
            },
        },
    )

    form_index = next(index for index, issue in enumerate(issues) if issue.code == "platform.workflow_form_without_handler")
    broad_index = next(index for index, issue in enumerate(issues) if issue.code == "platform.cross_role_update_not_rendered_in_role")
    assert form_index < broad_index
    issue = issues[form_index]
    packet = RepairCatalog.classify_issue(
        {
            "check": "frontend_interaction_static_smoke",
            "logs": [json.dumps(issue.model_dump(mode="json"))],
        }
    )
    assert packet["signature"] == "frontend.unwired_form"
    assert packet["suggested_tool_after_read"] == "write_file"
    assert packet["target_files"][:2] == [
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/app.js",
    ]


def test_generated_js_diagnostics_reject_brittle_route_manifest_root_assertion(tmp_path: Path) -> None:
    test_file = tmp_path / "generated_app.test.mjs"
    test_file.write_text(
        "import assert from 'node:assert/strict';\n"
        "assert.equal(manifest.roles.client.root, '/client');\n",
        encoding="utf-8",
    )

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        ["file:///workspace/miniapp/tests/generated_app.test.mjs:2:8"],
        test_file=test_file,
    )

    assert diagnostics["js_test_brittle_route_manifest_assertion"]["problem"] == "generated_js_test_requires_exact_route_manifest_root"
    assert "Do not edit generated/route_manifest.json" in diagnostics["js_test_brittle_route_manifest_assertion"]["expected_fix"]
    packet = RepairCatalog.classify_issue(
        {
            "check": "generated_app_js_tests",
            "diagnostics": diagnostics,
            "paths": ["miniapp/tests/generated_app.test.mjs"],
        }
    )
    assert packet["signature"] == "tests.js_brittle_route_manifest_root"
    assert packet["target_files"] == ["miniapp/tests/generated_app.test.mjs"]


def test_button_wiring_issue_emits_exact_repair_recipe(tmp_path: Path) -> None:
    source = '<button id="refreshNow" type="button">Refresh</button>'
    js_path = tmp_path / "miniapp/app/static/client/app.js"
    js_path.parent.mkdir(parents=True, exist_ok=True)
    js_path.write_text("console.log('ready');\n", encoding="utf-8")

    issues = CheckRunner._button_wiring_issues(
        "miniapp/app/static/client/index.html",
        js_path,
        source,
        js_path.read_text(encoding="utf-8"),
    )

    assert issues[0].repair_recipe is not None
    assert issues[0].repair_recipe["target_files"] == [
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/app.js",
    ]


def test_generated_js_browser_global_failure_gets_operational_packet(tmp_path: Path) -> None:
    test_file = tmp_path / "generated_app.test.mjs"
    test_file.write_text(
        "import { pathToFileURL } from 'node:url';\n"
        "await import(pathToFileURL(new URL('../app/static/specialist/app.js', import.meta.url).pathname));\n",
        encoding="utf-8",
    )

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "# /workspace/source/miniapp/app/static/specialist/app.js:2",
            "# const view = document.body.dataset.view || 'dashboard';",
            "#              ^",
            "# ReferenceError: document is not defined",
            "file:///workspace/miniapp/tests/generated_app.test.mjs:2:7",
        ],
        test_file=test_file,
    )
    packet = RepairCatalog.classify_issue(
        {
            "check": "generated_app_js_tests",
            "details": "Generated JS app tests failed for the draft miniapp.",
            "diagnostics": diagnostics,
            "paths": ["miniapp/tests/generated_app.test.mjs"],
        }
    )

    assert diagnostics["js_test_imports_browser_app_without_dom"]["missing_global"] == "document"
    assert packet["signature"] == "tests.js_browser_global_import"
    assert packet["target_files"] == ["miniapp/tests/generated_app.test.mjs"]


def test_generated_js_stale_selector_assertion_gets_operational_packet(tmp_path: Path) -> None:
    test_file = tmp_path / "generated_app.test.mjs"
    test_file.write_text(
        "import assert from 'node:assert/strict';\n"
        "assert.match(managerJs, /data-choose-id/);\n",
        encoding="utf-8",
    )

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        ["file:///workspace/miniapp/tests/generated_app.test.mjs:2:8"],
        test_file=test_file,
    )
    packet = RepairCatalog.classify_issue(
        {
            "check": "generated_app_js_tests",
            "details": "Generated JS app tests failed for the draft miniapp.",
            "diagnostics": diagnostics,
            "paths": ["miniapp/tests/generated_app.test.mjs"],
        }
    )

    assert diagnostics["stale_selector_assertion"]["problem"] == "generated_js_test_requires_exact_selector_literal"
    assert "do not edit app code or route metadata" in diagnostics["stale_selector_assertion"]["expected_fix"]
    assert packet["signature"] == "tests.js_stale_selector_assertion"
    assert packet["target_files"][0] == "miniapp/tests/generated_app.test.mjs"


def test_generated_python_assertion_failure_targets_api_contract_slice() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "check": "generated_app_python_tests",
            "details": "Generated Python app tests failed for the draft miniapp.",
            "logs": ["AssertionError: 'pending' != 'ready'"],
            "diagnostics": {
                "python_assertion_failures": [
                    {
                        "file_path": "miniapp/tests/test_generated_app.py",
                        "line": 41,
                        "source": "self.assertEqual(payload['state'], 'ready')",
                    }
                ]
            },
            "paths": ["miniapp/tests/test_generated_app.py"],
        }
    )

    assert packet["signature"] == "tests.python_api_contract_mismatch"
    assert "miniapp/app/routes/**" in packet["target_files"]
    assert "miniapp/tests/test_generated_app.py" in packet["target_files"]


def test_generated_python_test_blocks_platform_shell_reset_import(tmp_path: Path) -> None:
    backend_dir = tmp_path / "miniapp"
    tests_dir = backend_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_generated_app.py").write_text(
        "import unittest\n"
        "from app.routes.health import reset_items\n\n"
        "class GeneratedAppTestCase(unittest.TestCase):\n"
        "    def test_contract(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._run_python_app_tests(backend_dir, require_present=True)
    packet = RepairCatalog.classify_issue(
        {
            "check": result.name,
            "details": result.details,
            "diagnostics": result.diagnostics,
            "logs": result.logs,
            "paths": ["miniapp/tests/test_generated_app.py"],
        }
    )

    assert result.status == "failed"
    assert result.diagnostics["protected_platform_shell_import"]["module"] == "app.routes.health"
    assert packet["signature"] == "tests.python_protected_shell_import"
    assert "miniapp/tests/test_generated_app.py" in packet["target_files"]


def test_generated_python_import_error_from_platform_shell_is_stale_test_repair_signal() -> None:
    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        ["ImportError: cannot import name 'reset_items' from 'app.routes.health' (/tmp/source/miniapp/app/routes/health.py)"]
    )
    packet = RepairCatalog.classify_issue(
        {
            "check": "generated_app_python_tests",
            "details": "Generated Python app tests failed for the draft miniapp.",
            "diagnostics": diagnostics,
            "logs": ["ImportError: cannot import name 'reset_items' from 'app.routes.health'"],
            "paths": ["miniapp/tests/test_generated_app.py"],
        }
    )

    assert diagnostics["protected_platform_shell_import"]["imported"] == ["reset_items"]
    assert packet["signature"] == "tests.python_protected_shell_import"


def test_backend_import_name_error_targets_traceback_file() -> None:
    diagnostics = CheckRunner._extract_backend_import_diagnostics(
        [
            '  File "/workspace/source/miniapp/app/routes/api.py", line 12, in <module>',
            "    updated_at = mapped_column(onupdate=_now)",
            "NameError: name '_now' is not defined",
        ]
    )
    packet = RepairCatalog.classify_issue(
        {
            "check": "changed_files_static",
            "details": "Backend import smoke failed for the draft miniapp.",
            "diagnostics": diagnostics,
            "paths": ["miniapp/app/routes/api.py"],
        }
    )

    assert diagnostics["python_name_error"]["file_path"] == "miniapp/app/routes/api.py"
    assert packet["signature"] == "backend.python_name_error"
    assert packet["target_files"][0] == "miniapp/app/routes/api.py"


def test_diagnostics_delta_only_reports_changed_failures() -> None:
    previous = AgentDiagnosticsDelta.snapshot(
        [
            RunCheckResult(name="changed_files_static", status="failed", details="old", logs=["old"]),
            RunCheckResult(name="api_workflow_smoke", status="failed", details="same", logs=["same"]),
        ]
    )
    current = AgentDiagnosticsDelta.snapshot(
        [
            RunCheckResult(name="changed_files_static", status="failed", details="new", logs=["new"]),
            RunCheckResult(name="browser_flow_smoke", status="failed", details="added", logs=["added"]),
            RunCheckResult(name="api_workflow_smoke", status="failed", details="same", logs=["same"]),
        ]
    )

    delta = AgentDiagnosticsDelta.delta(previous, current)

    assert delta["status"] == "changed"
    assert [item["name"] for item in delta["added"]] == ["browser_flow_smoke"]
    assert delta["changed"][0]["current"]["name"] == "changed_files_static"
    assert delta["source_counts"]["browser_flow"] == 1
    assert delta["source_counts"]["api_workflow"] == 1


def test_diagnostics_delta_tracks_lsp_and_browser_sources() -> None:
    snapshot = AgentDiagnosticsDelta.snapshot(
        [
            RunCheckResult(name="frontend_build", status="failed", command="npm run build", details="tsc failed"),
            RunCheckResult(name="ruff", status="failed", command="ruff check miniapp/app", details="lint failed"),
            RunCheckResult(name="pyright", status="failed", command="pyright miniapp/app", details="type failed"),
            RunCheckResult(
                name="browser_flow_smoke",
                status="failed",
                details="console errors",
                diagnostics={"console_errors": ["ReferenceError"], "network_errors": ["/api/missing"]},
            ),
        ]
    )
    delta = AgentDiagnosticsDelta.delta({}, snapshot)

    assert delta["source_counts"]["tsc"] == 1
    assert delta["source_counts"]["ruff"] == 1
    assert delta["source_counts"]["pyright"] == 1
    assert delta["source_counts"]["browser_console"] == 1
    assert delta["source_counts"]["browser_network"] == 1


def test_safe_diagnostic_commands_are_scoped() -> None:
    assert validate_workspace_command("python -m unittest discover") is None
    assert validate_workspace_command("node --test tests/generated_app.test.mjs") is None
    assert validate_workspace_command("rg api miniapp/app") is None
    assert validate_workspace_command("npm install") is not None
    assert validate_workspace_command("rm -rf miniapp") is not None
    assert validate_workspace_command("curl https://example.com") is not None


def test_command_policy_returns_typed_decisions() -> None:
    allowed = decide_workspace_command("python -m py_compile miniapp/app/main.py")
    cat_read = decide_workspace_command("cat miniapp/app/main.py")
    miniapp_cwd = decide_workspace_command("cd miniapp && node --check tests/generated_app.test.mjs")
    denied = decide_workspace_command("npm install")
    examples = DEFAULT_COMMAND_POLICY.validation_examples()

    assert allowed.allowed is True
    assert allowed.action == "allow"
    assert cat_read.allowed is True
    assert cat_read.safety_class == "read_only"
    assert miniapp_cwd.allowed is True
    assert miniapp_cwd.cwd_policy == "miniapp"
    assert denied.allowed is False
    assert denied.action == "forbidden"
    assert denied.safety_class == "network"
    assert all(item["status"] == "passed" for item in examples)


def test_process_manager_streams_head_tail_and_rg_no_match_is_success(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    decision = decide_workspace_command("rg missing-token miniapp/app")

    result = AgentProcessManager().run(
        draft_source=root,
        command="rg missing-token miniapp/app",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
        progress_callback=lambda payload: events.append(payload),
    ).as_dict()

    assert result["exit_code"] == 1
    assert str(result["process_id"]).startswith("proc_")
    assert result["semantic_status"] == "no_matches"
    assert result["success"] is True
    assert any(event.get("status") == "started" for event in events)
    assert any(event.get("status") == "completed" for event in events)


def test_process_manager_keeps_completed_output_readable(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    manager = AgentProcessManager()
    decision = decide_workspace_command("python3 -m py_compile miniapp/app/main.py")

    result = manager.run(
        draft_source=root,
        command="python3 -m py_compile miniapp/app/main.py",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
    ).as_dict()
    output = manager.read_output(str(result["process_id"]), stream="stderr")

    assert result["success"] is True
    assert output["process_id"] == result["process_id"]
    assert manager.snapshot()["processes"][0]["status"] == "completed"  # type: ignore[index]


def test_process_manager_keeps_managed_process_readable_until_terminated(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    manager = AgentProcessManager()
    events: list[dict[str, object]] = []
    base_decision = decide_workspace_command("cat miniapp/app")
    decision = replace(
        base_decision,
        action="allow",
        reason="Test-managed stdin process.",
        argv=("cat",),
        resolved_argv=(shutil.which("cat") or "cat",),
    )

    def run_process() -> None:
        manager.run(
            draft_source=root,
            command="cat",
            decision=decision,
            timeout_seconds=0,
            max_output_chars=800,
            progress_callback=lambda payload: events.append(payload),
            process_id="proc_managed_test",
            yield_time_ms=100,
        )

    import threading

    thread = threading.Thread(target=run_process, daemon=True)
    thread.start()
    for _ in range(50):
        if any(event.get("status") == "started" for event in events):
            break
        time.sleep(0.02)

    assert manager.write_stdin("proc_managed_test", "hello managed\n") is True
    for _ in range(50):
        output = manager.read_output("proc_managed_test", stream="stdout", start=0)
        if "hello managed" in output["content"]:
            break
        time.sleep(0.02)
    else:
        output = manager.read_output("proc_managed_test", stream="stdout", start=0)

    assert output["status"] == "running"
    assert output["next_start"] >= len("hello managed\n")
    assert "hello managed" in output["content"]
    assert manager.terminate("proc_managed_test") is True
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert manager.snapshot()["processes"][0]["status"] == "completed"  # type: ignore[index]


def test_process_manager_writes_head_tail_output_artifact(tmp_path: Path) -> None:
    root = tmp_path
    app_dir = root / "miniapp/app"
    app_dir.mkdir(parents=True)
    (app_dir / "big.txt").write_text(
        "\n".join([f"needle head {index}" for index in range(80)] + ["needle tail error marker"]),
        encoding="utf-8",
    )
    written: list[dict[str, object]] = []

    def writer(payload: dict[str, object]) -> dict[str, object] | None:
        written.append(payload)
        return {
            "ref": f"exec_output:ws_1:run_1:{payload['process_id']}:{payload['stream']}:abc",
            "artifact_id": f"{payload['process_id']}:{payload['stream']}:abc",
            "stream": payload["stream"],
            "sha256": payload.get("sha256"),
            "chars": payload.get("total_chars"),
            "omitted_chars": (payload.get("head_tail") or {}).get("omitted_chars") if isinstance(payload.get("head_tail"), dict) else 0,
        }

    manager = AgentProcessManager()
    decision = decide_workspace_command("rg needle miniapp/app")
    result = manager.run(
        draft_source=root,
        command="rg needle miniapp/app",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=120,
        output_artifact_writer=writer,
    ).as_dict()

    assert result["success"] is True
    assert result["stdout_ref"].startswith("exec_output:ws_1:run_1:")
    assert result["stdout_sha256"]
    assert result["stdout_omitted_chars"] > 0
    assert "needle head" in result["stdout_head"]
    assert "tail error marker" in result["stdout_tail"]
    assert written[0]["content"] and "tail error marker" in str(written[0]["content"])


def test_head_tail_output_buffer_omits_middle() -> None:
    buffer = HeadTailOutputBuffer(max_chars=20)
    buffer.append("a" * 30)
    buffer.append("b" * 30)
    snapshot = buffer.snapshot()

    assert snapshot["total_chars"] == 60
    assert snapshot["omitted_chars"] > 0
    assert "omitted" in snapshot["excerpt"]


def test_context_pressure_recommends_compaction_for_large_payload() -> None:
    pressure = AgentContextPressureAnalyzer().analyze_payload(
        {
            "file_contexts": {"miniapp/app/main.py": "x" * 45_000},
            "tool_results": [{"tool": "run_command", "stdout": "y" * 45_000}],
            "agent_memory": {"notes": "z" * 25_000},
        }
    )

    assert pressure["compact_recommended"] is True
    assert {item["kind"] for item in pressure["suggestions"]} & {"narrow_file_context", "spill_tool_results", "compact_memory"}
    assert pressure["schema"] == "grounded.context_pressure_snapshot.v2"
    assert {"files", "tool_outputs", "memory", "diff", "skills", "checks", "prompt_contract", "full_payload"} <= set(pressure["sections"])
    assert {item["code"] for item in pressure["recommendations"]} & {"avoid_broad_file_reads", "use_artifact_ref", "compact_memory_context"}
    assert pressure["sections"]["files"]["top_contributors"][0]["label"] == "miniapp/app/main.py"


def test_context_packer_builds_retry_product_error_template_packs_without_noise() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=["miniapp/app/static/manager/app.js"],
        results=[
            RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="failed",
                details="selector #manager-submit is not wired",
                logs=["missing selector #manager-submit"],
                diagnostics={"failed_selector": "#manager-submit"},
            ),
            RunCheckResult(name="api_workflow_smoke", status="passed", details="ok"),
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )
    packs = AgentContextPacker.pack(
        user_prompt="Build role workflow",
        acceptance_contract={"roles": ["client", "specialist", "manager"], "flows": [{"id": "manager_update"}]},
        implementation_plan={"product_task_ledger": [{"task_id": "manager.workflow"}], "routeable_screen_plan": {"manager": ["/manager"]}},
        latest_execution=execution,
        file_contexts={
            "miniapp/app/static/manager/app.js": "const submit = document.querySelector('#manager-submit');",
            "miniapp/app/generated/route_manifest.json": "{}",
            "miniapp/app/static/client/app.js": "console.log('unrelated')",
        },
        latest_diff_summary="diff --git a/miniapp/app/static/manager/app.js b/miniapp/app/static/manager/app.js\n",
        repair_packets=[
            {
                "repair_class": "selector",
                "focused_patch_plan": {"allowed_files": ["miniapp/app/static/manager/app.js"]},
                "relevant_checks": [{"check": "frontend_interaction_static_smoke"}],
            }
        ],
        active_repair_case={
            "repair_class": "selector",
            "focused_patch_plan": {"allowed_files": ["miniapp/app/static/manager/app.js"]},
            "relevant_checks": [{"check": "frontend_interaction_static_smoke"}],
            "escalation": {"escalate_to": "full_repair"},
        },
        diagnostics_delta={"added_failures": ["frontend_interaction_static_smoke"]},
        repeated_no_progress=1,
        context_mode="minimal",
    )

    assert packs["schema"] == "grounded.agent_context_packs.v1"
    assert packs["mode"] == "retry"
    assert packs["product"]["schema"] == "grounded.product_context_pack.v1"
    assert packs["current_error"]["schema"] == "grounded.error_context_pack.v1"
    assert packs["template_invariants"]["schema"] == "grounded.template_invariants_pack.v1"
    assert packs["retry_policy"]["active"] is True
    assert packs["retry_policy"]["include_only"] == ["product_contract", "latest_diff", "failure_diagnostics", "relevant_source", "repair_recipe"]
    assert list(packs["retry_policy"]["relevant_source"]) == ["miniapp/app/static/manager/app.js"]
    assert "miniapp/app/generated/route_manifest.json" not in packs["retry_policy"]["relevant_source"]
    assert packs["current_error"]["failed_checks"][0]["name"] == "frontend_interaction_static_smoke"
    assert packs["current_error"]["repair_class"] == "selector"
    assert packs["retry_policy"]["escalation"]["escalate_to"] == "full_repair"


def test_context_pressure_accounts_for_budgeted_context_packs() -> None:
    pressure = AgentContextPressureAnalyzer().analyze_payload(
        {
            "context_packs": {
                "schema": "grounded.agent_context_packs.v1",
                "product": {"prompt_excerpt": "x" * 2000},
                "current_error": {"failed_checks": [{"details": "y" * 2000}]},
                "template_invariants": {"invariants": ["keep route metadata derived"]},
            }
        }
    )

    assert "context_packs" in pressure["sections"]
    assert pressure["section_tokens"]["context_packs"] > 0


def test_context_pressure_detects_duplicate_reads_from_transcript() -> None:
    transcript = {
        "events": [
            {
                "event_type": "model_turn",
                "payload": {
                    "tool_calls": [
                        {"tool_use_id": "read_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]},
                    ]
                },
            },
            {
                "event_type": "tool_call",
                "payload": {
                    "tool_use_id": "read_2",
                    "tool": "read_files",
                    "arguments": {"targets": ["miniapp/app/main.py"]},
                },
            },
            {
                "event_type": "tool_call",
                "payload": {
                    "tool_use_id": "read_3",
                    "tool": "read_files",
                    "arguments": {"targets": ["miniapp/app/static/client/app.js"]},
                },
            },
        ]
    }
    pressure = AgentContextPressureAnalyzer().analyze_transcript(
        transcript,
        current_file_contexts={"miniapp/app/main.py": "x" * 20_000, "miniapp/app/static/client/app.js": "y" * 80},
    )

    assert pressure["compact_recommended"] is True
    assert pressure["duplicate_file_reads"][0]["path"] == "miniapp/app/main.py"
    assert pressure["suggestions"][0]["kind"] == "avoid_duplicate_reads"
    assert pressure["suggestions"][0]["code"] == "avoid_reread_files"
    assert pressure["avoid_reread_files"][0]["path"] == "miniapp/app/main.py"


def test_context_pressure_detects_stale_paths_and_phase_budget_payload() -> None:
    payload_pressure = AgentContextPressureAnalyzer().analyze_payload(
        {
            "phase_budget": {
                "schema": "grounded.phase_token_cost_budget.v1",
                "phases": [{"phase": "editing", "status": "active", "token_budget": 1000, "tokens_used": 850}],
            },
            "stale_path_refs": [{"path": "miniapp/app/old.py", "suggested_path": "miniapp/app/main.py"}],
        }
    )
    transcript_pressure = AgentContextPressureAnalyzer().analyze_transcript(
        {
            "events": [
                {
                    "sequence": 7,
                    "event_type": "tool_call",
                    "payload": {
                        "tool_use_id": "read_old",
                        "tool": "read_files",
                        "arguments": {"targets": ["miniapp/app/old.py"]},
                    },
                }
            ]
        },
        path_exists=lambda path: False if path == "miniapp/app/old.py" else True,
        find_similar_path=lambda path: "miniapp/app/main.py" if path == "miniapp/app/old.py" else None,
    )

    assert payload_pressure["phase_budgets"][0]["phase"] == "editing"
    assert payload_pressure["stale_path_refs"][0]["suggested_path"] == "miniapp/app/main.py"
    assert transcript_pressure["stale_path_refs"][0]["last_sequence"] == 7
    assert transcript_pressure["stale_path_refs"][0]["suggested_path"] == "miniapp/app/main.py"
    assert transcript_pressure["suggestions"][0]["code"] == "refresh_stale_path_refs"


def test_hook_manager_records_lifecycle_events() -> None:
    hooks = AgentHookManager()
    hooks.record("run_1", "pre_tool_use", status="started", payload={"tool": "read_files"})
    hooks.record("run_1", "post_tool_use", status="completed", payload={"tool": "read_files"})
    snapshot = hooks.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["counts"]["pre_tool_use:started"] == 1
    assert snapshot["counts"]["post_tool_use:completed"] == 1


def test_coordinator_scratchpad_and_memory_compact_context(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        implementation_plan={"summary": "Build a role workflow", "roles": ["client"], "primary_entities": ["item"]},
    )
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_plan({"summary": "Build a role workflow", "roles": ["client"]}, coordinator.snapshot()["todo_plan"])  # type: ignore[arg-type]
    scratchpad.record_compact_boundary(
        plan={"summary": "Build a role workflow"},
        diff_summary="miniapp/app/main.py changed",
        failed_signatures=["ui_state_not_visible"],
        next_action="patch visible state rendering",
    )
    memory = AgentMemoryStore()
    memory.add("run_1", "reference", "Check miniapp/app/main.py and /api/entities before reuse.")
    stale_checks = memory.verify_stale_claims("run_1", root)

    assert coordinator.snapshot()["worker_specs"]
    assert coordinator.verification_completed() is False
    coordinator.complete_phase("browser_verifying")
    assert coordinator.verification_completed() is True
    assert "Build a role workflow" in scratchpad.snapshot()["files"]["plan.md"]  # type: ignore[index]
    assert scratchpad.snapshot()["compact_boundaries"][0]["failed_signatures"] == ["ui_state_not_visible"]  # type: ignore[index]
    assert stale_checks[0]["paths"][0]["exists"] is True  # type: ignore[index]


def test_coordinator_todo_gate_requires_real_build_and_verification() -> None:
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.FAST,
        implementation_plan={"summary": "Build a prompt-derived role workflow"},
    )

    assert coordinator.ready_to_finalize() is False
    coordinator.complete_phase("planning")
    coordinator.complete_phase("reading")
    coordinator.complete_phase("editing")
    coordinator.complete_phase("checking")
    assert coordinator.ready_to_finalize() is False
    assert coordinator.incomplete_required_todos()[0]["phase"] == "browser_verifying"
    coordinator.complete_phase("browser_verifying")

    assert coordinator.ready_to_finalize() is True


def test_scratchpad_stores_next_action_as_durable_file() -> None:
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_next_action(action="repair failed selector", reason="browser_proof_failed", payload={"selector": "#save"})

    snapshot = scratchpad.snapshot()

    assert snapshot["files"]["next_action.json"]["action"] == "repair failed selector"  # type: ignore[index]
    assert snapshot["files"]["next_action.json"]["payload"] == {"selector": "#save"}  # type: ignore[index]


def test_file_state_cache_reuses_and_invalidates_reads(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/main.py"
    target.parent.mkdir(parents=True)
    target.write_text("one\n", encoding="utf-8")
    cache = AgentFileStateCache()

    first = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    second = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    cache.invalidate("run_1", ["miniapp/app/main.py"])
    target.write_text("two\n", encoding="utf-8")
    third = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())

    assert first == "one\n"
    assert second == "one\n"
    assert third == "two\n"
    assert cache.snapshot("run_1")["entry_count"] == 1


def test_turn_diff_tracker_records_changed_lines(tmp_path: Path) -> None:
    class WorkspaceStub:
        def __init__(self) -> None:
            self.content = "alpha\n"

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str:
            del workspace_id, path, run_id
            return self.content

    class ApplyResult:
        status = "applied"
        conflict_reason = None

    workspace = WorkspaceStub()
    tracker = AgentTurnDiffTracker()
    tracker.capture_baseline(workspace_service=workspace, workspace_id="ws_1", run_id="run_1", turn=1, paths=["miniapp/app/main.py"])  # type: ignore[arg-type]
    workspace.content = "alpha\nbeta\n"
    record = tracker.record_result(
        workspace_service=workspace,  # type: ignore[arg-type]
        workspace_id="ws_1",
        run_id="run_1",
        turn=1,
        paths=["miniapp/app/main.py"],
        apply_result=ApplyResult(),
        owner_for_path=lambda path: "backend_api_worker",
    )

    assert record.changed_line_counts["miniapp/app/main.py"]["added"] == 1
    snapshot = tracker.snapshot("run_1")
    assert snapshot["turn_count"] == 1
    assert snapshot["records"][0]["has_product_runtime_diff"] is True  # type: ignore[index]
    assert snapshot["records"][0]["product_runtime_paths"] == ["miniapp/app/main.py"]  # type: ignore[index]
    assert isinstance(snapshot["records"][0]["diff_sha256"], str)  # type: ignore[index]


def test_semantic_scan_extracts_generic_routes_forms_and_handlers(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app/routes").mkdir(parents=True)
    (root / "miniapp/app/static/client").mkdir(parents=True)
    (root / "miniapp/app/routes/api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.post('/api/entities')\ndef create_entity():\n    return {}\n",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/index.html").write_text(
        "<form id='client-main-form'><input name='title'><button>Save</button></form><script src='app.js'></script>",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/app.js").write_text(
        "document.querySelector('#client-main-form')?.addEventListener('submit', () => fetch('/api/entities'));\n",
        encoding="utf-8",
    )

    result = semantic_scan(root=root, targets=["miniapp/app"])

    assert result["python"][0]["routes"][0]["name"] == "create_entity"  # type: ignore[index]
    assert result["html"][0]["forms"][0]["id"] == "client-main-form"  # type: ignore[index]
    assert "#client-main-form" in result["javascript"][0]["selectors"]  # type: ignore[index]


def test_worker_manager_rejects_conflicting_owned_edits() -> None:
    report = AgentWorkerManager.validate_non_conflicting(
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="first"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="second"),
        ]
    )

    assert report["ok"] is False
    assert report["conflicts"][0]["path"] == "miniapp/app/static/client/app.js"  # type: ignore[index]


def test_worker_manager_rejects_forbidden_worker_paths() -> None:
    report = AgentWorkerManager.validate_non_conflicting(
        [
            DraftAction(
                file_path="miniapp/app/static/manager/app.js",
                operation="replace",
                content="console.log('manager');\n",
                reason="[client_surface_worker] wrong role",
            ),
        ]
    )

    assert report["ok"] is False
    assert report["forbidden"][0]["path"] == "miniapp/app/static/manager/app.js"  # type: ignore[index]
    assert report["conflict_report"]["forbidden_count"] == 1  # type: ignore[index]


def test_worker_manager_reports_disjoint_write_scopes() -> None:
    default_report = AgentWorkerManager.write_scope_report()
    overlapping_report = AgentWorkerManager.write_scope_report(
        [
            {
                "worker_id": "client_surface_worker",
                "branch_role": "writer",
                "owner_scope": "client",
                "ownership": {"allowed_paths": ["miniapp/app/static/client"], "forbidden_paths": [], "exclusive_write": True},
            },
            {
                "worker_id": "client_child_worker",
                "branch_role": "writer",
                "owner_scope": "client child",
                "ownership": {"allowed_paths": ["miniapp/app/static/client/details"], "forbidden_paths": [], "exclusive_write": True},
            },
            {
                "worker_id": "mobile_polish_worker",
                "branch_role": "verifier",
                "ownership": {"allowed_paths": ["miniapp/app/static/client"], "exclusive_write": False},
            },
        ]
    )

    assert default_report["status"] == "passed"
    assert default_report["disjoint"] is True
    assert overlapping_report["status"] == "conflict"
    assert overlapping_report["overlaps"][0]["left_worker"] == "client_surface_worker"  # type: ignore[index]


def test_worker_manager_maps_serial_coordinator_edits_to_path_owner() -> None:
    report = AgentWorkerManager.validate_non_conflicting(
        [
            DraftAction(
                file_path="miniapp/tests/generated_app.test.mjs",
                operation="patch",
                diff="@@\n-a\n+b\n",
                reason="[coordinator] repair stale generated JS acceptance test",
            ),
            DraftAction(
                file_path="miniapp/app/static/manager/app.js",
                operation="patch",
                diff="@@\n-a\n+b\n",
                reason="[coordinator] repair manager workflow handler",
            ),
        ]
    )

    assert report["ok"] is True
    assert report["owners"]["miniapp/tests/generated_app.test.mjs"] == "test_verifier_worker"  # type: ignore[index]
    assert report["owners"]["miniapp/app/static/manager/app.js"] == "manager_surface_worker"  # type: ignore[index]


def test_progress_does_not_jump_for_metadata_only_patch() -> None:
    stage, progress = WorkspaceCodeAgentRuntime._run_progress_for_event(
        "patch_apply_completed",
        details={
            "changed_files": [
                "miniapp/app/generated/miniapp_contract.json",
                "miniapp/app/generated/route_manifest.json",
            ],
            "has_file_edits": True,
        },
    )

    assert stage.startswith("Patch applied")
    assert progress <= 20


def test_progress_reserves_ninety_percent_for_apply_not_repair() -> None:
    _, repair_progress = WorkspaceCodeAgentRuntime._run_progress_for_event(
        "agent_turn_started",
        details={
            "attempt": 4,
            "phase": "model_request",
            "has_draft_diff": True,
            "changed_files": ["miniapp/tests/generated_app.test.mjs"],
        },
    )
    _, final_check_progress = WorkspaceCodeAgentRuntime._run_progress_for_event(
        "final_checks_started",
        details={
            "has_file_edits": True,
            "has_draft_diff": True,
            "changed_files": ["miniapp/app/static/client/app.js"],
        },
    )
    _, apply_progress = WorkspaceCodeAgentRuntime._run_progress_for_event("apply_started", details={})

    assert repair_progress < 75
    assert final_check_progress < 75
    assert apply_progress >= 90


def test_quality_mode_uses_real_worker_branch_plan_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GROUNDED_ENABLE_WORKER_BRANCHES", raising=False)
    plan = {"prompt_contract_v1": {"required": True, "entities": ["entity"], "flows": [{"roles": ["client"]}]}}
    for mode in (GenerationMode.FAST, GenerationMode.BALANCED):
        mailbox = AgentWorkerManager.mailbox_for_plan(
            generation_mode=mode,
            implementation_plan=plan,
        )
        prompt_payload = WorkspaceCodeAgentRuntime._worker_branching_prompt_payload(
            generation_mode=mode,
            implementation_plan=plan,
        )

        assert mailbox["enabled"] is False
        assert mailbox["write_coordination"] == "serial_contract_runtime_writes"
        assert mailbox["disabled_reason"]
        assert all(worker["status"] == "available_disabled" for worker in mailbox["workers"])  # type: ignore[index]
        assert prompt_payload["enabled"] is False
    quality_mailbox = AgentWorkerManager.mailbox_for_plan(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan=plan,
    )
    no_contract_mailbox = AgentWorkerManager.mailbox_for_plan(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan={},
    )
    assert quality_mailbox["enabled"] is True
    assert quality_mailbox["branch_schema"] == "grounded.worker_branch_plan.v2"
    assert quality_mailbox["write_coordination"] == "parallel_owned_branches"
    assert quality_mailbox["worker_groups"]["writer"] == [  # type: ignore[index]
        "backend_api_worker",
        "client_surface_worker",
        "specialist_surface_worker",
        "manager_surface_worker",
        "test_verifier_worker",
    ]
    assert quality_mailbox["worker_groups"]["verifier"] == ["mobile_polish_worker"]  # type: ignore[index]
    assert quality_mailbox["execution_stages"][0]["stage"] == "backend_contract"  # type: ignore[index]
    assert quality_mailbox["subagent_contract"]["schema"] == "grounded.subagent_fork_contract.v1"
    assert quality_mailbox["subagent_contract"]["execution_order"] == ["planner", "backend", "frontend-role-ui", "tests", "verifier", "polish", "repair"]
    verifier_lane = next(lane for lane in quality_mailbox["subagent_contract"]["lanes"] if lane["lane_id"] == "verifier")
    assert verifier_lane["file_scope"]["exclusive_write"] is False
    assert "patch_files" not in verifier_lane["tool_allowlist"]
    assert no_contract_mailbox["enabled"] is False
    production_mailbox = AgentWorkerManager.mailbox_for_plan(
        generation_mode=GenerationMode.PRODUCTION,
        implementation_plan=plan,
    )
    assert production_mailbox["enabled"] is True
    assert production_mailbox["worker_groups"]["verifier"] == ["mobile_polish_worker"]  # type: ignore[index]
    assert any(worker["worker_id"] == "backend_api_worker" for worker in quality_mailbox["workers"])  # type: ignore[index]
    assert any(worker["worker_id"] == "mobile_polish_worker" and worker["branch_role"] == "verifier" for worker in quality_mailbox["workers"])  # type: ignore[index]
    assert all(worker["alias_ids"] == [] for worker in quality_mailbox["workers"])  # type: ignore[index]
    monkeypatch.setenv("GROUNDED_ENABLE_WORKER_BRANCHES", "0")
    disabled_quality = AgentWorkerManager.mailbox_for_plan(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan=plan,
    )
    assert disabled_quality["enabled"] is False
    assert "GROUNDED_ENABLE_WORKER_BRANCHES=0" in disabled_quality["disabled_reason"]


def test_hook_manager_records_context_and_blocks_forbidden_tool() -> None:
    manager = AgentHookManager()

    failed = manager.run("run_1", "on_check_failed", payload={"failed_checks": ["browser_flow_smoke"]})
    blocked = manager.run("run_1", "pre_tool_use", payload={"risk": "forbidden", "tool": "shell.exec"})
    snapshot = manager.snapshot("run_1")

    assert failed.should_block is False
    assert failed.additional_contexts
    assert blocked.should_block is True
    assert blocked.block_reason
    assert snapshot["counts"]["pre_tool_use:failed"] == 1


def test_hook_policy_service_validates_and_evaluates_workspace_rules(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = HookPolicyService(store)
    report = service.update_workspace_policy(
        "ws_1",
        {
            "policy_id": "workspace-test",
            "rules": [
                {
                    "rule_id": "block_write",
                    "conditions": {"hook": "pre_tool_use", "canonical_tool": "file.write"},
                    "actions": [{"action": "block", "reason": "Writes are blocked in this workspace."}],
                },
                {
                    "rule_id": "apply_context",
                    "conditions": {"hook": "before_apply", "path_globs": ["miniapp/app/*.py"]},
                    "actions": [{"action": "add_context", "text": "Review app route changes before apply.", "priority": 7, "target": "repair_turn"}],
                },
                {
                    "rule_id": "secret_context",
                    "conditions": {"hook": "before_apply"},
                    "actions": [{"action": "add_context", "text": "sk-testsecret000000000000000000"}],
                },
            ],
        },
    )

    blocked = service.evaluate(
        HookContext(
            hook="pre_tool_use",
            workspace_id="ws_1",
            run_id="run_1",
            payload={"tool": "file.write", "model_tool": "write_file", "risk": "mutating"},
        )
    )
    context = service.evaluate(
        HookContext(
            hook="before_apply",
            workspace_id="ws_1",
            run_id="run_1",
            payload={"paths": ["miniapp/app/main.py"]},
        )
    )

    assert report["validation_issues"][0]["code"] == "secret_like_content"
    assert report["policy"]["rules"][2]["enabled"] is False
    assert report["policy"]["rules"][2]["actions"][0]["text"] is None
    assert blocked.should_block is True
    assert blocked.block_reason == "Writes are blocked in this workspace."
    assert blocked.matched_rules[0]["rule_id"] == "block_write"
    assert [item.text for item in context.added_contexts] == ["Review app route changes before apply."]
    assert all(item.source_rule_id != "secret_context" for item in context.added_contexts)


def test_hook_manager_uses_configured_policy_in_snapshot(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    service = HookPolicyService(store)
    service.update_workspace_policy(
        "ws_1",
        {
            "rules": [
                {
                    "rule_id": "protect_routes",
                    "conditions": {"hook": "before_apply", "path_globs": ["miniapp/app/routes/*.py"]},
                    "actions": [{"action": "block", "reason": "Route changes require manual review."}],
                }
            ]
        },
    )
    manager = AgentHookManager(policy_service=service)

    outcome = manager.run(
        "run_1",
        "before_apply",
        payload={"workspace_id": "ws_1", "paths": ["miniapp/app/routes/orders.py"]},
    )
    snapshot = manager.snapshot("run_1")

    assert outcome.should_block is True
    assert outcome.block_reason == "Route changes require manual review."
    assert snapshot["blocked_count"] == 1
    assert snapshot["matched_rules"][0]["rule_id"] == "protect_routes"


def test_worker_task_planner_builds_self_contained_owner_prompts() -> None:
    tasks = AgentWorkerTaskPlanner.worker_tasks(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan={
            "principle": "plan_inspect_build_verify_repair_final_browser_proof",
            "primary_entities": ["entity"],
            "product_task_ledger": [
                {"id": "client.role_surface", "role": "client", "kind": "source", "expected_min_routes": 2},
                {"id": "shared_state.persistence_api", "role": "shared", "kind": "backend"},
                {"id": "proof.generated_and_browser", "role": "shared", "kind": "proof"},
            ],
        },
    )

    by_id = {task["worker_id"]: task for task in tasks}

    assert "client_surface_worker" in by_id
    assert by_id["client_surface_worker"]["alias_ids"] == []
    assert by_id["client_surface_worker"]["branch_role"] == "writer"
    assert "patch_files" in by_id["client_surface_worker"]["tool_allowlist"]
    assert by_id["mobile_polish_worker"]["branch_role"] == "verifier"
    assert "patch_files" not in by_id["mobile_polish_worker"]["tool_allowlist"]
    assert by_id["mobile_polish_worker"]["isolated_branch"] is False
    assert "blocker findings" in by_id["mobile_polish_worker"]["prompt"]
    assert "Own only these paths" in by_id["client_surface_worker"]["prompt"]
    assert "Forbidden paths" in by_id["client_surface_worker"]["prompt"]
    assert "Branch role is writer" in by_id["client_surface_worker"]["prompt"]
    assert "Product task ledger slice" in by_id["client_surface_worker"]["prompt"]
    assert by_id["client_surface_worker"]["product_task_ledger_slice"][0]["id"] == "client.role_surface"
    assert by_id["backend_api_worker"]["product_task_ledger_slice"][0]["id"] == "shared_state.persistence_api"
    assert "miniapp/app/generated/miniapp_contract.json" in by_id["client_surface_worker"]["prompt"]
    assert "keep them consistent across backend, JS payloads, renderers, and tests" in by_id["client_surface_worker"]["prompt"]
    assert by_id["client_surface_worker"]["mode_contract"]["depth"] == "deep"
    assert "mobile layout works" in by_id["client_surface_worker"]["self_check"][3]


def test_worker_runtime_prepares_isolated_drafts_and_merge_reports(tmp_path: Path) -> None:
    source = tmp_path / "draft"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('ready');\n", encoding="utf-8")
    runtime = AgentWorkerRuntime()

    prepared = runtime.prepare(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        draft_source=source,
        worker_specs=[
            {"worker_id": "client_surface_worker", "owner_scope": "client role app"},
            {"worker_id": "manager_surface_worker", "owner_scope": "manager role app"},
        ],
    )
    report = runtime.merge_report(
        "run_1",
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="two"),
        ],
    )

    assert prepared["enabled"] is True
    assert prepared["schema"] == "grounded.worker_drafts.v2"
    assert len(prepared["workers"]) == 2  # type: ignore[arg-type]
    assert Path(prepared["workers"][0]["source_dir"]).exists()  # type: ignore[index]
    assert prepared["workers"][0]["agent_loop_ref"] == "worker_agent_loop:run_1:client_surface_worker"  # type: ignore[index]
    assert prepared["workers"][0]["branch_policy"] == "isolated_draft_writer"  # type: ignore[index]
    assert prepared["workers"][0]["write_scope"]["allowed_paths"] == ["miniapp/app/static/client"]  # type: ignore[index]
    assert prepared["workers"][0]["tool_allowlist"] == []  # type: ignore[index]
    assert prepared["write_scope_report"]["status"] == "passed"  # type: ignore[index]
    assert report["status"] == "conflict"
    assert report["conflict_report"]["blocked_paths"] == ["miniapp/app/static/client/app.js"]
    assert report["post_merge_verifier"]["status"] == "blocked_until_conflicts_resolved"

    branch_results = runtime.record_branch_results(
        "run_1",
        [DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one")],
    )

    assert branch_results[0]["agent_loop"] == "branch_scoped"
    assert branch_results[0]["self_check"]["owned_paths_only"] is True  # type: ignore[index]


def test_worker_merge_decisions_emit_repair_worker_packets() -> None:
    packets = WorkspaceCodeAgentRuntime._repair_packets_from_worker_decisions(
        [
            {
                "worker_id": "client_surface_worker",
                "decision": "rejected",
                "changed_files": ["miniapp/app/static/client/app.js"],
                "blocked_paths": ["miniapp/app/static/client/app.js"],
                "failure_reason": "conflicting owned diff",
                "repair_worker_required": True,
            },
            {
                "worker_id": "backend_api_worker",
                "decision": "accepted",
                "changed_files": ["miniapp/app/routes/api.py"],
                "repair_worker_required": False,
            },
        ]
    )

    assert packets[0]["failure_class"] == "worker_merge"
    assert packets[0]["failure_signature"] == "worker_merge.client_surface_worker.rejected"
    assert packets[0]["target_files"] == ["miniapp/app/static/client/app.js"]
    assert packets[0]["required_next_tool"] == "read_files"


def test_worker_runtime_prepares_workspace_branch_run_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")

    class WorkspaceStub:
        def clone_draft(self, workspace_id: str, source_run_id: str, target_run_id: str) -> Path:
            del workspace_id, source_run_id
            target = tmp_path / target_run_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            return target

    runtime = AgentWorkerRuntime()
    prepared = runtime.prepare_workspace_branches(
        workspace_id="ws",
        run_id="run_main",
        generation_mode=GenerationMode.BALANCED,
        workspace_service=WorkspaceStub(),
        worker_specs=[{"worker_id": "client_surface_worker", "owner_scope": "client role app"}],
    )

    worker = prepared["workers"][0]  # type: ignore[index]
    assert prepared["isolation"] == "workspace_draft_clone_per_worker"
    assert worker["branch_run_id"] == "run_main__worker__client_surface_worker"
    assert worker["base_run_id"] == "run_main"
    assert worker["branch_kind"] == "workspace_draft_clone"
    assert worker["merge_base_ref"] == "coordinator_draft:ws:run_main"
    assert Path(worker["source_dir"], "miniapp/app/static/client/app.js").exists()


def test_worker_branch_loop_runs_own_tool_transcript_and_patch(tmp_path: Path) -> None:
    branch_source = tmp_path / "branch"
    (branch_source / "miniapp/app/static/client").mkdir(parents=True)
    (branch_source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")
    (branch_source / "miniapp/app/static/client/index.html").write_text(
        "<main><form id='main-form'><button type='submit'>Save</button></form></main>",
        encoding="utf-8",
    )
    (branch_source / "miniapp/app/static/client/styles.css").write_text(
        ".page{display:block}.card{padding:12px}.button{min-height:44px}\n",
        encoding="utf-8",
    )
    for slug in ("list", "detail"):
        page_dir = branch_source / "miniapp/app/static/client" / slug
        page_dir.mkdir(parents=True)
        (page_dir / "index.html").write_text("<main><button>Open</button></main>", encoding="utf-8")

    class OpenAIStub:
        def generate_agent_tool_step(self, **_: object) -> dict[str, object]:
            return {
                "model": "test-model",
                "payload": {
                    "assistant_message": "client branch edited",
                    "response_id": "resp_worker_1",
                    "tool_calls": [
                        {
                            "tool": "write_file",
                            "tool_use_id": "call_1",
                            "file_path": "miniapp/app/static/client/app.js",
                            "content": (
                                "document.querySelector('#main-form')?.addEventListener('submit', event => {"
                                "event.preventDefault(); fetch('/api/entities', { method: 'POST', body: '{}' }); });\n"
                            ),
                            "reason": "create owned client behavior",
                        }
                    ],
                },
                "cache_stats": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            }

    class WorkspaceStub:
        def __init__(self) -> None:
            self.written: list[str] = []

        def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:
            del workspace_id, run_id
            return [{"path": "miniapp/app/static/client/app.js", "type": "file"}]

        def try_read_text_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            path = branch_source / relative_path
            return path.read_text(encoding="utf-8") if path.exists() else None

        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return "\n".join(f"diff --git a/{path} b/{path}" for path in self.written)

        def build_patch_envelope_for_file_changes(self, workspace_id: str, run_id: str, file_changes: list[DraftAction]) -> list[DraftAction]:
            del workspace_id, run_id
            return file_changes

        def apply_patch_envelope_to_draft(self, workspace_id: str, run_id: str, envelope: list[DraftAction]) -> ApplyPatchResult:
            del run_id
            for change in envelope:
                target = branch_source / change.file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(change.content or ""), encoding="utf-8")
                self.written.append(change.file_path)
            return ApplyPatchResult(workspace_id=workspace_id, run_id="branch", status="applied", changed_files=list(self.written))

    workspace = WorkspaceStub()
    result = AgentWorkerBranchLoop(openai_client=OpenAIStub(), workspace_service=workspace).run(  # type: ignore[arg-type]
        workspace_id="ws",
        parent_run_id="run_main",
        branch_run_id="run_main__worker__client_surface_worker",
        branch_source=branch_source,
        generation_mode=GenerationMode.FAST,
        model_profile="",
        user_prompt="Build a role-separated mobile mini-app.",
        worker_task={"worker_id": "client_surface_worker", "owner_scope": "client role app"},
        worker_prefix={"plan": "prompt-derived"},
        max_steps=1,
    )

    assert result.status == "changes_ready"
    assert result.transcript["counts"]["model_turn"] == 1
    assert result.transcript["counts"]["file_change"] == 1
    assert result.changed_files == ["miniapp/app/static/client/app.js"]
    assert "method: 'POST'" in (branch_source / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")


def test_verification_worker_requires_real_browser_proof() -> None:
    report = VerificationWorker.verify(
        latest_execution=None,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    assert report["status"] == "failed"
    assert any(issue["kind"] == "missing_required_proof" for issue in report["issues"])  # type: ignore[index]


def test_verification_worker_rejects_incomplete_browser_proof() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(name="api_workflow_smoke", status="passed", details="ok", command="api"),
            RunCheckResult(
                name="browser_flow_smoke",
                status="passed",
                details="ok",
                command="browser",
                diagnostics={"roles_checked": ["client"], "ui_steps": []},
            ),
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    report = VerificationWorker.verify(
        latest_execution=execution,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    issue_kinds = {issue["kind"] for issue in report["issues"]}  # type: ignore[index]
    assert report["status"] == "failed"
    assert "browser_proof_missing_roles" in issue_kinds
    assert "browser_proof_missing_persisted_marker" in issue_kinds


def test_guardian_review_emits_blocker_findings_before_apply(tmp_path: Path) -> None:
    draft = tmp_path / "draft"
    target = draft / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "const mockData = [{ name: 'John Doe' }];\n"
        "const accessToken = 'sk_live_1234567890abcdef';\n"
        "export function render() { document.body.innerText = mockData[0].name; }\n",
        encoding="utf-8",
    )
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=["miniapp/app/static/client/app.js"],
        results=[
            RunCheckResult(name="api_workflow_smoke", status="skipped", details="not rerun", command="api"),
            RunCheckResult(
                name="browser_flow_smoke",
                status="passed",
                details="ok",
                command="browser",
                diagnostics={
                    "roles_checked": ["client"],
                    "ui_steps": [{"action": "open"}],
                    "mobile_layout": {"status": "failed", "horizontal_overflow": True},
                },
            ),
        ],
        completed_at=utc_now(),
    )

    report = GuardianReview.review(
        workspace_id="ws_1",
        run_id="run_1",
        draft_source=draft,
        changed_files=["miniapp/app/static/client/app.js"],
        latest_execution=execution,
        acceptance_contract={"required": True, "flows": [{"id": "create_task"}], "features": {"workflow_update": True}},
        implementation_plan={"primary_entities": ["task"]},
        target_role_scope=["client", "specialist", "manager"],
        intent="create",
        review_context={
            "diff": "\n".join(
                [
                    "diff --git a/miniapp/app/routes/api.py b/miniapp/app/routes/api.py",
                    "-@router.post('/api/tasks')",
                    *["+bulk change" for _ in range(3010)],
                ]
            ),
            "token_usage": {"total_tokens": 170000, "context_window_remaining": 4000},
            "context_pressure": {"status": "critical"},
        },
    ).model_dump(mode="json", by_alias=True)

    codes = {finding["code"] for finding in report["findings"]}
    checklist = {item["key"]: item for item in report["checklist"]}
    assert report["schema"] == "grounded.guardian_review.v1"
    assert report["status"] == "failed"
    assert report["final_review_gate"]["schema"] == "grounded.final_review_gate.v1"
    assert report["final_review_gate"]["status"] == "failed"
    assert "guardian.seeded_mock_data.mock_or_seed_named_data" in codes
    assert "guardian.security_privacy.hardcoded_secret" in codes
    assert "guardian.breaking_changes.removed_api_route_without_api_proof" in codes
    assert "guardian.changed_size_risk.large_candidate_delta" in codes
    assert "guardian.context_bloat.context_pressure_critical" in codes
    assert "guardian.mobile_overflow" in codes
    assert "guardian.role_workflow_missing_browser_roles" in codes
    assert "guardian.missing_acceptance_tests" in codes
    assert "guardian.weak_persistence_missing_browser_marker" in codes
    assert {
        "breaking_changes",
        "missing_tests",
        "product_readiness",
        "mobile_overflow",
        "stale_mock_data",
        "context_bloat",
        "changed_size_risk",
        "security_privacy",
    }.issubset(checklist)
    assert all(checklist[key]["status"] == "failed" for key in checklist)


def test_check_orchestrator_selects_full_create_gate() -> None:
    plan = WorkspaceAgentCheckOrchestrator.plan(
        focused_visual_edit=False,
        create_intent=True,
        acceptance_required=True,
        generation_mode=GenerationMode.FAST,
        has_draft_diff=True,
    )

    assert plan.scope_mode == "agentic"
    assert plan.check_profile == "full"
    assert plan.check_attempt == 1


def test_browser_replay_extracts_failed_step_packet() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(
                name="browser_flow_smoke",
                status="failed",
                details="failed",
                command="browser",
                logs=["button did not update state"],
                diagnostics={
                    "failed_step": "client_submit",
                    "failed_selector": "#main-form",
                    "failed_route": "/client",
                    "dom_selector": "form#main-form",
                    "screenshot_before": "/tmp/before.png",
                    "screenshot_after": "/tmp/after.png",
                    "mobile_viewport": {"width": 390, "height": 844},
                    "network_errors": ["500 /api/items"],
                    "console_errors": ["ReferenceError"],
                    "playwright_scenario": {
                        "schema": "grounded.browser_playwright_scenario.v1",
                        "mobile_viewport": {"width": 390, "height": 844},
                        "steps": [
                            {"action": "open", "route": "/client", "screenshot_after": "/tmp/open.png"},
                            {
                                "action": "client_submit",
                                "route": "/client",
                                "selector": "form#main-form",
                                "screenshot_before": "/tmp/before.png",
                                "screenshot_after": "/tmp/after.png",
                            },
                        ],
                    },
                },
            )
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    packet = BrowserProofReplay.failed_step_packet(execution)

    assert packet is not None
    assert packet["schema"] == "grounded.browser_replay_packet.v2"
    assert packet["failed_step"] == "client_submit"
    assert packet["dom_selector"] == "form#main-form"
    assert packet["screenshot_before"] == "/tmp/before.png"
    assert packet["screenshot_after"] == "/tmp/after.png"
    assert packet["network_errors"] == ["500 /api/items"]
    assert packet["replay_plan"]["first_action"] == "reproduce_failed_step"
    assert packet["replay_plan"]["scenario_step"]["selector"] == "form#main-form"
    assert BrowserProofReplay.should_rerun_step_first(packet) is True


def test_browser_flow_script_compiles_with_replay_capture() -> None:
    script = CheckRunner._real_browser_ui_flow_python_script()

    compile(script, "<browser-proof-script>", "exec")
    assert "grounded.browser_playwright_scenario.v1" in script
    assert "screenshot_before" in script
    assert "MOBILE_VIEWPORT" in script


def test_process_recovery_marks_running_processes_stale() -> None:
    checkpoint = {
        "process_summary": {
            "active_processes": [
                {"process_id": "proc_1", "command": "rg token miniapp/app", "status": "running"},
            ]
        }
    }

    restored = AgentProcessRecovery.restore_view(checkpoint)
    process_checkpoint = AgentProcessRecovery.checkpoint({"active_processes": [], "processes": [{"process_id": "proc_2"}]})

    assert restored["stale_processes"][0]["status"] == "stale_after_restart"  # type: ignore[index]
    assert process_checkpoint["active_count"] == 0


def test_rollout_trace_reduces_tool_events() -> None:
    trace = RolloutTraceRecorder()
    trace.append("run_1", "tool_batch", {"tool": "read_files"})
    trace.append("run_1", "tool_batch", {"tool": "run_checks"})
    snapshot = trace.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["tool_counts"] == {"read_files": 1, "run_checks": 1}


def test_file_state_cache_restore_keeps_freshness_without_content(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/main.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('ok')\n", encoding="utf-8")
    cache = AgentFileStateCache()
    assert cache.read(run_id="run_a", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text(encoding="utf-8"))
    snapshot = cache.snapshot("run_a", root=root)

    restored = AgentFileStateCache()
    result = restored.restore_snapshot(run_id="run_b", snapshot=snapshot)
    freshness = restored.freshness(run_id="run_b", root=root, path="miniapp/app/main.py")
    content = restored.read(run_id="run_b", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text(encoding="utf-8"))

    assert result["restored"] == 1
    assert freshness["status"] == "fresh"
    assert content == "print('ok')\n"


def test_exact_edit_tool_reports_multiple_matches_and_applies_unique_match(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True)
    target.write_text("const status = 'new';\nconst next = 'new';\n", encoding="utf-8")

    changes, trace = file_changes_from_mutating_tool_calls(
        [
            {
                "tool": "edit_file_exact",
                "file_path": "miniapp/app/static/client/app.js",
                "old_string": "new",
                "new_string": "ready",
                "reason": "test",
            }
        ],
        read_text_file=lambda path: (root / path).read_text(encoding="utf-8") if (root / path).exists() else None,
    )

    assert changes == []
    assert trace[0]["status"] == "failed"
    assert trace[0]["repair_packet"]["code"] == "multiple_matches"  # type: ignore[index]

    changes, trace = file_changes_from_mutating_tool_calls(
        [
            {
                "tool": "edit_file_exact",
                "file_path": "miniapp/app/static/client/app.js",
                "old_string": "const status = 'new';",
                "new_string": "const status = 'ready';",
                "reason": "test",
            }
        ],
        read_text_file=lambda path: (root / path).read_text(encoding="utf-8") if (root / path).exists() else None,
    )

    assert trace[0]["tool"] == "edit_file_exact"
    assert changes[0].operation == "replace"
    assert "const status = 'ready';" in str(changes[0].content)


def test_exact_edit_tool_requires_fresh_read_state_and_reports_similar_path(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True)
    target.write_text("const status = 'new';\n", encoding="utf-8")

    changes, trace = file_changes_from_mutating_tool_calls(
        [
            {
                "tool": "edit_file_exact",
                "file_path": "miniapp/app/static/client/app.js",
                "old_string": "new",
                "new_string": "ready",
            }
        ],
        read_text_file=lambda path: (root / path).read_text(encoding="utf-8") if (root / path).exists() else None,
        file_freshness=lambda path: {"path": path, "status": "unread", "exists": True, "fresh": False},
    )

    assert changes == []
    assert trace[0]["repair_packet"]["code"] == "file_not_read"  # type: ignore[index]
    assert trace[0]["repair_packet"]["required_next_tool"] == "read_files"  # type: ignore[index]

    changes, trace = file_changes_from_mutating_tool_calls(
        [
            {
                "tool": "edit_file_exact",
                "file_path": "miniapp/app/static/client/missing.js",
                "old_string": "new",
                "new_string": "ready",
            }
        ],
        read_text_file=lambda path: (root / path).read_text(encoding="utf-8") if (root / path).exists() else None,
        file_freshness=lambda path: {"path": path, "status": "missing", "exists": False, "fresh": False},
        find_similar_path=lambda path: "miniapp/app/static/client/app.js",
    )

    assert changes == []
    assert trace[0]["repair_packet"]["code"] == "similar_path_found"  # type: ignore[index]
    assert trace[0]["repair_packet"]["target_files"] == ["miniapp/app/static/client/app.js"]  # type: ignore[index]


def test_command_policy_blocks_shell_expansion_invariants() -> None:
    assert decide_workspace_command("PYTHONPATH=platform/backend pytest -q").action == "forbidden"
    assert decide_workspace_command("rg foo miniapp/app/*.py").action == "forbidden"
    assert decide_workspace_command("rg foo miniapp/app <<EOF").action == "forbidden"
    assert decide_workspace_command("python -m py_compile miniapp/app/main.py").action == "allow"


def test_command_policy_handles_shell_git_rg_and_find_parser_invariants() -> None:
    assert decide_workspace_command("bash -lc 'rg api miniapp/app'").action == "allow"
    assert decide_workspace_command("bash -lc 'rg api miniapp/app > out.txt'").action == "forbidden"
    assert decide_workspace_command("bash -lc 'bash -lc \"rg api miniapp/app\"'").action == "forbidden"
    assert decide_workspace_command("git status --short").action == "allow"
    git_write = decide_workspace_command("git diff --output out.patch")
    assert git_write.action == "forbidden"
    assert git_write.safety_class == "workspace_write"
    assert decide_workspace_command("git -c core.pager=cat status").action == "forbidden"
    assert decide_workspace_command("rg --pre tool miniapp/app").action == "forbidden"
    assert decide_workspace_command("find miniapp/app -type f").action == "allow"
    assert decide_workspace_command("find miniapp/app -exec rm {} ;").action == "forbidden"
    find_delete = decide_workspace_command("find miniapp/app -delete")
    assert find_delete.action == "forbidden"
    assert find_delete.safety_class == "destructive"


def test_command_policy_blocks_shell_bypass_executables_and_ast_forms() -> None:
    blocked = [
        "zsh -lc 'rg api miniapp/app'",
        "pwsh -Command Get-ChildItem",
        "powershell -Command Get-ChildItem",
        "cmd /c dir",
        "/usr/bin/env python -m py_compile miniapp/app/main.py",
        "./python -m py_compile miniapp/app/main.py",
        "eval rg api miniapp/app",
        "source .venv/bin/activate",
        "rg api miniapp/app | cat",
        "rg api miniapp/app || true",
        "rg api miniapp/app &",
        "rg $(pwd) miniapp/app",
        "rg foo <(cat miniapp/app/main.py)",
        "rg foo ${HOME}",
        "rg foo {miniapp/app,miniapp/tests}",
        "rg api miniapp/app\nls miniapp",
    ]

    for command in blocked:
        assert decide_workspace_command(command).action == "forbidden", command


def test_command_policy_blocks_network_and_package_operations() -> None:
    blocked = {
        "curl https://example.com": "direct_network_tool",
        "wget https://example.com": "direct_network_tool",
        "ssh example.com": "direct_network_tool",
        "python -m pip install requests": "package_network_operation",
        "pip install requests": "package_network_operation",
        "npm add vite": "package_network_operation",
        "pnpm update": "package_network_operation",
        "yarn add vite": "package_network_operation",
        "git clone https://example.com/repo.git": "git_network_operation",
        "git fetch origin": "git_network_operation",
        "git pull": "git_network_operation",
        "git push": "git_network_operation",
        "git ls-remote origin": "git_network_operation",
        "git submodule update --init": "git_network_operation",
        "node --experimental-network-imports --test tests/generated_app.test.mjs": "node_network_imports",
    }

    for command, code in blocked.items():
        decision = decide_workspace_command(command)
        assert decision.action == "forbidden", command
        assert decision.network_policy.get("code") == code, command


def test_command_policy_resolves_basename_to_trusted_host_path() -> None:
    decision = decide_workspace_command("python3 -m py_compile miniapp/app/main.py")

    assert decision.action == "allow"
    assert decision.executable_resolution["status"] == "trusted_basename"
    assert decision.resolved_argv
    assert Path(decision.resolved_argv[0]).is_absolute()


def test_command_policy_additive_amendments_are_deny_precedence_and_safe_only() -> None:
    policy = AgentCommandPolicy.from_dsl_text(
        """
amendment_rule(
    pattern = ["python3", "-m", "pytest"],
    decision = "allow",
    justification = "allow focused pytest diagnostics",
    match = ["python3 -m pytest miniapp/tests -q"],
)
amendment_rule(
    pattern = ["python3", "-m", "pytest", "--snapshot-update"],
    decision = "forbidden",
    justification = "deny pytest snapshot updates",
    match = ["python3 -m pytest --snapshot-update miniapp/tests"],
)
amendment_rule(
    pattern = ["curl"],
    decision = "allow",
    justification = "attempted unsafe network override",
    match = ["curl https://example.com"],
)
""",
        source="amendments.codexpolicy",
    )

    allowed = policy.decide("python3 -m pytest miniapp/tests -q")
    denied = policy.decide("python3 -m pytest --snapshot-update miniapp/tests")
    network = policy.decide("curl https://example.com")

    assert allowed.action == "allow"
    assert allowed.matched_amendments
    assert denied.action == "forbidden"
    assert denied.matched_amendments
    assert network.action == "forbidden"
    assert network.network_policy["code"] == "direct_network_tool"
    assert network.matched_amendments == ()


def test_hook_trace_declares_record_only_side_effects() -> None:
    hooks = AgentHookManager()
    event = hooks.record("run_1", "before_apply", payload={"path": "miniapp/app/main.py"})
    snapshot = hooks.snapshot("run_1")

    assert event["schema"] == "grounded.hook_event.v1"
    assert event["side_effects_allowed"] is False
    assert event["payload"]["permission"] == "record_only"
    assert snapshot["schema"] == "grounded.hook_trace.v1"
