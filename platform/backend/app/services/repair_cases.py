from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from app.models.domain import CreateRunRequest, RunRecord
from app.repositories.state_store import StateStore


REPAIR_CASE_SCHEMA = "grounded.repair_case.v1"
REPAIR_CASE_INDEX_SCHEMA = "grounded.repair_cases.v1"
REPAIR_ATTEMPT_SCHEMA = "grounded.repair_attempt.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _clean_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _paths_from_any(value: Any) -> list[str]:
    paths: list[str] = []

    def add(candidate: object) -> None:
        path = _clean_path(candidate)
        if path.startswith("miniapp/") and path not in paths:
            paths.append(path)

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            for match in re.finditer(r"(miniapp/[A-Za-z0-9_./-]+\.(?:py|js|mjs|html|css|json))", item):
                add(match.group(1))
            return
        if isinstance(item, dict):
            for key in ("path", "file", "file_path", "location", "frontend_ref", "suggested_patch_target"):
                add(item.get(key))
            for key in ("paths", "files", "target_files", "changed_files"):
                nested = item.get(key)
                if isinstance(nested, list):
                    for candidate in nested:
                        add(candidate)
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)

    visit(value)
    return paths[:16]


def _compact_evidence(value: Any, *, max_chars: int = 7000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= max_chars:
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return {
        "truncated": True,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "excerpt": text[:max_chars],
    }


def patch_sha256(file_changes: list[Any]) -> str:
    normalized = []
    for change in file_changes:
        normalized.append(
            {
                "file_path": str(getattr(change, "file_path", "") or ""),
                "operation": str(getattr(change, "operation", "") or ""),
                "reason": str(getattr(change, "reason", "") or ""),
                "content_sha256": hashlib.sha256(str(getattr(change, "content", "") or "").encode("utf-8")).hexdigest(),
                "diff_sha256": hashlib.sha256(str(getattr(change, "diff", "") or "").encode("utf-8")).hexdigest(),
            }
        )
    return _json_sha(normalized)


class RepairPromptBuilder:
    @staticmethod
    def build(case: dict[str, Any]) -> dict[str, Any]:
        failure = str(case.get("failure_signature") or case.get("failure_class") or case.get("case_id") or "repair_case")
        first_tool = str(case.get("required_next_tool") or "read_files")
        target_files = list(case.get("target_files") or [])
        forbidden_files = list(case.get("forbidden_files") or [])
        sections = {
            "failure": failure,
            "evidence": case.get("evidence") or {},
            "target_files": target_files,
            "forbidden_files": forbidden_files,
            "first_tool": first_tool,
            "allowed_edit_slice": case.get("allowed_edit_slice") or target_files,
            "expected_proof": case.get("expected_proof") or [],
            "retry_policy": case.get("retry_policy") or {},
        }
        return {
            "schema": "grounded.repair_prompt.v1",
            "case_id": case.get("case_id"),
            "mission": "Fix exactly this repair case. Do not broaden the edit.",
            "sections": sections,
            "failure": failure,
            "likely_cause": case.get("likely_cause") or case.get("likely_root_cause") or "",
            "evidence": sections["evidence"],
            "target_files": target_files,
            "forbidden_files": forbidden_files,
            "first_tool": first_tool,
            "allowed_edit_slice": sections["allowed_edit_slice"],
            "expected_proof": sections["expected_proof"],
            "retry_policy": sections["retry_policy"],
            "attempt_count": len(case.get("attempts") or []),
            "forbidden_repeat_action": case.get("forbidden_repeat_action") or "Do not repeat a patch/action already recorded as failed for this case.",
        }


class RepairCaseService:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def index_ref(run_id: str) -> str:
        return f"repair_cases:{run_id}"

    @staticmethod
    def case_ref(workspace_id: str, run_id: str, case_id: str) -> str:
        return f"repair_case:{workspace_id}:{run_id}:{case_id}"

    def list_cases(self, run_id: str) -> dict[str, Any]:
        index = self.store.get("reports", self.index_ref(run_id)) or {}
        items: list[dict[str, Any]] = []
        for ref in index.get("case_refs") or []:
            payload = self.store.get("reports", str(ref))
            if isinstance(payload, dict):
                items.append(payload)
        items.sort(key=lambda item: (self._severity_rank(item.get("severity")), str(item.get("updated_at") or "")))
        return {
            "schema": REPAIR_CASE_INDEX_SCHEMA,
            "run_id": run_id,
            "status": "available" if items else "empty",
            "items": items,
            "active_case": self._active_case(items),
            "case_refs": [self.case_ref(str(item.get("workspace_id")), run_id, str(item.get("case_id"))) for item in items],
            "updated_at": index.get("updated_at") or _now(),
        }

    def get_case(self, run_id: str, case_id: str) -> dict[str, Any] | None:
        for case in self.list_cases(run_id).get("items") or []:
            if str(case.get("case_id")) == str(case_id):
                return case
        return None

    def attempts(self, run_id: str, case_id: str) -> dict[str, Any]:
        case = self.get_case(run_id, case_id)
        if not case:
            return {"schema": "grounded.repair_attempts.v1", "run_id": run_id, "case_id": case_id, "status": "missing", "items": []}
        return {
            "schema": "grounded.repair_attempts.v1",
            "run_id": run_id,
            "case_id": case_id,
            "status": "available" if case.get("attempts") else "empty",
            "items": list(case.get("attempts") or []),
        }

    def sync_from_packets(
        self,
        *,
        workspace_id: str,
        run_id: str,
        packets: list[dict[str, Any]],
        source: str,
        trace_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not packets:
            return self.list_cases(run_id)
        existing_by_id = {
            str(item.get("case_id")): item
            for item in self.list_cases(run_id).get("items") or []
            if isinstance(item, dict)
        }
        case_refs: list[str] = []
        values: dict[str, dict[str, Any]] = {}
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            case = self._case_from_packet(workspace_id=workspace_id, run_id=run_id, packet=packet, source=source, trace_state=trace_state)
            existing = existing_by_id.get(str(case["case_id"]))
            if existing:
                case["attempts"] = list(existing.get("attempts") or [])
                case["created_at"] = existing.get("created_at") or case["created_at"]
                case["status"] = existing.get("status") if existing.get("status") in {"failed_attempt", "blocked"} else case["status"]
            ref = self.case_ref(workspace_id, run_id, str(case["case_id"]))
            case_refs.append(ref)
            values[ref] = case
        if values:
            self.store.upsert_many("reports", values)
        all_refs = list(dict.fromkeys([*(self.store.get("reports", self.index_ref(run_id)) or {}).get("case_refs", []), *case_refs]))
        index = {
            "schema": REPAIR_CASE_INDEX_SCHEMA,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "available",
            "case_refs": all_refs,
            "updated_at": _now(),
        }
        self.store.upsert("reports", self.index_ref(run_id), index)
        return self.list_cases(run_id)

    def sync_from_review(self, *, run: RunRecord, findings: list[dict[str, Any]]) -> dict[str, Any]:
        packets: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, dict) or not finding.get("is_blocker_for_product_acceptance"):
                continue
            packets.append(
                {
                    "signature": f"review.{finding.get('code') or finding.get('category') or 'finding'}",
                    "issue_code": str(finding.get("code") or finding.get("category") or "review_finding"),
                    "severity": finding.get("severity") or "high",
                    "likely_root_cause": finding.get("message") or "Review found a product acceptance blocker.",
                    "target_files": _paths_from_any(finding),
                    "verification_check": finding.get("source") or finding.get("category") or "review",
                    "instruction": finding.get("message") or "Repair the review finding and rerun review.",
                    "required_next_tool": "read_files",
                    "expected_proof": "review finding is no longer reported as a blocker",
                    "failure_class": "review_finding",
                    "failure_signature": f"review.{finding.get('code') or finding.get('category') or 'finding'}",
                    "evidence": {"review_finding": finding},
                }
            )
        return self.sync_from_packets(workspace_id=run.workspace_id, run_id=run.run_id, packets=packets, source="review")

    def record_attempt(
        self,
        *,
        workspace_id: str,
        run_id: str,
        case_id: str,
        attempt: dict[str, Any],
    ) -> dict[str, Any] | None:
        case = self.get_case(run_id, case_id)
        if not case:
            return None
        attempts = list(case.get("attempts") or [])
        payload = {
            "schema": REPAIR_ATTEMPT_SCHEMA,
            "attempt_id": attempt.get("attempt_id") or f"attempt_{len(attempts) + 1}",
            "case_id": case_id,
            "run_id": run_id,
            "status": attempt.get("status") or "recorded",
            "files_read": list(attempt.get("files_read") or []),
            "changed_files": list(attempt.get("changed_files") or []),
            "patch_sha256": attempt.get("patch_sha256") or "",
            "diagnostics_before": attempt.get("diagnostics_before") or {},
            "diagnostics_after": attempt.get("diagnostics_after") or {},
            "proof_result": attempt.get("proof_result") or {},
            "failure_reason": attempt.get("failure_reason") or "",
            "forbidden_repeat_action": attempt.get("forbidden_repeat_action") or case.get("forbidden_repeat_action") or "",
            "created_at": attempt.get("created_at") or _now(),
        }
        attempts.append(payload)
        case["attempts"] = attempts[-20:]
        case["status"] = "failed_attempt" if payload["status"] in {"failed", "blocked", "conflict"} else case.get("status") or "open"
        case["updated_at"] = _now()
        case["repair_prompt"] = RepairPromptBuilder.build(case)
        self.store.upsert("reports", self.case_ref(workspace_id, run_id, case_id), case)
        return case

    def repeated_patch(self, *, run_id: str, case_id: str, patch_hash: str) -> bool:
        case = self.get_case(run_id, case_id)
        if not case or not patch_hash:
            return False
        return any(str(item.get("patch_sha256") or "") == patch_hash for item in case.get("attempts") or [])

    def retry_request(self, run: RunRecord, case_id: str) -> CreateRunRequest:
        case = self.get_case(run.run_id, case_id)
        if not case:
            raise KeyError(case_id)
        prompt = (
            "Retry this specific repair case. Do not broaden the task.\n"
            f"{json.dumps(RepairPromptBuilder.build(case), ensure_ascii=False, indent=2, default=str)}"
        )
        return CreateRunRequest(
            prompt=prompt,
            mode="fix",
            intent="edit",
            apply_strategy="staged_auto_apply",
            target_role_scope=list(run.target_role_scope or []),
            model_profile=run.model_profile,
            generation_mode=run.generation_mode,
            resume_from_run_id=run.run_id,
        )

    @classmethod
    def enrich_packets(cls, packets: list[dict[str, Any]], cases_report: dict[str, Any]) -> list[dict[str, Any]]:
        active = cases_report.get("active_case") if isinstance(cases_report, dict) else None
        if not isinstance(active, dict):
            return packets
        prompt = RepairPromptBuilder.build(active)
        enriched: list[dict[str, Any]] = []
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            signature = str(packet.get("failure_signature") or packet.get("signature") or "")
            active_signature = str(active.get("failure_signature") or active.get("signature") or "")
            if signature and signature != active_signature:
                enriched.append(packet)
                continue
            enriched.append(
                {
                    **packet,
                    "repair_case_id": active.get("case_id"),
                    "repair_prompt": prompt,
                    "attempt_count": len(active.get("attempts") or []),
                    "forbidden_repeat_action": active.get("forbidden_repeat_action"),
                }
            )
        return enriched or packets

    @staticmethod
    def active_case_id(cases_report: dict[str, Any] | None) -> str:
        active = (cases_report or {}).get("active_case") if isinstance(cases_report, dict) else None
        return str(active.get("case_id") or "") if isinstance(active, dict) else ""

    @classmethod
    def _case_from_packet(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        packet: dict[str, Any],
        source: str,
        trace_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        signature = str(packet.get("failure_signature") or packet.get("signature") or packet.get("code") or "repair.uncatalogued_repair_case")
        target_files = list(dict.fromkeys([*list(packet.get("target_files") or []), *_paths_from_any(packet.get("evidence"))]))[:16]
        case_id = f"rc_{_json_sha({'signature': signature, 'target_files': target_files, 'check': packet.get('verification_check')})[:12]}"
        failure_class = str(packet.get("failure_class") or packet.get("verification_check") or source or "repair")
        evidence = {
            "packet": packet,
            "browser_replay": cls._browser_replay(packet),
            "api_replay": cls._api_replay(packet),
            "trace": {
                key: (trace_state or {}).get(key)
                for key in ("last_failed_attempt", "repeated_action", "next_best_repair_case", "stale_diff")
                if isinstance(trace_state, dict) and key in trace_state
            },
        }
        retry_policy = cls._retry_policy(packet)
        case = {
            "schema": REPAIR_CASE_SCHEMA,
            "case_id": case_id,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "open",
            "source": source,
            "failure_class": failure_class,
            "failure_signature": signature,
            "issue_code": str(packet.get("issue_code") or packet.get("code") or "uncatalogued_repair_case"),
            "severity": str(packet.get("severity") or "medium"),
            "likely_cause": str(packet.get("likely_root_cause") or packet.get("instruction") or "Repair requires evidence-driven triage."),
            "target_files": target_files,
            "forbidden_files": list(packet.get("forbidden_target_files") or []),
            "required_next_tool": str(packet.get("required_next_tool") or retry_policy.get("first_tool") or "read_files"),
            "allowed_edit_slice": target_files,
            "expected_proof": cls._expected_proof(packet),
            "retry_policy": retry_policy,
            "attempts": [],
            "evidence": _compact_evidence(evidence),
            "forbidden_repeat_action": "Do not repeat the same patch hash, same stale edit payload, or same broad rewrite for this case.",
            "created_at": _now(),
            "updated_at": _now(),
        }
        case["repair_prompt"] = RepairPromptBuilder.build(case)
        return case

    @staticmethod
    def _retry_policy(packet: dict[str, Any]) -> dict[str, Any]:
        code = str(packet.get("issue_code") or packet.get("code") or packet.get("signature") or "").lower()
        check = str(packet.get("verification_check") or "").lower()
        if "stale" in code or "read_state" in code:
            return {"policy_id": "stale_edit", "first_tool": "read_files", "steps": ["fresh_read", "single_patch_retry", "write_file_if_patch_conflicts"]}
        if "missing_route" in code or "route" in check:
            return {"policy_id": "missing_route", "first_tool": "lsp.route_static_context", "steps": ["route_static_context", "patch_route_or_page", "api_or_browser_proof"]}
        if "api_workflow" in code or "api_workflow" in check:
            return {"policy_id": "api_workflow_failed", "first_tool": "lsp.route_static_context", "steps": ["backend_schema_payload_slice", "api_smoke", "browser_flow"]}
        if "overflow" in code or "layout" in code:
            return {"policy_id": "browser_overflow", "first_tool": "read_files", "steps": ["css_html_slice_only", "mobile_browser_proof"]}
        if "boot" in code or "preview_boot" in check:
            return {"policy_id": "preview_boot_failed", "first_tool": "lsp.diagnostics", "steps": ["backend_import_route_boot", "preview_boot_smoke", "role_page_probe"], "forbidden_once": ["browser_verify"]}
        return {"policy_id": "evidence_driven_repair_case", "first_tool": "lsp.diagnostics" if packet.get("target_files") else "semantic_scan", "steps": ["collect_exact_evidence", "constrained_patch", "rerun_failing_check"]}

    @staticmethod
    def _expected_proof(packet: dict[str, Any]) -> list[dict[str, str]]:
        proof = packet.get("expected_proof") or packet.get("verification_check") or "rerun failing check successfully"
        if isinstance(proof, list):
            return [{"kind": "check", "value": str(item)} for item in proof]
        return [{"kind": "check", "value": str(proof)}]

    @staticmethod
    def _browser_replay(packet: dict[str, Any]) -> dict[str, Any]:
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        candidates = [evidence, evidence.get("diagnostics") if isinstance(evidence, dict) else {}, evidence.get("source_issue") if isinstance(evidence, dict) else {}]
        keys = ("role", "url", "step", "selector", "expected_marker", "actual_marker", "dom_excerpt", "console_errors", "network_errors", "screenshot_ref", "proof_ref")
        replay = {key: value.get(key) for value in candidates if isinstance(value, dict) for key in keys if value.get(key)}
        return replay

    @staticmethod
    def _api_replay(packet: dict[str, Any]) -> dict[str, Any]:
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        text = json.dumps(evidence, ensure_ascii=False, default=str)
        method_match = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b", text)
        path_match = re.search(r"(/api/[A-Za-z0-9_/{}/.-]+)", text)
        payload = evidence.get("payload") if isinstance(evidence, dict) else None
        body = evidence.get("body") if isinstance(evidence, dict) else None
        return {
            key: value
            for key, value in {
                "method": method_match.group(1) if method_match else None,
                "path": path_match.group(1) if path_match else None,
                "payload": payload,
                "response_body": body,
                "expected_persisted_marker": evidence.get("expected_marker") if isinstance(evidence, dict) else None,
            }.items()
            if value
        }

    @staticmethod
    def _active_case(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        open_items = [item for item in items if str(item.get("status") or "open") not in {"repaired", "resolved"}]
        if not open_items:
            return None
        return sorted(open_items, key=lambda item: (RepairCaseService._severity_rank(item.get("severity")), len(item.get("attempts") or [])))[0]

    @staticmethod
    def _severity_rank(value: object) -> int:
        return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(value or "").lower(), 4)
