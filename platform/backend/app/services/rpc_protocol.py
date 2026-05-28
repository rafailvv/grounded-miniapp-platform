from __future__ import annotations

from typing import Any

from app.models.protocol import (
    CommandExecParams,
    CompatibilityRule,
    EmptyParams,
    ExperimentalFieldSpec,
    FsReadFileParams,
    FsWriteFileParams,
    InitializeParams,
    RpcCursorPage,
    RpcIdempotency,
    RpcMethodSpecV2,
    RpcProtocolReport,
    RunBookmarkParams,
    RunCompareParams,
    RunReplayParams,
    SessionIdParams,
    SlashCommandExecuteParams,
    ThreadForkParams,
    ThreadIdParams,
    ThreadListParams,
    ThreadStartParams,
    TurnIdParams,
    TurnInterruptParams,
    TurnStartParams,
    WorkerSessionMessageParams,
    WorkerSessionParams,
)

RPC_PROTOCOL_SCHEMA = "grounded.rpc_protocol.v2"
JSON_RPC_VERSION = "2.0"

RPC_PARAM_MODELS: dict[str, type] = {
    "initialize": InitializeParams,
    "rpc/protocol": EmptyParams,
    "thread/list": ThreadListParams,
    "thread/start": ThreadStartParams,
    "thread/read": ThreadIdParams,
    "session/protocol": SessionIdParams,
    "session/context_manager": SessionIdParams,
    "thread/resume": ThreadIdParams,
    "session/resume": SessionIdParams,
    "thread/fork": ThreadForkParams,
    "turn/start": TurnStartParams,
    "turn/protocol": TurnIdParams,
    "turn/interrupt": TurnInterruptParams,
    "run/protocol": RunReplayParams,
    "run/context_manager": RunReplayParams,
    "worker/sessions": RunReplayParams,
    "worker/mailbox": RunReplayParams,
    "worker/session": WorkerSessionParams,
    "worker/resume": WorkerSessionParams,
    "worker/message": WorkerSessionMessageParams,
    "run/replay": RunReplayParams,
    "run/compare": RunCompareParams,
    "run/resume_from_bookmark": RunBookmarkParams,
    "run/fork_from_bookmark": RunBookmarkParams,
    "skills/list": EmptyParams,
    "slash_commands/list": EmptyParams,
    "slash_commands/execute": SlashCommandExecuteParams,
    "command/exec": CommandExecParams,
    "fs/readFile": FsReadFileParams,
    "fs/writeFile": FsWriteFileParams,
    "plugin/list": EmptyParams,
}


def rpc_protocol_report() -> RpcProtocolReport:
    return RpcProtocolReport(
        capabilities={
            "typed_json_rpc": True,
            "event_replay": True,
            "run_bookmarks": True,
            "run_resume": True,
            "run_fork": True,
            "run_compare": True,
            "params_validation": True,
            "idempotency_keys": True,
            "cursor_pagination": True,
            "experimental_markers": True,
        },
        compatibility_rules=[
            CompatibilityRule(
                rule_id="rpc.v2.additive",
                description="RPC v2 may add fields and methods without removing or renaming existing v1-compatible fields.",
            ),
            CompatibilityRule(
                rule_id="rpc.params.aliases",
                description="Existing snake_case and camelCase request params remain accepted for compatibility.",
            ),
            CompatibilityRule(
                rule_id="rpc.breaking_changes",
                description="Breaking envelope or method parameter changes require a future v3 protocol version.",
            ),
        ],
        methods=[
            _method("initialize", InitializeParams, "InitializeResult", idempotent=True, description="Initialize typed JSON-RPC session and return server capabilities."),
            _method("rpc/protocol", EmptyParams, "RpcProtocolReport", idempotent=True, description="Return the typed RPC protocol manifest."),
            _method(
                "thread/list",
                ThreadListParams,
                "ThreadListResult",
                idempotent=True,
                description="List resumable workbench threads.",
                cursor=RpcCursorPage(cursor_kind="opaque_cursor", cursor_param="cursor", next_cursor_field="next_cursor"),
            ),
            _method(
                "thread/start",
                ThreadStartParams,
                "ThreadRecord",
                description="Create a resumable workbench thread.",
                idempotency=RpcIdempotency(mode="recommended", scope="workspace"),
            ),
            _method("thread/read", ThreadIdParams, "ThreadSnapshot", idempotent=True, description="Read a thread snapshot."),
            _method("session/protocol", SessionIdParams, "SessionProtocolReport", idempotent=True, description="Read canonical session protocol state."),
            _method("session/context_manager", SessionIdParams, "ContextManagerReport", idempotent=True, description="Read session context budget reports."),
            _method("thread/resume", ThreadIdParams, "ThreadRecord", description="Resume an existing thread from stored journal state."),
            _method("session/resume", SessionIdParams, "SessionProtocolReport", description="Resume a session and return canonical protocol state."),
            _method("thread/fork", ThreadForkParams, "ThreadRecord", description="Fork an existing thread while preserving lineage."),
            _method(
                "turn/start",
                TurnStartParams,
                "TurnRecord",
                description="Start a new turn and associated generation workflow.",
                idempotency=RpcIdempotency(mode="recommended", scope="thread"),
            ),
            _method("turn/protocol", TurnIdParams, "TurnProtocolReport", idempotent=True, description="Read canonical turn protocol state."),
            _method("turn/interrupt", TurnInterruptParams, "TurnRecord", description="Interrupt a running turn."),
            _method("run/protocol", RunReplayParams, "RunProtocolReportV2", idempotent=True, description="Read canonical run protocol state."),
            _method("run/context_manager", RunReplayParams, "ContextManagerReport", idempotent=True, description="Read run context budget manifest and decisions."),
            _method("worker/sessions", RunReplayParams, "WorkerSessionsReport", idempotent=True, description="Read durable worker sessions for a run."),
            _method("worker/mailbox", RunReplayParams, "WorkerMailboxReport", idempotent=True, description="Read durable worker mailbox for a run."),
            _method("worker/session", WorkerSessionParams, "WorkerSessionRecord", idempotent=True, description="Read one durable worker session."),
            _method("worker/resume", WorkerSessionParams, "WorkerSessionResume", description="Build a resume decision packet for a worker session."),
            _method("worker/message", WorkerSessionMessageParams, "WorkerMailboxReport", description="Append a durable message to a worker mailbox."),
            _method(
                "run/replay",
                RunReplayParams,
                "RunEventReplayReport",
                idempotent=True,
                description="Reconstruct a run from event journal, protocol events, bookmarks, and refs.",
                cursor=RpcCursorPage(cursor_kind="sequence_cursor", cursor_param="after_sequence", next_cursor_field="replay_cursor"),
            ),
            _method("run/compare", RunCompareParams, "RunCompareReport", idempotent=True, description="Compare two run versions."),
            _method("run/resume_from_bookmark", RunBookmarkParams, "grounded.run_bookmark_action.v1", description="Continue a run from a validated protocol bookmark."),
            _method("run/fork_from_bookmark", RunBookmarkParams, "grounded.run_bookmark_action.v1", description="Fork a run from a validated protocol bookmark."),
            _method("skills/list", EmptyParams, "grounded.skills.v2", idempotent=True, description="List active generation skill packs."),
            _method("slash_commands/list", EmptyParams, "grounded.slash_commands.v1", idempotent=True, description="List available product slash commands."),
            _method("slash_commands/execute", SlashCommandExecuteParams, "grounded.slash_command_execution.v1", description="Execute a product slash-command workflow."),
            _method(
                "command/exec",
                CommandExecParams,
                "grounded.command_exec_result.v1",
                description="Execute a policy-checked workspace command.",
                idempotency=RpcIdempotency(mode="recommended", scope="thread"),
                experimental=[ExperimentalFieldSpec(name="preset", reason="Command policy presets are still evolving.", since="v2")],
            ),
            _method("fs/readFile", FsReadFileParams, "grounded.fs_read_file_result.v1", idempotent=True, description="Read a workspace file."),
            _method(
                "fs/writeFile",
                FsWriteFileParams,
                "grounded.fs_write_file_result.v1",
                description="Write a workspace file through the RPC filesystem bridge.",
                idempotency=RpcIdempotency(mode="recommended", scope="workspace"),
                experimental=[ExperimentalFieldSpec(name="content", reason="Direct RPC filesystem writes require policy hardening before stable external use.", since="v2")],
            ),
            _method("plugin/list", EmptyParams, "grounded.plugins.v1", idempotent=True, description="List configured generation plugins."),
        ],
    )


def rpc_protocol_manifest() -> dict[str, Any]:
    return rpc_protocol_report().model_dump(mode="json", by_alias=True)


def _method(
    method: str,
    params_model: type,
    result_model: str,
    *,
    idempotent: bool = False,
    description: str = "",
    cursor: RpcCursorPage | None = None,
    idempotency: RpcIdempotency | None = None,
    experimental: list[ExperimentalFieldSpec] | None = None,
) -> RpcMethodSpecV2:
    schema = params_model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}") if hasattr(params_model, "model_json_schema") else {}
    return RpcMethodSpecV2(
        method=method,
        stability="experimental" if experimental else "stable",
        idempotent=idempotent,
        description=description,
        params_model=params_model.__name__,
        result_model=result_model,
        params_schema=schema,
        result_schema=result_model,
        cursor=cursor or RpcCursorPage(),
        idempotency=idempotency or RpcIdempotency(mode="none" if idempotent else "optional"),
        experimental=experimental or [],
    )
