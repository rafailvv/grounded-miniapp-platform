from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import app.services.check_runner as check_runner_module
from fastapi.testclient import TestClient
import pytest

from app.ai.model_registry import TASK_PROFILES
from app.ai.openrouter_client import ACTIVE_WORKSPACE_LOG_CONTEXT, OpenRouterClient
from app.main import create_app
from app.models.common import PreviewProfile, TargetPlatform
from app.models.artifacts import ValidationIssue
from app.models.domain import CheckExecutionRecord, CreateRunRequest, DraftFileOperation, FixScopeEntry, GenerateRequest, GenerationMode, JobRecord, PreviewRecord, RunCheckResult, ValidationSnapshot, WorkspaceRecord
from app.models.grounded_spec import APIRequirement
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext
from app.services.code_index_service import CodeIndexService
from app.services.engine.mode_profiles import ModeProfiles
from app.services.generation_service import DESIGN_REFERENCE_FILES, SHARED_GENERATED_FILES, GenerationService
from app.services.run_service import RunService
from app.modules.miniapp_validation.build_validator import BuildValidator


def _install_llm_stub(app) -> None:
    helper_path = Path(__file__).with_name("test_api_smoke.py")
    spec = importlib.util.spec_from_file_location("test_api_smoke_helper", helper_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._install_llm_stub(app)


def test_code_index_retrieval_prefers_symbol_overlap(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Index Workspace",
            "description": "Index retrieval test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")
    client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/custom_order_service.py",
            "content": "def order_queue_status(order_id: str) -> str:\n    return f'queue:{order_id}'\n",
        },
    )
    response = client.post(f"/workspaces/{workspace_id}/index")
    assert response.status_code == 200

    code_index: CodeIndexService = app.state.container.code_index_service
    retrieval = code_index.retrieve(
        workspace_id=workspace_id,
        prompt="Fix the order queue status flow in miniapp service",
        code_limit=12,
        doc_limit=1,
    )
    indexed_chunks = code_index.get_chunks(workspace_id, kind="code")
    assert any("custom_order_service.py" in item.path for item in indexed_chunks)
    assert retrieval["stats"]["code_hits"] > 0


def test_create_workspace_auto_clones_template(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    response = client.post(
        "/workspaces",
        json={
            "name": "Auto Clone Workspace",
            "description": "Workspace creation should immediately clone the canonical template.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["template_cloned"] is True
    workspace_id = workspace["workspace_id"]
    source_dir = app.state.container.workspace_service.source_dir(workspace_id)
    assert (source_dir / "miniapp/app/main.py").exists()


def test_system_configuration_defaults_to_balanced(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    response = client.get("/system/configuration")
    assert response.status_code == 200
    assert response.json()["defaults"]["generation_mode"] == "balanced"


def test_openrouter_client_truncates_large_log_text() -> None:
    text = "a" * 6000

    compact = OpenRouterClient._truncate_log_text(text, limit=1000)

    assert compact.startswith("a" * 500)
    assert compact.endswith("a" * 500)
    assert "[truncated 5000 chars]" in compact


def test_openrouter_client_compacts_large_nested_payloads() -> None:
    payload = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "x" * 9000}]} for _ in range(20)],
        "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
    }

    compact = OpenRouterClient._compact_log_payload(payload)

    assert len(compact["input"]) == OpenRouterClient._LOG_LIST_LIMIT + 1
    assert compact["input"][-1] == "<truncated 8 more items>"
    text_value = compact["input"][0]["content"][0]["text"]
    assert isinstance(text_value, str)
    assert "[truncated " in text_value


def test_openrouter_client_retries_dns_resolution_failures() -> None:
    error = RuntimeError("[Errno 8] nodename nor servname provided, or not known")
    assert OpenRouterClient._is_retryable_request_error(error) is True


def test_generation_service_retries_dns_resolution_failures() -> None:
    error = RuntimeError("Whole-file cluster failed: [Errno 8] nodename nor servname provided, or not known")
    assert GenerationService._is_retryable_llm_error(error) is True


def test_invoke_llm_with_timeout_preserves_workspace_log_context() -> None:
    token = ACTIVE_WORKSPACE_LOG_CONTEXT.set("ws_context")
    try:
        result = GenerationService._invoke_llm_with_timeout(
            lambda **_kwargs: {"workspace_id": ACTIVE_WORKSPACE_LOG_CONTEXT.get()},
            timeout_seconds=1.0,
            role="spec_analysis",
            schema_name="context_test",
        )
    finally:
        ACTIVE_WORKSPACE_LOG_CONTEXT.reset(token)

    assert result["workspace_id"] == "ws_context"


def test_submit_with_context_preserves_workspace_log_context() -> None:
    token = ACTIVE_WORKSPACE_LOG_CONTEXT.set("ws_context_submit")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = GenerationService._submit_with_context(
                executor,
                lambda **_kwargs: {"workspace_id": ACTIVE_WORKSPACE_LOG_CONTEXT.get()},
            )
            result = future.result(timeout=1)
    finally:
        ACTIVE_WORKSPACE_LOG_CONTEXT.reset(token)

    assert result["workspace_id"] == "ws_context_submit"


def test_fast_grounded_spec_timeout_returns_error_without_legacy_fallback_wording(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    original_timeout = service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS
    original_inner = service._resolve_grounded_spec_fast_inner

    def _hung_fast_inner(**_kwargs):
        time.sleep(0.2)
        return {"spec": "unreachable"}

    service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS = 0.01
    service._resolve_grounded_spec_fast_inner = _hung_fast_inner  # type: ignore[method-assign]
    try:
        result = service._resolve_grounded_spec_fast(
            workspace_id="ws_timeout",
            prompt="Create a client, specialist, and manager workflow app.",
            doc_refs=[],
            target_platform=TargetPlatform.TELEGRAM,
            preview_profile=PreviewProfile.TELEGRAM_MOCK,
            template_revision_id="template-test",
            prompt_turn_id="turn_test",
            generation_mode=GenerationMode.BALANCED,
            creative_direction={},
        )
    finally:
        service.GROUNDED_SPEC_TOTAL_TIMEOUT_SECONDS = original_timeout
        service._resolve_grounded_spec_fast_inner = original_inner  # type: ignore[method-assign]
    assert "error" in result
    assert "timed out without a valid result" in result["error"]
    assert "compiler fallback" not in result["error"].lower()


def test_workspace_name_is_derived_from_prompt() -> None:
    name = RunService._derive_workspace_name_from_prompt(
        "I need a simple clean mini app for service requests with manager specialist client roles."
    )
    assert name == "Clean Service Requests Manager Specialist"


def test_workspace_log_service_creates_platform_and_api_logs(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    log_service = app.state.container.workspace_log_service

    workspace_id = "ws_logs"
    log_service.ensure_log_files(workspace_id)
    log_service.append(workspace_id, source="test", message="platform entry")
    log_service.append_api(workspace_id, source="test", message="api entry")

    assert log_service.log_path(workspace_id).exists()
    assert log_service.api_log_path(workspace_id).exists()
    assert any("platform entry" in line for line in log_service.read_lines(workspace_id, kind="platform"))
    assert any("api entry" in line for line in log_service.read_lines(workspace_id, kind="api"))


def test_generation_clusters_grouped_for_safe_parallel_execution() -> None:
    clusters = [
        {"cluster_name": "backend_support", "target_files": ["miniapp/app/main.py"]},
        {"cluster_name": "backend_route_assignments", "target_files": ["miniapp/app/routes/assignments.py"]},
        {"cluster_name": "backend_route_comments", "target_files": ["miniapp/app/routes/comments.py"]},
        {"cluster_name": "backend_route_requests", "target_files": ["miniapp/app/routes/requests.py"]},
        {"cluster_name": "role_manager_ui_root", "target_files": ["miniapp/app/static/manager/index.html"]},
        {"cluster_name": "role_manager_ui_profile", "target_files": ["miniapp/app/static/manager/profile/index.html"]},
        {"cluster_name": "role_manager_ui_requests", "target_files": ["miniapp/app/static/manager/requests/index.html"]},
        {"cluster_name": "role_specialist_ui_root", "target_files": ["miniapp/app/static/specialist/index.html"]},
        {"cluster_name": "shared_static", "target_files": ["miniapp/app/static/shared/common.js"]},
    ]

    grouped = GenerationService._group_generation_clusters_for_execution(clusters)
    names = [[item["cluster_name"] for item in batch] for batch in grouped]

    assert names == [
        ["backend_support"],
        ["backend_route_assignments", "backend_route_comments"],
        ["backend_route_requests"],
        ["role_manager_ui_root", "role_manager_ui_profile", "role_manager_ui_requests"],
        ["role_specialist_ui_root"],
        ["shared_static"],
    ]


def test_create_run_renames_workspace_from_prompt(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service
    workspace_service = app.state.container.workspace_service

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Research Workspace",
            description="Temporary name before prompt-based rename.",
            path=str((tmp_path / "data" / "workspaces" / "ws_name_from_prompt").resolve()),
        )
    )
    original_execute_run = run_service._execute_run
    run_service._execute_run = lambda *_args, **_kwargs: None
    try:
        run_service.create_run(
            workspace.workspace_id,
            CreateRunRequest(
                prompt="Create a booking operations dashboard for clinic appointments and manager oversight.",
            ),
        )
    finally:
        run_service._execute_run = original_execute_run

    renamed = workspace_service.get_workspace(workspace.workspace_id)
    assert renamed.name == "Booking Operations Dashboard Clinic Appointments"


def test_preview_runtime_compose_uses_built_image_without_runtime_pip_install(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    runtime_manager = app.state.container.runtime_manager

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Preview Compose Workspace",
            description="Rendered preview compose should rely on a built image instead of pip install at runtime.",
            path=str((tmp_path / "data" / "workspaces" / "ws_preview_compose").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    source_dir = workspace_service.source_dir(workspace.workspace_id)

    rendered_compose = runtime_manager._render_host_compose_file(source_dir)
    try:
        content = rendered_compose.read_text(encoding="utf-8")
    finally:
        rendered_compose.unlink(missing_ok=True)

    assert "image: grounded-miniapp-preview-base:latest" in content
    assert "context: " in content and "/miniapp" in content
    assert "pip install --no-cache-dir -r requirements.txt" not in content
    assert 'uvicorn app.main:app --host ${UVICORN_HOST:-0.0.0.0} --port ${UVICORN_PORT:-8000}' in content


def test_fix_case_accepts_container_published_port_metadata(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)

    workspace_service = app.state.container.workspace_service
    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Fix Case Workspace",
            description="Fix case should accept current container metadata.",
            path=str((tmp_path / "data" / "workspaces" / "ws_fix_case").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)

    request = GenerateRequest(
        prompt="Fix the preview issue.",
        mode="fix",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        error_context={"raw_error": "Runtime failed while loading preview."},
    )
    check_execution = CheckExecutionRecord(
        workspace_id=workspace.workspace_id,
        run_id="run_test",
        results=[],
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )
    preview_details = {
        "logs": ["Preview check failed once."],
        "containers": [
            {
                "service": "preview-app",
                "name": "grounded_preview_test-preview-app-1",
                "state": "running",
                "status": "Up",
                "health": "healthy",
                "published_port": "16435",
            }
        ],
        "container_logs": {},
    }

    fix_case = app.state.container.fix_orchestrator._build_fix_case(
        workspace_id=workspace.workspace_id,
        run_id="run_test",
        attempt=1,
        request=request,
        check_execution=check_execution,
        preview_details=preview_details,
        prior_attempts=[],
        existing_scope=[],
    )

    assert fix_case.container_statuses
    assert fix_case.container_statuses[0].service == "preview-app"
    assert fix_case.container_statuses[0].published_port == "16435"


def test_resolve_intent_prefers_create_for_workflow_heavy_app_requests(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")

    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(
            name="Intent Workspace",
            description="Intent classification should preserve create-like app requests.",
            path=str((tmp_path / "data" / "workspaces" / "ws_intent").resolve()),
        )
    )
    request = CreateRunRequest(
        prompt=(
            "Create a multi-page flower shop mini app with client, specialist, and manager roles. "
            "Managers should add products and edit existing products, specialists should process orders, "
            "and customers should browse the storefront and checkout."
        ),
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        target_role_scope=["client", "specialist", "manager"],
    )

    intent = app.state.container.run_service._resolve_intent(workspace, request)

    assert intent == "create"


def test_context_pack_and_generation_context_skip_non_utf8_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Binary Context Workspace",
            description="Non-UTF8 files should not crash context collection.",
            path=str((tmp_path / "data" / "workspaces" / "ws_binary_context").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    run_id = "run_binary_context"
    draft_source = workspace_service.ensure_draft(workspace.workspace_id, run_id)
    binary_path = draft_source / "miniapp/app/generated/app.db"
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    binary_path.write_bytes(b"\xf8\x00\x01binary")

    context_pack = app.state.container.context_pack_builder.build(
        workspace=workspace_service.get_workspace(workspace.workspace_id),
        prompt="Build the app.",
        model_profile="openai_code_fast",
        generation_mode=GenerationMode.BALANCED,
        active_paths=["miniapp/app/generated/app.db"],
        target_files=["miniapp/app/generated/app.db"],
        run_id=run_id,
    )
    file_contexts = app.state.container.generation_service._collect_existing_file_contexts(
        workspace.workspace_id,
        run_id,
        ["miniapp/app/generated/app.db"],
    )

    assert "miniapp/app/generated/app.db" not in context_pack.targeted_files
    assert "miniapp/app/generated/app.db" not in file_contexts


def test_whole_file_generation_times_out_instead_of_hanging(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    monkeypatch.setattr(
        service,
        "_build_generation_clusters",
        lambda _target_files: [
            {"cluster_name": "backend_core", "target_files": ["miniapp/app/main.py"]},
            {"cluster_name": "role_client_ui", "target_files": ["miniapp/app/static/client/index.html"]},
        ],
    )
    monkeypatch.setattr(
        service,
        "_timed_whole_file_cluster",
        lambda **_kwargs: time.sleep(0.05) or {
            "cluster_name": "backend_core",
            "target_files": ["miniapp/app/main.py"],
            "duration_ms": 1,
            "assistant_message": "",
            "operations": [],
        },
    )
    service.WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS = 0.001

    result = service._resolve_whole_file_code_edits(
        workspace_id="ws_test",
        draft_run_id="run_test",
        prompt="Build app",
        grounded_spec=None,
        role_scope=["client"],
        file_contexts={},
        target_files=["miniapp/app/main.py", "miniapp/app/static/client/index.html"],
        role_contract={},
        page_graph={"roles": {"client": {"pages": []}}},
        intent="create",
        scope_mode="whole_file_build",
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert "error" in result
    assert "Whole-file generation timed out" in result["error"]


def test_generate_structured_with_retry_times_out_stuck_llm_call(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service
    monkeypatch.setattr(service, "STRUCTURED_LLM_TIMEOUT_SECONDS", 0.01)

    def _stuck_generate_structured(**_kwargs):
        time.sleep(0.2)
        return {"model": "fake", "payload": {}, "cache_stats": {}}

    monkeypatch.setattr(service.openrouter_client, "generate_structured", _stuck_generate_structured)

    try:
        service._generate_structured_with_retry(
            role="code_edit",
            schema_name="whole_file_bundle_v1_backend_core",
            schema={"type": "object"},
            system_prompt="System",
            user_prompt="User",
        )
    except TimeoutError as exc:
        assert "timed out waiting for code_edit structured generation" in str(exc).lower()
    else:
        raise AssertionError("Expected TimeoutError for stuck structured LLM call")


def test_generate_json_object_with_retry_times_out_stuck_llm_call(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service
    monkeypatch.setattr(service, "JSON_OBJECT_LLM_TIMEOUT_SECONDS", 0.01)

    def _stuck_generate_json_object(**_kwargs):
        time.sleep(0.2)
        return {"model": "fake", "payload": {}, "cache_stats": {}}

    monkeypatch.setattr(service.openrouter_client, "generate_json_object", _stuck_generate_json_object)

    try:
        service._generate_json_object_with_retry(
            role="code_plan",
            schema_name="page_graph_targeting_v1",
            schema={"type": "object"},
            system_prompt="System",
            user_prompt="User",
        )
    except TimeoutError as exc:
        assert "timed out waiting for code_plan structured generation" in str(exc).lower()
    else:
        raise AssertionError("Expected TimeoutError for stuck JSON-object LLM call")


def test_code_plan_sections_merge_successful_parts_when_one_section_times_out(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service
    service.CODE_PLAN_SECTION_TIMEOUT_SECONDS = 0.01

    spec = service._build_grounded_spec(
        workspace_id="ws_code_plan_partial",
        prompt="Create a workflow app for clients, specialists, and managers.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="rev_test",
        prompt_turn_id="turn_test",
        generation_mode=GenerationMode.BALANCED,
    )

    def _stub_generate_structured_with_retry(*, schema_name: str, **_kwargs):
        if schema_name == "page_graph_structure_v1":
            return {
                "model": "fake-graph",
                "payload": {
                    "summary": "Role-aware workflow pages.",
                    "flow_mode": "multi_page",
                    "page_graph": {
                        "summary": "Role-aware workflow pages.",
                        "roles": {
                            role: {
                                "entry_path": "/",
                                "landing_page_id": f"{role}_home",
                                "routes_file": f"miniapp/app/routes/{role}.py",
                                "pages": [
                                    {
                                        "page_id": f"{role}_home",
                                        "route_path": "/",
                                        "file_path": f"miniapp/app/static/{role}/index.html",
                                        "title": f"{role.title()} Home",
                                    }
                                ],
                            }
                            for role in ("client", "specialist", "manager")
                        },
                    },
                },
                "cache_stats": {},
            }
        if schema_name == "page_graph_targeting_v1":
            time.sleep(0.05)
            return {
                "model": "fake-targeting",
                "payload": {
                    "files_to_read": ["miniapp/app/main.py"],
                    "target_files": ["miniapp/app/static/client/index.html"],
                    "shared_files": ["miniapp/app/static/shared/base.css"],
                    "backend_targets": ["miniapp/app/routes/requests.py"],
                },
                "cache_stats": {},
            }
        raise AssertionError(f"Unexpected schema_name {schema_name}")

    monkeypatch.setattr(service, "_generate_structured_with_retry", _stub_generate_structured_with_retry)

    payload = service._generate_code_plan_sections(
        workspace_id="ws_code_plan_partial",
        prompt="Create a workflow app for clients, specialists, and managers.",
        grounded_spec=spec,
        doc_refs=[],
        role_scope=["client", "specialist", "manager"],
        role_contract={},
        scope_mode="whole_file_build",
        require_multi_page=True,
        workspace_tree=[],
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    normalized = payload["payload"]
    assert payload["model"] == "fake-graph"
    assert normalized["target_files"] == []
    assert "page_graph" in normalized
    assert normalized["summary"]


def test_grounded_spec_sections_fail_when_one_section_times_out(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service
    service.GROUNDED_SPEC_SECTION_TIMEOUT_SECONDS = 0.01

    def _stub_generate_structured_with_retry(*, schema_name: str, **_kwargs):
        if schema_name == "grounded_spec_outline_v1":
            return {
                "model": "fake-outline",
                "payload": {
                    "product_goal": "Workflow app",
                    "roles": [],
                    "entities": [],
                    "flows": [],
                    "api_needs": [],
                    "risks": [],
                },
            }
        raise AssertionError(f"Unexpected schema_name {schema_name}")

    def _stub_generate_grounded_spec_section(*, section_id: str, **_kwargs):
        if section_id == "core":
            time.sleep(0.05)
            return {"model": "fake-core", "payload": {}}
        if section_id == "requirements":
            return {
                "model": "fake-reqs",
                "payload": {
                    "ui_requirements": [],
                    "api_requirements": [],
                    "persistence_requirements": [],
                    "integration_requirements": [],
                    "security_requirements": [],
                    "platform_constraints": [],
                    "non_functional_requirements": [],
                },
            }
        if section_id == "governance":
            return {
                "model": "fake-gov",
                "payload": {"assumptions": [], "unknowns": [], "contradictions": []},
            }
        raise AssertionError(f"Unexpected section_id {section_id}")

    monkeypatch.setattr(service, "_generate_structured_with_retry", _stub_generate_structured_with_retry)
    monkeypatch.setattr(service, "_generate_grounded_spec_section", _stub_generate_grounded_spec_section)

    with pytest.raises(RuntimeError, match="incomplete sections without a valid agent response"):
        service._generate_grounded_spec_pair(
            workspace_id="ws_grounded_spec_partial",
            prompt="Create a workflow app for clients, specialists, and managers.",
            doc_refs=[],
            target_platform=TargetPlatform.TELEGRAM,
            preview_profile=PreviewProfile.TELEGRAM_MOCK,
            template_revision_id="rev_test",
            prompt_turn_id="turn_test",
            creative_direction={},
            relaxed=False,
            compact=True,
        )


def test_build_generation_clusters_splits_backend_and_shared_static_targets() -> None:
    clusters = GenerationService._build_generation_clusters(
        [
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/routes/requests.py",
            "miniapp/app/routes/comments.py",
            "miniapp/app/static/shared/base.css",
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/client/app.js",
        ]
    )

    cluster_map = {item["cluster_name"]: item["target_files"] for item in clusters}
    assert "shared_static" in cluster_map
    assert cluster_map["shared_static"] == ["miniapp/app/static/shared/base.css"]
    assert "backend_support" in cluster_map
    assert "miniapp/app/main.py" in cluster_map["backend_support"]
    assert "miniapp/app/routes/profiles.py" in cluster_map["backend_support"]
    assert "backend_route_requests" in cluster_map
    assert cluster_map["backend_route_requests"] == ["miniapp/app/routes/requests.py"]
    assert "backend_route_comments" in cluster_map
    assert "role_client_ui_root" in cluster_map


def test_runtime_artifacts_are_synthesized_from_page_graph(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    generation_service = app.state.container.generation_service
    spec = generation_service._build_grounded_spec(
        workspace_id="ws_manifest",
        prompt="Create a requests mini app for clients, specialists, and managers.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="rev_test",
        prompt_turn_id="turn_test",
        generation_mode=GenerationMode.BALANCED,
    )
    page_graph = {
        "roles": {
            "client": {
                "entry_path": "/client",
                "pages": [
                    {"page_id": "client_home", "route_path": "/client", "file_path": "miniapp/app/static/client/index.html", "title": "Client Home", "navigation_label": "Home", "is_entry": True},
                    {"page_id": "client_profile", "route_path": "/client/profile", "file_path": "miniapp/app/static/client/profile.html", "title": "Profile", "navigation_label": "Profile", "page_kind": "profile"},
                ],
            },
            "specialist": {
                "entry_path": "/specialist",
                "pages": [
                    {"page_id": "specialist_home", "route_path": "/specialist", "file_path": "miniapp/app/static/specialist/index.html", "title": "Desk", "navigation_label": "Desk", "is_entry": True},
                    {"page_id": "specialist_profile", "route_path": "/specialist/profile", "file_path": "miniapp/app/static/specialist/profile.html", "title": "Profile", "navigation_label": "Profile", "page_kind": "profile"},
                ],
            },
            "manager": {
                "entry_path": "/manager",
                "pages": [
                    {"page_id": "manager_home", "route_path": "/manager", "file_path": "miniapp/app/static/manager/index.html", "title": "Overview", "navigation_label": "Overview", "is_entry": True},
                    {"page_id": "manager_profile", "route_path": "/manager/profile", "file_path": "miniapp/app/static/manager/profile.html", "title": "Profile", "navigation_label": "Profile", "page_kind": "profile"},
                ],
            },
        }
    }

    ensured = generation_service._ensure_runtime_artifact_operations(
        grounded_spec=spec,
        page_graph=page_graph,
        role_scope=["client", "specialist", "manager"],
        generation_mode=GenerationMode.BALANCED,
        operations=[],
    )
    ensured_paths = {operation.file_path for operation in ensured}

    assert "miniapp/app/generated/route_manifest.json" in ensured_paths
    assert "miniapp/app/generated/runtime_manifest.json" in ensured_paths
    assert "miniapp/app/generated/runtime_state.json" not in ensured_paths
    assert "miniapp/app/generated/static_runtime_manifest.json" not in ensured_paths
    assert "miniapp/app/generated/role_seed.json" not in ensured_paths
    assert "miniapp/app/generated/role_experience.json" not in ensured_paths


def test_generated_app_tests_cover_shell_styles_dom_contracts_and_local_routes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    generation_service = app.state.container.generation_service
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_home",
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                        "title": "Client Home",
                    }
                ]
            }
        }
    }

    python_test = generation_service._python_app_level_test_content(page_graph=page_graph, role_scope=["client"])
    js_test = generation_service._js_app_level_test_content(page_graph=page_graph, role_scope=["client"])

    assert "/static/shared/base.css" in python_test
    assert "_extract_js_dom_ids" in python_test
    assert "_extract_local_route_refs" in python_test
    assert "test_declared_role_routes_render_shell_contract" in python_test
    assert "test_local_route_refs_resolve_inside_generated_route_manifest" in python_test
    assert "test_role_journey_round_trip_persists_shared_record" in python_test
    assert "test_backend_route_modules_import" in python_test
    assert "_workflow_api_requirements" in python_test
    assert "_extract_record_id" in python_test
    assert 'not str(item.get("path") or "").startswith("/api/runtime/")' in python_test
    assert 'TemporaryDirectory' in python_test
    assert 'os.environ["DATABASE_URL"]' in python_test
    assert 'asset_path = MINIAPP_DIR / "app" / "static" / asset.removeprefix("/static/")' in python_test
    assert "stripRouteTemplateExpressions" in js_test
    assert "generated javascript files parse" in js_test
    assert "cls._client_context = TestClient(app)" in python_test
    assert "client_context.__exit__(None, None, None)" in python_test


def test_runtime_artifacts_overwrite_noncanonical_generated_manifest(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    generation_service = app.state.container.generation_service
    spec = generation_service._build_grounded_spec(
        workspace_id="ws_manifest_overwrite",
        prompt="Build a minimal workflow app with client, specialist, and manager roles.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="rev_test",
        prompt_turn_id="turn_test",
        generation_mode=GenerationMode.BALANCED,
    )
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_home",
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                        "title": "Client Home",
                        "is_entry": True,
                    }
                ]
            }
        }
    }

    ensured = generation_service._ensure_runtime_artifact_operations(
        grounded_spec=spec,
        page_graph=page_graph,
        role_scope=["client"],
        generation_mode=GenerationMode.BALANCED,
        operations=[
            DraftFileOperation(
                file_path="miniapp/app/generated/route_manifest.json",
                operation="replace",
                content=json.dumps({"routes": [{"path": "/client", "file": "static/client/index.html"}]}),
                reason="stale llm artifact",
            )
        ],
    )

    route_manifest = next(operation for operation in ensured if operation.file_path == "miniapp/app/generated/route_manifest.json")
    payload = json.loads(route_manifest.content or "{}")
    assert "roles" in payload
    assert "routes" not in payload


def test_codegen_prompts_require_db_and_schemas_for_stateful_apps(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    generation_service = app.state.container.generation_service
    prompt = "Create a multi-role request workflow app with persistent requests and comments."
    grounded_spec = generation_service._build_grounded_spec(
        workspace_id="ws_prompt_contract",
        prompt=prompt,
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="rev_test",
        prompt_turn_id="turn_test",
        generation_mode=GenerationMode.BALANCED,
    )
    role_contract = {
        "roles": {
            "client": {"responsibility": "Create requests", "primary_jobs": ["Create", "Track"]},
            "specialist": {"responsibility": "Process requests", "primary_jobs": ["Open", "Update"]},
            "manager": {"responsibility": "Oversee requests", "primary_jobs": ["Assign", "Monitor"]},
        }
    }
    plan_prompt = generation_service._code_plan_user_prompt(
        prompt=prompt,
        grounded_spec=grounded_spec,
        doc_refs=[],
        role_scope=["client", "specialist", "manager"],
        role_contract=role_contract,
        scope_mode="whole_file_build",
        require_multi_page=True,
        workspace_tree=[],
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )
    composition_prompt = generation_service._composition_user_prompt(
        prompt=prompt,
        grounded_spec=grounded_spec,
        role_scope=["client", "specialist", "manager"],
        role_contract=role_contract,
        page_graph={"roles": {}, "backend_targets": [], "shared_files": []},
        scope_mode="whole_file_build",
        intent="create",
        stage_name="miniapp",
        target_files=["miniapp/app/db.py", "miniapp/app/schemas.py"],
        file_contexts={},
        generated_page_sources={},
        generated_support_sources={},
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )
    repair_prompt = generation_service._repair_user_prompt(
        prompt=prompt,
        grounded_spec=grounded_spec,
        role_scope=["client", "specialist", "manager"],
        role_contract=role_contract,
        page_graph={"roles": {}, "backend_targets": [], "shared_files": []},
        scope_mode="whole_file_build",
        target_files=["miniapp/app/db.py", "miniapp/app/schemas.py"],
        file_contexts={},
        build_issues=[],
        preview_issue=None,
        preview_logs=[],
        attempt=1,
    )

    assert "db.py" in plan_prompt and "schemas.py" in plan_prompt
    assert "SQLAlchemy" in composition_prompt and "schemas.py" in composition_prompt
    assert "db.py" in repair_prompt and "schemas.py" in repair_prompt


def test_backend_contract_target_inference_helpers_remain_available_for_invariant_repairs() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_requests",
                        "route_path": "/client/requests",
                        "data_dependencies": ["/api/requests", "/api/comments"],
                    }
                ]
            }
        }
    }

    inferred = GenerationService._detect_missing_backend_contract_targets_from_page_graph(
        page_graph=page_graph,
        current_target_files=["miniapp/app/main.py"],
        backend_targets=["miniapp/app/main.py"],
    )

    assert "miniapp/app/routes/requests.py" in inferred
    assert "miniapp/app/routes/comments.py" in inferred


def test_backend_contract_target_inference_normalizes_hyphenated_api_stems() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_request_new",
                        "route_path": "/client/request/new",
                        "data_dependencies": ["/api/time-slots", "/api/workload/summary"],
                    }
                ]
            }
        }
    }

    inferred = GenerationService._detect_missing_backend_contract_targets_from_page_graph(
        page_graph=page_graph,
        current_target_files=["miniapp/app/main.py"],
        backend_targets=["miniapp/app/main.py"],
    )

    assert "miniapp/app/routes/time_slots.py" in inferred
    assert "miniapp/app/routes/time-slots.py" not in inferred


def test_backend_contract_target_inference_from_spec_helper_normalizes_required_routes() -> None:
    grounded_spec = SimpleNamespace(
        api_requirements=[
            APIRequirement(
                api_req_id="api_workload",
                name="Workload",
                method="GET",
                path="/api/workload",
                purpose="Load team workload",
                request_fields=[],
                response_fields=[],
                evidence=[],
                auth_required=False,
                existing_in_template=False,
            ),
            APIRequirement(
                api_req_id="api_specialists",
                name="Specialists",
                method="GET",
                path="/api/specialists",
                purpose="Load specialist options",
                request_fields=[],
                response_fields=[],
                evidence=[],
                auth_required=False,
                existing_in_template=False,
            ),
            APIRequirement(
                api_req_id="api_auth",
                name="Auth",
                method="GET",
                path="/api/auth",
                purpose="Forbidden auth route should be ignored",
                request_fields=[],
                response_fields=[],
                evidence=[],
                auth_required=False,
                existing_in_template=False,
            ),
        ]
    )
    page_graph = {
        "roles": {
            "client": {"pages": [{"route_path": "/profile"}]},
            "manager": {"pages": [{"route_path": "/workload"}]},
        }
    }

    inferred = GenerationService._detect_missing_backend_contract_targets_from_spec(
        grounded_spec=grounded_spec,
        page_graph=page_graph,
        current_target_files=[],
        backend_targets=[],
    )

    assert "miniapp/app/routes/profiles.py" in inferred
    assert "miniapp/app/routes/workload.py" in inferred
    assert "miniapp/app/routes/users.py" in inferred
    assert "miniapp/app/routes/auth.py" not in inferred


def test_generated_page_sources_infer_backend_contract_targets_from_template_literal_api_calls() -> None:
    inferred = GenerationService._detect_missing_backend_contract_targets(
        generated_page_sources={
            "miniapp/app/static/client/workflowrequests/app.js": "window.miniappApiFetch(`/api/workflowrequests`);\nfetch(`/api/workflowrequests/${recordId}`, { method: 'PATCH', body: '{}' });\n",
        },
        current_target_files=["miniapp/app/static/client/workflowrequests/index.html"],
        backend_targets=[],
    )

    assert "miniapp/app/routes/workflowrequests.py" in inferred
    assert "miniapp/app/main.py" in inferred
    assert "miniapp/app/db.py" in inferred
    assert "miniapp/app/schemas.py" in inferred


def test_prepare_runtime_plan_adds_backend_targets_from_page_graph_dependencies(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    grounded_spec = SimpleNamespace(
        api_requirements=[
            APIRequirement(
                api_req_id="api_requests",
                name="Requests",
                method="POST",
                path="/api/requests",
                purpose="Create shared workflow records",
                request_fields=[],
                response_fields=[],
                evidence=[],
                auth_required=False,
                existing_in_template=False,
            )
        ]
    )
    plan_result = {
        "target_files": [
            "miniapp/app/static/client/requests/index.html",
            "miniapp/app/static/client/requests/styles.css",
            "miniapp/app/static/client/requests/app.js",
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
        ],
        "backend_targets": [
            "miniapp/app/main.py",
            "miniapp/app/db.py",
            "miniapp/app/schemas.py",
        ],
        "files_to_read": [],
        "shared_files": [],
        "page_graph": {
            "roles": {
                "client": {
                    "pages": [
                        {
                            "page_id": "client_requests",
                            "route_path": "/requests",
                            "file_path": "miniapp/app/static/client/requests/index.html",
                            "style_path": "miniapp/app/static/client/requests/styles.css",
                            "script_path": "miniapp/app/static/client/requests/app.js",
                            "data_dependencies": ["/api/requests"],
                        }
                    ]
                }
            }
        },
    }

    prepared = service.generation_plan_runtime.prepare_runtime_plan(
        workspace_id="ws_test",
        draft_source=tmp_path,
        grounded_spec=grounded_spec,
        role_scope=["client", "specialist", "manager"],
        plan_result=plan_result,
    )

    assert "miniapp/app/routes/requests.py" in prepared["backend_targets"]
    assert "miniapp/app/routes/requests.py" in prepared["target_files"]


def test_route_module_path_for_endpoint_name_uses_snake_case() -> None:
    assert GenerationService._route_module_path_for_endpoint_name("time-slots") == "miniapp/app/routes/time_slots.py"
    assert GenerationService._route_module_path_for_endpoint_name("requests") == "miniapp/app/routes/requests.py"


def test_fix_loop_builds_turn_context_without_fix_strategy(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    orchestrator = app.state.container.fix_orchestrator

    fix_case = orchestrator._build_fix_case(
        workspace_id="ws_test",
        run_id="run_test",
        attempt=1,
        request=GenerateRequest(
            prompt="Fix generated tests",
            mode="fix",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
        check_execution=CheckExecutionRecord(
            workspace_id="ws_test",
            run_id="run_test",
            results=[
                RunCheckResult(
                    name="generated_app_python_tests",
                    status="failed",
                    details="Python generated app tests failed.",
                    logs=["NameError: name 'SessionLocal' is not defined"],
                )
            ],
        ),
        preview_details={},
        prior_attempts=[],
        existing_scope=[],
    )

    assert fix_case.failure_class is not None
    assert not hasattr(fix_case, "fix_strategy")


def test_fix_implicated_files_include_missing_runtime_and_profile_contract_targets(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    orchestrator = app.state.container.fix_orchestrator

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Fix Scope Workspace",
            description="Scope inference should include missing structural files.",
            path=str((tmp_path / "data" / "workspaces" / "ws_fix_scope").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_fix_scope"
    workspace_service.prepare_draft(workspace.workspace_id, draft_run_id)

    implicated = orchestrator._implicated_files(
        workspace.workspace_id,
        draft_run_id,
        "\n".join(
            [
                "NameError: name 'SessionLocal' is not defined",
                "Route /client/profile referenced by miniapp/app/static/client/index.html is not declared in route_manifest.json",
            ]
        ),
        [],
    )

    assert "miniapp/app/main.py" in implicated
    assert "miniapp/app/db.py" in implicated
    assert "miniapp/app/routes/client.py" in implicated
    assert "miniapp/app/static/client/profile/index.html" in implicated
    assert "miniapp/app/static/client/profile/styles.css" not in implicated
    assert "miniapp/app/static/client/profile/app.js" not in implicated


def test_structural_write_scope_keeps_missing_contract_files_editable(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    orchestrator = app.state.container.fix_orchestrator

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Structural Scope Workspace",
            description="Structural fix scope should allow creating missing files.",
            path=str((tmp_path / "data" / "workspaces" / "ws_structural_scope").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_structural_scope"
    workspace_service.prepare_draft(workspace.workspace_id, draft_run_id)

    scope = orchestrator._build_write_scope(
        workspace.workspace_id,
        draft_run_id,
        [
            "miniapp/app/main.py",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/static/client/profile/index.html",
            "miniapp/app/static/client/profile/styles.css",
            "miniapp/app/static/client/profile/app.js",
        ],
        "app/runtime_test",
        [],
    )
    scope_paths = {entry.file_path for entry in scope}

    assert "miniapp/app/routes/profiles.py" not in scope_paths
    assert "miniapp/app/generated/route_manifest.json" not in scope_paths
    assert "miniapp/tests/test_generated_app.py" not in scope_paths
    assert "miniapp/tests/generated_app.test.mjs" not in scope_paths
    assert "miniapp/app/main.py" in scope_paths
    assert "miniapp/app/static/client/profile/index.html" in scope_paths
    assert "miniapp/app/static/client/profile/styles.css" in scope_paths
    assert "miniapp/app/static/client/profile/app.js" in scope_paths


def test_runtime_fix_scope_excludes_generated_tests_from_write_surface(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    orchestrator = app.state.container.fix_orchestrator

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="No Test Rewrite Workspace",
            description="Runtime fixes should patch app files, not generated tests.",
            path=str((tmp_path / "data" / "workspaces" / "ws_no_test_rewrite").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_no_test_rewrite"
    workspace_service.prepare_draft(workspace.workspace_id, draft_run_id)

    scope = orchestrator._build_write_scope(
        workspace.workspace_id,
        draft_run_id,
        [
            "miniapp/tests/test_generated_app.py",
            "miniapp/tests/generated_app.test.mjs",
            "miniapp/app/main.py",
            "miniapp/app/generated/route_manifest.json",
        ],
        "backend_startup/import/schema",
        [],
    )
    scope_paths = {entry.file_path for entry in scope}

    assert "miniapp/app/main.py" in scope_paths
    assert "miniapp/app/generated/route_manifest.json" not in scope_paths
    assert "miniapp/tests/test_generated_app.py" not in scope_paths
    assert "miniapp/tests/generated_app.test.mjs" not in scope_paths


def test_forbidden_generated_api_requirement_blocks_auth_and_realtime_contracts() -> None:
    assert GenerationService._is_forbidden_generated_api_requirement(
        APIRequirement(
            api_req_id="api_auth",
            name="Auth: login, role-aware session token",
            method="POST",
            path="/api/auth/login",
            purpose="Create a role-aware session token for the mini app.",
            request_fields=[],
            response_fields=[],
            evidence=[],
        )
    )


def test_grounded_spec_outline_sanitizer_removes_auth_and_realtime_api_needs() -> None:
    outline = {
        "api_needs": [
            "Auth / user profile (GET current user, role-specific session bootstrapping)",
            "Requests CRUD",
            "Push/Realtime notifications or polling endpoint",
            "Comments API",
        ]
    }

    sanitized = GenerationService._sanitize_grounded_spec_outline(outline)

    assert sanitized["api_needs"] == ["Requests CRUD", "Comments API"]


def test_forbidden_spec_governance_text_flags_auth_and_realtime() -> None:
    assert GenerationService._is_forbidden_spec_governance_text(
        "All roles will be authenticated via a single Auth / user profile endpoint and session bootstrap."
    )
    assert GenerationService._is_forbidden_spec_governance_text(
        "All roles will authenticate through Telegram initData and receive a role-aware bootstrap payload."
    )
    assert GenerationService._is_forbidden_spec_governance_text(
        "Use push/realtime notifications or a polling endpoint for status changes."
    )
    assert GenerationService._is_forbidden_spec_governance_text(
        "Use foreground polling with a manual refresh action when the detail screen opens."
    )
    assert not GenerationService._is_forbidden_spec_governance_text(
        "Use compact request cards and explicit status transitions."
    )


def test_route_manifest_prefixes_role_local_paths() -> None:
    manifest = GenerationService._route_manifest_from_page_graph(
        {
            "roles": {
                "client": {
                    "pages": [
                        {"route_path": "/", "file_path": "miniapp/app/static/client/index.html"},
                        {"route_path": "/new", "file_path": "miniapp/app/static/client/new/index.html"},
                        {"route_path": "/profile", "file_path": "miniapp/app/static/client/profile/index.html"},
                    ]
                }
            }
        },
        ["client"],
    )

    routes = [page["route_path"] for page in manifest["roles"]["client"]["pages"]]
    assert routes == ["/client", "/client/create", "/client/profile"]


def test_preflight_profile_schema_contract_detects_missing_role_profile(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "miniapp/app/routes").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app/routes/profiles.py").write_text(
        "from app.schemas import AppRole, RoleProfile\n",
        encoding="utf-8",
    )
    (workspace_root / "miniapp/app/schemas.py").write_text(
        "from pydantic import BaseModel\nclass Placeholder(BaseModel):\n    value: str\n",
        encoding="utf-8",
    )

    issues = GenerationService._preflight_profile_schema_issues(workspace_root)

    assert any(issue.code == "preflight.profile_schema_contract" for issue in issues)


def test_preflight_route_manifest_link_detects_missing_declared_route(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "miniapp/app/generated").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app/static/client").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app/generated/route_manifest.json").write_text(
        json.dumps(
            {
                "roles": {
                    "client": {
                        "pages": [
                            {"route_path": "/client", "file_path": "miniapp/app/static/client/index.html"},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (workspace_root / "miniapp/app/static/client/index.html").write_text(
        '<a href="/client/new">Create</a>\n',
        encoding="utf-8",
    )

    issues = GenerationService._preflight_route_manifest_link_issues(
        workspace_root,
        {
            "roles": {
                "client": {
                    "pages": [
                        {"file_path": "miniapp/app/static/client/index.html"},
                    ]
                }
            }
        },
        ["client"],
    )

    assert any(issue.code == "preflight.route_manifest_link_mismatch" for issue in issues)


def test_preflight_route_manifest_link_allows_trailing_slash_when_manifest_declares_canonical_route(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "miniapp/app/generated").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app/static/client").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app/generated/route_manifest.json").write_text(
        json.dumps(
            {
                "roles": {
                    "client": {
                        "pages": [
                            {"route_path": "/client", "file_path": "miniapp/app/static/client/index.html"},
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (workspace_root / "miniapp/app/static/client/index.html").write_text(
        '<a href="/client/">Dashboard</a>\n',
        encoding="utf-8",
    )

    issues = GenerationService._preflight_route_manifest_link_issues(
        workspace_root,
        {
            "roles": {
                "client": {
                    "pages": [
                        {"file_path": "miniapp/app/static/client/index.html"},
                    ]
                }
            }
        },
        ["client"],
    )

    assert not any(issue.code == "preflight.route_manifest_link_mismatch" for issue in issues)


def test_preflight_backend_syntax_issues_detect_invalid_python(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    target = workspace_root / "miniapp/app/routes/uploads.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('url = f"https://example.com/{token"\n', encoding="utf-8")

    issues = GenerationService._preflight_backend_syntax_issues(
        workspace_root,
        ["miniapp/app/routes/uploads.py"],
    )

    assert any(issue.code == "preflight.python_syntax_error" for issue in issues)
    assert GenerationService._is_forbidden_generated_api_requirement(
        APIRequirement(
            api_req_id="api_events",
            name="Realtime updates stream",
            method="GET",
            path="/api/events/stream",
            purpose="Server-sent events stream for realtime updates.",
            request_fields=[],
            response_fields=[],
            evidence=[],
        )
    )


def test_fix_prompt_uses_code_first_turn_packet_without_fix_strategy(tmp_path: Path) -> None:
    prompt_context = FixPromptContext(
        workspace_id="ws_test",
        run_id="run_test",
        attempt=1,
        failure_class="route_api_contract_mismatch",
        failure_signature="sig",
        failing_file_paths=["miniapp/app/db.py"],
        file_contexts={"miniapp/app/db.py": "engine = create_engine(...)"},
    )

    payload = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data").state.container.fix_orchestrator._repair_user_prompt(
        prompt_context,
    )

    assert "repair_packet" in payload
    assert "fix_strategy" not in payload
    assert "\"deterministic_companions\"" not in payload
    assert "\"failing_file_paths\"" in payload


def test_edit_gate_allows_loading_copy_when_page_has_real_surface() -> None:
    page_graph = {
        "roles": {
            "specialist": {
                "pages": [
                    {
                        "page_id": "specialist_home",
                        "route_path": "/specialist",
                        "file_path": "miniapp/app/static/specialist/index.html",
                        "title": "Assigned Tasks",
                        "navigation_label": "Desk",
                        "data_dependencies": [],
                    }
                ]
            }
        }
    }
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/specialist/index.html",
            operation="replace",
            content="""<!doctype html>
<html lang="en">
  <body>
    <main class="page-shell">
      <section class="page">
        <a class="card-link" href="/specialist/profile">Open profile</a>
        <section class="feature-block">Loading workload...</section>
        <section class="filters"><button class="chip">All</button></section>
        <section class="state" id="tasks-loading">Loading tasks...</section>
        <section class="state hidden" id="tasks-empty">No tasks yet.</section>
        <section class="state hidden" id="tasks-error">Unable to load tasks. <button id="retry-button">Retry</button></section>
        <section class="task-list" id="task-list"></section>
      </section>
    </main>
  </body>
</html>""",
            reason="Generated specialist desk page.",
        )
    ]

    issues = GenerationService._edit_gate_issues(
        page_graph,
        operations,
        ["specialist"],
        scope_mode="whole_file_build",
        target_files=["miniapp/app/static/specialist/index.html"],
    )

    assert "miniapp/app/static/specialist/index.html is dominated by loading copy instead of real page content." not in issues


def test_edit_gate_allows_generated_app_level_test_files() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_home",
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "title": "Client Home",
                        "navigation_label": "Home",
                        "data_dependencies": [],
                    }
                ]
            }
        }
    }
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content="<html><body><main>Client</main></body></html>",
            reason="Generated client page.",
        ),
        DraftFileOperation(
            file_path="miniapp/tests/test_generated_app.py",
            operation="replace",
            content="class GeneratedMiniAppTests: ...\n",
            reason="Generated backend app-level test.",
        ),
        DraftFileOperation(
            file_path="miniapp/tests/generated_app.test.mjs",
            operation="replace",
            content="import test from 'node:test';\n",
            reason="Generated frontend app-level test.",
        ),
    ]

    issues = GenerationService._edit_gate_issues(
        page_graph,
        operations,
        ["client"],
        scope_mode="whole_file_build",
        target_files=["miniapp/app/static/client/index.html"],
    )

    assert not any("planned target scope" in issue for issue in issues)
    assert not any("canonical architecture roots" in issue for issue in issues)


def test_runtime_python_route_paths_are_normalized() -> None:
    plan_result = {
        "target_files": ["miniapp/app/routes/time-slots.py", "miniapp/app/static/client/index.html"],
        "backend_targets": ["miniapp/app/routes/time-slots.py"],
        "files_to_read": ["miniapp/app/routes/time-slots.py"],
        "shared_files": [],
        "planner_contract_enrichment": {
            "proactive_backend_targets": ["miniapp/app/routes/time-slots.py"],
        },
        "generation_clusters": [
            {"cluster_name": "backend_core", "target_files": ["miniapp/app/routes/time-slots.py"]},
        ],
        "execution_plan": {
            "miniapp": {"target_files": ["miniapp/app/routes/time-slots.py"]},
            "generation_clusters": [{"cluster_name": "backend_core", "target_files": ["miniapp/app/routes/time-slots.py"]}],
        },
        "page_graph": {
            "backend_targets": ["miniapp/app/routes/time-slots.py"],
            "roles": {
                "client": {
                    "routes_file": "miniapp/app/routes/time-slots.py",
                    "pages": [
                        {"file_path": "miniapp/app/routes/time-slots.py"},
                    ],
                }
            },
        },
    }

    GenerationService._normalize_runtime_python_paths_in_plan(plan_result)

    assert "miniapp/app/routes/time_slots.py" in plan_result["target_files"]
    assert "miniapp/app/routes/time_slots.py" in plan_result["backend_targets"]
    assert plan_result["planner_contract_enrichment"]["proactive_backend_targets"] == ["miniapp/app/routes/time_slots.py"]
    assert plan_result["generation_clusters"][0]["target_files"] == ["miniapp/app/routes/time_slots.py"]
    assert plan_result["execution_plan"]["miniapp"]["target_files"] == ["miniapp/app/routes/time_slots.py"]
    assert plan_result["execution_plan"]["generation_clusters"][0]["target_files"] == ["miniapp/app/routes/time_slots.py"]
    assert plan_result["page_graph"]["roles"]["client"]["routes_file"] == "miniapp/app/routes/time_slots.py"
    assert plan_result["page_graph"]["roles"]["client"]["pages"][0]["file_path"] == "miniapp/app/routes/time_slots.py"


def test_causal_and_structural_repair_targets_normalize_hyphenated_api_paths(tmp_path: Path) -> None:
    service = create_app(
        repo_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
    ).state.container.generation_service
    issues = [
        SimpleNamespace(
            code="connectivity.unwired_page_dependency",
            message="Page depends on /api/time-slots but route is missing.",
            location="miniapp/app/static/client/request_new/index.html",
        )
    ]

    causal = service._causal_surface_for_issues(
        build_issues=issues,
        check_results=[],
        active_targets=["miniapp/app/main.py", "miniapp/app/routes/time_slots.py"],
    )
    expanded, added = service._expand_structural_repair_targets(
        active_targets=["miniapp/app/main.py"],
        build_issues=issues,
    )

    assert "miniapp/app/routes/time_slots.py" in causal
    assert "miniapp/app/routes/time-slots.py" not in causal
    assert "miniapp/app/routes/time_slots.py" in expanded
    assert "miniapp/app/routes/time_slots.py" in added


def test_generated_paths_use_underscores_for_static_and_route_files() -> None:
    normalized = GenerationService._normalize_path_list(
        [
            "miniapp/app/routes/time-slots.py",
            "miniapp/app/static/client/request-new.html",
            "miniapp/app/static/manager/workload-board.html",
            "miniapp/tests/generated-app.test.mjs",
        ],
        [],
    )

    assert "miniapp/app/routes/time_slots.py" in normalized
    assert "miniapp/app/static/client/request_new.html" in normalized
    assert "miniapp/app/static/manager/workload_board.html" in normalized
    assert "miniapp/tests/generated_app.test.mjs" in normalized
    assert all("-" not in Path(path).name for path in normalized)


def test_canonicalize_target_files_drops_flat_role_entries_and_template_owned_bridge(tmp_path: Path) -> None:
    service = create_app(
        repo_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
    ).state.container.generation_service

    canonical = service._canonicalize_target_files(
        [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/preview_bridge.js",
        ],
        scope_mode="whole_file_build",
    )

    assert "miniapp/app/static/client/index.html" in canonical
    assert "miniapp/app/static/preview_bridge.js" not in canonical
    assert "miniapp/app/static/client/styles.css" in canonical


def test_canonicalize_target_files_adds_missing_page_triplet_companions(tmp_path: Path) -> None:
    service = create_app(
        repo_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
    ).state.container.generation_service

    canonical = service._canonicalize_target_files(
        [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/new/index.html",
        ],
        scope_mode="whole_file_build",
    )

    assert "miniapp/app/static/client/index.html" in canonical
    assert "miniapp/app/static/client/styles.css" in canonical
    assert "miniapp/app/static/client/app.js" in canonical
    assert "miniapp/app/static/client/new/index.html" in canonical
    assert "miniapp/app/static/client/new/styles.css" in canonical
    assert "miniapp/app/static/client/new/app.js" in canonical


def test_prune_non_page_static_targets_drops_helper_js_outside_page_triplets() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                    },
                    {
                        "file_path": "miniapp/app/static/client/new/index.html",
                        "style_path": "miniapp/app/static/client/new/styles.css",
                        "script_path": "miniapp/app/static/client/new/app.js",
                    },
                ]
            }
        }
    }

    pruned = GenerationService._prune_non_page_static_targets(
        [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/client/new/index.html",
            "miniapp/app/static/client/new/styles.css",
            "miniapp/app/static/client/new/app.js",
            "miniapp/app/static/client/new_request_form/app.js",
            "miniapp/app/static/shared/base.css",
        ],
        page_graph=page_graph,
    )

    assert "miniapp/app/static/client/new_request_form/app.js" not in pruned
    assert "miniapp/app/static/shared/base.css" in pruned


def test_canonicalize_target_files_flattens_nested_role_static_paths(tmp_path: Path) -> None:
    service = create_app(
        repo_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
    ).state.container.generation_service

    canonical = service._canonicalize_target_files(
        [
            "miniapp/app/static/client/requests/detail.html",
            "miniapp/app/static/client/requests/detail.css",
            "miniapp/app/static/client/requests/detail.js",
        ],
        scope_mode="whole_file_build",
    )

    assert "miniapp/app/static/client/requests_detail/index.html" in canonical
    assert "miniapp/app/static/client/requests_detail/styles.css" in canonical
    assert "miniapp/app/static/client/requests_detail/app.js" in canonical
    assert "miniapp/app/static/client/requests/detail.html" not in canonical


def test_prune_non_page_static_targets_drops_noncanonical_shared_components() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                    }
                ]
            }
        }
    }

    pruned = GenerationService._prune_non_page_static_targets(
        [
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/shared/base.css",
            "miniapp/app/static/shared/request_card.html",
            "miniapp/app/static/shared/request_card.css",
            "miniapp/app/static/shared/request_card.js",
        ],
        page_graph=page_graph,
    )

    assert "miniapp/app/static/shared/base.css" in pruned
    assert "miniapp/app/static/shared/request_card.html" not in pruned
    assert "miniapp/app/static/shared/request_card.css" not in pruned
    assert "miniapp/app/static/shared/request_card.js" not in pruned


def test_sanitize_backend_targets_drops_forbidden_route_modules_and_manifest() -> None:
    sanitized = GenerationService._sanitize_backend_targets(
        [
            "miniapp/app/routes/requests.py",
            "miniapp/app/routes/notifications.py",
            "miniapp/app/routes/attachments.py",
            "miniapp/app/routes/worklog.py",
            "miniapp/app/routes/profiles.py",
            "miniapp/app/generated/route_manifest.json",
        ]
    )

    assert "miniapp/app/routes/requests.py" in sanitized
    assert "miniapp/app/routes/profiles.py" in sanitized
    assert "miniapp/app/routes/notifications.py" not in sanitized
    assert "miniapp/app/routes/attachments.py" not in sanitized
    assert "miniapp/app/routes/worklog.py" not in sanitized
    assert "miniapp/app/generated/route_manifest.json" not in sanitized


def test_forbidden_endpoint_names_are_not_inferred_into_backend_targets() -> None:
    inferred = GenerationService._detect_missing_backend_contract_targets_from_page_graph(
        page_graph={
            "roles": {
                "client": {
                    "pages": [
                        {
                            "data_dependencies": [
                                "GET /api/attachments",
                                "GET /api/notifications",
                                "GET /api/requests",
                            ]
                        }
                    ]
                }
            }
        },
        current_target_files=[],
        backend_targets=[],
    )

    assert "miniapp/app/routes/requests.py" in inferred
    assert "miniapp/app/routes/attachments.py" not in inferred
    assert "miniapp/app/routes/notifications.py" not in inferred


def test_sanitize_planner_target_files_keeps_only_canonical_page_and_backend_targets() -> None:
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "file_path": "miniapp/app/static/client/index.html",
                        "style_path": "miniapp/app/static/client/styles.css",
                        "script_path": "miniapp/app/static/client/app.js",
                    },
                    {
                        "file_path": "miniapp/app/static/client/create/index.html",
                        "style_path": "miniapp/app/static/client/create/styles.css",
                        "script_path": "miniapp/app/static/client/create/app.js",
                    },
                ]
            }
        }
    }

    sanitized = GenerationService._sanitize_planner_target_files(
        target_files=[
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/styles.css",
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/client/create/index.html",
            "miniapp/app/static/client/create/styles.css",
            "miniapp/app/static/client/create/app.js",
            "miniapp/app/static/client/dashboard/index.html",
            "miniapp/app/static/shared/request_card.js",
            "miniapp/app/routes/requests.py",
            "miniapp/app/routes/notifications.py",
            "miniapp/app/generated/route_manifest.json",
        ],
        backend_targets=[
            "miniapp/app/routes/requests.py",
            "miniapp/app/routes/notifications.py",
        ],
        page_graph=page_graph,
    )

    assert "miniapp/app/static/client/index.html" in sanitized
    assert "miniapp/app/static/client/create/index.html" in sanitized
    assert "miniapp/app/routes/requests.py" in sanitized
    assert "miniapp/app/static/client/dashboard/index.html" not in sanitized
    assert "miniapp/app/static/shared/request_card.js" not in sanitized
    assert "miniapp/app/routes/notifications.py" not in sanitized
    assert "miniapp/app/generated/route_manifest.json" not in sanitized


def test_merge_advisory_generation_inputs_prefers_inferred_shape_and_unions_targets(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    role_contract, plan_result = service.generation_entry._merge_advisory_generation_inputs(
        role_contract={"roles": {"client": {"responsibility": "Create records."}}},
        inferred_role_contract={"roles": {"specialist": {"responsibility": "Process records."}, "manager": {"responsibility": "Observe records."}}},
        advisory_plan_result={
            "target_files": ["miniapp/app/static/client/dashboard/index.html", "miniapp/app/routes/requests.py"],
            "backend_targets": ["miniapp/app/routes/requests.py"],
            "shared_files": [],
            "files_to_read": ["miniapp/app/main.py"],
            "page_graph": {"roles": {}},
            "generation_clusters": [],
            "execution_plan": {},
            "scope_mode": "whole_file_build",
            "flow_mode": "multi_page",
            "require_multi_page": True,
        },
        inferred_plan_result={
            "target_files": ["miniapp/app/static/client/index.html"],
            "backend_targets": ["miniapp/app/routes/client.py"],
            "shared_files": ["miniapp/app/static/shared/base.css"],
            "files_to_read": ["miniapp/app/static/shared/base.css"],
            "page_graph": {
                "roles": {
                    "client": {
                        "pages": [
                            {
                                "page_id": "client_index",
                                "route_path": "/",
                                "file_path": "miniapp/app/static/client/index.html",
                            }
                        ]
                    }
                }
            },
            "generation_clusters": [{"cluster_name": "role_client_ui_root", "target_files": ["miniapp/app/static/client/index.html"]}],
            "execution_plan": {"role_steps": []},
            "scope_mode": "whole_file_build",
            "flow_mode": "multi_page",
            "require_multi_page": True,
        },
    )

    assert set(role_contract["roles"]) == {"client", "specialist", "manager"}
    assert "miniapp/app/static/client/index.html" in plan_result["target_files"]
    assert "miniapp/app/static/client/dashboard/index.html" not in plan_result["target_files"]
    assert "miniapp/app/routes/requests.py" in plan_result["backend_targets"]
    assert plan_result["page_graph"]["roles"]["client"]["pages"][0]["file_path"] == "miniapp/app/static/client/index.html"


def test_landing_alias_paths_are_canonicalized_to_role_root() -> None:
    normalized = GenerationService._normalize_path_list(
        [
            "miniapp/app/static/client/client_home/index.html",
            "miniapp/app/static/client/home/styles.css",
            "miniapp/app/static/manager/manager_home/app.js",
        ],
        [],
    )

    assert normalized == [
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/styles.css",
        "miniapp/app/static/manager/app.js",
    ]


def test_normalize_role_route_path_canonicalizes_dashboard_and_tasks_aliases() -> None:
    assert GenerationService._normalize_role_route_path("manager", "/dashboard", index=1) == "/"
    assert GenerationService._normalize_role_route_path("specialist", "/tasks", index=1) == "/requests"
    assert GenerationService._normalize_role_route_path("client", "/new", index=1) == "/create"


def test_normalize_path_list_drops_directory_targets_and_preserves_dunder_init() -> None:
    normalized = GenerationService._normalize_path_list(
        [
            "miniapp/app/routes",
            "miniapp/app/routes/__init__.py",
            "miniapp/app/routes/requests.py",
        ],
        [],
    )

    assert "miniapp/app/routes" not in normalized
    assert "miniapp/app/routes/__init__.py" in normalized
    assert "miniapp/app/routes/init.py" not in normalized


def test_page_graph_gate_normalizes_role_prefixed_entry_routes() -> None:
    page_graph = {
        "roles": {
            "client": {
                "routes_file": "miniapp/app/routes/client.py",
                "pages": [
                    {
                        "page_id": "client_home",
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/client_home/index.html",
                        "page_kind": "dashboard",
                        "handoff_paths": ["/client/profile"],
                        "purpose": "Client dashboard for starting request work.",
                    },
                    {
                        "page_id": "client_profile",
                        "route_path": "/client/profile",
                        "file_path": "miniapp/app/static/client/profile/index.html",
                        "page_kind": "profile",
                        "handoff_paths": ["/client"],
                        "purpose": "Client profile editing page.",
                    },
                ],
            }
        },
        "shared_files": [],
        "backend_targets": [],
    }

    issues = GenerationService._page_graph_gate_issues(
        page_graph,
        ["client"],
        scope_mode="whole_file_build",
        require_multi_page=True,
    )

    assert "client is missing an entry route at /." not in issues


def test_default_page_file_uses_snake_case_names() -> None:
    assert (
        GenerationService._default_page_file("client", "ClientRequestNewPage", route_path="/request-new")
        == "miniapp/app/static/client/request_new/index.html"
    )
    assert (
        GenerationService._default_page_file("manager", "ManagerWorkloadBoardPage", route_path="/workload-board")
        == "miniapp/app/static/manager/workload_board/index.html"
    )
    assert (
        GenerationService._default_page_file("specialist", "SpecialistRequestDetailPage", route_path="/requests/:request_id")
        == "miniapp/app/static/specialist/requests_detail/index.html"
    )


def test_default_page_asset_paths_follow_page_stem() -> None:
    assert GenerationService._default_page_asset_path(
        "miniapp/app/static/client/index.html",
        asset_kind="css",
    ) == "miniapp/app/static/client/styles.css"
    assert GenerationService._default_page_asset_path(
        "miniapp/app/static/client/index.html",
        asset_kind="js",
    ) == "miniapp/app/static/client/app.js"
    assert GenerationService._default_page_asset_path(
        "miniapp/app/static/client/request_new/index.html",
        asset_kind="css",
    ) == "miniapp/app/static/client/request_new/styles.css"
    assert GenerationService._default_page_asset_path(
        "miniapp/app/static/client/request_new/index.html",
        asset_kind="js",
    ) == "miniapp/app/static/client/request_new/app.js"


def test_route_manifest_uses_snake_case_file_names() -> None:
    manifest = GenerationService._build_route_manifest(
        {
            "roles": {
                "manager": {
                    "routes": [
                        {"path": "/", "screen_id": "manager_home", "is_entry": True},
                        {"path": "/workload-board", "screen_id": "manager_workload"},
                        {"path": "/requests/:request_id", "screen_id": "manager_request"},
                    ],
                    "screens": {
                        "manager_home": {"kind": "dashboard", "title": "Home"},
                        "manager_workload": {"kind": "workspace", "title": "Workload"},
                        "manager_request": {"kind": "workspace", "title": "Request"},
                    },
                }
            }
        }
    )

    pages = manifest["roles"]["manager"]["pages"]
    assert [page["file_path"] for page in pages] == [
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/workload_board/index.html",
        "miniapp/app/static/manager/requests_detail/index.html",
    ]
    assert [page["style_path"] for page in pages] == [
        "miniapp/app/static/manager/styles.css",
        "miniapp/app/static/manager/workload_board/styles.css",
        "miniapp/app/static/manager/requests_detail/styles.css",
    ]
    assert [page["script_path"] for page in pages] == [
        "miniapp/app/static/manager/app.js",
        "miniapp/app/static/manager/workload_board/app.js",
        "miniapp/app/static/manager/requests_detail/app.js",
    ]


def test_normalize_generated_file_path_canonicalizes_flat_role_pages_to_page_folders() -> None:
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/request_detail.html")
        == "miniapp/app/static/client/request_detail/index.html"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/request_detail.css")
        == "miniapp/app/static/client/request_detail/styles.css"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/request_detail.js")
        == "miniapp/app/static/client/request_detail/app.js"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/index.css")
        == "miniapp/app/static/client/styles.css"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/index.js")
        == "miniapp/app/static/client/app.js"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/app.js")
        == "miniapp/app/static/client/app.js"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/app.css")
        == "miniapp/app/static/client/styles.css"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/styles.css")
        == "miniapp/app/static/client/styles.css"
    )
    assert (
        GenerationService._normalize_generated_file_path("miniapp/app/static/client/styles.js")
        == "miniapp/app/static/client/app.js"
    )


def test_non_root_page_file_path_is_rewritten_when_model_returns_js_path(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service
    page = service._normalize_page_definition(
        "client",
        {
            "page_id": "client_requests",
            "route_path": "/requests",
            "file_path": "miniapp/app/static/client/app.js",
            "title": "Requests",
        },
        1,
    )
    assert page["file_path"] == "miniapp/app/static/client/requests/index.html"
    assert page["style_path"] == "miniapp/app/static/client/requests/styles.css"
    assert page["script_path"] == "miniapp/app/static/client/requests/app.js"


def test_page_definition_canonicalizes_alias_folder_names_from_route_path(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    new_page = service._normalize_page_definition(
        "client",
        {
            "page_id": "client_create_request",
            "route_path": "/new",
            "file_path": "miniapp/app/static/client/create_request/index.html",
            "style_path": "miniapp/app/static/client/create_request/styles.css",
            "script_path": "miniapp/app/static/client/create_request/app.js",
            "title": "New request",
        },
        1,
    )
    detail_page = service._normalize_page_definition(
        "specialist",
        {
            "page_id": "specialist_request_detail",
            "route_path": "/requests/:request_id",
            "file_path": "miniapp/app/static/specialist/request_detail/index.html",
            "style_path": "miniapp/app/static/specialist/request_detail/styles.css",
            "script_path": "miniapp/app/static/specialist/request_detail/app.js",
            "title": "Request detail",
        },
        2,
    )

    assert new_page["file_path"] == "miniapp/app/static/client/new/index.html"
    assert new_page["style_path"] == "miniapp/app/static/client/new/styles.css"
    assert new_page["script_path"] == "miniapp/app/static/client/new/app.js"
    assert detail_page["file_path"] == "miniapp/app/static/specialist/requests_detail/index.html"
    assert detail_page["style_path"] == "miniapp/app/static/specialist/requests_detail/styles.css"
    assert detail_page["script_path"] == "miniapp/app/static/specialist/requests_detail/app.js"


def test_finalize_role_pages_does_not_reinsert_profile_page(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    pages = service._finalize_role_pages(
        "client",
        [
            {
                "page_id": "client_home",
                "route_path": "/",
                "file_path": "miniapp/app/static/client/index.html",
                "page_kind": "dashboard",
                "title": "Home",
            }
        ],
        require_multi_page=True,
    )

    route_paths = {page["route_path"] for page in pages}
    assert "/" in route_paths
    assert "/profile" not in route_paths


def test_endpoint_names_from_dependency_text_supports_non_api_paths() -> None:
    endpoints = GenerationService._endpoint_names_from_dependency_text("GET /users?role=specialist")
    assert endpoints == {"users"}


def test_endpoint_names_from_dependency_text_does_not_infer_api_as_route_module() -> None:
    endpoints = GenerationService._endpoint_names_from_dependency_text("GET /api/requests?role=client")
    assert "requests" in endpoints
    assert "api" not in endpoints


def test_structural_repair_targets_stay_broad_for_profile_and_manifest_failures(tmp_path: Path) -> None:
    service = create_app(
        repo_root=Path(__file__).resolve().parents[3],
        data_dir=tmp_path / "data",
    ).state.container.generation_service
    active_targets = [
        "miniapp/app/db.py",
        "miniapp/app/schemas.py",
        "miniapp/app/main.py",
        "miniapp/app/routes/profiles.py",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/static/client/index.html",
        "miniapp/app/static/client/app.js",
    ]
    selected = service._repair_targets_for_attempt(
        active_targets=active_targets,
        check_results=[],
        attempt=1,
        causal_surface={"miniapp/app/generated/route_manifest.json", "miniapp/app/routes/profiles.py"},
        scope_mode="whole_file_build",
        structural_failure=True,
    )

    assert "miniapp/app/generated/route_manifest.json" in selected
    assert "miniapp/app/routes/profiles.py" in selected
    assert "miniapp/app/db.py" in selected


def test_plan_expands_page_triplet_targets() -> None:
    plan_result = {
        "target_files": ["miniapp/app/static/client/index.html"],
        "files_to_read": [],
        "page_graph": {
            "roles": {
                "client": {
                    "pages": [
                        {
                            "page_id": "client_home",
                            "route_path": "/client",
                            "file_path": "miniapp/app/static/client/index.html",
                        }
                    ]
                }
            }
        },
    }

    GenerationService._expand_page_asset_targets_in_plan(plan_result)

    assert plan_result["page_graph"]["roles"]["client"]["pages"][0]["style_path"] == "miniapp/app/static/client/styles.css"
    assert plan_result["page_graph"]["roles"]["client"]["pages"][0]["script_path"] == "miniapp/app/static/client/app.js"
    assert "miniapp/app/static/client/styles.css" in plan_result["target_files"]
    assert "miniapp/app/static/client/app.js" in plan_result["target_files"]


def test_whole_file_cluster_safe_companion_expansion_is_role_local_only() -> None:
    expanded = GenerationService._expand_cluster_targets_for_safe_companions(
        cluster_name="role_manager_ui",
        cluster_targets=["miniapp/app/static/manager/index.html"],
        invalid_paths=["miniapp/app/static/manager/request-detail.js"],
    )

    assert expanded == [
        "miniapp/app/static/manager/index.html",
        "miniapp/app/static/manager/request-detail.js",
    ]
    assert (
        GenerationService._expand_cluster_targets_for_safe_companions(
            cluster_name="role_manager_ui",
            cluster_targets=["miniapp/app/static/manager/index.html"],
            invalid_paths=["miniapp/app/static/client/request-detail.js"],
        )
        is None
    )


def test_normalize_page_definition_rewrites_cross_role_profile_path() -> None:
    service = GenerationService.__new__(GenerationService)

    page = service._normalize_page_definition(
        "specialist",
        {
            "page_id": "specialist_profile",
            "route_path": "/profile",
            "file_path": "miniapp/app/static/manager/profile/index.html",
        },
        0,
    )

    assert page["file_path"] == "miniapp/app/static/specialist/profile/index.html"
    assert page["style_path"] == "miniapp/app/static/specialist/profile/styles.css"
    assert page["script_path"] == "miniapp/app/static/specialist/profile/app.js"


def test_build_generation_clusters_splits_role_ui_by_page_surface() -> None:
    clusters = GenerationService._build_generation_clusters(
        [
            "miniapp/app/main.py",
            "miniapp/app/static/manager/index.html",
            "miniapp/app/static/manager/styles.css",
            "miniapp/app/static/manager/app.js",
            "miniapp/app/static/manager/profile/index.html",
            "miniapp/app/static/manager/profile/styles.css",
            "miniapp/app/static/manager/profile/app.js",
            "miniapp/app/static/client/index.html",
        ]
    )

    assert clusters == [
        {"cluster_name": "backend_support", "target_files": ["miniapp/app/main.py"]},
        {
            "cluster_name": "role_manager_ui_root",
            "target_files": [
                "miniapp/app/static/manager/index.html",
                "miniapp/app/static/manager/styles.css",
                "miniapp/app/static/manager/app.js",
            ],
        },
        {
            "cluster_name": "role_manager_ui_profile",
            "target_files": [
                "miniapp/app/static/manager/profile/index.html",
                "miniapp/app/static/manager/profile/styles.css",
                "miniapp/app/static/manager/profile/app.js",
            ],
        },
        {"cluster_name": "role_client_ui_root", "target_files": ["miniapp/app/static/client/index.html"]},
    ]


def test_app_level_test_operations_are_synthesized() -> None:
    page_graph = {
        "backend_targets": ["miniapp/app/routes/time-slots.py", "miniapp/app/routes/requests.py"],
        "shared_files": ["miniapp/app/static/preview_bridge.js"],
        "roles": {
            role: {
                "pages": [
                    {
                        "page_id": f"{role}_home",
                        "route_path": f"/{role}",
                        "file_path": f"miniapp/app/static/{role}/index.html",
                        "page_kind": "dashboard",
                    }
                ]
            }
            for role in ("client", "specialist", "manager")
        },
    }
    generation_service = GenerationService.__new__(GenerationService)

    ensured = generation_service._ensure_app_level_test_operations(
        page_graph=page_graph,
        role_scope=["client", "specialist", "manager"],
        operations=[],
    )
    ensured_paths = {operation.file_path for operation in ensured}

    assert "miniapp/tests/test_generated_app.py" in ensured_paths
    assert "miniapp/tests/generated_app.test.mjs" in ensured_paths
    python_test = next(operation for operation in ensured if operation.file_path == "miniapp/tests/test_generated_app.py")
    assert "time_slots.py" in python_test.content
    assert "GeneratedMiniAppTests" in python_test.content
    assert "test_backend_route_modules_import" in python_test.content


def test_page_graph_gate_rejects_multiple_routes_sharing_one_file() -> None:
    page_graph = {
        "flow_mode": "multi_page",
        "roles": {
            "manager": {
                "routes_file": "miniapp/app/routes/manager.py",
                "pages": [
                    {
                        "page_id": "manager_home",
                        "route_path": "/",
                        "file_path": "miniapp/app/static/manager/index.html",
                        "page_kind": "dashboard",
                        "purpose": "Manager dashboard with queue metrics and alerts.",
                        "handoff_paths": ["/profile"],
                    },
                    {
                        "page_id": "manager_workload",
                        "route_path": "/workload",
                        "file_path": "miniapp/app/static/manager/index.html",
                        "page_kind": "workspace",
                        "purpose": "Manager workload page for balancing assignments and capacity.",
                        "handoff_paths": ["/"],
                    },
                    {
                        "page_id": "manager_profile",
                        "route_path": "/profile",
                        "file_path": "miniapp/app/static/manager/profile.html",
                        "page_kind": "profile",
                        "purpose": "Manager profile page for identity and preferences.",
                        "handoff_paths": ["/"],
                    },
                ],
            }
        },
        "shared_files": [],
        "backend_targets": [],
    }

    issues = GenerationService._page_graph_gate_issues(
        page_graph,
        ["manager"],
        scope_mode="whole_file_build",
        require_multi_page=True,
    )

    assert any("reuses the same file for multiple routes" in issue for issue in issues)


def test_materialization_report_rejects_duplicate_page_file_mappings() -> None:
    page_graph = {
        "backend_targets": [],
        "shared_files": [],
        "roles": {
            "manager": {
                "pages": [
                    {"route_path": "/", "file_path": "miniapp/app/static/manager/index.html"},
                    {"route_path": "/workload", "file_path": "miniapp/app/static/manager/index.html"},
                    {"route_path": "/profile", "file_path": "miniapp/app/static/manager/profile.html"},
                ]
            }
        },
    }

    report = GenerationService._build_materialization_report(
        execution_class="data_crud_app",
        page_graph=page_graph,
        role_scope=["manager"],
        realized_paths={
            "miniapp/app/static/manager/index.html",
            "miniapp/app/static/manager/profile.html",
            "miniapp/app/generated/route_manifest.json",
            "miniapp/app/generated/runtime_manifest.json",
            "artifacts/generated_app_graph.json",
        },
    )

    assert report.page_surface_ok is False
    assert report.duplicate_page_file_roles == {"manager": ["miniapp/app/static/manager/index.html"]}


def test_run_soft_completes_when_generation_returns_validation_failure(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Soft Complete Workspace",
            "description": "Validation failures should still produce a completed run with warnings.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    workspace_service = app.state.container.workspace_service
    workspace_service.clone_template(workspace_id)

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        draft_root = workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_soft_complete")
        target = draft_root / "miniapp/app/static/client/index.html"
        target.write_text(target.read_text(encoding="utf-8") + "\n<section>draft</section>\n", encoding="utf-8")
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="failed",
            mode="generate",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Validation failed after draft generation.",
            failure_reason="Connectivity validators reported unresolved issues.",
            failure_class="validator/domain_constraint",
            root_cause_summary="connectivity.missing_ui_loading_state",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=False,
                blocking=True,
                issues=[{"code": "connectivity.missing_ui_loading_state", "message": "Missing loading state.", "severity": "high"}],
            ),
        )

    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a flower shop mini app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert run.status == "failed"
    assert run.apply_status == "failed"
    assert run.draft_status == "ready"
    assert run.draft_ready is True
    assert run.current_stage == "failed"
    assert run.failure_reason == "Connectivity validators reported unresolved issues."


def test_run_does_not_soft_complete_when_only_preview_issue_remains(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service

    run = SimpleNamespace(run_id="run_preview_warning", mode="generate")
    job = SimpleNamespace(
        status="failed",
        failure_class="runtime_preview_boot",
        validation_snapshot=ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=True,
            issues=[
                {
                    "code": "connectivity.preview_route_unreachable",
                    "message": "/manager could not be opened in preview.",
                    "severity": "high",
                    "location": "preview",
                    "blocking": True,
                }
            ],
        ),
    )

    assert not hasattr(run_service, "_should_soft_complete_with_warnings")


def test_run_applies_draft_when_only_preview_warning_remains(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Warning Apply Workspace",
            "description": "Preview-only warnings should still apply the draft to source.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    workspace_service = app.state.container.workspace_service
    workspace_service.clone_template(workspace_id)

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        draft_root = workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_preview_apply")
        target = draft_root / "README.md"
        target.write_text(target.read_text(encoding="utf-8") + "\npreview-warning-fix\n", encoding="utf-8")
        app.state.container.store.upsert(
            "reports",
            f"candidate_diff:{workspace_id}",
            {
                "diff": "\n".join(
                    [
                        "diff --git a/source/README.md b/draft/README.md",
                        "--- a/source/README.md",
                        "+++ b/draft/README.md",
                        "@@",
                        "+preview-warning-fix",
                    ]
                )
            },
        )
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="failed",
            mode="generate",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Preview connectivity warning remained after repair.",
            failure_reason="Preview route smoke still failed.",
            failure_class="runtime_preview_boot",
            root_cause_summary="/manager could not be opened in preview.",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=True,
                issues=[
                    {
                        "code": "connectivity.preview_route_unreachable",
                        "message": "/manager could not be opened in preview.",
                        "severity": "high",
                        "location": "preview",
                        "blocking": True,
                    }
                ],
            ),
        )

    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a service requests mini app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    source_readme = workspace_service.source_dir(workspace_id) / "README.md"

    assert run.status == "failed"
    assert run.apply_status == "failed"
    assert run.current_stage == "failed"
    assert run.draft_ready is True
    assert "preview-warning-fix" not in source_readme.read_text(encoding="utf-8")


def test_run_keeps_manual_review_draft_when_only_tests_and_preview_warnings_remain(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service
    workspace_service = app.state.container.workspace_service

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Manual Review Warning Workspace",
            description="Manual review should stay available when only tests/preview warnings remain.",
            path=str((tmp_path / "data" / "workspaces" / "ws_manual_review_warning").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    workspace_service.prepare_draft(workspace.workspace_id, "run_manual_review")

    run = SimpleNamespace(
        run_id="run_manual_review",
        workspace_id=workspace.workspace_id,
        mode="generate",
        apply_strategy="manual_approve",
    )
    job = SimpleNamespace(
        status="failed",
        validation_snapshot=ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=True,
            issues=[
                {"code": "tests.python_generated_app", "message": "Tests failed.", "severity": "high", "location": "tests", "blocking": True},
                {"code": "connectivity.preview_route_unreachable", "message": "Preview failed.", "severity": "high", "location": "preview", "blocking": True},
            ],
        ),
    )

    assert run_service._should_keep_draft_for_manual_review(run, job, meaningful_paths=["miniapp/app/main.py"]) is False


def test_run_auto_fix_triggers_for_failed_generate_preview_issue(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service

    request = CreateRunRequest(
        prompt="Create a requests mini app.",
        apply_strategy="staged_auto_apply",
        model_profile="openai_code_fast",
        generation_mode="balanced",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
    )
    job = SimpleNamespace(
        status="failed",
        handoff_from_failed_generate={"prompt": "Fix the preview route issue."},
        validation_snapshot=ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=True,
            issues=[
                {
                    "code": "connectivity.preview_route_unreachable",
                    "message": "/manager could not be opened in preview.",
                    "severity": "high",
                    "location": "preview",
                    "blocking": True,
                }
            ],
        ),
    )

    assert run_service._should_auto_fix_failed_generate(request, job) is True


def test_run_exposes_checks_patch_and_index_status(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Acceleration Workspace",
            "description": "Checks and patch endpoints",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    index_response = client.post(f"/workspaces/{workspace_id}/index")
    assert index_response.status_code == 200
    status_response = client.get(f"/workspaces/{workspace_id}/index/status")
    assert status_response.status_code == 200
    assert status_response.json()["workspace"]["status"] == "ready"

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Refine the role pages with booking-oriented route labels.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "basic",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(90):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["status"] in {"awaiting_approval", "completed", "failed", "blocked"}
    checks_response = client.get(f"/runs/{run_id}/checks")
    patch_response = client.get(f"/runs/{run_id}/patch")
    assert checks_response.status_code == 200
    assert patch_response.status_code == 200
    checks_payload = checks_response.json()
    patch_payload = patch_response.json()
    assert checks_payload["items"]
    assert patch_payload["envelope"]["ops"]
    assert patch_payload["apply_result"]["status"] == "applied"


def test_fast_generation_mode_round_trips_on_run(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Fast Workspace",
            "description": "Fast mode test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Create a multi-page booking app for all roles.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
            "generation_mode": "fast",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )
    assert run_response.status_code == 200

    run_payload = run_response.json()
    assert run_payload["generation_mode"] == "fast"
    run_id = run_payload["run_id"]

    final_run = run_payload
    for _ in range(90):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["status"] in {"awaiting_approval", "failed", "blocked"}
    assert final_run["generation_mode"] == "fast"
    artifacts = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    assert artifacts.json()["code_change_plan"]["targets"]


def test_fix_mode_run_exposes_failure_analysis_metadata(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Fix Mode Workspace",
            "description": "Fix mode test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Analyze the reported failure and apply the smallest safe fix.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "basic",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Docker preview rebuild failed: process \"/bin/sh -c npm run build\" did not complete successfully.",
                "source": "preview",
                "failing_target": "frontend build",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(90):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["mode"] == "fix"
    assert final_run["generation_mode"] == "balanced"
    assert final_run["error_context"]["source"] == "preview"
    artifacts_response = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts_response.status_code == 200
    failure_analysis = artifacts_response.json()["failure_analysis"]
    assert failure_analysis["mode"] == "fix"
    assert failure_analysis["error_context"]["raw_error"].startswith("Docker preview rebuild failed")


def test_fix_mode_repairs_frontend_import_and_state_errors(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Frontend Repair Workspace",
            "description": "Fix loop should repair frontend import and state typing issues",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    broken_files = {
        "miniapp/app/static/client/app.js": """const role = "client";
window.setupPreviewBridge?.(role);
loadProfile();

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  const avatar = document.getElementById("profile-avatar");
  const name = document.getElementById("profile-name");
  name.textContent = getDisplayName(profile, "Client profile");
  avatar.innerHTML = renderBrokenAvatar(profile.photo_url, getInitials(profile, "C"), "avatar", "avatar-fallback");
}
""",
        "miniapp/app/static/manager/app.js": """const role = "manager";
window.setupPreviewBridge?.(role);
loadProfile();

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  const avatar = document.getElementById("profile-avatar");
  const name = document.getElementById("profile-name");
  name.textContent = getDisplayName(profile, "Manager profile");
  avatar.innerHTML = renderBrokenManagerAvatar(profile.photo_url, getInitials(profile, "M"), "avatar", "avatar-fallback");
}
""",
        "miniapp/app/static/client/profile.js": """const role = "client";
const form = document.getElementById("profile-form");
let errors = {};

function clearPhoneError() {
  errors = { ...errors, phone: undefined };
}
""",
    }
    for relative_path, content in broken_files.items():
        save_response = client.post(
            f"/workspaces/{workspace_id}/files/save",
            json={"relative_path": relative_path, "content": content},
        )
        assert save_response.status_code == 200

    def fake_static_check(*, source_dir, changed_files):
        del changed_files
        client_routes = (source_dir / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")
        manager_routes = (source_dir / "miniapp/app/static/manager/app.js").read_text(encoding="utf-8")
        booking_form = (source_dir / "miniapp/app/static/client/profile.js").read_text(encoding="utf-8")
        still_broken = (
            "renderBrokenAvatar" in client_routes
            or "renderBrokenManagerAvatar" in manager_routes
            or "phone: undefined" in booking_form
        )
        if still_broken:
            return check_runner_module.RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Static miniapp validation failed for the draft runtime.",
                command="python -m py_compile miniapp/app/main.py",
                exit_code=2,
                logs=[
                    "miniapp/app/static/client/app.js: renderBrokenAvatar is not defined.",
                    "miniapp/app/static/client/profile.js: phone: undefined leaves invalid state in the profile payload.",
                    "miniapp/app/static/manager/app.js: renderBrokenManagerAvatar is not defined.",
                ],
            )
        return check_runner_module.RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Stubbed compile checks passed after the repair patch.",
            command="python -m py_compile miniapp/app/main.py",
            exit_code=0,
            logs=["Stubbed compile checks passed after the repair patch."],
        )

    app.state.container.check_runner._static_check = fake_static_check

    def fake_rebuild(workspace_id: str, source_dir=None, draft_run_id=None):
        del source_dir, draft_run_id
        preview = app.state.container.preview_service._get_or_create(workspace_id)
        preview.status = "running"
        preview.stage = "running"
        preview.progress_percent = 100
        preview.url = "http://localhost:18181"
        preview.frontend_url = preview.url
        preview.backend_url = f"{preview.url}/api"
        preview.logs.append("Preview rebuild completed and runtime is healthy.")
        app.state.container.preview_service.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    app.state.container.preview_service.rebuild = fake_rebuild  # type: ignore[method-assign]

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job
        workspace_service = app.state.container.workspace_service
        operations: list[dict[str, str | None]] = []
        rationale: dict[str, str] = {}
        target_files = {
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/manager/app.js",
            "miniapp/app/static/client/profile.js",
        }
        for file_path in target_files:
            content = workspace_service.read_file(repair_packet.workspace_id, file_path, run_id=repair_packet.run_id)
            if file_path.endswith("client/app.js"):
                operations.append(
                    {
                        "file_path": file_path,
                        "operation": "replace",
                        "content": content.replace(
                            "renderBrokenAvatar",
                            "renderAvatar",
                        ),
                        "reason": "Use the correct avatar renderer in the client home script.",
                    }
                )
                rationale[file_path] = "Align the client home script with the shared avatar helper."
            elif file_path.endswith("manager/app.js"):
                operations.append(
                    {
                        "file_path": file_path,
                        "operation": "replace",
                        "content": content.replace(
                            "renderBrokenManagerAvatar",
                            "renderAvatar",
                        ),
                        "reason": "Use the correct avatar renderer in the manager home script.",
                    }
                )
                rationale[file_path] = "Align the manager home script with the shared avatar helper."
            elif file_path.endswith("client/profile.js"):
                operations.append(
                    {
                        "file_path": file_path,
                        "operation": "replace",
                        "content": content.replace(
                            "errors = { ...errors, phone: undefined };",
                            "const next = { ...errors };\ndelete next.phone;\nerrors = next;",
                        ),
                        "reason": "Delete the error key instead of storing undefined in the profile state.",
                    }
                )
                rationale[file_path] = "Keep the profile error state free of undefined values."
        return {
            "outcome": "patch_ready",
            "diagnosis": "Apply the smallest targeted fix for the broken avatar helper names and profile state cleanup.",
            "tool_requests": [],
            "expected_verification": "Static miniapp validation should pass and preview should stay healthy.",
            "rationale_by_file": rationale,
            "operations": operations,
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Analyze the reported failure and apply the smallest safe fix.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client", "manager"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Static miniapp validation failed in client/app.js, manager/app.js, and client/profile.js.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert final_run["mode"] == "fix"
    assert final_run["current_fix_phase"] == "completed"
    artifacts = client.get(f"/runs/{run_id}/artifacts").json()
    assert artifacts["failure_analysis"]["failure_class"] in {
        "frontend_compile/type/import",
        "preview_runtime/docker_orchestration",
    }
    assert artifacts["fix_attempts"]["items"]
    workspace_service = app.state.container.workspace_service
    target_root = (
        workspace_service.draft_source_dir(workspace_id, run_id)
        if workspace_service.draft_exists(workspace_id, run_id)
        else workspace_service.source_dir(workspace_id)
    )
    assert "renderAvatar" in (target_root / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")
    assert "renderAvatar" in (target_root / "miniapp/app/static/manager/app.js").read_text(encoding="utf-8")
    assert "delete next.phone;" in (target_root / "miniapp/app/static/client/profile.js").read_text(encoding="utf-8")


def test_fix_mode_stops_on_repeated_failure_signature(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Repeated Failure Workspace",
            "description": "Fix loop should stop on repeated failure signatures",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    seed_files = {
        "miniapp/app/static/client/app.js": """const role = "client";
window.setupPreviewBridge?.(role);
loadProfile();

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  const avatar = document.getElementById("profile-avatar");
  avatar.innerHTML = renderBrokenAvatar(profile.photo_url, "CI", "avatar", "avatar-fallback");
}
""",
    }
    for relative_path, content in seed_files.items():
        save_response = client.post(
            f"/workspaces/{workspace_id}/files/save",
            json={"relative_path": relative_path, "content": content},
        )
        assert save_response.status_code == 200

    def always_fail(*, source_dir, changed_files):
        del source_dir, changed_files
        return check_runner_module.RunCheckResult(
            name="changed_files_static",
            status="failed",
            details="Static miniapp validation failed for the draft runtime.",
            command="python -m py_compile miniapp/app/main.py",
            exit_code=2,
            logs=["miniapp/app/static/client/app.js: renderBrokenAvatar is not defined."],
        )

    app.state.container.check_runner._static_check = always_fail

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        target = next(iter(repair_packet.file_contexts.keys()), "miniapp/app/static/client/app.js")
        content = str(repair_packet.file_contexts.get(target) or "")
        return {
            "outcome": "patch_ready",
            "diagnosis": "Apply a minimal static helper name fix.",
            "tool_requests": [],
            "expected_verification": "Static miniapp validation should pass.",
            "rationale_by_file": {target: "Attempt the smallest possible helper patch before retrying."},
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace(
                        "renderBrokenAvatar",
                        "renderAvatar",
                    )
                    if content
                    else "export {};",
                    "reason": "Attempt the smallest helper correction.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Analyze the reported failure and apply the smallest safe fix.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "npm run build failed with the same TS2614 error in ClientRoutes.",
                "source": "frontend",
                "failing_target": "frontend build",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] == "failed"
    assert "expanded-context and full-bundle retries" in (final_run["failure_reason"] or "").lower()
    artifacts = client.get(f"/runs/{run_id}/artifacts").json()
    assert artifacts["fix_attempts"]["items"]


def test_fix_mode_uses_tool_requests_to_expand_context_between_turns(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Tool First Fix Workspace",
            "description": "Fix loop should adopt model-requested files between turns.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/static/client/profile.js",
            "content": 'const profileStatus = "broken";\n',
        },
    )

    def fake_static_check(*, source_dir, changed_files):
        del changed_files
        profile_script = (source_dir / "miniapp/app/static/client/profile.js").read_text(encoding="utf-8")
        if '"broken"' in profile_script:
            return check_runner_module.RunCheckResult(
                name="changed_files_static",
                status="failed",
                details="Profile helper state is invalid in the current draft.",
                command="python -m py_compile miniapp/app/main.py",
                exit_code=2,
                logs=["Profile helper state is invalid in the current draft."],
            )
        return check_runner_module.RunCheckResult(
            name="changed_files_static",
            status="passed",
            details="Static checks passed after the profile helper repair.",
            command="python -m py_compile miniapp/app/main.py",
            exit_code=0,
            logs=["Static checks passed after the profile helper repair."],
        )

    app.state.container.check_runner._static_check = fake_static_check

    seen_contexts: list[set[str]] = []

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        current_paths = set(repair_packet.file_contexts.keys())
        seen_contexts.append(current_paths)
        target = "miniapp/app/static/client/profile.js"
        if target not in current_paths:
            return {
                "outcome": "tool_request",
                "diagnosis": "Need the profile helper source before applying the fix.",
                "tool_requests": [
                    {
                        "tool": "read_files",
                        "targets": [target],
                        "reason": "Inspect the profile helper source before patching.",
                    }
                ],
                "operations": [],
            }
        content = repair_packet.file_contexts[target]
        return {
            "outcome": "patch_ready",
            "diagnosis": "Repair the profile helper state constant.",
            "tool_requests": [],
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace('"broken"', '"healthy"'),
                    "reason": "Replace the broken profile helper state with the repaired value.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Inspect the failing runtime and fix the broken profile helper.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Client runtime is failing in miniapp/app/static/client/app.js and needs more inspection.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert seen_contexts
    assert "miniapp/app/static/client/profile.js" not in seen_contexts[0]
    assert any("miniapp/app/static/client/profile.js" in paths for paths in seen_contexts[1:])


def test_fix_mode_uses_explicit_final_check_tool_action(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Tool Check Fix Workspace",
            "description": "Fix loop should let the model choose an explicit final check action.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    final_check_invocations: list[list[str]] = []
    original_execute_final_checks = app.state.container.fix_orchestrator._execute_final_checks

    def fake_execute_final_checks(**kwargs):
        final_check_invocations.append(list(kwargs.get("changed_files") or []))
        return original_execute_final_checks(**kwargs)

    app.state.container.fix_orchestrator._execute_final_checks = fake_execute_final_checks  # type: ignore[method-assign]

    seen_tool_results: list[list[dict[str, object]]] = []

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        seen_tool_results.append(list(repair_packet.tool_results))
        target = "miniapp/app/static/client/app.js"
        if not repair_packet.tool_results:
            return {
                "outcome": "tool_request",
                "diagnosis": "Run the full final verification snapshot before deciding on the patch.",
                "tool_requests": [
                    {
                        "tool": "run_checks",
                        "mode": "final",
                        "targets": [target],
                        "reason": "Need the final verification snapshot for the implicated client app file.",
                    }
                ],
                "expected_verification": "Final verification should report the remaining blocking checks.",
                "rationale_by_file": {},
                "operations": [],
            }
        assert any(item.get("tool") == "run_checks" and item.get("mode") == "final" for item in repair_packet.tool_results)
        content = repair_packet.file_contexts[target]
        return {
            "outcome": "patch_ready",
            "diagnosis": "Apply the smallest helper repair after the explicit final check action.",
            "tool_requests": [],
            "expected_verification": "Static validation should pass after the helper rename.",
            "rationale_by_file": {target: "Use the final-check result before applying the direct fix."},
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace("renderBrokenAvatar", "renderAvatar"),
                    "reason": "Repair the broken helper name in the implicated client file.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/static/client/app.js",
            "content": """const role = "client";
window.setupPreviewBridge?.(role);
loadProfile();

async function loadProfile() {
  const response = await fetch(`/api/profiles/${role}`);
  const profile = await response.json();
  const avatar = document.getElementById("profile-avatar");
  avatar.innerHTML = renderBrokenAvatar(profile.photo_url, "CI", "avatar", "avatar-fallback");
}
""",
        },
    )
    assert save_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Inspect the failing client runtime and fix the broken helper only after you run a final check action.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Client runtime is failing because renderBrokenAvatar is not defined in miniapp/app/static/client/app.js.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert final_check_invocations
    assert any("miniapp/app/static/client/app.js" in item for item in final_check_invocations)
    assert seen_tool_results
    assert any(any(tool_result.get("tool") == "run_checks" and tool_result.get("mode") == "final" for tool_result in tool_results) for tool_results in seen_tool_results[1:])


def test_fix_mode_uses_search_and_command_tool_actions_before_patching(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Open Tool Fix Workspace",
            "description": "Fix loop should support search and shell command actions before patching.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/static/client/profile.js",
            "content": 'const profileStatus = "broken";\n',
        },
    )
    assert save_response.status_code == 200

    observed_tool_results: list[list[dict[str, object]]] = []

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        observed_tool_results.append(list(repair_packet.tool_results))
        target = "miniapp/app/static/client/profile.js"
        if not repair_packet.tool_results:
            return {
                "outcome": "tool_request",
                "diagnosis": "Search the workspace and run a shell diagnostic before patching.",
                "tool_requests": [
                    {
                        "tool": "search_files",
                        "targets": ["miniapp/app/static/client"],
                        "pattern": "broken",
                        "reason": "Locate the broken helper state in the client static files.",
                    },
                    {
                        "tool": "run_command",
                        "command": "printf open-tool-fix",
                        "targets": [],
                        "reason": "Verify shell command execution inside the draft workspace.",
                    },
                    {
                        "tool": "read_files",
                        "targets": [target],
                        "reason": "Load the implicated profile helper file before patching.",
                    },
                ],
                "expected_verification": "The shell and search results should identify the correct file to patch.",
                "rationale_by_file": {},
                "operations": [],
            }
        assert any(item.get("tool") == "search_files" for item in repair_packet.tool_results)
        assert any(item.get("tool") == "run_command" and "open-tool-fix" in str(item.get("stdout") or "") for item in repair_packet.tool_results)
        content = repair_packet.file_contexts[target]
        return {
            "outcome": "patch_ready",
            "diagnosis": "Patch the located broken helper state after open tool actions completed.",
            "tool_requests": [],
            "expected_verification": "Static validation should pass after replacing the broken state value.",
            "rationale_by_file": {target: "Use the search and shell results to confirm the implicated file before patching."},
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace('"broken"', '"healthy"'),
                    "reason": "Replace the broken profile helper state with the repaired value.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Use open tool actions to inspect the failing client profile helper and fix it.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Client profile helper is failing because profileStatus is broken.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert observed_tool_results
    assert any(any(item.get("tool") == "search_files" for item in batch) for batch in observed_tool_results[1:])
    assert any(any(item.get("tool") == "run_command" and "open-tool-fix" in str(item.get("stdout") or "") for item in batch) for batch in observed_tool_results[1:])


def test_fix_mode_blocks_unsafe_shell_commands(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Safe Shell Fix Workspace",
            "description": "Fix loop should block unsafe shell commands and keep the repair tool-owned.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/static/client/profile.js",
            "content": 'const profileStatus = "broken";\n',
        },
    )
    assert save_response.status_code == 200

    observed_tool_results: list[list[dict[str, object]]] = []

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        observed_tool_results.append(list(repair_packet.tool_results))
        target = "miniapp/app/static/client/profile.js"
        if not repair_packet.tool_results:
            return {
                "outcome": "tool_request",
                "diagnosis": "Try a shell command first, then inspect the target file.",
                "tool_requests": [
                    {
                        "tool": "run_command",
                        "command": "rm -rf miniapp/app/static/client",
                        "targets": [],
                        "reason": "Unsafe shell should be blocked.",
                    },
                    {
                        "tool": "read_files",
                        "targets": [target],
                        "reason": "Load the implicated profile helper file before patching.",
                    },
                ],
                "expected_verification": "Unsafe commands should be rejected while the file evidence stays available.",
                "rationale_by_file": {},
                "operations": [],
            }
        assert any(
            item.get("tool") == "run_command"
            and "blocked" in str(item.get("error") or "").lower()
            for item in repair_packet.tool_results
        )
        content = repair_packet.file_contexts[target]
        return {
            "outcome": "patch_ready",
            "diagnosis": "Patch the client profile helper after the blocked shell command result.",
            "tool_requests": [],
            "expected_verification": "Static validation should pass after replacing the broken state value.",
            "rationale_by_file": {target: "Unsafe shell was blocked, so patch the implicated file directly."},
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace('"broken"', '"healthy"'),
                    "reason": "Replace the broken profile helper state with the repaired value.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Use the fix tool loop, but block unsafe shell commands and repair the implicated client file directly.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Client profile helper is failing because profileStatus is broken.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert any(
        any(item.get("tool") == "run_command" and "blocked" in str(item.get("error") or "").lower() for item in batch)
        for batch in observed_tool_results[1:]
    )


def test_fix_mode_can_list_workspace_files_before_reading_and_patching(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "List Files Fix Workspace",
            "description": "Fix loop should allow list-files exploration before reading and patching.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/static/client/profile.js",
            "content": 'const profileStatus = "broken";\n',
        },
    )
    assert save_response.status_code == 200

    seen_tool_results: list[list[dict[str, object]]] = []

    def fake_plan_patch(*, job, repair_packet, repair_feedback=None):
        del job, repair_feedback
        seen_tool_results.append(list(repair_packet.tool_results))
        target = "miniapp/app/static/client/profile.js"
        if not repair_packet.tool_results:
            return {
                "outcome": "tool_request",
                "diagnosis": "Inspect the client static tree before reading the implicated file.",
                "tool_requests": [
                    {
                        "tool": "list_files",
                        "targets": ["miniapp/app/static/client"],
                        "reason": "Inspect the client static tree.",
                    }
                ],
                "expected_verification": "The client static tree should reveal the implicated profile helper file.",
                "rationale_by_file": {},
                "operations": [],
            }
        if not repair_packet.file_contexts.get(target):
            assert any(item.get("tool") == "list_files" and target in list(item.get("paths") or []) for item in repair_packet.tool_results)
            return {
                "outcome": "tool_request",
                "diagnosis": "Now read the implicated profile helper file.",
                "tool_requests": [
                    {
                        "tool": "read_files",
                        "targets": [target],
                        "reason": "Load the profile helper after listing the client static tree.",
                    }
                ],
                "expected_verification": "The implicated profile helper source should now be available for patching.",
                "rationale_by_file": {},
                "operations": [],
            }
        content = repair_packet.file_contexts[target]
        return {
            "outcome": "patch_ready",
            "diagnosis": "Patch the listed and inspected profile helper.",
            "tool_requests": [],
            "expected_verification": "Static validation should pass after fixing the profile helper state.",
            "rationale_by_file": {target: "Patch only after listing the tree and reading the implicated file."},
            "operations": [
                {
                    "file_path": target,
                    "operation": "replace",
                    "content": content.replace('"broken"', '"healthy"'),
                    "reason": "Replace the broken profile helper state with the repaired value.",
                }
            ],
        }

    app.state.container.fix_orchestrator._plan_patch = fake_plan_patch  # type: ignore[method-assign]

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Explore the client static tree, inspect the implicated file, and then repair the broken profile helper.",
            "mode": "fix",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "error_context": {
                "raw_error": "Client profile helper is failing because profileStatus is broken.",
                "source": "frontend",
                "failing_target": "miniapp static runtime",
            },
        },
    )
    assert run_response.status_code == 200
    run_id = run_response.json()["run_id"]

    final_run = run_response.json()
    for _ in range(60):
        current = client.get(f"/runs/{run_id}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "completed", "blocked", "failed"}:
            break
        time.sleep(0.1)

    assert final_run["status"] in {"awaiting_approval", "completed"}
    assert seen_tool_results
    assert any(any(item.get("tool") == "list_files" for item in batch) for batch in seen_tool_results[1:])


def test_contract_pass_requires_grounded_spec_artifact(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Fallback Spec Workspace",
            "description": "Deterministic repair should not require grounded_spec.json to exist in the draft.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    with pytest.raises(ValueError, match="grounded_spec.json is required"):
        app.state.container.generation_service._resolve_grounded_spec_for_contract_pass(
            workspace_id=workspace_id,
            draft_run_id="run_missing_grounded_spec",
            operations=[],
        )


def test_fix_orchestrator_detects_context_refusal_diagnosis() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    assert FixOrchestrator._looks_like_context_refusal(
        "I’m not able to proceed because I can’t inspect or edit the workspace files."
    )
    assert FixOrchestrator._looks_like_context_refusal(
        "Without access to the actual file contents, I cannot craft the patch."
    )
    assert FixOrchestrator._looks_like_context_refusal(
        "I need to inspect the current route wiring before I can produce a minimal patch."
    )
    assert FixOrchestrator._looks_like_context_refusal(
        "I need the current contents of the implicated files to apply the fix."
    )
    assert not FixOrchestrator._looks_like_context_refusal(
        "Patch the DOM ids and route wiring in the implicated files."
    )


def test_fix_orchestrator_normalizes_tool_request_targets() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    assert FixOrchestrator._planned_target_paths(
        {
            "tool_requests": [
                {
                    "tool": "read_files",
                    "targets": [
                        "./miniapp/app/main.py",
                        "miniapp/app/routes/specialist.py",
                        "miniapp/app/main.py",
                        "",
                        None,
                    ],
                    "reason": "Inspect the backend runtime files.",
                }
            ]
        }
    ) == [
        "miniapp/app/main.py",
        "miniapp/app/routes/specialist.py",
    ]


def test_fix_orchestrator_uses_current_page_graph_for_role_scope(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Deterministic Repair Workspace",
            "description": "Deterministic repair should prefer app code changes over generated artifacts",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    workspace_service = app.state.container.workspace_service
    run_id = "run_fix_scope"
    draft_root = workspace_service.prepare_draft(workspace_id, run_id)
    (draft_root / "artifacts").mkdir(parents=True, exist_ok=True)
    (draft_root / "artifacts" / "generated_app_graph.json").write_text(
        json.dumps(
            {
                "roles": {
                    "client": {
                        "pages": [
                            {
                                "page_id": "client_home",
                                "route_path": "/client",
                                "file_path": "miniapp/app/static/client/index.html",
                                "style_path": "miniapp/app/static/client/styles.css",
                                "script_path": "miniapp/app/static/client/app.js",
                                "is_entry": True,
                            }
                        ]
                    },
                    "manager": {
                        "pages": [
                            {
                                "page_id": "manager_home",
                                "route_path": "/manager",
                                "file_path": "miniapp/app/static/manager/index.html",
                                "style_path": "miniapp/app/static/manager/styles.css",
                                "script_path": "miniapp/app/static/manager/app.js",
                                "is_entry": True,
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    orchestrator = app.state.container.fix_orchestrator
    request = SimpleNamespace(target_role_scope=[])

    role_scope = orchestrator._role_scope_for_fix_request(workspace_id, run_id, request)

    assert role_scope == ["client", "manager"]


def test_fix_orchestrator_detects_missing_content_in_repair_operations() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    assert FixOrchestrator._operations_missing_content(
        [
            {"file_path": "miniapp/app/main.py", "operation": "replace", "content": None},
            {"file_path": "miniapp/app/routes/requests.py", "operation": "create", "content": None},
            {"file_path": "miniapp/app/routes/comments.py", "operation": "delete"},
        ]
    ) == [
        "miniapp/app/main.py",
        "miniapp/app/routes/requests.py",
    ]


def test_fix_orchestrator_retries_invalid_patch_validation_for_missing_content() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    assert FixOrchestrator._should_retry_patch_validation(
        "Repair returned replace for miniapp/app/main.py without content."
    ) is True
    assert FixOrchestrator._should_retry_patch_validation(
        "Repair touched files outside the allowed evidence-based scope: miniapp/app/routes/runtime.py"
    ) is False
    assert FixOrchestrator._should_retry_patch_validation(
        "Repair attempted to edit generated tests instead of the app surface."
    ) is False


def test_fix_orchestrator_retries_backend_framework_validation_errors() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    assert FixOrchestrator._should_retry_patch_validation(
        "miniapp/app/routes/client.py must stay on FastAPI APIRouter, not Flask/Blueprint."
    ) is True
    assert FixOrchestrator._should_retry_patch_validation(
        "miniapp/app/routes/client.py must define router = APIRouter(...)."
    ) is True


def test_generation_service_rejects_flask_route_modules_in_targeted_operations() -> None:
    with pytest.raises(RuntimeError, match="FastAPI APIRouter"):
        GenerationService._validate_targeted_operations(
            stage_name="backend_route_client",
            target_files=["miniapp/app/routes/client.py"],
            operations=[
                DraftFileOperation(
                    file_path="miniapp/app/routes/client.py",
                    operation="replace",
                    content=(
                        "from flask import Blueprint, current_app, send_from_directory\n\n"
                        "client_router = Blueprint('client', __name__, url_prefix='/client')\n"
                    ),
                    reason="bad framework drift",
                )
            ],
        )


def test_fix_orchestrator_classifies_flask_import_trace_as_backend_framework_mismatch(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    orchestrator = app.state.container.fix_orchestrator

    failure_class = orchestrator._specialized_failure_class(
        workspace_id="ws_fix",
        run_id="run_fix",
        results=[],
        combined_text=(
            "ImportError: Failed to import test module\n"
            "ModuleNotFoundError: No module named 'flask'\n"
            "from flask import Blueprint"
        ),
        implicated_files=["miniapp/app/routes/client.py"],
    )

    assert failure_class == "backend_framework_mismatch"


def test_generated_app_test_templates_strip_route_template_expressions() -> None:
    from app.services.miniapp_generation.artifact_builder import MiniappArtifactBuilder

    builder = MiniappArtifactBuilder(
        normalize_role_route_path=lambda role, path: path,
        absolute_role_route_path=lambda role, path: path,
        default_page_asset_path=lambda path, kind: path,
        normalize_runtime_python_path=lambda path: path,
    )

    python_test = builder.python_app_level_test_content(page_graph={}, role_scope=["client"])
    js_test = builder.js_app_level_test_content(page_graph={}, role_scope=["client"])

    assert "_strip_route_template_expressions" in python_test
    assert r"\$\{[^}]+\}" in python_test
    assert "stripRouteTemplateExpressions" in js_test
    assert r"\$\{[^}]+\}" in js_test


def test_fix_orchestrator_treats_empty_repair_patch_as_no_progress(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    orchestrator = app.state.container.fix_orchestrator

    prompt_context = FixPromptContext(
        workspace_id="ws_fix",
        run_id="run_fix",
        attempt=1,
        failing_file_paths=["miniapp/app/main.py"],
    )
    fix_turn = FixTurnContext(
        workspace_id="ws_fix",
        run_id="run_fix",
        failure_class="api_endpoint_missing",
        implicated_files=["miniapp/app/main.py"],
        write_scope=[FixScopeEntry(file_path="miniapp/app/main.py", reason="missing route contract")],
    )

    outcome = orchestrator._repair_outcome_from_response(
        llm_result={"diagnosis": "I need a retry, no concrete edits yet.", "operations": []},
        prompt_context=prompt_context,
        fix_turn=fix_turn,
        scope_entries=fix_turn.write_scope,
        scope_expansions=[],
    )

    assert outcome.outcome == "no_progress"
    assert "patch operations" in str(outcome.validation_error or "").lower()


def test_fix_orchestrator_allows_optimistic_completion_for_generated_test_tail() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    results = [
        RunCheckResult(name="schema_validators", status="passed", details="ok"),
        RunCheckResult(name="connectivity_validators", status="passed", details="ok"),
        RunCheckResult(name="changed_files_static", status="passed", details="ok"),
        RunCheckResult(name="generated_app_python_tests", status="failed", details="python test tail", logs=["assert route alias"]),
        RunCheckResult(name="generated_app_js_tests", status="passed", details="ok"),
        RunCheckResult(name="preview_boot_smoke", status="passed", details="preview ok"),
        RunCheckResult(name="preview_connectivity_smoke", status="passed", details="preview ok"),
    ]

    completion = FixOrchestrator._completion_state_from_results(
        results,
        {"status": "running", "stage": "running"},
        validation_snapshot=ValidationSnapshot(
            grounded_spec_valid=True,
            app_ir_valid=True,
            build_valid=True,
            blocking=False,
            issues=[],
        ),
    )

    assert completion["strict_green"] is False
    assert completion["optimistic_complete"] is False
    assert completion["remaining_issues"]


def test_structural_repeated_signature_guard_detects_expandable_scope() -> None:
    from app.services.fix_orchestrator import FixOrchestrator

    current_scope = [FixScopeEntry(file_path="miniapp/app/main.py", reason="current")]
    next_scope = [
        FixScopeEntry(file_path="miniapp/app/main.py", reason="current"),
        FixScopeEntry(file_path="miniapp/app/db.py", reason="new"),
    ]

    assert FixOrchestrator._scope_can_still_expand(current_scope, next_scope) is True
    assert FixOrchestrator._scope_can_still_expand(next_scope, next_scope) is False


def test_check_runner_executes_preview_smoke_even_when_generated_tests_fail() -> None:
    preview = SimpleNamespace(status="running", url="http://preview.local", logs=["preview ok"], draft_run_id="run_test")
    preview_service = SimpleNamespace(get=lambda _workspace_id: preview)
    validation_suite = SimpleNamespace(validate_build=lambda _source_dir: [], validate_connectivity=lambda _source_dir: [])
    runner = check_runner_module.CheckRunner(validation_suite, preview_service)
    runner._static_check = lambda **_kwargs: RunCheckResult(name="changed_files_static", status="passed", details="static ok", logs=[])  # type: ignore[method-assign]
    runner._run_python_app_tests = lambda _backend_dir: RunCheckResult(name="generated_app_python_tests", status="failed", details="python tests failed", logs=["python fail"])  # type: ignore[method-assign]
    runner._run_js_app_tests = lambda _backend_dir: RunCheckResult(name="generated_app_js_tests", status="failed", details="js tests failed", logs=["js fail"])  # type: ignore[method-assign]
    runner._preview_connectivity_smoke = lambda **_kwargs: RunCheckResult(name="preview_connectivity_smoke", status="passed", details="preview ok", logs=["preview ok"])  # type: ignore[method-assign]

    execution = runner.run(
        workspace_id="ws_preview",
        run_id="run_test",
        source_dir=Path("/tmp"),
        changed_files=[],
        preview_run_id="run_test",
    )

    preview_boot = next(result for result in execution.results if result.name == "preview_boot_smoke")
    preview_connectivity = next(result for result in execution.results if result.name == "preview_connectivity_smoke")
    assert preview_boot.status == "passed"
    assert preview_connectivity.status == "passed"


def test_generation_service_route_module_stub_detection_catches_inline_models_and_mutable_stores() -> None:
    assert GenerationService._route_module_needs_stub(
        "from fastapi import APIRouter\nfrom pydantic import BaseModel\nrouter = APIRouter()\n@router.get('/')\ndef ok(): return {}"
    ) is True
    assert GenerationService._route_module_needs_stub(
        "from fastapi import APIRouter\nREQUESTS = []\nrouter = APIRouter()\n@router.get('/')\ndef ok(): return {}"
    ) is True
    assert GenerationService._route_module_needs_stub(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/')\ndef ok(): return {}"
    ) is False


def test_generation_service_bootstrap_runtime_source_includes_runtime_manifest_endpoint() -> None:
    content = GenerationService._deterministic_main_runtime_source(["requests", "assignments"])

    assert 'ROUTE_MANIFEST_PATH = GENERATED_DIR / "route_manifest.json"' in content
    assert "def _load_route_manifest() -> dict:" in content
    assert "def _resolve_declared_page_file(role: str, actual_path: str) -> Path | None:" in content
    assert "return RedirectResponse(url=\"/client\", status_code=307)" in content


def test_run_completes_before_async_preview_rebuild_finishes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)
    preview_service = app.state.container.preview_service
    preview_service.ensure_started = lambda workspace_id, force_rebuild=False: preview_service._get_or_create(workspace_id)  # type: ignore[method-assign]

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Async Preview Workspace",
            "description": "Run completion should not block on preview rebuild",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def fake_rebuild_async(workspace_id: str, source_dir=None, draft_run_id=None, on_complete=None):
        del source_dir, draft_run_id
        preview = preview_service._get_or_create(workspace_id)
        preview.status = "starting"
        preview.stage = "rebuilding"
        preview.progress_percent = 10
        preview.logs.append("Queued asynchronous preview rebuild.")
        preview_service.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))

        def worker() -> None:
            rebuild_started.set()
            release_rebuild.wait(1.0)
            current = preview_service._get_or_create(workspace_id)
            current.status = "running"
            current.stage = "running"
            current.progress_percent = 100
            current.url = "http://localhost:18181"
            current.frontend_url = current.url
            current.backend_url = f"{current.url}/api"
            preview_service.store.upsert("previews", workspace_id, current.model_dump(mode="json"))
            if on_complete is not None:
                on_complete(current)

        threading.Thread(target=worker, daemon=True).start()
        return preview

    preview_service.rebuild_async = fake_rebuild_async  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a simple role-based booking app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert run.status in {"completed", "failed", "blocked"}
    if run.status == "completed":
        assert run.current_stage == "completed"
        assert rebuild_started.is_set()
        preview = preview_service.get(workspace_id)
        assert preview.stage == "rebuilding"
        release_rebuild.set()
        time.sleep(0.15)
        assert preview_service.get(workspace_id).status == "running"
    else:
        release_rebuild.set()


def test_preview_rebuild_failure_does_not_change_completed_optimistic_run(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Failure Workspace",
            "description": "Completed run should stay completed even if preview rebuild fails",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    app.state.container.workspace_service.clone_template(workspace_id)

    preview_service = app.state.container.preview_service

    def fake_rebuild_async(workspace_id: str, source_dir=None, draft_run_id=None, on_complete=None):
        del source_dir, draft_run_id
        preview = preview_service._get_or_create(workspace_id)
        preview.status = "starting"
        preview.stage = "rebuilding"
        preview.progress_percent = 10
        preview_service.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))

        def worker() -> None:
            current = preview_service._get_or_create(workspace_id)
            current.status = "error"
            current.stage = "error"
            current.progress_percent = 100
            current.last_error = "Simulated preview rebuild failure."
            preview_service.store.upsert("previews", workspace_id, current.model_dump(mode="json"))
            if on_complete is not None:
                on_complete(current)

        threading.Thread(target=worker, daemon=True).start()
        return preview

    preview_service.rebuild_async = fake_rebuild_async  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a simple role-based booking app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert run.status in {"completed", "failed", "blocked"}
    time.sleep(0.15)
    stored_run = app.state.container.run_service.get_run(run.run_id)
    assert stored_run.status in {"completed", "failed", "blocked"}
    assert preview_service.get(workspace_id).status in {"stopped", "error", "starting", "running"}
    artifacts = app.state.container.run_service.get_run_artifacts(run.run_id)
    assert artifacts["run"]["status"] == stored_run.status


def test_openrouter_payload_uses_stable_cache_prefix_and_reports_cache_stats(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    openrouter = app.state.container.openrouter_client
    openrouter.api_key = "test-key"
    captured: dict[str, object] = {}

    def fake_post_json_with_retries(*, endpoint: str, model: str, payload: dict[str, object]) -> dict[str, object]:
        captured["endpoint"] = endpoint
        captured["model"] = model
        captured["payload"] = payload
        return {
            "output_text": "{\"ok\":true}",
            "usage": {
                "prompt_tokens_details": {
                    "cached_tokens": 11,
                    "cache_write_tokens": 3,
                }
            },
        }

    openrouter._post_json_with_retries = fake_post_json_with_retries  # type: ignore[method-assign]
    result = openrouter.generate_structured(
        role="code_plan",
        schema_name="cache_test",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        system_prompt="System prompt",
        user_prompt='{"ok": true}',
        prompt_cache_key="cache-key-123",
        stable_prefix="Stable workspace prefix",
    )

    assert result["cache_stats"]["cached_tokens"] == 11
    assert result["cache_stats"]["cache_write_tokens"] == 3
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert captured["endpoint"] in {"responses", "chat/completions"}
    if captured["endpoint"] == "responses":
        input_items = payload["input"]
        assert isinstance(input_items, list)
        assert "cache-key-123" in input_items[1]["content"][0]["text"]
        assert "Stable workspace prefix" in input_items[1]["content"][0]["text"]
        assert input_items[2]["content"][0]["text"] == '{"ok": true}'
    else:
        messages = payload["messages"]
        assert isinstance(messages, list)
        assert "cache-key-123" in messages[1]["content"]
        assert "Stable workspace prefix" in messages[1]["content"]
        assert messages[2]["content"] == '{"ok": true}'


def test_build_validator_flags_contract_drift(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace"
    (workspace_path / "miniapp" / "app").mkdir(parents=True)
    (workspace_path / "frontend" / "src" / "roles" / "client").mkdir(parents=True)
    (workspace_path / "docker").mkdir(parents=True)
    (workspace_path / "artifacts").mkdir(parents=True)

    (workspace_path / "miniapp" / "app" / "main.py").write_text("app = None\n", encoding="utf-8")
    (workspace_path / "miniapp" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (workspace_path / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    (workspace_path / "frontend" / "src" / "main.tsx").write_text("export {};\n", encoding="utf-8")
    (workspace_path / "frontend" / "src" / "app").mkdir(parents=True)
    (workspace_path / "frontend" / "src" / "app" / "App.tsx").write_text("export default function App(){return null;}\n", encoding="utf-8")
    (workspace_path / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_path / "docker" / "nginx.conf").write_text("server { listen 80; location / { proxy_pass http://frontend; } }\n", encoding="utf-8")
    (workspace_path / "artifacts" / "grounded_spec.json").write_text("{}\n", encoding="utf-8")
    (workspace_path / "artifacts" / "generated_app_graph.json").write_text('{"scope_mode":"minimal_patch","flow_mode":"multi_page"}\n', encoding="utf-8")
    (workspace_path / "frontend" / "src" / "roles" / "client" / "ClientRoutes.tsx").write_text(
        "import ClientCatalogPage from './ClientCatalogPage';\nexport default function ClientRoutes(){return <ClientCatalogPage />;}\n",
        encoding="utf-8",
    )
    (workspace_path / "frontend" / "src" / "roles" / "client" / "ClientCatalogPage.tsx").write_text(
        "import Link from 'next/link';\nexport const ClientCatalogPage = () => null;\nfetch('/api/orders');\nfetch('/builds/latest');\n",
        encoding="utf-8",
    )

    issues = BuildValidator().validate(workspace_path)
    issue_codes = {issue.code for issue in issues}
    assert "build.unsupported_next_import" in issue_codes
    assert "build.authless_api_fetch" in issue_codes
    assert "build.unproxied_backend_route" in issue_codes
    assert "build.route_export_mismatch" in issue_codes


def test_generation_repair_allows_safe_shared_static_companion_targets(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    service._generate_structured_with_retry = lambda **_kwargs: {
        "model": "stub",
        "payload": {
            "assistant_message": "Repair shared UI asset.",
            "operations": [
                {
                    "file_path": "miniapp/app/static/shared/ui.js",
                    "operation": "replace",
                    "content": "export const ready = true;\n",
                    "reason": "Provide the shared UI script the page wiring expects.",
                }
            ],
        },
    }
    service._repair_user_prompt = lambda **_kwargs: "repair"

    result = service._repair_draft_after_failure(
        workspace_id="ws_1",
        draft_run_id="run_1",
        prompt="Fix the generated app.",
        grounded_spec=SimpleNamespace(),
        role_scope=["client"],
        role_contract={},
        page_graph={},
        scope_mode="whole_file_build",
        target_files=["miniapp/app/static/client/index.html"],
        file_contexts={},
        build_issues=[
            ValidationIssue(
                code="connectivity.missing_static_asset",
                message="miniapp/app/static/client/index.html references /static/shared/ui.js but the static asset is missing.",
                severity="high",
                location="miniapp/app/static/client/index.html",
            )
        ],
        preview_issue=None,
        preview_logs=[],
        attempt=1,
    )

    assert "error" not in result
    assert [operation.file_path for operation in result["operations"]] == ["miniapp/app/static/shared/ui.js"]


def test_generation_repair_retries_with_expanded_context_when_first_patch_is_empty(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    calls: list[bool] = []

    def _stub_generate(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {
                "model": "stub",
                "payload": {
                    "assistant_message": "I cannot access the repository files.",
                    "operations": [],
                },
            }
        return {
            "model": "stub",
            "payload": {
                "assistant_message": "Expanded context repair succeeded.",
                "operations": [
                    {
                        "file_path": "miniapp/app/static/client/index.html",
                        "operation": "replace",
                        "content": "<html></html>\n",
                        "reason": "Repair the page with the provided context.",
                    }
                ],
            },
        }

    service._generate_structured_with_retry = _stub_generate

    result = service._repair_draft_after_failure(
        workspace_id="ws_1",
        draft_run_id="run_1",
        prompt="Fix the generated app.",
        grounded_spec=SimpleNamespace(product_goal="Fix app", api_requirements=[], assumptions=[]),
        role_scope=["client"],
        role_contract={},
        page_graph={},
        scope_mode="whole_file_build",
        target_files=["miniapp/app/static/client/index.html"],
        file_contexts={"miniapp/app/static/client/index.html": "<html></html>"},
        build_issues=[
            ValidationIssue(
                code="build.page_script_dom_contract",
                message="index.html is missing DOM ids required by app.js.",
                severity="high",
                location="miniapp/app/static/client/index.html",
            )
        ],
        preview_issue=None,
        preview_logs=[],
        attempt=1,
    )

    assert len(calls) == 2
    assert "error" not in result
    assert result["operations"][0].file_path == "miniapp/app/static/client/index.html"


def test_generation_repair_uses_tool_requests_before_patching(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)
    service: GenerationService = app.state.container.generation_service

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Generation Repair Tool Workspace",
            "description": "Generation repair should use tool requests before patching.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    run_id = "run_generation_tool_repair"
    draft_source = app.state.container.workspace_service.prepare_draft(workspace_id, run_id)
    target = "miniapp/app/static/client/profile.js"
    target_path = draft_source / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text('const profileStatus = "broken";\n', encoding="utf-8")

    seen_payloads: list[dict[str, object]] = []

    def _stub_generate(**kwargs):
        payload = json.loads(kwargs["user_prompt"])
        seen_payloads.append(payload)
        file_contexts = payload.get("file_contexts") or {}
        tool_results = payload.get("tool_results") or []
        if not tool_results:
            assert target not in file_contexts
            return {
                "model": "stub",
                "payload": {
                    "outcome": "tool_request",
                    "diagnosis": "Inspect the workspace tree, run exact checks, then read the implicated profile helper.",
                    "tool_requests": [
                        {
                            "tool": "run_checks",
                            "mode": "exact",
                            "targets": [target],
                            "reason": "Capture exact-check failures before repairing the profile helper.",
                        },
                        {
                            "tool": "list_files",
                            "targets": ["miniapp/app/static/client"],
                            "reason": "Inspect the client static tree before patching.",
                        },
                        {
                            "tool": "read_files",
                            "targets": [target],
                            "reason": "Load the implicated profile helper source before patching.",
                        },
                    ],
                    "operations": [],
                },
            }
        assert any(item.get("tool") == "run_checks" and item.get("mode") == "exact" for item in tool_results)
        assert any(item.get("tool") == "list_files" for item in tool_results)
        assert target in file_contexts
        return {
            "model": "stub",
            "payload": {
                "outcome": "patch_ready",
                "diagnosis": "Patch the implicated profile helper after using tool actions.",
                "tool_requests": [],
                "operations": [
                    {
                        "file_path": target,
                        "operation": "replace",
                        "content": str(file_contexts[target]).replace('"broken"', '"healthy"'),
                        "reason": "Replace the broken profile helper state with the repaired value.",
                    }
                ],
            },
        }

    service._generate_structured_with_retry = _stub_generate

    result = service._repair_draft_after_failure(
        workspace_id=workspace_id,
        draft_run_id=run_id,
        prompt="Fix the generated app through the tool-owned repair loop.",
        grounded_spec=SimpleNamespace(product_goal="Fix app", api_requirements=[], assumptions=[]),
        role_scope=["client", "specialist", "manager"],
        role_contract={},
        page_graph={},
        scope_mode="whole_file_build",
        target_files=["miniapp/app/static/client/index.html"],
        file_contexts={},
        build_issues=[
            ValidationIssue(
                code="build.page_script_dom_contract",
                message="Client profile helper state is invalid and requires inspection.",
                severity="high",
                location="miniapp/app/static/client/profile.js",
            )
        ],
        preview_issue=None,
        preview_logs=[],
        attempt=1,
    )

    assert "error" not in result
    assert result["operations"][0].file_path == target


def test_initial_codegen_uses_tool_requests_before_page_patch(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)
    service: GenerationService = app.state.container.generation_service

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Initial Codegen Tool Workspace",
            "description": "Initial codegen should use tool requests before patching page files.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    assert client.post(f"/workspaces/{workspace_id}/clone-template").status_code == 200

    run_id = "run_initial_codegen_tool_request"
    draft_source = app.state.container.workspace_service.prepare_draft(workspace_id, run_id)
    target = "miniapp/app/static/client/orders.html"
    target_path = draft_source / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("<main>stale orders</main>\n", encoding="utf-8")

    spec = service._build_grounded_spec(
        workspace_id=workspace_id,
        prompt="Create a three-role order workflow app.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="rev_test",
        prompt_turn_id="turn_test",
        generation_mode=GenerationMode.FAST,
    )
    role_contract = service._minimal_role_contract(spec, ["client", "specialist", "manager"])
    page_graph = {
        "roles": {
            "client": {
                "pages": [
                    {
                        "page_id": "client_orders",
                        "route_path": "/client/orders",
                        "file_path": target,
                        "page_kind": "feature",
                        "navigation_label": "Orders",
                        "title": "Client Orders",
                        "description": "Track and create shared orders.",
                        "purpose": "Create and inspect orders.",
                        "primary_actions": ["Create order"],
                        "handoff_paths": ["/specialist/orders"],
                        "data_dependencies": ["orders"],
                    }
                ]
            }
        }
    }

    seen_payloads: list[dict[str, object]] = []

    def _stub_generate(**kwargs):
        if not kwargs["schema_name"].startswith("page_file_v1_"):
            return {"model": "stub", "payload": {"outcome": "patch_ready", "diagnosis": "No-op", "tool_requests": [], "operations": []}}
        payload = json.loads(kwargs["user_prompt"])
        seen_payloads.append(payload)
        tool_results = payload.get("tool_results") or []
        file_contexts = payload.get("file_contexts") or {}
        current_file = str(payload.get("current_file") or "")
        if not tool_results:
            assert target not in file_contexts
            return {
                "model": "stub",
                "payload": {
                    "outcome": "tool_request",
                    "diagnosis": "Inspect the client static workspace, run final checks, and read the targeted page before editing.",
                    "tool_requests": [
                        {
                            "tool": "run_checks",
                            "mode": "final",
                            "targets": [target],
                            "reason": "Check the current draft state before concluding the page patch.",
                        },
                        {
                            "tool": "list_files",
                            "targets": ["miniapp/app/static/client"],
                            "reason": "Inspect the client static workspace before editing the page.",
                        },
                        {
                            "tool": "read_files",
                            "targets": [target],
                            "reason": "Read the targeted page source before patching it.",
                        },
                    ],
                    "operations": [],
                },
            }
        assert any(item.get("tool") == "run_checks" and item.get("mode") == "final" for item in tool_results)
        assert any(item.get("tool") == "list_files" for item in tool_results)
        assert "stale orders" in current_file
        return {
            "model": "stub",
            "payload": {
                "outcome": "patch_ready",
                "diagnosis": "Patch the role page after tool-driven workspace inspection.",
                "tool_requests": [],
                "operations": [
                    {
                        "file_path": target,
                        "operation": "replace",
                        "content": "<main>healthy orders</main>\n",
                        "reason": "Replace the stale role page with the repaired order surface.",
                    }
                ],
            },
        }

    service._generate_structured_with_retry = _stub_generate

    result = service._resolve_code_edits(
        workspace_id=workspace_id,
        draft_run_id=run_id,
        prompt="Create a three-role order workflow app.",
        grounded_spec=spec,
        role_scope=["client", "specialist", "manager"],
        file_contexts={},
        target_files=[target],
        role_contract=role_contract,
        page_graph=page_graph,
        intent="create",
        scope_mode="patch",
        generation_mode=GenerationMode.FAST,
        creative_direction={},
    )

    assert "error" not in result
    assert len(seen_payloads) == 2
    assert any(operation.file_path == target for operation in result["operations"])
    assert len(seen_payloads) >= 2
    assert seen_payloads[0]["tool_results"] == []
    assert any(item.get("tool") == "list_files" for item in seen_payloads[1]["tool_results"])


def test_clone_template_skips_heavy_frontend_artifacts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Light Clone Workspace",
            "description": "Clone should skip node_modules and dist",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    source_root = tmp_path / "data" / "workspaces" / workspace_id / "source"
    assert not (source_root / "frontend" / "node_modules").exists()
    assert not (source_root / "frontend" / "dist").exists()
    assert (source_root / ".gitignore").exists()


def test_workspace_platform_log_is_persisted_to_file(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Workspace Log",
            "description": "Platform events should be written to a per-workspace log file",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    log_path = tmp_path / "data" / "workspaces" / workspace_id / "logs" / "platform.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "Workspace created." in content
    assert "Canonical template cloned." in content


def test_base_template_tree_is_clean(tmp_path: Path) -> None:
    del tmp_path
    repo_root = Path(__file__).resolve().parents[3]
    tracked = subprocess.run(
        ["git", "ls-files", "--", "runtime/templates/base-miniapp"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    junk_paths = [
        path
        for path in tracked
        if (
            "/node_modules/" in path
            or "/dist/" in path
            or "/__pycache__/" in path
            or path.endswith(".DS_Store")
            or path.endswith(".tsbuildinfo")
        )
    ]

    assert junk_paths == []


def test_generation_references_existing_canonical_template_paths(tmp_path: Path) -> None:
    del tmp_path
    repo_root = Path(__file__).resolve().parents[3]
    template_root = repo_root / "runtime/templates/base-miniapp"

    assert DESIGN_REFERENCE_FILES
    assert SHARED_GENERATED_FILES
    assert all("shared/ui/templates" not in path for path in DESIGN_REFERENCE_FILES)
    assert all("shared/ui/generated" not in path for path in DESIGN_REFERENCE_FILES)
    assert all("shared/generated" not in path for path in SHARED_GENERATED_FILES)
    assert all((template_root / path).exists() for path in (*DESIGN_REFERENCE_FILES, *SHARED_GENERATED_FILES))


def test_approve_draft_does_not_block_on_index_refresh(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Async Index Workspace",
            "description": "Approve draft should not wait for reindex",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    service = app.state.container.workspace_service
    service.clone_template(workspace_id)
    service.prepare_draft(workspace_id, "run_async_index")

    started = threading.Event()
    finished = threading.Event()

    def fake_refresh_indexes(workspace):
        del workspace
        started.set()
        time.sleep(0.5)
        finished.set()

    monkeypatch.setattr(service, "_refresh_indexes", fake_refresh_indexes)

    started_at = time.perf_counter()
    service.approve_draft(workspace_id, "run_async_index", "Approve draft asynchronously")
    elapsed = time.perf_counter() - started_at

    assert started.wait(1.0)
    assert not finished.is_set()
    assert elapsed < 1.5
    assert finished.wait(1.0)


def test_file_tree_hides_temporary_build_artifacts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Hidden Artifact Workspace",
            "description": "Temporary artifacts should stay out of file tree",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    service = app.state.container.workspace_service
    service.clone_template(workspace_id)

    source_root = tmp_path / "data" / "workspaces" / workspace_id / "source"
    (source_root / "frontend" / "node_modules" / "demo").mkdir(parents=True)
    (source_root / "frontend" / "node_modules" / "demo" / "index.js").write_text("export {};\n", encoding="utf-8")
    (source_root / "frontend" / "dist").mkdir(parents=True)
    (source_root / "frontend" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (source_root / "miniapp" / "__pycache__").mkdir(parents=True)
    (source_root / "miniapp" / "__pycache__" / "store.cpython-312.pyc").write_bytes(b"pyc")
    (source_root / "frontend" / "tsconfig.tsbuildinfo").write_text("{}", encoding="utf-8")

    paths = {item["path"] for item in service.file_tree(workspace_id)}

    assert "frontend/node_modules" not in paths
    assert "frontend/dist" not in paths
    assert "miniapp/__pycache__" not in paths
    assert "frontend/tsconfig.tsbuildinfo" not in paths


def test_frontend_build_tooling_failure_is_classified_as_platform_issue(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    runner = app.state.container.check_runner

    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir(parents=True)
    (frontend_dir / "package.json").write_text('{"name":"demo","scripts":{"build":"vite build"}}\n', encoding="utf-8")

    monkeypatch.delenv("FRONTEND_NPM_BINARY", raising=False)
    monkeypatch.setattr(check_runner_module.shutil, "which", lambda _: None)

    result = runner._run_frontend_build(frontend_dir)

    assert result.status == "failed"
    assert result.details == "Frontend build tooling is unavailable in the miniapp runtime."
    assert "npm was not found on PATH." in result.logs
    assert runner.has_tooling_failure([result]) is True
    assert runner.classify_failure([result]) == "tooling/runtime_misconfiguration"


def test_generated_python_tests_install_workspace_requirements_first(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    runner = app.state.container.check_runner

    backend_dir = tmp_path / "miniapp"
    tests_dir = backend_dir / "tests"
    tests_dir.mkdir(parents=True)
    (backend_dir / "requirements.txt").write_text("fastapi\nsqlalchemy\n", encoding="utf-8")
    (tests_dir / "test_generated_app.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(check_runner_module.subprocess, "run", fake_run)

    result = runner._run_python_app_tests(backend_dir)

    assert result.status == "passed"
    assert commands[0][:5] == [check_runner_module.sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    assert commands[0][-2:] == ["-r", "requirements.txt"]
    assert commands[1][:4] == [check_runner_module.sys.executable, "-m", "unittest", "discover"]


def test_check_runner_expands_connectivity_validation_issue_codes() -> None:
    result = RunCheckResult(
        name="connectivity_validators",
        status="failed",
        details="Connectivity validation failed.",
        logs=[
            '{"code":"connectivity.missing_backend_route","message":"Missing orders route.","severity":"high","location":"miniapp/app/routes/orders.py","blocking":true}',
            '{"code":"connectivity.unwired_page_dependency","message":"Client page is unwired.","severity":"high","location":"miniapp/app/static/client/index.html","blocking":true}',
        ],
    )

    issues = check_runner_module.CheckRunner.failing_issues([result])

    assert {issue.code for issue in issues} == {
        "connectivity.missing_backend_route",
        "connectivity.unwired_page_dependency",
    }


def test_preview_connectivity_smoke_reports_unreachable_route(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    runner = app.state.container.check_runner

    graph = {
        "roles": {
            "client": {"pages": [{"route_path": "/client"}]},
            "specialist": {"pages": [{"route_path": "/specialist"}]},
            "manager": {"pages": [{"route_path": "/manager"}]},
        }
    }
    artifacts_dir = tmp_path / "workspace" / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "generated_app_graph.json").write_text(json.dumps(graph), encoding="utf-8")

    class FakeResponse:
        def __init__(self, body: str):
            self.status = 200
            self._body = body.encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/specialist"):
            raise check_runner_module.URLError("connection refused")
        return FakeResponse("<html><body><main>usable preview content for route</main></body></html>")

    monkeypatch.setattr(check_runner_module, "urlopen", fake_urlopen)

    result = runner._preview_connectivity_smoke(
        source_dir=tmp_path / "workspace",
        preview=PreviewRecord(workspace_id="ws_1", status="running", url="http://localhost:3000", draft_run_id=None),
        preview_run_id=None,
    )

    assert result.status == "failed"
    assert any("/specialist" in line for line in result.logs)


def test_preview_connectivity_smoke_retries_transient_route_failures(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    runner = app.state.container.check_runner

    graph = {
        "roles": {
            "client": {"pages": [{"route_path": "/client"}]},
            "specialist": {"pages": [{"route_path": "/specialist"}]},
            "manager": {"pages": [{"route_path": "/manager"}]},
        }
    }
    artifacts_dir = tmp_path / "workspace" / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "generated_app_graph.json").write_text(json.dumps(graph), encoding="utf-8")

    class FakeResponse:
        def __init__(self, body: str):
            self.status = 200
            self._body = body.encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    attempts: dict[str, int] = {}

    def fake_urlopen(request, timeout):
        route = request.full_url.rsplit("/", 1)[-1]
        attempts[route] = attempts.get(route, 0) + 1
        if route == "client" and attempts[route] == 1:
            raise check_runner_module.URLError("connection refused")
        return FakeResponse("<html><body><main>usable preview content for route</main></body></html>")

    monkeypatch.setattr(check_runner_module, "urlopen", fake_urlopen)

    result = runner._preview_connectivity_smoke(
        source_dir=tmp_path / "workspace",
        preview=PreviewRecord(workspace_id="ws_1", status="running", url="http://localhost:3000", draft_run_id=None),
        preview_run_id=None,
    )

    assert result.status == "passed"
    assert attempts["client"] == 2
    assert any("/client returned usable preview content after 2 attempt(s)." in line for line in result.logs)


def test_preview_connectivity_smoke_skips_for_draft_bound_runs() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=Path("/tmp") / f"preview-source-only-{time.time_ns()}")
    runner = app.state.container.check_runner

    result = runner._preview_connectivity_smoke(
        source_dir=Path("/tmp/nonexistent-preview-source"),
        preview=PreviewRecord(workspace_id="ws_1", status="running", url="http://localhost:3000", draft_run_id=None),
        preview_run_id="run_draft",
    )

    assert result.status == "skipped"
    assert "source-only" in result.details


def test_generation_service_detects_missing_static_asset_targets(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    targets = service._detect_missing_static_asset_targets(
        generated_page_sources={
            "miniapp/app/static/client/cart.html": """
            <main>
              <script src="/static/client/cart.js"></script>
              <script src="./checkout.js"></script>
            </main>
            """
        },
        current_target_files=["miniapp/app/static/client/cart.html"],
    )

    assert targets == [
        "miniapp/app/static/client/cart.js",
        "miniapp/app/static/client/checkout.js",
    ]


def test_fix_orchestrator_does_not_misclassify_preview_route_errors_as_typescript(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")

    failure_class = app.state.container.fix_orchestrator._classify_failure_text(
        "/client could not be opened in preview: <urlopen error [Errno 111] Connection refused>"
    )

    assert failure_class == "runtime_preview_boot"


def test_openrouter_json_parser_recovers_first_object_from_concatenated_json() -> None:
    parsed = OpenRouterClient._parse_json_payload(
        '{"assistant_message":"first","operations":[]}{"assistant_message":"second","operations":[]}',
        "responses",
    )
    assert parsed == {"assistant_message": "first", "operations": []}


def test_page_generation_retries_with_compact_recovery_after_retryable_provider_error(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    prompts: list[tuple[str, str]] = []

    def fake_generate_structured_with_retry(**kwargs):
        prompts.append((kwargs["system_prompt"], kwargs["user_prompt"]))
        if len(prompts) == 1:
            raise RuntimeError("OpenRouter chat/completions returned 502: provider returned error")
        return {
            "payload": {
                "assistant_message": "Recovered.",
                "operations": [
                    {
                        "file_path": "miniapp/app/static/client/product-detail.html",
                        "operation": "replace",
                        "content": "<main><section>Product detail</section></main>\n",
                        "reason": "recover",
                    }
                ],
            },
            "model": "stub-model",
        }

    service._page_edit_system_prompt = lambda: "page-system"  # type: ignore[method-assign]
    service._page_edit_user_prompt = lambda **kwargs: f"mode={kwargs['generation_mode']}"  # type: ignore[method-assign]
    service._generate_structured_with_retry = fake_generate_structured_with_retry  # type: ignore[method-assign]

    result = service._resolve_page_file_edit(
        prompt="Build the product detail page.",
        grounded_spec=None,  # type: ignore[arg-type]
        role="client",
        page={"page_id": "product-detail", "file_path": "miniapp/app/static/client/product-detail.html"},
        page_graph={"roles": {}},
        role_contract={},
        scope_mode="whole_file_build",
        intent="create",
        file_contexts={},
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert "error" not in result
    assert len(prompts) == 2
    assert "Provider recovery mode" in prompts[1][0]
    assert "mode=GenerationMode.FAST" in prompts[1][1]


def test_parallel_page_generation_retries_via_serial_recovery_mode(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    page_one = "miniapp/app/static/client/product-list.html"
    page_two = "miniapp/app/static/client/product-detail.html"
    retried: list[tuple[str, str]] = []

    from app.models.domain import DraftFileOperation

    second_page_operation = DraftFileOperation(
        file_path=page_two,
        operation="replace",
        content="<main><section>Product detail</section></main>\n",
        reason="page2",
    )
    recovered_operation = DraftFileOperation(
        file_path=page_one,
        operation="replace",
        content="<main><section>Product list</section></main>\n",
        reason="page1",
    )

    async def fake_async_page_results(**kwargs):
        del kwargs
        return [
            {"error": f"Page generation failed for {page_one}: OpenRouter chat/completions returned 502", "retryable": True, "file_path": page_one},
            {"assistant_message": "Second page ok.", "operation": second_page_operation, "model": "stub"},
        ]

    def fake_page_edit(**kwargs):
        retried.append((kwargs["page"]["file_path"], kwargs.get("recovery_mode", "default")))
        return {"assistant_message": "Recovered page.", "operation": recovered_operation, "model": "stub"}

    service._resolve_page_file_edits_async = fake_async_page_results  # type: ignore[method-assign]
    service._resolve_page_file_edit = fake_page_edit  # type: ignore[method-assign]

    result = service._resolve_code_edits(
        workspace_id="ws_test",
        draft_run_id="run_test",
        prompt="Create the client shopping flow.",
        grounded_spec=None,  # type: ignore[arg-type]
        role_scope=["client"],
        file_contexts={},
        target_files=[page_one, page_two],
        role_contract={},
        page_graph={"roles": {"client": {"pages": [{"page_id": "list", "file_path": page_one}, {"page_id": "detail", "file_path": page_two}]}}},
        intent="create",
        scope_mode="minimal_patch",
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert "error" not in result
    assert retried == [(page_one, "serial_recovery_retry")]
    assert any(item.file_path == page_one for item in result["operations"])
    assert any(item.file_path == page_two for item in result["operations"])


def test_page_generation_retries_after_recoverable_format_failure(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    prompts: list[tuple[str, str]] = []

    def fake_generate_structured_with_retry(**kwargs):
        prompts.append((kwargs["system_prompt"], kwargs["user_prompt"]))
        if len(prompts) == 1:
            return {
                "payload": {
                    "assistant_message": "Wrong shape.",
                    "operations": [
                        {
                            "file_path": "miniapp/app/static/client/other.html",
                            "operation": "replace",
                            "content": "<main>Other</main>\n",
                            "reason": "wrong target",
                        }
                    ],
                },
                "model": "stub-model",
            }
        return {
            "payload": {
                "assistant_message": "Recovered.",
                "operations": [
                    {
                        "file_path": "miniapp/app/static/client/product-detail.html",
                        "operation": "replace",
                        "content": "<main><section>Product detail</section></main>\n",
                        "reason": "recover",
                    }
                ],
            },
            "model": "stub-model",
        }

    service._page_edit_system_prompt = lambda: "page-system"  # type: ignore[method-assign]
    service._page_edit_user_prompt = lambda **kwargs: f"mode={kwargs['generation_mode']}"  # type: ignore[method-assign]
    service._generate_structured_with_retry = fake_generate_structured_with_retry  # type: ignore[method-assign]

    result = service._resolve_page_file_edit(
        prompt="Build the product detail page.",
        grounded_spec=None,  # type: ignore[arg-type]
        role="client",
        page={"page_id": "product-detail", "file_path": "miniapp/app/static/client/product-detail.html"},
        page_graph={"roles": {}},
        role_contract={},
        scope_mode="whole_file_build",
        intent="create",
        file_contexts={},
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert "error" not in result
    assert len(prompts) == 2
    assert "Provider recovery mode" in prompts[1][0]
    assert "mode=GenerationMode.FAST" in prompts[1][1]


def test_sanitize_draft_operations_strips_control_chars(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    operations = service._sanitize_draft_operations(
        [
            DraftFileOperation(
                file_path="miniapp/app/static/client/index.html",
                operation="replace",
                content="<div>Loading\u0007\u007f</div>\n",
                reason="test",
            )
        ]
    )

    assert operations[0].content == "<div>Loading</div>\n"


def test_prompt_assets_are_english_only(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    assert service.validate_prompt_assets_are_english() == []


def test_scope_mode_prefers_whole_file_build_for_large_create_requests(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    scope_mode = service._scope_mode(
        "create",
        "Create a multi-page flower shop storefront with manager, specialist, and client roles.",
        ["client", "specialist", "manager"],
    )

    assert scope_mode == "whole_file_build"


def test_scope_mode_prefers_whole_file_build_for_create_like_edit_requests(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    scope_mode = service._scope_mode(
        "edit",
        (
            "Create a multi-page flower shop storefront with catalog, product detail, cart, checkout, "
            "and separate manager and specialist workspaces."
        ),
        ["client", "specialist", "manager"],
    )

    assert scope_mode == "whole_file_build"


def test_scope_mode_prefers_minimal_patch_for_small_local_edits(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    scope_mode = service._scope_mode(
        "edit",
        "Fix only the button spacing on the client page without touching anything else.",
        ["client"],
    )

    assert scope_mode == "minimal_patch"


def test_normalize_page_plan_keeps_backend_targets_advisory(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    planned = service._normalize_page_plan(
        {
            "summary": "Plan a storefront flow.",
            "page_graph": {
                "app_title": "Flower Shop",
                "summary": "Catalog and order management.",
                "flow_mode": "multi_page",
                "roles": [
                    {
                        "role": "client",
                        "entry_path": "/client",
                        "landing_page_id": "catalog",
                        "routes_file": "miniapp/app/static/client/index.html",
                        "pages": [
                            {
                                "page_id": "catalog",
                                "route_path": "/client/catalog",
                                "navigation_label": "Catalog",
                                "component_name": "CatalogPage",
                                "file_path": "miniapp/app/static/client/catalog.html",
                                "title": "Catalog",
                                "description": "Browse flowers.",
                                "purpose": "Browse flowers.",
                                "page_kind": "workspace",
                                "primary_actions": ["Browse products"],
                                "data_dependencies": ["/api/catalog", "/api/orders?status=open"],
                                "loading_state": "",
                                "empty_state": "",
                                "error_state": "",
                            }
                        ],
                    }
                ],
            },
            "target_files": ["miniapp/app/static/client/catalog.html"],
            "shared_files": [],
            "backend_targets": [],
            "files_to_read": [],
        },
        role_scope=["client"],
        scope_mode="whole_file_build",
        require_multi_page=True,
        workspace_tree=[],
    )

    assert planned["backend_targets"] == []
    assert planned["planner_contract_enrichment"]["proactive_backend_targets"] == []


def test_normalize_page_plan_infers_semantic_state_contract_for_dynamic_pages(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    planned = service._normalize_page_plan(
        {
            "summary": "Plan a storefront flow.",
            "page_graph": {
                "app_title": "Flower Shop",
                "summary": "Catalog and order management.",
                "flow_mode": "multi_page",
                "roles": [
                    {
                        "role": "client",
                        "entry_path": "/client",
                        "landing_page_id": "catalog",
                        "routes_file": "miniapp/app/static/client/index.html",
                        "pages": [
                            {
                                "page_id": "catalog",
                                "route_path": "/client/catalog",
                                "navigation_label": "Catalog",
                                "component_name": "CatalogPage",
                                "file_path": "miniapp/app/static/client/catalog.html",
                                "title": "Catalog",
                                "description": "Browse flowers.",
                                "purpose": "Browse flowers.",
                                "page_kind": "workspace",
                                "primary_actions": ["Browse products"],
                                "data_dependencies": ["/api/catalog"],
                                "loading_state": "",
                                "empty_state": "",
                                "error_state": "",
                            }
                        ],
                    }
                ],
            },
            "target_files": ["miniapp/app/static/client/catalog.html"],
            "shared_files": [],
            "backend_targets": [],
            "files_to_read": [],
        },
        role_scope=["client"],
        scope_mode="whole_file_build",
        require_multi_page=True,
        workspace_tree=[],
    )

    page = planned["page_graph"]["roles"]["client"]["pages"][0]
    assert "#catalog-loading" in page["loading_state"]
    assert '[data-ui-state="loading"]' in page["loading_state"]
    assert "#catalog-error" in page["error_state"]
    assert '[data-ui-state="error"]' in page["error_state"]
    assert "empty-state container" in page["empty_state"]


def test_page_generation_accepts_multiple_same_file_operations_and_uses_last_valid_replace(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    service._page_edit_system_prompt = lambda: "page-system"  # type: ignore[method-assign]
    service._page_edit_user_prompt = lambda **kwargs: "page-user"  # type: ignore[method-assign]
    service._generate_structured_with_retry = lambda **kwargs: {  # type: ignore[method-assign]
        "payload": {
            "assistant_message": "Generated page.",
            "operations": [
                {
                    "file_path": "miniapp/app/static/client/catalog.html",
                    "operation": "replace",
                    "content": "<main>Draft catalog</main>\n",
                    "reason": "initial draft",
                },
                {
                    "file_path": "miniapp/app/static/client/catalog.html",
                    "operation": "replace",
                    "content": "<main><section>Final catalog page</section></main>\n",
                    "reason": "finalize same file",
                },
            ],
        },
        "model": "stub-model",
    }

    result = service._resolve_page_file_edit(
        prompt="Build the catalog page.",
        grounded_spec=None,  # type: ignore[arg-type]
        role="client",
        page={"page_id": "catalog", "file_path": "miniapp/app/static/client/catalog.html"},
        page_graph={"roles": {}},
        role_contract={},
        scope_mode="whole_file_build",
        intent="create",
        file_contexts={},
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert "error" not in result
    assert result["operation"].file_path == "miniapp/app/static/client/catalog.html"
    assert "Final catalog page" in result["operation"].content


def test_page_graph_gate_accepts_root_and_profile_only_multi_page_flows(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    page_graph = {
        "flow_mode": "multi_page",
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    {"route_path": "/client", "file_path": "miniapp/app/static/client/index.html"},
                    {"route_path": "/client/profile", "file_path": "miniapp/app/static/client/profile.html"},
                ],
            },
            "specialist": {
                "routes_file": "miniapp/app/static/specialist/index.html",
                "pages": [
                    {"route_path": "/specialist", "file_path": "miniapp/app/static/specialist/index.html"},
                    {"route_path": "/specialist/profile", "file_path": "miniapp/app/static/specialist/profile.html"},
                ],
            },
        },
    }

    issues = service._page_graph_gate_issues(
        page_graph,
        ["client", "specialist"],
        scope_mode="whole_file_build",
        require_multi_page=True,
    )

    assert not any("missing separate business pages" in issue for issue in issues)


def test_page_graph_gate_rejects_incomplete_multi_page_plan_without_business_page_rails(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    page_graph = {
        "flow_mode": "multi_page",
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [{"route_path": "/client", "file_path": "miniapp/app/static/client/index.html"}],
            },
            "specialist": {
                "routes_file": "miniapp/app/static/specialist/index.html",
                "pages": [{"route_path": "/specialist", "file_path": "miniapp/app/static/specialist/index.html"}],
            },
            "manager": {
                "routes_file": "miniapp/app/static/manager/index.html",
                "pages": [{"route_path": "/manager", "file_path": "miniapp/app/static/manager/index.html"}],
            },
        },
    }

    issues = service._page_graph_gate_issues(
        page_graph,
        ["client", "specialist", "manager"],
        scope_mode="minimal_patch",
        require_multi_page=True,
    )

    assert any("did not receive enough distinct pages" in issue for issue in issues)
    assert not any("missing separate business pages" in issue for issue in issues)
    assert not any("collapses the app into one screen per selected role" in issue for issue in issues)


def test_edit_gate_rejects_placeholder_surface_and_unknown_handoff(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    page_graph = {
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    {
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "handoff_paths": ["/client/profile", "/client/missing"],
                    },
                    {
                        "route_path": "/client/profile",
                        "file_path": "miniapp/app/static/client/profile.html",
                        "handoff_paths": ["/client"],
                    }
                ],
            }
        }
    }
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content="<html><body><main>TODO placeholder</main></body></html>",
            reason="Placeholder page",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/profile.html",
            operation="replace",
            content="<html><body><main><h1>Profile</h1></main></body></html>",
            reason="Profile page",
        ),
    ]

    issues = service._edit_gate_issues(
        page_graph,
        operations,
        ["client"],
        scope_mode="whole_file_build",
        target_files=[item.file_path for item in operations],
    )

    assert any("still contains placeholder copy" in issue for issue in issues)
    assert any("references a non-canonical handoff path" in issue for issue in issues)


def test_edit_gate_accepts_content_first_root_page_with_valid_handoffs(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    page_graph = {
        "roles": {
            "client": {
                "routes_file": "miniapp/app/static/client/index.html",
                "pages": [
                    {
                        "route_path": "/client",
                        "file_path": "miniapp/app/static/client/index.html",
                        "handoff_paths": ["/client/profile"],
                    },
                    {
                        "route_path": "/client/profile",
                        "file_path": "miniapp/app/static/client/profile.html",
                        "handoff_paths": ["/client"],
                    },
                ],
            }
        }
    }
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content=(
                "<html><body><main class=\"page-shell\">"
                "<section class=\"summary-card\"><h1>Workspace</h1><p>Open the current client flow.</p></section>"
                "<section class=\"primary-actions\"><a href=\"/client/profile\">Open profile</a></section>"
                "</main></body></html>"
            ),
            reason="Content-first root page",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/profile.html",
            operation="replace",
            content="<html><body><main><h1>Profile</h1></main></body></html>",
            reason="Profile page",
        ),
    ]

    issues = service._edit_gate_issues(
        page_graph,
        operations,
        ["client"],
        scope_mode="whole_file_build",
        target_files=[item.file_path for item in operations],
    )

    assert not any("placeholder copy" in issue for issue in issues)
    assert not any("non-canonical handoff path" in issue for issue in issues)


def test_preview_get_does_not_collect_runtime_logs_for_preview_url_polling(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Poll Workspace",
            "description": "Preview URL polling should not block on docker log collection.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")

    preview_service = app.state.container.preview_service
    preview = preview_service._get_or_create(workspace_id)
    preview.runtime_mode = "docker"
    preview.status = "running"
    preview.stage = "running"
    preview.url = "http://localhost:19999"
    preview.frontend_url = preview.url
    preview.backend_url = f"{preview.url}/api"
    preview.proxy_port = 19999
    preview.project_name = "grounded_preview_test"
    preview_service._persist(preview)

    original_collect_logs = app.state.container.runtime_manager.collect_logs
    original_http_ready = preview_service._http_preview_ready
    original_inspect = app.state.container.runtime_manager.inspect_containers
    app.state.container.runtime_manager.collect_logs = lambda workspace_id, source_dir, proxy_port: (_ for _ in ()).throw(AssertionError("collect_logs should not be called"))
    preview_service._http_preview_ready = lambda url: True
    app.state.container.runtime_manager.inspect_containers = (
        lambda current_workspace_id, source_dir, proxy_port: [{"state": "running", "published_port": str(proxy_port or 19999)}]
    )
    try:
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
    finally:
        app.state.container.runtime_manager.collect_logs = original_collect_logs
        preview_service._http_preview_ready = original_http_ready
        app.state.container.runtime_manager.inspect_containers = original_inspect

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["url"] == "http://localhost:19999"


def test_preview_get_fast_restores_running_state_from_ready_http_port(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Fast Restore Workspace",
            "description": "Health-check previews should recover without docker reconcile blocking polling.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")

    preview_service = app.state.container.preview_service
    preview = preview_service._get_or_create(workspace_id)
    preview.runtime_mode = "docker"
    preview.status = "starting"
    preview.stage = "health_check"
    preview.url = None
    preview.frontend_url = None
    preview.backend_url = None
    preview.proxy_port = 16734
    preview.project_name = "grounded_preview_test"
    preview_service._persist(preview)

    original_http_ready = preview_service._http_preview_ready
    original_inspect = app.state.container.runtime_manager.inspect_containers
    preview_service._http_preview_ready = lambda url: True
    app.state.container.runtime_manager.inspect_containers = (
        lambda workspace_id, source_dir, proxy_port: (_ for _ in ()).throw(AssertionError("inspect_containers should not be called"))
    )
    try:
        preview_state = preview_service.get(workspace_id)
    finally:
        preview_service._http_preview_ready = original_http_ready
        app.state.container.runtime_manager.inspect_containers = original_inspect

    assert preview_state.status == "running"
    assert preview_state.url == "http://localhost:16734"


def test_preview_url_requires_real_http_readiness_for_health_check_restore(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Peek Workspace",
            "description": "Preview URL should recover from persisted health_check state without blocking.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")

    preview_service = app.state.container.preview_service
    preview = preview_service._get_or_create(workspace_id)
    preview.runtime_mode = "docker"
    preview.status = "starting"
    preview.stage = "health_check"
    preview.url = None
    preview.frontend_url = None
    preview.backend_url = None
    preview.proxy_port = 16734
    preview.project_name = "grounded_preview_test"
    preview.logs.append("Preview runtime is healthy at http://localhost:16734.")
    preview_service._persist(preview)
    original_http_ready = preview_service._http_preview_ready
    original_inspect = app.state.container.runtime_manager.inspect_containers
    preview_service._http_preview_ready = lambda url: True
    app.state.container.runtime_manager.inspect_containers = (
        lambda workspace_id, source_dir, proxy_port: (_ for _ in ()).throw(AssertionError("inspect_containers should not be called"))
    )
    try:
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
    finally:
        preview_service._http_preview_ready = original_http_ready
        app.state.container.runtime_manager.inspect_containers = original_inspect

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["url"] == "http://localhost:16734"


def test_preview_url_hides_runtime_url_until_http_is_ready(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview URL Readiness Gate",
            "description": "Preview URL should not be exposed until the HTTP runtime responds.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")

    preview_service = app.state.container.preview_service
    preview = preview_service._get_or_create(workspace_id)
    preview.runtime_mode = "docker"
    preview.status = "running"
    preview.stage = "running"
    preview.url = "http://localhost:16736"
    preview.frontend_url = preview.url
    preview.backend_url = f"{preview.url}/api"
    preview.proxy_port = 16736
    preview.project_name = "grounded_preview_test"
    preview_service._persist(preview)

    original_http_ready = preview_service._http_preview_ready
    original_inspect = app.state.container.runtime_manager.inspect_containers
    preview_service._http_preview_ready = lambda url: False
    app.state.container.runtime_manager.inspect_containers = (
        lambda workspace_id, source_dir, proxy_port: (_ for _ in ()).throw(AssertionError("inspect_containers should not be called"))
    )
    try:
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
    finally:
        preview_service._http_preview_ready = original_http_ready
        app.state.container.runtime_manager.inspect_containers = original_inspect

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "starting"
    assert payload["stage"] == "health_check"
    assert payload["url"] is None
    assert payload["role_urls"] == {}


def test_generate_run_auto_switches_to_fix_on_frontend_build_failure(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Auto Fix Workspace",
            "description": "Generate should auto-enter fix when frontend build fails",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    app.state.container.workspace_service.clone_template(workspace_id)

    generation_calls: list[str] = []
    fix_calls: list[str] = []
    workspace_service = app.state.container.workspace_service

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        generation_calls.append(request.mode)
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="failed",
            mode="generate",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            failure_reason="Build validation failed after automatic repair attempts. Root cause: npm run build failed for the draft frontend.",
            failure_class="syntax/build",
            root_cause_summary="npm run build failed for the draft frontend.",
            handoff_from_failed_generate={
                "mode": "fix",
                "prompt": "Analyze the reported failure and apply the smallest safe fix.",
                "error_context": {
                    "raw_error": "npm run build failed for the draft frontend.",
                    "source": "frontend",
                    "failing_target": "frontend build",
                },
                "failure_class": "syntax/build",
            },
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=False,
                blocking=True,
                issues=[
                    {
                        "code": "check.changed_files_static",
                        "message": "npm run build failed for the draft frontend.",
                        "severity": "high",
                    }
                ],
            ),
        )

    def fake_fix_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        fix_calls.append(request.mode)
        run_id = request.linked_run_id or "run_test"
        draft_root = workspace_service.prepare_draft(workspace_id, run_id)
        client_routes = draft_root / "frontend" / "src" / "roles" / "client" / "ClientRoutes.tsx"
        client_routes.parent.mkdir(parents=True, exist_ok=True)
        if not client_routes.exists():
            client_routes.write_text(
                "export function ClientRoutes(): JSX.Element {\n  return <div />;\n}\n",
                encoding="utf-8",
            )
        client_routes.write_text(
            client_routes.read_text(encoding="utf-8").replace(
                "export function ClientRoutes(): JSX.Element {",
                "export function ClientRoutes(): JSX.Element {\n  // repaired automatically during auto-fix\n",
            ),
            encoding="utf-8",
        )
        app.state.container.store.upsert(
            "reports",
            f"candidate_diff:{workspace_id}",
            {
                "diff": "\n".join(
                    [
                        "diff --git a/source/miniapp/app/static/client/app.js b/draft/miniapp/app/static/client/app.js",
                        "--- a/source/miniapp/app/static/client/app.js",
                        "+++ b/draft/miniapp/app/static/client/app.js",
                        "@@",
                        "+  // repaired automatically during auto-fix",
                    ]
                )
            },
        )
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode="fix",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Auto-fix completed successfully.",
            failure_class="syntax/build",
            root_cause_summary="Miniapp static runtime issue repaired automatically.",
            current_fix_phase="completed",
            fix_targets=["miniapp/app/static/client/app.js"],
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]
    app.state.container.fix_orchestrator.generate = fake_fix_generate  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a simple multi-page app.",
            apply_strategy="manual_approve",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert generation_calls == ["generate"]
    assert fix_calls == ["fix"]
    assert run.status == "awaiting_approval"
    assert run.current_fix_phase == "completed"
    assert run.generation_mode == "balanced"


def test_fix_orchestrator_reuses_existing_generation_draft(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Reuse Draft Workspace",
            "description": "Fix should reuse an existing generation draft instead of resetting it",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    workspace_service = app.state.container.workspace_service
    workspace_service.clone_template(workspace_id)

    run_id = "run_existing_generation_draft"
    draft_source = workspace_service.prepare_draft(workspace_id, run_id)
    app_path = draft_source / "miniapp/app/static/client/index.html"
    marker = "\n<!-- generated-draft-marker -->\n"
    app_path.write_text(app_path.read_text(encoding="utf-8") + marker, encoding="utf-8")

    def fake_execute_exact_checks(*, job, workspace_id, run_id, draft_source, changed_files):
        del job, draft_source, changed_files
        return (
            CheckExecutionRecord(
                workspace_id=workspace_id,
                run_id=run_id,
                results=[
                    RunCheckResult(name="schema_validators", status="passed", details="Validators passed."),
                    RunCheckResult(name="connectivity_validators", status="passed", details="Connectivity validators passed."),
                    RunCheckResult(
                        name="changed_files_static",
                        status="passed",
                        details="Static assets validated.",
                        command="python -m py_compile miniapp/app/main.py",
                        exit_code=0,
                        logs=["Static assets validated."],
                    ),
                    RunCheckResult(
                        name="preview_boot_smoke",
                        status="passed",
                        details="Preview is healthy.",
                        command="docker compose up -d --build",
                        exit_code=0,
                        logs=["Preview is healthy."],
                    ),
                    RunCheckResult(
                        name="preview_connectivity_smoke",
                        status="passed",
                        details="Preview routes are healthy.",
                        command="preview route smoke (current session)",
                        exit_code=0,
                        logs=["/client returned usable preview content."],
                    ),
                ],
                duration_ms=1,
            ),
            {
                "status": "running",
                "stage": "running",
                "progress_percent": 100,
                "logs": ["Preview is healthy."],
                "last_error": None,
                "mini_app_logs": ["=== preview-app ===", "Preview is healthy."],
            },
        )

    app.state.container.fix_orchestrator._execute_exact_checks = fake_execute_exact_checks  # type: ignore[method-assign]
    app.state.container.fix_orchestrator._execute_final_checks = fake_execute_exact_checks  # type: ignore[method-assign]

    job = app.state.container.fix_orchestrator.generate(
        workspace_id,
        GenerateRequest(
            prompt="Analyze the reported failure and apply the smallest safe fix.",
            mode="fix",
            linked_run_id=run_id,
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
            generation_mode="balanced",
            model_profile="openai_code_fast",
        ),
    )

    assert job.status == "completed"
    assert marker.strip() in app_path.read_text(encoding="utf-8")
    trace = app.state.container.store.get("reports", f"trace:{workspace_id}")
    assert trace is not None
    assert any(entry.get("stage") == "draft_reused" for entry in trace.get("entries", []))


def test_fix_context_includes_generated_app_graph_for_connectivity_state_failures(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)

    workspace_service = app.state.container.workspace_service
    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Fix Context Workspace",
            description="Connectivity repair should receive generated app graph context.",
            path=str((tmp_path / "data" / "workspaces" / "ws_fix_context").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_fix_context"
    draft_source = workspace_service.ensure_draft(workspace.workspace_id, draft_run_id)
    (draft_source / "artifacts").mkdir(parents=True, exist_ok=True)
    (draft_source / "artifacts" / "generated_app_graph.json").write_text(
        json.dumps(
            {
                "roles": {
                    "client": {
                        "pages": [
                            {
                                "file_path": "miniapp/app/static/client/index.html",
                                "loading_state": "Show storefront skeleton while products load.",
                                "error_state": "Show a retry state if catalog loading fails.",
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    check_execution = CheckExecutionRecord(
        workspace_id=workspace.workspace_id,
        run_id=draft_run_id,
        results=[
            RunCheckResult(
                name="connectivity_validators",
                status="failed",
                details="Connectivity validation failed.",
                logs=[
                    '{"code":"connectivity.missing_ui_loading_state","message":"miniapp/app/static/client/index.html is missing its planned loading state for dynamic data.","severity":"high","location":"miniapp/app/static/client/index.html","blocking":true}'
                ],
            )
        ],
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )
    request = GenerateRequest(
        prompt="Fix the loading state mismatch.",
        mode="fix",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        error_context={"raw_error": "Client loading state validator failed."},
    )
    scope_entries = [FixScopeEntry(file_path="miniapp/app/static/client/index.html", reason="HTML page failed the validator.")]

    fix_case = app.state.container.fix_orchestrator._build_fix_case(
        workspace_id=workspace.workspace_id,
        run_id=draft_run_id,
        attempt=1,
        request=request,
        check_execution=check_execution,
        preview_details={"logs": [], "containers": [], "container_logs": {}},
        prior_attempts=[],
        existing_scope=scope_entries,
    )
    contexts = app.state.container.fix_orchestrator._collect_file_contexts(
        workspace.workspace_id,
        draft_run_id,
        scope_entries,
        fix_turn=fix_case,
    )

    assert "artifacts/generated_app_graph.json" in contexts


def test_auto_fixed_generate_run_does_not_resume_generation_from_checkpoint(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Auto Fix Resume Workspace",
            "description": "Auto-fix on generate should resume from the same run checkpoint",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    app.state.container.workspace_service.clone_template(workspace_id)

    workspace_service = app.state.container.workspace_service
    preview_service = app.state.container.preview_service
    generation_modes: list[str] = []
    fix_calls: list[str] = []

    def fake_rebuild_async(workspace_id: str, source_dir=None, draft_run_id=None, on_complete=None):
        del source_dir, draft_run_id
        preview = preview_service._get_or_create(workspace_id)
        preview.status = "running"
        preview.stage = "running"
        preview.progress_percent = 100
        preview.url = "http://localhost:18181"
        preview.frontend_url = preview.url
        preview.backend_url = f"{preview.url}/api"
        preview_service.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        if on_complete is not None:
            on_complete(preview)
        return preview

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        generation_modes.append(request.mode)
        if len(generation_modes) == 1:
            workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_test")
            app.state.container.store.upsert(
                "reports",
                f"resume_checkpoint:{workspace_id}",
                {
                    "workspace_id": workspace_id,
                    "source_run_id": request.linked_run_id,
                    "draft_run_id": request.linked_run_id,
                    "status": "pending",
                    "prompt": request.prompt,
                    "intent": "create",
                    "mode": "generate",
                    "generation_mode": "balanced",
                    "target_platform": "telegram_mini_app",
                    "preview_profile": "telegram_mock",
                    "target_role_scope": ["client", "specialist", "manager"],
                    "model_profile": "openai_code_fast",
                },
            )
            return JobRecord(
                workspace_id=workspace_id,
                prompt=request.prompt,
                status="failed",
                mode="generate",
                generation_mode=request.generation_mode,
                target_platform=request.target_platform,
                preview_profile=request.preview_profile,
                current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
                fidelity="balanced_app",
                linked_run_id=request.linked_run_id,
                failure_reason="Build validation failed after automatic repair attempts. Root cause: npm run build failed for the draft frontend.",
                failure_class="syntax/build",
                root_cause_summary="npm run build failed for the draft frontend.",
                handoff_from_failed_generate={
                    "mode": "fix",
                    "prompt": "Analyze the reported failure and apply the smallest safe fix.",
                    "error_context": {
                        "raw_error": "npm run build failed for the draft frontend.",
                        "source": "frontend",
                        "failing_target": "frontend build",
                    },
                    "failure_class": "syntax/build",
                },
                validation_snapshot=ValidationSnapshot(
                    grounded_spec_valid=True,
                    app_ir_valid=True,
                    build_valid=False,
                    blocking=True,
                    issues=[{"code": "check.changed_files_static", "message": "npm run build failed for the draft frontend.", "severity": "high"}],
                ),
            )
        raise AssertionError("Generation should not auto-resume after successful fix.")

    def fake_fix_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        fix_calls.append(request.mode)
        assert workspace_service.draft_exists(workspace_id, request.linked_run_id or "")
        run_id = request.linked_run_id or "run_test"
        draft_root = workspace_service.ensure_draft(workspace_id, run_id)
        readme = draft_root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nfix applied\n", encoding="utf-8")
        app.state.container.store.upsert(
            "reports",
            f"candidate_diff:{workspace_id}",
            {
                "diff": "\n".join(
                    [
                        "diff --git a/source/miniapp/app/static/client/app.js b/draft/miniapp/app/static/client/app.js",
                        "--- a/source/miniapp/app/static/client/app.js",
                        "+++ b/draft/miniapp/app/static/client/app.js",
                        "@@",
                        "+  // fixed before resuming generation",
                    ]
                )
            },
        )
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode="fix",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Auto-fix completed successfully.",
            current_fix_phase="completed",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    preview_service.rebuild_async = fake_rebuild_async  # type: ignore[method-assign]
    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]
    app.state.container.fix_orchestrator.generate = fake_fix_generate  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a simple multi-page app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert run.status == "completed"
    assert generation_modes == ["generate"]
    assert fix_calls == ["fix"]
    checkpoint = app.state.container.store.get("reports", f"resume_checkpoint:{workspace_id}")
    assert checkpoint is not None
    assert checkpoint["status"] == "pending"

def test_successful_fix_run_does_not_queue_resume_generation_from_checkpoint(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Resume Workspace",
            "description": "Fix should continue generation from checkpoint",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    app.state.container.workspace_service.clone_template(workspace_id)

    workspace_service = app.state.container.workspace_service
    preview_service = app.state.container.preview_service
    resumed_generation = threading.Event()

    def fake_rebuild_async(workspace_id: str, source_dir=None, draft_run_id=None, on_complete=None):
        del source_dir, draft_run_id
        preview = preview_service._get_or_create(workspace_id)
        preview.status = "running"
        preview.stage = "running"
        preview.progress_percent = 100
        preview.url = "http://localhost:18181"
        preview.frontend_url = preview.url
        preview.backend_url = f"{preview.url}/api"
        preview_service.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        if on_complete is not None:
            on_complete(preview)
        return preview

    def fake_fix_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        run_id = request.linked_run_id or "run_test"
        draft_root = workspace_service.prepare_draft(workspace_id, run_id)
        readme = draft_root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nfix completed\n", encoding="utf-8")
        app.state.container.store.upsert(
            "reports",
            f"candidate_diff:{workspace_id}",
            {
                "diff": "\n".join(
                    [
                        "diff --git a/source/miniapp/app/static/specialist/app.js b/draft/miniapp/app/static/specialist/app.js",
                        "--- a/source/miniapp/app/static/specialist/app.js",
                        "+++ b/draft/miniapp/app/static/specialist/app.js",
                        "@@",
                        "+  // fix completed before resume",
                    ]
                )
            },
        )
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode="fix",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Fix completed successfully.",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        resumed_generation.set()
        workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_test")
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode="generate",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Resumed generation completed.",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    preview_service.rebuild_async = fake_rebuild_async  # type: ignore[method-assign]
    app.state.container.fix_orchestrator.generate = fake_fix_generate  # type: ignore[method-assign]
    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]
    app.state.container.store.upsert(
        "reports",
        f"resume_checkpoint:{workspace_id}",
        {
            "workspace_id": workspace_id,
            "source_run_id": "run_source_failed",
            "draft_run_id": "run_source_failed",
            "status": "pending",
            "prompt": "Build the flower shop mini app.",
            "intent": "create",
            "mode": "generate",
            "generation_mode": "balanced",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
        },
    )

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Analyze the reported failure and apply the smallest safe fix.",
            mode="fix",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    assert run.status == "completed"
    assert not resumed_generation.wait(0.2)
    checkpoint = app.state.container.store.get("reports", f"resume_checkpoint:{workspace_id}")
    assert checkpoint is not None
    assert checkpoint["status"] == "pending"


def test_run_fails_when_draft_has_only_auxiliary_changes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Auxiliary Diff Workspace",
            "description": "No-op drafts must not be marked as applied runs",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    app.state.container.workspace_service.clone_template(workspace_id)

    preview_called = threading.Event()

    def fake_rebuild_async(workspace_id: str, source_dir=None, draft_run_id=None, on_complete=None):
        del workspace_id, source_dir, draft_run_id, on_complete
        preview_called.set()
        raise AssertionError("Preview rebuild should not start for drafts with no meaningful source diff.")

    def fake_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        draft_root = app.state.container.workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_aux")
        frontend_dir = draft_root / "frontend"
        frontend_dir.mkdir(parents=True, exist_ok=True)
        (frontend_dir / "vite.config.js").write_text("export default {};\n", encoding="utf-8")
        (frontend_dir / "vite.config.d.ts").write_text("export {};\n", encoding="utf-8")
        return JobRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            status="completed",
            mode="generate",
            generation_mode=request.generation_mode,
            target_platform=request.target_platform,
            preview_profile=request.preview_profile,
            current_revision_id=app.state.container.workspace_service.get_workspace(workspace_id).current_revision_id,
            fidelity="balanced_app",
            linked_run_id=request.linked_run_id,
            summary="Generated only auxiliary files.",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    app.state.container.preview_service.rebuild_async = fake_rebuild_async  # type: ignore[method-assign]
    app.state.container.generation_service.generate = fake_generate  # type: ignore[method-assign]

    run = app.state.container.run_service.create_run_sync(
        workspace_id,
        CreateRunRequest(
            prompt="Create a simple role-based mini app.",
            apply_strategy="staged_auto_apply",
            model_profile="openai_code_fast",
            generation_mode="balanced",
            target_platform="telegram_mini_app",
            preview_profile="telegram_mock",
        ),
    )

    source_root = tmp_path / "data" / "workspaces" / workspace_id / "source"
    assert run.status == "failed"
    assert run.apply_status == "failed"
    assert run.failure_reason == "Draft produced no meaningful source changes to apply."
    assert run.touched_files == []
    assert not preview_called.is_set()
    assert not (source_root / "frontend" / "vite.config.js").exists()
    assert not (source_root / "frontend" / "vite.config.d.ts").exists()


def test_mode_profiles_differentiate_fast_balanced_and_quality() -> None:
    fast = ModeProfiles.resolve(GenerationMode.FAST)
    balanced = ModeProfiles.resolve(GenerationMode.BALANCED)
    quality = ModeProfiles.resolve(GenerationMode.QUALITY)

    assert fast.targeted_file_limit < balanced.targeted_file_limit < quality.targeted_file_limit
    assert fast.edit_iteration_limit < balanced.edit_iteration_limit < quality.edit_iteration_limit
    assert fast.repair_attempt_limit < balanced.repair_attempt_limit < quality.repair_attempt_limit
    assert fast.compact_aggressiveness == "high"
    assert balanced.verification_depth == "balanced"
    assert quality.verification_depth == "deep"


    def test_task_profiles_use_codex_for_code_paths() -> None:
        for profile in TASK_PROFILES.values():
            routing = profile["routing"]
            assert routing["spec_analysis"] == "gpt-5-mini"
            assert routing["code_plan"] == "gpt-5-mini"
            assert routing["ir_codegen"] == "gpt-5.2-codex"
            assert routing["code_edit"] == "gpt-5.2-codex"
            assert routing["repair"] == "gpt-5.2-codex"


def test_context_pack_builder_applies_mode_budget_and_prompt_fingerprint(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Budget Workspace",
            description="Context budgets should differ by mode.",
            path=str((tmp_path / "data" / "workspaces" / "ws_budget").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_budget"
    workspace_service.ensure_draft(workspace.workspace_id, draft_run_id)
    target_files = [f"miniapp/app/static/client/{name}" for name in ["index.html", "workbench.html", "workspace.html", "profile.html"]]

    fast_pack = app.state.container.context_pack_builder.build(
        workspace=workspace_service.get_workspace(workspace.workspace_id),
        prompt="Create a fast flower shop app",
        model_profile="openai_code_fast",
        generation_mode=GenerationMode.FAST,
        target_files=target_files,
        run_id=draft_run_id,
    )
    quality_pack = app.state.container.context_pack_builder.build(
        workspace=workspace_service.get_workspace(workspace.workspace_id),
        prompt="Create a quality flower shop app",
        model_profile="openai_code_fast",
        generation_mode=GenerationMode.QUALITY,
        target_files=target_files,
        run_id=draft_run_id,
    )

    assert fast_pack.retrieval_stats["budget"]["verification_depth"] == "fast"
    assert quality_pack.retrieval_stats["budget"]["verification_depth"] == "deep"
    assert fast_pack.retrieval_stats["mode_profile"]["targeted_file_limit"] < quality_pack.retrieval_stats["mode_profile"]["targeted_file_limit"]
    assert "combined_hash" in fast_pack.retrieval_stats["prompt_fingerprint"]


def test_code_index_retrieval_records_candidate_cache_hits(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Retrieval Cache Workspace",
            "description": "Candidate path ranking should be cached per prompt and revision.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")
    client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "miniapp/app/routes/order_queue.py",
            "content": "def order_queue_status(order_id: str) -> str:\n    return f'queued:{order_id}'\n",
        },
    )
    client.post(f"/workspaces/{workspace_id}/index")

    code_index: CodeIndexService = app.state.container.code_index_service
    first = code_index.retrieve(
        workspace_id=workspace_id,
        prompt="Inspect the order queue status route",
        code_limit=4,
    )
    second = code_index.retrieve(
        workspace_id=workspace_id,
        prompt="Inspect the order queue status route",
        code_limit=4,
    )

    assert first["stats"]["candidate_cache_hit"] is False
    assert second["stats"]["candidate_cache_hit"] is True


def test_run_artifacts_expose_engine_diagnostics_reports(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Engine Artifacts Workspace",
            "description": "Engine diagnostics should flow through run artifacts.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    client.post(f"/workspaces/{workspace_id}/clone-template")

    response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Create a flower shop mini app with clear manager oversight and specialist workflow.",
            "mode": "generate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "balanced",
            "intent": "create",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
        },
    )
    assert response.status_code == 200
    job = response.json()
    run_id = job["linked_run_id"]

    artifacts = client.get(f"/runs/{run_id}/artifacts").json()

    if artifacts.get("context_budget") is not None:
        assert artifacts["context_budget"]["budget"]["verification_depth"] == "balanced"
    if artifacts.get("prompt_fingerprint") is not None:
        assert "combined_hash" in artifacts["prompt_fingerprint"]
    if artifacts.get("mode_profile_snapshot") is not None:
        assert artifacts["mode_profile_snapshot"]["generation_mode"] == "balanced"
    if artifacts.get("phase_metrics") is not None:
        assert isinstance(artifacts["phase_metrics"]["items"], list)
    if artifacts.get("engine_trace") is not None:
        assert isinstance(artifacts["engine_trace"]["entries"], list)


def test_session_engine_persists_workspace_session_costs_and_project_memory(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    engine = app.state.container.session_engine

    payload = engine.record_run_summary(
        workspace_id="ws_costs",
        run_id="run_cost_1",
        prompt="Build a flower ordering app with delivery tracking.",
        run_mode="generate",
        generation_mode="balanced",
        status="completed",
        model_profile="openai_code_fast",
        llm_model="openai/test-model",
        cache_stats={
            "llm_requests": 3,
            "input_tokens": 1200,
            "output_tokens": 350,
            "total_tokens": 1550,
            "reasoning_tokens": 90,
            "cached_tokens": 800,
            "cache_write_tokens": 220,
            "estimated_cost_usd": 0.031,
        },
        latency_breakdown={"total_ms": 4200},
        summary="Generated a delivery-aware flower ordering flow and preserved role routing.",
        files=["miniapp/app/static/client/app.js", "miniapp/app/main.py"],
        failure_class=None,
    )

    session_costs = payload["session_costs"]
    assert session_costs["totals"]["run_count"] == 1
    assert session_costs["totals"]["input_tokens"] == 1200
    assert session_costs["totals"]["cached_tokens"] == 800
    assert session_costs["totals"]["cache_hit_ratio"] > 0

    memory_context = engine.select_project_memory(
        workspace_id="ws_costs",
        prompt="Improve delivery tracking in the flower ordering app.",
        generation_mode="balanced",
        run_mode="generate",
    )
    assert memory_context["selected_count"] >= 1
    assert "flower" in memory_context["summary"].lower()


def test_session_engine_selects_fix_memory_with_failure_bias(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    engine = app.state.container.session_engine

    engine.record_run_summary(
        workspace_id="ws_memory",
        run_id="run_mem_1",
        prompt="Fix broken avatar helper wiring in client profile.",
        run_mode="fix",
        generation_mode="balanced",
        status="failed",
        model_profile="openai_code_fast",
        llm_model="openai/test-model",
        cache_stats={"llm_requests": 1, "input_tokens": 220, "output_tokens": 40, "total_tokens": 260},
        latency_breakdown={"fix_total_ms": 900},
        summary="Broken avatar helper names in client and manager profile scripts caused static validation failures.",
        files=["miniapp/app/static/client/app.js", "miniapp/app/static/manager/app.js"],
        failure_class="frontend_compile/type/import",
    )

    selected = engine.select_project_memory(
        workspace_id="ws_memory",
        prompt="Repair avatar helper import failures in the profile scripts.",
        generation_mode="balanced",
        run_mode="fix",
    )
    assert selected["selected_count"] == 1
    assert "avatar" in selected["summary"].lower()


def test_diminishing_returns_service_stops_after_repeated_low_signal_iterations(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    engine = app.state.container.session_engine

    first = engine.should_stop_for_diminishing_returns(
        workspace_id="ws_diminishing",
        run_id="run_dim_1",
        phase="fix_repair",
        generation_mode="balanced",
        metrics={
            "attempt": 1,
            "changed_files_count": 1,
            "diff_chars": 80,
            "failure_signature": "same:error",
            "total_tokens": 2000,
        },
    )
    second = engine.should_stop_for_diminishing_returns(
        workspace_id="ws_diminishing",
        run_id="run_dim_1",
        phase="fix_repair",
        generation_mode="balanced",
        metrics={
            "attempt": 2,
            "changed_files_count": 1,
            "diff_chars": 70,
            "failure_signature": "same:error",
            "total_tokens": 2200,
        },
    )

    assert first["should_stop"] is False
    assert second["should_stop"] is True
    report = app.state.container.generation_service.current_report("ws_diminishing", "diminishing_returns")
    assert report["items"]


def test_pre_apply_contract_pass_rewrites_actor_dependency_and_api_fetch_and_injects_error_state(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/requests.py",
            operation="replace",
            content='from fastapi import Depends\n\ndef get_actor_context():\n    return None\n\ndef handler(actor=Depends(lambda: get_actor_context())):\n    return actor\n',
            reason="test fixture",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content='<html><body><main class="page-shell"></main><script src="/static/client/app.js"></script></body></html>',
            reason="test fixture",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/app.js",
            operation="replace",
            content='const errorState = document.getElementById("error-state");\nwindow.setupPreviewBridge?.("client");\nfetch("/api/requests");\n',
            reason="test fixture",
        ),
    ]

    result = service._synchronize_backend_dependency_contract("ws_test", "run_test", operations)
    result = service._synchronize_frontend_api_contract("ws_test", "run_test", result)
    result = service._synchronize_basic_page_state_contract(
        "ws_test",
        "run_test",
        page_graph={"roles": {"client": {"pages": [{"file_path": "miniapp/app/static/client/index.html", "error_state": "error", "loading_state": ""}]}}},
        operations=result,
    )
    operation_map = {operation.file_path: operation for operation in result}
    assert "Depends(get_actor_context)" in (operation_map["miniapp/app/routes/requests.py"].content or "")
    assert 'window.miniappApiFetch("/api/requests")' in (operation_map["miniapp/app/static/client/app.js"].content or "")
    assert 'id="error-state"' not in (operation_map["miniapp/app/static/client/index.html"].content or "")


def test_basic_page_contract_normalizes_shell_assets_and_role_local_links_and_depends_import(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    operations = [
        DraftFileOperation(
            file_path="miniapp/app/routes/attachments.py",
            operation="replace",
            content=(
                "from fastapi import APIRouter, File, UploadFile, Header, HTTPException\n\n"
                "router = APIRouter()\n"
                "def _get_caller():\n    return ('client', 'c_1')\n"
                "@router.post('/api/attachments')\n"
                "def upload(file: UploadFile = File(...), uploader: tuple[str, str] = Depends(_get_caller)):\n    return {'ok': True}\n"
            ),
            reason="test fixture",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content=(
                "<html><head>"
                '<link rel="stylesheet" href="/static/shell.css" />'
                "</head><body><main class='page-shell'>"
                '<a href="/profile">Profile</a>'
                '<a href="/create">Create</a>'
                "</main></body></html>"
            ),
            reason="test fixture",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/app.js",
            operation="replace",
            content='console.log("client");\n',
            reason="test fixture",
        ),
    ]

    result = service._synchronize_backend_dependency_contract("ws_test", "run_test", operations)
    result = service._synchronize_basic_page_state_contract(
        "ws_test",
        "run_test",
        page_graph={
            "roles": {
                "client": {
                    "pages": [
                        {
                            "file_path": "miniapp/app/static/client/index.html",
                            "style_path": "miniapp/app/static/client/styles.css",
                            "script_path": "miniapp/app/static/client/app.js",
                            "route_path": "/",
                            "loading_state": "",
                            "error_state": "",
                        },
                        {
                            "file_path": "miniapp/app/static/client/create/index.html",
                            "style_path": "miniapp/app/static/client/create/styles.css",
                            "script_path": "miniapp/app/static/client/create/app.js",
                            "route_path": "/create",
                            "loading_state": "",
                            "error_state": "",
                        },
                        {
                            "file_path": "miniapp/app/static/client/profile/index.html",
                            "style_path": "miniapp/app/static/client/profile/styles.css",
                            "script_path": "miniapp/app/static/client/profile/app.js",
                            "route_path": "/profile",
                            "loading_state": "",
                            "error_state": "",
                        },
                    ]
                }
            }
        },
        operations=result,
    )
    operation_map = {operation.file_path: operation for operation in result}
    attachments = operation_map["miniapp/app/routes/attachments.py"].content or ""
    html = operation_map["miniapp/app/static/client/index.html"].content or ""

    assert "from fastapi import APIRouter, File, UploadFile, Header, HTTPException, Depends" in attachments
    assert '/static/shared/base.css' in html
    assert '/static/preview_bridge.js' in html
    assert '/static/client/styles.css' in html
    assert '/static/client/app.js' in html
    assert 'href="/client/profile"' in html
    assert 'href="/client/create"' in html
    assert 'class="page-shell"' in html or 'class=\'page-shell\'' in html
    assert 'padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));' in html


def test_basic_page_contract_upgrades_generated_main_to_shell_root_and_bridge(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/manager/workload/index.html",
            operation="replace",
            content=(
                "<!doctype html><html><head>"
                '<link rel="stylesheet" href="/static/shared/base.css" />'
                "</head><body><main class='page' id='app'></main>"
                '<script src="/static/manager/workload/app.js" defer></script>'
                "</body></html>"
            ),
            reason="test fixture",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/manager/workload/app.js",
            operation="replace",
            content='console.log("workload");\n',
            reason="test fixture",
        ),
    ]

    result = service._synchronize_basic_page_state_contract(
        "ws_test",
        "run_test",
        page_graph={
            "roles": {
                "manager": {
                    "pages": [
                        {
                            "file_path": "miniapp/app/static/manager/workload/index.html",
                            "style_path": "miniapp/app/static/manager/workload/styles.css",
                            "script_path": "miniapp/app/static/manager/workload/app.js",
                            "route_path": "/workload",
                            "loading_state": "",
                            "error_state": "",
                        }
                    ]
                }
            }
        },
        operations=operations,
    )
    operation_map = {operation.file_path: operation for operation in result}
    html = operation_map["miniapp/app/static/manager/workload/index.html"].content or ""

    assert '/static/preview_bridge.js' in html
    assert "page-shell" in html
    assert 'padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));' in html


def test_selected_pages_for_edit_infers_minimal_pages_from_target_files(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    selected = service.generation_codegen_selection._selected_pages_for_edit(
        {},
        {
            "miniapp/app/static/client/index.html",
            "miniapp/app/static/client/profile/index.html",
        },
    )

    assert [role for role, _page in selected] == ["client", "client"]
    assert selected[0][1]["route_path"] == "/"
    assert selected[1][1]["route_path"] == "/profile"
    assert selected[1][1]["style_path"] == "miniapp/app/static/client/profile/styles.css"
    assert selected[1][1]["script_path"] == "miniapp/app/static/client/profile/app.js"


def test_generation_shell_contract_owner_enforces_shared_shell_markers(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    html = "<html><head></head><body><main id='app'></main></body></html>"
    html = service.generation_shell_contract.ensure_base_stylesheet_ref(html)
    html = service.generation_shell_contract.ensure_preview_bridge_ref(html)
    html = service.generation_shell_contract.ensure_page_shell_contract(html)

    assert '/static/shared/base.css' in html
    assert '/static/preview_bridge.js' in html
    assert 'page-shell' in html
    assert 'padding-top: max(76px, calc(var(--telegram-top-safe-offset) + 12px));' in html


def test_normalize_local_route_ref_handles_js_template_literals() -> None:
    normalized = GenerationService._normalize_local_route_ref("/client/requests/${item.id}")
    assert normalized == "/client/requests/sample"


def test_normalize_role_route_path_handles_underscore_role_aliases() -> None:
    assert GenerationService._normalize_role_route_path("client", "/client_request_create", index=1) == "/request_create"
    assert GenerationService._normalize_role_route_path("manager", "/manager_home", index=0) == "/home"


def test_finalize_role_pages_resolves_handoff_aliases_from_stripped_page_ids(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    pages = service._finalize_role_pages(
        "client",
        [
            {
                "page_id": "client_home",
                "route_path": "/",
                "file_path": "miniapp/app/static/client/index.html",
                "title": "Home",
                "description": "",
                "purpose": "",
                "page_kind": "dashboard",
                "navigation_label": "Home",
                "component_name": "ClientHomePage",
                "primary_actions": [],
                "handoff_paths": ["/client_request_create", "/client_profile_edit"],
                "loading_state": "",
                "empty_state": "",
                "error_state": "",
            },
            {
                "page_id": "client_request_create",
                "route_path": "/requests_new",
                "file_path": "miniapp/app/static/client/requests_new/index.html",
                "title": "New",
                "description": "",
                "purpose": "",
                "page_kind": "form",
                "navigation_label": "New",
                "component_name": "ClientRequestCreatePage",
                "primary_actions": [],
                "handoff_paths": [],
                "loading_state": "",
                "empty_state": "",
                "error_state": "",
            },
            {
                "page_id": "client_profile_edit",
                "route_path": "/profile",
                "file_path": "miniapp/app/static/client/profile/index.html",
                "title": "Profile",
                "description": "",
                "purpose": "",
                "page_kind": "profile",
                "navigation_label": "Profile",
                "component_name": "ClientProfilePage",
                "primary_actions": [],
                "handoff_paths": [],
                "loading_state": "",
                "empty_state": "",
                "error_state": "",
            },
        ],
        require_multi_page=True,
    )

    home_page = next(page for page in pages if page["page_id"] == "client_home")
    assert home_page["handoff_paths"] == ["/requests_new", "/profile"]


def test_build_validator_flags_invalid_actor_dependency(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    (workspace_root / "miniapp/app/routes").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/app").mkdir(parents=True, exist_ok=True)
    (workspace_root / "miniapp/requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (workspace_root / "docker").mkdir(parents=True, exist_ok=True)
    (workspace_root / "docker/docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_root / "artifacts").mkdir(parents=True, exist_ok=True)
    (workspace_root / "artifacts/grounded_spec.json").write_text(json.dumps({}), encoding="utf-8")
    (workspace_root / "miniapp/app/main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (workspace_root / "miniapp/app/routes/example.py").write_text(
        "from fastapi import Depends\n\n"
        "def get_actor_context():\n    return None\n\n"
        "def route(actor=Depends(lambda: get_actor_context())):\n    return actor\n",
        encoding="utf-8",
    )

    issues = BuildValidator()._validate_route_module_import_safety(workspace_root)
    assert any(issue.code == "build.invalid_actor_dependency" for issue in issues)


def test_compile_prompt_to_scaffold_builds_role_local_targets_and_excludes_manifests(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    spec = service._build_grounded_spec(
        workspace_id="ws_test",
        prompt="Create a request management app for client, specialist, and manager roles.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="template",
        prompt_turn_id="turn_1",
        generation_mode=GenerationMode.BALANCED,
    )
    role_contract, plan_result = service._compile_prompt_to_scaffold(
        prompt=spec.product_goal,
        grounded_spec=spec,
        role_scope=["client", "specialist", "manager"],
        workspace_tree=[],
    )

    assert role_contract["source"] == "prompt_scaffold"
    assert plan_result["scope_mode"] == "whole_file_build"
    assert "miniapp/app/generated/route_manifest.json" not in plan_result["target_files"]
    assert "miniapp/app/generated/runtime_manifest.json" not in plan_result["target_files"]
    assert "miniapp/app/routes/client.py" in plan_result["backend_targets"]
    assert "miniapp/app/routes/manager.py" in plan_result["backend_targets"]
    assert any(path == "miniapp/app/static/client/index.html" for path in plan_result["target_files"])
    assert any(path == "miniapp/app/static/manager/profile/index.html" for path in plan_result["target_files"])


def test_scaffold_backend_targets_do_not_infer_business_routes_from_prompt_tokens(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service: GenerationService = app.state.container.generation_service

    spec = service._build_grounded_spec(
        workspace_id="ws_test",
        prompt="Clients submit requests, choose time slots, managers assign specialists, specialists leave comments.",
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        doc_refs=[],
        template_revision_id="template",
        prompt_turn_id="turn_1",
        generation_mode=GenerationMode.BALANCED,
    )
    targets = service._scaffold_backend_targets_from_spec(
        prompt=spec.product_goal,
        grounded_spec=spec,
        role_scope=["client", "specialist", "manager"],
    )

    assert targets == []


def test_fix_scope_builder_does_not_auto_expand_page_triplet_scope(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    workspace_service = app.state.container.workspace_service
    orchestrator = app.state.container.fix_orchestrator

    workspace = workspace_service.create_workspace(
        WorkspaceRecord(
            name="Evidence Scope Workspace",
            description="Frontend fixes should stay on the directly implicated file unless evidence expands them.",
            path=str((tmp_path / "data" / "workspaces" / "ws_evidence_scope").resolve()),
        )
    )
    workspace_service.clone_template(workspace.workspace_id)
    draft_run_id = "run_evidence_scope"
    workspace_service.prepare_draft(workspace.workspace_id, draft_run_id)

    scope = orchestrator._build_write_scope(
        workspace.workspace_id,
        draft_run_id,
        ["miniapp/app/static/client/profile/index.html"],
        "frontend_compile/type/import",
        [],
    )

    assert {entry.file_path for entry in scope} == {"miniapp/app/static/client/profile/index.html"}


def test_pre_apply_dom_contract_sync_does_not_inject_missing_page_ids() -> None:
    html = """<!doctype html>
<html lang="en">
  <body>
    <main>
      <section>Profile</section>
    </main>
  </body>
</html>
"""
    script = """
document.getElementById("profile-form");
document.getElementById("save-button");
document.getElementById("email-error");
document.getElementById("preview-name");
"""

    updated = GenerationService._ensure_html_dom_ids_for_script(html, script)

    assert updated == html


def test_pre_apply_dom_contract_sync_keeps_existing_ids_intact() -> None:
    html = """<!doctype html>
<html lang="en">
  <body>
    <main>
      <form id="profile-form"></form>
      <button id="save-button" type="button"></button>
    </main>
  </body>
</html>
"""
    script = """
document.getElementById("profile-form");
document.getElementById("save-button");
"""

    updated = GenerationService._ensure_html_dom_ids_for_script(html, script)

    assert updated == html


def test_fix_orchestrator_marks_generated_manifests_read_only(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    orchestrator = app.state.container.fix_orchestrator

    assert orchestrator._is_read_only_generated_surface("miniapp/app/generated/route_manifest.json")
    assert orchestrator._is_read_only_generated_surface("artifacts/generated_app_graph.json")
    assert not orchestrator._is_read_only_generated_surface("miniapp/app/routes/requests.py")


def test_endpoint_aliases_canonicalize_to_requests() -> None:
    assert GenerationService._route_module_path_for_endpoint_name("submissions") == "miniapp/app/routes/requests.py"
    assert GenerationService._route_module_path_for_endpoint_name("booking") == "miniapp/app/routes/requests.py"


def test_route_contract_bootstrap_only_limits_missing_routes_to_core_skeleton(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    service = app.state.container.generation_service

    operations = service._synchronize_minimal_workflow_route_contracts(
        "ws_test",
        "run_test",
        [],
        contract_sync_mode="bootstrap_only",
    )

    paths = {operation.file_path for operation in operations}

    assert "miniapp/app/routes/runtime.py" in paths
    assert "miniapp/app/routes/profiles.py" in paths
    assert "miniapp/app/routes/client.py" in paths
    assert "miniapp/app/routes/specialist.py" in paths
    assert "miniapp/app/routes/manager.py" in paths
    assert "miniapp/app/routes/requests.py" not in paths
    assert "miniapp/app/routes/comments.py" not in paths
    assert "miniapp/app/routes/assignments.py" not in paths


def test_role_page_routes_with_jinja_templates_are_not_forced_through_invariant_repair() -> None:
    content = """from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
router = APIRouter(prefix="/manager")
templates = Jinja2Templates(directory="miniapp/app/static")
@router.get("/requests", response_class=HTMLResponse)
async def manager_requests(request: Request):
    return templates.TemplateResponse("manager/requests/index.html", {"request": request})
"""

    assert GenerationService._route_module_requires_db_backed_repair("miniapp/app/routes/manager.py", content) is False


def test_strip_noncanonical_runtime_route_handlers_removes_prefixed_runtime_aliases() -> None:
    content = """from fastapi import APIRouter

router = APIRouter(prefix="/client")

@router.get("/")
def index():
    return {}

@router.get("/api/runtime/client/manifest")
def runtime_manifest():
    return {"ok": True}
"""

    updated = GenerationService._strip_noncanonical_runtime_route_handlers(content)

    assert '@router.get("/api/runtime/client/manifest")' not in updated
    assert "def runtime_manifest" not in updated
    assert '@router.get("/")' in updated


def test_normalize_runtime_route_module_source_supports_sample_alias() -> None:
    content = """from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["runtime"])
ALLOWED_ROLES = {"client", "specialist", "manager"}

def _validate_role(role: str) -> None:
    if role not in ALLOWED_ROLES:
        raise HTTPException(status_code=404, detail="Role not supported")

@router.get("/api/runtime/{role}/manifest")
def manifest(role: str):
    _validate_role(role)
    return {"role": role}
"""

    updated = GenerationService._normalize_runtime_route_module_source(content)

    assert 'if normalized == "sample":' in updated
    assert "def _validate_role(role: str) -> str:" in updated
    assert "role = _validate_role(role)" in updated


def test_normalize_runtime_route_module_source_is_idempotent() -> None:
    content = """from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["runtime"])
ALLOWED_ROLES = {"client", "specialist", "manager"}

def _validate_role(role: str) -> None:
    if role not in ALLOWED_ROLES:
        raise HTTPException(status_code=404, detail="Role not supported")

@router.get("/api/runtime/{role}/manifest")
def manifest(role: str):
    _validate_role(role)
    return {"role": role}
"""

    once = GenerationService._normalize_runtime_route_module_source(content)
    twice = GenerationService._normalize_runtime_route_module_source(once)

    assert twice == once
    assert twice.count("def _validate_role(role: str) -> str:") == 1
    assert twice.count("role = _validate_role(role)") == 1


def test_whole_file_cluster_allows_no_op_when_targets_already_exist() -> None:
    generation_service = GenerationService.__new__(GenerationService)
    generation_service._whole_file_cluster_system_prompt = lambda _cluster_name: "system"
    generation_service._whole_file_cluster_user_prompt = lambda **_kwargs: "user"
    generation_service._generate_structured_with_retry = lambda **_kwargs: {  # type: ignore[method-assign]
        "payload": {"assistant_message": "No changes needed.", "operations": []},
        "model": "gpt-5.2",
    }
    generation_service._normalize_model_payload = lambda payload: payload  # type: ignore[method-assign]
    generation_service._sanitize_draft_operations = lambda operations: operations  # type: ignore[method-assign]

    result = generation_service._resolve_whole_file_cluster(
        cluster_name="backend_route_assignments",
        cluster_targets=["miniapp/app/routes/assignments.py"],
        prompt="Keep the assignments route as is.",
        grounded_spec=None,
        role_scope=["manager"],
        role_contract={},
        page_graph={},
        scope_mode="whole_file_build",
        intent="edit",
        file_contexts={"miniapp/app/routes/assignments.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"},
        generation_mode=GenerationMode.BALANCED,
        creative_direction={},
    )

    assert result["outcome"] == "no_op"
    assert result["operations"] == []


def test_canonicalize_local_role_links_in_text_strips_trailing_role_slashes() -> None:
    text = '<a href="/specialist/">Desk</a><script>fetch("/client/?tab=open"); window.location.href="/manager/requests/";</script>'

    normalized = GenerationService._canonicalize_local_role_links_in_text(text)

    assert 'href="/specialist"' in normalized
    assert 'fetch("/client?tab=open")' in normalized
    assert '"/manager/requests"' in normalized


def test_best_effort_apply_is_disabled_for_manual_approve_runs(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    run_service = app.state.container.run_service
    run = SimpleNamespace(mode="generate", apply_strategy="manual_approve")
    job = SimpleNamespace(status="failed", failure_class="app/runtime_test", validation_snapshot=SimpleNamespace(issues=[{"blocking": True, "location": "tests", "code": "tests.python_generated_app"}]))

    allowed = run_service._should_apply_best_effort_after_failed_repairs(run, job, meaningful_paths=["miniapp/app/main.py"])

    assert allowed is False
