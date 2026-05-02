from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.ai.openai_client import OpenAIClient
from app.ai.model_registry import CODEX_MINI_MODEL, models_for_role
from app.models.common import GenerationMode
from app.models.domain import CheckExecutionRecord, DraftAction, RunCheckResult, utc_now
from app.modules.miniapp_agent_loop.agent_file_state import AgentFileStateCache
from app.modules.miniapp_agent_loop.agent_command_policy import DEFAULT_COMMAND_POLICY, decide_workspace_command
from app.modules.miniapp_agent_loop.agent_coordinator import AgentCoordinator
from app.modules.miniapp_agent_loop.agent_hooks import AgentHookManager
from app.modules.miniapp_agent_loop.agent_kernel import agent_tool_kind, plan_agent_tool_batches
from app.modules.miniapp_agent_loop.agent_memory_store import AgentMemoryStore
from app.modules.miniapp_agent_loop.agent_process_manager import AgentProcessManager, HeadTailOutputBuffer
from app.modules.miniapp_agent_loop.agent_scratchpad import AgentScratchpad
from app.modules.miniapp_agent_loop.agent_transcript import AgentTranscriptStore
from app.modules.miniapp_agent_loop.agent_tool_registry import AgentToolRegistry
from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager
from app.modules.miniapp_agent_loop.agent_worker_branch_loop import AgentWorkerBranchLoop
from app.modules.miniapp_agent_loop.agent_worker_runtime import AgentWorkerRuntime
from app.modules.miniapp_agent_loop.agent_worker_tasks import AgentWorkerTaskPlanner
from app.models.artifacts import ApplyPatchResult
from app.modules.miniapp_agent_loop.context_pressure import AgentContextPressureAnalyzer
from app.modules.miniapp_agent_loop.edit_validator import AgentEditValidator
from app.modules.miniapp_agent_loop.rollout_trace import RolloutTraceRecorder
from app.modules.miniapp_agent_loop.semantic_tools import semantic_scan
from app.modules.miniapp_agent_loop.tool_agent_runtime import validate_workspace_command
from app.modules.miniapp_agent_loop.turn_diff_tracker import AgentTurnDiffTracker
from app.modules.miniapp_agent_loop.types import AgentTurnPlan
from app.modules.miniapp_agent_loop.verification_worker import VerificationWorker
from app.modules.workspace_code_agent_runtime.browser_replay import BrowserProofReplay
from app.modules.workspace_code_agent_runtime.check_orchestrator import WorkspaceAgentCheckOrchestrator
from app.modules.workspace_code_agent_runtime.process_recovery import AgentProcessRecovery
from app.modules.workspace_code_agent_runtime.prompt_contract import agent_system_prompt


def test_code_agent_defaults_to_mini_for_all_generation_modes() -> None:
    for mode in (GenerationMode.FAST, GenerationMode.BALANCED, GenerationMode.QUALITY):
        assert models_for_role("agent_turn", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("repair", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL
        assert models_for_role("summarize", model_profile="", generation_mode=mode) == CODEX_MINI_MODEL


def test_agent_prompt_is_tool_loop_contract_not_domain_template() -> None:
    prompt = agent_system_prompt()

    assert "plan, inspect, patch the draft, run checks/browser proof" in prompt
    assert "The user prompt is the only product source" in prompt
    assert "three separate role apps" in prompt
    assert "Do not add mock data, seed data, demo data, sample data" in prompt


def test_agent_tools_batch_reads_and_serialize_mutations() -> None:
    plan = plan_agent_tool_batches(
        [
            {"tool": "read_files", "targets": ["miniapp/app/main.py"]},
            {"tool": "semantic_scan", "targets": ["miniapp/app"]},
            {"tool": "browser_verify", "targets": ["/client"]},
            {"tool": "apply_patch_to_draft", "targets": ["miniapp/app/main.py"], "diff": "@@\n-old\n+new\n"},
            {"tool": "write_file", "targets": ["miniapp/app/static/client/app.js"], "content": "console.log(1);\n"},
        ]
    )

    assert agent_tool_kind("read_files") == "read_only"
    assert agent_tool_kind("apply_patch_to_draft") == "mutating"
    assert AgentToolRegistry.spec("browser_verify") is not None
    assert AgentToolRegistry.spec("browser_verify").kind == "verification"  # type: ignore[union-attr]
    assert [item["tool"] for item in plan.read_only_requests] == ["read_files", "semantic_scan", "browser_verify"]
    assert [item["tool"] for item in plan.mutating_requests] == ["apply_patch_to_draft", "write_file"]
    assert [[item["tool"] for item in batch.requests] for batch in plan.ordered_batches] == [
        ["read_files", "semantic_scan"],
        ["browser_verify"],
        ["apply_patch_to_draft"],
        ["write_file"],
    ]


def test_agent_registry_exposes_real_openai_tool_contract() -> None:
    tools = AgentToolRegistry.openai_tools()
    encoded = json.dumps(tools)

    assert any(tool["name"] == "apply_patch_to_draft" for tool in tools)
    assert any(tool["name"] == "browser_verify" for tool in tools)
    assert ("tool_" + "requests") not in encoded
    assert all(tool["type"] == "function" for tool in tools)


def test_openai_tool_step_extracts_response_function_calls() -> None:
    parsed = OpenAIClient._extract_response_tool_step(
        {
            "id": "resp_1",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Reading first."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_files",
                    "arguments": '{"targets":["miniapp/app/main.py"],"reason":"inspect"}',
                },
            ],
        }
    )

    assert parsed["assistant_message"] == "Reading first."
    assert parsed["tool_calls"][0]["tool"] == "read_files"  # type: ignore[index]
    assert parsed["tool_calls"][0]["tool_use_id"] == "call_1"  # type: ignore[index]


def test_openai_tool_result_messages_are_model_conversation_items() -> None:
    messages = [{"tool_use_id": "call_1", "tool": "read_files", "output": {"ok": True}}]

    response_items = OpenAIClient._responses_tool_result_items(messages)
    chat_items = OpenAIClient._chat_tool_result_messages(messages)

    assert response_items == [{"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'}]
    assert chat_items[0]["role"] == "assistant"
    assert chat_items[0]["tool_calls"][0]["id"] == "call_1"  # type: ignore[index]
    assert chat_items[1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}


def test_agent_transcript_tracks_pending_tool_results_and_reduced_graph() -> None:
    transcript = AgentTranscriptStore()
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Read files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "read_files", "targets": ["miniapp/app/main.py"]}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "read_files", "files": []}])

    context = transcript.next_model_context("run_1")
    snapshot = transcript.snapshot("run_1")

    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"
    assert snapshot["counts"]["model_turn"] == 1
    assert snapshot["counts"]["tool_result"] == 1
    assert snapshot["reduced_graph"][0]["event_type"] == "model_turn"


def test_agent_transcript_persists_and_restores_pending_tool_results() -> None:
    writes: list[dict[str, object]] = []
    transcript = AgentTranscriptStore()
    transcript.configure_persistence("run_1", writer=lambda payload: writes.append(payload))
    transcript.append_model_turn(
        "run_1",
        attempt=1,
        tool_round=0,
        response_id="resp_1",
        assistant_message="Search files.",
        tool_calls=[{"tool_use_id": "call_1", "tool": "search_files"}],
        model="gpt-5.4-mini",
    )
    transcript.append_tool_results("run_1", [{"tool_use_id": "call_1", "tool": "search_files", "matches": []}])

    restored = AgentTranscriptStore()
    restored.restore("run_1", writes[-1])
    context = restored.next_model_context("run_1")

    assert len(writes) >= 3
    assert context["previous_response_id"] == "resp_1"
    assert context["tool_result_messages"][0]["tool_use_id"] == "call_1"


def test_agent_edit_validator_rejects_unsafe_or_invalid_file_changes() -> None:
    unsafe = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[DraftAction(file_path="../miniapp/app/main.py", operation="replace", content="x", reason="bad")],
        )
    )
    invalid_patch = AgentEditValidator.normalize_plan(
        AgentTurnPlan(
            outcome="changes_ready",
            file_changes=[
                DraftAction(
                    file_path="miniapp/app/static/client/app.js",
                    operation="patch",
                    diff="not a unified diff",
                    reason="bad",
                )
            ],
        )
    )

    assert unsafe.failure_signature == "generation.invalid_edit_operation:unsafe_path"
    assert invalid_patch.failure_signature == "generation.invalid_edit_operation:invalid_patch_diff"


def test_safe_diagnostic_commands_are_scoped() -> None:
    assert validate_workspace_command("python -m unittest discover") is None
    assert validate_workspace_command("node --test tests/generated_app.test.mjs") is None
    assert validate_workspace_command("rg api miniapp/app") is None
    assert validate_workspace_command("npm install") is not None
    assert validate_workspace_command("rm -rf miniapp") is not None
    assert validate_workspace_command("curl https://example.com") is not None


def test_command_policy_returns_typed_decisions() -> None:
    allowed = decide_workspace_command("python -m py_compile miniapp/app/main.py")
    miniapp_cwd = decide_workspace_command("cd miniapp && node --check tests/generated_app.test.mjs")
    denied = decide_workspace_command("npm install")
    examples = DEFAULT_COMMAND_POLICY.validation_examples()

    assert allowed.allowed is True
    assert allowed.action == "allow"
    assert miniapp_cwd.allowed is True
    assert miniapp_cwd.cwd_policy == "miniapp"
    assert denied.allowed is False
    assert denied.action == "forbidden"
    assert all(item["status"] == "passed" for item in examples)


def test_process_manager_streams_head_tail_and_rg_no_match_is_success(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    decision = decide_workspace_command("rg missing-token miniapp/app")

    result = AgentProcessManager().run(
        draft_source=root,
        command="rg missing-token miniapp/app",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
        progress_callback=lambda payload: events.append(payload),
    ).as_dict()

    assert result["exit_code"] == 1
    assert str(result["process_id"]).startswith("proc_")
    assert result["semantic_status"] == "no_matches"
    assert result["success"] is True
    assert any(event.get("status") == "started" for event in events)
    assert any(event.get("status") == "completed" for event in events)


def test_process_manager_keeps_completed_output_readable(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("print('ready')\n", encoding="utf-8")
    manager = AgentProcessManager()
    decision = decide_workspace_command("python3 -m py_compile miniapp/app/main.py")

    result = manager.run(
        draft_source=root,
        command="python3 -m py_compile miniapp/app/main.py",
        decision=decision,
        timeout_seconds=5,
        max_output_chars=800,
    ).as_dict()
    output = manager.read_output(str(result["process_id"]), stream="stderr")

    assert result["success"] is True
    assert output["process_id"] == result["process_id"]
    assert manager.snapshot()["processes"][0]["status"] == "completed"  # type: ignore[index]


def test_head_tail_output_buffer_omits_middle() -> None:
    buffer = HeadTailOutputBuffer(max_chars=20)
    buffer.append("a" * 30)
    buffer.append("b" * 30)
    snapshot = buffer.snapshot()

    assert snapshot["total_chars"] == 60
    assert snapshot["omitted_chars"] > 0
    assert "omitted" in snapshot["excerpt"]


def test_context_pressure_recommends_compaction_for_large_payload() -> None:
    pressure = AgentContextPressureAnalyzer().analyze_payload(
        {
            "file_contexts": {"miniapp/app/main.py": "x" * 45_000},
            "tool_results": [{"tool": "run_command", "stdout": "y" * 45_000}],
            "agent_memory": {"notes": "z" * 25_000},
        }
    )

    assert pressure["compact_recommended"] is True
    assert {item["kind"] for item in pressure["suggestions"]} & {"narrow_file_context", "spill_tool_results", "compact_memory"}


def test_hook_manager_records_lifecycle_events() -> None:
    hooks = AgentHookManager()
    hooks.record("run_1", "pre_tool_use", status="started", payload={"tool": "read_files"})
    hooks.record("run_1", "post_tool_use", status="completed", payload={"tool": "read_files"})
    snapshot = hooks.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["counts"]["pre_tool_use:started"] == 1
    assert snapshot["counts"]["post_tool_use:completed"] == 1


def test_coordinator_scratchpad_and_memory_compact_context(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app").mkdir(parents=True)
    (root / "miniapp/app/main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        implementation_plan={"summary": "Build a role workflow", "roles": ["client"], "primary_entities": ["item"]},
    )
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_plan({"summary": "Build a role workflow", "roles": ["client"]}, coordinator.snapshot()["todo_plan"])  # type: ignore[arg-type]
    scratchpad.record_compact_boundary(
        plan={"summary": "Build a role workflow"},
        diff_summary="miniapp/app/main.py changed",
        failed_signatures=["ui_state_not_visible"],
        next_action="patch visible state rendering",
    )
    memory = AgentMemoryStore()
    memory.add("run_1", "reference", "Check miniapp/app/main.py and /api/entities before reuse.")
    stale_checks = memory.verify_stale_claims("run_1", root)

    assert coordinator.snapshot()["worker_specs"]
    assert coordinator.verification_completed() is False
    coordinator.complete_phase("browser_verifying")
    assert coordinator.verification_completed() is True
    assert "Build a role workflow" in scratchpad.snapshot()["files"]["plan.md"]  # type: ignore[index]
    assert scratchpad.snapshot()["compact_boundaries"][0]["failed_signatures"] == ["ui_state_not_visible"]  # type: ignore[index]
    assert stale_checks[0]["paths"][0]["exists"] is True  # type: ignore[index]


def test_coordinator_todo_gate_requires_real_build_and_verification() -> None:
    coordinator = AgentCoordinator(
        run_id="run_1",
        generation_mode=GenerationMode.FAST,
        implementation_plan={"summary": "Build a generic role workflow"},
    )

    assert coordinator.ready_to_finalize() is False
    coordinator.complete_phase("planning")
    coordinator.complete_phase("reading")
    coordinator.complete_phase("editing")
    coordinator.complete_phase("checking")
    assert coordinator.ready_to_finalize() is False
    assert coordinator.incomplete_required_todos()[0]["phase"] == "browser_verifying"
    coordinator.complete_phase("browser_verifying")

    assert coordinator.ready_to_finalize() is True


def test_scratchpad_stores_next_action_as_durable_file() -> None:
    scratchpad = AgentScratchpad(run_id="run_1")
    scratchpad.set_next_action(action="repair failed selector", reason="browser_proof_failed", payload={"selector": "#save"})

    snapshot = scratchpad.snapshot()

    assert snapshot["files"]["next_action.json"]["action"] == "repair failed selector"  # type: ignore[index]
    assert snapshot["files"]["next_action.json"]["payload"] == {"selector": "#save"}  # type: ignore[index]


def test_file_state_cache_reuses_and_invalidates_reads(tmp_path: Path) -> None:
    root = tmp_path
    target = root / "miniapp/app/main.py"
    target.parent.mkdir(parents=True)
    target.write_text("one\n", encoding="utf-8")
    cache = AgentFileStateCache()

    first = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    second = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())
    cache.invalidate("run_1", ["miniapp/app/main.py"])
    target.write_text("two\n", encoding="utf-8")
    third = cache.read(run_id="run_1", root=root, path="miniapp/app/main.py", read_text=lambda path: (root / path).read_text())

    assert first == "one\n"
    assert second == "one\n"
    assert third == "two\n"
    assert cache.snapshot("run_1")["entry_count"] == 1


def test_turn_diff_tracker_records_changed_lines(tmp_path: Path) -> None:
    class WorkspaceStub:
        def __init__(self) -> None:
            self.content = "alpha\n"

        def try_read_text_file(self, workspace_id: str, path: str, run_id: str | None = None) -> str:
            del workspace_id, path, run_id
            return self.content

    class ApplyResult:
        status = "applied"
        conflict_reason = None

    workspace = WorkspaceStub()
    tracker = AgentTurnDiffTracker()
    tracker.capture_baseline(workspace_service=workspace, workspace_id="ws_1", run_id="run_1", turn=1, paths=["miniapp/app/main.py"])  # type: ignore[arg-type]
    workspace.content = "alpha\nbeta\n"
    record = tracker.record_result(
        workspace_service=workspace,  # type: ignore[arg-type]
        workspace_id="ws_1",
        run_id="run_1",
        turn=1,
        paths=["miniapp/app/main.py"],
        apply_result=ApplyResult(),
        owner_for_path=lambda path: "backend_api",
    )

    assert record.changed_line_counts["miniapp/app/main.py"]["added"] == 1
    assert tracker.snapshot("run_1")["turn_count"] == 1


def test_semantic_scan_extracts_generic_routes_forms_and_handlers(tmp_path: Path) -> None:
    root = tmp_path
    (root / "miniapp/app/routes").mkdir(parents=True)
    (root / "miniapp/app/static/client").mkdir(parents=True)
    (root / "miniapp/app/routes/app_api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.post('/api/entities')\ndef create_entity():\n    return {}\n",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/index.html").write_text(
        "<form id='client-main-form'><input name='title'><button>Save</button></form><script src='app.js'></script>",
        encoding="utf-8",
    )
    (root / "miniapp/app/static/client/app.js").write_text(
        "document.querySelector('#client-main-form')?.addEventListener('submit', () => fetch('/api/entities'));\n",
        encoding="utf-8",
    )

    result = semantic_scan(root=root, targets=["miniapp/app"])

    assert result["python"][0]["routes"][0]["name"] == "create_entity"  # type: ignore[index]
    assert result["html"][0]["forms"][0]["id"] == "client-main-form"  # type: ignore[index]
    assert "#client-main-form" in result["javascript"][0]["selectors"]  # type: ignore[index]


def test_worker_manager_rejects_conflicting_owned_edits() -> None:
    report = AgentWorkerManager.validate_non_conflicting(
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="first"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="second"),
        ]
    )

    assert report["ok"] is False
    assert report["conflicts"][0]["path"] == "miniapp/app/static/client/app.js"  # type: ignore[index]


def test_worker_task_planner_builds_self_contained_owner_prompts() -> None:
    tasks = AgentWorkerTaskPlanner.worker_tasks(
        generation_mode=GenerationMode.QUALITY,
        implementation_plan={"principle": "plan_inspect_build_verify_repair_final_browser_proof", "primary_entities": ["entity"]},
    )

    by_id = {task["worker_id"]: task for task in tasks}

    assert "client_ui" in by_id
    assert "Own only these paths" in by_id["client_ui"]["prompt"]
    assert by_id["client_ui"]["mode_contract"]["depth"] == "deep"
    assert "mobile layout works" in by_id["client_ui"]["self_check"][3]


def test_worker_runtime_prepares_isolated_drafts_and_merge_reports(tmp_path: Path) -> None:
    source = tmp_path / "draft"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('ready');\n", encoding="utf-8")
    runtime = AgentWorkerRuntime()

    prepared = runtime.prepare(
        run_id="run_1",
        generation_mode=GenerationMode.BALANCED,
        draft_source=source,
        worker_specs=[
            {"worker_id": "client_ui", "owner_scope": "client role app"},
            {"worker_id": "manager_ui", "owner_scope": "manager role app"},
        ],
    )
    report = runtime.merge_report(
        "run_1",
        [
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one"),
            DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-b\n+c\n", reason="two"),
        ],
    )

    assert prepared["enabled"] is True
    assert len(prepared["workers"]) == 2  # type: ignore[arg-type]
    assert Path(prepared["workers"][0]["source_dir"]).exists()  # type: ignore[index]
    assert prepared["workers"][0]["agent_loop_ref"] == "worker_agent_loop:run_1:client_ui"  # type: ignore[index]
    assert report["status"] == "conflict"

    branch_results = runtime.record_branch_results(
        "run_1",
        [DraftAction(file_path="miniapp/app/static/client/app.js", operation="patch", diff="@@\n-a\n+b\n", reason="one")],
    )

    assert branch_results[0]["agent_loop"] == "branch_scoped"
    assert branch_results[0]["self_check"]["owned_paths_only"] is True  # type: ignore[index]


def test_worker_runtime_prepares_workspace_branch_run_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "miniapp/app/static/client").mkdir(parents=True)
    (source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")

    class WorkspaceStub:
        def clone_draft(self, workspace_id: str, source_run_id: str, target_run_id: str) -> Path:
            del workspace_id, source_run_id
            target = tmp_path / target_run_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            return target

    runtime = AgentWorkerRuntime()
    prepared = runtime.prepare_workspace_branches(
        workspace_id="ws",
        run_id="run_main",
        generation_mode=GenerationMode.BALANCED,
        workspace_service=WorkspaceStub(),
        worker_specs=[{"worker_id": "client_ui", "owner_scope": "client role app"}],
    )

    worker = prepared["workers"][0]  # type: ignore[index]
    assert worker["branch_run_id"] == "run_main__worker__client_ui"
    assert Path(worker["source_dir"], "miniapp/app/static/client/app.js").exists()


def test_worker_branch_loop_runs_own_tool_transcript_and_patch(tmp_path: Path) -> None:
    branch_source = tmp_path / "branch"
    (branch_source / "miniapp/app/static/client").mkdir(parents=True)
    (branch_source / "miniapp/app/static/client/app.js").write_text("console.log('base');\n", encoding="utf-8")

    class OpenAIStub:
        def generate_agent_tool_step(self, **_: object) -> dict[str, object]:
            return {
                "model": "test-model",
                "payload": {
                    "assistant_message": "client branch edited",
                    "response_id": "resp_worker_1",
                    "tool_calls": [
                        {
                            "tool": "write_file",
                            "tool_use_id": "call_1",
                            "file_path": "miniapp/app/static/client/app.js",
                            "content": "console.log('client branch');\n",
                            "reason": "create owned client behavior",
                        }
                    ],
                },
                "cache_stats": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            }

    class WorkspaceStub:
        def __init__(self) -> None:
            self.written: list[str] = []

        def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:
            del workspace_id, run_id
            return [{"path": "miniapp/app/static/client/app.js", "type": "file"}]

        def try_read_text_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str | None:
            del workspace_id, run_id
            path = branch_source / relative_path
            return path.read_text(encoding="utf-8") if path.exists() else None

        def diff(self, workspace_id: str, run_id: str | None = None) -> str:
            del workspace_id, run_id
            return "\n".join(f"diff --git a/{path} b/{path}" for path in self.written)

        def build_patch_envelope_for_file_changes(self, workspace_id: str, run_id: str, file_changes: list[DraftAction]) -> list[DraftAction]:
            del workspace_id, run_id
            return file_changes

        def apply_patch_envelope_to_draft(self, workspace_id: str, run_id: str, envelope: list[DraftAction]) -> ApplyPatchResult:
            del run_id
            for change in envelope:
                target = branch_source / change.file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(change.content or ""), encoding="utf-8")
                self.written.append(change.file_path)
            return ApplyPatchResult(workspace_id=workspace_id, run_id="branch", status="applied", changed_files=list(self.written))

    workspace = WorkspaceStub()
    result = AgentWorkerBranchLoop(openai_client=OpenAIStub(), workspace_service=workspace).run(  # type: ignore[arg-type]
        workspace_id="ws",
        parent_run_id="run_main",
        branch_run_id="run_main__worker__client_ui",
        branch_source=branch_source,
        generation_mode=GenerationMode.BALANCED,
        model_profile="",
        user_prompt="Build a role-separated mobile mini-app.",
        worker_task={"worker_id": "client_ui", "owner_scope": "client role app"},
        worker_prefix={"plan": "generic"},
        max_steps=1,
    )

    assert result.status == "changes_ready"
    assert result.transcript["counts"]["model_turn"] == 1
    assert result.transcript["counts"]["file_change"] == 1
    assert result.changed_files == ["miniapp/app/static/client/app.js"]
    assert "client branch" in (branch_source / "miniapp/app/static/client/app.js").read_text(encoding="utf-8")


def test_verification_worker_requires_real_browser_proof() -> None:
    report = VerificationWorker.verify(
        latest_execution=None,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    assert report["status"] == "failed"
    assert any(issue["kind"] == "missing_required_proof" for issue in report["issues"])  # type: ignore[index]


def test_verification_worker_rejects_incomplete_browser_proof() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(name="api_workflow_smoke", status="passed", details="ok", command="api"),
            RunCheckResult(
                name="browser_flow_smoke",
                status="passed",
                details="ok",
                command="browser",
                diagnostics={"roles_checked": ["client"], "ui_steps": []},
            ),
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    report = VerificationWorker.verify(
        latest_execution=execution,
        preview_details={},
        acceptance_contract={"required": True},
        require_browser_proof=True,
    ).model_dump()

    issue_kinds = {issue["kind"] for issue in report["issues"]}  # type: ignore[index]
    assert report["status"] == "failed"
    assert "browser_proof_missing_roles" in issue_kinds
    assert "browser_proof_missing_created_marker" in issue_kinds


def test_check_orchestrator_selects_full_create_gate() -> None:
    plan = WorkspaceAgentCheckOrchestrator.plan(
        focused_visual_edit=False,
        create_intent=True,
        acceptance_required=True,
        generation_mode=GenerationMode.FAST,
        has_draft_diff=True,
    )

    assert plan.scope_mode == "agentic"
    assert plan.check_profile == "full"
    assert plan.check_attempt == 1


def test_browser_replay_extracts_failed_step_packet() -> None:
    execution = CheckExecutionRecord(
        workspace_id="ws_1",
        run_id="run_1",
        changed_files=[],
        results=[
            RunCheckResult(
                name="browser_flow_smoke",
                status="failed",
                details="failed",
                command="browser",
                logs=["button did not update state"],
                diagnostics={
                    "failed_step": "client_submit",
                    "failed_selector": "#main-form",
                    "failed_route": "/client",
                    "console_errors": ["ReferenceError"],
                },
            )
        ],
        started_at=utc_now(),
        completed_at=utc_now(),
        duration_ms=1,
    )

    packet = BrowserProofReplay.failed_step_packet(execution)

    assert packet is not None
    assert packet["failed_step"] == "client_submit"
    assert BrowserProofReplay.should_rerun_step_first(packet) is True


def test_process_recovery_marks_running_processes_stale() -> None:
    checkpoint = {
        "process_summary": {
            "active_processes": [
                {"process_id": "proc_1", "command": "rg token miniapp/app", "status": "running"},
            ]
        }
    }

    restored = AgentProcessRecovery.restore_view(checkpoint)
    process_checkpoint = AgentProcessRecovery.checkpoint({"active_processes": [], "processes": [{"process_id": "proc_2"}]})

    assert restored["stale_processes"][0]["status"] == "stale_after_restart"  # type: ignore[index]
    assert process_checkpoint["active_count"] == 0


def test_rollout_trace_reduces_tool_events() -> None:
    trace = RolloutTraceRecorder()
    trace.append("run_1", "tool_batch", {"tool": "read_files"})
    trace.append("run_1", "tool_batch", {"tool": "run_checks"})
    snapshot = trace.snapshot("run_1")

    assert snapshot["event_count"] == 2
    assert snapshot["tool_counts"] == {"read_files": 1, "run_checks": 1}
