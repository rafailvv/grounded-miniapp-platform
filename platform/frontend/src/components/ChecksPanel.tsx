import type { PromptContractReport, TestMatrixReport } from "../lib/api";

type ChecksPanelProps = {
  matrix: TestMatrixReport | null;
  promptContract: PromptContractReport | null;
};

export function ChecksPanel({ matrix, promptContract }: ChecksPanelProps) {
  return (
    <div className="workbench-stack">
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Test matrix</strong>
          <span>{matrix?.status ?? "not loaded"}</span>
        </div>
        <div className="doctor-grid">
          {matrix?.items.map((item) => (
            <div key={item.key} className="doctor-check">
              <div className="worker-lane-head">
                <strong>{item.label}</strong>
                <span className={`run-status ${item.status}`}>{item.status}</span>
              </div>
              <p>{item.required ? "Required" : "Optional"}</p>
            </div>
          )) ?? <p className="muted">No test matrix has been loaded for this run.</p>}
        </div>
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Prompt contract</strong>
          <span>{promptContract?.status ?? "not loaded"}</span>
        </div>
        {promptContract?.findings.length ? (
          <div className="run-detail-list">
            {promptContract.findings.map((finding, index) => (
              <div key={index} className="run-detail-item">
                <strong>{String(finding.severity ?? "finding")}</strong>
                <p>{String(finding.message ?? "Review prompt contract.")}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">Prompt terms match the available change evidence or no diff has been recorded.</p>
        )}
        {promptContract ? <small>{promptContract.matched_terms.slice(0, 16).join(", ")}</small> : null}
      </div>
    </div>
  );
}
