from __future__ import annotations

import json
from pathlib import Path
import shutil
import time
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import JobRecord, PreviewRecord, RunRecord
from app.modules.miniapp_agent_loop.agent_command_policy import AgentCommandPolicy
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.services.repair_catalog import RepairCatalog
from app.services.exec_policy_service import ExecPolicyService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, canonical_tool_name, tool_envelope
from app.services.workspace.runtime_manager import PreviewRuntimeManager


def test_tool_protocol_normalizes_aliases() -> None:
    assert canonical_tool_name("run_command") == "shell.exec"
    envelope = tool_envelope(tool="apply_patch_to_draft", input_payload={"path": "miniapp/app/main.py"})

    assert envelope["version"] == TOOL_PROTOCOL_VERSION
    assert envelope["tool"] == "patch.apply"
    assert envelope["status"] == "started"
    assert envelope["risk"] == "mutating"
    assert envelope["sandbox_profile"] == "agent_draft"
    assert "changed_files" in envelope
    assert envelope["approval"]["status"] == "not_required"


def test_exec_policy_classifies_and_redacts_commands() -> None:
    service = ExecPolicyService()

    allowed = service.evaluate_command("rg api miniapp/app")
    blocked = service.evaluate_command("rm -rf miniapp")
    redacted = service.evaluate_command("rg api_key=sk-secretvalue miniapp/app")
    redirection = service.evaluate_command("rg api miniapp/app > out.txt")
    git_internal = service.evaluate_command("ls .git")

    assert allowed["decision"]["action"] == "allow"
    assert allowed["decision"]["risk"] == "read_only"
    assert allowed["sandbox_summary"]["profile"] == "analysis_only"
    assert blocked["decision"]["action"] == "forbidden"
    assert blocked["approval"]["status"] == "blocked"
    assert redirection["decision"]["action"] == "forbidden"
    assert git_internal["decision"]["action"] == "forbidden"
    assert "sk-secretvalue" not in redacted["command"]


def test_exec_policy_loads_json_policy_and_validates_not_match_examples() -> None:
    service = ExecPolicyService(Path("runtime/policies/agent_exec_policy.json").resolve())
    snapshot = service.snapshot()

    assert snapshot["policy_file"]["status"] == "loaded"
    assert str(snapshot["policy_file"]["source"]).endswith("agent_exec_policy.codexpolicy")
    assert any("not_match" in item for item in snapshot["rules"])
    assert service.evaluate_command("python3 -m py_compile miniapp/app/main.py")["decision"]["action"] == "allow"
    assert service.evaluate_command("python3 -m pip install requests")["decision"]["action"] == "forbidden"


def test_exec_policy_dsl_severity_merge_and_matched_rules() -> None:
    policy = AgentCommandPolicy.from_dsl_text(
        """
prefix_rule(
    pattern = ["rg"],
    decision = "allow",
    justification = "read-only search",
    match = ["rg api miniapp/app"],
)
prefix_rule(
    pattern = ["rg", "secret"],
    decision = "forbidden",
    justification = "secret scans are blocked in this test policy",
    match = ["rg secret miniapp/app"],
    not_match = ["rg api miniapp/app"],
)
""",
        source="test.codexpolicy",
    )
    decision = policy.decide("rg secret miniapp/app")

    assert decision.action == "forbidden"
    assert len(decision.matched_rules) == 2
    assert decision.matched_rules[-1]["decision"] == "forbidden"
    assert all(item["status"] == "passed" for item in policy.validation_examples())


def test_exec_policy_dsl_rejects_invalid_rules_and_service_falls_back(tmp_path: Path) -> None:
    policy_path = tmp_path / "agent_exec_policy.codexpolicy"
    policy_path.write_text(
        """
prefix_rule(
    pattern = ["rg"],
    decision = "sometimes",
    justification = "invalid",
)
""",
        encoding="utf-8",
    )

    service = ExecPolicyService(policy_path)
    doctor = service.doctor_check()

    assert service.snapshot()["policy_file"]["status"] == "fallback_builtin"
    assert doctor["status"] == "failed"
    assert service.evaluate_command("rg api miniapp/app")["decision"]["action"] == "allow"


def test_exec_policy_resolves_trusted_host_executable() -> None:
    python_path = shutil.which("python3")
    assert python_path
    policy = AgentCommandPolicy.from_dsl_text(
        """
prefix_rule(
    pattern = ["python3", "-m", "py_compile"],
    decision = "allow",
    justification = "compile diagnostics",
    match = ["python3 -m py_compile miniapp/app/main.py"],
)
""",
        source="host.codexpolicy",
    )

    trusted = policy.decide(f"{python_path} -m py_compile miniapp/app/main.py")
    untrusted = policy.decide("/tmp/python3 -m py_compile miniapp/app/main.py")

    assert trusted.action == "allow"
    assert trusted.executable_resolution["status"] == "trusted_absolute"
    assert untrusted.action == "forbidden"
    assert untrusted.executable_resolution["status"] == "untrusted_absolute"


def test_policy_simulation_endpoint_returns_matched_rules(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    response = client.post("/policy/evaluate", json={"command": "python3 -m pip install requests"}).json()
    doctor = client.get("/doctor").json()

    assert response["decision"]["action"] == "forbidden"
    assert response["matched_rules"]
    assert response["selected_decision"] == "forbidden"
    assert response["policy_file"]["status"] == "loaded"
    assert any(item["name"] == "exec_policy" for item in doctor["checks"])


def test_background_tasks_crud_output_stop_retry_and_run_lane(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Task Workspace",
            "description": "background task test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="edit",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    created = client.post(
        "/tasks",
        json={
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "type": "worker_branch",
            "title": "Backend worker",
            "input": {"worker_id": "backend_api_worker"},
            "max_attempts": 2,
            "auto_start": False,
        },
    ).json()
    listed = client.get(f"/tasks?workspace_id={workspace['workspace_id']}").json()
    updated = client.patch(f"/tasks/{created['task_id']}", json={"status": "failed", "title": "Backend worker failed"}).json()
    output = client.get(f"/tasks/{created['task_id']}/output?cursor=0&limit=2").json()
    retry = client.post(f"/tasks/{created['task_id']}/retry").json()
    lane = client.get(f"/runs/{run.run_id}/tasks").json()
    stopped = client.post(f"/tasks/{created['task_id']}/stop").json()

    assert created["status"] == "queued"
    assert listed["items"][0]["task_id"] == created["task_id"]
    assert updated["status"] == "failed"
    assert output["items"][0]["event_type"] == "task_created"
    assert output["next_cursor"] == 2
    assert retry["parent_task_id"] == created["task_id"]
    assert retry["attempt"] == 2
    assert lane["items"][0]["source"] == "background"
    assert any(item["artifact_refs"]["background_task"] == created["task_id"] for item in lane["items"])
    assert stopped["status"] in {"cancelled", "failed", "stopping"}


def test_background_task_memory_consolidate_completes(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Memory Task Workspace",
            "description": "memory task test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    app.state.container.store.upsert(
        "reports",
        f"memory_stage1:{workspace['workspace_id']}:run_1",
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": "run_1",
            "items": [{"kind": "preference", "text": "Use compact operational UI.", "confidence": 0.9}],
        },
    )

    created = client.post(
        "/tasks",
        json={
            "workspace_id": workspace["workspace_id"],
            "type": "memory_consolidate",
            "title": "Consolidate memory",
        },
    ).json()
    task = _wait_for_background_task(client, created["task_id"])
    output = client.get(f"/tasks/{created['task_id']}/output").json()

    assert task["status"] == "completed"
    assert task["linked_refs"]["memory_ref"] == f"workspace_memory:{workspace['workspace_id']}"
    assert any(item["event_type"] == "task_completed" for item in output["items"])


def test_background_task_generate_product_links_created_run(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Generate Task Workspace",
            "description": "generate task test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    def fake_create_run(workspace_id: str, request: Any) -> RunRecord:
        run = RunRecord(
            workspace_id=workspace_id,
            prompt=request.prompt,
            intent="edit",
            target_role_scope=["client", "specialist", "manager"],
            model_profile="test",
        )
        app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
        return run

    app.state.container.run_service.create_run = fake_create_run

    created = client.post(
        "/tasks",
        json={
            "workspace_id": workspace["workspace_id"],
            "type": "generate_product",
            "title": "Generate product",
            "input": {"prompt": "Build a simple workflow", "intent": "edit"},
        },
    ).json()
    task = _wait_for_background_task(client, created["task_id"])

    assert task["status"] == "completed"
    assert task["run_id"]
    assert task["linked_refs"]["run_id"] == task["run_id"]


def test_workbench_public_endpoints_are_additive(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Workbench Workspace",
            "description": "Workbench endpoint test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        linked_job_id=None,
    )
    run.worker_mailbox_ref = f"worker_mailbox:{workspace['workspace_id']}:{run.run_id}"
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        run.worker_mailbox_ref,
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "mailbox": {
                "enabled": False,
                "workers": [
                    {
                        "worker_id": "backend_api_worker",
                        "worker": "backend_api_worker",
                        "worker_type": "backend_api_worker",
                        "alias_ids": [],
                        "status": "available_disabled",
                        "disabled_reason": "test gate disabled",
                    }
                ],
            },
            "worker_tasks": [],
        },
    )
    app.state.container.store.upsert(
        "reports",
        f"worker_context:{workspace['workspace_id']}:{run.run_id}:backend_api_worker",
        {"schema": "grounded.worker_context.v1", "worker_id": "backend_api_worker", "contract_slice": {}},
    )
    app.state.container.store.upsert(
        "reports",
        f"worker_memory_snapshot:{workspace['workspace_id']}:{run.run_id}:backend_api_worker",
        {"schema": "grounded.worker_memory_snapshot.v1", "worker_id": "backend_api_worker", "items": []},
    )
    app.state.container.store.upsert(
        "reports",
        f"worker_output:{workspace['workspace_id']}:{run.run_id}:backend_api_worker",
        {"schema": "grounded.worker_output.v1", "worker_id": "backend_api_worker", "status": "available_disabled", "changed_files": []},
    )
    app.state.container.store.upsert(
        "reports",
        f"worker_manager_merge_decision:{workspace['workspace_id']}:{run.run_id}",
        {"schema": "grounded.worker_manager_merge_decision.v1", "status": "empty", "decisions": []},
    )

    policy = client.get("/system/policies/exec").json()
    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/policy/evaluate-command",
        json={"command": "rg workflow miniapp/app"},
    ).json()
    timeline = client.get(f"/runs/{run.run_id}/timeline").json()
    trace = client.get(f"/runs/{run.run_id}/trace-view").json()
    trace_bundle = client.get(f"/runs/{run.run_id}/trace-bundle").json()
    trace_bundle_state = client.get(f"/runs/{run.run_id}/trace-bundle/state").json()
    protocol = client.get(f"/runs/{run.run_id}/protocol").json()
    bookmarks = client.get(f"/runs/{run.run_id}/bookmarks").json()
    compaction = client.get(f"/runs/{run.run_id}/compaction").json()
    compaction_boundaries = client.get(f"/runs/{run.run_id}/compaction/boundaries").json()
    tasks = client.get(f"/runs/{run.run_id}/tasks").json()
    run_events = client.get(f"/runs/{run.run_id}/events").json()
    doctor = client.get("/doctor").json()
    memory = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory",
        json={"kind": "project_rule", "text": "Use dense operational UI."},
    ).json()
    extracted_memory = client.post(f"/runs/{run.run_id}/memory/extract").json()
    memory_pipeline = client.get(f"/workspaces/{workspace['workspace_id']}/memory/pipeline").json()
    consolidated_memory = client.post(f"/workspaces/{workspace['workspace_id']}/memory/consolidate").json()
    skills = client.get("/skills").json()
    workers = client.get(f"/runs/{run.run_id}/workers").json()
    worker_context = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/context").json()
    worker_memory = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/memory").json()
    worker_output = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/output").json()
    worker_merge_decision = client.get(f"/runs/{run.run_id}/workers/merge-decision").json()
    permissions = client.get("/system/permissions/rules").json()
    lsp = client.get(f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp").json()
    lsp_symbols = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/symbol-context?q=app").json()
    lsp_refs = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/references?symbol=app").json()
    lsp_route_context = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/route-static-context").json()
    thread = client.post("/threads", json={"workspace_id": workspace["workspace_id"], "title": "Workbench Thread"}).json()
    thread_snapshot = client.get(f"/threads/{thread['thread_id']}").json()
    resumed_thread = client.post(f"/threads/{thread['thread_id']}/resume").json()
    patch_preflight = client.post(
        f"/workspaces/{workspace['workspace_id']}/patch/preflight",
        json={
            "ops": [
                {
                    "operation_id": "op_1",
                    "op": "update",
                    "file_path": "README.md",
                    "content": "# Updated\n",
                    "explanation": "test",
                }
            ]
        },
    ).json()

    assert policy["tool_protocol_version"] == TOOL_PROTOCOL_VERSION
    assert evaluation["decision"]["action"] == "allow"
    assert timeline["items"][0]["kind"] == "prompt"
    assert trace["reducer"]["why"] == "Build a workflow"
    assert trace_bundle["schema"] == "grounded.trace_bundle.v1"
    assert trace_bundle_state["schema"] == "grounded.trace_bundle_state.v1"
    assert protocol["schema"] == "grounded.run_protocol.v1"
    assert bookmarks["schema"] == "grounded.run_bookmarks.v1"
    assert compaction["schema"] == "grounded.run_compaction.v1"
    assert compaction_boundaries["schema"] == "grounded.run_compaction_boundaries.v1"
    assert tasks["schema"] == "grounded.run_tasks.v1"
    assert run_events["schema"] == "grounded.run_events.v1"
    assert doctor["checks"]
    assert memory["items"][0]["text"] == "Use dense operational UI."
    assert extracted_memory["schema"] == "grounded.memory_stage1.v1"
    assert memory_pipeline["schema"] == "grounded.memory_pipeline.v1"
    assert consolidated_memory["pipeline"]["schema"] == "grounded.memory_pipeline.v1"
    assert any(item["id"] == "state-workflow" for item in skills["items"])
    assert any(item["id"] == "role-surfaces" for item in skills["items"])
    assert workers["schema"] == "grounded.product_workers.v1"
    assert any(item["worker_id"] == "backend_api_worker" and item["alias_ids"] == [] for item in workers["workers"])
    assert any(item["status"] == "available_disabled" for item in workers["workers"])
    assert worker_context["schema"] == "grounded.worker_context.v1"
    assert worker_memory["schema"] == "grounded.worker_memory_snapshot.v1"
    assert worker_output["schema"] == "grounded.worker_output.v1"
    assert worker_merge_decision["schema"] == "grounded.worker_manager_merge_decision.v1"
    assert any(item["rule_id"] == "block_destructive" for item in permissions["items"])
    assert lsp["status"] in {"passed", "failed"}
    assert lsp["schema"] == "grounded.lsp_diagnostics.v1"
    assert "jump" in lsp["items"][0] if lsp["items"] else True
    assert lsp_symbols["schema"] == "grounded.lsp_symbol_context.v1"
    assert lsp_refs["schema"] == "grounded.lsp_find_references.v1"
    assert lsp_route_context["schema"] == "grounded.lsp_route_static_context.v1"
    assert thread_snapshot["thread"]["thread_id"] == thread["thread_id"]
    assert resumed_thread["status"] == "active"
    assert patch_preflight["status"] == "passed"


def test_run_events_are_persisted_by_platform_db(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Run Event Workspace",
            "description": "Run event persistence",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
    )
    job = JobRecord(
        workspace_id=workspace["workspace_id"],
        prompt=run.prompt,
        status="running",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        generation_mode="fast",
        linked_run_id=run.run_id,
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.workspace_code_agent_runtime.append_event(
        job,
        "job_started",
        "Workspace code agent started.",
        {"run_id": run.run_id},
    )

    events = client.get(f"/runs/{run.run_id}/events").json()

    assert events["items"][0]["event_type"] == "run.started"
    assert events["items"][0]["payload"]["source_event_type"] == "job_started"
    assert events["protocol_events"][0]["type"] == "run_started"
    assert events["protocol_events"][0]["source_event_type"] == "job_started"
    assert events["state_snapshots"][0]["reason"] == "job_started"


def test_run_protocol_appends_events_and_bookmarks(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Protocol Workspace",
            "description": "Protocol persistence",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        session_id="thread_test",
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert("reports", "resume_checkpoint:test", {"status": "pending"})
    app.state.container.run_protocol_service.append_event(
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        session_id=run.session_id,
        event_type="turn_started",
        status="started",
        turn_id="turn_1_1",
        message="Turn started.",
    )
    bookmark = app.state.container.run_protocol_service.create_bookmark(
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        turn_id="turn_1_1",
        response_id="resp_123",
        checkpoint_ref="resume_checkpoint:test",
        trace_bundle_ref="trace_bundle:test",
        diff_sha256_value=None,
        tool_result_count=2,
    )

    protocol = client.get(f"/runs/{run.run_id}/protocol").json()
    bookmarks = client.get(f"/runs/{run.run_id}/bookmarks").json()

    assert protocol["items"][0]["schema"] == "grounded.run_protocol_event.v1"
    assert protocol["items"][0]["turn_id"] == "turn_1_1"
    assert protocol["latest_bookmark"]["response_id"] == "resp_123"
    assert bookmarks["items"][0]["bookmark_id"] == bookmark["bookmark_id"]


def test_resume_from_stale_bookmark_returns_structured_conflict(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Protocol Conflict",
            "description": "Protocol resume conflict",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    bookmark = app.state.container.run_protocol_service.create_bookmark(
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        turn_id="turn_1_1",
        response_id=None,
        checkpoint_ref="missing_checkpoint",
        trace_bundle_ref=None,
        diff_sha256_value=None,
    )

    response = client.post(
        f"/runs/{run.run_id}/resume-from-bookmark",
        json={"bookmark_id": bookmark["bookmark_id"], "prompt": "Continue"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["detail"]["reason"] == "missing_checkpoint"


def test_run_compaction_endpoint_records_contract_plan_and_protocol_boundary(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Compaction Workspace",
            "description": "Compaction endpoint test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        acceptance_contract={"required": True, "flows": [{"id": "create_item"}]},
        implementation_plan={"steps": [{"id": "backend"}]},
        touched_files=["miniapp/app/main.py"],
        budget_status={"status": "ok"},
        completion_budget={"turns": 4},
        checks_summary={"validators": "passed", "build": "failed", "preview": "pending", "gate_status": "blocked", "issues": []},
        resume_checkpoint_ref=f"resume_checkpoint:{workspace['workspace_id']}:run_compact_test",
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        run.resume_checkpoint_ref,
        {
            "latest_diff_summary": "diff --git a/miniapp/app/main.py b/miniapp/app/main.py",
            "repair_packets": [{"failure_signature": "build.failed", "required_next_tool": "run_checks"}],
            "todo_plan": [{"status": "in_progress", "step": "Fix build"}],
            "pending_tool_result_count": 1,
        },
    )
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {"check_results": [{"name": "frontend build", "status": "failed", "details": "syntax"}]},
    )

    compaction = client.post(f"/runs/{run.run_id}/compact").json()
    boundaries = client.get(f"/runs/{run.run_id}/compaction/boundaries").json()
    post_message = client.get(f"/runs/{run.run_id}/compaction/post-message/{compaction['boundary_id']}").json()
    events = client.get(f"/runs/{run.run_id}/events").json()

    assert compaction["schema"] == "grounded.run_compaction.v1"
    assert compaction["sections"]["product_contract"]["acceptance_contract"]["required"] is True
    assert compaction["sections"]["files_changed"]["touched_files"] == ["miniapp/app/main.py"]
    assert compaction["sections"]["failing_checks"][0]["name"] == "frontend build"
    assert compaction["sections"]["current_plan"]["todo_plan"][0]["step"] == "Fix build"
    assert compaction["sections"]["next_repair_action"]["failure_signature"] == "build.failed"
    assert compaction["sections"]["budget_status"]["completion_budget"]["turns"] == "4" or compaction["sections"]["budget_status"]["completion_budget"]["turns"] == 4
    assert compaction["post_compact_message_ref"] == f"post_compact_message:{run.run_id}:{compaction['boundary_id']}"
    assert compaction["post_compact_status"] == "pending"
    assert post_message["schema"] == "grounded.post_compact_message.v1"
    assert post_message["status"] == "pending"
    assert post_message["sections"]["current_plan"]["todo_plan"][0]["step"] == "Fix build"
    assert boundaries["items"][0]["boundary_id"] == compaction["boundary_id"]
    assert boundaries["items"][0]["post_compact_message_ref"] == compaction["post_compact_message_ref"]
    assert any(item["type"] == "compact_boundary" for item in events["protocol_events"])


def test_agent_transcript_microcompacts_large_tool_results(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.run_compaction_service
    transcript = AgentTranscriptStore()
    run_key = "run_microcompact"
    transcript.configure_microcompact(
        run_key,
        writer=lambda result, serialized: service.microcompact_tool_result(
            workspace_id="ws_micro",
            run_id=run_key,
            tool_result=result,
            serialized=serialized,
        ),
    )
    large_result = {
        "tool_use_id": "tool_1",
        "tool": "read_files",
        "status": "completed",
        "content": "x" * 9000,
    }

    transcript.append_tool_results(run_key, [large_result])
    snapshot = transcript.snapshot(run_key)
    pending = snapshot["tool_result_messages"][0]
    microcompact_ref = pending["microcompact_ref"]
    stored = app.state.container.store.get("reports", microcompact_ref)

    assert pending["tool_use_id"] == "tool_1"
    assert "microcompact_ref" in pending["output"]
    assert len(pending["output"]) < 4000
    assert stored["schema"] == "grounded.microcompact.v1"
    assert stored["original_chars"] > 6000


def test_agent_transcript_queues_and_consumes_post_compact_message() -> None:
    transcript = AgentTranscriptStore()
    run_key = "run_post_compact"
    transcript.append_post_compact_message(
        run_key,
        {
            "boundary_id": "compact_1",
            "ref": "post_compact_message:run_post_compact:compact_1",
            "status": "pending",
            "message": "{\"sections\":{\"current_plan\":[]}}",
            "sections": {"current_plan": []},
            "refs": {"compaction_ref": "run_compaction:run_post_compact"},
        },
    )

    context = transcript.next_model_context(run_key)
    assert context["post_compact_messages"][0]["boundary_id"] == "compact_1"
    assert context["tool_result_messages"] == []

    transcript.append_model_turn(
        run_key,
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="continue",
        tool_calls=[],
        model="test",
        consumed_post_compact_count=1,
        consumed_post_compact_refs=["post_compact_message:run_post_compact:compact_1"],
    )
    snapshot = transcript.snapshot(run_key)

    assert snapshot["pending_post_compact_count"] == 0
    assert snapshot["counts"]["post_compact_message"] == 1
    assert snapshot["counts"]["post_compact_message_consumed"] == 1


def test_workspace_memory_rejects_secret_like_text(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Memory Secret Guard",
            "description": "Memory should not store secrets",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    response = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory",
        json={"kind": "project_rule", "text": "api_key=sk-secretvalue123456"},
    )

    assert response.status_code == 400
    assert "secret" in response.json()["error"]["message"].lower()


def test_reliability_gate_reconciles_completed_run_with_missing_product_proof(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Gate Reconcile",
            "description": "Terminal state must follow Reliability Gate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a business workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        draft_status="approved",
        acceptance_contract={"required": True},
        touched_files=["miniapp/app/main.py"],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "run": run.model_dump(mode="json"),
            "diff": "diff --git a/miniapp/app/main.py b/miniapp/app/main.py\n",
            "check_results": [],
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    reconciled = client.get(f"/runs/{run.run_id}").json()
    state = client.get(f"/runs/{run.run_id}/state").json()

    assert gate["status"] == "blocked"
    assert gate["run_state"]["blocking"] is True
    assert state["schema"] == "grounded.run_state.v1"
    assert reconciled["status"] == "blocked"
    assert reconciled["apply_status"] == "applied"
    assert reconciled["failure_class"] == "reliability_gate.blocked"


def test_reliability_gate_passes_applied_run_with_product_proof(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Gate Passed",
            "description": "Green product proof keeps completed status",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a business workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        draft_status="approved",
        acceptance_contract={"required": True},
        touched_files=["miniapp/app/main.py"],
    )
    check_results = [
        {"name": "backend_static_validators", "status": "passed", "details": "ok"},
        {"name": "api_workflow_smoke", "status": "passed", "details": "ok"},
        {"name": "browser_flow_smoke", "status": "passed", "details": "ok", "diagnostics": {"steps": [{"role": "client", "status": "passed"}]}},
    ]
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "run": run.model_dump(mode="json"),
            "diff": "diff --git a/miniapp/app/main.py b/miniapp/app/main.py\n",
            "check_results": check_results,
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    state = client.get(f"/runs/{run.run_id}/state").json()
    final_report = client.get(f"/runs/{run.run_id}/final-report").json()

    assert gate["status"] == "passed"
    assert gate["blocking"] is False
    assert state["status"] == "passed"
    assert final_report["status"] == "passed"


def test_api_errors_use_typed_envelope(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    response = client.get("/runs/run_missing")

    assert response.status_code == 404
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["blocking"] is True
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["failure_signature"].startswith("api.not_found:")


def test_config_schema_is_versioned_json_schema(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    schema = client.get("/system/config/schema").json()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["platform_config_version"] == "grounded.platform.v1"
    assert schema["schemas"]["platform"]["properties"]["preview_port_base"]["type"] == "integer"


def test_workspace_creation_does_not_start_preview_before_generated_app_exists(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Lock Guard",
            "description": "Avoid starting preview on the blank template",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    preview = app.state.container.preview_service.peek(workspace["workspace_id"])

    assert preview.status == "stopped"
    assert preview.stage == "idle"
    assert preview.proxy_port is None
    assert not any("Preview ensure requested" in item for item in preview.logs)


def test_local_preview_reconciles_live_process_without_resetting_to_stopped(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    workspace = TestClient(app).post(
        "/workspaces",
        json={
            "name": "Local Preview Reconcile",
            "description": "Restore live local preview state",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    preview = PreviewRecord(
        workspace_id=workspace["workspace_id"],
        runtime_mode="local",
        status="starting",
        stage="starting",
        proxy_port=None,
        url=None,
        project_name=None,
    )
    container = app.state.container
    container.store.upsert("previews", workspace["workspace_id"], preview.model_dump(mode="json"))
    container.preview_service.runtime_manager.local_process_port = lambda workspace_id: 16666  # type: ignore[method-assign]
    container.preview_service._http_preview_ready = lambda url: url == "http://localhost:16666"  # type: ignore[method-assign]

    reconciled = container.preview_service.get(workspace["workspace_id"])

    assert reconciled.status == "running"
    assert reconciled.stage == "running"
    assert reconciled.proxy_port == 16666
    assert reconciled.url == "http://localhost:16666"


def test_local_preview_port_selection_does_not_probe_docker(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    workspace = TestClient(app).post(
        "/workspaces",
        json={
            "name": "Local Port Selection",
            "description": "Keep local preview lifecycle independent from docker state",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    container = app.state.container
    preview = PreviewRecord(
        workspace_id=workspace["workspace_id"],
        runtime_mode="local",
        status="starting",
        stage="rebuilding",
        proxy_port=16667,
        url="http://localhost:16667",
        project_name="local-preview",
    )

    container.preview_service.runtime_manager.local_process_port = lambda workspace_id: 16667  # type: ignore[method-assign]
    container.preview_service.runtime_manager.inspect_containers = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("local preview selection must not inspect docker containers")
    )

    selected = container.preview_service._select_proxy_port(  # noqa: SLF001
        workspace["workspace_id"],
        preview,
        container.workspace_service.source_dir(workspace["workspace_id"]),
    )

    assert selected == 16667


def test_preview_get_never_marks_running_without_url(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    workspace = TestClient(app).post(
        "/workspaces",
        json={
            "name": "Queued Preview",
            "description": "Do not fake readiness without a URL",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    preview = PreviewRecord(
        workspace_id=workspace["workspace_id"],
        runtime_mode="docker",
        status="starting",
        stage="rebuilding",
        proxy_port=None,
        url=None,
        project_name=None,
        logs=[
            "Queued asynchronous preview rebuild.",
            "Observed asynchronous preview rebuild from API polling.",
        ],
    )
    container = app.state.container
    container.store.upsert("previews", workspace["workspace_id"], preview.model_dump(mode="json"))

    observed = container.preview_service.get(workspace["workspace_id"])

    assert observed.status == "starting"
    assert observed.url is None


def test_delete_workspace_cleans_docker_resources_for_stale_local_record(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Delete Cleanup",
            "description": "Remove stale docker resources during delete",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    container = app.state.container
    calls: list[tuple[str, str]] = []
    preview = PreviewRecord(
        workspace_id=workspace["workspace_id"],
        runtime_mode="local",
        status="running",
        stage="running",
        proxy_port=16668,
        url="http://localhost:16668",
        project_name="grounded_preview_stale",
    )
    container.store.upsert("previews", workspace["workspace_id"], preview.model_dump(mode="json"))
    container.preview_service.runtime_manager.reset_local = lambda workspace_id: calls.append(("local", workspace_id)) or []  # type: ignore[method-assign]
    container.preview_service.runtime_manager.remove_project_resources = lambda workspace_id: calls.append(("docker", workspace_id)) or [  # type: ignore[method-assign]
        "[runtime] removed stale preview resources"
    ]
    container.preview_service.runtime_manager.reset = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("local preview deletion should use direct docker cleanup, not compose down")
    )

    response = client.delete(f"/workspaces/{workspace['workspace_id']}")

    assert response.status_code == 200
    assert response.json()["preview_cleanup"] == "completed"
    assert ("local", workspace["workspace_id"]) in calls
    assert ("docker", workspace["workspace_id"]) in calls


def test_preview_compose_uses_bridge_network_without_project_network() -> None:
    rendered = """services:
  preview-app:
    image: grounded-miniapp-preview-base:latest
    build:
      context: ../miniapp
    working_dir: /app
"""

    normalized = PreviewRuntimeManager._force_preview_bridge_network(rendered)  # noqa: SLF001

    assert "network_mode: bridge" in normalized
    assert normalized.count("network_mode: bridge") == 1


def test_api_errors_use_typed_envelope(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    response = client.get("/runs/run_missing_for_error_envelope")

    assert response.status_code == 404
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["blocking"] is True
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["deterministic"] is True


def test_provider_quota_errors_keep_typed_envelope(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)

    @app.get("/test/provider-quota")
    def provider_quota_probe() -> None:
        raise RuntimeError(
            'OpenAI responses returned 429: {"error":{"type":"insufficient_quota","code":"insufficient_quota"}}'
        )

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/test/provider-quota")

    assert response.status_code == 429
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["blocking"] is True
    assert payload["error"]["code"] == "provider.insufficient_quota"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["deterministic"] is False
    assert payload["error"]["failure_class"] == "provider.insufficient_quota"


def test_run_record_reconciles_from_terminal_artifacts(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    workspace = TestClient(app).post(
        "/workspaces",
        json={
            "name": "Reconcile Workspace",
            "description": "Run state reconciliation test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a reconciled workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="running",
        apply_status="pending",
        current_stage="refreshing preview",
        progress_percent=99,
    )
    job = JobRecord(
        workspace_id=workspace["workspace_id"],
        prompt=run.prompt,
        status="completed",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        generation_mode="fast",
        linked_run_id=run.run_id,
        outcome_kind="applied",
        token_usage={"total_tokens": 123, "turn_count": 2},
    )
    run.linked_job_id = job.job_id
    terminal = run.model_copy(deep=True)
    terminal.status = "completed"
    terminal.apply_status = "applied"
    terminal.current_stage = "completed"
    terminal.progress_percent = 100
    terminal.token_usage = {"total_tokens": 123, "turn_count": 2}
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert("jobs", job.job_id, job.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "run": terminal.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "diff": "diff --git a/miniapp/app/main.py b/miniapp/app/main.py\n",
            "check_results": [],
        },
    )

    reconciled = app.state.container.run_service.get_run(run.run_id)
    persisted = app.state.container.store.get("runs", run.run_id)

    assert reconciled.status == "completed"
    assert reconciled.apply_status == "applied"
    assert reconciled.progress_percent == 100
    assert reconciled.token_usage["total_tokens"] == 123
    assert persisted["status"] == "completed"


def test_reliability_gate_final_report_and_empty_repair_queue(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Gate Workspace",
            "description": "Reliability gate test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a role-owned workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True, "flows": [{"id": "create_role_owned_entity"}]},
        browser_flow_proof={"steps": [{"status": "passed", "route": "/client"}]},
        mobile_layout_report={"status": "passed"},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {"name": "api_workflow_smoke", "status": "passed", "details": "ok", "logs": []},
                {"name": "browser_flow_smoke", "status": "passed", "details": "ok", "logs": [], "diagnostics": {"mobile_layout": {"status": "passed"}}},
            ],
            "preview": {"url": "http://127.0.0.1:18000", "role_urls": {"/client": "/client"}, "status": "running"},
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    final_report = client.get(f"/runs/{run.run_id}/final-report").json()
    repair = client.get(f"/runs/{run.run_id}/repair-signatures").json()

    assert gate["status"] == "passed"
    assert gate["blocking"] is False
    assert final_report["status"] == "passed"
    assert final_report["diff_summary"]["diff_available"] is True
    assert repair["status"] == "empty"


def test_reliability_gate_blocks_missing_browser_proof(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Blocked Gate Workspace",
            "description": "Reliability gate failure test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build an operations workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="blocked",
        apply_status="blocked",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True},
        repair_issue_signatures=[{"signature": "browser_flow_smoke: failed click", "check": "browser_flow_smoke"}],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [{"name": "api_workflow_smoke", "status": "passed", "details": "ok", "logs": []}],
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    repair = client.get(f"/runs/{run.run_id}/repair-signatures").json()
    repair_cases = client.get(f"/runs/{run.run_id}/repair-cases").json()

    assert gate["status"] == "blocked"
    assert any(issue["check"] == "browser_flow_smoke" for issue in gate["issues"])
    assert any(item["signature"] == "preview.browser_flow_failed" for item in repair["items"])
    assert repair_cases["items"]
    assert repair_cases["active_case"]["failure_class"] in {"browser_flow_smoke", "browser_proof_gap"}
    assert repair_cases["active_case"]["repair_prompt"]["sections"]["expected_proof"]


def test_review_report_prioritizes_acceptance_blockers_with_locations(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Review Mode Workspace",
            "description": "Review findings shape",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build an operations workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="blocked",
        apply_status="blocked",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True},
        checks_summary={
            "validators": "failed",
            "build": "passed",
            "preview": "failed",
            "gate_status": "blocked",
            "issues": [
                {
                    "severity": "high",
                    "code": "platform.workflow_selector_matches_no_html",
                    "message": "Missing selector",
                    "file_path": "miniapp/app/static/client/app.js",
                    "line": 12,
                    "blocking": True,
                }
            ],
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {
                    "name": "frontend_interaction_static_smoke",
                    "status": "failed",
                    "details": "Selector mismatch",
                    "logs": [],
                    "diagnostics": {"file_path": "miniapp/app/static/client/app.js", "line": 12},
                }
            ],
        },
    )

    review = client.get(f"/runs/{run.run_id}/review").json()
    repair_cases = client.get(f"/runs/{run.run_id}/repair-cases").json()

    assert review["schema"] == "grounded.review_report.v2"
    assert review["status"] == "failed"
    assert review["summary"]["blocker_count"] >= 2
    assert review["summary"]["missing_tests"] >= 1
    assert review["summary"]["browser_proof_gaps"] >= 1
    assert review["summary"]["contract_mismatches"] >= 1
    assert review["findings"][0]["is_blocker_for_product_acceptance"] is True
    assert any(item["category"] == "stale_test_risk" for item in review["findings"])
    assert any(item.get("file_path") == "miniapp/app/static/client/app.js" and item.get("line") == 12 for item in review["findings"])
    assert repair_cases["items"]
    assert any(item["source"] == "review" for item in repair_cases["items"])
    assert any("miniapp/app/static/client/app.js" in item["target_files"] for item in repair_cases["items"])


def test_repair_case_service_blocks_repeated_patch_attempts(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Build a prompt-derived workflow",
        intent="create",
        target_role_scope=["client"],
        model_profile="test",
        status="blocked",
    )
    service = app.state.container.repair_case_service
    cases = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "preview.browser_flow_failed",
                "failure_class": "browser_flow_smoke",
                "check": "browser_flow_smoke",
                "target_files": ["miniapp/app/static/client/app.js"],
                "evidence": {"selector": "#save"},
            }
        ],
        source="test",
    )
    case_id = cases["active_case"]["case_id"]
    patch_hash = "same-diff"

    service.record_attempt(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        case_id=case_id,
        attempt={
            "status": "failed",
            "changed_files": ["miniapp/app/static/client/app.js"],
            "patch_sha256": patch_hash,
            "failure_reason": "proof_failed",
            "forbidden_repeat_action": {"type": "same_patch_sha256", "sha256": patch_hash},
        },
    )

    assert service.repeated_patch(run_id=run.run_id, case_id=case_id, patch_hash=patch_hash) is True
    attempts = service.attempts(run.run_id, case_id)
    assert attempts["items"][0]["forbidden_repeat_action"]["type"] == "same_patch_sha256"


def test_repair_case_service_focuses_latest_check_cases_and_sets_next_action(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.repair_case_service
    run = RunRecord(workspace_id="ws_1", prompt="Build a prompt-derived workflow", intent="create", status="blocked")

    first = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "frontend.old_blocker",
                "failure_class": "frontend_interaction_static_smoke",
                "severity": "high",
                "target_files": ["miniapp/app/static/client/app.js"],
                "verification_check": "frontend_interaction_static_smoke",
            }
        ],
        source="agent_loop_checks",
    )
    old_case_id = first["active_case"]["case_id"]
    plan = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "generation.invalid_edit_operation:patch_conflict",
                "failure_class": "generation.invalid_edit_operation",
                "severity": "high",
                "target_files": ["miniapp/app/static/manager/app.js"],
                "verification_check": "repair_case_attempt_ledger",
            }
        ],
        source="agent_loop_plan",
    )
    plan_case_id = plan["active_case"]["case_id"]

    latest = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "frontend.current_blocker",
                "failure_class": "platform_invariants",
                "severity": "high",
                "target_files": ["miniapp/app/static/manager/app.js"],
                "verification_check": "platform_invariants",
            }
        ],
        source="agent_loop_checks",
    )

    old_case = service.get_case(run.run_id, old_case_id)
    plan_case = service.get_case(run.run_id, plan_case_id)
    assert old_case["status"] == "superseded"
    assert plan_case["status"] == "superseded"
    assert latest["active_case"]["failure_signature"] == "frontend.current_blocker"
    assert latest["active_case"]["next_action"]["target_files"] == ["miniapp/app/static/manager/app.js"]
    assert latest["active_case"]["repair_prompt"]["sections"]["next_action"]["action"]


def test_repair_case_service_expands_role_directory_evidence_targets(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.repair_case_service
    run = RunRecord(workspace_id="ws_1", prompt="Build a prompt-derived workflow", intent="create", status="blocked")

    cases = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "frontend.raw_status_rendered_to_user.manager",
                "failure_class": "platform_invariants",
                "severity": "high",
                "verification_check": "platform_invariants",
                "evidence": {
                    "validator_issue": {
                        "code": "platform.raw_status_rendered_to_user",
                        "location": "miniapp/app/static/manager",
                    }
                },
            }
        ],
        source="agent_loop_checks",
    )

    target_files = cases["active_case"]["target_files"]
    assert "miniapp/app/static/manager/app.js" in target_files
    assert "miniapp/app/static/manager/index.html" in target_files
    assert cases["active_case"]["next_action"]["first_tool"] == "read_files"


def test_run_service_schedules_auto_repair_continuation_from_active_case(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    run_service = app.state.container.run_service
    repair_service = app.state.container.repair_case_service
    run = RunRecord(
        workspace_id="ws_1",
        prompt="Build a prompt-derived workflow",
        intent="create",
        status="blocked",
        apply_status="blocked",
        draft_ready=True,
        draft_status="ready",
        failure_reason="Generation token budget exhausted: 1200001/1200000 tokens.",
    )
    repair_service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "frontend.raw_status_rendered_to_user.manager",
                "failure_class": "platform_invariants",
                "severity": "high",
                "target_files": ["miniapp/app/static/manager/app.js"],
                "verification_check": "platform_invariants",
            }
        ],
        source="agent_loop_checks",
    )

    class FakeBackgroundTaskService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create_task(self, **kwargs):
            self.calls.append(kwargs)
            return type("Task", (), {"task_id": "task_auto_repair"})()

    fake = FakeBackgroundTaskService()
    monkeypatch.setenv("GROUNDED_AUTO_REPAIR_CONTINUATION_MAX", "1")
    run_service.attach_background_task_service(fake)

    run_service._schedule_auto_repair_continuation_if_needed(run)

    assert fake.calls
    call = fake.calls[0]
    assert call["task_type"] == "repair_failed_run"
    assert call["auto_start"] is True
    assert call["input_payload"]["source_run_id"] == run.run_id
    assert call["input_payload"]["repair_case_id"]
    assert "repair_prompt" in call["input_payload"]["prompt"] or "grounded.repair_prompt.v1" in call["input_payload"]["prompt"]
    report = app.state.container.store.get("reports", f"auto_repair_continuation:{run.run_id}")
    assert report["status"] == "scheduled"
    assert report["task_id"] == "task_auto_repair"
    saved = run_service.get_run(run.run_id)
    assert saved.current_stage == "auto_repair_queued"
    assert "Auto repair continuation" in (saved.summary or "")


def test_repair_catalog_extracts_nested_workflow_evidence() -> None:
    packets = RepairCatalog.classify_many(
        [
            {
                "kind": "check_failure",
                "check": "platform_invariants",
                "evidence": {
                    "logs": [
                        '{"code":"platform.missing_role_workflow_actions","message":"manager role lacks its own workflow actions."}'
                    ]
                },
            },
            {
                "kind": "check_failure",
                "check": "frontend_interaction_static_smoke",
                "evidence": {
                    "logs": [
                        '{"code":"platform.workflow_patch_payload_field_mismatch","message":"manager sends PATCH fields not accepted by the backend update schema: assigned_to."}'
                    ]
                },
            },
        ]
    )

    signatures = {item["signature"] for item in packets}
    assert "workflow.missing_role_actions" in signatures
    assert "workflow.payload_schema_mismatch" in signatures


def test_repair_catalog_prefers_embedded_validator_recipe_over_broad_static_failure() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "kind": "check_failure",
            "check": "frontend_interaction_static_smoke",
            "details": "Frontend interaction smoke checked required buttons/forms.",
            "logs": [
                json.dumps(
                    {
                        "code": "platform.frontend_update_visibility",
                        "message": "A role detail view omits persisted fields required by the contract before its action.",
                        "severity": "high",
                        "location": "miniapp/app/static/manager/app.js",
                        "blocking": True,
                        "repair_recipe": {
                            "recipe_id": "frontend.update_visibility",
                            "failure_class": "frontend_interaction_static_smoke",
                            "failure_signature": "frontend.update_visibility",
                            "required_next_tool": "read_files",
                            "suggested_tool_after_read": "write_file",
                            "target_files": ["miniapp/app/static/manager/app.js"],
                            "verification_check": "frontend_interaction_static_smoke",
                            "verification_command": "run_checks frontend_interaction_static_smoke",
                            "retry_policy": "deterministic_repair",
                            "deterministic": True,
                            "retryable": True,
                            "instruction": "Render the contract-required persisted fields in the role detail view.",
                            "evidence": {"missing_contract_fields": ["результат"]},
                        },
                    }
                )
            ],
            "failure_class": "frontend_interaction_static_smoke",
            "failure_signature": "frontend_interaction_static_smoke:buttons/forms",
        }
    )

    assert packet["signature"] == "frontend.update_visibility"
    assert packet["code"] == "frontend_update_visibility"
    assert packet["suggested_tool_after_read"] == "write_file"
    assert packet["target_files"] == ["miniapp/app/static/manager/app.js"]


def test_thread_snapshots_are_persistent(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Snapshot Workspace",
            "description": "snapshot test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    thread = app.state.container.thread_service.start_thread(workspace_id=workspace["workspace_id"], title="Snapshot thread")

    created = client.post(f"/threads/{thread.thread_id}/snapshots", json={"reason": "test"}).json()
    snapshots = client.get(f"/threads/{thread.thread_id}/snapshots").json()

    assert created["reason"] == "test"
    assert snapshots["items"][0]["snapshot_id"] == created["snapshot_id"]


def test_exec_runtime_streams_and_blocks_workspace_escape(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Exec Workspace",
            "description": "exec runtime test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.thread_service

    started = service.exec_command(workspace_id=workspace["workspace_id"], command="ls miniapp", timeout=5)
    process_id = started["process_id"]
    output = _wait_for_exec_output(service, process_id)
    resized = service.resize_exec(process_id, cols=100, rows=32)

    escaped_error = ""
    try:
        service.exec_command(workspace_id=workspace["workspace_id"], command="ls /tmp", timeout=5)
    except ValueError as exc:
        escaped_error = str(exc)
    source_dir = app.state.container.workspace_service.source_dir(workspace["workspace_id"])
    outside_link = source_dir / "miniapp" / "outside"
    try:
        outside_link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        outside_link = None
    symlink_session: dict[str, Any] | None = None
    if outside_link is not None:
        symlinked = service.exec_command(workspace_id=workspace["workspace_id"], command="ls miniapp/outside", timeout=5)
        symlink_session = _wait_for_exec_session(app, symlinked["process_id"])

    assert started["status"] in {"starting", "running"}
    assert output["status"] == "completed"
    assert "app" in output["content"]
    assert resized["ok"] is True
    assert "Absolute and home-relative paths are blocked" in escaped_error
    if symlink_session is not None:
        assert symlink_session["result"]["semantic_status"] == "blocked_by_sandbox"


def _workspace_with_run(tmp_path: Path) -> tuple[Any, TestClient, dict, RunRecord]:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Workbench Behavior",
            "description": "Second pass behavior test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Improve internal dashboard",
        intent="edit",
        apply_strategy="manual_approve",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="awaiting_approval",
        apply_status="awaiting_approval",
        draft_status="ready",
        draft_ready=True,
        approval_required=True,
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    return app, client, workspace, run


def _wait_for_background_task(client: TestClient, task_id: str, timeout: float = 5.0) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        latest = client.get(f"/tasks/{task_id}").json()
        if latest.get("status") in {"completed", "failed", "blocked", "cancelled"}:
            return latest
        time.sleep(0.05)
    return latest


def _wait_for_exec_output(service: Any, process_id: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for _ in range(30):
        output = service.read_exec_output(process_id, stream="stdout")
        if output.get("status") == "completed":
            return output
        time.sleep(0.05)
    return output


def _wait_for_exec_session(app: Any, process_id: str) -> dict[str, Any]:
    session: dict[str, Any] = {}
    for _ in range(30):
        snapshot = app.state.container.exec_runtime_service.snapshot()
        session = next((item for item in snapshot.get("sessions", []) if item.get("process_id") == process_id), {})
        if session.get("status") in {"completed", "failed"}:
            return session
        time.sleep(0.05)
    return session


def test_policy_decision_auto_accepts_without_user_approval(tmp_path: Path) -> None:
    _app, client, workspace, run = _workspace_with_run(tmp_path)

    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/policy/evaluate-command",
        json={"command": "rg FastAPI miniapp/app", "preset": "strict_manual", "run_id": run.run_id},
    ).json()

    approvals = client.get(f"/runs/{run.run_id}/approvals").json()
    events = client.get(f"/runs/{run.run_id}/tool-events").json()
    timeline = client.get(f"/runs/{run.run_id}/timeline").json()

    assert evaluation["approval"]["required"] is False
    assert evaluation["approval"]["approval_id"] is None
    assert approvals["items"] == []
    assert any(item["tool"] == "policy.evaluate" for item in events["events"])
    assert not any(item["kind"] == "approval" for item in timeline["items"])


def test_staged_apply_commits_only_selected_files_and_discard_restores_draft(tmp_path: Path) -> None:
    app, client, workspace, run = _workspace_with_run(tmp_path)
    workspace_id = workspace["workspace_id"]
    service = app.state.container.workspace_service
    draft = service.prepare_draft(workspace_id, run.run_id)
    source = service.source_dir(workspace_id)
    backend_path = "miniapp/app/main.py"
    ui_path = "miniapp/app/static/client/index.html"
    original_backend = (source / backend_path).read_text(encoding="utf-8")
    original_ui = (source / ui_path).read_text(encoding="utf-8")
    (draft / backend_path).write_text(original_backend + "\n# staged behavior marker\n", encoding="utf-8")
    (draft / ui_path).write_text(original_ui.replace("</body>", "<!-- draft marker --></body>"), encoding="utf-8")

    staged = client.post(f"/runs/{run.run_id}/stage/files", json={"files": [backend_path]}).json()
    applied = client.post(f"/runs/{run.run_id}/apply/staged").json()
    discarded = client.post(f"/runs/{run.run_id}/discard/files", json={"files": [ui_path]}).json()

    assert staged["files"] == [backend_path]
    assert "staged behavior marker" in (source / backend_path).read_text(encoding="utf-8")
    assert (source / ui_path).read_text(encoding="utf-8") == original_ui
    assert applied["status"] == "awaiting_approval"
    assert ui_path in discarded["result"]["discarded_files"]
    assert (draft / ui_path).read_text(encoding="utf-8") == original_ui


def test_staged_apply_uses_actual_diff_for_generated_manifest(tmp_path: Path) -> None:
    app, client, workspace, run = _workspace_with_run(tmp_path)
    workspace_id = workspace["workspace_id"]
    service = app.state.container.workspace_service
    draft = service.prepare_draft(workspace_id, run.run_id)
    source = service.source_dir(workspace_id)
    backend_path = "miniapp/app/main.py"
    manifest_path = "miniapp/app/generated/route_manifest.json"
    original_backend = (source / backend_path).read_text(encoding="utf-8")
    original_manifest = (source / manifest_path).read_text(encoding="utf-8")
    (draft / backend_path).write_text(original_backend + "\n# touched only marker\n", encoding="utf-8")
    (draft / manifest_path).write_text(original_manifest.replace('"shared": {}', '"shared": {}, "diffGraphMarker": true'), encoding="utf-8")
    run.touched_files = [backend_path]
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    applied = client.post(f"/runs/{run.run_id}/apply/staged").json()

    assert applied["status"] == "completed"
    assert "touched only marker" in (source / backend_path).read_text(encoding="utf-8")
    assert "diffGraphMarker" in (source / manifest_path).read_text(encoding="utf-8")


def test_file_search_and_plugin_validation(tmp_path: Path) -> None:
    _app, client, workspace, _run = _workspace_with_run(tmp_path)

    search = client.get(f"/workspaces/{workspace['workspace_id']}/files/search", params={"q": "FastAPI"}).json()
    traversal = client.get(
        f"/workspaces/{workspace['workspace_id']}/files/search",
        params={"q": "FastAPI", "run_id": "../escape"},
    )
    invalid_plugin = client.post("/plugins/install-local", json={"id": "local.validator", "capabilities": ["validators"]})
    valid_plugin = client.post(
        "/plugins/install-local",
        json={"id": "local.validator", "version": "0.1.0", "capabilities": ["validators"]},
    ).json()

    assert search["items"]
    assert traversal.status_code == 400
    assert invalid_plugin.status_code == 400
    assert valid_plugin["status"] == "registered"


def test_config_security_test_matrix_prompt_contract_and_exports(tmp_path: Path) -> None:
    app, client, workspace, run = _workspace_with_run(tmp_path)
    workspace_id = workspace["workspace_id"]
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/main.py b/miniapp/app/main.py\n+internal dashboard FastAPI workflow\n",
            "check_results": [
                {"name": "frontend_interaction_static_smoke", "status": "passed"},
                {"name": "api_workflow_proof", "status": "passed"},
            ],
        },
    )

    schema = client.get("/system/config/schema").json()
    migrations = client.get("/system/migrations").json()
    security = client.get("/system/security/summary").json()
    matrix = client.get(f"/runs/{run.run_id}/test-matrix").json()
    contract = client.get(f"/runs/{run.run_id}/prompt-contract").json()
    manifest_export = client.post(f"/workspaces/{workspace_id}/export/manifest").json()
    deploy_export = client.post(f"/workspaces/{workspace_id}/export/deploy-bundle").json()
    docker_export = client.post(f"/workspaces/{workspace_id}/export/docker-validation-report").json()

    assert schema["platform_config_version"] == "grounded.platform.v1"
    assert migrations["status"] == "current"
    assert security["status"] == "configured"
    assert any(item["key"] == "frontend_js_smoke" and item["status"] == "passed" for item in matrix["items"])
    assert contract["status"] == "passed"
    assert Path(manifest_export["file_path"]).exists()
    assert Path(deploy_export["file_path"]).exists()
    assert Path(docker_export["file_path"]).exists()


def test_structured_thread_compaction_contains_workbench_fields(tmp_path: Path) -> None:
    app, _client, workspace, run = _workspace_with_run(tmp_path)
    thread = app.state.container.thread_service.start_thread(workspace_id=workspace["workspace_id"], title="Compaction")
    turn = app.state.container.thread_service.start_turn(
        thread.thread_id,
        {
            "prompt": "Refine dense operations UI",
            "mode": "fix",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "test",
        },
    )
    created_run = app.state.container.run_service.get_run(turn.linked_run_id)
    created_run.touched_files = ["miniapp/app/main.py"]
    created_run.failure_reason = "example verification failure"
    app.state.container.store.upsert("runs", created_run.run_id, created_run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"approvals:{created_run.run_id}",
        {"run_id": created_run.run_id, "items": [{"approval_id": "appr_test", "status": "pending"}]},
    )

    compacted = app.state.container.thread_service.compact_thread(thread.thread_id)
    summary = compacted.metadata["compaction"]

    assert summary["linked_runs"]
    assert summary["current_file_focus"] == ["miniapp/app/main.py"]
    assert summary["known_failures"] == ["example verification failure"]
    assert summary["unresolved_approvals"][0]["approval_id"] == "appr_test"
