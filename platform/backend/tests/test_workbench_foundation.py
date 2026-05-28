from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Any
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.models.common import GenerationMode
from app.models.context_pressure import ContextPressureReport
from app.models.artifacts import PatchEnvelope, PatchOperationModel
from app.models.domain import CreateRunRequest, DraftAction, GenerateRequest, JobRecord, PreviewRecord, RunCheckResult, RunRecord
from app.models.event_journal import EventJournalPage, RunJournalState, ThreadJournalState
from app.models.memory import MemoryRetrievalResult
from app.models.output_artifacts import CommandOutputArtifact, OutputArtifactIndex
from app.models.prompt_suggestions import PromptSuggestionsReport
from app.models.threads import TurnRecord
from app.models.workbench import (
    GateReport,
    PromptCompletionAuditReport,
    RepairAttemptsReport,
    RepairCase,
    RepairCasesReport,
    RunBookmarksReport,
    RunEventsReport,
    RunProtocolReport,
    RunTimelineReport,
    RunTraceViewReport,
    ToolEnvelope,
    TraceBundleReport,
    TraceState,
    VisualRegressionReport,
)
from app.modules.miniapp_agent_loop.agent_command_policy import AgentCommandPolicy, CommandPolicyDecision, CommandPolicyRule
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager
from app.modules.miniapp_agent_loop.agent_tool_call_loop import AgentToolCallLoop
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.guardian_review import GuardianReview
from app.modules.workspace_code_agent_runtime.budget import completion_budget_for_mode, completion_budget_status
from app.openapi_export import export_openapi
from app.repositories.platform_db import PlatformDb
from app.services.repair_catalog import RepairCatalog
from app.services.repair_classifier import RepairClassifier
from app.services.event_journal import EventJournalSecretError, EventJournalService
from app.services.exec_policy_service import ExecPolicyService
from app.services.sandbox_service import SandboxService, SandboxViolationError
from app.services.pr_babysitter import PrBabysitterService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, canonical_tool_name, tool_envelope
from app.services.workspace.runtime_manager import PreviewRuntimeManager


def test_explicit_data_dir_uses_matching_host_dir_despite_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PLATFORM_DATA_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_HOST_DATA_DIR", raising=False)

    settings = get_settings(repo_root=Path.cwd(), data_dir=tmp_path)

    assert settings.data_dir == tmp_path
    assert settings.host_data_dir == tmp_path


def test_explicit_host_data_dir_env_still_overrides_data_dir(tmp_path: Path, monkeypatch) -> None:
    host_dir = tmp_path / "host"
    monkeypatch.setenv("PLATFORM_HOST_DATA_DIR", str(host_dir))

    settings = get_settings(repo_root=Path.cwd(), data_dir=tmp_path / "data")

    assert settings.data_dir == tmp_path / "data"
    assert settings.host_data_dir == host_dir


def test_model_manager_reports_status_cache_and_fallbacks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_CODE_AGENT_TURN_FALLBACK_MODELS", "gpt-test-fallback")
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    config = client.get("/system/configuration").json()
    status = client.get("/system/models").json()
    cached = app.state.container.store.get("reports", "model_catalog_cache:v1")
    route = app.state.container.model_manager_service.select(
        role="agent_turn",
        model_profile="research_balanced",
        generation_mode="balanced",
    )

    assert config["llm"]["enabled"] is True
    assert config["llm"]["provider"] == "openai"
    assert config["llm"]["model_manager"]["schema"] == "grounded.model_manager.v1"
    assert status["providers"]["openai"]["status"] == "ready"
    assert status["catalog_cache"]["offline_usable"] is True
    assert cached["schema"] == "grounded.model_catalog_cache.v1"
    assert any(candidate.model == "gpt-test-fallback" for candidate in route.candidates)


def test_tool_protocol_normalizes_aliases() -> None:
    assert canonical_tool_name("run_command") == "shell.exec"
    envelope = tool_envelope(tool="apply_patch_to_draft", input_payload={"path": "miniapp/app/main.py"})
    typed = ToolEnvelope.model_validate(envelope)

    assert envelope["version"] == TOOL_PROTOCOL_VERSION
    assert envelope["tool"] == "patch.apply"
    assert typed.tool_call_id == envelope["tool_call_id"]
    assert envelope["status"] == "started"
    assert envelope["risk"] == "mutating"
    assert envelope["sandbox_profile"] == "agent_draft_write"
    assert envelope["capabilities"]
    assert envelope["side_effect_class"] == "write_draft"
    assert envelope["allowed_paths"]["write"]
    assert envelope["parallel_safe"] is False
    assert envelope["retry_policy"]["first_retry_tool"] == "read_files"
    assert "changed_files" in envelope
    assert envelope["approval"]["status"] == "not_required"


def test_event_journal_v2_appends_payload_refs_dedupes_and_rejects_secrets(tmp_path: Path) -> None:
    db = PlatformDb(tmp_path / "platform.db")
    journal = EventJournalService(db)

    first = journal.append_run(
        workspace_id="ws_1",
        run_id="run_1",
        event_type="run.created",
        payload={"status": "pending", "token_usage": {"total_tokens": 12}},
        idempotency_key="run.created:run_1",
    )
    duplicate = journal.append_run(
        workspace_id="ws_1",
        run_id="run_1",
        event_type="run.created",
        payload={"status": "pending"},
        idempotency_key="run.created:run_1",
    )
    second = journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="run.started", payload={"status": "running"})
    thread_event = journal.append_thread(workspace_id="ws_1", thread_id="thread_1", event_type="thread.started", payload={"run_id": "run_1"})
    payload = journal.read_payload(first.payload_ref)

    assert first.event_id == duplicate.event_id
    assert first.sequence == 1
    assert second.sequence == 2
    assert payload is not None
    assert payload.payload["status"] == "pending"
    assert payload.payload_sha256 == first.payload_sha256
    assert thread_event.sequence == 1
    assert journal.list_run("run_1", after_sequence=1)[0].event_type == "run.started"
    db.append_run_event("run_legacy", "legacy.started", {"message": "started"})
    journal.backfill_run(workspace_id="ws_1", run_id="run_legacy")
    journal.backfill_run(workspace_id="ws_1", run_id="run_legacy")
    assert [item.event_type for item in journal.list_run("run_legacy")] == ["legacy.started"]
    with pytest.raises(EventJournalSecretError):
        journal.append_run(workspace_id="ws_1", run_id="run_1", event_type="tool.completed", payload={"api_key": "sk-" + "a" * 30})


def test_create_run_writes_journal_v2_lifecycle_events(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Journal Lifecycle Workspace",
            "description": "Journal lifecycle test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    app.state.container.run_service._execute_run = lambda run_id, payload: None

    run = app.state.container.run_service.create_run(
        workspace["workspace_id"],
        CreateRunRequest(prompt="Update the client wording", mode="fix", intent="edit", generation_mode="fast"),
    )
    events = client.get(f"/runs/{run.run_id}/events-v2").json()

    assert {"run.created", "run.session_configured", "run.started"}.issubset({item["event_type"] for item in events["items"]})


def test_exec_policy_classifies_and_redacts_commands() -> None:
    service = ExecPolicyService()

    allowed = service.evaluate_command("rg api miniapp/app")
    cat_read = service.evaluate_command("cat miniapp/app/main.py", workspace_id="ws_policy")
    git_write = service.evaluate_command("git diff --output out.patch", workspace_id="ws_policy")
    blocked = service.evaluate_command("rm -rf miniapp")
    package_network = service.evaluate_command("npm install")
    redacted = service.evaluate_command("rg api_key=sk-secretvalue miniapp/app")
    redirection = service.evaluate_command("rg api miniapp/app > out.txt")
    git_internal = service.evaluate_command("ls .git")

    assert allowed["decision"]["action"] == "allow"
    assert allowed["decision"]["risk"] == "read_only"
    assert allowed["command_class"] == "read_only"
    assert allowed["approval_gate"]["gate"] == "auto"
    assert allowed["canonical_command"]["normalized_command"] == "rg api miniapp/app"
    assert allowed["canonical_command"]["executable_name"] == "rg"
    assert allowed["canonical_command"]["canonical_string"]
    assert allowed["command_prefix"]["prefix"] == ["rg"]
    assert allowed["decision_trace"]["schema"] == "grounded.exec_policy_decision_trace.v1"
    assert [step["step"] for step in allowed["decision_trace"]["steps"][:3]] == ["parse_shell_subset", "extract_executable", "network_policy"]
    assert allowed["read_write_network_policy"]["read"]["allowed"] is True
    assert allowed["read_write_network_policy"]["write"]["allowed"] is False
    assert allowed["read_write_network_policy"]["network"]["allowed"] is False
    assert allowed["dangerous_command_classifier"]["matched_class"] == "none"
    assert allowed["decision"]["shell_parse"]["kind"] == "simple_command"
    assert allowed["decision"]["network_policy"]["blocked"] is False
    assert allowed["decision"]["resolved_argv"]
    assert Path(allowed["decision"]["resolved_argv"][0]).is_absolute()
    assert allowed["sandbox_summary"]["profile"] == "analysis_readonly"
    assert cat_read["decision"]["action"] == "allow"
    assert cat_read["safety"]["class"] == "read_only"
    assert cat_read["command_fingerprint"]
    assert git_write["decision"]["action"] == "forbidden"
    assert git_write["decision"]["risk"] == "mutating"
    assert git_write["safety"]["class"] == "workspace_write"
    assert blocked["decision"]["action"] == "forbidden"
    assert blocked["decision"]["risk"] == "destructive"
    assert blocked["command_class"] == "destructive"
    assert blocked["safety"]["class"] == "destructive"
    assert blocked["approval"]["status"] == "blocked"
    assert blocked["approval_gate"]["gate"] == "block"
    assert blocked["dangerous_command_classifier"]["matched_class"] == "host_destructive"
    assert blocked["dangerous_command_classifier"]["severity"] == "critical"
    assert blocked["block_explanation"]["blocked"] is True
    assert "delete" in blocked["block_explanation"]["remediation"].lower() or "reset" in blocked["block_explanation"]["remediation"].lower()
    assert package_network["decision"]["risk"] == "network"
    assert package_network["command_class"] == "network"
    assert package_network["safety"]["class"] == "network"
    assert package_network["approval_template"]["template_id"] == "network_exception"
    assert package_network["read_write_network_policy"]["network"]["blocked"] is True
    assert package_network["network_policy_decision"]["blocked"] is True
    assert package_network["network_policy_decision"]["code"] == "package_network_operation"
    assert redirection["decision"]["action"] == "forbidden"
    assert redirection["decision"]["blocked_syntax"]["code"] == "shell_metacharacter"
    assert redirection["dangerous_command_classifier"]["matched_class"] == "shell_escape"
    assert git_internal["decision"]["action"] == "forbidden"
    assert "sk-secretvalue" not in redacted["command"]


def test_exec_policy_snapshot_exposes_command_classes_gates_and_per_tool_policy() -> None:
    snapshot = ExecPolicyService().snapshot()

    assert {"read_only", "build_test", "network", "mutation", "destructive"} <= set(snapshot["command_class_model"])
    assert snapshot["approval_gates"]["block"]["classes"] == ["network", "destructive", "unknown"]
    assert snapshot["generated_command_default"]["action"] == "forbidden"
    assert snapshot["read_write_network_policy"]["network"]["default"] == "blocked"
    assert snapshot["dangerous_command_classifier"]["shell_escape"]["action"] == "forbidden"
    assert snapshot["approval_templates"]["draft_mutation"]["scope"] == "run_draft"
    assert snapshot["per_tool_policy"]["shell.exec"]["dangerous_generated_default"] == "forbidden"
    assert snapshot["per_tool_policy"]["patch.apply"]["approval_template"] == "draft_mutation"
    assert snapshot["per_tool_limits"]["shell.exec"]["timeout_seconds"] == 30


def test_workspace_policy_records_denials_and_scoped_approval_grants(tmp_path: Path) -> None:
    app, client, workspace, run = _workspace_with_run(tmp_path)
    workspace_id = workspace["workspace_id"]

    denied = client.post(
        f"/workspaces/{workspace_id}/policy/evaluate-command",
        json={"command": "rm -rf miniapp", "run_id": run.run_id},
    ).json()
    recent = client.get("/system/permissions/recent-denials").json()
    audit = client.get(f"/workspaces/{workspace_id}/permissions/command-audit").json()

    assert denied["decision"]["action"] == "forbidden"
    assert denied["decision"]["risk"] == "destructive"
    assert denied["command_class"] == "destructive"
    assert denied["block_explanation"]["code"]
    assert any(item.get("workspace_id") == workspace_id and item.get("safety_class") == "destructive" for item in recent["items"])
    assert audit["schema"] == "grounded.command_audit.v1"
    assert audit["items"][0]["outcome"] == "blocked"
    assert audit["items"][0]["command_class"] == "destructive"

    app.state.container.exec_policy_service.policy = AgentCommandPolicy(
        [
            CommandPolicyRule(
                prefixes=(("python", "-m", "pytest"),),
                action="prompt",
                reason="Pytest requires a workspace-scoped approval in this test policy.",
                rule_id="test_prompt_pytest",
            )
        ]
    )
    prompt_eval = client.post(
        f"/workspaces/{workspace_id}/policy/evaluate-command",
        json={"command": "python -m pytest miniapp/tests", "run_id": run.run_id},
    ).json()
    approvals = client.get(f"/runs/{run.run_id}/approvals").json()

    approval_id = prompt_eval["approval"]["approval_id"]
    assert prompt_eval["approval"]["required"] is True
    assert prompt_eval["approval"]["scope"] == "workspace"
    assert any(item["approval_id"] == approval_id and item["workspace_id"] == workspace_id for item in approvals["items"])

    approved = client.post(f"/runs/{run.run_id}/approvals/{approval_id}/approve").json()
    grants = client.get(f"/workspaces/{workspace_id}/permissions/approval-grants").json()
    granted_eval = client.post(
        f"/workspaces/{workspace_id}/policy/evaluate-command",
        json={"command": "python -m pytest miniapp/tests", "run_id": run.run_id},
    ).json()
    prefix_granted_eval = client.post(
        f"/workspaces/{workspace_id}/policy/evaluate-command",
        json={"command": "python -m pytest miniapp/tests -q", "run_id": run.run_id},
    ).json()
    audit_after_grant = client.get(f"/workspaces/{workspace_id}/permissions/command-audit").json()

    assert approved["status"] == "approved"
    assert any(item["command_fingerprint"] == prompt_eval["command_fingerprint"] for item in grants["items"])
    assert any(item.get("grant_scope") == "approved_command_prefix" and item.get("prefix_fingerprint") == prompt_eval["command_prefix"]["prefix_fingerprint"] for item in grants["items"])
    assert granted_eval["approval"]["required"] is False
    assert granted_eval["approval"]["status"] == "approved_by_workspace_grant"
    assert prefix_granted_eval["approval"]["required"] is False
    assert prefix_granted_eval["approval"]["status"] == "approved_by_workspace_grant"
    assert prefix_granted_eval["approval"]["grant_scope"] == "approved_command_prefix"
    assert granted_eval["decision"]["action"] == "allow"
    assert granted_eval["decision"]["original_action"] == "prompt"
    assert any(item["outcome"] == "allowed" and item["command_fingerprint"] == granted_eval["command_fingerprint"] for item in audit_after_grant["items"])


def test_sandbox_service_blocks_traversal_symlinks_and_hardlink_writes(tmp_path: Path) -> None:
    sandbox = SandboxService()
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    inside_link = root / "inside_link.txt"
    outside_link = root / "outside_link.txt"
    inside_link.symlink_to(inside)
    outside_link.symlink_to(outside)
    linked = root / "linked.txt"
    linked.write_text("linked", encoding="utf-8")
    try:
        os.link(linked, tmp_path / "linked-peer.txt")
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")

    assert sandbox.resolve_path(root, "inside.txt", operation="read", profile="analysis_readonly").allowed
    assert sandbox.resolve_path(root, "../outside.txt", operation="read", profile="analysis_readonly").allowed is False
    assert sandbox.resolve_path(root, "bad\\path.txt", operation="read", profile="analysis_readonly").allowed is False
    assert sandbox.resolve_path(root, "inside_link.txt", operation="read", profile="analysis_readonly").allowed
    assert sandbox.resolve_path(root, "outside_link.txt", operation="read", profile="analysis_readonly").allowed is False
    assert sandbox.resolve_path(root, "inside_link.txt", operation="write", profile="agent_draft_write").allowed is False
    assert sandbox.resolve_path(root, "linked.txt", operation="write", profile="agent_draft_write").allowed is False


def test_sandbox_execution_fails_closed_without_hard_network_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = SandboxService()
    monkeypatch.setattr(sandbox, "network_provider", lambda: "none")
    plan = sandbox.build_execution_plan(root=tmp_path, cwd=tmp_path, argv=["python3", "-c", "print('x')"])

    assert plan.allowed is False
    assert plan.enforcement == "unavailable"
    assert any(item.code == "network_provider_unavailable" for item in plan.violations)


def test_sandbox_runtime_manifest_exposes_execution_boundary_contract() -> None:
    manifest = SandboxService().manifest()

    assert manifest["schema"] == "grounded.sandbox_runtime.manifest.v1"
    assert manifest["execution_boundary"]["isolated_generated_app_workspace"]
    assert manifest["network_policy"]["default"] == "blocked"
    assert manifest["process_timeout"]["termination"] == ["SIGTERM process group", "SIGKILL process group after grace"]
    assert "stdout" in manifest["log_capture"]
    assert "timeout" in manifest["killed_process_diagnostics"]["reasons"]
    assert manifest["preview_lifecycle"]["destroy"]


def test_agent_process_manager_returns_runtime_boundary_and_timeout_diagnostics(tmp_path: Path) -> None:
    draft = tmp_path / "draft"
    (draft / "miniapp").mkdir(parents=True)
    decision = CommandPolicyDecision(
        action="allow",
        reason="test command",
        command="sleep via python",
        normalized_command="sleep via python",
        argv=(sys.executable, "-c", "import time; time.sleep(2)"),
        resolved_argv=(sys.executable, "-c", "import time; time.sleep(2)"),
        matched_prefix=(Path(sys.executable).name,),
        cwd_policy="miniapp",
        safety_class="read_only",
    )

    result = AgentProcessManager(sandbox_service=SandboxService(strict_network=False)).run(
        draft_source=draft,
        command=decision.command,
        decision=decision,
        timeout_seconds=1,
        max_output_chars=4000,
    ).as_dict()

    assert result["semantic_status"] == "timeout"
    assert result["sandbox_boundary"]["schema"] == "grounded.sandbox.runtime_boundary.v1"
    assert result["sandbox_boundary"]["filesystem"]["write_roots"]
    assert result["environment_snapshot"]["env_sha256"]
    assert result["log_capture"]["stdout"]["stream"] == "stdout"
    assert result["killed_diagnostics"]["reason"] == "timeout"
    assert result["killed_diagnostics"]["requested_signal"] == "SIGTERM"


def test_sandbox_exec_blocks_raw_socket_on_macos(tmp_path: Path) -> None:
    sandbox = SandboxService()
    if sandbox.network_provider() != "sandbox-exec":
        pytest.skip("macOS sandbox-exec provider is not available")
    python = shutil.which("python3")
    if not python:
        pytest.skip("python3 is unavailable")
    command = "python3 -c socket-connect"
    code = "import socket; s=socket.socket(); s.settimeout(.2); s.connect(('1.1.1.1',80)); print('connected')"
    decision = CommandPolicyDecision(
        action="allow",
        reason="test",
        command=command,
        normalized_command=command,
        argv=("python3", "-c", code),
        resolved_argv=(python, "-c", code),
        matched_prefix=("python3",),
        network_policy={"blocked": False},
    )

    result = AgentProcessManager(sandbox_service=sandbox).run(
        draft_source=tmp_path,
        command=command,
        decision=decision,
        timeout_seconds=5,
        max_output_chars=4000,
    )

    assert result.policy_decision["sandbox"]["provider"] == "sandbox-exec"
    assert result.policy_decision["sandbox"]["enforcement"] == "hard"
    assert result.exit_code != 0
    assert "PermissionError" in result.stderr.get("excerpt", "")


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


def test_exec_policy_json_amendments_are_additive_and_cannot_unblock_network() -> None:
    policy = AgentCommandPolicy.from_rule_payload(
        {
            "schema": "grounded.agent_exec_policy.v1",
            "source": "amendments.json",
            "amendments": [
                {
                    "prefixes": [["python3", "-m", "pytest"]],
                    "action": "allow",
                    "reason": "Allow focused pytest diagnostics.",
                    "match": ["python3 -m pytest miniapp/tests -q"],
                },
                {
                    "prefixes": [["curl"]],
                    "action": "allow",
                    "reason": "Unsafe network override should not take effect.",
                    "match": ["curl https://example.com"],
                },
            ],
        }
    )

    pytest_decision = policy.decide("python3 -m pytest miniapp/tests -q")
    curl_decision = policy.decide("curl https://example.com")

    assert pytest_decision.action == "allow"
    assert pytest_decision.matched_amendments
    assert curl_decision.action == "forbidden"
    assert curl_decision.network_policy["code"] == "direct_network_tool"


def test_policy_simulation_endpoint_returns_matched_rules(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    response = client.post("/policy/evaluate", json={"command": "python3 -m pip install requests"}).json()
    doctor = client.get("/doctor").json()

    assert doctor["schema"] == "grounded.doctor_health_panel.v1"
    assert response["decision"]["action"] == "forbidden"
    assert response["decision"]["network_policy"]["code"] == "package_network_operation"
    assert response["shell_parse"]["kind"] == "simple_command"
    assert response["matched_rules"]
    assert response["selected_decision"] == "forbidden"
    assert response["policy_file"]["status"] == "loaded"
    check_names = {item["name"] for item in doctor["checks"]}
    assert "exec_policy" in check_names
    assert {
        "python_deps",
        "backend_imports",
        "preview_runtime",
        "db_writable",
        "browser_availability",
        "docker_daemon",
        "playwright_browsers",
        "model_access",
        "writable_dirs",
        "disk_space",
        "template_hash",
        "preview_port_range",
        "runtime_policy_files",
    }.issubset(check_names)
    section_keys = {item["key"] for item in doctor["sections"]}
    assert {"python", "node", "browser", "backend", "preview", "storage", "templates", "policy"}.issubset(section_keys)
    assert doctor["summary"]["total"] == len(doctor["checks"])
    template_hash = next(item for item in doctor["checks"] if item["name"] == "template_hash")
    disk_space = next(item for item in doctor["checks"] if item["name"] == "disk_space")
    assert "sha256=" in template_hash["details"]
    assert "free=" in disk_space["details"]


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


def test_run_tasks_exposes_runtime_task_ledger_with_proof_and_blockers(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Runtime Ledger Workspace",
            "description": "runtime ledger test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a " + "book" + "ing workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="blocked",
        current_stage="completion_gate",
        implementation_plan={
            "product_task_ledger": [
                {
                    "id": "client.role_surface",
                    "role": "client",
                    "kind": "source",
                    "content": "Client books a slot.",
                    "owned_paths": ["miniapp/app/static/client/app.js"],
                    "proof_checks": ["platform_invariants", "browser_flow_smoke"],
                },
                {
                    "id": "manager.role_surface",
                    "role": "manager",
                    "kind": "observer",
                    "content": "Manager reviews " + "book" + "ings.",
                    "owned_paths": ["miniapp/app/static/manager/app.js"],
                    "proof_checks": ["platform_invariants"],
                },
            ]
        },
        remaining_issues=[
            {
                "kind": "product_task_ledger",
                "ledger_item_id": "manager.role_surface",
                "role": "manager",
                "details": "manager route missing",
                "blocking": True,
            }
        ],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"check_results:{workspace['workspace_id']}",
        {
            "run_id": run.run_id,
            "items": [
                RunCheckResult(name="platform_invariants", status="passed").model_dump(mode="json"),
                RunCheckResult(name="browser_flow_smoke", status="passed").model_dump(mode="json"),
            ],
        },
    )

    lane = client.get(f"/runs/{run.run_id}/tasks").json()

    assert lane["task_ledger"]["schema"] == "grounded.run_task_ledger.v1"
    assert lane["task_graph"]["schema"] == "grounded.run_task_graph.v1"
    by_id = {item["task_id"]: item for item in lane["items"] if item["source"] == "runtime_task_ledger"}
    assert by_id["client.role_surface"]["status"] == "completed"
    assert by_id["client.role_surface"]["proof_status"] == "passed"
    assert by_id["manager.role_surface"]["status"] == "blocked"
    assert by_id["manager.role_surface"]["blocker"]["details"] == "manager route missing"
    graph_by_id = {item["task_id"]: item for item in lane["task_graph"]["nodes"]}
    assert graph_by_id["planner.plan_ready"]["status"] == "completed"
    assert graph_by_id["client.role_surface"]["dependencies"] == ["planner.plan_ready"]
    assert graph_by_id["repair.resolve_blockers"]["ready"] is True
    assert lane["task_graph"]["blockers"][0]["task_id"] == "manager.role_surface"
    assert any(edge["from"] == "planner.plan_ready" and edge["to"] == "client.role_surface" for edge in lane["task_graph"]["edges"])


def test_skillify_successful_run_generates_and_writes_user_skill(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Skillify Workspace",
            "description": "skillify test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Create a salon " + "book" + "ing app with client " + "book" + "ing, specialist schedule, and manager utilization.",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js", "miniapp/app/routes/" + "book" + "ings.py"],
        acceptance_contract={
            "required": True,
            "flows": [
                {
                    "id": "book_slot",
                    "name": "Book a salon slot",
                    "roles": ["client", "specialist", "manager"],
                    "api_paths": ["/api/" + "book" + "ings"],
                }
            ],
        },
        implementation_plan={
            "product_task_ledger": [
                {
                    "id": "client.role_surface",
                    "role": "client",
                    "kind": "source",
                    "content": "Client chooses a service and books a free slot.",
                    "owned_paths": ["miniapp/app/static/client/app.js"],
                    "proof_checks": ["browser_flow_smoke"],
                },
                {
                    "id": "shared_state.persistence_api",
                    "kind": "shared_state",
                    "content": "Book" + "ings persist through FastAPI routes.",
                    "owned_paths": ["miniapp/app/routes/" + "book" + "ings.py"],
                    "proof_checks": ["api_workflow_smoke"],
                },
            ]
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "run_id": run.run_id,
            "check_results": [
                RunCheckResult(name="api_workflow_smoke", status="passed", details="created " + "book" + "ing").model_dump(mode="json"),
                RunCheckResult(name="browser_flow_smoke", status="passed", details="client booked slot").model_dump(mode="json"),
            ],
        },
    )

    skill_id = "salon-" + "book" + "ing"
    preview = client.post(f"/runs/{run.run_id}/skillify", json={"skill_id": skill_id, "title": "Salon Booking", "write": False}).json()
    written = client.post(f"/runs/{run.run_id}/skillify", json={"skill_id": skill_id, "title": "Salon Booking", "write": True}).json()
    skills = client.get("/skills").json()
    slash_commands = client.get("/slash-commands").json()
    slash = client.post("/slash-commands/skillify/execute", json={"run_id": run.run_id, "metadata": {"skill_id": skill_id + "-preview"}}).json()

    assert preview["schema"] == "grounded.skillify.v1"
    assert preview["write_status"] == "preview"
    assert "metadata_schema: grounded.skill.v2" in preview["content"]
    assert "triggerRules:" in preview["content"]
    assert "requiredProof:" in preview["content"]
    assert "outputExpectations:" in preview["content"]
    assert "## Incompatible Skills" in preview["content"]
    assert "Client chooses a service" in preview["content"]
    assert "browser_flow_smoke" in preview["content"]
    assert written["write_status"] == "written"
    assert Path(written["target_path"]).exists()
    generated = next(item for item in skills["items"] if item["id"] == skill_id and item["scope"] == "user")
    assert generated["metadata_schema"] == "grounded.skill.v2"
    assert generated["trigger_rules"]
    assert "browser_flow_smoke" in generated["required_proof"]
    assert "repair-failed-generation" in generated["incompatible_skills"]
    assert generated["output_expectations"]
    assert any(item["id"] == "skillify" for item in slash_commands["items"])
    assert slash["workflow"] == "skillify_successful_run"
    assert slash["report"]["skill_id"] == skill_id + "-preview"


def test_session_memory_sections_are_exposed_and_embedded_in_workspace_memory(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Session Memory Workspace",
            "description": "sectioned memory test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Add payment flow and repair manager dashboard.",
        intent="edit",
        target_role_scope=["client", "manager"],
        model_profile="test",
        status="failed",
        apply_status="failed",
        current_stage="verify",
        touched_files=["miniapp/app/static/manager/dashboard.js", "miniapp/app/routes/payments.py"],
        acceptance_contract={
            "required": True,
            "flows": [
                {"id": "pay_invoice", "name": "Pay invoice", "roles": ["client"], "api_paths": ["/api/payments"]},
            ],
        },
        implementation_plan={
            "product_task_ledger": [
                {
                    "id": "manager.dashboard",
                    "role": "manager",
                    "content": "Manager dashboard shows revenue and failed payments.",
                    "owned_paths": ["miniapp/app/static/manager/dashboard.js"],
                }
            ]
        },
        failure_signature="manager_dashboard_missing_total",
        failure_reason="Revenue total was not rendered after payment repair.",
        remaining_issues=[{"details": "Payment success state needs browser proof."}],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    for payload in [
        {"kind": "product_decision", "text": "Payment flow must keep manager dashboard revenue in sync."},
        {"kind": "reusable_workflow", "text": "After payment changes, run API workflow smoke before browser proof."},
        {"kind": "failure_shield", "text": "Do not mark payment repair complete until manager revenue total is visible."},
        {"kind": "preference", "text": "Prefer dense operational manager screens."},
    ]:
        response = client.post(f"/workspaces/{workspace['workspace_id']}/memory", json=payload)
        assert response.status_code == 200

    session = client.get(f"/workspaces/{workspace['workspace_id']}/session-memory").json()
    memory = client.get(f"/workspaces/{workspace['workspace_id']}/memory").json()
    section_ids = [section["id"] for section in session["sections"]]

    assert session["schema"] == "grounded.session_memory.v1"
    assert section_ids == [
        "current_state",
        "task_specification",
        "files_and_functions",
        "workflow",
        "errors_and_corrections",
        "learnings",
        "worklog",
    ]
    assert "Current State" in session["text"]
    assert "Pay invoice" in session["text"]
    assert "miniapp/app/static/manager/dashboard.js" in session["text"]
    assert "manager_dashboard_missing_total" in session["text"]
    assert memory["session_memory"]["schema"] == "grounded.session_memory.v1"


def test_workspace_memory_is_split_into_product_memory_types(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Typed Product Memory Workspace",
            "description": "typed memory test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Create invoice workflow. Use label Invoice total and persist invoice_status field in SQLite.",
        intent="create",
        target_role_scope=["client", "manager"],
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/routes/invoices.py", "miniapp/app/static/client/app.js"],
        acceptance_contract={
            "required": True,
            "roles": ["client", "manager"],
            "flows": [{"id": "invoice_pay", "name": "Pay invoice", "roles": ["client"], "api_paths": ["/api/invoices"]}],
        },
        implementation_plan={
            "primary_entities": ["invoice"],
            "role_state_contract": {"entity": "invoice", "fields": ["invoice_status", "invoice_total"]},
            "product_task_ledger": [
                {"id": "client.invoice", "role": "client", "content": "Client sees Invoice total label.", "owned_paths": ["miniapp/app/static/client/app.js"]}
            ],
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "check_results": [
                RunCheckResult(name="api_workflow_smoke", status="passed", details="invoice persisted").model_dump(mode="json"),
                RunCheckResult(name="browser_flow_smoke", status="passed", details="invoice visible").model_dump(mode="json"),
            ]
        },
    )

    client.post(f"/workspaces/{workspace['workspace_id']}/memory", json={"memory_type": "preferences", "text": "Prefer compact manager tables."})
    client.post(f"/workspaces/{workspace['workspace_id']}/memory", json={"memory_type": "rejected_approaches", "text": "Do not use seeded invoice records as proof."})
    client.post(f"/workspaces/{workspace['workspace_id']}/memory", json={"memory_type": "persistence_schema_decisions", "text": "Keep invoice_status as the persisted field name."})
    extracted = client.post(f"/runs/{run.run_id}/memory/extract").json()
    memory = client.get(f"/workspaces/{workspace['workspace_id']}/memory").json()
    retrieved = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory/retrieve",
        json={"prompt": "invoice_status Invoice total manager table", "top_k": 8},
    ).json()

    assert extracted["schema"] == "grounded.memory_stage1.v1"
    assert {item["memory_type"] for item in extracted["items"]} >= {"product_facts", "successful_patterns", "ui_vocabulary", "persistence_schema_decisions"}
    assert set(memory["product_memory_types"]) == {
        "preferences",
        "product_facts",
        "known_failures",
        "successful_patterns",
        "rejected_approaches",
        "ui_vocabulary",
        "persistence_schema_decisions",
    }
    assert memory["preferences"][0]["memory_type"] == "preferences"
    assert any("invoice" in item["text"].lower() for item in memory["product_facts"])
    assert memory["successful_patterns"]
    assert memory["rejected_approaches"]
    assert memory["ui_vocabulary"]
    assert memory["persistence_schema_decisions"]
    assert memory["pipeline"]["type_counts"]["persistence_schema_decisions"] >= 1
    assert retrieved["stats"]["type_counts"]["ui_vocabulary"] >= 1
    assert any("type:" in reason for hit in retrieved["hits"] for reason in hit["selection_reason"])


def test_rollout_trace_exposes_raw_evidence_before_interpretation(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Rollout Trace Workspace",
            "description": "raw trace evidence test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Repair checkout flow.",
        intent="edit",
        model_profile="test",
        status="failed",
        apply_status="failed",
        current_stage="checks",
        failure_class="check_failed",
        failure_signature="checkout_total_missing",
        failure_reason="Checkout total was not visible after repair.",
        rollout_trace_ref=f"rollout_trace:{workspace['workspace_id']}:run_rollout",
        tool_trace_ref=f"tool_trace:{workspace['workspace_id']}:run_rollout",
        process_outputs_ref=f"process_outputs:{workspace['workspace_id']}:run_rollout",
        trace_bundle_ref=f"trace_bundle:{workspace['workspace_id']}:run_rollout",
        trace_reducer_ref=f"trace_reducer:{workspace['workspace_id']}:run_rollout",
        worker_drafts_ref=f"worker_drafts:{workspace['workspace_id']}:run_rollout",
        worker_branch_refs=[f"worker_drafts:{workspace['workspace_id']}:run_rollout"],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        run.rollout_trace_ref,
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "events": [
                {"sequence": 1, "event_type": "agent_turn_started", "payload": {"summary": "turn 1"}, "created_at": "2026-05-21T00:00:00+00:00"},
                {"sequence": 2, "event_type": "model_prompt_response", "payload": {"tool_calls": [{"tool": "run_command"}]}, "created_at": "2026-05-21T00:00:01+00:00"},
                {"sequence": 3, "event_type": "tool_failed_reason", "payload": {"details": {"status": "failed", "tool": "run_command", "reason": "pytest failed"}}, "created_at": "2026-05-21T00:00:02+00:00"},
            ],
            "graph": [{"sequence": 3, "event_type": "tool_failed_reason", "status": "failed", "summary": "pytest failed"}],
        },
    )
    app.state.container.store.upsert(
        "reports",
        run.tool_trace_ref,
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "items": [{"tool_use_id": "tool_1", "tool": "run_command", "status": "failed", "command": "pytest", "exit_code": 1}],
        },
    )
    app.state.container.store.upsert(
        "reports",
        run.process_outputs_ref,
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "items": [{"tool_use_id": "tool_1", "command": "pytest", "exit_code": 1, "stderr_tail": "checkout total missing"}],
        },
    )
    app.state.container.store.upsert(
        "reports",
        run.trace_bundle_ref,
        {
            "schema": "grounded.trace_bundle.v1",
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "status": "reduced",
            "event_count": 3,
            "state": {
                "schema": "grounded.trace_bundle_state.v1",
                "workspace_id": workspace["workspace_id"],
                "run_id": run.run_id,
                "event_count": 3,
                "blockers": [{"seq": 3, "event_type": "tool_failed_reason", "status": "failed"}],
                "tool_calls": [{"seq": 3, "event_type": "tool_failed_reason", "summary": "pytest failed"}],
                "prompt_contexts": [{"seq": 2, "event_type": "model_prompt_response"}],
                "payload_refs": [{"seq": 3, "event_type": "tool_failed_reason", "payload_ref": "payloads/000003_tool_failed_reason.json"}],
                "next_action": {"action": "repair", "reason": "pytest failed"},
            },
        },
    )
    app.state.container.store.upsert(
        "reports",
        run.worker_drafts_ref,
        {"workspace_id": workspace["workspace_id"], "run_id": run.run_id, "items": [{"worker_id": "frontend_worker", "status": "failed"}]},
    )

    trace = client.get(f"/runs/{run.run_id}/rollout-trace").json()

    assert trace["schema"] == "grounded.rollout_trace_evidence.v1"
    assert trace["principle"] == "raw_evidence_first_interpret_later"
    assert trace["raw_events"][0]["event_type"] == "agent_turn_started"
    assert trace["payload_refs"][0]["payload_ref"] == "payloads/000003_tool_failed_reason.json"
    assert trace["inference_calls"]
    assert any(item.get("tool") == "run_command" for item in trace["tool_calls"])
    assert any(item.get("exit_code") == 1 for item in trace["terminal_ops"])
    assert trace["child_agents"][0]["worker_id"] == "frontend_worker"
    assert trace["interpretations"]["run_failure"]["failure_signature"] == "checkout_total_missing"
    assert trace["repair_learning_hooks"]["can_extract_failure_shield"] is True


def test_simplify_pass_reviews_changed_files_after_green_gate(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Simplify Workspace",
            "description": "post green simplify test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    source_dir = app.state.container.workspace_service.source_dir(workspace["workspace_id"])
    client_dir = source_dir / "miniapp" / "app" / "static" / "client"
    manager_dir = source_dir / "miniapp" / "app" / "static" / "manager"
    client_dir.mkdir(parents=True, exist_ok=True)
    manager_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "app.js").write_text(
        """
const stateKey = "checkout-state";
function renderCard(item) { return `<div class="card stack row primary">${item}</div>`; }
function renderCard(item) { return `<div class="card stack row primary">${item}</div>`; }
const total = document.querySelector("#total");
document.querySelector("#total");
document.querySelector("#total");
document.querySelector("#total");
localStorage.setItem("checkout-state", JSON.stringify({ total: 10 }));
fetch("/api/checkout");
""",
        encoding="utf-8",
    )
    (manager_dir / "app.js").write_text(
        """
const saved = localStorage.getItem("checkout-state");
fetch("/api/checkout");
""",
        encoding="utf-8",
    )
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build checkout flow.",
        intent="create",
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js", "miniapp/app/static/manager/app.js"],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert("reports", f"gate:{run.run_id}", {"status": "passed", "blocking": False, "issues": []})

    report = client.post(f"/runs/{run.run_id}/simplify").json()
    slash = client.post("/slash-commands/simplify/execute", json={"run_id": run.run_id}).json()

    assert report["schema"] == "grounded.simplify_pass.v1"
    assert report["status"] == "needs_simplify"
    categories = report["summary"]["categories"]
    assert categories["selectors"] >= 1
    assert categories["state_consistency"] >= 1
    assert any(task["task_id"].startswith("simplify.") for task in report["safe_refactor_tasks"])
    assert slash["workflow"] == "post_green_simplify"
    assert slash["report"]["status"] == "needs_simplify"


def test_guardian_pre_mutation_review_blocks_destructive_repeated_actions(tmp_path: Path) -> None:
    draft_source = tmp_path / "draft"
    target = draft_source / "miniapp" / "app"
    target.mkdir(parents=True)
    (target / "main.py").write_text("API_KEY = 'sk-secretvalue1234567890'\n", encoding="utf-8")
    changes = [
        DraftAction(file_path="miniapp/app/main.py", operation="replace", content="API_KEY = 'sk-secretvalue1234567890'\n", reason="update"),
        DraftAction(file_path="miniapp/app/old.py", operation="delete", reason="remove"),
    ]

    first = GuardianReview.review_risky_action(
        workspace_id="ws_guardian",
        run_id="run_guardian",
        draft_source=draft_source,
        file_changes=changes,
        action_kind="draft_apply",
    ).model_dump(mode="json", by_alias=True)
    prior = [
        {"signature": GuardianReview._rejection_signature(code=str(item["code"]), paths=first["evidence"]["changed_files"])}
        for item in first["findings"]
    ]
    repeated = GuardianReview.review_risky_action(
        workspace_id="ws_guardian",
        run_id="run_guardian",
        draft_source=draft_source,
        file_changes=changes,
        action_kind="draft_apply",
        previous_rejections=prior,
    ).model_dump(mode="json", by_alias=True)

    assert first["status"] == "failed"
    assert first["summary"]["risk_level"] == "critical"
    assert any(item["code"] == "guardian.destructive_action.delete_operation" for item in first["findings"])
    assert any(item["code"] == "guardian.security_privacy.hardcoded_secret" for item in first["findings"])
    assert repeated["final_review_gate"]["rejection_circuit_open"] is True
    assert any(item["code"] == "guardian.rejection_circuit.repeated_rejected_action" for item in repeated["findings"])
    assert repeated["review_prompt"]["action_kind"] == "draft_apply"


def test_debug_stuck_and_doctor_workflows_emit_repair_packets(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Debug Workflow Workspace",
            "description": "debug workflow test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    logs_dir = tmp_path / "workspaces" / workspace["workspace_id"] / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "api.log").write_text('{"level":"ERROR","message":"NameError in miniapp/app/routes/checkout.py"}\n', encoding="utf-8")
    (logs_dir / "platform.log").write_text('{"level":"INFO","message":"agent started"}\n', encoding="utf-8")
    failed_run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Fix checkout API.",
        intent="edit",
        model_profile="test",
        status="failed",
        apply_status="failed",
        current_stage="checks",
        failure_class="api_workflow",
        failure_signature="api_workflow_smoke.checkout_name_error",
        touched_files=["miniapp/app/routes/checkout.py"],
    )
    stuck_run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Continue checkout API.",
        intent="edit",
        model_profile="test",
        status="running",
        apply_status="pending",
        current_stage="agent_loop",
        touched_files=["miniapp/app/routes/checkout.py"],
    )
    app.state.container.store.upsert("runs", failed_run.run_id, failed_run.model_dump(mode="json"))
    app.state.container.store.upsert("runs", stuck_run.run_id, stuck_run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{failed_run.run_id}",
        {
            "check_results": [
                {
                    "name": "api_workflow_smoke",
                    "status": "failed",
                    "details": "NameError: checkout_total in miniapp/app/routes/checkout.py",
                    "logs": ["Traceback miniapp/app/routes/checkout.py"],
                }
            ]
        },
    )
    app.state.container.store.upsert(
        "previews",
        workspace["workspace_id"],
        {
            "workspace_id": workspace["workspace_id"],
            "status": "error",
            "stage": "error",
            "runtime_mode": "local",
            "last_error": "NameError in miniapp/app/routes/checkout.py",
            "logs": ["Preview failed: miniapp/app/routes/checkout.py NameError"],
        },
    )

    debug = client.get(f"/runs/{failed_run.run_id}/debug").json()
    stuck = client.get(f"/runs/{stuck_run.run_id}/stuck").json()
    doctor = client.get(f"/workspaces/{workspace['workspace_id']}/doctor-workspace").json()
    debug_slash = client.post("/slash-commands/debug-run/execute", json={"run_id": failed_run.run_id}).json()
    stuck_slash = client.post("/slash-commands/stuck-run/execute", json={"run_id": stuck_run.run_id}).json()
    doctor_slash = client.post("/slash-commands/doctor-workspace/execute", json={"workspace_id": workspace["workspace_id"]}).json()

    assert debug["schema"] == "grounded.diagnostic_workflow.v1"
    assert debug["mode"] == "debug_run"
    assert debug["repair_packet"]["schema"] == "grounded.repair_packet.v2"
    assert debug["repair_packet"]["target_files"] == ["miniapp/app/routes/checkout.py"]
    assert debug["repair_packet"]["owner"] == "backend_api_worker"
    assert stuck["mode"] == "stuck_run"
    assert stuck["diagnosis"]["stuck"]["kind"] == "active_not_terminal"
    assert doctor["mode"] == "doctor_workspace"
    assert doctor["environment_health"]["schema"] == "grounded.doctor_health_panel.v1"
    assert doctor["repair_packet"]["target_files"][0] == "miniapp/app/routes/checkout.py"
    assert debug_slash["workflow"] == "debug_run"
    assert stuck_slash["workflow"] == "stuck_run"
    assert doctor_slash["workflow"] == "doctor_workspace"


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


def test_lsp_diagnostics_async_task_persists_report(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Async LSP Workspace",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    source = app.state.container.workspace_service.source_dir(workspace["workspace_id"])
    app_dir = source / "miniapp" / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "main.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    started = client.post(
        f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp/async",
        json={"files": ["miniapp/app/main.py"], "changed_only": False},
    ).json()
    task = _wait_for_background_task(client, started["task"]["task_id"])
    status = client.get(f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp/async/{task['task_id']}").json()

    assert started["schema"] == "grounded.lsp_diagnostics_task.v1"
    assert task["status"] == "completed"
    assert status["diagnostics"]["schema"] == "grounded.lsp_diagnostics.v1"
    assert status["diagnostics"]["status"] == "failed"
    assert any(item["source"] == "python_compile" for item in status["diagnostics"]["items"])
    assert any(item["event_type"] == "diagnostic_stream" for item in status["output"]["items"])


def test_pr_babysitter_snapshot_surfaces_ci_review_and_retry_actions(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "PR Watch",
            "description": "watch exported app",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = PrBabysitterService(store=app.state.container.store, workspace_service=app.state.container.workspace_service)

    def fake_gh_json(args: list[str], repo: str | None = None) -> Any:
        if args[:2] == ["api", "user"]:
            return {"login": "rafailvv"}
        if args[:2] == ["pr", "view"]:
            return {
                "number": 12,
                "url": "https://github.com/acme/exported-app/pull/12",
                "state": "OPEN",
                "mergedAt": None,
                "closedAt": None,
                "headRefName": "codex/exported-app",
                "headRefOid": "sha123",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "UNSTABLE",
                "reviewDecision": "CHANGES_REQUESTED",
                "headRepositoryOwner": {"login": "acme"},
                "headRepository": {"name": "exported-app"},
            }
        if args[:2] == ["pr", "checks"]:
            assert repo == "acme/exported-app"
            return [
                {"name": "build", "bucket": "fail", "state": "COMPLETED", "workflow": "ci"},
                {"name": "lint", "bucket": "pass", "state": "COMPLETED", "workflow": "ci"},
            ]
        if args[:2] == ["api", "repos/acme/exported-app/actions/runs"]:
            return {"workflow_runs": [{"id": 777, "name": "build", "head_sha": "sha123", "status": "completed", "conclusion": "failure", "html_url": "https://github.com/acme/exported-app/actions/runs/777"}]}
        if args and args[0] == "api" and "/issues/12/comments" in args[1]:
            return [{"id": 1, "user": {"login": "teammate"}, "author_association": "MEMBER", "created_at": "2026-01-01T00:00:00Z", "body": "Please fix the failing build.", "html_url": "https://github.com/acme/exported-app/pull/12#issuecomment-1"}]
        if args and args[0] == "api" and "/pulls/12/comments" in args[1]:
            return []
        if args and args[0] == "api" and "/pulls/12/reviews" in args[1]:
            return []
        raise AssertionError(f"unexpected gh call: {args!r}")

    service._gh_json = fake_gh_json  # type: ignore[method-assign]
    report = service.snapshot(workspace_id=workspace["workspace_id"], pr="12", repo="acme/exported-app", run_id="run_1", export_id="export_1")
    second = service.snapshot(workspace_id=workspace["workspace_id"], pr="12", repo="acme/exported-app", run_id="run_1", export_id="export_1")
    listed = service.list_reports(workspace_id=workspace["workspace_id"])

    assert report["schema"] == "grounded.pr_babysitter.v1"
    assert report["actions"][:3] == ["process_review_comment", "diagnose_ci_failure", "retry_failed_checks"]
    assert report["failure_diagnostics"]["classification"] == "likely_branch_related"
    assert report["automation_plan"]["auto_fix_push"]["enabled"] is True
    assert second["new_review_items"] == []
    assert listed["latest"]["pr"]["number"] == 12


def test_pr_babysitter_background_task_uses_service_snapshot(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "PR Background",
            "description": "pr babysitter task",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
        return {
            "schema": "grounded.pr_babysitter.v1",
            "workspace_id": kwargs["workspace_id"],
            "status": "ready",
            "actions": ["ready_to_merge"],
            "checks": {"passed_count": 3, "failed_count": 0, "pending_count": 0, "all_terminal": True},
            "pr": {"number": 9, "repo": "acme/exported-app"},
        }

    app.state.container.pr_babysitter_service.snapshot = fake_snapshot  # type: ignore[method-assign]
    created = client.post(
        f"/workspaces/{workspace['workspace_id']}/pr-babysitter/watch",
        json={"pr": "9", "repo": "acme/exported-app", "max_polls": 1},
    ).json()
    task = _wait_for_background_task(client, created["task"]["task_id"])
    output = client.get(f"/tasks/{task['task_id']}/output").json()

    assert task["type"] == "pr_ci_babysit"
    assert task["status"] == "completed"
    assert task["linked_refs"]["actions"] == ["ready_to_merge"]
    assert task["linked_refs"]["watch"]["polls_completed"] == 1
    assert output["items"][-1]["payload"]["actions"] == ["ready_to_merge"]


def test_pr_babysitter_watch_polls_until_terminal_action(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "PR Watch Loop",
            "description": "pr babysitter watch loop",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    snapshots: list[list[str]] = [["idle"], ["stop_pr_closed"]]

    def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
        actions = snapshots.pop(0)
        return {
            "schema": "grounded.pr_babysitter.v1",
            "workspace_id": kwargs["workspace_id"],
            "status": "stopped" if actions == ["stop_pr_closed"] else "watching",
            "actions": actions,
            "checks": {"passed_count": 1, "failed_count": 0, "pending_count": 1, "all_terminal": False},
            "pr": {"number": 9, "repo": "acme/exported-app", "closed": actions == ["stop_pr_closed"]},
        }

    app.state.container.pr_babysitter_service.snapshot = fake_snapshot  # type: ignore[method-assign]
    created = client.post(
        f"/workspaces/{workspace['workspace_id']}/pr-babysitter/watch",
        json={"pr": "9", "repo": "acme/exported-app", "max_polls": 5, "poll_seconds": 0},
    ).json()
    task = _wait_for_background_task(client, created["task"]["task_id"])
    output = client.get(f"/tasks/{task['task_id']}/output").json()

    assert task["status"] == "completed"
    assert task["linked_refs"]["watch"]["polls_completed"] == 2
    assert task["linked_refs"]["watch"]["terminal_reason"] == "stop_pr_closed"
    assert [item["payload"]["actions"] for item in output["items"] if item["event_type"] == "pr_snapshot"] == [["idle"], ["stop_pr_closed"]]


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


def test_hook_policy_apis_validate_store_and_evaluate(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Hook Workspace",
            "description": "Hook policy endpoint test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    manifest = client.get("/system/policies/hooks").json()
    stored = client.put(
        f"/workspaces/{workspace['workspace_id']}/hooks",
        json={
            "policy_id": "workspace-hooks",
            "rules": [
                {
                    "rule_id": "block_shell",
                    "conditions": {"hook": "pre_tool_use", "canonical_tool": "shell.exec"},
                    "actions": [{"action": "block", "reason": "Shell is disabled for this workspace."}],
                },
                {
                    "rule_id": "bad_secret",
                    "conditions": {"hook": "before_apply"},
                    "actions": [{"action": "add_context", "text": "ghp_abcdefghijklmnopqrstuvwxyz123456"}],
                },
            ],
        },
    ).json()
    fetched = client.get(f"/workspaces/{workspace['workspace_id']}/hooks").json()
    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/hooks/evaluate",
        json={"hook": "pre_tool_use", "payload": {"tool": "shell.exec", "model_tool": "run_command", "risk": "network"}},
    ).json()

    assert manifest["schema"] == "grounded.hook_policy_manifest.v1"
    assert "pre_tool_use" in manifest["supported_hooks"]
    assert stored["workspace_id"] == workspace["workspace_id"]
    assert stored["validation_issues"][0]["code"] == "secret_like_content"
    assert fetched["policy"]["policy_id"] == "workspace-hooks"
    assert evaluation["should_block"] is True
    assert evaluation["block_reason"] == "Shell is disabled for this workspace."
    assert evaluation["matched_rules"][0]["rule_id"] == "block_shell"


def test_hook_runtime_manifest_output_parser_and_context_injection(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Hook Runtime Workspace",
            "description": "Hook runtime test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    client.put(
        f"/workspaces/{workspace['workspace_id']}/hooks",
        json={
            "policy_id": "runtime-hooks",
            "rules": [
                {
                    "rule_id": "prompt_context",
                    "conditions": {"hook": "user_prompt_submit"},
                    "actions": [{"action": "add_context", "text": "Prefer small, repairable product increments.", "target": "system_context", "priority": 50}],
                },
                {
                    "rule_id": "permission_tag",
                    "conditions": {"hook": "permission_request", "canonical_tool": "shell.exec"},
                    "actions": [{"action": "request_permission", "reason": "Shell requires explicit audit metadata.", "metadata": {"approval": "audit"}}],
                },
            ],
        },
    )
    manifest = client.get("/system/policies/hooks").json()
    manager = AgentHookManager(policy_service=app.state.container.hook_policy_service)

    prompt_outcome = manager.run(
        "run_hook_runtime",
        "user_prompt_submit",
        payload={"workspace_id": workspace["workspace_id"], "prompt": "Build a CRM", "generation_mode": "fast"},
    )
    permission_outcome = manager.run(
        "run_hook_runtime",
        "permission_request",
        payload={"workspace_id": workspace["workspace_id"], "tool": "shell.exec", "canonical_tool": "shell.exec", "reason": "run command"},
    )
    parsed = AgentHookManager.parse_output(
        json.dumps({"additional_contexts": ["Use the failing check as the next edit target."], "tags": {"source": "test"}})
    )

    assert manifest["runtime"]["schema"] == "grounded.hook_runtime_manifest.v1"
    assert "user_prompt_submit" in manifest["supported_hooks"]
    assert "stop" in manifest["supported_hooks"]
    assert manifest["runtime"]["output_schema"]["properties"]["permission_request"]["type"] == "object"
    assert prompt_outcome.additional_contexts == ["Prefer small, repairable product increments."]
    assert permission_outcome.evaluation is not None
    assert permission_outcome.evaluation["tags"]["permission_tag"]["permission_request"]["approval"] == "audit"
    assert parsed is not None
    assert parsed["added_contexts"][0]["text"] == "Use the failing check as the next edit target."


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
        implementation_plan={"primary_entities": ["workflow"]},
        acceptance_contract={"roles": ["client", "specialist", "manager"]},
        linked_job_id=None,
    )
    run.worker_mailbox_ref = f"worker_mailbox:{workspace['workspace_id']}:{run.run_id}"
    run.context_pressure_ref = f"context_pressure:{workspace['workspace_id']}:{run.run_id}"
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    pressure_snapshot = {
        "schema": "grounded.context_pressure_snapshot.v2",
        "total_tokens_estimate": 88000,
        "context_window_tokens": 128000,
        "pressure_ratio": 0.6875,
        "sections": {
            "tool_outputs": {
                "key": "tool_outputs",
                "label": "Tool outputs",
                "tokens": 24000,
                "ratio": 0.1875,
                "budget_tokens": 19200,
                "top_contributors": [{"label": "run_command", "tokens": 24000, "section": "tool_outputs"}],
            }
        },
        "section_tokens": {"tool_outputs": 24000, "full_payload": 88000},
        "recommendations": [
            {
                "code": "use_artifact_ref",
                "message": "Tool outputs are large; use artifact or microcompact refs instead of raw stdout/stderr.",
                "section": "tool_outputs",
                "severity": "warning",
                "tokens": 24000,
                "action": "use_artifact_ref",
                "microcompact_ref": "microcompact:ws:run:digest",
                "paths": [],
                "metadata": {},
            }
        ],
        "suggestions": [],
        "microcompact_candidates": [
            {
                "tool": "run_command",
                "status": "completed",
                "original_chars": 96000,
                "tokens_estimate": 24000,
                "microcompact_ref": "microcompact:ws:run:digest",
                "artifact_ref": "microcompact:ws:run:digest",
                "digest": "digest",
                "reason": "existing_microcompact_ref",
            }
        ],
        "avoid_reread_files": [],
        "duplicate_file_reads": [],
        "duplicate_read_token_estimate": 0,
        "compact_boundary": {
            "recommended": False,
            "pressure_ratio": 0.6875,
            "threshold": 0.8,
            "message": "Context is below compact boundary.",
            "boundary_ref": None,
            "reason": None,
        },
        "compact_recommended": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    app.state.container.store.upsert(
        "reports",
        run.context_pressure_ref,
        {
            "schema": "grounded.context_pressure.v2",
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "status": "ready",
            "latest": pressure_snapshot,
            "items": [pressure_snapshot],
            "sections": pressure_snapshot["sections"],
            "recommendations": pressure_snapshot["recommendations"],
            "microcompact_candidates": pressure_snapshot["microcompact_candidates"],
            "avoid_reread_files": [],
            "compact_boundary": pressure_snapshot["compact_boundary"],
            "updated_at": pressure_snapshot["created_at"],
        },
    )
    output_artifact = app.state.container.output_artifact_service.store_command_output(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        process_id="proc_test",
        stream="stdout",
        command="rg workflow miniapp/app",
        content="workflow head\n" + ("middle\n" * 200) + "workflow tail error\n",
        head_tail={
            "head": "workflow head\n",
            "tail": "workflow tail error\n",
            "excerpt": "workflow head\n...[omitted 1200 chars]...\nworkflow tail error\n",
            "total_chars": 1420,
            "omitted_chars": 1200,
            "chunk_count": 1,
        },
        exit_code=1,
        semantic_status="failed",
    )
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
    app.state.container.event_journal_service.append_run(
        workspace_id=workspace["workspace_id"],
        run_id=run.run_id,
        event_type="run.created",
        payload={"run_id": run.run_id, "status": run.status},
        idempotency_key=f"test.run.created:{run.run_id}",
    )

    policy = client.get("/system/policies/exec").json()
    dynamic_tools = client.get("/system/tools/dynamic").json()
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
    context_pressure = client.get(f"/runs/{run.run_id}/context-pressure").json()
    compaction_boundaries = client.get(f"/runs/{run.run_id}/compaction/boundaries").json()
    output_artifacts = client.get(f"/runs/{run.run_id}/output-artifacts").json()
    output_artifact_detail = client.get(f"/runs/{run.run_id}/output-artifacts/{output_artifact['artifact_id']}").json()
    tasks = client.get(f"/runs/{run.run_id}/tasks").json()
    run_events = client.get(f"/runs/{run.run_id}/events").json()
    run_events_v2 = client.get(f"/runs/{run.run_id}/events-v2").json()
    run_journal_state = client.get(f"/runs/{run.run_id}/journal/state").json()
    doctor = client.get("/doctor").json()
    memory = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory",
        json={"kind": "project_rule", "text": "Use dense operational UI."},
    ).json()
    extracted_memory = client.post(f"/runs/{run.run_id}/memory/extract").json()
    memory_pipeline = client.get(f"/workspaces/{workspace['workspace_id']}/memory/pipeline").json()
    memory_summary = client.get(f"/workspaces/{workspace['workspace_id']}/memory/summary").json()
    consolidated_memory = client.post(f"/workspaces/{workspace['workspace_id']}/memory/consolidate").json()
    retrieved_memory = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory/retrieve",
        json={"prompt": "dense operational workflow UI", "top_k": 5},
    ).json()
    skills = client.get("/skills").json()
    skill_manifest = client.get("/system/skills/manifest").json()
    skill_evaluation = client.post(
        "/skills/evaluate",
        json={
            "prompt": "Build telegram app with browser proof",
            "intent": "create",
            "generation_mode": "quality",
            "paths": ["miniapp/app/static/client/app.js"],
        },
    ).json()
    workers = client.get(f"/runs/{run.run_id}/workers").json()
    worker_orchestration = client.get(f"/runs/{run.run_id}/workers/orchestration").json()
    worker_context = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/context").json()
    worker_memory = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/memory").json()
    worker_output = client.get(f"/runs/{run.run_id}/workers/backend_api_worker/output").json()
    worker_merge_decision = client.get(f"/runs/{run.run_id}/workers/merge-decision").json()
    permissions = client.get("/system/permissions/rules").json()
    lsp = client.get(f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp").json()
    lsp_symbols = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/symbol-context?q=app").json()
    lsp_refs = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/references?symbol=app").json()
    lsp_definition = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/definition?symbol=app").json()
    lsp_route_context = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/route-static-context").json()
    lsp_route_graph = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/route-graph").json()
    thread = client.post("/threads", json={"workspace_id": workspace["workspace_id"], "title": "Workbench Thread"}).json()
    thread_snapshot = client.get(f"/threads/{thread['thread_id']}").json()
    thread_events_v2 = client.get(f"/threads/{thread['thread_id']}/events-v2").json()
    thread_journal_state = client.get(f"/threads/{thread['thread_id']}/journal/state").json()
    event_payload = client.get(f"/event-payloads/{thread_events_v2['items'][0]['payload_ref']}").json()
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
    assert policy["tool_registry"]["router"]["dynamic_tool_discovery"]["default_visible_tool"] == "tool_search"
    assert policy["tool_registry"]["router"]["schema"] == "grounded.tool_router.manifest.v2"
    assert policy["tool_registry"]["router"]["parallel_execution_policy"]["parallel_safe_field"] == "parallel_safe"
    assert dynamic_tools["schema"] == "grounded.dynamic_tool_catalog.v1"
    assert {"deploy", "browser", "database", "payments", "cms", "github", "vercel"}.issubset(set(dynamic_tools["domains"]))
    assert evaluation["decision"]["action"] == "allow"
    RunTimelineReport.model_validate(timeline)
    RunTraceViewReport.model_validate(trace)
    TraceBundleReport.model_validate(trace_bundle)
    TraceState.model_validate(trace_bundle_state)
    RunProtocolReport.model_validate(protocol)
    RunBookmarksReport.model_validate(bookmarks)
    RunEventsReport.model_validate(run_events)
    ContextPressureReport.model_validate(context_pressure)
    OutputArtifactIndex.model_validate(output_artifacts)
    CommandOutputArtifact.model_validate(output_artifact_detail)
    EventJournalPage.model_validate(run_events_v2)
    EventJournalPage.model_validate(thread_events_v2)
    RunJournalState.model_validate(run_journal_state)
    ThreadJournalState.model_validate(thread_journal_state)
    assert timeline["items"][0]["kind"] == "prompt"
    assert trace["reducer"]["why"] == "Build a workflow"
    assert trace_bundle["schema"] == "grounded.trace_bundle.v1"
    assert trace_bundle_state["schema"] == "grounded.trace_bundle_state.v1"
    assert protocol["schema"] == "grounded.run_protocol.v1"
    assert bookmarks["schema"] == "grounded.run_bookmarks.v1"
    assert compaction["schema"] == "grounded.run_compaction.v1"
    assert context_pressure["schema"] == "grounded.context_pressure.v2"
    assert context_pressure["recommendations"][0]["code"] == "use_artifact_ref"
    assert context_pressure["microcompact_candidates"][0]["microcompact_ref"]
    assert compaction_boundaries["schema"] == "grounded.run_compaction_boundaries.v1"
    assert output_artifacts["schema"] == "grounded.output_artifact_index.v1"
    assert output_artifacts["items"][0]["ref"] == output_artifact["ref"]
    assert output_artifact_detail["head_tail"]["tail"].strip() == "workflow tail error"
    assert tasks["schema"] == "grounded.run_tasks.v1"
    assert run_events["schema"] == "grounded.run_events.v1"
    assert run_events_v2["schema"] == "grounded.event_journal_page.v2"
    assert run_journal_state["latest_status"] == run.status
    assert doctor["checks"]
    assert memory["items"][0]["text"] == "Use dense operational UI."
    assert extracted_memory["schema"] == "grounded.memory_stage1.v1"
    assert extracted_memory["items"][0]["fingerprint"]
    assert memory_pipeline["schema"] == "grounded.memory_pipeline.v1"
    assert memory_pipeline["phase1"]["raw_count"] >= len(extracted_memory["items"])
    assert memory_pipeline["phase2"]["retrieval_schema"] == "grounded.memory_retrieval.v1"
    assert memory_pipeline["phase2"]["summary_schema"] == "grounded.memory_summary.v1"
    assert memory_summary["schema"] == "grounded.memory_summary.v1"
    assert memory_summary["always_loaded"] is True
    assert consolidated_memory["pipeline"]["schema"] == "grounded.memory_pipeline.v1"
    assert consolidated_memory["memory_summary"]["schema"] == "grounded.memory_summary.v1"
    MemoryRetrievalResult.model_validate(retrieved_memory)
    assert retrieved_memory["schema"] == "grounded.memory_retrieval.v1"
    assert retrieved_memory["hits"][0]["selection_reason"]
    assert skills["schema"] == "grounded.skills.v2"
    assert any(item["id"] == "state-workflow" for item in skills["items"])
    assert any(item["id"] == "role-surfaces" for item in skills["items"])
    assert skill_manifest["schema"] == "grounded.skill_registry_manifest.v1"
    assert skill_manifest["scopes"]["repo"] >= 1
    assert skill_evaluation["schema"] == "grounded.skill_search.v2"
    assert skill_evaluation["effective"]["allowedTools"]
    assert skill_evaluation["effective"]["requiredProof"]
    assert skill_evaluation["ranking"]["algorithm"]
    assert all(item["ranking"]["score"] == item["activation_score"] for item in skill_evaluation["selected"])
    assert workers["schema"] == "grounded.product_workers.v1"
    assert any(item["worker_id"] == "backend_api_worker" and "persistence_api_worker" in item["alias_ids"] for item in workers["workers"])
    assert workers["ownership_contract"]["schema"] == "grounded.product_worker_ownership_contract.v1"
    assert workers["ownership_contract"]["lanes"]["role_ui"] == ["client_surface_worker", "specialist_surface_worker", "manager_surface_worker"]
    assert any(item["status"] == "available_disabled" for item in workers["workers"])
    assert worker_orchestration["schema"] == "grounded.worker_orchestration.v1"
    assert worker_orchestration["write_scope_report"]["schema"] == "grounded.worker_write_scope_report.v1"
    assert worker_orchestration["post_merge_verifier"]["worker_id"] == "mobile_polish_worker"
    assert worker_context["schema"] == "grounded.worker_context.v1"
    assert worker_memory["schema"] == "grounded.worker_memory_snapshot.v1"
    assert worker_output["schema"] == "grounded.worker_output.v1"
    assert worker_merge_decision["schema"] == "grounded.worker_manager_merge_decision.v1"
    assert any(item["rule_id"] == "block_destructive" for item in permissions["items"])
    assert lsp["status"] in {"passed", "failed"}
    assert lsp["schema"] == "grounded.lsp_diagnostics.v1"
    assert lsp["engine"] in {"static", "real_lsp+static"}
    assert lsp["diagnostic_stream"]
    assert lsp["route_graph"]["schema"] == "grounded.lsp_route_graph.v1"
    assert "jump" in lsp["items"][0] if lsp["items"] else True
    assert lsp_symbols["schema"] == "grounded.lsp_symbol_context.v1"
    assert lsp_refs["schema"] == "grounded.lsp_find_references.v1"
    assert lsp_definition["schema"] == "grounded.lsp_definition.v1"
    assert lsp_route_context["schema"] == "grounded.lsp_route_static_context.v1"
    assert lsp_route_graph["schema"] == "grounded.lsp_route_graph.v1"
    assert thread_snapshot["thread"]["thread_id"] == thread["thread_id"]
    assert thread_events_v2["items"][0]["event_type"] == "thread.started"
    assert thread_journal_state["event_count"] >= 1
    assert event_payload["payload_ref"] == thread_events_v2["items"][0]["payload_ref"]
    assert resumed_thread["status"] == "active"
    assert patch_preflight["status"] == "passed"
    assert patch_preflight["lsp_pre_edit_context"]["schema"] == "grounded.lsp_symbol_context.v1"
    assert patch_preflight["lsp_pre_edit_context"]["policy"]["required_before_patch"] is True
    assert patch_preflight["lsp_pre_edit_context"]["policy"]["targets"] == ["README.md"]


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
    events_v2 = client.get(f"/runs/{run.run_id}/events-v2").json()
    state_v2 = client.get(f"/runs/{run.run_id}/journal/state").json()

    assert events["items"][0]["event_type"] == "run.started"
    assert events["items"][0]["payload"]["source_event_type"] == "job_started"
    assert events["protocol_events"][0]["type"] == "run_started"
    assert events["protocol_events"][0]["source_event_type"] == "job_started"
    assert events["state_snapshots"][0]["reason"] == "job_started"
    assert {"trace.event_recorded", "run.started", "protocol.run_started"}.issubset({item["event_type"] for item in events_v2["items"]})
    assert state_v2["status"] == "available"
    assert state_v2["latest_status"] in {"running", "started"}


def test_patch_preflight_rejects_invalid_partial_edit_with_conflict_packet(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Patch Grammar Workspace",
            "description": "Strict patch preflight",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    report = client.post(
        f"/workspaces/{workspace['workspace_id']}/patch/preflight",
        json={
            "ops": [
                {
                    "operation_id": "op_bad_partial",
                    "op": "patch",
                    "file_path": "README.md",
                    "diff": "replace the title with Better App",
                    "explanation": "invalid partial edit",
                }
            ]
        },
    ).json()

    assert report["status"] == "conflict"
    assert report["validation_report"]["schema"] == "grounded.patch_validation.v1"
    assert report["validation_report"]["status"] == "failed"
    assert report["patch_sha256"]
    assert report["conflict_packet"]["schema"] == "grounded.patch_conflict_packet.v1"
    assert report["conflict_packet"]["forbidden_repeat_action"]["sha256"] == report["patch_sha256"]
    assert any(item["code"] in {"malformed_unified_diff", "missing_patch_path", "missing_hunk"} for item in report["validation_report"]["issues"])


def test_patch_apply_validator_blocks_malformed_hunk_without_mutating_draft(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Patch Apply Grammar Workspace",
            "description": "Strict patch apply",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(workspace_id=workspace["workspace_id"], prompt="Patch README", intent="edit")
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    workspace_service = app.state.container.workspace_service
    draft = workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    readme = draft / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    malformed = PatchEnvelope(
        workspace_id=workspace["workspace_id"],
        summary="Malformed patch",
        ops=[
            PatchOperationModel(
                operation_id="op_bad_hunk",
                op="patch",
                file_path="README.md",
                diff="--- a/README.md\n+++ b/README.md\n@@\n-hello\n+hello world\n",
                explanation="malformed hunk",
            )
        ],
    )

    result = workspace_service.apply_patch_envelope_to_draft(workspace["workspace_id"], run.run_id, malformed)

    assert result.status == "failed"
    assert result.patch_sha256
    assert result.validation_report["status"] == "failed"
    assert result.conflict_packet["schema"] == "grounded.patch_conflict_packet.v1"
    assert any(item["code"] == "malformed_hunk_header" for item in result.validation_report["issues"])
    assert readme.read_text(encoding="utf-8") == "hello\n"


def test_patch_apply_records_hash_and_validation_for_valid_unified_diff(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Patch Apply Hash Workspace",
            "description": "Patch hash audit",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(workspace_id=workspace["workspace_id"], prompt="Patch README", intent="edit")
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    workspace_service = app.state.container.workspace_service
    draft = workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    readme = draft / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    envelope = PatchEnvelope(
        workspace_id=workspace["workspace_id"],
        summary="Valid patch",
        ops=[
            PatchOperationModel(
                operation_id="op_valid",
                op="patch",
                file_path="README.md",
                diff="--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hello\n+hello world\n",
                explanation="valid hunk",
            )
        ],
    )

    result = workspace_service.apply_patch_envelope_to_draft(workspace["workspace_id"], run.run_id, envelope)
    stored = app.state.container.store.get("patch_applies", result.apply_id)

    assert result.status == "applied"
    assert result.changed_files == ["README.md"]
    assert result.patch_sha256
    assert result.validation_report["status"] == "passed"
    assert result.validation_report["operations"][0]["diff_kind"] == "unified_diff"
    assert readme.read_text(encoding="utf-8") == "hello world\n"
    assert stored["patch_sha256"] == result.patch_sha256
    assert stored["validation_report"]["status"] == "passed"


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


def test_typed_event_replay_reconstructs_run_resume_and_compare(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Replay Workspace",
            "description": "Typed replay",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    base = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a reservation app",
        intent="create",
        status="failed",
        current_stage="browser proof",
        failure_reason="Browser proof failed.",
        failure_class="browser_flow_smoke",
        failure_signature="missing_confirmed_reservation",
        resume_checkpoint_ref="resume_checkpoint:replay",
        touched_files=["miniapp/app/static/client/app.js"],
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
    )
    target = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Fix reservation app",
        intent="edit",
        status="completed",
        apply_status="applied",
        current_stage="completed",
        result_revision_id="rev_last_good",
        resume_from_run_id=base.run_id,
        forked_from_run_id=base.run_id,
        touched_files=["miniapp/app/static/client/app.js", "miniapp/app/static/manager/app.js"],
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
    )
    app.state.container.store.upsert("runs", base.run_id, base.model_dump(mode="json"))
    app.state.container.store.upsert("runs", target.run_id, target.model_dump(mode="json"))
    app.state.container.store.upsert("reports", "resume_checkpoint:replay", {"status": "pending", "source_run_id": base.run_id})
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{base.run_id}",
        {"check_results": [{"name": "browser_flow_smoke", "status": "failed"}], "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n"},
    )
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{target.run_id}",
        {"check_results": [{"name": "browser_flow_smoke", "status": "passed"}], "diff": "diff --git a/miniapp/app/static/manager/app.js b/miniapp/app/static/manager/app.js\n"},
    )
    app.state.container.store.upsert("reports", f"gate:{base.run_id}", {"product_readiness": {"status": "blocked", "blocking_reasons": [{"check": "browser_flow_smoke"}]}})
    app.state.container.store.upsert("reports", f"gate:{target.run_id}", {"product_readiness": {"status": "passed", "blocking_reasons": []}})
    app.state.container.event_journal_service.append_run(workspace_id=base.workspace_id, run_id=base.run_id, event_type="run.created", payload={"status": "pending", "stage": "created"})
    app.state.container.event_journal_service.append_run(workspace_id=base.workspace_id, run_id=base.run_id, event_type="run.status_changed", payload={"status": "running", "stage": "browser proof"})
    app.state.container.event_journal_service.append_run(
        workspace_id=base.workspace_id,
        run_id=base.run_id,
        event_type="check.result",
        payload={"name": "browser_flow_smoke", "status": "failed", "blocking": True},
    )
    app.state.container.event_journal_service.append_run(workspace_id=base.workspace_id, run_id=base.run_id, event_type="run.failed", payload={"status": "failed", "stage": "browser proof"})
    app.state.container.run_protocol_service.append_event(
        run_id=base.run_id,
        workspace_id=base.workspace_id,
        event_type="turn_started",
        status="started",
        turn_id="turn_1_1",
        message="Turn started.",
    )
    bookmark = app.state.container.run_protocol_service.create_bookmark(
        run_id=base.run_id,
        workspace_id=base.workspace_id,
        turn_id="turn_1_1",
        response_id="resp_replay",
        checkpoint_ref="resume_checkpoint:replay",
        trace_bundle_ref="trace_bundle:replay",
        diff_sha256_value=None,
    )

    protocol = client.get("/system/rpc-protocol").json()
    replay = client.get(f"/runs/{base.run_id}/event-replay").json()
    compare = client.get(f"/runs/{base.run_id}/compare/{target.run_id}").json()
    checkpoints = client.get(f"/runs/{base.run_id}/checkpoints").json()
    last_working_compare = client.get(f"/runs/{base.run_id}/compare-last-working").json()

    assert protocol["schema"] == "grounded.rpc_protocol.v2"
    assert {"run/replay", "run/compare", "run/fork_from_bookmark"}.issubset({item["method"] for item in protocol["methods"]})
    assert replay["schema"] == "grounded.run_event_replay.v1"
    assert replay["failure_point"]["check"] == "browser_flow_smoke"
    assert replay["resume"]["latest_bookmark"]["bookmark_id"] == bookmark["bookmark_id"]
    assert {"resume_from_bookmark", "fork_from_bookmark"}.issubset({item["action"] for item in replay["resume"]["actions"]})
    assert replay["replay_refs"]["protocol"].endswith("/protocol")
    assert compare["schema"] == "grounded.run_compare.v1"
    assert compare["lineage"]["relation"] == "target_forked_from_base"
    assert "browser_flow_smoke" in compare["check_delta"]["improved"]
    assert compare["readiness_delta"]["base_status"] == "blocked"
    assert compare["readiness_delta"]["target_status"] == "passed"
    assert checkpoints["schema"] == "grounded.run_session_checkpoints.v1"
    assert {"resume_checkpoint", "protocol_bookmark", "failed_check", "browser_failure"}.issubset({item["kind"] for item in checkpoints["items"]})
    assert checkpoints["latest_good_run_id"] == target.run_id
    assert {"resume_from_failed_check", "rollback_to_last_good_app", "compare_current_vs_last_working_product"}.issubset({item["action"] for item in checkpoints["actions"]})
    assert last_working_compare["base_run_id"] == target.run_id
    assert last_working_compare["target_run_id"] == base.run_id


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
        token_usage={"total_tokens": 120_000, "estimated_cost_usd": 0.42},
        budget_status={"status": "ok", "current_phase": "editing", "token_limit": 300_000, "total_tokens": 120_000},
        completion_budget={"turns": 4, "token_limit": 300_000, "cost_limit_usd": 1.2, "mode": "quality"},
        orchestration_phases=[{"id": "planning"}, {"id": "editing"}, {"id": "checking"}],
        checks_summary={"validators": "passed", "build": "failed", "preview": "pending", "gate_status": "blocked", "issues": []},
        resume_checkpoint_ref=f"resume_checkpoint:{workspace['workspace_id']}:run_compact_test",
    )
    run.agent_transcript_ref = f"agent_transcript:{workspace['workspace_id']}:{run.run_id}"
    run.context_pressure_ref = f"context_pressure:{workspace['workspace_id']}:{run.run_id}"
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        run.agent_transcript_ref,
        {
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "events": [
                {
                    "sequence": 1,
                    "event_type": "model_turn",
                    "payload": {
                        "response_id": "resp_1",
                        "assistant_message": "Need to fix frontend build.",
                        "tool_calls": [{"tool_use_id": "read_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]}],
                        "usage": {"total_tokens": 1200},
                    },
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
                {
                    "sequence": 2,
                    "event_type": "tool_result",
                    "payload": {
                        "tool_use_id": "read_1",
                        "tool": "read_files",
                        "microcompact_ref": "microcompact:ws:run:digest",
                        "digest": "digest",
                        "original_chars": 20_000,
                    },
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            ],
            "pending_tool_result_count": 1,
        },
    )
    app.state.container.store.upsert(
        "reports",
        run.context_pressure_ref,
        {
            "schema": "grounded.context_pressure.v2",
            "workspace_id": workspace["workspace_id"],
            "run_id": run.run_id,
            "status": "ready",
            "latest": {
                "schema": "grounded.context_pressure_snapshot.v2",
                "total_tokens_estimate": 110_000,
                "context_window_tokens": 128_000,
                "pressure_ratio": 0.86,
                "sections": {},
                "section_tokens": {},
                "recommendations": [],
                "suggestions": [],
                "microcompact_candidates": [],
                "avoid_reread_files": [],
                "stale_path_refs": [{"path": "miniapp/app/old.py", "source": "transcript.read_files", "reason": "missing_in_workspace", "suggested_path": "miniapp/app/main.py"}],
                "phase_budgets": [],
                "token_cost_budget": {},
                "compact_boundary": {"recommended": True, "pressure_ratio": 0.86, "threshold": 0.8, "message": "Context is close to the compact boundary."},
                "compact_recommended": True,
            },
            "items": [],
            "stale_path_refs": [{"path": "miniapp/app/old.py", "source": "transcript.read_files", "reason": "missing_in_workspace", "suggested_path": "miniapp/app/main.py"}],
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )
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
        {
            "check_results": [{"name": "frontend build", "status": "failed", "details": "syntax"}],
            "items": [{"artifact_id": "stdout_1", "ref": "output_artifact:ws:run:stdout_1", "stream": "stdout", "chars": 20_000, "omitted_chars": 12_000, "sha256": "abc"}],
        },
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
    assert compaction["preserved_tail"]["events"][-1]["microcompact_ref"] == "microcompact:ws:run:digest"
    assert compaction["stale_path_refs"][0]["suggested_path"] == "miniapp/app/main.py"
    assert compaction["phase_budget"]["total_token_budget"] == 300_000
    assert compaction["phase_budget"]["phases"][1]["phase"] == "editing"
    assert compaction["sections"]["large_artifacts"]["items"][0]["ref"] == "output_artifact:ws:run:stdout_1"
    assert compaction["post_compact_message_ref"] == f"post_compact_message:{run.run_id}:{compaction['boundary_id']}"
    assert compaction["post_compact_status"] == "pending"
    assert post_message["schema"] == "grounded.post_compact_message.v1"
    assert post_message["status"] == "pending"
    assert post_message["sections"]["current_plan"]["todo_plan"][0]["step"] == "Fix build"
    assert post_message["sections"]["preserved_tail"]["events"][-1]["digest"] == "digest"
    assert post_message["sections"]["phase_budget"]["current_phase"] == "editing"
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

    reset = transcript.compact_model_context(
        run_key,
        reason="context_pressure",
        preserved_tail={"schema": "grounded.preserved_tail.v1", "events": [{"sequence": 99, "event_type": "model_turn"}]},
    )
    compact_snapshot = transcript.snapshot(run_key)

    assert reset["preserved_tail"]["events"][0]["sequence"] == 99
    assert compact_snapshot["preserved_tails"][-1]["schema"] == "grounded.preserved_tail.v1"


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
        acceptance_contract={"required": True, "flows": [{"id": "client_create_task", "role": "client"}]},
        touched_files=["miniapp/app/main.py"],
    )
    screenshot = tmp_path / "client-proof.png"
    screenshot.write_bytes(b"proof")
    check_results = [
        {"name": "changed_files_static", "status": "passed", "details": "ok"},
        {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "ok"},
        {"name": "backend_static_validators", "status": "passed", "details": "ok"},
        {
            "name": "platform_invariants",
            "status": "passed",
            "details": "ok",
            "diagnostics": {
                "role_coverage": {
                    "client": {"status": "present", "route_count": 1},
                    "specialist": {"status": "present", "route_count": 1},
                    "manager": {"status": "present", "route_count": 1},
                }
            },
        },
        {
            "name": "api_workflow_smoke",
            "status": "passed",
            "details": "ok",
            "diagnostics": {"persisted_state_marker": "task-1", "api_before": [], "api_after": [{"id": "task-1"}]},
        },
        {
            "name": "browser_flow_smoke",
            "status": "passed",
            "details": "ok",
            "diagnostics": {
                "roles_checked": ["client", "specialist", "manager"],
                "ui_steps": [{"role": "client", "status": "passed", "flow_id": "client_create_task", "screenshot_after": str(screenshot)}],
                "role_screenshots": {
                    "client": str(screenshot),
                    "specialist": str(screenshot),
                    "manager": str(screenshot),
                },
                "acceptance_scenarios": [{"id": "client_create_task", "role": "client", "status": "passed", "source": "acceptance_contract"}],
                "persisted_state_marker": "task-1",
                "persisted_marker_after_reload": "task-1",
                "reload_verified": True,
                "console_errors": [],
                "network_errors": [],
                "mobile_layout": {"status": "passed", "horizontal_overflow": False, "critical_overlap": False},
            },
        },
        {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
        {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
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
    assert final_report["product_readiness"]["status"] == "passed"


def test_requirement_traceability_matrix_links_prompt_to_route_api_state_tests_and_browser(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Traceability Passed",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Create tasks from the client screen and persist title/status for manager review.",
        intent="create",
        target_role_scope=["client", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js", "miniapp/app/routes/tasks.py"],
        acceptance_contract={
            "required": True,
            "flows": [
                {
                    "id": "create_task",
                    "name": "Client creates a task",
                    "route": "/client",
                    "api_paths": ["/api/tasks"],
                    "state_fields": ["id", "title", "status"],
                    "required_tests": ["generated_app_python_tests", "generated_app_js_tests"],
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
            "preview": {"url": "http://127.0.0.1:18000", "role_urls": {"client": "/client"}, "status": "running"},
            "check_results": [
                {"name": "changed_files_static", "status": "passed", "details": "ok"},
                {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "ok"},
                {
                    "name": "platform_invariants",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "role_coverage": {
                            "client": {"status": "present", "route_count": 1},
                            "manager": {"status": "present", "route_count": 1},
                        }
                    },
                },
                {
                    "name": "api_workflow_smoke",
                    "status": "passed",
                    "details": "POST /api/tasks created task-1",
                    "diagnostics": {
                        "api_paths": ["/api/tasks"],
                        "persisted_state_marker": "task-1",
                        "api_after": [{"id": "task-1", "title": "Launch", "status": "new"}],
                    },
                },
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "roles_checked": ["client", "manager"],
                        "ui_steps": [{"role": "client", "route": "/client", "action": "create task", "status": "passed", "flow_id": "create_task", "screenshot_after": "/tmp/task-client.png"}],
                        "role_screenshots": {"client": "/tmp/task-client.png", "manager": "/tmp/task-manager.png"},
                        "acceptance_scenarios": [{"id": "create_task", "role": "client", "status": "passed", "source": "acceptance_contract"}],
                        "persisted_state_marker": "task-1",
                        "persisted_marker_after_reload": "task-1",
                        "reload_verified": True,
                        "screenshots": ["/tmp/task-client.png"],
                        "console_errors": [],
                        "network_errors": [],
                        "mobile_layout": {"status": "passed", "horizontal_overflow": False, "critical_overlap": False},
                    },
                },
                {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
            ],
        },
    )

    matrix = client.get(f"/runs/{run.run_id}/requirement-traceability").json()
    audit = client.get(f"/runs/{run.run_id}/completion-audit").json()
    gate = client.get(f"/runs/{run.run_id}/gate").json()
    final_report = client.get(f"/runs/{run.run_id}/final-report").json()

    row = matrix["rows"][0]
    audit_row = audit["rows"][0]
    assert matrix["schema"] == "grounded.requirement_traceability_matrix.v1"
    PromptCompletionAuditReport.model_validate(audit)
    assert audit["schema"] == "grounded.prompt_completion_audit.v1"
    assert audit["status"] == "passed"
    assert audit_row["implemented"]["files"] == ["miniapp/app/static/client/app.js", "miniapp/app/routes/tasks.py"]
    assert audit_row["proof"]["api"]["status"] == "passed"
    assert audit_row["proof"]["browser"]["status"] == "passed"
    assert audit_row["proof"]["tests"]["status"] == "passed"
    assert audit_row["uncovered"] == []
    assert matrix["status"] == "passed"
    assert row["route_page"]["status"] == "passed"
    assert row["api"]["paths"] == ["/api/tasks"]
    assert row["state"]["status"] == "passed"
    assert row["test"]["status"] == "passed"
    assert row["browser_proof"]["status"] == "passed"
    assert gate["status"] == "passed"
    assert gate["artifact_refs"]["requirement_traceability"] == f"requirement_traceability:{run.run_id}"
    assert gate["artifact_refs"]["prompt_completion_audit"] == f"prompt_completion_audit:{run.run_id}"
    assert gate["prompt_completion_audit"]["status"] == "passed"
    assert final_report["requirement_traceability"]["status"] == "passed"
    assert final_report["prompt_completion_audit"]["status"] == "passed"


def test_requirement_traceability_blocks_gate_when_requirement_proof_chain_is_missing(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Traceability Blocked",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Create tasks from the client screen.",
        intent="create",
        target_role_scope=["client"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={
            "required": True,
            "flows": [{"id": "create_task", "route": "/client", "api_paths": ["/api/tasks"], "state_fields": ["id"]}],
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
            ],
        },
    )

    matrix = client.get(f"/runs/{run.run_id}/requirement-traceability").json()
    audit = client.get(f"/runs/{run.run_id}/completion-audit").json()
    gate = client.get(f"/runs/{run.run_id}/gate").json()

    assert matrix["status"] == "blocked"
    assert audit["status"] == "blocked"
    assert {"api", "state", "browser_proof"}.issubset(set(audit["rows"][0]["uncovered"]))
    assert {"api", "state", "browser_proof"}.issubset(set(matrix["rows"][0]["missing"]))
    assert gate["status"] == "blocked"
    assert any(issue["kind"] == "requirement_traceability" for issue in gate["issues"])
    assert any(issue["kind"] == "prompt_completion_audit" for issue in gate["issues"])


def _readiness_check_results(
    *,
    api_diagnostics: dict[str, Any] | None = None,
    browser_diagnostics: dict[str, Any] | None = None,
    include_generated_tests: bool = True,
) -> list[dict[str, Any]]:
    results = [
        {"name": "changed_files_static", "status": "passed", "details": "ok"},
        {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "ok"},
        {
            "name": "platform_invariants",
            "status": "passed",
            "details": "ok",
            "diagnostics": {
                "role_coverage": {
                    "client": {"status": "present", "route_count": 1},
                    "specialist": {"status": "present", "route_count": 1},
                    "manager": {"status": "present", "route_count": 1},
                }
            },
        },
        {
            "name": "api_workflow_smoke",
            "status": "passed",
            "details": "ok",
            "diagnostics": api_diagnostics if api_diagnostics is not None else {"persisted_state_marker": "entity-1", "api_before": [], "api_after": [{"id": "entity-1"}]},
        },
        {
            "name": "browser_flow_smoke",
            "status": "passed",
            "details": "ok",
            "diagnostics": browser_diagnostics
            if browser_diagnostics is not None
            else {
                "roles_checked": ["client", "specialist", "manager"],
                "ui_steps": [{"role": "client", "status": "passed"}],
                "persisted_state_marker": "entity-1",
                "mobile_layout": {"status": "passed"},
            },
        },
    ]
    if include_generated_tests:
        results.extend(
            [
                {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
            ]
        )
    return results


@pytest.mark.parametrize(
    ("check_results", "expected_kinds"),
    [
        (
            _readiness_check_results(api_diagnostics={}),
            {"persistence_proof_missing_marker"},
        ),
        (
            _readiness_check_results(
                browser_diagnostics={
                    "roles_checked": ["client"],
                    "ui_steps": [],
                    "mobile_layout": {"status": "passed"},
                }
            ),
            {"browser_proof_missing_roles", "browser_proof_missing_ui_steps", "browser_proof_missing_persisted_marker"},
        ),
        (
            _readiness_check_results(
                browser_diagnostics={
                    "roles_checked": ["client", "specialist", "manager"],
                    "ui_steps": [{"role": "client", "status": "passed"}],
                    "persisted_state_marker": "entity-1",
                    "mobile_layout": {"status": "failed", "horizontal_overflow": True},
                }
            ),
            {"mobile_layout"},
        ),
        (
            _readiness_check_results(include_generated_tests=False),
            {"required_product_proof"},
        ),
    ],
)
def test_product_readiness_gate_blocks_incomplete_proof(tmp_path: Path, check_results: list[dict[str, Any]], expected_kinds: set[str]) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Product Readiness Blocked",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build an accountable workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": check_results,
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    final_report = client.get(f"/runs/{run.run_id}/final-report").json()

    issue_kinds = {issue["kind"] for issue in gate["issues"]}
    assert gate["status"] == "blocked"
    assert expected_kinds.issubset(issue_kinds)
    assert gate["product_readiness"]["status"] == "blocked"
    assert final_report["product_readiness"]["blocking_reasons"]


def test_strict_product_acceptance_gate_blocks_placeholder_data_and_out_of_scope_diff(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Strict Product Gate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a persisted role workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js", "README.md"],
        acceptance_contract={"required": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "\n".join(
                [
                    "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js",
                    "+const mockData = [{ name: 'John Doe' }];",
                    "diff --git a/README.md b/README.md",
                    "+notes",
                ]
            ),
            "check_results": _readiness_check_results(),
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    reconciled = client.get(f"/runs/{run.run_id}").json()
    issue_kinds = {issue["kind"] for issue in gate["issues"]}
    checklist = {item["key"]: item["status"] for item in gate["product_readiness"]["checklist"]}

    assert gate["status"] == "blocked"
    assert {"placeholder_runtime_data", "diff_scope"}.issubset(issue_kinds)
    assert checklist["runtime_data"] == "blocked"
    assert checklist["diff_scope"] == "blocked"
    assert reconciled["status"] == "blocked"


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


def test_system_schema_manifest_and_openapi_export_include_workbench_contracts(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    manifest = client.get("/system/schema").json()
    openapi = client.get("/openapi.json").json()
    exported = export_openapi()
    model_names = {item["name"] for item in manifest["models"]}
    component_names = set(openapi["components"]["schemas"])

    assert manifest["schema"] == "grounded.system_schema.v1"
    assert manifest["openapi_url"] == "/openapi.json"
    assert manifest["generated_types_path"] == "platform/frontend/src/lib/generated/openapi-types.ts"
    assert "model_shapes" not in manifest
    for name in {
        "RunEvent",
        "RunEventV2",
        "ThreadEventV2",
        "EventJournalPage",
        "EventJournalPayload",
        "RunJournalState",
        "ThreadJournalState",
        "ToolEnvelope",
        "CheckResult",
        "ArtifactRef",
        "GateReport",
        "ProductReadinessResult",
        "RepairCase",
        "TraceState",
        "ThreadSnapshot",
    }:
        assert name in model_names
        assert name in component_names
        assert name in exported["components"]["schemas"]


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


def test_preview_runtime_boundary_endpoint_describes_clean_lifecycle(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Boundary",
            "description": "preview boundary test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()

    boundary = client.get(f"/workspaces/{workspace['workspace_id']}/preview/runtime-boundary").json()

    assert boundary["schema"] == "grounded.sandbox.preview_lifecycle.v1"
    assert boundary["status"] == "stopped"
    assert boundary["diagnostics"]["destroyed_with_workspace"] is True
    assert boundary["diagnostics"]["isolated_generated_app_workspace"].endswith("/source")
    assert "reset stops local processes or docker compose resources" in boundary["lifecycle_events"]


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
        browser_flow_proof={
            "steps": [{"role": "client", "status": "passed", "route": "/client", "flow_id": "create_role_owned_entity", "screenshot_after": "/tmp/client-proof.png"}],
            "role_screenshots": {
                "client": "/tmp/client-proof.png",
                "specialist": "/tmp/specialist-proof.png",
                "manager": "/tmp/manager-proof.png",
            },
            "acceptance_scenarios": [{"id": "create_role_owned_entity", "status": "passed", "source": "acceptance_contract"}],
            "persisted_state_marker": "briefing-1",
            "persisted_marker_after_reload": "briefing-1",
            "reload_verified": True,
            "console_errors": [],
            "network_errors": [],
            "mobile_layout": {"status": "passed", "horizontal_overflow": False, "critical_overlap": False},
        },
        mobile_layout_report={"status": "passed"},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {"name": "changed_files_static", "status": "passed", "details": "ok", "logs": []},
                {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "ok", "logs": []},
                {
                    "name": "platform_invariants",
                    "status": "passed",
                    "details": "ok",
                    "logs": [],
                    "diagnostics": {
                        "role_coverage": {
                            "client": {"status": "present", "route_count": 1},
                            "specialist": {"status": "present", "route_count": 1},
                            "manager": {"status": "present", "route_count": 1},
                        }
                    },
                },
                {
                    "name": "api_workflow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "logs": [],
                    "diagnostics": {"persisted_state_marker": "briefing-1", "api_before": [], "api_after": [{"id": "briefing-1"}]},
                },
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "logs": [],
                    "diagnostics": {
                        "roles_checked": ["client", "specialist", "manager"],
                        "ui_steps": [{"role": "client", "status": "passed", "flow_id": "create_role_owned_entity", "screenshot_after": "/tmp/client-proof.png"}],
                        "role_screenshots": {
                            "client": "/tmp/client-proof.png",
                            "specialist": "/tmp/specialist-proof.png",
                            "manager": "/tmp/manager-proof.png",
                        },
                        "acceptance_scenarios": [{"id": "create_role_owned_entity", "status": "passed", "source": "acceptance_contract"}],
                        "persisted_state_marker": "briefing-1",
                        "persisted_marker_after_reload": "briefing-1",
                        "reload_verified": True,
                        "console_errors": [],
                        "network_errors": [],
                        "mobile_layout": {"status": "passed", "horizontal_overflow": False, "critical_overlap": False},
                    },
                },
                {"name": "generated_app_python_tests", "status": "passed", "details": "ok", "logs": []},
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok", "logs": []},
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


def test_browser_proof_final_artifact_includes_scenarios_errors_and_screenshots(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Browser Proof Artifact",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    screenshot = tmp_path / "proof-shot.png"
    screenshot.write_bytes(b"not-a-real-png-but-exportable")
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a checked browser workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "roles_checked": ["client", "specialist", "manager"],
                        "ui_steps": [{"action": "client_create", "role": "client", "route": "/client", "status": "passed"}],
                        "playwright_scenario": {
                            "schema": "grounded.browser_playwright_scenario.v1",
                            "mobile_viewport": {"width": 390, "height": 844},
                            "steps": [
                                {
                                    "action": "client_create",
                                    "role": "client",
                                    "route": "/client",
                                    "selector": "form#client-main",
                                    "screenshot_before": str(screenshot),
                                    "screenshot_after": str(screenshot),
                                }
                            ],
                        },
                        "failed_step_context": {"action": "client_create", "selector": "form#client-main"},
                        "dom_selector": "form#client-main",
                        "screenshot_before": str(screenshot),
                        "screenshot_after": str(screenshot),
                        "mobile_viewport": {"width": 390, "height": 844},
                        "persisted_state_marker": "entity-1",
                        "screenshots": [str(screenshot)],
                        "dom_snapshots": [
                            {
                                "route": "/client",
                                "role": "client",
                                "phase": "client_create_after",
                                "screenshot": str(screenshot),
                                "snapshot": {"title": "Client", "controlCount": 2, "forms": 1},
                            }
                        ],
                        "layout_reports": [
                            {
                                "route": "/client",
                                "role": "client",
                                "mobile_viewport": {"width": 390, "height": 844},
                                "overflow": False,
                                "overlaps": [],
                            }
                        ],
                        "visual_diffs": [
                            {
                                "action": "client_create",
                                "route": "/client",
                                "role": "client",
                                "screenshot_before": str(screenshot),
                                "screenshot_after": str(screenshot),
                                "changed": False,
                            }
                        ],
                        "console_errors": [],
                        "network_errors": ["500 http://localhost/api/items"],
                        "mobile_layout": {"status": "passed", "viewports": ["390x844"]},
                    },
                }
            ],
        },
    )

    proof = client.get(f"/runs/{run.run_id}/browser-proof").json()
    visual = client.get(f"/runs/{run.run_id}/visual-regression").json()
    final_report = client.get(f"/runs/{run.run_id}/final-report").json()
    export = client.post(f"/workspaces/{workspace['workspace_id']}/export/browser-proof-bundle").json()

    assert proof["schema"] == "grounded.browser_proof.v1"
    assert proof["final_artifact"] is True
    assert proof["status"] == "failed"
    assert proof["network_errors"] == ["500 http://localhost/api/items"]
    assert proof["screenshots"] == [str(screenshot)]
    assert proof["role_page_screenshots"][0]["role"] == "client"
    assert proof["replayable_scripts"][0]["schema"] == "grounded.browser_replay_script.v1"
    assert proof["replayable_scripts"][0]["steps"][0]["selector"] == "form#client-main"
    assert proof["playwright_scenario"]["steps"][0]["selector"] == "form#client-main"
    assert proof["dom_selector"] == "form#client-main"
    assert proof["screenshot_before"] == str(screenshot)
    assert proof["screenshot_after"] == str(screenshot)
    assert any(item["scenario_id"] == "browser_step_1" for item in proof["scenarios"])
    assert any(item["scenario_id"] == "mobile_viewport_layout" for item in proof["scenarios"])
    VisualRegressionReport.model_validate(visual)
    assert visual["schema"] == "grounded.visual_regression.v1"
    assert visual["mobile_viewport_screenshots"][0]["path"] == str(screenshot)
    assert visual["dom_state_snapshots"][0]["snapshot"]["controlCount"] == 2
    assert visual["visual_diffs"][0]["action"] == "client_create"
    assert visual["overflow_overlap"]["status"] == "passed"
    assert any(item["kind"] == "js_error" for item in visual["issues"])
    assert final_report["visual_regression"]["schema"] == "grounded.visual_regression.v1"
    assert final_report["browser_proof"]["artifact_refs"]["export_browser_proof_bundle"].endswith("/export/browser-proof-bundle")
    with zipfile.ZipFile(export["file_path"]) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert f"reports/browser_proof:{run.run_id}.json" in names
        assert any(name.startswith(f"screenshots/{run.run_id}/") for name in names)


def test_browser_product_proof_requires_contract_scenarios_and_reload_evidence(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Browser Product Proof",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    screenshot = tmp_path / "client-role.png"
    screenshot.write_bytes(b"proof")
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a client request workflow",
        intent="create",
        target_role_scope=["client"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True, "flows": [{"id": "client_create_request", "role": "client"}]},
    )
    base_checks = [
        {"name": "changed_files_static", "status": "passed", "details": "ok"},
        {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "ok"},
        {"name": "backend_static_validators", "status": "passed", "details": "ok"},
        {
            "name": "platform_invariants",
            "status": "passed",
            "details": "ok",
            "diagnostics": {"role_coverage": {"client": {"status": "present", "route_count": 1}}},
        },
        {
            "name": "api_workflow_smoke",
            "status": "passed",
            "details": "ok",
            "diagnostics": {"persisted_state_marker": "request-1", "api_after": [{"id": "request-1"}]},
        },
        {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
        {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
    ]
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                *base_checks,
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "roles_checked": ["client"],
                        "ui_steps": [{"role": "client", "status": "passed"}],
                        "persisted_state_marker": "request-1",
                        "mobile_layout": {"status": "passed"},
                    },
                },
            ],
        },
    )

    blocked_gate = client.get(f"/runs/{run.run_id}/gate").json()

    blocked_kinds = {issue["kind"] for issue in blocked_gate["browser_product_proof"]["issues"]}
    assert blocked_gate["browser_product_proof"]["status"] == "failed"
    assert "browser_product_proof_missing_role_screenshots" in blocked_kinds
    assert "browser_product_proof_missing_console_capture" in blocked_kinds
    assert "browser_product_proof_missing_network_capture" in blocked_kinds
    assert "browser_product_proof_missing_reload_marker" in blocked_kinds
    assert "browser_product_proof_missing_acceptance_scenarios" in blocked_kinds

    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                *base_checks,
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "roles_checked": ["client"],
                        "ui_steps": [
                            {
                                "role": "client",
                                "status": "passed",
                                "flow_id": "client_create_request",
                                "screenshot_after": str(screenshot),
                            }
                        ],
                        "role_screenshots": [{"role": "client", "route": "/client", "path": str(screenshot)}],
                        "acceptance_scenarios": [
                            {"id": "client_create_request", "role": "client", "status": "passed", "source": "acceptance_contract"}
                        ],
                        "persisted_state_marker": "request-1",
                        "persisted_marker_after_reload": "request-1",
                        "reload_verified": True,
                        "console_errors": [],
                        "network_errors": [],
                        "mobile_layout": {"status": "passed", "horizontal_overflow": False, "critical_overlap": False},
                    },
                },
            ],
        },
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    passed_gate = client.get(f"/runs/{run.run_id}/gate").json()

    assert passed_gate["browser_product_proof"]["status"] == "passed"
    assert not any(issue["check"] == "browser_product_proof" for issue in passed_gate["issues"])
    assert passed_gate["product_readiness"]["evidence"]["browser_product_proof"]["report_ref"] == f"browser_product_proof:{run.run_id}"


def test_visual_regression_blocks_gate_on_mobile_overflow_and_overlap(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Visual Regression Gate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    screenshot = tmp_path / "overflow-shot.png"
    screenshot.write_bytes(b"not-a-real-png-but-exportable")
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a mobile-safe request workflow",
        intent="create",
        target_role_scope=["client"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.css"],
        acceptance_contract={"required": True},
        mobile_layout_report={"status": "failed", "horizontal_overflow": True, "critical_overlap": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.css b/miniapp/app/static/client/app.css\n",
            "check_results": [
                {"name": "backend_static_validators", "status": "passed", "details": "ok"},
                {
                    "name": "api_workflow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {"persisted_state_marker": "request-1", "api_after": [{"id": "request-1"}]},
                },
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "diagnostics": {
                        "roles_checked": ["client"],
                        "ui_steps": [
                            {
                                "action": "client_create",
                                "role": "client",
                                "route": "/client",
                                "status": "passed",
                                "screenshot_after": str(screenshot),
                                "mobile_viewport": {"width": 390, "height": 844},
                            }
                        ],
                        "persisted_state_marker": "request-1",
                        "screenshots": [str(screenshot)],
                        "dom_snapshots": [
                            {"route": "/client", "role": "client", "phase": "after", "snapshot": {"controlCount": 3}}
                        ],
                        "layout_reports": [
                            {
                                "route": "/client",
                                "role": "client",
                                "mobile_viewport": {"width": 390, "height": 844},
                                "overflow": True,
                                "scrollWidth": 520,
                                "clientWidth": 390,
                                "overlaps": [{"a": ".request-card", "b": ".submit-row"}],
                            }
                        ],
                        "mobile_layout": {"status": "failed", "horizontal_overflow": True, "critical_overlap": True, "viewports": ["390x844"]},
                    },
                },
                {"name": "generated_app_python_tests", "status": "passed", "details": "ok"},
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok"},
            ],
        },
    )

    visual = client.get(f"/runs/{run.run_id}/visual-regression").json()
    gate = client.get(f"/runs/{run.run_id}/gate").json()

    VisualRegressionReport.model_validate(visual)
    assert visual["status"] == "failed"
    assert visual["blocking"] is True
    assert visual["overflow_overlap"]["horizontal_overflow"] is True
    assert visual["overflow_overlap"]["critical_overlap"] is True
    assert any(item["kind"] == "horizontal_overflow" for item in visual["issues"])
    assert gate["status"] == "blocked"
    assert gate["visual_regression"]["status"] == "failed"
    assert gate["artifact_refs"]["visual_regression"] == f"visual_regression:{run.run_id}"
    assert any(issue["kind"] == "visual_regression" for issue in gate["issues"])


def test_visual_regression_compares_previous_successful_role_screenshots(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Visual Baseline Workspace",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    baseline = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build role workflow",
        intent="create",
        target_role_scope=["client", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
    )
    app.state.container.store.upsert("runs", baseline.run_id, baseline.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"visual_regression:{baseline.run_id}",
        {
            "schema": "grounded.visual_regression.v1",
            "run_id": baseline.run_id,
            "workspace_id": workspace["workspace_id"],
            "status": "passed",
            "blocking": False,
            "role_page_snapshots": [
                {"role": "client", "route": "/client", "screenshot": "/tmp/base-client.png"},
                {"role": "manager", "route": "/manager", "screenshot": "/tmp/base-manager.png"},
            ],
            "artifact_refs": {"visual_regression": f"visual_regression:{baseline.run_id}"},
            "created_at": "2026-05-20T00:00:00+00:00",
        },
    )
    time.sleep(0.001)
    target = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Edit role workflow",
        intent="edit",
        target_role_scope=["client", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/static/client/app.js"],
    )
    app.state.container.store.upsert("runs", target.run_id, target.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{target.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "diagnostics": {
                        "roles_checked": ["client"],
                        "ui_steps": [{"role": "client", "route": "/client", "action": "open", "status": "passed", "screenshot_after": "/tmp/current-client.png"}],
                        "screenshots": ["/tmp/current-client.png"],
                        "mobile_layout": {"status": "passed"},
                    },
                }
            ],
        },
    )

    visual = client.get(f"/runs/{target.run_id}/visual-regression").json()

    assert visual["status"] == "failed"
    assert visual["baseline"]["baseline_run_id"] == baseline.run_id
    assert "manager:/manager" in visual["baseline"]["missing_role_snapshots"]
    assert any(item["kind"] == "layout_regression" for item in visual["issues"])


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
    journal = client.get(f"/runs/{run.run_id}/events-v2").json()

    GateReport.model_validate(gate)
    RepairCasesReport.model_validate(repair_cases)
    assert gate["status"] == "blocked"
    assert any(issue["check"] == "browser_flow_smoke" for issue in gate["issues"])
    assert any(item["signature"] == "preview.browser_flow_failed" for item in repair["items"])
    assert repair_cases["items"]
    assert repair_cases["active_case"]["failure_class"] in {"browser_flow_smoke", "browser_proof_gap"}
    assert repair_cases["active_case"]["repair_prompt"]["sections"]["expected_proof"]
    case_id = repair_cases["active_case"]["case_id"]
    RepairCase.model_validate(client.get(f"/runs/{run.run_id}/repair-cases/{case_id}").json())
    RepairAttemptsReport.model_validate(client.get(f"/runs/{run.run_id}/repair-cases/{case_id}/attempts").json())
    event_types = {item["event_type"] for item in journal["items"]}
    assert "gate.evaluated" in event_types
    assert "repair.case_opened" in event_types


def test_product_readiness_gate_schedules_auto_repair_from_active_case(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Auto Repair Gate",
            "description": "Gate schedules repair continuation",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a workflow and repair missing proof",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="blocked",
        apply_status="blocked",
        draft_ready=True,
        draft_status="ready",
        touched_files=["miniapp/app/static/client/app.js"],
        acceptance_contract={"required": True},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [{"name": "api_workflow_smoke", "status": "passed", "details": "ok", "diagnostics": {"persisted_state_marker": "entity-1"}}],
        },
    )

    class FakeBackgroundTaskService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create_task(self, **kwargs):
            self.calls.append(kwargs)
            return type("Task", (), {"task_id": "task_gate_auto_repair"})()

    fake = FakeBackgroundTaskService()
    monkeypatch.setenv("GROUNDED_AUTO_REPAIR_CONTINUATION_MAX", "1")
    app.state.container.run_service.attach_background_task_service(fake)

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    report = app.state.container.store.get("reports", f"auto_repair_continuation:{run.run_id}")

    assert gate["status"] == "blocked"
    assert fake.calls
    assert fake.calls[0]["task_type"] == "repair_failed_run"
    assert fake.calls[0]["auto_start"] is True
    assert fake.calls[0]["input_payload"]["source_run_id"] == run.run_id
    assert report["status"] == "scheduled"
    assert gate["auto_repair_continuation"]["task_id"] == "task_gate_auto_repair"


def test_reliability_gate_blocks_test_only_acceptance_diff(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Test Only Gate Workspace",
            "description": "Reliability gate test-only diff failure",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Fix the working product, not only tests",
        mode="fix",
        intent="edit",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/tests/generated_app.test.mjs"],
        acceptance_contract={"required": True},
        mobile_layout_report={"status": "passed"},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/tests/generated_app.test.mjs b/miniapp/tests/generated_app.test.mjs\n",
            "check_results": [
                {"name": "api_workflow_smoke", "status": "passed", "details": "ok", "logs": []},
                {"name": "browser_flow_smoke", "status": "passed", "details": "ok", "logs": [], "diagnostics": {"mobile_layout": {"status": "passed"}}},
            ],
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()

    assert gate["status"] == "blocked"
    assert any(issue["kind"] == "product_source_diff" for issue in gate["issues"])


def test_gate_requires_changed_files_lsp_diagnostics_after_patch(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "LSP Gate Workspace",
            "description": "Changed file diagnostics gate",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Patch FastAPI route",
        intent="edit",
        status="completed",
        apply_status="applied",
        touched_files=["miniapp/app/routes/broken.py"],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    draft = app.state.container.workspace_service.prepare_draft(workspace["workspace_id"], run.run_id)
    target = draft / "miniapp/app/routes/broken.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def broken(:\n    pass\n", encoding="utf-8")
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/routes/broken.py b/miniapp/app/routes/broken.py\n",
            "check_results": [],
        },
    )

    gate = client.get(f"/runs/{run.run_id}/gate").json()
    public_diagnostics = client.get(
        f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp",
        params={"run_id": run.run_id, "changed_only": "true"},
    ).json()
    lsp_report = app.state.container.store.get("reports", f"lsp_verification:{run.run_id}")
    changed_report = app.state.container.store.get("reports", f"lsp_diagnostics:{workspace['workspace_id']}:{run.run_id}:changed")

    assert gate["status"] == "blocked"
    assert gate["lsp_verification"]["status"] == "failed"
    assert any(issue["kind"] == "lsp_changed_files_diagnostics" for issue in gate["issues"])
    assert gate["artifact_refs"]["lsp_verification"] == f"lsp_verification:{run.run_id}"
    assert gate["requirements"]["lsp_changed_files_diagnostics"] is True
    assert lsp_report["changed_only"] is True
    assert lsp_report["policy"]["diagnostics_after_each_patch"] is True
    assert lsp_report["policy"]["find_references_before_rename"]
    assert lsp_report["route_graph_ref"] == f"lsp_route_graph:{workspace['workspace_id']}:{run.run_id}"
    assert changed_report["changed_files"] == ["miniapp/app/routes/broken.py"]
    assert changed_report["gate_required"] is True
    assert any(item["source"] == "python_compile" for item in changed_report["items"])
    assert public_diagnostics["changed_only"] is True
    assert public_diagnostics["changed_files"] == ["miniapp/app/routes/broken.py"]


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


def test_review_report_exposes_and_filters_review_targets(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Review Targets Workspace",
            "description": "Review target filtering",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    previous = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Previous successful app",
        intent="create",
        status="completed",
        apply_status="applied",
        result_revision_id="rev_previous_success",
        touched_files=["miniapp/app/static/client/app.js"],
    )
    current = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Repair failed client flow",
        mode="fix",
        intent="edit",
        status="failed",
        apply_status="failed",
        failure_class="browser_flow",
        failure_signature="selector-missing",
        fix_targets=["miniapp/app/static/client/app.js"],
        touched_files=[
            "miniapp/app/static/client/app.js",
            "miniapp/tests/generated_app.test.mjs",
            "README.md",
        ],
        remaining_issues=[{"code": "selector_missing", "file_path": "miniapp/app/static/client/app.js"}],
    )
    app.state.container.store.upsert("runs", previous.run_id, previous.model_dump(mode="json"))
    app.state.container.store.upsert("runs", current.run_id, current.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{current.run_id}",
        {
            "diff": "\n".join(
                [
                    "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js",
                    "+document.querySelector('[data-action=\"save\"]')",
                    "diff --git a/miniapp/tests/generated_app.test.mjs b/miniapp/tests/generated_app.test.mjs",
                    "+test('client save', async () => {})",
                    "diff --git a/README.md b/README.md",
                    "+notes",
                ]
            ),
            "check_results": [],
        },
    )

    default_review = client.get(f"/runs/{current.run_id}/review").json()
    runtime_review = client.get(f"/runs/{current.run_id}/review", params={"target": "product_runtime_files"}).json()
    failed_patch_review = client.get(f"/runs/{current.run_id}/review", params={"target": "failed_repair_patch"}).json()
    since_success_review = client.get(f"/runs/{current.run_id}/review", params={"target": "since_last_successful_run"}).json()
    default_review_after_target = client.get(f"/runs/{current.run_id}/review").json()

    assert default_review["schema"] == "grounded.review_report.v2"
    assert default_review["review_target"]["id"] == "current_draft"
    assert {item["id"] for item in default_review["review_targets"]} == {
        "current_draft",
        "against_base_template",
        "since_last_successful_run",
        "product_runtime_files",
        "failed_repair_patch",
    }
    assert runtime_review["review_target"]["id"] == "product_runtime_files"
    assert runtime_review["evidence"]["changed_files"] == ["miniapp/app/static/client/app.js"]
    assert all(not path.startswith("miniapp/tests/") for path in runtime_review["evidence"]["changed_files"])
    assert failed_patch_review["review_target"]["available"] is True
    assert failed_patch_review["review_target"]["metadata"]["failure_signature"] == "selector-missing"
    assert since_success_review["review_target"]["metadata"]["base_run_id"] == previous.run_id
    assert since_success_review["review_target"]["metadata"]["shared_files"] == ["miniapp/app/static/client/app.js"]
    assert default_review_after_target["review_target"]["id"] == "current_draft"


def test_prompt_suggestions_are_product_specific_followups(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Prompt Suggestions Workspace",
            "description": "Prompt suggestions shape",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build an order intake product with client, specialist, and manager list views",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        status="completed",
        apply_status="applied",
        touched_files=[
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/specialist/app.js",
            "miniapp/app/static/manager/app.js",
        ],
        acceptance_contract={"required": True, "flows": [{"name": "Order intake"}]},
        implementation_plan={"primary_entities": ["order"], "roles": ["client", "specialist", "manager"]},
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "\n".join(
                [
                    "diff --git a/miniapp/app/static/manager/app.js b/miniapp/app/static/manager/app.js",
                    "+++ b/miniapp/app/static/manager/app.js",
                    "+renderOrderList(orders)",
                ]
            ),
            "check_results": [{"name": "browser_flow_smoke", "status": "passed", "details": "ok"}],
        },
    )

    response = client.get(f"/runs/{run.run_id}/prompt-suggestions")
    payload = response.json()
    typed = PromptSuggestionsReport.model_validate(payload)
    categories = {item.category for item in typed.items}
    prompts = "\n".join(item.prompt for item in typed.items).lower()

    assert response.status_code == 200
    assert payload["schema"] == "grounded.prompt_suggestions.v1"
    assert typed.status == "ready"
    assert {"status_workflow", "manager_dashboard", "export", "empty_state"}.issubset(categories)
    assert "order" in prompts
    assert all(item.suggestion_id.startswith("ps_") for item in typed.items)


def test_guardian_review_blocks_staged_apply_with_blocker_findings(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Guardian Apply Workspace",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    run = RunRecord(
        workspace_id=workspace_id,
        prompt="Build a persisted role workflow",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        status="awaiting_approval",
        apply_status="awaiting_approval",
        draft_status="ready",
        draft_ready=True,
        acceptance_contract={"required": True, "flows": [{"id": "create_task"}], "features": {"workflow_update": True}},
        implementation_plan={"primary_entities": ["task"]},
        touched_files=["miniapp/app/static/client/app.js"],
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    draft_source = app.state.container.workspace_service.prepare_draft(workspace_id, run.run_id)
    target = draft_source / "miniapp/app/static/client/app.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("const mockData = [{ name: 'John Doe' }];\n", encoding="utf-8")
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js\n",
            "check_results": [
                {"name": "api_workflow_smoke", "status": "passed", "details": "ok", "logs": []},
                {
                    "name": "browser_flow_smoke",
                    "status": "passed",
                    "details": "ok",
                    "logs": [],
                    "diagnostics": {"roles_checked": ["client", "specialist", "manager"], "ui_steps": [{"action": "open"}]},
                },
                {"name": "generated_app_js_tests", "status": "passed", "details": "ok", "logs": []},
            ],
        },
    )

    response = client.post(f"/runs/{run.run_id}/apply/staged")

    assert response.status_code == 200
    payload = response.json()
    guardian = app.state.container.store.get("reports", f"guardian_review:{workspace_id}:{run.run_id}")
    assert payload["status"] == "blocked"
    assert payload["apply_status"] == "blocked"
    assert guardian["status"] == "failed"
    assert guardian["final_review_gate"]["schema"] == "grounded.final_review_gate.v1"
    assert guardian["final_review_gate"]["status"] == "failed"
    assert {item["key"] for item in guardian["checklist"]} >= {
        "breaking_changes",
        "missing_tests",
        "product_readiness",
        "mobile_overflow",
        "stale_mock_data",
        "context_bloat",
        "changed_size_risk",
        "security_privacy",
    }
    assert any(item["category"] == "seeded_mock_data" for item in guardian["findings"])


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


def test_repair_case_service_promotes_precise_repair_packet_fields(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.repair_case_service
    run = RunRecord(workspace_id="ws_1", prompt="Build a prompt-derived workflow", intent="create", status="blocked")
    packet = RepairCatalog.classify_issue(
        {
            "kind": "check_failure",
            "check": "browser_flow_smoke",
            "details": "browser_flow_smoke failed while saving manager approval.",
            "evidence": {
                "failed_role": "manager",
                "failed_route": "/manager",
                "failed_selector": "#approve-order",
                "failed_step": "submit manager approval",
                "network_errors": ["POST /api/orders 500"],
                "screenshots": ["screenshots/run_1/manager-mobile.png"],
            },
        }
    )

    cases = service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[packet],
        source="agent_loop_checks",
    )
    active = cases["active_case"]
    prompt_sections = active["repair_prompt"]["sections"]

    assert active["failed_check"] == "browser_flow_smoke"
    assert active["broken_surface"]["role"] == "manager"
    assert active["broken_surface"]["selector"] == "#approve-order"
    assert active["broken_surface"]["api_route"] == "/api/orders"
    assert "miniapp/app/static/manager/app.js" in active["likely_files"]
    assert "miniapp/app/routes/api.py" in active["likely_files"]
    assert active["normalized_signature"] == "preview.browser_flow_failed"
    assert active["probable_files"]
    assert active["product_guardrails"]["do_not_redesign_product"] is True
    assert active["known_fix_recipe"]["steps"]
    assert active["repair_confidence"]["score"] >= 0.8
    assert active["post_fix_proof"]["check"] == "browser_flow_smoke"
    assert active["post_repair_proof"]["check"] == "browser_flow_smoke"
    assert "console and network errors are empty" in active["post_fix_proof"]["evidence_required"]
    assert prompt_sections["failed_check"] == "browser_flow_smoke"
    assert prompt_sections["broken_surface"]["selector"] == "#approve-order"
    assert prompt_sections["product_guardrails"]["do_not_redesign_product"] is True
    assert prompt_sections["repair_confidence"]["score"] >= 0.8
    assert prompt_sections["post_fix_proof"]["check"] == "browser_flow_smoke"


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


def test_browser_replay_repair_case_requires_failed_step_first(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.repair_case_service
    replay_plan = {
        "schema": "grounded.browser_replay_plan.v1",
        "first_action": "reproduce_failed_step",
        "failed_step": "manager_button_update",
        "route": "/manager",
        "selector": "button[data-action=\"approve\"]",
        "mobile_viewport": {"width": 390, "height": 844},
    }

    cases = service.sync_from_packets(
        workspace_id="ws_replay",
        run_id="run_replay",
        packets=[
            {
                "signature": "browser_flow.manager_button_update",
                "failure_class": "browser_flow_smoke",
                "verification_check": "browser_flow_smoke",
                "target_files": ["miniapp/app/static/manager/app.js"],
                "failed_step": "manager_button_update",
                "failed_role": "manager",
                "failed_route": "/manager",
                "failed_selector": "button[data-action=\"approve\"]",
                "dom_selector": "button[data-action=\"approve\"]",
                "screenshot_before": "/tmp/before.png",
                "screenshot_after": "/tmp/after.png",
                "console_logs": ["pageerror: ReferenceError"],
                "network_logs": ["500 /api/orders"],
                "mobile_viewport": {"width": 390, "height": 844},
                "replay_plan": replay_plan,
            }
        ],
        source="browser_replay",
    )

    next_action = cases["active_case"]["next_action"]

    assert next_action["action"] == "reproduce_browser_step_first"
    assert next_action["replay_first"] is True
    assert next_action["browser_replay"]["replay_plan"] == replay_plan
    assert cases["active_case"]["repair_prompt"]["sections"]["next_action"]["action"] == "reproduce_browser_step_first"


def test_browser_replay_endpoint_returns_latest_failed_step_plan(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    run = RunRecord(workspace_id="ws_replay", prompt="Repair browser replay", intent="edit", status="blocked")
    replay_ref = f"browser_replay:{run.workspace_id}:{run.run_id}:latest"
    run.browser_step_refs = [replay_ref]
    packet = {
        "schema": "grounded.browser_replay_packet.v2",
        "failed_step": "client_submit",
        "failed_route": "/client",
        "failed_selector": "form#client-main",
        "replay_plan": {"first_action": "reproduce_failed_step", "route": "/client"},
    }
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert("reports", replay_ref, {"workspace_id": run.workspace_id, "run_id": run.run_id, "packet": packet})

    replay = client.get(f"/runs/{run.run_id}/browser-replay").json()

    assert replay["schema"] == "grounded.browser_replay.v1"
    assert replay["status"] == "ready"
    assert replay["replay_first"] is True
    assert replay["latest_packet"]["failed_step"] == "client_submit"


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


def test_run_service_schedules_auto_repair_continuation_for_repeated_no_progress(tmp_path: Path, monkeypatch) -> None:
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
        failure_class="generation.repeated_no_progress",
        failure_signature="repair.no_progress:browser_flow_smoke",
        current_fix_phase="blocked_repair_continuation_needed",
        failure_reason="Repeated repair no-progress reached.",
    )
    repair_service.sync_from_packets(
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        packets=[
            {
                "signature": "browser_flow_smoke:role_update_state_change",
                "failure_class": "browser_flow_smoke",
                "severity": "high",
                "target_files": ["miniapp/app/static/specialist/app.js"],
                "verification_check": "browser_flow_smoke",
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
    assert fake.calls[0]["task_type"] == "repair_failed_run"
    assert fake.calls[0]["input_payload"]["source_run_id"] == run.run_id
    assert app.state.container.store.get("reports", f"auto_repair_continuation:{run.run_id}")["status"] == "scheduled"


def test_deterministic_repair_case_escalates_after_one_failed_attempt() -> None:
    should_stop = AgentToolCallLoop._should_escalate_repeated_no_progress(
        repeated_count=2,
        has_draft_diff=True,
        active_case={
            "issue_code": "platform.routeable_role_views_not_wired",
            "failure_signature": "platform.routeable_role_views_not_wired.manager",
            "attempts": [{"status": "applied", "changed_files": ["miniapp/app/static/manager/app.js"]}],
            "evidence": {"packet": {"retry_policy": "deterministic_repair"}},
            "next_action": {"action": "patch_constrained_slice", "attempt_count": 1},
        },
        transition={},
    )

    assert should_stop is True


def test_completion_gate_issues_become_targeted_repair_packets() -> None:
    packets = AgentToolCallLoop._repair_packets_from_completion_state(
        {
            "remaining_issues": [
                {
                    "kind": "product_task_ledger",
                    "check": "product_task_ledger",
                    "details": "manager ledger item manager.role_surface is incomplete",
                    "role": "manager",
                    "ledger_item_id": "manager.role_surface",
                    "expected_min_routes": 3,
                    "blocking": True,
                }
            ]
        }
    )

    assert packets
    assert packets[0]["issue_code"] == "product_task_ledger"
    assert "miniapp/app/static/manager/app.js" in packets[0]["target_files"]
    assert packets[0]["verification_check"] == "product_task_ledger"


def test_completion_budget_status_enforces_turn_cap() -> None:
    job = JobRecord(
        workspace_id="ws_budget",
        prompt="Build product",
        status="running",
        target_platform="telegram_mini_app",
        preview_profile="telegram_mock",
        generation_mode="fast",
        completion_budget={"time_limit_ms": 9999999, "token_limit": 9999999, "turn_budget_cap": 2},
    )

    status = completion_budget_status(job=job, mode=GenerationMode.FAST, started_at=time.perf_counter(), attempt=2)

    assert status["exhausted"] is True
    assert status["reason"] == "turn_budget_exhausted"


def test_generation_mode_sla_manifest_exposes_product_profiles_and_second_queue(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)

    manifest = client.get("/system/generation-modes").json()
    modes = {item["mode"]: item for item in manifest["modes"]}

    assert manifest["schema"] == "grounded.generation_sla.v1"
    assert modes["fast"]["required_checks"] == ["api_workflow_smoke", "browser_flow_smoke"]
    assert "mobile layout" in " ".join(modes["balanced"]["proof_requirements"])
    assert "visual snapshots" in modes["quality"]["proof_requirements"]
    assert "security/privacy review" in modes["production"]["proof_requirements"]
    assert any(item["id"] == "structured_diff" for item in manifest["second_queue"])


def test_completion_budget_for_production_mode_carries_release_sla() -> None:
    budget = completion_budget_for_mode(GenerationMode.PRODUCTION)

    assert budget["mode"] == "production"
    assert budget["policy"] == "time_or_token_budget_plus_product_sla"
    assert budget["sla_profile"]["audit_level"] == "release"
    assert "browser_flow_smoke" in budget["required_checks"]


def test_repair_continuation_inherits_source_product_contract(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Original Product Workspace",
            "description": "contract inheritance test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run_service = app.state.container.run_service
    source = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Создай mini-app для общего рабочего процесса с ролями client, specialist и manager.",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        status="blocked",
        apply_status="blocked",
        draft_ready=True,
        draft_status="ready",
        acceptance_contract={
            "required": True,
            "status": "planned",
            "prompt_hints": {
                "prompt_summary": "shared role workflow",
                "resource_hint": "рабочий процесс",
                "field_hints": ["название", "статус"],
                "role_field_hints": {"client": ["название"], "specialist": ["статус"], "manager": ["решение"]},
                "role_action_prompts": {
                    "client": ["создает рабочий процесс"],
                    "specialist": ["обновляет статус"],
                    "manager": ["принимает решение"],
                },
            },
            "flows": [{"id": "role_shared_persistence", "steps": [{"role": "client", "kind": "prompt_state_source"}]}],
            "required_endpoints": [
                {"method": "GET", "path": "/api/workflows", "purpose": "read state"},
                {"method": "POST", "path": "/api/workflows", "purpose": "create state"},
            ],
            "features": {"prompt_contract_v1": True, "platform_product_scaffold": False},
        },
        implementation_plan={"principle": "source product contract", "tasks": []},
    )
    app.state.container.store.upsert("runs", source.run_id, source.model_dump(mode="json"))
    monkeypatch.setattr(run_service, "_execute_run", lambda *_args, **_kwargs: None)

    continuation = run_service.create_run(
        workspace["workspace_id"],
        CreateRunRequest(
            prompt="Repair only the active case.",
            mode="fix",
            intent="edit",
            generation_mode="fast",
            resume_from_run_id=source.run_id,
        ),
    )
    saved = run_service.get_run(continuation.run_id)
    workspace_after = app.state.container.workspace_service.get_workspace(workspace["workspace_id"])

    assert saved.prompt == "Repair only the active case."
    assert saved.acceptance_contract["required"] is True
    assert saved.acceptance_contract["inherited_from_run_id"] == source.run_id
    assert saved.acceptance_contract["repair_continuation"] is True
    assert saved.flow_coverage["status"] == "planned"
    assert saved.implementation_plan["repair_continuation"]["contract_inherited"] is True
    assert workspace_after.name == "Original Product Workspace"


def test_repair_continuation_recovers_contract_from_lineage(tmp_path: Path, monkeypatch) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Lineage Contract Workspace",
            "description": "contract lineage test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    run_service = app.state.container.run_service
    contract = {
        "required": True,
        "status": "planned",
        "prompt_hints": {
            "prompt_summary": "shared role workflow",
            "resource_hint": "общий процесс",
            "field_hints": ["название", "статус"],
            "role_field_hints": {"client": ["название"], "specialist": ["статус"], "manager": ["решение"]},
            "role_action_prompts": {
                "client": ["создает общий процесс"],
                "specialist": ["меняет статус"],
                "manager": ["принимает решение"],
            },
        },
        "flows": [{"id": "role_shared_persistence"}],
    }
    root = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Создай mini-app для общего процесса между client, specialist и manager.",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        status="blocked",
        apply_status="blocked",
        draft_ready=True,
        draft_status="ready",
        acceptance_contract=contract,
        implementation_plan={"principle": "root contract"},
    )
    intermediate = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Repair active case.",
        mode="fix",
        intent="edit",
        target_role_scope=["client", "specialist", "manager"],
        status="blocked",
        apply_status="blocked",
        draft_ready=True,
        draft_status="ready",
        resume_from_run_id=root.run_id,
        acceptance_contract={"required": False, "flows": []},
        implementation_plan={"repair_continuation": {"source_run_id": root.run_id}},
    )
    store = app.state.container.store
    store.upsert("runs", root.run_id, root.model_dump(mode="json"))
    store.upsert("runs", intermediate.run_id, intermediate.model_dump(mode="json"))
    store.upsert(
        "reports",
        f"acceptance_contract:{workspace['workspace_id']}:{root.run_id}",
        {"workspace_id": workspace["workspace_id"], "run_id": root.run_id, "contract": contract},
    )
    monkeypatch.setattr(run_service, "_execute_run", lambda *_args, **_kwargs: None)

    continuation = run_service.create_run(
        workspace["workspace_id"],
        CreateRunRequest(
            prompt="Continue repair from latest failed case.",
            mode="fix",
            intent="edit",
            generation_mode="fast",
            resume_from_run_id=intermediate.run_id,
        ),
    )
    saved = run_service.get_run(continuation.run_id)

    assert saved.acceptance_contract["required"] is True
    assert saved.acceptance_contract["inherited_from_run_id"] == root.run_id
    assert saved.acceptance_contract["continued_from_run_id"] == intermediate.run_id
    assert saved.implementation_plan["repair_continuation"]["contract_source_run_id"] == root.run_id


def test_runtime_repair_lineage_keeps_acceptance_gate_required(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Runtime Gate Workspace",
            "description": "runtime gate contract test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    contract = {
        "required": True,
        "prompt_hints": {
            "prompt_summary": "shared role workflow",
            "resource_hint": "общий процесс",
            "field_hints": ["название"],
            "role_field_hints": {"client": ["название"], "specialist": ["статус"], "manager": ["решение"]},
            "role_action_prompts": {
                "client": ["создает общий процесс"],
                "specialist": ["обновляет статус"],
                "manager": ["принимает решение"],
            },
        },
        "flows": [{"id": "role_shared_persistence"}],
    }
    root = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Создай mini-app для общего процесса между ролями.",
        intent="create",
        acceptance_contract=contract,
    )
    child = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Repair active case.",
        mode="fix",
        intent="edit",
        resume_from_run_id=root.run_id,
        acceptance_contract={"required": False},
    )
    runtime = app.state.container.workspace_code_agent_runtime
    store = app.state.container.store
    store.upsert("runs", root.run_id, root.model_dump(mode="json"))
    store.upsert("runs", child.run_id, child.model_dump(mode="json"))
    store.upsert(
        "reports",
        f"acceptance_contract:{workspace['workspace_id']}:{root.run_id}",
        {"workspace_id": workspace["workspace_id"], "run_id": root.run_id, "contract": contract},
    )

    inherited = runtime._stored_acceptance_contract_for_runtime(
        workspace_id=workspace["workspace_id"],
        run_id=child.run_id,
        request=GenerateRequest(prompt="Repair active case.", mode="fix", intent="edit", generation_mode="fast", resume_from_run_id=root.run_id),
        stored_run=child.model_dump(mode="json"),
    )
    state = runtime._completion_state(
        workspace_id=workspace["workspace_id"],
        run_id=child.run_id,
        request=GenerateRequest(prompt="Repair active case.", mode="fix", intent="edit", generation_mode="fast", resume_from_run_id=root.run_id),
        results=[
            RunCheckResult(name="platform_invariants", status="passed"),
            RunCheckResult(name="api_workflow_smoke", status="skipped"),
            RunCheckResult(name="browser_flow_smoke", status="skipped"),
        ],
        preview_details={},
        validation_snapshot=None,
        acceptance_contract=inherited,
    )

    assert inherited["required"] is True
    assert state["product_proof_required"] is True
    assert state["strict_green"] is False
    assert {issue["check"] for issue in state["remaining_issues"] if issue["kind"] == "required_product_proof"} == {
        "api_workflow_smoke",
        "browser_flow_smoke",
    }
    assert state["product_readiness"]["evidence"]["generation_sla"]["mode"] == "fast"


def test_completion_gate_blocks_test_only_acceptance_diff(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "Completion Gate Test Only", "target_platform": "telegram_mini_app", "preview_profile": "telegram_mock"},
    ).json()
    workspace_id = workspace["workspace_id"]
    run_id = "run_test_only"
    service = app.state.container.workspace_service
    draft = service.prepare_draft(workspace_id, run_id)
    test_path = draft / "miniapp/tests/generated_app.test.mjs"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("import test from 'node:test';\n// stronger test only\n", encoding="utf-8")

    runtime = app.state.container.workspace_code_agent_runtime
    state = runtime._completion_state(
        workspace_id=workspace_id,
        run_id=run_id,
        request=GenerateRequest(prompt="Fix the product", mode="fix", intent="edit", generation_mode="balanced"),
        results=[
            RunCheckResult(name="platform_invariants", status="passed"),
            RunCheckResult(name="api_workflow_smoke", status="passed"),
            RunCheckResult(name="browser_flow_smoke", status="passed", diagnostics={"mobile_layout": {"status": "passed"}}),
        ],
        preview_details={},
        validation_snapshot=None,
        acceptance_contract={"required": True},
    )

    assert state["strict_green"] is False
    assert any(issue["kind"] == "product_source_diff" for issue in state["remaining_issues"])


def test_completion_gate_blocks_incomplete_product_task_ledger(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "Ledger Gate", "target_platform": "telegram_mini_app", "preview_profile": "telegram_mock"},
    ).json()
    workspace_id = workspace["workspace_id"]
    run_id = "run_ledger_gate"
    service = app.state.container.workspace_service
    draft = service.prepare_draft(workspace_id, run_id)
    app_js = draft / "miniapp/app/static/manager/app.js"
    app_js.parent.mkdir(parents=True, exist_ok=True)
    app_js.write_text("fetch('/api/items');\n", encoding="utf-8")

    runtime = app.state.container.workspace_code_agent_runtime
    state = runtime._completion_state(
        workspace_id=workspace_id,
        run_id=run_id,
        request=GenerateRequest(prompt="Build a manager-heavy workflow", mode="generate", intent="create", generation_mode="balanced"),
        results=[
            RunCheckResult(
                name="platform_invariants",
                status="passed",
                diagnostics={
                    "role_coverage": {
                        "client": {"status": "present", "route_count": 1},
                        "specialist": {"status": "present", "route_count": 1},
                        "manager": {"status": "present", "route_count": 1},
                    }
                },
            ),
            RunCheckResult(name="api_workflow_smoke", status="passed"),
            RunCheckResult(name="browser_flow_smoke", status="passed", diagnostics={"mobile_layout": {"status": "passed"}}),
        ],
        preview_details={},
        validation_snapshot=None,
        acceptance_contract={"required": True},
        implementation_plan={
            "product_task_ledger": [
                {
                    "id": "manager.role_surface",
                    "role": "manager",
                    "kind": "source",
                    "expected_min_routes": 3,
                    "owned_paths": ["miniapp/app/static/manager/app.js"],
                    "proof_checks": ["platform_invariants"],
                }
            ]
        },
    )

    assert state["strict_green"] is False
    issue = next(issue for issue in state["remaining_issues"] if issue["kind"] == "product_task_ledger" and issue["role"] == "manager")
    assert issue["target_files"] == ["miniapp/app/static/manager/app.js"]
    runtime_issue = next(issue for issue in state["remaining_issues"] if issue["kind"] == "runtime_task_ledger")
    assert runtime_issue["task_id"] == "manager.role_surface"
    assert runtime_issue["proof_status"] == "passed"


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


def test_repair_catalog_emits_precise_repair_packet_contract() -> None:
    packet = RepairCatalog.classify_issue(
        {
            "kind": "check_failure",
            "check": "browser_flow_smoke",
            "details": "Browser proof failed after clicking the manager submit button.",
            "evidence": {
                "failed_role": "manager",
                "failed_route": "/manager",
                "failed_selector": "#manager-submit",
                "failed_step": "click submit",
                "network_errors": [{"method": "POST", "url": "http://127.0.0.1:8010/api/requests", "status_code": 500}],
            },
        }
    )

    assert packet["repair_packet"]["schema"] == "grounded.repair_packet.v2"
    assert packet["failed_check"] == "browser_flow_smoke"
    assert packet["normalized_signature"] == "preview.browser_flow_failed"
    assert packet["broken_surface"]["role"] == "manager"
    assert packet["broken_surface"]["route"] == "/manager"
    assert packet["broken_surface"]["selector"] == "#manager-submit"
    assert packet["broken_surface"]["api_route"] == "/api/requests"
    assert "miniapp/app/static/manager/app.js" in packet["likely_files"]
    assert "miniapp/app/routes/api.py" in packet["likely_files"]
    assert packet["probable_files"][0]["path"].startswith("miniapp/")
    assert packet["known_fix_recipe"]["recipe_id"] == packet["repair_recipe_id"]
    assert packet["retry_strategy"]["steps"]
    assert packet["product_guardrails"]["do_not_redesign_product"] is True
    assert packet["repair_confidence"]["score"] >= 0.8
    assert packet["post_fix_proof"]["check"] == "browser_flow_smoke"
    assert packet["post_repair_proof"]["check"] == "browser_flow_smoke"
    assert "console and network errors are empty" in packet["post_fix_proof"]["evidence_required"]


def test_repair_classifier_selects_focused_recipe_and_relevant_checks() -> None:
    api = RepairClassifier.classify(
        {
            "verification_check": "api_workflow_smoke",
            "failure_signature": "sqlite OperationalError: no such table: requests",
            "target_files": ["miniapp/app/routes/api.py"],
            "evidence": {"logs": ["sqlite3.OperationalError: no such table: requests"], "path": "miniapp/app/db.py"},
        }
    )
    selector = RepairClassifier.classify(
        {
            "verification_check": "frontend_interaction_static_smoke",
            "details": "Visible workflow form has no submit handler for selector #manager-submit",
            "evidence": {"failed_role": "manager", "failed_selector": "#manager-submit"},
        }
    )
    overflow = RepairClassifier.classify(
        {
            "verification_check": "browser_flow_smoke",
            "details": "mobile viewport reports horizontal overflow and overlap",
            "evidence": {"failed_role": "client", "path": "miniapp/app/static/client/styles.css"},
        }
    )

    assert api["repair_class"] == "db_schema"
    assert api["recipe"]["recipe_id"] == "repair.db_schema_contract"
    assert api["focused_patch_plan"]["allowed_files"][0] == "miniapp/app/routes/api.py"
    assert [item["check"] for item in api["relevant_checks"]] == ["api_workflow_smoke", "generated_app_python_tests"]
    assert api["escalation"]["escalate_to"] == "full_repair"
    assert selector["repair_class"] == "selector"
    assert selector["focused_patch_plan"]["selector"] == "#manager-submit"
    assert selector["check_profile"] == "focused_frontend_interaction"
    assert overflow["repair_class"] == "css_overflow"
    assert overflow["recipe"]["focused"] is True


def test_repair_catalog_and_case_include_repair_class_recipe_and_focused_checks(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    service = app.state.container.repair_case_service
    packet = RepairCatalog.classify_issue(
        {
            "kind": "check_failure",
            "check": "generated_app_js_tests",
            "details": "generated_app_js_tests failed because of stale selector assertion for #client-save",
            "paths": ["miniapp/tests/generated_app.test.mjs", "miniapp/app/static/client/app.js"],
        }
    )

    cases = service.sync_from_packets(
        workspace_id="ws_classifier",
        run_id="run_classifier",
        packets=[packet],
        source="agent_loop_checks",
    )
    active = cases["active_case"]

    assert packet["repair_class"] == "selector"
    assert packet["repair_packet"]["focused_patch_plan"]["mode"] == "focused_patch"
    assert active["repair_class"] == "selector"
    assert active["known_fix_recipe"]["classifier_recipe_id"] == "repair.selector_wiring"
    assert active["next_action"]["focused_patch_plan"]["mode"] == "focused_patch"
    assert active["next_action"]["relevant_checks"][0]["check"] == "frontend_interaction_static_smoke"
    assert active["repair_prompt"]["sections"]["escalation"]["escalate_to"] == "full_repair"
    RepairCase.model_validate(active)


def test_repair_catalog_v2_normalizes_dynamic_error_signatures_and_dedupes() -> None:
    packets = RepairCatalog.classify_many(
        [
            {
                "check": "custom_quality_check",
                "failure_signature": "custom_quality_check: line 42 http://127.0.0.1:8010/api/items run_abc123 expected 500",
                "details": "Prompt-specific invariant failed.",
                "paths": ["miniapp/app/static/client/app.js"],
            },
            {
                "check": "custom_quality_check",
                "failure_signature": "custom_quality_check: line 77 http://127.0.0.1:9010/api/items run_def456 expected 500",
                "details": "Prompt-specific invariant failed.",
                "paths": ["miniapp/app/static/client/app.js"],
            },
        ]
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet["repair_catalog_version"] == "v2"
    assert packet["signature_normalization"]["algorithm"] == "repair_signature_normalizer.v2"
    assert packet["normalized_signature"] == "custom_quality_check: line <n> <url> run_<id> expected <n>"
    assert packet["repair_confidence"]["score"] < 0.7
    assert packet["retry_strategy"]["policy_id"] == "evidence_driven_repair_case"
    assert packet["post_repair_proof"]["check"] == "custom_quality_check"


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


def test_thread_metadata_archive_resume_and_fork_are_append_only(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Thread Store Workspace",
            "description": "thread metadata test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.thread_service
    thread = service.start_thread(workspace_id=workspace["workspace_id"], title="Thread store")

    archived = service.archive_thread(thread.thread_id)
    resumed = service.resume_thread(thread.thread_id)
    fork = service.fork_thread(thread.thread_id, title="Thread fork")
    events = app.state.container.platform_db.list_events(thread.thread_id, limit=50)
    fork_events = app.state.container.platform_db.list_events(fork.thread_id, limit=20)
    snapshots = app.state.container.platform_db.list_thread_snapshots(thread.thread_id, limit=10)

    assert archived.metadata["stable_thread"]["schema"] == "grounded.thread_metadata.v1"
    assert resumed.archived is False
    assert resumed.metadata["stable_thread"]["status"] == "idle"
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert {event.event_type for event in events} >= {"thread.started", "thread.archived", "thread.unarchived", "thread.snapshot"}
    assert fork.forked_from_thread_id == thread.thread_id
    assert fork.metadata["fork"]["source_snapshot_id"] == snapshots[0]["snapshot_id"]
    assert fork_events[-1].event_type == "thread.forked"


def test_thread_live_writer_and_recovery_persist_unfinished_run_state(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Live Writer Workspace",
            "description": "thread live writer test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.thread_service
    thread = service.start_thread(workspace_id=workspace["workspace_id"], title="Live writer")
    turn = TurnRecord(thread_id=thread.thread_id, workspace_id=workspace["workspace_id"], status="running", prompt="Build app", started_at=service._now())
    app.state.container.platform_db.insert_turn(turn)
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build app",
        intent="create",
        status="running",
        current_stage="generating_code",
        progress_percent=42,
        session_id=thread.thread_id,
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    turn.linked_run_id = run.run_id
    app.state.container.platform_db.insert_turn(turn)
    thread.status = "running"
    thread.current_turn_id = turn.turn_id
    app.state.container.platform_db.upsert_thread(thread)

    live = service.write_live_snapshot(thread.thread_id, reason="test")
    recovered = service.recover_thread_state(thread.thread_id)
    events = app.state.container.platform_db.list_events(thread.thread_id, limit=50)
    run_snapshots = app.state.container.platform_db.list_run_state_snapshots(run.run_id, limit=10)

    assert live["schema"] == "grounded.thread_live_writer.v1"
    assert live["run"]["current_stage"] == "generating_code"
    assert live["run"]["progress_percent"] == 42
    assert recovered.status == "running"
    assert any(event.event_type == "run.live_snapshot" for event in events)
    assert any(snapshot["reason"].startswith("thread_live:") for snapshot in run_snapshots)


def test_thread_recovery_closes_terminal_linked_run_after_crash(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Recovery Workspace",
            "description": "thread recovery test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.thread_service
    thread = service.start_thread(workspace_id=workspace["workspace_id"], title="Recovery")
    turn = TurnRecord(thread_id=thread.thread_id, workspace_id=workspace["workspace_id"], status="running", prompt="Build app", started_at=service._now())
    run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build app",
        intent="create",
        status="completed",
        apply_status="applied",
        current_stage="completed",
        progress_percent=100,
        session_id=thread.thread_id,
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    turn.linked_run_id = run.run_id
    app.state.container.platform_db.insert_turn(turn)
    thread.status = "running"
    thread.current_turn_id = turn.turn_id
    app.state.container.platform_db.upsert_thread(thread)

    recovered = service.recover_thread_state(thread.thread_id)
    recovered_turn = app.state.container.platform_db.get_turn(turn.turn_id)
    events = app.state.container.platform_db.list_events(thread.thread_id, limit=50)

    assert recovered.status == "idle"
    assert recovered_turn is not None and recovered_turn.status == "completed"
    assert any(event.event_type == "thread.recovered" for event in events)


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
    completed_session = _wait_for_exec_session(app, process_id)
    resized = service.resize_exec(process_id, cols=100, rows=32)
    audit = client.get(f"/workspaces/{workspace['workspace_id']}/permissions/command-audit").json()

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
    assert started["policy_decision"]["resolved_argv"]
    assert Path(started["policy_decision"]["resolved_argv"][0]).is_absolute()
    assert {item["source"] for item in audit["items"]} >= {"command.exec.evaluate", "command.exec.start"}
    assert output["status"] == "completed"
    assert "app" in output["content"]
    assert completed_session["sandbox_boundary"]["schema"] == "grounded.sandbox.runtime_boundary.v1"
    assert completed_session["environment_snapshot"]["snapshot_sha256"]
    assert completed_session["log_capture"]["stdout"]["total_chars"] >= len(output["content"])
    assert completed_session["result"]["killed_diagnostics"]["reason"] == "none"
    assert resized["ok"] is True
    assert "Absolute and home-relative paths are blocked" in escaped_error
    if symlink_session is not None:
        assert symlink_session["result"]["semantic_status"] == "blocked_by_sandbox"


def test_exec_runtime_manages_long_running_process_until_terminate(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Managed Exec Workspace",
            "description": "managed exec runtime test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.thread_service
    source_dir = app.state.container.workspace_service.source_dir(workspace["workspace_id"])
    fifo_path = source_dir / "miniapp" / "app" / "live.pipe"
    os.mkfifo(fifo_path)

    started = service.exec_command(workspace_id=workspace["workspace_id"], command="cat miniapp/app/live.pipe", timeout=0, managed=True)
    process_id = started["process_id"]
    for _ in range(50):
        snapshot = app.state.container.exec_runtime_service.snapshot()
        if any(item.get("process_id") == process_id for item in snapshot.get("managed_processes", [])):
            break
        time.sleep(0.02)

    release_writer = threading.Event()

    def write_fifo() -> None:
        with fifo_path.open("w", encoding="utf-8") as fifo:
            fifo.write("managed line\n")
            fifo.flush()
            release_writer.wait(timeout=3)

    writer = threading.Thread(target=write_fifo, daemon=True)
    writer.start()
    for _ in range(50):
        output = service.read_exec_output(process_id, stream="stdout", start=0)
        if "managed line" in output["content"]:
            break
        time.sleep(0.02)
    else:
        output = service.read_exec_output(process_id, stream="stdout", start=0)
    next_output = service.read_exec_output(process_id, stream="stdout", start=output["next_start"])
    terminated = service.terminate_exec(process_id)
    release_writer.set()
    writer.join(timeout=3)
    completed_session = _wait_for_exec_session(app, process_id)

    assert started["managed"] is True
    assert started["timeout_seconds"] == 0
    assert "managed line" in output["content"]
    assert output["managed"] is True
    assert next_output["content"] == ""
    assert terminated["ok"] is True
    assert completed_session["result"]["killed_diagnostics"]["reason"] == "manual_terminate"


def test_workspace_source_apply_blocks_draft_symlink_before_mutating_source(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Symlink Apply Workspace",
            "description": "sandbox source apply",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.workspace_service
    source = service.source_dir(workspace["workspace_id"])
    original_readme = (source / "README.md").read_text(encoding="utf-8")
    draft = service.prepare_draft(workspace["workspace_id"], "run_symlink")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    symlink_path = draft / "miniapp/app/static/client/evil.js"
    symlink_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        symlink_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this filesystem")

    with pytest.raises(SandboxViolationError):
        service.approve_draft(workspace["workspace_id"], "run_symlink", "blocked symlink apply")

    assert (source / "README.md").read_text(encoding="utf-8") == original_readme
    assert not (source / "miniapp/app/static/client/evil.js").exists()


def test_workspace_source_apply_blocks_hardlinked_target(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "Hardlink Apply Workspace",
            "description": "sandbox hardlink apply",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    service = app.state.container.workspace_service
    workspace_id = workspace["workspace_id"]
    source = service.source_dir(workspace_id)
    draft = service.prepare_draft(workspace_id, "run_hardlink")
    (draft / "README.md").write_text("# changed\n", encoding="utf-8")
    peer = tmp_path / "readme-peer.md"
    try:
        os.link(source / "README.md", peer)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")

    with pytest.raises(SandboxViolationError):
        service.apply_selected_draft_files(workspace_id, "run_hardlink", ["README.md"], message="blocked hardlink apply")

    assert peer.read_text(encoding="utf-8") == (source / "README.md").read_text(encoding="utf-8")


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
    journal = client.get(f"/runs/{run.run_id}/events-v2").json()
    event_types = {item["event_type"] for item in journal["items"]}
    assert {"apply.staged", "apply.applied", "apply.discarded"}.issubset(event_types)


def test_diff_review_panel_groups_risk_coverage_and_revert_actions(tmp_path: Path) -> None:
    app, client, workspace, run = _workspace_with_run(tmp_path)
    run.prompt = "Build a client request workflow with manager review"
    run.draft_ready = True
    run.draft_status = "ready"
    run.touched_files = [
        "miniapp/app/static/client/app.js",
        "miniapp/tests/generated_app.test.mjs",
        "platform/backend/app/services/internal.py",
    ]
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    app.state.container.store.upsert(
        "reports",
        f"run_artifacts:{run.run_id}",
        {
            "diff": "\n".join(
                [
                    "diff --git a/miniapp/app/static/client/app.js b/miniapp/app/static/client/app.js",
                    "+submitClientRequest();",
                    "diff --git a/miniapp/tests/generated_app.test.mjs b/miniapp/tests/generated_app.test.mjs",
                    "+await page.click('#client-submit');",
                    "diff --git a/platform/backend/app/services/internal.py b/platform/backend/app/services/internal.py",
                    "+INTERNAL_FLAG = True",
                ]
            ),
            "check_results": [
                {"name": "changed_files_static", "status": "passed", "details": "syntax ok"},
                {"name": "frontend_interaction_static_smoke", "status": "passed", "details": "handlers ok"},
                {"name": "browser_flow_smoke", "status": "passed", "diagnostics": {"roles_checked": ["client"]}},
                {"name": "generated_app_js_tests", "status": "passed", "details": "workflow test passed"},
            ],
        },
    )

    review = client.get(f"/runs/{run.run_id}/diff-review").json()

    by_path = {item["path"]: item for item in review["files"]}
    group_keys = {item["key"] for item in review["groups"]}
    assert review["schema"] == "grounded.run_diff_review.v1"
    assert review["summary"]["file_count"] == 3
    assert review["summary"]["platform_file_count"] == 1
    assert review["summary"]["highest_risk"] == "high"
    assert "generated_app:client_surface_worker" in group_keys
    assert "generated_tests:tests" in group_keys
    assert "platform:other" in group_keys
    assert by_path["miniapp/app/static/client/app.js"]["risk"] == "medium"
    assert by_path["platform/backend/app/services/internal.py"]["risk"] == "high"
    assert "run prompt" in by_path["miniapp/app/static/client/app.js"]["why_changed"]
    assert {item["check"] for item in by_path["miniapp/app/static/client/app.js"]["coverage"]}.issuperset({"changed_files_static", "browser_flow_smoke"})
    assert by_path["miniapp/app/static/client/app.js"]["actions"][1]["action"] == "revert_draft_file"
    assert by_path["miniapp/app/static/client/app.js"]["actions"][1]["enabled"] is True
    assert any(item["action"] == "revert_all_draft_files" for item in review["actions"])


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


def test_webhook_contracts_support_idempotent_sdk_management(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "SDK hooks", "description": "typed sdk", "target_platform": "telegram_mini_app"},
    ).json()
    payload = {
        "url": "https://example.com/grounded-webhook",
        "events": ["run.completed", "check.failed"],
        "workspace_id": workspace["workspace_id"],
        "metadata": {"owner": "sdk"},
        "secret": "local-signing-secret",
    }

    created = client.post("/webhooks", json=payload, headers={"Idempotency-Key": "sdk-webhook-1"})
    duplicate = client.post("/webhooks", json={**payload, "description": "ignored duplicate"}, headers={"Idempotency-Key": "sdk-webhook-1"})
    listed = client.get("/webhooks", params={"workspace_id": workspace["workspace_id"]}).json()
    detail = client.get(f"/webhooks/{created.json()['webhook_id']}").json()
    delivery = client.post(
        f"/webhooks/{created.json()['webhook_id']}/test",
        json={"event_type": "run.completed", "payload": {"run_id": "run_sdk"}},
    ).json()
    invalid_secret = client.post(
        "/webhooks",
        json={"url": "https://example.com/hook", "events": ["run.completed"], "metadata": {"api_token": "hidden"}},
    )
    openapi = client.get("/openapi.json").json()

    assert created.status_code == 200
    assert duplicate.json()["webhook_id"] == created.json()["webhook_id"]
    assert created.json()["secret_configured"] is True
    assert "secret_sha256" not in created.json()
    assert listed["schema"] == "grounded.webhooks.v1"
    assert listed["items"][0]["webhook_id"] == created.json()["webhook_id"]
    assert detail["metadata"] == {"owner": "sdk"}
    assert delivery["status"] == "simulated"
    assert delivery["payload_preview"]["run_id"] == "run_sdk"
    assert invalid_secret.status_code == 400
    assert "WebhookSubscription" in openapi["components"]["schemas"]


def test_observability_report_tracks_cost_latency_green_rate_and_repairs(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={"name": "Observability", "description": "metrics", "target_platform": "telegram_mini_app"},
    ).json()
    green_run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a green app",
        intent="create",
        generation_mode="balanced",
        target_role_scope=["client"],
        model_profile="test",
        llm_model="gpt-5.4-mini",
        status="completed",
        apply_status="applied",
        checks_summary={"validators": "passed", "build": "passed", "preview": "passed", "gate_status": "passed", "issues": []},
        token_usage={"input_tokens": 1000, "output_tokens": 500, "reasoning_tokens": 100, "total_tokens": 1500, "turn_count": 2},
        latency_breakdown={"agent_total_ms": 1200, "checks_ms": 300},
        orchestration_phases=[{"phase": "planner", "tokens": 500}, {"phase": "ui", "tokens": 1000}],
    )
    green_run.created_at = green_run.created_at.replace(hour=10, minute=0, second=0, microsecond=0)
    green_run.updated_at = green_run.created_at.replace(hour=10, minute=2, second=0)
    failed_run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Build a failing app",
        intent="create",
        generation_mode="quality",
        target_role_scope=["client"],
        model_profile="test",
        llm_model="gpt-5.4-mini",
        status="failed",
        failure_class="browser_flow_smoke",
        failure_reason="button did not navigate",
        checks_summary={"validators": "passed", "build": "passed", "preview": "failed", "gate_status": "failed", "issues": []},
        token_usage={"input_tokens": 2000, "output_tokens": 1000, "total_tokens": 3000, "turn_count": 3},
        latency_breakdown={"agent_total_ms": 2400, "preview_ms": 900},
    )
    fix_run = RunRecord(
        workspace_id=workspace["workspace_id"],
        prompt="Fix the failing app",
        mode="fix",
        intent="edit",
        generation_mode="fast",
        target_role_scope=["client"],
        model_profile="test",
        llm_model="gpt-5.4-mini",
        status="completed",
        apply_status="applied",
        checks_summary={"validators": "passed", "build": "passed", "preview": "passed", "gate_status": "passed", "issues": []},
        token_usage={"input_tokens": 800, "output_tokens": 400, "total_tokens": 1200, "turn_count": 1},
        latency_breakdown={"agent_total_ms": 800},
        repair_iterations=[{"status": "passed"}],
    )
    fix_run.created_at = fix_run.created_at.replace(hour=11, minute=0, second=0, microsecond=0)
    fix_run.updated_at = fix_run.created_at.replace(hour=11, minute=1, second=0)
    for run in (green_run, failed_run, fix_run):
        app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))
    case_ref = f"repair_case:{workspace['workspace_id']}:{failed_run.run_id}:case_1"
    app.state.container.store.upsert(
        "reports",
        f"repair_cases:{failed_run.run_id}",
        {"schema": "grounded.repair_cases.v1", "run_id": failed_run.run_id, "case_refs": [case_ref]},
    )
    app.state.container.store.upsert(
        "reports",
        case_ref,
        {
            "schema": "grounded.repair_case.v1",
            "workspace_id": workspace["workspace_id"],
            "run_id": failed_run.run_id,
            "case_id": "case_1",
            "status": "resolved",
            "attempts": [{"attempt_id": "attempt_1", "status": "passed"}],
            "updated_at": "2026-05-20T00:00:00+00:00",
        },
    )

    report = client.get("/system/metrics/summary").json()
    scoped = client.get(f"/workspaces/{workspace['workspace_id']}/observability").json()
    openapi = client.get("/openapi.json").json()

    assert report["schema"] == "grounded.observability.v1"
    assert report["run_count"] == 3
    assert report["completed_runs"] == 2
    assert report["token_usage_total"] == 5700
    assert report["token_usage"]["input_tokens"] == 3800
    assert report["cost"]["estimated_cost_usd"] > 0
    assert report["latency"]["p95_ms"] >= 1200
    assert report["latency"]["phase_totals_ms"]["agent_total_ms"] == 4400
    assert any(item["generation_mode"] == "balanced" and item["green_rate"] == 1.0 for item in report["green_rate_by_generation_mode"])
    assert report["failure_classes"][0]["failure_class"] == "browser_flow_smoke"
    assert report["repair_success"]["fix_success_rate"] == 1.0
    assert report["repair_success"]["attempt_success_rate"] == 1.0
    assert report["quality_dashboard"]["schema"] == "grounded.quality_observability_dashboard.v1"
    assert report["quality_dashboard"]["completed_product_count"] == 2
    assert report["quality_dashboard"]["retry_run_count"] >= 1
    assert report["time_to_completed_product"]["completed_product_count"] == 2
    assert report["time_to_completed_product"]["average_ms"] == 90000
    assert any(item["phase"] == "planner" and item["total_tokens"] == 500 for item in report["tokens_per_phase"])
    assert report["retries_per_run"][0]["retry_count"] >= 1
    assert report["cost_by_workspace"][0]["workspace_id"] == workspace["workspace_id"]
    assert any(item["model"] == "gpt-5.4-mini" and item["task_type"] == "generate:balanced" for item in report["model_performance_by_task_type"])
    assert scoped["workspace_id"] == workspace["workspace_id"]
    assert "ObservabilityReport" in openapi["components"]["schemas"]
