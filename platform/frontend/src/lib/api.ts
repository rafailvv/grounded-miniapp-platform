export type Workspace = {
  workspace_id: string;
  name: string;
  description?: string | null;
  template_cloned: boolean;
  current_revision_id?: string | null;
  revisions?: Array<{
    revision_id: string;
    commit_sha: string;
    message: string;
    source: string;
    created_at: string;
  }>;
};

export type Run = {
  run_id: string;
  workspace_id: string;
  prompt: string;
  mode?: "generate" | "fix";
  generation_mode?: "fast" | "balanced" | "quality" | "basic";
  intent: "create" | "edit" | "refine" | "role_only_change";
  apply_strategy: "staged_auto_apply" | "manual_approve";
  target_role_scope: Array<"client" | "specialist" | "manager">;
  model_profile: string;
  llm_provider?: string | null;
  llm_model?: string | null;
  linked_job_id?: string | null;
  resume_from_run_id?: string | null;
  source_revision_id?: string | null;
  result_revision_id?: string | null;
  candidate_revision_id?: string | null;
  status: "pending" | "running" | "awaiting_approval" | "completed" | "blocked" | "failed";
  apply_status: "pending" | "applied" | "awaiting_approval" | "blocked" | "failed" | "rolled_back" | "noop";
  draft_status: "none" | "ready" | "approved" | "discarded" | "failed";
  draft_ready: boolean;
  approval_required: boolean;
  iteration_count: number;
  current_stage: string;
  progress_percent: number;
  summary?: string | null;
  failure_reason?: string | null;
  failure_class?: string | null;
  failure_signature?: string | null;
  root_cause_summary?: string | null;
  current_fix_phase?: string | null;
  current_failing_command?: string | null;
  current_exit_code?: number | null;
  fix_targets?: string[];
  handoff_from_failed_generate?: {
    mode?: "generate" | "fix";
    prompt?: string;
    error_context?: {
      raw_error: string;
      source?: "build" | "preview" | "miniapp" | "frontend" | "runtime" | null;
      failing_target?: string | null;
    } | null;
    failure_class?: string | null;
  } | null;
  error_context?: {
    raw_error: string;
    source?: "build" | "preview" | "miniapp" | "frontend" | "runtime" | null;
    failing_target?: string | null;
  } | null;
  checks_summary: {
    validators: "pending" | "passed" | "failed" | "blocked" | "skipped";
    build: "pending" | "passed" | "failed" | "blocked" | "skipped";
    preview: "pending" | "passed" | "failed" | "blocked" | "skipped";
    issues: Array<{ code?: string; message?: string; severity?: string }>;
  };
  touched_files: string[];
  role_coverage?: Record<string, unknown>;
  generated_tests?: Record<string, unknown>;
  neutral_template_findings?: Array<Record<string, unknown>>;
  orchestration_phases?: Array<Record<string, unknown>>;
  implementation_plan?: Record<string, unknown>;
  agent_activity_events?: Array<{
    type?: string;
    message?: string;
    created_at?: string;
    details?: Record<string, unknown>;
    batch_id?: string;
    worker?: string | null;
    worker_id?: string | null;
    owner_scope?: string | null;
    tool_use_id?: string;
    phase?: string;
    elapsed_ms?: number;
    artifact_ref?: string | null;
    summary?: string;
    duration_ms?: number;
    status?: string;
    hook?: string;
    semantic_status?: string;
  }>;
  agent_memory?: Record<string, unknown>;
  acceptance_contract?: Record<string, unknown>;
  worker_summaries?: Array<Record<string, unknown>>;
  flow_coverage?: Record<string, unknown>;
  browser_flow_proof?: Record<string, unknown>;
  agent_transcript_ref?: string | null;
  tool_trace_ref?: string | null;
  file_change_history_ref?: string | null;
  browser_proof_ref?: string | null;
  large_tool_outputs_ref?: string | null;
  file_state_cache_ref?: string | null;
  turn_diff_ref?: string | null;
  environment_snapshot_ref?: string | null;
  tool_batch_summaries_ref?: string | null;
  worker_mailbox_ref?: string | null;
  scratchpad_ref?: string | null;
  memory_ref?: string | null;
  worker_drafts_ref?: string | null;
  worker_merge_ref?: string | null;
  trace_bundle_ref?: string | null;
  trace_reducer_ref?: string | null;
  command_policy_ref?: string | null;
  verification_report_ref?: string | null;
  rollout_trace_ref?: string | null;
  exec_trace_ref?: string | null;
  process_outputs_ref?: string | null;
  tool_result_messages_ref?: string | null;
  active_processes?: Array<Record<string, unknown>>;
  artifact_read_trace_ref?: string | null;
  resume_checkpoint_ref?: string | null;
  worker_branch_refs?: Array<string>;
  verifier_review_ref?: string | null;
  browser_step_refs?: Array<string>;
  active_tool_uses?: Array<Record<string, unknown>>;
  context_pressure_ref?: string | null;
  hook_trace_ref?: string | null;
  semantic_graph_ref?: string | null;
  worker_prefix_ref?: string | null;
  replay_trace_ref?: string | null;
  miniapp_contract_ref?: string | null;
  route_registry_ref?: string | null;
  contract_compile_ref?: string | null;
  repair_recipes_ref?: string | null;
  repair_issue_signatures?: Array<Record<string, unknown>>;
  mobile_layout_report?: Record<string, unknown>;
  artifacts: Record<string, string>;
  repair_iterations?: Array<Record<string, unknown>>;
  token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    reasoning_tokens?: number;
    total_tokens?: number;
    turn_count?: number;
    last_turn?: Record<string, unknown>;
  };
  rolled_back: boolean;
  rolled_back_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type RunArtifacts = {
  run: Run;
  job?: {
    job_id: string;
    status: string;
    compile_summary?: Record<string, number | string>;
    summary?: string | null;
    assumptions_report?: Array<{ text?: string; rationale?: string }>;
    validation_snapshot?: {
      platform_valid: boolean;
      checks_valid: boolean;
      build_valid: boolean;
      blocking: boolean;
      issues: Array<{ code: string; message: string; severity?: string }>;
    } | null;
  };
  validation?: Record<string, unknown> | null;
  trace?: { entries?: Array<{ stage: string; message: string; created_at?: string }> } | null;
  agent_diagnostics?: { items?: Array<Record<string, unknown>> } | null;
  iterations?: Array<{
    iteration_id: string;
    assistant_message: string;
    files_read: string[];
    file_changes: Array<{ file_path: string; operation: "create" | "replace" | "delete" | "patch"; reason: string }>;
    check_results: Array<{ name: string; status: string; details?: string | null }>;
    diff_summary?: string | null;
    role_scope: Array<"client" | "specialist" | "manager">;
    created_at: string;
  }>;
  check_results?: Array<{ name: string; status: string; details?: string | null }>;
  role_coverage?: Record<string, unknown>;
  generated_tests?: Record<string, unknown>;
  neutral_template_findings?: Array<Record<string, unknown>>;
  orchestration_phases?: Array<Record<string, unknown>>;
  implementation_plan?: Record<string, unknown>;
  agent_activity_events?: Array<{
    type?: string;
    message?: string;
    created_at?: string;
    details?: Record<string, unknown>;
    batch_id?: string;
    worker?: string | null;
    worker_id?: string | null;
    owner_scope?: string | null;
    tool_use_id?: string;
    phase?: string;
    elapsed_ms?: number;
    artifact_ref?: string | null;
    summary?: string;
    duration_ms?: number;
    status?: string;
    hook?: string;
    semantic_status?: string;
  }>;
  agent_memory?: Record<string, unknown>;
  acceptance_contract?: Record<string, unknown>;
  worker_summaries?: Array<Record<string, unknown>>;
  flow_coverage?: Record<string, unknown>;
  browser_flow_proof?: Record<string, unknown>;
  agent_transcript_ref?: string | null;
  tool_trace_ref?: string | null;
  file_change_history_ref?: string | null;
  browser_proof_ref?: string | null;
  large_tool_outputs_ref?: string | null;
  file_state_cache_ref?: string | null;
  turn_diff_ref?: string | null;
  environment_snapshot_ref?: string | null;
  tool_batch_summaries_ref?: string | null;
  worker_mailbox_ref?: string | null;
  scratchpad_ref?: string | null;
  memory_ref?: string | null;
  worker_drafts_ref?: string | null;
  worker_merge_ref?: string | null;
  trace_bundle_ref?: string | null;
  trace_reducer_ref?: string | null;
  command_policy_ref?: string | null;
  verification_report_ref?: string | null;
  rollout_trace_ref?: string | null;
  exec_trace_ref?: string | null;
  process_outputs_ref?: string | null;
  tool_result_messages_ref?: string | null;
  active_processes?: Array<Record<string, unknown>>;
  artifact_read_trace_ref?: string | null;
  resume_checkpoint_ref?: string | null;
  worker_branch_refs?: Array<string>;
  verifier_review_ref?: string | null;
  browser_step_refs?: Array<string>;
  active_tool_uses?: Array<Record<string, unknown>>;
  context_pressure_ref?: string | null;
  hook_trace_ref?: string | null;
  semantic_graph_ref?: string | null;
  worker_prefix_ref?: string | null;
  replay_trace_ref?: string | null;
  repair_issue_signatures?: Array<Record<string, unknown>>;
  mobile_layout_report?: Record<string, unknown>;
  draft_preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
  };
  diff?: string;
  failure_analysis?: {
    mode?: "generate" | "fix";
    failure_class?: string | null;
    failure_signature?: string | null;
    root_cause_summary?: string | null;
    fix_targets?: string[];
    handoff_from_failed_generate?: Run["handoff_from_failed_generate"] | null;
    error_context?: Run["error_context"] | null;
    current_fix_phase?: string | null;
    current_failing_command?: string | null;
    current_exit_code?: number | null;
    executed_checks?: Array<Record<string, unknown>>;
    container_statuses?: Array<Record<string, unknown>>;
  } | null;
  fix_case?: Record<string, unknown> | null;
  fix_runtime?: Record<string, unknown> | null;
  preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
    logs?: string[];
    draft_run_id?: string | null;
  };
};

export type WorkspaceLogs = {
  workspace_id: string;
  job: {
    job_id: string;
    status: string;
    generation_mode?: string;
    fidelity?: string;
    llm_model?: string | null;
    llm_provider?: string | null;
    failure_reason?: string | null;
    failure_class?: string | null;
    failure_signature?: string | null;
    current_fix_phase?: string | null;
    current_failing_command?: string | null;
    current_exit_code?: number | null;
  } | null;
  events: Array<{
    event_id: string;
    event_type: string;
    message: string;
    created_at: string;
    details?: Record<string, unknown>;
  }>;
  workspace_logs?: string[];
  preview: {
    status: string;
    runtime_mode: string;
    url: string | null;
    logs: string[];
    draft_run_id?: string | null;
    mini_app_logs?: string[];
  };
  reports: {
    trace?: {
      workspace_id: string;
      entries: Array<{
        stage: string;
        message: string;
        created_at: string;
        payload?: Record<string, unknown>;
      }>;
    } | null;
    validation?: Record<string, unknown> | null;
    iterations?: Record<string, unknown> | null;
    candidate_diff?: Record<string, unknown> | null;
    check_results?: Record<string, unknown> | null;
    fix_case?: Record<string, unknown> | null;
    fix_runtime?: Record<string, unknown> | null;
  };
};

export type SystemConfiguration = {
  llm: {
    enabled: boolean;
    provider?: string | null;
    models?: Record<string, unknown>;
    task_profiles?: Record<string, unknown>;
  };
  defaults: {
    generation_mode: "fast" | "balanced" | "quality" | "basic";
    model_profile: string;
  };
  default_coding_profile: string;
  supports_staged_apply: boolean;
  research_artifacts_enabled: boolean;
};

export type AgentThread = {
  thread_id: string;
  workspace_id: string;
  title: string;
  status: "active" | "running" | "idle" | "archived" | "failed";
  archived: boolean;
  current_turn_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentTurn = {
  turn_id: string;
  thread_id: string;
  workspace_id: string;
  kind: "user" | "agent" | "review" | "compaction" | "repair";
  status: "queued" | "running" | "completed" | "failed" | "interrupted";
  prompt: string;
  linked_run_id?: string | null;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentItem = {
  item_id: string;
  thread_id: string;
  turn_id?: string | null;
  item_type: string;
  status: "started" | "completed" | "failed";
  sequence: number;
  payload: Record<string, unknown>;
  created_at: string;
};

export type RunTimeline = {
  run_id: string;
  tool_protocol_version: string;
  items: Array<{
    sequence: number;
    kind: string;
    status: string;
    title: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
};

export type RunTraceView = {
  run_id: string;
  trace_id: string;
  status: string;
  apply_status: string;
  timeline: RunTimeline["items"];
  reducer: {
    why?: string;
    failed_checks?: RunTimeline["items"];
    patches?: RunTimeline["items"];
    browser_proofs?: RunTimeline["items"];
    failures?: RunTimeline["items"];
    fixes?: RunTimeline["items"];
  };
  artifact_refs?: Record<string, string | null | undefined>;
};

export type DoctorReport = {
  status: string;
  checks: Array<{
    name: string;
    status: string;
    details?: string;
    command?: string;
    required?: boolean;
  }>;
  created_at?: string;
};

export type WorkerReport = {
  run_id: string;
  workers: Array<{
    worker_id: string;
    status: string;
    owner_scope: string;
    changed_files: string[];
    summaries?: Array<Record<string, unknown>>;
    merge_reports?: Array<Record<string, unknown>>;
  }>;
  worker_branch_refs?: string[];
};

export type ReviewReport = {
  run_id: string;
  status: string;
  findings: Array<Record<string, unknown>>;
  evidence?: Record<string, unknown>;
};

export type TestMatrixReport = {
  run_id: string;
  workspace_id: string;
  status: string;
  items: Array<{
    key: string;
    label: string;
    status: string;
    required: boolean;
    evidence?: Array<Record<string, unknown>>;
  }>;
};

export type PromptContractReport = {
  run_id: string;
  status: string;
  prompt_terms_checked: string[];
  matched_terms: string[];
  findings: Array<Record<string, unknown>>;
};

export type MiniAppContractReport = {
  run_id: string;
  workspace_id: string;
  status: string;
  contract: Record<string, unknown>;
  registry_snapshot: Record<string, unknown>;
  drift_issues: Array<Record<string, unknown>>;
  repair_recipes: Array<Record<string, unknown>>;
  artifact_refs: Record<string, string | null>;
};

export type WorkspaceMemory = {
  workspace_id: string;
  items: Array<{
    memory_id?: string;
    kind: string;
    text: string;
    citation?: Record<string, unknown> | null;
    created_at?: string;
  }>;
  project_rules?: string[];
  user_preferences?: string[];
  platform_constraints?: string[];
  repeated_fixes?: string[];
};

export type ApprovalRecord = {
  approval_id: string;
  status: "pending" | "approved" | "rejected" | string;
  kind?: string;
  risk?: string;
  summary?: string;
  input?: Record<string, unknown>;
  policy_decision?: Record<string, unknown>;
  created_at?: string;
  decided_at?: string;
};

export type ToolEventEnvelope = {
  tool_call_id: string;
  tool: string;
  version: string;
  input: Record<string, unknown>;
  risk: string;
  approval: Record<string, unknown>;
  progress?: Array<Record<string, unknown>>;
  result: Record<string, unknown>;
  artifacts: Array<Record<string, unknown>>;
  timing: Record<string, unknown>;
  retry?: Record<string, unknown>;
  truncation?: Record<string, unknown>;
  error?: Record<string, unknown> | null;
  created_at?: string;
};

export type StagedApplyState = {
  run_id: string;
  files: string[];
  categories?: Record<string, string>;
  status: string;
  updated_at?: string;
};

export type FileSearchResult = {
  workspace_id: string;
  run_id?: string | null;
  query: string;
  items: Array<{
    path: string;
    hits: Array<{ line: number; text: string }>;
    score?: number;
    language?: string;
    symbols?: Array<{ kind: string; name: string; line: number }>;
  }>;
  symbols?: Array<{ path: string; kind: string; name: string; line: number }>;
};

export type LspDiagnosticsReport = {
  workspace_id: string;
  run_id?: string | null;
  status: string;
  items: Array<{ path: string; severity: string; message: string; source: string }>;
  symbols?: Array<{ path: string; kind: string; name: string; line: number }>;
};

export type CommandPaletteAction = {
  id: string;
  label: string;
  description?: string;
  disabled?: boolean;
};

type RpcEnvelope = {
  id?: number;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
};

type RpcNotificationHandler = (message: RpcEnvelope) => void;

class JsonRpcClient {
  private socket: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
  private connected: Promise<void> | null = null;
  private handlers = new Set<RpcNotificationHandler>();

  async call<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ensureConnected();
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("RPC socket is not open.");
    }
    const id = this.nextId++;
    const result = new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
    });
    socket.send(JSON.stringify({ id, method, params }));
    return result;
  }

  subscribe(handler: RpcNotificationHandler): () => void {
    this.handlers.add(handler);
    void this.ensureConnected().catch(() => undefined);
    return () => {
      this.handlers.delete(handler);
    };
  }

  private async ensureConnected(): Promise<void> {
    if (this.socket?.readyState === WebSocket.OPEN) {
      return;
    }
    if (this.connected) {
      return this.connected;
    }
    this.connected = new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(this.rpcUrl());
      this.socket = socket;
      socket.addEventListener("open", async () => {
        try {
          await this.callRaw("initialize", { clientInfo: { name: "grounded-miniapp-frontend" } });
          resolve();
        } catch (error) {
          reject(error);
        }
      });
      socket.addEventListener("message", (event) => {
        const message = JSON.parse(String(event.data)) as RpcEnvelope;
        if (typeof message.id === "number") {
          const pending = this.pending.get(message.id);
          if (!pending) {
            return;
          }
          this.pending.delete(message.id);
          if (message.error) {
            pending.reject(new Error(message.error.message));
          } else {
            pending.resolve(message.result);
          }
          return;
        }
        this.handlers.forEach((handler) => handler(message));
      });
      socket.addEventListener("close", () => {
        this.socket = null;
        this.connected = null;
      });
      socket.addEventListener("error", () => {
        reject(new Error("RPC socket connection failed."));
      });
    });
    return this.connected;
  }

  private async callRaw<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("RPC socket is not open.");
    }
    const id = this.nextId++;
    const result = new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
    });
    socket.send(JSON.stringify({ id, method, params }));
    return result;
  }

  private rpcUrl(): string {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}/rpc`;
  }
}

const rpcClient = new JsonRpcClient();
const workspaceThreadIds = new Map<string, string>();
const runTurnRefs = new Map<string, { threadId: string; turnId: string }>();

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  return request<Workspace[]>("/workspaces");
}

export async function ensureWorkspace(): Promise<Workspace> {
  return request<Workspace>("/workspaces", {
    method: "POST",
    body: JSON.stringify({
      name: "Research Workspace",
      description: "Single-user grounded research session",
      target_platform: "telegram_mini_app",
      preview_profile: "telegram_mock",
    }),
  });
}

export async function openWorkspace(workspaceId: string): Promise<Workspace> {
  return request<Workspace>(`/workspaces/${workspaceId}`);
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  await request<{ deleted: string }>(`/workspaces/${workspaceId}`, {
    method: "DELETE",
  });
}

export async function listRuns(workspaceId: string): Promise<Run[]> {
  return request<Run[]>(`/workspaces/${workspaceId}/runs`);
}

export async function createRun(
  workspaceId: string,
  payload: {
    prompt: string;
    mode?: "generate" | "fix";
    intent?: "auto" | "create" | "edit" | "refine" | "role_only_change";
    apply_strategy?: "staged_auto_apply" | "manual_approve";
    target_role_scope?: Array<"client" | "specialist" | "manager">;
    model_profile?: string;
    generation_mode?: "fast" | "balanced" | "quality" | "basic";
    target_platform?: "telegram_mini_app" | "max_mini_app";
    preview_profile?: "telegram_mock" | "max_mock" | "web_preview";
    resume_from_run_id?: string;
    error_context?: {
      raw_error: string;
      source?: "build" | "preview" | "miniapp" | "frontend" | "runtime";
      failing_target?: string;
    };
  },
): Promise<Run> {
  const threadId = await getOrCreateWorkspaceThread(workspaceId);
  const turn = await rpcClient.call<AgentTurn>("turn/start", {
    thread_id: threadId,
    mode: "generate",
    intent: "auto",
    apply_strategy: "staged_auto_apply",
    target_role_scope: [],
    generation_mode: "balanced",
    target_platform: "telegram_mini_app",
    preview_profile: "telegram_mock",
    ...payload,
  });
  const run = turn.metadata?.run as Run | undefined;
  if (!run?.run_id) {
    throw new Error("RPC turn did not return a linked run.");
  }
  runTurnRefs.set(run.run_id, { threadId, turnId: turn.turn_id });
  return run;
}

export async function getRunArtifacts(runId: string): Promise<RunArtifacts> {
  return request<RunArtifacts>(`/runs/${runId}/artifacts`);
}

export async function getRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}`);
}

export async function getRunTimeline(runId: string): Promise<RunTimeline> {
  return request<RunTimeline>(`/runs/${runId}/timeline`);
}

export async function getRunTraceView(runId: string): Promise<RunTraceView> {
  return request<RunTraceView>(`/runs/${runId}/trace-view`);
}

export async function getRunApprovals(runId: string): Promise<{ run_id: string; items: ApprovalRecord[] }> {
  return request<{ run_id: string; items: ApprovalRecord[] }>(`/runs/${runId}/approvals`);
}

export async function approveRunApproval(runId: string, approvalId: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(`/runs/${runId}/approvals/${approvalId}/approve`, { method: "POST" });
}

export async function rejectRunApproval(runId: string, approvalId: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(`/runs/${runId}/approvals/${approvalId}/reject`, { method: "POST" });
}

export async function stageRunFiles(runId: string, files: string[]): Promise<StagedApplyState> {
  return request<StagedApplyState>(`/runs/${runId}/stage/files`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
}

export async function discardRunFiles(runId: string, files: string[]): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/discard/files`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
}

export async function applyStagedRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/apply/staged`, { method: "POST" });
}

export async function compactRun(runId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/compact`, { method: "POST" });
}

export async function startReviewFix(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/review/fix`, { method: "POST" });
}

export async function getDoctorReport(): Promise<DoctorReport> {
  return request<DoctorReport>("/doctor");
}

export async function getRunWorkers(runId: string): Promise<WorkerReport> {
  return request<WorkerReport>(`/runs/${runId}/workers`);
}

export async function getRunReview(runId: string): Promise<ReviewReport> {
  return request<ReviewReport>(`/runs/${runId}/review`);
}

export async function getRunTestMatrix(runId: string): Promise<TestMatrixReport> {
  return request<TestMatrixReport>(`/runs/${runId}/test-matrix`);
}

export async function getRunPromptContract(runId: string): Promise<PromptContractReport> {
  return request<PromptContractReport>(`/runs/${runId}/prompt-contract`);
}

export async function getRunMiniAppContract(runId: string): Promise<MiniAppContractReport> {
  return request<MiniAppContractReport>(`/runs/${runId}/miniapp-contract`);
}

export async function getWorkspaceMemory(workspaceId: string): Promise<WorkspaceMemory> {
  return request<WorkspaceMemory>(`/workspaces/${workspaceId}/memory`);
}

export async function searchWorkspaceFiles(workspaceId: string, query: string, runId?: string): Promise<FileSearchResult> {
  const params = new URLSearchParams({ q: query });
  if (runId) {
    params.set("run_id", runId);
  }
  return request<FileSearchResult>(`/workspaces/${workspaceId}/files/search?${params.toString()}`);
}

export async function getLspDiagnostics(workspaceId: string, runId?: string): Promise<LspDiagnosticsReport> {
  const params = new URLSearchParams();
  if (runId) {
    params.set("run_id", runId);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<LspDiagnosticsReport>(`/workspaces/${workspaceId}/diagnostics/lsp${suffix}`);
}

export async function stopRun(runId: string): Promise<Run> {
  const ref = runTurnRefs.get(runId);
  if (ref) {
    await rpcClient.call("turn/interrupt", { thread_id: ref.threadId, turn_id: ref.turnId });
  }
  return request<Run>(`/runs/${runId}/stop`, {
    method: "POST",
  });
}

export async function getRunIterations(runId: string): Promise<Array<Record<string, unknown>>> {
  return request<Array<Record<string, unknown>>>(`/runs/${runId}/iterations`);
}

export async function approveRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/approve`, {
    method: "POST",
  });
}

export async function discardRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/discard`, {
    method: "POST",
  });
}

export async function rebuildPreview(workspaceId: string): Promise<void> {
  await request(`/workspaces/${workspaceId}/preview/rebuild`, {
    method: "POST",
  });
}

export async function startPreview(workspaceId: string): Promise<void> {
  await request(`/workspaces/${workspaceId}/preview/start`, {
    method: "POST",
  });
}

export async function ensurePreview(workspaceId: string): Promise<void> {
  await request(`/workspaces/${workspaceId}/preview/ensure`, {
    method: "POST",
  });
}

export async function rollbackRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/rollback`, {
    method: "POST",
  });
}

export async function getWorkspaceLogs(workspaceId: string): Promise<WorkspaceLogs> {
  return request<WorkspaceLogs>(`/workspaces/${workspaceId}/logs`);
}

export function subscribeRpcNotifications(handler: RpcNotificationHandler): () => void {
  return rpcClient.subscribe(handler);
}

export async function execWorkspaceCommand(payload: {
  workspace_id: string;
  command: string;
  thread_id?: string;
  turn_id?: string;
  timeout?: number;
  approval_id?: string;
  preset?: string;
}): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec", payload);
}

export async function writeExecStdin(processId: string, data: string): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/write", { process_id: processId, data });
}

export async function resizeExec(processId: string, cols: number, rows: number): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/resize", { process_id: processId, cols, rows });
}

export async function terminateExec(processId: string): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/terminate", { process_id: processId });
}

export async function readExecOutput(processId: string, stream: "stdout" | "stderr" = "stdout"): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/read", { process_id: processId, stream });
}

export async function listThreads(workspaceId: string): Promise<AgentThread[]> {
  const page = await rpcClient.call<{ items: AgentThread[]; next_cursor?: string | null }>("thread/list", { workspace_id: workspaceId });
  return page.items;
}

export async function readThread(threadId: string): Promise<{ thread: AgentThread; turns: AgentTurn[]; items: AgentItem[] }> {
  return rpcClient.call("thread/read", { thread_id: threadId });
}

async function getOrCreateWorkspaceThread(workspaceId: string): Promise<string> {
  const cached = workspaceThreadIds.get(workspaceId);
  if (cached) {
    return cached;
  }
  const existing = await listThreads(workspaceId);
  const thread = existing[0] ?? await rpcClient.call<AgentThread>("thread/start", {
    workspace_id: workspaceId,
    title: "Mini-app generation",
  });
  workspaceThreadIds.set(workspaceId, thread.thread_id);
  return thread.thread_id;
}
