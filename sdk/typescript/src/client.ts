export type GroundedClientOptions = {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export class GroundedClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: GroundedClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async listWorkspaces<T = unknown>(): Promise<T> {
    return this.request<T>("/workspaces");
  }

  async createRun<T = unknown>(workspaceId: string, payload: Record<string, unknown>): Promise<T> {
    return this.request<T>(`/workspaces/${encodeURIComponent(workspaceId)}/runs`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getRun<T = unknown>(runId: string): Promise<T> {
    return this.request<T>(`/runs/${encodeURIComponent(runId)}`);
  }

  async getTimeline<T = unknown>(runId: string): Promise<T> {
    return this.request<T>(`/runs/${encodeURIComponent(runId)}/timeline`);
  }

  async getApprovals<T = unknown>(runId: string): Promise<T> {
    return this.request<T>(`/runs/${encodeURIComponent(runId)}/approvals`);
  }

  async approve(runId: string, approvalId: string): Promise<unknown> {
    return this.request(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/approve`, { method: "POST" });
  }

  async searchFiles<T = unknown>(workspaceId: string, query: string, runId?: string): Promise<T> {
    const params = new URLSearchParams({ q: query });
    if (runId) params.set("run_id", runId);
    return this.request<T>(`/workspaces/${encodeURIComponent(workspaceId)}/files/search?${params.toString()}`);
  }

  async export(workspaceId: string, kind: "zip" | "git-patch" | "deploy-bundle" | "docker-validation-report" | "manifest" | "browser-proof-bundle"): Promise<unknown> {
    return this.request(`/workspaces/${encodeURIComponent(workspaceId)}/export/${kind}`, { method: "POST" });
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
      ...init,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json() as Promise<T>;
  }
}
