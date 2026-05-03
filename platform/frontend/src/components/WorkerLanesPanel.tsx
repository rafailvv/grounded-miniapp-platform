import type { WorkerReport } from "../lib/api";

type WorkerLanesPanelProps = {
  workerReport: WorkerReport | null;
};

export function WorkerLanesPanel({ workerReport }: WorkerLanesPanelProps) {
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Worker lanes</strong>
        <span>{workerReport?.workers.length ?? 0} lanes</span>
      </div>
      <div className="worker-lanes">
        {workerReport?.workers.map((worker) => (
          <div key={worker.worker_id} className="worker-lane">
            <div className="worker-lane-head">
              <strong>{worker.worker_id}</strong>
              <span className={`run-status ${worker.status}`}>{worker.status}</span>
            </div>
            <p>{worker.owner_scope}</p>
            <small>{worker.changed_files.length ? worker.changed_files.join(", ") : "No owned file changes recorded."}</small>
          </div>
        )) ?? <p className="muted">No worker report is available.</p>}
      </div>
    </div>
  );
}
