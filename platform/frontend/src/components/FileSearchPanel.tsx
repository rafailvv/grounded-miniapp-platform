import type { FileSearchResult } from "../lib/api";

type FileSearchPanelProps = {
  query: string;
  result: FileSearchResult | null;
  onQueryChange: (query: string) => void;
  onSelectFile: (path: string) => void;
};

export function FileSearchPanel({ query, result, onQueryChange, onSelectFile }: FileSearchPanelProps) {
  return (
    <div className="workbench-panel">
      <div className="workbench-panel-header">
        <strong>File search</strong>
        <span>{result?.items.length ?? 0} matches</span>
      </div>
      <label className="workspace-search file-search-input">
        <span>Search</span>
        <input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="Type path or text" />
      </label>
      <div className="run-detail-list">
        {result?.items.length ? (
          result.items.map((item) => (
            <button key={item.path} type="button" className="command-palette-action" onClick={() => onSelectFile(item.path)}>
              <strong>{item.path}</strong>
              {item.hits.slice(0, 2).map((hit) => (
                <span key={`${hit.line}-${hit.text}`}>{hit.line}: {hit.text}</span>
              ))}
            </button>
          ))
        ) : (
          <p className="muted">Search by file path or content.</p>
        )}
      </div>
    </div>
  );
}
