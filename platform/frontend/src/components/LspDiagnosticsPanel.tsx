import type { LspDiagnosticsReport } from "../lib/api";

type LspDiagnosticsPanelProps = {
  report: LspDiagnosticsReport | null;
  onJumpToLine: (path: string, line?: number) => void;
  onRefresh: () => void;
};

export function LspDiagnosticsPanel({ report, onJumpToLine, onRefresh }: LspDiagnosticsPanelProps) {
  const items = report?.items ?? [];
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>LSP diagnostics</strong>
        <span>{report ? `${report.status} · ${items.length} issues` : "Not loaded"}</span>
      </div>
      <div className="panel-toolbar">
        <button type="button" onClick={onRefresh}>Refresh</button>
        {report?.changed_only ? <span>Changed files only</span> : null}
      </div>
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
