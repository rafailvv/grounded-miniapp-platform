import { FormEvent, useEffect, useMemo, useState } from "react";

import { ensureWorkspace, request, Workspace } from "./lib/api";
import "./styles/app.css";

type Job = {
  job_id: string;
  workspace_id: string;
  status: string;
  summary?: string | null;
  assumptions_report: Array<{ text: string; rationale: string }>;
  validation_snapshot?: {
    grounded_spec_valid: boolean;
    app_ir_valid: boolean;
    build_valid: boolean;
    blocking: boolean;
    issues: Array<{ code: string; message: string }>;
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
};

const INITIAL_PROMPT =
  "Build a consultation booking form mini-app with name, phone, preferred date and comment fields.";

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");
  const [expandedDirectories, setExpandedDirectories] = useState<Set<string>>(new Set());

  useEffect(() => {
    ensureWorkspace()
      .then(setWorkspace)
      .catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    if (!workspace) {
      return;
    }
    void refreshWorkspace(workspace.workspace_id);
  }, [workspace]);

  const changedFiles = useMemo(
    () => files.filter((entry) => entry.type === "file" && entry.path.startsWith("artifacts/")).length,
    [files],
  );
  const fileTree = useMemo(() => buildFileTree(files), [files]);
  const editorStats = useMemo(() => {
    const lines = fileContent ? fileContent.split("\n").length : 0;
    const characters = fileContent.length;
    return { lines, characters };
  }, [fileContent]);

  async function refreshWorkspace(workspaceId: string) {
    const [tree, chatTurns, validationPayload, previewPayload] = await Promise.all([
      request<FileEntry[]>(`/workspaces/${workspaceId}/files/tree`),
      request<ChatTurn[]>(`/workspaces/${workspaceId}/chat/turns`),
      request<Record<string, unknown> | null>(`/workspaces/${workspaceId}/validation/current`),
      request<PreviewInfo>(`/workspaces/${workspaceId}/preview/url`),
    ]);
    setFiles(tree);
    setTurns(chatTurns);
    setValidation(validationPayload);
    setPreviewUrl(previewPayload.url ?? "");
    setRolePreviewUrls(previewPayload.role_urls ?? {});
    setExpandedDirectories((current) => {
      if (current.size > 0) {
        return current;
      }
      return new Set(collectExpandedDirectories(buildFileTree(tree)).slice(0, 8));
    });
  }

  async function handleGenerate(event: FormEvent) {
    event.preventDefault();
    if (!workspace) {
      return;
    }
    setLoading(true);
    setError("");
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
        }),
      });
      setJob(nextJob);
      await request(`/workspaces/${workspace.workspace_id}/preview/start`, { method: "POST" });
      await refreshWorkspace(workspace.workspace_id);
      setActiveTab("preview");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Generation failed.");
    } finally {
      setLoading(false);
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
              <span>Assumptions</span>
              <strong>{job?.assumptions_report?.length ?? 0}</strong>
              <div className="artifact-popover">
                {job?.assumptions_report?.length ? (
                  job.assumptions_report.map((assumption, index) => (
                    <div key={`${assumption.text}-${index}`} className="artifact-popover-item">
                      <strong>{assumption.text}</strong>
                      <p>{assumption.rationale}</p>
                    </div>
                  ))
                ) : (
                  <p>No explicit assumptions recorded yet.</p>
                )}
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
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={8} />
            <div className="actions">
              <button type="submit" disabled={loading || !workspace}>
                {loading ? "Generating..." : "Generate"}
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
          <div className="warnings">
            <h3>Assumptions / warnings</h3>
            {job?.assumptions_report?.length ? (
              job.assumptions_report.map((assumption, index) => (
                <div key={`${assumption.text}-${index}`} className="warning-card">
                  <strong>{assumption.text}</strong>
                  <p>{assumption.rationale}</p>
                </div>
              ))
            ) : (
              <p className="muted">No explicit assumptions recorded yet.</p>
            )}
            {error ? <p className="error">{error}</p> : null}
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
              {(["client", "specialist", "manager"] as const).map((role) => (
                <div key={role} className="preview-column">
                  <div className="preview-heading">
                    <strong>{role}</strong>
                    <span>{role === "client" ? "Role 1" : role === "specialist" ? "Role 2" : "Role 3"}</span>
                  </div>
                  <div className="phone-shell">
                    {previewUrl ? (
                      <iframe
                        title={`Live preview ${role}`}
                        src={rolePreviewUrls[role] ?? `${previewUrl}?role=${role}`}
                      />
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
              <pre>{JSON.stringify(job?.validation_snapshot ?? validation ?? {}, null, 2)}</pre>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
