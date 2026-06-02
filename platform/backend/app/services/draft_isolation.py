from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any
from uuid import uuid4

from app.models.draft_isolation import DraftApplyDecision, DraftGateReport, DraftIsolationManifest, DraftVariantReport
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.workspace.service import WorkspaceService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DraftIsolationService:
    """Durable protocol wrapper around existing filesystem draft storage."""

    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.event_journal_service = event_journal_service

    @staticmethod
    def manifest_ref(workspace_id: str, run_id: str) -> str:
        return f"draft_isolation:{workspace_id}:{run_id}"

    @staticmethod
    def gate_ref(workspace_id: str, run_id: str) -> str:
        return f"draft_gate:{workspace_id}:{run_id}"

    @staticmethod
    def apply_decision_ref(workspace_id: str, run_id: str) -> str:
        return f"draft_apply_decision:{workspace_id}:{run_id}"

    @staticmethod
    def variant_ref(workspace_id: str, run_id: str) -> str:
        return f"draft_variant:{workspace_id}:{run_id}"

    def ensure_manifest(
        self,
        *,
        workspace_id: str,
        run_id: str,
        parent_run_id: str | None = None,
        parent_isolation_ref: str | None = None,
        status: str | None = None,
    ) -> DraftIsolationManifest:
        workspace = self.workspace_service.get_workspace(workspace_id)
        ref = self.manifest_ref(workspace_id, run_id)
        existing = self.store.get("reports", ref)
        base_commit_sha = None
        if workspace.current_revision_id:
            revision = next((item for item in workspace.revisions if item.revision_id == workspace.current_revision_id), None)
            base_commit_sha = revision.commit_sha if revision is not None else None
        draft_source = self.workspace_service.draft_source_dir(workspace_id, run_id)
        diff_text = self.workspace_service.diff(workspace_id, run_id=run_id) if draft_source.exists() else ""
        changed_files = self.workspace_service.draft_changed_paths(workspace_id, run_id) if draft_source.exists() else []
        resolved_status = str(status or ("dirty" if changed_files else "ready"))
        manifest = DraftIsolationManifest(
            workspace_id=workspace_id,
            run_id=run_id,
            isolation_id=str((existing or {}).get("isolation_id") or f"draftiso_{uuid4().hex}"),
            source_ref=f"workspace_source:{workspace_id}",
            draft_source_dir=str(draft_source),
            base_revision_id=str((existing or {}).get("base_revision_id") or workspace.current_revision_id or ""),
            base_commit_sha=str((existing or {}).get("base_commit_sha") or base_commit_sha or ""),
            status=resolved_status if resolved_status in {"created", "ready", "dirty", "gated", "applied", "discarded", "blocked"} else "dirty",
            parent_run_id=parent_run_id or (existing or {}).get("parent_run_id"),
            parent_isolation_ref=parent_isolation_ref or (existing or {}).get("parent_isolation_ref"),
            diff_sha256=_digest(diff_text),
            changed_files=changed_files,
            gate_ref=(existing or {}).get("gate_ref"),
            apply_decision_ref=(existing or {}).get("apply_decision_ref"),
            created_at=(existing or {}).get("created_at") or _now(),
            updated_at=_now(),
        )
        self.store.upsert("reports", ref, manifest.model_dump(mode="json", by_alias=True))
        self._append_run_event(
            workspace_id,
            run_id,
            "draft.isolation.created",
            {
                "isolation_id": manifest.isolation_id,
                "isolation_ref": ref,
                "status": manifest.status,
                "base_revision_id": manifest.base_revision_id,
                "diff_sha256": manifest.diff_sha256,
                "changed_files": manifest.changed_files,
            },
            source_ref=ref,
            idempotency_key=f"draft.isolation.created:{workspace_id}:{run_id}:{manifest.isolation_id}",
        )
        return manifest

    def create_gate(
        self,
        *,
        workspace_id: str,
        run_id: str,
        status: str | None = None,
        blocking_reasons: list[dict[str, Any]] | None = None,
        checks_ref: str | None = None,
        lsp_ref: str | None = None,
        readiness_ref: str | None = None,
    ) -> DraftGateReport:
        manifest = self.ensure_manifest(workspace_id=workspace_id, run_id=run_id)
        reasons = list(blocking_reasons or [])
        if not manifest.changed_files:
            reasons.append({"kind": "no_diff", "details": "Draft has no changed files.", "blocking": True})
        resolved_status = str(status or ("blocked" if reasons else "passed"))
        if resolved_status not in {"passed", "failed", "blocked"}:
            resolved_status = "blocked"
        token = self._apply_token(run_id=run_id, base_revision_id=manifest.base_revision_id, diff_sha256=manifest.diff_sha256 or "", status=resolved_status) if resolved_status == "passed" else None
        ref = self.gate_ref(workspace_id, run_id)
        report = DraftGateReport(
            workspace_id=workspace_id,
            run_id=run_id,
            gate_ref=ref,
            isolation_ref=self.manifest_ref(workspace_id, run_id),
            status=resolved_status,
            diff_sha256=manifest.diff_sha256 or "",
            changed_files=manifest.changed_files,
            checks_ref=checks_ref,
            lsp_ref=lsp_ref,
            readiness_ref=readiness_ref,
            approval_required=True,
            apply_token=token,
            blocking_reasons=reasons,
            next_sequence=self._next_sequence(run_id),
        )
        self.store.upsert("reports", ref, report.model_dump(mode="json", by_alias=True))
        manifest.status = "gated" if resolved_status == "passed" else "blocked"
        manifest.gate_ref = ref
        manifest.updated_at = _now()
        self.store.upsert("reports", self.manifest_ref(workspace_id, run_id), manifest.model_dump(mode="json", by_alias=True))
        self._append_run_event(workspace_id, run_id, "draft.gate.started", {"gate_ref": ref, "isolation_ref": report.isolation_ref}, source_ref=ref, idempotency_key=f"draft.gate.started:{run_id}:{report.diff_sha256}")
        self._append_run_event(
            workspace_id,
            run_id,
            "draft.gate.passed" if resolved_status == "passed" else "draft.gate.failed",
            report.model_dump(mode="json", by_alias=True),
            source_ref=ref,
            idempotency_key=f"draft.gate.{resolved_status}:{run_id}:{report.diff_sha256}",
        )
        return report

    def latest_gate(self, *, workspace_id: str, run_id: str) -> DraftGateReport | None:
        payload = self.store.get("reports", self.gate_ref(workspace_id, run_id))
        return DraftGateReport.model_validate(payload) if payload else None

    def validate_apply_gate(
        self,
        *,
        workspace_id: str,
        run_id: str,
        apply_token: str | None = None,
        selected_files: list[str] | None = None,
    ) -> DraftApplyDecision:
        manifest = self.ensure_manifest(workspace_id=workspace_id, run_id=run_id)
        gate = self.latest_gate(workspace_id=workspace_id, run_id=run_id)
        selected = self._normalize_files(selected_files or manifest.changed_files)
        blocked: list[dict[str, Any]] = []
        if gate is None:
            blocked.append({"kind": "missing_gate", "details": "No passing draft gate exists for this run."})
        elif gate.status != "passed":
            blocked.append({"kind": "gate_not_passed", "details": f"Latest draft gate status is {gate.status}."})
        elif manifest.diff_sha256 != gate.diff_sha256:
            blocked.append({"kind": "stale_gate", "details": "Draft diff changed after the latest passing gate."})
        elif apply_token and apply_token != gate.apply_token:
            blocked.append({"kind": "token_mismatch", "details": "Apply token does not match the latest passing gate."})
        if gate is not None:
            gated_files = set(gate.changed_files)
            outside = sorted(path for path in selected if path not in gated_files)
            if outside:
                blocked.append({"kind": "outside_gated_diff", "details": "Selected files are not in the gated draft diff.", "files": outside})
        decision = DraftApplyDecision(
            workspace_id=workspace_id,
            run_id=run_id,
            decision="blocked" if blocked else "allowed",
            apply_token=apply_token or (gate.apply_token if gate else None),
            selected_files=selected,
            gate_ref=gate.gate_ref if gate else None,
            blocked_reasons=blocked,
        )
        ref = self.apply_decision_ref(workspace_id, run_id)
        self.store.upsert("reports", ref, decision.model_dump(mode="json", by_alias=True))
        if blocked:
            manifest.status = "blocked"
            manifest.apply_decision_ref = ref
            manifest.updated_at = _now()
            self.store.upsert("reports", self.manifest_ref(workspace_id, run_id), manifest.model_dump(mode="json", by_alias=True))
            self._append_run_event(workspace_id, run_id, "draft.apply.blocked", decision.model_dump(mode="json", by_alias=True), source_ref=ref, idempotency_key=f"draft.apply.blocked:{run_id}:{manifest.diff_sha256}:{','.join(selected)}")
        return decision

    def record_apply(
        self,
        *,
        workspace_id: str,
        run_id: str,
        revision_id: str,
        selected_files: list[str],
        gate_ref: str | None = None,
        apply_token: str | None = None,
    ) -> DraftApplyDecision:
        decision = DraftApplyDecision(
            workspace_id=workspace_id,
            run_id=run_id,
            decision="applied",
            apply_token=apply_token,
            selected_files=self._normalize_files(selected_files),
            gate_ref=gate_ref,
            revision_id=revision_id,
        )
        ref = self.apply_decision_ref(workspace_id, run_id)
        self.store.upsert("reports", ref, decision.model_dump(mode="json", by_alias=True))
        manifest = self.ensure_manifest(workspace_id=workspace_id, run_id=run_id, status="applied")
        manifest.status = "applied"
        manifest.apply_decision_ref = ref
        manifest.updated_at = _now()
        self.store.upsert("reports", self.manifest_ref(workspace_id, run_id), manifest.model_dump(mode="json", by_alias=True))
        self._append_run_event(workspace_id, run_id, "draft.apply.completed", decision.model_dump(mode="json", by_alias=True), source_ref=ref, idempotency_key=f"draft.apply.completed:{run_id}:{revision_id}")
        return decision

    def record_apply_started(self, *, workspace_id: str, run_id: str, gate_ref: str | None, selected_files: list[str]) -> None:
        self._append_run_event(
            workspace_id,
            run_id,
            "draft.apply.started",
            {"gate_ref": gate_ref, "selected_files": self._normalize_files(selected_files)},
            source_ref=gate_ref,
            idempotency_key=f"draft.apply.started:{run_id}:{gate_ref}:{','.join(self._normalize_files(selected_files))}",
        )

    def create_variant(self, *, workspace_id: str, source_run_id: str, variant_run_id: str | None = None) -> DraftVariantReport:
        target_run_id = variant_run_id or f"{source_run_id}_variant_{uuid4().hex[:10]}"
        source_manifest = self.ensure_manifest(workspace_id=workspace_id, run_id=source_run_id)
        self.workspace_service.clone_draft(workspace_id, source_run_id, target_run_id)
        target_manifest = self.ensure_manifest(
            workspace_id=workspace_id,
            run_id=target_run_id,
            parent_run_id=source_run_id,
            parent_isolation_ref=self.manifest_ref(workspace_id, source_run_id),
        )
        report = DraftVariantReport(
            workspace_id=workspace_id,
            source_run_id=source_run_id,
            variant_run_id=target_run_id,
            parent_isolation_ref=self.manifest_ref(workspace_id, source_run_id),
            isolation_ref=self.manifest_ref(workspace_id, target_run_id),
        )
        self.store.upsert("reports", self.variant_ref(workspace_id, target_run_id), report.model_dump(mode="json", by_alias=True))
        self._append_run_event(
            workspace_id,
            source_run_id,
            "draft.variant.created",
            {**report.model_dump(mode="json", by_alias=True), "parent_isolation_id": source_manifest.isolation_id, "variant_isolation_id": target_manifest.isolation_id},
            source_ref=report.isolation_ref,
            idempotency_key=f"draft.variant.created:{source_run_id}:{target_run_id}",
        )
        return report

    def _apply_token(self, *, run_id: str, base_revision_id: str | None, diff_sha256: str, status: str) -> str:
        return _digest(f"{run_id}:{base_revision_id or ''}:{diff_sha256}:{status}")[:40]

    @staticmethod
    def _normalize_files(files: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in files:
            path = str(raw or "").strip().replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path and path not in normalized:
                normalized.append(path)
        return normalized

    def _next_sequence(self, run_id: str) -> int:
        if self.event_journal_service is None:
            return 0
        events = self.event_journal_service.list_run(run_id, limit=1)
        return (events[-1].sequence + 1) if events else 1

    def _append_run_event(
        self,
        workspace_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        source_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if self.event_journal_service is None:
            return
        self.event_journal_service.append_run(
            workspace_id=workspace_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            actor="system",
            summary=event_type.replace(".", " "),
            source_ref=source_ref,
            idempotency_key=idempotency_key,
        )
