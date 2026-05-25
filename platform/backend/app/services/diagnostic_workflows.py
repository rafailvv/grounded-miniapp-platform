from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.modules.miniapp_agent_loop.agent_worker_manager import AgentWorkerManager


class DiagnosticWorkflow:
    """Debug/stuck/doctor workflows that end in a concrete repair packet."""

    SCHEMA = "grounded.diagnostic_workflow.v1"

    @classmethod
    def debug_run(
        cls,
        *,
        run: Any,
        workspace_root: Path,
        artifacts: dict[str, Any],
        gate: dict[str, Any] | None,
        trace_state: dict[str, Any] | None,
        trace_reducer: dict[str, Any] | None,
        preview: dict[str, Any] | None,
        process_outputs: dict[str, Any] | None,
        platform_logs: list[str],
        api_logs: list[str],
    ) -> dict[str, Any]:
        evidence = cls._evidence(
            run=run,
            artifacts=artifacts,
            gate=gate,
            trace_state=trace_state,
            trace_reducer=trace_reducer,
            preview=preview,
            process_outputs=process_outputs,
            platform_logs=platform_logs,
            api_logs=api_logs,
        )
        diagnosis = cls._diagnose(run=run, evidence=evidence, mode="debug_run")
        packet = cls._repair_packet(run=run, diagnosis=diagnosis, evidence=evidence)
        return cls._report(run=run, workspace_root=workspace_root, mode="debug_run", evidence=evidence, diagnosis=diagnosis, repair_packet=packet)

    @classmethod
    def stuck_run(
        cls,
        *,
        run: Any,
        workspace_root: Path,
        artifacts: dict[str, Any],
        gate: dict[str, Any] | None,
        trace_state: dict[str, Any] | None,
        trace_reducer: dict[str, Any] | None,
        preview: dict[str, Any] | None,
        process_outputs: dict[str, Any] | None,
        platform_logs: list[str],
        api_logs: list[str],
    ) -> dict[str, Any]:
        evidence = cls._evidence(
            run=run,
            artifacts=artifacts,
            gate=gate,
            trace_state=trace_state,
            trace_reducer=trace_reducer,
            preview=preview,
            process_outputs=process_outputs,
            platform_logs=platform_logs,
            api_logs=api_logs,
        )
        diagnosis = cls._diagnose(run=run, evidence=evidence, mode="stuck_run")
        packet = cls._repair_packet(run=run, diagnosis={**diagnosis, "stuck": cls._stuck_reason(run, evidence)}, evidence=evidence)
        return cls._report(run=run, workspace_root=workspace_root, mode="stuck_run", evidence=evidence, diagnosis={**diagnosis, "stuck": cls._stuck_reason(run, evidence)}, repair_packet=packet)

    @classmethod
    def doctor_workspace(
        cls,
        *,
        workspace_id: str,
        workspace_root: Path,
        latest_run: Any | None,
        preview: dict[str, Any] | None,
        platform_logs: list[str],
        api_logs: list[str],
        reports: dict[str, Any],
    ) -> dict[str, Any]:
        run = latest_run
        evidence = {
            "preview": cls._preview_evidence(preview),
            "platform_logs": platform_logs[-80:],
            "api_logs": api_logs[-120:],
            "reports": {key: cls._compact(value) for key, value in reports.items() if value},
            "log_signals": cls._log_signals([*platform_logs[-120:], *api_logs[-160:], *cls._preview_lines(preview)]),
        }
        diagnosis = cls._workspace_diagnosis(workspace_id=workspace_id, run=run, evidence=evidence)
        packet = cls._workspace_repair_packet(workspace_id=workspace_id, run=run, diagnosis=diagnosis, evidence=evidence)
        return {
            "schema": cls.SCHEMA,
            "mode": "doctor_workspace",
            "workspace_id": workspace_id,
            "run_id": getattr(run, "run_id", None),
            "status": "needs_repair" if diagnosis.get("blocking") else "ok",
            "workspace_root": str(workspace_root),
            "evidence": evidence,
            "diagnosis": diagnosis,
            "repair_packet": packet,
        }

    @staticmethod
    def _evidence(
        *,
        run: Any,
        artifacts: dict[str, Any],
        gate: dict[str, Any] | None,
        trace_state: dict[str, Any] | None,
        trace_reducer: dict[str, Any] | None,
        preview: dict[str, Any] | None,
        process_outputs: dict[str, Any] | None,
        platform_logs: list[str],
        api_logs: list[str],
    ) -> dict[str, Any]:
        check_results = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        failed_checks = [item for item in check_results if str(item.get("status") or "").lower() in {"failed", "blocked", "error"}]
        process_items = [item for item in (process_outputs or {}).get("items") or [] if isinstance(item, dict)]
        lines = [
            *[str(item.get("details") or "") for item in failed_checks],
            *[line for item in failed_checks for line in (item.get("logs") or []) if isinstance(line, str)],
            *[str(item.get("stderr_tail") or item.get("stdout_tail") or "") for item in process_items],
            *platform_logs[-80:],
            *api_logs[-120:],
            *DiagnosticWorkflow._preview_lines(preview),
        ]
        return {
            "run": {
                "status": run.status,
                "apply_status": run.apply_status,
                "current_stage": run.current_stage,
                "failure_class": run.failure_class,
                "failure_signature": run.failure_signature,
                "failure_reason": run.failure_reason,
            },
            "checks": {"items": check_results[-40:], "failed": failed_checks[-20:]},
            "gate": cls_compact(gate or {}),
            "trace": {
                "state_next_action": (trace_state or {}).get("next_action") if isinstance(trace_state, dict) else {},
                "blockers": list((trace_state or {}).get("blockers") or [])[-20:] if isinstance(trace_state, dict) else [],
                "reducer_next_action": (trace_reducer or {}).get("next_action") if isinstance(trace_reducer, dict) else {},
                "last_failed_attempt": (trace_reducer or {}).get("last_failed_attempt") if isinstance(trace_reducer, dict) else {},
            },
            "preview": DiagnosticWorkflow._preview_evidence(preview),
            "process_outputs": process_items[-40:],
            "platform_logs": platform_logs[-80:],
            "api_logs": api_logs[-120:],
            "log_signals": DiagnosticWorkflow._log_signals(lines),
        }

    @staticmethod
    def _diagnose(*, run: Any, evidence: dict[str, Any], mode: str) -> dict[str, Any]:
        failed_checks = evidence.get("checks", {}).get("failed") or []
        preview = evidence.get("preview") or {}
        signals = evidence.get("log_signals") or {}
        if failed_checks:
            first = failed_checks[-1]
            check = str(first.get("name") or first.get("check") or "check")
            return {
                "blocking": True,
                "failure_area": DiagnosticWorkflow._area_from_check(check),
                "primary_signal": f"{check} failed",
                "details": str(first.get("details") or first.get("summary") or "Check failed."),
                "target_files": DiagnosticWorkflow._target_files(evidence, fallback=list(getattr(run, "touched_files", []) or [])),
                "required_next_tool": "read_files",
                "suggested_tool_after_read": "run_checks",
                "mode": mode,
            }
        if preview.get("status") == "error" or preview.get("last_error"):
            return {
                "blocking": True,
                "failure_area": "preview",
                "primary_signal": "Preview runtime is failing",
                "details": str(preview.get("last_error") or "Preview status is error."),
                "target_files": DiagnosticWorkflow._target_files(evidence, fallback=["miniapp/app/main.py"]),
                "required_next_tool": "read_files",
                "suggested_tool_after_read": "run_checks",
                "mode": mode,
            }
        if signals.get("error_count"):
            return {
                "blocking": True,
                "failure_area": "runtime_logs",
                "primary_signal": "Runtime logs contain errors",
                "details": signals.get("top_error") or "Logs contain error markers.",
                "target_files": DiagnosticWorkflow._target_files(evidence, fallback=list(getattr(run, "touched_files", []) or [])),
                "required_next_tool": "read_files",
                "suggested_tool_after_read": "run_checks",
                "mode": mode,
            }
        return {
            "blocking": bool(run.status in {"failed", "blocked"}),
            "failure_area": str(run.failure_class or "unknown"),
            "primary_signal": str(run.failure_reason or run.summary or "No explicit failing evidence found."),
            "details": str(run.failure_reason or ""),
            "target_files": DiagnosticWorkflow._target_files(evidence, fallback=list(getattr(run, "touched_files", []) or [])),
            "required_next_tool": "inspect_trace",
            "suggested_tool_after_read": "run_checks",
            "mode": mode,
        }

    @staticmethod
    def _repair_packet(*, run: Any, diagnosis: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        targets = [str(path) for path in diagnosis.get("target_files") or [] if str(path).strip()][:12]
        owner = AgentWorkerManager.owner_for_path(targets[0]) if targets else "repair_worker"
        signature = str(run.failure_signature or diagnosis.get("failure_area") or diagnosis.get("primary_signal") or "debug_run")
        return {
            "schema": "grounded.repair_packet.v2",
            "source": diagnosis.get("mode") or "debug_run",
            "failure_class": str(run.failure_class or diagnosis.get("failure_area") or "debug_run"),
            "failure_signature": signature[:240],
            "issue_code": str(diagnosis.get("failure_area") or "debug_run"),
            "severity": "high" if diagnosis.get("blocking") else "medium",
            "summary": diagnosis.get("primary_signal"),
            "instruction": f"{diagnosis.get('details') or diagnosis.get('primary_signal')} Read the target files first, patch only the owner scope, then run the suggested checks.",
            "target_files": targets,
            "owner": owner,
            "required_next_tool": diagnosis.get("required_next_tool") or "read_files",
            "suggested_tool_after_read": diagnosis.get("suggested_tool_after_read") or "run_checks",
            "proof_required": ["latest failed check passes", "preview/API logs no longer show the primary error"],
            "evidence": {
                "checks": evidence.get("checks", {}).get("failed") or [],
                "preview": evidence.get("preview") or {},
                "trace": evidence.get("trace") or {},
                "log_signals": evidence.get("log_signals") or {},
            },
        }

    @staticmethod
    def _report(*, run: Any, workspace_root: Path, mode: str, evidence: dict[str, Any], diagnosis: dict[str, Any], repair_packet: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": DiagnosticWorkflow.SCHEMA,
            "mode": mode,
            "workspace_id": run.workspace_id,
            "run_id": run.run_id,
            "status": "needs_repair" if diagnosis.get("blocking") else "diagnosed",
            "workspace_root": str(workspace_root),
            "evidence": evidence,
            "diagnosis": diagnosis,
            "repair_packet": repair_packet,
        }

    @staticmethod
    def _workspace_diagnosis(*, workspace_id: str, run: Any | None, evidence: dict[str, Any]) -> dict[str, Any]:
        preview = evidence.get("preview") or {}
        signals = evidence.get("log_signals") or {}
        blocking = preview.get("status") == "error" or bool(preview.get("last_error")) or bool(signals.get("error_count"))
        return {
            "blocking": blocking,
            "failure_area": "workspace_preview" if preview.get("last_error") else "workspace_logs",
            "primary_signal": preview.get("last_error") or signals.get("top_error") or "Workspace doctor found no blocking runtime signal.",
            "target_files": DiagnosticWorkflow._target_files(evidence, fallback=["miniapp/app/main.py"]),
            "run_status": getattr(run, "status", None),
        }

    @staticmethod
    def _workspace_repair_packet(*, workspace_id: str, run: Any | None, diagnosis: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        fake_run = type("RunLike", (), {"failure_signature": None, "failure_class": None})()
        packet = DiagnosticWorkflow._repair_packet(run=fake_run, diagnosis={**diagnosis, "mode": "doctor_workspace"}, evidence=evidence)
        packet["workspace_id"] = workspace_id
        packet["run_id"] = getattr(run, "run_id", None)
        return packet

    @staticmethod
    def _preview_evidence(preview: dict[str, Any] | None) -> dict[str, Any]:
        preview = preview if isinstance(preview, dict) else {}
        return {
            "status": preview.get("status"),
            "stage": preview.get("stage"),
            "runtime_mode": preview.get("runtime_mode"),
            "last_error": preview.get("last_error"),
            "failure_kind": preview.get("preview_failure_kind"),
            "logs": list(preview.get("logs") or [])[-80:],
        }

    @staticmethod
    def _preview_lines(preview: dict[str, Any] | None) -> list[str]:
        preview = preview if isinstance(preview, dict) else {}
        return [str(preview.get("last_error") or ""), *[str(line) for line in list(preview.get("logs") or [])[-80:]]]

    @staticmethod
    def _log_signals(lines: list[str]) -> dict[str, Any]:
        markers = ("error", "exception", "traceback", "failed", "timeout", "refused", "not found", "syntaxerror", "nameerror")
        hits = [line for line in lines if any(marker in str(line).lower() for marker in markers)]
        paths = []
        for line in hits:
            paths.extend(re.findall(r"miniapp/[A-Za-z0-9_./-]+\.(?:py|js|mjs|html|css|json)", str(line)))
        return {"error_count": len(hits), "top_error": hits[-1] if hits else "", "referenced_paths": list(dict.fromkeys(paths))[:20]}

    @staticmethod
    def _target_files(evidence: dict[str, Any], *, fallback: list[str]) -> list[str]:
        signals = evidence.get("log_signals") or {}
        paths = [str(path) for path in signals.get("referenced_paths") or [] if str(path).strip()]
        for check in (evidence.get("checks") or {}).get("failed") or []:
            if isinstance(check, dict):
                for key in ("target_files", "changed_files", "paths"):
                    value = check.get(key)
                    if isinstance(value, list):
                        paths.extend(str(path) for path in value if str(path).strip())
                diagnostics = check.get("diagnostics") if isinstance(check.get("diagnostics"), dict) else {}
                for value in diagnostics.values():
                    if isinstance(value, str) and value.startswith("miniapp/"):
                        paths.append(value)
                    elif isinstance(value, list):
                        paths.extend(str(path) for path in value if str(path).startswith("miniapp/"))
        paths.extend(str(path) for path in fallback if str(path).strip())
        return [path for path in dict.fromkeys(paths) if path.startswith("miniapp/")][:12]

    @staticmethod
    def _area_from_check(check: str) -> str:
        lowered = check.lower()
        if "browser" in lowered:
            return "browser_flow"
        if "api" in lowered:
            return "api"
        if "preview" in lowered:
            return "preview"
        if "js" in lowered or "frontend" in lowered:
            return "frontend"
        if "python" in lowered or "backend" in lowered:
            return "backend"
        return "checks"

    @staticmethod
    def _stuck_reason(run: Any, evidence: dict[str, Any]) -> dict[str, Any]:
        if run.status in {"running", "queued"}:
            return {"kind": "active_not_terminal", "stage": run.current_stage}
        if evidence.get("trace", {}).get("blockers"):
            return {"kind": "trace_blocker", "latest": evidence["trace"]["blockers"][-1]}
        if evidence.get("log_signals", {}).get("error_count"):
            return {"kind": "log_error_loop", "top_error": evidence["log_signals"].get("top_error")}
        return {"kind": "unknown_or_waiting", "stage": run.current_stage}

    @staticmethod
    def _compact(value: Any) -> Any:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)[:4000]
        if len(text) <= 4000:
            return value
        return {"truncated": True, "chars": len(text), "excerpt": text[:4000]}


def cls_compact(value: Any) -> Any:
    return DiagnosticWorkflow._compact(value)
