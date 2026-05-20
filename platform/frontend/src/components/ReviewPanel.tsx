import type { PromptSuggestion, PromptSuggestionsReport, ReviewReport } from "../lib/api";

type ReviewPanelProps = {
  review: ReviewReport | null;
  suggestions?: PromptSuggestionsReport | null;
  onUseSuggestion?: (suggestion: PromptSuggestion) => void;
};

export function ReviewPanel({ review, suggestions, onUseSuggestion }: ReviewPanelProps) {
  const summary = review?.summary;
  const suggestionItems = suggestions?.items ?? [];
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
      {suggestionItems.length ? (
        <section className="prompt-suggestions-panel">
          <div className="workbench-panel-header prompt-suggestions-header">
            <strong>Product follow-ups</strong>
            <span>{suggestionItems.length} suggestions</span>
          </div>
          <div className="prompt-suggestions-grid">
            {suggestionItems.map((suggestion) => (
              <button
                key={suggestion.suggestion_id}
                type="button"
                className={`prompt-suggestion-card priority-${String(suggestion.priority ?? "should")}`}
                onClick={() => onUseSuggestion?.(suggestion)}
              >
                <span className="prompt-suggestion-meta">
                  {String(suggestion.category ?? "follow-up").replace(/_/g, " ")}
                  {suggestion.target_role ? ` · ${suggestion.target_role}` : ""}
                </span>
                <strong>{String(suggestion.title ?? "Product follow-up")}</strong>
                <p>{String(suggestion.reason ?? "")}</p>
                {suggestion.target_files?.length ? <small>{suggestion.target_files.slice(0, 3).join(", ")}</small> : null}
              </button>
            ))}
          </div>
        </section>
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
