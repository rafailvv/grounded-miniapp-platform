import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { deleteWorkspace, ensureWorkspace, listWorkspaces, openWorkspace, request, SystemConfiguration, Workspace } from "./lib/api";
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
type RoleKey = keyof typeof PREVIEW_BOOT_ROLES;

const ROLE_ORDER: RoleKey[] = ["client", "specialist", "manager"];
const ROLE_LABELS: Record<RoleKey, string> = {
  client: "Client",
  specialist: "Specialist",
  manager: "Manager",
};
const ROLE_PLACEHOLDERS: Record<RoleKey, string> = {
  client: "What should the client be able to do in the app?",
  specialist: "What workflow should the specialist have?",
  manager: "What control and analytics should the manager have?",
};
const EMPTY_ROLE_REQUIREMENTS: Record<RoleKey, string> = {
  client: "",
  specialist: "",
  manager: "",
};

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

function buildBusinessPrompt(
  roleRequirements: Record<RoleKey, string>,
  previousRoleRequirements: Record<RoleKey, string>,
  hasExistingBuild: boolean,
): string {
  const lines = [
    "Build a production-ready Telegram mini-app for a real business use case.",
    "Deliver a complete app flow with robust UX, clear validation, and actionable business value.",
    "Avoid demo scaffolding and generic placeholders unless explicitly requested.",
    "",
    "Role requirements and update policy:",
  ];

  ROLE_ORDER.forEach((role) => {
    const current = roleRequirements[role].trim();
    const previous = previousRoleRequirements[role].trim();
    if (!current) {
      lines.push(`- ${ROLE_LABELS[role]}: no new requirement provided; keep this role unchanged.`);
      return;
    }
    lines.push(`- ${ROLE_LABELS[role]} requirement: ${current}`);
    lines.push(
      previous
        ? `- ${ROLE_LABELS[role]}: refine and extend the existing flow based on this updated requirement.`
        : `- ${ROLE_LABELS[role]}: create this flow from scratch with full business-ready behavior.`,
    );
  });

  if (!hasExistingBuild) {
    lines.push(
      "- If a role has no requirement and no existing flow yet, keep it minimal and avoid inventing unsupported functionality.",
    );
  }

  lines.push("");
  lines.push("Quality bar:");
  lines.push("- Real-world forms, status handling, validation, and error states.");
  lines.push("- Consistent navigation between client, specialist, and manager roles.");
  lines.push("- Production-oriented structure and maintainable code artifacts.");
  return lines.join("\n");
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
  const previewRoles = ROLE_ORDER;
  const generationMode = "quality" as const;
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const [workspaceTransitioning, setWorkspaceTransitioning] = useState(true);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [deletingWorkspaceId, setDeletingWorkspaceId] = useState<string>("");
  const [roleRequirements, setRoleRequirements] = useState<Record<RoleKey, string>>({ ...EMPTY_ROLE_REQUIREMENTS });
  const [submittedRoleRequirements, setSubmittedRoleRequirements] = useState<Record<RoleKey, string>>({
    ...EMPTY_ROLE_REQUIREMENTS,
  });
  const [job, setJob] = useState<Job | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [fileContent, setFileContent] = useState<string>("");
  const [selectedPath, setSelectedPath] = useState<string>("");
  const [diff, setDiff] = useState<string>("");
  const [validation, setValidation] = useState<Record<string, unknown> | null>(null);
  const [activeTab, setActiveTab] = useState<"preview" | "code" | "diff" | "logs">("preview");
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [rolePreviewUrls, setRolePreviewUrls] = useState<Record<string, string>>({});
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
  const [rolePreviewCycle, setRolePreviewCycle] = useState<Record<string, number>>({
    client: 0,
    specialist: 0,
    manager: 0,
  });
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
    void (async () => {
      try {
        const config = await request<SystemConfiguration>("/system/configuration");
        if (!isMounted) {
          return;
        }
        setSystemConfig(config);

        const requestedWorkspaceId = new URLSearchParams(window.location.search).get("workspace_id");
        const listedWorkspaces = await listWorkspaces();

        let nextWorkspace: Workspace | null = null;
        if (requestedWorkspaceId) {
          try {
            nextWorkspace = await openWorkspace(requestedWorkspaceId);
          } catch {
            nextWorkspace = null;
          }
        }
        if (!nextWorkspace && listedWorkspaces.length > 0) {
          nextWorkspace = await openWorkspace(listedWorkspaces[0].workspace_id);
        }
        if (!nextWorkspace) {
          nextWorkspace = await ensureWorkspace();
        }
        if (!isMounted) {
          return;
        }
        const refreshedWorkspaces = await listWorkspaces();
        setWorkspaces(refreshedWorkspaces);
        setWorkspace(nextWorkspace);
        const params = new URLSearchParams(window.location.search);
        if (params.get("workspace_id") !== nextWorkspace.workspace_id) {
          params.set("workspace_id", nextWorkspace.workspace_id);
          window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
        }
      } catch (err) {
        if (!isMounted) {
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to initialize workspace.");
      } finally {
        if (isMounted) {
          setInitializing(false);
        }
      }
    })();
    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (!workspace) {
      return;
    }
    void (async () => {
      setWorkspaceTransitioning(true);
      try {
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
      } finally {
        setWorkspaceTransitioning(false);
      }
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
  const filteredWorkspaces = useMemo(() => {
    const query = workspaceSearch.trim().toLowerCase();
    if (!query) {
      return workspaces;
    }
    return workspaces.filter((item) => item.workspace_id.toLowerCase().includes(query));
  }, [workspaceSearch, workspaces]);
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
  }, [job, systemConfig, validation, workspaceLogs]);

  async function refreshWorkspace(workspaceId: string) {
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    setPreviewFailed({
      client: false,
      specialist: false,
      manager: false,
    });
    const [treeResult, validationResult, previewResult, logsResult] = await Promise.allSettled([
      request<FileEntry[]>(`/workspaces/${workspaceId}/files/tree`),
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
    const normalizedRequirements = ROLE_ORDER.reduce(
      (acc, role) => ({ ...acc, [role]: roleRequirements[role].trim() }),
      { ...EMPTY_ROLE_REQUIREMENTS },
    );
    const hasAnyInput = ROLE_ORDER.some((role) => normalizedRequirements[role].length > 0);
    if (!hasAnyInput) {
      setError("Fill at least one role requirement to run generation.");
      return;
    }
    const hasExistingBuild = files.some((entry) => entry.type === "file" && entry.path.startsWith("artifacts/"));
    const composedPrompt = buildBusinessPrompt(normalizedRequirements, submittedRoleRequirements, hasExistingBuild);

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
      const nextJob = await request<Job>(`/workspaces/${workspace.workspace_id}/generate`, {
        method: "POST",
        body: JSON.stringify({
          prompt: composedPrompt,
          target_platform: "telegram_mini_app",
          preview_profile: "telegram_mock",
          generation_mode: generationMode,
        }),
      });
      setJob(nextJob);
      setSubmittedRoleRequirements(normalizedRequirements);
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

  async function refreshWorkspaceList(activeWorkspaceId?: string) {
    const listed = await listWorkspaces();
    setWorkspaces(listed);
    if (!activeWorkspaceId) {
      return;
    }
    const params = new URLSearchParams(window.location.search);
    if (params.get("workspace_id") !== activeWorkspaceId) {
      params.set("workspace_id", activeWorkspaceId);
      window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
    }
  }

  async function handleSelectWorkspace(workspaceId: string) {
    if (workspace?.workspace_id === workspaceId) {
      setWorkspaceDrawerOpen(false);
      return;
    }
    setError("");
    setWorkspaceDrawerOpen(false);
    setWorkspaceTransitioning(true);
    setInitializing(true);
    try {
      const nextWorkspace = await openWorkspace(workspaceId);
      setWorkspace(nextWorkspace);
      await refreshWorkspaceList(nextWorkspace.workspace_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to open workspace.");
    } finally {
      setInitializing(false);
    }
  }

  async function handleCreateWorkspace() {
    setError("");
    setWorkspaceDrawerOpen(false);
    setWorkspaceTransitioning(true);
    setCreatingWorkspace(true);
    try {
      const nextWorkspace = await ensureWorkspace();
      setWorkspace(nextWorkspace);
      await refreshWorkspaceList(nextWorkspace.workspace_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace.");
      setWorkspaceTransitioning(false);
    } finally {
      setCreatingWorkspace(false);
    }
  }

  async function handleDeleteWorkspace(workspaceId: string) {
    if (deletingWorkspaceId) {
      return;
    }
    setError("");
    setDeletingWorkspaceId(workspaceId);
    try {
      await deleteWorkspace(workspaceId);
      const listed = await listWorkspaces();
      setWorkspaces(listed);

      if (workspace?.workspace_id === workspaceId) {
        const fallbackWorkspace = listed[0] ? await openWorkspace(listed[0].workspace_id) : await ensureWorkspace();
        setWorkspace(fallbackWorkspace);
        await refreshWorkspaceList(fallbackWorkspace.workspace_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete workspace.");
    } finally {
      setDeletingWorkspaceId("");
    }
  }

  function handleDownloadLogs() {
    const logs = logOutput || "No logs available.";
    const blob = new Blob([logs], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = URL.createObjectURL(blob);
    link.download = `runtime-logs-${timestamp}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  }

  function handleRefreshRolePreview(role: "client" | "specialist" | "manager") {
    if (!previewUrl) {
      return;
    }
    clearPreviewTimeout(role);
    setPreviewFailed((current) => ({ ...current, [role]: false }));
    setPreviewLoading((current) => ({ ...current, [role]: true }));
    setRolePreviewCycle((current) => ({ ...current, [role]: (current[role] ?? 0) + 1 }));
    armPreviewTimeout(role);
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
      {initializing || workspaceTransitioning || creatingWorkspace ? (
        <div className="global-loader-overlay" role="status" aria-live="polite">
          <div className="global-loader-card">
            <div className="global-loader-spinner" />
            <strong>{creatingWorkspace ? "Creating application..." : "Preparing workspace..."}</strong>
            <p>Please wait while the environment is being initialized.</p>
          </div>
        </div>
      ) : null}
      <div className="topbar">
        <div className="topbar-left">
          <button
            type="button"
            className="workspace-menu-trigger"
            aria-label="Open workspace menu"
            onClick={() => setWorkspaceDrawerOpen(true)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="topbar-title">
            <p className="eyebrow">Grounded Mini-App Platform</p>
            <h1 className="topbar-heading">AI module for Generating Mini-Applications</h1>
          </div>
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
      <div
        className={`workspace-drawer-backdrop ${workspaceDrawerOpen ? "is-open" : ""}`}
        onClick={() => setWorkspaceDrawerOpen(false)}
      />
      <aside className={`workspace-drawer ${workspaceDrawerOpen ? "is-open" : ""}`} aria-hidden={!workspaceDrawerOpen}>
        <div className="workspace-drawer-head">
          <strong>Applications</strong>
          <button type="button" className="icon-btn ghost" aria-label="Close drawer" onClick={() => setWorkspaceDrawerOpen(false)}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path
                d="M18.3 5.71a1 1 0 0 0-1.41 0L12 10.59 7.11 5.7A1 1 0 0 0 5.7 7.1L10.59 12 5.7 16.89a1 1 0 1 0 1.41 1.41L12 13.41l4.89 4.89a1 1 0 0 0 1.41-1.41L13.41 12l4.89-4.89a1 1 0 0 0 0-1.4z"
                fill="currentColor"
              />
            </svg>
          </button>
        </div>
        <button type="button" className="workspace-create" onClick={handleCreateWorkspace} disabled={creatingWorkspace}>
          {creatingWorkspace ? "Creating..." : "Create New"}
        </button>
        <label className="workspace-search">
          <span>Search by workspace id</span>
          <input
            type="search"
            value={workspaceSearch}
            onChange={(event) => setWorkspaceSearch(event.target.value)}
            placeholder="ws_980a7566..."
          />
        </label>
        <div className="workspace-list">
          {filteredWorkspaces.map((item) => (
            <div key={item.workspace_id} className={`workspace-item ${workspace?.workspace_id === item.workspace_id ? "active" : ""}`}>
              <button type="button" className="workspace-open" onClick={() => handleSelectWorkspace(item.workspace_id)}>
                <strong>{item.name}</strong>
                <span>{item.workspace_id}</span>
              </button>
              <button
                type="button"
                className="workspace-delete icon-btn"
                aria-label={`Delete ${item.name}`}
                onClick={() => handleDeleteWorkspace(item.workspace_id)}
                disabled={deletingWorkspaceId === item.workspace_id}
              >
                {deletingWorkspaceId === item.workspace_id ? (
                  "..."
                ) : (
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path
                      d="M6 7h12l-1 13a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L6 7zm3-4h6l1 2h4v2H4V5h4l1-2z"
                      fill="currentColor"
                    />
                  </svg>
                )}
              </button>
            </div>
          ))}
          {!filteredWorkspaces.length ? <p className="workspace-search-empty">No workspaces found.</p> : null}
        </div>
      </aside>

      <div className="layout">
        <section className="panel panel-chat">
          <header>
            <h2>Role Requirements</h2>
            <p>Describe expected behavior for each role. Empty role fields stay unchanged.</p>
          </header>
          <form onSubmit={handleGenerate} className="prompt-form">
            {previewRoles.map((role) => (
              <label key={role} className="role-input-field">
                <span>{ROLE_LABELS[role]}</span>
                <textarea
                  value={roleRequirements[role]}
                  onChange={(event) =>
                    setRoleRequirements((current) => ({
                      ...current,
                      [role]: event.target.value,
                    }))
                  }
                  rows={6}
                  placeholder={ROLE_PLACEHOLDERS[role]}
                />
              </label>
            ))}
            <button className="generate-full" type="submit" disabled={initializing || loading || !workspace}>
              {initializing ? "Preparing..." : loading ? "Generating..." : "Generate"}
            </button>
          </form>
          {error ? <p className="error">{error}</p> : null}
          <div className="suggestions">
            <h3>Suggestions</h3>
            {visibleWarnings.length ? (
              visibleWarnings.map((warning, index) => (
                <div key={`${warning.title}-${index}`} className="suggestion-card">
                  <strong>{warning.title}</strong>
                  <p>{warning.message}</p>
                </div>
              ))
            ) : (
              <p className="muted">No suggestions right now.</p>
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
            <div className="preview-toolbar-actions">
              <button type="button" className="icon-btn ghost" aria-label="Download logs" onClick={handleDownloadLogs}>
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="M5 20h14v-2H5v2zm7-18v10.17l3.59-3.58L17 10l-5 5-5-5 1.41-1.41L11 12.17V2h1z" fill="currentColor" />
                </svg>
              </button>
            </div>
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
                    <button
                      type="button"
                      className="preview-refresh"
                      onClick={() => handleRefreshRolePreview(role)}
                      aria-label={`Refresh ${role} preview`}
                      disabled={!previewUrl || previewLoading[role]}
                    >
                      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                        <path
                          d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z"
                          fill="currentColor"
                        />
                      </svg>
                    </button>
                    {previewUrl ? (
                      <>
                        {previewLoading[role] ? (
                          <div className="preview-loader">
                            <div className="preview-loader-spinner" />
                            <p>Loading runtime…</p>
                          </div>
                        ) : null}
                        <iframe
                          key={`${role}-${previewCycle}-${rolePreviewCycle[role]}-${rolePreviewUrls[role] ?? previewUrl}`}
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
