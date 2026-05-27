from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from pathlib import Path
from typing import Any

from app.models.memory import (
    ConsolidatedMemoryItem,
    MemoryCitation,
    MemoryConfidence,
    MemoryExpiry,
    MemoryFailureShield,
    MemoryRetrievalResult,
    MemorySummaryReport,
    MemorySummarySection,
    MemoryStaleCheck,
    RawMemoryItem,
    RunMemoryBatch,
)
from app.services.repair_catalog import RepairCatalog


MEMORY_KINDS = {
    "preference",
    "product_fact",
    "product_decision",
    "ui_vocabulary",
    "persistence_schema_decision",
    "working_pattern",
    "reusable_workflow",
    "failure_signature",
    "failure_shield",
    "known_failure_recipe",
    "successful_app_pattern",
    "avoidance",
}
PRODUCT_MEMORY_TYPES = (
    "preferences",
    "product_facts",
    "known_failures",
    "successful_patterns",
    "rejected_approaches",
    "ui_vocabulary",
    "persistence_schema_decisions",
)
KIND_TO_MEMORY_TYPE = {
    "preference": "preferences",
    "user_preference": "preferences",
    "project_rule": "preferences",
    "product_fact": "product_facts",
    "product_decision": "product_facts",
    "architecture": "product_facts",
    "failure_signature": "known_failures",
    "failure_shield": "known_failures",
    "known_failure_recipe": "known_failures",
    "working_pattern": "successful_patterns",
    "reusable_workflow": "successful_patterns",
    "successful_app_pattern": "successful_patterns",
    "avoidance": "rejected_approaches",
    "rejected_approach": "rejected_approaches",
    "ui_vocabulary": "ui_vocabulary",
    "ux_rule": "ui_vocabulary",
    "persistence_schema_decision": "persistence_schema_decisions",
}
SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|token|secret|password)\s*=", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
PATH_PATTERN = re.compile(r"\b(?:miniapp|app|tests|runtime)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b")
TOKEN_PATTERN = re.compile(r"[A-Za-zА-Яа-я0-9_]{3,}")
SENTENCE_SPLIT_RE = re.compile(r"[\n\r.!?;]+")
PREFERENCE_POSITIVE_RE = re.compile(
    r"\b(prefer|i like|we like|make sure|important|must keep)\b|"
    r"(предпочитаю|люблю|нравится|важно|хочу|мне нужно|нужно чтобы)",
    re.I,
)
PREFERENCE_NEGATIVE_RE = re.compile(
    r"\b(avoid|do not|don't|never|dislike|hate|without)\b|"
    r"(не\s+люблю|не\s+нравится|не\s+хочу|избегай|без\s+лишн|не\s+делай)",
    re.I,
)
PRODUCT_DECISION_RE = re.compile(
    r"\b(my goal|goal is|product should|the app should|the task is)\b|"
    r"(моя задача|цель|задача|продукт должен|приложение должно|нужно быстро)",
    re.I,
)
WORKFLOW_RE = re.compile(
    r"\b(workflow|pipeline|repeatable|reusable|step|repair loop|generation loop)\b|"
    r"(воркфлоу|пайплайн|процесс|сценарий|повторн|легко.*исправ|быстро.*создавать)",
    re.I,
)
SUMMARY_KIND_ORDER = (
    "preference",
    "product_fact",
    "product_decision",
    "ui_vocabulary",
    "persistence_schema_decision",
    "failure_shield",
    "known_failure_recipe",
    "successful_app_pattern",
    "reusable_workflow",
    "working_pattern",
    "failure_signature",
    "avoidance",
)
STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "как",
    "что",
    "для",
    "или",
    "это",
    "нужно",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object, *, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _secret_free(text: str) -> bool:
    return not any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _tokens(value: object) -> set[str]:
    tokens: set[str] = set()
    for raw_token in TOKEN_PATTERN.findall(str(value or "")):
        token = raw_token.lower()
        if token in STOP_WORDS:
            continue
        tokens.add(token)
        if token.endswith("s") and len(token) > 4:
            tokens.add(token[:-1])
    return tokens


class WorkspaceMemoryPipeline:
    """Deterministic two-phase memory extraction for generation runs."""

    @staticmethod
    def extract_run(run: Any, artifacts: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        created_at = _now()

        def add(kind: str, text: str, payload: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None) -> None:
            text = _clean(text)
            if not text or kind not in MEMORY_KINDS or not _secret_free(text):
                return
            citation = {
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "report_ref": f"run_artifacts:{run.run_id}",
                "artifact_refs": WorkspaceMemoryPipeline._artifact_refs(run),
                "source": "run_memory_extraction",
                "created_at": created_at,
            }
            citation_model = WorkspaceMemoryPipeline._citation_model(citation)
            raw = RawMemoryItem(
                memory_id=f"stage1_{run.run_id}_{len(items) + 1}",
                kind=kind,
                memory_type=WorkspaceMemoryPipeline.memory_type_for_kind(kind),
                text=text,
                status="candidate",
                fingerprint=WorkspaceMemoryPipeline._fingerprint(kind, text),
                citation=citation,
                citations=[citation_model],
                confidence=WorkspaceMemoryPipeline._initial_confidence(kind, evidence or payload or {}),
                expiry=WorkspaceMemoryPipeline._expiry_for_kind(kind, created_at),
                evidence=evidence or {},
                payload=payload or {},
                created_at=created_at,
            )
            items.append(raw.model_dump(mode="json", by_alias=True))

        for kind, text, payload, evidence in WorkspaceMemoryPipeline._prompt_memory_candidates(getattr(run, "prompt", "")):
            add(kind, text, payload, evidence)

        contract = dict(run.acceptance_contract or {})
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        primary_entities = (run.implementation_plan or {}).get("primary_entities") if isinstance(run.implementation_plan, dict) else []
        resource = prompt_hints.get("resource_hint") or (primary_entities[0] if primary_entities else None)
        if resource:
            add(
                "product_fact",
                f"Product fact: workflow centers on `{resource}` with role responsibilities from the prompt contract.",
                {"resource_hint": resource, "roles": contract.get("roles", [])},
                {"source": "acceptance_contract", "resource_hint": resource},
            )
        role_state_contract = (run.implementation_plan or {}).get("role_state_contract") if isinstance(run.implementation_plan, dict) else {}
        if isinstance(role_state_contract, dict) and role_state_contract:
            add(
                "persistence_schema_decision",
                "Persistence schema decision: keep role state contract fields and API payload names aligned across backend, UI, and tests.",
                {"role_state_contract": role_state_contract},
                {"source": "implementation_plan_role_state_contract"},
            )
        vocabulary = WorkspaceMemoryPipeline._ui_vocabulary_from_run(run)
        if vocabulary:
            add(
                "ui_vocabulary",
                f"UI vocabulary: prefer product labels {', '.join(vocabulary[:12])}.",
                {"labels": vocabulary[:40], "roles": list(getattr(run, "target_role_scope", []) or [])},
                {"source": "run_prompt_ui_vocabulary"},
            )
        if run.status == "completed" and run.apply_status == "applied":
            add(
                "working_pattern",
                "Completed run reached applied state; prefer preserving its persisted workflow, generated tests, and browser proof shape during later edits.",
                {"touched_files": list(run.touched_files or [])[:20]},
                {"source": "run_terminal_state", "status": run.status, "apply_status": run.apply_status},
            )
            workflow_payload = WorkspaceMemoryPipeline._successful_workflow_payload(run, artifacts)
            add(
                "reusable_workflow",
                WorkspaceMemoryPipeline._successful_workflow_text(workflow_payload),
                workflow_payload,
                {"source": "run_success_workflow", "status": run.status, "apply_status": run.apply_status},
            )
            add(
                "successful_app_pattern",
                WorkspaceMemoryPipeline._successful_app_pattern_text(workflow_payload),
                WorkspaceMemoryPipeline._successful_app_pattern_payload(run, workflow_payload),
                {"source": "run_success_pattern", "status": run.status, "apply_status": run.apply_status},
            )
        failure_signature = run.failure_signature or run.failure_class
        if failure_signature:
            add(
                "failure_signature",
                f"Run hit `{failure_signature}`: {run.root_cause_summary or run.failure_reason or 'repair evidence is available in run artifacts'}.",
                {"failure_class": run.failure_class, "failure_signature": run.failure_signature},
                {"source": "run_failure", "failure_class": run.failure_class, "failure_signature": run.failure_signature},
            )
            add(
                "avoidance",
                f"Avoid repeating the failure pattern `{failure_signature}`; start future repair from the stored repair packet and failing check evidence.",
                {"failure_class": run.failure_class, "failure_signature": run.failure_signature},
                {"source": "run_failure", "failure_class": run.failure_class, "failure_signature": run.failure_signature},
            )
            shield = WorkspaceMemoryPipeline._failure_shield(
                {
                    "failure_signature": failure_signature,
                    "failure_class": run.failure_class,
                    "details": run.failure_reason,
                    "message": run.failure_reason,
                    "root_cause_summary": run.root_cause_summary,
                    "paths": list(run.fix_targets or run.touched_files or [])[:20],
                },
                run=run,
                source="run_failure",
            )
            add("failure_shield", WorkspaceMemoryPipeline._failure_shield_text(shield), shield, {"source": "run_failure", "failure_signature": failure_signature})
            add("known_failure_recipe", WorkspaceMemoryPipeline._known_failure_recipe_text(shield), shield, {"source": "run_failure_recipe", "failure_signature": failure_signature})
        for item in run.repair_issue_signatures or []:
            if isinstance(item, dict) and item.get("signature"):
                add(
                    "failure_signature",
                    f"Repair signature `{item.get('signature')}` appeared for check `{item.get('check') or 'unknown'}`.",
                    item,
                    {"source": "repair_issue_signature", **item},
                )
                shield = WorkspaceMemoryPipeline._failure_shield(item, run=run, source="repair_issue_signature")
                add("failure_shield", WorkspaceMemoryPipeline._failure_shield_text(shield), shield, {"source": "repair_issue_signature", **item})
                add("known_failure_recipe", WorkspaceMemoryPipeline._known_failure_recipe_text(shield), shield, {"source": "repair_issue_signature_recipe", **item})
        for iteration in run.repair_iterations or []:
            if isinstance(iteration, dict) and (iteration.get("failure_signature") or iteration.get("signature") or iteration.get("failed_check")):
                shield = WorkspaceMemoryPipeline._failure_shield(iteration, run=run, source="repair_iteration")
                add("failure_shield", WorkspaceMemoryPipeline._failure_shield_text(shield), shield, {"source": "repair_iteration"})
                add("known_failure_recipe", WorkspaceMemoryPipeline._known_failure_recipe_text(shield), shield, {"source": "repair_iteration_recipe"})
        for check in artifacts.get("check_results") or []:
            if isinstance(check, dict) and str(check.get("status") or "") in {"failed", "blocked"}:
                add(
                    "failure_signature",
                    f"Check `{check.get('name')}` failed: {check.get('details') or 'see check logs'}.",
                    {"check": check.get("name"), "logs": list(check.get("logs") or [])[-5:]},
                    {"source": "check_result", "check_name": check.get("name"), "status": check.get("status")},
                )
                shield = WorkspaceMemoryPipeline._failure_shield(
                    {
                        "signature": check.get("signature"),
                        "failure_signature": check.get("failure_signature") or f"check_failed:{check.get('name') or 'unknown'}",
                        "failure_class": check.get("name"),
                        "check": check.get("name"),
                        "details": check.get("details"),
                        "message": check.get("details"),
                        "logs": list(check.get("logs") or [])[-5:],
                    },
                    run=run,
                    source="check_result",
                )
                add("failure_shield", WorkspaceMemoryPipeline._failure_shield_text(shield), shield, {"source": "check_result", "check_name": check.get("name")})
                add("known_failure_recipe", WorkspaceMemoryPipeline._known_failure_recipe_text(shield), shield, {"source": "check_result_recipe", "check_name": check.get("name")})
        status = "empty" if not items else "extracted"
        deduped = WorkspaceMemoryPipeline._dedupe(items)
        batch = RunMemoryBatch(
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            status=status,
            items=[RawMemoryItem.model_validate(item) for item in deduped],
            raw_count=len(deduped),
            created_at=created_at,
        )
        return batch.model_dump(mode="json", by_alias=True)

    @staticmethod
    def consolidate(
        workspace_id: str,
        stage1_payloads: list[dict[str, Any]],
        existing: dict[str, Any],
        *,
        workspace_root: Path | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        deduped_count = 0
        for raw_existing in existing.get("items") or []:
            if not isinstance(raw_existing, dict):
                continue
            item = WorkspaceMemoryPipeline._normalize_consolidated_item(raw_existing)
            key = WorkspaceMemoryPipeline._key(item)
            if key in by_key:
                item["status"] = "superseded"
                item["superseded_by"] = by_key[key].get("memory_id")
                deduped_count += 1
            else:
                by_key[key] = item
            items.append(item)
        for payload in stage1_payloads:
            for raw_item in payload.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                candidate = WorkspaceMemoryPipeline._normalize_consolidated_item(raw_item)
                key = WorkspaceMemoryPipeline._key(candidate)
                if not key:
                    continue
                if key in by_key:
                    WorkspaceMemoryPipeline._merge_memory_item(by_key[key], candidate)
                    deduped_count += 1
                    continue
                candidate["memory_id"] = candidate.get("memory_id") or f"mem_{len(items) + 1}"
                candidate["status"] = "active"
                candidate["source"] = "memory_pipeline"
                candidate["consolidated_at"] = candidate.get("consolidated_at") or _now()
                by_key[key] = candidate
                items.append(candidate)
        for item in items:
            WorkspaceMemoryPipeline._refresh_item_status(item, workspace_root=workspace_root)
        current = dict(existing or {})
        active_items = [item for item in items if item.get("status") != "superseded"]
        overflow = max(0, len(active_items) - 200)
        if overflow:
            active_ids = {item.get("memory_id") for item in active_items[overflow:]}
            items = [item for item in items if item.get("status") == "superseded" or item.get("memory_id") in active_ids]
        current["workspace_id"] = workspace_id
        current["schema"] = "grounded.workspace_memory.v2"
        current["items"] = items
        type_buckets = WorkspaceMemoryPipeline.type_buckets(items)
        counts = WorkspaceMemoryPipeline._status_counts(items)
        current["pipeline"] = {
            "schema": "grounded.memory_pipeline.v1",
            "phase1": {"schema": "grounded.memory_stage1.v1", "batch_count": len(stage1_payloads), "raw_count": sum(len(p.get("items") or []) for p in stage1_payloads)},
            "phase2": {"schema": "grounded.workspace_memory.v2", **counts, "deduped_count": deduped_count},
            "category_counts": WorkspaceMemoryPipeline._category_counts(items),
            "type_counts": {key: len(value) for key, value in type_buckets.items()},
            "stage1_count": len(stage1_payloads),
            "stage1_items": sum(len(payload.get("items") or []) for payload in stage1_payloads),
            "active_count": counts["active_count"],
            "stale_count": counts["stale_count"],
            "expired_count": counts["expired_count"],
            "superseded_count": counts["superseded_count"],
            "deduped_count": deduped_count,
            "retrieval_schema": "grounded.memory_retrieval.v1",
            "updated_at": _now(),
        }
        WorkspaceMemoryPipeline._populate_buckets(current)
        current["product_memory_types"] = type_buckets
        for type_name, bucket_items in type_buckets.items():
            current[type_name] = bucket_items
        current["memory_summary"] = WorkspaceMemoryPipeline.summary(workspace_id, current)
        return current

    @staticmethod
    def stale_check(workspace_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            paths = sorted(WorkspaceMemoryPipeline._item_paths(item))[:12]
            checks.extend({"memory_id": item.get("memory_id"), "path": path, "exists": (workspace_root / path).exists()} for path in paths)
        result = {
            "status": "stale" if any(not check["exists"] for check in checks) else "fresh_or_unreferenced",
            "checks": checks,
            "items": checks[:40],
        }
        return MemoryStaleCheck.model_validate(result).model_dump(mode="json", by_alias=True)

    @staticmethod
    def retrieve(
        workspace_id: str,
        memory: dict[str, Any],
        *,
        prompt: str = "",
        paths: list[str] | None = None,
        top_k: int = 10,
        include_inactive: bool = False,
        failure_class: str | None = None,
        detail_mode: str = "relevant",
    ) -> dict[str, Any]:
        prompt_tokens = _tokens(prompt)
        requested_paths = {str(path or "").strip().replace("\\", "/") for path in (paths or []) if str(path or "").strip()}
        top_k = max(1, min(int(top_k or 10), 50))
        hits: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw_item in memory.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item = WorkspaceMemoryPipeline._normalize_consolidated_item(raw_item)
            memory_type = str(item.get("memory_type") or WorkspaceMemoryPipeline.memory_type_for_kind(str(item.get("kind") or "")))
            text = str(item.get("text") or "")
            status = str(item.get("status") or "active")
            expired = bool((item.get("expiry") or {}).get("expired"))
            stale = status == "stale" or (item.get("stale_check") or {}).get("status") == "stale"
            if not _secret_free(text) or not _secret_free(str(item.get("payload") or "")):
                skipped.append(WorkspaceMemoryPipeline._skip(item, "secret_like_material"))
                continue
            if not include_inactive and (status in {"expired", "superseded"} or expired or stale):
                skipped.append(WorkspaceMemoryPipeline._skip(item, status if status in {"expired", "superseded"} else "stale_reference"))
                continue
            score, reasons = WorkspaceMemoryPipeline._retrieval_score(
                item,
                prompt_tokens=prompt_tokens,
                requested_paths=requested_paths,
                failure_class=failure_class,
            )
            if score <= 0:
                skipped.append(WorkspaceMemoryPipeline._skip(item, "low_relevance"))
                continue
            if memory_type:
                reasons.append(f"type:{memory_type}")
            hits.append({"item": item, "score": round(score, 4), "selection_reason": reasons})
        hits.sort(key=lambda hit: (-float(hit["score"]), str(hit["item"].get("memory_id") or "")))
        selected = hits[:top_k]
        result = MemoryRetrievalResult(
            workspace_id=workspace_id,
            prompt_excerpt=_clean(prompt, limit=240),
            top_k=top_k,
            detail_mode=detail_mode,
            status="retrieved" if selected else "empty",
            hits=selected,
            items=[hit["item"] for hit in selected],
            skipped=skipped[:50],
            summary=MemorySummaryReport.model_validate(WorkspaceMemoryPipeline.summary(workspace_id, memory, prompt=prompt, paths=paths or [], top_k=min(top_k, 12))),
            stats={
                "candidate_count": len([item for item in memory.get("items") or [] if isinstance(item, dict)]),
                "selected_count": len(selected),
                "skipped_count": len(skipped),
                "include_inactive": include_inactive,
                "type_counts": {key: len(value) for key, value in WorkspaceMemoryPipeline.type_buckets(memory.get("items") or []).items()},
            },
            source_refs={"workspace_memory": f"workspace_memory:{workspace_id}"},
            created_at=_now(),
        )
        return result.model_dump(mode="json", by_alias=True)

    @staticmethod
    def summary(
        workspace_id: str,
        memory: dict[str, Any],
        *,
        prompt: str = "",
        paths: list[str] | None = None,
        top_k: int = 12,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        stale_items: list[dict[str, Any]] = []
        expired_count = 0
        superseded_count = 0
        for raw_item in memory.get("items") or []:
            if not isinstance(raw_item, dict):
                continue
            item = WorkspaceMemoryPipeline._normalize_consolidated_item(raw_item)
            text = str(item.get("text") or "")
            status = str(item.get("status") or "active")
            stale = status == "stale" or (item.get("stale_check") or {}).get("status") == "stale"
            expired = status == "expired" or bool((item.get("expiry") or {}).get("expired"))
            if status == "superseded":
                superseded_count += 1
                continue
            if expired:
                expired_count += 1
                continue
            if stale:
                stale_items.append(WorkspaceMemoryPipeline._skip(item, "stale_reference"))
                continue
            if not _secret_free(text) or not _secret_free(str(item.get("payload") or "")):
                continue
            items.append(item)

        top_k = max(1, min(int(top_k or 12), 30))
        sections: list[dict[str, Any]] = []
        lines = ["Workspace memory summary (always loaded; retrieve details on demand):"]
        used = 0
        for kind in SUMMARY_KIND_ORDER:
            candidates = [item for item in items if item.get("kind") == kind]
            if not candidates:
                continue
            candidates.sort(key=WorkspaceMemoryPipeline._summary_rank)
            selected = candidates[:3 if kind in {"preference", "failure_shield"} else 2]
            if used >= top_k:
                break
            selected = selected[: max(0, top_k - used)]
            used += len(selected)
            section_items = [WorkspaceMemoryPipeline._summary_item(item) for item in selected]
            if not section_items:
                continue
            title = WorkspaceMemoryPipeline._summary_title(kind)
            sections.append({"kind": kind, "title": title, "items": section_items})
            rendered = "; ".join(str(item.get("summary") or item.get("text") or "") for item in section_items if item.get("summary") or item.get("text"))
            if rendered:
                lines.append(f"- {title}: {rendered[:520]}")

        if len(lines) == 1:
            lines.append("- No active workspace memory yet.")
        if stale_items:
            lines.append(f"- Stale memory hidden: {len(stale_items)} item(s); use memory retrieval with include_inactive for audit details.")

        type_counts = {key: len(value) for key, value in WorkspaceMemoryPipeline.type_buckets(items).items()}
        counts = {
            "active_count": len(items),
            "section_count": len(sections),
            "selected_count": sum(len(section.get("items") or []) for section in sections),
            "stale_count": len(stale_items),
            "expired_count": expired_count,
            "superseded_count": superseded_count,
            **{f"type_count_{key}": value for key, value in type_counts.items()},
        }
        detail_query = {
            "schema": "grounded.memory_retrieval.v1",
            "endpoint": f"/workspaces/{workspace_id}/memory/retrieve",
            "mode": "details_on_demand",
            "prompt_excerpt": _clean(prompt, limit=160),
            "paths": list(paths or [])[:20],
        }
        report = MemorySummaryReport(
            workspace_id=workspace_id,
            status="summarized" if sections else "empty",
            generated_at=_now(),
            text="\n".join(lines),
            sections=[MemorySummarySection.model_validate(section) for section in sections],
            counts=counts,
            stale={"status": "stale_hidden" if stale_items else "fresh", "items": stale_items[:20], "count": len(stale_items)},
            detail_retrieval=detail_query,
            source_refs={"workspace_memory": f"workspace_memory:{workspace_id}"},
        )
        return report.model_dump(mode="json", by_alias=True)

    @staticmethod
    def apply_stale_status(payload: dict[str, Any], stale_check: dict[str, Any]) -> dict[str, Any]:
        checks_by_id: dict[str, list[dict[str, Any]]] = {}
        for check in stale_check.get("checks") or []:
            if isinstance(check, dict) and check.get("memory_id"):
                checks_by_id.setdefault(str(check.get("memory_id")), []).append(check)
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            memory_id = str(item.get("memory_id") or "")
            checks = checks_by_id.get(memory_id, [])
            if not checks:
                continue
            is_stale = any(not bool(check.get("exists", True)) for check in checks)
            item["stale_check"] = MemoryStaleCheck(
                status="stale" if is_stale else "fresh_or_unreferenced",
                checks=checks,
                items=checks[:40],
            ).model_dump(mode="json", by_alias=True)
            if item.get("status") == "superseded" or (item.get("expiry") or {}).get("expired"):
                continue
            item["status"] = "stale" if is_stale else "active"
        return payload

    @staticmethod
    def _prompt_memory_candidates(prompt: object) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
        candidates: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for sentence in WorkspaceMemoryPipeline._prompt_sentences(prompt):
            lowered = sentence.lower()
            if PREFERENCE_NEGATIVE_RE.search(sentence) or PREFERENCE_POSITIVE_RE.search(sentence):
                polarity = "dislike" if PREFERENCE_NEGATIVE_RE.search(sentence) else "like"
                candidates.append(
                    (
                        "preference",
                        f"User preference ({polarity}): {sentence}",
                        {"polarity": polarity, "source_text": sentence},
                        {"source": "prompt_preference", "polarity": polarity},
                    )
                )
            if PRODUCT_DECISION_RE.search(sentence):
                candidates.append(
                    (
                        "product_fact",
                        f"Product fact from prompt: {sentence}",
                        {"source_text": sentence},
                        {"source": "prompt_product_fact"},
                    )
                )
            if any(token in lowered for token in ("label", "button", "copy", "термин", "назов", "текст", "кнопк", "лейбл")):
                candidates.append(
                    (
                        "ui_vocabulary",
                        f"UI vocabulary from prompt: {sentence}",
                        {"source_text": sentence, "labels": WorkspaceMemoryPipeline._ui_terms(sentence)},
                        {"source": "prompt_ui_vocabulary"},
                    )
                )
            if any(token in lowered for token in ("schema", "field", "payload", "database", "sqlite", "persist", "схем", "поле", "баз", "сохраня")):
                candidates.append(
                    (
                        "persistence_schema_decision",
                        f"Persistence schema decision from prompt: {sentence}",
                        {"source_text": sentence},
                        {"source": "prompt_persistence_schema"},
                    )
                )
            if WORKFLOW_RE.search(sentence) or ("созда" in lowered and "исправ" in lowered):
                candidates.append(
                    (
                        "reusable_workflow",
                        f"Reusable workflow expectation from prompt: {sentence}",
                        {"source_text": sentence, "workflow_steps": WorkspaceMemoryPipeline._workflow_steps_from_text(sentence)},
                        {"source": "prompt_reusable_workflow"},
                    )
                )
        return candidates

    @staticmethod
    def _ui_vocabulary_from_run(run: Any) -> list[str]:
        text_parts = [str(getattr(run, "prompt", "") or "")]
        contract = getattr(run, "acceptance_contract", None) if isinstance(getattr(run, "acceptance_contract", None), dict) else {}
        plan = getattr(run, "implementation_plan", None) if isinstance(getattr(run, "implementation_plan", None), dict) else {}
        for flow in contract.get("flows") or []:
            if isinstance(flow, dict):
                text_parts.extend(str(flow.get(key) or "") for key in ("id", "name", "title"))
        for item in plan.get("product_task_ledger") or []:
            if isinstance(item, dict):
                text_parts.extend(str(item.get(key) or "") for key in ("content", "description", "intent"))
        return WorkspaceMemoryPipeline._ui_terms(" ".join(text_parts))

    @staticmethod
    def _ui_terms(text: str) -> list[str]:
        terms: list[str] = []
        for token in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_-]{2,}", str(text or "")):
            lowered = token.lower()
            if lowered in STOP_WORDS or len(lowered) < 4:
                continue
            if lowered not in terms:
                terms.append(lowered)
        return terms[:40]

    @staticmethod
    def _prompt_sentences(prompt: object) -> list[str]:
        text = str(prompt or "").strip()
        sentences: list[str] = []
        for raw in SENTENCE_SPLIT_RE.split(text):
            sentence = _clean(raw, limit=520)
            if len(sentence) < 12:
                continue
            if not _secret_free(sentence):
                continue
            sentences.append(sentence)
        return sentences[:12]

    @staticmethod
    def _workflow_steps_from_text(text: str) -> list[str]:
        lowered = text.lower()
        steps: list[str] = []
        if "созда" in lowered or "generate" in lowered or "generation" in lowered:
            steps.append("Generate the app from the prompt contract.")
        if "работ" in lowered or "fully working" in lowered or "quality" in lowered:
            steps.append("Verify the product is fully working before completion.")
        if "исправ" in lowered or "repair" in lowered or "fix" in lowered:
            steps.append("Use failure evidence to repair without changing unrelated behavior.")
        if "улучш" in lowered or "improve" in lowered:
            steps.append("Keep the implementation easy to extend in later iterations.")
        return steps or ["Preserve this prompt-level workflow expectation during future runs."]

    @staticmethod
    def _successful_workflow_payload(run: Any, artifacts: dict[str, Any]) -> dict[str, Any]:
        checks = [item for item in artifacts.get("check_results") or [] if isinstance(item, dict)]
        passed_checks = [str(item.get("name") or "") for item in checks if str(item.get("status") or "") in {"passed", "success"} and item.get("name")]
        touched_files = list(getattr(run, "touched_files", []) or [])[:30]
        return {
            "run_id": getattr(run, "run_id", None),
            "workflow_steps": [
                "Start from the prompt-derived acceptance contract.",
                "Preserve touched workflow files unless the new prompt changes the contract.",
                "Rerun the successful checks or equivalent proof before applying.",
            ],
            "touched_files": touched_files,
            "passed_checks": passed_checks[:20],
            "verification_refs": {
                "verification": getattr(run, "verification_report_ref", None),
                "trace_bundle": getattr(run, "trace_bundle_ref", None),
            },
        }

    @staticmethod
    def _successful_workflow_text(payload: dict[str, Any]) -> str:
        files = ", ".join(str(path) for path in (payload.get("touched_files") or [])[:4])
        checks = ", ".join(str(name) for name in (payload.get("passed_checks") or [])[:4])
        parts = ["Successful applied run forms a reusable workflow."]
        if files:
            parts.append(f"Preserve changed files such as {files}.")
        if checks:
            parts.append(f"Verify with checks such as {checks}.")
        return " ".join(parts)

    @staticmethod
    def _successful_app_pattern_payload(run: Any, workflow_payload: dict[str, Any]) -> dict[str, Any]:
        contract = getattr(run, "acceptance_contract", None) if isinstance(getattr(run, "acceptance_contract", None), dict) else {}
        implementation_plan = getattr(run, "implementation_plan", None) if isinstance(getattr(run, "implementation_plan", None), dict) else {}
        return {
            "run_id": getattr(run, "run_id", None),
            "intent": getattr(run, "intent", None),
            "target_role_scope": list(getattr(run, "target_role_scope", []) or [])[:8],
            "workflow_kind": contract.get("workflow_kind"),
            "primary_entities": list(implementation_plan.get("primary_entities") or [])[:8],
            "touched_files": list(workflow_payload.get("touched_files") or [])[:30],
            "passed_checks": list(workflow_payload.get("passed_checks") or [])[:20],
            "reuse_instruction": "Reuse this app shape when a future prompt asks for a similar product flow, but keep prompt-specific labels and data fields fresh.",
        }

    @staticmethod
    def _successful_app_pattern_text(payload: dict[str, Any]) -> str:
        entities = ", ".join(str(item) for item in (payload.get("primary_entities") or [])[:4])
        roles = ", ".join(str(item) for item in (payload.get("target_role_scope") or [])[:4])
        files = ", ".join(str(path) for path in (payload.get("touched_files") or [])[:4])
        parts = ["Successful app pattern: applied run produced a working product shape."]
        if entities:
            parts.append(f"Primary entities: {entities}.")
        if roles:
            parts.append(f"Role surfaces: {roles}.")
        if files:
            parts.append(f"Useful files: {files}.")
        parts.append("Reuse the structure only when the future prompt matches the product flow.")
        return " ".join(parts)

    @staticmethod
    def _failure_shield(issue: dict[str, Any], *, run: Any, source: str) -> dict[str, Any]:
        packet = RepairCatalog.classify_issue(issue)
        signature = str(
            issue.get("failure_signature")
            or issue.get("signature")
            or packet.get("failure_signature")
            or packet.get("signature")
            or getattr(run, "failure_signature", None)
            or getattr(run, "failure_class", None)
            or "uncatalogued_failure"
        )
        symptom = _clean(
            issue.get("details")
            or issue.get("message")
            or issue.get("failure_reason")
            or getattr(run, "failure_reason", None)
            or packet.get("failed_check")
            or signature,
            limit=520,
        )
        cause = _clean(
            issue.get("root_cause_summary")
            or getattr(run, "root_cause_summary", None)
            or packet.get("likely_root_cause")
            or "Cause is not proven yet; collect focused evidence before patching.",
            limit=520,
        )
        fix = _clean(packet.get("instruction") or "Read implicated files, patch the smallest failing slice, then rerun the failing check.", limit=620)
        verification = _clean(
            " ".join(str(value or "") for value in (packet.get("verification_command"), packet.get("verification_check"))).strip()
            or str(packet.get("expected_proof") or "rerun failing check successfully"),
            limit=240,
        )
        shield = MemoryFailureShield(
            failure_signature=signature,
            symptom=symptom,
            cause=cause,
            fix=fix,
            verification=verification,
            check_name=str(issue.get("check") or issue.get("failed_check") or packet.get("failed_check") or "") or None,
            source=source,
            payload={
                "repair_packet": packet.get("repair_packet") if isinstance(packet.get("repair_packet"), dict) else {},
                "target_files": list(packet.get("target_files") or packet.get("likely_files") or [])[:20],
                "retry_policy": packet.get("retry_policy"),
                "required_next_tool": packet.get("required_next_tool"),
                "expected_proof": packet.get("expected_proof") or packet.get("post_fix_proof"),
            },
        )
        return shield.model_dump(mode="json", by_alias=True)

    @staticmethod
    def _failure_shield_text(shield: dict[str, Any]) -> str:
        signature = shield.get("failure_signature") or "uncatalogued_failure"
        symptom = shield.get("symptom") or "unknown symptom"
        cause = shield.get("cause") or "unknown cause"
        fix = shield.get("fix") or "repair from evidence"
        verification = shield.get("verification") or "rerun failing check"
        return f"Failure shield `{signature}`: symptom {symptom}; cause {cause}; fix {fix}; verification {verification}."

    @staticmethod
    def _known_failure_recipe_text(shield: dict[str, Any]) -> str:
        signature = shield.get("failure_signature") or "uncatalogued_failure"
        fix = shield.get("fix") or "repair from evidence"
        verification = shield.get("verification") or "rerun failing check"
        check_name = shield.get("check_name") or "related check"
        return f"Known failure recipe `{signature}`: apply fix `{fix}` and verify with `{verification}` for {check_name}."

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
        return str(item.get("fingerprint") or WorkspaceMemoryPipeline._fingerprint(str(item.get("kind") or ""), str(item.get("text") or "")))

    @staticmethod
    def _fingerprint(kind: str, text: str) -> str:
        material = f"{kind}:{_clean(text, limit=320).lower()}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _initial_confidence(kind: str, evidence: dict[str, Any]) -> MemoryConfidence:
        base = {
            "preference": 0.7,
            "product_fact": 0.68,
            "product_decision": 0.66,
            "ui_vocabulary": 0.64,
            "persistence_schema_decision": 0.74,
            "working_pattern": 0.82,
            "reusable_workflow": 0.78,
            "failure_signature": 0.76,
            "failure_shield": 0.8,
            "known_failure_recipe": 0.82,
            "successful_app_pattern": 0.84,
            "avoidance": 0.72,
        }.get(kind, 0.5)
        signals = [str(evidence.get("source") or "extracted")]
        return WorkspaceMemoryPipeline._confidence_model(base, signals)

    @staticmethod
    def _confidence_model(score: float, signals: list[str] | None = None) -> MemoryConfidence:
        score = max(0.0, min(float(score), 0.99))
        level = "high" if score >= 0.75 else "medium" if score >= 0.45 else "low"
        return MemoryConfidence(score=round(score, 3), level=level, signals=list(dict.fromkeys(signals or []))[:12])

    @staticmethod
    def _confidence_dict(value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            score = value.get("score", 0.5)
            signals = value.get("signals") if isinstance(value.get("signals"), list) else []
            return WorkspaceMemoryPipeline._confidence_model(float(score or 0.5), [str(item) for item in signals]).model_dump(mode="json")
        if isinstance(value, (int, float)):
            return WorkspaceMemoryPipeline._confidence_model(float(value), ["legacy_numeric_confidence"]).model_dump(mode="json")
        return WorkspaceMemoryPipeline._confidence_model(0.5, ["legacy_default"]).model_dump(mode="json")

    @staticmethod
    def _expiry_for_kind(kind: str, created_at: str) -> MemoryExpiry:
        ttl_by_kind = {
            "failure_signature": 30,
            "failure_shield": 60,
            "known_failure_recipe": 90,
            "avoidance": 45,
            "working_pattern": 120,
            "reusable_workflow": 120,
            "successful_app_pattern": 180,
            "ui_vocabulary": 180,
            "persistence_schema_decision": 180,
        }
        ttl = ttl_by_kind.get(kind)
        expires_at = None
        if ttl:
            base = _parse_dt(created_at) or datetime.now(timezone.utc)
            expires_at = (base + timedelta(days=ttl)).isoformat()
        return MemoryExpiry(expires_at=expires_at, ttl_days=ttl, reason="kind_default_ttl" if ttl else None, expired=False)

    @staticmethod
    def _expiry_dict(kind: str, value: object, created_at: str | None) -> dict[str, Any]:
        if isinstance(value, dict):
            expiry = dict(value)
            expiry.setdefault("expired", False)
        else:
            expiry = WorkspaceMemoryPipeline._expiry_for_kind(kind, created_at or _now()).model_dump(mode="json")
        expires_at = _parse_dt(expiry.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            expiry["expired"] = True
        return MemoryExpiry.model_validate(expiry).model_dump(mode="json")

    @staticmethod
    def _citations(item: dict[str, Any]) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        for citation in item.get("citations") or []:
            if isinstance(citation, dict):
                citations.append(WorkspaceMemoryPipeline._citation_model(citation).model_dump(mode="json", by_alias=True))
        legacy = item.get("citation")
        if isinstance(legacy, dict):
            citations.append(WorkspaceMemoryPipeline._citation_model(legacy).model_dump(mode="json", by_alias=True))
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for citation in citations:
            key = repr(sorted(citation.items()))
            if key not in seen:
                seen.add(key)
                unique.append(citation)
        return unique[:12]

    @staticmethod
    def _citation_model(citation: dict[str, Any]) -> MemoryCitation:
        return MemoryCitation(
            run_id=str(citation.get("run_id")) if citation.get("run_id") is not None else None,
            workspace_id=str(citation.get("workspace_id")) if citation.get("workspace_id") is not None else None,
            report_ref=str(citation.get("report_ref")) if citation.get("report_ref") is not None else None,
            artifact_refs=citation.get("artifact_refs") if isinstance(citation.get("artifact_refs"), dict) else {},
            file_path=str(citation.get("file_path") or citation.get("path")) if citation.get("file_path") or citation.get("path") else None,
            check_name=str(citation.get("check_name") or citation.get("check")) if citation.get("check_name") or citation.get("check") else None,
            source=str(citation.get("source")) if citation.get("source") is not None else None,
            created_at=str(citation.get("created_at")) if citation.get("created_at") is not None else None,
        )

    @staticmethod
    def _normalize_consolidated_item(raw_item: dict[str, Any]) -> dict[str, Any]:
        kind = str(raw_item.get("kind") or "note")
        text = _clean(raw_item.get("text") or raw_item.get("summary") or "", limit=1600)
        created_at = str(raw_item.get("created_at") or _now())
        citations = WorkspaceMemoryPipeline._citations(raw_item)
        citation = raw_item.get("citation") if isinstance(raw_item.get("citation"), dict) else (citations[0] if citations else None)
        item = {
            "memory_id": str(raw_item.get("memory_id") or f"mem_{WorkspaceMemoryPipeline._fingerprint(kind, text)[:12]}"),
            "kind": kind,
            "memory_type": str(raw_item.get("memory_type") or WorkspaceMemoryPipeline.memory_type_for_kind(kind)),
            "text": text,
            "status": str(raw_item.get("status") or "active"),
            "fingerprint": str(raw_item.get("fingerprint") or WorkspaceMemoryPipeline._fingerprint(kind, text)),
            "citation": citation,
            "citations": citations,
            "confidence": WorkspaceMemoryPipeline._confidence_dict(raw_item.get("confidence")),
            "expiry": WorkspaceMemoryPipeline._expiry_dict(kind, raw_item.get("expiry"), created_at),
            "stale_check": raw_item.get("stale_check") if isinstance(raw_item.get("stale_check"), dict) else {"status": "fresh_or_unreferenced", "checks": [], "items": []},
            "evidence": raw_item.get("evidence") if isinstance(raw_item.get("evidence"), dict) else {},
            "payload": raw_item.get("payload") if isinstance(raw_item.get("payload"), dict) else {},
            "source": str(raw_item.get("source") or "memory_pipeline"),
            "created_at": created_at,
            "consolidated_at": raw_item.get("consolidated_at"),
            "updated_at": raw_item.get("updated_at"),
            "superseded_by": raw_item.get("superseded_by"),
            "retrieval": raw_item.get("retrieval") if isinstance(raw_item.get("retrieval"), dict) else {},
        }
        return ConsolidatedMemoryItem.model_validate(item).model_dump(mode="json", by_alias=True)

    @staticmethod
    def memory_type_for_kind(kind: str) -> str:
        return KIND_TO_MEMORY_TYPE.get(str(kind or ""), "product_facts")

    @staticmethod
    def type_buckets(items: list[Any]) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, list[dict[str, Any]]] = {type_name: [] for type_name in PRODUCT_MEMORY_TYPES}
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = WorkspaceMemoryPipeline._normalize_consolidated_item(raw_item)
            if item.get("status") != "active":
                continue
            memory_type = str(item.get("memory_type") or WorkspaceMemoryPipeline.memory_type_for_kind(str(item.get("kind") or "")))
            if memory_type not in buckets:
                memory_type = "product_facts"
            buckets[memory_type].append(item)
        return {key: value[:60] for key, value in buckets.items()}

    @staticmethod
    def _merge_memory_item(target: dict[str, Any], incoming: dict[str, Any]) -> None:
        target["citations"] = WorkspaceMemoryPipeline._merge_unique_dicts(target.get("citations") or [], incoming.get("citations") or [])[:12]
        target["citation"] = target.get("citation") or incoming.get("citation")
        target_evidence = target.get("evidence") if isinstance(target.get("evidence"), dict) else {}
        incoming_evidence = incoming.get("evidence") if isinstance(incoming.get("evidence"), dict) else {}
        target["evidence"] = {**incoming_evidence, **target_evidence, "confirmations": len(target.get("citations") or [])}
        target_payload = target.get("payload") if isinstance(target.get("payload"), dict) else {}
        incoming_payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}
        target["payload"] = {**incoming_payload, **target_payload}
        current_score = float((target.get("confidence") or {}).get("score") or 0.5)
        incoming_score = float((incoming.get("confidence") or {}).get("score") or 0.5)
        signals = list((target.get("confidence") or {}).get("signals") or []) + list((incoming.get("confidence") or {}).get("signals") or []) + ["dedup_confirmed"]
        target["confidence"] = WorkspaceMemoryPipeline._confidence_model(min(max(current_score, incoming_score) + 0.08, 0.99), signals).model_dump(mode="json")
        target["updated_at"] = _now()

    @staticmethod
    def _merge_unique_dicts(left: list[Any], right: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*left, *right]:
            if not isinstance(item, dict):
                continue
            key = repr(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _refresh_item_status(item: dict[str, Any], *, workspace_root: Path | None) -> None:
        expiry = WorkspaceMemoryPipeline._expiry_dict(str(item.get("kind") or ""), item.get("expiry"), item.get("created_at"))
        item["expiry"] = expiry
        if item.get("status") == "superseded":
            return
        if expiry.get("expired"):
            item["status"] = "expired"
            return
        if workspace_root is not None:
            stale = WorkspaceMemoryPipeline.stale_check(workspace_root, {"items": [item]})
            item["stale_check"] = stale
            if stale.get("status") == "stale":
                item["status"] = "stale"
                return
        if item.get("status") in {"candidate", "stale", "expired", ""}:
            item["status"] = "active"

    @staticmethod
    def _item_paths(item: dict[str, Any]) -> set[str]:
        refs = set(PATH_PATTERN.findall(str(item.get("text") or "")))
        for citation in item.get("citations") or []:
            if isinstance(citation, dict) and citation.get("file_path"):
                refs.add(str(citation["file_path"]))
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        for key in ("path", "paths", "files", "touched_files", "changed_files"):
            value = payload.get(key)
            if isinstance(value, str):
                refs.add(value)
            elif isinstance(value, list):
                refs.update(str(entry) for entry in value if isinstance(entry, str))
        return {ref.replace("\\", "/").strip() for ref in refs if ref}

    @staticmethod
    def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "active_count": sum(1 for item in items if item.get("status") == "active"),
            "stale_count": sum(1 for item in items if item.get("status") == "stale"),
            "expired_count": sum(1 for item in items if item.get("status") == "expired"),
            "superseded_count": sum(1 for item in items if item.get("status") == "superseded"),
        }

    @staticmethod
    def _category_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        active_items = [item for item in items if item.get("status") == "active"]
        return {
            "user_preferences": sum(1 for item in active_items if item.get("kind") == "preference"),
            "preferences": sum(1 for item in active_items if item.get("memory_type") == "preferences"),
            "product_facts": sum(1 for item in active_items if item.get("memory_type") == "product_facts"),
            "known_failure_recipes": sum(1 for item in active_items if item.get("kind") == "known_failure_recipe"),
            "successful_app_patterns": sum(1 for item in active_items if item.get("kind") == "successful_app_pattern"),
            "successful_patterns": sum(1 for item in active_items if item.get("memory_type") == "successful_patterns"),
            "known_failures": sum(1 for item in active_items if item.get("kind") == "failure_signature"),
            "failure_shields": sum(1 for item in active_items if item.get("kind") == "failure_shield"),
            "rejected_approaches": sum(1 for item in active_items if item.get("memory_type") == "rejected_approaches"),
            "reusable_workflows": sum(1 for item in active_items if item.get("kind") == "reusable_workflow"),
            "ui_vocabulary": sum(1 for item in active_items if item.get("memory_type") == "ui_vocabulary"),
            "persistence_schema_decisions": sum(1 for item in active_items if item.get("memory_type") == "persistence_schema_decisions"),
        }

    @staticmethod
    def _retrieval_score(
        item: dict[str, Any],
        *,
        prompt_tokens: set[str],
        requested_paths: set[str],
        failure_class: str | None,
    ) -> tuple[float, list[str]]:
        item_tokens = _tokens(f"{item.get('kind') or ''} {item.get('text') or ''} {item.get('payload') or ''}")
        overlap = prompt_tokens & item_tokens
        score = 0.0
        reasons: list[str] = []
        if overlap:
            score += min(len(overlap), 8) * 2.0
            reasons.append(f"prompt_overlap:{len(overlap)}")
        item_paths = WorkspaceMemoryPipeline._item_paths(item)
        path_matches = sorted(path for path in requested_paths if path and (path in item_paths or any(path.startswith(ref.rstrip('/') + '/') for ref in item_paths)))
        if path_matches:
            score += 6.0 + min(len(path_matches), 4)
            reasons.append(f"path_match:{path_matches[0]}")
        if failure_class and failure_class.lower() in str(item.get("payload") or item.get("text") or "").lower():
            score += 5.0
            reasons.append("failure_match")
        kind = str(item.get("kind") or "")
        kind_weight = {
            "product_fact": 2.25,
            "product_decision": 2.2,
            "preference": 2.0,
            "persistence_schema_decision": 2.15,
            "ui_vocabulary": 1.75,
            "successful_app_pattern": 2.05,
            "known_failure_recipe": 2.0,
            "failure_shield": 1.95,
            "reusable_workflow": 1.9,
            "working_pattern": 1.8,
            "failure_signature": 1.6,
            "avoidance": 1.4,
        }.get(kind, 0.7)
        score += kind_weight
        confidence = item.get("confidence") if isinstance(item.get("confidence"), dict) else {}
        confidence_score = float(confidence.get("score") or 0.5)
        score += confidence_score * 2.0
        reasons.append(f"confidence:{confidence.get('level') or 'medium'}")
        if item.get("status") == "active":
            score += 0.8
            reasons.append("fresh")
        created_at = _parse_dt(item.get("updated_at") or item.get("consolidated_at") or item.get("created_at"))
        if created_at is not None and datetime.now(timezone.utc) - created_at < timedelta(days=14):
            score += 0.6
            reasons.append("recent")
        if not prompt_tokens and not requested_paths:
            score = max(score, confidence_score + kind_weight)
            reasons.append("default_rank")
        return score, list(dict.fromkeys(reasons))

    @staticmethod
    def _summary_rank(item: dict[str, Any]) -> tuple[float, str]:
        confidence = item.get("confidence") if isinstance(item.get("confidence"), dict) else {}
        score = float(confidence.get("score") or 0.5)
        created = str(item.get("updated_at") or item.get("consolidated_at") or item.get("created_at") or "")
        return (-score, created)

    @staticmethod
    def _summary_item(item: dict[str, Any]) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        text = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or "")
        summary = text[:260]
        if kind == "preference" and payload.get("polarity"):
            summary = f"{payload.get('polarity')}: {text[:220]}"
        elif kind == "failure_shield":
            signature = payload.get("failure_signature") or item.get("failure_signature")
            if not signature:
                match = re.search(r"`([^`]+)`", text)
                signature = match.group(1) if match else item.get("memory_id")
            fix = payload.get("fix") or WorkspaceMemoryPipeline._nested_payload_value(payload, ("fix", "instruction"))
            verification = payload.get("verification") or WorkspaceMemoryPipeline._nested_payload_value(payload, ("verification_check", "verification_command"))
            parts = [str(signature or "failure")]
            if fix:
                parts.append(f"fix: {str(fix)[:160]}")
            if verification:
                parts.append(f"verify: {str(verification)[:120]}")
            summary = " -> ".join(parts)
        elif kind == "reusable_workflow" and payload.get("workflow_steps"):
            summary = " / ".join(str(step) for step in list(payload.get("workflow_steps") or [])[:3])[:280]
        elif kind == "known_failure_recipe":
            signature = payload.get("failure_signature") or item.get("memory_id")
            fix = payload.get("fix") or WorkspaceMemoryPipeline._nested_payload_value(payload, ("fix", "instruction"))
            verification = payload.get("verification") or WorkspaceMemoryPipeline._nested_payload_value(payload, ("verification_check", "verification_command"))
            summary = f"{signature}: fix {str(fix or 'from evidence')[:150]}; verify {str(verification or 'failing check')[:100]}"
        elif kind == "successful_app_pattern":
            entities = ", ".join(str(value) for value in list(payload.get("primary_entities") or [])[:3])
            checks = ", ".join(str(value) for value in list(payload.get("passed_checks") or [])[:3])
            summary = "reuse app shape"
            if entities:
                summary += f" for {entities}"
            if checks:
                summary += f"; proof {checks}"
        elif kind == "ui_vocabulary":
            labels = ", ".join(str(value) for value in list(payload.get("labels") or [])[:8])
            summary = f"labels: {labels}" if labels else summary
        elif kind == "persistence_schema_decision":
            summary = text[:280]
        return {
            "memory_id": item.get("memory_id"),
            "kind": kind,
            "memory_type": item.get("memory_type") or WorkspaceMemoryPipeline.memory_type_for_kind(kind),
            "summary": summary,
            "text": text[:320],
            "confidence": item.get("confidence") or {},
            "payload": {
                key: payload.get(key)
                for key in ("polarity", "failure_signature", "workflow_steps", "touched_files", "passed_checks", "primary_entities", "reuse_instruction", "labels", "role_state_contract")
                if key in payload
            },
        }

    @staticmethod
    def _nested_payload_value(value: Any, keys: tuple[str, ...]) -> str:
        if isinstance(value, dict):
            for key in keys:
                raw = value.get(key)
                if raw:
                    return _clean(raw, limit=180)
            for nested in value.values():
                found = WorkspaceMemoryPipeline._nested_payload_value(nested, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = WorkspaceMemoryPipeline._nested_payload_value(nested, keys)
                if found:
                    return found
        return ""

    @staticmethod
    def _summary_title(kind: str) -> str:
        return {
            "preference": "User likes and dislikes",
            "product_fact": "Product facts",
            "product_decision": "Product decisions",
            "ui_vocabulary": "UI vocabulary",
            "persistence_schema_decision": "Persistence schema decisions",
            "failure_shield": "Failure shields",
            "known_failure_recipe": "Known failure recipes",
            "successful_app_pattern": "Successful app patterns",
            "reusable_workflow": "Reusable workflows",
            "working_pattern": "Working patterns",
            "failure_signature": "Known failures",
            "avoidance": "Avoidance rules",
        }.get(kind, kind.replace("_", " ").title())

    @staticmethod
    def _skip(item: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "memory_id": item.get("memory_id"),
            "kind": item.get("kind"),
            "status": item.get("status"),
            "reason": reason,
            "text_excerpt": str(item.get("text") or "")[:160],
        }

    @staticmethod
    def _populate_buckets(payload: dict[str, Any]) -> None:
        bucket_map = {
            "preference": "user_preferences",
            "product_fact": "product_facts",
            "product_decision": "product_decisions",
            "ui_vocabulary": "ui_vocabulary",
            "persistence_schema_decision": "persistence_schema_decisions",
            "working_pattern": "architecture_summary",
            "reusable_workflow": "reusable_workflows",
            "successful_app_pattern": "successful_app_patterns",
            "failure_signature": "known_failures",
            "failure_shield": "failure_shields",
            "known_failure_recipe": "known_failure_recipes",
            "avoidance": "rejected_approaches",
        }
        for bucket in bucket_map.values():
            payload[bucket] = []
        for bucket in PRODUCT_MEMORY_TYPES:
            payload[bucket] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            bucket = bucket_map.get(str(item.get("kind") or ""))
            if bucket:
                payload.setdefault(bucket, []).append(item)
            memory_type = str(item.get("memory_type") or WorkspaceMemoryPipeline.memory_type_for_kind(str(item.get("kind") or "")))
            if memory_type in PRODUCT_MEMORY_TYPES:
                payload.setdefault(memory_type, []).append(item)
        payload["preferences"] = payload.get("preferences") or payload.get("user_preferences") or []
        payload["successful_patterns"] = payload.get("successful_patterns") or [
            *payload.get("successful_app_patterns", []),
            *payload.get("reusable_workflows", []),
            *payload.get("architecture_summary", []),
        ]
        payload["product_memory_types"] = {bucket: list(payload.get(bucket) or [])[:60] for bucket in PRODUCT_MEMORY_TYPES}
