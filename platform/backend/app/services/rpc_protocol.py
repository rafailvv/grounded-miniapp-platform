from __future__ import annotations

from typing import Any


RPC_PROTOCOL_SCHEMA = "grounded.rpc_protocol.v1"
JSON_RPC_VERSION = "2.0"


def rpc_protocol_manifest() -> dict[str, Any]:
    methods = [
        {
            "method": "initialize",
            "idempotent": True,
            "description": "Initialize typed JSON-RPC session and return server capabilities.",
            "params_schema": {"type": "object", "additionalProperties": True},
            "result_schema": "grounded.rpc_protocol.initialize_result.v1",
        },
        {
            "method": "thread/start",
            "description": "Create a resumable workbench thread.",
            "params_schema": {"type": "object", "required": ["workspace_id"], "properties": {"workspace_id": {"type": "string"}, "title": {"type": "string"}}},
            "result_schema": "ThreadRecord",
        },
        {
            "method": "thread/resume",
            "description": "Resume an existing thread from stored journal state.",
            "params_schema": {"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}}},
            "result_schema": "ThreadRecord",
        },
        {
            "method": "thread/fork",
            "description": "Fork an existing thread while preserving lineage.",
            "params_schema": {"type": "object", "required": ["thread_id"], "properties": {"thread_id": {"type": "string"}, "title": {"type": "string"}}},
            "result_schema": "ThreadRecord",
        },
        {
            "method": "turn/start",
            "description": "Start a new turn and associated generation workflow.",
            "params_schema": {"type": "object", "required": ["thread_id", "prompt"], "properties": {"thread_id": {"type": "string"}, "prompt": {"type": "string"}}},
            "result_schema": "TurnRecord",
        },
        {
            "method": "run/replay",
            "idempotent": True,
            "description": "Reconstruct a run from event journal, protocol events, bookmarks, and refs.",
            "params_schema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {"run_id": {"type": "string"}, "after_sequence": {"type": "integer"}, "limit": {"type": "integer"}},
            },
            "result_schema": "RunEventReplayReport",
        },
        {
            "method": "run/compare",
            "idempotent": True,
            "description": "Compare two run versions, including lineage, files, checks, readiness, and failures.",
            "params_schema": {"type": "object", "required": ["base_run_id", "target_run_id"], "properties": {"base_run_id": {"type": "string"}, "target_run_id": {"type": "string"}}},
            "result_schema": "RunCompareReport",
        },
        {
            "method": "run/resume_from_bookmark",
            "description": "Continue a run from a validated protocol bookmark.",
            "params_schema": {"type": "object", "required": ["run_id", "bookmark_id"], "properties": {"run_id": {"type": "string"}, "bookmark_id": {"type": "string"}, "prompt": {"type": "string"}}},
            "result_schema": "grounded.run_bookmark_action.v1",
        },
        {
            "method": "run/fork_from_bookmark",
            "description": "Fork a run from a validated protocol bookmark.",
            "params_schema": {"type": "object", "required": ["run_id", "bookmark_id"], "properties": {"run_id": {"type": "string"}, "bookmark_id": {"type": "string"}, "prompt": {"type": "string"}}},
            "result_schema": "grounded.run_bookmark_action.v1",
        },
        {
            "method": "skills/list",
            "idempotent": True,
            "description": "List active generation skill packs.",
            "params_schema": {"type": "object", "additionalProperties": False},
            "result_schema": "grounded.skills.v2",
        },
        {
            "method": "slash_commands/execute",
            "description": "Execute a product slash-command workflow.",
            "params_schema": {"type": "object", "required": ["command_id"], "properties": {"command_id": {"type": "string"}}},
            "result_schema": "grounded.slash_command_execution.v1",
        },
    ]
    return {
        "schema": RPC_PROTOCOL_SCHEMA,
        "status": "ok",
        "jsonrpc": JSON_RPC_VERSION,
        "endpoint": "/rpc",
        "capabilities": {
            "typed_json_rpc": True,
            "event_replay": True,
            "run_bookmarks": True,
            "run_resume": True,
            "run_fork": True,
            "run_compare": True,
        },
        "methods": [{**method, "transport": "websocket"} for method in methods],
    }
