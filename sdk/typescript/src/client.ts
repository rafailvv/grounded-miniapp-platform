export type GroundedClientOptions = {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type RunEventType =
  | "run_started"
  | "run.started"
  | "turn.started"
  | "tool.started"
  | "tool.completed"
  | "check.completed"
  | "repair.packet"
  | "browser.step"
  | "gate.changed"
  | "run.completed"
  | "usage"
  | "tool_event"
  | "check_completed"
  | "repair_packet"
  | "browser_step"
  | "gate_changed"
  | "run_state_changed"
  | "run_completed"
  | "run_stream_timeout"
  | "timeline_event";

export type RunEvent = {
  type: RunEventType;
  runId: string;
  payload: JsonObject;
};

export class GroundedClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: GroundedClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async listWorkspaces(): Promise<JsonValue> {
    return this.requestValue("/workspaces");
  }

  async createWorkspace(payload: JsonObject): Promise<JsonObject> {
    return this.requestObject("/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async createRun(workspaceId: string, payload: JsonObject): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/runs`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async listThreads(options: { workspaceId?: string; includeArchived?: boolean; limit?: number } = {}): Promise<JsonObject> {
    const params = new URLSearchParams({
      include_archived: String(options.includeArchived ?? false),
      limit: String(options.limit ?? 50),
    });
    if (options.workspaceId) params.set("workspace_id", options.workspaceId);
    return this.requestObject(`/threads?${params.toString()}`);
  }

  async startThread(workspaceId: string, options: { title?: string; metadata?: JsonObject } = {}): Promise<JsonObject> {
    return this.requestObject("/threads", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, title: options.title ?? "", metadata: options.metadata ?? {} }),
    });
  }

  async getThread(threadId: string): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}`);
  }

  async resumeThread(threadId: string): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/resume`, { method: "POST" });
  }

  async startTurn(threadId: string, payload: JsonObject): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/turns`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getRun(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}`);
  }

  async getTimeline(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/timeline`);
  }

  async getTraceView(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/trace-view`);
  }

  async getRunEvents(runId: string, options: { afterSequence?: number; limit?: number } = {}): Promise<JsonObject> {
    const params = new URLSearchParams({
      after_sequence: String(options.afterSequence ?? 0),
      limit: String(options.limit ?? 500),
    });
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/events?${params.toString()}`);
  }

  async getGate(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/gate`);
  }

  async getRunState(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/state`);
  }

  async getFinalReport(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/final-report`);
  }

  async getRepairSignatures(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/repair-signatures`);
  }

  async resumeRun(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/resume`, { method: "POST" });
  }

  async getArtifacts(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/artifacts`);
  }

  async getApprovals(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/approvals`);
  }

  async approve(runId: string, approvalId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/approve`, { method: "POST" });
  }

  async searchFiles(workspaceId: string, query: string, runId?: string): Promise<JsonObject> {
    const params = new URLSearchParams({ q: query });
    if (runId) params.set("run_id", runId);
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/files/search?${params.toString()}`);
  }

  async diagnostics(workspaceId: string, runId?: string): Promise<JsonObject> {
    const params = new URLSearchParams();
    if (runId) params.set("run_id", runId);
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/diagnostics/lsp${suffix}`);
  }

  async patchPreflight(workspaceId: string, payload: JsonObject): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/patch/preflight`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async doctor(): Promise<JsonObject> {
    return this.requestObject("/doctor");
  }

  async metrics(): Promise<JsonObject> {
    return this.requestObject("/system/metrics/summary");
  }

  async securitySummary(): Promise<JsonObject> {
    return this.requestObject("/system/security/summary");
  }

  async permissionRules(): Promise<JsonObject> {
    return this.requestObject("/system/permissions/rules");
  }

  async export(workspaceId: string, kind: "zip" | "git-patch" | "deploy-bundle" | "docker-validation-report" | "manifest" | "browser-proof-bundle"): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/export/${kind}`, { method: "POST" });
  }

  async *streamRunEvents(runId: string, options: { pollIntervalMs?: number; timeoutMs?: number } = {}): AsyncGenerator<RunEvent> {
    const started = Date.now();
    const seen = new Set<string>();
    let lastSequence = 0;
    yield { type: "run_started", runId, payload: await this.getRun(runId) };
    while (true) {
      const eventPage = await this.getRunEvents(runId, { afterSequence: lastSequence });
      const runEvents = Array.isArray(eventPage.items) ? eventPage.items : [];
      for (const event of runEvents) {
        if (!isJsonObject(event)) continue;
        lastSequence = Math.max(lastSequence, Number(event.sequence ?? 0));
        const type = String(event.event_type ?? "run.event") as RunEventType;
        yield { type, runId, payload: event };
      }
      const timeline = await this.getTimeline(runId);
      const items = Array.isArray(timeline.items) ? timeline.items : [];
      for (const item of items) {
        if (!isJsonObject(item)) continue;
        const key = String(item.id ?? item.created_at ?? JSON.stringify(item));
        if (seen.has(key)) continue;
        seen.add(key);
        yield { type: timelineEventType(item), runId, payload: item };
      }
      const gate = await this.getGate(runId);
      yield { type: "gate_changed", runId, payload: gate };
      yield { type: "run_state_changed", runId, payload: await this.getRunState(runId) };
      const run = await this.getRun(runId);
      const status = String(run.status ?? "");
      if (["completed", "blocked", "failed", "awaiting_approval"].includes(status)) {
        yield { type: "run_completed", runId, payload: { run, gate } };
        return;
      }
      if (Date.now() - started >= (options.timeoutMs ?? 600_000)) {
        yield { type: "run_stream_timeout", runId, payload: { run, gate } };
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, options.pollIntervalMs ?? 1000));
    }
  }

  private async requestValue(path: string, init: RequestInit = {}): Promise<JsonValue> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
      ...init,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json() as Promise<JsonValue>;
  }

  private async requestObject(path: string, init: RequestInit = {}): Promise<JsonObject> {
    const value = await this.requestValue(path, init);
    if (!isJsonObject(value)) {
      throw new Error(`Expected JSON object from ${path}`);
    }
    return value;
  }
}

function isJsonObject(value: JsonValue): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function timelineEventType(item: JsonObject): RunEventType {
  const kind = String(item.kind ?? "");
  if (kind === "tool") return "tool_event";
  if (kind === "check") return "check_completed";
  if (kind === "browser") return "browser_step";
  if (kind === "repair") return "repair_packet";
  return kind ? "timeline_event" : "timeline_event";
}
