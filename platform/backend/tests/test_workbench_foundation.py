from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.services.repair_catalog import RepairCatalog
from app.services.exec_policy_service import ExecPolicyService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, canonical_tool_name, tool_envelope


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
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    policy = client.get("/system/policies/exec").json()
    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/policy/evaluate-command",
        json={"command": "rg workflow miniapp/app"},
    ).json()
    timeline = client.get(f"/runs/{run.run_id}/timeline").json()
    trace = client.get(f"/runs/{run.run_id}/trace-view").json()
    doctor = client.get("/doctor").json()
    memory = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory",
        json={"kind": "project_rule", "text": "Use dense operational UI."},
    ).json()
    skills = client.get("/skills").json()
    workers = client.get(f"/runs/{run.run_id}/workers").json()
    permissions = client.get("/system/permissions/rules").json()
    lsp = client.get(f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp").json()
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
    assert doctor["checks"]
    assert memory["items"][0]["text"] == "Use dense operational UI."
    assert any(item["id"] == "state-workflow" for item in skills["items"])
    assert any(item["id"] == "role-surfaces" for item in skills["items"])
    assert any(item["worker_id"] == "backend_api" for item in workers["workers"])
    assert any(item["rule_id"] == "block_destructive" for item in permissions["items"])
    assert lsp["status"] in {"passed", "failed"}
    assert patch_preflight["status"] == "passed"


def test_reliability_gate_final_report_and_repair_catalog(tmp_path: Path) -> None:
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
        prompt="Build an intake workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True, "flows": [{"id": "create_intake"}]},
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
    assert any(item["signature"] == "backend.missing_route" for item in RepairCatalog.entries())


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
        prompt="Build a CRM workflow",
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

    assert gate["status"] == "blocked"
    assert any(issue["check"] == "browser_flow_smoke" for issue in gate["issues"])
    assert any(item["signature"] == "preview.browser_flow_failed" for item in repair["items"])


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


def test_approval_rejection_blocks_and_appears_in_timeline(tmp_path: Path) -> None:
    _app, client, workspace, run = _workspace_with_run(tmp_path)

    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/policy/evaluate-command",
        json={"command": "rg FastAPI miniapp/app", "preset": "strict_manual", "run_id": run.run_id},
    ).json()
    approval_id = evaluation["approval"]["approval_id"]

    approvals = client.get(f"/runs/{run.run_id}/approvals").json()
    rejected = client.post(f"/runs/{run.run_id}/approvals/{approval_id}/reject").json()
    events = client.get(f"/runs/{run.run_id}/tool-events").json()
    timeline = client.get(f"/runs/{run.run_id}/timeline").json()

    assert approvals["items"][0]["status"] == "pending"
    assert rejected["status"] == "rejected"
    assert any(item["tool"] == "policy.evaluate" for item in events["events"])
    assert any(item["kind"] == "approval" and item["status"] == "rejected" for item in timeline["items"])


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
