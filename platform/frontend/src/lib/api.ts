export type Workspace = {
  workspace_id: string;
  name: string;
  description?: string | null;
  template_cloned: boolean;
  current_revision_id?: string | null;
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

