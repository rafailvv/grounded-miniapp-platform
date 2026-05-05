import type { WorkspaceMemory } from "../lib/api";

type MemoryPanelProps = {
  memory: WorkspaceMemory | null;
  formatTimestamp: (value?: string) => string;
};

export function MemoryPanel({ memory, formatTimestamp }: MemoryPanelProps) {
  const pipeline = memory?.pipeline;
  return (
    <div className="workbench-stack">
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Memory pipeline</strong>
          <span>{pipeline?.status || "empty"}</span>
        </div>
        <div className="trace-bundle-grid">
          <div>
            <strong>{pipeline?.stage1_count ?? 0}</strong>
            <span>runs</span>
          </div>
          <div>
            <strong>{pipeline?.stage1_items ?? 0}</strong>
            <span>stage items</span>
          </div>
          <div>
            <strong>{pipeline?.consolidated_at ? "yes" : "no"}</strong>
            <span>consolidated</span>
          </div>
        </div>
        {pipeline?.consolidated_at ? <p className="muted">Last consolidation: {formatTimestamp(pipeline.consolidated_at)}</p> : null}
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Workspace memory</strong>
          <span>{memory?.items.length ?? 0} notes</span>
        </div>
        <div className="run-detail-list">
          {memory?.items.length ? (
            memory.items.map((item) => (
              <div key={item.memory_id ?? item.text} className="run-detail-item">
                <div className="run-detail-item-top">
                  <strong>{item.kind}</strong>
                  <span>{formatTimestamp(item.created_at)}</span>
                </div>
                <p>{item.text}</p>
              </div>
            ))
          ) : (
            <p className="muted">No workspace memory has been saved yet.</p>
          )}
        </div>
      </div>
    </div>
  );
}
