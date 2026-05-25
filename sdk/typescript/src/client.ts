export type GroundedClientOptions = {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  defaultHeaders?: HeadersInit;
};

export type RequestOptions = {
  idempotencyKey?: string;
  headers?: HeadersInit;
};

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type RunEventType = string;

export type RunRecordJson = JsonObject & {
  run_id?: string;
  workspace_id?: string;
  status?: string;
  prompt?: string;
  created_at?: string;
  updated_at?: string;
};

export type RunEventV2Json = JsonObject & {
  event_id?: string;
  workspace_id?: string;
  run_id?: string;
  sequence?: number;
  event_type?: string;
  actor?: string;
  payload_ref?: string;
  payload_sha256?: string;
  summary?: string;
  source_ref?: string;
  created_at?: string;
};

export type EventJournalPageJson = JsonObject & {
  scope?: string;
  run_id?: string;
  thread_id?: string;
  next_sequence?: number;
  items?: RunEventV2Json[];
};

export type RunJournalStateJson = JsonObject & {
  schema?: string;
  run_id?: string;
  status_timeline?: JsonObject[];
  tool_events?: JsonObject[];
  checks?: JsonObject[];
  repair?: JsonObject;
  protocol_refs?: JsonObject[];
};

export type ArtifactIndexJson = JsonObject & {
  schema?: string;
  run_id?: string;
  items?: JsonObject[];
};

export type CheckReportJson = JsonObject & {
  items?: JsonObject[];
  status?: string;
  blocking?: boolean;
};

export type PreviewUrlJson = JsonObject & {
  url?: string | null;
  role_urls?: Record<string, string>;
  runtime_mode?: string;
  status?: string;
  stage?: string;
  progress_percent?: number;
  draft_run_id?: string | null;
  latency_breakdown?: JsonObject;
  last_error?: string | null;
};

export type WebhookCreatePayload = JsonObject & {
  url: string;
  events?: string[];
  workspace_id?: string | null;
  enabled?: boolean;
  description?: string | null;
  metadata?: JsonObject;
  secret?: string | null;
};

export type WebhookSubscriptionJson = JsonObject & {
  schema?: string;
  webhook_id?: string;
  url?: string;
  events?: string[];
  workspace_id?: string | null;
  enabled?: boolean;
  description?: string | null;
  metadata?: JsonObject;
  secret_configured?: boolean;
  last_delivery?: JsonObject | null;
  created_at?: string;
  updated_at?: string;
};

export type RunEvent = {
  type: RunEventType;
  runId: string;
  payload: JsonObject;
  sequence?: number;
  source?: "journal" | "timeline" | "state" | "legacy" | "synthetic";
};

export type StreamRunOptions = {
  pollIntervalMs?: number;
  timeoutMs?: number;
  includePayloads?: boolean;
};

const TERMINAL_RUN_STATUSES = new Set(["completed", "blocked", "failed", "awaiting_approval"]);

export class GroundedClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly defaultHeaders?: HeadersInit;

  constructor(options: GroundedClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://127.0.0.1:8000").replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.defaultHeaders = options.defaultHeaders;
  }

  async listWorkspaces(): Promise<JsonValue> {
    return this.requestValue("/workspaces");
  }

  async createWorkspace(payload: JsonObject, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject("/workspaces", { method: "POST", body: JSON.stringify(payload) }, options);
  }

  async createRun(workspaceId: string, payload: JsonObject, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/runs`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, options) as Promise<RunRecordJson>;
  }

  async listRuns(workspaceId: string): Promise<RunRecordJson[]> {
    const value = await this.requestValue(`/workspaces/${encodeURIComponent(workspaceId)}/runs`);
    if (!Array.isArray(value)) throw new Error("Expected JSON list from listRuns");
    return value.filter(isJsonObject) as RunRecordJson[];
  }

  async listThreads(options: { workspaceId?: string; includeArchived?: boolean; limit?: number } = {}): Promise<JsonObject> {
    const params = new URLSearchParams({
      include_archived: String(options.includeArchived ?? false),
      limit: String(options.limit ?? 50),
    });
    if (options.workspaceId) params.set("workspace_id", options.workspaceId);
    return this.requestObject(`/threads?${params.toString()}`);
  }

  async startThread(workspaceId: string, options: { title?: string; metadata?: JsonObject } = {}, requestOptions: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject("/threads", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, title: options.title ?? "", metadata: options.metadata ?? {} }),
    }, requestOptions);
  }

  async getThread(threadId: string): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}`);
  }

  async getThreadSnapshot(threadId: string): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/snapshot`);
  }

  async resumeThread(threadId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/resume`, { method: "POST" }, options);
  }

  async startTurn(threadId: string, payload: JsonObject, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/turns`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, options);
  }

  async getThreadEventsV2(threadId: string, options: { afterSequence?: number; limit?: number } = {}): Promise<EventJournalPageJson> {
    const params = new URLSearchParams({
      after_sequence: String(options.afterSequence ?? 0),
      limit: String(options.limit ?? 500),
    });
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/events-v2?${params.toString()}`) as Promise<EventJournalPageJson>;
  }

  async getThreadJournalState(threadId: string): Promise<JsonObject> {
    return this.requestObject(`/threads/${encodeURIComponent(threadId)}/journal/state`);
  }

  async getRun(runId: string): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}`) as Promise<RunRecordJson>;
  }

  async getTimeline(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/timeline`);
  }

  async getTraceView(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/trace-view`);
  }

  async getTraceBundle(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/trace-bundle`);
  }

  async getTraceBundleState(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/trace-bundle/state`);
  }

  async getRunEvents(runId: string, options: { afterSequence?: number; limit?: number } = {}): Promise<JsonObject> {
    const params = new URLSearchParams({
      after_sequence: String(options.afterSequence ?? 0),
      limit: String(options.limit ?? 500),
    });
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/events?${params.toString()}`);
  }

  async getRunEventsV2(runId: string, options: { afterSequence?: number; limit?: number } = {}): Promise<EventJournalPageJson> {
    const params = new URLSearchParams({
      after_sequence: String(options.afterSequence ?? 0),
      limit: String(options.limit ?? 500),
    });
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/events-v2?${params.toString()}`) as Promise<EventJournalPageJson>;
  }

  async getRunJournalState(runId: string): Promise<RunJournalStateJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/journal/state`) as Promise<RunJournalStateJson>;
  }

  async getEventPayload(payloadRef: string): Promise<JsonObject> {
    return this.requestObject(`/event-payloads/${encodeURIComponent(payloadRef)}`);
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

  async resumeRun(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/resume`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async resumeFromBookmark(runId: string, bookmarkId: string, options: { prompt?: string } = {}, requestOptions: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/resume-from-bookmark`, {
      method: "POST",
      body: JSON.stringify({ bookmark_id: bookmarkId, prompt: options.prompt ?? null }),
    }, requestOptions);
  }

  async forkFromBookmark(runId: string, bookmarkId: string, options: { prompt?: string } = {}, requestOptions: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/fork-from-bookmark`, {
      method: "POST",
      body: JSON.stringify({ bookmark_id: bookmarkId, prompt: options.prompt ?? null }),
    }, requestOptions);
  }

  async applyRun(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/apply`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async discardRun(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/discard`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async stopRun(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async rollbackRun(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/rollback`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async getArtifacts(runId: string): Promise<ArtifactIndexJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/artifacts`) as Promise<ArtifactIndexJson>;
  }

  async getArtifact(runId: string, artifactRef: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactRef)}`);
  }

  async getOutputArtifacts(runId: string): Promise<ArtifactIndexJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/output-artifacts`) as Promise<ArtifactIndexJson>;
  }

  async getOutputArtifact(runId: string, artifactId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/output-artifacts/${encodeURIComponent(artifactId)}`);
  }

  async getMicrocompact(runId: string, digest: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/microcompact/${encodeURIComponent(digest)}`);
  }

  async getApprovals(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/approvals`);
  }

  async approve(runId: string, approvalId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/approve`, { method: "POST" }, options);
  }

  async rejectApproval(runId: string, approvalId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/reject`, { method: "POST" }, options);
  }

  async getChecks(runId: string): Promise<CheckReportJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/checks`) as Promise<CheckReportJson>;
  }

  async getTestMatrix(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/test-matrix`);
  }

  async getAcceptanceScenarios(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/acceptance-scenarios`);
  }

  async getBrowserProof(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/browser-proof`);
  }

  async startBrowserProof(runId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/browser-proof`, { method: "POST" }, options);
  }

  async getVisualQa(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/visual-qa`);
  }

  async getValidationCurrent(workspaceId: string): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/validation/current`);
  }

  async runValidation(workspaceId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/validation/run`, { method: "POST" }, options);
  }

  async getPreviewUrl(workspaceId: string): Promise<PreviewUrlJson> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/url`) as Promise<PreviewUrlJson>;
  }

  async ensurePreview(workspaceId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/ensure`, { method: "POST" }, options);
  }

  async startPreview(workspaceId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/start`, { method: "POST" }, options);
  }

  async rebuildPreview(workspaceId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/rebuild`, { method: "POST" }, options);
  }

  async resetPreview(workspaceId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/reset`, { method: "POST" }, options);
  }

  async getPreviewLogs(workspaceId: string): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/preview/logs`);
  }

  async getWorkspaceLogs(workspaceId: string): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/logs`);
  }

  async getReview(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/review`);
  }

  async startReview(runId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/review`, { method: "POST" }, options);
  }

  async reviewFix(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/review/fix`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async getRepairCases(runId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/repair-cases`);
  }

  async getRepairCase(runId: string, caseId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/repair-cases/${encodeURIComponent(caseId)}`);
  }

  async getRepairAttempts(runId: string, caseId: string): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/repair-cases/${encodeURIComponent(caseId)}/attempts`);
  }

  async retryRepairCase(runId: string, caseId: string, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/repair-cases/${encodeURIComponent(caseId)}/retry`, { method: "POST" }, options);
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

  async patchPreflight(workspaceId: string, payload: JsonObject, options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/patch/preflight`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, options);
  }

  async stageFiles(runId: string, files: string[], options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/stage/files`, {
      method: "POST",
      body: JSON.stringify({ files }),
    }, options);
  }

  async discardFiles(runId: string, files: string[], options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/discard/files`, {
      method: "POST",
      body: JSON.stringify({ files }),
    }, options);
  }

  async applyStaged(runId: string, options: RequestOptions = {}): Promise<RunRecordJson> {
    return this.requestObject(`/runs/${encodeURIComponent(runId)}/apply/staged`, { method: "POST" }, options) as Promise<RunRecordJson>;
  }

  async listWebhooks(options: { workspaceId?: string } = {}): Promise<JsonObject> {
    const suffix = options.workspaceId ? `?${new URLSearchParams({ workspace_id: options.workspaceId }).toString()}` : "";
    return this.requestObject(`/webhooks${suffix}`);
  }

  async createWebhook(payload: WebhookCreatePayload, options: RequestOptions = {}): Promise<WebhookSubscriptionJson> {
    return this.requestObject("/webhooks", { method: "POST", body: JSON.stringify(payload) }, options) as Promise<WebhookSubscriptionJson>;
  }

  async getWebhook(webhookId: string): Promise<WebhookSubscriptionJson> {
    return this.requestObject(`/webhooks/${encodeURIComponent(webhookId)}`) as Promise<WebhookSubscriptionJson>;
  }

  async updateWebhook(webhookId: string, payload: JsonObject): Promise<WebhookSubscriptionJson> {
    return this.requestObject(`/webhooks/${encodeURIComponent(webhookId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }) as Promise<WebhookSubscriptionJson>;
  }

  async deleteWebhook(webhookId: string): Promise<JsonObject> {
    return this.requestObject(`/webhooks/${encodeURIComponent(webhookId)}`, { method: "DELETE" });
  }

  async testWebhook(webhookId: string, options: { eventType?: string; payload?: JsonObject } = {}): Promise<JsonObject> {
    return this.requestObject(`/webhooks/${encodeURIComponent(webhookId)}/test`, {
      method: "POST",
      body: JSON.stringify({ event_type: options.eventType ?? "webhook.test", payload: options.payload ?? {} }),
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

  async systemSchema(): Promise<JsonObject> {
    return this.requestObject("/system/schema");
  }

  async export(workspaceId: string, kind: "zip" | "git-patch" | "deploy-bundle" | "docker-validation-report" | "manifest" | "browser-proof-bundle", options: RequestOptions = {}): Promise<JsonObject> {
    return this.requestObject(`/workspaces/${encodeURIComponent(workspaceId)}/export/${kind}`, { method: "POST" }, options);
  }

  async *streamRun(runId: string, options: StreamRunOptions = {}): AsyncGenerator<RunEvent> {
    const started = Date.now();
    let lastSequence = 0;
    yield { type: "run_started", runId, payload: await this.getRun(runId), source: "synthetic" };
    while (true) {
      const eventPage = await this.getRunEventsV2(runId, { afterSequence: lastSequence });
      const runEvents = Array.isArray(eventPage.items) ? eventPage.items : [];
      for (const event of runEvents) {
        if (!isJsonObject(event)) continue;
        const sequence = Number(event.sequence ?? 0);
        lastSequence = Math.max(lastSequence, sequence);
        const payload: JsonObject = { ...event };
        if (options.includePayloads && typeof payload.payload_ref === "string") {
          payload.payload = await this.getEventPayload(payload.payload_ref);
        }
        yield { type: String(event.event_type ?? "run.event"), runId, payload, sequence, source: "journal" };
      }
      const run = await this.getRun(runId);
      const status = String(run.status ?? "");
      if (TERMINAL_RUN_STATUSES.has(status)) {
        yield { type: "run_completed", runId, payload: { run }, source: "synthetic" };
        return;
      }
      if (Date.now() - started >= (options.timeoutMs ?? 600_000)) {
        yield { type: "run_stream_timeout", runId, payload: { run }, source: "synthetic" };
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, options.pollIntervalMs ?? 1000));
    }
  }

  async *streamRunEvents(runId: string, options: { pollIntervalMs?: number; timeoutMs?: number } = {}): AsyncGenerator<RunEvent> {
    const started = Date.now();
    const seen = new Set<string>();
    let lastSequence = 0;
    yield { type: "run_started", runId, payload: await this.getRun(runId), source: "legacy" };
    while (true) {
      const eventPage = await this.getRunEvents(runId, { afterSequence: lastSequence });
      const runEvents = Array.isArray(eventPage.items) ? eventPage.items : [];
      for (const event of runEvents) {
        if (!isJsonObject(event)) continue;
        lastSequence = Math.max(lastSequence, Number(event.sequence ?? 0));
        const type = String(event.event_type ?? "run.event");
        yield { type, runId, payload: event, sequence: lastSequence, source: "legacy" };
      }
      const timeline = await this.getTimeline(runId);
      const items = Array.isArray(timeline.items) ? timeline.items : [];
      for (const item of items) {
        if (!isJsonObject(item)) continue;
        const key = String(item.id ?? item.created_at ?? JSON.stringify(item));
        if (seen.has(key)) continue;
        seen.add(key);
        yield { type: timelineEventType(item), runId, payload: item, source: "timeline" };
      }
      const gate = await this.getGate(runId);
      yield { type: "gate_changed", runId, payload: gate, source: "state" };
      yield { type: "run_state_changed", runId, payload: await this.getRunState(runId), source: "state" };
      const run = await this.getRun(runId);
      const status = String(run.status ?? "");
      if (TERMINAL_RUN_STATUSES.has(status)) {
        yield { type: "run_completed", runId, payload: { run, gate }, source: "synthetic" };
        return;
      }
      if (Date.now() - started >= (options.timeoutMs ?? 600_000)) {
        yield { type: "run_stream_timeout", runId, payload: { run, gate }, source: "synthetic" };
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, options.pollIntervalMs ?? 1000));
    }
  }

  private async requestValue(path: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<JsonValue> {
    const { headers: initHeaders, ...rest } = init;
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      ...rest,
      headers: this.headers(initHeaders, options),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const text = await response.text();
    return text ? JSON.parse(text) as JsonValue : {};
  }

  private async requestObject(path: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<JsonObject> {
    const value = await this.requestValue(path, init, options);
    if (!isJsonObject(value)) {
      throw new Error(`Expected JSON object from ${path}`);
    }
    return value;
  }

  private headers(initHeaders: HeadersInit | undefined, options: RequestOptions): Headers {
    const headers = new Headers({ "Content-Type": "application/json" });
    appendHeaders(headers, this.defaultHeaders);
    appendHeaders(headers, initHeaders);
    appendHeaders(headers, options.headers);
    if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
    return headers;
  }
}

function appendHeaders(target: Headers, source?: HeadersInit): void {
  if (!source) return;
  new Headers(source).forEach((value, key) => target.set(key, value));
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
  return kind || "timeline_event";
}
