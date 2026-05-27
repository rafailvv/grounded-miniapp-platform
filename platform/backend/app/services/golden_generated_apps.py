from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.skill_registry import SkillRegistryService
from app.services.workflow_acceptance import build_acceptance_contract


GOLDEN_APPS_SCHEMA = "grounded.golden.generated_apps.v1"
GOLDEN_APP_COMPILE_SCHEMA = "grounded.golden.generated_app_compile.v1"
GOLDEN_APP_SCORE_SCHEMA = "grounded.golden.generated_app_regression_score.v1"
READINESS_CHECKLIST_KEYS = ("api", "persistence", "ui", "roles", "mobile", "tests", "apply_guardian")
BENCHMARK_EXPECTATION_KEYS = ("expected_roles", "expected_routes", "expected_api", "expected_persistence_markers", "visual_mobile_thresholds")


class GoldenGeneratedAppCatalog:
    """Golden product contracts for generation regression tests.

    These fixtures are deliberately product contracts, not source-code templates.
    They keep generation quality measurable without teaching the agent to copy
    fixed apps or seed records.
    """

    DEFAULT_RELATIVE_PATH = Path("golden-generated-apps/catalog.json")

    @classmethod
    def load(cls, runtime_dir: Path) -> dict[str, Any]:
        path = runtime_dir / cls.DEFAULT_RELATIVE_PATH
        if not path.exists():
            return {
                "schema": GOLDEN_APPS_SCHEMA,
                "status": "missing",
                "items": [],
                "count": 0,
                "fixture_path": str(path),
                "issues": [{"code": "golden_catalog_missing", "message": "Golden generated app catalog is missing."}],
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "schema": GOLDEN_APPS_SCHEMA,
                "status": "invalid",
                "items": [],
                "count": 0,
                "fixture_path": str(path),
                "issues": [{"code": "golden_catalog_invalid_json", "message": str(exc)}],
            }
        items = [cls._normalize_item(item) for item in payload.get("items") or [] if isinstance(item, dict)]
        issues = cls._catalog_issues(items)
        score = cls.regression_score(items=items, runtime_dir=runtime_dir, repo_root=runtime_dir.parent)
        return {
            "schema": GOLDEN_APPS_SCHEMA,
            "status": "ready" if items and not issues else "invalid" if issues else "empty",
            "description": str(payload.get("description") or ""),
            "items": items,
            "count": len(items),
            "fixture_path": str(path),
            "ids": [item["id"] for item in items],
            "issues": issues,
            "regression_score": score,
        }

    @classmethod
    def get(cls, runtime_dir: Path, app_id: str) -> dict[str, Any]:
        catalog = cls.load(runtime_dir)
        for item in catalog.get("items") or []:
            if item.get("id") == app_id:
                return item
        raise KeyError(f"Golden generated app not found: {app_id}")

    @classmethod
    def compile(
        cls,
        item: dict[str, Any],
        *,
        runtime_dir: Path,
        repo_root: Path,
        max_skills: int = 8,
    ) -> dict[str, Any]:
        prompt = str(item.get("prompt") or "")
        generation_mode = str(item.get("generation_mode") or "quality")
        contract = build_acceptance_contract(
            prompt=prompt,
            intent="create",
            generation_mode=generation_mode,
            prompt_analysis=dict(item.get("prompt_analysis") or {}),
        )
        selected = SkillRegistryService(
            runtime_dir=runtime_dir,
            repo_root=repo_root,
            data_dir=repo_root / "data",
        ).search_for_context(
            prompt=prompt,
            intent="create",
            generation_mode=generation_mode,
            max_skills=max_skills,
        )
        selected_ids = [str(skill.get("id") or "") for skill in selected.get("selected") or []]
        expected = item.get("expected_contract") if isinstance(item.get("expected_contract"), dict) else {}
        issues = cls._compiled_issues(item=item, contract=contract, selected_ids=selected_ids, expected=expected)
        benchmark = cls._benchmark_expectations(item)
        benchmark_issues = cls._benchmark_issues(item=item, contract=contract, benchmark=benchmark)
        issues.extend(benchmark_issues)
        score = cls._score_for_issues(app_id=str(item.get("id") or ""), issues=issues)
        return {
            "schema": GOLDEN_APP_COMPILE_SCHEMA,
            "status": "passed" if not issues else "failed",
            "id": item.get("id"),
            "title": item.get("title"),
            "contract": contract,
            "selected_skill_ids": selected_ids,
            "expected_skill_ids": list(item.get("expected_skill_ids") or []),
            "benchmark_expectations": benchmark,
            "readiness_required_checks": list(item.get("readiness_required_checks") or READINESS_CHECKLIST_KEYS),
            "regression_score": score,
            "issues": issues,
        }

    @classmethod
    def regression_score(cls, *, items: list[dict[str, Any]], runtime_dir: Path, repo_root: Path, max_skills: int = 8) -> dict[str, Any]:
        compiled = [cls.compile(item, runtime_dir=runtime_dir, repo_root=repo_root, max_skills=max_skills) for item in items]
        failures = [item for item in compiled if item.get("status") != "passed"]
        total_score = sum(float((item.get("regression_score") or {}).get("earned") or 0.0) for item in compiled)
        total_possible = sum(float((item.get("regression_score") or {}).get("possible") or 0.0) for item in compiled)
        score = round(total_score / total_possible, 4) if total_possible else 0.0
        return {
            "schema": GOLDEN_APP_SCORE_SCHEMA,
            "status": "passed" if not failures else "failed",
            "score": score,
            "passed": len(compiled) - len(failures),
            "failed": len(failures),
            "total": len(compiled),
            "possible": total_possible,
            "earned": total_score,
            "failure_ids": [str(item.get("id") or "") for item in failures],
            "minimum_passing_score": 1.0,
        }

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        normalized["id"] = str(normalized.get("id") or "").strip()
        normalized["title"] = str(normalized.get("title") or normalized["id"]).strip()
        normalized["domain"] = str(normalized.get("domain") or "").strip()
        normalized["generation_mode"] = str(normalized.get("generation_mode") or "quality").strip().lower()
        normalized["expected_skill_ids"] = [str(value).strip() for value in normalized.get("expected_skill_ids") or [] if str(value).strip()]
        normalized["readiness_required_checks"] = [
            str(value).strip()
            for value in normalized.get("readiness_required_checks") or READINESS_CHECKLIST_KEYS
            if str(value).strip()
        ]
        normalized["expected_roles"] = [str(value).strip() for value in normalized.get("expected_roles") or ["client", "specialist", "manager"] if str(value).strip()]
        normalized["expected_routes"] = [dict(value) for value in normalized.get("expected_routes") or [] if isinstance(value, dict)]
        normalized["expected_api"] = [dict(value) for value in normalized.get("expected_api") or [] if isinstance(value, dict)]
        normalized["expected_persistence_markers"] = [str(value).strip() for value in normalized.get("expected_persistence_markers") or [] if str(value).strip()]
        normalized["visual_mobile_thresholds"] = normalized.get("visual_mobile_thresholds") if isinstance(normalized.get("visual_mobile_thresholds"), dict) else {}
        return normalized

    @staticmethod
    def _catalog_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            app_id = str(item.get("id") or "")
            if not app_id:
                issues.append({"code": "missing_id", "message": "Golden app item is missing id."})
            elif app_id in seen:
                issues.append({"code": "duplicate_id", "id": app_id, "message": "Golden app id is duplicated."})
            seen.add(app_id)
            if not str(item.get("prompt") or "").strip():
                issues.append({"code": "missing_prompt", "id": app_id, "message": "Golden app prompt is missing."})
            if not isinstance(item.get("prompt_analysis"), dict):
                issues.append({"code": "missing_prompt_analysis", "id": app_id, "message": "Golden app prompt_analysis is missing."})
            if not item.get("expected_skill_ids"):
                issues.append({"code": "missing_expected_skills", "id": app_id, "message": "Golden app expected_skill_ids are missing."})
            for key in BENCHMARK_EXPECTATION_KEYS:
                if not item.get(key):
                    issues.append({"code": f"missing_{key}", "id": app_id, "message": f"Golden app {key} is missing."})
            missing_checks = sorted(set(READINESS_CHECKLIST_KEYS) - set(item.get("readiness_required_checks") or []))
            if missing_checks:
                issues.append({"code": "missing_readiness_checks", "id": app_id, "missing": missing_checks, "message": "Golden app readiness checklist is incomplete."})
        return issues

    @classmethod
    def _compiled_issues(
        cls,
        *,
        item: dict[str, Any],
        contract: dict[str, Any],
        selected_ids: list[str],
        expected: dict[str, Any],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        app_id = str(item.get("id") or "")
        if not contract.get("required") or contract.get("blocking"):
            issues.append({"code": "acceptance_contract_blocked", "id": app_id, "message": "Golden app did not compile to a required non-blocked acceptance contract."})
        expected_skills = set(str(value) for value in item.get("expected_skill_ids") or [])
        missing_skills = sorted(expected_skills - set(selected_ids))
        if missing_skills:
            issues.append({"code": "expected_skills_missing", "id": app_id, "missing": missing_skills, "selected": selected_ids})
        hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        api_contract = contract.get("api_contract") if isinstance(contract.get("api_contract"), dict) else {}
        if expected.get("resource_hint") and hints.get("resource_hint") != expected.get("resource_hint"):
            issues.append({"code": "resource_hint_mismatch", "id": app_id, "expected": expected.get("resource_hint"), "actual": hints.get("resource_hint")})
        cls._append_missing_list_issue(issues, code="resource_hints_missing", app_id=app_id, expected=expected.get("required_resource_hints"), actual=api_contract.get("resource_hints"))
        cls._append_missing_list_issue(issues, code="field_hints_missing", app_id=app_id, expected=expected.get("required_fields"), actual=api_contract.get("field_hints"))
        role_state = api_contract.get("role_state_contract") if isinstance(api_contract.get("role_state_contract"), dict) else {}
        for key in ("source_roles", "update_roles", "observer_roles", "status_values"):
            cls._append_missing_list_issue(issues, code=f"{key}_missing", app_id=app_id, expected=expected.get(key), actual=role_state.get(key))
        page_contract = contract.get("page_contract") if isinstance(contract.get("page_contract"), dict) else {}
        min_routes = page_contract.get("min_role_routes") if isinstance(page_contract.get("min_role_routes"), dict) else {}
        for role, minimum in (expected.get("min_role_routes") or {}).items():
            try:
                actual = int(min_routes.get(role) or 0)
                required = int(minimum)
            except (TypeError, ValueError):
                actual = 0
                required = 1
            if actual < required:
                issues.append({"code": "min_role_routes_too_low", "id": app_id, "role": role, "expected": required, "actual": actual})
        step_kinds = {
            str(step.get("kind") or "")
            for flow in contract.get("flows") or []
            if isinstance(flow, dict)
            for step in flow.get("steps") or []
            if isinstance(step, dict)
        }
        cls._append_missing_list_issue(issues, code="acceptance_steps_missing", app_id=app_id, expected=expected.get("required_step_kinds"), actual=step_kinds)
        return issues

    @staticmethod
    def _benchmark_expectations(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item.get(key) for key in BENCHMARK_EXPECTATION_KEYS}

    @classmethod
    def _benchmark_issues(cls, *, item: dict[str, Any], contract: dict[str, Any], benchmark: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        app_id = str(item.get("id") or "")
        expected_roles = {str(role).strip() for role in benchmark.get("expected_roles") or [] if str(role).strip()}
        role_plan = ((contract.get("page_contract") or {}).get("routeable_screen_plan") or {}).get("roles")
        actual_roles = set(role_plan.keys()) if isinstance(role_plan, dict) else set()
        missing_roles = sorted(expected_roles - actual_roles)
        if missing_roles:
            issues.append({"code": "expected_roles_missing", "id": app_id, "missing": missing_roles, "actual": sorted(actual_roles)})
        expected_routes = [route for route in benchmark.get("expected_routes") or [] if isinstance(route, dict)]
        for route in expected_routes:
            role = str(route.get("role") or "").strip()
            min_count = int(route.get("min_count") or 1)
            actual_count = len(role_plan.get(role) or []) if isinstance(role_plan, dict) and isinstance(role_plan.get(role), list) else 0
            if actual_count < min_count:
                issues.append({"code": "expected_route_count_too_low", "id": app_id, "role": role, "expected": min_count, "actual": actual_count})
        api_contract = contract.get("api_contract") if isinstance(contract.get("api_contract"), dict) else {}
        cls._append_missing_list_issue(issues, code="expected_persistence_markers_missing", app_id=app_id, expected=benchmark.get("expected_persistence_markers"), actual=api_contract.get("field_hints"))
        thresholds = benchmark.get("visual_mobile_thresholds") if isinstance(benchmark.get("visual_mobile_thresholds"), dict) else {}
        if thresholds:
            min_width = int(thresholds.get("min_mobile_width") or 0)
            max_overflow = int(thresholds.get("max_horizontal_overflow_px") or 0)
            max_overlap = int(thresholds.get("max_critical_overlaps") or 0)
            if min_width < 320 or max_overflow != 0 or max_overlap != 0:
                issues.append({"code": "visual_mobile_threshold_not_strict", "id": app_id, "thresholds": thresholds})
        expected_api = [item for item in benchmark.get("expected_api") or [] if isinstance(item, dict)]
        if expected_api and not api_contract.get("resource_hint"):
            issues.append({"code": "expected_api_resource_missing", "id": app_id, "expected_api": expected_api})
        return issues

    @staticmethod
    def _score_for_issues(*, app_id: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
        possible = 10.0
        penalties = {
            "expected_skills_missing": 2.0,
            "acceptance_contract_blocked": 3.0,
            "resource_hint_mismatch": 1.5,
            "resource_hints_missing": 1.0,
            "field_hints_missing": 1.0,
            "source_roles_missing": 1.0,
            "update_roles_missing": 1.0,
            "observer_roles_missing": 1.0,
            "status_values_missing": 1.0,
            "min_role_routes_too_low": 1.0,
            "acceptance_steps_missing": 1.0,
            "expected_roles_missing": 1.0,
            "expected_route_count_too_low": 1.0,
            "expected_persistence_markers_missing": 1.0,
            "visual_mobile_threshold_not_strict": 1.0,
            "expected_api_resource_missing": 1.0,
        }
        lost = sum(float(penalties.get(str(issue.get("code") or ""), 0.5)) for issue in issues)
        earned = max(0.0, possible - lost)
        return {
            "schema": GOLDEN_APP_SCORE_SCHEMA,
            "id": app_id,
            "status": "passed" if not issues else "failed",
            "score": round(earned / possible, 4),
            "earned": earned,
            "possible": possible,
            "issue_count": len(issues),
        }

    @staticmethod
    def _append_missing_list_issue(
        issues: list[dict[str, Any]],
        *,
        code: str,
        app_id: str,
        expected: Any,
        actual: Any,
    ) -> None:
        expected_values = {str(value).strip() for value in expected or [] if str(value).strip()}
        actual_values = {str(value).strip() for value in actual or [] if str(value).strip()}
        missing = sorted(expected_values - actual_values)
        if missing:
            issues.append({"code": code, "id": app_id, "missing": missing, "actual": sorted(actual_values)})
