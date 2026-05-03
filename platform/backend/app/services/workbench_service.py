from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from typing import Any
from uuid import uuid4

from app.repositories.state_store import StateStore
from app.services.exec_policy_service import ExecPolicyService
from app.services.tool_protocol import TOOL_PROTOCOL_VERSION, tool_envelope, tool_registry_contract
from app.services.workspace.run_service import RunService
from app.services.workspace.service import WorkspaceService
from app.core.config import Settings
from app.ai.openai_client import OpenAIClient


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
            "items": [{**item, "sequence": index + 1} for index, item in enumerate(items)],
        }

    def observability(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        return {
            "trace_id": f"trace_{run.run_id}",
            "run_id": run.run_id,
            "thread_id": None,
            "turn_id": None,
            "tool_call_count": len((self.tool_events(run_id)).get("events") or []),
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

    def workers(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        merge = ((artifacts.get("worker_results") or {}).get("merge") or {}) if isinstance(artifacts.get("worker_results"), dict) else {}
        worker_ids = ["backend_api", "client_ui", "specialist_ui", "manager_ui", "generated_tests", "verifier"]
        lanes = []
        for worker_id in worker_ids:
            summaries = [item for item in run.worker_summaries if isinstance(item, dict) and item.get("worker") == worker_id or item.get("worker_id") == worker_id]
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

    def browser_proof(self, run_id: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        payload = {
            "run_id": run_id,
            "status": "available" if (run.browser_flow_proof or artifacts.get("browser_proof_steps")) else "not_recorded",
            "screenshots": [],
            "console_errors": [],
            "network_errors": [],
            "route_coverage": (run.flow_coverage or {}).get("routes", []),
            "mobile_layout": run.mobile_layout_report,
            "role_workflows": run.browser_flow_proof,
            "steps": artifacts.get("browser_proof_steps") or [],
        }
        self.store.upsert("reports", f"browser_proof:{run_id}", payload)
        return payload

    def memory(self, workspace_id: str) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        return self.store.get("reports", f"workspace_memory:{workspace_id}") or {
            "workspace_id": workspace_id,
            "items": [],
            "project_rules": [],
            "user_preferences": [],
            "platform_constraints": [],
            "repeated_fixes": [],
        }

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
        self.store.upsert("reports", f"workspace_memory:{workspace_id}", current)
        return current

    def skills(self) -> dict[str, Any]:
        return {"items": list(self._builtin_skills().values())}

    def skill(self, skill_id: str) -> dict[str, Any]:
        item = self._builtin_skills().get(skill_id)
        if item is None:
            raise KeyError(f"Skill not found: {skill_id}")
        return item

    def plugins(self) -> dict[str, Any]:
        return {
            "items": [
                {"id": "core.validators", "version": "0.1.0", "capabilities": ["validators"], "status": "installed"},
                {"id": "core.exporters", "version": "0.1.0", "capabilities": ["exporters"], "status": "installed"},
                {"id": "core.preview", "version": "0.1.0", "capabilities": ["preview_adapters"], "status": "installed"},
            ]
        }

    def install_plugin_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        manifest = dict(payload or {})
        plugin_id = str(manifest.get("id") or "").strip()
        if not plugin_id:
            raise ValueError("Plugin manifest id is required.")
        record = {"id": plugin_id, "status": "registered", "manifest": manifest, "installed_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"plugin:{plugin_id}", record)
        return record

    def mcp_servers(self) -> dict[str, Any]:
        return {"items": [], "status": "not_configured", "message": "MCP registry endpoint is ready; server discovery is not configured in this runtime."}

    def mcp_tools(self) -> dict[str, Any]:
        return {"items": [], "tool_protocol": tool_registry_contract()}

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
        }

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
        self.run_service.get_run(run_id)
        record = {"run_id": run_id, "files": list(payload.get("files") or []), "status": "staged", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"staged_files:{run_id}", record)
        return record

    def discard_files(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        record = {"run_id": run_id, "files": list(payload.get("files") or []), "status": "discard_requested", "updated_at": datetime.now(timezone.utc).isoformat()}
        self.store.upsert("reports", f"discarded_files:{run_id}", record)
        return record

    def apply_staged(self, run_id: str) -> Any:
        return self.run_service.apply_run(run_id)

    def diff(self, run_id: str, *, base: str, target: str) -> dict[str, Any]:
        run = self.run_service.get_run(run_id)
        artifacts = self._run_artifacts_or_empty(run_id)
        diff = artifacts.get("diff") or self.workspace_service.diff(run.workspace_id, run_id=run_id)
        return {"run_id": run_id, "base": base, "target": target, "diff": diff, "files": run.touched_files}

    def _run_artifacts_or_empty(self, run_id: str) -> dict[str, Any]:
        try:
            return self.run_service.get_run_artifacts(run_id)
        except KeyError:
            self.run_service.get_run(run_id)
            return {}

    def approval_decision(self, run_id: str, approval_id: str, *, approved: bool) -> dict[str, Any]:
        self.run_service.get_run(run_id)
        key = f"approvals:{run_id}"
        payload = self.store.get("reports", key) or {"run_id": run_id, "items": []}
        item = {"approval_id": approval_id, "status": "approved" if approved else "rejected", "decided_at": datetime.now(timezone.utc).isoformat()}
        payload.setdefault("items", []).append(item)
        self.store.upsert("reports", key, payload)
        return item

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

    def _test_command_check(self) -> dict[str, Any]:
        return self._check("platform_tests", (self.settings.repo_root / "platform" / "backend" / "tests").exists(), "pytest platform/backend/tests", required=True)

    @staticmethod
    def _builtin_skills() -> dict[str, dict[str, Any]]:
        return {
            "crm": {"id": "crm", "name": "CRM", "trigger_keywords": ["crm", "lead", "pipeline"], "constraints": ["Persist customer records and status transitions."], "validation_hints": ["Create/update/list workflow exists."]},
            "reservation": {"id": "reservation", "name": "Reservation", "trigger_keywords": ["reservation", "schedule", "slot"], "constraints": ["Persist reserved slots and status."], "validation_hints": ["Client creates request; staff confirms."]},
            "marketplace": {"id": "marketplace", "name": "Marketplace", "trigger_keywords": ["marketplace", "catalog", "order"], "constraints": ["Catalog and order state must be connected."], "validation_hints": ["Role pages expose different order actions."]},
            "internal-dashboard": {"id": "internal-dashboard", "name": "Internal dashboard", "trigger_keywords": ["dashboard", "admin", "ops"], "constraints": ["Dense operational UI over marketing layout."], "validation_hints": ["Filters, statuses, and review actions exist."]},
            "telegram-miniapp": {"id": "telegram-miniapp", "name": "Telegram miniapp", "trigger_keywords": ["telegram", "mini app"], "constraints": ["Mobile-first role pages."], "validation_hints": ["No horizontal overflow on mobile."]},
            "max-miniapp": {"id": "max-miniapp", "name": "Max miniapp", "trigger_keywords": ["max", "mini app"], "constraints": ["Mobile-first role pages."], "validation_hints": ["Preview profile supports max_mock."]},
        }
