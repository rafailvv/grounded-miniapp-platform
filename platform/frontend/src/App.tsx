import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  createRun,
  deleteWorkspace,
  ensureWorkspace,
  getRunArtifacts,
  listRuns,
  listWorkspaces,
  openWorkspace,
  rebuildPreview,
  request,
  Run,
  RunArtifacts,
  SystemConfiguration,
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
};

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

const DEFAULT_PROMPT = `Build or refine the current mini-app as a real AI-assisted product. Keep client, specialist, and manager distinct, preserve existing code when possible, and produce code changes that pass validators and preview.`;

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

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceDrawerOpen, setWorkspaceDrawerOpen] = useState(false);
  const [workspaceSearch, setWorkspaceSearch] = useState("");
  const [workspaceTransitioning, setWorkspaceTransitioning] = useState(true);
  const [creatingWorkspace, setCreatingWorkspace] = useState(false);
  const [deletingWorkspaceId, setDeletingWorkspaceId] = useState("");
  const [initializing, setInitializing] = useState(true);
  const [systemConfig, setSystemConfig] = useState<SystemConfiguration | null>(null);
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [selectedRoles, setSelectedRoles] = useState<Record<RoleKey, boolean>>({
    client: true,
    specialist: true,
    manager: true,
  });
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runArtifacts, setRunArtifacts] = useState<RunArtifacts | null>(null);
  const [activeTab, setActiveTab] = useState<"preview" | "code" | "diff" | "research">("preview");
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
  const [previewBooting, setPreviewBooting] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
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
        setWorkspace(nextWorkspace);
        setWorkspaces(await listWorkspaces());
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
    void refreshWorkspaceState(workspace.workspace_id);
  }, [workspace]);

  useEffect(() => {
    if (!selectedRunId) {
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
      } catch (err) {
        if (!cancelled) {
          setRunArtifacts(null);
          setError(err instanceof Error ? err.message : "Failed to load run artifacts.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

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
  const touchedFilesCount = topbarRun?.touched_files.length ?? 0;
  const editorStats = useMemo(() => {
    const lines = fileContent ? fileContent.split("\n").length : 0;
    const characters = fileContent.length;
    return { lines, characters };
  }, [fileContent]);
  const visibleIssues = topbarRun?.checks_summary.issues ?? [];
  const activeRoleScope = useMemo(
    () => ROLE_ORDER.filter((role) => selectedRoles[role]),
    [selectedRoles],
  );
  const diffText = runArtifacts?.diff ?? "";
  const groundedActorsCount = Array.isArray((runArtifacts?.grounded_spec as { actors?: unknown[] } | null | undefined)?.actors)
    ? ((runArtifacts?.grounded_spec as { actors?: unknown[] }).actors ?? []).length
    : 0;
  const appIrScreensCount = Array.isArray((runArtifacts?.app_ir as { screens?: unknown[] } | null | undefined)?.screens)
    ? ((runArtifacts?.app_ir as { screens?: unknown[] }).screens ?? []).length
    : 0;

  async function refreshWorkspaceState(workspaceId: string, preferredRunId?: string) {
    setWorkspaceTransitioning(true);
    const [treeResult, previewResult, runsResult] = await Promise.allSettled([
      request<FileEntry[]>(`/workspaces/${workspaceId}/files/tree`),
      request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`),
      listRuns(workspaceId),
    ]);

    const refreshErrors: string[] = [];
    let nextRuns: Run[] = [];

    if (treeResult.status === "fulfilled") {
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

    if (previewResult.status === "fulfilled") {
      const previewPayload = previewResult.value;
      setPreviewUrl(previewPayload.url ?? "");
      setRolePreviewUrls(previewPayload.role_urls ?? {});
      setPreviewRuntimeMode(previewPayload.runtime_mode ?? "");
      setPreviewStatus(previewPayload.status ?? "");
      if (previewPayload.url) {
        setPreviewCycle((current) => current + 1);
        setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
        setPreviewFailed({
          client: false,
          specialist: false,
          manager: false,
        });
      } else {
        setPreviewLoading({
          client: false,
          specialist: false,
          manager: false,
        });
      }
    } else {
      setPreviewUrl("");
      setRolePreviewUrls({});
      setPreviewStatus("error");
      refreshErrors.push(`preview: ${previewResult.reason instanceof Error ? previewResult.reason.message : "failed to load"}`);
    }

    if (runsResult.status === "fulfilled") {
      nextRuns = runsResult.value;
      setRuns(nextRuns);
      const nextSelectedRunId =
        preferredRunId && nextRuns.some((run) => run.run_id === preferredRunId)
          ? preferredRunId
          : selectedRunId && nextRuns.some((run) => run.run_id === selectedRunId)
            ? selectedRunId
            : nextRuns[0]?.run_id ?? "";
      setSelectedRunId(nextSelectedRunId);
    } else {
      refreshErrors.push(`runs: ${runsResult.reason instanceof Error ? runsResult.reason.message : "failed to load"}`);
    }

    setWorkspaceTransitioning(false);
    setError(refreshErrors.length ? refreshErrors.join(" | ") : "");
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
      const run = await createRun(workspace.workspace_id, {
        prompt: prompt.trim(),
        intent: "auto",
        apply_strategy: "staged_auto_apply",
        target_role_scope: activeRoleScope,
        model_profile: systemConfig?.default_coding_profile ?? systemConfig?.defaults.model_profile ?? "openai_code_fast",
        generation_mode: systemConfig?.defaults.generation_mode ?? "quality",
      });
      setSelectedRunId(run.run_id);
      await refreshWorkspaceState(workspace.workspace_id, run.run_id);
      setRunArtifacts(await getRunArtifacts(run.run_id));
      setActiveTab("preview");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to execute run.");
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
      `/workspaces/${workspace.workspace_id}/files/content?path=${encodeURIComponent(path)}`,
    );
    setFileContent(payload.content);
    setActiveTab("code");
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
      setWorkspace(nextWorkspace);
      setRuns([]);
      setSelectedRunId("");
      setRunArtifacts(null);
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
      setWorkspace(nextWorkspace);
      setRuns([]);
      setSelectedRunId("");
      setRunArtifacts(null);
      await refreshWorkspaceList(nextWorkspace.workspace_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create workspace.");
    } finally {
      setCreatingWorkspace(false);
    }
  }

  async function handleDeleteWorkspace(workspaceId: string) {
    if (deletingWorkspaceId) {
      return;
    }
    setDeletingWorkspaceId(workspaceId);
    setError("");
    try {
      await deleteWorkspace(workspaceId);
      const listed = await listWorkspaces();
      setWorkspaces(listed);
      if (workspace?.workspace_id === workspaceId) {
        const fallback = listed[0] ? await openWorkspace(listed[0].workspace_id) : await ensureWorkspace();
        setWorkspace(fallback);
        setRuns([]);
        setSelectedRunId("");
        setRunArtifacts(null);
        await refreshWorkspaceList(fallback.workspace_id);
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

  async function handleRefreshPreview() {
    if (!workspace) {
      return;
    }
    setPreviewBooting(true);
    setPreviewLoading({ ...PREVIEW_BOOT_ROLES });
    try {
      await rebuildPreview(workspace.workspace_id);
      for (let attempt = 0; attempt < 20; attempt += 1) {
        const preview = await request<PreviewInfo>(`/workspaces/${workspace.workspace_id}/preview/url`);
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

  return (
    <div className="page">
      {initializing || workspaceTransitioning || creatingWorkspace ? (
        <div className="global-loader-overlay" role="status" aria-live="polite">
          <div className="global-loader-card">
            <div className="global-loader-spinner" />
            <strong>{creatingWorkspace ? "Creating workspace..." : "Preparing workspace..."}</strong>
            <p>Bootstrapping files, runs, and preview context.</p>
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
            <p className="eyebrow">Grounded Research Workspace</p>
            <h1 className="topbar-heading">AI code workspace for grounded mini-app generation</h1>
          </div>
        </div>
        <div className="topbar-meta">
          <div className="status-pill">{topbarRun?.status ?? "idle"}</div>
          <div className="artifact-strip">
            <div className="artifact-chip">
              <span>Model profile</span>
              <strong>{topbarRun?.model_profile ?? systemConfig?.default_coding_profile ?? "pending"}</strong>
            </div>
            <div className="artifact-chip">
              <span>Touched files</span>
              <strong>{touchedFilesCount}</strong>
            </div>
            <div className="artifact-chip">
              <span>Validators</span>
              <strong>{topbarRun?.checks_summary.validators ?? "pending"}</strong>
            </div>
            <div className="artifact-chip">
              <span>Build</span>
              <strong>{topbarRun?.checks_summary.build ?? "pending"}</strong>
            </div>
            <div className="artifact-chip">
              <span>Preview</span>
              <strong>{topbarRun?.checks_summary.preview ?? (previewStatus || "pending")}</strong>
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

      <div className="layout">
        <section className="panel panel-chat">
          <header className="panel-header">
            <h2>Task Composer</h2>
            <p>Describe what to create or change. The platform plans code edits, applies them to the current workspace, then validates and previews the result.</p>
          </header>

          <form onSubmit={handleRun} className="composer-form">
            <label className="composer-field">
              <span>Prompt</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                rows={9}
                placeholder="Describe the product change, workflow shift, or role-specific refinement."
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
                    onClick={() => toggleRole(role)}
                  >
                    {ROLE_LABELS[role]}
                  </button>
                ))}
              </div>
            </div>

            <button className="generate-full" type="submit" disabled={!workspace || loading}>
              {loading ? "Running..." : "Plan + Apply"}
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
                runs.map((run) => (
                  <button
                    key={run.run_id}
                    type="button"
                    className={`run-card ${selectedRunId === run.run_id ? "is-active" : ""}`}
                    onClick={() => setSelectedRunId(run.run_id)}
                  >
                    <div className="run-card-top">
                      <strong>{run.intent.replaceAll("_", " ")}</strong>
                      <span className={`run-status ${run.status}`}>{run.status}</span>
                    </div>
                    <p>{run.prompt}</p>
                    <div className="run-card-meta">
                      <span>{formatTimestamp(run.created_at)}</span>
                      <span>{run.touched_files.length} files</span>
                    </div>
                  </button>
                ))
              ) : (
                <p className="muted">No runs yet. Start with a prompt to create the first research trace.</p>
              )}
            </div>
          </div>

          <div className="suggestions">
            <h3>Run Issues</h3>
            {visibleIssues.length ? (
              visibleIssues.map((issue, index) => (
                <div key={`${issue.code ?? "issue"}-${index}`} className="suggestion-card">
                  <strong>{issue.code ?? issue.severity ?? "issue"}</strong>
                  <p>{issue.message ?? "Validator issue"}</p>
                </div>
              ))
            ) : (
              <p className="muted">No blocking issues in the selected run.</p>
            )}
          </div>
        </section>

        <section className="panel panel-preview">
          <div className="preview-toolbar">
            <div className="preview-toolbar-main">
              <header>
                <h2>Workspace Orchestrator</h2>
                <p className="toolbar-subtitle">
                  Preview stays live, but code, diff, and research artifacts are now first-class outputs of each run.
                </p>
              </header>
              <div className="tabs">
                {(["preview", "code", "diff", "research"] as const).map((tab) => (
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

          {activeTab === "research" ? (
            <div className="research-layout">
              <div className="research-card">
                <h3>Run Snapshot</h3>
                {selectedRun ? (
                  <div className="research-grid">
                    <div>
                      <span>Status</span>
                      <strong>{selectedRun.status}</strong>
                    </div>
                    <div>
                      <span>Intent</span>
                      <strong>{selectedRun.intent}</strong>
                    </div>
                    <div>
                      <span>Apply</span>
                      <strong>{selectedRun.apply_status}</strong>
                    </div>
                    <div>
                      <span>Model</span>
                      <strong>{selectedRun.llm_model ?? selectedRun.model_profile}</strong>
                    </div>
                  </div>
                ) : (
                  <p className="muted">Select a run to inspect its research artifacts.</p>
                )}
              </div>

              <div className="research-card">
                <h3>Code Change Plan</h3>
                {runArtifacts?.code_change_plan ? (
                  <>
                    <p>{runArtifacts.code_change_plan.summary}</p>
                    <div className="tag-list">
                      {(runArtifacts.code_change_plan.targets ?? []).map((target) => (
                        <span key={`${target.file_path}-${target.operation}`} className="tag">
                          {target.operation}: {target.file_path}
                        </span>
                      ))}
                    </div>
                    <div className="research-columns">
                      <div>
                        <h4>Risks</h4>
                        {(runArtifacts.code_change_plan.risks ?? []).length ? (
                          <ul className="plain-list">
                            {(runArtifacts.code_change_plan.risks ?? []).map((risk) => (
                              <li key={risk}>{risk}</li>
                            ))}
                          </ul>
                        ) : (
                          <p className="muted">No explicit risks recorded.</p>
                        )}
                      </div>
                      <div>
                        <h4>Acceptance Checks</h4>
                        {(runArtifacts.code_change_plan.acceptance_checks ?? []).length ? (
                          <ul className="plain-list">
                            {(runArtifacts.code_change_plan.acceptance_checks ?? []).map((check) => (
                              <li key={check}>{check}</li>
                            ))}
                          </ul>
                        ) : (
                          <p className="muted">No checks recorded.</p>
                        )}
                      </div>
                    </div>
                  </>
                ) : (
                  <p className="muted">Code change plan will appear after the first run.</p>
                )}
              </div>

              <div className="research-card">
                <h3>Artifact Summary</h3>
                <div className="research-grid">
                  <div>
                    <span>Grounded spec actors</span>
                    <strong>{groundedActorsCount}</strong>
                  </div>
                  <div>
                    <span>AppIR screens</span>
                    <strong>{appIrScreensCount}</strong>
                  </div>
                  <div>
                    <span>Trace entries</span>
                    <strong>{runArtifacts?.trace?.entries?.length ?? 0}</strong>
                  </div>
                  <div>
                    <span>Changed files</span>
                    <strong>{selectedRun?.touched_files.length ?? 0}</strong>
                  </div>
                </div>
                <pre className="json-block">{JSON.stringify(runArtifacts?.validation ?? {}, null, 2)}</pre>
              </div>
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
              <pre>{diffText || "No diff recorded for the selected run."}</pre>
            </div>
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
                    <button
                      type="button"
                      className="preview-refresh"
                      onClick={() => handleRefreshRolePreview(role)}
                      aria-label={`Refresh ${role} preview`}
                      disabled={!previewUrl || previewLoading[role]}
                    >
                      ↻
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
                              setPreviewFailed((current) => ({ ...current, [role]: false }));
                              setPreviewLoading((current) => ({ ...current, [role]: false }));
                            }, 350)
                          }
                        />
                      </>
                    ) : previewBooting || previewLoading[role] || loading || previewStatus === "starting" ? (
                      <div className="preview-loader">
                        <div className="preview-loader-spinner" />
                        <p>Loading runtime…</p>
                      </div>
                    ) : previewFailed[role] ? (
                      <div className="placeholder placeholder-error">
                        <strong>Failed to load preview.</strong>
                        <p>This role runtime did not open in time.</p>
                      </div>
                    ) : (
                      <div className="placeholder">Run the workspace to populate the preview.</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
