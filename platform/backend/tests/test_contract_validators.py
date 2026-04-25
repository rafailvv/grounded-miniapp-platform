from __future__ import annotations

from pathlib import Path

from app.main import create_app
from app.models.domain import WorkspaceRecord
from app.validators.build_validator import BuildValidator


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
