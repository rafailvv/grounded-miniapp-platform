from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from pathlib import Path
from typing import Any

from app.models.domain import CheckExecutionRecord, RunCheckResult, RunRecord
from app.models.guardian import GuardianFinding, GuardianGateReport, GuardianSemanticReviewReport
from app.modules.miniapp_agent_loop.guardian_review import GuardianReview
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.workspace.service import WorkspaceService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GuardianGateService:
    """Canonical hybrid pre-apply gate over deterministic and semantic guardian review."""

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
    def gate_ref(workspace_id: str, run_id: str) -> str:
        return f"guardian_gate:{workspace_id}:{run_id}"

    @staticmethod
    def semantic_ref(workspace_id: str, run_id: str) -> str:
        return f"guardian_semantic_review:{workspace_id}:{run_id}"

    @staticmethod
    def packet_ref(workspace_id: str, run_id: str) -> str:
        return f"guardian_review_packet:{workspace_id}:{run_id}"

    @staticmethod
    def deterministic_ref(workspace_id: str, run_id: str) -> str:
        return f"guardian_review:{workspace_id}:{run_id}"

    def latest_gate(self, *, workspace_id: str, run_id: str) -> dict[str, Any] | None:
        payload = self.store.get("reports", self.gate_ref(workspace_id, run_id))
        return dict(payload) if isinstance(payload, dict) else None

    def run_gate(
        self,
        *,
        run: RunRecord,
        source: str = "pre_apply_guardian",
        changed_files: list[str] | None = None,
        semantic_override: str | None = None,
    ) -> GuardianGateReport:
        artifacts = self._run_artifacts(run)
        diff_text = self._diff_text(run, artifacts)
        changed = self._changed_files(run, changed_files, diff_text)
        draft_source = self.workspace_service.draft_source_dir(run.workspace_id, run.run_id)
        if not draft_source.exists():
            draft_source = self.workspace_service.source_dir(run.workspace_id)
        draft_gate_ref = getattr(run, "draft_gate_ref", None) or f"draft_gate:{run.workspace_id}:{run.run_id}"
        self._append_event(
            run,
            "guardian.gate.started",
            {"guardian_gate_ref": self.gate_ref(run.workspace_id, run.run_id), "changed_files": changed, "draft_gate_ref": draft_gate_ref},
            source_ref=self.gate_ref(run.workspace_id, run.run_id),
            idempotency_key=f"guardian.gate.started:{run.run_id}:{_sha256(diff_text)}",
        )
        deterministic = GuardianReview.review(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            draft_source=draft_source,
            changed_files=changed,
            latest_execution=self._check_execution(run, artifacts, changed),
            preview_details=artifacts.get("preview") if isinstance(artifacts.get("preview"), dict) else {},
            acceptance_contract=run.acceptance_contract,
            implementation_plan=run.implementation_plan,
            target_role_scope=run.target_role_scope,
            intent=run.intent,
            source=source if source in {"pre_apply_guardian", "runtime_verifier", "manual_review"} else "pre_apply_guardian",
            review_context={
                "run": run.model_dump(mode="json"),
                "diff": diff_text,
                "token_usage": run.token_usage,
                "context_pressure": self.store.get("reports", run.context_pressure_ref) if run.context_pressure_ref else {},
            },
        ).model_dump(mode="json", by_alias=True)
        deterministic_ref = self.deterministic_ref(run.workspace_id, run.run_id)
        self.store.upsert("reports", deterministic_ref, deterministic)
        self._append_event(
            run,
            "guardian.deterministic.completed",
            {"status": deterministic.get("status"), "deterministic_review_ref": deterministic_ref, "finding_count": len(deterministic.get("findings") or [])},
            source_ref=deterministic_ref,
            idempotency_key=f"guardian.deterministic.completed:{run.run_id}:{_sha256(diff_text)}",
        )
        packet = self._review_packet(run=run, artifacts=artifacts, changed_files=changed, diff_text=diff_text, draft_gate_ref=draft_gate_ref)
        packet_ref = self.packet_ref(run.workspace_id, run.run_id)
        self.store.upsert("reports", packet_ref, packet)
        self._append_event(run, "guardian.semantic.started", {"review_packet_ref": packet_ref}, source_ref=packet_ref, idempotency_key=f"guardian.semantic.started:{run.run_id}:{_sha256(diff_text)}")
        semantic = self._semantic_review(run=run, packet=packet, packet_ref=packet_ref, override=semantic_override)
        semantic_ref = self.semantic_ref(run.workspace_id, run.run_id)
        self.store.upsert("reports", semantic_ref, semantic.model_dump(mode="json", by_alias=True))
        self._append_event(
            run,
            "guardian.semantic.completed",
            {"semantic_review_ref": semantic_ref, "verdict": semantic.verdict, "status": semantic.status, "finding_count": len(semantic.findings)},
            source_ref=semantic_ref,
            idempotency_key=f"guardian.semantic.completed:{run.run_id}:{_sha256(diff_text)}:{semantic.verdict}",
        )
        findings = [
            *[GuardianFinding.model_validate(item) for item in deterministic.get("findings") or [] if isinstance(item, dict)],
            *semantic.findings,
        ]
        blocker_count = sum(1 for item in findings if item.is_blocker_for_apply)
        allow = deterministic.get("status") == "passed" and semantic.verdict == "allow" and blocker_count == 0
        repair_packets = self._repair_packets(run=run, findings=findings, semantic=semantic)
        report = GuardianGateReport(
            status="passed" if allow else "blocked",
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            guardian_gate_ref=self.gate_ref(run.workspace_id, run.run_id),
            deterministic_review_ref=deterministic_ref,
            semantic_review_ref=semantic_ref,
            draft_gate_ref=draft_gate_ref,
            prompt_contract_ref=getattr(run, "prompt_contract_ref", None) or getattr(run, "miniapp_contract_ref", None) or "run.acceptance_contract",
            diff_sha256=_sha256(diff_text),
            changed_files=changed,
            findings=findings,
            repair_packets=repair_packets,
            apply_decision="allow" if allow else "block",
            next_sequence=self._next_sequence(run.run_id),
            semantic_verdict=semantic.verdict,
        )
        self.store.upsert("reports", report.guardian_gate_ref, report.model_dump(mode="json", by_alias=True))
        self._append_event(
            run,
            "guardian.apply.allowed" if allow else "guardian.apply.blocked",
            report.model_dump(mode="json", by_alias=True),
            source_ref=report.guardian_gate_ref,
            idempotency_key=f"guardian.apply.{report.apply_decision}:{run.run_id}:{report.diff_sha256}:{semantic.verdict}",
        )
        return report

    def _semantic_review(self, *, run: RunRecord, packet: dict[str, Any], packet_ref: str, override: str | None = None) -> GuardianSemanticReviewReport:
        findings: list[GuardianFinding] = []
        verdict = "allow"
        status = "passed"
        if override in {"block", "uncertain", "allow"}:
            verdict = "allow" if override == "allow" else override
        diff_summary = packet.get("diff_summary") if isinstance(packet.get("diff_summary"), dict) else {}
        changed_files = [str(path) for path in packet.get("changed_files") or []]
        improve_slice = packet.get("improve_slice") if isinstance(packet.get("improve_slice"), dict) else {}
        prompt = str(packet.get("prompt") or "").lower()
        acceptance = packet.get("acceptance_contract") if isinstance(packet.get("acceptance_contract"), dict) else {}
        deleted_routes = diff_summary.get("deleted_routes") or []
        deleted_ui = diff_summary.get("deleted_role_ui") or []
        added_ui = diff_summary.get("added_role_ui") or []
        role_ui_changed = any(path.startswith(("miniapp/app/static/client/", "miniapp/app/static/specialist/", "miniapp/app/static/manager/")) for path in changed_files)
        proof_refs = packet.get("proof_refs") if isinstance(packet.get("proof_refs"), dict) else {}
        if deleted_routes and not proof_refs.get("api_workflow_smoke_passed"):
            findings.append(self._finding("guardian.semantic.deleted_api_without_proof", "Candidate deletes API surface without matching API proof.", {"deleted_routes": deleted_routes[:20]}, category="breaking_changes"))
        if role_ui_changed and deleted_ui and not added_ui and not proof_refs.get("browser_flow_smoke_passed"):
            findings.append(self._finding("guardian.semantic.deleted_role_ui_without_proof", "Candidate deletes role UI surface without browser workflow proof.", {"deleted_role_ui": deleted_ui[:20]}, category="role_workflow"))
        if (acceptance.get("required") or run.intent == "create") and not changed_files:
            findings.append(self._finding("guardian.semantic_contract_mismatch", "Acceptance requires product changes, but the draft has no changed files.", {"acceptance_contract": acceptance}, category="product_readiness"))
        if re.search(r"\b(api|backend|route|endpoint)\b", prompt) and changed_files and not any(path.startswith(("miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py")) for path in changed_files):
            findings.append(self._finding("guardian.semantic_contract_mismatch", "Prompt asks for API/backend work but changed files do not touch backend contract surfaces.", {"prompt_terms": ["api", "backend"], "changed_files": changed_files[:20]}, category="product_readiness"))
        allowed = {str(path) for path in improve_slice.get("connected_files") or [] if str(path).strip()}
        protected = {str(path) for path in improve_slice.get("protected_files") or [] if str(path).strip()}
        if allowed:
            outside = [
                path
                for path in changed_files
                if path not in allowed and path in protected and not path.startswith("docs/")
            ]
            if outside:
                findings.append(
                    self._finding(
                        "guardian.improve_slice_broad_rewrite",
                        "Improve mode changed protected files outside the planned connected slice.",
                        {"outside_slice": outside[:20], "improve_slice_ref": improve_slice.get("improve_slice_ref")},
                        category="scope_control",
                    )
                )
        if override == "uncertain":
            findings.append(self._finding("guardian.semantic_uncertain", "Semantic reviewer could not confidently approve the source apply.", {"review_packet_ref": packet_ref}, category="policy", severity="high"))
        elif override == "block":
            findings.append(self._finding("guardian.semantic_forced_block", "Semantic reviewer blocked the source apply.", {"review_packet_ref": packet_ref}, category="policy", severity="high"))
        if findings and verdict == "allow":
            verdict = "block"
        if verdict == "uncertain":
            status = "uncertain"
        elif verdict == "block":
            status = "blocked"
        return GuardianSemanticReviewReport(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            status=status,
            verdict=verdict,
            review_packet_ref=packet_ref,
            findings=findings,
            summary={"finding_count": len(findings), "bounded_packet": True, "mode": "deterministic_semantic_worker_v1"},
            evidence={"changed_files": changed_files[:80], "diff_summary": diff_summary},
        )

    def _review_packet(self, *, run: RunRecord, artifacts: dict[str, Any], changed_files: list[str], diff_text: str, draft_gate_ref: str | None) -> dict[str, Any]:
        check_results = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        check_by_name = {str(item.get("name") or ""): str(item.get("status") or "") for item in check_results}
        improve_slice = self.store.get("reports", getattr(run, "improve_slice_ref", None) or f"improve_slice:{run.workspace_id}:{run.run_id}")
        return {
            "schema": "grounded.guardian_review_packet.v1",
            "workspace_id": run.workspace_id,
            "run_id": run.run_id,
            "prompt": run.prompt[:2000],
            "acceptance_contract": run.acceptance_contract,
            "implementation_plan_summary": self._summary(run.implementation_plan),
            "changed_files": changed_files[:100],
            "diff_sha256": _sha256(diff_text),
            "diff_summary": self._diff_summary(diff_text),
            "improve_slice": improve_slice if isinstance(improve_slice, dict) else {},
            "proof_refs": {
                "draft_gate_ref": draft_gate_ref,
                "lsp_context_ref": getattr(run, "lsp_context_ref", None),
                "browser_proof_ref": run.browser_proof_ref,
                "verification_report_ref": run.verification_report_ref,
                "api_workflow_smoke_passed": check_by_name.get("api_workflow_smoke") == "passed",
                "browser_flow_smoke_passed": check_by_name.get("browser_flow_smoke") == "passed",
                "check_names": sorted(check_by_name),
            },
        }

    @staticmethod
    def _summary(value: dict[str, Any]) -> dict[str, Any]:
        return {str(key): value.get(key) for key in list(value.keys())[:20]} if isinstance(value, dict) else {}

    @staticmethod
    def _diff_summary(diff_text: str) -> dict[str, Any]:
        deleted_routes: list[str] = []
        deleted_role_ui: list[str] = []
        added_role_ui: list[str] = []
        added_lines = 0
        removed_lines = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines += 1
                if re.search(r"(client|specialist|manager|role|tab|view|form|button|input|status)", line, flags=re.IGNORECASE):
                    added_role_ui.append(line[1:].strip()[:180])
            if not line.startswith("-") or line.startswith("---"):
                continue
            removed_lines += 1
            if re.search(r"@(?:router|app)\.(?:get|post|put|patch|delete)\s*\(", line) or re.search(r"['\"](/api/[a-zA-Z0-9_./:-]+)['\"]", line):
                deleted_routes.append(line[1:].strip()[:180])
            if re.search(r"(client|specialist|manager|role|tab|view)", line, flags=re.IGNORECASE):
                deleted_role_ui.append(line[1:].strip()[:180])
        return {
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "deleted_routes": list(dict.fromkeys(deleted_routes))[:20],
            "deleted_role_ui": list(dict.fromkeys(deleted_role_ui))[:20],
            "added_role_ui": list(dict.fromkeys(added_role_ui))[:20],
        }

    @staticmethod
    def _finding(code: str, message: str, evidence: dict[str, Any], *, category: str = "product_readiness", severity: str = "high") -> GuardianFinding:
        return GuardianFinding(code=code, category=category, severity=severity, message=message, evidence=evidence, repair_hint="Repair the semantic mismatch and rerun guardian gate before apply.")

    def _repair_packets(self, *, run: RunRecord, findings: list[GuardianFinding], semantic: GuardianSemanticReviewReport) -> list[dict[str, Any]]:
        packets: list[dict[str, Any]] = []
        for finding in findings:
            if not finding.is_blocker_for_apply:
                continue
            packets.append(
                {
                    "schema": "grounded.guardian_repair_packet.v1",
                    "signature": finding.code,
                    "issue_code": finding.code,
                    "severity": finding.severity,
                    "target_files": [finding.file_path] if finding.file_path else list(semantic.evidence.get("changed_files") or [])[:20],
                    "required_next_tool": "read_files",
                    "failure_class": "guardian.semantic_gate_blocked" if finding.code.startswith("guardian.semantic") else "guardian.pre_apply_blocked",
                    "failure_signature": f"{finding.code}:{run.run_id}",
                    "instruction": finding.repair_hint or "Inspect the guardian finding, repair the changed draft, rerun checks and guardian gate.",
                    "evidence": finding.model_dump(mode="json"),
                }
            )
        return packets

    def _run_artifacts(self, run: RunRecord) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_artifacts:{run.run_id}")
        return payload if isinstance(payload, dict) else {}

    def _diff_text(self, run: RunRecord, artifacts: dict[str, Any]) -> str:
        diff_text = str(artifacts.get("diff") or "")
        if diff_text:
            return diff_text
        try:
            return self.workspace_service.diff(run.workspace_id, run_id=run.run_id)
        except Exception:
            return ""

    def _changed_files(self, run: RunRecord, changed_files: list[str] | None, diff_text: str) -> list[str]:
        if changed_files:
            return self._normalize_paths(changed_files)
        try:
            changed = self.workspace_service.draft_changed_paths(run.workspace_id, run.run_id)
            if changed:
                return changed
        except Exception:
            pass
        return self._paths_from_diff(diff_text) or list(run.touched_files or [])

    @staticmethod
    def _normalize_paths(paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in paths:
            path = str(raw or "").strip().replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path.startswith("draft/"):
                path = path.split("draft/", 1)[-1]
            if path.startswith("source/"):
                path = path.split("source/", 1)[-1]
            if path and path not in normalized:
                normalized.append(path)
        return normalized

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE):
            path = match.group(1).strip().replace("\\", "/")
            if path and path not in paths:
                paths.append(path)
        return paths

    def _check_execution(self, run: RunRecord, artifacts: dict[str, Any], changed_files: list[str]) -> CheckExecutionRecord | None:
        raw_checks = artifacts.get("check_results") if isinstance(artifacts.get("check_results"), list) else []
        if not raw_checks and run.linked_job_id:
            job_payload = self.store.get("jobs", run.linked_job_id)
            raw_checks = job_payload.get("executed_checks") if isinstance(job_payload, dict) and isinstance(job_payload.get("executed_checks"), list) else []
        results: list[RunCheckResult] = []
        for item in raw_checks or []:
            if not isinstance(item, dict):
                continue
            try:
                results.append(RunCheckResult.model_validate(item))
            except Exception:
                continue
        if not results:
            return None
        return CheckExecutionRecord(workspace_id=run.workspace_id, run_id=run.run_id, changed_files=changed_files, results=results, completed_at=_now())

    def _next_sequence(self, run_id: str) -> int:
        if self.event_journal_service is None:
            return 0
        events = self.event_journal_service.list_run(run_id, limit=1)
        return (events[-1].sequence + 1) if events else 1

    def _append_event(self, run: RunRecord, event_type: str, payload: dict[str, Any], *, source_ref: str | None = None, idempotency_key: str | None = None) -> None:
        if self.event_journal_service is None:
            return
        self.event_journal_service.append_run(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            event_type=event_type,
            payload=payload,
            actor="system",
            summary=event_type.replace(".", " "),
            source_ref=source_ref,
            idempotency_key=idempotency_key,
        )
