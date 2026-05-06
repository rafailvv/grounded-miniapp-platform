from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json(value: Any, *, max_chars: int = 20000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=False)
    if len(text) <= max_chars:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return str(value)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "truncated": True,
        "sha256": digest,
        "chars": len(text),
        "excerpt": text[:max_chars],
    }


@dataclass(frozen=True)
class TraceBundleIndex:
    schema: str
    trace_id: str
    workspace_id: str
    run_id: str
    bundle_dir: str
    manifest_path: str
    trace_path: str
    state_path: str
    payload_dir: str
    event_count: int
    payload_count: int
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "trace_id": self.trace_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "bundle_dir": self.bundle_dir,
            "manifest_path": self.manifest_path,
            "trace_path": self.trace_path,
            "state_path": self.state_path,
            "payload_dir": self.payload_dir,
            "event_count": self.event_count,
            "payload_count": self.payload_count,
            "status": self.status,
        }


class TraceBundleWriter:
    """Persist-first raw diagnostic bundle for one run."""

    SCHEMA = "grounded.trace_bundle.v1"

    def __init__(self, *, root: Path, workspace_id: str, run_id: str) -> None:
        self.root = root
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.trace_id = f"trace_{run_id}_{uuid4().hex[:8]}"
        self.bundle_dir = root / "trace-bundles" / workspace_id / run_id
        self.payload_dir = self.bundle_dir / "payloads"
        self.manifest_path = self.bundle_dir / "manifest.json"
        self.trace_path = self.bundle_dir / "trace.jsonl"
        self.state_path = self.bundle_dir / "state.json"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            self.manifest_path.write_text(
                json.dumps(
                    {
                        "schema": self.SCHEMA,
                        "trace_id": self.trace_id,
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "created_at": _now(),
                        "privacy": "local_diagnostic_bundle",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        self._event_count = self._count_existing_events()

    @classmethod
    def from_existing(cls, *, root: Path, workspace_id: str, run_id: str) -> "TraceBundleWriter":
        return cls(root=root, workspace_id=workspace_id, run_id=run_id)

    def record(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self._event_count += 1
        safe_payload = _safe_json(payload or {})
        payload_name = f"{self._event_count:06d}_{re_slug(event_type)}.json"
        payload_path = self.payload_dir / payload_name
        payload_path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        event = {
            "seq": self._event_count,
            "event_type": str(event_type or "event"),
            "payload_ref": f"payloads/{payload_name}",
            "payload_sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest(),
            "summary": self._summary(event_type, safe_payload),
            "created_at": _now(),
        }
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        return event

    def reduce(self) -> dict[str, Any]:
        state = TraceBundleReducer.reduce_bundle(self.bundle_dir)
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return state

    def index(self, *, status: str = "recording") -> dict[str, Any]:
        return TraceBundleIndex(
            schema=self.SCHEMA,
            trace_id=self.trace_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            bundle_dir=str(self.bundle_dir),
            manifest_path=str(self.manifest_path),
            trace_path=str(self.trace_path),
            state_path=str(self.state_path),
            payload_dir=str(self.payload_dir),
            event_count=self._event_count,
            payload_count=len(list(self.payload_dir.glob("*.json"))) if self.payload_dir.exists() else 0,
            status=status,
        ).as_dict()

    def _count_existing_events(self) -> int:
        if not self.trace_path.exists():
            return 0
        try:
            return sum(1 for line in self.trace_path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            return 0

    @staticmethod
    def _summary(event_type: str, payload: Any) -> str:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("summary")
            if not message and isinstance(payload.get("details"), dict):
                message = payload["details"].get("summary") or payload["details"].get("reason")
            if message:
                return str(message)[:240]
        return str(event_type or "event").replace("_", " ")[:240]


class TraceBundleReducer:
    @staticmethod
    def reduce_bundle(bundle_dir: Path) -> dict[str, Any]:
        manifest = TraceBundleReducer._read_json(bundle_dir / "manifest.json")
        raw_events = TraceBundleReducer._read_events(bundle_dir)
        events: list[dict[str, Any]] = []
        changed_files: list[str] = []
        blockers: list[dict[str, Any]] = []
        proof_edges: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        payload_refs: list[dict[str, Any]] = []
        prompt_contexts: list[dict[str, Any]] = []
        skill_edges: list[dict[str, Any]] = []
        memory_edges: list[dict[str, Any]] = []
        diff_edges: list[dict[str, Any]] = []
        acceptance_gate: list[dict[str, Any]] = []
        for raw in raw_events:
            payload = TraceBundleReducer._read_json(bundle_dir / str(raw.get("payload_ref") or ""))
            event = {**raw, "payload": payload}
            events.append(event)
            payload_refs.append(
                {
                    "seq": raw.get("seq"),
                    "event_type": raw.get("event_type"),
                    "payload_ref": raw.get("payload_ref"),
                    "payload_sha256": raw.get("payload_sha256"),
                }
            )
            event_type = str(raw.get("event_type") or "")
            details = payload.get("details") if isinstance(payload, dict) and isinstance(payload.get("details"), dict) else {}
            TraceBundleReducer._collect_files(changed_files, payload)
            if event_type in {"agent_turn_started", "iteration_ready"}:
                turns.append(TraceBundleReducer._compact_event(event))
            if event_type in {"prompt_context_pack", "model_prompt_response"}:
                prompt_contexts.append(TraceBundleReducer._compact_event(event))
                if event_type == "prompt_context_pack":
                    skills = payload.get("skills") if isinstance(payload, dict) and isinstance(payload.get("skills"), dict) else {}
                    memory = payload.get("memory") if isinstance(payload, dict) and isinstance(payload.get("memory"), dict) else {}
                    skill_edges.extend(
                        {
                            "event_seq": raw.get("seq"),
                            "skill_id": item.get("id"),
                            "reason": item.get("activation_reason"),
                            "score": item.get("activation_score"),
                        }
                        for item in (skills.get("selected") or [])
                        if isinstance(item, dict)
                    )
                    memory_edges.extend(
                        {
                            "event_seq": raw.get("seq"),
                            "source": item.get("source"),
                            "kind": item.get("kind"),
                            "reason": item.get("reason"),
                            "text_excerpt": item.get("text_excerpt"),
                        }
                        for item in [*(memory.get("injected") or []), *(memory.get("skipped") or [])]
                        if isinstance(item, dict)
                    )
            if "tool" in details or event_type in {"tool_use_summary", "tool_progress"}:
                tool_calls.append(TraceBundleReducer._compact_event(event))
            if event_type == "tool_failed_reason":
                tool_calls.append(TraceBundleReducer._compact_event(event))
            if details.get("artifact_ref"):
                artifacts.append({"event_seq": raw.get("seq"), "artifact_ref": details.get("artifact_ref"), "event_type": event_type})
            if event_type in {"turn_diff_before", "turn_diff_after"}:
                diff_edges.append(TraceBundleReducer._compact_event(event))
            if event_type == "final_acceptance_gate_decision":
                acceptance_gate.append(TraceBundleReducer._compact_event(event))
            status = str(details.get("status") or payload.get("status") or "").lower() if isinstance(payload, dict) else ""
            if status in {"failed", "blocked", "conflict", "forbidden", "error"} or event_type in {"job_failed", "worker_failed", "tool_failed_reason"}:
                blockers.append(TraceBundleReducer._compact_event(event))
            if event_type in {"preview_validation_started", "verification_worker", "checks_completed"}:
                proof_edges.append(TraceBundleReducer._compact_event(event))
        next_action = {"action": "none", "reason": "No blocking trace event."}
        if blockers:
            next_action = {
                "action": "repair",
                "reason": "Trace bundle contains blocking events.",
                "event_seq": blockers[0].get("seq"),
            }
        return {
            "schema": "grounded.trace_bundle_state.v1",
            "trace_id": manifest.get("trace_id"),
            "workspace_id": manifest.get("workspace_id"),
            "run_id": manifest.get("run_id"),
            "event_count": len(events),
            "turns": turns[-80:],
            "tool_calls": tool_calls[-160:],
            "prompt_contexts": prompt_contexts[-120:],
            "skill_edges": skill_edges[-120:],
            "memory_edges": memory_edges[-160:],
            "diff_edges": diff_edges[-120:],
            "acceptance_gate": acceptance_gate[-40:],
            "changed_files": changed_files[:200],
            "blockers": blockers[-80:],
            "proof_edges": proof_edges[-80:],
            "artifacts": artifacts[-160:],
            "payload_refs": payload_refs[-300:],
            "next_action": next_action,
            "reduced_at": _now(),
        }

    @staticmethod
    def _read_events(bundle_dir: Path) -> list[dict[str, Any]]:
        path = bundle_dir / "trace.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {"value": payload}
        except (OSError, json.JSONDecodeError):
            return {}
        return {}

    @staticmethod
    def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        return {
            "seq": event.get("seq"),
            "event_type": event.get("event_type"),
            "status": details.get("status") or payload.get("status"),
            "summary": event.get("summary") or payload.get("message") or payload.get("summary") or details.get("summary") or payload.get("repair_focus"),
            "tool": payload.get("tool") or details.get("tool"),
            "tool_use_id": payload.get("tool_use_id") or details.get("tool_use_id"),
            "artifact_ref": payload.get("artifact_ref") or details.get("artifact_ref"),
            "turn": payload.get("turn"),
            "attempt": payload.get("attempt"),
            "tool_round": payload.get("tool_round"),
            "paths": payload.get("paths"),
            "created_at": event.get("created_at"),
        }

    @staticmethod
    def _collect_files(result: list[str], payload: Any) -> None:
        if isinstance(payload, dict):
            for key in ("files", "changed_files", "paths", "target_files"):
                value = payload.get(key)
                if isinstance(value, list):
                    for item in value:
                        path = str(item or "").strip().replace("\\", "/")
                        if path and path not in result:
                            result.append(path)
            for value in payload.values():
                TraceBundleReducer._collect_files(result, value)
        elif isinstance(payload, list):
            for item in payload:
                TraceBundleReducer._collect_files(result, item)


def re_slug(value: object) -> str:
    text = "".join(ch if ch.isalnum() else "-" for ch in str(value or "").lower()).strip("-")
    return text[:80] or "event"
