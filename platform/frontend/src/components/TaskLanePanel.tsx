import type { RunTaskReport } from "../lib/api";

type TaskLanePanelProps = {
  taskReport: RunTaskReport | null;
};

export function TaskLanePanel({ taskReport }: TaskLanePanelProps) {
  const items = taskReport?.items ?? [];
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Task lane</strong>
        <span>{items.length} tasks</span>
      </div>
      <div className="task-lane">
        {items.length ? (
          items.map((item) => (
            <div key={item.task_id} className="task-lane-item">
              <div className="task-lane-top">
                <strong>{item.title || item.phase || item.task_id}</strong>
                <span className={`run-status ${item.status}`}>{item.status}</span>
              </div>
              <div className="task-lane-meta">
                <span>{item.owner || "agent"}</span>
                {item.phase ? <span>{item.phase}</span> : null}
              </div>
              {item.files?.length ? <small>{item.files.slice(0, 6).join(", ")}</small> : null}
              {item.blocker ? <p className="task-blocker">{typeof item.blocker === "string" ? item.blocker : JSON.stringify(item.blocker)}</p> : null}
              {item.proof && (typeof item.proof === "string" || Object.keys(item.proof).length) ? (
                <small>Proof: {typeof item.proof === "string" ? item.proof : Object.keys(item.proof).join(", ")}</small>
              ) : null}
            </div>
          ))
        ) : (
          <p className="muted">No run task lane is available yet.</p>
        )}
      </div>
    </div>
  );
}
