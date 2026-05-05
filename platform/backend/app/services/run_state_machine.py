from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.domain import RunRecord


TERMINAL_RUN_STATUSES = {"completed", "blocked", "failed", "awaiting_approval"}


class RunStateMachine:
    """Single evaluator for run/job/gate/artifact consistency.

    The generator can finish through different code paths: the agent job, the
    draft apply path, preview/browser proof, or a manual approval path. This
    reducer keeps the API-facing state honest without inventing recovery or
    switching execution modes.
    """

    SCHEMA_VERSION = "grounded.run_state.v1"

    @classmethod
    def evaluate(
        cls,
        *,
        run: RunRecord,
        gate: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        browser_proof: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate_payload = dict(gate or {})
        artifacts_payload = dict(artifacts or {})
        browser_payload = dict(browser_proof or {})
        terminal = run.status in TERMINAL_RUN_STATUSES
        manual_approval_ok = (
            run.apply_strategy == "manual_approve"
            and run.status == "awaiting_approval"
            and run.apply_status == "awaiting_approval"
        )
        apply_ok = run.apply_status == "applied" or manual_approval_ok
        gate_status = str(gate_payload.get("status") or "pending")
        gate_blocking = bool(gate_payload.get("blocking"))
        gate_issues = [item for item in gate_payload.get("issues") or [] if isinstance(item, dict)]
        invariant_issues = cls._invariant_issues(
            run=run,
            terminal=terminal,
            apply_ok=apply_ok,
            manual_approval_ok=manual_approval_ok,
            gate_status=gate_status,
            gate_blocking=gate_blocking,
            browser_proof=browser_payload,
        )
        blocking = gate_blocking or bool(invariant_issues)
        if not terminal:
            status = run.status
        elif blocking:
            status = "blocked"
        elif gate_status == "passed" and apply_ok:
            status = "passed"
        elif apply_ok:
            status = "pending_gate"
        else:
            status = "blocked"
            invariant_issues.append(
                cls._issue(
                    "apply_status",
                    "apply_gate",
                    "Terminal run is neither applied nor awaiting manual approval.",
                    {"run_status": run.status, "apply_status": run.apply_status},
                )
            )
            blocking = True
        return {
            "schema": cls.SCHEMA_VERSION,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "blocking": blocking,
            "terminal": terminal,
            "apply_ok": apply_ok,
            "manual_approval_ok": manual_approval_ok,
            "gate_status": gate_status,
            "gate_blocking": gate_blocking,
            "issues": [*gate_issues, *invariant_issues],
            "invariant_issues": invariant_issues,
            "source_state": {
                "run_status": run.status,
                "apply_status": run.apply_status,
                "draft_status": run.draft_status,
                "outcome_kind": run.outcome_kind,
                "failure_class": run.failure_class,
                "failure_signature": run.failure_signature,
                "artifact_run_status": ((artifacts_payload.get("run") or {}) if isinstance(artifacts_payload.get("run"), dict) else {}).get("status"),
                "browser_proof_status": browser_payload.get("status"),
            },
            "artifact_refs": {
                "gate": f"gate:{run.run_id}",
                "run_artifacts": f"run_artifacts:{run.run_id}",
                "browser_proof": run.browser_proof_ref,
                "resume_checkpoint": run.resume_checkpoint_ref,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def _invariant_issues(
        cls,
        *,
        run: RunRecord,
        terminal: bool,
        apply_ok: bool,
        manual_approval_ok: bool,
        gate_status: str,
        gate_blocking: bool,
        browser_proof: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not terminal:
            return []
        issues: list[dict[str, Any]] = []
        if run.status == "completed" and run.apply_status != "applied":
            issues.append(
                cls._issue(
                    "run_apply_mismatch",
                    "run_state",
                    "A completed run must have apply_status='applied'.",
                    {"run_status": run.status, "apply_status": run.apply_status},
                )
            )
        if run.status == "awaiting_approval" and not manual_approval_ok:
            issues.append(
                cls._issue(
                    "manual_approval_mismatch",
                    "run_state",
                    "A run can await approval only in manual mode with apply_status='awaiting_approval'.",
                    {"apply_strategy": run.apply_strategy, "apply_status": run.apply_status},
                )
            )
        if run.status == "completed" and gate_blocking:
            issues.append(
                cls._issue(
                    "completed_run_blocked_by_gate",
                    "reliability_gate",
                    "Run is marked completed while Reliability Gate still has blocking issues.",
                    {"gate_status": gate_status},
                )
            )
        if gate_status == "passed" and not apply_ok:
            issues.append(
                cls._issue(
                    "gate_passed_without_apply",
                    "apply_gate",
                    "Reliability Gate passed but the run is not applied or awaiting manual approval.",
                    {"run_status": run.status, "apply_status": run.apply_status},
                )
            )
        browser_status = str(browser_proof.get("status") or "").strip().lower()
        if run.status == "completed" and browser_proof and browser_status not in {"passed"}:
            issues.append(
                cls._issue(
                    "completed_run_browser_proof_not_passed",
                    "browser_proof",
                    "A completed run must have passing browser proof when browser proof is recorded.",
                    {"browser_proof_status": browser_status},
                )
            )
        return issues

    @staticmethod
    def _issue(kind: str, check: str, details: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind,
            "check": check,
            "details": details,
            "blocking": True,
            "evidence": evidence,
        }
