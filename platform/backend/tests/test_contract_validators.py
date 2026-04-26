from __future__ import annotations

from pathlib import Path

from app.main import create_app
from app.models.domain import WorkspaceRecord
from app.services.check_runner import CheckRunner
from app.services.workspace.preview_service import PreviewService
from app.validators.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator


def test_template_shell_passes_platform_build_validator(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Validator Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)

    issues = BuildValidator().validate(source_dir)

    assert issues == []


def test_build_validator_flags_missing_dom_id(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="DOM Contract Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    script = source_dir / "miniapp/app/static/client/app.js"
    script.write_text(script.read_text(encoding="utf-8") + "\ndocument.getElementById('missing-target');\n", encoding="utf-8")

    issues = BuildValidator().validate(source_dir)

    assert any(issue.code == "build.page_script_dom_contract" for issue in issues)


def test_build_validator_flags_duplicate_runtime_route(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Route Contract Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    duplicate = source_dir / "miniapp/app/routes/duplicate_health.py"
    duplicate.write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n@router.get('/health')\ndef health():\n    return {'ok': True}\n",
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert any(issue.code == "build.duplicate_runtime_route" for issue in issues)


def test_build_validator_accepts_manifest_static_paths_without_workspace_prefix(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Manifest Static Path Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        '{"roles":{"client":{"pages":[{"route_path":"/client","file_path":"static/client/index.html"}]}},"shared":{}}',
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.missing_static_page" for issue in issues)


def test_static_check_rejects_typescript_syntax_inside_plain_js(tmp_path: Path) -> None:
    backend_dir = tmp_path / "miniapp"
    script = backend_dir / "app/static/client/app.js"
    script.parent.mkdir(parents=True)
    script.write_text("const button = (event.target as HTMLElement).closest('button');\n", encoding="utf-8")

    runner = object.__new__(CheckRunner)
    result = runner._run_static_js_syntax_check(backend_dir)

    assert result.status == "failed"
    assert "Static JavaScript syntax check failed" in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_neutral_role_template(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Neutral Role Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/specialist/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.neutral_role_template" in "\n".join(result.logs)
    assert "specialist" in result.diagnostics["role_coverage"]


def test_agentic_generated_app_tests_are_required(tmp_path: Path) -> None:
    runner = object.__new__(CheckRunner)
    backend_dir = tmp_path / "miniapp"
    (backend_dir / "app").mkdir(parents=True)

    python_result = runner._run_python_app_tests(backend_dir, require_present=True)
    js_result = runner._run_js_app_tests(backend_dir, require_present=True)

    assert python_result.status == "failed"
    assert js_result.status == "failed"
    assert python_result.diagnostics["missing_test_file"] == "tests/test_generated_app.py"
    assert js_result.diagnostics["missing_test_file"] == "tests/generated_app.test.mjs"


def test_generated_js_test_double_miniapp_path_is_diagnostic() -> None:
    logs = [
        "Error: ENOENT: no such file or directory",
        "/tmp/workspace/source/miniapp/miniapp/app/static/client/index.html",
    ]

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(logs)

    assert diagnostics["js_test_path_root"]["problem"] == "generated_js_test_prefixed_miniapp_twice"
    assert "cwd=miniapp" in diagnostics["js_test_path_root"]["expected_root"]


def test_generated_tests_explain_server_html_vs_js_rendered_content() -> None:
    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "AssertionError: 'Waterproof jacket' not found in '<!doctype html>...'",
            "assert(html.includes(\"Packing cubes\"))",
            'TypeError [ERR_INVALID_ARG_TYPE]: The "paths[0]" argument must be of type string. Received an instance of URL',
        ]
    )

    assert diagnostics["server_rendered_html_assertion"]["problem"] == "test_asserts_js_rendered_text_in_server_html"
    assert "TestClient sees HTML before browser JavaScript runs" in diagnostics["server_rendered_html_assertion"]["expected_scope"]
    assert diagnostics["static_html_assertion"]["problem"] == "js_test_asserts_dynamic_text_only_in_html"
    assert diagnostics["js_test_url_path_api"]["problem"] == "generated_js_test_passed_url_to_path_api"
    assert "fileURLToPath" in diagnostics["js_test_url_path_api"]["expected_path_api"]


def test_preview_ready_probes_all_role_roots(monkeypatch) -> None:
    probed: list[str] = []
    service = object.__new__(PreviewService)

    monkeypatch.setattr(PreviewService, "_probe_base_urls", staticmethod(lambda _url: ["http://preview.local"]))
    monkeypatch.setattr(PreviewService, "_probe_http", staticmethod(lambda url: probed.append(url) or True))

    assert service._http_preview_ready("http://preview.local")
    assert probed == [
        "http://preview.local/health",
        "http://preview.local/client",
        "http://preview.local/specialist",
        "http://preview.local/manager",
    ]


def test_connectivity_validator_accepts_api_routes_declared_inside_role_module(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "fetch('/api/products');\nfetch('/api/orders');\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/client.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/client')\n"
        "api = APIRouter(prefix='/api')\n\n"
        "@api.get('/products')\n"
        "def products():\n"
        "    return []\n\n"
        "@api.post('/orders')\n"
        "def orders():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_accepts_base_api_constant_when_child_routes_exist(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="API Base Constant Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "const API_BASE = '/api/study-sprint';\nfetch(`${API_BASE}/topics`);\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/study_sprint.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/study-sprint')\n\n"
        "@router.get('/topics')\n"
        "def topics():\n"
        "    return []\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_flags_missing_nested_api_route(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Nested API Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "fetch(`/api/client/bookings/${bookingId}`, { method: 'DELETE' });\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/studio.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/api/client/bookings')\n"
        "def bookings():\n"
        "    return []\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert any(
        issue.code == "connectivity.missing_backend_route"
        and "/api/client/bookings/{param}" in issue.message
        for issue in issues
    )
