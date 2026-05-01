from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import (
    CheckExecutionRecord,
    CreateRunRequest,
    DraftFileOperation,
    GenerateRequest,
    JobEvent,
    JobRecord,
    RunCheckResult,
    RunRecord,
)
from app.modules.miniapp_agent_loop.tool_agent_runtime import validate_workspace_command
from app.modules.miniapp_agent_loop.edit_validator import WorkspaceLoopEditValidator
from app.modules.miniapp_agent_loop.turn_runner import WorkspaceLoopTurnRunner
from app.modules.miniapp_agent_loop.types import WorkspaceLoopTurnPlan
from app.modules.workspace_code_agent_runtime.runtime import (
    FOCUSED_VISUAL_CONTENT_MAX_LENGTH,
    FOCUSED_VISUAL_OPERATION_LIMIT,
    SEED_CONTEXT_PATHS,
    WorkspaceCodeAgentRuntime,
)
from app.repositories.state_store import StateStore
from app.services.workflow_acceptance import (
    build_acceptance_contract,
    build_implementation_plan,
    orchestration_metadata_for_contract,
    prompt_resource_candidates,
)
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
    assert "Additional pages/resources should come from the implementation plan and user prompt" in prompt
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
    assert "meaningfully stronger UI than Fast" in prompt
    assert "Quality design quality must be top-tier and product-ready" in prompt
    assert "page-specific DOM selectors must be null-safe" in prompt
    assert "Visible generated UI copy must use the user's language" in prompt
    assert "Do not paste raw prompt excerpts" in prompt
    assert "preserve existing selectors" in prompt
    assert "miniapp/tests/test_generated_app.py" in prompt
    assert "miniapp/tests/generated_app.test.mjs" in prompt
    assert "do not use miniapp/app/..." in prompt
    assert "fileURLToPath" in prompt
    assert schema["properties"]["operations"]["maxItems"] == 20
    assert "run_command" in schema["properties"]["tool_requests"]["items"]["properties"]["tool"]["enum"]
    assert "miniapp/tests/test_generated_app.py" in SEED_CONTEXT_PATHS
    assert "miniapp/tests/generated_app.test.mjs" in SEED_CONTEXT_PATHS


def test_edit_validator_rejects_patch_envelope_as_replace_content() -> None:
    plan = WorkspaceLoopTurnPlan(
        outcome="patch_ready",
        operations=[
            DraftFileOperation(
                file_path="miniapp/app/static/specialist/app.js",
                operation="replace",
                content="*** Begin Patch\n*** Delete File: miniapp/app/static/specialist/app.js\n*** End Patch\n",
                reason="bad replace",
            )
        ],
    )

    normalized = WorkspaceLoopEditValidator.normalize_plan(plan)

    assert normalized.outcome == "fatal_invalid_response"
    assert normalized.failure_class == "generation.invalid_patch_operation"
    assert normalized.operations == []


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


def test_parallel_worker_schema_requires_non_empty_operations() -> None:
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema(
        operation_limit=7,
        content_max_length=12000,
        allow_tool_requests=False,
        allowed_outcomes=["patch_ready"],
        min_operations=1,
    )

    assert schema["properties"]["operations"]["minItems"] == 1
    assert schema["properties"]["operations"]["maxItems"] == 7
    assert schema["properties"]["tool_requests"]["maxItems"] == 0


def test_visual_style_edit_uses_focused_css_contract() -> None:
    request = GenerateRequest(prompt="Поменяй стиль и цвета на фиолетовый", intent="edit", generation_mode=GenerationMode.FAST)
    visual_no_logic_request = GenerateRequest(
        prompt="Сделай интерфейс аккуратнее, увеличь отступы и не меняй логику приложения.",
        intent="edit",
        generation_mode=GenerationMode.FAST,
    )

    assert WorkspaceCodeAgentRuntime._focused_edit_kind(request) == "visual_style_edit"
    assert WorkspaceCodeAgentRuntime._focused_edit_kind(visual_no_logic_request) == "visual_style_edit"
    assert WorkspaceCodeAgentRuntime._focused_visual_css_paths(["client", "manager"]) == [
        "miniapp/app/static/shared/base.css",
        "miniapp/app/static/client/styles.css",
        "miniapp/app/static/manager/styles.css",
    ]

    schema = WorkspaceCodeAgentRuntime._agent_turn_schema(
        operation_limit=FOCUSED_VISUAL_OPERATION_LIMIT,
        content_max_length=FOCUSED_VISUAL_CONTENT_MAX_LENGTH,
        allow_tool_requests=False,
        allowed_outcomes=["patch_ready", "fatal_invalid_response"],
    )

    assert schema["properties"]["operations"]["maxItems"] == 4
    assert schema["properties"]["tool_requests"]["maxItems"] == 0
    assert schema["properties"]["outcome"]["enum"] == ["patch_ready", "fatal_invalid_response"]
    assert schema["properties"]["operations"]["items"]["properties"]["content"]["maxLength"] == FOCUSED_VISUAL_CONTENT_MAX_LENGTH


def test_patch_first_converts_large_existing_file_replace_for_edits() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    before = "\n".join(f"line {index}" for index in range(600)) + "\n"
    after = before.replace("line 300", "line 300 changed")
    runtime.workspace_service = SimpleNamespace(try_read_text_file=lambda *_args, **_kwargs: before)

    operations = runtime._enforce_patch_first_operations(
        [
            DraftFileOperation(
                file_path="miniapp/app/static/client/app.js",
                operation="replace",
                content=after,
                reason="Small behavior edit.",
            )
        ],
        request=GenerateRequest(prompt="Исправь кнопку", intent="edit"),
        workspace_id="ws",
        run_id="run",
    )

    assert operations[0].operation == "patch"
    assert operations[0].content is None
    assert "--- a/miniapp/app/static/client/app.js" in str(operations[0].diff)
    assert "+line 300 changed" in str(operations[0].diff)


def test_patch_first_allows_create_and_tiny_existing_replace() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = SimpleNamespace(try_read_text_file=lambda *_args, **_kwargs: "small\n")
    operation = DraftFileOperation(
        file_path="miniapp/app/static/client/index.html",
        operation="replace",
        content="small changed\n",
        reason="Tiny page update.",
    )

    edit_ops = runtime._enforce_patch_first_operations(
        [operation],
        request=GenerateRequest(prompt="Переименуй заголовок", intent="edit"),
        workspace_id="ws",
        run_id="run",
    )
    create_ops = runtime._enforce_patch_first_operations(
        [operation],
        request=GenerateRequest(prompt="Создай приложение", intent="create"),
        workspace_id="ws",
        run_id="run",
    )

    assert edit_ops[0].operation == "replace"
    assert create_ops[0].operation == "replace"


def test_workspace_service_treats_runtime_artifacts_as_ignored_paths() -> None:
    assert WorkspaceService._is_ignored_workspace_path(Path("miniapp/app/generated/app.db"))
    assert WorkspaceService._is_ignored_workspace_path(Path("miniapp/app/generated/cache.sqlite3"))
    assert WorkspaceService._is_ignored_workspace_path(Path("miniapp/app/routes/__pycache__/app.cpython-312.pyc"))
    assert not WorkspaceService._is_ignored_workspace_path(Path("miniapp/app/generated/route_manifest.json"))
    assert not WorkspaceService._is_ignored_workspace_path(Path("miniapp/app/static/client/app.js"))


def test_state_store_shards_heavy_reports_and_job_events(tmp_path) -> None:
    store = StateStore(tmp_path / "platform-state.json")
    events = [
        JobEvent(event_type="running_checks", message=f"check {index}", details={"check_step": "x"}).model_dump(mode="json")
        for index in range(StateStore.JOB_EVENT_SHARD_MIN_COUNT + 5)
    ]
    job = JobRecord(
        workspace_id="ws_shard",
        prompt="Build a catalog",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        events=[JobEvent.model_validate(event) for event in events],
    )

    store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
    store.upsert("reports", "run_artifacts:run_shard", {"workspace_id": "ws_shard", "diff": "x" * 4096})

    raw_state = store._read()
    raw_job = raw_state["jobs"][job.job_id]
    raw_report = raw_state["reports"]["run_artifacts:run_shard"]
    assert raw_job["event_storage_ref"].startswith("state-shards/job_events/")
    assert len(raw_job["events"]) <= StateStore.JOB_EVENT_TAIL_LIMIT
    assert raw_report["__sharded__"] is True

    hydrated_job = store.get("jobs", job.job_id)
    hydrated_report = store.get("reports", "run_artifacts:run_shard")
    assert hydrated_job is not None and len(hydrated_job["events"]) == len(events)
    assert hydrated_report == {"workspace_id": "ws_shard", "diff": "x" * 4096}


def test_runtime_spills_large_tool_results_to_artifacts(tmp_path) -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.store = StateStore(tmp_path / "platform-state.json")
    large_result = {
        "tool": "run_checks",
        "status": "failed",
        "logs": ["Generated test failed: " + ("x" * 8000)],
        "details": {"stdout": "line\n" * 3000, "stderr": "error\n" * 1200},
    }

    compact = runtime._compact_tool_results(
        [large_result],
        workspace_id="ws_tool",
        run_id="run_tool",
        max_items=1,
    )

    assert len(compact) == 1
    assert compact[0]["tool"] == "run_checks"
    assert compact[0]["has_more"] is True
    assert compact[0]["original_chars"] > 6000
    assert "tool_result" not in compact[0]
    ref = str(compact[0]["persisted_output_ref"])
    raw_report = runtime.store._read()["reports"][ref]
    assert raw_report["__sharded__"] is True

    hydrated_report = runtime.store.get("reports", ref)
    assert hydrated_report is not None
    assert hydrated_report["tool_result"] == large_result


def test_runtime_keeps_small_tool_results_inline(tmp_path) -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.store = StateStore(tmp_path / "platform-state.json")

    compact = runtime._compact_tool_results(
        [{"tool": "ls", "status": "ok", "output": "miniapp/app/main.py"}],
        workspace_id="ws_tool",
        run_id="run_tool",
        max_items=1,
    )

    assert compact == [{"tool": "ls", "status": "ok", "output": "miniapp/app/main.py"}]
    assert runtime.store._read()["reports"] == {}


def test_safe_copy_and_visual_edits_do_not_wait_for_preview_refresh() -> None:
    visual = CreateRunRequest(prompt="Поменяй цвета и отступы", intent="edit")
    copy = CreateRunRequest(prompt="Переименуй заголовок заказа", intent="edit")
    behavior = CreateRunRequest(prompt="Не работает кнопка в корзину", intent="edit")
    run = RunRecord(
        workspace_id="ws",
        prompt="",
        intent="edit",
        generation_mode=GenerationMode.FAST,
    )

    assert RunService._should_wait_for_preview_refresh(visual, run) is False
    assert RunService._should_wait_for_preview_refresh(copy, run) is False
    assert RunService._should_wait_for_preview_refresh(behavior, run) is True


def test_noisy_check_progress_events_are_compacted() -> None:
    assert WorkspaceCodeAgentRuntime._is_noisy_check_progress_event(
        "final_checks_started",
        {"check_step": "generated_app_js_tests", "check_status": "skipped"},
    )
    assert WorkspaceCodeAgentRuntime._is_noisy_check_progress_event(
        "backend_compile_started",
        {"check_step": "changed_files_static", "check_status": "passed"},
    )
    assert not WorkspaceCodeAgentRuntime._is_noisy_check_progress_event(
        "final_checks_started",
        {"check_step": "generated_app_js_tests", "check_status": "failed"},
    )


def test_compact_edit_tuning_reduces_output_budget() -> None:
    copy_tuning = WorkspaceCodeAgentRuntime._agent_turn_tuning(
        GenerationMode.FAST,
        intent="edit",
        focused_edit_kind="small_copy_edit",
    )
    standard_tuning = WorkspaceCodeAgentRuntime._agent_turn_tuning(
        GenerationMode.FAST,
        intent="edit",
        focused_edit_kind="standard",
    )

    assert copy_tuning["reasoning"]["effort"] == "low"
    assert copy_tuning["max_output_tokens"] <= 12000
    assert standard_tuning["max_output_tokens"] <= 14000


def test_create_repair_prompt_uses_compact_failed_check_context() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    large_file = "\n".join(f"const item{index} = {index};" for index in range(500))
    files = {
        "miniapp/tests/test_generated_app.py": "class TestGeneratedApp:\n    pass\n" + large_file,
        "miniapp/tests/generated_app.test.mjs": "import test from 'node:test';\n" + large_file,
        "miniapp/app/routes/orders.py": "from fastapi import APIRouter\n" + large_file,
        "miniapp/app/static/client/app.js": "const checkoutButton = document.querySelector('[data-testid=\"checkout\"]');\n" + large_file,
    }
    diff = "\n".join(f"diff --git a/{path} b/{path}" for path in files)

    class FakeWorkspaceService:
        def file_tree(self, *_args, **_kwargs):
            return list(files.keys()) + ["miniapp/app/static/client/index.html"]

        def diff(self, *_args, **_kwargs):
            return diff

        def try_read_text_file(self, _workspace_id, path, **_kwargs):
            return files.get(path)

    runtime.workspace_service = FakeWorkspaceService()
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                details="Cart flow failed because checkout button has no effective handler. " + "x" * 4000,
                command="node --test miniapp/tests/generated_app.test.mjs",
                exit_code=1,
                logs=["AssertionError: checkout handler missing " + "y" * 5000],
                diagnostics={
                    "file_path": "miniapp/tests/generated_app.test.mjs",
                    "huge_payload": "z" * 6000,
                },
            )
        ],
    )

    prompt = runtime._agent_user_prompt(
        workspace_id="ws",
        run_id="run",
        request=GenerateRequest(
            prompt="Я владелец пекарни, хочу каталог, корзину и оформление заказа.",
            intent="create",
            generation_mode=GenerationMode.FAST,
        ),
        attempt=2,
        tool_round=0,
        context_mode="minimal",
        repeated_no_progress=0,
        latest_execution=execution,
        latest_preview_details={"container_logs": ["preview log " + "p" * 5000], "status": "running"},
        seed_context={"miniapp/app/static/client/index.html": "seed" * 3000},
        extra_file_context={"miniapp/app/static/manager/index.html": "extra" * 3000},
        tool_results=[{"tool": "run_checks", "logs": ["tool log " + "t" * 7000], "files": files}],
        last_turn_summary="previous turn",
        latest_diff_summary=diff,
    )
    payload = json.loads(prompt)

    assert payload["prompt_payload_mode"] == "compact_repair"
    assert payload["context_pack"] == {}
    assert payload["fast_create_required_file_set"] == []
    assert "miniapp/tests/generated_app.test.mjs" in payload["file_contexts"]
    assert len(prompt) < 35000
    assert "z" * 2000 not in prompt
    assert any("compact repair turn" in rule for rule in payload["rules"])


def test_platform_shell_stabilizer_restores_safe_top_spacing(tmp_path) -> None:
    source_dir = tmp_path / "source"
    base_path = source_dir / "miniapp/app/static/shared/base.css"
    page_path = source_dir / "miniapp/app/static/client/index.html"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(
        ".page-shell { max-width: 620px; padding: 32px 16px 48px; }\n",
        encoding="utf-8",
    )
    page_path.write_text(
        '<main class="page-shell"><h1>Client</h1></main>',
        encoding="utf-8",
    )
    runtime = object.__new__(WorkspaceCodeAgentRuntime)

    changed = runtime._stabilize_platform_shell("ws", "run", source_dir, ["miniapp/app/static/client/index.html"])

    assert "miniapp/app/static/shared/base.css" in changed
    assert "miniapp/app/static/client/index.html" in changed
    assert "padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px)) !important" in base_path.read_text(encoding="utf-8")
    assert 'style="padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));"' in page_path.read_text(encoding="utf-8")


def test_copy_and_behavior_edits_are_classified_separately() -> None:
    copy_request = GenerateRequest(prompt="Переименуй заголовок в карточке заказа", intent="edit")
    behavior_request = GenerateRequest(prompt="Сделай POST endpoint и fetch для сохранения заказа", intent="edit")
    workflow_request = GenerateRequest(prompt="Не работает кнопка в корзину, заказ должен появляться у специалиста", intent="edit")

    assert WorkspaceCodeAgentRuntime._focused_edit_kind(copy_request) == "small_copy_edit"
    assert WorkspaceCodeAgentRuntime._focused_edit_kind(behavior_request) == "behavior_edit"
    assert WorkspaceCodeAgentRuntime._focused_edit_kind(workflow_request) == "behavior_workflow_edit"


def test_acceptance_contract_captures_generic_workflow_and_orchestration() -> None:
    contract = build_acceptance_contract(
        prompt="Интернет-магазин: специалист добавляет товар, клиент кладет в корзину и оформляет заказ, менеджер видит заказы",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        focused_edit_kind="standard",
    )
    orchestration = orchestration_metadata_for_contract(
        contract=contract,
        generation_mode=GenerationMode.BALANCED,
        focused_edit_kind="standard",
    )
    implementation_plan = build_implementation_plan(
        prompt="Интернет-магазин: специалист добавляет товар, клиент кладет в корзину и оформляет заказ, менеджер видит заказы",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
        orchestration=orchestration,
    )

    assert contract["required"] is True
    assert "commerce_catalog_cart_order" not in contract["features"]
    assert not any(flow["id"] == "commerce_catalog_cart_order" for flow in contract["flows"])
    assert any(flow["id"] == "related_resource_workflow" for flow in contract["flows"])
    resources = {item["resource"] for item in contract["required_endpoints"]}
    assert resources != {"records", "status_updates"}
    assert contract["features"]["resource_strategy"] == "prompt_derived_without_fixed_domain_template"
    assert "required_buttons" not in contract
    assert [item["role"] for item in contract["required_controls"]] == ["client", "specialist", "manager"]
    assert orchestration["enabled"] is True
    assert [phase["id"] for phase in orchestration["phases"]] == [
        "spec_extract",
        "parallel_build",
        "merge",
        "verify_repair",
    ]
    assert {worker["worker"] for worker in orchestration["worker_summaries"]} >= {
        "backend_api",
        "client_ui",
        "specialist_ui",
        "manager_ui",
        "generated_tests",
    }
    assert implementation_plan["principle"] == "plan_inspect_build_verify_repair_final_browser_proof"
    assert implementation_plan["test_contract"]["browser_flow_required"] is True
    assert implementation_plan["mobile_design_contract"]["no_horizontal_scroll"] is True


def test_prompt_resource_candidates_skip_intro_and_role_words() -> None:
    candidates = prompt_resource_candidates(
        "Я управляю небольшим детским центром. Родители оставляют заявку на занятие, педагог меняет статус.",
        limit=3,
    )

    assert candidates[0] == "requests"
    assert "upravlyayu" not in candidates
    assert not any(candidate.startswith("nebolsh") for candidate in candidates)
    assert not any(candidate.startswith("roditel") for candidate in candidates)
    assert not any(candidate.startswith("zanyati") for candidate in candidates)


def test_prompt_resource_candidates_prefer_tasks_over_intro_verb() -> None:
    candidates = prompt_resource_candidates(
        "Я руковожу небольшой студией разработки. Клиент описывает задачу, разработчик меняет статус задачи.",
        limit=3,
    )

    assert candidates[0] == "tasks"
    assert "rukovozhu" not in candidates


def test_cross_role_behavior_addition_gets_workflow_contract() -> None:
    prompt = (
        "Добавь срочность заявки: клиент выбирает обычная или срочная, "
        "исполнитель видит срочность в очереди, менеджер видит количество срочных. "
        "Срочность должна сохраняться после обновления и быть видна во всех трех частях приложения."
    )
    request = GenerateRequest(prompt=prompt, intent="edit", generation_mode=GenerationMode.FAST)

    focused_kind = WorkspaceCodeAgentRuntime._focused_edit_kind(request)
    contract = build_acceptance_contract(
        prompt=prompt,
        intent="edit",
        generation_mode=GenerationMode.FAST,
        focused_edit_kind=focused_kind,
    )
    orchestration = orchestration_metadata_for_contract(
        contract=contract,
        generation_mode=GenerationMode.FAST,
        focused_edit_kind=focused_kind,
    )

    assert focused_kind == "behavior_workflow_edit"
    assert contract["required"] is True
    assert orchestration["execution_style"] == "fast_parallel_workers"


def test_orchestration_job_events_are_valid_domain_events() -> None:
    for event_type in [
        "spec_extract_started",
        "parallel_build_started",
        "parallel_build_completed",
        "parallel_build_failed",
    ]:
        event = JobEvent(event_type=event_type, message="ok", details={})
        assert event.event_type == event_type


def test_fast_acceptance_contract_enables_parallel_workers() -> None:
    contract = build_acceptance_contract(
        prompt="Создай интернет-магазин: специалист добавляет товар, клиент оформляет заказ из корзины",
        intent="create",
        generation_mode=GenerationMode.FAST,
        focused_edit_kind="standard",
    )
    orchestration = orchestration_metadata_for_contract(
        contract=contract,
        generation_mode=GenerationMode.FAST,
        focused_edit_kind="standard",
    )

    assert orchestration["enabled"] is True
    assert orchestration["execution_style"] == "fast_parallel_workers"
    assert orchestration["parallel_worker_count"] == 5


def test_fast_parallel_worker_merge_rejects_conflicting_ownership() -> None:
    client_op = DraftFileOperation(
        file_path="miniapp/app/static/client/app.js",
        operation="replace",
        content="console.log('client')",
        reason="client",
    )
    bad_backend_op = DraftFileOperation(
        file_path="miniapp/app/static/client/app.js",
        operation="replace",
        content="console.log('bad')",
        reason="bad",
    )
    results = [
        {"worker": "backend_api", "status": "completed", "outcome": "patch_ready", "operations": [bad_backend_op]},
        {"worker": "client_ui", "status": "completed", "outcome": "patch_ready", "operations": [client_op]},
        {
            "worker": "specialist_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/specialist/app.js",
                    operation="replace",
                    content="",
                    reason="specialist",
                )
            ],
        },
        {
            "worker": "manager_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/manager/app.js",
                    operation="replace",
                    content="",
                    reason="manager",
                )
            ],
        },
        {
            "worker": "generated_tests",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/tests/test_generated_app.py",
                    operation="replace",
                    content="",
                    reason="tests",
                )
            ],
        },
    ]

    merged, error = WorkspaceCodeAgentRuntime._merge_fast_parallel_worker_operations(results)

    assert merged == []
    assert error is not None
    assert "outside ownership" in error


def test_fast_parallel_worker_merge_deduplicates_same_worker_path() -> None:
    first_manifest = DraftFileOperation(
        file_path="miniapp/app/generated/route_manifest.json",
        operation="replace",
        content='{"version": 1}',
        reason="first manifest",
    )
    final_manifest = DraftFileOperation(
        file_path="miniapp/app/generated/route_manifest.json",
        operation="replace",
        content='{"version": 2}',
        reason="final manifest",
    )
    results = [
        {
            "worker": "backend_api",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/routes/app_api.py",
                    operation="replace",
                    content="router = object()",
                    reason="api",
                ),
                first_manifest,
                final_manifest,
            ],
        },
        {
            "worker": "client_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/client/app.js",
                    operation="replace",
                    content="",
                    reason="client",
                )
            ],
        },
        {
            "worker": "specialist_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/specialist/app.js",
                    operation="replace",
                    content="",
                    reason="specialist",
                )
            ],
        },
        {
            "worker": "manager_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/manager/app.js",
                    operation="replace",
                    content="",
                    reason="manager",
                )
            ],
        },
        {
            "worker": "generated_tests",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/tests/test_generated_app.py",
                    operation="replace",
                    content="",
                    reason="tests",
                )
            ],
        },
    ]

    merged, error = WorkspaceCodeAgentRuntime._merge_fast_parallel_worker_operations(results)

    assert error is None
    manifests = [operation for operation in merged if operation.file_path == "miniapp/app/generated/route_manifest.json"]
    assert len(manifests) == 1
    assert manifests[0].content == '{"version": 2}'


def test_parallel_worker_merge_accepts_deferred_generated_tests_slice() -> None:
    results = [
        {
            "worker": "backend_api",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/routes/app_api.py",
                    operation="replace",
                    content="router = object()",
                    reason="api",
                )
            ],
        },
        {
            "worker": "client_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/client/app.js",
                    operation="replace",
                    content="",
                    reason="client",
                )
            ],
        },
        {
            "worker": "specialist_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/specialist/app.js",
                    operation="replace",
                    content="",
                    reason="specialist",
                )
            ],
        },
        {
            "worker": "manager_ui",
            "status": "completed",
            "outcome": "patch_ready",
            "operations": [
                DraftFileOperation(
                    file_path="miniapp/app/static/manager/app.js",
                    operation="replace",
                    content="",
                    reason="manager",
                )
            ],
        },
    ]

    merged, error = WorkspaceCodeAgentRuntime._merge_fast_parallel_worker_operations(results)

    assert error is None
    assert {operation.file_path for operation in merged} == {
        "miniapp/app/routes/app_api.py",
        "miniapp/app/static/client/app.js",
        "miniapp/app/static/specialist/app.js",
        "miniapp/app/static/manager/app.js",
    }


def test_parallel_worker_repair_result_preserves_existing_owned_files() -> None:
    existing = {
        "worker": "generated_tests",
        "status": "completed",
        "outcome": "patch_ready",
        "operations": [
            DraftFileOperation(
                file_path="miniapp/tests/test_generated_app.py",
                operation="replace",
                content="python test v1",
                reason="python tests",
            )
        ],
    }
    repair = {
        "worker": "generated_tests",
        "status": "completed",
        "outcome": "patch_ready",
        "operations": [
            DraftFileOperation(
                file_path="miniapp/tests/generated_app.test.mjs",
                operation="replace",
                content="js test",
                reason="js tests",
            )
        ],
    }

    merged = WorkspaceCodeAgentRuntime._merge_parallel_worker_repair_result(existing, repair)

    assert [operation.file_path for operation in merged["operations"]] == [
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    ]


def test_fast_parallel_blueprint_uses_generic_role_pages() -> None:
    contract = build_acceptance_contract(
        prompt="Интернет-магазин с каталогом, корзиной и заказами",
        intent="create",
        generation_mode=GenerationMode.FAST,
        focused_edit_kind="standard",
    )

    blueprint = WorkspaceCodeAgentRuntime._fast_parallel_blueprint(contract)
    workers = WorkspaceCodeAgentRuntime._fast_parallel_workers(blueprint)

    assert "commerce_flow" not in blueprint
    assert blueprint["role_files"]["client"]["child"].startswith("miniapp/app/static/client/")
    assert blueprint["role_files"]["client"]["child"] != "miniapp/app/static/client/request/index.html"
    assert blueprint["role_files"]["specialist"]["child"].startswith("miniapp/app/static/specialist/")
    assert blueprint["role_files"]["specialist"]["child"] != "miniapp/app/static/specialist/queue/index.html"
    assert {worker["worker"] for worker in workers} == {
        "backend_api",
        "client_ui",
        "specialist_ui",
        "manager_ui",
        "generated_tests",
    }


def test_no_platform_create_fallback_entrypoints_remain() -> None:
    removed_entrypoints = {
        "_fast_create_fallback_operations",
        "_fast_create_fallback_operations_with_cleanup",
        "_fast_create_should_use_fallback_repair",
        "_mark_fast_create_fallback_job",
    }

    for name in removed_entrypoints:
        assert not hasattr(WorkspaceCodeAgentRuntime, name)


def test_parallel_failures_target_owned_repair_workers() -> None:
    worker_ids = {"backend_api", "client_ui", "specialist_ui", "manager_ui", "generated_tests"}

    assert WorkspaceCodeAgentRuntime._parallel_repair_targets_from_merge_error(
        "client_ui returned no operations.",
        worker_ids,
    ) == {"client_ui"}
    assert WorkspaceCodeAgentRuntime._parallel_repair_targets_from_merge_error(
        "miniapp/app/routes/store.py was edited by both backend_api and client_ui.",
        worker_ids,
    ) == {"backend_api", "client_ui"}
    assert WorkspaceCodeAgentRuntime._parallel_repair_targets_from_coverage_gap(
        [
            "frontend form/fetch POST /api/<resource>",
            "backend status/update endpoint",
            "miniapp/tests/test_generated_app.py API status/update coverage",
        ],
        {},
    ) == {"backend_api", "client_ui", "generated_tests"}
    assert WorkspaceCodeAgentRuntime._parallel_repair_targets_from_coverage_gap(
        ["quality create: rich update/status endpoint"],
        {},
    ) == {"backend_api", "generated_tests"}


def test_parallel_retry_defers_generated_tests_until_app_slices_exist() -> None:
    worker_ids = {"backend_api", "client_ui", "specialist_ui", "manager_ui", "generated_tests"}
    retry = WorkspaceCodeAgentRuntime._parallel_filter_retry_targets(
        {"backend_api", "generated_tests"},
        {"client_ui", "specialist_ui", "manager_ui"},
        worker_ids,
    )

    assert retry == {"backend_api"}
    assert WorkspaceCodeAgentRuntime._parallel_filter_retry_targets(
        {"generated_tests"},
        {"backend_api", "client_ui", "specialist_ui", "manager_ui"},
        worker_ids,
    ) == {"generated_tests"}


def test_soft_create_coverage_gap_can_defer_to_validation_repair() -> None:
    assert WorkspaceCodeAgentRuntime._is_soft_create_coverage_gap(
        [
            "miniapp/tests/test_generated_app.py API persistence coverage",
            "miniapp/tests/test_generated_app.py unittest.TestCase coverage",
            "backend status/update endpoint",
            "frontend specialist/manager status update action",
            "miniapp/tests/test_generated_app.py API status/update coverage",
            "balanced create: second API resource or update/status endpoint",
        ]
    )
    assert not WorkspaceCodeAgentRuntime._is_soft_create_coverage_gap(
        ["miniapp/app/static/client/<one-child-page>/index.html"]
    )
    assert not WorkspaceCodeAgentRuntime._is_soft_create_coverage_gap(
        ["frontend form/fetch POST /api/<resource>"]
    )


def test_missing_generated_tests_get_explicit_repair_priority() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="platform_invariants",
                status="failed",
                details="Platform invariant smoke failed.",
                logs=[
                    '{"code": "platform.missing_generated_app_tests", "location": "miniapp/tests/test_generated_app.py"}',
                    '{"code": "platform.missing_generated_app_tests", "location": "miniapp/tests/generated_app.test.mjs"}',
                ],
            ),
            RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                details="Generated Python app tests are required for agentic create/edit runs but were not present in the draft workspace.",
            ),
        ],
    )

    assert WorkspaceCodeAgentRuntime._missing_generated_tests_repair_needed(execution)
    rules = WorkspaceCodeAgentRuntime._compact_repair_rules(
        generation_mode=GenerationMode.QUALITY,
        focused_edit_kind="standard",
        workflow_slice_repair=True,
        missing_generated_tests_repair=True,
    )
    joined = "\n".join(rules)
    assert "Create both miniapp/tests/test_generated_app.py and miniapp/tests/generated_app.test.mjs" in joined
    assert "Do not defer missing tests" in joined


def test_repair_rules_do_not_force_patch_into_one_role_script() -> None:
    rules = WorkspaceCodeAgentRuntime._compact_repair_rules(
        generation_mode=GenerationMode.QUALITY,
        focused_edit_kind="standard",
        workflow_slice_repair=True,
    )
    joined = "\n".join(rules)

    assert "another role script already owns the real update workflow" in joined
    assert "instead of adding fake PATCH code to the read-only role" in joined
    assert "assert.match(managerJs, /PATCH/)" in joined
    assert "assert manager GET/summary/refresh wiring instead" in joined


def test_repair_rules_require_exact_button_id_handler_or_link() -> None:
    rules = WorkspaceCodeAgentRuntime._compact_repair_rules(
        generation_mode=GenerationMode.QUALITY,
        focused_edit_kind="standard",
        workflow_slice_repair=True,
    )
    joined = "\n".join(rules)

    assert "workflow_button_without_handler" in joined
    assert "exact id string" in joined
    assert "plain link/remove it" in joined


def test_repair_rules_avoid_brittle_generated_js_selector_assertions() -> None:
    rules = WorkspaceCodeAgentRuntime._compact_repair_rules(
        generation_mode=GenerationMode.QUALITY,
        focused_edit_kind="standard",
        workflow_slice_repair=True,
    )
    joined = "\n".join(rules)

    assert "exact `id=\"...\"` quote style" in joined
    assert "single or double quotes" in joined
    assert "optional refresh/filter button id" in joined
    assert "actual required workflow controls" in joined


def test_api_resource_stems_include_fastapi_router_prefix_routes() -> None:
    source = """
from fastapi import APIRouter

router = APIRouter(prefix="/api")

@router.get("/products")
def list_products():
    return []

@router.post("/orders")
def create_order():
    return {}

@router.patch("/orders/{order_id}/status")
def update_order(order_id: str):
    return {}
"""

    assert WorkspaceCodeAgentRuntime._api_resource_stems(source) == {"products", "orders"}


def test_create_patch_coverage_keeps_existing_content_for_repair_patch() -> None:
    request = GenerateRequest(prompt="Create a bakery app", intent="create", generation_mode=GenerationMode.BALANCED)
    required_paths = {
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/request/index.html",
        "miniapp/app/static/client/app.js",
        "miniapp/app/static/client/styles.css",
        "miniapp/app/static/specialist/index.html",
        "miniapp/app/static/specialist/queue/index.html",
        "miniapp/app/static/specialist/app.js",
        "miniapp/app/static/specialist/styles.css",
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/overview/index.html",
        "miniapp/app/static/manager/app.js",
        "miniapp/app/static/manager/styles.css",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/main.py",
        "miniapp/app/routes/app_api.py",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    }
    existing_text = {
        "miniapp/app/static/client/index.html": '<link rel="stylesheet" href="/static/client/styles.css"><form></form>',
        "miniapp/app/static/specialist/index.html": '<link rel="stylesheet" href="/static/specialist/styles.css">',
        "miniapp/app/static/manager/index.html": '<link rel="stylesheet" href="/static/manager/styles.css">',
        "miniapp/app/routes/app_api.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/api')\n"
            "@router.get('/records')\n"
            "def list_records(): return []\n"
            "@router.post('/records')\n"
            "def create_record(): return {}\n"
            "@router.patch('/records/{record_id}')\n"
            "def patch_record(record_id: int): return {}\n"
            "@router.get('/status_updates')\n"
            "def list_status_updates(): return []\n"
        ),
        "miniapp/app/static/client/app.js": "fetch('/api/records', { method: 'POST' });",
        "miniapp/app/static/specialist/app.js": "fetch('/api/records/1', { method: 'PATCH' });",
        "miniapp/tests/test_generated_app.py": (
            "import unittest\n"
            "class GeneratedAppTest(unittest.TestCase):\n"
            "    def test_flow(self):\n"
            "        client.get('/api/records'); client.post('/api/records'); client.patch('/api/records/1')\n"
        ),
    }

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(
        [
            DraftFileOperation(
                file_path="miniapp/tests/test_generated_app.py",
                operation="patch",
                diff="@@\n-        client.patch('/api/records/1')\n+        client.patch('/api/records/1')\n",
                reason="repair generated test assertion",
            )
        ],
        request=request,
        existing_paths=required_paths,
        existing_text_by_path=existing_text,
    )

    assert "miniapp/tests/test_generated_app.py API persistence coverage" not in missing
    assert "miniapp/tests/test_generated_app.py unittest.TestCase coverage" not in missing
    assert "miniapp/tests/test_generated_app.py API status/update coverage" not in missing


def test_repair_context_targets_traceback_file_paths() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        results=[
            RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Backend import smoke failed.",
                logs=[
                    '  File "/tmp/workspace/source/miniapp/app/db.py", line 16, in <module>',
                    "NameError: name 'create_engine' is not defined",
                ],
            )
        ],
    )

    paths = WorkspaceCodeAgentRuntime._target_files_from_execution(execution)

    assert "miniapp/app/db.py" in paths


def test_repeated_apply_conflict_requires_full_file_replace() -> None:
    assert WorkspaceLoopTurnRunner._apply_conflict_required_next_action(1) == "Return corrected operations for the conflicted files."
    assert WorkspaceLoopTurnRunner._apply_conflict_required_next_action(2) == "Return full-file replace operations for only the conflicted files."


def test_agent_turn_schema_can_force_replace_only_operations() -> None:
    schema = WorkspaceCodeAgentRuntime._agent_turn_schema(allowed_operations=["replace"], operation_limit=1)

    operation_schema = schema["properties"]["operations"]["items"]["properties"]["operation"]

    assert operation_schema["enum"] == ["replace"]
    assert schema["properties"]["operations"]["maxItems"] == 1


def test_generated_test_failures_seed_backend_and_role_context_for_repair() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        changed_files=[],
        results=[
            RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                logs=["AssertionError: 422 not found in [200, 201]"],
            ),
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                logs=[
                    "Client submit control missing in readStatic(\"client/index.html\")",
                    "actual: 'const role = \"manager\";'",
                ],
            ),
        ],
    )

    targets = WorkspaceCodeAgentRuntime._target_files_from_execution(execution)

    assert "miniapp/tests/test_generated_app.py" in targets
    assert "miniapp/app/schemas.py" in targets
    assert "miniapp/app/routes/app_api.py" in targets
    assert "miniapp/tests/generated_app.test.mjs" in targets
    assert "miniapp/app/static/client/index.html" in targets
    assert "miniapp/app/static/manager/app.js" in targets
    assert "miniapp/app/static/specialist/index.html" not in targets


def test_frontend_workflow_failure_targets_role_js_and_backend_contract() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        changed_files=[],
        results=[
            RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="failed",
                logs=[
                    (
                        '{"code": "platform.workflow_form_field_not_submitted", '
                        '"location": "miniapp/app/static/specialist/requests-work/index.html", '
                        '"message": "field coach_note is not read"}'
                    )
                ],
            )
        ],
    )

    targets = WorkspaceCodeAgentRuntime._target_files_from_execution(execution)

    assert "miniapp/app/static/specialist/requests-work/index.html" in targets
    assert "miniapp/app/static/specialist/app.js" in targets
    assert "miniapp/app/schemas.py" in targets
    assert "miniapp/app/routes/app_api.py" in targets


def test_repair_context_expands_schema_and_role_wiring_files() -> None:
    paths = WorkspaceCodeAgentRuntime._repair_context_paths(
        failed_paths=[
            "miniapp/tests/test_generated_app.py",
            "miniapp/app/static/manager/index.html",
        ],
        diff_paths=[
            "miniapp/app/routes/app_api.py",
            "miniapp/app/schemas.py",
            "miniapp/app/static/manager/index.html",
            "miniapp/app/static/manager/zanyatiyami/index.html",
            "miniapp/app/static/manager/app.js",
            "miniapp/app/static/manager/styles.css",
        ],
    )

    assert "miniapp/app/routes/app_api.py" in paths
    assert "miniapp/app/schemas.py" in paths
    assert "miniapp/app/static/manager/app.js" in paths
    assert "miniapp/app/static/manager/styles.css" in paths
    assert "miniapp/tests/generated_app.test.mjs" in paths


def test_repair_context_includes_backend_for_frontend_schema_mismatch() -> None:
    paths = WorkspaceCodeAgentRuntime._repair_context_paths(
        failed_paths=["miniapp/app/static/specialist/requests-work/index.html"],
        diff_paths=[
            "miniapp/app/static/specialist/requests-work/index.html",
            "miniapp/app/static/specialist/app.js",
            "miniapp/app/static/specialist/styles.css",
        ],
    )

    assert "miniapp/app/routes/app_api.py" in paths
    assert "miniapp/app/schemas.py" in paths
    assert "miniapp/tests/test_generated_app.py" in paths
    assert "miniapp/app/static/specialist/app.js" in paths


def test_workflow_slice_repair_detects_connected_check_failures() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        changed_files=[],
        results=[
            RunCheckResult(
                name="connectivity_validators",
                status="failed",
                logs=["missing backend route"],
            ),
            RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="failed",
                logs=["form missing handler", "button missing handler"],
            ),
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                logs=["stale literal assertion"],
            ),
        ],
    )

    assert WorkspaceCodeAgentRuntime._workflow_slice_repair_needed(execution)
    rules = WorkspaceCodeAgentRuntime._compact_repair_rules(
        generation_mode=GenerationMode.BALANCED,
        focused_edit_kind="",
        workflow_slice_repair=True,
    )
    joined = "\n".join(rules)
    assert "connected workflow slice" in joined
    assert "Never satisfy a generated test by adding one role's app script to another role page" in joined


def test_compact_repair_checks_include_platform_and_frontend_diagnostics() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        changed_files=[],
        results=[
            RunCheckResult(name="changed_files_static", status="failed", logs=["backend import failed"]),
            RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                logs=["ImportError: missing schema"],
            ),
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                logs=["stale selector"],
            ),
            RunCheckResult(
                name="platform_invariants",
                status="failed",
                logs=['{"code":"preflight.route_schema_contract","location":"miniapp/app/schemas.py"}'],
            ),
            RunCheckResult(
                name="frontend_interaction_static_smoke",
                status="failed",
                logs=['{"code":"platform.workflow_form_without_handler","location":"miniapp/app/static/client/request/index.html"}'],
            ),
        ],
    )

    compact = WorkspaceCodeAgentRuntime._compact_repair_checks(execution)
    names = [item["name"] for item in compact]

    assert "platform_invariants" in names
    assert "frontend_interaction_static_smoke" in names
    assert names.index("platform_invariants") < names.index("generated_app_python_tests")
    assert WorkspaceCodeAgentRuntime._workflow_slice_repair_needed(execution)


def test_generated_test_contract_mismatch_gets_workflow_repair_context() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws",
        run_id="run",
        changed_files=[],
        results=[
            RunCheckResult(
                name="generated_app_python_tests",
                status="failed",
                logs=["AssertionError: 422 != 200"],
            ),
            RunCheckResult(
                name="generated_app_js_tests",
                status="failed",
                logs=["assert.ok(rootHtml.includes(page.css))"],
            ),
        ],
    )

    assert WorkspaceCodeAgentRuntime._workflow_slice_repair_needed(execution)
    rules = "\n".join(
        WorkspaceCodeAgentRuntime._compact_repair_rules(
            generation_mode=GenerationMode.FAST,
            focused_edit_kind="",
            workflow_slice_repair=True,
        )
    )
    assert "422 != 200" in rules
    assert "backend create schema, frontend FormData/payload field names" in rules
    assert "/static/<role>/styles.css" in rules
    assert "workflow_frontend_backend_field_mismatch" in rules


def test_operation_contract_correction_reports_empty_replace() -> None:
    correction = WorkspaceCodeAgentRuntime._operation_contract_correction_result(
        "Agent returned replace for miniapp/app/static/specialist/app.js without content.",
        {
            "operations": [
                {
                    "operation": "replace",
                    "file_path": "miniapp/app/static/specialist/app.js",
                    "content": None,
                }
            ]
        },
    )

    assert correction["tool"] == "operation_contract_correction"
    assert "create/replace require full resulting file content" in correction["contract"]
    assert correction["previous_operations"][0]["has_content"] == "False"


def test_provider_quota_errors_are_classified_as_terminal_platform_blockers() -> None:
    assert WorkspaceCodeAgentRuntime._is_provider_quota_error(
        "OpenAI responses returned 429: {\"error\":{\"code\":\"insufficient_quota\"}}"
    )
    assert WorkspaceCodeAgentRuntime._is_provider_quota_error("You exceeded your current quota")
    assert not WorkspaceCodeAgentRuntime._is_provider_quota_error("OpenAI responses returned 503")


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
            operation.content = (
                "import unittest\n\n"
                "class GeneratedAppTest(unittest.TestCase):\n"
                "    def test_orders_persist_and_update(self):\n"
                "        client.get('/api/orders')\n"
                "        client.post('/api/orders', json={'title': 'User order'})\n"
                "        client.get('/api/orders')\n"
                "        client.patch('/api/orders/1', json={'status':'confirmed'})\n"
            )

    assert WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request) == []


def test_parallel_mode_contracts_make_fast_balanced_quality_different() -> None:
    prompt = "Я владелец интернет-магазина одежды, хочу каталог, остатки, корзину и заказы"
    contracts = {
        mode: build_acceptance_contract(
            prompt=prompt,
            intent="create",
            generation_mode=mode,
            focused_edit_kind="standard",
        )
        for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY)
    }
    blueprints = {
        mode: WorkspaceCodeAgentRuntime._fast_parallel_blueprint(contracts[mode], generation_mode=mode)
        for mode in contracts
    }

    assert WorkspaceCodeAgentRuntime._parallel_create_round_budget(GenerationMode.FAST) == 10
    assert WorkspaceCodeAgentRuntime._parallel_create_round_budget(GenerationMode.BALANCED) == 14
    assert WorkspaceCodeAgentRuntime._parallel_create_round_budget(GenerationMode.QUALITY) == 18
    assert blueprints[GenerationMode.FAST]["required_child_pages_per_role"] == 1
    assert blueprints[GenerationMode.BALANCED]["required_child_pages_per_role"] == 1
    assert blueprints[GenerationMode.QUALITY]["required_child_pages_per_role"] == 1
    assert blueprints[GenerationMode.FAST]["mode_contract"]["design_level"] != blueprints[GenerationMode.BALANCED]["mode_contract"]["design_level"]
    assert blueprints[GenerationMode.BALANCED]["mode_contract"]["design_level"] != blueprints[GenerationMode.QUALITY]["mode_contract"]["design_level"]
    assert len(blueprints[GenerationMode.QUALITY]["resources"]) >= len(blueprints[GenerationMode.BALANCED]["resources"])


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

    assert "miniapp/app/routes/app_api.py or prompt-derived API route" in missing
    assert "backend GET /api/<resource>" in missing
    assert "backend POST /api/<resource>" in missing
    assert "frontend form/fetch POST /api/<resource>" in missing
    assert "miniapp/tests/test_generated_app.py API persistence coverage" in missing


def test_create_patch_coverage_requires_sqlalchemy_table_creation_and_unittest_tests() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/store_api.py",
            operation="create",
            content=(
                "from fastapi import APIRouter\n"
                "from app.db import Base\n"
                "from sqlalchemy.orm import Session\n"
                "router = APIRouter()\n"
                "def get_db(): pass\n"
                "class Product(Base):\n"
                "    __tablename__ = 'products'\n"
                "@router.get('/api/products')\n"
                "def list_products(): return []\n"
                "@router.post('/api/products')\n"
                "def create_product(payload: dict): return payload\n"
                "@router.patch('/api/products/{product_id}')\n"
                "def update_product(product_id: int, payload: dict, session: Session = next(get_db())): return Item()\n"
            ),
            reason="test",
        ),
        DraftFileOperation(file_path="miniapp/app/main.py", operation="replace", content="include_router", reason="test"),
        DraftFileOperation(file_path="miniapp/app/generated/route_manifest.json", operation="replace", content="{}", reason="test"),
        DraftFileOperation(file_path="miniapp/tests/test_generated_app.py", operation="replace", content="client.get('/api/products')\nclient.post('/api/products')\nclient.patch('/api/products/1')", reason="test"),
        DraftFileOperation(file_path="miniapp/tests/generated_app.test.mjs", operation="replace", content="node:test", reason="test"),
    ]
    for role, child in {"client": "catalog", "specialist": "inventory", "manager": "overview"}.items():
        operations.extend(
            [
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/index.html", operation="replace", content=f"/static/{role}/styles.css", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/{child}/index.html", operation="replace", content=f"/static/{role}/styles.css", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/app.js", operation="replace", content="fetch('/api/products', { method: 'POST' }); fetch('/api/products/1', { method: 'PATCH' });", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/styles.css", operation="replace", content=".card { padding: 8px; }", reason="test"),
            ]
        )

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request)

    assert "backend SQLAlchemy table creation via Base.metadata.create_all" in missing
    assert "backend FastAPI SQLAlchemy Session dependency via Depends(get_db_session)" in missing
    assert "backend undefined ORM Item model" in missing
    assert "miniapp/tests/test_generated_app.py unittest.TestCase coverage" in missing


def test_create_patch_coverage_does_not_treat_request_item_schema_as_undefined_item_model() -> None:
    request = GenerateRequest(prompt="Create a repair queue", intent="create", generation_mode=GenerationMode.FAST)
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/repair_api.py",
            operation="create",
            content=(
                "from fastapi import APIRouter\n"
                "from app.schemas import StrictModel\n"
                "router = APIRouter()\n"
                "class RequestItem(StrictModel):\n"
                "    id: int\n"
                "@router.get('/api/requests')\n"
                "def list_requests(): return []\n"
                "@router.post('/api/requests')\n"
                "def create_request(payload: dict): return payload\n"
                "@router.patch('/api/requests/{request_id}')\n"
                "def update_request(request_id: int, payload: dict): return payload\n"
            ),
            reason="test",
        ),
        DraftFileOperation(file_path="miniapp/app/main.py", operation="replace", content="include_router", reason="test"),
        DraftFileOperation(file_path="miniapp/app/generated/route_manifest.json", operation="replace", content="{}", reason="test"),
        DraftFileOperation(
            file_path="miniapp/tests/test_generated_app.py",
            operation="replace",
            content="import unittest\nclass T(unittest.TestCase):\n def test_flow(self): client.get('/api/requests'); client.post('/api/requests'); client.patch('/api/requests/1')",
            reason="test",
        ),
        DraftFileOperation(file_path="miniapp/tests/generated_app.test.mjs", operation="replace", content="node:test", reason="test"),
    ]
    for role, child in {"client": "request", "specialist": "queue", "manager": "overview"}.items():
        operations.extend(
            [
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/index.html", operation="replace", content=f"/static/{role}/styles.css", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/{child}/index.html", operation="replace", content=f"/static/{role}/styles.css", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/app.js", operation="replace", content="fetch('/api/requests', { method: 'POST' }); fetch('/api/requests/1', { method: 'PATCH' });", reason="test"),
                DraftFileOperation(file_path=f"miniapp/app/static/{role}/styles.css", operation="replace", content=".card { padding: 8px; }", reason="test"),
            ]
        )

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request)

    assert "backend undefined ORM Item model" not in missing


def test_create_patch_coverage_accepts_fastapi_depends_session_default() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/store_api.py",
            operation="create",
            content=(
                "from fastapi import APIRouter, Depends\n"
                "from sqlalchemy.orm import Session\n"
                "from app.db import Base\n"
                "router = APIRouter()\n"
                "def get_db_session():\n"
                "    yield object()\n"
                "class Product(Base):\n"
                "    __tablename__ = 'products'\n"
                "@router.get('/api/products')\n"
                "def list_products(session: Session = Depends(get_db_session)): return []\n"
                "@router.post('/api/products')\n"
                "def create_product(payload: dict, session: Session = Depends(get_db_session)): return payload\n"
                "@router.patch('/api/products/{product_id}')\n"
                "def update_product(product_id: int, payload: dict, session: Session = Depends(get_db_session)): return payload\n"
            ),
            reason="test",
        ),
        DraftFileOperation(file_path="miniapp/app/main.py", operation="replace", content="Base.metadata.create_all(bind=engine)", reason="test"),
    ]

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request)

    assert "backend FastAPI SQLAlchemy Session dependency via Depends(get_db_session)" not in missing
    assert "backend FastAPI generator dependency without @contextmanager" not in missing


def test_create_patch_coverage_rejects_contextmanager_fastapi_dependency() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.FAST)
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/store_api.py",
            operation="create",
            content=(
                "from contextlib import contextmanager\n"
                "from fastapi import APIRouter, Depends\n"
                "from sqlalchemy.orm import Session\n"
                "router = APIRouter()\n"
                "@contextmanager\n"
                "def get_db_session():\n"
                "    yield object()\n"
                "@router.get('/api/products')\n"
                "def list_products(session: Session = Depends(get_db_session)): return []\n"
                "@router.post('/api/products')\n"
                "def create_product(payload: dict, session: Session = Depends(get_db_session)): return payload\n"
                "@router.patch('/api/products/{product_id}')\n"
                "def update_product(product_id: int, payload: dict, session: Session = Depends(get_db_session)): return payload\n"
            ),
            reason="test",
        ),
    ]

    missing = WorkspaceCodeAgentRuntime._create_patch_coverage_gap(operations, request=request)

    assert "backend FastAPI generator dependency without @contextmanager" in missing


def test_quality_create_patch_coverage_requires_deeper_child_pages_per_role() -> None:
    request = GenerateRequest(prompt="Create a store", intent="create", generation_mode=GenerationMode.QUALITY)

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


def test_completion_budgets_are_time_and_token_based() -> None:
    fast_budget = WorkspaceCodeAgentRuntime._completion_budget_for_mode(GenerationMode.FAST)
    balanced_budget = WorkspaceCodeAgentRuntime._completion_budget_for_mode(GenerationMode.BALANCED)
    quality_budget = WorkspaceCodeAgentRuntime._completion_budget_for_mode(GenerationMode.QUALITY)

    assert fast_budget["time_limit_ms"] == 18 * 60 * 1000
    assert fast_budget["token_limit"] == 600_000
    assert balanced_budget["time_limit_ms"] == 25 * 60 * 1000
    assert balanced_budget["token_limit"] == 1_200_000
    assert quality_budget["time_limit_ms"] == 45 * 60 * 1000
    assert quality_budget["token_limit"] == 2_500_000
    assert WorkspaceCodeAgentRuntime._max_attempts(GenerationMode.FAST) >= 50


def test_terminal_failure_progress_is_100_for_ui_status_separation() -> None:
    assert RunService._terminal_failure_progress(0) == 100
    assert WorkspaceCodeAgentRuntime._run_progress_for_event("job_failed")[1] == 100


def test_run_save_preserves_live_token_usage_and_keeps_terminal_failure_at_100(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    run = RunRecord(
        workspace_id="ws",
        prompt="Create app",
        intent="create",
        status="running",
        token_usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "turn_count": 1},
        budget_status={"total_tokens": 0, "token_limit": 600_000, "exhausted": False},
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

    assert saved.progress_percent == 100
    assert saved.linked_job_id == "job_live"
    assert saved.token_usage["total_tokens"] == 15
    assert saved.token_usage["turn_count"] == 1
    assert saved.budget_status["total_tokens"] == 15


def test_stale_run_recovery_retains_nonempty_draft_for_resume(tmp_path) -> None:
    class FakeWorkspaceService:
        def draft_exists(self, workspace_id: str, run_id: str) -> bool:
            return workspace_id == "ws" and run_id == "run_stale"

        def diff(self, workspace_id: str, *, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return "diff --git a/miniapp/app/static/client/index.html b/miniapp/app/static/client/index.html\n"

    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    service.workspace_service = FakeWorkspaceService()
    run = RunRecord(
        run_id="run_stale",
        workspace_id="ws",
        prompt="Create shop",
        intent="create",
        status="running",
        apply_status="pending",
        current_stage="repairing generated app",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="fast",
        generation_mode=GenerationMode.FAST,
    )

    recovered = service._recover_stale_run_with_retained_draft(run)

    assert recovered
    assert run.status == "blocked"
    assert run.apply_status == "blocked"
    assert run.draft_status == "ready"
    assert run.draft_ready is True
    assert run.failure_class == "generation.interrupted_stale_worker"
    checkpoint = service.store.get("reports", "resume_checkpoint:ws")
    assert checkpoint["status"] == "pending"
    assert checkpoint["source_run_id"] == "run_stale"
    assert checkpoint["generation_mode"] == "fast"


def test_completed_source_run_closes_stale_resume_checkpoint_without_duplicate_run(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    service.workspace_log_service = SimpleNamespace(
        append=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resume log should not be written"))
    )
    service.create_run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate resume run queued"))
    service._append_job_event = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resume job event should not be written"))

    run = RunRecord(
        run_id="run_source",
        workspace_id="ws",
        prompt="Create shop",
        intent="create",
        status="completed",
        apply_status="applied",
        linked_job_id="job_source",
        target_role_scope=["client", "specialist", "manager"],
    )
    service.store.upsert(
        "reports",
        "resume_checkpoint:ws",
        {
            "status": "pending",
            "source_run_id": "run_source",
            "prompt": "Create shop",
            "intent": "create",
            "generation_mode": "balanced",
        },
    )

    service._queue_resume_generation_from_checkpoint_if_needed(
        run,
        CreateRunRequest(prompt="Create shop", mode="generate", apply_strategy="staged_auto_apply"),
    )

    checkpoint = service.store.get("reports", "resume_checkpoint:ws")
    assert checkpoint["status"] == "completed"
    assert checkpoint["completed_by_run_id"] == "run_source"
    assert checkpoint["completion_reason"] == "source_run_completed_applied"


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

    assert hydrated.progress_percent == 100
    assert hydrated.linked_job_id == job.job_id
    assert hydrated.token_usage["total_tokens"] == 1200
    assert hydrated.token_usage["turn_count"] == 1
    assert "no such table: bookings" in hydrated.failure_reason


def test_get_run_surfaces_failed_retained_draft_as_blocked(tmp_path) -> None:
    service = object.__new__(RunService)
    service.store = StateStore(tmp_path / "state.json")
    run = RunRecord(
        workspace_id="ws",
        prompt="Create app",
        intent="create",
        status="failed",
        apply_status="failed",
        current_stage="failed",
        progress_percent=98,
        outcome_kind="blocked_generation",
        draft_status="ready",
        draft_ready=True,
        failure_reason="platform_invariants: frontend_missing_update_api",
    )
    service.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    hydrated = service.get_run(run.run_id)

    assert hydrated.status == "blocked"
    assert hydrated.apply_status == "blocked"
    assert hydrated.current_stage == "blocked"
    assert hydrated.draft_ready is True


def test_get_run_keeps_explicit_zero_token_usage_from_linked_job(tmp_path) -> None:
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
    assert "smallest coherent failing workflow slice" in str(correction["required_next_action"])
    assert "touch only the concrete failed files/checks" in str(correction["required_next_action"])
    assert "compact but complete prompt-derived app surface" in str(correction["required_next_action"])
    assert "align backend API routes" in str(correction["required_next_action"])
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
    assert "compact working product" in str(budget["required_next_action"])
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
    ) == ("Running validation checks", 59)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "frontend_build_started",
        details={
            "attempt": 1,
            "has_file_edits": True,
            "check_step": "connectivity_validators",
            "check_status": "started",
        },
    ) == ("Checking frontend API connectivity", 64)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "frontend_build_started",
        details={
            "attempt": 1,
            "has_file_edits": True,
            "check_step": "platform_invariants",
            "check_status": "passed",
            "duration_ms": 1530,
        },
    ) == ("Passed role workflow invariants (1.5s)", 71)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 2, "outcome": "no_progress", "operation_count": 0, "tool_request_count": 0},
    ) == ("No file edits returned", 45)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "tool_request", "operation_count": 0, "tool_request_count": 2},
    ) == ("Requested 2 context reads", 41)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "iteration_ready",
        details={"attempt": 1, "outcome": "patch_ready", "operation_count": 8, "tool_request_count": 0},
    ) == ("Prepared 8 file edits", 45)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "agent_turn_started",
        details={"attempt": 1, "phase": "context_ready"},
    ) == ("Prepared edit context 1", 30)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "agent_turn_started",
        details={"attempt": 1, "phase": "model_request"},
    ) == ("Generating code edit 1", 36)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "patch_apply_started",
        details={"attempt": 2, "files": ["miniapp/app/main.py"], "first_patch": True, "has_draft_diff": False},
    ) == ("Applying patch • 1 file", 52)
    assert WorkspaceCodeAgentRuntime._run_progress_for_event(
        "patch_apply_started",
        details={"attempt": 2, "files": ["miniapp/app/main.py"], "has_draft_diff": True},
    ) == ("Applying patch • 1 file", 87)
    assert not WorkspaceCodeAgentRuntime._should_update_run_stage(
        "running_checks",
        progress=19,
        existing_progress=37,
    )
    assert WorkspaceCodeAgentRuntime._should_update_run_stage(
        "frontend_build_started",
        progress=64,
        existing_progress=82,
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
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(
        GenerationMode.FAST,
        intent="edit",
        focused_edit_kind="visual_style_edit",
    ) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": FOCUSED_VISUAL_CONTENT_MAX_LENGTH,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="edit") == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 14000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 24000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="create") == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 28000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.FAST, intent="create", repair_turn=True) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 18000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED, intent="create") == {
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 36000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED, intent="create", repair_turn=True) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 22000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY, intent="create") == {
        "reasoning": {"effort": "high"},
        "max_output_tokens": 52000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY, intent="create", repair_turn=True) == {
        "reasoning": {"effort": "low"},
        "max_output_tokens": 26000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.BALANCED) == {
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 32000,
    }
    assert WorkspaceCodeAgentRuntime._agent_turn_tuning(GenerationMode.QUALITY) == {
        "reasoning": {"effort": "high"},
        "max_output_tokens": 42000,
    }


def test_agent_quality_from_execution_keeps_check_diagnostics() -> None:
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


def test_run_service_has_no_hidden_fallback_or_followup_completion_paths() -> None:
    removed_helpers = [
        "_should_auto_fix_failed_generate",
        "_build_auto_fix_request",
        "_complete_blocked_noop_run_from_green_source",
        "_complete_failed_run_from_green_draft",
        "_is_noop_loop_failure",
        "_should_queue_async_followup_verification",
        "_launch_async_followup_verification",
        "_run_async_followup_verification",
        "_should_apply_best_effort_after_failed_repairs",
        "_should_keep_draft_for_manual_review",
    ]
    for helper in removed_helpers:
        assert not hasattr(RunService, helper)


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


def test_prompt_alignment_does_not_block_css_only_visual_edits() -> None:
    class FakeWorkspaceService:
        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return (
                "diff --git a/miniapp/app/static/client/styles.css b/miniapp/app/static/client/styles.css\n"
                "diff --git a/miniapp/app/static/manager/styles.css b/miniapp/app/static/manager/styles.css\n"
            )

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str | None:
            del workspace_id, path, run_id
            return ".page-shell { color: #1f2937; background: #ffffff; }\n.primary-action { background: #6d28d9; }"

    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    runtime.workspace_service = FakeWorkspaceService()

    result = runtime._prompt_alignment_smoke(
        workspace_id="ws",
        run_id="run",
        prompt="Поменяй стиль и цвета на фиолетовый",
        intent="edit",
        focused_edit_kind="visual_style_edit",
    )

    assert result.status == "skipped"
    assert "visual/style edits" in result.details


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
                "reason": "Use full content when a separate patch is also provided.",
            }
        ]
    )

    assert operations[0].operation == "replace"
    assert operations[0].content == 'const role = "client";\nwindow.setupPreviewBridge?.(role);\n'


def test_create_with_full_file_text_in_diff_is_coerced_to_content() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    operations = runtime._coerce_operations(
        [
            {
                "file_path": "miniapp/tests/test_generated_app.py",
                "operation": "create",
                "content": None,
                "diff": "import unittest\n\nclass GeneratedAppTests(unittest.TestCase):\n    def test_smoke(self):\n        self.assertTrue(True)\n",
                "reason": "Create generated tests.",
            }
        ]
    )

    assert operations[0].operation == "create"
    assert operations[0].diff is None
    assert "GeneratedAppTests" in str(operations[0].content)


def test_replace_with_unified_diff_in_diff_field_is_coerced_to_patch() -> None:
    runtime = object.__new__(WorkspaceCodeAgentRuntime)
    operations = runtime._coerce_operations(
        [
            {
                "file_path": "miniapp/app/static/client/app.js",
                "operation": "replace",
                "content": None,
                "diff": "--- a/miniapp/app/static/client/app.js\n+++ b/miniapp/app/static/client/app.js\n@@\n-old\n+new\n",
                "reason": "Patch existing JS.",
            }
        ]
    )

    assert operations[0].operation == "patch"
    assert operations[0].content is None
    assert str(operations[0].diff).startswith("--- a/miniapp/app/static/client/app.js")


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
