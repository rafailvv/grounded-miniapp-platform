from __future__ import annotations

import importlib.util
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app
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
