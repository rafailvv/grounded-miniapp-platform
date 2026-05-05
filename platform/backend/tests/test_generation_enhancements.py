from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.services.generation_enhancements import SkillPackCatalog
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.trace_bundle import TraceBundleWriter


def _workspace(client: TestClient) -> dict:
    return client.post(
        "/workspaces",
        json={
            "name": "Enhancement Workspace",
            "description": "Workbench enhancement coverage",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()


def test_project_instructions_skills_slash_commands_and_worker_roles(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    instructions = client.get("/system/project-instructions").json()
    skills = client.get("/skills").json()
    slash = client.get("/slash-commands").json()
    resolved = client.post("/slash-commands/polish/resolve", json={"prompt": "make it cleaner"}).json()
    workers = client.get("/system/worker-roles").json()

    assert instructions["schema"] == "grounded.project_instructions.v1"
    assert any(source["path"] == "AGENTS.md" for source in instructions["sources"])
    assert skills["schema"] == "grounded.skills.v1"
    assert any(item["id"] == "telegram-miniapp-product" for item in skills["items"])
    assert any(item["id"] == "browser-acceptance-proof" for item in skills["items"])
    telegram_skill = next(item for item in skills["items"] if item["id"] == "telegram-miniapp-product")
    assert "quality generation" in telegram_skill["whenToUse"]
    assert "browser_verify" in telegram_skill["allowedTools"]
    assert telegram_skill["activation_reason"] == "available_metadata"
    assert slash["schema"] == "grounded.slash_commands.v1"
    assert any(item["name"] == "/visual-qa" for item in slash["items"])
    assert resolved["ui_action"]["type"] == "submit_composer_with_prompt"
    assert "Polish the current app visually" in resolved["prompt_template"]
    assert any(item["worker_id"] == "planner" for item in workers["items"])
    assert any(item["worker_id"] == "verifier" for item in workers["items"])


def test_trace_bundle_writes_payloads_and_reduces_state(tmp_path: Path) -> None:
    writer = TraceBundleWriter(root=tmp_path, workspace_id="ws_1", run_id="run_1")

    writer.record("planning", {"message": "Plan ready", "files": ["miniapp/app/main.py"]})
    writer.record(
        "checks_completed",
        {
            "status": "failed",
            "details": {
                "status": "failed",
                "changed_files": ["miniapp/app/main.py"],
                "artifact_ref": "run_artifacts:run_1",
            },
        },
    )
    state = writer.reduce()

    assert writer.manifest_path.exists()
    assert writer.trace_path.exists()
    assert writer.state_path.exists()
    assert len(list(writer.payload_dir.glob("*.json"))) == 2
    assert state["event_count"] == 2
    assert "miniapp/app/main.py" in state["changed_files"]
    assert state["blockers"]
    assert state["next_action"]["action"] == "repair"


def test_memory_pipeline_extracts_consolidates_dedupes_and_rejects_secrets(tmp_path: Path) -> None:
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Build a prompt-defined product api_key=secret",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="failed",
        apply_status="failed",
        failure_class="preview_boot_failed",
        failure_signature="preview_boot_failed:NameError",
        failure_reason="NameError without token value",
        implementation_plan={"primary_entities": ["prompt_resource"]},
        acceptance_contract={"required": True, "roles": ["client", "specialist", "manager"]},
    )
    stage1 = WorkspaceMemoryPipeline.extract_run(
        run,
        {"check_results": [{"name": "preview_boot_smoke", "status": "failed", "details": "NameError"}]},
    )
    consolidated = WorkspaceMemoryPipeline.consolidate("ws_1", [stage1, stage1], {"workspace_id": "ws_1", "items": []})
    stale = WorkspaceMemoryPipeline.stale_check(tmp_path, {"items": [{"memory_id": "m1", "text": "See miniapp/app/missing.py"}]})

    assert stage1["status"] == "extracted"
    assert {item["kind"] for item in stage1["items"]} >= {"product_decision", "failure_signature", "avoidance"}
    assert all("api_key=secret" not in item["text"] for item in stage1["items"])
    assert len(consolidated["items"]) == len(stage1["items"])
    assert all(item["status"] == "active" for item in consolidated["items"])
    assert stale["status"] == "stale"


def test_skill_frontmatter_parser_and_activation(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    skill_dir = runtime_dir / "skills" / "api-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
description: API persistence skill
whenToUse:
  - api workflow
paths:
  - miniapp/app/routes/**
allowedTools:
  - read_files
validation:
  - api_workflow_smoke
---
# API Skill

## Rules
- Keep API and tests aligned.

## Acceptance
- Smoke passes.
""",
        encoding="utf-8",
    )
    old_dir = runtime_dir / "skills" / "old-skill"
    old_dir.mkdir(parents=True)
    (old_dir / "SKILL.md").write_text("# Old Skill\n\n## Rules\n- Read first.\n", encoding="utf-8")

    skills = SkillPackCatalog.load_from_runtime(runtime_dir, tmp_path)
    selected = SkillPackCatalog.select_for_context(
        skills,
        prompt="Fix api workflow smoke failure",
        intent="edit",
        generation_mode="quality",
        paths=["miniapp/app/routes/items.py"],
        failure_class="api_workflow_smoke",
    )

    parsed = {item["id"]: item for item in skills}
    assert parsed["api-skill"]["paths"] == ["miniapp/app/routes/**"]
    assert parsed["old-skill"]["constraints"] == ["Read first."]
    assert selected[0]["id"] == "api-skill"
    assert "paths" in selected[0]["activation_reason"]


def test_acceptance_visual_trace_and_magic_docs_reports(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a prompt-defined role workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/index.html", "miniapp/app/routes/resources.py"],
        acceptance_contract={
            "required": True,
            "flows": [
                {
                    "id": "role-resource-flow",
                    "title": "Prompt resource is handled by assigned roles",
                    "roles": ["client", "specialist", "manager"],
                }
            ],
        },
        browser_flow_proof={"status": "passed", "steps": [{"role": "client", "status": "passed"}]},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/routes/resources.py b/miniapp/app/routes/resources.py\n",
            "check_results": [
                {"name": "api_workflow_smoke", "status": "passed", "details": "ok"},
                {"name": "browser_flow_smoke", "status": "passed", "details": "ok", "diagnostics": {"mobile_layout": {"status": "passed"}}},
            ],
            "browser_proof_steps": [{"role": "client", "status": "passed"}],
        },
    )

    scenarios = client.get(f"/runs/{run.run_id}/acceptance-scenarios").json()
    visual = client.get(f"/runs/{run.run_id}/visual-qa").json()
    trace = client.get(f"/runs/{run.run_id}/trace-reducer").json()
    matrix = client.get(f"/runs/{run.run_id}/test-matrix").json()
    magic = client.post(f"/workspaces/{workspace['workspace_id']}/magic-docs/product-architecture").json()

    assert scenarios["schema"] == "grounded.acceptance_scenarios.v1"
    assert scenarios["items"][0]["scenario_id"] == "role-resource-flow"
    assert scenarios["items"][0]["status"] == "proved"
    assert visual["schema"] == "grounded.visual_qa.v1"
    assert visual["viewports"] == [{"width": 360}, {"width": 390}, {"width": 430}]
    assert trace["schema"] == "grounded.trace_reducer.v1"
    assert trace["quality_signals"]["has_diff"] is True
    assert any(item["key"] == "acceptance_scenarios" for item in matrix["items"])
    assert magic["schema"] == "grounded.magic_doc.v1"
    assert magic["write_status"] == "written"
    assert (app.state.container.workspace_service.source_dir(workspace["workspace_id"]) / "docs/product-architecture.md").exists()


def test_context_pack_includes_workspace_memory_and_instruction_summary(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace_payload = _workspace(client)
    container = app.state.container
    workspace = container.workspace_service.get_workspace(workspace_payload["workspace_id"])
    container.workbench_service.upsert_memory(
        workspace.workspace_id,
        {"kind": "project_rule", "text": "Use compact operational dashboards for this workspace."},
    )
    container.code_index_service.index_workspace(workspace, container.workspace_service.source_dir(workspace.workspace_id))

    pack = container.context_pack_builder.build(
        workspace=workspace,
        prompt="Add a prompt-defined status dashboard",
        model_profile="test",
        generation_mode="balanced",
    )

    assert "Workspace memory:" in pack.workspace_summary
    assert "compact operational dashboards" in pack.workspace_summary
    assert "Project instruction summary:" in pack.workspace_summary
