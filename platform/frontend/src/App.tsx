import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  createRun,
  deleteWorkspace,
  getRun,
  ensureWorkspace,
  getRunArtifacts,
  getWorkspaceLogs,
  listRuns,
  listWorkspaces,
  openWorkspace,
  rebuildPreview,
  rollbackRun,
  ensurePreview,
  stopRun,
  request,
  Run,
  RunArtifacts,
  SystemConfiguration,
  WorkspaceLogs,
  Workspace,
} from "./lib/api";
import "./styles/app.css";

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
  stage?: string;
  progress_percent?: number;
  draft_run_id?: string | null;
  last_error?: string | null;
};

type RunComposerMode = "generate" | "fix";
type UserGenerationMode = "fast" | "balanced" | "quality";

type FixErrorContext = {
  raw_error: string;
  source?: "build" | "preview" | "backend" | "frontend" | "runtime";
  failing_target?: string;
};

type RunProgressDisplayMap = Record<string, number>;

const ROLE_ORDER = ["client", "specialist", "manager"] as const;
type RoleKey = (typeof ROLE_ORDER)[number];

const PREVIEW_BOOT_ROLES: Record<RoleKey, boolean> = {
  client: true,
  specialist: true,
  manager: true,
};

const ROLE_LABELS: Record<RoleKey, string> = {
  client: "Client",
  specialist: "Specialist",
  manager: "Manager",
};

const DEFAULT_PROMPT = "";
const ROOT_PREVIEW_PATH = "/";

function inferFixSource(rawError: string): FixErrorContext["source"] {
  const lowered = rawError.toLowerCase();
  if (lowered.includes("docker preview") || lowered.includes("runtime did not start") || lowered.includes("preview failed")) {
    return "preview";
  }
  if (lowered.includes("traceback") || lowered.includes("modulenotfounderror") || lowered.includes("importerror")) {
    return "backend";
  }
  if (lowered.includes("npm run build") || lowered.includes("vite") || lowered.includes("ts230") || lowered.includes("typescript")) {
    return "frontend";
  }
  if (lowered.includes("permission denied") || lowered.includes("403") || lowered.includes("401")) {
    return "runtime";
  }
  return "build";
}

function buildFixPrefill(run: Run): { prompt: string; context: FixErrorContext } {
  const handoff = run.handoff_from_failed_generate;
  const rawError = handoff?.error_context?.raw_error ?? run.error_context?.raw_error ?? run.failure_reason ?? run.root_cause_summary ?? "Run failed.";
  return {
    prompt: rawError,
    context: {
      raw_error: rawError,
      source: handoff?.error_context?.source ?? run.error_context?.source ?? inferFixSource(rawError),
      failing_target: handoff?.error_context?.failing_target ?? run.error_context?.failing_target ?? run.fix_targets?.[0],
    },
  };
}

function getRoleRootPreviewPath(role: RoleKey): string {
  return `/${role}`;
}

function isRoleAtRootPreviewPath(role: RoleKey, path: string | undefined): boolean {
  const normalized = path || ROOT_PREVIEW_PATH;
  return normalized === ROOT_PREVIEW_PATH || normalized === getRoleRootPreviewPath(role);
}
const PREVIEW_BOOT_POLL_ATTEMPTS = 45;
const PREVIEW_BOOT_POLL_INTERVAL_MS = 1000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
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

function clampText(value: string, maxLength = 180): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1).trimEnd()}…`;
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item));
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string");
}

function formatRoleScope(scope: RoleKey[]): string {
  if (!scope.length) {
    return "all roles";
  }
  return scope.map((role) => ROLE_LABELS[role]).join(", ");
}

function stripAnsi(value: string): string {
  return value.replace(/\u001b\[[0-9;]*[A-Za-z]/g, "");
}

function isBenignPreviewLogLine(value: string): boolean {
  const normalized = stripAnsi(value).trim().toLowerCase();
  if (!normalized) {
    return true;
  }
  if (/\"\s(200|201|202|204|304)\s/.test(normalized)) {
    return true;
  }
  if (/^\d{1,3}(\.\d{1,3}){3}\s-\s-\s\[\d{2}\/[a-z]{3}\/\d{4}:/.test(normalized)) {
    return true;
  }
  return [
    "uvicorn running on http://",
    "application startup complete",
    "started server process",
    "waiting for application startup",
    "press ctrl+c to quit",
    "info:",
    "started",
    "waiting",
    "running",
    "recreate",
    "recreated",
    "container ",
    "get /",
    "post /",
    "put /",
    "patch /",
    "delete /",
  ].some((token) => normalized.includes(token));
}

function isExplicitPreviewErrorLine(value: string): boolean {
  const normalized = stripAnsi(value).trim().toLowerCase();
  if (!normalized || isBenignPreviewLogLine(normalized)) {
    return false;
  }
  return [
    "error",
    "failed",
    "exception",
    "traceback",
    "permission denied",
    "did not complete successfully",
    "exited (1)",
    "dependency failed to start",
    "cannot find",
    "module not found",
    "eaddrinuse",
    "syntaxerror",
    "typeerror",
    "referenceerror",
  ].some((token) => normalized.includes(token));
}

function extractPreviewErrorMessage(logs?: string[] | null): string | null {
  if (!logs?.length) {
    return null;
  }
  const lastMeaningful = [...logs]
    .map((line) => stripAnsi(line).trim())
    .reverse()
    .find((line) => isExplicitPreviewErrorLine(line));
  return lastMeaningful ?? null;
}

function displayRunStatus(run: Run | null): string {
  if (!run) {
    return "idle";
  }
  if (run.rolled_back) {
    return "rolled_back";
  }
  if (run.apply_status === "rolled_back") {
    return "rolled_back";
  }
  if (run.status === "running" && run.current_stage === "stopping") {
    return "stopping";
  }
  if (run.status === "failed" || run.current_stage === "failed") {
    return "failed";
  }
  if (run.status === "blocked" || run.current_stage === "blocked") {
    return "blocked";
  }
  if (run.status === "awaiting_approval") {
    return "awaiting_approval";
  }
  if (run.status === "completed") {
    return "completed";
  }
  return run.status;
}

function displayFixPhase(run: Run, phase?: string | null): string {
  const normalized = (phase ?? run.current_fix_phase ?? "").trim().toLowerCase();
  const status = displayRunStatus(run);
  if ((status === "failed" || status === "blocked") && normalized === "completed") {
    return "failed";
  }
  return phase ?? run.current_fix_phase ?? "n/a";
}

function progressCeilingForRun(run: Run): number {
  const stage = (run.current_stage || "").toLowerCase();
  if (run.status === "completed" || run.status === "failed" || run.status === "blocked" || run.status === "awaiting_approval") {
    return 100;
  }
  if (stage.includes("retrieving context")) {
    return 28;
  }
  if (stage.includes("building grounded spec")) {
    return 48;
  }
  if (stage.includes("planning code changes")) {
    return 68;
  }
  if (stage.includes("editing draft files")) {
    return 84;
  }
  if (stage.includes("repairing draft")) {
    return 89;
  }
  if (stage.includes("running validation and build")) {
    return 95;
  }
  if (stage.includes("refreshing preview")) {
    return 99;
  }
  return Math.min(96, Math.max(18, (run.progress_percent || 0) + 8));
}

function nextVisualProgress(current: number, run: Run): number {
  const actual = Math.max(0, Math.min(100, run.progress_percent || 0));
  const status = displayRunStatus(run);
  const stage = (run.current_stage || "").toLowerCase();
  if (["completed", "failed", "blocked", "rolled_back", "awaiting_approval"].includes(status)) {
    return actual;
  }

  const floor = Math.max(actual, 4);
  const ceiling = Math.max(floor, progressCeilingForRun(run));
  const isEarlyPhase =
    stage.includes("retrieving context") ||
    stage.includes("building grounded spec") ||
    stage.includes("spec") ||
    stage.includes("starting");
  if (current < actual) {
    const catchUpStep = isEarlyPhase
      ? Math.max(0.55, (actual - current) * 0.18)
      : Math.max(1.1, (actual - current) * 0.28);
    return Math.min(actual, current + catchUpStep);
  }
  if (current >= ceiling) {
    return current;
  }

  let driftStep = 0.18;
  if (stage.includes("retrieving context")) {
    driftStep = 0.08;
  } else if (stage.includes("building grounded spec") || stage.includes("spec")) {
    driftStep = 0.1;
  } else if (stage.includes("planning")) {
    driftStep = 0.16;
  } else if (stage.includes("editing")) {
    driftStep = 0.2;
  } else if (stage.includes("validation") || stage.includes("preview")) {
    driftStep = 0.12;
  }
  return Math.min(ceiling, current + driftStep);
}

function displayProgressForRun(run: Run, progressDisplay: RunProgressDisplayMap): number {
  const actual = Math.max(0, Math.min(100, run.progress_percent || 0));
  const visual = progressDisplay[run.run_id];
  if (visual === undefined) {
    return Math.max(4, actual);
  }
  if (displayRunStatus(run) === "completed") {
    return 100;
  }
  if (displayRunStatus(run) === "failed" || displayRunStatus(run) === "blocked") {
    return actual;
  }
  return Math.max(actual, Math.min(100, Math.round(visual)));
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

type DiffLineKind = "meta" | "hunk" | "add" | "remove" | "context";

function classifyDiffLine(line: string): DiffLineKind {
  if (
    line.startsWith("diff --git") ||
    line.startsWith("index ") ||
    line.startsWith("--- ") ||
    line.startsWith("+++ ") ||
    line.startsWith("new file mode") ||
    line.startsWith("deleted file mode")
  ) {
    return "meta";
  }
  if (line.startsWith("@@")) {
    return "hunk";
  }
  if (line.startsWith("+")) {
    return "add";
  }
  if (line.startsWith("-")) {
    return "remove";
  }
  return "context";
}

function diffStats(text: string): { files: number; additions: number; removals: number } {
  const lines = text.split("\n");
  let files = 0;
  let additions = 0;
  let removals = 0;
  lines.forEach((line) => {
    if (line.startsWith("diff --git")) {
      files += 1;
      return;
    }
    if (line.startsWith("+++") || line.startsWith("---")) {
      return;
    }
    if (line.startsWith("+")) {
      additions += 1;
      return;
    }
    if (line.startsWith("-")) {
      removals += 1;
    }
  });
  return { files, additions, removals };
}

function DiffViewer({ text }: { text: string }) {
  if (!text.trim()) {
    return (
      <div className="terminal diff-terminal diff-terminal-empty">
        <div className="diff-empty-state">
          <strong>No diff recorded</strong>
          <p>Select a run with draft changes to inspect the patch here.</p>
        </div>
      </div>
    );
  }

  const stats = diffStats(text);
  const lines = text.split("\n");

  return (
    <div className="terminal diff-terminal">
      <div className="diff-header">
        <div className="diff-header-copy">
          <span className="diff-header-eyebrow">Patch review</span>
          <strong>Generated workspace diff</strong>
        </div>
        <div className="diff-stats">
          <div className="diff-stat">
            <span>Files</span>
            <strong>{stats.files}</strong>
          </div>
          <div className="diff-stat diff-stat-add">
            <span>Additions</span>
            <strong>+{stats.additions}</strong>
          </div>
          <div className="diff-stat diff-stat-remove">
            <span>Removals</span>
            <strong>-{stats.removals}</strong>
          </div>
        </div>
      </div>
      <div className="diff-surface">
        {lines.map((line, index) => {
          const kind = classifyDiffLine(line);
          const marker = kind === "add" ? "+" : kind === "remove" ? "−" : kind === "hunk" ? "@@" : kind === "meta" ? "•" : "";
          return (
            <div key={`${index}-${line}`} className={`diff-line diff-line-${kind}`}>
              <span className="diff-line-number">{index + 1}</span>
              <span className="diff-line-marker">{marker}</span>
              <code>{line || " "}</code>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const [workspaceTransitioning, setWorkspaceTransitioning] = useState(true);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [deletingWorkspaceId, setDeletingWorkspaceId] = useState("");
  const [issuesDrawerOpen, setIssuesDrawerOpen] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [systemConfig, setSystemConfig] = useState<SystemConfiguration | null>(null);
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [selectedRunMode, setSelectedRunMode] = useState<RunComposerMode>("generate");
  const [fixErrorContext, setFixErrorContext] = useState<FixErrorContext | null>(null);
  const [selectedGenerationMode, setSelectedGenerationMode] = useState<UserGenerationMode>("balanced");
  const [selectedRoles, setSelectedRoles] = useState<Record<RoleKey, boolean>>({
    client: true,
    specialist: true,
    manager: true,
  });
  const [runs, setRuns] = useState<Run[]>([]);
  const [runProgressDisplay, setRunProgressDisplay] = useState<RunProgressDisplayMap>({});
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runDetailsOpen, setRunDetailsOpen] = useState(false);
  const [runArtifacts, setRunArtifacts] = useState<RunArtifacts | null>(null);
  const [workspaceLogs, setWorkspaceLogs] = useState<WorkspaceLogs | null>(null);
  const [selectedLogService, setSelectedLogService] = useState<string>("");
  const [activeTab, setActiveTab] = useState<"preview" | "code" | "diff" | "logs">("preview");
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [expandedDirectories, setExpandedDirectories] = useState<Set<string>>(new Set());
  const [previewUrl, setPreviewUrl] = useState("");
  const [rolePreviewUrls, setRolePreviewUrls] = useState<Record<string, string>>({});
  const [previewRuntimeMode, setPreviewRuntimeMode] = useState("");
  const [previewStatus, setPreviewStatus] = useState("");
  const [previewCycle, setPreviewCycle] = useState(0);
  const [rolePreviewCycle, setRolePreviewCycle] = useState<Record<RoleKey, number>>({
    client: 0,
    specialist: 0,
    manager: 0,
  });
  const [previewLoading, setPreviewLoading] = useState<Record<RoleKey, boolean>>({
    client: false,
    specialist: false,
    manager: false,
  });
  const [previewFailed, setPreviewFailed] = useState<Record<RoleKey, boolean>>({
    client: false,
    specialist: false,
    manager: false,
  });
  const [previewMenuRole, setPreviewMenuRole] = useState<RoleKey | null>(null);
  const [rolePreviewPath, setRolePreviewPath] = useState<Record<RoleKey, string>>({
    client: ROOT_PREVIEW_PATH,
    specialist: ROOT_PREVIEW_PATH,
    manager: ROOT_PREVIEW_PATH,
  });
  const [previewBooting, setPreviewBooting] = useState(false);
  const [stoppingRunId, setStoppingRunId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const previewTimeoutsRef = useRef<Record<string, number | undefined>>({});
  const previewFrameRefs = useRef<Record<RoleKey, HTMLIFrameElement | null>>({
    client: null,
    specialist: null,
    manager: null,
  });
  const activeWorkspaceIdRef = useRef("");

  useEffect(() => {
    function handlePreviewMessage(event: MessageEvent) {
      const payload = event.data;
      if (!payload || typeof payload !== "object" || payload.type !== "runtime-preview-route") {
        return;
      }
      const role = ROLE_ORDER.find((candidate) => previewFrameRefs.current[candidate]?.contentWindow === event.source);
      if (!role) {
        return;
      }
      setRolePreviewPath((current) => ({
        ...current,
        [role]: typeof payload.path === "string" && payload.path ? payload.path : ROOT_PREVIEW_PATH,
      }));
    }

    window.addEventListener("message", handlePreviewMessage);
    return () => window.removeEventListener("message", handlePreviewMessage);
  }, []);

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
        setWorkspace(nextWorkspace);
        void refreshWorkspaceList(nextWorkspace.workspace_id);
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
    activeWorkspaceIdRef.current = workspace.workspace_id;
    primePreviewSurfaceForBoot();
    void (async () => {
      await refreshWorkspaceState(workspace.workspace_id);
      try {
        await ensurePreview(workspace.workspace_id);
      } catch {
        // Preview bootstrap is best-effort; polling and rebuild fallback will handle late startup.
      }
      void pollPreviewUntilReady(workspace.workspace_id);
    })();
  }, [workspace?.workspace_id]);

  useEffect(() => {
    if (!selectedRunId) {
      setRunArtifacts(null);
      return;
    }
    const activeRun = runs.find((item) => item.run_id === selectedRunId);
    if (activeRun && !["awaiting_approval", "completed", "blocked", "failed"].includes(activeRun.status)) {
      setRunArtifacts(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const nextArtifacts = await getRunArtifacts(selectedRunId);
        if (!cancelled) {
          setRunArtifacts(nextArtifacts);
        }
      } catch {
        if (!cancelled) {
          setRunArtifacts(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runs, selectedRunId]);

  useEffect(() => {
    if (!workspace) {
      return;
    }
    const activeRun = runs.find((item) => item.run_id === selectedRunId);
    const runId = activeRun?.draft_ready || activeRun?.status === "awaiting_approval" ? activeRun.run_id : "";
    let cancelled = false;
    void (async () => {
      try {
        const nextFiles = await request<FileEntry[]>(
          `/workspaces/${workspace.workspace_id}/files/tree${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`,
        );
        if (!cancelled) {
          setFiles(nextFiles);
        }
      } catch {
        // Keep the last file tree if the selected run has no draft.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runs, selectedRunId, workspace]);

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
    if (!runDetailsOpen) {
      return;
    }
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setRunDetailsOpen(false);
      }
    }
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [runDetailsOpen]);

  useEffect(() => {
    if (!previewUrl) {
      return;
    }
    ROLE_ORDER.forEach((role) => {
      if (previewLoading[role]) {
        armPreviewTimeout(role);
      }
    });
  }, [previewCycle, previewLoading, previewUrl]);

  const fileTree = useMemo(() => buildFileTree(files), [files]);
  const filteredWorkspaces = useMemo(() => {
    const query = workspaceSearch.trim().toLowerCase();
    if (!query) {
      return workspaces;
    }
    return workspaces.filter((item) => {
      return item.workspace_id.toLowerCase().includes(query) || item.name.toLowerCase().includes(query);
    });
  }, [workspaceSearch, workspaces]);
  const selectedRun = useMemo(() => runs.find((item) => item.run_id === selectedRunId) ?? runs[0] ?? null, [runs, selectedRunId]);
  const topbarRun = selectedRun ?? null;
  const topbarStatus = displayRunStatus(topbarRun);
  const touchedFilesCount = topbarRun?.touched_files.length ?? 0;
  const editorStats = useMemo(() => {
    const lines = fileContent ? fileContent.split("\n").length : 0;
    const characters = fileContent.length;
    return { lines, characters };
  }, [fileContent]);
  const visibleIssues = useMemo(() => {
    const items = [...(topbarRun?.checks_summary.issues ?? [])];
    if (topbarRun?.failure_reason) {
      items.unshift({
        code: "run_failure",
        message: topbarRun.failure_reason,
        severity: "high",
      });
    }
    const seen = new Set<string>();
    return items.filter((item) => {
      const key = `${item.code ?? ""}:${item.message ?? ""}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
  }, [topbarRun]);
  const activeRoleScope = useMemo(
    () => ROLE_ORDER.filter((role) => selectedRoles[role]),
    [selectedRoles],
  );
  const activeRunIds = useMemo(
    () =>
      runs
        .filter((run) => ["pending", "running"].includes(run.status))
        .map((run) => run.run_id),
    [runs],
  );
  const draftContextRunId = selectedRun?.draft_ready || selectedRun?.status === "awaiting_approval" ? selectedRun.run_id : "";
  const diffText = runArtifacts?.candidate_diff ?? runArtifacts?.diff ?? "";
  const showGlobalLoader = initializing || creatingWorkspace || (workspaceTransitioning && !workspace);
  const runDetailSummary =
    runArtifacts?.final_summary ??
    runArtifacts?.job?.summary ??
    selectedRun?.summary ??
    selectedRun?.failure_reason ??
    "";
  const failureAnalysis = runArtifacts?.failure_analysis ?? (selectedRun
    ? {
        mode: selectedRun.mode,
        failure_class: selectedRun.failure_class,
        failure_signature: selectedRun.failure_signature,
        root_cause_summary: selectedRun.root_cause_summary,
        fix_targets: selectedRun.fix_targets,
        handoff_from_failed_generate: selectedRun.handoff_from_failed_generate,
        error_context: selectedRun.error_context,
        current_fix_phase: selectedRun.current_fix_phase,
        current_failing_command: selectedRun.current_failing_command,
        current_exit_code: selectedRun.current_exit_code,
      }
    : null);
  const fixAttemptItems = asRecordArray(runArtifacts?.fix_attempts?.items ?? selectedRun?.fix_attempts);
  const scopeExpansionItems = asRecordArray(runArtifacts?.scope_expansions?.items ?? selectedRun?.scope_expansions);
  const fixCase = runArtifacts?.fix_case ?? workspaceLogs?.reports?.fix_case ?? null;
  const composerTitle = selectedRunMode === "fix" ? "fixbug Input" : "Task Input";
  const composerHelp =
    selectedRunMode === "fix"
      ? "Paste the failing build log, preview error, stack trace, or API mismatch. The system will analyze the error and apply the smallest safe fixbug."
      : "Describe what to build or change. Use fixbug mode when you want targeted error repair instead of broad generation.";
  const composerPlaceholder =
    selectedRunMode === "fix"
      ? "Paste the exact error or log, for example: Docker preview rebuild failed... or a TypeScript traceback."
      : "Describe the change you want to build. Switch to fixbug mode for build failures, preview issues, or stack traces.";
  const effectiveGenerationMode: UserGenerationMode = selectedRunMode === "fix" ? "balanced" : selectedGenerationMode;
  const previewErrorMessage = useMemo(
    () => extractPreviewErrorMessage(workspaceLogs?.preview?.logs ?? runArtifacts?.preview?.logs ?? []),
    [runArtifacts?.preview?.logs, workspaceLogs?.preview?.logs],
  );
  const containerStatuses = workspaceLogs?.preview?.containers ?? [];
  const containerLogs = workspaceLogs?.preview?.container_logs ?? {};
  const selectedContainerLogLines = containerLogs[selectedLogService] ?? [];
  const eventLogLines = useMemo(
    () => {
      const eventLines =
        workspaceLogs?.events?.length
          ? workspaceLogs.events.map((event) => {
              const details =
                event.details && Object.keys(event.details).length ? ` | ${JSON.stringify(event.details)}` : "";
              return `- [${formatTimestamp(event.created_at)}] ${event.event_type}: ${event.message}${details}`;
            })
          : [];
      const platformLines = workspaceLogs?.platform_log?.length ? workspaceLogs.platform_log : [];
      const apiLines = workspaceLogs?.api_log?.length ? workspaceLogs.api_log : [];
      const combined = [
        ...eventLines,
        ...(platformLines.length ? ["", "=== platform.log ===", ...platformLines] : []),
        ...(apiLines.length ? ["", "=== api.log ===", ...apiLines] : []),
      ].filter((line, index, items) => !(line === "" && (index === 0 || items[index - 1] === "")));
      return combined.length ? combined : ["No run events yet."];
    },
    [workspaceLogs?.api_log, workspaceLogs?.events, workspaceLogs?.platform_log],
  );
  useEffect(() => {
    const availableServices = Object.keys(containerLogs);
    if (selectedLogService && !availableServices.includes(selectedLogService)) {
      setSelectedLogService("");
    }
  }, [containerLogs, selectedLogService]);

  useEffect(() => {
    if (!workspace || activeRunIds.length === 0) {
      return;
    }

    let cancelled = false;
    let inFlight = false;

    const syncActiveRuns = async () => {
      if (cancelled || inFlight || activeWorkspaceIdRef.current !== workspace.workspace_id) {
        return;
      }
      inFlight = true;
      try {
        const [runsResult, logsResult] = await Promise.allSettled([
          listRuns(workspace.workspace_id),
          getWorkspaceLogs(workspace.workspace_id),
        ]);

        if (cancelled || activeWorkspaceIdRef.current !== workspace.workspace_id) {
          return;
        }

        if (runsResult.status === "fulfilled") {
          setRuns(runsResult.value);
        }

        if (logsResult.status === "fulfilled") {
          setWorkspaceLogs(logsResult.value);
        }
      } finally {
        inFlight = false;
      }
    };

    void syncActiveRuns();
    const intervalId = window.setInterval(() => {
      void syncActiveRuns();
    }, document.visibilityState === "visible" ? 1000 : 1500);

    const handleVisibilityOrFocus = () => {
      void syncActiveRuns();
    };

    document.addEventListener("visibilitychange", handleVisibilityOrFocus);
    window.addEventListener("focus", handleVisibilityOrFocus);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", handleVisibilityOrFocus);
      window.removeEventListener("focus", handleVisibilityOrFocus);
    };
  }, [activeRunIds, workspace]);

  useEffect(() => {
    setRunProgressDisplay((current) => {
      const next: RunProgressDisplayMap = {};
      runs.forEach((run) => {
        const actual = Math.max(0, Math.min(100, run.progress_percent || 0));
        const existing = current[run.run_id];
        if (existing === undefined) {
          next[run.run_id] = Math.max(4, actual);
          return;
        }
        if (displayRunStatus(run) === "completed") {
          next[run.run_id] = 100;
          return;
        }
        if (displayRunStatus(run) === "failed" || displayRunStatus(run) === "blocked" || displayRunStatus(run) === "rolled_back") {
          next[run.run_id] = actual;
          return;
        }
        next[run.run_id] = Math.max(existing, actual);
      });
      return next;
    });
  }, [runs]);

  useEffect(() => {
    const hasAnimatedRuns = runs.some((run) => displayRunStatus(run) === "running");
    if (!hasAnimatedRuns) {
      return;
    }
    const intervalId = window.setInterval(() => {
      setRunProgressDisplay((current) => {
        let changed = false;
        const next: RunProgressDisplayMap = { ...current };
        runs.forEach((run) => {
          const previous = next[run.run_id] ?? Math.max(4, run.progress_percent || 0);
          const visual = nextVisualProgress(previous, run);
          if (Math.abs(visual - previous) > 0.01) {
            next[run.run_id] = visual;
            changed = true;
          }
        });
        return changed ? next : current;
      });
    }, 180);
    return () => window.clearInterval(intervalId);
  }, [runs]);

  async function refreshWorkspaceState(workspaceId: string, preferredRunId?: string) {
    setWorkspaceTransitioning(true);
    try {
      const [treeResult, runsResult, logsResult, previewResult] = await Promise.allSettled([
        request<FileEntry[]>(`/workspaces/${workspaceId}/files/tree`),
        listRuns(workspaceId),
        getWorkspaceLogs(workspaceId),
        request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`),
      ]);

      const refreshErrors: string[] = [];
      let nextRuns: Run[] = [];
      let nextSelectedRunId = "";

      if (runsResult.status === "fulfilled") {
        nextRuns = runsResult.value;
        setRuns(nextRuns);
        nextSelectedRunId =
          preferredRunId && nextRuns.some((run) => run.run_id === preferredRunId)
            ? preferredRunId
            : selectedRunId && nextRuns.some((run) => run.run_id === selectedRunId)
              ? selectedRunId
              : nextRuns[0]?.run_id ?? "";
        setSelectedRunId(nextSelectedRunId);
      } else {
        refreshErrors.push(`runs: ${runsResult.reason instanceof Error ? runsResult.reason.message : "failed to load"}`);
      }

      const selectedRunForTree = nextRuns.find((run) => run.run_id === nextSelectedRunId);
      const draftTreeRunId = selectedRunForTree?.draft_ready || selectedRunForTree?.status === "awaiting_approval" ? selectedRunForTree.run_id : "";
      if (draftTreeRunId) {
        try {
          const draftTree = await request<FileEntry[]>(
            `/workspaces/${workspaceId}/files/tree?run_id=${encodeURIComponent(draftTreeRunId)}`,
          );
          setFiles(draftTree);
          setExpandedDirectories((current) => {
            if (current.size > 0) {
              return current;
            }
            return new Set(collectExpandedDirectories(buildFileTree(draftTree)).slice(0, 10));
          });
        } catch (err) {
          refreshErrors.push(`files: ${err instanceof Error ? err.message : "failed to load draft files"}`);
        }
      } else if (treeResult.status === "fulfilled") {
        setFiles(treeResult.value);
        setExpandedDirectories((current) => {
          if (current.size > 0) {
            return current;
          }
          return new Set(collectExpandedDirectories(buildFileTree(treeResult.value)).slice(0, 10));
        });
      } else {
        refreshErrors.push(`files: ${treeResult.reason instanceof Error ? treeResult.reason.message : "failed to load"}`);
      }

      if (logsResult.status === "fulfilled") {
        setWorkspaceLogs(logsResult.value);
      } else {
        setWorkspaceLogs(null);
      }

      if (previewResult.status === "fulfilled") {
        const previewPayload = previewResult.value;
        const previewIsReady = previewPayload.status === "running" && Boolean(previewPayload.url);
        setPreviewRuntimeMode(previewPayload.runtime_mode ?? "");
        if (previewIsReady) {
          setPreviewUrl(previewPayload.url ?? "");
          setRolePreviewUrls(previewPayload.role_urls ?? {});
          setPreviewStatus(previewPayload.status ?? "");
          setPreviewCycle((current) => current + 1);
          setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
          setPreviewFailed({
            client: false,
            specialist: false,
            manager: false,
          });
          setPreviewBooting(false);
        } else {
          const previewNeedsBootstrap = previewPayload.status === "stopped" || previewPayload.status === "error" || !previewPayload.status;
          setPreviewUrl("");
          setRolePreviewUrls({});
          setPreviewStatus(previewNeedsBootstrap ? "starting" : (previewPayload.status ?? "starting"));
          if (previewNeedsBootstrap) {
            void ensurePreview(workspaceId).catch(() => undefined);
          }
          setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
          setPreviewFailed({
            client: false,
            specialist: false,
            manager: false,
          });
        }
      } else {
        setPreviewUrl("");
        setRolePreviewUrls({});
        setPreviewStatus("starting");
        setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
        setPreviewFailed({
          client: false,
          specialist: false,
          manager: false,
        });
      }

      setError(refreshErrors.length ? refreshErrors.join(" | ") : "");
    } finally {
      setWorkspaceTransitioning(false);
    }
  }

  async function pollPreviewUntilReady(workspaceId: string) {
    let rebuildTriggered = false;
    for (let attempt = 0; attempt < PREVIEW_BOOT_POLL_ATTEMPTS; attempt += 1) {
      if (activeWorkspaceIdRef.current !== workspaceId) {
        return;
      }
      try {
        const [previewResult, logsResult] = await Promise.allSettled([
          request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`),
          getWorkspaceLogs(workspaceId),
        ]);
        if (activeWorkspaceIdRef.current !== workspaceId) {
          return;
        }
        if (logsResult.status === "fulfilled") {
          setWorkspaceLogs(logsResult.value);
        }
        if (previewResult.status !== "fulfilled") {
          await sleep(PREVIEW_BOOT_POLL_INTERVAL_MS);
          continue;
        }
        const preview = previewResult.value;
        setPreviewRuntimeMode(preview.runtime_mode ?? "");
        setPreviewStatus(preview.status ?? "");

        if (preview.url) {
          setPreviewUrl(preview.url);
          setRolePreviewUrls(preview.role_urls ?? {});
          setPreviewCycle((current) => current + 1);
          setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
          setPreviewFailed({
            client: false,
            specialist: false,
            manager: false,
          });
          setPreviewBooting(false);
          return;
        }

        if (preview.status === "error") {
          setPreviewStatus("starting");
          try {
            await ensurePreview(workspaceId);
          } catch {
            // If ensure also fails, keep current error state and surface logs.
          }
          await sleep(PREVIEW_BOOT_POLL_INTERVAL_MS);
          continue;
        }

        if (!rebuildTriggered && attempt >= 2 && (preview.status === "stopped" || !preview.status)) {
          rebuildTriggered = true;
          try {
            await rebuildPreview(workspaceId);
          } catch {
            // Keep polling and let the regular error path surface if rebuild also fails.
          }
        }
      } catch {
        // Keep polling while the runtime is still booting.
      }
      await sleep(PREVIEW_BOOT_POLL_INTERVAL_MS);
    }
    setPreviewBooting(false);
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
  }

  async function handleRun(event: FormEvent) {
    event.preventDefault();
    if (!workspace) {
      return;
    }
    if (!prompt.trim()) {
      setError("Enter a prompt describing the change you want.");
      return;
    }
    setLoading(true);
    setError("");
    setPreviewBooting(true);
    try {
      const trimmedPrompt = prompt.trim();
      const fixPayload =
        selectedRunMode === "fix"
          ? {
              mode: "fix" as const,
              prompt:
                trimmedPrompt.length > 180
                  ? "Analyze the reported failure and apply the smallest safe fix."
                  : trimmedPrompt,
              error_context: {
                raw_error: fixErrorContext?.raw_error?.trim() || trimmedPrompt,
                source: fixErrorContext?.source ?? inferFixSource(trimmedPrompt),
                ...(fixErrorContext?.failing_target ? { failing_target: fixErrorContext.failing_target } : {}),
              },
            }
          : {
              mode: "generate" as const,
              prompt: trimmedPrompt,
            };
      const run = await createRun(workspace.workspace_id, {
        ...fixPayload,
        intent: "auto",
        apply_strategy: "staged_auto_apply",
        target_role_scope: activeRoleScope,
        model_profile: systemConfig?.default_coding_profile ?? systemConfig?.defaults.model_profile ?? "openai_code_fast",
        generation_mode: effectiveGenerationMode,
      });
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      setSelectedRunId(run.run_id);
      setRunArtifacts(null);
      await refreshWorkspaceState(workspace.workspace_id, run.run_id);
      setActiveTab("preview");
      void pollRunUntilSettled(workspace.workspace_id, run.run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to execute run.");
    } finally {
      setPreviewBooting(false);
      setLoading(false);
    }
  }

  function handoffRunToFix(run: Run) {
    const handoff = buildFixPrefill(run);
    setSelectedRunMode("fix");
    setPrompt(handoff.prompt);
    setFixErrorContext(handoff.context);
  }

  async function handleRunFix(run: Run) {
    if (!workspace || loading) {
      return;
    }
    const handoff = buildFixPrefill(run);
    setLoading(true);
    setError("");
    setPreviewBooting(true);
    try {
      const nextRun = await createRun(workspace.workspace_id, {
        prompt: handoff.prompt.length > 180 ? "Analyze the reported failure and apply the smallest safe fix." : handoff.prompt,
        mode: "fix",
        intent: "auto",
        apply_strategy: "staged_auto_apply",
        target_role_scope: run.target_role_scope.length ? run.target_role_scope : activeRoleScope,
        model_profile: run.model_profile || systemConfig?.default_coding_profile || systemConfig?.defaults.model_profile || "openai_code_fast",
        generation_mode: "balanced",
        target_platform: "telegram_mini_app",
        preview_profile: "telegram_mock",
        resume_from_run_id: run.run_id,
        error_context: handoff.context,
      });
      setSelectedRunMode("fix");
      setPrompt(handoff.prompt);
      setFixErrorContext(handoff.context);
      setRuns((current) => [nextRun, ...current.filter((item) => item.run_id !== nextRun.run_id)]);
      setSelectedRunId(nextRun.run_id);
      setRunArtifacts(null);
      await refreshWorkspaceState(workspace.workspace_id, nextRun.run_id);
      setActiveTab("preview");
      void pollRunUntilSettled(workspace.workspace_id, nextRun.run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start fixbug run.");
    } finally {
      setPreviewBooting(false);
      setLoading(false);
    }
  }

  async function handleSelectFile(path: string) {
    if (!workspace) {
      return;
    }
    setSelectedPath(path);
    const payload = await request<{ path: string; content: string }>(
      `/workspaces/${workspace.workspace_id}/files/content?path=${encodeURIComponent(path)}${
        draftContextRunId ? `&run_id=${encodeURIComponent(draftContextRunId)}` : ""
      }`,
    );
    setFileContent(payload.content);
    setActiveTab("code");
  }

  async function pollRunUntilSettled(workspaceId: string, runId: string) {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      if (activeWorkspaceIdRef.current !== workspaceId) {
        return;
      }
      try {
        const [runResult, logsResult] = await Promise.allSettled([
          getRun(runId),
          getWorkspaceLogs(workspaceId),
        ]);

        if (runResult.status !== "fulfilled") {
          await sleep(1000);
          continue;
        }

        const currentRun = runResult.value;
        if (activeWorkspaceIdRef.current !== workspaceId) {
          return;
        }

        setRuns((current) => {
          const existing = current.filter((item) => item.run_id !== runId);
          return [currentRun, ...existing];
        });

        if (logsResult.status === "fulfilled") {
          setWorkspaceLogs(logsResult.value);
        }

        if (["awaiting_approval", "completed", "blocked", "failed"].includes(currentRun.status)) {
          await refreshWorkspaceState(workspaceId, runId);
          try {
            setRunArtifacts(await getRunArtifacts(runId));
          } catch {
            setRunArtifacts(null);
          }
          if (currentRun.status === "completed") {
            await rebuildWorkspacePreview(workspaceId);
          }
          return;
        }
      } catch {
        // Keep polling on transient client/network errors instead of freezing the UI.
      }
      await sleep(1000);
    }
  }

  async function handleSaveFile() {
    if (!workspace || !selectedPath) {
      return;
    }
    setError("");
    try {
      await request(`/workspaces/${workspace.workspace_id}/files/save`, {
        method: "POST",
        body: JSON.stringify({
          relative_path: selectedPath,
          content: fileContent,
          run_id: draftContextRunId || undefined,
        }),
      });
      await refreshWorkspaceState(workspace.workspace_id, selectedRunId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save file.");
    }
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
    setWorkspaceDrawerOpen(false);
    setInitializing(true);
    setError("");
    try {
      const nextWorkspace = await openWorkspace(workspaceId);
      clearWorkspaceDraftSurface();
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
    setCreatingWorkspace(true);
    try {
      const nextWorkspace = await ensureWorkspace();
      clearWorkspaceDraftSurface();
      setWorkspace(nextWorkspace);
      void refreshWorkspaceList(nextWorkspace.workspace_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace.");
    } finally {
      setCreatingWorkspace(false);
    }
  }

  function resetWorkspaceSurface() {
    activeWorkspaceIdRef.current = "";
    setWorkspace(null);
    setRuns([]);
    setSelectedRunId("");
    setRunArtifacts(null);
    setWorkspaceLogs(null);
    setFiles([]);
    setSelectedPath("");
    setFileContent("");
    setExpandedDirectories(new Set<string>());
    setPreviewUrl("");
    setRolePreviewUrls({});
    setPreviewRuntimeMode("");
    setPreviewStatus("");
    setPreviewLoading({
      client: false,
      specialist: false,
      manager: false,
    });
    setPreviewFailed({
      client: false,
      specialist: false,
      manager: false,
    });
    setRolePreviewPath({
      client: ROOT_PREVIEW_PATH,
      specialist: ROOT_PREVIEW_PATH,
      manager: ROOT_PREVIEW_PATH,
    });
    setPreviewBooting(false);
  }

  function primePreviewSurfaceForBoot() {
    ROLE_ORDER.forEach((role) => clearPreviewTimeout(role));
    setPreviewUrl("");
    setRolePreviewUrls({});
    setPreviewRuntimeMode("");
    setPreviewStatus("starting");
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    setPreviewFailed({
      client: false,
      specialist: false,
      manager: false,
    });
    setRolePreviewPath({
      client: ROOT_PREVIEW_PATH,
      specialist: ROOT_PREVIEW_PATH,
      manager: ROOT_PREVIEW_PATH,
    });
    setPreviewBooting(true);
  }

  function clearWorkspaceDraftSurface() {
    setRuns([]);
    setSelectedRunId("");
    setRunArtifacts(null);
    setWorkspaceLogs(null);
    setFiles([]);
    setSelectedPath("");
    setFileContent("");
    setExpandedDirectories(new Set<string>());
    primePreviewSurfaceForBoot();
  }

  function bootstrapWorkspaceAfterDelete(nextWorkspaces: Workspace[]) {
    void (async () => {
      try {
        const nextWorkspace = nextWorkspaces[0] ? await openWorkspace(nextWorkspaces[0].workspace_id) : await ensureWorkspace();
        clearWorkspaceDraftSurface();
        setWorkspace(nextWorkspace);
        await refreshWorkspaceList(nextWorkspace.workspace_id);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to bootstrap the next workspace.");
      }
    })();
  }

  async function handleDeleteWorkspace(workspaceId: string) {
    if (deletingWorkspaceId) {
      return;
    }
    setDeletingWorkspaceId(workspaceId);
    setError("");
    try {
      const deletingActiveWorkspace = workspace?.workspace_id === workspaceId;
      await deleteWorkspace(workspaceId);
      const listed = await listWorkspaces();
      setWorkspaces(listed);
      if (deletingActiveWorkspace) {
        resetWorkspaceSurface();
        bootstrapWorkspaceAfterDelete(listed);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete workspace.");
    } finally {
      setDeletingWorkspaceId("");
    }
  }

  function armPreviewTimeout(role: RoleKey) {
    const existing = previewTimeoutsRef.current[role];
    if (existing) {
      window.clearTimeout(existing);
    }
    previewTimeoutsRef.current[role] = window.setTimeout(() => {
      setPreviewLoading((current) => ({ ...current, [role]: false }));
      setPreviewFailed((current) => ({ ...current, [role]: true }));
    }, 12000);
  }

  function clearPreviewTimeout(role: RoleKey) {
    const existing = previewTimeoutsRef.current[role];
    if (existing) {
      window.clearTimeout(existing);
      previewTimeoutsRef.current[role] = undefined;
    }
  }

  function sendPreviewCommand(role: RoleKey, command: "back" | "close" | "refresh") {
    previewFrameRefs.current[role]?.contentWindow?.postMessage(
      {
        type: "runtime-preview-command",
        command,
      },
      "*",
    );
  }

  function handleMockupPrimaryAction(role: RoleKey) {
    if (isRoleAtRootPreviewPath(role, rolePreviewPath[role])) {
      sendPreviewCommand(role, "close");
      return;
    }
    sendPreviewCommand(role, "back");
  }

  function handleMockupRefresh(role: RoleKey) {
    setPreviewMenuRole(null);
    sendPreviewCommand(role, "refresh");
    handleRefreshRolePreview(role);
  }

  async function rebuildWorkspacePreview(workspaceId: string) {
    setPreviewBooting(true);
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    try {
      await rebuildPreview(workspaceId);
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const preview = await request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`);
        setPreviewRuntimeMode(preview.runtime_mode ?? "");
        setPreviewStatus(preview.status ?? "");
        if (preview.status === "running") {
          setPreviewUrl(preview.url ?? "");
          setRolePreviewUrls(preview.role_urls ?? {});
          setPreviewCycle((current) => current + 1);
          setPreviewFailed({
            client: false,
            specialist: false,
            manager: false,
          });
          break;
        }
        await sleep(800);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to rebuild preview.");
    } finally {
      setPreviewBooting(false);
    }
  }

  async function handleRefreshPreview() {
    if (!workspace) {
      return;
    }
    await rebuildWorkspacePreview(workspace.workspace_id);
  }

  async function handleRollbackRun(runId: string) {
    if (!workspace) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const rolledBackRun = await rollbackRun(runId);
      const rolledBackWorkspace = await openWorkspace(workspace.workspace_id);
      setWorkspace(rolledBackWorkspace);
      setWorkspaces((current) =>
        current.map((item) => (item.workspace_id === rolledBackWorkspace.workspace_id ? rolledBackWorkspace : item)),
      );
      setRuns((current) => current.map((item) => (item.run_id === rolledBackRun.run_id ? rolledBackRun : item)));
      setSelectedRunId(rolledBackRun.run_id);
      await refreshWorkspaceState(rolledBackWorkspace.workspace_id);
      setRunArtifacts(await getRunArtifacts(rolledBackRun.run_id));
      await rebuildWorkspacePreview(rolledBackWorkspace.workspace_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to roll back run.");
    } finally {
      setLoading(false);
    }
  }

  async function handleStopRun(runId: string) {
    setError("");
    setStoppingRunId(runId);
    try {
      const stoppedRun = await stopRun(runId);
      setRuns((current) => current.map((run) => (run.run_id === stoppedRun.run_id ? stoppedRun : run)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to stop run.");
    } finally {
      setStoppingRunId("");
    }
  }

  function handleRefreshRolePreview(role: RoleKey) {
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

  function toggleRole(role: RoleKey) {
    setSelectedRoles((current) => ({ ...current, [role]: !current[role] }));
  }

  function openRunDetails(runId: string) {
    setSelectedRunId(runId);
    setRunDetailsOpen(true);
  }

  return (
    <div className="page">
      {showGlobalLoader ? (
        <div className="global-loader-overlay" role="status" aria-live="polite">
          <div className="global-loader-card">
            <div className="global-loader-spinner" />
            <strong>{creatingWorkspace ? "Creating workspace..." : "Preparing workspace..."}</strong>
            <p>Bootstrapping files, runs, and preview context.</p>
          </div>
        </div>
      ) : null}

      <div
        className={`workspace-drawer-backdrop ${workspaceDrawerOpen ? "is-open" : ""}`}
        onClick={() => setWorkspaceDrawerOpen(false)}
      />
      <div
        className={`issues-drawer-backdrop ${issuesDrawerOpen ? "is-open" : ""}`}
        onClick={() => setIssuesDrawerOpen(false)}
      />
      <div
        className={`run-details-backdrop ${runDetailsOpen ? "is-open" : ""}`}
        onClick={() => setRunDetailsOpen(false)}
      />
      <aside className={`workspace-drawer ${workspaceDrawerOpen ? "is-open" : ""}`} aria-hidden={!workspaceDrawerOpen}>
        <div className="workspace-drawer-head">
          <strong>Workspaces</strong>
          <button type="button" className="icon-btn ghost" aria-label="Close drawer" onClick={() => setWorkspaceDrawerOpen(false)}>
            ×
          </button>
        </div>
        <button type="button" className="workspace-create" onClick={handleCreateWorkspace} disabled={creatingWorkspace}>
          {creatingWorkspace ? "Creating..." : "Create New"}
        </button>
        <label className="workspace-search">
          <span>Search workspaces</span>
          <input
            type="search"
            value={workspaceSearch}
            onChange={(event) => setWorkspaceSearch(event.target.value)}
            placeholder="Research Workspace"
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
                {deletingWorkspaceId === item.workspace_id ? "…" : "⌫"}
              </button>
            </div>
          ))}
          {!filteredWorkspaces.length ? <p className="workspace-search-empty">No workspaces found.</p> : null}
        </div>
      </aside>
      <aside className={`issues-drawer ${issuesDrawerOpen ? "is-open" : ""}`} aria-hidden={!issuesDrawerOpen}>
        <div className="issues-drawer-head">
          <strong>Run Issues</strong>
          <button type="button" className="icon-btn ghost" aria-label="Close issues drawer" onClick={() => setIssuesDrawerOpen(false)}>
            ×
          </button>
        </div>
        <div className="issues-drawer-body">
          {visibleIssues.map((issue, index) => (
            <div key={`${issue.code ?? "issue"}-${index}`} className="issue-sheet">
              <strong>{issue.code ?? issue.severity ?? "issue"}</strong>
              <p>{issue.message ?? "Validator issue"}</p>
            </div>
          ))}
        </div>
      </aside>
      <aside className={`run-details-modal ${runDetailsOpen ? "is-open" : ""}`} aria-hidden={!runDetailsOpen}>
        <div className="run-details-head">
          <div className="run-details-head-copy">
            <strong>Run Details</strong>
            <span>{selectedRun ? formatTimestamp(selectedRun.created_at) : "No run selected"}</span>
          </div>
          <button type="button" className="icon-btn ghost" aria-label="Close run details" onClick={() => setRunDetailsOpen(false)}>
            ×
          </button>
        </div>
        {selectedRun ? (
          <div className="run-details-body">
            <div className="run-details-grid">
              <div className="run-detail-card">
                <span>Status</span>
                <strong>{displayRunStatus(selectedRun)}</strong>
              </div>
              <div className="run-detail-card">
                <span>Stage</span>
                <strong>{selectedRun.current_stage || "n/a"}</strong>
              </div>
              <div className="run-detail-card">
                <span>Role scope</span>
                <strong>{formatRoleScope(selectedRun.target_role_scope)}</strong>
              </div>
              <div className="run-detail-card">
                <span>Generation mode</span>
                <strong>{selectedRun.generation_mode || "n/a"}</strong>
              </div>
              <div className="run-detail-card">
                <span>Files touched</span>
                <strong>{selectedRun.touched_files.length}</strong>
              </div>
              <div className="run-detail-card">
                <span>Iterations</span>
                <strong>{selectedRun.iteration_count}</strong>
              </div>
              <div className="run-detail-card">
                <span>Apply status</span>
                <strong>{selectedRun.apply_status}</strong>
              </div>
            </div>

            <section className="run-detail-section">
              <h4>Prompt</h4>
              <pre className="json-block">{selectedRun.prompt}</pre>
            </section>

            {runDetailSummary ? (
              <section className="run-detail-section">
                <h4>Summary</h4>
                <p>{runDetailSummary}</p>
              </section>
            ) : null}

            {selectedRun.failure_reason ? (
              <section className="run-detail-section">
                <h4>Failure reason</h4>
                <p>{selectedRun.failure_reason}</p>
                {selectedRun.status !== "completed" ? (
                  <button type="button" className="ghost-action" onClick={() => void handleRunFix(selectedRun)}>
                    Open In fixbug Mode
                  </button>
                ) : null}
              </section>
            ) : null}

            {failureAnalysis?.failure_class || failureAnalysis?.root_cause_summary || failureAnalysis?.fix_targets?.length ? (
              <section className="run-detail-section">
                <h4>Error analysis</h4>
                <div className="run-details-grid">
                  <div className="run-detail-card">
                    <span>Run mode</span>
                    <strong>{failureAnalysis.mode ?? selectedRun.mode ?? "generate"}</strong>
                  </div>
                  <div className="run-detail-card">
                    <span>Failure class</span>
                    <strong>{failureAnalysis.failure_class ?? "n/a"}</strong>
                  </div>
                  <div className="run-detail-card run-detail-card-wide">
                    <span>Failure signature</span>
                    <strong>{failureAnalysis.failure_signature ?? selectedRun.failure_signature ?? "n/a"}</strong>
                  </div>
                  <div className="run-detail-card">
                    <span>fixbug phase</span>
                    <strong>{displayFixPhase(selectedRun, failureAnalysis.current_fix_phase ?? selectedRun.current_fix_phase)}</strong>
                  </div>
                  <div className="run-detail-card">
                    <span>Repair attempts</span>
                    <strong>{fixAttemptItems.length || selectedRun.repair_iterations?.length || 0}</strong>
                  </div>
                  <div className="run-detail-card">
                    <span>Exit code</span>
                    <strong>{failureAnalysis.current_exit_code ?? selectedRun.current_exit_code ?? "n/a"}</strong>
                  </div>
                </div>
                {failureAnalysis.root_cause_summary ? <p>{failureAnalysis.root_cause_summary}</p> : null}
                {failureAnalysis.current_failing_command ? (
                  <pre className="json-block">{failureAnalysis.current_failing_command}</pre>
                ) : null}
                {failureAnalysis.fix_targets?.length ? (
                  <div className="run-detail-list">
                    {failureAnalysis.fix_targets.map((target) => (
                      <div key={target} className="run-detail-item">
                        <strong>{target}</strong>
                      </div>
                    ))}
                  </div>
                ) : null}
              </section>
            ) : null}

            {selectedRun.mode === "fix" ? (
              <section className="run-detail-section">
                <h4>fixbug attempts</h4>
                {fixAttemptItems.length ? (
                  <div className="run-detail-list">
                    {fixAttemptItems.map((attempt, index) => {
                      const commands = asStringArray(attempt["commands"]);
                      const filesChanged = asStringArray(attempt["files_changed"]);
                      return (
                        <div key={`${String(attempt["fix_attempt_id"] ?? "attempt")}-${index}`} className="run-detail-item">
                          <div className="run-detail-item-top">
                            <strong>Attempt {String(attempt["attempt"] ?? index + 1)}</strong>
                            <span>{String(attempt["result"] ?? "patched")}</span>
                          </div>
                          {attempt["diagnosis"] ? <p>{String(attempt["diagnosis"])}</p> : null}
                          {attempt["failure_signature"] ? <p>Signature: {String(attempt["failure_signature"])}</p> : null}
                          {commands.length ? <p>Commands: {commands.join(" · ")}</p> : null}
                          {filesChanged.length ? <p>Files changed: {filesChanged.join(", ")}</p> : null}
                          {attempt["expected_verification"] ? <p>Expected verification: {String(attempt["expected_verification"])}</p> : null}
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="muted">No fix attempts recorded yet.</p>
                )}
                {scopeExpansionItems.length ? (
                  <div className="run-detail-list">
                    {scopeExpansionItems.map((item, index) => (
                      <div key={`scope-expansion-${index}`} className="run-detail-item">
                        <strong>Scope expansion {String(item["attempt"] ?? index + 1)}</strong>
                        <p>{asStringArray(item["files"]).join(", ") || "No files recorded."}</p>
                        {item["reason"] ? <p>{String(item["reason"])}</p> : null}
                      </div>
                    ))}
                  </div>
                ) : null}
                {fixCase ? <pre className="json-block">{JSON.stringify(fixCase, null, 2)}</pre> : null}
              </section>
            ) : null}

            <section className="run-detail-section">
              <h4>Checks</h4>
              <div className="run-details-grid">
                <div className="run-detail-card">
                  <span>Validators</span>
                  <strong>{selectedRun.checks_summary.validators}</strong>
                </div>
                <div className="run-detail-card">
                  <span>Build</span>
                  <strong>{selectedRun.checks_summary.build}</strong>
                </div>
                <div className="run-detail-card">
                  <span>Preview</span>
                  <strong>{selectedRun.checks_summary.preview}</strong>
                </div>
              </div>
              {selectedRun.checks_summary.issues.length ? (
                <div className="run-detail-list">
                  {selectedRun.checks_summary.issues.map((issue, index) => (
                    <div key={`${issue.code ?? "issue"}-${index}`} className="run-detail-item">
                      <strong>{issue.code ?? issue.severity ?? "issue"}</strong>
                      <p>{issue.message ?? "Validator issue"}</p>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted">No validator issues recorded.</p>
              )}
            </section>

            <section className="run-detail-section">
              <h4>Files</h4>
              {selectedRun.touched_files.length ? (
                <div className="run-detail-list">
                  {selectedRun.touched_files.map((filePath) => (
                    <div key={filePath} className="run-detail-item">
                      <strong>{filePath}</strong>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted">No files were changed.</p>
              )}
            </section>

            <section className="run-detail-section">
              <h4>Iterations</h4>
              {runArtifacts?.iterations?.length ? (
                <div className="run-detail-list">
                  {runArtifacts.iterations.map((iteration) => (
                    <div key={iteration.iteration_id} className="run-detail-item">
                      <div className="run-detail-item-top">
                        <strong>{formatTimestamp(iteration.created_at)}</strong>
                        <span>{formatRoleScope(iteration.role_scope)}</span>
                      </div>
                      <p>{iteration.assistant_message}</p>
                      {iteration.operations.length ? (
                        <p>
                          {iteration.operations.map((operation) => `${operation.operation} ${operation.file_path}`).join(", ")}
                        </p>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted">No iteration details recorded yet.</p>
              )}
            </section>
          </div>
        ) : (
          <div className="run-details-body">
            <p className="muted">Select a run to inspect its details.</p>
          </div>
        )}
      </aside>

      <div className="page-scale">
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
              <p className="eyebrow">Agentic Mini-App Workspace</p>
              <h1 className="topbar-heading">AI draft workspace for grounded mini-app code</h1>
            </div>
          </div>
          <div className="topbar-meta">
            <div className="status-pill">run {topbarStatus}</div>
            {visibleIssues.length ? (
              <button type="button" className="issues-pill" onClick={() => setIssuesDrawerOpen(true)}>
                issues {visibleIssues.length}
              </button>
            ) : null}
          </div>
        </div>

        <div className="layout">
        <section className="panel panel-chat">
          <header className="panel-header">
            <div className="panel-header-row">
              <h2>{composerTitle}</h2>
              <div className="panel-help">
                <button type="button" className="panel-help-trigger" aria-label="Task input help">
                  ?
                </button>
                <div className="panel-help-tooltip" role="tooltip">
                  {composerHelp}
                </div>
              </div>
            </div>
          </header>

          <form onSubmit={handleRun} className="composer-form">
            <div className="composer-mode-switch" role="tablist" aria-label="Run mode">
              <button
                type="button"
                className={`composer-mode-pill ${selectedRunMode === "generate" ? "is-active" : ""}`}
                onClick={() => {
                  setSelectedRunMode("generate");
                  setFixErrorContext(null);
                }}
              >
                Generate
              </button>
              <button
                type="button"
                className={`composer-mode-pill ${selectedRunMode === "fix" ? "is-active" : ""}`}
                onClick={() => {
                  setSelectedRunMode("fix");
                  setSelectedGenerationMode("balanced");
                }}
              >
                fixbug
              </button>
            </div>
            <label className="composer-field">
              <span>{selectedRunMode === "fix" ? "Error or failure context" : "Prompt"}</span>
              <textarea
                value={prompt}
                onChange={(event) => {
                  const nextValue = event.target.value;
                  setPrompt(nextValue);
                  if (selectedRunMode === "fix") {
                    setFixErrorContext((current) => ({
                      raw_error: nextValue,
                      source: current?.source ?? inferFixSource(nextValue),
                      failing_target: current?.failing_target,
                    }));
                  }
                }}
                rows={9}
                placeholder={composerPlaceholder}
              />
            </label>

            <div className="role-scope">
              <span>Role scope</span>
              <div className="role-pill-row">
                {ROLE_ORDER.map((role) => (
                  <button
                    key={role}
                    type="button"
                    className={`role-pill ${selectedRoles[role] ? "is-active" : ""}`}
                    aria-pressed={selectedRoles[role]}
                    onClick={() => toggleRole(role)}
                  >
                    <span className={`role-pill-check ${selectedRoles[role] ? "is-active" : ""}`} aria-hidden="true">
                      {selectedRoles[role] ? "✓" : ""}
                    </span>
                    <span className="role-pill-label">{ROLE_LABELS[role]}</span>
                  </button>
                ))}
              </div>
            </div>

            <label className="composer-field">
              <span>Generation mode</span>
              <select
                value={effectiveGenerationMode}
                disabled={selectedRunMode === "fix"}
                onChange={(event) => setSelectedGenerationMode(event.target.value as UserGenerationMode)}
              >
                <option value="fast">Fast</option>
                <option value="balanced">Balanced</option>
                <option value="quality">Quality</option>
              </select>
            </label>

            <button className="generate-full" type="submit" disabled={!workspace || loading}>
              {loading ? "Running..." : selectedRunMode === "fix" ? "Analyze and fix" : "Generate and apply"}
            </button>
          </form>

          {error ? <p className="error">{error}</p> : null}

          <div className="runs-panel">
            <div className="runs-panel-head">
              <h3>Run Timeline</h3>
              <span>{runs.length} total</span>
            </div>
            <div className="run-list">
              {runs.length ? (
                runs.map((run) => {
                  const runStatus = displayRunStatus(run);
                  const visualProgress = displayProgressForRun(run, runProgressDisplay);
                  const canRollbackRun =
                    Boolean(run.result_revision_id) &&
                    run.status === "completed" &&
                    run.apply_status === "applied" &&
                    !run.rolled_back &&
                    workspace?.current_revision_id === run.result_revision_id;
                  const canStopRun = run.status === "running";
                  const isStoppingRun = stoppingRunId === run.run_id || run.current_stage === "stopping";

                  return (
                    <div
                      key={run.run_id}
                      className={`run-card ${selectedRunId === run.run_id ? "is-active" : ""} ${run.rolled_back ? "is-muted" : ""}`}
                    >
                      <button
                        type="button"
                        className="run-card-select"
                        onClick={() => openRunDetails(run.run_id)}
                      >
                        <div className="run-card-top">
                          <strong>{run.intent.replaceAll("_", " ")}</strong>
                          <span className={`run-status ${runStatus === "rolled_back" ? "rolled-back" : runStatus}`}>
                            {runStatus}
                          </span>
                        </div>
                        <p className="run-card-copy">{clampText(run.prompt, 120)}</p>
                        <div className="run-progress">
                          <div className="run-progress-bar">
                            <div className="run-progress-fill" style={{ width: `${visualProgress}%` }} />
                          </div>
                          <div className="run-progress-meta">
                            <span>{run.current_stage}</span>
                            <span>{visualProgress}%</span>
                          </div>
                        </div>
                        <div className="run-card-meta">
                          <span>{formatTimestamp(run.created_at)}</span>
                          <span>{run.touched_files.length} files</span>
                        </div>
                      </button>
                      {run.status === "completed" || canStopRun || run.status === "failed" || run.status === "blocked" ? (
                        <div className="run-card-actions">
                          {canStopRun ? (
                            <button
                              type="button"
                              className="ghost-action run-card-action run-card-stop"
                              onClick={() => {
                                void handleStopRun(run.run_id);
                              }}
                              disabled={isStoppingRun}
                            >
                              {isStoppingRun ? "Stopping..." : "Stop"}
                            </button>
                          ) : null}
                          {(run.status === "failed" || run.status === "blocked") ? (
                            <button
                              type="button"
                              className="ghost-action run-card-action"
                              onClick={() => void handleRunFix(run)}
                            >
                              Fix
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="ghost-action run-card-action"
                            onClick={() => {
                              void handleRollbackRun(run.run_id);
                            }}
                            disabled={!canRollbackRun || loading}
                          >
                            {run.rolled_back ? "Rolled back" : "Rollback"}
                          </button>
                        </div>
                      ) : null}
                    </div>
                  );
                })
              ) : (
                <p className="muted">No runs yet. Start with a prompt to create the first draft.</p>
              )}
            </div>
          </div>
        </section>

        <section className="panel panel-preview">
          <div className="preview-toolbar">
            <div className="preview-toolbar-main">
              <header>
                <h2>Workspace Orchestrator</h2>
                <p className="toolbar-subtitle">
                  Preview stays live while each run exposes a draft diff, editable files, and logs.
                </p>
              </header>
              <div className="tabs">
                {(["preview", "code", "diff", "logs"] as const).map((tab) => (
                  <button key={tab} type="button" className={tab === activeTab ? "active" : ""} onClick={() => setActiveTab(tab)}>
                    {tab}
                  </button>
                ))}
              </div>
            </div>
            <div className="preview-toolbar-actions">
              <button type="button" className="ghost-action" onClick={handleRefreshPreview} disabled={!workspace || previewBooting}>
                {previewBooting ? "Rebuilding..." : "Rebuild preview"}
              </button>
            </div>
          </div>
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
            <DiffViewer text={diffText || ""} />
          ) : null}

          {activeTab === "preview" ? (
            <div className="preview-grid">
              {ROLE_ORDER.map((role) => (
                <div key={role} className="preview-column">
                  <div className="preview-heading">
                    <strong>{role}</strong>
                    <span>{previewRuntimeMode || "runtime"} preview</span>
                  </div>
                  <div className="phone-shell">
                    <div className="mockup-topbar">
                      <button
                        type="button"
                        className="mockup-pill mockup-pill-primary"
                        onClick={() => handleMockupPrimaryAction(role)}
                        disabled={!previewUrl}
                      >
                        <span
                          className={`mockup-icon ${isRoleAtRootPreviewPath(role, rolePreviewPath[role]) ? "is-close" : "is-back"}`}
                          aria-hidden="true"
                        />
                        <span>{isRoleAtRootPreviewPath(role, rolePreviewPath[role]) ? "Close" : "Back"}</span>
                      </button>
                      <div className="mockup-menu-wrap">
                        <button
                          type="button"
                          className="mockup-pill mockup-pill-menu"
                          onClick={() => setPreviewMenuRole((current) => (current === role ? null : role))}
                          aria-label={`Open ${role} preview menu`}
                          disabled={!previewUrl}
                        >
                          <span className="mockup-chevron-icon" aria-hidden="true">
                            <svg viewBox="0 0 20 20" focusable="false" aria-hidden="true">
                              <path
                                d="M4.5 7.5 10 13l5.5-5.5"
                                fill="none"
                                stroke="currentColor"
                                strokeWidth="3.2"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </span>
                          <span className="mockup-dots" aria-hidden="true">
                            <i />
                            <i />
                            <i />
                          </span>
                        </button>
                        {previewMenuRole === role ? (
                          <div className="mockup-menu">
                            <button type="button" onClick={() => handleMockupRefresh(role)} disabled={!previewUrl || previewLoading[role]}>
                              <span className="mockup-refresh-icon" aria-hidden="true">
                                <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                                  <path
                                    d="M12 5a7 7 0 1 1-6.6 9.3"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                    strokeLinecap="round"
                                  />
                                  <path
                                    d="M5 4.5v5h5"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                  />
                                </svg>
                              </span>
                              <span>Refresh page</span>
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </div>
                    {previewStatus === "error" ? (
                      <div className="preview-loader preview-error" role="status" aria-live="polite">
                        <div className="preview-loader-card preview-error-card">
                          <strong>Preview failed</strong>
                          <p>{previewErrorMessage ?? "Runtime did not start successfully. Try Rebuild preview."}</p>
                        </div>
                      </div>
                    ) : previewStatus !== "running" ? (
                      <div className="preview-loader" role="status" aria-live="polite">
                        <div className="preview-loader-card">
                          <div className="preview-loader-spinner" aria-hidden="true" />
                          <strong>Loading preview</strong>
                          <p>Starting the workspace container and waiting for a healthy response.</p>
                        </div>
                      </div>
                    ) : !previewUrl && previewFailed[role] ? (
                      <div className="preview-loader preview-error" role="status" aria-live="polite">
                        <div className="preview-loader-card preview-error-card">
                          <strong>Preview did not start</strong>
                          <p>Runtime was not detected automatically. Try Rebuild preview.</p>
                        </div>
                      </div>
                    ) : previewUrl ? (
                      <>
                        {previewLoading[role] ? (
                          <div className="preview-loader">
                            <div className="preview-loader-card">
                              <div className="preview-loader-spinner" aria-hidden="true" />
                              <strong>Loading preview</strong>
                              <p>Starting runtime and connecting this screen.</p>
                            </div>
                          </div>
                        ) : null}
                        <iframe
                          key={`${role}-${previewCycle}-${rolePreviewCycle[role]}-${rolePreviewUrls[role] ?? previewUrl}`}
                          title={`Live preview ${role}`}
                          src={rolePreviewUrls[role] ?? `${previewUrl}?role=${role}`}
                          ref={(node) => {
                            previewFrameRefs.current[role] = node;
                          }}
                          className={previewLoading[role] ? "is-loading" : ""}
                          onLoad={() =>
                            window.setTimeout(() => {
                              clearPreviewTimeout(role);
                              setPreviewFailed((current) => ({ ...current, [role]: false }));
                              setPreviewLoading((current) => ({ ...current, [role]: false }));
                            }, 350)
                          }
                        />
                        {previewFailed[role] && previewErrorMessage ? (
                          <div className="preview-loader preview-error" role="status" aria-live="polite">
                            <div className="preview-loader-card preview-error-card">
                              <strong>Preview did not load</strong>
                              <p>{previewErrorMessage}</p>
                            </div>
                          </div>
                        ) : previewFailed[role] ? (
                          <div className="preview-loader" role="status" aria-live="polite">
                            <div className="preview-loader-card">
                              <div className="preview-loader-spinner" aria-hidden="true" />
                              <strong>Loading preview</strong>
                              <p>Runtime is still starting. Waiting for this screen to become available.</p>
                            </div>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <div className="preview-loader">
                        <div className="preview-loader-card">
                          <div className="preview-loader-spinner" aria-hidden="true" />
                          <strong>Loading preview</strong>
                          <p>Starting runtime and connecting this screen.</p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {activeTab === "logs" ? (
            <div className="terminal logs-terminal">
              <div className="logs-shell">
                <div className="container-status-grid">
                  {containerStatuses.length ? (
                    containerStatuses.map((container) => (
                      <button
                        key={container.service}
                        type="button"
                        className={`container-status-card${selectedLogService === container.service ? " is-active" : ""}`}
                        onClick={() =>
                          setSelectedLogService((current) => (current === container.service ? "" : container.service))
                        }
                      >
                        <div className="container-status-card-head">
                          <strong>{container.service}</strong>
                          <span className="container-status-meta">
                            {container.health ?? container.status ?? container.state ?? "unknown"}
                          </span>
                        </div>
                        <small>
                          {container.state ? `state: ${container.state}` : "state: unknown"}
                          {container.exit_code ? ` · exit: ${container.exit_code}` : ""}
                        </small>
                      </button>
                    ))
                  ) : (
                    <div className="container-status-empty">No container status yet.</div>
                  )}
                </div>
                <div className="container-logs-panel">
                  <div className="container-logs-header">
                    <strong>{selectedLogService || "events"}</strong>
                    <span>
                      {selectedLogService
                        ? selectedContainerLogLines.length
                          ? `${selectedContainerLogLines.length} log lines`
                          : "No logs yet"
                        : eventLogLines.length
                          ? `${eventLogLines.length} events`
                          : "No events yet"}
                    </span>
                  </div>
                  <pre>
                    {selectedLogService
                      ? selectedContainerLogLines.length
                        ? selectedContainerLogLines.join("\n")
                        : "No logs for this container yet."
                      : eventLogLines.join("\n")}
                  </pre>
                </div>
              </div>
            </div>
          ) : null}
        </section>
        </div>
      </div>
    </div>
  );
}
