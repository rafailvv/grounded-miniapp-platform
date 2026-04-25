from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

from app.models.common import GenerationMode, PreviewProfile, TargetPlatform
from app.models.domain import ErrorContext, GenerateRequest, JobRecord
from app.modules.miniapp_fix_runtime.fix_entry import FixEntryRuntime
from app.modules.miniapp_generation_runtime.generation_entry import MiniappGenerationEntry
from app.modules.miniapp_visual_patch_fast_lane import VISUAL_PATCH_MODEL, MiniappVisualPatchFastLane


def test_simple_visual_classifier_accepts_safe_role_patch() -> None:
    assert MiniappVisualPatchFastLane.should_attempt(
        prompt="Change the client button color to green and increase card padding.",
        intent="edit",
        run_mode="generate",
        role_scope=["client"],
    )


def test_simple_visual_classifier_rejects_flow_or_backend_patch() -> None:
    assert not MiniappVisualPatchFastLane.should_attempt(
        prompt="When I click a request, open a separate details page and save the approval status.",
        intent="edit",
        run_mode="generate",
        role_scope=["client"],
    )
    assert not MiniappVisualPatchFastLane.should_attempt(
        prompt="Fix API error: loading data fails with schema failed traceback.",
        intent="edit",
        run_mode="fix",
        role_scope=["manager"],
    )


def test_simple_visual_classifier_all_role_scope_allows_explicit_single_role_visual_patch() -> None:
    assert MiniappVisualPatchFastLane.should_attempt(
        prompt=(
            "When I open the request details page from the manager view, the details screen appears in a dark theme. "
            "Please fix only this manager request details page so it uses the same light visual style. "
            "Keep the existing data, status update logic, back navigation, and page structure working exactly the same."
        ),
        intent="edit",
        run_mode="fix",
        role_scope=["client", "specialist", "manager"],
    )


def test_simple_visual_classifier_accepts_empty_indicator_cleanup() -> None:
    assert MiniappVisualPatchFastLane.should_attempt(
        prompt=(
            "On the manager overview page, remove the two small colored pill placeholders directly below the All, Open, "
            "Assigned, and Completed filter row. Nothing should be visible there when there is no active message."
        ),
        intent="edit",
        run_mode="generate",
        role_scope=["manager"],
    )


def test_visual_patch_model_respects_chip(monkeypatch) -> None:  # noqa: ANN001
    import app.ai.model_registry as model_registry

    original_chip = os.environ.get("CHIP")
    try:
        monkeypatch.setenv("CHIP", "false")
        reloaded = importlib.reload(model_registry)
        assert reloaded.VISUAL_PATCH_MODEL == reloaded.FAST_CODE_MODEL

        monkeypatch.setenv("CHIP", "true")
        reloaded = importlib.reload(model_registry)
        assert reloaded.VISUAL_PATCH_MODEL == reloaded.FAST_CODE_MODEL
    finally:
        if original_chip is None:
            monkeypatch.delenv("CHIP", raising=False)
        else:
            monkeypatch.setenv("CHIP", original_chip)
        importlib.reload(model_registry)


class _FakeStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def upsert(self, collection: str, key: str, payload: dict) -> None:
        self.items[(collection, key)] = payload


class _FakeApplyResult:
    status = "applied"
    conflict_reason = None

    def model_dump(self, *, mode: str = "json") -> dict[str, str]:  # noqa: ARG002
        return {"status": "applied"}


class _FakeWorkspaceService:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.files = {
            "miniapp/app/static/shared/base.css": ":root { color-scheme: light; }",
            "miniapp/app/static/client/index.html": "<main id=\"app\"><button id=\"request-button\">Request</button></main>",
            "miniapp/app/static/client/styles.css": ".request-button { background: blue; padding: 8px; }",
            "miniapp/app/static/client/app.js": "document.getElementById('request-button');",
            "miniapp/app/static/manager/index.html": "<main id=\"manager\"><a href=\"/manager/request/1\">Open detail</a></main>",
            "miniapp/app/static/manager/styles.css": ".manager { background: white; }",
            "miniapp/app/static/manager/app.js": "document.getElementById('manager');",
            "miniapp/app/static/manager/request_detail/index.html": "<main class=\"dark-detail\"><button id=\"back\">Back</button></main>",
            "miniapp/app/static/manager/request_detail/styles.css": ".dark-detail { background: #050912; color: #f8fafc; }",
            "miniapp/app/static/manager/request_detail/app.js": "document.getElementById('back');",
            "miniapp/app/main.py": "print('backend')",
        }

    def prepare_draft(self, workspace_id: str, run_id: str) -> Path:  # noqa: ARG002
        draft = self.tmp_path / run_id / "source"
        draft.mkdir(parents=True, exist_ok=True)
        return draft

    def file_tree(self, workspace_id: str, run_id: str | None = None) -> list[dict[str, str]]:  # noqa: ARG002
        return [{"path": path, "type": "file"} for path in sorted(self.files)]

    def try_read_text_file(self, workspace_id: str, relative_path: str, run_id: str | None = None) -> str | None:  # noqa: ARG002
        return self.files.get(relative_path)

    def build_patch_envelope_for_draft(self, workspace_id: str, run_id: str, operations: list) -> list:  # noqa: ARG002
        return operations

    def apply_patch_envelope_to_draft(self, workspace_id: str, run_id: str, envelope: list) -> _FakeApplyResult:  # noqa: ARG002
        for operation in envelope:
            self.files[operation.file_path] = operation.content or ""
        return _FakeApplyResult()

    def diff(self, workspace_id: str, run_id: str | None = None) -> str:  # noqa: ARG002
        return "diff --git a/miniapp/app/static/client/styles.css b/miniapp/app/static/client/styles.css"


class _FakeOpenRouterClient:
    enabled = True

    def __init__(self, operation_path: str, *, should_raise: bool = False) -> None:
        self.operation_path = operation_path
        self.should_raise = should_raise
        self.model_override: str | None = None

    def generate_structured(self, **kwargs):  # noqa: ANN001
        self.model_override = kwargs.get("model_override")
        if self.should_raise:
            raise RuntimeError("provider temporarily unavailable")
        return {
            "model": self.model_override,
            "payload": {
                "summary": "Updated the client visual style quickly.",
                "operations": [
                    {
                        "file_path": self.operation_path,
                        "operation": "replace",
                        "content": ".request-button { background: green; padding: 12px; }",
                        "reason": "Adjust button color and spacing.",
                    }
                ],
            },
        }


class _FakeService:
    def __init__(self, tmp_path: Path, operation_path: str, *, should_raise: bool = False) -> None:
        self.workspace_service = _FakeWorkspaceService(tmp_path)
        self.openrouter_client = _FakeOpenRouterClient(operation_path, should_raise=should_raise)
        self.store = _FakeStore()
        self.traces: list[dict] = []

    def _append_event(self, job: JobRecord, event_type: str, message: str, details: dict | None = None) -> None:
        from app.models.domain import JobEvent

        job.events.append(JobEvent(event_type=event_type, message=message, details=details or {}))

    def _append_trace(self, workspace_id: str, stage: str, message: str, payload: dict | None = None) -> None:
        self.traces.append({"workspace_id": workspace_id, "stage": stage, "message": message, "payload": payload or {}})

    def _store_report(self, key: str, payload: dict) -> None:
        self.store.upsert("reports", key, payload)


def _job() -> JobRecord:
    return JobRecord(
        workspace_id="ws_test",
        prompt="Change the client button color to green.",
        status="running",
        mode="generate",
        generation_mode=GenerationMode.BALANCED,
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        fidelity="balanced_app",
        linked_run_id="run_test",
    )


def test_visual_fast_lane_applies_allowed_static_patch(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/static/client/styles.css")
    request = GenerateRequest(
        prompt="Change the client button color to green and increase card padding.",
        intent="edit",
        target_role_scope=["client"],
    )

    result = MiniappVisualPatchFastLane(service).try_run(
        workspace_id="ws_test",
        run_id="run_test",
        request=request,
        job=_job(),
        role_scope=["client"],
        started_at=0.0,
        draft_source=service.workspace_service.prepare_draft("ws_test", "run_test"),
        run_mode="generate",
    )

    assert result is not None
    assert result.status == "completed"
    assert result.outcome_kind == "applied"
    assert result.fix_targets == ["miniapp/app/static/client/styles.css"]
    assert service.openrouter_client.model_override == VISUAL_PATCH_MODEL
    assert service.workspace_service.files["miniapp/app/static/client/styles.css"].startswith(".request-button { background: green")


def test_visual_fast_lane_uses_visual_error_context_when_prompt_is_generic(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/static/manager/request_detail/styles.css")
    request = GenerateRequest(
        prompt="Analyze the reported failure and apply the smallest safe fix.",
        mode="fix",
        intent="edit",
        target_role_scope=["client", "specialist", "manager"],
        error_context=ErrorContext(
            source="build",
            raw_error=(
                "When I open the request details page from the manager view, the details screen appears in a dark theme. "
                "Please fix only this manager request details page so it uses the same light visual style. "
                "Keep the existing data, buttons, status update logic, back navigation, and page structure working exactly the same; "
                "this is only a visual HTML/CSS fix for the details screen."
            ),
        ),
    )

    result = MiniappVisualPatchFastLane(service).try_run(
        workspace_id="ws_test",
        run_id="run_test",
        request=request,
        job=_job().model_copy(update={"mode": "fix"}),
        role_scope=["client", "specialist", "manager"],
        started_at=0.0,
        draft_source=service.workspace_service.prepare_draft("ws_test", "run_test"),
        run_mode="fix",
    )

    assert result is not None
    assert result.status == "completed"
    assert result.fix_targets == ["miniapp/app/static/manager/request_detail/styles.css"]
    assert service.workspace_service.files["miniapp/app/static/client/styles.css"] == ".request-button { background: blue; padding: 8px; }"


def test_visual_fast_lane_rejects_forbidden_file_and_falls_back(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/main.py")
    request = GenerateRequest(
        prompt="Change the client button color to green.",
        intent="edit",
        target_role_scope=["client"],
    )

    result = MiniappVisualPatchFastLane(service).try_run(
        workspace_id="ws_test",
        run_id="run_test",
        request=request,
        job=_job(),
        role_scope=["client"],
        started_at=0.0,
        draft_source=service.workspace_service.prepare_draft("ws_test", "run_test"),
        run_mode="generate",
    )

    assert result is None
    assert service.workspace_service.files["miniapp/app/main.py"] == "print('backend')"
    assert any(trace["stage"] == "fast_visual_patch_defer_to_main_pipeline" for trace in service.traces)


def test_visual_fast_lane_model_error_falls_back(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/static/client/styles.css", should_raise=True)
    request = GenerateRequest(
        prompt="Change the client button color to green.",
        intent="edit",
        target_role_scope=["client"],
    )

    result = MiniappVisualPatchFastLane(service).try_run(
        workspace_id="ws_test",
        run_id="run_test",
        request=request,
        job=_job(),
        role_scope=["client"],
        started_at=0.0,
        draft_source=service.workspace_service.prepare_draft("ws_test", "run_test"),
        run_mode="generate",
    )

    assert result is None
    assert any(trace["payload"]["exception_type"] == "RuntimeError" for trace in service.traces)


def test_generation_entry_uses_fast_lane_before_grounded_spec(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/static/client/styles.css")
    request = GenerateRequest(
        prompt="Change the client button color to green.",
        intent="edit",
        target_role_scope=["client"],
    )

    result = MiniappGenerationEntry(service).generate_with_agent_loop(
        workspace=SimpleNamespace(current_revision_id="rev_test"),
        workspace_id="ws_test",
        job=_job(),
        request=request,
        draft_run_id="run_test",
        effective_prompt=request.prompt,
        target_platform=TargetPlatform.TELEGRAM,
        preview_profile=PreviewProfile.TELEGRAM_MOCK,
        generation_mode=GenerationMode.BALANCED,
        role_scope=["client"],
        doc_refs=[],
        retrieval_ms=0,
        started_at=0.0,
        creative_direction={},
        should_stop=None,
        prompt_turn_id="turn_test",
    )

    assert result.status == "completed"
    assert any(event.event_type == "fast_visual_patch" for event in result.events)


def test_fix_entry_uses_fast_lane_before_repair_loop(tmp_path: Path) -> None:
    service = _FakeService(tmp_path, "miniapp/app/static/client/styles.css")
    request = GenerateRequest(
        prompt="Fix the client button color and card padding.",
        mode="fix",
        intent="edit",
        target_role_scope=["client"],
    )
    job = _job().model_copy(update={"mode": "fix"})

    result = FixEntryRuntime(service).generate_with_workspace_loop(
        workspace_id="ws_test",
        run_id="run_test",
        request=request,
        job=job,
        draft_source=service.workspace_service.prepare_draft("ws_test", "run_test"),
        started_at=0.0,
        role_scope=["client"],
        effective_mode=GenerationMode.BALANCED,
        memory_context=None,
        should_stop=None,
    )

    assert result.status == "completed"
    assert result.current_fix_phase == "completed"
    assert any(event.event_type == "fast_visual_patch" for event in result.events)
