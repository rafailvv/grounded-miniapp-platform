from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterator, TypeAlias, cast
from urllib import parse, request
import json

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class RunEvent:
    type: str
    run_id: str
    payload: JsonObject


@dataclass
class GroundedClient:
    base_url: str = "http://127.0.0.1:8000"

    def list_workspaces(self) -> JsonValue:
        return self._json_value("GET", "/workspaces")

    def create_run(self, workspace_id: str, payload: JsonObject) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/runs", payload)

    def create_workspace(self, payload: JsonObject) -> JsonObject:
        return self._json_object("POST", "/workspaces", payload)

    def list_threads(self, workspace_id: str | None = None, *, include_archived: bool = False, limit: int = 50) -> JsonObject:
        params: dict[str, str] = {"include_archived": str(include_archived).lower(), "limit": str(limit)}
        if workspace_id:
            params["workspace_id"] = workspace_id
        return self._json_object("GET", f"/threads?{parse.urlencode(params)}")

    def start_thread(self, workspace_id: str, *, title: str | None = None, metadata: JsonObject | None = None) -> JsonObject:
        return self._json_object("POST", "/threads", {"workspace_id": workspace_id, "title": title or "", "metadata": metadata or {}})

    def get_thread(self, thread_id: str) -> JsonObject:
        return self._json_object("GET", f"/threads/{parse.quote(thread_id)}")

    def resume_thread(self, thread_id: str) -> JsonObject:
        return self._json_object("POST", f"/threads/{parse.quote(thread_id)}/resume")

    def start_turn(self, thread_id: str, payload: JsonObject) -> JsonObject:
        return self._json_object("POST", f"/threads/{parse.quote(thread_id)}/turns", payload)

    def get_run(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}")

    def timeline(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/timeline")

    def trace_view(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/trace-view")

    def run_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> JsonObject:
        params = parse.urlencode({"after_sequence": str(after_sequence), "limit": str(limit)})
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/events?{params}")

    def gate(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/gate")

    def run_state(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/state")

    def final_report(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/final-report")

    def repair_signatures(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/repair-signatures")

    def resume_run(self, run_id: str) -> JsonObject:
        return self._json_object("POST", f"/runs/{parse.quote(run_id)}/resume")

    def artifacts(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/artifacts")

    def approvals(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/approvals")

    def approve(self, run_id: str, approval_id: str) -> JsonObject:
        return self._json_object("POST", f"/runs/{parse.quote(run_id)}/approvals/{parse.quote(approval_id)}/approve")

    def search_files(self, workspace_id: str, query: str, run_id: str | None = None) -> JsonObject:
        params = {"q": query}
        if run_id:
            params["run_id"] = run_id
        return self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/files/search?{parse.urlencode(params)}")

    def diagnostics(self, workspace_id: str, run_id: str | None = None) -> JsonObject:
        params: dict[str, str] = {}
        if run_id:
            params["run_id"] = run_id
        suffix = f"?{parse.urlencode(params)}" if params else ""
        return self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/diagnostics/lsp{suffix}")

    def patch_preflight(self, workspace_id: str, payload: JsonObject) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/patch/preflight", payload)

    def doctor(self) -> JsonObject:
        return self._json_object("GET", "/doctor")

    def metrics(self) -> JsonObject:
        return self._json_object("GET", "/system/metrics/summary")

    def security_summary(self) -> JsonObject:
        return self._json_object("GET", "/system/security/summary")

    def permission_rules(self) -> JsonObject:
        return self._json_object("GET", "/system/permissions/rules")

    def export(self, workspace_id: str, kind: str) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/export/{kind}")

    def stream_run_events(self, run_id: str, *, poll_interval_seconds: float = 1.0, timeout_seconds: float = 600.0) -> Iterator[RunEvent]:
        started = time.monotonic()
        seen_event_keys: set[str] = set()
        last_sequence = 0
        yield RunEvent("run_started", run_id, self.get_run(run_id))
        while True:
            event_page = self.run_events(run_id, after_sequence=last_sequence)
            for event in cast(list[JsonValue], event_page.get("items", []) if isinstance(event_page, dict) else []):
                if not isinstance(event, dict):
                    continue
                last_sequence = max(last_sequence, int(event.get("sequence") or 0))
                event_type = str(event.get("event_type") or "run.event")
                yield RunEvent(event_type, run_id, cast(JsonObject, event))
            timeline = self.timeline(run_id)
            for item in cast(list[JsonValue], timeline.get("items", []) if isinstance(timeline, dict) else []):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or item.get("created_at") or json.dumps(item, sort_keys=True, ensure_ascii=False))
                if key in seen_event_keys:
                    continue
                seen_event_keys.add(key)
                yield RunEvent(self._event_type_from_timeline(item), run_id, cast(JsonObject, item))
            gate = self.gate(run_id)
            yield RunEvent("gate_changed", run_id, gate)
            yield RunEvent("run_state_changed", run_id, self.run_state(run_id))
            run = self.get_run(run_id)
            if str(run.get("status") or "") in {"completed", "blocked", "failed", "awaiting_approval"}:
                yield RunEvent("run_completed", run_id, {"run": run, "gate": gate})
                return
            if time.monotonic() - started >= timeout_seconds:
                yield RunEvent("run_stream_timeout", run_id, {"run": run, "gate": gate})
                return
            time.sleep(poll_interval_seconds)

    @staticmethod
    def _event_type_from_timeline(item: JsonObject) -> str:
        kind = str(item.get("kind") or "")
        if kind == "tool":
            return "tool_event"
        if kind == "check":
            return "check_completed"
        if kind == "browser":
            return "browser_step"
        if kind == "repair":
            return "repair_packet"
        return kind or "timeline_event"

    def _json_value(self, method: str, path: str, payload: JsonObject | None = None) -> JsonValue:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=30) as response:
            return cast(JsonValue, json.loads(response.read().decode("utf-8")))

    def _json_object(self, method: str, path: str, payload: JsonObject | None = None) -> JsonObject:
        value = self._json_value(method, path, payload)
        if not isinstance(value, dict):
            raise TypeError(f"Expected JSON object from {path}.")
        return cast(JsonObject, value)
