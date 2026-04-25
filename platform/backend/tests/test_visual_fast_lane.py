from __future__ import annotations

from pathlib import Path


def test_legacy_visual_lane_module_is_removed() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    legacy_path = repo_root / "platform/backend/app/modules" / ("miniapp_visual" + "_patch_fast_lane.py")

    assert not legacy_path.exists()


def test_visual_changes_are_handled_by_agent_runtime(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime

    assert runtime._run_progress_for_event("agent_turn_started") == ("agent_turn", 48)
    assert runtime._run_progress_for_event("patch_apply_started") == ("applying", 70)
