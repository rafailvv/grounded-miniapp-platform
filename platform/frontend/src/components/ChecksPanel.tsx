import type { BrowserProofReport, MiniAppContractReport, PromptContractReport, RunGateReport, TestMatrixReport } from "../lib/api";

type ChecksPanelProps = {
  matrix: TestMatrixReport | null;
  promptContract: PromptContractReport | null;
  miniappContract: MiniAppContractReport | null;
  gate: RunGateReport | null;
  browserProof: BrowserProofReport | null;
  previewRuntimeMode?: string;
};

function statusClass(status?: string | null): string {
  if (status === "passed") {
    return "completed";
  }
  if (status === "blocked" || status === "failed") {
    return "blocked";
  }
  return status || "pending";
}

export function ChecksPanel({ matrix, promptContract, miniappContract, gate, browserProof, previewRuntimeMode }: ChecksPanelProps) {
  const registry = miniappContract?.registry_snapshot ?? {};
  const contractRoutes = Array.isArray(registry.contract_routes) ? registry.contract_routes : [];
  const regenerated = Array.isArray(registry.regenerated_files) ? registry.regenerated_files : [];
  const readiness = gate?.product_readiness ?? null;
  const checklist = Array.isArray(readiness?.checklist) ? readiness.checklist : [];
  const blockers = Array.isArray(readiness?.blocking_reasons) ? readiness.blocking_reasons : [];
  const scenarios = Array.isArray(browserProof?.scenarios) ? browserProof.scenarios : [];
  return (
    <div className="workbench-stack">
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Production readiness</strong>
          <span>{readiness?.status ?? gate?.status ?? "not loaded"}</span>
        </div>
        <div className="doctor-grid">
          {checklist.length ? checklist.map((item) => (
            <div key={item.key} className="doctor-check">
              <div className="worker-lane-head">
                <strong>{item.label}</strong>
                <span className={`run-status ${statusClass(item.status)}`}>{item.status}</span>
              </div>
              <p>{item.details || item.check || "proof recorded"}</p>
            </div>
          )) : <p className="muted">No production readiness report has been loaded for this run.</p>}
        </div>
        {blockers.length ? (
          <div className="run-detail-list">
            {blockers.slice(0, 5).map((issue, index) => (
              <div key={`${String(issue.kind ?? "blocker")}-${index}`} className="run-detail-item">
                <strong>{String(issue.kind ?? issue.check ?? "blocker")}</strong>
                <p>{String(issue.details ?? "Readiness proof is blocked.")}</p>
              </div>
            ))}
          </div>
        ) : null}
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Browser proof</strong>
          <span>{browserProof?.status ?? "not loaded"}</span>
        </div>
        <div className="run-detail-list">
          {scenarios.length ? scenarios.slice(0, 8).map((scenario, index) => (
            <div key={scenario.scenario_id ?? index} className="run-detail-item">
              <div className="worker-lane-head">
                <strong>{scenario.title ?? scenario.action ?? "Scenario"}</strong>
                <span className={`run-status ${statusClass(scenario.status)}`}>{scenario.status ?? "recorded"}</span>
              </div>
              <p>{[scenario.role, scenario.route].filter(Boolean).join(" · ") || String(scenario.marker ?? browserProof?.proof_statement ?? "Scenario evidence recorded.")}</p>
            </div>
          )) : <p className="muted">No browser proof scenarios have been recorded for this run.</p>}
        </div>
        <div className="run-details-grid">
          <div className="run-detail-card">
            <span>Screenshots</span>
            <strong>{browserProof?.screenshots?.length ?? 0}</strong>
          </div>
          <div className="run-detail-card">
            <span>Console errors</span>
            <strong>{browserProof?.console_errors?.length ?? 0}</strong>
          </div>
          <div className="run-detail-card">
            <span>Network errors</span>
            <strong>{browserProof?.network_errors?.length ?? 0}</strong>
          </div>
        </div>
      </div>
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
          <p className="muted">Prompt contract uses structured LLM analysis.</p>
        )}
        {promptContract ? <small>{promptContract.analysis_status ?? "analysis not loaded"}</small> : null}
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>MiniApp contract</strong>
          <span>{miniappContract?.status ?? "not loaded"}</span>
        </div>
        <div className="run-detail-list">
          <div className="run-detail-item">
            <strong>Runtime</strong>
            <p>{previewRuntimeMode || "not loaded"}</p>
          </div>
          <div className="run-detail-item">
            <strong>Contract routes</strong>
            <p>{contractRoutes.length ? contractRoutes.slice(0, 6).join(", ") : "No contract routes loaded."}</p>
          </div>
          <div className="run-detail-item">
            <strong>Generated sync</strong>
            <p>{regenerated.length ? regenerated.slice(0, 6).join(", ") : "No regenerated files in latest snapshot."}</p>
          </div>
        </div>
        {miniappContract?.drift_issues.length ? (
          <div className="run-detail-list">
            {miniappContract.drift_issues.slice(0, 6).map((issue, index) => (
              <div key={index} className="run-detail-item">
                <strong>{String(issue.code ?? "contract drift")}</strong>
                <p>{String(issue.expected ?? issue.frontend_ref ?? issue.location ?? "Review generated contract drift.")}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">Generated files, route registry, and contract routes are aligned.</p>
        )}
        {miniappContract?.repair_recipes.length ? (
          <small>{miniappContract.repair_recipes.slice(0, 3).map((recipe) => String(recipe.suggested_patch_target ?? recipe.issue_code ?? "repair")).join(", ")}</small>
        ) : null}
      </div>
    </div>
  );
}
