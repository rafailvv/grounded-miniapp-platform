from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from app.models.domain import CheckExecutionRecord
from app.models.guardian import GuardianChecklistItem, GuardianFinding, GuardianReviewReport
from app.services.product_readiness import ProductReadinessContract


ROLE_ORDER = ("client", "specialist", "manager")
PRODUCT_PREFIXES = ("miniapp/app/", "miniapp/requirements.txt", "miniapp/Dockerfile")
TEST_PREFIXES = ("miniapp/tests/",)
BACKEND_STATE_PREFIXES = ("miniapp/app/routes", "miniapp/app/db.py", "miniapp/app/schemas.py")
GENERATED_TEST_CHECKS = {"generated_app_python_tests", "generated_app_js_tests"}
REQUIRED_PRODUCT_PROOF = {"api_workflow_smoke", "browser_flow_smoke"}
MOCK_DATA_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:mock|demo|sample|seed)(?:Data|Items|Records|Users|Tasks|Orders)?\b\s*[:=]", "mock_or_seed_named_data"),
    (r"\b(?:const|let|var)\s+(?:users|tasks|orders|records|items)\s*=\s*\[", "hardcoded_collection"),
    (r"\b(?:John Doe|Jane Doe|Alice Example|Bob Example|Lorem ipsum|placeholder)\b", "placeholder_seed_text"),
)
SECURITY_PRIVACY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"(?i)\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|password|private[_-]?key)\b\s*[:=]\s*['\"]([^'\"]{12,})['\"]", "hardcoded_secret", "Changed source appears to contain a hard-coded secret or credential."),
    (r"\b(?:eval|Function)\s*\(", "dynamic_code_execution", "Changed source uses dynamic code execution."),
    (r"\.innerHTML\s*=", "unsafe_inner_html", "Changed source writes raw HTML and may expose injection risk."),
    (r"\bdocument\.cookie\s*=", "cookie_write", "Changed source writes browser cookies directly."),
    (r"(?i)\blocalStorage\.setItem\s*\(\s*['\"][^'\"]*(?:token|password|secret|email|phone|address)", "sensitive_local_storage", "Changed source stores sensitive data in localStorage."),
)
CHECKLIST_SPECS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("breaking_changes", "Breaking changes", ("breaking_changes",), ("guardian.breaking_changes.",)),
    ("missing_tests", "Missing tests", ("missing_tests",), ("guardian.missing_acceptance_tests",)),
    ("product_readiness", "Product readiness", ("product_readiness",), ("guardian.product_readiness.",)),
    ("mobile_overflow", "Mobile overflow", ("mobile_overflow",), ("guardian.mobile_overflow",)),
    ("stale_mock_data", "Stale mock data", ("seeded_mock_data", "stale_mock_data"), ("guardian.seeded_mock_data.", "guardian.stale_mock_data.")),
    ("context_bloat", "Context bloat", ("context_bloat",), ("guardian.context_bloat.",)),
    ("changed_size_risk", "Changed-size risk", ("changed_size_risk",), ("guardian.changed_size_risk.",)),
    ("security_privacy", "Security/privacy", ("security_privacy",), ("guardian.security_privacy.",)),
)
LARGE_CHANGED_FILE_BYTES = 300_000
TOTAL_CHANGED_BYTES_LIMIT = 1_500_000
CHANGED_FILE_COUNT_LIMIT = 35
DIFF_LINE_LIMIT = 3_000


class GuardianReview:
    """Deterministic pre-apply reviewer that emits blocker findings only."""

    RISK_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

    @classmethod
    def review_risky_action(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path | None,
        file_changes: list[Any],
        action_kind: str = "draft_apply",
        previous_rejections: list[dict[str, Any]] | None = None,
        diff_text: str = "",
    ) -> GuardianReviewReport:
        findings: list[GuardianFinding] = []
        changes = [item for item in file_changes if getattr(item, "file_path", None)]
        changed_files = cls._normalize_paths([str(getattr(item, "file_path", "")) for item in changes])
        destructive = [item for item in changes if str(getattr(item, "operation", "")) == "delete"]
        if destructive:
            findings.append(
                cls._finding(
                    code="guardian.destructive_action.delete_operation",
                    category="destructive_action",
                    message="Draft action attempts to delete files and requires a narrower reviewed edit.",
                    evidence={"files": [str(getattr(item, "file_path", "")) for item in destructive][:20], "action_kind": action_kind},
                    repair_hint="Replace delete operations with targeted patches unless the user explicitly requested deletion and proof covers the removed surface.",
                    severity="critical",
                )
            )
        forbidden_paths = [
            path
            for path in changed_files
            if path.startswith((".git/", "node_modules/", "dist/", "build/", ".cache/", ".sandbox/"))
            or "/.git/" in path
            or path in {".git", "node_modules", "dist", "build"}
        ]
        if forbidden_paths:
            findings.append(
                cls._finding(
                    code="guardian.policy.forbidden_mutation_path",
                    category="policy",
                    message="Draft action targets a path outside the product mutation surface.",
                    evidence={"forbidden_paths": forbidden_paths[:20]},
                    repair_hint="Use allowed draft file tools only for product source, tests, docs, or explicit app configuration.",
                    severity="critical",
                )
            )
        large_payloads = [
            {
                "file_path": str(getattr(item, "file_path", "")),
                "operation": str(getattr(item, "operation", "")),
                "chars": len(str(getattr(item, "content", "") or getattr(item, "diff", "") or "")),
            }
            for item in changes
            if len(str(getattr(item, "content", "") or getattr(item, "diff", "") or "")) > 250_000
        ]
        if large_payloads or len(changes) > 20:
            findings.append(
                cls._finding(
                    code="guardian.changed_size_risk.large_mutation_batch",
                    category="changed_size_risk",
                    message="Mutation batch is large enough to require splitting or focused review.",
                    evidence={"change_count": len(changes), "large_payloads": large_payloads[:20]},
                    repair_hint="Split the change into a smaller coherent patch and verify that slice before continuing.",
                    severity="high",
                )
            )
        findings.extend(cls._action_secret_findings(changes))
        findings.extend(cls._rejection_circuit_findings(changed_files=changed_files, findings=findings, previous_rejections=previous_rejections or []))
        findings = cls._dedupe(findings)
        risk_level = cls._risk_level(findings)
        blocker_count = sum(1 for item in findings if item.is_blocker_for_apply)
        status = "failed" if blocker_count else "passed"
        return GuardianReviewReport(
            run_id=run_id,
            workspace_id=workspace_id,
            status=status,
            source="pre_mutation_guardian",
            findings=findings,
            checklist=[],
            final_review_gate={
                "schema": "grounded.guardian_action_gate.v1",
                "status": status,
                "action_kind": action_kind,
                "risk_level": risk_level,
                "blocker_count": blocker_count,
                "finding_codes": [item.code for item in findings],
                "rejection_circuit_open": any(item.code == "guardian.rejection_circuit.repeated_rejected_action" for item in findings),
            },
            review_prompt=cls._review_prompt(action_kind=action_kind, changed_files=changed_files, diff_text=diff_text, risk_level=risk_level),
            summary={
                "finding_count": len(findings),
                "blocker_count": blocker_count,
                "risk_level": risk_level,
                "action_kind": action_kind,
            },
            evidence={"changed_files": changed_files[:80], "change_count": len(changes), "previous_rejection_count": len(previous_rejections or [])},
        )

    @classmethod
    def review(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        draft_source: Path | None,
        changed_files: list[str],
        latest_execution: CheckExecutionRecord | None,
        preview_details: dict[str, Any] | None = None,
        acceptance_contract: dict[str, Any] | None = None,
        implementation_plan: dict[str, Any] | None = None,
        target_role_scope: list[str] | None = None,
        intent: str | None = None,
        source: str = "runtime_verifier",
        review_context: dict[str, Any] | None = None,
    ) -> GuardianReviewReport:
        del preview_details
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        context = review_context if isinstance(review_context, dict) else {}
        diff_text = str(context.get("diff") or "")
        acceptance_required = bool(contract.get("required")) or str(intent or "") == "create"
        changed = cls._normalize_paths(changed_files)
        product_changed = [path for path in changed if cls._is_product_path(path)]
        app_changed = [path for path in changed if path.startswith("miniapp/app/")]
        test_changed = [path for path in changed if path.startswith(TEST_PREFIXES)]
        backend_state_changed = [path for path in changed if path.startswith(BACKEND_STATE_PREFIXES)]
        findings: list[GuardianFinding] = []
        result_by_name = {
            result.name: result
            for result in (latest_execution.results if latest_execution is not None else [])
        }

        findings.extend(
            cls._breaking_change_findings(
                diff_text=diff_text,
                changed_files=changed,
                result_by_name=result_by_name,
                acceptance_required=acceptance_required,
            )
        )
        findings.extend(
            cls._changed_size_risk_findings(
                draft_source=draft_source,
                changed_files=product_changed,
                diff_text=diff_text,
            )
        )
        findings.extend(
            cls._security_privacy_findings(
                draft_source=draft_source,
                changed_files=product_changed,
            )
        )
        findings.extend(cls._context_bloat_findings(review_context=context))
        findings.extend(cls._check_green_findings(latest_execution, acceptance_required=acceptance_required))
        findings.extend(
            cls._missing_test_findings(
                acceptance_required=acceptance_required,
                app_changed=app_changed,
                test_changed=test_changed,
                result_by_name=result_by_name,
            )
        )
        findings.extend(
            cls._role_workflow_findings(
                acceptance_required=acceptance_required,
                target_role_scope=target_role_scope,
                result_by_name=result_by_name,
            )
        )
        findings.extend(cls._mobile_findings(result_by_name=result_by_name))
        findings.extend(
            cls._persistence_findings(
                acceptance_required=acceptance_required,
                product_changed=product_changed,
                backend_state_changed=backend_state_changed,
                result_by_name=result_by_name,
                acceptance_contract=contract,
                implementation_plan=plan,
            )
        )
        findings.extend(
            cls._seeded_mock_findings(
                draft_source=draft_source,
                changed_files=product_changed,
                acceptance_required=acceptance_required,
            )
        )
        findings.extend(
            cls._product_readiness_findings(
                acceptance_required=acceptance_required,
                latest_execution=latest_execution,
                acceptance_contract=contract,
                implementation_plan=plan,
                target_role_scope=target_role_scope,
            )
        )

        findings = cls._dedupe(findings)
        checklist = cls._final_review_checklist(findings)
        blocker_count = sum(1 for item in findings if item.is_blocker_for_apply)
        category_counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        for item in findings:
            category_counts[item.category] = category_counts.get(item.category, 0) + 1
            severity_counts[item.severity] = severity_counts.get(item.severity, 0) + 1
        return GuardianReviewReport(
            run_id=run_id,
            workspace_id=workspace_id,
            status="failed" if blocker_count else "passed",
            source=source if source in {"pre_apply_guardian", "runtime_verifier", "manual_review"} else "runtime_verifier",
            findings=findings,
            checklist=checklist,
            final_review_gate={
                "schema": "grounded.final_review_gate.v1",
                "status": "failed" if blocker_count else "passed",
                "checklist_version": 1,
                "checklist_order": [item[0] for item in CHECKLIST_SPECS],
                "blocker_count": blocker_count,
                "failed_checks": [item.key for item in checklist if item.status == "failed"],
                "finding_codes": [item.code for item in findings],
            },
            review_prompt=cls._review_prompt(action_kind="diff_apply", changed_files=changed, diff_text=diff_text, risk_level=cls._risk_level(findings)),
            summary={
                "finding_count": len(findings),
                "blocker_count": blocker_count,
                "risk_level": cls._risk_level(findings),
                "category_counts": category_counts,
                "severity_counts": severity_counts,
                "final_review_gate_status": "failed" if blocker_count else "passed",
                "failed_checklist_items": [item.key for item in checklist if item.status == "failed"],
            },
            evidence={
                "changed_files": changed[:80],
                "product_changed": product_changed[:80],
                "acceptance_required": acceptance_required,
                "check_names": sorted(result_by_name),
                "target_role_scope": list(target_role_scope or []),
                "diff_line_count": len(diff_text.splitlines()) if diff_text else 0,
            },
        )

    @staticmethod
    def _normalize_paths(paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for path in paths:
            candidate = str(path or "").strip().replace("\\", "/")
            if not candidate:
                continue
            if candidate.startswith("draft/"):
                candidate = candidate.split("draft/", 1)[-1]
            if candidate.startswith("source/"):
                candidate = candidate.split("source/", 1)[-1]
            normalized.append(candidate)
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _is_product_path(path: str) -> bool:
        if path.startswith(TEST_PREFIXES) or path.startswith("miniapp/app/generated/"):
            return False
        return path.startswith(PRODUCT_PREFIXES)

    @classmethod
    def _breaking_change_findings(
        cls,
        *,
        diff_text: str,
        changed_files: list[str],
        result_by_name: dict[str, Any],
        acceptance_required: bool,
    ) -> list[GuardianFinding]:
        if not acceptance_required:
            return []
        api = result_by_name.get("api_workflow_smoke")
        api_passed = api is not None and getattr(api, "status", None) == "passed"
        backend_changed = [
            path
            for path in changed_files
            if path.startswith(("miniapp/app/routes", "miniapp/app/main.py", "miniapp/app/db.py", "miniapp/app/schemas.py"))
        ]
        removed_routes: list[str] = []
        for line in diff_text.splitlines():
            if not line.startswith("-") or line.startswith("---"):
                continue
            if re.search(r"@(?:router|app)\.(?:get|post|put|patch|delete)\s*\(", line):
                removed_routes.append(line[1:].strip()[:180])
                continue
            removed_routes.extend(re.findall(r"['\"](/api/[a-zA-Z0-9_./:-]+)['\"]", line))
        findings: list[GuardianFinding] = []
        if removed_routes and not api_passed:
            findings.append(
                cls._finding(
                    code="guardian.breaking_changes.removed_api_route_without_api_proof",
                    category="breaking_changes",
                    message="Candidate removes or changes API route surface without a passing API workflow proof.",
                    evidence={"removed_routes": list(dict.fromkeys(removed_routes))[:20], "api_workflow_smoke": getattr(api, "status", None)},
                    repair_hint="Re-run API workflow proof and update generated tests for the changed route contract before apply.",
                )
            )
        if backend_changed and not api_passed:
            findings.append(
                cls._finding(
                    code="guardian.breaking_changes.backend_contract_changed_without_api_proof",
                    category="breaking_changes",
                    message="Backend route/schema/state files changed without a passing API workflow proof.",
                    evidence={"backend_changed": backend_changed[:20], "api_workflow_smoke": getattr(api, "status", None)},
                    repair_hint="Run API workflow smoke against the changed backend contract before applying source.",
                )
            )
        return findings

    @classmethod
    def _changed_size_risk_findings(
        cls,
        *,
        draft_source: Path | None,
        changed_files: list[str],
        diff_text: str,
    ) -> list[GuardianFinding]:
        diff_line_count = len(diff_text.splitlines()) if diff_text else 0
        large_files: list[dict[str, Any]] = []
        total_bytes = 0
        if draft_source is not None:
            for path in changed_files:
                target = draft_source / path
                try:
                    if not target.is_file():
                        continue
                    size = target.stat().st_size
                except OSError:
                    continue
                total_bytes += size
                if size > LARGE_CHANGED_FILE_BYTES:
                    large_files.append({"path": path, "bytes": size})
        changed_count = len(changed_files)
        if changed_count <= CHANGED_FILE_COUNT_LIMIT and total_bytes <= TOTAL_CHANGED_BYTES_LIMIT and diff_line_count <= DIFF_LINE_LIMIT and not large_files:
            return []
        return [
            cls._finding(
                code="guardian.changed_size_risk.large_candidate_delta",
                category="changed_size_risk",
                message="Candidate delta is large enough to raise review and regression risk before apply.",
                evidence={
                    "changed_file_count": changed_count,
                    "diff_line_count": diff_line_count,
                    "total_changed_file_bytes": total_bytes,
                    "large_files": large_files[:20],
                    "limits": {
                        "changed_files": CHANGED_FILE_COUNT_LIMIT,
                        "diff_lines": DIFF_LINE_LIMIT,
                        "total_bytes": TOTAL_CHANGED_BYTES_LIMIT,
                        "single_file_bytes": LARGE_CHANGED_FILE_BYTES,
                    },
                },
                repair_hint="Split the patch, reduce generated bulk, or add focused proof for the large changed surface before apply.",
            )
        ]

    @classmethod
    def _security_privacy_findings(
        cls,
        *,
        draft_source: Path | None,
        changed_files: list[str],
    ) -> list[GuardianFinding]:
        if draft_source is None:
            return []
        findings: list[GuardianFinding] = []
        for path in changed_files:
            if not path.startswith("miniapp/app/") or not path.endswith((".js", ".ts", ".jsx", ".tsx", ".py", ".html")):
                continue
            target = draft_source / path
            try:
                if not target.is_file() or target.stat().st_size > 400_000:
                    continue
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pattern, reason, message in SECURITY_PRIVACY_PATTERNS:
                match = re.search(pattern, text)
                if not match:
                    continue
                if reason == "hardcoded_secret" and cls._placeholder_secret(match.group(1)):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    cls._finding(
                        code=f"guardian.security_privacy.{reason}",
                        category="security_privacy",
                        message=message,
                        file_path=path,
                        line=line,
                        evidence={"pattern": reason, "excerpt": text[max(0, match.start() - 80): match.end() + 120]},
                        repair_hint="Remove the risky security/privacy pattern or replace it with a safe server-side/configured flow before apply.",
                        severity="critical" if reason == "hardcoded_secret" else "high",
                    )
                )
                break
        return findings

    @classmethod
    def _context_bloat_findings(cls, *, review_context: dict[str, Any]) -> list[GuardianFinding]:
        token_usage = review_context.get("token_usage") if isinstance(review_context.get("token_usage"), dict) else {}
        run_payload = review_context.get("run") if isinstance(review_context.get("run"), dict) else {}
        if not token_usage and isinstance(run_payload.get("token_usage"), dict):
            token_usage = run_payload["token_usage"]
        context_pressure = review_context.get("context_pressure") if isinstance(review_context.get("context_pressure"), dict) else {}
        if not context_pressure and isinstance(review_context.get("context_pressure_report"), dict):
            context_pressure = review_context["context_pressure_report"]
        findings: list[GuardianFinding] = []
        pressure_status = str(context_pressure.get("status") or context_pressure.get("level") or "").lower()
        if pressure_status in {"critical", "blocked", "over_budget", "exceeded"}:
            findings.append(
                cls._finding(
                    code="guardian.context_bloat.context_pressure_critical",
                    category="context_bloat",
                    message="Context pressure is critical before apply; the final patch may not have been reviewed with full working context.",
                    evidence={"context_pressure": context_pressure},
                    repair_hint="Compact/summarize the run state, re-open the changed slices, and rerun final verification before apply.",
                    severity="medium",
                )
            )
        total_tokens = cls._number_value(token_usage.get("total_tokens") or token_usage.get("input_tokens") or token_usage.get("tokens"))
        remaining_tokens = cls._number_value(token_usage.get("context_window_remaining") or token_usage.get("remaining_context_tokens"))
        if total_tokens >= 160_000 or (remaining_tokens and remaining_tokens < 8_000):
            findings.append(
                cls._finding(
                    code="guardian.context_bloat.token_budget_risk",
                    category="context_bloat",
                    message="Token/context budget is high enough to require a fresh final review pass before apply.",
                    evidence={"token_usage": token_usage},
                    repair_hint="Run a compacted final review over the current diff and proofs before applying source.",
                    severity="medium",
                )
            )
        return findings

    @classmethod
    def _action_secret_findings(cls, changes: list[Any]) -> list[GuardianFinding]:
        findings: list[GuardianFinding] = []
        for item in changes:
            path = str(getattr(item, "file_path", "") or "")
            payload = str(getattr(item, "content", "") or getattr(item, "diff", "") or "")
            if not payload:
                continue
            for pattern, reason, message in SECURITY_PRIVACY_PATTERNS:
                match = re.search(pattern, payload)
                if not match:
                    continue
                if reason == "hardcoded_secret" and cls._placeholder_secret(match.group(1)):
                    continue
                findings.append(
                    cls._finding(
                        code=f"guardian.security_privacy.{reason}",
                        category="security_privacy",
                        message=message,
                        file_path=path,
                        evidence={"pattern": reason, "payload": "draft_action"},
                        repair_hint="Remove the risky payload from the proposed mutation before applying the draft action.",
                        severity="critical" if reason == "hardcoded_secret" else "high",
                    )
                )
                break
        return findings

    @classmethod
    def _rejection_circuit_findings(
        cls,
        *,
        changed_files: list[str],
        findings: list[GuardianFinding],
        previous_rejections: list[dict[str, Any]],
    ) -> list[GuardianFinding]:
        if not findings or not previous_rejections:
            return []
        current_signatures = {cls._rejection_signature(code=finding.code, paths=changed_files) for finding in findings}
        repeats = [
            item
            for item in previous_rejections
            if isinstance(item, dict) and str(item.get("signature") or "") in current_signatures
        ]
        if len(repeats) < 1:
            return []
        return [
            cls._finding(
                code="guardian.rejection_circuit.repeated_rejected_action",
                category="policy",
                message="This mutation repeats a previously rejected risky action.",
                evidence={"repeat_count": len(repeats), "signatures": sorted(current_signatures)[:20], "changed_files": changed_files[:20]},
                repair_hint="Do not retry the same workaround. Read the prior rejection, choose a different implementation path, and reduce the risky surface.",
                severity="critical",
            )
        ]

    @classmethod
    def _risk_level(cls, findings: list[GuardianFinding]) -> str:
        if not findings:
            return "low"
        highest = max(cls.RISK_ORDER.get(item.severity, 0) for item in findings)
        for label, value in cls.RISK_ORDER.items():
            if value == highest:
                return label
        return "high"

    @staticmethod
    def _rejection_signature(*, code: str, paths: list[str]) -> str:
        normalized_paths = ",".join(sorted(str(path or "").strip().replace("\\", "/") for path in paths if str(path or "").strip())[:20])
        return f"{code}:{normalized_paths}"

    @staticmethod
    def _review_prompt(*, action_kind: str, changed_files: list[str], diff_text: str, risk_level: str) -> dict[str, Any]:
        return {
            "schema": "grounded.guardian_review_prompt.v1",
            "action_kind": action_kind,
            "risk_level": risk_level,
            "instructions": [
                "Review only the proposed diff/action, not unrelated code.",
                "Block destructive, secret-bearing, broad, or policy-bypassing changes.",
                "After a rejection, do not approve a repeated workaround with the same risky signature.",
            ],
            "changed_files": changed_files[:80],
            "diff_preview": diff_text[:4000],
        }

    @classmethod
    def _check_green_findings(cls, latest_execution: CheckExecutionRecord | None, *, acceptance_required: bool) -> list[GuardianFinding]:
        if latest_execution is None:
            if not acceptance_required:
                return []
            return [
                cls._finding(
                    code="guardian.missing_check_execution",
                    category="check",
                    message="No green check execution is available for the candidate draft.",
                    evidence={},
                    repair_hint="Run the full product checks before source apply.",
                )
            ]
        findings: list[GuardianFinding] = []
        for result in latest_execution.results:
            if result.status not in {"failed", "blocked"}:
                continue
            findings.append(
                cls._finding(
                    code=f"guardian.check_not_green.{result.name}",
                    category="check",
                    message=f"{result.name} is {result.status}; source apply requires green checks.",
                    evidence={"check": result.model_dump(mode="json"), "logs_tail": list(result.logs or [])[-6:]},
                    repair_hint="Fix the failing check and rerun verification.",
                )
            )
        return findings

    @classmethod
    def _missing_test_findings(
        cls,
        *,
        acceptance_required: bool,
        app_changed: list[str],
        test_changed: list[str],
        result_by_name: dict[str, Any],
    ) -> list[GuardianFinding]:
        if not acceptance_required or not app_changed:
            return []
        generated_checks = [
            name
            for name in GENERATED_TEST_CHECKS
            if name in result_by_name and getattr(result_by_name[name], "status", None) == "passed"
        ]
        if generated_checks or test_changed:
            return []
        return [
            cls._finding(
                code="guardian.missing_acceptance_tests",
                category="missing_tests",
                message="Product files changed without generated acceptance test evidence.",
                evidence={"app_changed": app_changed[:20], "test_changed": test_changed, "generated_test_checks": sorted(GENERATED_TEST_CHECKS & set(result_by_name))},
                repair_hint="Add or update generated acceptance tests and run them before apply.",
            )
        ]

    @classmethod
    def _role_workflow_findings(
        cls,
        *,
        acceptance_required: bool,
        target_role_scope: list[str] | None,
        result_by_name: dict[str, Any],
    ) -> list[GuardianFinding]:
        if not acceptance_required:
            return []
        required_roles = set(target_role_scope or ROLE_ORDER)
        browser = result_by_name.get("browser_flow_smoke")
        diagnostics = dict(getattr(browser, "diagnostics", {}) or {}) if browser is not None else {}
        checked_roles = {str(role) for role in diagnostics.get("roles_checked") or []}
        findings: list[GuardianFinding] = []
        if required_roles and checked_roles and not required_roles.issubset(checked_roles):
            findings.append(
                cls._finding(
                    code="guardian.role_workflow_missing_browser_roles",
                    category="role_workflow",
                    message="Browser proof did not cover every required role workflow.",
                    evidence={"required_roles": sorted(required_roles), "checked_roles": sorted(checked_roles)},
                    repair_hint="Exercise the missing role pages in browser proof.",
                )
            )
        if browser is None:
            findings.append(
                cls._finding(
                    code="guardian.role_workflow_missing_browser_proof",
                    category="role_workflow",
                    message="No browser workflow proof is present for role workflow acceptance.",
                    evidence={"required_roles": sorted(required_roles)},
                    repair_hint="Run browser_flow_smoke with role workflow coverage.",
                )
            )
        ui_steps = diagnostics.get("ui_steps") or diagnostics.get("steps") or []
        if browser is not None and (not isinstance(ui_steps, list) or not ui_steps):
            findings.append(
                cls._finding(
                    code="guardian.role_workflow_missing_ui_steps",
                    category="role_workflow",
                    message="Browser proof lacks concrete UI workflow steps.",
                    evidence={"browser_flow_smoke": diagnostics},
                    repair_hint="Record create/update/read UI steps for the changed workflow.",
                )
            )
        return findings

    @classmethod
    def _mobile_findings(cls, *, result_by_name: dict[str, Any]) -> list[GuardianFinding]:
        browser = result_by_name.get("browser_flow_smoke")
        diagnostics = dict(getattr(browser, "diagnostics", {}) or {}) if browser is not None else {}
        mobile = diagnostics.get("mobile_layout")
        if not isinstance(mobile, dict):
            return []
        if mobile.get("status") == "failed" or mobile.get("horizontal_overflow") or mobile.get("critical_overlap"):
            return [
                cls._finding(
                    code="guardian.mobile_overflow",
                    category="mobile_overflow",
                    message="Mobile layout proof has overflow or critical overlap.",
                    evidence={"mobile_layout": mobile},
                    repair_hint="Fix mobile overflow/overlap and rerun browser_flow_smoke.",
                )
            ]
        return []

    @classmethod
    def _persistence_findings(
        cls,
        *,
        acceptance_required: bool,
        product_changed: list[str],
        backend_state_changed: list[str],
        result_by_name: dict[str, Any],
        acceptance_contract: dict[str, Any],
        implementation_plan: dict[str, Any],
    ) -> list[GuardianFinding]:
        if not acceptance_required or not product_changed:
            return []
        browser = result_by_name.get("browser_flow_smoke")
        diagnostics = dict(getattr(browser, "diagnostics", {}) or {}) if browser is not None else {}
        has_persisted_marker = any(
            diagnostics.get(key)
            for key in ("persisted_marker", "persisted_state_marker", "created_marker", "created_state_marker")
        )
        features = acceptance_contract.get("features") if isinstance(acceptance_contract.get("features"), dict) else {}
        flows = acceptance_contract.get("flows") if isinstance(acceptance_contract.get("flows"), list) else []
        entities = implementation_plan.get("primary_entities") if isinstance(implementation_plan.get("primary_entities"), list) else []
        persistence_expected = bool(features.get("workflow_update") or flows or entities)
        if has_persisted_marker:
            return []
        findings: list[GuardianFinding] = [
            cls._finding(
                code="guardian.weak_persistence_missing_browser_marker",
                category="weak_persistence",
                message="Browser proof does not show persisted create/update state.",
                evidence={"browser_flow_smoke": diagnostics, "features": features, "flow_count": len(flows), "primary_entities": entities[:12]},
                repair_hint="Persist workflow state and prove create/update survives UI/API roundtrip.",
            )
        ]
        if persistence_expected and not backend_state_changed:
            findings.append(
                cls._finding(
                    code="guardian.weak_persistence_no_backend_state_change",
                    category="weak_persistence",
                    message="Workflow product changed without backend route/schema/db persistence changes.",
                    evidence={"product_changed": product_changed[:20], "backend_state_changed": backend_state_changed},
                    repair_hint="Add or update the backend route/schema/db layer for the workflow state.",
                )
            )
        return findings

    @classmethod
    def _seeded_mock_findings(
        cls,
        *,
        draft_source: Path | None,
        changed_files: list[str],
        acceptance_required: bool,
    ) -> list[GuardianFinding]:
        if not acceptance_required or draft_source is None:
            return []
        findings: list[GuardianFinding] = []
        for path in changed_files:
            if not path.startswith("miniapp/app/") or not path.endswith((".js", ".ts", ".jsx", ".tsx", ".py", ".html")):
                continue
            target = draft_source / path
            try:
                if not target.is_file() or target.stat().st_size > 300_000:
                    continue
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pattern, reason in MOCK_DATA_PATTERNS:
                match = re.search(pattern, text)
                if not match:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    cls._finding(
                        code=f"guardian.seeded_mock_data.{reason}",
                        category="seeded_mock_data",
                        message="Product runtime appears to rely on seeded/mock data instead of user-created state.",
                        file_path=path,
                        line=line,
                        evidence={"pattern": reason, "excerpt": text[max(0, match.start() - 80): match.end() + 120]},
                        repair_hint="Replace seeded/mock runtime data with persisted product state and acceptance proof.",
                    )
                )
                break
        return findings

    @classmethod
    def _product_readiness_findings(
        cls,
        *,
        acceptance_required: bool,
        latest_execution: CheckExecutionRecord | None,
        acceptance_contract: dict[str, Any],
        implementation_plan: dict[str, Any],
        target_role_scope: list[str] | None,
    ) -> list[GuardianFinding]:
        if not acceptance_required:
            return []
        readiness = ProductReadinessContract.evaluate(
            run_mode="generate",
            acceptance_contract=acceptance_contract,
            implementation_plan=implementation_plan,
            results=list(latest_execution.results if latest_execution is not None else []),
            target_role_scope=target_role_scope,
            require_diff=False,
            require_product_source_change=False,
            require_apply=False,
        )
        findings: list[GuardianFinding] = []
        for issue in readiness.blocking_reasons:
            payload = issue.model_dump(mode="json") if hasattr(issue, "model_dump") else dict(issue)
            kind = str(payload.get("kind") or "readiness")
            check = str(payload.get("check") or "product_readiness")
            findings.append(
                cls._finding(
                    code=f"guardian.product_readiness.{kind}.{check}",
                    category="product_readiness",
                    message=str(payload.get("details") or "Production readiness proof is incomplete."),
                    evidence=payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload,
                    repair_hint="Complete the API, persistence, browser role, mobile, and generated-test proof before apply.",
                )
            )
        return findings

    @classmethod
    def _final_review_checklist(cls, findings: list[GuardianFinding]) -> list[GuardianChecklistItem]:
        items: list[GuardianChecklistItem] = []
        for key, label, categories, code_prefixes in CHECKLIST_SPECS:
            matched = [
                finding
                for finding in findings
                if finding.category in categories or any(finding.code.startswith(prefix) for prefix in code_prefixes)
            ]
            blockers = [finding for finding in matched if finding.is_blocker_for_apply]
            items.append(
                GuardianChecklistItem(
                    key=key,  # type: ignore[arg-type]
                    label=label,
                    status="failed" if blockers else "passed",
                    required=True,
                    blocker=bool(blockers),
                    finding_codes=[finding.code for finding in matched],
                    details=(
                        f"{len(blockers)} blocker finding(s) require repair before apply."
                        if blockers
                        else "No blocker findings."
                    ),
                    evidence={
                        "finding_count": len(matched),
                        "blocker_count": len(blockers),
                        "categories": list(categories),
                    },
                )
            )
        return items

    @staticmethod
    def _placeholder_secret(value: str) -> bool:
        lowered = str(value or "").lower()
        return any(token in lowered for token in ("placeholder", "example", "dummy", "test", "your_", "changeme", "replace_me"))

    @staticmethod
    def _number_value(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _finding(
        *,
        code: str,
        category: str,
        message: str,
        evidence: dict[str, Any],
        repair_hint: str,
        file_path: str | None = None,
        line: int | None = None,
        severity: str = "high",
    ) -> GuardianFinding:
        return GuardianFinding(
            code=code,
            severity=severity if severity in {"critical", "high", "medium", "low", "info"} else "high",
            category=category,  # type: ignore[arg-type]
            message=message,
            is_blocker_for_apply=True,
            file_path=file_path,
            line=line,
            evidence=evidence,
            repair_hint=repair_hint,
        )

    @staticmethod
    def _dedupe(findings: list[GuardianFinding]) -> list[GuardianFinding]:
        deduped: list[GuardianFinding] = []
        seen: set[tuple[str, str, int | None]] = set()
        for finding in findings:
            key = (finding.code, finding.file_path or "", finding.line)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(finding)
        return deduped
