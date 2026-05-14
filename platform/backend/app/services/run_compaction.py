from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from app.models.domain import RunRecord
from app.repositories.state_store import StateStore
from app.services.run_protocol import RunProtocolService


RUN_COMPACTION_SCHEMA = "grounded.run_compaction.v1"
RUN_COMPACTION_BOUNDARY_SCHEMA = "grounded.run_compaction_boundary.v1"
MICROCOMPACT_SCHEMA = "grounded.microcompact.v1"
POST_COMPACT_MESSAGE_SCHEMA = "grounded.post_compact_message.v1"
MICROCOMPACT_THRESHOLD_CHARS = 6000
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _truncate(value: Any, *, limit: int = 1200) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return f"{text[:head]}\n...[omitted {len(text) - limit} chars]...\n{text[-tail:]}"


def _compact_json(value: Any, *, max_chars: int = 2400, max_items: int = 12) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                compact["__truncated_keys__"] = len(value) - max_items
                break
            compact[str(key)] = _compact_json(item, max_chars=max_chars // 2, max_items=max_items)
        return compact
    if isinstance(value, list):
        items = [_compact_json(item, max_chars=max_chars // 2, max_items=max_items) for item in value[:max_items]]
        if len(value) > max_items:
            items.append({"__truncated_items__": len(value) - max_items})
        return items
    text = str(value)
    return _truncate(text, limit=max_chars) if len(text) > max_chars else value


def _message_text(sections: dict[str, Any], refs: dict[str, Any]) -> str:
    payload = {
        "instruction": (
            "Continue from this compact boundary. Use this summary as the active run context; "
            "do not assume omitted tool output unless you explicitly read the referenced artifact."
        ),
        "sections": sections,
        "refs": refs,
    }
    return _truncate(_json_text(payload), limit=9000)


def _contains_secret(value: Any) -> bool:
    text = _json_text(value)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


class RunCompactionService:
    def __init__(self, store: StateStore, run_protocol_service: RunProtocolService | None = None) -> None:
        self.store = store
        self.run_protocol_service = run_protocol_service

    def microcompact_tool_result(
        self,
        *,
        workspace_id: str,
        run_id: str,
        tool_result: dict[str, Any],
        serialized: str | None = None,
    ) -> dict[str, Any]:
        text = serialized if serialized is not None else _json_text(tool_result)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        ref = f"microcompact:{workspace_id}:{run_id}:{digest[:24]}"
        payload = {
            "schema": MICROCOMPACT_SCHEMA,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "digest": digest,
            "tool": tool_result.get("tool") or tool_result.get("name") or tool_result.get("type"),
            "status": tool_result.get("status") or tool_result.get("outcome"),
            "original_chars": len(text),
            "excerpt": _truncate(text, limit=2400),
            "tool_result": tool_result,
            "created_at": _now(),
        }
        self.store.upsert("reports", ref, payload)
        index_ref = f"microcompacts:{run_id}"
        index = self.store.get("reports", index_ref) or {"schema": "grounded.microcompact_index.v1", "run_id": run_id, "items": []}
        items = [item for item in index.get("items") or [] if isinstance(item, dict)]
        if not any(item.get("ref") == ref for item in items):
            items.append({"ref": ref, "digest": digest, "tool": payload["tool"], "original_chars": len(text), "created_at": payload["created_at"]})
        index["items"] = items[-200:]
        index["updated_at"] = _now()
        self.store.upsert("reports", index_ref, index)
        return {
            "tool": payload["tool"],
            "status": payload["status"],
            "microcompact_ref": ref,
            "digest": digest,
            "original_chars": len(text),
            "omitted_chars": max(0, len(text) - len(payload["excerpt"])),
            "output": _json_text(
                {
                    "microcompact_ref": ref,
                    "digest": digest,
                    "tool": payload["tool"],
                    "status": payload["status"],
                    "excerpt": payload["excerpt"],
                    "original_chars": len(text),
                    "instruction": "Full tool output is persisted locally. Use the excerpt and ref unless exact raw output is required.",
                }
            ),
        }

    def compact_run(
        self,
        *,
        run: RunRecord,
        artifacts: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        context_pressure: dict[str, Any] | None = None,
        reason: str = "manual",
        source: str = "manual",
        boundary_id: str | None = None,
    ) -> dict[str, Any]:
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        context_pressure = context_pressure if isinstance(context_pressure, dict) else {}
        boundary_id = boundary_id or f"compact_{uuid4().hex}"
        microcompact_index = self.store.get("reports", f"microcompacts:{run.run_id}") or {}
        sections = self._sections(run, artifacts, checkpoint, context_pressure, microcompact_index)
        refs = {
            "run": run.run_id,
            "resume_checkpoint": run.resume_checkpoint_ref,
            "trace_bundle": run.trace_bundle_ref,
            "context_pressure": run.context_pressure_ref,
            "tool_result_messages": run.tool_result_messages_ref,
            "microcompacts": f"microcompacts:{run.run_id}",
        }
        payload = {
            "schema": RUN_COMPACTION_SCHEMA,
            "status": "completed",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "boundary_id": boundary_id,
            "reason": reason,
            "source": source,
            "sections": sections,
            "refs": refs,
            "created_at": _now(),
        }
        compaction_ref = f"run_compaction:{run.run_id}"
        boundary_ref = f"run_compaction_boundary:{run.run_id}:{boundary_id}"
        payload["compaction_ref"] = compaction_ref
        payload["boundary_ref"] = boundary_ref
        post_message = self._create_post_compact_message(
            run=run,
            boundary_id=boundary_id,
            sections=sections,
            refs={**refs, "compaction_ref": compaction_ref, "boundary_ref": boundary_ref},
        )
        payload["post_compact_message_ref"] = post_message["ref"]
        payload["post_compact_status"] = post_message["status"]
        self.store.upsert("reports", compaction_ref, payload)
        boundary = {
            "schema": RUN_COMPACTION_BOUNDARY_SCHEMA,
            **payload,
        }
        self.store.upsert("reports", boundary_ref, boundary)
        self._append_boundary_index(run.run_id, boundary)
        if self.run_protocol_service is not None:
            self.run_protocol_service.append_event(
                run_id=run.run_id,
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                event_type="compact_boundary",
                status="completed",
                message="Run context compacted.",
                payload={"reason": reason, "source": source, "sections": list(sections.keys())},
                refs={
                    "compaction_ref": compaction_ref,
                    "boundary_ref": boundary_ref,
                    "post_compact_message_ref": post_message["ref"],
                    **refs,
                },
                source_event_type="compact_boundary",
            )
        return payload

    def get_compaction(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_compaction:{run_id}")
        if isinstance(payload, dict):
            return payload
        return {"schema": RUN_COMPACTION_SCHEMA, "status": "missing", "run_id": run_id, "sections": {}, "refs": {}}

    def boundaries(self, run_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"run_compaction_boundaries:{run_id}")
        if isinstance(payload, dict):
            return payload
        return {"schema": "grounded.run_compaction_boundaries.v1", "status": "empty", "run_id": run_id, "items": []}

    def microcompact(self, workspace_id: str, run_id: str, digest: str) -> dict[str, Any]:
        prefix = f"microcompact:{workspace_id}:{run_id}:{digest[:24]}"
        payload = self.store.get("reports", prefix)
        if not isinstance(payload, dict):
            raise KeyError(f"Microcompact not found: {digest}")
        return payload

    def post_compact_message(self, run_id: str, boundary_id: str) -> dict[str, Any]:
        payload = self.store.get("reports", f"post_compact_message:{run_id}:{boundary_id}")
        if not isinstance(payload, dict):
            raise KeyError(f"Post-compact message not found: {boundary_id}")
        return payload

    def mark_post_compact_consumed(
        self,
        *,
        run_id: str,
        boundary_id: str,
        turn_id: str | None,
    ) -> dict[str, Any]:
        ref = f"post_compact_message:{run_id}:{boundary_id}"
        payload = self.store.get("reports", ref)
        if not isinstance(payload, dict):
            raise KeyError(f"Post-compact message not found: {boundary_id}")
        payload = dict(payload)
        payload["status"] = "consumed"
        payload["consumed_by_turn_id"] = turn_id
        payload["consumed_at"] = _now()
        self.store.upsert("reports", ref, payload)
        latest = self.store.get("reports", f"run_compaction:{run_id}")
        if isinstance(latest, dict) and latest.get("boundary_id") == boundary_id:
            latest = dict(latest)
            latest["post_compact_status"] = "consumed"
            latest["consumed_by_turn_id"] = turn_id
            latest["consumed_at"] = payload["consumed_at"]
            self.store.upsert("reports", f"run_compaction:{run_id}", latest)
        return payload

    def _sections(
        self,
        run: RunRecord,
        artifacts: dict[str, Any],
        checkpoint: dict[str, Any],
        context_pressure: dict[str, Any],
        microcompact_index: dict[str, Any],
    ) -> dict[str, Any]:
        checks = artifacts.get("check_results") if isinstance(artifacts.get("check_results"), list) else []
        failing_checks = [item for item in checks if isinstance(item, dict) and str(item.get("status") or "") in {"failed", "blocked"}]
        memory_stage1 = self.store.get("reports", f"memory_stage1:{run.workspace_id}:{run.run_id}") or {}
        memory_items = [item for item in memory_stage1.get("items") or [] if isinstance(item, dict) and not _contains_secret(item)]
        next_repair = (
            checkpoint.get("next_forced_action")
            or (checkpoint.get("repair_packets") or [{}])[0]
            if isinstance(checkpoint.get("repair_packets"), list) and checkpoint.get("repair_packets")
            else {}
        )
        return {
            "product_contract": _compact_json(
                {
                    "acceptance_contract": run.acceptance_contract,
                    "flow_coverage": run.flow_coverage,
                    "miniapp_contract_ref": run.miniapp_contract_ref,
                },
                max_chars=3600,
            ),
            "files_changed": {
                "touched_files": list(run.touched_files or [])[:80],
                "file_change_history_ref": run.file_change_history_ref,
                "latest_diff_summary": _truncate(checkpoint.get("latest_diff_summary"), limit=2400),
            },
            "failing_checks": _compact_json(failing_checks or run.checks_summary.model_dump(mode="json"), max_chars=3600),
            "current_plan": _compact_json(
                {
                    "implementation_plan": run.implementation_plan,
                    "todo_plan": checkpoint.get("todo_plan") or [],
                    "scratchpad": checkpoint.get("scratchpad") or {},
                },
                max_chars=3600,
            ),
            "memory_candidates": _compact_json(memory_items[:20], max_chars=2400),
            "next_repair_action": _compact_json(next_repair, max_chars=2400),
            "budget_status": _compact_json(
                {
                    "completion_budget": run.completion_budget,
                    "budget_status": run.budget_status,
                    "token_usage": run.token_usage,
                    "pending_tool_result_count": checkpoint.get("pending_tool_result_count"),
                    "previous_response_id": checkpoint.get("previous_response_id"),
                },
                max_chars=2400,
            ),
            "check_status": _compact_json(
                {
                    "checks_summary": run.checks_summary.model_dump(mode="json"),
                    "latest_check_ref": checkpoint.get("latest_check_ref") or f"latest_check_execution:{run.run_id}",
                    "diagnostics_delta_ref": checkpoint.get("diagnostics_delta_ref"),
                },
                max_chars=2400,
            ),
            "context_pressure": _compact_json(context_pressure, max_chars=1800),
            "microcompacts": _compact_json(microcompact_index.get("items") or [], max_chars=1800),
        }

    def _append_boundary_index(self, run_id: str, boundary: dict[str, Any]) -> None:
        key = f"run_compaction_boundaries:{run_id}"
        payload = self.store.get("reports", key) or {"schema": "grounded.run_compaction_boundaries.v1", "run_id": run_id, "items": []}
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        items.append(
            {
                "boundary_id": boundary.get("boundary_id"),
                "boundary_ref": boundary.get("boundary_ref"),
                "compaction_ref": boundary.get("compaction_ref"),
                "post_compact_message_ref": boundary.get("post_compact_message_ref"),
                "post_compact_status": boundary.get("post_compact_status"),
                "reason": boundary.get("reason"),
                "source": boundary.get("source"),
                "created_at": boundary.get("created_at"),
            }
        )
        payload["status"] = "ok"
        payload["items"] = items[-200:]
        payload["updated_at"] = _now()
        self.store.upsert("reports", key, payload)

    def _create_post_compact_message(
        self,
        *,
        run: RunRecord,
        boundary_id: str,
        sections: dict[str, Any],
        refs: dict[str, Any],
    ) -> dict[str, Any]:
        selected = {
            key: sections.get(key)
            for key in (
                "product_contract",
                "files_changed",
                "failing_checks",
                "current_plan",
                "memory_candidates",
                "next_repair_action",
                "budget_status",
                "check_status",
            )
        }
        ref = f"post_compact_message:{run.run_id}:{boundary_id}"
        payload = {
            "schema": POST_COMPACT_MESSAGE_SCHEMA,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "boundary_id": boundary_id,
            "sections": selected,
            "refs": refs,
            "status": "pending",
            "message": _message_text(selected, refs),
            "created_at": _now(),
            "consumed_at": None,
            "consumed_by_turn_id": None,
            "ref": ref,
        }
        self.store.upsert("reports", ref, payload)
        return payload
