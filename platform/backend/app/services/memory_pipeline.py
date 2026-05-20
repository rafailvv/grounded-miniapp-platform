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
    MemoryRetrievalResult,
    MemoryStaleCheck,
    RawMemoryItem,
    RunMemoryBatch,
)


MEMORY_KINDS = {"preference", "product_decision", "working_pattern", "failure_signature", "avoidance"}
SECRET_PATTERNS = (
    re.compile(r"(api[_-]?key|token|secret|password)\s*=", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)
PATH_PATTERN = re.compile(r"\b(?:miniapp|app|tests|runtime)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b")
TOKEN_PATTERN = re.compile(r"[A-Za-zА-Яа-я0-9_]{3,}")
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
    return {token.lower() for token in TOKEN_PATTERN.findall(str(value or "")) if token.lower() not in STOP_WORDS}


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

        contract = dict(run.acceptance_contract or {})
        prompt_hints = contract.get("prompt_hints") if isinstance(contract.get("prompt_hints"), dict) else {}
        primary_entities = (run.implementation_plan or {}).get("primary_entities") if isinstance(run.implementation_plan, dict) else []
        resource = prompt_hints.get("resource_hint") or (primary_entities[0] if primary_entities else None)
        if resource:
            add(
                "product_decision",
                f"Product workflow centers on `{resource}` with role responsibilities from the prompt contract.",
                {"resource_hint": resource, "roles": contract.get("roles", [])},
                {"source": "acceptance_contract", "resource_hint": resource},
            )
        if run.status == "completed" and run.apply_status == "applied":
            add(
                "working_pattern",
                "Completed run reached applied state; prefer preserving its persisted workflow, generated tests, and browser proof shape during later edits.",
                {"touched_files": list(run.touched_files or [])[:20]},
                {"source": "run_terminal_state", "status": run.status, "apply_status": run.apply_status},
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
        for item in run.repair_issue_signatures or []:
            if isinstance(item, dict) and item.get("signature"):
                add(
                    "failure_signature",
                    f"Repair signature `{item.get('signature')}` appeared for check `{item.get('check') or 'unknown'}`.",
                    item,
                    {"source": "repair_issue_signature", **item},
                )
        for check in artifacts.get("check_results") or []:
            if isinstance(check, dict) and str(check.get("status") or "") in {"failed", "blocked"}:
                add(
                    "failure_signature",
                    f"Check `{check.get('name')}` failed: {check.get('details') or 'see check logs'}.",
                    {"check": check.get("name"), "logs": list(check.get("logs") or [])[-5:]},
                    {"source": "check_result", "check_name": check.get("name"), "status": check.get("status")},
                )
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
        counts = WorkspaceMemoryPipeline._status_counts(items)
        current["pipeline"] = {
            "schema": "grounded.memory_pipeline.v1",
            "phase1": {"schema": "grounded.memory_stage1.v1", "batch_count": len(stage1_payloads), "raw_count": sum(len(p.get("items") or []) for p in stage1_payloads)},
            "phase2": {"schema": "grounded.workspace_memory.v2", **counts, "deduped_count": deduped_count},
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
            hits.append({"item": item, "score": round(score, 4), "selection_reason": reasons})
        hits.sort(key=lambda hit: (-float(hit["score"]), str(hit["item"].get("memory_id") or "")))
        selected = hits[:top_k]
        result = MemoryRetrievalResult(
            workspace_id=workspace_id,
            prompt_excerpt=_clean(prompt, limit=240),
            top_k=top_k,
            status="retrieved" if selected else "empty",
            hits=selected,
            items=[hit["item"] for hit in selected],
            skipped=skipped[:50],
            stats={
                "candidate_count": len([item for item in memory.get("items") or [] if isinstance(item, dict)]),
                "selected_count": len(selected),
                "skipped_count": len(skipped),
                "include_inactive": include_inactive,
            },
            created_at=_now(),
        )
        return result.model_dump(mode="json", by_alias=True)

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
            "product_decision": 0.66,
            "working_pattern": 0.82,
            "failure_signature": 0.76,
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
        ttl_by_kind = {"failure_signature": 30, "avoidance": 45, "working_pattern": 120}
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
        kind_weight = {"product_decision": 2.2, "preference": 2.0, "working_pattern": 1.8, "failure_signature": 1.6, "avoidance": 1.4}.get(kind, 0.7)
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
