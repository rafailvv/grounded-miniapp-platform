from __future__ import annotations

from pathlib import Path

from app.main import create_app
from app.models.domain import WorkspaceRecord


def _new_workspace(app, tmp_path: Path) -> str:
    workspace = WorkspaceRecord(
        name="Agent Prompt Workspace",
        description="Prompt alignment smoke test",
        path=str(tmp_path / "workspace"),
    )
    created = app.state.container.workspace_service.create_workspace(workspace)
    app.state.container.workspace_service.clone_template(created.workspace_id)
    return created.workspace_id


def test_commerce_prompt_requires_commerce_surface_not_generic_queue(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime
    workspace_id = _new_workspace(app, tmp_path)
    run_id = "run_alignment_bad"
    draft = app.state.container.workspace_service.prepare_draft(workspace_id, run_id)
    target = draft / "miniapp/app/static/client/app.js"
    target.write_text("const view = 'intake queue'; fetch('/api/intake');\n", encoding="utf-8")

    result = runtime._prompt_alignment_smoke(
        workspace_id=workspace_id,
        run_id=run_id,
        prompt="Создай интернет магазин с каталогом товаров и корзиной",
    )

    assert result.status == "failed"
    assert any("Commerce prompt" in line for line in result.logs)


def test_commerce_prompt_accepts_catalog_cart_order_surface(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime
    workspace_id = _new_workspace(app, tmp_path)
    run_id = "run_alignment_good"
    draft = app.state.container.workspace_service.prepare_draft(workspace_id, run_id)
    target = draft / "miniapp/app/static/client/app.js"
    target.write_text("const commerce = ['product', 'catalog', 'cart', 'order'];\n", encoding="utf-8")

    result = runtime._prompt_alignment_smoke(
        workspace_id=workspace_id,
        run_id=run_id,
        prompt="Create an online store with catalog, products, cart, and orders",
    )

    assert result.status == "passed"
