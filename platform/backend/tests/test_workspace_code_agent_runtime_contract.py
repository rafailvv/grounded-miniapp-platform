from __future__ import annotations

import json
from types import SimpleNamespace

from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import CheckExecutionRecord, DraftFileOperation, GenerateRequest, JobEvent, JobRecord, RunCheckResult, RunRecord
from app.modules.miniapp_agent_loop.tool_agent_runtime import validate_workspace_command
from app.modules.miniapp_agent_loop.turn_runner import WorkspaceLoopTurnRunner
from app.modules.workspace_code_agent_runtime.runtime import SEED_CONTEXT_PATHS, WorkspaceCodeAgentRuntime
from app.repositories.state_store import StateStore
from app.services.workspace.service import WorkspaceService
from app.services.workspace.run_service import RunService


def test_agent_prompt_declares_run_checks_read_only() -> None:
    prompt = WorkspaceCodeAgentRuntime._agent_system_prompt()
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema()

    assert "run_checks is a read-only platform validation snapshot" in prompt
    assert "run_command is diagnostic-only" in prompt
    assert "Tools are diagnostic only" in prompt
    assert "All code changes must be returned in the operations array" in prompt
    assert "client, specialist, and manager" in prompt
    assert "three separate role apps" in prompt
    assert "Create tasks must be multi-page" in prompt
    assert "Fast requires at least one domain-specific child page per role" in prompt
    assert "static/client/styles.css" in prompt
    assert "placeholder CSS" in prompt
    assert "route_manifest.json" in prompt
    assert "compact routes map" in prompt
    assert "never ship static-only mockups" in prompt
    assert "Do not add mock data, seed data, demo data, sample data" in prompt
    assert "GET and POST APIs" in prompt
    assert "status/update endpoint" in prompt
    assert "GET starts empty, POST creates" in prompt
    assert "Never declare a route_manifest route unless" in prompt
    assert "Every generated HTML route page" in prompt
    assert "literally present in the file being read" in prompt
    assert 'node:test does not export expect' in prompt
    assert "Generate normal light-mode interfaces by default" in prompt
    assert "Do not give roles different color palettes" in prompt
    assert "preserve existing selectors" in prompt
    assert "miniapp/tests/test_generated_app.py" in prompt
    assert "miniapp/tests/generated_app.test.mjs" in prompt
    assert "do not use miniapp/app/..." in prompt
    assert "fileURLToPath" in prompt
    assert schema["properties"]["operations"]["maxItems"] == 20
    assert "run_command" in schema["properties"]["tool_requests"]["items"]["properties"]["tool"]["enum"]
    assert "miniapp/tests/test_generated_app.py" in SEED_CONTEXT_PATHS
    assert "miniapp/tests/generated_app.test.mjs" in SEED_CONTEXT_PATHS


def test_fast_first_create_schema_can_force_patch_only_turn() -> None:
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema(
        operation_limit=12,
        content_max_length=7000,
        allow_tool_requests=False,
        allowed_outcomes=["patch_ready"],
    )

    assert schema["properties"]["operations"]["maxItems"] == 12
    assert schema["properties"]["tool_requests"]["maxItems"] == 0
    assert "tool_request" not in schema["properties"]["outcome"]["enum"]
    assert schema["properties"]["outcome"]["enum"] == ["patch_ready"]
    assert schema["properties"]["operations"]["items"]["properties"]["content"]["maxLength"] == 7000


def test_create_patch_coverage_rejects_partial_role_slice() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)

    def op(path: str) -> DraftFileOperation:
        return DraftFileOperation(file_path=path, operation="replace", content="<html></html>", reason="test")

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(
        [
            op("miniapp/app/static/client/index.html"),
            op("miniapp/app/static/client/catalog/index.html"),
            op("miniapp/app/static/client/orders/index.html"),
            op("miniapp/app/static/specialist/index.html"),
            op("miniapp/app/generated/route_manifest.json"),
        ],
        request=request,
    )

    assert "miniapp/app/static/manager/index.html" in missing
    assert "miniapp/tests/test_generated_app.py" in missing
    assert "miniapp/tests/generated_app.test.mjs" in missing
    assert "miniapp/app/static/specialist/<one-child-page>/index.html" in missing
    assert "miniapp/app/static/manager/<one-child-page>/index.html" in missing
    correction = WorkspaceCodeAgentRuntime._create_patch_coverage_correction_result(missing)
    assert "client-only first patch" in str(correction["required_next_action"])


def test_fast_create_patch_coverage_accepts_one_child_page_per_role_and_test_patch() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)

    def op(path: str) -> DraftFileOperation:
        return DraftFileOperation(file_path=path, operation="replace", content="<html></html>", reason="test")

    paths = [
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/catalog/index.html",
        "miniapp/app/static/client/app.js",
        "miniapp/app/static/client/styles.css",
        "miniapp/app/static/specialist/index.html",
        "miniapp/app/static/specialist/queue/index.html",
        "miniapp/app/static/specialist/app.js",
        "miniapp/app/static/specialist/styles.css",
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/analytics/index.html",
        "miniapp/app/static/manager/app.js",
        "miniapp/app/static/manager/styles.css",
        "miniapp/app/main.py",
        "miniapp/app/routes/store_api.py",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    ]

    operations = [op(path) for path in paths]
    for operation in operations:
        if operation.file_path == "miniapp/app/routes/store_api.py":
            operation.content = (
                "from fastapi import APIRouter\n\n"
                "router = APIRouter()\n"
                "@router.get('/api/orders')\n"
                "def list_orders():\n"
                "    return []\n"
                "@router.post('/api/orders')\n"
                "def create_order(payload: dict):\n"
                "    return payload\n"
                "@router.patch('/api/orders/{order_id}')\n"
                "def update_order(order_id: str, payload: dict):\n"
                "    return payload\n"
            )
        if operation.file_path.endswith(".html"):
            role = operation.file_path.split("/")[3]
            operation.content = f'<link rel="stylesheet" href="/static/{role}/styles.css"><script src="/static/preview_bridge.js"></script>'
        if operation.file_path == "miniapp/app/static/client/app.js":
            operation.content = "fetch('/api/orders', { method: 'POST', body: JSON.stringify({ title: value }) });"
        if operation.file_path == "miniapp/app/static/specialist/app.js":
            operation.content = "fetch('/api/orders/1', { method: 'PATCH', body: JSON.stringify({ status: 'confirmed' }) });"
        if operation.file_path.endswith("styles.css"):
            operation.content = ".page-shell { color: #172033; }\n.record-card { border: 1px solid #ddd; }\n.metric-card { padding: 12px; }\n"
        if operation.file_path == "miniapp/tests/test_generated_app.py":
            operation.content = "client.get('/api/orders')\nclient.post('/api/orders', json={'title': 'User order'})\nclient.get('/api/orders')\nclient.patch('/api/orders/1', json={'status':'confirmed'})\n"

    assert WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request) == []


def test_fast_create_fallback_generates_separate_role_apps_and_css() -> None:
    request = GenerateRequest(prompt="Я тренер, хочу записи на тренировки", intent="create", generation_mode=GenerationMode.FAST)

    operations = WorkspaceCodeAgentRuntime._fast_create_fallback_operations(request)
    by_path = {operation.file_path: operation.content or "" for operation in operations}
    route_paths = [path for path in by_path if path.startswith("miniapp/app/routes/") and path != "miniapp/app/routes/role_pages.py"]

    for role in ("client", "specialist", "manager"):
        assert f"miniapp/app/static/{role}/index.html" in by_path
        assert f"miniapp/app/static/{role}/app.js" in by_path
        assert f"miniapp/app/static/{role}/styles.css" in by_path
        assert f"/static/{role}/styles.css" in by_path[f"miniapp/app/static/{role}/index.html"]
        assert f".{role}-app" in by_path[f"miniapp/app/static/{role}/styles.css"]
        assert "padding: max(76px" in by_path[f"miniapp/app/static/{role}/styles.css"]
        assert "/static/client/styles.css" not in by_path[f"miniapp/app/static/{role}/index.html"] or role == "client"
        assert "/static/specialist/styles.css" not in by_path[f"miniapp/app/static/{role}/index.html"] or role == "specialist"
        assert "/static/manager/styles.css" not in by_path[f"miniapp/app/static/{role}/index.html"] or role == "manager"
    assert 'href="/specialist"' not in by_path["miniapp/app/static/client/index.html"]
    assert 'href="/manager"' not in by_path["miniapp/app/static/client/index.html"]
    assert 'href="/client"' not in by_path["miniapp/app/static/specialist/index.html"]
    assert 'href="/manager"' not in by_path["miniapp/app/static/specialist/index.html"]
    assert 'href="/client"' not in by_path["miniapp/app/static/manager/index.html"]
    assert 'href="/specialist"' not in by_path["miniapp/app/static/manager/index.html"]
    assert 'method: "POST"' in by_path["miniapp/app/static/client/app.js"]
    assert 'method: "PATCH"' in by_path["miniapp/app/static/specialist/app.js"]
    assert "metric-card" in by_path["miniapp/app/static/manager/app.js"]
    assert len(route_paths) == 1
    assert route_paths[0] != "miniapp/app/routes/bookings.py"
    assert '@router.patch("/api/' in by_path[route_paths[0]]
    assert "тренер" in "\n".join(by_path.values()).lower()


def test_create_patch_coverage_rejects_static_only_app_without_api() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)

    def op(path: str) -> DraftFileOperation:
        return DraftFileOperation(file_path=path, operation="replace", content="<html></html>", reason="test")

    paths = [
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/main.py",
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/catalog/index.html",
        "miniapp/app/static/specialist/index.html",
        "miniapp/app/static/specialist/queue/index.html",
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/analytics/index.html",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    ]

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap([op(path) for path in paths], request=request)

    assert "miniapp/app/routes/<domain_resource>.py" in missing
    assert "backend GET /api/<resource>" in missing
    assert "backend POST /api/<resource>" in missing
    assert "frontend form/fetch POST /api/<resource>" in missing
    assert "miniapp/tests/test_generated_app.py API persistence coverage" in missing


def test_balanced_create_patch_coverage_requires_two_child_pages_per_role() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.BALANCED)

    def op(path: str) -> DraftFileOperation:
        return DraftFileOperation(file_path=path, operation="replace", content="<html></html>", reason="test")

    operations = [
        op("miniapp/app/generated/route_manifest.json"),
        op("miniapp/app/static/client/index.html"),
        op("miniapp/app/static/client/catalog/index.html"),
        op("miniapp/app/static/specialist/index.html"),
        op("miniapp/app/static/specialist/queue/index.html"),
        op("miniapp/app/static/manager/index.html"),
        op("miniapp/app/static/manager/analytics/index.html"),
        op("miniapp/app/main.py"),
        op("miniapp/app/routes/store_api.py"),
        op("miniapp/tests/test_generated_app.py"),
        op("miniapp/tests/generated_app.test.mjs"),
    ]
    for operation in operations:
        if operation.file_path == "miniapp/app/routes/store_api.py":
            operation.content = (
                "from fastapi import APIRouter\nrouter = APIRouter()\n"
                "@router.get('/api/orders')\ndef list_orders(): return []\n"
                "@router.post('/api/orders')\ndef create_order(payload: dict): return payload\n"
                "@router.patch('/api/orders/{order_id}')\ndef update_order(order_id: str, payload: dict): return payload\n"
            )
        if operation.file_path == "miniapp/app/static/client/index.html":
            operation.content = "fetch('/api/orders', { method: 'POST', body: JSON.stringify({ title: value }) });"
        if operation.file_path == "miniapp/tests/test_generated_app.py":
            operation.content = "client.get('/api/orders')\nclient.post('/api/orders', json={'title': 'User order'})\nclient.get('/api/orders')\n"

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request)

    assert "miniapp/app/static/client/<two-child-pages>/index.html" in missing
    assert "miniapp/app/static/specialist/<two-child-pages>/index.html" in missing
    assert "miniapp/app/static/manager/<two-child-pages>/index.html" in missing


def test_fix_completion_requires_meaningful_diff_even_when_checks_are_green() -> None:
    class FakeWorkspaceService:
        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return ""

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()

    state = runtime._completion_state(
        workspace_id="ws",
        run_id="run",
        request=GenerateRequest(prompt="Исправь видимый баг", mode="fix", intent="edit"),
        results=[RunCheckResult(name="schema_validators", status="passed", details="ok")],
        preview_details={},
        validation_snapshot=None,
    )

    assert state["strict_green"] is False
    assert any(issue["check"] == "meaningful_diff" for issue in state["remaining_issues"])


def test_run_token_usage_accumulates_iteration_ready_details() -> None:
    first = WorkspaceCodeAgentRuntime._merge_run_token_usage(
        {},
        {"input_tokens": 1200, "output_tokens": 300, "reasoning_tokens": 50, "total_tokens": 1550},
    )
    merged = WorkspaceCodeAgentRuntime._merge_run_token_usage(
        first,
        {"input_tokens": 800, "output_tokens": 200, "reasoning_tokens": 25},
    )

    assert merged["input_tokens"] == 2000
    assert merged["output_tokens"] == 500
    assert merged["reasoning_tokens"] == 75
    assert merged["total_tokens"] == 2550
    assert merged["turn_count"] == 2
    assert merged["last_turn"] == {
        "input_tokens": 800,
        "output_tokens": 200,
        "reasoning_tokens": 25,
        "total_tokens": 1000,
    }


def test_run_save_preserves_live_token_usage_and_caps_failed_progress(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    run = RunRecord(
        workspace_id="ws",
        prompt="Create app",
        intent="create",
        status="running",
        token_usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "turn_count": 1},
        linked_job_id="job_live",
    )
    service._save_run(run)

    stale_terminal = run.model_copy(
        update={
            "status": "failed",
            "apply_status": "failed",
            "current_stage": "failed",
            "progress_percent": 100,
            "token_usage": {},
            "linked_job_id": None,
        }
    )
    service._save_run(stale_terminal)
    saved = RunRecord.model_validate(service.store.get("runs", run.run_id))

    assert saved.progress_percent == 98
    assert saved.linked_job_id == "job_live"
    assert saved.token_usage["total_tokens"] == 15
    assert saved.token_usage["turn_count"] == 1


def test_preview_refresh_progress_does_not_step_back_from_99() -> None:
    assert RunService._preview_refresh_progress(94) == 98
    assert RunService._preview_refresh_progress(98) == 98
    assert RunService._preview_refresh_progress(99) == 99
    assert RunService._preview_refresh_progress(100) == 99


def test_get_run_backfills_token_usage_and_specific_failure_from_job_events(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    run = RunRecord(
        workspace_id="ws",
        prompt="Create app",
        intent="create",
        status="failed",
        apply_status="failed",
        current_stage="failed",
        progress_percent=100,
        failure_reason="Workspace loop exhausted its retry budget without reaching a usable state.",
    )
    job = JobRecord(
        workspace_id="ws",
        prompt="Create app",
        status="failed",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        linked_run_id=run.run_id,
        events=[
            JobEvent(
                event_type="iteration_ready",
                message="turn",
                details={"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50, "total_tokens": 1200},
            )
        ],
        remaining_issues=[
            {
                "check": "generated_app_python_tests",
                "logs": [
                    "sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: bookings",
                    "FAILED (errors=1)",
                ],
            }
        ],
    )
    service.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    service.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    hydrated = service.get_run(run.run_id)

    assert hydrated.progress_percent == 98
    assert hydrated.linked_job_id == job.job_id
    assert hydrated.token_usage["total_tokens"] == 1200
    assert hydrated.token_usage["turn_count"] == 1
    assert "no such table: bookings" in hydrated.failure_reason


def test_get_run_keeps_explicit_zero_token_usage_from_fallback_job(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    run = RunRecord(
        workspace_id="ws",
        prompt="Create app",
        intent="create",
        status="completed",
        apply_status="applied",
        current_stage="completed",
        progress_percent=100,
        token_usage={},
    )
    zero_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "turn_count": 0,
        "last_turn": {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        },
    }
    job = JobRecord(
        workspace_id="ws",
        prompt="Create app",
        status="completed",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        linked_run_id=run.run_id,
        token_usage=zero_usage,
    )
    service.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    service.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))

    hydrated = service.get_run(run.run_id)

    assert hydrated.linked_job_id == job.job_id
    assert hydrated.token_usage["total_tokens"] == 0
    assert hydrated.token_usage["input_tokens"] == 0
    assert hydrated.token_usage["output_tokens"] == 0
    assert hydrated.token_usage["reasoning_tokens"] == 0
    assert hydrated.token_usage["turn_count"] == 0


def test_scoped_run_command_policy_allows_diagnostics_and_blocks_mutation() -> None:
    assert validate_workspace_command("cd miniapp && python -m unittest discover -s tests -p test_generated_app.py") is None
    assert validate_workspace_command("python3 -m py_compile app/main.py") is None
    assert validate_workspace_command("node --test tests/generated_app.test.mjs") is None
    assert validate_workspace_command("node --check app/static/client/app.js") is None
    assert validate_workspace_command("rg \"fetch\" app/static") is None
    assert validate_workspace_command("sed -n '1,80p' app/main.py") is None
    assert validate_workspace_command("ls app/static/client") is None

    assert "Package installation" in str(validate_workspace_command("npm install"))
    assert "Destructive" in str(validate_workspace_command("rm -rf miniapp"))
    assert "Only 'python -m unittest'" in str(validate_workspace_command("python -c 'print(1)'"))
    assert "Network" in str(validate_workspace_command("curl https://example.com"))
    assert "Command chaining" in str(validate_workspace_command("cd miniapp && node --test tests/a.mjs && ls"))


def test_failed_generated_tests_add_test_context_and_repair_contract() -> None:
    class FakeWorkspaceService:
        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            return {
                "miniapp/tests/test_generated_app.py": "def test_roles(): pass\n",
                "miniapp/tests/generated_app.test.mjs": (
                    "const insightsPath = path.join(process.cwd(), 'app/static/specialist/insights/index.html');\n"
                    "test('client HTML exposes catalog', () => {})\n"
                ),
                "miniapp/app/static/specialist/insights/index.html": "<h1>Recent gadget trends</h1>\n",
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Generated JS app tests failed.",
                command="node --test tests/generated_app.test.mjs",
                logs=["AssertionError: id=\"product-grid\" was missing"],
            )
        ],
    )
    extra_file_context: dict[str, str] = {}
    tool_results: list[dict[str, object]] = []

    runtime._add_failed_generated_test_context(
        workspace_id="ws",
        run_id="run",
        latest_execution=execution,
        extra_file_context=extra_file_context,
        tool_results=tool_results,
    )

    assert "miniapp/tests/test_generated_app.py" in extra_file_context
    assert "miniapp/tests/generated_app.test.mjs" in extra_file_context
    assert "miniapp/app/static/specialist/insights/index.html" in extra_file_context
    assert tool_results[-1]["tool"] == "generated_test_failure_context"
    assert "same generated test failure" in str(tool_results[-1]["required_next_action"])
    assert tool_results[-1]["related_fixture_files_loaded"] == [
        "miniapp/app/static/specialist/insights/index.html"
    ]


def test_build_validator_failures_add_targeted_repair_context() -> None:
    class FakeWorkspaceService:
        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            return {
                "miniapp/app/generated/route_manifest.json": '{"roles":{"manager":{"routes":{"/manager/profile":"static/manager/profile/index.html"}}}}',
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="schema_validators",
                status="failed",
                details="Build validators failed.",
                logs=[
                    json.dumps(
                        {
                            "code": "build.missing_static_page",
                            "message": "Static page is missing: index.html",
                            "location": "miniapp/app/static/manager/profile/index.html",
                        }
                    )
                ],
            )
        ],
    )
    extra_file_context: dict[str, str] = {}
    tool_results: list[dict[str, object]] = []

    runtime._add_build_validator_failure_context(
        workspace_id="ws",
        run_id="run",
        latest_execution=execution,
        extra_file_context=extra_file_context,
        tool_results=tool_results,
    )

    assert "miniapp/app/generated/route_manifest.json" in extra_file_context
    assert tool_results[-1]["tool"] == "build_validator_failure_context"
    assert "create the exact missing HTML file" in str(tool_results[-1]["required_next_action"])
    assert tool_results[-1]["issues"][0]["code"] == "build.missing_static_page"


def test_static_asset_and_generated_test_failures_add_repair_context() -> None:
    class FakeWorkspaceService:
        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            return {
                "miniapp/app/static/client/catalog/index.html": '<script src="/static/client/catalog.js"></script>',
                "miniapp/app/generated/route_manifest.json": '{"routes":{"/client/catalog":"static/client/catalog/index.html"}}',
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="schema_validators",
                status="failed",
                details="Build validators failed.",
                logs=[
                    json.dumps(
                        {
                            "code": "build.broken_static_ref",
                            "message": "Static asset reference is broken: /static/client/catalog.js",
                            "location": "miniapp/app/static/client/catalog/index.html",
                        }
                    )
                ],
            ),
            RunCheckResult(
                name="platform_invariants",
                status="failed",
                details="Platform invariants failed.",
                logs=[
                    json.dumps(
                        {
                            "code": "platform.missing_generated_app_tests",
                            "message": "Generated test file is missing.",
                            "location": "miniapp/tests/generated_app.test.mjs",
                        }
                    )
                ],
            ),
        ],
    )
    extra_file_context: dict[str, str] = {}
    tool_results: list[dict[str, object]] = []

    runtime._add_build_validator_failure_context(
        workspace_id="ws",
        run_id="run",
        latest_execution=execution,
        extra_file_context=extra_file_context,
        tool_results=tool_results,
    )

    assert "miniapp/app/static/client/catalog/index.html" in extra_file_context
    assert "build.broken_static_ref" in str(tool_results[-1]["issues"])
    assert "platform.missing_generated_app_tests" in str(tool_results[-1]["issues"])
    assert "create miniapp/tests/test_generated_app.py" in str(tool_results[-1]["required_next_action"])


def test_static_js_failures_add_exact_file_repair_context() -> None:
    class FakeWorkspaceService:
        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            return {
                "miniapp/app/static/shared/store_data.js": 'const title = "12.4" bright";\n',
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Static JavaScript syntax check failed.",
                command="node --check app/static/shared/store_data.js",
                logs=[
                    "Static JavaScript syntax check failed for the draft miniapp.",
                    "/workspace/source/miniapp/app/static/shared/store_data.js:1",
                    "SyntaxError: Unexpected identifier 'bright'",
                ],
                diagnostics={
                    "static_js_syntax_error": {
                        "file_path": "miniapp/app/static/shared/store_data.js",
                        "line": 1,
                        "syntax_error": "SyntaxError: Unexpected identifier 'bright'",
                    }
                },
            )
        ],
    )
    extra_file_context: dict[str, str] = {}
    tool_results: list[dict[str, object]] = []

    runtime._add_static_js_failure_context(
        workspace_id="ws",
        run_id="run",
        latest_execution=execution,
        extra_file_context=extra_file_context,
        tool_results=tool_results,
    )

    assert extra_file_context["miniapp/app/static/shared/store_data.js"] == 'const title = "12.4" bright";\n'
    assert tool_results[-1]["tool"] == "static_js_failure_context"
    assert "Patch the exact file_path" in str(tool_results[-1]["required_next_action"])


def test_create_effective_role_scope_always_includes_all_roles() -> None:
    request = GenerateRequest(
        prompt="Create a catalog for buyers",
        intent="create",
        target_role_scope=["client"],
    )

    assert WorkspaceCodeAgentRuntime._effective_role_scope(request) == ["client", "specialist", "manager"]


def test_generated_tests_are_writeable_agent_outputs() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)

    operations = runtime._coerce_operations(
        [
            {
                "file_path": "miniapp/tests/test_generated_app.py",
                "operation": "create",
                "content": "import unittest\n",
                "reason": "cover generated backend app behavior",
            },
            {
                "file_path": "miniapp/tests/generated_app.test.mjs",
                "operation": "create",
                "content": "import test from 'node:test';\n",
                "reason": "cover generated frontend app behavior",
            },
        ]
    )

    assert [operation.file_path for operation in operations] == [
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    ]


def test_self_blocked_tool_contract_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I could not update the workspace because run_checks never returned.",
        "diagnosis": "The tool could not run a Python script to rewrite files, so no file changes were applied.",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)

    correction = WorkspaceCodeAgentRuntime._tool_contract_correction_result(payload)
    assert correction["tool"] == "tool_contract_correction"
    assert "cannot write files" in str(correction["contract"])
    assert "operations" in str(correction["required_next_action"])


def test_no_progress_self_blocked_tool_contract_response_is_retryable() -> None:
    payload = {
        "outcome": "no_progress",
        "assistant_message": "Нужен доступ к apply_patch или инструменту редактирования, без возможности записи файлов я не могу закончить задачу.",
        "diagnosis": "Требуется инструмент редактирования файлов.",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)


def test_no_more_tool_rounds_fatal_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "Request error: No more tool rounds allowed.",
        "diagnosis": "",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)


def test_unrecognized_tool_call_fatal_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "Tool call was not provided in recognized format.",
        "diagnosis": "",
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_self_blocked_tool_contract_response(payload)


def test_empty_fatal_agent_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I’m sorry, but I can’t help with that.",
        "diagnosis": "",
        "tool_requests": [],
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_empty_fatal_agent_response(payload)
    correction = WorkspaceCodeAgentRuntime._empty_fatal_correction_result(payload)
    assert "ordinary workspace code generation" in str(correction["contract"])


def test_empty_fatal_not_able_to_generate_response_is_retryable() -> None:
    payload = {
        "outcome": "fatal_invalid_response",
        "assistant_message": "I’m sorry, but I’m not able to help with that.",
        "diagnosis": "I cannot generate the required response in this context.",
        "tool_requests": [],
        "operations": [],
    }

    assert WorkspaceCodeAgentRuntime._is_empty_fatal_agent_response(payload)


def test_output_cap_correction_for_create_requires_compact_patch() -> None:
    from app.models.domain import CreateRunRequest

    request = CreateRunRequest(prompt="Создай приложение", mode="generate", intent="create")

    correction = WorkspaceCodeAgentRuntime._output_cap_correction_result(
        {"error": "max_output_tokens reached"},
        request=request,
    )

    assert correction["tool"] == "output_cap_correction"
    assert "no more than 12 concise file operations" in str(correction["required_next_action"])
    assert "backend API route module with GET/POST persistence" in str(correction["required_next_action"])
    assert "Do not add mock data" in str(correction["required_next_action"])
    assert "do not request more context" in str(correction["required_next_action"])


def test_output_cap_correction_for_edit_prefers_focused_replace() -> None:
    from app.models.domain import CreateRunRequest

    request = CreateRunRequest(prompt="Добавь блок Сегодня", mode="generate", intent="edit")

    correction = WorkspaceCodeAgentRuntime._output_cap_correction_result(
        {"error": "max_output_tokens reached"},
        request=request,
    )

    assert "1-2 focused operations" in str(correction["required_next_action"])
    assert "full-file replace" in str(correction["required_next_action"])


def test_fast_create_budget_prefers_frontend_first_compact_patch() -> None:
    budget = WorkspaceCodeAgentRuntime._fast_create_budget_result()

    assert budget["tool"] == "fast_create_budget"
    assert "up to 20 concise operations" in str(budget["required_next_action"])
    assert "first answer" in str(budget["required_next_action"])
    assert "backend persistence" in str(budget["contract"])
    assert "three separate role root apps" in str(budget["required_next_action"])
    assert "role styles.css files" in str(budget["required_next_action"])
    assert "POST-capable /api resource" in str(budget["required_next_action"])
    assert "status/update endpoint" in str(budget["required_next_action"])
    assert "GET starts empty" in str(budget["required_next_action"])
    assert "Choose child-page names and flows from the user's request" in str(budget["required_next_action"])
    assert "cwd=miniapp" in str(budget["required_next_action"])
    assert "path.join(process.cwd()" in str(budget["required_next_action"])
    assert "Per-role CSS files are required" in str(budget["required_next_action"])
    assert "consistent neutral light palette" in str(budget["required_next_action"])


def test_tool_budget_correction_forces_patch_ready_after_reads() -> None:
    request = GenerateRequest(prompt="Создай магазин", intent="create")

    correction = WorkspaceCodeAgentRuntime._tool_budget_correction_result(
        [{"tool": "read_files", "targets": ["miniapp/app/routes/role_pages.py"], "reason": "inspect"}],
        request=request,
    )

    assert correction["tool"] == "tool_budget_correction"
    assert "Return outcome=patch_ready now" in str(correction["required_next_action"])
    assert "Do not request more tools" in str(correction["required_next_action"])
    assert correction["ignored_tool_requests"][0]["targets"] == ["miniapp/app/routes/role_pages.py"]


def test_prompt_semantic_tokens_are_not_domain_classifier() -> None:
    assert WorkspaceCodeAgentRuntime._prompt_semantic_tokens(
        "fix the javascript syntax error without changing product behavior"
    ) == ["fix", "javascript", "syntax", "error", "without", "changing", "product", "behavior"]
    assert WorkspaceCodeAgentRuntime._prompt_semantic_tokens(
        "создай приложение для бронирования тренировок"
    ) == ["бронирования", "тренировок"]


def test_running_progress_stays_early_until_file_edits_exist() -> None:
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "running_checks",
        details={"attempt": 2, "has_file_edits": False},
    ) == ("Checking workspace shell", 19)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "running_checks",
        details={"attempt": 1, "has_file_edits": True},
    ) == ("Running final checks", 64)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 2, "outcome": "no_progress", "operation_count": 0, "tool_request_count": 0},
    ) == ("No file edits returned", 37)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "tool_request", "operation_count": 0, "tool_request_count": 2},
    ) == ("Requested 2 context reads", 29)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "patch_ready", "operation_count": 8, "tool_request_count": 0},
    ) == ("Prepared 8 file edits", 43)
    assert not WorkspaceCodeAgentRuntime._should_update_run_stage(
        "running_checks",
        progress=19,
        existing_progress=37,
    )
    assert WorkspaceCodeAgentRuntime._should_update_run_stage(
        "agent_turn_started",
        progress=42,
        existing_progress=37,
    )


def test_fix_run_preserves_requested_generation_mode() -> None:
    from app.models.domain import CreateRunRequest, WorkspaceRecord

    service = object.__new__(RunService)
    workspace = WorkspaceRecord(name="Fix mode", path="/tmp/fix-mode")

    for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY):
        request = CreateRunRequest(prompt="Fix bug", mode="fix", generation_mode=mode)
        assert service._resolve_generation_mode(workspace, request, "edit") == mode


def test_agent_turn_tuning_caps_all_generation_modes() -> None:
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="edit") == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 16000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 24000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="create") == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 28000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED, intent="create") == {
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 36000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY, intent="create") == {
        "reasoning": {"effort": "high"},
        "max_output_tokens": 52000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED) == {
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 32000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY) == {
        "reasoning": {"effort": "high"},
        "max_output_tokens": 42000,
    }


def test_green_draft_followup_keeps_agentic_quality_diagnostics() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="platform_invariants",
                status="passed",
                diagnostics={
                    "role_coverage": {
                        "client": {"status": "present", "route_count": 3},
                        "specialist": {"status": "present", "route_count": 3},
                        "manager": {"status": "present", "route_count": 3},
                    },
                    "generated_tests": {
                        "python": {"status": "present", "file_path": "miniapp/tests/test_generated_app.py"},
                        "js": {"status": "present", "file_path": "miniapp/tests/generated_app.test.mjs"},
                    },
                    "neutral_template_findings": [],
                },
            ),
            RunCheckResult(
                name="generated_app_python_tests",
                status="passed",
                details="Generated Python app tests passed.",
                command="python -m unittest discover",
            ),
            RunCheckResult(
                name="generated_app_js_tests",
                status="passed",
                details="Generated JS app tests passed.",
                command="node --test tests/generated_app.test.mjs",
            ),
        ],
    )

    quality = RunService._agent_quality_from_execution(execution)

    assert quality["role_coverage"]["client"]["route_count"] == 3
    assert quality["generated_tests"]["python"]["status"] == "present"
    assert quality["generated_tests"]["generated_app_python_tests"]["status"] == "passed"
    assert quality["generated_tests"]["generated_app_js_tests"]["status"] == "passed"
    assert RunService._validation_scope_for_run(SimpleNamespace(intent="create", mode="generate")) == "agentic"


def test_successful_agent_runs_do_not_spawn_hidden_followup_generation() -> None:
    from app.models.domain import CreateRunRequest, RunRecord

    request = CreateRunRequest(prompt="Создай приложение", mode="generate", intent="create")
    run = RunRecord(
        workspace_id="ws",
        prompt=request.prompt,
        intent="create",
        status="completed",
        apply_status="applied",
        generation_mode=GenerationMode.FAST,
    )

    assert not RunService._should_queue_async_followup_verification(request, run)


def test_fast_loop_reaches_full_context_before_repeated_signature_failure() -> None:
    assert WorkspaceLoopTurnRunner._next_fast_context_mode(
        next_attempt=5,
        made_progress=False,
        signature_changed=False,
    ) == "full_bundle"


def test_prompt_alignment_uses_prompt_terms_without_domain_branches() -> None:
    class FakeWorkspaceService:
        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return (
                "diff --git a/miniapp/app/static/client/index.html b/miniapp/app/static/client/index.html\n"
                "diff --git a/miniapp/app/static/manager/index.html b/miniapp/app/static/manager/index.html\n"
            )

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            return {
                "miniapp/app/static/client/index.html": "Кабинет клиента для букетов, доставки и адреса",
                "miniapp/app/static/manager/index.html": "Панель менеджера: букеты, доставка, адреса",
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()

    result = runtime._prompt_alignment_smoke(
        workspace_id="ws",
        run_id="run",
        prompt="Создай приложение для доставки букетов клиентам",
    )

    assert result.status == "passed"


def test_workspace_name_ignores_leading_timestamp() -> None:
    prompt = "1:49 AM\n\n\nСоздай интернет-магазин с каталогом товаров и корзиной"

    assert RunService._derive_workspace_name_from_prompt(prompt) == "Интернет Магазин Каталогом Товаров Корзиной"


def test_patch_hunk_without_file_headers_is_normalized_to_target_path() -> None:
    diff = "@@ -1,2 +1,2 @@\n-old\n+new\n"

    normalized = WorkspaceService._ensure_unified_diff_paths(diff, "miniapp/app/db.py")

    assert normalized.startswith("--- a/miniapp/app/db.py\n+++ b/miniapp/app/db.py\n@@")
    assert WorkspaceService._paths_from_unified_diff(normalized) == ["miniapp/app/db.py"]


def test_line_free_hunk_patch_is_preserved_and_applied_without_git_ranges() -> None:
    existing = 'const role = "manager";\nwindow.setupPreviewBridge?.(role);\n'
    diff = (
        "@@\n"
        '-const role = "manager";\n'
        "-window.setupPreviewBridge?.(role);\n"
        '+const role = "manager";\n'
        "+window.setupPreviewBridge?.(role);\n"
        '+const API_ROOT = "/api/manager";\n'
    )

    assert WorkspaceService._ensure_unified_diff_paths(diff, "miniapp/app/static/manager/app.js") == diff

    updated = WorkspaceService._apply_line_free_hunks(existing, diff)

    assert updated == (
        'const role = "manager";\n'
        "window.setupPreviewBridge?.(role);\n"
        'const API_ROOT = "/api/manager";\n'
    )


def test_line_free_addition_hunk_can_create_new_file_content() -> None:
    diff = "@@\n+from app.routes.studio import router as studio_router\n"

    assert WorkspaceService._apply_line_free_hunks("", diff) == "from app.routes.studio import router as studio_router\n"


def test_line_free_addition_hunk_uses_nearby_anchor_when_context_drifted() -> None:
    existing = (
        '@router.get("/api/client/bookings", response_model=list[BookingOut])\n'
        "def client_bookings(db: Session = Depends(get_db)) -> list[BookingOut]:\n"
        "    return []\n"
        "\n"
        "\n"
        '@router.post("/api/client/book", response_model=BookingOut)\n'
        "def create_booking(request: BookingRequest, db: Session = Depends(get_db)) -> BookingOut:\n"
        "    return BookingOut()\n"
    )
    diff = (
        "@@\n"
        ' @router.get("/api/client/bookings", response_model=list[BookingOut])\n'
        "def list_client_bookings(db: Session = Depends(get_db)) -> list[BookingOut]:\n"
        "     return []\n"
        " \n"
        " \n"
        '+@router.delete("/api/client/bookings/{booking_id}", status_code=204)\n'
        "+def delete_client_booking(booking_id: int, db: Session = Depends(get_db)) -> None:\n"
        "+    booking = db.get(Booking, booking_id)\n"
        "+    if booking is None:\n"
        '+        raise HTTPException(status_code=404, detail="Бронирование не найдено")\n'
        "+    db.delete(booking)\n"
        "+    db.commit()\n"
        "+\n"
        ' @router.post("/api/client/book", response_model=BookingOut)\n'
        "def create_booking(request: BookingRequest, db: Session = Depends(get_db)) -> BookingOut:\n"
        "     return BookingOut()\n"
    )

    updated = WorkspaceService._apply_line_free_hunks(existing, diff)

    assert updated is not None
    assert '@router.delete("/api/client/bookings/{booking_id}", status_code=204)' in updated
    assert updated.index('@router.delete("/api/client/bookings/{booking_id}"') < updated.index('@router.post("/api/client/book"')


def test_line_free_route_addition_does_not_insert_before_class_field_anchor() -> None:
    existing = (
        "class BookingOut(StrictModel):\n"
        "    id: int\n"
        "    client_name: str\n"
        "    slot_id: int\n"
        "    slot_label: str\n"
    )
    diff = (
        "@@\n"
        "     id: int\n"
        "     client_name: str\n"
        "     slot_id: int\n"
        '+@router.delete("/api/client/bookings/{booking_id}")\n'
        "+def cancel_booking(booking_id: int) -> None:\n"
        "+    return None\n"
        "     slot_label: str\n"
    )

    assert WorkspaceService._apply_line_free_hunks(existing, diff) is None


def test_unified_diff_detection_rejects_plain_file_content() -> None:
    assert WorkspaceCodeAgentRuntime._looks_like_unified_diff("@@ -1 +1 @@\n-old\n+new\n")
    assert not WorkspaceCodeAgentRuntime._looks_like_unified_diff("from __future__ import annotations\n\nprint('full file')\n")


def test_patch_with_separate_full_content_is_coerced_to_replace() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    operations = runtime._coerce_operations(
        [
            {
                "file_path": "miniapp/app/static/client/app.js",
                "operation": "patch",
                "content": 'const role = "client";\nwindow.setupPreviewBridge?.(role);\n',
                "diff": "*** Begin Patch\n*** Update File: miniapp/app/static/client/app.js\n@@\n-old\n+new\n*** End Patch\n",
                "reason": "Use full content fallback when a separate patch is also provided.",
            }
        ]
    )

    assert operations[0].operation == "replace"
    assert operations[0].content == 'const role = "client";\nwindow.setupPreviewBridge?.(role);\n'


def test_codex_update_patch_can_be_applied_to_expected_file() -> None:
    existing = "from sqlalchemy import create_engine\nfrom sqlalchemy.orm import DeclarativeBase, sessionmaker\n"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: miniapp/app/db.py\n"
        "@@\n"
        "-from sqlalchemy import create_engine\n"
        "-from sqlalchemy.orm import DeclarativeBase, sessionmaker\n"
        "+from sqlalchemy import Column, create_engine\n"
        "+from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker\n"
        "*** End Patch\n"
    )

    updated = WorkspaceService._apply_codex_update_patch(existing, patch, expected_path="miniapp/app/db.py")

    assert updated == "from sqlalchemy import Column, create_engine\nfrom sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker\n"
    assert WorkspaceService._ensure_unified_diff_paths(patch, "miniapp/app/db.py") == patch
