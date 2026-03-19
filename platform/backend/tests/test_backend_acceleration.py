from __future__ import annotations

import importlib.util
from pathlib import Path
import threading
import time

import app.services.check_runner as check_runner_module
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import CreateRunRequest
from app.services.code_index_service import CodeIndexService
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
            "relative_path": "backend/app/custom_order_service.py",
            "content": "def order_queue_status(order_id: str) -> str:\n    return f'queue:{order_id}'\n",
        },
    )
    response = client.post(f"/workspaces/{workspace_id}/index")
    assert response.status_code == 200

    code_index: CodeIndexService = app.state.container.code_index_service
    retrieval = code_index.retrieve(
        workspace_id=workspace_id,
        prompt="Fix the order queue status flow in backend service",
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

    assert final_run["status"] == "awaiting_approval"
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
    (workspace_path / "backend" / "app").mkdir(parents=True)
    (workspace_path / "frontend" / "src" / "roles" / "client").mkdir(parents=True)
    (workspace_path / "docker").mkdir(parents=True)
    (workspace_path / "artifacts").mkdir(parents=True)

    (workspace_path / "backend" / "app" / "main.py").write_text("app = None\n", encoding="utf-8")
    (workspace_path / "backend" / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
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
    assert result.details == "Frontend build tooling is unavailable in the backend runtime."
    assert "npm was not found on PATH." in result.logs
    assert runner.has_tooling_failure([result]) is True
    assert runner.classify_failure([result]) == "tooling/runtime_misconfiguration"
