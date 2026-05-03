import type { DoctorReport } from "../lib/api";

type DoctorPanelProps = {
  report: DoctorReport | null;
};

export function DoctorPanel({ report }: DoctorPanelProps) {
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Doctor</strong>
        <span>{report?.status ?? "not loaded"}</span>
      </div>
      <div className="doctor-grid">
        {report?.checks.map((check) => (
          <div key={check.name} className="doctor-check">
            <div className="worker-lane-head">
              <strong>{check.name}</strong>
              <span className={`run-status ${check.status}`}>{check.status}</span>
            </div>
            <p>{check.details ?? ""}</p>
            {check.command ? <small>{check.command}</small> : null}
          </div>
        )) ?? <p className="muted">Open this tab to run platform diagnostics.</p>}
      </div>
    </div>
  );
}
