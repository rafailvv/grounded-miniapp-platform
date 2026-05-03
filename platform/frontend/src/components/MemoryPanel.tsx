import type { WorkspaceMemory } from "../lib/api";

type MemoryPanelProps = {
  memory: WorkspaceMemory | null;
  formatTimestamp: (value?: string) => string;
};

export function MemoryPanel({ memory, formatTimestamp }: MemoryPanelProps) {
  return (
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
  );
}
