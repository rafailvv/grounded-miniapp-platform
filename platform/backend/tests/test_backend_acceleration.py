from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import threading
import time

import app.services.check_runner as check_runner_module
from fastapi.testclient import TestClient

from app.ai.openrouter_client import OpenRouterClient
from app.main import create_app
from app.models.domain import CheckExecutionRecord, CreateRunRequest, DraftFileOperation, FixScopeEntry, GenerateRequest, GenerationMode, JobRecord, PreviewRecord, RunCheckResult, ValidationSnapshot, WorkspaceRecord
from app.services.code_index_service import CodeIndexService
from app.services.engine.mode_profiles import ModeProfiles
from app.services.generation_service import DESIGN_REFERENCE_FILES, SHARED_GENERATED_FILES
from app.validators.build_validator import BuildValidator


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


def test_system_configuration_defaults_to_balanced(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    response = client.get("/system/configuration")
    assert response.status_code == 200
    assert response.json()["defaults"]["generation_mode"] == "balanced"


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

    assert run.status == "completed"
    assert run.apply_status == "noop"
    assert run.draft_status == "ready"
    assert run.draft_ready is True
    assert run.current_stage == "completed with warnings"
    assert run.failure_reason == "Connectivity validators reported unresolved issues."


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

    assert final_run["status"] in {"awaiting_approval", "completed"}
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

    assert final_run["status"] == "awaiting_approval"
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

    def fake_plan_patch(*, job, fix_case, file_contexts):
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
            content = workspace_service.read_file(fix_case.workspace_id, file_path, run_id=fix_case.run_id)
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
            "diagnosis": "Apply the smallest targeted fix for the broken avatar helper names and profile state cleanup.",
            "planned_targets": list(target_files),
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

    def fake_plan_patch(*, job, fix_case, file_contexts):
        del job, fix_case
        target = next(iter(file_contexts.keys()), "miniapp/app/static/client/app.js")
        content = str(file_contexts.get(target) or "")
        return {
            "diagnosis": "Apply a minimal static helper name fix.",
            "planned_targets": [target],
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
    assert "same failure repeated twice" in (final_run["failure_reason"] or "").lower()
    artifacts = client.get(f"/runs/{run_id}/artifacts").json()
    assert artifacts["fix_attempts"]["items"]


def test_run_completes_before_async_preview_rebuild_finishes(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

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
    app.state.container.workspace_service.clone_template(workspace_id)

    preview_service = app.state.container.preview_service
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

    assert run.status == "completed"
    assert run.current_stage == "completed"
    assert rebuild_started.is_set()
    preview = preview_service.get(workspace_id)
    assert preview.stage == "rebuilding"
    release_rebuild.set()
    time.sleep(0.15)
    assert preview_service.get(workspace_id).status == "running"


def test_preview_rebuild_failure_does_not_revert_completed_run(tmp_path: Path) -> None:
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

    assert run.status == "completed"
    time.sleep(0.15)
    assert app.state.container.run_service.get_run(run.run_id).status == "completed"
    assert preview_service.get(workspace_id).status == "error"
    artifacts = app.state.container.run_service.get_run_artifacts(run.run_id)
    assert artifacts["preview"]["status"] == "error"
    assert artifacts["preview"]["last_error"] == "Simulated preview rebuild failure."


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
        preview=PreviewRecord(workspace_id="ws_1", status="running", url="http://localhost:3000", draft_run_id="run_1"),
        preview_run_id="run_1",
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
        preview=PreviewRecord(workspace_id="ws_1", status="running", url="http://localhost:3000", draft_run_id="run_1"),
        preview_run_id="run_1",
    )

    assert result.status == "passed"
    assert attempts["client"] == 2
    assert any("/client returned usable preview content after 2 attempt(s)." in line for line in result.logs)


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


def test_parallel_page_generation_falls_back_to_serial_compact_retry(tmp_path: Path) -> None:
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
    assert retried == [(page_one, "serial_compact_retry")]
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


def test_normalize_page_plan_proactively_adds_backend_targets_from_page_graph_dependencies(tmp_path: Path) -> None:
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

    assert "miniapp/app/routes/catalog.py" in planned["backend_targets"]
    assert "miniapp/app/routes/orders.py" in planned["backend_targets"]
    assert "miniapp/app/main.py" in planned["backend_targets"]
    assert "miniapp/app/routes/catalog.py" in planned["target_files"]
    assert planned["planner_contract_enrichment"]["proactive_backend_targets"]


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


def test_page_graph_gate_rejects_workflow_heavy_role_trees_without_business_pages(tmp_path: Path) -> None:
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
        require_business_pages=True,
    )

    assert any("missing separate business pages" in issue for issue in issues)


def test_page_graph_gate_rejects_collapsed_workflow_plan_even_in_minimal_patch_mode(tmp_path: Path) -> None:
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
        require_business_pages=True,
    )

    assert any("did not receive enough distinct pages" in issue for issue in issues)
    assert any("missing separate business pages" in issue for issue in issues)
    assert any("collapses the app into one screen per selected role" in issue for issue in issues)


def test_edit_gate_rejects_loading_first_static_page_without_dependencies(tmp_path: Path) -> None:
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
                        "data_dependencies": [],
                    },
                    {
                        "route_path": "/client/catalog",
                        "file_path": "miniapp/app/static/client/catalog.html",
                        "data_dependencies": ["records"],
                    },
                    {
                        "route_path": "/client/profile",
                        "file_path": "miniapp/app/static/client/profile.html",
                        "data_dependencies": [],
                    },
                ],
            }
        }
    }
    operations = [
        DraftFileOperation(
            file_path="miniapp/app/static/client/index.html",
            operation="replace",
            content="<html><body><main>Loading content… Loading... Loading preview...</main></body></html>",
            reason="Bad static page",
        ),
        DraftFileOperation(
            file_path="miniapp/app/static/client/catalog.html",
            operation="replace",
            content="<html><body><main><h1>Catalog</h1><a href='/client/cart'>Cart</a></main></body></html>",
            reason="Catalog page",
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
        require_business_pages=True,
    )

    assert any("loading-first copy" in issue for issue in issues)


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


def test_preview_url_uses_lightweight_preview_peek_for_stuck_health_check_state(tmp_path: Path) -> None:
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

    response = client.get(f"/workspaces/{workspace_id}/preview/url")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["url"] == "http://localhost:16734"


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
        fix_case=fix_case,
    )

    assert "artifacts/generated_app_graph.json" in contexts


def test_auto_fixed_generate_run_resumes_generation_from_same_run_checkpoint(tmp_path: Path) -> None:
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
    resumed_generation = threading.Event()
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
        resumed_generation.set()
        workspace_service.prepare_draft(workspace_id, request.linked_run_id or "run_resumed")
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
            summary="Resumed generation completed successfully.",
            validation_snapshot=ValidationSnapshot(
                grounded_spec_valid=True,
                app_ir_valid=True,
                build_valid=True,
                blocking=False,
                issues=[],
            ),
        )

    def fake_fix_generate(workspace_id: str, request: GenerateRequest, *, should_stop=None):
        del should_stop
        fix_calls.append(request.mode)
        assert workspace_service.draft_exists(workspace_id, request.linked_run_id or "")
        run_id = request.linked_run_id or "run_test"
        draft_root = workspace_service.ensure_draft(workspace_id, run_id)
        client_routes = draft_root / "frontend" / "src" / "roles" / "client" / "ClientRoutes.tsx"
        client_routes.write_text(
            client_routes.read_text(encoding="utf-8").replace(
                "export function ClientRoutes(): JSX.Element {",
                "export function ClientRoutes(): JSX.Element {\n  // fixed before resuming generation\n",
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
    assert generation_modes == ["generate", "generate"]
    assert fix_calls == ["fix"]
    assert resumed_generation.wait(1.0)
    checkpoint = app.state.container.store.get("reports", f"resume_checkpoint:{workspace_id}")
    assert checkpoint is not None
    assert checkpoint["status"] == "resumed"
    assert checkpoint["resumed_from_fix_run_id"] == run.run_id

def test_successful_fix_run_queues_resume_generation_from_checkpoint(tmp_path: Path) -> None:
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
        specialist_routes = draft_root / "frontend" / "src" / "roles" / "specialist" / "SpecialistRoutes.tsx"
        specialist_routes.write_text(
            specialist_routes.read_text(encoding="utf-8").replace(
                "export function SpecialistRoutes(): JSX.Element {",
                "export function SpecialistRoutes(): JSX.Element {\n  // fix completed before resume\n",
            ),
            encoding="utf-8",
        )
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
    assert resumed_generation.wait(1.0)
    checkpoint = app.state.container.store.get("reports", f"resume_checkpoint:{workspace_id}")
    assert checkpoint is not None
    assert checkpoint["status"] == "resumed"
    assert checkpoint["resumed_from_fix_run_id"] == run.run_id


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
        (draft_root / "frontend" / "vite.config.js").write_text("export default {};\n", encoding="utf-8")
        (draft_root / "frontend" / "vite.config.d.ts").write_text("export {};\n", encoding="utf-8")
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

    assert artifacts["context_budget"]["budget"]["verification_depth"] == "balanced"
    assert "combined_hash" in artifacts["prompt_fingerprint"]
    assert artifacts["mode_profile_snapshot"]["generation_mode"] == "balanced"
    assert isinstance(artifacts["phase_metrics"]["items"], list)
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
