import type { ReviewReport } from "../lib/api";

type ReviewPanelProps = {
  review: ReviewReport | null;
};

export function ReviewPanel({ review }: ReviewPanelProps) {
  const summary = review?.summary;
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Review findings</strong>
        <span>{review ? `${review.status} · ${summary?.blocker_count ?? 0} blockers` : "not loaded"}</span>
      </div>
      {summary ? (
        <div className="review-summary-grid">
          <div>
            <strong>{summary.finding_count ?? review?.findings.length ?? 0}</strong>
            <span>findings</span>
          </div>
          <div>
            <strong>{summary.missing_tests ?? 0}</strong>
            <span>missing tests</span>
          </div>
          <div>
            <strong>{summary.stale_test_risks ?? 0}</strong>
            <span>stale test risks</span>
          </div>
          <div>
            <strong>{summary.browser_proof_gaps ?? 0}</strong>
            <span>browser gaps</span>
          </div>
          <div>
            <strong>{summary.contract_mismatches ?? 0}</strong>
            <span>contract mismatches</span>
          </div>
        </div>
      ) : null}
      {review?.findings.length ? (
        <div className="run-detail-list">
          {review.findings.map((finding, index) => (
            <div key={index} className={`run-detail-item review-finding severity-${String(finding.severity ?? "medium")}`}>
              <div className="run-detail-item-top">
                <strong>{String(finding.code ?? "finding")}</strong>
                <span>{String(finding.severity ?? "medium")}{finding.is_blocker_for_product_acceptance ? " · acceptance blocker" : ""}</span>
              </div>
              <p>{String(finding.message ?? "Review finding")}</p>
              <span>{String(finding.category ?? "review")} · {String(finding.source ?? "unknown")}</span>
              {finding.file_path || finding.path ? (
                <span>{String(finding.file_path ?? finding.path)}{finding.line ? `:${finding.line}` : ""}</span>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">No review findings recorded for the selected run.</p>
      )}
      {review?.evidence ? <pre className="json-block">{JSON.stringify(review.evidence, null, 2)}</pre> : null}
    </div>
  );
}
