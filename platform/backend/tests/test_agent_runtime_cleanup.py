from __future__ import annotations

from pathlib import Path


def test_removed_generation_runtime_packages_do_not_exist() -> None:
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
        Path("platform/backend/app/services/run_service.py"),
        Path("platform/backend/app/services/workspace_service.py"),
        Path("platform/backend/app/services/preview_service.py"),
        Path("platform/backend/app/services/" + "agent_" + "loop_" + "engine.py"),
        Path("platform/backend/app/services/workspace_log_service.py"),
        Path("platform/backend/app/services/runtime_manager.py"),
        Path("platform/backend/app/modules/miniapp_agent_loop/engine.py"),
        Path("platform/backend/app/models/" + "grounded_" + "spec.py"),
        Path("platform/backend/app/models/" + "app_" + "ir.py"),
        Path("platform/backend/app/validators/" + "grounded_" + "spec_validator.py"),
        Path("platform/backend/app/validators/" + "app_" + "ir_validator.py"),
        Path("platform/backend/app/ai/" + "open" + "router" + "_client.py"),
        Path("contracts/" + "grounded-" + "spec.v1.json"),
        Path("contracts/" + "app-" + "ir.v1.json"),
        Path("platform/backend/app/models/" + "leg" + "acy_" + "state_" + "migr" + "ation.py"),
    ]

    assert [path for path in deleted_paths if (repo_root / path).exists()] == []


def test_active_sources_do_not_reference_retired_generation_tokens() -> None:
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
        "Workspace" + "Loop" + "Turn" + "Runner",
        "Workspace" + "Loop",
        "work" + "space_" + "loop",
        "Agent" + "Loop" + "Engine",
        "agent_" + "loop_" + "engine",
        "prompt_" + "alignment",
        "Draft" + "File" + "Operation",
        "fix_" + "attempts",
        "scope_" + "expansions",
        "draft_" + "actions",
        "draft_" + "patch_history_ref",
        "com" + "merce",
        "bak" + "ery",
        "ca" + "rt",
        "book" + "ing",
        "les" + "son",
        "appoint" + "ment",
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
