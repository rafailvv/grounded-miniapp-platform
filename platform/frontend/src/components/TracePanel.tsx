import type { RunCompactionBoundaries, RunCompactionReport, RunProtocolReport, RunRepairCases, RunTraceView, TraceBundleReport } from "../lib/api";

type TracePanelProps = {
  trace: RunTraceView | null;
  traceBundle: TraceBundleReport | null;
  protocol: RunProtocolReport | null;
  compaction: RunCompactionReport | null;
  compactionBoundaries: RunCompactionBoundaries | null;
  repairCases: RunRepairCases | null;
};

export function TracePanel({ trace, traceBundle, protocol, compaction, compactionBoundaries, repairCases }: TracePanelProps) {
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
  const promptContexts = bundleState.prompt_contexts || [];
  const skillEdges = bundleState.skill_edges || [];
  const memoryEdges = bundleState.memory_edges || [];
  const diffEdges = bundleState.diff_edges || [];
  const acceptanceGate = bundleState.acceptance_gate || [];
  const nextAction = bundleState.next_action || {};
  const protocolEvents = protocol?.items || [];
  const compactBoundaries = compactionBoundaries?.items || bundleState.compact_boundaries || [];
  const compactionSections = compaction?.sections || {};
  const microcompacts = (compactionSections.microcompacts as Array<Record<string, unknown>> | undefined) || [];
  const postCompactRef = compaction?.post_compact_message_ref || String(compaction?.refs?.post_compact_message_ref || "");
  const postCompactStatus = compaction?.post_compact_status || "missing";
  const sectionCount = Object.keys(compactionSections).length;
  const bookmarks = protocol?.bookmarks || [];
  const latestBookmark = protocol?.latest_bookmark || bookmarks[0];
  const cases = repairCases?.items || [];
  const activeCase = repairCases?.active_case || cases.find((item) => ["open", "failed_attempt", "blocked"].includes(item.status)) || null;
  const protocolCounts = protocolEvents.reduce<Record<string, number>>((acc, event) => {
    acc[event.type] = (acc[event.type] || 0) + 1;
    return acc;
  }, {});
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
          <div>
            <strong>{promptContexts.length}</strong>
            <span>prompts</span>
          </div>
          <div>
            <strong>{memoryEdges.length}</strong>
            <span>memory</span>
          </div>
          <div>
            <strong>{diffEdges.length}</strong>
            <span>diffs</span>
          </div>
          <div>
            <strong>{acceptanceGate.length}</strong>
            <span>gates</span>
          </div>
        </div>
        {skillEdges.length || memoryEdges.length || acceptanceGate.length ? (
          <div className="run-detail-list compact">
            {skillEdges.slice(0, 3).map((item, index) => (
              <div className="run-detail-item" key={`skill-edge-${index}`}>
                <strong>{String(item.skill_id || "skill")}</strong>
                <span>{String(item.reason || "")}</span>
              </div>
            ))}
            {memoryEdges.slice(0, 3).map((item, index) => (
              <div className="run-detail-item" key={`memory-edge-${index}`}>
                <strong>{String(item.kind || item.source || "memory")}</strong>
                <span>{String(item.reason || "")}</span>
              </div>
            ))}
            {acceptanceGate.slice(-1).map((item, index) => (
              <div className="run-detail-item" key={`gate-${index}`}>
                <strong>acceptance gate</strong>
                <span>{String(item.status || "recorded")}</span>
              </div>
            ))}
          </div>
        ) : null}
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
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Run protocol</strong>
          <span>{protocol?.status || "missing"}</span>
        </div>
        <div className="trace-bundle-grid">
          <div>
            <strong>{protocolEvents.length}</strong>
            <span>events</span>
          </div>
          <div>
            <strong>{protocolCounts.turn_started || 0}</strong>
            <span>turns</span>
          </div>
          <div>
            <strong>{protocolCounts.tool_requested || 0}</strong>
            <span>tools</span>
          </div>
          <div>
            <strong>{bookmarks.length}</strong>
            <span>bookmarks</span>
          </div>
        </div>
        {latestBookmark ? (
          <p className="muted">
            Latest bookmark: {latestBookmark.bookmark_id} · {latestBookmark.turn_id || "turn unknown"}
          </p>
        ) : null}
        <div className="timeline-list">
          {protocolEvents.slice(-8).map((event) => (
            <div className="timeline-event" key={event.event_id}>
              <strong>{event.type}</strong>
              <span>{event.status} · {event.turn_id || event.source_event_type || "run"}</span>
            </div>
          ))}
          {!protocolEvents.length ? <p className="muted">No protocol events.</p> : null}
        </div>
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Compaction</strong>
          <span>{compaction?.status || "missing"}</span>
        </div>
        <div className="trace-bundle-grid">
          <div>
            <strong>{compactBoundaries.length}</strong>
            <span>boundaries</span>
          </div>
          <div>
            <strong>{microcompacts.length}</strong>
            <span>micro</span>
          </div>
          <div>
            <strong>{String((compactionSections.context_pressure as Record<string, unknown> | undefined)?.pressure_ratio ?? "n/a")}</strong>
            <span>pressure</span>
          </div>
          <div>
            <strong>{compaction?.boundary_id ? "yes" : "no"}</strong>
            <span>latest</span>
          </div>
          <div>
            <strong>{postCompactStatus}</strong>
            <span>post</span>
          </div>
          <div>
            <strong>{sectionCount}</strong>
            <span>sections</span>
          </div>
        </div>
        {postCompactRef ? (
          <p className="muted">
            Post compact: {postCompactRef}
            {compaction?.consumed_by_turn_id ? ` · consumed by ${compaction.consumed_by_turn_id}` : ""}
          </p>
        ) : null}
        {microcompacts.length ? (
          <div className="run-detail-list compact">
            {microcompacts.slice(0, 3).map((item, index) => (
              <div className="run-detail-item" key={`microcompact-${index}`}>
                <strong>{String(item.tool || item.digest || "microcompact")}</strong>
                <span>{String(item.ref || item.digest || "")}</span>
              </div>
            ))}
          </div>
        ) : null}
        {(compactionSections.next_repair_action as Record<string, unknown> | undefined) ? (
          <p className="muted">Next repair: {String((compactionSections.next_repair_action as Record<string, unknown>).failure_signature || (compactionSections.next_repair_action as Record<string, unknown>).required_next_tool || "recorded")}</p>
        ) : null}
      </div>
      <div className="workbench-panel">
        <div className="workbench-panel-header">
          <strong>Repair cases</strong>
          <span>{repairCases?.status || "missing"}</span>
        </div>
        <div className="trace-bundle-grid">
          <div>
            <strong>{cases.length}</strong>
            <span>cases</span>
          </div>
          <div>
            <strong>{activeCase ? activeCase.status : "none"}</strong>
            <span>active</span>
          </div>
          <div>
            <strong>{activeCase?.attempts?.length ?? 0}</strong>
            <span>attempts</span>
          </div>
          <div>
            <strong>{activeCase?.target_files?.length ?? 0}</strong>
            <span>targets</span>
          </div>
        </div>
        {activeCase ? (
          <div className="run-detail-list compact">
            <div className="run-detail-item">
              <strong>{activeCase.failure_class || activeCase.issue_code || activeCase.case_id}</strong>
              <span>{activeCase.likely_cause || activeCase.failure_signature || "evidence-driven repair case"}</span>
            </div>
            {activeCase.target_files?.slice(0, 4).map((file) => (
              <div className="run-detail-item" key={`repair-target-${file}`}>
                <strong>target</strong>
                <span>{file}</span>
              </div>
            ))}
            {activeCase.expected_proof?.slice(0, 3).map((proof, index) => (
              <div className="run-detail-item" key={`repair-proof-${index}`}>
                <strong>{String(proof.type || "proof")}</strong>
                <span>{String(proof.status || proof.check || proof.ref || "required")}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">No active repair case.</p>
        )}
        {cases.length ? (
          <div className="timeline-list">
            {cases.slice(0, 6).map((item) => (
              <div className="timeline-event" key={item.case_id}>
                <strong>{item.failure_class || item.issue_code || item.case_id}</strong>
                <span>{item.status} · {item.attempts?.length ?? 0} attempts</span>
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
