from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any

from app.models.domain import RunCheckResult
from app.models.workbench import ProductReadinessCheck, ProductReadinessResult
from app.services.generation_sla import GenerationSla
from app.services.platform_config import platform_config


ROLE_ORDER = ("client", "specialist", "manager")
REQUIRED_PRODUCT_CHECKS = GenerationSla.required_checks("balanced")
STRICT_STATIC_CHECKS = ("changed_files_static", "frontend_interaction_static_smoke", "platform_invariants")
PREVIEW_CHECKS = ("preview_boot_smoke", "preview_connectivity_smoke", "browser_flow_smoke")
PLACEHOLDER_DATA_RE = re.compile(
    r"\b(mockData|demoData|sampleData|placeholderData|fakeData|seedData)\b|"
    r"\b(John Doe|Jane Doe|Lorem ipsum|TODO product|demo item|mock item)\b",
    re.I,
)


class ProductReadinessContract:
    """Canonical production acceptance proof for generated mini-app runs."""

    @classmethod
    def evaluate(
        cls,
        *,
        run_mode: str | None,
        generation_mode: str | None = None,
        intent: str | None = None,
        acceptance_contract: dict[str, Any] | None = None,
        implementation_plan: dict[str, Any] | None = None,
        results: list[RunCheckResult | dict[str, Any]] | None = None,
        diff_text: str = "",
        touched_files: list[str] | None = None,
        target_role_scope: list[str] | None = None,
        mobile_layout_report: dict[str, Any] | None = None,
        apply_status: str | None = None,
        run_status: str | None = None,
        repair_issue_signatures: list[dict[str, Any]] | None = None,
        require_diff: bool = True,
        require_product_source_change: bool = True,
        require_apply: bool = False,
    ) -> ProductReadinessResult:
        del intent
        contract = acceptance_contract if isinstance(acceptance_contract, dict) else {}
        plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        check_results = [cls._normalize_result(item) for item in (results or [])]
        by_name = {str(item.get("name") or ""): item for item in check_results}
        mode = str(run_mode or "").lower()
        generation = str(generation_mode or "").lower()
        sla_profile = GenerationSla.profile(generation)
        product_config = platform_config()
        browser_config = product_config.browser_proof
        required_product_checks = tuple(sla_profile.required_checks)
        browser_required = browser_config.enabled and ("browser_flow_smoke" in required_product_checks or generation in set(browser_config.required_modes))
        require_browser_ui_steps = not generation or generation in set(browser_config.require_ui_steps_modes)
        require_browser_persisted_marker = not generation or generation in set(browser_config.require_persisted_marker_modes)
        full_audit_required = GenerationSla.requires_full_audit(generation)
        acceptance_required = bool(contract.get("required")) or mode in {"generate", "fix"} or generation in {"quality", "balanced", "production"}
        diff_paths = cls._paths_from_diff(diff_text)
        changed_paths = list(dict.fromkeys([*diff_paths, *[str(path) for path in (touched_files or []) if str(path).strip()]]))
        product_paths = [path for path in changed_paths if cls._is_product_runtime_source_path(path)]
        issues: list[dict[str, Any]] = []
        required_checks: list[ProductReadinessCheck] = []
        evidence: dict[str, Any] = {
            "changed_files": changed_paths[:80],
            "product_runtime_paths": product_paths[:80],
            "acceptance_contract_required": bool(contract.get("required")),
            "generation_sla": {
                "mode": sla_profile.mode,
                "label": sla_profile.label,
                "required_checks": list(required_product_checks),
                "audit_level": sla_profile.audit_level,
                "proof_requirements": list(sla_profile.proof_requirements),
            },
            "platform_config": {
                "browser_proof": browser_config.model_dump(mode="json"),
            },
        }

        def add_issue(kind: str, check: str, details: str, *, evidence_payload: dict[str, Any] | None = None) -> None:
            issues.append(
                {
                    "kind": kind,
                    "check": check,
                    "details": details,
                    "blocking": True,
                    "evidence": evidence_payload or {},
                }
            )

        if require_diff and mode in {"generate", "fix"} and not str(diff_text or "").strip() and not touched_files:
            add_issue("meaningful_diff", "meaningful_diff", "Run has no meaningful draft/source diff.")
        if acceptance_required and require_product_source_change and changed_paths and not product_paths:
            add_issue(
                "product_source_diff",
                "meaningful_product_diff",
                "Acceptance run changed only generated tests or contract metadata; product runtime source must change.",
                evidence_payload={"touched_files": touched_files or [], "diff_paths": diff_paths},
            )
        for item in check_results:
            if str(item.get("status") or "") in {"failed", "blocked"}:
                add_issue("check_failure", str(item.get("name") or "check"), str(item.get("details") or "Check failed."), evidence_payload=item)

        if acceptance_required:
            for check_name in required_product_checks:
                check = by_name.get(check_name)
                status = str((check or {}).get("status") or "missing")
                details = f"{check_name} must pass for {sla_profile.mode} generation SLA."
                required_checks.append(
                    ProductReadinessCheck(
                        key=check_name,
                        label=cls._check_label(check_name),
                        status="passed" if status == "passed" else "blocked",
                        required=True,
                        check=check_name,
                        details=None if status == "passed" else details,
                        evidence=check or {},
                    )
                )
                if status != "passed":
                    add_issue("required_product_proof", check_name, details, evidence_payload=check or {"status": status})

            api = by_name.get("api_workflow_smoke") or {}
            api_diagnostics = cls._diagnostics(api)
            api_marker = cls._first_truthy(
                api_diagnostics,
                "persisted_state_marker",
                "persisted_marker",
                "created_state_marker",
                "created_marker",
            )
            evidence["api"] = {
                "status": api.get("status"),
                "persisted_marker": api_marker,
                "api_before": api_diagnostics.get("api_before"),
                "api_after": api_diagnostics.get("api_after"),
                "steps": api_diagnostics.get("steps") or [],
                "paths": api_diagnostics.get("api_paths") or [],
            }
            if api.get("status") == "passed" and not api_marker:
                add_issue(
                    "persistence_proof_missing_marker",
                    "api_workflow_smoke",
                    "API workflow proof passed without a persisted state marker.",
                    evidence_payload=api,
                )

            browser = by_name.get("browser_flow_smoke") or {}
            browser_diagnostics = cls._diagnostics(browser)
            browser_steps = browser_diagnostics.get("ui_steps") or browser_diagnostics.get("steps") or []
            browser_marker = cls._first_truthy(
                browser_diagnostics,
                "persisted_state_marker",
                "persisted_marker",
                "created_state_marker",
                "created_marker",
            )
            update_marker = cls._first_truthy(
                browser_diagnostics,
                "update_state_marker",
                "update_marker",
                "updated_state_marker",
                "updated_marker",
            )
            required_roles = cls._required_roles(contract=contract, target_role_scope=target_role_scope)
            checked_roles = {str(role).strip().lower() for role in browser_diagnostics.get("roles_checked") or [] if str(role).strip()}
            mobile = mobile_layout_report if isinstance(mobile_layout_report, dict) and mobile_layout_report else browser_diagnostics.get("mobile_layout")
            evidence["browser"] = {
                "status": browser.get("status"),
                "roles_required": required_roles,
                "roles_checked": sorted(checked_roles),
                "ui_steps": browser_steps if isinstance(browser_steps, list) else [],
                "persisted_marker": browser_marker,
                "update_marker": update_marker,
                "screenshots": browser_diagnostics.get("screenshots") or [],
                "console_errors": cls._list_values(browser_diagnostics.get("console_errors")),
                "network_errors": cls._list_values(browser_diagnostics.get("network_errors")),
                "visible_errors": cls._list_values(browser_diagnostics.get("visible_errors")),
            }
            evidence["mobile"] = mobile if isinstance(mobile, dict) else {}
            if browser_required and browser.get("status") == "passed":
                if require_browser_ui_steps and (not isinstance(browser_steps, list) or not browser_steps):
                    add_issue("browser_proof_missing_ui_steps", "browser_flow_smoke", "Browser proof lacks concrete UI workflow steps.", evidence_payload=browser)
                if require_browser_persisted_marker and not browser_marker:
                    add_issue("browser_proof_missing_persisted_marker", "browser_flow_smoke", "Browser proof does not show UI-created state persisted after read/reload.", evidence_payload=browser)
                missing_roles = sorted(set(required_roles) - checked_roles)
                if missing_roles:
                    add_issue(
                        "browser_proof_missing_roles",
                        "browser_flow_smoke",
                        "Browser proof did not cover every required role workflow.",
                        evidence_payload={"required_roles": required_roles, "checked_roles": sorted(checked_roles), "missing_roles": missing_roles},
                    )
                features = contract.get("features") if isinstance(contract.get("features"), dict) else {}
                if features.get("workflow_update") and not update_marker:
                    add_issue("browser_proof_missing_update_marker", "browser_flow_smoke", "Browser proof does not show update state persisted.", evidence_payload=browser)
                runtime_errors = [
                    *cls._list_values(browser_diagnostics.get("console_errors")),
                    *cls._list_values(browser_diagnostics.get("network_errors")),
                    *cls._list_values(browser_diagnostics.get("visible_errors")),
                ]
                if runtime_errors:
                    add_issue("browser_proof_runtime_errors", "browser_flow_smoke", "Browser proof contains console, network, or visible runtime errors.", evidence_payload={"errors": runtime_errors[:12], "browser_flow_smoke": browser})
            if browser_required and (not isinstance(mobile, dict) or not mobile):
                add_issue("mobile_layout_missing", "browser_flow_smoke", "Mobile layout proof is missing from browser workflow evidence.", evidence_payload=browser)
            elif browser_required and (mobile.get("status") == "failed" or mobile.get("horizontal_overflow") or mobile.get("critical_overlap")):
                add_issue("mobile_layout", "browser_flow_smoke", "Mobile layout report contains blocking issues.", evidence_payload=mobile)

            strict_gate = cls._strict_product_gate(
                by_name=by_name,
                contract=contract,
                plan=plan,
                diff_text=diff_text,
                changed_paths=changed_paths,
                product_paths=product_paths,
                target_role_scope=target_role_scope,
                browser_diagnostics=browser_diagnostics,
            )
            evidence["strict_product_gate"] = strict_gate.get("evidence") or {}
            issues.extend(strict_gate.get("issues") or [])
            if full_audit_required:
                issues.extend(cls._product_task_ledger_issues(by_name, implementation_plan=plan))

        for signature in repair_issue_signatures or []:
            if isinstance(signature, dict) and not signature.get("resolved"):
                add_issue(
                    "unresolved_repair_signature",
                    str(signature.get("check") or "repair"),
                    str(signature.get("signature") or "Unresolved repair signature."),
                    evidence_payload=signature,
                )

        if require_apply and run_status in {"completed", "blocked", "failed"} and apply_status != "applied":
            add_issue(
                "apply_gate",
                "apply_status",
                "Run must be applied after green checks.",
                evidence_payload={"apply_status": apply_status, "status": run_status},
            )

        checklist = cls._checklist(
            acceptance_required=acceptance_required,
            required_checks=required_checks,
            issues=issues,
            evidence=evidence,
            apply_status=apply_status,
            run_status=run_status,
            require_apply=require_apply,
        )
        blocking_reasons = [issue for issue in issues if issue.get("blocking", True)]
        next_forced_action = (
            {
                "action": "repair",
                "reason": blocking_reasons[0].get("details"),
                "check": blocking_reasons[0].get("check"),
                "issue_kind": blocking_reasons[0].get("kind"),
            }
            if blocking_reasons
            else {"action": "none", "reason": "Product readiness proof is green."}
        )
        return ProductReadinessResult(
            status="blocked" if blocking_reasons else "passed",
            acceptance_required=acceptance_required,
            required_checks=required_checks,
            checklist=checklist,
            evidence=evidence,
            blocking_reasons=blocking_reasons,
            next_forced_action=next_forced_action,
        )

    @staticmethod
    def _normalize_result(item: RunCheckResult | dict[str, Any]) -> dict[str, Any]:
        if isinstance(item, RunCheckResult):
            return item.model_dump(mode="json")
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")  # type: ignore[no-any-return]
        return dict(item) if isinstance(item, dict) else {}

    @staticmethod
    def _diagnostics(check: dict[str, Any]) -> dict[str, Any]:
        diagnostics = check.get("diagnostics")
        return dict(diagnostics) if isinstance(diagnostics, dict) else {}

    @staticmethod
    def _first_truthy(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _list_values(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    @staticmethod
    def _required_roles(*, contract: dict[str, Any], target_role_scope: list[str] | None) -> list[str]:
        raw = target_role_scope or contract.get("roles") or ROLE_ORDER
        roles = [str(role).strip().lower() for role in raw if str(role).strip().lower() in ROLE_ORDER]
        return list(dict.fromkeys(roles or list(ROLE_ORDER)))

    @staticmethod
    def _check_label(check_name: str) -> str:
        return {
            "api_workflow_smoke": "API workflow proof",
            "browser_flow_smoke": "Browser UI proof",
            "generated_app_python_tests": "Generated Python tests",
            "generated_app_js_tests": "Generated JS tests",
            "changed_files_static": "Changed files static check",
            "frontend_interaction_static_smoke": "Frontend/static interaction check",
            "platform_invariants": "Role route and platform invariants",
            "preview_boot_smoke": "Preview boot proof",
            "preview_connectivity_smoke": "Preview connectivity proof",
        }.get(check_name, check_name)

    @classmethod
    def _checklist(
        cls,
        *,
        acceptance_required: bool,
        required_checks: list[ProductReadinessCheck],
        issues: list[dict[str, Any]],
        evidence: dict[str, Any],
        apply_status: str | None,
        run_status: str | None,
        require_apply: bool,
    ) -> list[ProductReadinessCheck]:
        def blocked_by(*kinds: str) -> bool:
            return any(issue.get("kind") in kinds for issue in issues)

        required_by_key = {item.key: item for item in required_checks}
        api_ok = required_by_key.get("api_workflow_smoke")
        browser_ok = required_by_key.get("browser_flow_smoke")
        py_ok = required_by_key.get("generated_app_python_tests")
        js_ok = required_by_key.get("generated_app_js_tests")
        tests_passed = bool(py_ok and js_ok and py_ok.status == "passed" and js_ok.status == "passed")
        browser_evidence = evidence.get("browser") if isinstance(evidence.get("browser"), dict) else {}
        mobile_evidence = evidence.get("mobile") if isinstance(evidence.get("mobile"), dict) else {}
        strict_evidence = evidence.get("strict_product_gate") if isinstance(evidence.get("strict_product_gate"), dict) else {}
        items = [
            ProductReadinessCheck(
                key="backend_tests",
                label="Backend tests",
                status="blocked" if blocked_by("backend_tests_missing", "required_product_proof", "check_failure") else "passed" if acceptance_required else "not_required",
                check="api_workflow_smoke/generated_app_python_tests",
                details="Backend API workflow and generated Python tests must pass.",
                evidence=strict_evidence.get("backend_tests") or {},
            ),
            ProductReadinessCheck(
                key="frontend_static",
                label="Frontend/static checks",
                status="blocked" if blocked_by("frontend_static_missing", "static_check_missing", "check_failure") else "passed" if acceptance_required else "not_required",
                check="changed_files_static/frontend_interaction_static_smoke",
                details="Frontend and changed-file static checks must pass.",
                evidence=strict_evidence.get("frontend_static") or {},
            ),
            ProductReadinessCheck(
                key="api",
                label="API",
                status="passed" if api_ok and api_ok.status == "passed" and not blocked_by("persistence_proof_missing_marker") else "blocked" if acceptance_required else "not_required",
                check="api_workflow_smoke",
                details=None if not blocked_by("persistence_proof_missing_marker") else "API proof is missing persisted state evidence.",
                evidence=evidence.get("api") if isinstance(evidence.get("api"), dict) else {},
            ),
            ProductReadinessCheck(
                key="persistence",
                label="Persistence",
                status="blocked" if blocked_by("persistence_proof_missing_marker", "browser_proof_missing_persisted_marker", "browser_proof_missing_update_marker") else "passed" if acceptance_required else "not_required",
                check="api_workflow_smoke",
                details="API and browser proof must show persisted create/update state.",
                evidence={"api": evidence.get("api") or {}, "browser": browser_evidence},
            ),
            ProductReadinessCheck(
                key="ui",
                label="UI",
                status="passed" if browser_ok and browser_ok.status == "passed" and not blocked_by("browser_proof_missing_ui_steps", "browser_proof_runtime_errors", "browser_console_not_clean") else "blocked" if acceptance_required else "not_required",
                check="browser_flow_smoke",
                details="Browser proof needs concrete UI steps and no runtime errors.",
                evidence={"steps": browser_evidence.get("ui_steps") or []},
            ),
            ProductReadinessCheck(
                key="role_routes",
                label="Role routes",
                status="blocked" if blocked_by("role_routes_not_loaded", "platform_invariants_missing") else "passed" if acceptance_required else "not_required",
                check="platform_invariants",
                details="Every required role route must load.",
                evidence=strict_evidence.get("role_routes") or {},
            ),
            ProductReadinessCheck(
                key="roles",
                label="Roles",
                status="blocked" if blocked_by("browser_proof_missing_roles") else "passed" if acceptance_required else "not_required",
                check="browser_flow_smoke",
                details="Required role workflows must be visible in browser proof.",
                evidence={"required": browser_evidence.get("roles_required") or [], "checked": browser_evidence.get("roles_checked") or []},
            ),
            ProductReadinessCheck(
                key="mobile",
                label="Mobile",
                status="blocked" if blocked_by("mobile_layout", "mobile_layout_missing") else "passed" if acceptance_required else "not_required",
                check="browser_flow_smoke",
                details="Mobile preview must have a passing layout report.",
                evidence=mobile_evidence,
            ),
            ProductReadinessCheck(
                key="tests",
                label="Tests",
                status="passed" if tests_passed else "blocked" if acceptance_required else "not_required",
                check="generated_app_python_tests/generated_app_js_tests",
                details="Generated Python and JS tests must pass.",
                evidence={"generated_app_python_tests": (py_ok.evidence if py_ok else {}), "generated_app_js_tests": (js_ok.evidence if js_ok else {})},
            ),
            ProductReadinessCheck(
                key="prompt_requirements",
                label="Prompt requirements",
                status="blocked" if blocked_by("product_task_ledger", "prompt_requirements_missing") else "passed" if acceptance_required else "not_required",
                check="product_task_ledger",
                details="Prompt-derived product tasks and requirements must be covered.",
                evidence=strict_evidence.get("prompt_requirements") or {},
            ),
            ProductReadinessCheck(
                key="runtime_data",
                label="Runtime data",
                status="blocked" if blocked_by("placeholder_runtime_data") else "passed" if acceptance_required else "not_required",
                check="source_scan",
                details="Runtime source must not rely on placeholder/mock/demo data unless requested.",
                evidence=strict_evidence.get("runtime_data") or {},
            ),
            ProductReadinessCheck(
                key="diff_scope",
                label="Diff scope",
                status="blocked" if blocked_by("diff_scope") else "passed" if acceptance_required else "not_required",
                check="meaningful_product_diff",
                details="Diff must be scoped to product/runtime/tests/config files.",
                evidence=strict_evidence.get("diff_scope") or {},
            ),
            ProductReadinessCheck(
                key="preview_url",
                label="Preview URL",
                status="blocked" if blocked_by("preview_url_not_working") else "passed" if acceptance_required else "not_required",
                check="preview_boot_smoke/browser_flow_smoke",
                details="Preview must boot or browser proof must show a reachable preview.",
                evidence=strict_evidence.get("preview_url") or {},
            ),
            ProductReadinessCheck(
                key="apply_guardian",
                label="Apply/Guardian",
                status="passed" if not require_apply or run_status not in {"completed", "blocked", "failed"} or apply_status == "applied" else "blocked",
                check="apply_status",
                details="Applied source is required for terminal runs.",
                evidence={"apply_status": apply_status, "run_status": run_status},
            ),
        ]
        return items

    @classmethod
    def _strict_product_gate(
        cls,
        *,
        by_name: dict[str, dict[str, Any]],
        contract: dict[str, Any],
        plan: dict[str, Any],
        diff_text: str,
        changed_paths: list[str],
        product_paths: list[str],
        target_role_scope: list[str] | None,
        browser_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []

        def add(kind: str, check: str, details: str, evidence: dict[str, Any] | None = None) -> None:
            issues.append({"kind": kind, "check": check, "details": details, "blocking": True, "evidence": evidence or {}})

        def passed(check_name: str) -> bool:
            return str((by_name.get(check_name) or {}).get("status") or "") == "passed"

        backend_checks = ("api_workflow_smoke", "generated_app_python_tests")
        missing_backend = [check for check in backend_checks if not passed(check)]
        if missing_backend:
            add("backend_tests_missing", "backend_tests", "Backend/API proof is incomplete.", {"missing": missing_backend})

        missing_static = [check for check in STRICT_STATIC_CHECKS if not passed(check)]
        if missing_static:
            add("static_check_missing", "static_checks", "Static/frontend/platform checks must pass before completion.", {"missing": missing_static})

        missing_frontend = [check for check in ("frontend_interaction_static_smoke", "generated_app_js_tests") if not passed(check)]
        if missing_frontend:
            add("frontend_static_missing", "frontend_static", "Frontend/static proof is incomplete.", {"missing": missing_frontend})

        preview_ok = any(passed(check) for check in PREVIEW_CHECKS)
        preview_url = cls._preview_url_from_results(by_name, browser_diagnostics)
        if not preview_ok:
            add("preview_url_not_working", "preview_boot_smoke", "Preview URL has no passing boot/connectivity/browser proof.", {"preview_url": preview_url})

        runtime_errors = [
            *cls._list_values(browser_diagnostics.get("console_errors")),
            *cls._list_values(browser_diagnostics.get("page_errors")),
            *cls._list_values(browser_diagnostics.get("network_errors")),
            *cls._list_values(browser_diagnostics.get("visible_errors")),
        ]
        if runtime_errors:
            add("browser_console_not_clean", "browser_flow_smoke", "Browser console/network/visible errors must be empty.", {"errors": runtime_errors[:12]})

        role_coverage = cls._role_coverage_from_platform_invariants(by_name)
        required_roles = cls._required_roles(contract=contract, target_role_scope=target_role_scope)
        missing_roles: list[str] = []
        for role in required_roles:
            coverage = role_coverage.get(role) if isinstance(role_coverage.get(role), dict) else {}
            status = str(coverage.get("status") or "")
            try:
                route_count = int(coverage.get("route_count") or 0)
            except (TypeError, ValueError):
                route_count = 0
            if status != "present" or route_count < 1:
                missing_roles.append(role)
        if missing_roles:
            add("role_routes_not_loaded", "platform_invariants", "One or more required role routes did not load.", {"missing_roles": missing_roles, "role_coverage": role_coverage})

        issues.extend(cls._product_task_ledger_issues(by_name, implementation_plan=plan))
        if plan.get("product_task_ledger") and not any(str((by_name.get(check) or {}).get("status") or "") == "passed" for check in ("platform_invariants", "browser_flow_smoke")):
            add("prompt_requirements_missing", "product_task_ledger", "Prompt-derived product task ledger has no passing route/browser proof.", {"task_count": len(plan.get("product_task_ledger") or [])})

        if not cls._placeholder_data_allowed(contract=contract, plan=plan):
            placeholder_hits = cls._placeholder_hits(diff_text)
            if placeholder_hits:
                add("placeholder_runtime_data", "source_scan", "Runtime diff contains placeholder/mock/demo data.", {"hits": placeholder_hits[:8]})

        out_of_scope = [path for path in changed_paths if not cls._is_allowed_final_gate_path(path)]
        if out_of_scope:
            add("diff_scope", "meaningful_product_diff", "Diff touches files outside the final product acceptance scope.", {"paths": out_of_scope[:20], "product_paths": product_paths[:20]})

        return {
            "issues": issues,
            "evidence": {
                "backend_tests": {check: by_name.get(check) or {"status": "missing"} for check in backend_checks},
                "frontend_static": {check: by_name.get(check) or {"status": "missing"} for check in ("changed_files_static", "frontend_interaction_static_smoke", "generated_app_js_tests")},
                "role_routes": {"required_roles": required_roles, "role_coverage": role_coverage},
                "prompt_requirements": {"task_count": len(plan.get("product_task_ledger") or []), "has_plan": bool(plan)},
                "runtime_data": {"placeholder_allowed": cls._placeholder_data_allowed(contract=contract, plan=plan)},
                "diff_scope": {"changed_paths": changed_paths[:80], "product_paths": product_paths[:80], "out_of_scope": out_of_scope[:20]},
                "preview_url": {"preview_url": preview_url, "preview_ok": preview_ok},
            },
        }

    @classmethod
    def _product_task_ledger_issues(
        cls,
        by_name: dict[str, dict[str, Any]],
        *,
        implementation_plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ledger = implementation_plan.get("product_task_ledger") if isinstance(implementation_plan.get("product_task_ledger"), list) else []
        if not ledger:
            return []
        role_coverage = cls._role_coverage_from_platform_invariants(by_name)
        if not role_coverage:
            return []
        issues: list[dict[str, Any]] = []
        for item in ledger:
            if not isinstance(item, dict) or str(item.get("kind") or "") not in {"source", "update", "observer", "participant"}:
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in ROLE_ORDER:
                continue
            try:
                expected_routes = max(1, int(item.get("expected_min_routes") or 1))
            except (TypeError, ValueError):
                expected_routes = 1
            coverage = role_coverage.get(role) if isinstance(role_coverage.get(role), dict) else {}
            status = str(coverage.get("status") or "").strip()
            try:
                route_count = int(coverage.get("route_count") or 0)
            except (TypeError, ValueError):
                route_count = 0
            if status == "present" and route_count >= expected_routes:
                continue
            issues.append(
                {
                    "kind": "product_task_ledger",
                    "check": "product_task_ledger",
                    "details": (
                        f"{role} ledger item {item.get('id') or role} is incomplete: "
                        f"expected at least {expected_routes} routeable product page(s), got {route_count} with status {status or 'missing'}."
                    ),
                    "role": role,
                    "ledger_item_id": item.get("id"),
                    "expected_min_routes": expected_routes,
                    "actual_route_count": route_count,
                    "coverage_status": status,
                    "target_files": list(item.get("owned_paths") or []),
                    "expected_proof": list(item.get("proof_checks") or []),
                    "blocking": True,
                    "evidence": {"role_coverage": role_coverage},
                }
            )
        return issues

    @staticmethod
    def _role_coverage_from_platform_invariants(by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
        diagnostics = by_name.get("platform_invariants", {}).get("diagnostics")
        if not isinstance(diagnostics, dict):
            return {}
        role_coverage = diagnostics.get("role_coverage")
        return dict(role_coverage) if isinstance(role_coverage, dict) else {}

    @classmethod
    def _preview_url_from_results(cls, by_name: dict[str, dict[str, Any]], browser_diagnostics: dict[str, Any]) -> str | None:
        for payload in [browser_diagnostics, *(cls._diagnostics(by_name.get(check) or {}) for check in PREVIEW_CHECKS)]:
            for key in ("preview_url", "url", "base_url"):
                value = payload.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _placeholder_data_allowed(*, contract: dict[str, Any], plan: dict[str, Any]) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                contract.get("allow_placeholder_data"),
                contract.get("allow_mock_data"),
                contract.get("requested_mock_data"),
                plan.get("allow_placeholder_data"),
                plan.get("allow_mock_data"),
            )
        ).lower()
        return any(token in text for token in ("true", "requested", "allowed", "explicit"))

    @staticmethod
    def _placeholder_hits(diff_text: str) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for line_no, line in enumerate(str(diff_text or "").splitlines(), start=1):
            if not line.startswith("+") or line.startswith("+++"):
                continue
            match = PLACEHOLDER_DATA_RE.search(line)
            if match:
                hits.append({"line": line_no, "pattern": match.group(0), "excerpt": line[:220]})
        return hits

    @classmethod
    def _is_allowed_final_gate_path(cls, file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return True
        if cls._is_product_runtime_source_path(normalized):
            return True
        if normalized.startswith(("miniapp/tests/", "tests/", "runtime/templates/", "docker/")):
            return True
        if normalized in {"miniapp/requirements.txt", "miniapp/package.json", "miniapp/package-lock.json", "miniapp/pyproject.toml"}:
            return True
        return False

    @classmethod
    def _paths_from_diff(cls, diff_text: str) -> list[str]:
        paths: list[str] = []
        for match in re.finditer(r"^diff --git a/.+ b/(.+)$", str(diff_text or ""), flags=re.MULTILINE):
            path = match.group(1).strip()
            if path.startswith("draft/"):
                path = path.split("draft/", 1)[-1]
            if path.startswith("source/"):
                path = path.split("source/", 1)[-1]
            paths.append(path)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _is_product_runtime_source_path(file_path: str) -> bool:
        normalized = str(file_path or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized.startswith("miniapp/"):
            return False
        path = PurePosixPath(normalized)
        if any(part in {"__pycache__", "node_modules", "dist", "build", ".cache"} for part in path.parts):
            return False
        if normalized.startswith("miniapp/tests/") or normalized.startswith("miniapp/app/generated/"):
            return False
        if normalized.endswith((".pyc", ".pyo", ".tsbuildinfo")):
            return False
        return normalized.startswith(("miniapp/app/", "miniapp/requirements.txt", "miniapp/Dockerfile"))
