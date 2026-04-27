from __future__ import annotations

from pathlib import Path


def test_deleted_generation_runtime_packages_do_not_exist() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    deleted_paths = [
        Path("platform/backend/app/modules/" + "miniapp_" + "generation" + "_runtime"),
        Path("platform/backend/app/services/" + "miniapp_" + "generation"),
        Path("platform/backend/app/modules/" + "miniapp_" + "fix" + "_runtime"),
        Path("platform/backend/app/modules/" + "miniapp_" + "visual_patch" + "_fast" + "_lane.py"),
        Path("platform/backend/app/services/" + "generation_" + "service.py"),
        Path("platform/backend/app/services/" + "fix_" + "orchestrator.py"),
        Path("platform/backend/app/services/" + "miniapp_" + "artifact_builder.py"),
        Path("platform/backend/app/services/" + "generation_" + "runtime_config.py"),
        Path("platform/backend/app/services/" + "generation_" + "tool_orchestrator.py"),
        Path("platform/backend/app/models/" + "grounded_" + "spec.py"),
        Path("platform/backend/app/models/" + "app_" + "ir.py"),
        Path("platform/backend/app/validators/" + "grounded_" + "spec_validator.py"),
        Path("platform/backend/app/validators/" + "app_" + "ir_validator.py"),
        Path("platform/backend/app/ai/" + "open" + "router" + "_client.py"),
        Path("contracts/" + "grounded-" + "spec.v1.json"),
        Path("contracts/" + "app-" + "ir.v1.json"),
    ]

    assert [path for path in deleted_paths if (repo_root / path).exists()] == []


def test_active_backend_sources_do_not_reference_legacy_generation_tokens() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    scan_roots = [
        repo_root / "platform/backend/app",
        repo_root / "platform/backend/tests",
        repo_root / "platform/frontend/src",
        repo_root / "runtime/templates",
        repo_root / "docker",
    ]
    forbidden_tokens = [
        "miniapp_" + "generation" + "_runtime",
        "miniapp_" + "generation",
        "miniapp_" + "fix" + "_runtime",
        "miniapp_" + "visual_patch" + "_fast" + "_lane",
        "Generation" + "Service",
        "Fix" + "Orchestrator",
        "workflow_" + "canonical_smoke",
        "entity_" + "contract",
        "role_" + "contract",
        "page_" + "graph",
        "grounded_" + "spec",
        "Grounded" + "Spec",
        "app_" + "ir",
        "App" + "IR",
        "code_" + "plan",
        "spec_" + "analysis",
        "ir_" + "codegen",
        "mater" + "ialization",
        "generation_" + "runtime_config",
        "generation_" + "tool_orchestrator",
        "open" + "router",
        "Open" + "Router",
    ]

    matches: list[str] = []
    ignored_parts = {"__pycache__", ".pytest_cache", "node_modules", "dist", "build"}
    for root in scan_roots:
        for path in [item for item in root.rglob("*") if item.is_file()]:
            if ignored_parts.intersection(path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden_tokens:
                if token in text:
                    matches.append(f"{path.relative_to(repo_root)}: {token}")

    assert matches == []


def test_visual_changes_are_handled_by_agent_runtime(tmp_path: Path) -> None:
    from app.main import create_app

    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    runtime = app.state.container.workspace_code_agent_runtime

    assert runtime._run_progress_for_event("agent_turn_started", details={"attempt": 1}) == ("Planning code edit 1", 24)
    assert runtime._run_progress_for_event(
        "patch_apply_started",
        details={"attempt": 1, "files": ["miniapp/app/main.py", "miniapp/app/static/client/app.js"]},
    ) == ("Applying patch • 2 files", 52)
