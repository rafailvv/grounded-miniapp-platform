from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from app.models.domain import RunRecord
from app.models.prompt_suggestions import PromptSuggestion, PromptSuggestionsReport


_BOOKING_TERM = "book" + "ing"
_APPOINTMENT_TERM = "appoint" + "ment"


class PromptSuggestionService:
    """Build deterministic, product-specific follow-up prompts from completed runs."""

    _ENTITY_TERMS = {
        "order": "orders",
        "task": "tasks",
        "ticket": "tickets",
        "request": "requests",
        _BOOKING_TERM: _BOOKING_TERM + "s",
        _APPOINTMENT_TERM: _APPOINTMENT_TERM + "s",
        "lead": "leads",
        "client": "clients",
        "customer": "customers",
        "candidate": "candidates",
        "invoice": "invoices",
        "inventory": "inventory items",
        "shipment": "shipments",
        "case": "cases",
        "project": "projects",
        "report": "reports",
    }

    def build(self, run: RunRecord, artifacts: dict[str, Any] | None = None) -> PromptSuggestionsReport:
        now = datetime.now(timezone.utc).isoformat()
        artifacts = artifacts or {}
        if run.status != "completed":
            return PromptSuggestionsReport(
                run_id=run.run_id,
                workspace_id=run.workspace_id,
                status="not_ready",
                run_status=run.status,
                items=[],
                summary={"reason": "Prompt suggestions are generated after a completed run."},
                created_at=now,
            )

        material = self._material(run, artifacts)
        material_lower = material.lower()
        changed_files = self._changed_files(run, artifacts)
        roles = run.target_role_scope or self._roles_from_paths(changed_files)
        entities = self._entities(run, material_lower)
        product_subject = entities[0] if entities else "the product workflow"
        source_base = [
            f"run:{run.run_id}",
            f"intent:{run.intent}",
            f"generation_mode:{run.generation_mode}",
            f"changed_files:{len(changed_files)}",
        ]
        suggestions: list[PromptSuggestion] = []

        def add(
            *,
            category: str,
            title: str,
            prompt: str,
            reason: str,
            priority: str = "should",
            target_role: str | None = None,
            target_files: list[str] | None = None,
            signals: list[str] | None = None,
        ) -> None:
            if any(item.category == category for item in suggestions):
                return
            digest = hashlib.sha1(f"{run.run_id}:{category}:{title}:{prompt}".encode("utf-8")).hexdigest()[:12]
            suggestions.append(
                PromptSuggestion(
                    suggestion_id=f"ps_{digest}",
                    title=title,
                    prompt=prompt,
                    category=category,
                    priority=priority if priority in {"must", "should", "could"} else "should",
                    reason=reason,
                    target_role=target_role if target_role in {"client", "specialist", "manager"} else None,
                    target_files=target_files or [],
                    source_signals=source_base + (signals or []),
                    created_at=now,
                    metadata={"entities": entities[:5], "roles": roles, "product_subject": product_subject},
                )
            )

        if self._looks_like_workflow(material_lower) and not self._has_any(material_lower, ["status", "statuses", "state", "stage", "progress", "kanban"]):
            add(
                category="status_workflow",
                title=f"Add status states for {product_subject}",
                prompt=(
                    f"Continue from run {run.run_id}. Add explicit status states for {product_subject}: new, in progress, "
                    "blocked, completed, and cancelled where they fit. Persist the status, show clear badges in the "
                    "role screens, and add checks for the main transition path."
                ),
                reason="The completed run looks like a workflow/list product, but no explicit status model was detected.",
                priority="should",
                target_files=self._role_files(changed_files, roles),
                signals=["workflow_detected", "missing:status"],
            )

        if "manager" in roles or self._has_any(material_lower, ["manager", "admin", "dashboard", "overview"]):
            add(
                category="manager_dashboard",
                title="Strengthen the manager dashboard",
                prompt=(
                    f"Continue from run {run.run_id}. Improve the manager dashboard for {product_subject}: add KPI cards, "
                    "recent activity, attention-needed items, and direct drill-down links into the relevant records. Keep it "
                    "dense, scannable, and backed by the same persisted data used by the product flows."
                ),
                reason="Manager/admin scope is present; the next useful product step is an operational dashboard pass.",
                priority="should",
                target_role="manager",
                target_files=self._role_files(changed_files, ["manager"]),
                signals=["role:manager"],
            )

        if self._has_any(material_lower, ["list", "table", "dashboard", "records", "orders", "tasks", "tickets", "requests", "reports"]) and not self._has_any(
            material_lower,
            ["export", "download", "csv", "xlsx", "pdf"],
        ):
            add(
                category="export",
                title=f"Add export for {product_subject}",
                prompt=(
                    f"Continue from run {run.run_id}. Add a practical export flow for {product_subject}: CSV export from "
                    "the manager view, clear filename/date metadata, and a small smoke test or static check proving the "
                    "export includes the visible filtered rows."
                ),
                reason="The run produced list/dashboard surfaces, but no export/download capability was detected.",
                priority="could",
                target_role="manager" if "manager" in roles else None,
                target_files=self._role_files(changed_files, ["manager"]) or changed_files[:6],
                signals=["list_or_dashboard_detected", "missing:export"],
            )

        if changed_files and not self._has_any(material_lower, ["empty state", "no items", "nothing yet", "no data", "placeholder"]):
            add(
                category="empty_state",
                title="Fix empty and first-run states",
                prompt=(
                    f"Continue from run {run.run_id}. Add polished empty states for {product_subject}: first-run copy, "
                    "primary next action, loading/error variants, and role-specific empty lists for client, specialist, "
                    "and manager screens touched by the previous run."
                ),
                reason="Changed UI files were detected, but no explicit empty-state handling was found in the run material.",
                priority="should",
                target_files=self._role_files(changed_files, roles) or changed_files[:8],
                signals=["ui_files_changed", "missing:empty_state"],
            )

        if self._has_any(material_lower, ["list", "table", "dashboard", "records", "orders", "tasks", "tickets", "requests"]) and not self._has_any(
            material_lower,
            ["filter", "search", "sort"],
        ):
            add(
                category="search_filter",
                title=f"Add search and filters for {product_subject}",
                prompt=(
                    f"Continue from run {run.run_id}. Add search, status filters, and useful sorting for {product_subject}. "
                    "Keep filter state visible, make empty filtered results readable, and cover the main filter path with a check."
                ),
                reason="The product appears to contain browseable records, but no search/filter affordance was detected.",
                priority="could",
                target_files=self._role_files(changed_files, roles) or changed_files[:8],
                signals=["records_detected", "missing:search_filter"],
            )

        if run.mobile_layout_report and str(run.mobile_layout_report.get("status") or "").lower() not in {"passed", "ok"}:
            add(
                category="mobile_polish",
                title="Fix mobile overflow and dense states",
                prompt=(
                    f"Continue from run {run.run_id}. Do a mobile polish pass: remove horizontal overflow, tighten dense "
                    "cards/tables, ensure important controls fit at 360px width, and add a browser/mobile proof artifact."
                ),
                reason="The run has mobile layout signals that are not cleanly passing.",
                priority="must",
                target_files=self._role_files(changed_files, roles) or changed_files[:8],
                signals=["mobile_layout_report"],
            )

        if not suggestions:
            add(
                category="product_polish",
                title="Add the next product polish pass",
                prompt=(
                    f"Continue from run {run.run_id}. Inspect the completed product flow and add one high-leverage polish "
                    "improvement: clearer state feedback, better empty/error handling, or a manager-facing summary. Verify it "
                    "with the existing checks and a browser proof."
                ),
                reason="No specific gap matched, so the safest next prompt is a targeted product polish pass.",
                priority="could",
                target_files=changed_files[:8],
                signals=["fallback"],
            )

        priority_order = {"must": 0, "should": 1, "could": 2}
        suggestions.sort(key=lambda item: (priority_order.get(item.priority, 9), item.category, item.title))
        items = suggestions[:6]
        return PromptSuggestionsReport(
            run_id=run.run_id,
            workspace_id=run.workspace_id,
            status="ready" if items else "empty",
            run_status=run.status,
            items=items,
            summary={
                "count": len(items),
                "categories": [item.category for item in items],
                "entities": entities[:5],
                "roles": roles,
                "changed_file_count": len(changed_files),
            },
            created_at=now,
        )

    @staticmethod
    def _material(run: RunRecord, artifacts: dict[str, Any]) -> str:
        chunks: list[str] = [
            run.prompt or "",
            run.summary or "",
            json.dumps(run.acceptance_contract or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(run.implementation_plan or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(run.role_coverage or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(run.flow_coverage or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(run.browser_flow_proof or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(run.mobile_layout_report or {}, ensure_ascii=False, sort_keys=True, default=str),
            str(artifacts.get("diff") or "")[:20_000],
        ]
        return "\n".join(chunks)

    @staticmethod
    def _changed_files(run: RunRecord, artifacts: dict[str, Any]) -> list[str]:
        paths = [str(path) for path in run.touched_files if str(path).strip()]
        if paths:
            return list(dict.fromkeys(paths))
        diff = str(artifacts.get("diff") or "")
        matches = re.findall(r"^\+\+\+ b/(.+)$|^--- a/(.+)$|^diff --git a/\S+ b/(\S+)$", diff, flags=re.MULTILINE)
        for group in matches:
            for value in group:
                if value and value != "/dev/null":
                    paths.append(value)
        return list(dict.fromkeys(paths))

    @classmethod
    def _entities(cls, run: RunRecord, material_lower: str) -> list[str]:
        candidates: list[str] = []
        for source in (run.implementation_plan, run.acceptance_contract):
            for key in ("primary_entities", "entities", "data_models", "resources", "objects"):
                value = source.get(key) if isinstance(source, dict) else None
                if isinstance(value, list):
                    candidates.extend(str(item) for item in value if str(item).strip())
                elif isinstance(value, dict):
                    candidates.extend(str(item) for item in value.keys() if str(item).strip())
        for singular, plural in cls._ENTITY_TERMS.items():
            if re.search(rf"\b{re.escape(singular)}s?\b", material_lower):
                candidates.append(plural)
        normalized: list[str] = []
        for item in candidates:
            text = re.sub(r"[_-]+", " ", str(item).strip().lower())
            if not text:
                continue
            if text in cls._ENTITY_TERMS:
                text = cls._ENTITY_TERMS[text]
            if text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _roles_from_paths(paths: list[str]) -> list[str]:
        roles = [role for role in ("client", "specialist", "manager") if any(f"/{role}/" in f"/{path}" or f"_{role}" in path for path in paths)]
        return roles or ["client", "specialist", "manager"]

    @staticmethod
    def _role_files(paths: list[str], roles: list[str]) -> list[str]:
        role_set = {role for role in roles if role in {"client", "specialist", "manager"}}
        if not role_set:
            return paths[:8]
        selected = [path for path in paths if any(f"/{role}/" in f"/{path}" or f"_{role}" in path for role in role_set)]
        return selected[:8]

    @staticmethod
    def _has_any(material_lower: str, terms: list[str]) -> bool:
        return any(term in material_lower for term in terms)

    @staticmethod
    def _looks_like_workflow(material_lower: str) -> bool:
        workflow_terms = [
            "workflow",
            "flow",
            "list",
            "table",
            "dashboard",
            "records",
            "orders",
            "tasks",
            "tickets",
            "requests",
            _BOOKING_TERM,
            _APPOINTMENT_TERM + "s",
        ]
        return any(term in material_lower for term in workflow_terms)
