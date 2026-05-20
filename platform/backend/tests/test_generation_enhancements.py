from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.services.generation_enhancements import SkillPackCatalog
from app.services.skill_registry import SkillRegistryService
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
    assert skills["schema"] == "grounded.skills.v2"
    assert "repo" in skills["manifest"]["roots"]
    assert any(item["id"] == "telegram-miniapp-product" for item in skills["items"])
    assert any(item["id"] == "browser-acceptance-proof" for item in skills["items"])
    telegram_skill = next(item for item in skills["items"] if item["id"] == "telegram-miniapp-product")
    assert "quality generation" in telegram_skill["whenToUse"]
    assert "browser_verify" in telegram_skill["allowedTools"]
    assert telegram_skill["activation_reason"] == "available_metadata"
    assert telegram_skill["scope"] == "repo"
    assert telegram_skill["scoped_id"] == "repo:telegram-miniapp-product"
    assert slash["schema"] == "grounded.slash_commands.v1"
    assert any(item["name"] == "/visual-qa" for item in slash["items"])
    assert resolved["ui_action"]["type"] == "submit_composer_with_prompt"
    assert "Polish the current app visually" in resolved["prompt_template"]
    assert any(item["worker_id"] == "backend_api_worker" and item["alias_ids"] == [] for item in workers["items"])
    assert any(item["worker_id"] == "mobile_polish_worker" and item["alias_ids"] == [] for item in workers["items"])


def test_trace_bundle_writes_payloads_and_reduces_state(tmp_path: Path) -> None:
    writer = TraceBundleWriter(root=tmp_path, workspace_id="ws_1", run_id="run_1")

    writer.record("planning", {"message": "Plan ready", "files": ["miniapp/app/main.py"]})
    writer.record(
        "prompt_context_pack",
        {
            "attempt": 1,
            "tool_round": 1,
            "prompt_sha256": "abc",
            "skills": {"selected": [{"id": "mobile-ui-polish", "activation_reason": "quality", "activation_score": 2}]},
            "memory": {
                "injected": [{"source": "workspace_memory", "kind": "preference", "reason": "active_context", "text_excerpt": "Use dense UI."}],
                "skipped": [{"source": "workspace_memory", "kind": "avoidance", "reason": "secret_like_material", "text_excerpt": "api key"}],
            },
        },
    )
    writer.record("turn_diff_before", {"turn": 1, "paths": ["miniapp/app/main.py"], "file_hashes": {"miniapp/app/main.py": {"sha256": "old"}}})
    writer.record("turn_diff_after", {"turn": 1, "paths": ["miniapp/app/main.py"], "file_hashes": {"miniapp/app/main.py": {"sha256": "new"}}})
    writer.record("tool_failed_reason", {"tool": "run_command", "tool_use_id": "tool_1", "status": "failed", "error": "exit 1"})
    writer.record("final_acceptance_gate_decision", {"status": "blocked", "blocking": True, "failed_checks": ["browser_flow_smoke"]})
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
    assert len(list(writer.payload_dir.glob("*.json"))) == 7
    assert state["event_count"] == 7
    assert "miniapp/app/main.py" in state["changed_files"]
    assert state["blockers"]
    assert state["prompt_contexts"]
    assert state["skill_edges"][0]["skill_id"] == "mobile-ui-polish"
    assert any(item["reason"] == "secret_like_material" for item in state["memory_edges"])
    assert len(state["diff_edges"]) == 2
    assert state["acceptance_gate"][0]["status"] == "blocked"
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
    retrieval = WorkspaceMemoryPipeline.retrieve(
        "ws_1",
        consolidated,
        prompt="Fix preview boot NameError without repeating the old failure",
        top_k=2,
    )

    assert stage1["status"] == "extracted"
    assert stage1["phase"] == "raw"
    assert {item["kind"] for item in stage1["items"]} >= {"product_decision", "failure_signature", "avoidance"}
    assert all("api_key=secret" not in item["text"] for item in stage1["items"])
    assert all(item["fingerprint"] and item["citations"] for item in stage1["items"])
    assert all(item["confidence"]["score"] > 0 for item in stage1["items"])
    assert len(consolidated["items"]) == len(stage1["items"])
    assert all(item["status"] == "active" for item in consolidated["items"])
    assert consolidated["pipeline"]["phase1"]["raw_count"] == len(stage1["items"]) * 2
    assert consolidated["pipeline"]["phase2"]["deduped_count"] == len(stage1["items"])
    assert stale["status"] == "stale"
    assert retrieval["schema"] == "grounded.memory_retrieval.v1"
    assert retrieval["hits"]
    assert retrieval["hits"][0]["selection_reason"]
    assert any(item["kind"] == "failure_signature" for item in retrieval["items"])


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
    mobile_dir = runtime_dir / "skills" / "mobile-skill"
    mobile_dir.mkdir(parents=True)
    (mobile_dir / "SKILL.md").write_text(
        """---
description: Mobile polish skill
whenToUse:
  - mobile polish
paths:
  - miniapp/app/static/**/styles.css
validation:
  - mobile_layout
---
# Mobile Skill
""",
        encoding="utf-8",
    )

    skills = SkillPackCatalog.load_from_runtime(runtime_dir, tmp_path)
    prefetch = SkillPackCatalog.prefetch(runtime_dir, tmp_path)
    cached = SkillPackCatalog.prefetch(runtime_dir, tmp_path)
    selected = SkillPackCatalog.select_for_context(
        skills,
        prompt="Fix api workflow smoke failure",
        intent="edit",
        generation_mode="quality",
        paths=["miniapp/app/routes/items.py"],
        failure_class="api_workflow_smoke",
    )
    search = SkillPackCatalog.search_for_context(
        skills,
        prompt="Use $api-skill to fix api workflow smoke failure and mobile polish",
        intent="edit",
        generation_mode="fast",
        paths=["miniapp/app/routes/items.py", "miniapp/app/static/client/styles.css"],
        failure_class="api_workflow_smoke",
        max_skills=1,
    )
    telemetry = SkillPackCatalog.usage_telemetry(
        selected=list(search["selected"]),
        check_results=[{"name": "api_workflow_smoke", "status": "passed"}],
        run_status="completed",
    )

    parsed = {item["id"]: item for item in skills}
    assert parsed["api-skill"]["paths"] == ["miniapp/app/routes/**"]
    assert parsed["old-skill"]["constraints"] == ["Read first."]
    assert prefetch["status"] == "ready"
    assert cached["cache"]["status"] == "hit"
    assert selected[0]["id"] == "api-skill"
    assert "paths" in selected[0]["activation_reason"]
    assert search["selected"][0]["id"] == "api-skill"
    assert "explicit_mention" in search["selected"][0]["activation_reason"]
    assert any(item["reason"] == "activation_budget_exceeded" for item in search["skipped"])
    assert telemetry["items"][0]["outcome"] == "helped"


def test_scoped_skill_registry_loads_roots_dependencies_and_policy(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    repo_skill = runtime_dir / "skills" / "api-skill"
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text(
        """---
description: API repo skill
whenToUse:
  - api workflow
paths:
  - miniapp/app/routes/**
allowedTools:
  - read_files
model: gpt-test
effort: high
dependencies:
  - helper-skill
invocationPolicy: auto
---
# API Repo Skill

## Rules
- Keep API stable.
""",
        encoding="utf-8",
    )
    helper_skill = runtime_dir / "skills" / "helper-skill"
    helper_skill.mkdir(parents=True)
    (helper_skill / "SKILL.md").write_text(
        """---
description: Helper skill
invocationPolicy: explicit
---
# Helper Skill
""",
        encoding="utf-8",
    )
    plugin_root = runtime_dir / "plugins" / "demo-plugin"
    (plugin_root / "skills" / "plugin-skill").mkdir(parents=True)
    (plugin_root / "plugin.json").write_text('{"id":"demo-plugin","version":"1.0.0","capabilities":["skills"]}', encoding="utf-8")
    (plugin_root / "skills" / "plugin-skill" / "SKILL.md").write_text(
        """---
description: Plugin skill
whenToUse:
  - plugin flow
invocationPolicy: disabled
---
# Plugin Skill
""",
        encoding="utf-8",
    )
    user_skill = data_dir / "skills" / "user-skill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("# User Skill\n", encoding="utf-8")

    registry = SkillRegistryService(runtime_dir=runtime_dir, repo_root=tmp_path, data_dir=data_dir)
    prefetch = registry.prefetch()
    manifest = registry.manifest()
    search = registry.search_for_context(
        prompt="Fix api workflow",
        intent="edit",
        generation_mode="quality",
        paths=["miniapp/app/routes/items.py"],
    )
    explicit = registry.search_for_context(prompt="Use $repo:helper-skill", generation_mode="fast")

    all_items = {item["scoped_id"]: item for item in prefetch["all_items"]}
    selected_ids = [item["scoped_id"] for item in search["selected"]]
    assert prefetch["schema"] == "grounded.skill_prefetch.v2"
    assert manifest["schema"] == "grounded.skill_registry_manifest.v1"
    assert all_items["repo:api-skill"]["dependencies"][0]["id"] == "helper-skill"
    assert all_items["plugin:plugin-skill"]["plugin_id"] == "demo-plugin"
    assert all_items["plugin:plugin-skill"]["enabled"] is False
    assert "repo:api-skill" in selected_ids
    assert "repo:helper-skill" in selected_ids
    assert search["effective"]["allowedTools"] == ["read_files"]
    assert search["effective"]["model"] == "gpt-test"
    assert search["effective"]["effort"] == "high"
    assert explicit["selected"][0]["scoped_id"] == "repo:helper-skill"


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
        touched_files=["miniapp/app/static/client/index.html", "miniapp/app/routes/briefings.py"],
        acceptance_contract={
            "required": True,
            "flows": [
                {
                    "id": "role-briefing-flow",
                    "title": "Prompt briefing is handled by assigned roles",
                    "roles": ["client", "specialist", "manager"],
                    "steps": [
                        {
                            "kind": "prompt_state_source",
                            "role": "client",
                            "entity": "briefing",
                            "expectation": "Client records briefing data through app-owned UI and API.",
                        },
                        {
                            "kind": "mobile_layout",
                            "role": "all",
                            "entity": "briefing",
                            "expectation": "Role surfaces fit the mobile preview.",
                        },
                    ],
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
            "diff": "diff --git a/miniapp/app/routes/briefings.py b/miniapp/app/routes/briefings.py\n",
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
    assert scenarios["items"][0]["scenario_id"] == "role-briefing-flow"
    assert scenarios["items"][0]["status"] == "proved"
    assert visual["schema"] == "grounded.visual_qa.v1"
    assert visual["viewports"] == [{"width": 360}, {"width": 390}, {"width": 430}]
    assert trace["schema"] == "grounded.trace_reducer.v1"
    assert trace["quality_signals"]["has_diff"] is True
    assert any(item["key"] == "acceptance_scenarios" for item in matrix["items"])
    assert magic["schema"] == "grounded.magic_doc.v1"
    assert magic["write_status"] == "written"
    assert (app.state.container.workspace_service.source_dir(workspace["workspace_id"]) / "docs/product-architecture.md").exists()


def test_acceptance_scenarios_block_when_contract_missing(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a prompt-defined role workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="blocked",
        apply_status="blocked",
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    scenarios = client.get(f"/runs/{run.run_id}/acceptance-scenarios").json()

    assert scenarios["schema"] == "grounded.acceptance_scenarios.v1"
    assert scenarios["status"] == "blocked_contract_missing"
    assert scenarios["items"] == []
    assert scenarios["blocking"] is True
    assert "platform-invented" in scenarios["message"]


def test_acceptance_scenarios_without_contract_steps_fail_matrix(tmp_path: Path) -> None:
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
        acceptance_contract={
            "required": True,
            "flows": [{"id": "missing-steps", "title": "Missing prompt-derived steps", "roles": ["client"]}],
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    scenarios = client.get(f"/runs/{run.run_id}/acceptance-scenarios").json()
    matrix = client.get(f"/runs/{run.run_id}/test-matrix").json()
    acceptance_row = next(item for item in matrix["items"] if item["key"] == "acceptance_scenarios")

    assert scenarios["status"] == "blocked_contract_steps_missing"
    assert scenarios["items"][0]["blocking"] is True
    assert acceptance_row["status"] == "failed"


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
    for index in range(12):
        container.workbench_service.upsert_memory(
            workspace.workspace_id,
            {"kind": "project_rule", "text": f"Unrelated saved note {index} about checkout settings."},
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
    assert "selected:" in pack.workspace_summary
    assert "memory_retrieval" in pack.retrieval_stats
    assert "Project instruction summary:" in pack.workspace_summary
