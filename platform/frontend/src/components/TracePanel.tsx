import type { RunTraceView, TraceBundleReport } from "../lib/api";

type TracePanelProps = {
  trace: RunTraceView | null;
  traceBundle: TraceBundleReport | null;
};

export function TracePanel({ trace, traceBundle }: TracePanelProps) {
  if (!trace) {
    return (
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Trace</strong>
          <span>No run selected</span>
        </div>
      </div>
    );
  }

  const reducer = trace.reducer || {};
  const bundleState = traceBundle?.state || {};
  const blockers = bundleState.blockers || [];
  const nextAction = bundleState.next_action || {};
  const sections = [
    ["Failed checks", reducer.failed_checks || []],
    ["Patches", reducer.patches || []],
    ["Browser proofs", reducer.browser_proofs || []],
    ["Failures", reducer.failures || []],
    ["Fixes", reducer.fixes || []],
  ] as const;

  return (
    <div className="workbench-stack">
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Trace</strong>
          <span>{trace.trace_id}</span>
        </div>
        <p className="muted">{reducer.why || "No reducer summary recorded yet."}</p>
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Raw bundle</strong>
          <span>{traceBundle?.status || "missing"}</span>
        </div>
        <div className="trace-bundle-grid">
          <div>
            <strong>{traceBundle?.event_count ?? bundleState.event_count ?? 0}</strong>
            <span>events</span>
          </div>
          <div>
            <strong>{traceBundle?.payload_count ?? bundleState.payload_refs?.length ?? 0}</strong>
            <span>payloads</span>
          </div>
          <div>
            <strong>{bundleState.changed_files?.length ?? 0}</strong>
            <span>files</span>
          </div>
          <div>
            <strong>{blockers.length}</strong>
            <span>blockers</span>
          </div>
        </div>
        {nextAction.action ? <p className="muted">Next: {String(nextAction.action)} · {String(nextAction.reason || "")}</p> : null}
        {blockers.length ? (
          <div className="run-detail-list compact">
            {blockers.slice(0, 4).map((item, index) => (
              <div className="run-detail-item" key={`blocker-${index}`}>
                <strong>{String(item.type || item.event_type || "blocker")}</strong>
                <span>{String(item.status || item.reason || "")}</span>
              </div>
            ))}
          </div>
        ) : null}
      </div>
      {sections.map(([title, items]) => (
        <div className="workbench-panel" key={title}>
          <div className="workbench-panel-header">
            <strong>{title}</strong>
            <span>{items.length}</span>
          </div>
          <div className="timeline-list">
            {items.slice(0, 12).map((item) => (
              <div className="timeline-event" key={`${title}-${item.sequence}-${item.kind}`}>
                <strong>{item.title}</strong>
                <span>{item.kind} · {item.status}</span>
              </div>
            ))}
            {!items.length ? <p className="muted">No entries.</p> : null}
          </div>
        </div>
      ))}
    </div>
  );
}
