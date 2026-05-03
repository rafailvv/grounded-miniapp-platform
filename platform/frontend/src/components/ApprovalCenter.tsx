import type { ApprovalRecord } from "../lib/api";

type ApprovalCenterProps = {
  approvals: ApprovalRecord[];
  onApprove: (approvalId: string) => void;
  onReject: (approvalId: string) => void;
};

export function ApprovalCenter({ approvals, onApprove, onReject }: ApprovalCenterProps) {
  const pending = approvals.filter((item) => item.status === "pending");
  return (
    <div className="approval-center">
      <div className="workbench-panel-header">
        <strong>Approvals</strong>
        <span>{pending.length} pending</span>
      </div>
      {approvals.length ? (
        <div className="run-detail-list">
          {approvals.map((approval) => (
            <div key={approval.approval_id} className="run-detail-item">
              <div className="run-detail-item-top">
                <strong>{approval.summary ?? approval.kind ?? approval.approval_id}</strong>
                <span className={`run-status ${approval.status}`}>{approval.status}</span>
              </div>
              <p>{approval.risk ?? "unknown"} risk</p>
              {approval.status === "pending" ? (
                <div className="run-card-actions">
                  <button type="button" className="ghost-action run-card-action" onClick={() => onApprove(approval.approval_id)}>
                    Approve
                  </button>
                  <button type="button" className="ghost-action run-card-action run-card-stop" onClick={() => onReject(approval.approval_id)}>
                    Reject
                  </button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">No approval requests recorded for this run.</p>
      )}
    </div>
  );
}
