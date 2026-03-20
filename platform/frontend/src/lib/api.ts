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
  apply_status: "pending" | "applied" | "awaiting_approval" | "blocked" | "failed" | "rolled_back";
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
      source?: "build" | "preview" | "backend" | "frontend" | "runtime" | null;
      failing_target?: string | null;
    } | null;
    failure_class?: string | null;
  } | null;
  error_context?: {
    raw_error: string;
    source?: "build" | "preview" | "backend" | "frontend" | "runtime" | null;
    failing_target?: string | null;
  } | null;
  checks_summary: {
    validators: "pending" | "passed" | "failed" | "blocked" | "skipped";
    build: "pending" | "passed" | "failed" | "blocked" | "skipped";
    preview: "pending" | "passed" | "failed" | "blocked" | "skipped";
    issues: Array<{ code?: string; message?: string; severity?: string }>;
  };
  touched_files: string[];
  artifacts: Record<string, string>;
  repair_iterations?: Array<Record<string, unknown>>;
  fix_attempts?: Array<Record<string, unknown>>;
  scope_expansions?: Array<Record<string, unknown>>;
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
      grounded_spec_valid: boolean;
      app_ir_valid: boolean;
      build_valid: boolean;
      blocking: boolean;
      issues: Array<{ code: string; message: string; severity?: string }>;
    } | null;
  };
  grounded_spec?: Record<string, unknown> | null;
  validation?: Record<string, unknown> | null;
  assumptions?: Record<string, unknown> | null;
  traceability?: Record<string, unknown> | null;
  trace?: { entries?: Array<{ stage: string; message: string; created_at?: string }> } | null;
  code_change_plan?: {
    summary?: string;
    targets?: Array<{ file_path: string; operation: "create" | "replace" | "delete"; reason: string; risk: string }>;
    risks?: string[];
    acceptance_checks?: string[];
  } | null;
  iterations?: Array<{
    iteration_id: string;
    assistant_message: string;
    files_read: string[];
    operations: Array<{ file_path: string; operation: "create" | "replace" | "delete"; reason: string }>;
    check_results: Array<{ name: string; status: string; details?: string | null }>;
    diff_summary?: string | null;
    role_scope: Array<"client" | "specialist" | "manager">;
    created_at: string;
  }>;
  candidate_diff?: string;
  check_results?: Array<{ name: string; status: string; details?: string | null }>;
  draft_preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
  };
  final_summary?: string | null;
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
  fix_attempts?: { items?: Array<Record<string, unknown>> } | null;
  scope_expansions?: { items?: Array<Record<string, unknown>> } | null;
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
  platform_log?: string[];
  api_log?: string[];
  preview: {
    status: string;
    runtime_mode: string;
    url: string | null;
    logs: string[];
    draft_run_id?: string | null;
    containers?: Array<{
      service: string;
      name?: string | null;
      state?: string | null;
      status?: string | null;
      health?: string | null;
      exit_code?: string | null;
    }>;
    container_logs?: Record<string, string[]>;
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
    assumptions?: Record<string, unknown> | null;
    traceability?: Record<string, unknown> | null;
    iterations?: Record<string, unknown> | null;
    candidate_diff?: Record<string, unknown> | null;
    check_results?: Record<string, unknown> | null;
    fix_case?: Record<string, unknown> | null;
    fix_attempts?: Record<string, unknown> | null;
    scope_expansions?: Record<string, unknown> | null;
    fix_runtime?: Record<string, unknown> | null;
    spec_summary?: Record<string, unknown> | null;
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
  const workspace = await request<Workspace>("/workspaces", {
    method: "POST",
    body: JSON.stringify({
      name: "Research Workspace",
      description: "Single-user grounded research session",
      target_platform: "telegram_mini_app",
      preview_profile: "telegram_mock",
    }),
  });
  return request<Workspace>(`/workspaces/${workspace.workspace_id}/clone-template`, {
    method: "POST",
  });
}

export async function openWorkspace(workspaceId: string): Promise<Workspace> {
  const workspace = await request<Workspace>(`/workspaces/${workspaceId}`);
  if (workspace.template_cloned) {
    return workspace;
  }
  return request<Workspace>(`/workspaces/${workspace.workspace_id}/clone-template`, {
    method: "POST",
  });
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
      source?: "build" | "preview" | "backend" | "frontend" | "runtime";
      failing_target?: string;
    };
  },
): Promise<Run> {
  return request<Run>(`/workspaces/${workspaceId}/runs`, {
    method: "POST",
    body: JSON.stringify({
      mode: "generate",
      intent: "auto",
      apply_strategy: "staged_auto_apply",
      target_role_scope: [],
      generation_mode: "balanced",
      target_platform: "telegram_mini_app",
      preview_profile: "telegram_mock",
      ...payload,
    }),
  });
}

export async function getRunArtifacts(runId: string): Promise<RunArtifacts> {
  return request<RunArtifacts>(`/runs/${runId}/artifacts`);
}

export async function getRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}`);
}

export async function stopRun(runId: string): Promise<Run> {
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
