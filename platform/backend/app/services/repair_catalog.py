from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RepairCatalogEntry:
    signature: str
    issue_code: str
    severity: str
    likely_root_cause: str
    target_files: tuple[str, ...]
    verification_check: str
    instruction: str
    auto_fixable: bool = True
    required_next_tool: str = "read_files"
    suggested_tool_after_read: str = "apply_patch_to_draft_or_write_file"
    verification_command: str = "run_checks"
    retry_policy: str = "deterministic_repair"
    deterministic: bool = True
    forbidden_tools_once: tuple[str, ...] = field(default_factory=tuple)
    expected_proof: str | None = None
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)

    def packet(self, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        evidence_payload = evidence or {}
        target_files: list[str] = []
        for value in evidence_payload.get("paths") or evidence_payload.get("target_files") or []:
            text = str(value or "").strip().replace("\\", "/")
            if text.startswith("miniapp/") and text not in target_files:
                target_files.append(text)
        for value in self.target_files:
            if value not in target_files:
                target_files.append(value)
        return {
            "signature": self.signature,
            "issue_code": self.issue_code,
            "code": self.issue_code,
            "severity": self.severity,
            "likely_root_cause": self.likely_root_cause,
            "target_files": target_files,
            "verification_check": self.verification_check,
            "verification_command": self.verification_command,
            "instruction": self.instruction,
            "auto_fixable": self.auto_fixable,
            "required_next_tool": self.required_next_tool,
            "suggested_tool_after_read": self.suggested_tool_after_read,
            "retry_policy": self.retry_policy,
            "retryable": self.auto_fixable,
            "deterministic": self.deterministic,
            "failure_class": str(evidence_payload.get("failure_class") or self.verification_check),
            "failure_signature": str(evidence_payload.get("failure_signature") or self.signature),
            "repair_recipe_id": f"catalog.{self.issue_code}",
            "forbidden_tools_once": list(self.forbidden_tools_once),
            "expected_proof": self.expected_proof or self.verification_check,
            "next_forced_action": {
                "required_next_tool": self.required_next_tool,
                "target_files": target_files,
                "verification_check": self.verification_check,
            },
            "evidence": evidence_payload,
        }


def _rx(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE | re.DOTALL)


REPAIR_CATALOG: tuple[RepairCatalogEntry, ...] = (
    RepairCatalogEntry(
        signature="frontend.js_syntax",
        issue_code="js_syntax_error",
        severity="critical",
        likely_root_cause="Generated JavaScript cannot be parsed or executed.",
        target_files=("miniapp/app/static/**/app.js",),
        verification_check="changed_files_static",
        instruction="Open the failing JS file, fix the syntax at the reported line, then rerun static checks.",
        required_next_tool="read_files",
        verification_command="run_checks changed_files_static",
        patterns=(_rx(r"\bsyntaxerror\b"), _rx(r"node --check"), _rx(r"unexpected token")),
    ),
    RepairCatalogEntry(
        signature="backend.python_name_error",
        issue_code="python_name_error",
        severity="critical",
        likely_root_cause="Generated Python imports reference a name before it is defined.",
        target_files=("miniapp/app/**/*.py",),
        verification_check="changed_files_static",
        instruction=(
            "Read the traceback and patch the exact Python file. Move helper functions/constants above import-time model/route declarations "
            "or replace the reference with an inline callable so app.main imports cleanly."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks changed_files_static",
        patterns=(
            _rx(r"python_name_error"),
            _rx(r"NameError:\s*name\s+['\"][^'\"]+['\"]\s+is not defined"),
            _rx(r"backend_import_name_error"),
        ),
    ),
    RepairCatalogEntry(
        signature="frontend.missing_dom_id",
        issue_code="missing_dom_id",
        severity="high",
        likely_root_cause="JavaScript expects a DOM id or form field that the generated HTML does not expose.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Align HTML ids/form names and JS selectors. Prefer preserving the intended user workflow over deleting handlers.",
        required_next_tool="read_files",
        verification_command="run_checks frontend_interaction_static_smoke",
        patterns=(_rx(r"missing.*(?:dom|html).*id"), _rx(r"queryselector"), _rx(r"form_field")),
    ),
    RepairCatalogEntry(
        signature="backend.missing_route",
        issue_code="missing_backend_route",
        severity="critical",
        likely_root_cause="Frontend fetches an API endpoint that is not declared by the FastAPI app.",
        target_files=("miniapp/app/routes/**", "miniapp/app/static/**/app.js"),
        verification_check="connectivity_validators",
        instruction="Add the missing backend route or update the frontend API reference so both sides use one route contract.",
        required_next_tool="read_files",
        verification_command="run_checks connectivity_validators",
        patterns=(_rx(r"missing_backend_route"), _rx(r"frontend.*api.*missing"), _rx(r"404")),
    ),
    RepairCatalogEntry(
        signature="edit.stale_read_state",
        issue_code="stale_edit_read_state_failure",
        severity="high",
        likely_root_cause="The patch was based on stale file contents or attempted to edit a file that was not freshly read.",
        target_files=("miniapp/app/**",),
        verification_check="edit_validator",
        instruction=(
            "Read the exact target files again, compare the current content to the intended patch, then make a minimal patch against the fresh state. "
            "Do not retry the same stale edit payload."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="apply_patch_to_draft_or_write_file",
        verification_command="run_checks changed_files_static",
        retry_policy="refresh_read_before_retry",
        forbidden_tools_once=("apply_patch_to_draft", "write_file"),
        expected_proof="fresh read evidence plus successful changed_files_static check",
        patterns=(
            _rx(r"stale(?: file| read| state)"),
            _rx(r"old_string_not_found"),
            _rx(r"file_not_read"),
            _rx(r"partial_read"),
            _rx(r"read state"),
        ),
    ),
    RepairCatalogEntry(
        signature="backend.fastapi_response_model",
        issue_code="fastapi_response_model_error",
        severity="critical",
        likely_root_cause="A FastAPI route exposes an invalid dependency or response field, often a Session object in the signature.",
        target_files=("miniapp/app/routes/**", "miniapp/app/schemas.py", "miniapp/app/db.py"),
        verification_check="preview_boot_smoke",
        instruction="Move DB sessions to Depends/get_db patterns and keep response payloads Pydantic/JSON serializable.",
        required_next_tool="read_files",
        verification_command="run_checks preview_boot_smoke",
        patterns=(_rx(r"invalid args for response field"), _rx(r"sqlalchemy\.orm\.session\.session"), _rx(r"fastapi")),
    ),
    RepairCatalogEntry(
        signature="database.sqlite_schema_drift",
        issue_code="sqlite_missing_table_or_column",
        severity="critical",
        likely_root_cause="Generated persistence code reads or writes a table/column that DB initialization does not create.",
        target_files=("miniapp/app/db.py", "miniapp/app/routes/**", "miniapp/app/schemas.py"),
        verification_check="api_workflow_smoke",
        instruction="Update schema initialization and route SQL together, then verify prompt-derived persisted write/read/update behavior.",
        required_next_tool="read_files",
        verification_command="run_checks api_workflow_smoke",
        patterns=(_rx(r"no such table"), _rx(r"has no column named"), _rx(r"operationalerror")),
    ),
    RepairCatalogEntry(
        signature="tests.python_api_contract_mismatch",
        issue_code="generated_python_api_contract_mismatch",
        severity="high",
        likely_root_cause="Generated Python acceptance tests and the generated API behavior disagree about the prompt-owned persisted workflow.",
        target_files=(
            "miniapp/app/routes/**",
            "miniapp/app/schemas.py",
            "miniapp/app/db.py",
            "miniapp/tests/test_generated_app.py",
            "miniapp/app/static/**/app.js",
        ),
        verification_check="generated_app_python_tests",
        instruction=(
            "Read the failing assertion context, backend route/schema, and prompt contract. If the API violates the requested persisted workflow, "
            "patch the backend and frontend payloads; if the test asserts stale behavior, patch the generated test. Keep all three aligned."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="apply_patch_to_draft_or_write_file",
        verification_command="run_checks generated_app_python_tests",
        patterns=(
            _rx(r"generated_app_python_tests"),
            _rx(r"python_assertion_failures"),
            _rx(r"AssertionError:\s*.+\s*!=\s*.+"),
        ),
    ),
    RepairCatalogEntry(
        signature="tests.js_brittle_route_manifest_root",
        issue_code="generated_js_brittle_route_manifest_root",
        severity="high",
        likely_root_cause="Generated JS tests assert an implementation-specific route manifest root field instead of deriving actual role pages.",
        target_files=("miniapp/tests/generated_app.test.mjs",),
        verification_check="generated_app_js_tests",
        instruction=(
            "Patch generated_app.test.mjs to derive actual role routes/pages from route_manifest routes/pages and filesystem HTML files. "
            "Do not edit generated/route_manifest.json and do not require manifest.roles.<role>.root."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks generated_app_js_tests",
        patterns=(
            _rx(r"js_test_brittle_route_manifest_assertion"),
            _rx(r"generated_js_test_requires_exact_route_manifest_root"),
            _rx(r"manifest\.roles\.[A-Za-z0-9_]+\.root"),
            _rx(r"routeManifest\.roles\.[A-Za-z0-9_]+\.root"),
        ),
    ),
    RepairCatalogEntry(
        signature="contract.route_manifest_drift",
        issue_code="route_manifest_drift",
        severity="high",
        likely_root_cause="Route manifest, generated contract, and filesystem role pages disagree.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/tests/generated_app.test.mjs"),
        verification_check="schema_validators",
        instruction="Do not edit generated route_manifest.json directly. Create/remove the actual role page files or patch stale generated tests; the platform will resync generated route metadata.",
        required_next_tool="read_files",
        verification_command="run_checks schema_validators",
        patterns=(_rx(r"route_manifest"), _rx(r"duplicate_static_route"), _rx(r"declared routes")),
    ),
    RepairCatalogEntry(
        signature="tests.js_browser_global_import",
        issue_code="generated_js_browser_global_import",
        severity="high",
        likely_root_cause="Generated JS acceptance tests import browser-only role scripts in Node without providing DOM globals.",
        target_files=("miniapp/tests/generated_app.test.mjs",),
        verification_check="generated_app_js_tests",
        instruction=(
            "Patch generated_app.test.mjs so it does not import browser-only app.js directly unless it creates explicit window/document mocks first. "
            "Prefer reading role HTML/JS source text and asserting real selectors, API calls, route pages, and event handlers."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks generated_app_js_tests",
        patterns=(
            _rx(r"js_test_imports_browser_app_without_dom"),
            _rx(r"ReferenceError:\s*(?:document|window)\s+is not defined"),
            _rx(r"generated_app\.test\.mjs.*import.*app/static/(?:client|specialist|manager)/app\.js"),
        ),
    ),
    RepairCatalogEntry(
        signature="preview.blank_screen",
        issue_code="blank_preview",
        severity="critical",
        likely_root_cause="Preview loads but renders no meaningful role content.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js", "miniapp/app/static/**/styles.css"),
        verification_check="browser_flow_smoke",
        instruction="Restore visible role UI, remove template-only content, and verify each role route renders non-empty product content.",
        required_next_tool="read_files",
        verification_command="browser_verify",
        patterns=(_rx(r"blank"), _rx(r"empty preview"), _rx(r"template placeholder")),
    ),
    RepairCatalogEntry(
        signature="preview.missing_role_page",
        issue_code="missing_role_page",
        severity="critical",
        likely_root_cause="One of the required client/specialist/manager role pages is missing or not routeable.",
        target_files=("miniapp/app/static/client/**", "miniapp/app/static/specialist/**", "miniapp/app/static/manager/**"),
        verification_check="browser_flow_smoke",
        instruction="Create or route all required role pages and keep role navigation consistent with the manifest.",
        required_next_tool="read_files",
        verification_command="browser_verify",
        patterns=(_rx(r"no(?: declared)? pages?.*role"), _rx(r"missing_role_page"), _rx(r"missing.*role.*page"), _rx(r"/(?:client|specialist|manager)")),
    ),
    RepairCatalogEntry(
        signature="preview.boot_failed",
        issue_code="preview_boot_failure",
        severity="critical",
        likely_root_cause="The generated FastAPI app cannot start, so browser proof cannot run.",
        target_files=("miniapp/app/main.py", "miniapp/app/routes/**", "miniapp/app/db.py", "miniapp/app/schemas.py"),
        verification_check="preview_boot_smoke",
        instruction="Use the preview boot traceback to fix import-time, route registration, dependency, or DB initialization failures before touching UI flow code.",
        required_next_tool="read_files",
        suggested_tool_after_read="apply_patch_to_draft_or_write_file",
        verification_command="run_checks preview_boot_smoke",
        retry_policy="fix_boot_before_workflow_retry",
        forbidden_tools_once=("browser_verify",),
        expected_proof="preview_boot_smoke passes and the preview server returns a role page",
        patterns=(
            _rx(r"preview_boot_smoke"),
            _rx(r"preview.*(?:boot|start).*fail"),
            _rx(r"runtime did not start"),
            _rx(r"traceback.*uvicorn"),
        ),
    ),
    RepairCatalogEntry(
        signature="preview.browser_flow_failed",
        issue_code="failed_browser_flow_step",
        severity="critical",
        likely_root_cause="The product workflow could not be completed in browser proof.",
        target_files=("miniapp/app/static/**", "miniapp/app/routes/**", "miniapp/app/db.py"),
        verification_check="browser_flow_smoke",
        instruction="Use the failed browser step evidence to fix the exact prompt-derived interaction and reload proof, then rerun browser proof.",
        required_next_tool="read_files",
        verification_command="browser_verify",
        patterns=(_rx(r"browser_flow_smoke"), _rx(r"workflow_flow_failed"), _rx(r"browser proof")),
    ),
    RepairCatalogEntry(
        signature="preview.persistence_failed",
        issue_code="failed_persistence_proof",
        severity="critical",
        likely_root_cause="Create or update actions do not persist into shared state after list/reload.",
        target_files=("miniapp/app/routes/**", "miniapp/app/db.py", "miniapp/app/static/**/app.js"),
        verification_check="api_workflow_smoke",
        instruction="Fix the API persistence path first, then verify the frontend reads the same shared state.",
        required_next_tool="read_files",
        verification_command="browser_verify",
        patterns=(_rx(r"did not persist"), _rx(r"not reflect.*shared state"), _rx(r"api_workflow")),
    ),
    RepairCatalogEntry(
        signature="api.workflow_smoke_failed",
        issue_code="api_workflow_smoke_failed",
        severity="critical",
        likely_root_cause="The prompt-derived API workflow fails or returns an unexpected persisted marker.",
        target_files=("miniapp/app/routes/**", "miniapp/app/db.py", "miniapp/app/schemas.py", "miniapp/app/static/**/app.js", "miniapp/tests/test_generated_app.py"),
        verification_check="api_workflow_smoke",
        instruction="Align the API route, schema, persistence writes, and frontend payload contract so the expected prompt-derived markers survive a fresh read.",
        required_next_tool="read_files",
        suggested_tool_after_read="apply_patch_to_draft_or_write_file",
        verification_command="run_checks api_workflow_smoke",
        retry_policy="repair_api_then_browser",
        forbidden_tools_once=("browser_verify",),
        expected_proof="api_workflow_smoke passes with the expected prompt-derived persisted markers",
        patterns=(
            _rx(r"(?:create|update|list|post-update list|post-create list)\s+api\s+failed"),
            _rx(r"api_workflow_smoke"),
            _rx(r"expected persisted marker"),
        ),
    ),
    RepairCatalogEntry(
        signature="workflow.missing_role_actions",
        issue_code="missing_role_workflow_actions",
        severity="high",
        likely_root_cause="A required role page exists but lacks the prompt-assigned persisted action or shared-state view.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js", "miniapp/app/static/**/styles.css"),
        verification_check="platform_invariants",
        instruction="Read the acceptance contract and add only the missing role-owned controls and JS handlers. Use only the role responsibilities described by the LLM-derived contract.",
        required_next_tool="read_files",
        verification_command="run_checks platform_invariants",
        patterns=(_rx(r"missing_role_workflow_actions"), _rx(r"lacks its own workflow actions"), _rx(r"missing_role_actions")),
    ),
    RepairCatalogEntry(
        signature="workflow.raw_status_rendered_to_user",
        issue_code="raw_status_rendered_to_user",
        severity="high",
        likely_root_cause="A role UI maps known status values but still renders the raw persisted status for unknown or default cases.",
        target_files=("miniapp/app/static/**/app.js", "miniapp/app/static/**/index.html"),
        verification_check="platform_invariants",
        instruction=(
            "Read the implicated role app.js. Replace every direct persisted-state passthrough with a user-facing label "
            "derived from the contract or current UI copy; do not render `item.status`, `status`, `value`, or `key` "
            "as the visible default text."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks platform_invariants",
        patterns=(
            _rx(r"raw_status_rendered_to_user"),
            _rx(r"raw_status_copy"),
            _rx(r"status label raw passthrough"),
            _rx(r"renders persisted status codes directly"),
        ),
    ),
    RepairCatalogEntry(
        signature="frontend.unwired_button",
        issue_code="workflow_button_without_handler",
        severity="high",
        likely_root_cause="A visible workflow button is present in HTML but the role JavaScript never wires it.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Either wire the button to the intended handler or remove the button and expose the same action through an existing wired control.",
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks frontend_interaction_static_smoke",
        patterns=(
            _rx(r"workflow_button_without_handler"),
            _rx(r"has button #[A-Za-z0-9_-]+, but app\.js never references it"),
        ),
    ),
    RepairCatalogEntry(
        signature="frontend.unwired_form",
        issue_code="workflow_form_without_handler",
        severity="high",
        likely_root_cause="A visible workflow form is present in HTML but the role JavaScript never binds a submit/change handler for it.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js"),
        verification_check="frontend_interaction_static_smoke",
        instruction=(
            "Read the exact role HTML page and app.js. Bind the visible form to a submit/change handler, build the API payload from the form fields, "
            "persist it through the existing backend route, refresh rendered state, and keep optional controls guarded only after the required form is wired."
        ),
        required_next_tool="read_files",
        suggested_tool_after_read="write_file",
        verification_command="run_checks frontend_interaction_static_smoke",
        patterns=(
            _rx(r"workflow_form_without_handler"),
            _rx(r"workflow_form_without_submit_handler"),
            _rx(r"Visible workflow forms must have a submit handler"),
            _rx(r"no submit/change handler is wired"),
        ),
    ),
    RepairCatalogEntry(
        signature="workflow.cross_role_update_not_rendered_in_client",
        issue_code="cross_role_update_not_rendered_in_client",
        severity="high",
        likely_root_cause="A role persists update fields that another prompt-assigned observer/source role does not render after reload.",
        target_files=(
            "miniapp/app/static/client/app.js",
            "miniapp/app/static/specialist/app.js",
            "miniapp/app/static/manager/app.js",
            "miniapp/tests/generated_app.test.mjs",
        ),
        verification_check="frontend_interaction_static_smoke",
        instruction=(
            "Align prompt-assigned update payload fields with the role card/detail renderers so persisted changes are visible after reload "
            "where the acceptance contract says shared state must be observed."
        ),
        required_next_tool="read_files",
        verification_command="run_checks frontend_interaction_static_smoke",
        patterns=(
            _rx(r"cross_role_update_not_rendered_in_client"),
            _rx(r"cross_role_update_not_rendered_in_role"),
            _rx(r"never renders after reload"),
        ),
    ),
    RepairCatalogEntry(
        signature="workflow.shared_role_state_broken",
        issue_code="broken_shared_role_state",
        severity="high",
        likely_root_cause="Client, specialist, and manager surfaces are no longer reading or writing the same backend state contract.",
        target_files=("miniapp/app/routes/**", "miniapp/app/db.py", "miniapp/app/static/client/app.js", "miniapp/app/static/specialist/app.js", "miniapp/app/static/manager/app.js"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Trace the shared entity from create payload through backend persistence and every role renderer; repair the first role that diverges from the shared state contract.",
        required_next_tool="read_files",
        suggested_tool_after_read="apply_patch_to_draft_or_write_file",
        verification_command="run_checks frontend_interaction_static_smoke",
        retry_policy="repair_shared_state_contract",
        expected_proof="role-specific generated tests and frontend interaction smoke show the same persisted marker across roles",
        patterns=(
            _rx(r"broken shared role state"),
            _rx(r"shared role state"),
            _rx(r"persisted_marker"),
            _rx(r"update_marker"),
            _rx(r"created_marker"),
            _rx(r"updated_marker"),
            _rx(r"role state.*(?:diverge|broken|missing)"),
        ),
    ),
    RepairCatalogEntry(
        signature="workflow.payload_schema_mismatch",
        issue_code="workflow_patch_payload_field_mismatch",
        severity="high",
        likely_root_cause="Frontend sends fields the backend update schema does not accept, so role actions cannot persist the intended state.",
        target_files=("miniapp/app/routes", "miniapp/app/static/**/app.js", "miniapp/tests/test_generated_app.py", "miniapp/tests/generated_app.test.mjs"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Align the backend Pydantic update/create schemas, frontend JSON payloads, and generated tests so every prompt-required field is accepted and persisted.",
        required_next_tool="read_files",
        verification_command="run_checks frontend_interaction_static_smoke",
        patterns=(_rx(r"workflow_patch_payload_field_mismatch"), _rx(r"sends PATCH fields not accepted"), _rx(r"payload.*field.*mismatch")),
    ),
    RepairCatalogEntry(
        signature="ui.mobile_layout_blocked",
        issue_code="mobile_overflow_or_overlap",
        severity="high",
        likely_root_cause="Generated UI is not usable in the Telegram/mobile viewport.",
        target_files=("miniapp/app/static/**/styles.css", "miniapp/app/static/shared/base.css", "miniapp/app/static/**/index.html"),
        verification_check="browser_flow_smoke",
        instruction="Fix overflow/overlap using responsive layout constraints, then rerun mobile browser proof.",
        required_next_tool="read_files",
        verification_command="browser_verify mobile",
        patterns=(_rx(r"mobile_layout"), _rx(r"overflow"), _rx(r"overlap")),
    ),
    RepairCatalogEntry(
        signature="generation.repeated_no_progress",
        issue_code="repeated_no_progress_repair",
        severity="high",
        likely_root_cause="The agent repeated the same failed repair without changing the blocking condition.",
        target_files=("miniapp/app/**",),
        verification_check="repair_loop",
        instruction="Stop broad retries. Inspect the latest failure signature, target only implicated files, and make a different concrete patch.",
        auto_fixable=False,
        required_next_tool="read_files",
        verification_command="run_checks",
        retry_policy="do_not_retry_same_patch",
        patterns=(_rx(r"repeated"), _rx(r"no progress"), _rx(r"same failure")),
    ),
)


class RepairCatalog:
    @staticmethod
    def entries() -> list[dict[str, Any]]:
        return [entry.packet() for entry in REPAIR_CATALOG]

    @staticmethod
    def classify_issue(issue: dict[str, Any]) -> dict[str, Any]:
        embedded = RepairCatalog._packet_from_embedded_recipe(issue)
        if embedded:
            return embedded
        text = RepairCatalog._issue_text(issue)
        for entry in REPAIR_CATALOG:
            if entry.signature in text or entry.issue_code in text:
                return entry.packet(evidence=issue)
        for entry in REPAIR_CATALOG:
            if any(pattern.search(text) for pattern in entry.patterns):
                return entry.packet(evidence=issue)
        return {
            "signature": str(issue.get("signature") or issue.get("failure_signature") or "generation.unknown_failure"),
            "issue_code": str(issue.get("code") or issue.get("check") or "unknown_failure"),
            "code": str(issue.get("code") or issue.get("check") or "unknown_failure"),
            "severity": str(issue.get("severity") or "medium"),
            "likely_root_cause": str(issue.get("details") or issue.get("message") or "The run failed without a catalogued signature."),
            "target_files": list(issue.get("paths") or []),
            "verification_check": str(issue.get("check") or "checks.run"),
            "verification_command": str(issue.get("verification_command") or "run_checks"),
            "instruction": "Inspect the concrete check logs, patch the implicated files, and rerun the failing check.",
            "auto_fixable": False,
            "required_next_tool": str(issue.get("required_next_tool") or "read_files"),
            "suggested_tool_after_read": str(issue.get("suggested_tool_after_read") or "apply_patch_to_draft_or_write_file"),
            "retry_policy": str(issue.get("retry_policy") or "manual_triage"),
            "retryable": bool(issue.get("retryable", False)),
            "deterministic": bool(issue.get("deterministic", True)),
            "failure_class": str(issue.get("failure_class") or issue.get("check") or "checks.run"),
            "failure_signature": str(issue.get("failure_signature") or issue.get("signature") or "generation.unknown_failure"),
            "repair_recipe_id": str(issue.get("repair_recipe_id") or f"catalog.{issue.get('code') or issue.get('check') or 'unknown_failure'}"),
            "forbidden_tools_once": list(issue.get("forbidden_tools_once") or []),
            "expected_proof": str(issue.get("expected_proof") or issue.get("check") or "rerun failing check successfully"),
            "next_forced_action": {
                "required_next_tool": str(issue.get("required_next_tool") or "read_files"),
                "target_files": list(issue.get("paths") or []),
                "verification_check": str(issue.get("check") or "checks.run"),
            },
            "evidence": issue,
        }

    @staticmethod
    def _issue_text(value: Any) -> str:
        parts: list[str] = []

        def visit(item: Any) -> None:
            if item is None:
                return
            if isinstance(item, str):
                parts.append(item)
                return
            if isinstance(item, (int, float, bool)):
                parts.append(str(item))
                return
            if isinstance(item, dict):
                for key, nested in item.items():
                    parts.append(str(key))
                    visit(nested)
                return
            if isinstance(item, (list, tuple, set)):
                for nested in item:
                    visit(nested)

        visit(value)
        return " ".join(parts)

    @classmethod
    def classify_many(cls, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        packets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            packet = cls.classify_issue(issue)
            key = str(packet.get("signature") or packet.get("issue_code") or len(packets))
            if key in seen:
                continue
            seen.add(key)
            packets.append(packet)
        return packets

    @staticmethod
    def _packet_from_embedded_recipe(issue: dict[str, Any]) -> dict[str, Any] | None:
        """Prefer typed repair recipes emitted by validators over broad text matching."""

        candidates = RepairCatalog._embedded_recipe_candidates(issue)
        if not candidates:
            return None
        candidate = candidates[0]
        recipe = candidate.get("repair_recipe")
        if not isinstance(recipe, dict):
            return None
        code = str(candidate.get("code") or recipe.get("failure_signature") or issue.get("code") or "validator_repair")
        issue_code = code.removeprefix("platform.")
        signature = str(recipe.get("failure_signature") or candidate.get("signature") or issue_code)
        catalog_entry = RepairCatalog._entry_for_signature(signature, issue_code)
        evidence = {
            "validator_issue": {k: v for k, v in candidate.items() if k != "repair_recipe"},
            "repair_recipe": recipe,
            "source_issue": issue,
        }
        target_files = RepairCatalog._target_files_from_recipe(
            catalog_entry=catalog_entry,
            recipe=recipe,
            issue=issue,
            candidate=candidate,
        )
        verification_check = str(
            (catalog_entry.verification_check if catalog_entry else None)
            or recipe.get("verification_check")
            or issue.get("check")
            or "checks.run"
        )
        return {
            "signature": signature,
            "issue_code": issue_code,
            "code": issue_code,
            "severity": str(candidate.get("severity") or (catalog_entry.severity if catalog_entry else None) or issue.get("severity") or "high"),
            "likely_root_cause": str(
                candidate.get("message")
                or (catalog_entry.likely_root_cause if catalog_entry else None)
                or issue.get("details")
                or "Validator emitted a structured repair recipe."
            ),
            "target_files": target_files,
            "verification_check": verification_check,
            "verification_command": str((catalog_entry.verification_command if catalog_entry else None) or recipe.get("verification_command") or "run_checks"),
            "instruction": str(
                (catalog_entry.instruction if catalog_entry else None)
                or recipe.get("instruction")
                or "Read the target files, apply the validator repair recipe, and rerun the failing check."
            ),
            "auto_fixable": bool(recipe.get("retryable", catalog_entry.auto_fixable if catalog_entry else True)),
            "required_next_tool": str((catalog_entry.required_next_tool if catalog_entry else None) or recipe.get("required_next_tool") or "read_files"),
            "suggested_tool_after_read": str(
                (catalog_entry.suggested_tool_after_read if catalog_entry else None)
                or recipe.get("suggested_tool_after_read")
                or "apply_patch_to_draft_or_write_file"
            ),
            "retry_policy": str((catalog_entry.retry_policy if catalog_entry else None) or recipe.get("retry_policy") or "deterministic_repair"),
            "retryable": bool(recipe.get("retryable", catalog_entry.auto_fixable if catalog_entry else True)),
            "deterministic": bool(recipe.get("deterministic", catalog_entry.deterministic if catalog_entry else True)),
            "failure_class": str(recipe.get("failure_class") or issue.get("failure_class") or verification_check),
            "failure_signature": signature,
            "repair_recipe_id": str(recipe.get("recipe_id") or f"catalog.{issue_code}"),
            "forbidden_tools_once": [],
            "next_forced_action": {
                "required_next_tool": str(recipe.get("required_next_tool") or "read_files"),
                "target_files": target_files,
                "verification_check": verification_check,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _embedded_recipe_candidates(value: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def visit(item: Any) -> None:
            if item is None:
                return
            if isinstance(item, str):
                text = item.strip()
                if not (text.startswith("{") and "repair_recipe" in text):
                    return
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return
                visit(parsed)
                return
            if isinstance(item, dict):
                if isinstance(item.get("repair_recipe"), dict):
                    candidates.append(item)
                for nested in item.values():
                    visit(nested)
                return
            if isinstance(item, (list, tuple, set)):
                for nested in item:
                    visit(nested)

        visit(value)
        candidates.sort(
            key=lambda item: (
                0 if item.get("blocking", True) else 1,
                {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(item.get("severity") or "").lower(), 4),
            )
        )
        return candidates

    @staticmethod
    def _target_files_from_recipe(
        *,
        catalog_entry: RepairCatalogEntry | None,
        recipe: dict[str, Any],
        issue: dict[str, Any],
        candidate: dict[str, Any],
    ) -> list[str]:
        paths: list[str] = []
        missing_backend_route = str(candidate.get("code") or "").endswith("missing_backend_route") or bool(recipe.get("expected_route"))

        def add(value: object) -> None:
            text = str(value or "").strip().replace("\\", "/")
            if not text:
                return
            if ":" in text and text.startswith("miniapp/"):
                text = text.split(":", 1)[0].strip()
            if text.startswith("miniapp/app/generated/"):
                return
            if text.startswith("miniapp/") and text not in paths:
                paths.append(text)

        for value in recipe.get("target_files") or []:
            add(value)
        for value in issue.get("paths") or []:
            add(value)
        add(candidate.get("location"))
        add(recipe.get("frontend_ref"))
        add(recipe.get("suggested_patch_target"))
        if catalog_entry is not None and not missing_backend_route:
            for value in catalog_entry.target_files:
                add(value)
        if missing_backend_route:
            add(recipe.get("frontend_ref"))
            add("miniapp/app/routes")
            add("miniapp/app/routes/api.py")
            add("miniapp/app/main.py")
            add("miniapp/app/schemas.py")
        if not paths and catalog_entry is not None:
            for value in catalog_entry.target_files:
                add(value)
        return paths[:12]

    @staticmethod
    def _entry_for_signature(signature: str, issue_code: str) -> RepairCatalogEntry | None:
        normalized_issue = str(issue_code or "").split(".")[-1]
        for entry in REPAIR_CATALOG:
            if entry.signature == signature or entry.issue_code == issue_code or entry.issue_code == normalized_issue:
                return entry
        return None
