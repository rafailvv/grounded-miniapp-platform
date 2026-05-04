from __future__ import annotations

import re
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
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)

    def packet(self, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "issue_code": self.issue_code,
            "severity": self.severity,
            "likely_root_cause": self.likely_root_cause,
            "target_files": list(self.target_files),
            "verification_check": self.verification_check,
            "instruction": self.instruction,
            "auto_fixable": self.auto_fixable,
            "evidence": evidence or {},
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
        patterns=(_rx(r"\bsyntaxerror\b"), _rx(r"node --check"), _rx(r"unexpected token")),
    ),
    RepairCatalogEntry(
        signature="frontend.missing_dom_id",
        issue_code="missing_dom_id",
        severity="high",
        likely_root_cause="JavaScript expects a DOM id or form field that the generated HTML does not expose.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Align HTML ids/form names and JS selectors. Prefer preserving the intended user workflow over deleting handlers.",
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
        patterns=(_rx(r"missing_backend_route"), _rx(r"frontend.*api.*missing"), _rx(r"404")),
    ),
    RepairCatalogEntry(
        signature="backend.fastapi_response_model",
        issue_code="fastapi_response_model_error",
        severity="critical",
        likely_root_cause="A FastAPI route exposes an invalid dependency or response field, often a Session object in the signature.",
        target_files=("miniapp/app/routes/**", "miniapp/app/schemas.py", "miniapp/app/db.py"),
        verification_check="preview_boot_smoke",
        instruction="Move DB sessions to Depends/get_db patterns and keep response payloads Pydantic/JSON serializable.",
        patterns=(_rx(r"invalid args for response field"), _rx(r"sqlalchemy\.orm\.session\.session"), _rx(r"fastapi")),
    ),
    RepairCatalogEntry(
        signature="database.sqlite_schema_drift",
        issue_code="sqlite_missing_table_or_column",
        severity="critical",
        likely_root_cause="Generated persistence code reads or writes a table/column that DB initialization does not create.",
        target_files=("miniapp/app/db.py", "miniapp/app/routes/**", "miniapp/app/schemas.py"),
        verification_check="api_workflow_smoke",
        instruction="Update schema initialization and route SQL together, then verify create/list/update persistence.",
        patterns=(_rx(r"no such table"), _rx(r"has no column named"), _rx(r"operationalerror")),
    ),
    RepairCatalogEntry(
        signature="contract.route_manifest_drift",
        issue_code="route_manifest_drift",
        severity="high",
        likely_root_cause="Route manifest, generated contract, and filesystem role pages disagree.",
        target_files=("miniapp/app/generated/route_manifest.json", "miniapp/app/static/**/index.html"),
        verification_check="schema_validators",
        instruction="Regenerate or repair route manifest entries so each role route maps to the actual static page.",
        patterns=(_rx(r"route_manifest"), _rx(r"duplicate_static_route"), _rx(r"declared routes")),
    ),
    RepairCatalogEntry(
        signature="preview.blank_screen",
        issue_code="blank_preview",
        severity="critical",
        likely_root_cause="Preview loads but renders no meaningful role content.",
        target_files=("miniapp/app/static/**/index.html", "miniapp/app/static/**/app.js", "miniapp/app/static/**/styles.css"),
        verification_check="browser_flow_smoke",
        instruction="Restore visible role UI, remove template-only content, and verify each role route renders non-empty product content.",
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
        patterns=(_rx(r"no(?: declared)? pages?.*role"), _rx(r"missing_role_page"), _rx(r"missing.*role.*page"), _rx(r"/(?:client|specialist|manager)")),
    ),
    RepairCatalogEntry(
        signature="preview.browser_flow_failed",
        issue_code="failed_browser_flow_step",
        severity="critical",
        likely_root_cause="The product workflow could not be completed in browser proof.",
        target_files=("miniapp/app/static/**", "miniapp/app/routes/**", "miniapp/app/db.py"),
        verification_check="browser_flow_smoke",
        instruction="Use the failed browser step evidence to fix the exact create/update/list/reload interaction, then rerun browser proof.",
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
        patterns=(_rx(r"did not persist"), _rx(r"not reflect.*shared state"), _rx(r"api_workflow")),
    ),
    RepairCatalogEntry(
        signature="workflow.missing_role_actions",
        issue_code="missing_role_workflow_actions",
        severity="high",
        likely_root_cause="A required role page exists but lacks prompt-specific create/process/oversight actions.",
        target_files=("miniapp/app/static/manager/index.html", "miniapp/app/static/manager/app.js", "miniapp/app/static/manager/styles.css"),
        verification_check="platform_invariants",
        instruction="Add the missing role-specific controls and JS handlers. For manager, expose dashboard/oversight actions that mutate shared workflow state through the accepted API.",
        patterns=(_rx(r"missing_role_workflow_actions"), _rx(r"lacks its own workflow actions"), _rx(r"missing_role_actions")),
    ),
    RepairCatalogEntry(
        signature="workflow.payload_schema_mismatch",
        issue_code="workflow_patch_payload_field_mismatch",
        severity="high",
        likely_root_cause="Frontend sends fields the backend update schema does not accept, so role actions cannot persist the intended state.",
        target_files=("miniapp/app/routes/generated_contract.py", "miniapp/app/static/**/app.js", "miniapp/tests/test_generated_app.py", "miniapp/tests/generated_app.test.mjs"),
        verification_check="frontend_interaction_static_smoke",
        instruction="Align the backend Pydantic update/create schemas, frontend JSON payloads, and generated tests so every prompt-required field is accepted and persisted.",
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
        patterns=(_rx(r"repeated"), _rx(r"no progress"), _rx(r"same failure")),
    ),
)


class RepairCatalog:
    @staticmethod
    def entries() -> list[dict[str, Any]]:
        return [entry.packet() for entry in REPAIR_CATALOG]

    @staticmethod
    def classify_issue(issue: dict[str, Any]) -> dict[str, Any]:
        text = RepairCatalog._issue_text(issue)
        for entry in REPAIR_CATALOG:
            if entry.signature in text or any(pattern.search(text) for pattern in entry.patterns):
                return entry.packet(evidence=issue)
        return {
            "signature": str(issue.get("signature") or issue.get("failure_signature") or "generation.unknown_failure"),
            "issue_code": str(issue.get("code") or issue.get("check") or "unknown_failure"),
            "severity": str(issue.get("severity") or "medium"),
            "likely_root_cause": str(issue.get("details") or issue.get("message") or "The run failed without a catalogued signature."),
            "target_files": list(issue.get("paths") or []),
            "verification_check": str(issue.get("check") or "checks.run"),
            "instruction": "Inspect the concrete check logs, patch the implicated files, and rerun the failing check.",
            "auto_fixable": False,
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
