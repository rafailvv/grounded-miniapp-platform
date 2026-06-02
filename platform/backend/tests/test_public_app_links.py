from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.core.config import get_settings
from app.models.domain import PreviewRecord
from app.services.workspace.preview_service import PreviewService
from app.api.routes_public_apps import _rewrite_public_paths


def test_public_role_urls_use_configured_base(tmp_path: Path) -> None:
    settings = get_settings(repo_root=Path.cwd(), data_dir=tmp_path)
    settings = replace(settings, public_miniapp_base_url="https://aistudio.upmini.app/apps")
    service = PreviewService(
        settings=settings,
        store=None,  # type: ignore[arg-type]
        workspace_service=None,  # type: ignore[arg-type]
        runtime_manager=None,  # type: ignore[arg-type]
        workspace_log_service=None,  # type: ignore[arg-type]
    )
    preview = PreviewRecord(workspace_id="ws_123", status="running", url="http://localhost:16010")

    assert service.public_app_url("ws_123", preview) == "https://aistudio.upmini.app/apps/ws_123"
    assert service.public_role_urls("ws_123", preview) == {
        "client": "https://aistudio.upmini.app/apps/ws_123/client",
        "specialist": "https://aistudio.upmini.app/apps/ws_123/specialist",
        "manager": "https://aistudio.upmini.app/apps/ws_123/manager",
    }


def test_public_proxy_rewrites_absolute_miniapp_paths() -> None:
    html = '''
      <link href="/static/client/styles.css">
      <script src="/static/client/app.js"></script>
      <form action="/api/orders"></form>
      <script>fetch("/api/orders"); location.href = "/manager";</script>
    '''

    rewritten = _rewrite_public_paths(html, "/apps/ws_123")

    assert 'href="/apps/ws_123/static/client/styles.css"' in rewritten
    assert 'src="/apps/ws_123/static/client/app.js"' in rewritten
    assert 'action="/apps/ws_123/api/orders"' in rewritten
    assert 'fetch("/apps/ws_123/api/orders")' in rewritten
    assert '"/apps/ws_123/manager"' in rewritten
