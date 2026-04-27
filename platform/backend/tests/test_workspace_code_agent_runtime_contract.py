from __future__ import annotations

import json
from types import SimpleNamespace

from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord, DraftFileOperation, GenerateRequest, RunCheckResult
from app.modules.miniapp_agent_loop.turn_runner import WorkspaceLoopTurnRunner
from app.modules.workspace_code_agent_runtime.runtime import SEED_CONTEXT_PATHS, WorkspaceCodeAgentRuntime
from app.services.workspace.service import WorkspaceService
from app.services.workspace.run_service import RunService


def test_agent_prompt_declares_run_checks_read_only() -> None:
    prompt = WorkspaceCodeAgentRuntime._agent_system_prompt()
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema()

    assert "run_checks is a read-only platform validation snapshot" in prompt
    assert "Tools are diagnostic only" in prompt
    assert "All code changes must be returned in the operations array" in prompt
    assert "client, specialist, and manager" in prompt
    assert "Create tasks must be multi-page" in prompt
    assert "at least two domain-specific child pages" in prompt
    assert "route_manifest.json" in prompt
    assert "compact routes map" in prompt
    assert "Never declare a route_manifest route unless" in prompt
    assert "Every generated HTML route page" in prompt
    assert "literally present in the file being read" in prompt
    assert 'node:test does not export expect' in prompt
    assert "Generate light-mode interfaces by default" in prompt
    assert "preserve existing selectors" in prompt
    assert "miniapp/tests/test_generated_app.py" in prompt
    assert "miniapp/tests/generated_app.test.mjs" in prompt
    assert "do not use miniapp/app/..." in prompt
    assert "fileURLToPath" in prompt
    assert schema["properties"]["operations"]["maxItems"] == 12
    assert "miniapp/tests/test_generated_app.py" in SEED_CONTEXT_PATHS
    assert "miniapp/tests/generated_app.test.mjs" in SEED_CONTEXT_PATHS


def test_create_patch_coverage_rejects_partial_role_slice() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create")

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
    assert "miniapp/app/static/specialist/<two-child-pages>/index.html" in missing
    assert "miniapp/app/static/manager/<two-child-pages>/index.html" in missing
    correction = WorkspaceCodeAgentRuntime._create_patch_coverage_correction_result(missing)
    assert "client-only first patch" in str(correction["required_next_action"])


def test_create_patch_coverage_accepts_balanced_role_and_test_patch() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create")

    def op(path: str) -> DraftFileOperation:
        return DraftFileOperation(file_path=path, operation="replace", content="<html></html>", reason="test")

    paths = [
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/catalog/index.html",
        "miniapp/app/static/client/orders/index.html",
        "miniapp/app/static/specialist/index.html",
        "miniapp/app/static/specialist/queue/index.html",
        "miniapp/app/static/specialist/stock/index.html",
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/analytics/index.html",
        "miniapp/app/static/manager/products/index.html",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    ]

    assert WorkspaceCodeAgentRuntime._create_patch_coverage_gap([op(path) for path in paths], request=request) == []


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
    assert "no more than 12 concise full-file operations" in str(correction["required_next_action"])
    assert "staged frontend-only patch" in str(correction["required_next_action"])
    assert "exact missing route pages" in str(correction["required_next_action"])
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
    assert "10-12 concise operations" in str(budget["required_next_action"])
    assert "first answer" in str(budget["required_next_action"])
    assert "frontend-first" in str(budget["contract"])
    assert "three role root pages" in str(budget["required_next_action"])
    assert "profile/settings page pattern" in str(budget["required_next_action"])
    assert "cwd=miniapp" in str(budget["required_next_action"])
    assert "path.join(process.cwd()" in str(budget["required_next_action"])
    assert "large inline <style> blocks" in str(budget["required_next_action"])


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


def test_product_behavior_phrase_is_not_commerce_prompt() -> None:
    assert not WorkspaceCodeAgentRuntime._is_commerce_prompt(
        "fix the javascript syntax error without changing product behavior"
    )
    assert WorkspaceCodeAgentRuntime._is_commerce_prompt("create a product catalog with cart")


def test_running_progress_stays_early_until_file_edits_exist() -> None:
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "running_checks",
        details={"attempt": 2, "has_file_edits": False},
    ) == ("Checking workspace shell", 7)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "running_checks",
        details={"attempt": 1, "has_file_edits": True},
    ) == ("Running final checks", 64)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 2, "outcome": "no_progress", "operation_count": 0, "tool_request_count": 0},
    ) == ("No file edits returned", 30)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "tool_request", "operation_count": 0, "tool_request_count": 2},
    ) == ("Requested 2 context reads", 25)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "patch_ready", "operation_count": 8, "tool_request_count": 0},
    ) == ("Prepared 8 file edits", 38)


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
        "max_output_tokens": 14000,
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


def test_prompt_alignment_domain_detection_does_not_treat_booking_catalog_as_commerce() -> None:
    prompt = "создай приложение для бронирования тренировок: каталог направлений, расписание, тренеры и запись на слот"

    assert not WorkspaceCodeAgentRuntime._is_commerce_prompt(prompt)
    assert WorkspaceCodeAgentRuntime._is_booking_prompt(prompt)


def test_prompt_alignment_accepts_domain_specific_shop_language_without_literal_product_word() -> None:
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
                "miniapp/app/static/client/index.html": "Каталог сортов чая, карточки, корзина и оформление заказа",
                "miniapp/app/static/manager/index.html": "Панель менеджера: ассортимент, позиции каталога и заказы",
            }.get(path)

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()

    result = runtime._prompt_alignment_smoke(
        workspace_id="ws",
        run_id="run",
        prompt="Создай светлый интернет-магазин чая для клиента и менеджера",
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
