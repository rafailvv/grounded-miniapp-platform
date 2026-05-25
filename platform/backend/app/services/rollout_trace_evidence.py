from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RolloutTraceEvidence:
    """Raw-evidence-first trace view for debugging and repair learning."""

    SCHEMA = "grounded.rollout_trace_evidence.v1"

    @classmethod
    def build(
        cls,
        *,
        run: Any,
        store: Any,
        trace_bundle: dict[str, Any] | None = None,
        trace_state: dict[str, Any] | None = None,
        trace_reducer: dict[str, Any] | None = None,
        protocol: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_bundle = trace_bundle if isinstance(trace_bundle, dict) else {}
        trace_state = trace_state if isinstance(trace_state, dict) else {}
        trace_reducer = trace_reducer if isinstance(trace_reducer, dict) else {}
        protocol = protocol if isinstance(protocol, dict) else {}
        rollout = cls._report(store, getattr(run, "rollout_trace_ref", None))
        tool_trace = cls._report(store, getattr(run, "tool_trace_ref", None))
        process_outputs = cls._report(store, getattr(run, "process_outputs_ref", None))
        exec_trace = cls._report(store, getattr(run, "exec_trace_ref", None))
        worker_drafts = cls._report(store, getattr(run, "worker_drafts_ref", None))
        worker_merge = cls._report(store, getattr(run, "worker_merge_ref", None))
        worker_mailbox = cls._report(store, getattr(run, "worker_mailbox_ref", None))
        raw_events = cls._raw_events(trace_bundle=trace_bundle, rollout=rollout)
        payload_refs = cls._payload_refs(raw_events=raw_events, trace_state=trace_state)
        inference_calls = cls._inference_calls(raw_events=raw_events, trace_state=trace_state)
        tool_calls = cls._tool_calls(tool_trace=tool_trace, trace_state=trace_state, raw_events=raw_events)
        terminal_ops = cls._terminal_ops(run=run, process_outputs=process_outputs, exec_trace=exec_trace, protocol=protocol)
        child_agents = cls._child_agents(run=run, worker_drafts=worker_drafts, worker_merge=worker_merge, worker_mailbox=worker_mailbox)
        reduced_graph = cls._reduced_graph(raw_events=raw_events, trace_state=trace_state, rollout=rollout)
        interpretations = cls._interpretations(run=run, trace_state=trace_state, trace_reducer=trace_reducer)
        return {
            "schema": cls.SCHEMA,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": "ready" if raw_events or tool_calls or inference_calls else "empty",
            "principle": "raw_evidence_first_interpret_later",
            "raw_events": raw_events[-300:],
            "payload_refs": payload_refs[-300:],
            "evidence_streams": {
                "trace_bundle_ref": getattr(run, "trace_bundle_ref", None),
                "rollout_trace_ref": getattr(run, "rollout_trace_ref", None),
                "trace_reducer_ref": getattr(run, "trace_reducer_ref", None),
                "tool_trace_ref": getattr(run, "tool_trace_ref", None),
                "process_outputs_ref": getattr(run, "process_outputs_ref", None),
                "exec_trace_ref": getattr(run, "exec_trace_ref", None),
                "worker_branch_refs": list(getattr(run, "worker_branch_refs", []) or []),
            },
            "reduced_graph": reduced_graph,
            "inference_calls": inference_calls[-120:],
            "tool_calls": tool_calls[-180:],
            "terminal_ops": terminal_ops[-120:],
            "child_agents": child_agents[-120:],
            "interpretations": interpretations,
            "repair_learning_hooks": cls._repair_learning_hooks(interpretations, tool_calls=tool_calls, terminal_ops=terminal_ops),
            "counts": {
                "raw_events": len(raw_events),
                "payload_refs": len(payload_refs),
                "graph_nodes": len(reduced_graph.get("nodes") or []),
                "graph_edges": len(reduced_graph.get("edges") or []),
                "inference_calls": len(inference_calls),
                "tool_calls": len(tool_calls),
                "terminal_ops": len(terminal_ops),
                "child_agents": len(child_agents),
            },
        }

    @staticmethod
    def _report(store: Any, ref: str | None) -> dict[str, Any]:
        if not ref:
            return {}
        payload = store.get("reports", ref)
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _raw_events(cls, *, trace_bundle: dict[str, Any], rollout: dict[str, Any]) -> list[dict[str, Any]]:
        events = cls._bundle_raw_events(trace_bundle)
        if not events:
            events = [
                {
                    "seq": item.get("sequence"),
                    "event_type": item.get("event_type"),
                    "created_at": item.get("created_at"),
                    "summary": cls._summary(item.get("payload")),
                    "payload_inline": True,
                    "payload": cls._compact(item.get("payload") if isinstance(item.get("payload"), dict) else {}),
                    "source": "rollout_trace",
                }
                for item in rollout.get("events") or []
                if isinstance(item, dict)
            ]
        for index, event in enumerate(events, start=1):
            event.setdefault("seq", index)
            event.setdefault("source", "trace_bundle")
        return events

    @staticmethod
    def _bundle_raw_events(trace_bundle: dict[str, Any]) -> list[dict[str, Any]]:
        trace_path = str(trace_bundle.get("trace_path") or "").strip()
        if not trace_path:
            return []
        path = Path(trace_path)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    events.append({**item, "source": "trace_bundle"})
        except (OSError, json.JSONDecodeError):
            return []
        return events

    @staticmethod
    def _payload_refs(*, raw_events: list[dict[str, Any]], trace_state: dict[str, Any]) -> list[dict[str, Any]]:
        refs = [
            {
                "seq": item.get("seq"),
                "event_type": item.get("event_type"),
                "payload_ref": item.get("payload_ref"),
                "payload_sha256": item.get("payload_sha256"),
                "source": item.get("source"),
            }
            for item in raw_events
            if item.get("payload_ref")
        ]
        for item in trace_state.get("payload_refs") or []:
            if isinstance(item, dict):
                refs.append({**item, "source": item.get("source") or "trace_state"})
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, Any]] = []
        for item in refs:
            key = (str(item.get("payload_ref") or ""), str(item.get("seq") or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _inference_calls(*, raw_events: list[dict[str, Any]], trace_state: dict[str, Any]) -> list[dict[str, Any]]:
        calls = []
        for item in raw_events:
            event_type = str(item.get("event_type") or "")
            if event_type in {"prompt_context_pack", "model_prompt_response", "agent_turn_started", "model_response"}:
                calls.append(
                    {
                        "seq": item.get("seq"),
                        "event_type": event_type,
                        "payload_ref": item.get("payload_ref"),
                        "payload_sha256": item.get("payload_sha256"),
                        "summary": item.get("summary"),
                        "created_at": item.get("created_at"),
                    }
                )
        for item in trace_state.get("prompt_contexts") or []:
            if isinstance(item, dict):
                calls.append({**item, "source": "trace_state.prompt_contexts"})
        return calls

    @staticmethod
    def _tool_calls(*, tool_trace: dict[str, Any], trace_state: dict[str, Any], raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        calls = []
        for item in tool_trace.get("items") or []:
            if isinstance(item, dict):
                calls.append({**item, "source": "tool_trace"})
        for item in trace_state.get("tool_calls") or []:
            if isinstance(item, dict):
                calls.append({**item, "source": item.get("source") or "trace_state.tool_calls"})
        for item in raw_events:
            event_type = str(item.get("event_type") or "")
            if "tool" in event_type and not item.get("payload_inline"):
                calls.append(
                    {
                        "seq": item.get("seq"),
                        "event_type": event_type,
                        "payload_ref": item.get("payload_ref"),
                        "payload_sha256": item.get("payload_sha256"),
                        "summary": item.get("summary"),
                        "source": "trace_bundle",
                    }
                )
        return calls

    @staticmethod
    def _terminal_ops(*, run: Any, process_outputs: dict[str, Any], exec_trace: dict[str, Any], protocol: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        for source_name, report in (("process_outputs", process_outputs), ("exec_trace", exec_trace)):
            for item in report.get("items") or report.get("events") or []:
                if isinstance(item, dict):
                    items.append({**item, "source": source_name})
        for event in protocol.get("items") or []:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "run_completed" or str(event.get("status") or "") in {"failed", "blocked"}:
                items.append(
                    {
                        "source": "run_protocol",
                        "sequence": event.get("sequence"),
                        "type": event.get("type"),
                        "status": event.get("status"),
                        "message": event.get("message"),
                        "payload_ref": event.get("payload_ref"),
                    }
                )
        if str(getattr(run, "status", "")) in {"completed", "failed", "blocked", "cancelled"}:
            items.append(
                {
                    "source": "run_record",
                    "status": run.status,
                    "apply_status": run.apply_status,
                    "current_stage": run.current_stage,
                    "failure_class": run.failure_class,
                    "failure_signature": run.failure_signature,
                    "failure_reason": run.failure_reason,
                }
            )
        return items

    @staticmethod
    def _child_agents(*, run: Any, worker_drafts: dict[str, Any], worker_merge: dict[str, Any], worker_mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        items = []
        for source_name, report in (("worker_drafts", worker_drafts), ("worker_merge", worker_merge), ("worker_mailbox", worker_mailbox)):
            for key in ("items", "workers", "drafts", "messages", "branches"):
                for item in report.get(key) or []:
                    if isinstance(item, dict):
                        items.append({**item, "source": source_name})
        for ref in getattr(run, "worker_branch_refs", []) or []:
            items.append({"source": "run_record", "worker_branch_ref": ref})
        return items

    @staticmethod
    def _reduced_graph(*, raw_events: list[dict[str, Any]], trace_state: dict[str, Any], rollout: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        edges = []
        graph = rollout.get("graph") if isinstance(rollout.get("graph"), list) else []
        for item in graph:
            if isinstance(item, dict):
                nodes.append({"id": f"rollout:{item.get('sequence')}", **item, "source": "rollout_trace"})
        for item in raw_events:
            seq = item.get("seq")
            nodes.append({"id": f"raw:{seq}", "seq": seq, "event_type": item.get("event_type"), "summary": item.get("summary"), "source": item.get("source")})
        previous = None
        for node in nodes:
            if previous:
                edges.append({"from": previous, "to": node["id"], "kind": "happened_before"})
            previous = node["id"]
        for collection, kind in (("blockers", "blocks"), ("proof_edges", "proves"), ("diff_edges", "changes")):
            for item in trace_state.get(collection) or []:
                if isinstance(item, dict):
                    node_id = f"{collection}:{item.get('seq') or len(nodes) + 1}"
                    nodes.append({"id": node_id, **item, "source": f"trace_state.{collection}"})
                    if item.get("seq"):
                        edges.append({"from": f"raw:{item.get('seq')}", "to": node_id, "kind": kind})
        return {"nodes": nodes[-300:], "edges": edges[-300:]}

    @staticmethod
    def _interpretations(*, run: Any, trace_state: dict[str, Any], trace_reducer: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "derived",
            "run_failure": {
                "status": run.status,
                "failure_class": run.failure_class,
                "failure_signature": run.failure_signature,
                "failure_reason": run.failure_reason,
            },
            "trace_state_next_action": trace_state.get("next_action") or {},
            "trace_state_blockers": list(trace_state.get("blockers") or [])[-20:],
            "trace_reducer_next_action": trace_reducer.get("next_action") or {},
            "trace_reducer_last_failed_attempt": trace_reducer.get("last_failed_attempt") or {},
            "trace_reducer_stale_diff": trace_reducer.get("stale_diff") or {},
        }

    @staticmethod
    def _repair_learning_hooks(interpretations: dict[str, Any], *, tool_calls: list[dict[str, Any]], terminal_ops: list[dict[str, Any]]) -> dict[str, Any]:
        failed_tools = [item for item in tool_calls if str(item.get("status") or item.get("semantic_status") or "").lower() in {"failed", "error", "blocked"}]
        failed_commands = [item for item in terminal_ops if item.get("exit_code") not in {None, 0} or str(item.get("status") or "").lower() in {"failed", "blocked"}]
        return {
            "candidate_failure_signature": (interpretations.get("run_failure") or {}).get("failure_signature"),
            "failed_tools": failed_tools[-12:],
            "failed_commands": failed_commands[-12:],
            "can_extract_failure_shield": bool((interpretations.get("run_failure") or {}).get("failure_signature") or failed_tools or failed_commands),
        }

    @staticmethod
    def _summary(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("summary", "message", "reason", "status"):
                if payload.get(key):
                    return str(payload.get(key))[:240]
            details = payload.get("details")
            if isinstance(details, dict):
                for key in ("summary", "reason", "status"):
                    if details.get(key):
                        return str(details.get(key))[:240]
        return ""

    @staticmethod
    def _compact(payload: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= 4000:
            return payload
        return {"truncated": True, "chars": len(text), "excerpt": text[:4000]}
