import type { WorkerReport } from "../lib/api";

type WorkerLanesPanelProps = {
  workerReport: WorkerReport | null;
};

export function WorkerLanesPanel({ workerReport }: WorkerLanesPanelProps) {
  const selectedCount = workerReport?.workers.filter((worker) => worker.output_ref || worker.context_ref).length ?? 0;
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Worker lanes</strong>
        <span>{workerReport?.workers.length ?? 0} lanes · {selectedCount} artifacts</span>
      </div>
      <div className="worker-lanes">
        {workerReport?.workers.map((worker) => (
          <div key={worker.worker_id} className="worker-lane">
            <div className="worker-lane-head">
              <div>
                <strong>{worker.worker_id}</strong>
                {worker.alias_ids?.length ? <small>{worker.alias_ids.join(", ")}</small> : null}
              </div>
              <span className={`run-status ${worker.badge || worker.status}`}>{worker.badge || worker.status}</span>
            </div>
            <p>{worker.owner_scope}</p>
            {worker.disabled_reason ? <small>{worker.disabled_reason}</small> : null}
            <div className="worker-lane-meta">
              <span>{worker.changed_files.length ? `${worker.changed_files.length} files` : "No owned file changes"}</span>
              {worker.task_id ? <span>task {worker.task_id}</span> : null}
              {worker.proof_refs?.length ? <span>{worker.proof_refs.length} proof refs</span> : null}
              {worker.merge_decision ? <span>merge {String(worker.merge_decision.decision || "recorded")}</span> : null}
            </div>
            <div className="worker-lane-refs">
              {worker.context_ref ? <code>{worker.context_ref}</code> : null}
              {worker.memory_snapshot_ref ? <code>{worker.memory_snapshot_ref}</code> : null}
              {worker.output_ref ? <code>{worker.output_ref}</code> : null}
              {worker.merge_decision_ref ? <code>{worker.merge_decision_ref}</code> : null}
            </div>
            <small>{worker.changed_files.length ? worker.changed_files.join(", ") : "Artifacts will appear after worker context or branch execution."}</small>
          </div>
        )) ?? <p className="muted">No worker report is available.</p>}
      </div>
    </div>
  );
}
