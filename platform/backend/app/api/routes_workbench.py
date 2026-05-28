from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_container
from app.models.context_manager import ContextManagerReport
from app.models.context_pressure import ContextPressureReport
from app.models.domain import RunRecord
from app.models.event_journal import EventJournalPage, EventJournalPayload, RunJournalState, ThreadJournalState
from app.models.hooks import HookContext
from app.models.memory import MemoryRetrievalRequest, MemoryRetrievalResult, MemorySummaryReport
from app.models.observability import ObservabilityReport
from app.models.output_artifacts import CommandOutputArtifact, OutputArtifactIndex
from app.models.prompt_suggestions import PromptSuggestionsReport
from app.models.sandbox import SandboxRuntimeManifest
from app.models.threads import ThreadSnapshot
from app.models.webhooks import (
    WebhookCreateRequest,
    WebhookDeliveryReport,
    WebhookListReport,
    WebhookSubscription,
    WebhookTestRequest,
    WebhookUpdateRequest,
)
from app.models.workbench import (
    AppProtocolManifest,
    GateReport,
    GenerationModeSlaManifest,
    PromptCompletionAuditReport,
    ProtocolSchemaCatalog,
    RepairAttemptsReport,
    RepairCase,
    RepairCasesReport,
    RunBookmarksReport,
    RunCompareReport,
    RunDiffReviewReport,
    RunEventReplayReport,
    RunEventsReport,
    RunProtocolReport,
    RunProtocolReportV2,
    RunSessionCheckpointsReport,
    RpcProtocolReport,
    SessionProtocolReport,
    RunTimelineReport,
    RunTraceViewReport,
    SystemSchemaManifest,
    ToolEventsReport,
    TurnProtocolReport,
    TraceBundleReport,
    TraceState,
    VisualRegressionReport,
    system_schema_manifest,
)
from app.services.container import ServiceContainer
from app.services.app_protocol import app_protocol_manifest, app_protocol_schema_catalog
from app.services.generation_sla import GenerationSla
from app.services.run_protocol import RunProtocolConflict
from app.services.tool_protocol import tool_registry_contract
from app.modules.miniapp_agent_loop.tool_router import ToolRouter

router = APIRouter(tags=["workbench"])


class CommandPolicyRequest(BaseModel):
    command: str
    preset: str = "safe_auto"
    run_id: str | None = None


class HookEvaluateRequest(BaseModel):
    hook: str
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SkillEvaluateRequest(BaseModel):
    prompt: str = ""
    intent: str | None = None
    generation_mode: str | None = None
    paths: list[str] = Field(default_factory=list)
    failure_class: str | None = None
    max_skills: int | None = None


class MemoryRequest(BaseModel):
    kind: str = "note"
    memory_type: str | None = None
    text: str
    citation: dict[str, Any] | None = None


class FilesRequest(BaseModel):
    files: list[str] = []


class ImproveRunRequest(BaseModel):
    prompt: str
    run_id: str | None = None
    resume_from_run_id: str | None = None
    target_role_scope: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    generation_mode: str | None = None


class ThreadStartRequest(BaseModel):
    workspace_id: str
    title: str | None = None
    metadata: dict[str, Any] = {}


class TurnStartRequest(BaseModel):
    prompt: str
    mode: str = "generate"
    edit_mode: str = "default"
    generation_mode: str = "balanced"
    intent: str = "auto"
    metadata: dict[str, Any] = {}


class PermissionRuleRequest(BaseModel):
    rule_id: str | None = None
    scope: str = "workspace"
    risk: str = "unknown"
    action: str = "prompt"
    pattern: str = ""


class SlashCommandResolveRequest(BaseModel):
    prompt: str | None = None
    detail: str | None = None
    workspace_id: str | None = None
    run_id: str | None = None
    target_role_scope: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    generation_mode: str | None = None
    metadata: dict[str, Any] = {}


class SkillifyRequest(BaseModel):
    skill_id: str | None = None
    title: str | None = None
    write: bool = False
    scope: str = "user"


class BookmarkRunRequest(BaseModel):
    bookmark_id: str
    prompt: str | None = None


class BackgroundTaskCreateRequest(BaseModel):
    workspace_id: str
    type: str
    title: str | None = None
    run_id: str | None = None
    parent_task_id: str | None = None
    input: dict[str, Any] = {}
    owner: str = "agent"
    max_attempts: int = 1
    auto_start: bool = True


class BackgroundTaskUpdateRequest(BaseModel):
    title: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None


class WorkerSessionMessageRequest(BaseModel):
    kind: str = "manual"
    from_worker: str = Field(default="coordinator", alias="from")
    to_worker: str | None = Field(default=None, alias="to")
    payload: dict[str, Any] = Field(default_factory=dict)


class DraftApplyRequest(BaseModel):
    files: list[str] = Field(default_factory=list)
    apply_token: str | None = None


class DraftVariantRequest(BaseModel):
    variant_run_id: str | None = None


class GuardianGateRequest(BaseModel):
    semantic_override: str | None = None


class PrBabysitterRequest(BaseModel):
    pr: str = "auto"
    repo: str | None = None
    run_id: str | None = None
    export_id: str | None = None
    max_flaky_retries: int = 3
    retry_failed_now: bool = False
    auto_retry: bool = False
    auto_start: bool = True
    max_polls: int = 30
    poll_seconds: int = 60
    stop_when_ready: bool = False
    max_attempts: int = 1


class LspDiagnosticsAsyncRequest(BaseModel):
    run_id: str | None = None
    changed_only: bool = False
    files: list[str] = Field(default_factory=list)


@router.get("/system/policies/exec")
def get_exec_policy(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return {
        **container.exec_policy_service.snapshot(),
        "tool_registry": {**tool_registry_contract(), "router": ToolRouter.manifest()},
    }


@router.get("/system/sandbox-runtime", response_model=SandboxRuntimeManifest)
def get_sandbox_runtime(container: ServiceContainer = Depends(get_container)) -> SandboxRuntimeManifest:
    return SandboxRuntimeManifest.model_validate(container.sandbox_service.manifest())


@router.get("/system/tools/dynamic")
def get_dynamic_tool_catalog() -> dict[str, Any]:
    return ToolRouter.dynamic_tool_manifest()


@router.get("/system/policies/hooks")
def get_hook_policy_manifest(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.hook_policy_service.manifest()


@router.post("/policy/evaluate")
def evaluate_policy(
    request: CommandPolicyRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.exec_policy_service.evaluate_command(request.command, preset=request.preset)


@router.get("/system/project-instructions")
def get_project_instructions(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.project_instructions()


@router.get("/system/generation-modes", response_model=GenerationModeSlaManifest)
def get_generation_modes() -> GenerationModeSlaManifest:
    return GenerationModeSlaManifest.model_validate(GenerationSla.manifest())


@router.get("/system/rpc-protocol", response_model=RpcProtocolReport)
def get_rpc_protocol(container: ServiceContainer = Depends(get_container)) -> RpcProtocolReport:
    return container.workbench_service.rpc_protocol()


@router.get("/system/app-protocol", response_model=AppProtocolManifest)
def get_app_protocol() -> AppProtocolManifest:
    return app_protocol_manifest()


@router.get("/system/app-protocol/schemas", response_model=ProtocolSchemaCatalog, response_model_exclude_none=True)
def get_app_protocol_schemas(include_json_schema: bool = False) -> ProtocolSchemaCatalog:
    return app_protocol_schema_catalog(include_json_schema=include_json_schema)


@router.get("/system/golden-generated-apps")
def get_golden_generated_apps(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.golden_generated_apps()


@router.get("/system/golden-generated-apps/{app_id}")
def get_golden_generated_app(app_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.golden_generated_app(app_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/system/worker-roles")
def get_worker_roles(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.worker_roles()


@router.get("/system/subagents")
def get_subagent_fork_contract(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.subagent_fork_contract()


@router.post("/workspaces/{workspace_id}/policy/evaluate-command")
def evaluate_workspace_command(
    workspace_id: str,
    request: CommandPolicyRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        return {
            "workspace_id": workspace_id,
            **container.workbench_service.evaluate_command_for_workspace(
                workspace_id,
                request.command,
                preset=request.preset,
                run_id=request.run_id,
            ),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/hooks")
def get_workspace_hooks(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        return container.hook_policy_service.workspace_policy_report(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/workspaces/{workspace_id}/hooks")
def put_workspace_hooks(
    workspace_id: str,
    policy: dict[str, Any],
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        return container.hook_policy_service.update_workspace_policy(workspace_id, policy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/hooks/evaluate")
def evaluate_workspace_hooks(
    workspace_id: str,
    request: HookEvaluateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        evaluation = container.hook_policy_service.evaluate(
            HookContext(
                hook=request.hook,
                workspace_id=workspace_id,
                run_id=request.run_id,
                payload={**dict(request.payload or {}), "workspace_id": workspace_id, "run_id": request.run_id},
            )
        )
        return evaluation.model_dump(mode="json", by_alias=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/webhooks", response_model=WebhookListReport)
def list_webhooks(
    workspace_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.list_webhooks(workspace_id=workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/webhooks", response_model=WebhookSubscription)
def create_webhook(
    request: WebhookCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.create_webhook(request, idempotency_key=idempotency_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/improve", response_model=RunRecord)
def improve_workspace(workspace_id: str, request: ImproveRunRequest, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.improve_workspace(workspace_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/webhooks/{webhook_id}", response_model=WebhookSubscription)
def get_webhook(webhook_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.get_webhook(webhook_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/webhooks/{webhook_id}", response_model=WebhookSubscription)
def update_webhook(
    webhook_id: str,
    request: WebhookUpdateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.update_webhook(webhook_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.delete_webhook(webhook_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/webhooks/{webhook_id}/test", response_model=WebhookDeliveryReport)
def test_webhook(
    webhook_id: str,
    request: WebhookTestRequest | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        payload = request.payload if request is not None else {}
        event_type = request.event_type if request is not None else "webhook.test"
        return container.workbench_service.test_webhook(webhook_id, event_type=event_type, payload=payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/approvals")
def list_run_approvals(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.approvals(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/tool-events", response_model=ToolEventsReport)
def get_tool_events(run_id: str, container: ServiceContainer = Depends(get_container)) -> ToolEventsReport:
    try:
        return container.workbench_service.tool_events(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events", response_model=RunEventsReport)
def get_run_events(
    run_id: str,
    after_sequence: int = 0,
    limit: int = 500,
    container: ServiceContainer = Depends(get_container),
) -> RunEventsReport:
    try:
        return container.workbench_service.run_events(run_id, after_sequence=after_sequence, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events-v2", response_model=EventJournalPage)
def get_run_events_v2(
    run_id: str,
    after_sequence: int = 0,
    limit: int = 500,
    container: ServiceContainer = Depends(get_container),
) -> EventJournalPage:
    try:
        container.run_service.get_run(run_id)
        items = container.event_journal_service.list_run(run_id, after_sequence=after_sequence, limit=limit)
        next_sequence = max([item.sequence for item in items], default=int(after_sequence or 0))
        return EventJournalPage(scope="run", run_id=run_id, items=items, next_sequence=next_sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/journal/state", response_model=RunJournalState)
def get_run_journal_state(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunJournalState:
    try:
        container.run_service.get_run(run_id)
        return container.event_journal_service.reduce_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/protocol", response_model=RunProtocolReport)
def get_run_protocol(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunProtocolReport:
    try:
        return container.workbench_service.protocol(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/protocol-v2", response_model=RunProtocolReportV2)
def get_run_protocol_v2(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunProtocolReportV2:
    try:
        return container.session_protocol_reducer.run_protocol(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/turns/{turn_id}/protocol", response_model=TurnProtocolReport)
def get_turn_protocol(turn_id: str, container: ServiceContainer = Depends(get_container)) -> TurnProtocolReport:
    try:
        return container.session_protocol_reducer.turn_protocol(turn_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/protocol", response_model=SessionProtocolReport)
def get_session_protocol(session_id: str, container: ServiceContainer = Depends(get_container)) -> SessionProtocolReport:
    try:
        return container.session_protocol_reducer.session_protocol(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/resume", response_model=SessionProtocolReport)
def resume_session_protocol(session_id: str, container: ServiceContainer = Depends(get_container)) -> SessionProtocolReport:
    try:
        container.thread_service.resume_thread(session_id)
        return container.session_protocol_reducer.session_protocol(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/bookmarks", response_model=RunBookmarksReport)
def get_run_bookmarks(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunBookmarksReport:
    try:
        return container.workbench_service.bookmarks(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/event-replay", response_model=RunEventReplayReport)
def get_run_event_replay(
    run_id: str,
    after_sequence: int = 0,
    limit: int = 500,
    container: ServiceContainer = Depends(get_container),
) -> RunEventReplayReport:
    try:
        return container.workbench_service.event_replay(run_id, after_sequence=after_sequence, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/resume-from-bookmark")
def resume_run_from_bookmark(
    run_id: str,
    request: BookmarkRunRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.resume_from_bookmark(run_id, request.bookmark_id, prompt=request.prompt, fork=False)
    except RunProtocolConflict as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/fork-from-bookmark")
def fork_run_from_bookmark(
    run_id: str,
    request: BookmarkRunRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.resume_from_bookmark(run_id, request.bookmark_id, prompt=request.prompt, fork=True)
    except RunProtocolConflict as exc:
        raise HTTPException(status_code=409, detail=exc.payload) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compare/{target_run_id}", response_model=RunCompareReport)
def compare_runs(run_id: str, target_run_id: str, container: ServiceContainer = Depends(get_container)) -> RunCompareReport:
    try:
        return container.workbench_service.compare_runs(run_id, target_run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/checkpoints", response_model=RunSessionCheckpointsReport)
def get_run_session_checkpoints(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunSessionCheckpointsReport:
    try:
        return container.workbench_service.session_checkpoints(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compare-last-working", response_model=RunCompareReport)
def compare_run_with_last_working_product(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunCompareReport:
    try:
        return container.workbench_service.compare_current_vs_last_working_product(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/rollback-last-good")
def rollback_run_to_last_good_app(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.rollback_to_last_good_app(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/approvals/{approval_id}/approve")
def approve_tool_action(run_id: str, approval_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.approval_decision(run_id, approval_id, approved=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/approvals/{approval_id}/reject")
def reject_tool_action(run_id: str, approval_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.approval_decision(run_id, approval_id, approved=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/timeline", response_model=RunTimelineReport)
def get_run_timeline(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunTimelineReport:
    try:
        return container.workbench_service.timeline(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-view", response_model=RunTraceViewReport)
def get_run_trace_view(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunTraceViewReport:
    try:
        return container.workbench_service.trace_view(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-reducer")
def get_run_trace_reducer(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.trace_reducer(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/simplify")
def get_run_simplify(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.simplify_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/simplify")
def run_simplify(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.simplify_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/rollout-trace")
def get_run_rollout_trace(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.rollout_trace(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-bundle", response_model=TraceBundleReport)
def get_run_trace_bundle(run_id: str, container: ServiceContainer = Depends(get_container)) -> TraceBundleReport:
    try:
        return container.workbench_service.trace_bundle(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-bundle/state", response_model=TraceState)
def get_run_trace_bundle_state(run_id: str, container: ServiceContainer = Depends(get_container)) -> TraceState:
    try:
        return container.workbench_service.trace_bundle_state(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/artifacts/{artifact_ref:path}")
def get_run_artifact(run_id: str, artifact_ref: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.artifact(run_id, artifact_ref)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads")
def list_threads(
    workspace_id: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    cursor: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.thread_service.list_threads(
        workspace_id=workspace_id,
        include_archived=include_archived,
        limit=limit,
        cursor=cursor,
    )


@router.post("/threads")
def start_thread(request: ThreadStartRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.start_thread(
            workspace_id=request.workspace_id,
            title=request.title,
            metadata=request.metadata,
        ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads/{thread_id}", response_model=ThreadSnapshot)
def get_thread(thread_id: str, container: ServiceContainer = Depends(get_container)) -> ThreadSnapshot:
    try:
        snapshot = container.thread_service.read_thread(thread_id)
        return snapshot
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/events-v2", response_model=EventJournalPage)
def get_thread_events_v2(
    thread_id: str,
    after_sequence: int = 0,
    limit: int = 500,
    container: ServiceContainer = Depends(get_container),
) -> EventJournalPage:
    try:
        container.thread_service.read_thread(thread_id, include_events=False)
        items = container.event_journal_service.list_thread(thread_id, after_sequence=after_sequence, limit=limit)
        next_sequence = max([item.sequence for item in items], default=int(after_sequence or 0))
        return EventJournalPage(scope="thread", thread_id=thread_id, items=items, next_sequence=next_sequence)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/journal/state", response_model=ThreadJournalState)
def get_thread_journal_state(thread_id: str, container: ServiceContainer = Depends(get_container)) -> ThreadJournalState:
    try:
        container.thread_service.read_thread(thread_id, include_events=False)
        return container.event_journal_service.reduce_thread(thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/resume")
def resume_thread(thread_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.resume_thread(thread_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/turns")
def start_thread_turn(thread_id: str, request: TurnStartRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.start_turn(thread_id, request.model_dump()).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/snapshot", response_model=ThreadSnapshot)
def get_thread_snapshot(thread_id: str, container: ServiceContainer = Depends(get_container)) -> ThreadSnapshot:
    try:
        return container.thread_service.read_thread(thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/snapshots")
def create_thread_snapshot(thread_id: str, payload: dict[str, Any] | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.create_snapshot(thread_id, reason=str((payload or {}).get("reason") or "manual"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/snapshots")
def list_thread_snapshots(thread_id: str, limit: int = 50, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.list_snapshots(thread_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/fork")
def fork_thread(thread_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.fork_thread(thread_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/compact")
def compact_thread(thread_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.thread_service.compact_thread(thread_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/diff")
def get_run_diff(
    run_id: str,
    base: str = "source",
    target: str = "draft",
    file: str | None = None,
    worker_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.diff(run_id, base=base, target=target, file=file, worker_id=worker_id, category=category, status=status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/diff-review", response_model=RunDiffReviewReport)
def get_run_diff_review(
    run_id: str,
    base: str = "source",
    target: str = "draft",
    container: ServiceContainer = Depends(get_container),
) -> RunDiffReviewReport:
    try:
        return container.workbench_service.diff_review(run_id, base=base, target=target)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/stage/files")
def stage_run_files(run_id: str, request: FilesRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.stage_files(run_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/discard/files")
def discard_run_files(run_id: str, request: FilesRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.discard_files(run_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/apply/staged", response_model=RunRecord)
def apply_staged_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.apply_staged(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/files/search")
def search_workspace_files(
    workspace_id: str,
    q: str = "",
    run_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.file_search(workspace_id, query=q, run_id=run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/diagnostics/lsp")
def get_lsp_diagnostics(
    workspace_id: str,
    run_id: str | None = None,
    changed_only: bool = False,
    files: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        file_list = [item.strip() for item in str(files or "").split(",") if item.strip()]
        return container.workbench_service.lsp_diagnostics(workspace_id, run_id=run_id, changed_only=changed_only, files=file_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/diagnostics/lsp/async")
def start_lsp_diagnostics(
    workspace_id: str,
    request: LspDiagnosticsAsyncRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.start_lsp_diagnostics(
            workspace_id,
            run_id=request.run_id,
            changed_only=request.changed_only,
            files=request.files or None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/diagnostics/lsp/async/{task_id}")
def get_lsp_diagnostics_task(
    workspace_id: str,
    task_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.lsp_diagnostics_task(workspace_id, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/symbol-context")
def get_lsp_symbol_context(
    workspace_id: str,
    run_id: str | None = None,
    q: str = "",
    targets: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        target_list = [item.strip() for item in str(targets or "").split(",") if item.strip()]
        return container.workbench_service.lsp_symbol_context(workspace_id, run_id=run_id, query=q, targets=target_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/references")
def get_lsp_references(
    workspace_id: str,
    symbol: str,
    run_id: str | None = None,
    targets: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        target_list = [item.strip() for item in str(targets or "").split(",") if item.strip()]
        return container.workbench_service.lsp_find_references(workspace_id, run_id=run_id, symbol=symbol, targets=target_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/definition")
def get_lsp_definition(
    workspace_id: str,
    symbol: str,
    run_id: str | None = None,
    targets: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        target_list = [item.strip() for item in str(targets or "").split(",") if item.strip()]
        return container.workbench_service.lsp_definition(workspace_id, run_id=run_id, symbol=symbol, targets=target_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/route-static-context")
def get_lsp_route_static_context(
    workspace_id: str,
    run_id: str | None = None,
    targets: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        target_list = [item.strip() for item in str(targets or "").split(",") if item.strip()]
        return container.workbench_service.lsp_route_static_context(workspace_id, run_id=run_id, targets=target_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/route-graph")
def get_lsp_route_graph(
    workspace_id: str,
    run_id: str | None = None,
    targets: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        target_list = [item.strip() for item in str(targets or "").split(",") if item.strip()]
        return container.workbench_service.lsp_route_graph(workspace_id, run_id=run_id, targets=target_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/context")
def get_lsp_context(
    workspace_id: str,
    run_id: str | None = None,
    files: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        file_list = [item.strip() for item in str(files or "").split(",") if item.strip()]
        return container.workbench_service.lsp_context(workspace_id, run_id=run_id, files=file_list or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/lsp/servers")
def get_lsp_servers(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.lsp_servers(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/lsp/restart")
def restart_lsp(workspace_id: str, run_id: str | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.restart_lsp(workspace_id, run_id=run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/lsp-context")
def get_run_lsp_context(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.run_lsp_context(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/existing-app-map")
def get_existing_app_map(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.existing_app_map(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/improve-mode")
def get_improve_mode(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.improve_mode(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/patch/preflight")
def preflight_patch(workspace_id: str, payload: dict[str, Any], container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.patch_preflight(workspace_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/doctor")
def get_doctor(
    scope: str = "quick",
    workspace_id: str | None = None,
    run_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.workbench_service.doctor(scope=scope, workspace_id=workspace_id, run_id=run_id)


@router.post("/doctor/run")
def run_doctor(
    scope: str = "quick",
    workspace_id: str | None = None,
    run_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.workbench_service.doctor(scope=scope, workspace_id=workspace_id, run_id=run_id)


@router.get("/runs/{run_id}/observability")
def get_run_observability(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.observability(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/gate", response_model=GateReport)
def get_run_gate(run_id: str, container: ServiceContainer = Depends(get_container)) -> GateReport:
    try:
        return container.workbench_service.gate(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/draft-isolation")
def get_run_draft_isolation(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.draft_isolation(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/draft-gate")
def get_run_draft_gate(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.draft_gate(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/draft-gate")
def create_run_draft_gate(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.draft_gate(run_id, create=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/draft-apply")
def apply_run_draft(run_id: str, request: DraftApplyRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.draft_apply(run_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/runs/{run_id}/draft-variants")
def create_run_draft_variant(run_id: str, request: DraftVariantRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.draft_variants(run_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/guardian-gate")
def get_run_guardian_gate(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.guardian_gate(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/guardian-gate")
def create_run_guardian_gate(run_id: str, request: GuardianGateRequest | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        payload = request.model_dump() if request is not None else {}
        return container.workbench_service.guardian_gate(run_id, create=True, semantic_override=payload.get("semantic_override"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/guardian-gate/review")
def review_run_guardian_gate(run_id: str, request: GuardianGateRequest | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        payload = request.model_dump() if request is not None else {}
        return container.workbench_service.guardian_gate(run_id, create=True, semantic_override=payload.get("semantic_override"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/state")
def get_run_state(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.run_state(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/final-report")
def get_run_final_report(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.final_report(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/requirement-traceability")
def get_run_requirement_traceability(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.requirement_traceability(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/completion-audit", response_model=PromptCompletionAuditReport)
def get_run_completion_audit(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.prompt_completion_audit(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/repair-signatures")
def get_run_repair_signatures(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.repair_signatures(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/debug")
def get_run_debug(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.debug_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/stuck")
def get_run_stuck(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.stuck_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/doctor-workspace")
def get_doctor_workspace(workspace_id: str, scope: str = "quick", container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.doctor_workspace(workspace_id, scope=scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/repair-cases", response_model=RepairCasesReport)
def get_run_repair_cases(run_id: str, container: ServiceContainer = Depends(get_container)) -> RepairCasesReport:
    try:
        return container.workbench_service.repair_cases(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/repair-cases/{case_id}", response_model=RepairCase)
def get_run_repair_case(run_id: str, case_id: str, container: ServiceContainer = Depends(get_container)) -> RepairCase:
    try:
        return container.workbench_service.repair_case(run_id, case_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/repair-cases/{case_id}/attempts", response_model=RepairAttemptsReport)
def get_run_repair_case_attempts(run_id: str, case_id: str, container: ServiceContainer = Depends(get_container)) -> RepairAttemptsReport:
    try:
        return container.workbench_service.repair_case_attempts(run_id, case_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/repair-cases/{case_id}/retry")
def retry_run_repair_case(run_id: str, case_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.retry_repair_case(run_id, case_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/resume", response_model=RunRecord)
def resume_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.resume_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/system/metrics/summary", response_model=ObservabilityReport)
def get_metrics_summary(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.metrics_summary()


@router.get("/system/observability", response_model=ObservabilityReport)
def get_system_observability(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.observability_summary()


@router.get("/workspaces/{workspace_id}/observability", response_model=ObservabilityReport)
def get_workspace_observability(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.observability_summary(workspace_id=workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/git/status")
def get_workspace_git_status(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.git_status(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/system/config/schema")
def get_config_schema(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.config_schema()


@router.get("/system/schema", response_model=SystemSchemaManifest, response_model_exclude_none=True)
def get_system_schema() -> SystemSchemaManifest:
    return system_schema_manifest()


@router.get("/event-payloads/{payload_ref:path}", response_model=EventJournalPayload)
def get_event_payload(payload_ref: str, container: ServiceContainer = Depends(get_container)) -> EventJournalPayload:
    if container.platform_db.find_event_by_payload_ref(payload_ref) is None:
        raise HTTPException(status_code=404, detail="Event payload not found.")
    payload = container.event_journal_service.read_payload(payload_ref)
    if payload is None:
        raise HTTPException(status_code=404, detail="Event payload not found.")
    return payload


@router.get("/system/migrations")
def get_migrations(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.migrations()


@router.get("/system/security/summary")
def get_security_summary(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.security_summary()


@router.get("/system/exec/sessions")
def get_exec_sessions(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.exec_runtime_service.snapshot()


@router.get("/system/permissions/rules")
def get_permission_rules(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.permission_rules()


@router.post("/system/permissions/rules")
def upsert_permission_rule(request: PermissionRuleRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.upsert_permission_rule(request.model_dump())


@router.get("/system/permissions/recent-denials")
def get_recent_denials(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.recent_denials()


@router.get("/system/permissions/command-audit")
def get_command_audit(limit: int = 100, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.command_audit(limit=limit)


@router.get("/workspaces/{workspace_id}/permissions/command-audit")
def get_workspace_command_audit(workspace_id: str, limit: int = 100, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.command_audit(workspace_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/permissions/approval-grants")
def get_workspace_permission_grants(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.workspace_approval_grants(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/memory")
def get_workspace_memory(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.memory(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/memory/pipeline")
def get_workspace_memory_pipeline(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.memory_pipeline(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/memory/summary", response_model=MemorySummaryReport)
def get_workspace_memory_summary(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.memory_summary(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/session-memory")
def get_workspace_session_memory(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.session_memory(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/memory/consolidate")
def consolidate_workspace_memory(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.consolidate_memory(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/memory/retrieve", response_model=MemoryRetrievalResult)
def retrieve_workspace_memory(
    workspace_id: str,
    request: MemoryRetrievalRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.retrieve_memory(workspace_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/memory/extract")
def extract_run_memory(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.extract_run_memory(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/memory")
def upsert_workspace_memory(workspace_id: str, request: MemoryRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.upsert_memory(workspace_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/skills")
def list_skills(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.skills()


@router.get("/system/skills/manifest")
def get_skill_registry_manifest(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.skill_registry_manifest()


@router.post("/skills/evaluate")
def evaluate_skills(request: SkillEvaluateRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.evaluate_skills(request.model_dump())


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.skill(skill_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/skillify")
def skillify_run(run_id: str, request: SkillifyRequest, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.skillify_run(run_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/slash-commands")
def list_slash_commands(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.slash_commands()


@router.post("/slash-commands/{command_id}/resolve")
def resolve_slash_command(
    command_id: str,
    request: SlashCommandResolveRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.resolve_slash_command(command_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/slash-commands/{command_id}/execute")
def execute_slash_command(
    command_id: str,
    request: SlashCommandResolveRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.execute_slash_command(command_id, request.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/magic-docs/product-architecture")
def get_workspace_magic_doc(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.magic_doc(workspace_id, write=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/magic-docs/product-architecture")
def update_workspace_magic_doc(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.magic_doc(workspace_id, write=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/compact")
def compact_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.compact_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compaction")
def get_run_compaction(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.compaction(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/context-pressure", response_model=ContextPressureReport)
def get_run_context_pressure(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.context_pressure(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/context-manager", response_model=ContextManagerReport)
def get_run_context_manager(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.context_manager(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/context-manager")
def get_session_context_manager(session_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.session_context_manager(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/context-manager/compact")
def compact_run_context_manager(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.compact_context_manager(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compaction/boundaries")
def get_run_compaction_boundaries(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.compaction_boundaries(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/compaction/post-message/{boundary_id}")
def get_run_post_compact_message(run_id: str, boundary_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.post_compact_message(run_id, boundary_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/microcompact/{digest}")
def get_run_microcompact(run_id: str, digest: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.microcompact(run_id, digest)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/output-artifacts", response_model=OutputArtifactIndex)
def get_run_output_artifacts(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.output_artifacts(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/output-artifacts/{artifact_id}", response_model=CommandOutputArtifact)
def get_run_output_artifact(run_id: str, artifact_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.output_artifact(run_id, artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plugins")
def list_plugins(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.plugins()


@router.post("/plugins/install-local")
def install_local_plugin(payload: dict[str, Any], container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.install_plugin_local(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mcp/servers")
def list_mcp_servers(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.mcp_servers()


@router.get("/mcp/tools")
def list_mcp_tools(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.mcp_tools()


@router.post("/mcp/tools/{tool_id}/call")
def call_mcp_tool(tool_id: str, payload: dict[str, Any], container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.call_mcp_tool(tool_id, payload)


@router.post("/tasks")
def create_background_task(
    request: BackgroundTaskCreateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.create_background_task(request.model_dump(mode="python"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks")
def list_background_tasks(
    workspace_id: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    return container.workbench_service.list_background_tasks(workspace_id=workspace_id, run_id=run_id, status=status)


@router.get("/tasks/{task_id}")
def get_background_task(task_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.get_background_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/tasks/{task_id}")
def update_background_task(
    task_id: str,
    request: BackgroundTaskUpdateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.update_background_task(task_id, request.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/stop")
def stop_background_task(task_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.stop_background_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/retry")
def retry_background_task(task_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.retry_background_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/requeue")
def requeue_background_task(task_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.requeue_background_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/output")
def get_background_task_output(
    task_id: str,
    cursor: int = 0,
    limit: int = 100,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.background_task_output(task_id, cursor=cursor, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/pr-babysitter/snapshot")
def pr_babysitter_snapshot(
    workspace_id: str,
    request: PrBabysitterRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.pr_babysitter_snapshot(workspace_id, request.model_dump(mode="python"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/pr-babysitter/watch")
def pr_babysitter_watch(
    workspace_id: str,
    request: PrBabysitterRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.pr_babysitter_watch(workspace_id, request.model_dump(mode="python"))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/pr-babysitter")
def pr_babysitter_reports(
    workspace_id: str,
    run_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.pr_babysitter_reports(workspace_id, run_id=run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers")
def get_run_workers(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.workers(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/orchestration")
def get_run_worker_orchestration(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_orchestration(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/worker-sessions")
def get_run_worker_sessions(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_sessions(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/worker-sessions/{worker_session_id}")
def get_run_worker_session(run_id: str, worker_session_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_session(run_id, worker_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/worker-mailbox")
def get_run_worker_mailbox(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_mailbox(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/worker-sessions/{worker_session_id}/resume")
def resume_run_worker_session(run_id: str, worker_session_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.resume_worker_session(run_id, worker_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/worker-sessions/{worker_session_id}/message")
def message_run_worker_session(
    run_id: str,
    worker_session_id: str,
    request: WorkerSessionMessageRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.message_worker_session(run_id, worker_session_id, request.model_dump(mode="python", by_alias=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/tasks")
def get_run_tasks(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.tasks(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/{worker_id}/diff")
def get_worker_diff(run_id: str, worker_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_diff(run_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/{worker_id}/artifacts")
def get_worker_artifacts(run_id: str, worker_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_artifacts(run_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/{worker_id}/context")
def get_worker_context(run_id: str, worker_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_context(run_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/{worker_id}/memory")
def get_worker_memory(run_id: str, worker_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_memory(run_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/{worker_id}/output")
def get_worker_output(run_id: str, worker_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_output(run_id, worker_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/workers/merge-decision")
def get_worker_merge_decision(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.worker_merge_decision(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/review")
def start_run_review(run_id: str, target: str | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.review(run_id, target=target)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/review/fix", response_model=RunRecord)
def start_run_review_fix(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.start_review_fix(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/review")
def get_run_review(run_id: str, target: str | None = None, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        if target:
            return container.workbench_service.review(run_id, target=target)
        return container.store.get("reports", f"review:{run_id}") or container.workbench_service.review(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/prompt-suggestions", response_model=PromptSuggestionsReport)
def get_run_prompt_suggestions(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.prompt_suggestions(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/test-matrix")
def get_run_test_matrix(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.test_matrix(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/acceptance-scenarios")
def get_run_acceptance_scenarios(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.acceptance_scenarios(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/prompt-contract")
def get_run_prompt_contract(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.prompt_contract(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/prompt-contract/compile")
def compile_run_prompt_contract(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.compile_prompt_contract(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/prompt-contracts")
def list_workspace_prompt_contracts(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.workspace_prompt_contracts(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/miniapp-contract")
def get_run_miniapp_contract(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.miniapp_contract(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/browser-proof")
def start_browser_proof(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_proof(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/browser-proof")
def get_browser_proof(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_proof(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/browser-replay")
def get_browser_replay(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_replay(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/browser-replay-proof")
def get_browser_replay_proof(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_replay_proof(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/browser-replay-proof/{scenario_id}")
def get_browser_replay_scenario(run_id: str, scenario_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_replay_scenario(run_id, scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/browser-replay-proof/build")
def build_browser_replay_proof(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.browser_replay_proof(run_id, build=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/visual-qa")
def get_run_visual_qa(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.visual_qa(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/visual-regression", response_model=VisualRegressionReport)
def get_run_visual_regression(run_id: str, container: ServiceContainer = Depends(get_container)) -> VisualRegressionReport:
    try:
        return container.workbench_service.visual_regression(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
