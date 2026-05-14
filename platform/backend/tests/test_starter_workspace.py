from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.starter_workspace_service import BLOOM_STARTER_WORKSPACE_ID


def test_bloom_starter_workspace_bootstraps_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_STARTER_WORKSPACE", "1")
    monkeypatch.setenv("PREVIEW_RUNTIME_MODE", "local")

    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    workspaces = client.get("/workspaces").json()
    assert [item["workspace_id"] for item in workspaces] == [BLOOM_STARTER_WORKSPACE_ID]
    assert workspaces[0]["name"] == "Bloom Atelier - цветочный магазин"

    source_dir = Path(workspaces[0]["path"]) / "source"
    assert (source_dir / "miniapp/app/routes/flower_shop.py").exists()
    assert (source_dir / "miniapp/app/static/assets/bouquets/bouquet-1.jpg").exists()

    runs = client.get(f"/workspaces/{BLOOM_STARTER_WORKSPACE_ID}/runs").json()
    assert len(runs) == 3
    assert {run["status"] for run in runs} == {"completed"}
    assert {run["apply_status"] for run in runs} == {"applied"}
    assert all(run["token_usage"]["total_tokens"] > 0 for run in runs)

    logs = client.get(f"/workspaces/{BLOOM_STARTER_WORKSPACE_ID}/logs").json()
    assert logs["preview"]["mini_app_logs"]
    assert any("Bloom API mounted" in line for line in logs["preview"]["mini_app_logs"])


def test_bloom_starter_workspace_is_skipped_for_explicit_test_data_dir(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    assert client.get("/workspaces").json() == []
