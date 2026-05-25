from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import urlparse

from app.models.domain import RunCheckResult


TRACEABILITY_STAGES = ("requirement", "route_page", "api", "state", "test", "browser_proof")
ROLE_NAMES = ("client", "specialist", "manager")
GENERATED_TEST_CHECKS = ("generated_app_python_tests", "generated_app_js_tests")


class RequirementTraceabilityMatrix:
    """Maps every prompt-derived requirement to concrete production proof layers."""

    @classmethod
    def build(
        cls,
        *,
        run: Any,
        artifacts: dict[str, Any] | None = None,
        browser_proof: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_payload = dict(artifacts or {})
        contract = cls._dict_attr(run, "acceptance_contract")
        implementation_plan = cls._dict_attr(run, "implementation_plan")
        check_results = [cls._normalize_result(item) for item in (artifact_payload.get("check_results") or [])]
        evidence = cls._evidence_catalog(
            run=run,
            artifacts=artifact_payload,
            check_results=check_results,
            browser_proof=browser_proof if isinstance(browser_proof, dict) else {},
        )
        acceptance_required = cls._acceptance_required(run, contract)
        requirements = cls._requirements(run=run, contract=contract, implementation_plan=implementation_plan)
        if acceptance_required and not requirements:
            requirements = [cls._fallback_requirement(run=run, contract=contract)]

        rows = [
            cls._row_for_requirement(requirement=requirement, evidence=evidence, required=acceptance_required)
            for requirement in requirements
        ]
        blocking_reasons = [
            cls._blocking_reason(row)
            for row in rows
            if acceptance_required and row.get("blocking")
        ]
        passed = sum(1 for row in rows if row.get("status") == "passed")
        blocked = sum(1 for row in rows if row.get("blocking"))
        missing_stage_count = sum(len(row.get("missing") or []) for row in rows)
        status = (
            "blocked"
            if blocking_reasons
            else "passed"
            if acceptance_required
            else "not_required"
        )
        run_id = str(cls._get_attr(run, "run_id") or artifact_payload.get("run_id") or "")
        workspace_id = str(cls._get_attr(run, "workspace_id") or artifact_payload.get("workspace_id") or "")
        return {
            "schema": "grounded.requirement_traceability_matrix.v1",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "status": status,
            "required": acceptance_required,
            "stages": list(TRACEABILITY_STAGES),
            "coverage": {
                "total": len(rows),
                "passed": passed,
                "blocked": blocked if acceptance_required else 0,
                "missing": missing_stage_count,
            },
            "rows": rows,
            "blocking_reasons": blocking_reasons,
            "evidence_summary": cls._evidence_summary(evidence),
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}" if run_id else None,
                "browser_proof": cls._get_attr(run, "browser_proof_ref"),
                "requirement_traceability": f"requirement_traceability:{run_id}" if run_id else None,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def blocking_issues(cls, report: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in report.get("blocking_reasons") or [] if isinstance(item, dict)]

    @classmethod
    def _row_for_requirement(cls, *, requirement: dict[str, Any], evidence: dict[str, Any], required: bool) -> dict[str, Any]:
        route_stage = cls._route_stage(requirement, evidence)
        api_stage = cls._api_stage(requirement, evidence)
        state_stage = cls._state_stage(requirement, evidence)
        test_stage = cls._test_stage(requirement, evidence)
        browser_stage = cls._browser_stage(requirement, evidence)
        stage_map = {
            "requirement": {
                "status": "passed",
                "source": requirement.get("source"),
                "text": requirement.get("requirement"),
                "evidence": {"id": requirement.get("requirement_id")},
            },
            "route_page": route_stage,
            "api": api_stage,
            "state": state_stage,
            "test": test_stage,
            "browser_proof": browser_stage,
        }
        missing = [
            stage
            for stage in TRACEABILITY_STAGES
            if stage != "requirement" and (stage_map.get(stage) or {}).get("status") != "passed"
        ]
        blocking = bool(required and missing)
        return {
            "requirement_id": requirement.get("requirement_id"),
            "requirement": requirement.get("requirement"),
            "source": requirement.get("source"),
            "status": "blocked" if blocking else "passed" if not missing else "incomplete",
            "blocking": blocking,
            "expected": requirement.get("expected") or {},
            **stage_map,
            "missing": missing,
        }

    @classmethod
    def _route_stage(cls, requirement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        expected = cls._string_list((requirement.get("expected") or {}).get("routes"))
        routes = cls._string_list(evidence.get("routes"))
        pages = cls._string_list(evidence.get("pages"))
        route_set = {cls._normalize_route(item) for item in routes}
        missing_expected = [route for route in expected if cls._normalize_route(route) not in route_set]
        status = "passed" if (expected and not missing_expected) or (not expected and (routes or pages)) else "missing"
        return {
            "status": status,
            "routes": routes[:40],
            "pages": pages[:40],
            "expected_routes": expected,
            "missing_routes": missing_expected,
            "evidence": {
                "source": evidence.get("route_sources") or [],
                "touched_files": evidence.get("changed_files") or [],
            },
        }

    @classmethod
    def _api_stage(cls, requirement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        expected = cls._string_list((requirement.get("expected") or {}).get("api_paths"))
        api = evidence.get("api") if isinstance(evidence.get("api"), dict) else {}
        paths = cls._string_list(api.get("paths"))
        path_set = {cls._normalize_route(item) for item in paths}
        missing_expected = [path for path in expected if cls._normalize_route(path) not in path_set]
        api_passed = api.get("status") == "passed"
        has_api_proof = api_passed and (paths or api.get("state_after_present") or api.get("persisted_marker"))
        status = "passed" if (expected and not missing_expected) or (not expected and has_api_proof) else "missing"
        return {
            "status": status,
            "paths": paths[:40],
            "expected_paths": expected,
            "missing_paths": missing_expected,
            "evidence": {
                "check": "api_workflow_smoke",
                "check_status": api.get("status"),
                "persisted_marker": api.get("persisted_marker"),
                "state_after_present": bool(api.get("state_after_present")),
            },
        }

    @classmethod
    def _state_stage(cls, requirement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        expected = cls._string_list((requirement.get("expected") or {}).get("state_fields"))
        state = evidence.get("state") if isinstance(evidence.get("state"), dict) else {}
        fields = cls._string_list(state.get("fields"))
        markers = cls._string_list(state.get("markers"))
        field_set = {cls._field_key(item) for item in fields}
        matched_fields = [field for field in expected if cls._field_key(field) in field_set]
        has_state_proof = bool(markers or fields or state.get("state_after_present"))
        status = "passed" if has_state_proof else "missing"
        return {
            "status": status,
            "fields": fields[:80],
            "markers": markers[:20],
            "expected_fields": expected,
            "matched_fields": matched_fields,
            "evidence": {
                "api_after_present": bool(state.get("api_after_present")),
                "browser_marker_present": bool(state.get("browser_marker_present")),
            },
        }

    @classmethod
    def _test_stage(cls, requirement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        expected = cls._string_list((requirement.get("expected") or {}).get("tests"))
        tests = evidence.get("tests") if isinstance(evidence.get("tests"), dict) else {}
        passed_checks = cls._string_list(tests.get("passed_checks"))
        generated_required = [name for name in GENERATED_TEST_CHECKS if name in tests.get("all_check_names", [])]
        has_generated_pair = all(name in passed_checks for name in GENERATED_TEST_CHECKS)
        has_test_proof = has_generated_pair or (bool(expected) and bool(passed_checks)) or any("test" in item for item in passed_checks)
        status = "passed" if has_test_proof else "missing"
        return {
            "status": status,
            "checks": passed_checks[:40],
            "expected_tests": expected,
            "evidence": {
                "generated_required": generated_required,
                "generated_pair_passed": has_generated_pair,
                "test_count": len(passed_checks),
            },
        }

    @classmethod
    def _browser_stage(cls, requirement: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        browser = evidence.get("browser") if isinstance(evidence.get("browser"), dict) else {}
        steps = browser.get("steps") if isinstance(browser.get("steps"), list) else []
        screenshots = cls._string_list(browser.get("screenshots"))
        status = "passed" if browser.get("status") == "passed" and steps else "missing"
        return {
            "status": status,
            "steps": steps[:20],
            "screenshots": screenshots[:20],
            "evidence": {
                "check": "browser_flow_smoke",
                "check_status": browser.get("check_status"),
                "proof_status": browser.get("status"),
                "roles_checked": browser.get("roles_checked") or [],
            },
        }

    @classmethod
    def _blocking_reason(cls, row: dict[str, Any]) -> dict[str, Any]:
        missing = cls._string_list(row.get("missing"))
        details = f"Requirement {row.get('requirement_id') or 'requirement'} is missing proof for: {', '.join(missing)}."
        return {
            "kind": "requirement_traceability",
            "check": f"traceability:{row.get('requirement_id') or 'requirement'}",
            "details": details,
            "blocking": True,
            "evidence": {
                "requirement_id": row.get("requirement_id"),
                "requirement": row.get("requirement"),
                "missing": missing,
                "expected": row.get("expected") or {},
            },
        }

    @classmethod
    def _requirements(cls, *, run: Any, contract: dict[str, Any], implementation_plan: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, flow in enumerate(contract.get("flows") or [], start=1):
            if not isinstance(flow, dict):
                continue
            requirement_id = str(flow.get("id") or flow.get("key") or cls._slug(flow.get("name") or flow.get("title") or f"flow_{index}"))
            rows.append(
                {
                    "requirement_id": requirement_id,
                    "requirement": cls._requirement_text(flow, fallback=f"Flow {index} must work end to end."),
                    "source": "acceptance_contract.flows",
                    "expected": cls._expected_from_flow(flow, contract=contract, run=run),
                }
            )
        if rows:
            return rows

        ledger = implementation_plan.get("product_task_ledger") if isinstance(implementation_plan.get("product_task_ledger"), list) else []
        for index, item in enumerate(ledger, start=1):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or item.get("description") or item.get("intent") or "").strip()
            requirement = content or f"{role or 'Product'} ledger item {item.get('id') or index} must be implemented and verified."
            rows.append(
                {
                    "requirement_id": str(item.get("id") or f"ledger_{index}"),
                    "requirement": requirement,
                    "source": "implementation_plan.product_task_ledger",
                    "expected": {
                        "routes": cls._expected_routes_from_ledger(item, run),
                        "api_paths": cls._api_paths_from_payload(item),
                        "state_fields": cls._string_list(item.get("fields") or item.get("state_fields")),
                        "tests": cls._string_list(item.get("proof_checks") or item.get("required_tests")),
                    },
                }
            )
        if rows:
            return rows

        return [cls._fallback_requirement(run=run, contract=contract)] if cls._has_requirement_signal(run, contract) else []

    @classmethod
    def _fallback_requirement(cls, *, run: Any, contract: dict[str, Any]) -> dict[str, Any]:
        prompt = str(cls._get_attr(run, "prompt") or "").strip()
        prompt_text = cls._first_prompt_sentence(prompt) or "Prompt-owned workflow must be implemented and verified end to end."
        api_contract = contract.get("api_contract") if isinstance(contract.get("api_contract"), dict) else {}
        return {
            "requirement_id": "req_prompt_core_workflow",
            "requirement": prompt_text,
            "source": "prompt",
            "expected": {
                "routes": cls._routes_from_roles(cls._get_attr(run, "target_role_scope") or contract.get("roles") or []),
                "api_paths": cls._api_paths_from_payload(contract),
                "state_fields": cls._string_list(api_contract.get("field_hints") or contract.get("field_hints")),
                "tests": list(GENERATED_TEST_CHECKS),
            },
        }

    @classmethod
    def _expected_from_flow(cls, flow: dict[str, Any], *, contract: dict[str, Any], run: Any) -> dict[str, Any]:
        routes = cls._string_list(flow.get("routes") or flow.get("pages") or flow.get("route") or flow.get("page"))
        role = str(flow.get("role") or "").strip().lower()
        if role in ROLE_NAMES:
            routes.append(f"/{role}")
        for step in flow.get("steps") or []:
            if isinstance(step, dict):
                routes.extend(cls._string_list(step.get("route") or step.get("page") or step.get("path")))
        return {
            "routes": cls._dedupe(routes),
            "api_paths": cls._api_paths_from_payload(flow) or cls._api_paths_from_payload(contract),
            "state_fields": cls._string_list(
                flow.get("state_fields")
                or flow.get("db_fields")
                or flow.get("fields")
                or flow.get("persisted_fields")
            ),
            "tests": cls._string_list(flow.get("required_tests") or flow.get("tests") or contract.get("test_requirements")),
        }

    @classmethod
    def _expected_routes_from_ledger(cls, item: dict[str, Any], run: Any) -> list[str]:
        routes = cls._string_list(item.get("routes") or item.get("pages") or item.get("route"))
        role = str(item.get("role") or "").strip().lower()
        if role in ROLE_NAMES:
            routes.append(f"/{role}")
        if not routes:
            routes.extend(cls._routes_from_roles(cls._get_attr(run, "target_role_scope") or []))
        return cls._dedupe(routes)

    @classmethod
    def _evidence_catalog(
        cls,
        *,
        run: Any,
        artifacts: dict[str, Any],
        check_results: list[dict[str, Any]],
        browser_proof: dict[str, Any],
    ) -> dict[str, Any]:
        by_name = {str(item.get("name") or ""): item for item in check_results}
        api_check = by_name.get("api_workflow_smoke") or {}
        browser_check = by_name.get("browser_flow_smoke") or {}
        api_diagnostics = cls._dict(api_check.get("diagnostics"))
        browser_diagnostics = cls._dict(browser_check.get("diagnostics"))
        diff_text = str(artifacts.get("diff") or "")
        changed_files = cls._dedupe([*cls._paths_from_diff(diff_text), *cls._string_list(cls._get_attr(run, "touched_files"))])
        routes, route_sources = cls._route_evidence(
            run=run,
            artifacts=artifacts,
            changed_files=changed_files,
            browser_diagnostics=browser_diagnostics,
            browser_proof=browser_proof,
        )
        api_paths = cls._api_evidence_paths(api_check, api_diagnostics)
        state_markers = cls._dedupe(
            [
                *cls._state_markers(api_diagnostics),
                *cls._state_markers(browser_diagnostics),
                *cls._state_markers(browser_proof),
            ]
        )
        state_fields = cls._dedupe(
            [
                *cls._fields_from_state_payload(api_diagnostics.get("api_after")),
                *cls._fields_from_state_payload(api_diagnostics.get("state_after")),
                *cls._string_list(api_diagnostics.get("state_fields") or api_diagnostics.get("fields")),
            ]
        )
        browser_steps = cls._first_list(
            browser_proof.get("steps"),
            browser_proof.get("ui_steps"),
            browser_diagnostics.get("ui_steps"),
            browser_diagnostics.get("steps"),
            artifacts.get("browser_proof_steps"),
        )
        browser_screenshots = cls._dedupe(
            [
                *cls._collect_values(browser_proof, ("screenshot", "screenshot_path", "image_path", "screenshots")),
                *cls._collect_values(browser_diagnostics, ("screenshot", "screenshot_path", "image_path", "screenshots")),
            ]
        )
        passed_checks = [
            str(item.get("name"))
            for item in check_results
            if str(item.get("status") or "") == "passed" and str(item.get("name") or "")
        ]
        return {
            "changed_files": changed_files,
            "routes": routes,
            "route_sources": route_sources,
            "pages": cls._page_evidence(changed_files),
            "api": {
                "status": str(api_check.get("status") or ""),
                "paths": api_paths,
                "persisted_marker": state_markers[0] if state_markers else None,
                "state_after_present": bool(api_diagnostics.get("api_after") or api_diagnostics.get("state_after")),
            },
            "state": {
                "markers": state_markers,
                "fields": state_fields,
                "api_after_present": bool(api_diagnostics.get("api_after")),
                "state_after_present": bool(api_diagnostics.get("state_after")),
                "browser_marker_present": bool(cls._state_markers(browser_diagnostics) or cls._state_markers(browser_proof)),
            },
            "tests": {
                "passed_checks": passed_checks,
                "all_check_names": [str(item.get("name")) for item in check_results if str(item.get("name") or "")],
            },
            "browser": {
                "status": str(browser_proof.get("status") or browser_check.get("status") or ""),
                "check_status": str(browser_check.get("status") or ""),
                "steps": browser_steps,
                "screenshots": browser_screenshots,
                "roles_checked": cls._dedupe(
                    [
                        *cls._string_list(browser_proof.get("roles_checked")),
                        *cls._string_list(browser_diagnostics.get("roles_checked")),
                    ]
                ),
            },
        }

    @classmethod
    def _route_evidence(
        cls,
        *,
        run: Any,
        artifacts: dict[str, Any],
        changed_files: list[str],
        browser_diagnostics: dict[str, Any],
        browser_proof: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        routes: list[str] = []
        sources: list[str] = []
        preview = artifacts.get("preview") if isinstance(artifacts.get("preview"), dict) else {}
        role_urls = preview.get("role_urls") if isinstance(preview.get("role_urls"), dict) else {}
        for key, value in role_urls.items():
            for route in cls._string_list([key, value]):
                extracted = cls._extract_route(route)
                if extracted:
                    routes.append(extracted)
                    sources.append("preview.role_urls")
        for payload_name, payload in (
            ("browser_diagnostics", browser_diagnostics),
            ("browser_proof", browser_proof),
            ("flow_coverage", cls._dict_attr(run, "flow_coverage")),
        ):
            for route in cls._routes_from_nested(payload):
                routes.append(route)
                sources.append(payload_name)
        roles = cls._dedupe(
            [
                *cls._string_list(browser_diagnostics.get("roles_checked")),
                *cls._string_list(browser_proof.get("roles_checked")),
                *cls._string_list(cls._get_attr(run, "target_role_scope")),
            ]
        )
        for route in cls._routes_from_roles(roles):
            routes.append(route)
            sources.append("roles_checked")
        for path in changed_files:
            role_route = cls._route_from_file(path)
            if role_route:
                routes.append(role_route)
                sources.append("changed_files")
        return cls._dedupe([cls._normalize_route(item) for item in routes if item]), cls._dedupe(sources)

    @classmethod
    def _api_evidence_paths(cls, api_check: dict[str, Any], diagnostics: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        paths.extend(cls._api_paths_from_payload(diagnostics))
        for step in diagnostics.get("steps") or diagnostics.get("api_steps") or []:
            if isinstance(step, dict):
                paths.extend(cls._api_paths_from_payload(step))
        for value in [api_check.get("details"), api_check.get("command"), *(api_check.get("logs") or [])]:
            paths.extend(re.findall(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/api/[^\s,'\")]+)", str(value or ""), flags=re.IGNORECASE))
            paths.extend(re.findall(r"(/api/[a-zA-Z0-9_./:-]+)", str(value or "")))
        return cls._dedupe([cls._normalize_route(path) for path in paths if path])

    @classmethod
    def _api_paths_from_payload(cls, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        raw: list[Any] = []
        for key in ("api_paths", "paths", "endpoints", "required_endpoints", "api_path", "path", "endpoint", "url"):
            if key in payload:
                raw.extend(cls._string_list(payload.get(key)))
        for item in payload.get("api") or []:
            raw.extend(cls._string_list(item))
        paths: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                paths.extend(cls._api_paths_from_payload(item))
                continue
            text = str(item or "").strip()
            if not text:
                continue
            match = re.search(r"(/api/[a-zA-Z0-9_./:-]+)", text)
            if match:
                paths.append(match.group(1))
            elif text.startswith("/"):
                paths.append(text)
        return cls._dedupe(paths)

    @classmethod
    def _routes_from_nested(cls, payload: Any) -> list[str]:
        routes: list[str] = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in {"route", "routes", "page", "pages", "path", "url", "route_coverage"}:
                    for item in cls._string_list(value):
                        extracted = cls._extract_route(item)
                        if extracted and not extracted.startswith("/api/"):
                            routes.append(extracted)
                elif key in {"steps", "ui_steps", "scenarios", "screens", "flows"} and isinstance(value, list):
                    for child in value:
                        routes.extend(cls._routes_from_nested(child))
                elif isinstance(value, dict):
                    routes.extend(cls._routes_from_nested(value))
        elif isinstance(payload, list):
            for item in payload:
                routes.extend(cls._routes_from_nested(item))
        return cls._dedupe(routes)

    @staticmethod
    def _route_from_file(path: str) -> str | None:
        normalized = str(path or "").replace("\\", "/")
        match = re.search(r"miniapp/app/static/(client|specialist|manager)(?:/|$)", normalized)
        if match:
            return f"/{match.group(1)}"
        return None

    @classmethod
    def _page_evidence(cls, changed_files: list[str]) -> list[str]:
        pages = []
        for path in changed_files:
            normalized = str(path or "").replace("\\", "/")
            if normalized.startswith("miniapp/app/static/") or normalized.startswith("miniapp/app/templates/") or normalized.startswith("miniapp/app/routes/"):
                pages.append(normalized)
        return cls._dedupe(pages)

    @staticmethod
    def _extract_route(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("http://") or text.startswith("https://"):
            parsed = urlparse(text)
            return parsed.path or "/"
        if text.startswith("/"):
            return text
        return None

    @classmethod
    def _state_markers(cls, payload: dict[str, Any]) -> list[str]:
        markers: list[str] = []
        for key in (
            "persisted_state_marker",
            "persisted_marker",
            "created_state_marker",
            "created_marker",
            "updated_state_marker",
            "updated_marker",
            "update_state_marker",
            "update_marker",
        ):
            markers.extend(cls._string_list(payload.get(key)))
        return cls._dedupe(markers)

    @classmethod
    def _fields_from_state_payload(cls, payload: Any) -> list[str]:
        fields: list[str] = []
        if isinstance(payload, dict):
            fields.extend(str(key) for key in payload.keys())
            for value in payload.values():
                fields.extend(cls._fields_from_state_payload(value))
        elif isinstance(payload, list):
            for item in payload:
                fields.extend(cls._fields_from_state_payload(item))
        return cls._dedupe(fields)

    @staticmethod
    def _normalize_result(item: RunCheckResult | dict[str, Any]) -> dict[str, Any]:
        if isinstance(item, RunCheckResult):
            return item.model_dump(mode="json")
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return dict(item) if isinstance(item, dict) else {}

    @classmethod
    def _acceptance_required(cls, run: Any, contract: dict[str, Any]) -> bool:
        mode = str(cls._get_attr(run, "mode") or "").lower()
        generation = str(getattr(cls._get_attr(run, "generation_mode"), "value", cls._get_attr(run, "generation_mode")) or "").lower()
        return bool(contract.get("required")) or mode in {"generate", "fix"} or generation in {"quality", "balanced"}

    @classmethod
    def _has_requirement_signal(cls, run: Any, contract: dict[str, Any]) -> bool:
        return bool(contract.get("required") or cls._get_attr(run, "prompt") or contract.get("prompt_hints") or contract.get("api_contract"))

    @classmethod
    def _requirement_text(cls, payload: dict[str, Any], *, fallback: str) -> str:
        for key in ("requirement", "name", "title", "description", "summary"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value[:500]
        if payload.get("requirements"):
            return cls._compact_json(payload.get("requirements"))[:500]
        if payload.get("steps"):
            return cls._compact_json(payload.get("steps"))[:500]
        return fallback

    @staticmethod
    def _first_prompt_sentence(prompt: str) -> str:
        cleaned = re.sub(r"\s+", " ", prompt or "").strip()
        if not cleaned:
            return ""
        match = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)
        return match[0][:500]

    @classmethod
    def _routes_from_roles(cls, roles: Any) -> list[str]:
        return [f"/{role}" for role in cls._string_list(roles) if role in ROLE_NAMES]

    @staticmethod
    def _normalize_route(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        route = RequirementTraceabilityMatrix._extract_route(text) or text
        if not route.startswith("/"):
            route = f"/{route}"
        return route.rstrip("/") or "/"

    @staticmethod
    def _field_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9а-яё]+", "", str(value or "").lower())

    @staticmethod
    def _slug(value: Any) -> str:
        text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
        return text or "requirement"

    @staticmethod
    def _dict(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @classmethod
    def _dict_attr(cls, run: Any, name: str) -> dict[str, Any]:
        return cls._dict(cls._get_attr(run, name))

    @staticmethod
    def _get_attr(run: Any, name: str) -> Any:
        if isinstance(run, dict):
            return run.get(name)
        return getattr(run, name, None)

    @classmethod
    def _string_list(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, tuple | set):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, dict):
            text = str(value.get("path") or value.get("route") or value.get("name") or value.get("id") or "").strip()
            return [text] if text else []
        if value in (None, ""):
            return []
        return [str(value).strip()]

    @staticmethod
    def _first_list(*values: Any) -> list[Any]:
        for value in values:
            if isinstance(value, list) and value:
                return list(value)
        return []

    @classmethod
    def _dedupe(cls, values: list[Any]) -> list[Any]:
        output: list[Any] = []
        seen: set[str] = set()
        for value in values:
            if value in (None, ""):
                continue
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, dict | list) else str(value)
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    @classmethod
    def _collect_values(cls, payload: Any, keys: tuple[str, ...]) -> list[str]:
        values: list[str] = []
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys:
                    values.extend(cls._string_list(value))
                elif isinstance(value, dict | list):
                    values.extend(cls._collect_values(value, keys))
        elif isinstance(payload, list):
            for item in payload:
                values.extend(cls._collect_values(item, keys))
        return values

    @staticmethod
    def _paths_from_diff(diff_text: str) -> list[str]:
        paths: list[str] = []
        for line in str(diff_text or "").splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    paths.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
            elif line.startswith("+++ b/"):
                paths.append(line[6:])
        return list(dict.fromkeys(paths))

    @classmethod
    def _evidence_summary(cls, evidence: dict[str, Any]) -> dict[str, Any]:
        api = evidence.get("api") if isinstance(evidence.get("api"), dict) else {}
        state = evidence.get("state") if isinstance(evidence.get("state"), dict) else {}
        browser = evidence.get("browser") if isinstance(evidence.get("browser"), dict) else {}
        tests = evidence.get("tests") if isinstance(evidence.get("tests"), dict) else {}
        return {
            "routes": cls._string_list(evidence.get("routes"))[:20],
            "api_paths": cls._string_list(api.get("paths"))[:20],
            "state_markers": cls._string_list(state.get("markers"))[:20],
            "state_fields": cls._string_list(state.get("fields"))[:20],
            "test_checks": cls._string_list(tests.get("passed_checks"))[:20],
            "browser_steps": len(browser.get("steps") or []),
            "browser_screenshots": len(browser.get("screenshots") or []),
        }

    @staticmethod
    def _compact_json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            return str(value)


class PromptArtifactCompletionAudit:
    """Final prompt-to-artifact audit used by readiness gates."""

    @classmethod
    def build(
        cls,
        *,
        run: Any,
        artifacts: dict[str, Any] | None = None,
        traceability: dict[str, Any] | None = None,
        browser_proof: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_payload = dict(artifacts or {})
        matrix = traceability if isinstance(traceability, dict) else RequirementTraceabilityMatrix.build(
            run=run,
            artifacts=artifact_payload,
            browser_proof=browser_proof if isinstance(browser_proof, dict) else {},
        )
        rows = matrix.get("rows") if isinstance(matrix.get("rows"), list) else []
        check_results = [RequirementTraceabilityMatrix._normalize_result(item) for item in artifact_payload.get("check_results") or []]
        checks_by_name = {str(item.get("name") or ""): item for item in check_results if item.get("name")}
        changed_files = cls._changed_files(run=run, artifacts=artifact_payload, matrix=matrix)
        audit_rows = [
            cls._audit_row(row=row, changed_files=changed_files, checks_by_name=checks_by_name)
            for row in rows
            if isinstance(row, dict)
        ]
        required = bool(matrix.get("required"))
        uncovered = [
            {
                "requirement_id": row.get("requirement_id"),
                "requirement": row.get("requirement"),
                "uncovered": list(row.get("uncovered") or []),
                "details": row.get("uncovered_details") or [],
            }
            for row in audit_rows
            if row.get("uncovered")
        ]
        covered_count = sum(1 for row in audit_rows if row.get("status") == "passed")
        status = "blocked" if required and uncovered else "passed" if required else "not_required"
        run_id = str(RequirementTraceabilityMatrix._get_attr(run, "run_id") or artifact_payload.get("run_id") or "")
        workspace_id = str(RequirementTraceabilityMatrix._get_attr(run, "workspace_id") or artifact_payload.get("workspace_id") or "")
        return {
            "schema": "grounded.prompt_completion_audit.v1",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "status": status,
            "required": required,
            "prompt": str(RequirementTraceabilityMatrix._get_attr(run, "prompt") or "")[:1200],
            "requirement_count": len(audit_rows),
            "covered_count": covered_count,
            "uncovered_count": len(uncovered),
            "rows": audit_rows,
            "uncovered": uncovered,
            "changed_files": changed_files,
            "proof_summary": cls._proof_summary(checks_by_name=checks_by_name, rows=audit_rows),
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run_id}" if run_id else None,
                "requirement_traceability": f"requirement_traceability:{run_id}" if run_id else None,
                "prompt_completion_audit": f"prompt_completion_audit:{run_id}" if run_id else None,
                "browser_proof": RequirementTraceabilityMatrix._get_attr(run, "browser_proof_ref"),
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def blocking_issues(cls, report: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for item in report.get("uncovered") or []:
            if not isinstance(item, dict):
                continue
            missing = RequirementTraceabilityMatrix._string_list(item.get("uncovered"))
            issues.append(
                {
                    "kind": "prompt_completion_audit",
                    "check": f"prompt_completion:{item.get('requirement_id') or 'requirement'}",
                    "details": f"Prompt requirement is not fully tied to artifacts: {', '.join(missing)}.",
                    "blocking": True,
                    "evidence": {
                        "requirement_id": item.get("requirement_id"),
                        "requirement": item.get("requirement"),
                        "uncovered": missing,
                        "details": item.get("details") or [],
                    },
                }
            )
        return issues

    @classmethod
    def _audit_row(
        cls,
        *,
        row: dict[str, Any],
        changed_files: list[str],
        checks_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        requirement_id = str(row.get("requirement_id") or "")
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        implementation_files = cls._implementation_files(row=row, changed_files=changed_files)
        route_page = row.get("route_page") if isinstance(row.get("route_page"), dict) else {}
        api = row.get("api") if isinstance(row.get("api"), dict) else {}
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        test = row.get("test") if isinstance(row.get("test"), dict) else {}
        browser = row.get("browser_proof") if isinstance(row.get("browser_proof"), dict) else {}
        uncovered = RequirementTraceabilityMatrix._string_list(row.get("missing"))
        uncovered_details: list[dict[str, Any]] = [
            {
                "stage": stage,
                "reason": "required proof stage missing",
                "expected": expected,
            }
            for stage in uncovered
        ]
        if not implementation_files:
            uncovered.append("implementation_files")
            uncovered_details.append(
                {
                    "stage": "implementation_files",
                    "reason": "no changed source file could be linked to this prompt requirement",
                    "changed_files": changed_files,
                }
            )
        status = "blocked" if uncovered else "passed"
        return {
            "requirement_id": requirement_id,
            "requirement": row.get("requirement"),
            "source": row.get("source"),
            "status": status,
            "blocking": bool(uncovered),
            "implemented": {
                "status": "passed" if implementation_files else "missing",
                "files": implementation_files,
                "routes": route_page.get("routes") or [],
                "api_paths": api.get("paths") or [],
                "state_fields": state.get("fields") or [],
            },
            "changed_files": implementation_files,
            "proof": {
                "api": cls._proof_from_stage(stage=api, check=checks_by_name.get("api_workflow_smoke"), proof_type="api"),
                "browser": cls._proof_from_stage(stage=browser, check=checks_by_name.get("browser_flow_smoke"), proof_type="browser"),
                "tests": cls._test_proof(stage=test, checks_by_name=checks_by_name),
            },
            "browser_proof": browser,
            "api_proof": api,
            "test_proof": test,
            "uncovered": RequirementTraceabilityMatrix._dedupe(uncovered),
            "uncovered_details": uncovered_details,
        }

    @classmethod
    def _implementation_files(cls, *, row: dict[str, Any], changed_files: list[str]) -> list[str]:
        expected = row.get("expected") if isinstance(row.get("expected"), dict) else {}
        routes = set(RequirementTraceabilityMatrix._string_list((row.get("route_page") or {}).get("routes")) + RequirementTraceabilityMatrix._string_list(expected.get("routes")))
        api_paths = set(RequirementTraceabilityMatrix._string_list((row.get("api") or {}).get("paths")) + RequirementTraceabilityMatrix._string_list(expected.get("api_paths")))
        files: list[str] = []
        for path in changed_files:
            normalized = str(path or "").replace("\\", "/")
            role_route = RequirementTraceabilityMatrix._route_from_file(normalized)
            route_match = bool(role_route and role_route in routes)
            api_match = bool(api_paths and normalized.startswith("miniapp/app/routes/"))
            state_match = bool(normalized.startswith(("miniapp/app/models", "miniapp/app/db", "miniapp/app/state")))
            static_match = bool(not routes and normalized.startswith("miniapp/app/static/"))
            if route_match or api_match or state_match or static_match:
                files.append(normalized)
        if not files and len(changed_files) == 1:
            files = list(changed_files)
        return RequirementTraceabilityMatrix._dedupe(files)[:40]

    @staticmethod
    def _proof_from_stage(*, stage: dict[str, Any], check: dict[str, Any] | None, proof_type: str) -> dict[str, Any]:
        check_payload = check if isinstance(check, dict) else {}
        proof = {
            "type": proof_type,
            "status": stage.get("status") or "missing",
            "check": check_payload.get("name"),
            "check_status": check_payload.get("status"),
            "details": str(check_payload.get("details") or "")[:500],
            "evidence": stage.get("evidence") if isinstance(stage.get("evidence"), dict) else {},
        }
        if proof_type == "browser":
            proof["steps"] = stage.get("steps") or []
            proof["screenshots"] = stage.get("screenshots") or []
        if proof_type == "api":
            proof["paths"] = stage.get("paths") or []
        return proof

    @classmethod
    def _test_proof(cls, *, stage: dict[str, Any], checks_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
        checks = []
        for name in stage.get("checks") or []:
            check = checks_by_name.get(str(name))
            checks.append(
                {
                    "name": str(name),
                    "status": (check or {}).get("status"),
                    "details": str((check or {}).get("details") or "")[:300],
                }
            )
        return {
            "type": "tests",
            "status": stage.get("status") or "missing",
            "checks": checks,
            "evidence": stage.get("evidence") if isinstance(stage.get("evidence"), dict) else {},
        }

    @classmethod
    def _changed_files(cls, *, run: Any, artifacts: dict[str, Any], matrix: dict[str, Any]) -> list[str]:
        files = [
            *RequirementTraceabilityMatrix._paths_from_diff(str(artifacts.get("diff") or "")),
            *RequirementTraceabilityMatrix._string_list(RequirementTraceabilityMatrix._get_attr(run, "touched_files")),
        ]
        for row in matrix.get("rows") or []:
            if isinstance(row, dict):
                route_page = row.get("route_page") if isinstance(row.get("route_page"), dict) else {}
                evidence = route_page.get("evidence") if isinstance(route_page.get("evidence"), dict) else {}
                files.extend(RequirementTraceabilityMatrix._string_list(evidence.get("touched_files")))
        return RequirementTraceabilityMatrix._dedupe(files)[:80]

    @classmethod
    def _proof_summary(cls, *, checks_by_name: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
        passed_checks = [
            name
            for name, check in checks_by_name.items()
            if str(check.get("status") or "") == "passed"
        ]
        return {
            "passed_checks": passed_checks,
            "api_proof_count": sum(1 for row in rows if ((row.get("proof") or {}).get("api") or {}).get("status") == "passed"),
            "browser_proof_count": sum(1 for row in rows if ((row.get("proof") or {}).get("browser") or {}).get("status") == "passed"),
            "test_proof_count": sum(1 for row in rows if ((row.get("proof") or {}).get("tests") or {}).get("status") == "passed"),
            "uncovered_count": sum(1 for row in rows if row.get("uncovered")),
        }
