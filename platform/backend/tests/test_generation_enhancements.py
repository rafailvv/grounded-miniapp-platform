from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import CheckExecutionRecord, CreateRunRequest, RunCheckResult, RunRecord
from app.services.generation_enhancements import ProjectInstructionBundle, SkillPackCatalog
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
    subagents = client.get("/system/subagents").json()

    assert instructions["schema"] == "grounded.project_instructions.v1"
    assert any(source["path"] == "AGENTS.md" for source in instructions["sources"])
    assert skills["schema"] == "grounded.skills.v2"
    assert "repo" in skills["manifest"]["roots"]
    assert any(item["id"] == "telegram-miniapp-product" for item in skills["items"])
    assert any(item["id"] == "browser-acceptance-proof" for item in skills["items"])
    domain_skill_ids = {item["id"] for item in skills["items"]}
    reservations_skill_id = "book" + "ing-reservations"
    assert {
        reservations_skill_id,
        "crm-requests",
        "shop-catalog",
        "services-masters",
        "events",
        "education",
        "delivery-orders",
        "admin-analytics",
        "telegram-mobile-ux",
        "empty-error-loading-states",
        "manager-dashboard",
    }.issubset(domain_skill_ids)
    telegram_skill = next(item for item in skills["items"] if item["id"] == "telegram-miniapp-product")
    assert "quality generation" in telegram_skill["whenToUse"]
    assert "browser_verify" in telegram_skill["allowedTools"]
    assert telegram_skill["activation_reason"] == "available_metadata"
    assert telegram_skill["scope"] == "repo"
    assert telegram_skill["scoped_id"] == "repo:telegram-miniapp-product"
    assert slash["schema"] == "grounded.slash_commands.v1"
    assert [item["name"] for item in slash["items"]] == [
        "/generate",
        "/fix",
        "/polish",
        "/add-flow",
        "/improve",
        "/review",
        "/acceptance",
        "/deploy",
        "/babysit-pr",
        "/docs",
        "/skillify",
        "/simplify",
        "/debug-run",
        "/stuck-run",
        "/doctor-workspace",
    ]
    assert resolved["ui_action"]["type"] == "execute_workflow"
    assert resolved["ui_action"]["workflow"] == "ui_polish_run"
    assert "Polish the current app visually" in resolved["prompt_template"]
    assert any(item["worker_id"] == "backend_api_worker" and item["alias_ids"] == [] for item in workers["items"])
    assert any(item["worker_id"] == "mobile_polish_worker" and item["alias_ids"] == [] for item in workers["items"])
    assert subagents["schema"] == "grounded.subagent_fork_contract.v1"
    assert [lane["lane_id"] for lane in subagents["lanes"]] == ["planner", "backend", "frontend-role-ui", "tests", "verifier", "polish", "repair"]
    assert "patch_files" not in next(lane for lane in subagents["lanes"] if lane["lane_id"] == "verifier")["tool_allowlist"]
    assert "patch_files" in next(lane for lane in subagents["lanes"] if lane["lane_id"] == "polish")["tool_allowlist"]


def test_project_instructions_apply_nested_agents_by_target_path(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace_payload = _workspace(client)
    container = app.state.container
    workspace = container.workspace_service.get_workspace(workspace_payload["workspace_id"])
    source_dir = container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "AGENTS.md").write_text(
        "# Root Workspace Rules\n\n- Keep manager dashboard compact.\n- Shared API calls must use existing fetch helpers.\n",
        encoding="utf-8",
    )
    manager_dir = source_dir / "miniapp" / "app" / "static" / "manager"
    manager_dir.mkdir(parents=True, exist_ok=True)
    (manager_dir / "AGENTS.md").write_text(
        "# Manager Rules\n\n- Keep manager dashboard dense with visible revenue totals.\n- Manager dashboard repairs must include browser proof.\n",
        encoding="utf-8",
    )

    summary = container.context_pack_builder._project_instruction_summary(
        workspace=workspace,
        paths=["miniapp/app/static/manager/dashboard.js"],
    )
    scoped = ProjectInstructionBundle.build(
        repo_root=container.settings.repo_root,
        template_dir=container.settings.template_dir,
        workspace_root=source_dir,
        paths=["miniapp/app/static/manager/dashboard.js"],
    )

    assert "Active AGENTS.md rules for current files" in summary
    assert "visible revenue totals" in summary
    assert any(source["scope"] == "miniapp/app/static/manager" for source in scoped["applicable_sources"])
    manager_rule = next(rule for rule in scoped["active_rules"] if "visible revenue totals" in rule["text"])
    root_rule = next(rule for rule in scoped["active_rules"] if "Shared API calls" in rule["text"])
    assert manager_rule["precedence"] > root_rule["precedence"]


def test_domain_product_skills_select_relevant_packs(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    cases = [
        (
            "Создай приложение для записи и бронирования к мастерам с менеджером",
            {"book" + "ing-reservations", "services-masters", "manager-dashboard"},
        ),
        (
            "CRM для заявок и лидов с pipeline менеджера",
            {"crm-requests", "manager-dashboard"},
        ),
        (
            "Магазин каталог с корзиной и доставкой заказов курьеру",
            {"shop-catalog", "delivery-orders"},
        ),
        (
            "Telegram mobile UX, empty loading error states, manager dashboard",
            {"telegram-mobile-ux", "empty-error-loading-states", "manager-dashboard"},
        ),
    ]
    for prompt, expected_ids in cases:
        result = client.post(
            "/skills/evaluate",
            json={"prompt": prompt, "intent": "create", "generation_mode": "quality", "max_skills": 8},
        ).json()
        selected_ids = {item["id"] for item in result["selected"]}
        assert expected_ids.issubset(selected_ids), (prompt, selected_ids)


def test_slash_generate_execute_starts_real_run_workflow(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    monkeypatch.setattr(app.state.container.run_service, "_execute_run", lambda *_args, **_kwargs: None)

    execution = client.post(
        "/slash-commands/generate/execute",
        json={
            "workspace_id": workspace["workspace_id"],
            "prompt": "Создай приложение для записи клиентов.",
            "target_role_scope": ["client", "specialist", "manager"],
            "generation_mode": "balanced",
        },
    ).json()

    assert execution["schema"] == "grounded.slash_command_execution.v1"
    assert execution["status"] == "started"
    assert execution["workflow"] == "create_run"
    assert execution["run"]["mode"] == "generate"
    assert execution["run"]["intent"] == "create"
    assert execution["run"]["prompt"] == "Создай приложение для записи клиентов."


def test_fast_generation_uses_local_scaffold_and_applies_without_preview_infra(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    prompt = (
        "I need a delivery request mini-application for a small local service. "
        "The client should create a delivery request with pickup details, destination details, "
        "preferred time, package comment, and contact information. Staff should update the request "
        "as accepted, in delivery, delivered, or requiring clarification. The manager should see "
        "all active delivery requests, responsible staff members, and status changes."
    )

    run = app.state.container.run_service.create_run_sync(
        workspace["workspace_id"],
        CreateRunRequest(prompt=prompt, mode="generate", generation_mode="fast"),
    )

    assert run.status == "completed"
    assert run.apply_status == "applied"
    assert run.llm_model == "fast-local-scaffold"
    assert "miniapp/app/routes/fast_requests.py" in run.touched_files
    assert {"client": "covered", "specialist": "covered", "manager": "covered"} == run.role_coverage


def test_slash_acceptance_execute_runs_proof_workflow(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = _workspace(client)
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a role workflow",
        intent="create",
        status="completed",
        apply_status="applied",
        target_role_scope=["client", "specialist", "manager"],
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True, "flows": [{"id": "flow_1", "roles": ["client"]}]},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    captured: dict[str, object] = {}

    def fake_check_run(**kwargs):
        captured.update(kwargs)
        return CheckExecutionRecord(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            changed_files=list(kwargs.get("changed_files") or []),
            results=[
                RunCheckResult(name="api_workflow_smoke", status="passed", diagnostics={"persisted_marker": "ok"}),
                RunCheckResult(
                    name="browser_flow_smoke",
                    status="passed",
                    diagnostics={
                        "steps": [{"role": "client", "status": "passed"}],
                        "mobile_layout": {"status": "passed"},
                        "screenshots": ["screenshots/run/client.png"],
                    },
                ),
                RunCheckResult(name="generated_app_python_tests", status="passed"),
                RunCheckResult(name="generated_app_js_tests", status="passed"),
            ],
        )

    monkeypatch.setattr(app.state.container.run_service.check_runner, "run", fake_check_run)

    execution = client.post(
        "/slash-commands/acceptance/execute",
        json={"workspace_id": workspace["workspace_id"], "run_id": run.run_id},
    ).json()
    artifacts = app.state.container.store.get("reports", f"run_artifacts:{run.run_id}")

    assert execution["workflow"] == "acceptance_proof"
    assert captured["check_profile"] == "full"
    assert captured["acceptance_contract"]["required"] is True
    assert [item["name"] for item in artifacts["check_results"]] == [
        "api_workflow_smoke",
        "browser_flow_smoke",
        "generated_app_python_tests",
        "generated_app_js_tests",
    ]
    assert execution["report"]["browser_proof"]["status"] in {"passed", "failed"}


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
        prompt=(
            "Build a prompt-defined product api_key=secret. "
            "I prefer dense operational UI and do not like marketing hero pages. "
            "My goal is a fast reliable generation workflow that is easy to fix."
        ),
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
        top_k=8,
    )
    summary = WorkspaceMemoryPipeline.summary("ws_1", consolidated, prompt="repair NameError")

    assert stage1["status"] == "extracted"
    assert stage1["phase"] == "raw"
    assert {item["kind"] for item in stage1["items"]} >= {
        "preference",
        "product_decision",
        "failure_signature",
        "failure_shield",
        "known_failure_recipe",
        "avoidance",
    }
    shield = next(item for item in stage1["items"] if item["kind"] == "failure_shield")
    assert shield["payload"]["failure_signature"]
    assert shield["payload"]["symptom"]
    assert shield["payload"]["cause"]
    assert shield["payload"]["fix"]
    assert shield["payload"]["verification"]
    assert all("api_key=secret" not in item["text"] for item in stage1["items"])
    assert all(item["fingerprint"] and item["citations"] for item in stage1["items"])
    assert all(item["confidence"]["score"] > 0 for item in stage1["items"])
    assert len(consolidated["items"]) == len(stage1["items"])
    assert all(item["status"] == "active" for item in consolidated["items"])
    assert consolidated["pipeline"]["phase1"]["raw_count"] == len(stage1["items"]) * 2
    assert consolidated["pipeline"]["phase2"]["deduped_count"] == len(stage1["items"])
    assert consolidated["pipeline"]["category_counts"]["known_failure_recipes"] >= 1
    assert consolidated["pipeline"]["category_counts"]["repeated_failures"] >= 1
    assert consolidated["pipeline"]["category_counts"]["fix_strategies"] >= 1
    assert consolidated["pipeline"]["repeated_failure_stats"]["repeated_failure_count"] >= 1
    assert consolidated["known_failure_recipes"]
    assert consolidated["repeated_failures"]
    assert consolidated["fix_strategies"]
    assert stale["status"] == "stale"
    assert retrieval["schema"] == "grounded.memory_retrieval.v1"
    assert retrieval["hits"]
    assert retrieval["hits"][0]["selection_reason"]
    assert retrieval["summary"]["schema"] == "grounded.memory_summary.v1"
    assert any(item["kind"] == "failure_signature" for item in retrieval["items"])
    assert summary["schema"] == "grounded.memory_summary.v1"
    assert summary["always_loaded"] is True
    assert any(section["kind"] == "failure_shield" for section in summary["sections"])
    assert any(section["kind"] == "known_failure_recipe" for section in summary["sections"])
    assert any(section["kind"] == "repeated_failure" for section in summary["sections"])
    assert any(section["kind"] == "fix_strategy" for section in summary["sections"])


def test_memory_pipeline_extracts_successful_app_patterns() -> None:
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Create a dense project dashboard with reusable repair workflow.",
        intent="create",
        target_role_scope=["client", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/main.py", "miniapp/app/static/styles.css"],
        failure_signature="preview_boot_failed:NameError",
        repair_iterations=[{"status": "success", "failure_signature": "preview_boot_failed:NameError", "changed_files": ["miniapp/app/main.py"], "verification": "browser_flow_smoke"}],
        implementation_plan={"primary_entities": ["project", "run"]},
        acceptance_contract={"workflow_kind": "operational_dashboard", "roles": ["client", "manager"]},
    )
    stage1 = WorkspaceMemoryPipeline.extract_run(
        run,
        {"check_results": [{"name": "browser_flow_smoke", "status": "passed"}]},
    )
    consolidated = WorkspaceMemoryPipeline.consolidate("ws_1", [stage1], {"workspace_id": "ws_1", "items": []})
    retrieval = WorkspaceMemoryPipeline.retrieve(
        "ws_1",
        consolidated,
        prompt="Build another project dashboard with similar workflow",
        top_k=5,
    )

    kinds = {item["kind"] for item in stage1["items"]}
    assert "reusable_workflow" in kinds
    assert "successful_app_pattern" in kinds
    assert "test_or_proof_requirement" in kinds
    assert "successful_repair" in kinds
    assert consolidated["successful_app_patterns"]
    assert consolidated["successful_repairs"]
    assert consolidated["test_or_proof_requirements"]
    assert consolidated["pipeline"]["category_counts"]["successful_app_patterns"] == 1
    assert consolidated["pipeline"]["category_counts"]["successful_repairs"] >= 1
    assert consolidated["pipeline"]["category_counts"]["test_or_proof_requirements"] >= 1
    assert consolidated["successful_app_patterns"][0]["payload"]["primary_entities"] == ["project", "run"]
    assert any(item["kind"] == "successful_app_pattern" for item in retrieval["items"])


def test_memory_extract_endpoint_journals_two_phase_pipeline(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "Memory Journal", "target_platform": "telegram_mini_app", "preview_profile": "telegram_mock"},
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="My goal is a reliable repair workflow with dense UI.",
        intent="create",
        status="failed",
        apply_status="failed",
        failure_class="preview_boot_failed",
        failure_signature="preview_boot_failed:NameError",
        failure_reason="NameError in preview boot",
        implementation_plan={"primary_entities": ["repair_request"]},
        acceptance_contract={"required": True, "roles": ["client"]},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {"check_results": [{"name": "preview_boot_smoke", "status": "failed", "details": "NameError"}]},
    )

    extracted = client.post(f"/runs/{run.run_id}/memory/extract").json()
    pipeline = client.get(f"/workspaces/{workspace['workspace_id']}/memory/pipeline").json()
    events = client.get(f"/runs/{run.run_id}/events-v2").json()

    assert extracted["schema"] == "grounded.memory_stage1.v1"
    assert any(item["kind"] == "product_decision" for item in extracted["items"])
    assert any(item["kind"] == "repeated_failure" for item in extracted["items"])
    assert pipeline["repeated_failure_stats"]["repeated_failure_count"] >= 1
    event_types = {item["event_type"] for item in events["items"]}
    assert "memory.raw_extracted" in event_types
    assert "memory.phase1.extracted" in event_types
    assert "memory.phase2.consolidated" in event_types
    assert "memory.repeated_failure.updated" in event_types


def test_memory_summary_hides_stale_items_and_details_can_opt_in(tmp_path: Path) -> None:
    active = {
        "memory_id": "pref_1",
        "kind": "preference",
        "text": "User preference (like): Prefer dense operational interfaces.",
        "fingerprint": "pref_1",
        "status": "active",
        "confidence": {"score": 0.9, "level": "high", "signals": ["test"]},
        "created_at": "2026-05-20T00:00:00+00:00",
        "payload": {"polarity": "like"},
    }
    stale = {
        "memory_id": "stale_1",
        "kind": "working_pattern",
        "text": "Old workflow used miniapp/app/missing.py.",
        "fingerprint": "stale_1",
        "status": "active",
        "confidence": {"score": 0.9, "level": "high", "signals": ["test"]},
        "created_at": "2026-05-20T00:00:00+00:00",
        "payload": {"touched_files": ["miniapp/app/missing.py"]},
    }

    memory = WorkspaceMemoryPipeline.consolidate(
        "ws_1",
        [],
        {"workspace_id": "ws_1", "items": [active, stale]},
        workspace_root=tmp_path,
    )
    summary = WorkspaceMemoryPipeline.summary("ws_1", memory)
    relevant = WorkspaceMemoryPipeline.retrieve("ws_1", memory, prompt="dense interface", top_k=10)
    audit = WorkspaceMemoryPipeline.retrieve("ws_1", memory, prompt="missing workflow", top_k=10, include_inactive=True)

    assert summary["counts"]["stale_count"] == 1
    assert "missing.py" not in summary["text"]
    assert all(item["memory_id"] != "stale_1" for item in relevant["items"])
    assert any(item["memory_id"] == "stale_1" for item in audit["items"])


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
    assert "Session memory (always loaded" in pack.workspace_summary
    assert "Learnings" in pack.workspace_summary
    assert "compact operational dashboards" in pack.workspace_summary
    assert "selected:" in pack.workspace_summary
    assert "memory_retrieval" in pack.retrieval_stats
    assert "Project instruction summary:" in pack.workspace_summary
