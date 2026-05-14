import type { CommandPaletteAction } from "../lib/api";

type CommandPaletteProps = {
  open: boolean;
  actions: CommandPaletteAction[];
  onRun: (actionId: string) => void;
  onClose: () => void;
};

export function CommandPalette({ open, actions, onRun, onClose }: CommandPaletteProps) {
  if (!open) {
    return null;
  }
  return (
    <div className="command-palette-backdrop" role="dialog" aria-modal="true">
      <div className="command-palette">
        <div className="workbench-panel-header">
          <strong>Command palette</strong>
          <button type="button" className="ghost-action" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="run-detail-list">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              className="command-palette-action"
              disabled={action.disabled}
              onClick={() => onRun(action.id)}
            >
              <strong>{action.label}</strong>
              {action.description ? <span>{action.description}</span> : null}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
