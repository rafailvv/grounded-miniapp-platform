from __future__ import annotations

from pathlib import Path

from app.main import create_app
from app.models.domain import WorkspaceRecord
from app.services.check_runner import CheckRunner
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
