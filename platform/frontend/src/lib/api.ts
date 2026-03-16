export type Workspace = {
  workspace_id: string;
  name: string;
  description?: string | null;
  template_cloned: boolean;
  current_revision_id?: string | null;
};

export type SystemConfiguration = {
  llm: {
    enabled: boolean;
    provider?: string | null;
    models?: Record<string, unknown>;
  };
  defaults: {
    generation_mode: "quality" | "balanced" | "basic";
  };
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
