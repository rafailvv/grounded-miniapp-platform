import { useEffect, useState } from "react";
import {
  getBackgroundTaskOutput,
  requeueBackgroundTask,
  retryBackgroundTask,
  stopBackgroundTask,
  type BackgroundTaskOutput,
  type RunTaskReport,
} from "../lib/api";

type TaskLanePanelProps = {
  taskReport: RunTaskReport | null;
};

export function TaskLanePanel({ taskReport }: TaskLanePanelProps) {
  const items = taskReport?.items ?? [];
  const ledgerCounts = taskReport?.task_ledger?.counts;
  const ledgerTotal = taskReport?.task_ledger?.items?.length || items.length;
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [output, setOutput] = useState<BackgroundTaskOutput | null>(null);
  const [busyTaskId, setBusyTaskId] = useState("");
  const selectedTask = items.find((item) => item.task_id === selectedTaskId) || items.find((item) => item.source === "background") || null;

  useEffect(() => {
    if (!selectedTask?.task_id || selectedTask.source !== "background") {
      setOutput(null);
      return;
    }
    let cancelled = false;
    void getBackgroundTaskOutput(selectedTask.task_id)
      .then((payload) => {
        if (!cancelled) setOutput(payload);
      })
      .catch(() => {
        if (!cancelled) setOutput(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTask?.task_id, selectedTask?.source]);

  const runAction = async (action: "stop" | "retry" | "requeue", taskId: string) => {
    setBusyTaskId(taskId);
    try {
      if (action === "stop") await stopBackgroundTask(taskId);
      if (action === "retry") await retryBackgroundTask(taskId);
      if (action === "requeue") await requeueBackgroundTask(taskId);
      const nextOutput = await getBackgroundTaskOutput(taskId).catch(() => null);
      setOutput(nextOutput);
    } finally {
      setBusyTaskId("");
    }
  };

  return (
    <div className="task-drawer-grid">
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Task lane</strong>
          <span>{ledgerCounts ? `${ledgerCounts.completed || 0}/${ledgerTotal} done` : `${items.length} tasks`}</span>
        </div>
        <div className="task-lane">
          {items.length ? (
            items.map((item) => {
              const background = item.source === "background";
              return (
                <button
                  type="button"
                  key={item.task_id}
                  className={`task-lane-item task-lane-button ${selectedTask?.task_id === item.task_id ? "selected" : ""}`}
                  onClick={() => setSelectedTaskId(item.task_id)}
                >
                  <div className="task-lane-top">
                    <strong>{item.title || item.phase || item.task_id}</strong>
                    <span className={`run-status ${item.background_status || item.status}`}>{item.background_status || item.status}</span>
                  </div>
                  <div className="task-lane-meta">
                    <span>{item.owner || "agent"}</span>
                    {item.role ? <span>{item.role}</span> : null}
                    {item.phase ? <span>{item.phase}</span> : null}
                    {item.proof_status ? <span>proof {item.proof_status}</span> : null}
                    {background ? <span>attempt {item.attempt || 1}/{item.max_attempts || 1}</span> : null}
                  </div>
                  {item.files?.length ? <small>{item.files.slice(0, 6).join(", ")}</small> : null}
                  {item.output_summary ? <small>{item.output_summary}</small> : null}
                  {item.blocker ? <p className="task-blocker">{typeof item.blocker === "string" ? item.blocker : JSON.stringify(item.blocker)}</p> : null}
                  {item.proof && (typeof item.proof === "string" || Object.keys(item.proof).length) ? (
                    <small>Refs: {typeof item.proof === "string" ? item.proof : Object.keys(item.proof).join(", ")}</small>
                  ) : null}
                </button>
              );
            })
          ) : (
            <p className="muted">No run task lane is available yet.</p>
          )}
        </div>
      </div>
      <div className="workbench-panel task-drawer">
        <div className="workbench-panel-header">
          <strong>Task drawer</strong>
          <span>{selectedTask?.source === "background" ? "background" : "derived"}</span>
        </div>
        {selectedTask ? (
          <>
            <div className="task-drawer-summary">
              <div>
                <strong>{selectedTask.title || selectedTask.task_id}</strong>
                <span>{[selectedTask.role, selectedTask.phase || "task", selectedTask.proof_status ? `proof ${selectedTask.proof_status}` : ""].filter(Boolean).join(" / ")}</span>
              </div>
              <span className={`run-status ${selectedTask.background_status || selectedTask.status}`}>{selectedTask.background_status || selectedTask.status}</span>
            </div>
            {selectedTask.source === "background" ? (
              <div className="task-actions">
                <button type="button" disabled={busyTaskId === selectedTask.task_id} onClick={() => void runAction("stop", selectedTask.task_id)}>
                  Stop
                </button>
                <button type="button" disabled={busyTaskId === selectedTask.task_id} onClick={() => void runAction("retry", selectedTask.task_id)}>
                  Retry
                </button>
                <button type="button" disabled={busyTaskId === selectedTask.task_id} onClick={() => void runAction("requeue", selectedTask.task_id)}>
                  Requeue
                </button>
              </div>
            ) : null}
            <div className="task-output-stream">
              {(output?.items || []).map((event) => (
                <div className="task-output-event" key={event.sequence}>
                  <strong>{event.event_type}</strong>
                  <span>{event.message}</span>
                  <small>{event.created_at}</small>
                </div>
              ))}
              {selectedTask.source === "background" && !output?.items.length ? <p className="muted">No task output recorded yet.</p> : null}
            </div>
          </>
        ) : (
          <p className="muted">Select a task to inspect output and controls.</p>
        )}
      </div>
    </div>
  );
}
