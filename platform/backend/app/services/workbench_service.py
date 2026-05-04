from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from typing import Any
from uuid import uuid4

from app.models.domain import CreateRunRequest, RunRecord
from app.repositories.state_store import StateStore
from app.services.exec_policy_service import ExecPolicyService
from app.services.miniapp_contract import MiniAppContractCompiler, MiniAppContractMaterializer, MiniAppRouteRegistry
from app.services.repair_catalog import RepairCatalog
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, tool_envelope, tool_registry_contract
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService
from app.core.config import Settings
from app.ai.openai_client import OpenAIClient
from app.models.artifacts import PatchOperationModel


def re_slug(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "skill"


class WorkbenchService:
    """Read-model and scaffold service for the agent workbench APIs."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        workspace_service: WorkspaceService,
        run_service: RunService,
        openai_client: OpenAIClient,
        exec_policy_service: ExecPolicyService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service
        self.run_service = run_service
        self.openai_client = openai_client
        self.exec_policy_service = exec_policy_service

    def tool_events(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        events: list[dict[str, Any]] = []
        events.extend(self._stored_tool_events(run_id))
        for item in artifacts.get("agent_activity_events") or run.agent_activity_events or []:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool") or (item.get("details") if isinstance(item.get("details"), dict) else {}).get("tool")
            events.append(
                tool_envelope(
                    tool=tool or item.get("type") or "agent.activity",
                    input_payload={"message": item.get("message"), "details": item.get("details") or {}},
                    result={"status": item.get("status") or item.get("type") or "recorded"},
                    artifacts=[{"ref": item.get("artifact_ref")}] if item.get("artifact_ref") else [],
                    timing={"duration_ms": item.get("duration_ms") or item.get("elapsed_ms")},
                    tool_call_id=str(item.get("tool_use_id") or item.get("batch_id") or f"activity_{len(events) + 1}"),
                )
            )
        for decision in artifacts.get("command_policy_decisions") or []:
            events.append(
                tool_envelope(
                    tool="policy.evaluate",
                    input_payload=decision if isinstance(decision, dict) else {"decision": decision},
                    result={"status": "recorded"},
                    risk="read_only",
                )
            )
        return {"run_id": run_id, "tool_protocol_version": TOOL_PROTOCOL_VERSION, "events": events}

    def trace_view(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        timeline = self.timeline(run_id)["items"]
        artifacts = self._run_artifacts_or_empty(run_id)
        failures = [
            item
            for item in timeline
            if str(item.get("status") or "").lower() in {"failed", "blocked", "conflict"}
            or str(item.get("kind") or "") in {"failure"}
        ]
        fixes = [
            item
            for item in timeline
            if str(item.get("kind") or "") in {"editing", "apply", "checks", "browser"}
            and str(item.get("status") or "").lower() in {"completed", "passed", "applied"}
        ]
        reducer = {
            "why": self._trace_why(run, artifacts),
            "failed_checks": [item for item in timeline if item.get("kind") == "checks" and item.get("status") == "failed"],
            "patches": [item for item in timeline if item.get("kind") in {"editing", "diff", "apply"}],
            "browser_proofs": [item for item in timeline if item.get("kind") == "browser"],
            "failures": failures,
            "fixes": fixes,
        }
        payload = {
            "run_id": run_id,
            "trace_id": f"trace_{run_id}",
            "status": run.status,
            "apply_status": run.apply_status,
            "timeline": timeline,
            "reducer": reducer,
            "artifact_refs": {
                "transcript": run.agent_transcript_ref,
                "tool_trace": run.tool_trace_ref,
                "rollout_trace": run.rollout_trace_ref,
                "browser_proof": run.browser_proof_ref,
                "verification": run.verification_report_ref,
            },
        }
        self.store.upsert("reports", f"trace_view:{run_id}", payload)
        return payload

    def record_tool_event(self, run_id: str | None, event: dict[str, Any]) -> dict[str, Any]:
        if not run_id:
            return event
        self.run_service.get_run(run_id)
        key = f"tool_events:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {**event, "sequence": len(payload.get("items") or []) + 1, "created_at": datetime.now(timezone.utc).isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)
        return item

    def evaluate_command_for_run(self, run_id: str, command: str, *, preset: str = "safe_auto") -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        evaluation = self.exec_policy_service.evaluate_command(command, preset=preset)
        approval = dict(evaluation.get("approval") or {})
        if approval.get("required") and approval.get("approval_id"):
            self._upsert_approval(
                run_id,
                {
                    "approval_id": str(approval["approval_id"]),
                    "status": "pending",
                    "kind": "command",
                    "risk": (evaluation.get("decision") or {}).get("risk"),
                    "summary": self.exec_policy_service.redact(command),
                    "input": {"command": self.exec_policy_service.redact(command), "workspace_id": run.workspace_id},
                    "policy_decision": evaluation.get("decision"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        self.record_tool_event(
            run_id,
            tool_envelope(
                tool="policy.evaluate",
                input_payload={"command": self.exec_policy_service.redact(command), "preset": preset},
                result=evaluation,
                risk=(evaluation.get("decision") or {}).get("risk") or "unknown",
                approval=approval,
            ),
        )
        return evaluation

    def assert_approval_allows(self, run_id: str | None, approval_id: str | None) -> None:
        if not run_id or not approval_id:
            return
        approval = self._approval_by_id(run_id, approval_id)
        if not approval:
            raise PermissionError(f"Approval not found: {approval_id}")
        if approval.get("status") != "approved":
            raise PermissionError(f"Approval {approval_id} is {approval.get('status')}.")

    def artifact(self, run_id: str, artifact_ref: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        normalized = str(artifact_ref or "").strip()
        if normalized == "run_artifacts":
            return self.run_service.get_run_artifacts(run_id)
        payload = self.store.get("reports", normalized)
        if payload is None:
            raise KeyError(f"Artifact not found: {artifact_ref}")
        return {"artifact_ref": normalized, "payload": payload}

    def timeline(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        items: list[dict[str, Any]] = [
            self._timeline_item("prompt", "completed", "Prompt received", {"prompt": run.prompt}, created_at=run.created_at.isoformat()),
        ]
        for approval in self.approvals(run_id)["items"]:
            items.append(
                self._timeline_item(
                    "approval",
                    str(approval.get("status") or "pending"),
                    str(approval.get("summary") or approval.get("kind") or "Approval"),
                    approval,
                    created_at=str(approval.get("decided_at") or approval.get("created_at") or datetime.now(timezone.utc).isoformat()),
                )
            )
        for event in self._stored_tool_events(run_id):
            items.append(
                self._timeline_item(
                    "policy" if event.get("tool") == "policy.evaluate" else "tool",
                    str(((event.get("result") if isinstance(event.get("result"), dict) else {}) or {}).get("status") or "recorded"),
                    str(event.get("tool") or "Tool event"),
                    event,
                    created_at=str(event.get("created_at") or datetime.now(timezone.utc).isoformat()),
                )
            )
        for event in artifacts.get("agent_activity_events") or run.agent_activity_events or []:
            if isinstance(event, dict):
                items.append(self._timeline_from_activity(event))
        for check in artifacts.get("check_results") or []:
            if isinstance(check, dict):
                items.append(self._timeline_item("checks", str(check.get("status") or "completed"), str(check.get("name") or "Check"), check))
        if artifacts.get("diff"):
            items.append(
                self._timeline_item(
                    "diff",
                    "completed",
                    "Draft diff recorded",
                    {"changed_files": run.touched_files, "artifact_ref": "run_artifacts"},
                )
            )
        if artifacts.get("browser_flow_proof") or artifacts.get("browser_proof_steps"):
            items.append(self._timeline_item("browser", "completed", "Browser proof recorded", {"artifact_ref": run.browser_proof_ref}))
        if run.apply_status == "applied":
            items.append(self._timeline_item("apply", "completed", "Draft applied to workspace", {"revision_id": run.result_revision_id}))
        if run.status in {"failed", "blocked"}:
            items.append(self._timeline_item("failure", run.status, run.failure_reason or "Run did not complete", {"failure_class": run.failure_class}))
        if run.status == "completed":
            items.append(self._timeline_item("complete", "completed", "Run completed", {"summary": run.summary}))
        return {
            "run_id": run_id,
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "items": [
                {**item, "sequence": index + 1}
                for index, item in enumerate(sorted(items, key=lambda item: str(item.get("created_at") or "")))
            ],
        }

    def observability(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        timeline_items = self.timeline(run_id).get("items") or []
        return {
            "trace_id": f"trace_{run.run_id}",
            "run_id": run.run_id,
            "thread_id": None,
            "turn_id": None,
            "tool_call_count": len((self.tool_events(run_id)).get("events") or []),
            "span_count": len(timeline_items),
            "spans": [
                {
                    "span_id": f"span_{index + 1}",
                    "name": item.get("kind"),
                    "status": item.get("status"),
                    "started_at": item.get("created_at"),
                    "attributes": {"title": item.get("title"), "sequence": item.get("sequence")},
                }
                for index, item in enumerate(timeline_items[:200])
            ],
            "latency_breakdown": artifacts.get("latency_breakdown") or {},
            "token_usage": run.token_usage,
            "model_profile": run.model_profile,
            "llm_provider": run.llm_provider,
            "llm_model": run.llm_model,
            "failure": {
                "class": run.failure_class,
                "signature": run.failure_signature,
                "reason": run.failure_reason,
            },
        }

    def git_status(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)

        def git(args: list[str]) -> tuple[int, str, str]:
            try:
                result = subprocess.run(["git", *args], cwd=source_dir, text=True, capture_output=True, timeout=8)
                return result.returncode, result.stdout.strip(), result.stderr.strip()
            except Exception as exc:
                return 1, "", str(exc)

        branch_code, branch, branch_err = git(["rev-parse", "--abbrev-ref", "HEAD"])
        status_code, status, status_err = git(["status", "--short"])
        log_code, log, log_err = git(["log", "--oneline", "-5"])
        return {
            "workspace_id": workspace_id,
            "source_dir": str(source_dir),
            "branch": branch if branch_code == 0 else None,
            "status": status.splitlines() if status_code == 0 and status else [],
            "recent_commits": log.splitlines() if log_code == 0 and log else [],
            "worktree_recommended_branch_prefix": "grounded/run-",
            "errors": [item for item in [branch_err, status_err, log_err] if item],
        }

    def workers(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        merge = ((artifacts.get("worker_results") or {}).get("merge") or {}) if isinstance(artifacts.get("worker_results"), dict) else {}
        worker_ids = ["backend_api", "client_ui", "specialist_ui", "manager_ui", "generated_tests", "verifier"]
        lanes = []
        for worker_id in worker_ids:
            summaries = [
                item
                for item in run.worker_summaries
                if isinstance(item, dict) and (item.get("worker") == worker_id or item.get("worker_id") == worker_id)
            ]
            merge_reports = [item for item in (merge.get("merge_reports") or []) if isinstance(item, dict) and item.get("worker_id") == worker_id]
            lanes.append(
                {
                    "worker_id": worker_id,
                    "status": self._worker_status(worker_id, run, summaries, merge_reports),
                    "owner_scope": self._worker_scope(worker_id),
                    "changed_files": [path for path in run.touched_files if self._path_owned_by_worker(worker_id, path)],
                    "summaries": summaries,
                    "merge_reports": merge_reports,
                }
            )
        return {"run_id": run_id, "workers": lanes, "worker_branch_refs": run.worker_branch_refs}

    def worker_artifacts(self, run_id: str, worker_id: str) -> dict[str, Any]:
        workers = self.workers(run_id)["workers"]
        lane = next((item for item in workers if item["worker_id"] == worker_id), None)
        if lane is None:
            raise KeyError(f"Worker not found: {worker_id}")
        return {"run_id": run_id, "worker_id": worker_id, "artifacts": lane}

    def worker_diff(self, run_id: str, worker_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        diff = self._run_artifacts_or_empty(run_id).get("diff") or ""
        owned_files = [path for path in run.touched_files if self._path_owned_by_worker(worker_id, path)]
        return {"run_id": run_id, "worker_id": worker_id, "owned_files": owned_files, "diff": self._filter_diff(diff, owned_files)}

    def review(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        findings = []
        for issue in run.checks_summary.issues:
            findings.append({"severity": issue.get("severity") or "medium", "code": issue.get("code"), "message": issue.get("message")})
        diff_text = str(artifacts.get("diff") or "")
        changed_files = run.touched_files or self._paths_from_diff(diff_text)
        if diff_text and not (artifacts.get("browser_proof_steps") or run.browser_flow_proof):
            findings.append(
                {
                    "severity": "medium",
                    "code": "missing_browser_proof",
                    "message": "Changed draft has no recorded browser proof.",
                }
            )
        risky_paths = [
            path
            for path in changed_files
            if path.startswith(("miniapp/app/generated/", "docker/", ".github/", "runtime/"))
        ]
        if risky_paths:
            findings.append(
                {
                    "severity": "high",
                    "code": "risky_generated_or_runtime_change",
                    "message": f"Review risky generated/runtime paths before apply: {', '.join(risky_paths[:8])}.",
                    "paths": risky_paths,
                }
            )
        if len(changed_files) >= 12 and not artifacts.get("check_results"):
            findings.append(
                {
                    "severity": "medium",
                    "code": "large_untested_change",
                    "message": "Large draft has no recorded check results.",
                    "changed_file_count": len(changed_files),
                }
            )
        payload = {
            "run_id": run_id,
            "status": "failed" if findings else "passed",
            "findings": findings,
            "evidence": {
                "diff_available": bool(artifacts.get("diff")),
                "checks": artifacts.get("check_results") or [],
                "browser_proof_ref": run.browser_proof_ref,
            },
        }
        self.store.upsert("reports", f"review:{run_id}", payload)
        return payload

    def start_review_fix(self, run_id: str) -> RunRecord:
        run = self.run_service.get_run(run_id)
        review = self.review(run_id)
        prompt = "Fix review findings:\n" + "\n".join(
            f"- {item.get('code')}: {item.get('message')}" for item in review.get("findings", []) if isinstance(item, dict)
        )
        if not review.get("findings"):
            prompt = "Run a focused verification pass and fix any concrete issue found."
        return self.run_service.create_run(
            run.workspace_id,
            CreateRunRequest(
                prompt=prompt,
                mode="fix",
                intent="edit",
                apply_strategy="staged_auto_apply",
                target_role_scope=list(run.target_role_scope or []),
                model_profile=run.model_profile,
                generation_mode=run.generation_mode,
            ),
        )

    def browser_proof(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        payload = {
            "run_id": run_id,
            "status": "available" if (run.browser_flow_proof or artifacts.get("browser_proof_steps")) else "not_recorded",
            "screenshots": self._collect_values(artifacts, keys=("screenshot", "screenshot_path", "image_path")),
            "console_errors": self._collect_values(artifacts, keys=("console_error", "console_errors")),
            "network_errors": self._collect_values(artifacts, keys=("network_error", "network_errors")),
            "route_coverage": (run.flow_coverage or {}).get("routes", []),
            "mobile_layout": run.mobile_layout_report,
            "role_workflows": run.browser_flow_proof,
            "steps": artifacts.get("browser_proof_steps") or [],
            "verification_report": self.store.get("reports", run.verification_report_ref) if run.verification_report_ref else None,
        }
        self.store.upsert("reports", f"browser_proof:{run_id}", payload)
        return payload

    def gate(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        diff_text = str(artifacts.get("diff") or "")
        check_results = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        checks_by_name = {str(item.get("name") or ""): item for item in check_results}
        mode_value = str(getattr(run.generation_mode, "value", run.generation_mode) or "").lower()
        acceptance_required = bool((run.acceptance_contract or {}).get("required")) or run.mode == "generate" or mode_value in {"quality", "balanced"}
        browser = checks_by_name.get("browser_flow_smoke") or {}
        api = checks_by_name.get("api_workflow_smoke") or {}
        mobile = run.mobile_layout_report or (browser.get("diagnostics") or {}).get("mobile_layout") if isinstance(browser, dict) else {}
        issues: list[dict[str, Any]] = []

        def add_issue(kind: str, check: str, details: str, *, blocking: bool = True, evidence: dict[str, Any] | None = None) -> None:
            issues.append({"kind": kind, "check": check, "details": details, "blocking": blocking, "evidence": evidence or {}})

        if run.mode in {"generate", "fix"} and not diff_text.strip() and not run.touched_files:
            add_issue("meaningful_diff", "meaningful_diff", "Run has no meaningful draft/source diff.")
        for item in check_results:
            if str(item.get("status")) in {"failed", "blocked"}:
                add_issue("check_failure", str(item.get("name") or "check"), str(item.get("details") or "Check failed."), evidence=item)
        if acceptance_required:
            if api.get("status") != "passed":
                add_issue("required_product_proof", "api_workflow_smoke", "API workflow proof must pass before completion.", evidence=api)
            if browser.get("status") != "passed":
                add_issue("required_product_proof", "browser_flow_smoke", "Browser workflow proof must pass before completion.", evidence=browser)
        if isinstance(mobile, dict) and mobile.get("status") == "failed":
            add_issue("mobile_layout", "browser_flow_smoke", "Mobile layout report contains blocking issues.", evidence=mobile)
        for signature in run.repair_issue_signatures:
            if isinstance(signature, dict) and not signature.get("resolved"):
                add_issue("unresolved_repair_signature", str(signature.get("check") or "repair"), str(signature.get("signature") or "Unresolved repair signature."), evidence=signature)
        if run.outcome_kind == "blocked_preview_infra":
            add_issue("preview_infra", "browser_flow_smoke", run.failure_reason or "Browser/preview infrastructure blocked product proof.", evidence=artifacts.get("preview_infra_diagnostics") or {})
        apply_ok = run.apply_status == "applied" or (
            run.apply_strategy == "manual_approve" and run.status == "awaiting_approval" and run.apply_status == "awaiting_approval"
        )
        if run.status in {"completed", "awaiting_approval", "blocked", "failed"} and not apply_ok:
            add_issue("apply_gate", "apply_status", "Run must be applied or awaiting manual approval after green checks.", evidence={"apply_status": run.apply_status, "status": run.status})

        repair_packets = RepairCatalog.classify_many(issues)
        blocking = any(item.get("blocking", True) for item in issues)
        status = "passed" if not blocking and apply_ok else "blocked" if blocking else "pending"
        payload = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "blocking": blocking,
            "issues": issues,
            "repair_packets": repair_packets,
            "requirements": {
                "acceptance_required": acceptance_required,
                "meaningful_diff": True,
                "api_workflow_smoke": acceptance_required,
                "browser_flow_smoke": acceptance_required,
                "mobile_layout_non_blocking": True,
                "apply_status": "applied_or_awaiting_approval",
            },
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}",
                "browser_proof": run.browser_proof_ref,
                "repair_recipes": run.repair_recipes_ref,
                "final_report": f"final_report:{run_id}",
            },
        }
        self.store.upsert("reports", f"gate:{run_id}", payload)
        return payload

    def repair_signatures(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        gate = self.gate(run_id)
        explicit = [item for item in run.repair_issue_signatures if isinstance(item, dict)]
        packets = RepairCatalog.classify_many([*explicit, *gate.get("issues", [])])
        payload = {
            "run_id": run_id,
            "status": "available" if packets else "empty",
            "blocking": bool(gate.get("blocking")),
            "items": packets,
            "catalog": RepairCatalog.entries(),
        }
        self.store.upsert("reports", f"repair_signatures:{run_id}", payload)
        return payload

    def final_report(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        preview = artifacts.get("preview") or {}
        gate = self.gate(run_id)
        report = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": "passed" if gate.get("status") == "passed" else "blocked" if gate.get("blocking") else run.status,
            "blocking": bool(gate.get("blocking")),
            "prompt": run.prompt,
            "summary": run.summary,
            "acceptance_contract": run.acceptance_contract,
            "diff_summary": {
                "changed_files": run.touched_files or self._paths_from_diff(str(artifacts.get("diff") or "")),
                "diff_available": bool(str(artifacts.get("diff") or "").strip()),
            },
            "checks": artifacts.get("check_results") or [],
            "browser_proof": run.browser_flow_proof or artifacts.get("browser_flow_proof") or {},
            "repair_signatures": self.repair_signatures(run_id).get("items", []),
            "preview": {
                "url": preview.get("url"),
                "role_urls": preview.get("role_urls") or {},
                "status": preview.get("status"),
            },
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}",
                "gate": f"gate:{run_id}",
                "browser_proof": run.browser_proof_ref,
                "repair_recipes": run.repair_recipes_ref,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"final_report:{run_id}", report)
        return report

    def resume_run(self, run_id: str) -> RunRecord:
        run = self.run_service.get_run(run_id)
        checkpoint = self.store.get("reports", run.resume_checkpoint_ref) if run.resume_checkpoint_ref else None
        prompt = (
            "Resume the retained run from its checkpoint. Use the existing acceptance contract, "
            "repair signatures, and draft evidence; do not restart diagnosis from scratch."
        )
        if isinstance(checkpoint, dict) and checkpoint.get("reason"):
            prompt = f"{prompt}\nCheckpoint reason: {checkpoint.get('reason')}"
        return self.run_service.create_run(
            run.workspace_id,
            CreateRunRequest(
                prompt=prompt,
                mode="fix",
                intent="edit",
                apply_strategy=run.apply_strategy,
                target_role_scope=list(run.target_role_scope or []),
                model_profile=run.model_profile,
                generation_mode=run.generation_mode,
                resume_from_run_id=run.run_id,
            ),
        )

    def memory(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        current = self.store.get("reports", f"workspace_memory:{workspace_id}") or {
            "workspace_id": workspace_id,
            "items": [],
            "project_rules": [],
            "user_preferences": [],
            "product_decisions": [],
            "accepted_ux_rules": [],
            "architecture_summary": [],
            "known_failures": [],
            "rejected_approaches": [],
            "do_not_change": [],
            "platform_constraints": [],
            "repeated_fixes": [],
        }
        current["stale_check"] = self._memory_stale_check(workspace_id, current)
        return current

    def upsert_memory(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.memory(workspace_id)
        item = {
            "memory_id": f"mem_{uuid4().hex}",
            "kind": str(payload.get("kind") or "note"),
            "text": str(payload.get("text") or payload.get("content") or "").strip(),
            "citation": payload.get("citation"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if not item["text"]:
            raise ValueError("Memory text is required.")
        current.setdefault("items", []).append(item)
        bucket_map = {
            "user_preference": "user_preferences",
            "project_rule": "project_rules",
            "product_decision": "product_decisions",
            "ux_rule": "accepted_ux_rules",
            "architecture": "architecture_summary",
            "known_failure": "known_failures",
            "rejected_approach": "rejected_approaches",
            "do_not_change": "do_not_change",
            "repeated_fix": "repeated_fixes",
        }
        bucket = bucket_map.get(item["kind"])
        if bucket:
            current.setdefault(bucket, []).append(item)
        current["stale_check"] = self._memory_stale_check(workspace_id, current)
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", current)
        return current

    def skills(self) -> dict[str, Any]:
        skills = dict(self._builtin_skills())
        for item in self._document_skills():
            skills.setdefault(item["id"], item)
        return {"items": sorted(skills.values(), key=lambda item: str(item.get("id") or ""))}

    def skill(self, skill_id: str) -> dict[str, Any]:
        item = {item["id"]: item for item in self.skills()["items"]}.get(skill_id)
        if item is None:
            raise KeyError(f"Skill not found: {skill_id}")
        return item

    def plugins(self) -> dict[str, Any]:
        items = [
            {"id": "core.validators", "version": "0.1.0", "capabilities": ["validators"], "status": "installed"},
            {"id": "core.exporters", "version": "0.1.0", "capabilities": ["exporters"], "status": "installed"},
            {"id": "core.preview", "version": "0.1.0", "capabilities": ["preview_adapters"], "status": "installed"},
        ]
        items.extend(self._load_plugin_manifests())
        return {"items": sorted(items, key=lambda item: str(item.get("id") or ""))}

    def install_plugin_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = dict(payload or {})
        plugin_id = str(manifest.get("id") or "").strip()
        if not plugin_id:
            raise ValueError("Plugin manifest id is required.")
        if not str(manifest.get("version") or "").strip():
            raise ValueError("Plugin manifest version is required.")
        if not isinstance(manifest.get("capabilities"), list):
            raise ValueError("Plugin manifest capabilities must be a list.")
        record = {"id": plugin_id, "status": "registered", "manifest": manifest, "installed_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"plugin:{plugin_id}", record)
        return record

    def mcp_servers(self) -> dict[str, Any]:
        config = self._mcp_config()
        return {"items": config.get("servers", []), "status": "configured" if config.get("servers") else "not_configured"}

    def mcp_tools(self) -> dict[str, Any]:
        config = self._mcp_config()
        return {"items": config.get("tools", []), "tool_protocol": tool_registry_contract()}

    def call_mcp_tool(self, tool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_id": tool_id,
            "status": "blocked",
            "approval_required": True,
            "reason": "External MCP tool execution is reserved until connector configuration is present.",
            "input": payload,
        }

    def doctor(self) -> dict[str, Any]:
        checks = [
            self._check("python", True, sys.version.split()[0], str(Path(sys.executable))),
            self._binary_check("node"),
            self._binary_check("npm"),
            self._binary_check("docker"),
            self._compose_check(),
            self._playwright_check(),
            self._openai_check(),
            self._writable_check("data_dir", self.settings.data_dir),
            self._template_check(),
            self._port_check(),
            self._backend_routes_check(),
            self._stale_backend_check(),
            self._playwright_browsers_check(),
            self._preview_container_check(),
            self._test_command_check(),
        ]
        status = "passed" if all(item["status"] == "passed" for item in checks if item["required"]) else "failed"
        payload = {"status": status, "checks": checks, "created_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", "doctor:last", payload)
        return payload

    def metrics_summary(self) -> dict[str, Any]:
        runs = self.store.list("runs")
        return {
            "run_count": len(runs),
            "completed_runs": len([run for run in runs if run.get("status") == "completed"]),
            "failed_runs": len([run for run in runs if run.get("status") == "failed"]),
            "blocked_runs": len([run for run in runs if run.get("status") == "blocked"]),
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "token_usage_total": sum(int(((run.get("token_usage") or {}).get("total_tokens") or 0)) for run in runs),
            "latency_ms_total": sum(int(((run.get("latency_breakdown") or {}).get("total_ms") or 0)) for run in runs),
        }

    def config_schema(self) -> dict[str, Any]:
        return {
            "platform_config_version": "grounded.platform.v1",
            "workspace_config_version": "grounded.workspace.v1",
            "policy_config_version": "grounded.policy.v1",
            "plugin_config_version": "grounded.plugin.v1",
            "schemas": {
                "platform": {
                    "required": ["data_dir", "runtime_dir", "template_dir", "preview_port_base"],
                    "properties": {
                        "data_dir": str(self.settings.data_dir),
                        "runtime_dir": str(self.settings.runtime_dir),
                        "template_dir": str(self.settings.template_dir),
                        "preview_port_base": self.settings.preview_port_base,
                    },
                },
                "workspace": {
                    "required": ["workspace_id", "name", "target_platform", "preview_profile", "current_revision_id"],
                    "backward_compatible": True,
                },
                "policy": self.exec_policy_service.snapshot(),
                "plugin": {
                    "required": ["id", "version", "capabilities"],
                    "capabilities": ["validators", "exporters", "preview_adapters", "platform_adapters", "skills", "mcp_tools"],
                },
            },
        }

    def migrations(self) -> dict[str, Any]:
        checks = [
            {
                "id": "state_store_v1",
                "status": "current",
                "description": "JSON state collections are readable with additive fields.",
            },
            {
                "id": "workspace_metadata_v1",
                "status": "current",
                "description": "Workspace records keep revision history and tolerate unknown future fields through strict migrations at API edges.",
            },
            {
                "id": "artifact_refs_v1",
                "status": "current",
                "description": "Run artifacts remain addressable through report refs and compatibility fallbacks.",
            },
        ]
        return {"status": "current", "items": checks, "created_at": datetime.now(timezone.utc).isoformat()}

    def test_matrix(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        check_results = artifacts.get("check_results") or []
        checks_by_name = {str(item.get("name") or ""): item for item in check_results if isinstance(item, dict)}

        def entry(key: str, label: str, names: tuple[str, ...], *, required: bool = True) -> dict[str, Any]:
            matched = [checks_by_name[name] for name in names if name in checks_by_name]
            status = "skipped"
            if matched:
                status = "passed" if all(str(item.get("status")) == "passed" for item in matched) else "failed"
            return {"key": key, "label": label, "status": status, "required": required, "evidence": matched}

        items = [
            entry("backend_pytest", "Backend pytest", ("generated_backend_tests", "backend_tests", "pytest")),
            entry("frontend_js_smoke", "Frontend JS smoke", ("frontend_interaction_static_smoke", "js_syntax")),
            entry("role_pages", "Role page smoke", ("preview_route_smoke", "role_pages")),
            entry("accessibility", "Accessibility checks", ("mobile_layout", "accessibility"), required=False),
            entry("persistence", "Persisted workflow checks", ("api_workflow_proof", "browser_flow_smoke")),
            entry("docker_compose_boot", "Docker compose boot", ("preview_boot_smoke",), required=False),
            entry("playwright_proof", "Playwright proof", ("browser_flow_smoke", "browser_proof")),
        ]
        status = "passed" if all(item["status"] == "passed" for item in items if item["required"]) else "incomplete"
        payload = {"run_id": run_id, "workspace_id": run.workspace_id, "status": status, "items": items}
        self.store.upsert("reports", f"test_matrix:{run_id}", payload)
        return payload

    def prompt_contract(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        prompt_terms = set(re.findall(r"[a-zA-Zа-яА-Я0-9]{4,}", run.prompt.lower()))
        artifacts = self._run_artifacts_or_empty(run_id)
        diff_text = str(artifacts.get("diff") or "")
        diff_terms = set(re.findall(r"[a-zA-Zа-яА-Я0-9]{4,}", diff_text.lower()))
        overlap = sorted(prompt_terms & diff_terms)[:40]
        status = "passed" if not diff_text or overlap or len(prompt_terms) < 4 else "needs_review"
        payload = {
            "run_id": run_id,
            "status": status,
            "prompt_terms_checked": sorted(prompt_terms)[:80],
            "matched_terms": overlap,
            "findings": [] if status == "passed" else [{"severity": "medium", "message": "Diff has low lexical overlap with the user prompt; review product semantics before apply."}],
        }
        self.store.upsert("reports", f"prompt_contract:{run_id}", payload)
        return payload

    def miniapp_contract(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        contract_report = self.store.get("reports", run.miniapp_contract_ref) if run.miniapp_contract_ref else None
        if not isinstance(contract_report, dict):
            contract_report = artifacts.get("miniapp_contract") if isinstance(artifacts.get("miniapp_contract"), dict) else None
        contract_payload = (contract_report or {}).get("contract") if isinstance(contract_report, dict) else None
        source_dir = (
            self.workspace_service.draft_source_dir(run.workspace_id, run_id)
            if self.workspace_service.draft_exists(run.workspace_id, run_id)
            else self.workspace_service.source_dir(run.workspace_id)
        )
        contract = MiniAppRouteRegistry.load_contract(source_dir)
        if contract is None and isinstance(contract_payload, dict):
            try:
                from app.services.miniapp_contract import MiniAppContract

                contract = MiniAppContract.model_validate(contract_payload)
            except Exception:
                contract = None
        if contract is None:
            contract = MiniAppContractCompiler.compile(
                workspace_id=run.workspace_id,
                run_id=run.run_id,
                prompt=run.prompt,
                intent=run.intent,
                generation_mode=run.generation_mode,
                acceptance_contract=run.acceptance_contract,
                implementation_plan=run.implementation_plan,
            )
        registry_report = self.store.get("reports", run.route_registry_ref) if run.route_registry_ref else None
        registry_snapshot = None
        if isinstance(registry_report, dict) and isinstance(registry_report.get("snapshot"), dict):
            registry_snapshot = registry_report["snapshot"]
        if registry_snapshot is None:
            registry_snapshot = MiniAppRouteRegistry.snapshot(source_dir, contract).model_dump(mode="json")
        repair_report = self.store.get("reports", run.repair_recipes_ref) if run.repair_recipes_ref else None
        repair_recipes = (
            repair_report.get("items")
            if isinstance(repair_report, dict) and isinstance(repair_report.get("items"), list)
            else registry_snapshot.get("repair_recipes", [])
        )
        payload = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "status": registry_snapshot.get("status") or "passed",
            "contract": contract.model_dump(mode="json"),
            "registry_snapshot": registry_snapshot,
            "drift_issues": registry_snapshot.get("drift_issues", []),
            "repair_recipes": repair_recipes,
            "artifact_refs": {
                "miniapp_contract": run.miniapp_contract_ref,
                "route_registry": run.route_registry_ref,
                "contract_compile": run.contract_compile_ref,
                "repair_recipes": run.repair_recipes_ref,
            },
        }
        self.store.upsert("reports", f"miniapp_contract_view:{run_id}", payload)
        return payload

    def security_summary(self) -> dict[str, Any]:
        return {
            "status": "configured",
            "permission_rules": self.permission_rules()["items"],
            "recent_denials": self.recent_denials()["items"],
            "checks": [
                {"key": "path_traversal", "status": "covered", "evidence": "Workspace paths normalize through safe relative path checks."},
                {"key": "write_denylist", "status": "covered", "evidence": self.exec_policy_service.write_grants()["deny"]},
                {"key": "command_allow_deny", "status": "covered", "evidence": self.exec_policy_service.snapshot()["risk_model"]},
                {"key": "approval_bypass_prevention", "status": "covered", "evidence": "Approval ids are matched against run-scoped approval records."},
                {"key": "secret_redaction", "status": "covered", "evidence": "ExecPolicyService.redact is applied to command events."},
                {"key": "artifact_access_boundaries", "status": "covered", "evidence": "Artifacts are fetched through run-scoped refs."},
            ],
        }

    def permission_rules(self) -> dict[str, Any]:
        stored = self.store.get("reports", "permission_rules") or {"items": []}
        defaults = [
            {"rule_id": "allow_readonly_diagnostics", "scope": "workspace", "risk": "read_only", "action": "allow", "source": "default"},
            {"rule_id": "prompt_mutating", "scope": "workspace", "risk": "mutating", "action": "prompt", "source": "default"},
            {"rule_id": "block_destructive", "scope": "workspace", "risk": "destructive", "action": "block", "source": "default"},
            {"rule_id": "block_network", "scope": "external", "risk": "network", "action": "prompt", "source": "default"},
        ]
        merged = {item["rule_id"]: item for item in defaults}
        for item in stored.get("items") or []:
            if isinstance(item, dict) and item.get("rule_id"):
                merged[str(item["rule_id"])] = item
        return {"items": sorted(merged.values(), key=lambda item: str(item.get("rule_id") or ""))}

    def upsert_permission_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = {
            "rule_id": str(payload.get("rule_id") or f"rule_{uuid4().hex}"),
            "scope": str(payload.get("scope") or "workspace"),
            "risk": str(payload.get("risk") or "unknown"),
            "action": str(payload.get("action") or "prompt"),
            "pattern": str(payload.get("pattern") or ""),
            "source": "user",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        current = self.permission_rules()
        current["items"] = [entry for entry in current["items"] if entry.get("rule_id") != item["rule_id"]]
        current["items"].append(item)
        self.store.upsert("reports", "permission_rules", current)
        return item

    def recent_denials(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for key, payload in self.store.items("reports"):
            if not key.startswith("tool_events:") or not isinstance(payload, dict):
                continue
            for event in payload.get("items") or []:
                if not isinstance(event, dict):
                    continue
                approval = event.get("approval") if isinstance(event.get("approval"), dict) else {}
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
                if approval.get("status") == "blocked" or decision.get("action") == "forbidden":
                    items.append(
                        {
                            "run_id": payload.get("run_id"),
                            "tool": event.get("tool"),
                            "risk": event.get("risk") or decision.get("risk"),
                            "reason": decision.get("reason") or ((event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else ""),
                            "created_at": event.get("created_at"),
                        }
                    )
        return {"items": sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:50]}

    def compact_run(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        payload = {
            "run_id": run_id,
            "short_summary": run.summary or run.failure_reason or run.prompt[:240],
            "active_constraints": list((run.acceptance_contract or {}).get("constraints") or []),
            "accepted_decisions": list((run.implementation_plan or {}).get("decisions") or []),
            "known_failures": [run.failure_reason] if run.failure_reason else [],
            "current_file_focus": run.touched_files[:12],
            "unresolved_approvals": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"run_compaction:{run_id}", payload)
        return payload

    def stage_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        files = self._normalize_file_list(payload.get("files") or [])
        categories = {path: self._file_category(path) for path in files}
        record = {
            "run_id": run_id,
            "workspace_id": run.workspace_id,
            "files": files,
            "categories": categories,
            "status": "staged",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", f"staged_files:{run_id}", record)
        self.record_tool_event(run_id, tool_envelope(tool="approval.stage", input_payload=record, result={"status": "staged"}, risk="mutating"))
        return record

    def discard_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        files = self._normalize_file_list(payload.get("files") or [])
        result = self.workspace_service.discard_draft_files(run.workspace_id, run_id, files)
        record = {"run_id": run_id, "files": files, "result": result, "status": "discarded", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"discarded_files:{run_id}", record)
        self.record_tool_event(run_id, tool_envelope(tool="file.discard", input_payload={"files": files}, result=record, risk="mutating"))
        return record

    def apply_staged(self, run_id: str) -> Any:
        run = self.run_service.get_run(run_id)
        staged = self.store.get("reports", f"staged_files:{run_id}") or {}
        changed_before_apply = set(self.workspace_service.draft_changed_paths(run.workspace_id, run_id))
        staged_files = staged.get("files") if isinstance(staged.get("files"), list) else None
        files = self._normalize_file_list(staged_files if staged_files is not None else list(changed_before_apply))
        contract_owned = set(MiniAppContractMaterializer.contract_owned_paths())
        blocking_required = sorted(
            path
            for path in changed_before_apply
            if path in contract_owned or path.startswith("miniapp/app/generated/")
        )
        files = list(dict.fromkeys([*files, *blocking_required]))
        revision = self.workspace_service.apply_selected_draft_files(run.workspace_id, run_id, files, message=f"Apply staged AI draft files for run {run_id}")
        fully_applied = bool(changed_before_apply) and set(files).issuperset(changed_before_apply)
        run.result_revision_id = revision.revision_id
        run.candidate_revision_id = revision.revision_id
        run.touched_files = files
        run.apply_status = "applied" if fully_applied else "awaiting_approval"
        run.status = "completed" if fully_applied else "awaiting_approval"
        run.draft_status = "approved" if fully_applied else "ready"
        run.draft_ready = not fully_applied
        run.progress_percent = 100 if fully_applied else max(run.progress_percent, 95)
        run.current_stage = "completed" if fully_applied else "partially applied"
        run.updated_at = datetime.now(timezone.utc)
        if fully_applied:
            self.workspace_service.discard_draft(run.workspace_id, run_id)
        self.store.upsert("runs", run_id, run.model_dump(mode="json"))
        artifacts = self._run_artifacts_or_empty(run_id)
        artifacts["run"] = run.model_dump(mode="json")
        artifacts["staged_apply"] = {"files": files, "revision_id": revision.revision_id, "fully_applied": fully_applied}
        self.store.upsert("reports", f"run_artifacts:{run_id}", artifacts)
        self.record_tool_event(run_id, tool_envelope(tool="patch.apply", input_payload={"files": files}, result=artifacts["staged_apply"], risk="mutating"))
        return run

    def diff(self, run_id: str, *, base: str, target: str, file: str | None = None, worker_id: str | None = None, category: str | None = None, status: str | None = None) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        diff = artifacts.get("diff") or self.workspace_service.diff(run.workspace_id, run_id=run_id)
        files = run.touched_files or self._paths_from_diff(diff)
        if file:
            files = [path for path in files if path == file]
        if worker_id:
            files = [path for path in files if self._path_owned_by_worker(worker_id, path)]
        if category:
            files = [path for path in files if self._file_category(path) == category]
        filtered_diff = self._filter_diff(diff, files) if (file or worker_id or category or status) else diff
        return {"run_id": run_id, "base": base, "target": target, "diff": filtered_diff, "files": files, "filters": {"file": file, "worker_id": worker_id, "category": category, "status": status}}

    def approvals(self, run_id: str) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        return self.store.get("reports", f"approvals:{run_id}") or {"run_id": run_id, "items": []}

    def file_search(self, workspace_id: str, *, query: str, run_id: str | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        if run_id and ("/" in run_id or "\\" in run_id or ".." in Path(run_id).parts):
            raise ValueError("Run id must not contain path traversal.")
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"workspace_id": workspace_id, "query": normalized_query, "items": []}
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        if run_id and not root.exists():
            raise KeyError(f"Draft not found for run: {run_id}")
        if shutil.which("rg"):
            rg_items = self._ripgrep_search(root, normalized_query)
            if rg_items:
                return {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "query": normalized_query,
                    "engine": "ripgrep",
                    "items": self._rank_search_items(rg_items, normalized_query, root),
                    "symbols": self._symbol_overview(root, normalized_query),
                }
        items: list[dict[str, Any]] = []
        for entry in self.workspace_service.file_tree(workspace_id, run_id=run_id):
            if entry.get("type") != "file":
                continue
            relative_path = str(entry.get("path") or "")
            if ".." in Path(relative_path).parts:
                raise ValueError("Search paths must stay inside the workspace.")
            haystack = relative_path.lower()
            content = self.workspace_service.try_read_text_file(workspace_id, relative_path, run_id=run_id)
            hits: list[dict[str, Any]] = []
            if content:
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if normalized_query.lower() in line.lower():
                        hits.append({"line": line_no, "text": line[:240]})
                    if len(hits) >= 5:
                        break
            if normalized_query.lower() in haystack or hits:
                items.append({"path": relative_path, "hits": hits})
            if len(items) >= 80:
                break
        return {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "query": normalized_query,
            "engine": "python",
            "items": self._rank_search_items(items, normalized_query, root),
            "symbols": self._symbol_overview(root, normalized_query),
        }

    def lsp_diagnostics(self, workspace_id: str, *, run_id: str | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        root = self.workspace_service.draft_source_dir(workspace_id, run_id) if run_id else self.workspace_service.source_dir(workspace_id)
        items: list[dict[str, Any]] = []
        py_files = [path for path in root.rglob("*.py") if not self.workspace_service._is_ignored_workspace_path(path.relative_to(root))]
        for path in py_files[:120]:
            try:
                result = subprocess.run([sys.executable, "-m", "py_compile", str(path)], text=True, capture_output=True, timeout=4)
            except Exception as exc:
                items.append({"path": path.relative_to(root).as_posix(), "severity": "error", "message": str(exc), "source": "py_compile"})
                continue
            if result.returncode != 0:
                items.append({"path": path.relative_to(root).as_posix(), "severity": "error", "message": (result.stderr or result.stdout).strip()[:1200], "source": "py_compile"})
        if shutil.which("node"):
            for path in list(root.rglob("*.js"))[:80]:
                if self.workspace_service._is_ignored_workspace_path(path.relative_to(root)):
                    continue
                try:
                    result = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True, timeout=4)
                except Exception as exc:
                    items.append({"path": path.relative_to(root).as_posix(), "severity": "error", "message": str(exc), "source": "node --check"})
                    continue
                if result.returncode != 0:
                    items.append({"path": path.relative_to(root).as_posix(), "severity": "error", "message": (result.stderr or result.stdout).strip()[:1200], "source": "node --check"})
        return {"workspace_id": workspace_id, "run_id": run_id, "status": "passed" if not items else "failed", "items": items[:100], "symbols": self._symbol_overview(root, "")}

    def patch_preflight(self, workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ops = payload.get("ops") or payload.get("patch_actions") or []
        ops = [PatchOperationModel.model_validate(item) for item in raw_ops if isinstance(item, dict)]
        return self._patch_preflight(workspace_id, ops, payload)

    def _patch_preflight(self, workspace_id: str, ops: list[PatchOperationModel], payload: dict[str, Any]) -> dict[str, Any]:
        from app.services.patch_service import PatchService

        service = PatchService(self.workspace_service)
        return service.preflight(
            workspace_id=workspace_id,
            patch_actions=ops,
            run_id=payload.get("run_id"),
            base_revision_id=payload.get("base_revision_id"),
        )

    def _run_artifacts_or_empty(self, run_id: str) -> dict[str, Any]:
        try:
            return self.run_service.get_run_artifacts(run_id)
        except KeyError:
            self.run_service.get_run(run_id)
            return {}

    def approval_decision(self, run_id: str, approval_id: str, *, approved: bool) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        existing = self._approval_by_id(run_id, approval_id) or {
            "approval_id": approval_id,
            "kind": "manual",
            "summary": f"Manual approval {approval_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        item = {**existing, "status": "approved" if approved else "rejected", "decided_at": datetime.now(timezone.utc).isoformat()}
        self._upsert_approval(run_id, item)
        self.record_tool_event(run_id, tool_envelope(tool="approval.decision", input_payload={"approval_id": approval_id}, result=item, risk="safe"))
        return item

    def _upsert_approval(self, run_id: str, item: dict[str, Any]) -> None:
        key = f"approvals:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        items = [entry for entry in payload.get("items") or [] if isinstance(entry, dict)]
        replaced = False
        for index, entry in enumerate(items):
            if entry.get("approval_id") == item.get("approval_id"):
                items[index] = {**entry, **item}
                replaced = True
                break
        if not replaced:
            items.append(item)
        payload["items"] = items
        self.store.upsert("reports", key, payload)

    def _approval_by_id(self, run_id: str, approval_id: str) -> dict[str, Any] | None:
        for item in self.approvals(run_id).get("items") or []:
            if isinstance(item, dict) and item.get("approval_id") == approval_id:
                return item
        return None

    def _stored_tool_events(self, run_id: str) -> list[dict[str, Any]]:
        payload = self.store.get("reports", f"tool_events:{run_id}") or {}
        return [item for item in payload.get("items") or [] if isinstance(item, dict)]

    def _timeline_from_activity(self, event: dict[str, Any]) -> dict[str, Any]:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        kind = self._timeline_kind(str(event.get("type") or details.get("phase") or "tool"))
        return self._timeline_item(
            kind,
            str(event.get("status") or details.get("status") or "completed"),
            str(event.get("message") or details.get("summary") or details.get("reason") or kind),
            {
                "tool": event.get("tool") or details.get("tool"),
                "risk": details.get("risk"),
                "affected_files": details.get("targets") or details.get("changed_files") or [],
                "duration_ms": event.get("duration_ms") or event.get("elapsed_ms") or details.get("duration_ms"),
                "artifact_ref": event.get("artifact_ref") or details.get("artifact_ref"),
                "worker_id": event.get("worker_id") or details.get("worker_id"),
                "raw": event,
            },
            created_at=str(event.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _timeline_item(kind: str, status: str, title: str, payload: dict[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
        return {
            "kind": kind,
            "status": status,
            "title": title,
            "payload": payload,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _timeline_kind(raw: str) -> str:
        lowered = raw.lower()
        if "policy" in lowered:
            return "policy"
        if "approval" in lowered:
            return "approval"
        if "worker" in lowered:
            return "worker"
        if "check" in lowered:
            return "checks"
        if "browser" in lowered or "preview" in lowered:
            return "browser"
        if "patch" in lowered or "edit" in lowered or "write" in lowered:
            return "editing"
        if "read" in lowered or "search" in lowered:
            return "reading"
        if "apply" in lowered:
            return "apply"
        return "planning"

    @staticmethod
    def _worker_scope(worker_id: str) -> str:
        return {
            "backend_api": "Backend API and shared persistence",
            "client_ui": "Client role UI",
            "specialist_ui": "Specialist role UI",
            "manager_ui": "Manager role UI",
            "generated_tests": "Generated test coverage",
            "verifier": "Fresh verification and review",
        }.get(worker_id, worker_id)

    @staticmethod
    def _path_owned_by_worker(worker_id: str, path: str) -> bool:
        normalized = str(path or "").replace("\\", "/")
        if worker_id == "backend_api":
            return normalized.startswith("miniapp/app/") and "/static/" not in normalized
        if worker_id in {"client_ui", "specialist_ui", "manager_ui"}:
            role = worker_id.removesuffix("_ui")
            return f"/static/{role}/" in normalized or normalized.startswith(f"miniapp/app/static/{role}/")
        if worker_id == "generated_tests":
            return "test" in normalized
        return False

    @staticmethod
    def _worker_status(worker_id: str, run: Any, summaries: list[dict[str, Any]], merge_reports: list[dict[str, Any]]) -> str:
        if summaries or merge_reports:
            if any(str(item.get("status") or "") == "failed" for item in [*summaries, *merge_reports]):
                return "failed"
            return "completed"
        return "not_started" if run.status == "completed" else "pending"

    @staticmethod
    def _filter_diff(diff: str, paths: list[str]) -> str:
        if not paths:
            return ""
        active = False
        chunks: list[str] = []
        path_set = set(paths)
        for line in str(diff or "").splitlines():
            if line.startswith("diff --git "):
                active = any(path in line for path in path_set)
            if active:
                chunks.append(line)
        return "\n".join(chunks)

    @staticmethod
    def _paths_from_diff(diff: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff or "").splitlines():
            if not line.startswith("diff --git "):
                continue
            candidate = line.rsplit(" b/", 1)[-1].strip()
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            if candidate:
                paths.append(candidate)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _normalize_file_list(files: Any) -> list[str]:
        normalized: list[str] = []
        for item in files if isinstance(files, list) else []:
            path = str(item or "").strip().replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if not path or path.startswith("/") or ".." in Path(path).parts:
                raise ValueError("File paths must stay within the workspace.")
            normalized.append(path)
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _file_category(path: str) -> str:
        normalized = str(path or "").replace("\\", "/")
        if normalized.startswith("miniapp/app/generated/"):
            return "generated_manifest"
        if normalized.startswith("miniapp/app/static/client/"):
            return "client_ui"
        if normalized.startswith("miniapp/app/static/specialist/"):
            return "specialist_ui"
        if normalized.startswith("miniapp/app/static/manager/"):
            return "manager_ui"
        if "test" in normalized:
            return "tests"
        if normalized.endswith((".css", ".scss")):
            return "styles"
        if normalized.startswith("miniapp/app/"):
            return "backend"
        return "other"

    @staticmethod
    def _collect_values(payload: Any, *, keys: tuple[str, ...]) -> list[Any]:
        found: list[Any] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in keys:
                        if isinstance(nested, list):
                            found.extend(nested)
                        else:
                            found.append(nested)
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return found[:50]

    def _memory_stale_check(self, workspace_id: str, memory: dict[str, Any]) -> dict[str, Any]:
        try:
            source_dir = self.workspace_service.source_dir(workspace_id)
        except Exception:
            return {"status": "unknown", "items": []}
        items: list[dict[str, Any]] = []
        stale = False
        for item in memory.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "")
            paths = sorted(set(re.findall(r"\bminiapp/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b", text)))[:12]
            routes = sorted(set(re.findall(r"(?<![A-Za-z0-9_])/(?:client|specialist|manager|api)[A-Za-z0-9_./{}:-]*", text)))[:12]
            path_checks = [{"path": path, "exists": (source_dir / path).exists()} for path in paths]
            route_checks = [{"route": route, "present_in_source": self._text_exists_in_workspace(source_dir, route)} for route in routes]
            item_stale = any(not check["exists"] for check in path_checks) or (
                bool(route_checks) and not any(check["present_in_source"] for check in route_checks)
            )
            stale = stale or item_stale
            items.append(
                {
                    "memory_id": item.get("memory_id"),
                    "status": "stale" if item_stale else "fresh_or_unreferenced",
                    "paths": path_checks,
                    "routes": route_checks,
                }
            )
        return {"status": "stale" if stale else "fresh", "items": items}

    @staticmethod
    def _text_exists_in_workspace(source_dir: Path, needle: str) -> bool:
        if not needle:
            return False
        root = source_dir / "miniapp"
        if not root.exists():
            return False
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs", ".html", ".css", ".json"}:
                continue
            try:
                if needle in path.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                continue
        return False

    def _document_skills(self) -> list[dict[str, Any]]:
        roots = [self.settings.runtime_dir / "platform-docs", self.settings.template_dir / "docs"]
        skills: list[dict[str, Any]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.md"))[:80]:
                text = path.read_text(encoding="utf-8", errors="ignore")
                title = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
                skill_id = re_slug(path.relative_to(root).with_suffix("").as_posix())
                skills.append(
                    {
                        "id": skill_id,
                        "name": title[:80],
                        "source": str(path.relative_to(self.settings.repo_root)) if path.is_relative_to(self.settings.repo_root) else str(path),
                        "trigger_keywords": [part for part in re_slug(title).split("-") if len(part) > 2][:8],
                        "constraints": [line.strip("- ").strip() for line in text.splitlines() if line.strip().startswith("-")][:6],
                        "validation_hints": [],
                    }
                )
        return skills

    def _load_plugin_manifests(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        roots = [self.settings.runtime_dir / "plugins", self.settings.data_dir / "plugins"]
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("plugin.json")):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(manifest, dict) or not manifest.get("id") or not manifest.get("version"):
                    continue
                items.append({**manifest, "status": "installed", "source": str(path)})
        for key, payload in self.store.items("reports"):
            if key.startswith("plugin:") and isinstance(payload, dict):
                manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else payload
                items.append({**manifest, "status": payload.get("status", "registered"), "source": "state"})
        return items

    def _mcp_config(self) -> dict[str, Any]:
        candidates = [self.settings.data_dir / "mcp.json", self.settings.repo_root / "mcp.json"]
        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return {"servers": [], "tools": []}

    @staticmethod
    def _ripgrep_search(root: Path, query: str) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                ["rg", "--line-number", "--no-heading", "--color", "never", "--", query, str(root)],
                text=True,
                capture_output=True,
                timeout=6,
            )
        except Exception:
            return []
        if result.returncode not in {0, 1}:
            return []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for raw_line in result.stdout.splitlines()[:400]:
            parts = raw_line.split(":", 2)
            if len(parts) != 3:
                continue
            path_text, line_text, snippet = parts
            try:
                relative_path = Path(path_text).resolve().relative_to(root.resolve()).as_posix()
            except Exception:
                continue
            if ".." in Path(relative_path).parts:
                continue
            try:
                line_number = int(line_text)
            except ValueError:
                line_number = 0
            grouped.setdefault(relative_path, []).append({"line": line_number, "text": snippet[:240]})
        return [{"path": path, "hits": hits[:5]} for path, hits in list(grouped.items())[:80]]

    @staticmethod
    def _rank_search_items(items: list[dict[str, Any]], query: str, root: Path) -> list[dict[str, Any]]:
        query_lower = query.lower()
        ranked: list[dict[str, Any]] = []
        for item in items:
            path = str(item.get("path") or "")
            hits = item.get("hits") if isinstance(item.get("hits"), list) else []
            score = len(hits) * 10
            if query_lower in path.lower():
                score += 25
            if path.endswith((".py", ".js", ".ts", ".tsx", ".html", ".css")):
                score += 3
            ranked.append({**item, "score": score, "language": WorkbenchService._language_for_path(path), "symbols": WorkbenchService._symbols_for_file(root / path)[:12]})
        return sorted(ranked, key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))[:80]

    @staticmethod
    def _symbol_overview(root: Path, query: str) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        query_lower = query.lower()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".ts", ".tsx"}:
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(part in {".git", "node_modules", "dist", "build", "__pycache__"} for part in Path(relative).parts):
                continue
            for symbol in WorkbenchService._symbols_for_file(path):
                if query_lower and query_lower not in symbol["name"].lower() and query_lower not in relative.lower():
                    continue
                symbols.append({"path": relative, **symbol})
                if len(symbols) >= 100:
                    return symbols
        return symbols

    @staticmethod
    def _symbols_for_file(path: Path) -> list[dict[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        patterns = [
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)", re.M)),
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z0-9_$]+)", re.M)),
            ("const", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=", re.M)),
            ("python_function", re.compile(r"^\s*def\s+([A-Za-z0-9_]+)\s*\(", re.M)),
            ("python_class", re.compile(r"^\s*class\s+([A-Za-z0-9_]+)\s*[:(]", re.M)),
        ]
        symbols: list[dict[str, Any]] = []
        line_starts = [0]
        for match in re.finditer("\n", text):
            line_starts.append(match.end())
        for kind, pattern in patterns:
            for match in pattern.finditer(text):
                line = 1
                for index, start in enumerate(line_starts):
                    if start > match.start():
                        break
                    line = index + 1
                symbols.append({"kind": kind, "name": match.group(1), "line": line})
                if len(symbols) >= 50:
                    return symbols
        return symbols

    @staticmethod
    def _language_for_path(path: str) -> str:
        suffix = Path(path).suffix.lower()
        return {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".html": "html",
            ".css": "css",
        }.get(suffix, "text")

    @staticmethod
    def _trace_why(run: RunRecord, artifacts: dict[str, Any]) -> str:
        if run.failure_reason:
            return f"Run is focused on resolving: {run.failure_reason}"
        if run.summary:
            return run.summary
        plan = artifacts.get("implementation_plan") or run.implementation_plan or {}
        if isinstance(plan, dict) and plan.get("summary"):
            return str(plan["summary"])
        return run.prompt[:500]

    def _check(self, name: str, ok: bool, details: str = "", command: str | None = None, *, required: bool = True) -> dict[str, Any]:
        return {"name": name, "status": "passed" if ok else "failed", "details": details, "command": command, "required": required}

    def _binary_check(self, binary: str) -> dict[str, Any]:
        path = shutil.which(binary)
        return self._check(binary, bool(path), path or f"{binary} not found", binary, required=binary in {"node", "npm"})

    def _compose_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("docker_compose", False, "docker not found", "docker compose version", required=False)
        try:
            result = subprocess.run([docker, "compose", "version"], text=True, capture_output=True, timeout=5)
            return self._check("docker_compose", result.returncode == 0, (result.stdout or result.stderr).strip(), "docker compose version", required=False)
        except Exception as exc:
            return self._check("docker_compose", False, str(exc), "docker compose version", required=False)

    def _playwright_check(self) -> dict[str, Any]:
        try:
            import playwright  # noqa: F401

            return self._check("playwright", True, "Python package import succeeded", "python -c 'import playwright'", required=False)
        except Exception as exc:
            return self._check("playwright", False, str(exc), "python -c 'import playwright'", required=False)

    def _openai_check(self) -> dict[str, Any]:
        config = self.openai_client.configuration()
        return self._check("openai", bool(config.get("enabled")), "configured" if config.get("enabled") else "not configured", required=False)

    def _writable_check(self, name: str, path: Path) -> dict[str, Any]:
        return self._check(name, os.access(path, os.W_OK), str(path), required=True)

    def _template_check(self) -> dict[str, Any]:
        required = [self.settings.template_dir / "miniapp" / "app" / "main.py", self.settings.template_dir / "docker" / "docker-compose.yml"]
        missing = [str(path) for path in required if not path.exists()]
        return self._check("template_integrity", not missing, "missing: " + ", ".join(missing) if missing else str(self.settings.template_dir), required=True)

    def _port_check(self) -> dict[str, Any]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = sock.connect_ex(("127.0.0.1", int(self.settings.preview_port_base)))
            return self._check("preview_port_base", result != 0, f"port {self.settings.preview_port_base} {'available' if result != 0 else 'in use'}", required=False)
        finally:
            sock.close()

    def _backend_routes_check(self) -> dict[str, Any]:
        route_file = self.settings.repo_root / "platform" / "backend" / "app" / "api" / "routes_workbench.py"
        if not route_file.exists():
            return self._check("backend_routes", False, f"missing {route_file}", required=True)
        text = route_file.read_text(encoding="utf-8", errors="ignore")
        required_routes = ["/doctor", "/runs/{run_id}/timeline", "/runs/{run_id}/approvals", "/workspaces/{workspace_id}/files/search"]
        missing = [route for route in required_routes if route not in text]
        return self._check(
            "backend_routes",
            not missing,
            "registered" if not missing else "missing: " + ", ".join(missing),
            required=True,
        )

    def _stale_backend_check(self) -> dict[str, Any]:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=1.0)
            conn.request("GET", "/doctor")
            response = conn.getresponse()
            body = response.read(240).decode("utf-8", errors="ignore")
            conn.close()
            ok = response.status < 500
            details = f"127.0.0.1:8000 returned {response.status}; {body[:120]}"
            return self._check("stale_backend_port_8000", ok, details, "GET http://127.0.0.1:8000/doctor", required=False)
        except Exception as exc:
            return self._check("stale_backend_port_8000", True, f"no conflicting backend detected ({exc})", required=False)

    def _playwright_browsers_check(self) -> dict[str, Any]:
        try:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run"], text=True, capture_output=True, timeout=10)
            output = (result.stdout or result.stderr).strip()
            return self._check(
                "playwright_browsers",
                result.returncode == 0,
                output[:600] or "dry run completed",
                "python -m playwright install --dry-run",
                required=False,
            )
        except Exception as exc:
            return self._check("playwright_browsers", False, str(exc), "python -m playwright install --dry-run", required=False)

    def _preview_container_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("preview_containers", True, "docker not available; skipped", "docker ps", required=False)
        try:
            result = subprocess.run([docker, "ps", "--format", "{{.Names}}"], text=True, capture_output=True, timeout=5)
            names = [line for line in result.stdout.splitlines() if "grounded" in line or "miniapp" in line or "preview" in line]
            return self._check(
                "preview_containers",
                result.returncode == 0,
                ", ".join(names[:12]) if names else "no matching preview containers",
                "docker ps --format '{{.Names}}'",
                required=False,
            )
        except Exception as exc:
            return self._check("preview_containers", False, str(exc), "docker ps --format '{{.Names}}'", required=False)

    def _test_command_check(self) -> dict[str, Any]:
        return self._check("platform_tests", (self.settings.repo_root / "platform" / "backend" / "tests").exists(), "pytest platform/backend/tests", required=True)

    @staticmethod
    def _builtin_skills() -> dict[str, dict[str, Any]]:
        return {
            "state-workflow": {"id": "state-workflow", "name": "State workflow", "trigger_keywords": ["state", "status", "workflow"], "constraints": ["Persist shared records and status transitions."], "validation_hints": ["Create/update/list workflow exists."]},
            "role-surfaces": {"id": "role-surfaces", "name": "Role surfaces", "trigger_keywords": ["client", "specialist", "manager"], "constraints": ["Role pages share connected state."], "validation_hints": ["Each role has a distinct action surface."]},
            "route-manifest": {"id": "route-manifest", "name": "Route manifest", "trigger_keywords": ["route", "manifest", "page"], "constraints": ["Route manifest matches static pages."], "validation_hints": ["Every role page is routeable."]},
            "mobile-shell": {"id": "mobile-shell", "name": "Mobile shell", "trigger_keywords": ["mobile", "telegram", "mini app"], "constraints": ["Mobile-first role pages."], "validation_hints": ["No horizontal overflow on mobile."]},
            "preview-profile": {"id": "preview-profile", "name": "Preview profile", "trigger_keywords": ["preview", "max", "telegram"], "constraints": ["Preview profile selects the right host shell."], "validation_hints": ["Preview profile supports configured mock surface."]},
        }
