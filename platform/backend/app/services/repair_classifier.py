from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


REPAIR_CLASSIFIER_SCHEMA = "grounded.repair_classifier.v1"


def _text_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _compact(value: object, *, max_chars: int = 520) -> str:
    text = str(value or "").strip()
    return text if len(text) <= max_chars else f"{text[:max_chars]}..."


def _paths(value: Any) -> list[str]:
    paths: list[str] = []

    def add(candidate: object) -> None:
        text = str(candidate or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if ":" in text and text.startswith("miniapp/"):
            text = text.split(":", 1)[0].strip()
        if text.startswith("miniapp/") and text not in paths:
            paths.append(text)

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            for match in re.finditer(r"(miniapp/[A-Za-z0-9_./*{}-]+(?:\.(?:py|js|mjs|html|css|json))?)", item):
                add(match.group(1))
            return
        if isinstance(item, dict):
            for key in ("path", "file", "file_path", "location", "frontend_ref", "suggested_patch_target"):
                add(item.get(key))
            for key in ("paths", "files", "target_files", "changed_files", "likely_files"):
                values = item.get(key)
                if isinstance(values, list):
                    for value in values:
                        add(value)
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)

    visit(value)
    return paths[:16]


def _role(value: Any) -> str:
    blob = _text_blob(value).lower()
    for role in ("client", "specialist", "manager"):
        if f"/{role}" in blob or f'"{role}"' in blob or f" {role} " in f" {blob} ":
            return role
    return ""


def _api_path(value: Any) -> str:
    match = re.search(r"(/api/[A-Za-z0-9_{}./:-]+)", _text_blob(value))
    return match.group(1).rstrip(".,;:)'\"") if match else ""


def _selector(value: Any) -> str:
    blob = _text_blob(value)
    for key in ("failed_selector", "selector", "expected_selector", "missing_selector"):
        match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', blob)
        if match:
            return match.group(1)
    match = re.search(r"(#[A-Za-z][A-Za-z0-9_-]+|\[[A-Za-z0-9_:-]+[^\]]+\]|(?:button|form|input|select|textarea)\[[^\]]+\])", blob)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class RepairClassRule:
    repair_class: str
    recipe_id: str
    title: str
    check_profile: str
    relevant_checks: tuple[str, ...]
    target_globs: tuple[str, ...]
    patch_steps: tuple[str, ...]
    proof_steps: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    severity: str = "high"
    confidence: float = 0.72


def _rx(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE | re.DOTALL)


REPAIR_CLASS_RULES: tuple[RepairClassRule, ...] = (
    RepairClassRule(
        repair_class="import",
        recipe_id="repair.import_error",
        title="Import or app boot repair",
        check_profile="focused_backend_static",
        relevant_checks=("changed_files_static", "preview_boot_smoke"),
        target_globs=("miniapp/app/**/*.py", "miniapp/app/main.py", "miniapp/app/routes/**"),
        patch_steps=(
            "Read the traceback and the imported module pair.",
            "Patch the missing import, moved symbol, circular reference, or import-time name order only.",
            "Do not change UI or tests until the app imports cleanly.",
        ),
        proof_steps=("Run changed_files_static.", "Run preview_boot_smoke if import-time app boot was involved."),
        patterns=(_rx(r"\b(importerror|modulenotfounderror|cannot import name|nameerror|app import|preview.*boot)\b"),),
        severity="critical",
        confidence=0.86,
    ),
    RepairClassRule(
        repair_class="route",
        recipe_id="repair.route_contract",
        title="Route contract repair",
        check_profile="focused_route_static",
        relevant_checks=("schema_validators", "connectivity_validators", "preview_boot_smoke"),
        target_globs=("miniapp/app/routes/**", "miniapp/app/main.py", "miniapp/app/static/**/index.html"),
        patch_steps=(
            "Map the failed page or backend route against route registration.",
            "Patch the route declaration, mounted static page, or stale route reference.",
            "Keep generated route metadata derived from actual files rather than editing generated metadata directly.",
        ),
        proof_steps=("Run schema_validators.", "Run connectivity_validators for changed frontend/backend links."),
        patterns=(_rx(r"\b(route_manifest|missing_role_page|duplicate_static_route|404|not found|routeable|declared route)\b"),),
        severity="critical",
        confidence=0.8,
    ),
    RepairClassRule(
        repair_class="selector",
        recipe_id="repair.selector_wiring",
        title="Selector and interaction wiring repair",
        check_profile="focused_frontend_interaction",
        relevant_checks=("frontend_interaction_static_smoke", "generated_app_js_tests"),
        target_globs=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js", "miniapp/tests/generated_app.test.mjs"),
        patch_steps=(
            "Read the exact role HTML and app.js plus the failing selector evidence.",
            "Patch only the mismatched id/data-action/form binding or stale generated selector assertion.",
            "Preserve the intended workflow action rather than deleting controls or handlers.",
        ),
        proof_steps=("Run frontend_interaction_static_smoke.", "Run generated_app_js_tests when a generated selector test changed."),
        patterns=(_rx(r"\b(selector|queryselector|missing_dom_id|unwired_(?:button|form)|submit handler|stale_selector)\b"),),
        confidence=0.84,
    ),
    RepairClassRule(
        repair_class="db_schema",
        recipe_id="repair.db_schema_contract",
        title="DB schema and persistence repair",
        check_profile="focused_api_persistence",
        relevant_checks=("api_workflow_smoke", "generated_app_python_tests"),
        target_globs=("miniapp/app/db.py", "miniapp/app/routes/**", "miniapp/app/schemas.py", "miniapp/tests/test_generated_app.py"),
        patch_steps=(
            "Trace the persisted entity through schema initialization, route SQL, and response shape.",
            "Patch missing table/column/default handling together with the route code that reads or writes it.",
            "Keep create/update/read behavior aligned with the acceptance workflow.",
        ),
        proof_steps=("Run api_workflow_smoke.", "Run generated_app_python_tests when API test expectations changed."),
        patterns=(_rx(r"\b(no such table|has no column|operationalerror|sqlite|db schema|persistence|persisted marker)\b"),),
        severity="critical",
        confidence=0.88,
    ),
    RepairClassRule(
        repair_class="js_syntax",
        recipe_id="repair.js_syntax",
        title="JavaScript syntax repair",
        check_profile="focused_js_static",
        relevant_checks=("changed_files_static", "generated_app_js_tests"),
        target_globs=("miniapp/app/static/**/app.js", "miniapp/tests/generated_app.test.mjs"),
        patch_steps=(
            "Read the JS syntax error line and its immediate block.",
            "Patch the parse error without rewriting unrelated handlers.",
            "If the failure is in generated JS tests, patch the stale test harness rather than browser runtime code.",
        ),
        proof_steps=("Run changed_files_static.", "Run generated_app_js_tests if test JS changed."),
        patterns=(_rx(r"\b(syntaxerror|unexpected token|node --check|unterminated|string literal)\b"),),
        severity="critical",
        confidence=0.9,
    ),
    RepairClassRule(
        repair_class="css_overflow",
        recipe_id="repair.css_overflow",
        title="Mobile overflow and layout repair",
        check_profile="focused_mobile_visual",
        relevant_checks=("browser_flow_smoke", "visual_regression"),
        target_globs=("miniapp/app/static/**/styles.css", "miniapp/app/static/shared/base.css", "miniapp/app/static/**/index.html"),
        patch_steps=(
            "Read the implicated role markup and CSS.",
            "Patch wrapping, min-width, grid/flex constraints, and spacing only for the overflowing layout.",
            "Do not change product behavior to make the screenshot pass.",
        ),
        proof_steps=("Run mobile browser proof.", "Compare visual regression for the repaired role viewport."),
        patterns=(_rx(r"\b(horizontal overflow|overflow|overlap|mobile_layout|viewport|layout_regression)\b"),),
        confidence=0.86,
    ),
    RepairClassRule(
        repair_class="missing_api",
        recipe_id="repair.missing_api",
        title="Missing API repair",
        check_profile="focused_api_route",
        relevant_checks=("connectivity_validators", "api_workflow_smoke", "browser_flow_smoke"),
        target_globs=("miniapp/app/routes/**", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/app/static/**/app.js"),
        patch_steps=(
            "Trace the frontend fetch method/path and backend route table.",
            "Add the missing route or update the frontend to the existing route contract.",
            "Verify that the route persists and returns the workflow state expected by the UI.",
        ),
        proof_steps=("Run connectivity_validators.", "Run api_workflow_smoke.", "Run browser_flow_smoke if this API backs a role workflow."),
        patterns=(_rx(r"\b(missing_backend_route|frontend.*api.*missing|fetch.*404|api route|/api/)\b"),),
        severity="critical",
        confidence=0.84,
    ),
    RepairClassRule(
        repair_class="stale_test",
        recipe_id="repair.stale_generated_test",
        title="Stale generated test repair",
        check_profile="focused_generated_tests",
        relevant_checks=("generated_app_python_tests", "generated_app_js_tests"),
        target_globs=("miniapp/tests/test_generated_app.py", "miniapp/tests/generated_app.test.mjs", "miniapp/app/static/**/app.js", "miniapp/app/routes/**"),
        patch_steps=(
            "Read the failing generated test and the product code it asserts.",
            "Patch product code only when it violates the acceptance contract.",
            "When the app behavior is correct, update stale generated expectations to assert the actual workflow.",
        ),
        proof_steps=("Run only the failing generated app test target first.", "Escalate to full gate if generated tests pass but product proof still fails."),
        patterns=(_rx(r"\b(stale test|stale_selector|generated_app_(?:python|js)_tests|assertionerror|brittle|test.*contract mismatch)\b"),),
        confidence=0.78,
    ),
)


class RepairClassifier:
    @classmethod
    def classify(cls, packet: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(packet, dict):
            packet = {}
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        blob = _text_blob([packet, evidence]).lower()
        rule = cls._match_rule(blob)
        target_files = cls._target_files(packet, rule)
        selector = _selector([packet, evidence])
        api_path = _api_path([packet, evidence])
        role = _role([packet, evidence])
        failed_check = str(packet.get("failed_check") or packet.get("verification_check") or packet.get("failure_class") or "checks.run")
        recipe = {
            "schema": "grounded.repair_recipe.v1",
            "recipe_id": rule.recipe_id,
            "title": rule.title,
            "repair_class": rule.repair_class,
            "focused": True,
            "steps": list(rule.patch_steps),
            "target_files": target_files,
            "relevant_checks": list(rule.relevant_checks),
            "check_profile": rule.check_profile,
            "proof_steps": list(rule.proof_steps),
            "stop_conditions": [
                "same_patch_hash_repeated",
                "same_failure_signature_after_focused_attempt",
                "focused_check_still_fails_after_max_attempts",
            ],
        }
        focused_patch_plan = {
            "schema": "grounded.focused_patch_plan.v1",
            "mode": "focused_patch",
            "allowed_files": target_files,
            "forbidden_scope": [
                "unrelated role redesign",
                "editing generated route metadata directly",
                "removing workflow controls to silence tests",
                "broad full-app rewrite before focused proof fails",
            ],
            "first_step": "read_target_files" if target_files else "collect_exact_diagnostics",
            "selector": selector,
            "api_path": api_path,
            "role": role,
            "patch_budget": {"files": min(max(len(target_files), 1), 4), "intent": "smallest slice that explains the failure"},
        }
        relevant_checks = [
            {
                "check": check,
                "scope": "focused",
                "reason": f"{rule.repair_class} repair should rerun the failing proof slice before full gate.",
            }
            for check in rule.relevant_checks
        ]
        escalation = {
            "strategy": "full_repair_after_focused_failure",
            "max_focused_attempts": 2 if rule.repair_class not in {"js_syntax", "import"} else 1,
            "escalate_to": "full_repair",
            "when": [
                "focused checks still fail after max focused attempts",
                "new unrelated failure class appears",
                "target files do not explain the failure evidence",
            ],
        }
        return {
            "schema": REPAIR_CLASSIFIER_SCHEMA,
            "repair_class": rule.repair_class,
            "recipe": recipe,
            "recipe_id": rule.recipe_id,
            "focused_patch_plan": {key: value for key, value in focused_patch_plan.items() if value not in ("", [], {})},
            "relevant_checks": relevant_checks,
            "check_profile": rule.check_profile,
            "classification_evidence": {
                "failed_check": failed_check,
                "matched_patterns": [pattern.pattern for pattern in rule.patterns if pattern.search(blob)][:3],
                "selector": selector,
                "api_path": api_path,
                "role": role,
                "excerpt": _compact(blob),
            },
            "escalation": escalation,
            "confidence": {"score": rule.confidence, "severity": rule.severity},
        }

    @staticmethod
    def _match_rule(blob: str) -> RepairClassRule:
        for rule in REPAIR_CLASS_RULES:
            if any(pattern.search(blob) for pattern in rule.patterns):
                return rule
        return RepairClassRule(
            repair_class="unknown",
            recipe_id="repair.evidence_driven",
            title="Evidence-driven repair",
            check_profile="focused_failing_check",
            relevant_checks=("run_failing_check",),
            target_globs=("miniapp/app/**",),
            patch_steps=(
                "Collect exact diagnostics for the failing check.",
                "Patch only files named by the evidence.",
                "Rerun the failing check before escalating.",
            ),
            proof_steps=("Run the latest failing check.",),
            patterns=(),
            severity="medium",
            confidence=0.45,
        )

    @staticmethod
    def _target_files(packet: dict[str, Any], rule: RepairClassRule) -> list[str]:
        ordered: list[str] = []

        def add(value: object) -> None:
            text = str(value or "").strip().replace("\\", "/")
            while text.startswith("./"):
                text = text[2:]
            if text.startswith("miniapp/") and text not in ordered:
                ordered.append(text)

        for key in ("likely_files", "target_files"):
            for path in packet.get(key) or []:
                add(path)
        for path in _paths(packet.get("evidence")):
            add(path)
        if ordered:
            return ordered[:8]
        return list(rule.target_globs)[:8]
