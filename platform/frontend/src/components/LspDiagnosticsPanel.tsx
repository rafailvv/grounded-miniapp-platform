import type { LspDiagnosticsReport } from "../lib/api";

type LspDiagnosticsPanelProps = {
  report: LspDiagnosticsReport | null;
  onJumpToLine: (path: string, line?: number) => void;
  onRefresh: () => void;
};

export function LspDiagnosticsPanel({ report, onJumpToLine, onRefresh }: LspDiagnosticsPanelProps) {
  const items = report?.items ?? [];
  const toolStatus = report?.tool_status ?? {};
  const toolEntries = Object.entries(toolStatus);
  const stream = report?.diagnostic_stream ?? [];
  const routeGraph = report?.route_graph;
  const missingEdges = routeGraph?.missing_edges ?? [];
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>LSP diagnostics</strong>
        <span>{report ? `${report.status} · ${items.length} issues · ${report.engine ?? "lsp"}` : "Not loaded"}</span>
      </div>
      <div className="panel-toolbar">
        <button type="button" onClick={onRefresh}>Refresh</button>
        {report?.changed_only ? <span>Changed files only</span> : null}
      </div>
      {toolEntries.length ? (
        <div className="lsp-status-grid">
          {toolEntries.map(([name, value]) => {
            const payload = value && typeof value === "object" ? value as Record<string, unknown> : {};
            return (
              <div key={name} className={`lsp-status-card status-${String(payload.status ?? "unknown")}`}>
                <strong>{name}</strong>
                <span>{String(payload.status ?? "unknown")}</span>
                {payload.mode ? <small>{String(payload.mode)}</small> : null}
                {payload.project ? <small>{String(payload.project)}</small> : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {stream.length ? (
        <div className="lsp-stream-strip">
          {stream.map((event, index) => (
            <span key={`${event.phase}-${index}`} className={`status-${String(event.status ?? "unknown")}`}>
              {event.phase}: {event.status}{typeof event.issue_count === "number" ? ` (${event.issue_count})` : ""}
            </span>
          ))}
        </div>
      ) : null}
      {routeGraph?.summary ? (
        <section className="lsp-route-graph">
          <div className="run-detail-item-top">
            <strong>Route graph</strong>
            <span>{String(routeGraph.summary.edge_count ?? 0)} edges · {String(routeGraph.summary.missing_edge_count ?? 0)} missing</span>
          </div>
          {missingEdges.length ? (
            <div className="run-detail-list">
              {missingEdges.slice(0, 8).map((edge, index) => (
                <button
                  key={`${String(edge.file ?? "")}-${String(edge.path ?? "")}-${index}`}
                  type="button"
                  className="command-palette-action"
                  onClick={() => edge.file ? onJumpToLine(String(edge.file), 1) : undefined}
                >
                  <strong>{String(edge.method ?? "GET")} {String(edge.path ?? "")}</strong>
                  <span>{String(edge.file ?? "frontend call")} has no matching backend route</span>
                </button>
              ))}
            </div>
          ) : null}
        </section>
      ) : null}
      <div className="run-detail-list">
        {items.length ? (
          items.slice(0, 80).map((item, index) => {
            const path = item.jump?.path || item.path || item.file || "";
            const line = item.jump?.line || item.line || 1;
            return (
              <button key={`${path}-${line}-${item.source}-${index}`} type="button" className="command-palette-action" onClick={() => onJumpToLine(path, line)}>
                <strong>{item.source} · {item.severity}</strong>
                <span>{path}:{line}{item.column ? `:${item.column}` : ""}</span>
                <span>{item.message}</span>
                {item.code ? <span>{item.code}</span> : null}
              </button>
            );
          })
        ) : (
          <p className="muted">{report ? "No file or route diagnostics." : "Open this tab to run targeted diagnostics."}</p>
        )}
      </div>
      {report?.symbols?.length ? (
        <div className="run-detail-list">
          <strong>Symbol context</strong>
          {report.symbols.slice(0, 30).map((symbol) => (
            <button
              key={`${symbol.path}-${symbol.kind}-${symbol.name}-${symbol.line}`}
              type="button"
              className="command-palette-action"
              onClick={() => onJumpToLine(symbol.path, symbol.line)}
            >
              <strong>{symbol.name}</strong>
              <span>{symbol.kind} · {symbol.path}:{symbol.line}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
