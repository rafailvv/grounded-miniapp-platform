import type { RunTimeline } from "../lib/api";

type TimelinePanelProps = {
  timeline: RunTimeline | null;
};

export function TimelinePanel({ timeline }: TimelinePanelProps) {
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>Replay timeline</strong>
        <span>{timeline?.items.length ?? 0} events</span>
      </div>
      <div className="timeline-list">
        {timeline?.items.length ? (
          timeline.items.map((item) => (
            <details key={`${item.sequence}-${item.kind}`} className={`timeline-event timeline-${item.kind}`}>
              <summary>
                <span className="timeline-sequence">{item.sequence}</span>
                <span className="timeline-kind">{item.kind}</span>
                <strong>{item.title}</strong>
                <span className={`run-status ${item.status}`}>{item.status}</span>
              </summary>
              <pre className="json-block">{JSON.stringify(item.payload, null, 2)}</pre>
            </details>
          ))
        ) : (
          <p className="muted">No replay timeline is available for this run yet.</p>
        )}
      </div>
    </div>
  );
}
