from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.models.common import GenerationMode
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext


class FixPromptBuilder:
    @staticmethod
    def read_only_surfaces() -> list[str]:
        return [
            "miniapp/tests/",
            "miniapp/app/generated/",
            "artifacts/generated_app_graph.json",
        ]

    @classmethod
    def expected_contract_snapshot(cls, fix_turn: FixTurnContext) -> dict[str, Any]:
        evidence = "\n".join(
            [
                str(fix_turn.root_cause_summary or ""),
                str(fix_turn.exact_error_excerpt or ""),
                *[
                    " ".join(
                        str(item.get(key) or "")
                        for key in ("method", "path", "status_code", "resource_slug")
                    ).strip()
                    for item in fix_turn.api_failure_diagnostics
                ],
                *[item.details or "" for item in fix_turn.executed_checks],
                *[line for item in fix_turn.executed_checks for line in item.logs],
            ]
        )
        required_api_routes = sorted({endpoint for endpoint in re.findall(r"/api/([a-zA-Z0-9_-]+)", evidence) if endpoint})
        required_role_routes = sorted(
            {
                route
                for route in re.findall(r"(/(?:client|specialist|manager)(?:/[A-Za-z0-9_{}:-]+)*/?)", evidence)
                if route
            }
        )
        required_exports: list[str] = []
        if "get_db" in evidence:
            required_exports.append("get_db")
        return {
            "strict_green": True,
            "required_api_routes": required_api_routes,
            "required_role_routes": required_role_routes,
            "required_exports": required_exports,
            "read_only_surfaces": cls.read_only_surfaces(),
        }

    @staticmethod
    def repair_context_mode(fix_turn: FixTurnContext, repeated_signature_without_progress: int) -> str:
        route_runtime_failure = str(fix_turn.failure_class or "") in {
            "backend_framework_mismatch",
            "runtime_manifest_route_missing",
            "router_not_registered",
            "api_endpoint_missing",
            "frontend_link_route_mismatch",
            "db_dependency_export_missing",
            "loading_first_root_surface",
        }
        if fix_turn.generation_mode == GenerationMode.QUALITY:
            if repeated_signature_without_progress >= 1 or route_runtime_failure:
                return "full_bundle"
            return "expanded"
        if repeated_signature_without_progress >= 2:
            return "full_bundle"
        if repeated_signature_without_progress >= 1 or route_runtime_failure:
            return "expanded"
        return "minimal"

    @staticmethod
    def needs_full_context_first(fix_turn: FixTurnContext) -> bool:
        return str(fix_turn.failure_class or "") in {
            "backend_framework_mismatch",
            "runtime_manifest_route_missing",
            "router_not_registered",
            "api_endpoint_missing",
            "frontend_link_route_mismatch",
            "db_dependency_export_missing",
            "loading_first_root_surface",
        }

    @staticmethod
    def previous_attempt_summary(fix_turn: FixTurnContext) -> str | None:
        history = list(fix_turn.attempt_history or [])
        if not history:
            return None
        tail = history[-2:]
        fragments: list[str] = []
        for item in tail:
            attempt = item.get("attempt") if isinstance(item, dict) else getattr(item, "attempt", None)
            result = item.get("result") if isinstance(item, dict) else getattr(item, "result", None)
            diagnosis = str(item.get("diagnosis") if isinstance(item, dict) else getattr(item, "diagnosis", "") or "").strip()
            files_changed = item.get("files_changed") if isinstance(item, dict) else getattr(item, "files_changed", [])
            changed = len(files_changed or [])
            parts = [f"attempt={attempt}", f"result={result}", f"changed_files={changed}"]
            if diagnosis:
                parts.append(f"diagnosis={diagnosis}")
            fragments.append(", ".join(parts))
        return " | ".join(fragments) if fragments else None

    @staticmethod
    def normalized_critical_issues(results, failure_class: str | None = None) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for result in results:
            if result.status != "failed":
                continue
            marker = (result.name, str(result.details or ""), str(result.command or ""))
            if marker in seen:
                continue
            seen.add(marker)
            issues.append(
                {
                    "check": result.name,
                    "details": result.details,
                    "command": result.command,
                    "failure_class": failure_class,
                    "diagnostics": dict(result.diagnostics or {}),
                }
            )
        return issues[:16]

    @staticmethod
    def repair_system_prompt() -> str:
        return (
            "You are a focused software repair agent. "
            "Diagnose the current failure packet, patch only the generated app files justified by the evidence, "
            "keep the diff minimal, and aim for strict-green validation: validators, generated tests, and preview runtime all passing. "
            "Do not redesign the app. Fix the current root-cause cluster only. "
            "Preserve the existing backend architecture, routers, and static mounting unless the evidence explicitly implicates them. "
            "Never replace a functioning FastAPI backend or route module with placeholder HTML handlers, stub pages, or a simplified demo app. "
            "Do not rewrite generated tests or generated manifests to make the app pass; repair the application code and runtime contract instead. "
            "When generated app tests report a create/update API failure, treat the primary repair cluster as app/routes/<resource>.py plus app/schemas.py and app/db.py before touching broader runtime files."
        )

    @staticmethod
    def repair_user_prompt(repair_packet: FixPromptContext, *, repair_feedback: str | None = None) -> str:
        return json.dumps(
            {
                "task": "Patch the draft workspace to resolve the current failing bundle and converge the checks toward a working app state.",
                "repair_packet": {
                    "workspace_id": repair_packet.workspace_id,
                    "run_id": repair_packet.run_id,
                    "attempt": repair_packet.attempt,
                    "failure_class": repair_packet.failure_class,
                    "failure_signature": repair_packet.failure_signature,
                    "root_cause_summary": repair_packet.root_cause_summary,
                    "exact_error_excerpt": repair_packet.exact_error_excerpt,
                    "context_mode": repair_packet.context_mode,
                    "failing_checks": repair_packet.failing_checks,
                    "api_failure_diagnostics": repair_packet.api_failure_diagnostics,
                    "normalized_critical_issues": repair_packet.normalized_critical_issues,
                    "failing_file_paths": repair_packet.failing_file_paths,
                    "expected_contract": repair_packet.expected_contract,
                    "file_contexts": repair_packet.file_contexts,
                    "tool_results": repair_packet.tool_results,
                    "read_only_surfaces": repair_packet.read_only_surfaces,
                    "previous_attempt_summary": repair_packet.previous_attempt_summary,
                    "previous_diff_summary": repair_packet.previous_diff_summary,
                    "repair_base": repair_packet.repair_base,
                    "generation_mode": repair_packet.generation_mode.value,
                },
                "repair_feedback": repair_feedback,
                "rules": [
                    "Fix only the current root-cause cluster before moving on.",
                    "Return the smallest safe patch.",
                    "Prefer editing the implicated failing files first and expand only when adjacent structural files are genuinely required.",
                    "Treat failing_checks, failing_file_paths, file_contexts, and hard runtime invariants as the source of truth for repair scope.",
                    "Only change generated app code. Generated tests, generated manifests, and platform runtime assets are read-only.",
                    "Do not modify miniapp/tests/*; default to repairing app code instead of test code.",
                    "Do not modify generated manifests such as route_manifest.json or generated_app_graph.json; repair the application bundle so the deterministic manifest builder stays correct.",
                    "Do not replace route modules with placeholder text/html responses to satisfy navigation tests; repair real route wiring and page surfaces.",
                    "Strict-green is the ideal target, but the immediate goal is to remove blocking runtime, compile, routing, and preview failures first.",
                    "Preserve existing endpoints, router wiring, and static file serving unless the evidence shows they are broken.",
                    "Do not replace main.py, route modules, or backend services with placeholder HTML stubs or hard-coded pages.",
                    "If generated_app_python_tests reports a create/update API failure, prioritize the resource route module plus app/schemas.py and app/db.py before touching main.py or unrelated runtime files.",
                    "If a FastAPI route raises HTTPException from ValidationError, keep the detail JSON-safe instead of passing through raw Python exceptions.",
                    "Every create or replace operation must include the full resulting file content.",
                    "Use the tool-owned repair loop: ask for tool actions first when evidence is insufficient, then return a patch only after you have enough context.",
                    "Always return outcome=patch_ready, outcome=tool_request, outcome=no_progress, or outcome=fatal_invalid_response.",
                    "Prefer outcome=tool_request with tool_requests when you need to inspect more files or rerun checks before editing.",
                    "Use tool=list_files to inspect the workspace tree, tool=read_files to request specific additional files, tool=search_files to search the workspace, tool=run_command to execute a diagnostic workspace command, and tool=run_checks when you need a fresh validation snapshot before patching.",
                    "For run_checks, choose mode=exact for focused fix verification and mode=final when you need the full final verification snapshot.",
                    "For run_command, provide a shell command that can run inside the draft workspace root and use it for real diagnostics, not for speculative rewrites.",
                    "run_command is diagnostic-only: destructive shell, git reset/discard, network fetches, package installs, and docker rebuild commands are blocked.",
                    "If you use outcome=tool_request, return tool_requests and no operations.",
                    "If you understand the issue but cannot yet produce a safe patch, return outcome=no_progress and explain exactly what contract is still unresolved.",
                    "Do not return diagnosis-only responses without an explicit outcome and executable patch state.",
                ],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def prompt_cache_key(repair_packet: FixPromptContext) -> str:
        digest = hashlib.sha1(
            "|".join(
                [
                    repair_packet.failure_class or "unknown",
                    repair_packet.failure_signature or "unknown",
                    repair_packet.context_mode,
                    ",".join(sorted(repair_packet.failing_file_paths)),
                ]
            ).encode("utf-8")
        ).hexdigest()
        return f"fix:{digest}"
