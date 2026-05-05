from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_container
from app.models.domain import RunRecord
from app.services.container import ServiceContainer
from app.services.tool_protocol import tool_registry_contract

router = APIRouter(tags=["workbench"])


class CommandPolicyRequest(BaseModel):
    command: str
    preset: str = "safe_auto"
    run_id: str | None = None


class MemoryRequest(BaseModel):
    kind: str = "note"
    text: str
    citation: dict[str, Any] | None = None


class FilesRequest(BaseModel):
    files: list[str] = []


class ThreadStartRequest(BaseModel):
    workspace_id: str
    title: str | None = None
    metadata: dict[str, Any] = {}


class TurnStartRequest(BaseModel):
    prompt: str
    mode: str = "generate"
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
    metadata: dict[str, Any] = {}


@router.get("/system/policies/exec")
def get_exec_policy(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return {
        **container.exec_policy_service.snapshot(),
        "tool_registry": tool_registry_contract(),
    }


@router.get("/system/project-instructions")
def get_project_instructions(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.project_instructions()


@router.get("/system/worker-roles")
def get_worker_roles(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.worker_roles()


@router.post("/workspaces/{workspace_id}/policy/evaluate-command")
def evaluate_workspace_command(
    workspace_id: str,
    request: CommandPolicyRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        container.workspace_service.get_workspace(workspace_id)
        if request.run_id:
            return {"workspace_id": workspace_id, **container.workbench_service.evaluate_command_for_run(request.run_id, request.command, preset=request.preset)}
        return {"workspace_id": workspace_id, **container.exec_policy_service.evaluate_command(request.command, preset=request.preset)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/approvals")
def list_run_approvals(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.approvals(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/tool-events")
def get_tool_events(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.tool_events(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    after_sequence: int = 0,
    limit: int = 500,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.run_events(run_id, after_sequence=after_sequence, limit=limit)
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


@router.get("/runs/{run_id}/timeline")
def get_run_timeline(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.timeline(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-view")
def get_run_trace_view(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
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


@router.get("/runs/{run_id}/trace-bundle")
def get_run_trace_bundle(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.trace_bundle(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/trace-bundle/state")
def get_run_trace_bundle_state(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
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


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        snapshot = container.thread_service.read_thread(thread_id)
        return {
            "thread": snapshot.thread.model_dump(mode="json"),
            "turns": [turn.model_dump(mode="json") for turn in snapshot.turns],
            "items": [item.model_dump(mode="json") for item in snapshot.items],
            "events": [event.model_dump(mode="json") for event in snapshot.events],
        }
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


@router.get("/threads/{thread_id}/snapshot")
def get_thread_snapshot(thread_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        snapshot = container.thread_service.read_thread(thread_id)
        return {
            "thread": snapshot.thread.model_dump(mode="json"),
            "turns": [turn.model_dump(mode="json") for turn in snapshot.turns],
            "items": [item.model_dump(mode="json") for item in snapshot.items],
            "events": [event.model_dump(mode="json") for event in snapshot.events],
        }
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
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    try:
        return container.workbench_service.lsp_diagnostics(workspace_id, run_id=run_id)
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
def get_doctor(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.doctor()


@router.post("/doctor/run")
def run_doctor(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.doctor()


@router.get("/runs/{run_id}/observability")
def get_run_observability(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.observability(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/gate")
def get_run_gate(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.gate(run_id)
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


@router.get("/runs/{run_id}/repair-signatures")
def get_run_repair_signatures(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.repair_signatures(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/resume", response_model=RunRecord)
def resume_run(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.resume_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/system/metrics/summary")
def get_metrics_summary(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.metrics_summary()


@router.get("/workspaces/{workspace_id}/git/status")
def get_workspace_git_status(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.git_status(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/system/config/schema")
def get_config_schema(container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    return container.workbench_service.config_schema()


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


@router.post("/workspaces/{workspace_id}/memory/consolidate")
def consolidate_workspace_memory(workspace_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.consolidate_memory(workspace_id)
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


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.skill(skill_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get("/runs/{run_id}/workers")
def get_run_workers(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.workers(run_id)
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


@router.post("/runs/{run_id}/review")
def start_run_review(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.review(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{run_id}/review/fix", response_model=RunRecord)
def start_run_review_fix(run_id: str, container: ServiceContainer = Depends(get_container)) -> RunRecord:
    try:
        return container.workbench_service.start_review_fix(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}/review")
def get_run_review(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.store.get("reports", f"review:{run_id}") or container.workbench_service.review(run_id)
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


@router.get("/runs/{run_id}/visual-qa")
def get_run_visual_qa(run_id: str, container: ServiceContainer = Depends(get_container)) -> dict[str, Any]:
    try:
        return container.workbench_service.visual_qa(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
