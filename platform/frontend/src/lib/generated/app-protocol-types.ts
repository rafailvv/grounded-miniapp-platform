import type { components } from "./openapi-types";

type Schemas = components["schemas"];

export type RpcErrorObject = {
  schema?: "grounded.rpc.error.v2";
  code: number;
  message: string;
  data?: unknown;
};

export type RpcRequestEnvelopeV2 = {
  schema?: "grounded.rpc.request.v2";
  jsonrpc?: "2.0";
  id?: number | string | null;
  method: string;
  params?: Record<string, unknown>;
  idempotency_key?: string | null;
  metadata?: Record<string, unknown>;
};

export type RpcResponseEnvelopeV2 = {
  schema?: "grounded.rpc.response.v2";
  jsonrpc?: "2.0";
  id?: number | string | null;
  result?: unknown;
  error?: RpcErrorObject | null;
  idempotency_key?: string | null;
};

export type RpcNotificationEnvelopeV2 = {
  schema?: "grounded.rpc.notification.v2";
  jsonrpc?: "2.0";
  method?: string;
  params?: Record<string, unknown>;
  sequence?: number | null;
};

export type RpcProtocolReport = Schemas["RpcProtocolReport"];
export type RpcMethodSpecV2 = Schemas["RpcMethodSpecV2"];
export type SandboxRuntimeManifest = Schemas["SandboxRuntimeManifest"];
export type SandboxPreviewLifecycle = Schemas["SandboxPreviewLifecycle"];

export type EmptyParams = Record<string, never>;
export type InitializeParams = { clientInfo?: Record<string, unknown>; client_info?: Record<string, unknown> };
export type ThreadListParams = { workspace_id?: string | null; workspaceId?: string | null; include_archived?: boolean; includeArchived?: boolean; limit?: number; cursor?: string | null };
export type ThreadStartParams = { workspace_id: string; workspaceId?: string; title?: string | null; metadata?: Record<string, unknown> };
export type ThreadIdParams = { thread_id: string; threadId?: string };
export type ThreadForkParams = ThreadIdParams & { title?: string | null };
export type TurnStartParams = Record<string, unknown> & { thread_id: string; threadId?: string; prompt?: string };
export type TurnInterruptParams = ThreadIdParams & { turn_id: string; turnId?: string };
export type RunReplayParams = { run_id: string; runId?: string; after_sequence?: number; afterSequence?: number; limit?: number };
export type RunCompareParams = { base_run_id: string; baseRunId?: string; target_run_id: string; targetRunId?: string };
export type RunBookmarkParams = { run_id: string; runId?: string; bookmark_id: string; bookmarkId?: string; prompt?: string | null };
export type SlashCommandExecuteParams = Record<string, unknown> & { command_id?: string; commandId?: string; id?: string };
export type CommandExecParams = {
  workspace_id: string;
  workspaceId?: string;
  command: string;
  thread_id?: string | null;
  threadId?: string | null;
  turn_id?: string | null;
  turnId?: string | null;
  timeout?: number;
  approval_id?: string | null;
  approvalId?: string | null;
  preset?: string;
  idempotency_key?: string | null;
  idempotencyKey?: string | null;
};
export type FsReadFileParams = { workspace_id: string; workspaceId?: string; path: string; run_id?: string | null; runId?: string | null };
export type FsWriteFileParams = FsReadFileParams & { content: string };

export type RpcParamsByMethod = {
  initialize: InitializeParams;
  "rpc/protocol": EmptyParams;
  "thread/list": ThreadListParams;
  "thread/start": ThreadStartParams;
  "thread/read": ThreadIdParams;
  "thread/resume": ThreadIdParams;
  "thread/fork": ThreadForkParams;
  "turn/start": TurnStartParams;
  "turn/interrupt": TurnInterruptParams;
  "run/replay": RunReplayParams;
  "run/compare": RunCompareParams;
  "run/resume_from_bookmark": RunBookmarkParams;
  "run/fork_from_bookmark": RunBookmarkParams;
  "skills/list": EmptyParams;
  "slash_commands/list": EmptyParams;
  "slash_commands/execute": SlashCommandExecuteParams;
  "command/exec": CommandExecParams;
  "fs/readFile": FsReadFileParams;
  "fs/writeFile": FsWriteFileParams;
  "plugin/list": EmptyParams;
};

export type RpcKnownMethod = keyof RpcParamsByMethod;
