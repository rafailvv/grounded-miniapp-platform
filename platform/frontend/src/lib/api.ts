export type Workspace = {
  workspace_id: string;
  name: string;
  description?: string | null;
  template_cloned: boolean;
  current_revision_id?: string | null;
};

export type Run = {
  run_id: string;
  workspace_id: string;
  prompt: string;
  intent: "create" | "edit" | "refine" | "role_only_change";
  apply_strategy: "staged_auto_apply" | "manual_approve";
  target_role_scope: Array<"client" | "specialist" | "manager">;
  model_profile: string;
  llm_provider?: string | null;
  llm_model?: string | null;
  linked_job_id?: string | null;
  source_revision_id?: string | null;
  result_revision_id?: string | null;
  status: "pending" | "running" | "awaiting_approval" | "completed" | "blocked" | "failed";
  apply_status: "pending" | "applied" | "awaiting_approval" | "blocked" | "failed";
  current_stage: string;
  progress_percent: number;
  summary?: string | null;
  failure_reason?: string | null;
  checks_summary: {
    validators: "pending" | "passed" | "failed" | "blocked";
    build: "pending" | "passed" | "failed" | "blocked";
    preview: "pending" | "passed" | "failed" | "blocked";
    issues: Array<{ code?: string; message?: string; severity?: string }>;
  };
  touched_files: string[];
  artifacts: Record<string, string>;
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
  app_ir?: Record<string, unknown> | null;
  validation?: Record<string, unknown> | null;
  assumptions?: Record<string, unknown> | null;
  traceability?: Record<string, unknown> | null;
  artifact_plan?: Record<string, unknown> | null;
  trace?: { entries?: Array<{ stage: string; message: string; created_at?: string }> } | null;
  code_change_plan?: {
    summary?: string;
    targets?: Array<{ file_path: string; operation: string; reason: string; risk: string }>;
    risks?: string[];
    acceptance_checks?: string[];
  } | null;
  diff?: string;
  preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
    logs?: string[];
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
  } | null;
  events: Array<{
    event_id: string;
    event_type: string;
    message: string;
    created_at: string;
    details?: Record<string, unknown>;
  }>;
  preview: {
    status: string;
    runtime_mode: string;
    url: string | null;
    logs: string[];
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
    artifact_plan?: Record<string, unknown> | null;
    spec_summary?: Record<string, unknown> | null;
    ir_summary?: Record<string, unknown> | null;
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
    generation_mode: "quality" | "balanced" | "basic";
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
    intent?: "auto" | "create" | "edit" | "refine" | "role_only_change";
    apply_strategy?: "staged_auto_apply" | "manual_approve";
    target_role_scope?: Array<"client" | "specialist" | "manager">;
    model_profile?: string;
    generation_mode?: "quality" | "balanced" | "basic";
    target_platform?: "telegram_mini_app" | "max_mini_app";
    preview_profile?: "telegram_mock" | "max_mock" | "web_preview";
  },
): Promise<Run> {
  return request<Run>(`/workspaces/${workspaceId}/runs`, {
    method: "POST",
    body: JSON.stringify({
      intent: "auto",
      apply_strategy: "staged_auto_apply",
      target_role_scope: [],
      generation_mode: "quality",
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

export async function rebuildPreview(workspaceId: string): Promise<void> {
  await request(`/workspaces/${workspaceId}/preview/rebuild`, {
    method: "POST",
  });
}

export async function getWorkspaceLogs(workspaceId: string): Promise<WorkspaceLogs> {
  return request<WorkspaceLogs>(`/workspaces/${workspaceId}/logs`);
}
