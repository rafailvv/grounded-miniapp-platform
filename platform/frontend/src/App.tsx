import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { ensureWorkspace, openWorkspace, request, SystemConfiguration, Workspace } from "./lib/api";
import "./styles/app.css";

type Job = {
  job_id: string;
  workspace_id: string;
  status: string;
  generation_mode: "quality" | "balanced" | "basic";
  fidelity: "quality_app" | "balanced_app" | "basic_scaffold" | "blocked";
  llm_enabled: boolean;
  llm_provider?: string | null;
  llm_model?: string | null;
  failure_reason?: string | null;
  compile_summary?: Record<string, number | string>;
  summary?: string | null;
  assumptions_report: Array<{ text: string; rationale: string }>;
  validation_snapshot?: {
    grounded_spec_valid: boolean;
    app_ir_valid: boolean;
    build_valid: boolean;
    blocking: boolean;
    issues: Array<{ code: string; message: string; severity?: string }>;
  } | null;
};

type ChatTurn = {
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  summary?: string | null;
};

type FileEntry = {
  path: string;
  type: "file" | "directory";
};

type FileTreeNode = {
  name: string;
  path: string;
  type: "file" | "directory";
  children: FileTreeNode[];
};

type PreviewInfo = {
  url: string | null;
  role_urls?: Record<string, string>;
  runtime_mode?: string;
  status?: string;
};

const PREVIEW_BOOT_ROLES = {
  client: true,
  specialist: true,
  manager: true,
} as const;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

type WorkspaceLogs = {
  workspace_id: string;
  job: Job | null;
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

const INITIAL_PROMPT =
  "Build a consultation booking form mini-app with name, phone, preferred date and comment fields.";

function formatLogSection(title: string, lines: string[]): string {
  return [title, ...lines, ""].join("\n");
}

function formatTimestamp(value?: string): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function buildFileTree(entries: FileEntry[]): FileTreeNode[] {
  const root = new Map<string, FileTreeNode>();

  for (const entry of entries) {
    const parts = entry.path.split("/").filter(Boolean);
    let level = root;
    let currentPath = "";

    parts.forEach((part, index) => {
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      const isLeaf = index === parts.length - 1;
      const nodeType: "file" | "directory" = isLeaf ? entry.type : "directory";
      const existing = level.get(part);

      if (existing) {
        if (isLeaf) {
          existing.type = nodeType;
        }
        level = ensureChildrenMap(existing);
        return;
      }

      const node: FileTreeNode = {
        name: part,
        path: currentPath,
        type: nodeType,
        children: [],
      };
      level.set(part, node);
      level = ensureChildrenMap(node);
    });
  }

  return sortNodes(Array.from(root.values()));
}

function ensureChildrenMap(node: FileTreeNode): Map<string, FileTreeNode> {
  const map = new Map<string, FileTreeNode>();
  node.children.forEach((child) => map.set(child.name, child));
  const originalSet = map.set.bind(map);
  map.set = (key, value) => {
    const result = originalSet(key, value);
    node.children = Array.from(map.values());
    return result;
  };
  return map;
}

function sortNodes(nodes: FileTreeNode[]): FileTreeNode[] {
  return [...nodes]
    .sort((left, right) => {
      if (left.type !== right.type) {
        return left.type === "directory" ? -1 : 1;
      }
      return left.name.localeCompare(right.name);
    })
    .map((node) => ({
      ...node,
      children: sortNodes(node.children),
    }));
}

function collectExpandedDirectories(nodes: FileTreeNode[]): string[] {
  const expanded: string[] = [];
  const visit = (node: FileTreeNode) => {
    if (node.type === "directory") {
      expanded.push(node.path);
      node.children.forEach(visit);
    }
  };
  nodes.forEach(visit);
  return expanded;
}

type FileTreeProps = {
  nodes: FileTreeNode[];
  expandedPaths: Set<string>;
  selectedPath: string;
  onToggleDirectory: (path: string) => void;
  onSelectFile: (path: string) => void;
  depth?: number;
};

function FileTree({
  nodes,
  expandedPaths,
  selectedPath,
  onToggleDirectory,
  onSelectFile,
  depth = 0,
}: FileTreeProps) {
  return (
    <div className={depth === 0 ? "file-tree" : "file-tree-children"}>
      {nodes.map((node) => {
        const isDirectory = node.type === "directory";
        const isExpanded = expandedPaths.has(node.path);
        const isSelected = selectedPath === node.path;
        return (
          <div key={node.path} className="file-tree-node">
            <button
              type="button"
              className={`tree-row ${isDirectory ? "tree-row-directory" : "tree-row-file"} ${isSelected ? "is-selected" : ""}`}
              style={{ paddingLeft: `${12 + depth * 16}px` }}
              onClick={() => (isDirectory ? onToggleDirectory(node.path) : onSelectFile(node.path))}
            >
              <span className="tree-icon">{isDirectory ? (isExpanded ? "▾" : "▸") : "•"}</span>
              <span className="tree-label">{node.name}</span>
            </button>
            {isDirectory && isExpanded ? (
              <FileTree
                nodes={node.children}
                expandedPaths={expandedPaths}
                selectedPath={selectedPath}
                onToggleDirectory={onToggleDirectory}
                onSelectFile={onSelectFile}
                depth={depth + 1}
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export default function App() {
  const previewRoles = ["client", "specialist", "manager"] as const;
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [prompt, setPrompt] = useState(INITIAL_PROMPT);
  const [job, setJob] = useState<Job | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [fileContent, setFileContent] = useState<string>("");
  const [selectedPath, setSelectedPath] = useState<string>("");
  const [diff, setDiff] = useState<string>("");
  const [validation, setValidation] = useState<Record<string, unknown> | null>(null);
  const [activeTab, setActiveTab] = useState<"preview" | "code" | "diff" | "logs">("preview");
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [rolePreviewUrls, setRolePreviewUrls] = useState<Record<string, string>>({});
  const [generationMode, setGenerationMode] = useState<"quality" | "balanced" | "basic">("quality");
  const [previewRuntimeMode, setPreviewRuntimeMode] = useState<string>("");
  const [previewStatus, setPreviewStatus] = useState<string>("");
  const [previewBooting, setPreviewBooting] = useState(false);
  const [loading, setLoading] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [systemConfig, setSystemConfig] = useState<SystemConfiguration | null>(null);
  const [workspaceLogs, setWorkspaceLogs] = useState<WorkspaceLogs | null>(null);
  const [error, setError] = useState<string>("");
  const [expandedDirectories, setExpandedDirectories] = useState<Set<string>>(new Set());
  const [previewCycle, setPreviewCycle] = useState(0);
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({
    client: false,
    specialist: false,
    manager: false,
  });
  const [previewFailed, setPreviewFailed] = useState<Record<string, boolean>>({
    client: false,
    specialist: false,
    manager: false,
  });
  const previewTimeoutsRef = useRef<Record<string, number | undefined>>({});

  useEffect(() => {
    let isMounted = true;
    const requestedWorkspaceId = new URLSearchParams(window.location.search).get("workspace_id");
    const workspacePromise = requestedWorkspaceId ? openWorkspace(requestedWorkspaceId) : ensureWorkspace();
    Promise.all([workspacePromise, request<SystemConfiguration>("/system/configuration")])
      .then(([nextWorkspace, config]) => {
        if (!isMounted) {
          return;
        }
        setWorkspace(nextWorkspace);
        setSystemConfig(config);
        setGenerationMode(config.defaults.generation_mode);
        const params = new URLSearchParams(window.location.search);
        if (params.get("workspace_id") !== nextWorkspace.workspace_id) {
          params.set("workspace_id", nextWorkspace.workspace_id);
          window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
        }
      })
      .catch((err: Error) => {
        if (!isMounted) {
          return;
        }
        setError(err.message);
      })
      .finally(() => {
        if (isMounted) {
          setInitializing(false);
        }
      });
    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (!workspace) {
      return;
    }
    void (async () => {
      setPreviewBooting(true);
      setPreviewUrl("");
      setRolePreviewUrls({});
      setPreviewStatus("starting");
      setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
      try {
        await request(`/workspaces/${workspace.workspace_id}/preview/start`, { method: "POST" });
        await waitForPreviewResolution(workspace.workspace_id);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to start preview runtime.");
      } finally {
        setPreviewBooting(false);
      }
      await refreshWorkspace(workspace.workspace_id);
    })();
  }, [workspace]);

  useEffect(() => {
    return () => {
      Object.values(previewTimeoutsRef.current).forEach((timeoutId) => {
        if (timeoutId) {
          window.clearTimeout(timeoutId);
        }
      });
    };
  }, []);

  useEffect(() => {
    if (!previewUrl) {
      return;
    }
    previewRoles.forEach((role) => {
      if (previewLoading[role]) {
        armPreviewTimeout(role);
      }
    });
  }, [previewCycle, previewUrl]);

  const changedFiles = useMemo(
    () => files.filter((entry) => entry.type === "file" && entry.path.startsWith("artifacts/")).length,
    [files],
  );
  const fileTree = useMemo(() => buildFileTree(files), [files]);
  const autoNotices = useMemo(() => {
    return (job?.assumptions_report ?? [])
      .filter((assumption) => {
        const text = assumption.text.toLowerCase();
        return (
          text.includes("single-role prompts are expanded") ||
          text.includes("generated backend exposes a default submission api") ||
          text.includes("preserves the client, specialist, and manager roles") ||
          text.includes("linked client-specialist-manager workflow")
        );
      })
      .map((assumption) => ({
        title: assumption.text,
        message: assumption.rationale,
      }));
  }, [job?.assumptions_report]);
  const visibleWarnings = useMemo(() => {
    const issues = (job?.validation_snapshot?.issues ?? []).filter((issue) => {
      const severity = issue.severity ?? "";
      return severity === "critical" || severity === "high";
    });
    const seen = new Set<string>();
    const warningItems = issues
      .filter((issue) => {
        const key = `${issue.code}:${issue.message}`;
        if (seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      })
      .map((issue) => ({
        title: issue.code,
        message: issue.message,
      }));
    if (job?.failure_reason) {
      warningItems.unshift({
        title: "Generation blocked",
        message: job.failure_reason,
      });
    }
    if (error) {
      warningItems.unshift({
        title: "Request failed",
        message: error,
      });
    }
    return warningItems;
  }, [error, job?.failure_reason, job?.validation_snapshot?.issues]);
  const editorStats = useMemo(() => {
    const lines = fileContent ? fileContent.split("\n").length : 0;
    const characters = fileContent.length;
    return { lines, characters };
  }, [fileContent]);
  const logOutput = useMemo(() => {
    const logSource = workspaceLogs?.job ?? job;
    const traceEntries = workspaceLogs?.reports?.trace?.entries ?? [];
    const jobLines = [
      `status: ${logSource?.status ?? "idle"}`,
      `mode: ${logSource?.generation_mode ?? generationMode}`,
      `fidelity: ${logSource?.fidelity ?? "n/a"}`,
      `llm: ${logSource?.llm_model ?? (logSource?.llm_enabled || systemConfig?.llm.enabled ? "configured" : "off")}`,
      `provider: ${logSource?.llm_provider ?? systemConfig?.llm.provider ?? "none"}`,
      `failure_reason: ${logSource?.failure_reason ?? "none"}`,
    ];
    const eventLines =
      workspaceLogs?.events?.length
        ? workspaceLogs.events.flatMap((event) => {
            const details = event.details && Object.keys(event.details).length ? ` | ${JSON.stringify(event.details)}` : "";
            return [`- [${formatTimestamp(event.created_at)}] ${event.event_type}: ${event.message}${details}`];
          })
        : ["- no job events recorded"];
    const traceLines = traceEntries.length
      ? traceEntries.flatMap((entry) => {
          const payload =
            entry.payload && Object.keys(entry.payload).length ? [`  payload: ${JSON.stringify(entry.payload)}`] : [];
          return [`- [${formatTimestamp(entry.created_at)}] ${entry.stage}: ${entry.message}`, ...payload];
        })
      : ["- no execution trace recorded"];
    const previewLines = [
      `status: ${workspaceLogs?.preview?.status ?? "unknown"}`,
      `runtime_mode: ${workspaceLogs?.preview?.runtime_mode ?? "unknown"}`,
      `url: ${workspaceLogs?.preview?.url ?? "none"}`,
      ...(workspaceLogs?.preview?.logs?.length
        ? ["logs:", ...workspaceLogs.preview.logs.map((line) => `- ${line}`)]
        : ["logs: none"]),
    ];
    const reportLines = [
      `validation: ${JSON.stringify(workspaceLogs?.reports?.validation ?? job?.validation_snapshot ?? validation ?? {}, null, 2)}`,
      `spec_summary: ${JSON.stringify(workspaceLogs?.reports?.spec_summary ?? {}, null, 2)}`,
      `ir_summary: ${JSON.stringify(workspaceLogs?.reports?.ir_summary ?? {}, null, 2)}`,
      `artifact_plan: ${JSON.stringify(workspaceLogs?.reports?.artifact_plan ?? {}, null, 2)}`,
      `traceability: ${JSON.stringify(workspaceLogs?.reports?.traceability ?? {}, null, 2)}`,
      `assumptions: ${JSON.stringify(workspaceLogs?.reports?.assumptions ?? {}, null, 2)}`,
    ];
    const sections = [
      formatLogSection("# JOB", jobLines),
      formatLogSection("# EVENTS", eventLines),
      formatLogSection("# EXECUTION TRACE", traceLines),
      formatLogSection("# PREVIEW", previewLines),
      formatLogSection("# REPORTS", reportLines),
    ];
    return sections.join("\n");
  }, [generationMode, job, systemConfig, validation, workspaceLogs]);

  async function refreshWorkspace(workspaceId: string) {
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    setPreviewFailed({
      client: false,
      specialist: false,
      manager: false,
    });
    const [treeResult, turnsResult, validationResult, previewResult, logsResult] = await Promise.allSettled([
      request<FileEntry[]>(`/workspaces/${workspaceId}/files/tree`),
      request<ChatTurn[]>(`/workspaces/${workspaceId}/chat/turns`),
      request<Record<string, unknown> | null>(`/workspaces/${workspaceId}/validation/current`),
      request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`),
      request<WorkspaceLogs>(`/workspaces/${workspaceId}/logs`),
    ]);

    const refreshErrors: string[] = [];

    if (treeResult.status === "fulfilled") {
      setFiles(treeResult.value);
      setExpandedDirectories((current) => {
        if (current.size > 0) {
          return current;
        }
        return new Set(collectExpandedDirectories(buildFileTree(treeResult.value)).slice(0, 8));
      });
    } else {
      refreshErrors.push(`files: ${treeResult.reason instanceof Error ? treeResult.reason.message : "failed to load"}`);
    }

    if (turnsResult.status === "fulfilled") {
      setTurns(turnsResult.value);
    } else {
      refreshErrors.push(`chat: ${turnsResult.reason instanceof Error ? turnsResult.reason.message : "failed to load"}`);
    }

    if (validationResult.status === "fulfilled") {
      setValidation(validationResult.value);
    } else {
      refreshErrors.push(
        `validation: ${validationResult.reason instanceof Error ? validationResult.reason.message : "failed to load"}`,
      );
    }

    let nextPreviewBase = "";
    if (previewResult.status === "fulfilled") {
      const previewPayload = previewResult.value;
      setPreviewUrl(previewPayload.url ?? "");
      setRolePreviewUrls(previewPayload.role_urls ?? {});
      setPreviewRuntimeMode(previewPayload.runtime_mode ?? "");
      setPreviewStatus(previewPayload.status ?? "");
      nextPreviewBase = previewPayload.url ?? "";
    } else {
      setPreviewUrl("");
      setRolePreviewUrls({});
      setPreviewStatus("error");
      refreshErrors.push(`preview: ${previewResult.reason instanceof Error ? previewResult.reason.message : "failed to load"}`);
    }

    if (logsResult.status === "fulfilled") {
      setWorkspaceLogs(logsResult.value);
    } else {
      refreshErrors.push(`logs: ${logsResult.reason instanceof Error ? logsResult.reason.message : "failed to load"}`);
    }

    if (nextPreviewBase) {
      setPreviewCycle((current) => current + 1);
      setPreviewLoading({
        client: true,
        specialist: true,
        manager: true,
      });
    } else {
      setPreviewLoading({
        client: false,
        specialist: false,
        manager: false,
      });
      setPreviewFailed({
        client: previewStatus === "error",
        specialist: previewStatus === "error",
        manager: previewStatus === "error",
      });
    }

    setError(refreshErrors.length ? refreshErrors.join(" | ") : "");
  }

  async function handleGenerate(event: FormEvent) {
    event.preventDefault();
    if (!workspace) {
      return;
    }
    setLoading(true);
    setError("");
    setPreviewBooting(true);
    setPreviewUrl("");
    setRolePreviewUrls({});
    setPreviewStatus("starting");
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    setPreviewFailed({
      client: false,
      specialist: false,
      manager: false,
    });
    try {
      const createdTurn = await request<ChatTurn>(`/workspaces/${workspace.workspace_id}/chat/turns`, {
        method: "POST",
        body: JSON.stringify({ role: "user", content: prompt }),
      });
      setTurns((current) => [...current, createdTurn]);
      const nextJob = await request<Job>(`/workspaces/${workspace.workspace_id}/generate`, {
        method: "POST",
        body: JSON.stringify({
          prompt,
          target_platform: "telegram_mini_app",
          preview_profile: "telegram_mock",
          generation_mode: generationMode,
        }),
      });
      setJob(nextJob);
      if (nextJob.status === "completed") {
        await request(`/workspaces/${workspace.workspace_id}/preview/start`, { method: "POST" });
        await waitForPreviewResolution(workspace.workspace_id);
      }
      await refreshWorkspace(workspace.workspace_id);
      setActiveTab("preview");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed.");
    } finally {
      setPreviewBooting(false);
      setLoading(false);
    }
  }

  async function waitForPreviewResolution(workspaceId: string): Promise<void> {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const preview = await request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`);
      setPreviewRuntimeMode(preview.runtime_mode ?? "");
      setPreviewStatus(preview.status ?? "");
      if (preview.status === "error") {
        setPreviewLoading({
          client: false,
          specialist: false,
          manager: false,
        });
        setPreviewFailed({
          client: true,
          specialist: true,
          manager: true,
        });
        return;
      }
      if (preview.status === "running" && preview.url) {
        return;
      }
      await sleep(1000);
    }
    setPreviewStatus("error");
    setPreviewLoading({
      client: false,
      specialist: false,
      manager: false,
    });
    setPreviewFailed({
      client: true,
      specialist: true,
      manager: true,
    });
    setError("preview: runtime startup timed out");
  }

  function armPreviewTimeout(role: "client" | "specialist" | "manager") {
    const existing = previewTimeoutsRef.current[role];
    if (existing) {
      window.clearTimeout(existing);
    }
    previewTimeoutsRef.current[role] = window.setTimeout(() => {
      setPreviewLoading((current) => ({ ...current, [role]: false }));
      setPreviewFailed((current) => ({ ...current, [role]: true }));
    }, 12000);
  }

  function clearPreviewTimeout(role: "client" | "specialist" | "manager") {
    const existing = previewTimeoutsRef.current[role];
    if (existing) {
      window.clearTimeout(existing);
      previewTimeoutsRef.current[role] = undefined;
    }
  }

  async function handleSelectFile(path: string) {
    if (!workspace) {
      return;
    }
    setSelectedPath(path);
    const payload = await request<{ content: string }>(
      `/workspaces/${workspace.workspace_id}/files/content?path=${encodeURIComponent(path)}`,
    );
    setFileContent(payload.content);
    setActiveTab("code");
  }

  async function handleSaveFile() {
    if (!workspace || !selectedPath) {
      return;
    }
    await request(`/workspaces/${workspace.workspace_id}/files/save`, {
      method: "POST",
      body: JSON.stringify({ relative_path: selectedPath, content: fileContent }),
    });
    const diffPayload = await request<{ diff: string }>(`/workspaces/${workspace.workspace_id}/diff`);
    setDiff(diffPayload.diff);
    await refreshWorkspace(workspace.workspace_id);
    setActiveTab("diff");
  }

  async function handleShowDiff() {
    if (!workspace) {
      return;
    }
    const diffPayload = await request<{ diff: string }>(`/workspaces/${workspace.workspace_id}/diff`);
    setDiff(diffPayload.diff);
    setActiveTab("diff");
  }

  async function handleResetPreview() {
    if (!workspace) {
      return;
    }
    await request(`/workspaces/${workspace.workspace_id}/preview/reset`, { method: "POST" });
    await refreshWorkspace(workspace.workspace_id);
  }

  function toggleDirectory(path: string) {
    setExpandedDirectories((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  return (
    <div className="page">
      <div className="topbar">
        <div className="topbar-title">
          <p className="eyebrow">Grounded Mini-App Platform</p>
          <h1 className="topbar-heading">AI module for Generating Mini-Applications</h1>
        </div>
        <div className="topbar-meta">
          <div className="status-pill">{job?.status ?? "idle"}</div>
          <div className="artifact-strip">
            <div className="artifact-chip">
              <span>Mode</span>
              <strong>{job?.generation_mode ?? generationMode}</strong>
              <div className="artifact-popover">
                <p>Current generation mode and default pipeline profile.</p>
                <strong>{job?.fidelity ?? "pending"}</strong>
              </div>
            </div>
            <div className="artifact-chip">
              <span>Changed</span>
              <strong>{changedFiles}</strong>
              <div className="artifact-popover">
                <p>Compiled artifact files currently present in the workspace.</p>
                <strong>{changedFiles} files</strong>
              </div>
            </div>
            <div className="artifact-chip">
              <span>Validators</span>
              <strong>
                {String(job?.validation_snapshot?.grounded_spec_valid ?? false)} /{" "}
                {String(job?.validation_snapshot?.app_ir_valid ?? false)}
              </strong>
              <div className="artifact-popover">
                <pre>{JSON.stringify(validation ?? job?.validation_snapshot ?? {}, null, 2)}</pre>
              </div>
            </div>
            <div className="artifact-chip">
              <span>LLM</span>
              <strong>
                {job?.llm_model ??
                  (job?.llm_enabled
                    ? "configured"
                    : systemConfig?.llm.enabled
                      ? "configured"
                      : initializing
                        ? "checking"
                        : "off")}
              </strong>
              <div className="artifact-popover">
                <p>
                  {job?.llm_enabled || systemConfig?.llm.enabled
                    ? "OpenRouter-backed generation path is configured."
                    : initializing
                      ? "Startup configuration is still being checked."
                      : "OpenRouter is not configured for this run."}
                </p>
                <strong>{job?.llm_provider ?? systemConfig?.llm.provider ?? "no provider"}</strong>
              </div>
            </div>
            <div className="artifact-chip">
              <span>Assumptions</span>
              <strong>{visibleWarnings.length}</strong>
              <div className="artifact-popover">
                {visibleWarnings.length ? (
                  visibleWarnings.map((warning, index) => (
                    <div key={`${warning.title}-${index}`} className="artifact-popover-item">
                      <strong>{warning.title}</strong>
                      <p>{warning.message}</p>
                    </div>
                  ))
                ) : (
                  <p>No blocking warnings.</p>
                )}
              </div>
            </div>
            <div className="artifact-chip">
              <span>Compile</span>
              <strong>
                {String(job?.compile_summary?.screen_count ?? 0)} / {String(job?.compile_summary?.route_count ?? 0)}
              </strong>
              <div className="artifact-popover">
                <p>Screens and routes compiled into the generated runtime.</p>
                <pre>{JSON.stringify(job?.compile_summary ?? {}, null, 2)}</pre>
              </div>
            </div>
            <div className="artifact-chip">
              <span>Files</span>
              <strong>{files.filter((entry) => entry.type === "file").length}</strong>
              <div className="artifact-popover">
                {files
                  .filter((entry) => entry.type === "file")
                  .slice(0, 14)
                  .map((entry) => (
                    <button
                      key={entry.path}
                      type="button"
                      className="artifact-file"
                      onClick={() => handleSelectFile(entry.path)}
                    >
                      {entry.path}
                    </button>
                  ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="layout">
        <section className="panel panel-chat">
          <header>
            <h2>Chat / Prompt</h2>
            <p>Prompt turns, grounded summary, and manual iteration.</p>
          </header>
          <form onSubmit={handleGenerate} className="prompt-form">
            <label className="generation-mode-field">
              <span>Generation mode</span>
              <select value={generationMode} onChange={(event) => setGenerationMode(event.target.value as typeof generationMode)}>
                <option value="quality">quality</option>
                <option value="balanced">balanced</option>
                <option value="basic">basic</option>
              </select>
            </label>
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={8} />
            <div className="actions">
              <button type="submit" disabled={initializing || loading || !workspace}>
                {initializing ? "Preparing..." : loading ? "Generating..." : "Generate"}
              </button>
              <button type="button" className="ghost" onClick={handleShowDiff} disabled={!workspace}>
                Show Diff
              </button>
            </div>
          </form>
          <div className="turns">
            {turns.map((turn) => (
              <article key={turn.turn_id} className={`turn turn-${turn.role}`}>
                <span>{turn.role}</span>
                <p>{turn.summary ?? turn.content}</p>
              </article>
            ))}
          </div>
          {autoNotices.length ? (
            <div className="auto-notices">
              <h3>Auto-completed</h3>
              {autoNotices.map((notice, index) => (
                <div key={`${notice.title}-${index}`} className="notice-card">
                  <strong>{notice.title}</strong>
                  <p>{notice.message}</p>
                </div>
              ))}
            </div>
          ) : null}
          <div className="warnings">
            <h3>Warnings</h3>
            {visibleWarnings.length ? (
              visibleWarnings.map((warning, index) => (
                <div key={`${warning.title}-${index}`} className="warning-card">
                  <strong>{warning.title}</strong>
                  <p>{warning.message}</p>
                </div>
              ))
            ) : (
              <p className="muted">No blocking warnings.</p>
            )}
          </div>
        </section>

        <section className="panel panel-preview">
          <div className="preview-toolbar">
            <div className="preview-toolbar-main">
              <header>
                <h2>Runtime Lab</h2>
              </header>
              <div className="tabs">
                {(["preview", "code", "diff", "logs"] as const).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    className={tab === activeTab ? "active" : ""}
                    onClick={() => setActiveTab(tab)}
                  >
                    {tab}
                  </button>
                ))}
              </div>
            </div>
            <button type="button" className="ghost" onClick={handleResetPreview}>
              Reset all sessions
            </button>
          </div>
          {activeTab === "preview" ? (
            <div className="preview-grid">
              {previewRoles.map((role) => (
                <div key={role} className="preview-column">
                  <div className="preview-heading">
                    <strong>{role}</strong>
                    <span>
                      {(role === "client" ? "Role 1" : role === "specialist" ? "Role 2" : "Role 3")} ·{" "}
                      {previewRuntimeMode || "runtime"}
                    </span>
                  </div>
                  <div className="phone-shell">
                    {previewUrl ? (
                      <>
                        {previewLoading[role] ? (
                          <div className="preview-loader">
                            <div className="preview-loader-spinner" />
                            <p>Loading runtime…</p>
                          </div>
                        ) : null}
                        <iframe
                          key={`${role}-${previewCycle}-${rolePreviewUrls[role] ?? previewUrl}`}
                          title={`Live preview ${role}`}
                          src={rolePreviewUrls[role] ?? `${previewUrl}?role=${role}`}
                          className={previewLoading[role] ? "is-loading" : ""}
                          onLoad={() =>
                            window.setTimeout(() => {
                              clearPreviewTimeout(role);
                              setPreviewFailed((current) => ({
                                ...current,
                                [role]: false,
                              }));
                              setPreviewLoading((current) => ({
                                ...current,
                                [role]: false,
                              }));
                            }, 350)
                          }
                        />
                      </>
                    ) : previewBooting || previewLoading[role] || loading || initializing || previewStatus === "starting" ? (
                      <div className="preview-loader">
                        <div className="preview-loader-spinner" />
                        <p>Loading runtime…</p>
                      </div>
                    ) : previewFailed[role] ? (
                      <div className="placeholder placeholder-error">
                        <strong>Failed to load preview.</strong>
                        <p>This role runtime did not open in time.</p>
                      </div>
                    ) : previewStatus === "error" ? (
                      <div className="placeholder placeholder-error">
                        <strong>Failed to load preview.</strong>
                        <p>Open the logs tab to see the runtime startup error.</p>
                      </div>
                    ) : (
                      <div className="placeholder">Generate a workspace artifact to populate the preview.</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          {activeTab === "code" ? (
            <div className="code-layout">
              <div className="code-files">
                <FileTree
                  nodes={fileTree}
                  expandedPaths={expandedDirectories}
                  selectedPath={selectedPath}
                  onToggleDirectory={toggleDirectory}
                  onSelectFile={handleSelectFile}
                />
              </div>
              <div className="editor">
                <div className="editor-header">
                  <div className="editor-title-wrap">
                    <strong>{selectedPath || "Select a file"}</strong>
                    <span className="editor-subtitle">
                      {selectedPath ? `${editorStats.lines} lines • ${editorStats.characters} chars` : "Open a file from the tree"}
                    </span>
                  </div>
                  <div className="editor-actions">
                    <button type="button" onClick={handleSaveFile} disabled={!selectedPath}>
                      Save
                    </button>
                  </div>
                </div>
                <div className="editor-surface">
                  <textarea
                    className="edit-area"
                    value={fileContent}
                    onChange={(event) => setFileContent(event.target.value)}
                    rows={24}
                    spellCheck={false}
                  />
                </div>
              </div>
            </div>
          ) : null}
          {activeTab === "diff" ? (
            <div className="terminal">
              <pre>{diff || "No diff yet."}</pre>
            </div>
          ) : null}
          {activeTab === "logs" ? (
            <div className="terminal">
              <pre>{logOutput}</pre>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
