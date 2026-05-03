from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import RunRecord
from app.services.exec_policy_service import ExecPolicyService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, canonical_tool_name, tool_envelope


def test_tool_protocol_normalizes_aliases() -> None:
    assert canonical_tool_name("run_command") == "shell.exec"
    envelope = tool_envelope(tool="apply_patch_to_draft", input_payload={"path": "miniapp/app/main.py"})

    assert envelope["version"] == TOOL_PROTOCOL_VERSION
    assert envelope["tool"] == "patch.apply"
    assert envelope["risk"] == "mutating"
    assert envelope["approval"]["status"] == "not_required"


def test_exec_policy_classifies_and_redacts_commands() -> None:
    service = ExecPolicyService()

    allowed = service.evaluate_command("rg api miniapp/app")
    blocked = service.evaluate_command("rm -rf miniapp")
    redacted = service.evaluate_command("rg api_key=sk-secretvalue miniapp/app")

    assert allowed["decision"]["action"] == "allow"
    assert allowed["decision"]["risk"] == "read_only"
    assert blocked["decision"]["action"] == "forbidden"
    assert blocked["approval"]["status"] == "blocked"
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
        prompt="Build a CRM",
        intent="create",
        target_role_scope=["client", "specialist", "manager"],
        model_profile="test",
        linked_job_id=None,
    )
    app.state.container.store.upsert("runs", run.run_id, run.model_dump(mode="json"))

    policy = client.get("/system/policies/exec").json()
    evaluation = client.post(
        f"/workspaces/{workspace['workspace_id']}/policy/evaluate-command",
        json={"command": "rg CRM miniapp/app"},
    ).json()
    timeline = client.get(f"/runs/{run.run_id}/timeline").json()
    doctor = client.get("/doctor").json()
    memory = client.post(
        f"/workspaces/{workspace['workspace_id']}/memory",
        json={"kind": "project_rule", "text": "Use dense operational UI."},
    ).json()
    skills = client.get("/skills").json()
    workers = client.get(f"/runs/{run.run_id}/workers").json()

    assert policy["tool_protocol_version"] == TOOL_PROTOCOL_VERSION
    assert evaluation["decision"]["action"] == "allow"
    assert timeline["items"][0]["kind"] == "prompt"
    assert doctor["checks"]
    assert memory["items"][0]["text"] == "Use dense operational UI."
    assert any(item["id"] == "crm" for item in skills["items"])
    assert any(item["id"] == "reservation" for item in skills["items"])
    assert any(item["worker_id"] == "backend_api" for item in workers["workers"])
