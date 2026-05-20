from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from app.models.domain import CheckExecutionRecord
from app.models.guardian import GuardianFinding, GuardianReviewReport


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


class GuardianReview:
    """Deterministic pre-apply reviewer that emits blocker findings only."""

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
    ) -> GuardianReviewReport:
        del preview_details
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
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

        findings = cls._dedupe(findings)
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
            summary={
                "finding_count": len(findings),
                "blocker_count": blocker_count,
                "category_counts": category_counts,
                "severity_counts": severity_counts,
            },
            evidence={
                "changed_files": changed[:80],
                "product_changed": product_changed[:80],
                "acceptance_required": acceptance_required,
                "check_names": sorted(result_by_name),
                "target_role_scope": list(target_role_scope or []),
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
