from __future__ import annotations

from datetime import datetime, timezone
import re
from pathlib import Path
from typing import Any


MEMORY_KINDS = {"preference", "product_decision", "working_pattern", "failure_signature", "avoidance"}
SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|token|secret|password)\s*=", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object, *, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _secret_free(text: str) -> bool:
    return not any(pattern.search(text) for pattern in SECRET_PATTERNS)


class WorkspaceMemoryPipeline:
    """Deterministic two-phase memory extraction for generation runs."""

    @staticmethod
    def extract_run(run: Any, artifacts: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []

        def add(kind: str, text: str, payload: dict[str, Any] | None = None) -> None:
            text = _clean(text)
            if not text or kind not in MEMORY_KINDS or not _secret_free(text):
                return
            items.append(
                {
                    "memory_id": f"stage1_{run.run_id}_{len(items) + 1}",
                    "kind": kind,
                    "text": text,
                    "status": "candidate",
                    "citation": {
                        "run_id": run.run_id,
                        "workspace_id": run.workspace_id,
                        "artifact_refs": WorkspaceMemoryPipeline._artifact_refs(run),
                    },
                    "payload": payload or {},
                    "created_at": _now(),
                }
            )

        contract = dict(run.acceptance_contract or {})
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        primary_entities = (run.implementation_plan or {}).get("primary_entities") if isinstance(run.implementation_plan, dict) else []
        resource = prompt_hints.get("resource_hint") or (primary_entities[0] if primary_entities else None)
        if resource:
            add(
                "product_decision",
                f"Product workflow centers on `{resource}` with role responsibilities from the prompt contract.",
                {"resource_hint": resource, "roles": contract.get("roles", [])},
            )
        if run.status == "completed" and run.apply_status == "applied":
            add(
                "working_pattern",
                "Completed run reached applied state; prefer preserving its persisted workflow, generated tests, and browser proof shape during later edits.",
                {"touched_files": list(run.touched_files or [])[:20]},
            )
        failure_signature = run.failure_signature or run.failure_class
        if failure_signature:
            add(
                "failure_signature",
                f"Run hit `{failure_signature}`: {run.root_cause_summary or run.failure_reason or 'repair evidence is available in run artifacts'}.",
                {"failure_class": run.failure_class, "failure_signature": run.failure_signature},
            )
            add(
                "avoidance",
                f"Avoid repeating the failure pattern `{failure_signature}`; start future repair from the stored repair packet and failing check evidence.",
                {"failure_class": run.failure_class, "failure_signature": run.failure_signature},
            )
        for item in run.repair_issue_signatures or []:
            if isinstance(item, dict) and item.get("signature"):
                add(
                    "failure_signature",
                    f"Repair signature `{item.get('signature')}` appeared for check `{item.get('check') or 'unknown'}`.",
                    item,
                )
        for check in artifacts.get("check_results") or []:
            if isinstance(check, dict) and str(check.get("status") or "") in {"failed", "blocked"}:
                add(
                    "failure_signature",
                    f"Check `{check.get('name')}` failed: {check.get('details') or 'see check logs'}.",
                    {"check": check.get("name"), "logs": list(check.get("logs") or [])[-5:]},
                )
        status = "empty" if not items else "extracted"
        return {
            "schema": "grounded.memory_stage1.v1",
            "workspace_id": run.workspace_id,
            "run_id": run.run_id,
            "status": status,
            "items": WorkspaceMemoryPipeline._dedupe(items),
            "created_at": _now(),
        }

    @staticmethod
    def consolidate(workspace_id: str, stage1_payloads: list[dict[str, Any]], existing: dict[str, Any]) -> dict[str, Any]:
        items = [item for item in existing.get("items") or [] if isinstance(item, dict)]
        seen = {WorkspaceMemoryPipeline._key(item) for item in items}
        for payload in stage1_payloads:
            for raw_item in payload.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                key = WorkspaceMemoryPipeline._key(raw_item)
                if not key or key in seen:
                    continue
                seen.add(key)
                item = {
                    **raw_item,
                    "memory_id": raw_item.get("memory_id") or f"mem_{len(items) + 1}",
                    "status": "active",
                    "source": "memory_pipeline",
                    "consolidated_at": _now(),
                }
                items.append(item)
        current = dict(existing or {})
        current["workspace_id"] = workspace_id
        current["items"] = items[-200:]
        current["pipeline"] = {
            "schema": "grounded.memory_pipeline.v1",
            "stage1_count": len(stage1_payloads),
            "active_count": len(current["items"]),
            "updated_at": _now(),
        }
        WorkspaceMemoryPipeline._populate_buckets(current)
        return current

    @staticmethod
    def stale_check(workspace_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        path_pattern = re.compile(r"\bminiapp/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b")
        checks: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            paths = sorted(set(path_pattern.findall(str(item.get("text") or ""))))[:12]
            checks.extend({"memory_id": item.get("memory_id"), "path": path, "exists": (workspace_root / path).exists()} for path in paths)
        return {
            "status": "stale" if any(not check["exists"] for check in checks) else "fresh_or_unreferenced",
            "checks": checks,
        }

    @staticmethod
    def _artifact_refs(run: Any) -> dict[str, Any]:
        return {
            "trace_bundle": getattr(run, "trace_bundle_ref", None),
            "trace_reducer": getattr(run, "trace_reducer_ref", None),
            "verification": getattr(run, "verification_report_ref", None),
            "memory": getattr(run, "memory_ref", None),
        }

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            key = WorkspaceMemoryPipeline._key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _key(item: dict[str, Any]) -> str:
        return f"{item.get('kind')}:{_clean(item.get('text'), limit=260).lower()}"

    @staticmethod
    def _populate_buckets(payload: dict[str, Any]) -> None:
        bucket_map = {
            "preference": "user_preferences",
            "product_decision": "product_decisions",
            "working_pattern": "architecture_summary",
            "failure_signature": "known_failures",
            "avoidance": "rejected_approaches",
        }
        for bucket in bucket_map.values():
            payload[bucket] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            bucket = bucket_map.get(str(item.get("kind") or ""))
            if bucket:
                payload.setdefault(bucket, []).append(item)
