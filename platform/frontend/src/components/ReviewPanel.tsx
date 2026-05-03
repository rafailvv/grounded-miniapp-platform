import type { ReviewReport } from "../lib/api";

type ReviewPanelProps = {
  review: ReviewReport | null;
};

export function ReviewPanel({ review }: ReviewPanelProps) {
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Code review mode</strong>
        <span>{review?.status ?? "not loaded"}</span>
      </div>
      {review?.findings.length ? (
        <div className="run-detail-list">
          {review.findings.map((finding, index) => (
            <div key={index} className="run-detail-item">
              <strong>{String(finding.code ?? finding.severity ?? "finding")}</strong>
              <p>{String(finding.message ?? "Review finding")}</p>
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
