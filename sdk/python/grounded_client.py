from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Iterator, Mapping, NotRequired, TypeAlias, TypedDict, cast
from urllib import parse, request

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

TERMINAL_RUN_STATUSES = {"completed", "blocked", "failed", "awaiting_approval"}


class RequestOptions(TypedDict, total=False):
    idempotency_key: str
    headers: dict[str, str]


class RunRecordJson(TypedDict, total=False):
    run_id: str
    workspace_id: str
    status: str
    prompt: str
    created_at: str
    updated_at: str


class RunEventV2Json(TypedDict, total=False):
    event_id: str
    workspace_id: str
    run_id: str
    sequence: int
    event_type: str
    actor: str
    payload_ref: str
    payload_sha256: str
    summary: str
    source_ref: str
    created_at: str


class EventJournalPageJson(TypedDict, total=False):
    scope: str
    run_id: str
    thread_id: str
    next_sequence: int
    items: list[RunEventV2Json]


class RunJournalStateJson(TypedDict, total=False):
    schema: str
    run_id: str
    status_timeline: list[JsonObject]
    tool_events: list[JsonObject]
    checks: list[JsonObject]
    repair: JsonObject
    protocol_refs: list[JsonObject]


class ArtifactIndexJson(TypedDict, total=False):
    schema: str
    run_id: str
    items: list[JsonObject]


class CheckReportJson(TypedDict, total=False):
    items: list[JsonObject]
    status: str
    blocking: bool


class PreviewUrlJson(TypedDict, total=False):
    url: str | None
    role_urls: dict[str, str]
    runtime_mode: str
    status: str
    stage: str
    progress_percent: int | float
    draft_run_id: str | None
    latency_breakdown: JsonObject
    last_error: str | None


class WebhookSubscriptionJson(TypedDict, total=False):
    schema: str
    webhook_id: str
    url: str
    events: list[str]
    workspace_id: str | None
    enabled: bool
    description: str | None
    metadata: JsonObject
    secret_configured: bool
    last_delivery: JsonObject | None
    created_at: str
    updated_at: str


class WebhookCreatePayload(TypedDict, total=False):
    url: str
    events: list[str]
    workspace_id: str | None
    enabled: bool
    description: str | None
    metadata: JsonObject
    secret: NotRequired[str | None]


@dataclass(frozen=True)
class RunEvent:
    type: str
    run_id: str
    payload: JsonObject
    sequence: int | None = None
    source: str = "legacy"


@dataclass
class GroundedClient:
    base_url: str = "http://127.0.0.1:8000"
    default_headers: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0

    def list_workspaces(self) -> JsonValue:
        return self._json_value("GET", "/workspaces")

    def create_workspace(self, payload: JsonObject, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", "/workspaces", payload, idempotency_key=idempotency_key)

    def create_run(self, workspace_id: str, payload: JsonObject, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/runs", payload, idempotency_key=idempotency_key))

    def list_runs(self, workspace_id: str) -> list[RunRecordJson]:
        value = self._json_value("GET", f"/workspaces/{parse.quote(workspace_id)}/runs")
        if not isinstance(value, list):
            raise TypeError("Expected JSON list from list_runs.")
        return cast(list[RunRecordJson], value)

    def list_threads(self, workspace_id: str | None = None, *, include_archived: bool = False, limit: int = 50) -> JsonObject:
        params: dict[str, str] = {"include_archived": str(include_archived).lower(), "limit": str(limit)}
        if workspace_id:
            params["workspace_id"] = workspace_id
        return self._json_object("GET", f"/threads?{parse.urlencode(params)}")

    def start_thread(
        self,
        workspace_id: str,
        *,
        title: str | None = None,
        metadata: JsonObject | None = None,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self._json_object(
            "POST",
            "/threads",
            {"workspace_id": workspace_id, "title": title or "", "metadata": metadata or {}},
            idempotency_key=idempotency_key,
        )

    def get_thread(self, thread_id: str) -> JsonObject:
        return self._json_object("GET", f"/threads/{parse.quote(thread_id)}")

    def get_thread_snapshot(self, thread_id: str) -> JsonObject:
        return self._json_object("GET", f"/threads/{parse.quote(thread_id)}/snapshot")

    def resume_thread(self, thread_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/threads/{parse.quote(thread_id)}/resume", idempotency_key=idempotency_key)

    def start_turn(self, thread_id: str, payload: JsonObject, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/threads/{parse.quote(thread_id)}/turns", payload, idempotency_key=idempotency_key)

    def thread_events_v2(self, thread_id: str, *, after_sequence: int = 0, limit: int = 500) -> EventJournalPageJson:
        params = parse.urlencode({"after_sequence": str(after_sequence), "limit": str(limit)})
        return cast(EventJournalPageJson, self._json_object("GET", f"/threads/{parse.quote(thread_id)}/events-v2?{params}"))

    def thread_journal_state(self, thread_id: str) -> JsonObject:
        return self._json_object("GET", f"/threads/{parse.quote(thread_id)}/journal/state")

    def get_run(self, run_id: str) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}"))

    def timeline(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/timeline")

    def trace_view(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/trace-view")

    def trace_bundle(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/trace-bundle")

    def trace_bundle_state(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/trace-bundle/state")

    def run_events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> JsonObject:
        params = parse.urlencode({"after_sequence": str(after_sequence), "limit": str(limit)})
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/events?{params}")

    def run_events_v2(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> EventJournalPageJson:
        params = parse.urlencode({"after_sequence": str(after_sequence), "limit": str(limit)})
        return cast(EventJournalPageJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}/events-v2?{params}"))

    def run_journal_state(self, run_id: str) -> RunJournalStateJson:
        return cast(RunJournalStateJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}/journal/state"))

    def event_payload(self, payload_ref: str) -> JsonObject:
        return self._json_object("GET", f"/event-payloads/{parse.quote(payload_ref, safe='')}")

    def gate(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/gate")

    def run_state(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/state")

    def final_report(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/final-report")

    def repair_signatures(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/repair-signatures")

    def resume_run(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/resume", idempotency_key=idempotency_key))

    def resume_from_bookmark(self, run_id: str, bookmark_id: str, *, prompt: str | None = None, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object(
            "POST",
            f"/runs/{parse.quote(run_id)}/resume-from-bookmark",
            {"bookmark_id": bookmark_id, "prompt": prompt},
            idempotency_key=idempotency_key,
        )

    def fork_from_bookmark(self, run_id: str, bookmark_id: str, *, prompt: str | None = None, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object(
            "POST",
            f"/runs/{parse.quote(run_id)}/fork-from-bookmark",
            {"bookmark_id": bookmark_id, "prompt": prompt},
            idempotency_key=idempotency_key,
        )

    def apply_run(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/apply", idempotency_key=idempotency_key))

    def discard_run(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/discard", idempotency_key=idempotency_key))

    def stop_run(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/stop", idempotency_key=idempotency_key))

    def rollback_run(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/rollback", idempotency_key=idempotency_key))

    def artifacts(self, run_id: str) -> ArtifactIndexJson:
        return cast(ArtifactIndexJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}/artifacts"))

    def artifact(self, run_id: str, artifact_ref: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/artifacts/{parse.quote(artifact_ref, safe='')}")

    def output_artifacts(self, run_id: str) -> ArtifactIndexJson:
        return cast(ArtifactIndexJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}/output-artifacts"))

    def output_artifact(self, run_id: str, artifact_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/output-artifacts/{parse.quote(artifact_id)}")

    def microcompact(self, run_id: str, digest: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/microcompact/{parse.quote(digest)}")

    def approvals(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/approvals")

    def approve(self, run_id: str, approval_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object(
            "POST",
            f"/runs/{parse.quote(run_id)}/approvals/{parse.quote(approval_id)}/approve",
            idempotency_key=idempotency_key,
        )

    def reject_approval(self, run_id: str, approval_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object(
            "POST",
            f"/runs/{parse.quote(run_id)}/approvals/{parse.quote(approval_id)}/reject",
            idempotency_key=idempotency_key,
        )

    def checks(self, run_id: str) -> CheckReportJson:
        return cast(CheckReportJson, self._json_object("GET", f"/runs/{parse.quote(run_id)}/checks"))

    def test_matrix(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/test-matrix")

    def acceptance_scenarios(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/acceptance-scenarios")

    def browser_proof(self, run_id: str, *, start: bool = False, idempotency_key: str | None = None) -> JsonObject:
        method = "POST" if start else "GET"
        return self._json_object(method, f"/runs/{parse.quote(run_id)}/browser-proof", idempotency_key=idempotency_key)

    def visual_qa(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/visual-qa")

    def validation_current(self, workspace_id: str) -> JsonObject:
        return self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/validation/current")

    def validation_run(self, workspace_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/validation/run", idempotency_key=idempotency_key)

    def preview_url(self, workspace_id: str) -> PreviewUrlJson:
        return cast(PreviewUrlJson, self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/preview/url"))

    def ensure_preview(self, workspace_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/preview/ensure", idempotency_key=idempotency_key)

    def start_preview(self, workspace_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/preview/start", idempotency_key=idempotency_key)

    def rebuild_preview(self, workspace_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/preview/rebuild", idempotency_key=idempotency_key)

    def reset_preview(self, workspace_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/preview/reset", idempotency_key=idempotency_key)

    def preview_logs(self, workspace_id: str) -> JsonObject:
        return self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/preview/logs")

    def workspace_logs(self, workspace_id: str) -> JsonObject:
        return self._json_object("GET", f"/workspaces/{parse.quote(workspace_id)}/logs")

    def review(self, run_id: str, *, start: bool = False, idempotency_key: str | None = None) -> JsonObject:
        method = "POST" if start else "GET"
        return self._json_object(method, f"/runs/{parse.quote(run_id)}/review", idempotency_key=idempotency_key)

    def review_fix(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/review/fix", idempotency_key=idempotency_key))

    def repair_cases(self, run_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/repair-cases")

    def repair_case(self, run_id: str, case_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/repair-cases/{parse.quote(case_id)}")

    def repair_attempts(self, run_id: str, case_id: str) -> JsonObject:
        return self._json_object("GET", f"/runs/{parse.quote(run_id)}/repair-cases/{parse.quote(case_id)}/attempts")

    def retry_repair_case(self, run_id: str, case_id: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object(
            "POST",
            f"/runs/{parse.quote(run_id)}/repair-cases/{parse.quote(case_id)}/retry",
            idempotency_key=idempotency_key,
        )

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

    def patch_preflight(self, workspace_id: str, payload: JsonObject, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/patch/preflight", payload, idempotency_key=idempotency_key)

    def stage_files(self, run_id: str, files: list[str], *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/runs/{parse.quote(run_id)}/stage/files", {"files": files}, idempotency_key=idempotency_key)

    def discard_files(self, run_id: str, files: list[str], *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/runs/{parse.quote(run_id)}/discard/files", {"files": files}, idempotency_key=idempotency_key)

    def apply_staged(self, run_id: str, *, idempotency_key: str | None = None) -> RunRecordJson:
        return cast(RunRecordJson, self._json_object("POST", f"/runs/{parse.quote(run_id)}/apply/staged", idempotency_key=idempotency_key))

    def list_webhooks(self, *, workspace_id: str | None = None) -> JsonObject:
        suffix = f"?{parse.urlencode({'workspace_id': workspace_id})}" if workspace_id else ""
        return self._json_object("GET", f"/webhooks{suffix}")

    def create_webhook(self, payload: WebhookCreatePayload, *, idempotency_key: str | None = None) -> WebhookSubscriptionJson:
        return cast(WebhookSubscriptionJson, self._json_object("POST", "/webhooks", cast(JsonObject, payload), idempotency_key=idempotency_key))

    def get_webhook(self, webhook_id: str) -> WebhookSubscriptionJson:
        return cast(WebhookSubscriptionJson, self._json_object("GET", f"/webhooks/{parse.quote(webhook_id)}"))

    def update_webhook(self, webhook_id: str, payload: JsonObject) -> WebhookSubscriptionJson:
        return cast(WebhookSubscriptionJson, self._json_object("PATCH", f"/webhooks/{parse.quote(webhook_id)}", payload))

    def delete_webhook(self, webhook_id: str) -> JsonObject:
        return self._json_object("DELETE", f"/webhooks/{parse.quote(webhook_id)}")

    def test_webhook(self, webhook_id: str, *, event_type: str = "webhook.test", payload: JsonObject | None = None) -> JsonObject:
        return self._json_object("POST", f"/webhooks/{parse.quote(webhook_id)}/test", {"event_type": event_type, "payload": payload or {}})

    def doctor(self) -> JsonObject:
        return self._json_object("GET", "/doctor")

    def metrics(self) -> JsonObject:
        return self._json_object("GET", "/system/metrics/summary")

    def security_summary(self) -> JsonObject:
        return self._json_object("GET", "/system/security/summary")

    def permission_rules(self) -> JsonObject:
        return self._json_object("GET", "/system/permissions/rules")

    def system_schema(self) -> JsonObject:
        return self._json_object("GET", "/system/schema")

    def export(self, workspace_id: str, kind: str, *, idempotency_key: str | None = None) -> JsonObject:
        return self._json_object("POST", f"/workspaces/{parse.quote(workspace_id)}/export/{kind}", idempotency_key=idempotency_key)

    def stream_run(
        self,
        run_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        timeout_seconds: float = 600.0,
        include_payloads: bool = False,
    ) -> Iterator[RunEvent]:
        started = time.monotonic()
        last_sequence = 0
        yield RunEvent("run_started", run_id, cast(JsonObject, self.get_run(run_id)), source="synthetic")
        while True:
            event_page = self.run_events_v2(run_id, after_sequence=last_sequence)
            for event in event_page.get("items", []):
                if not isinstance(event, dict):
                    continue
                sequence = int(event.get("sequence") or 0)
                last_sequence = max(last_sequence, sequence)
                payload = cast(JsonObject, dict(event))
                if include_payloads and payload.get("payload_ref"):
                    payload["payload"] = self.event_payload(str(payload["payload_ref"]))
                yield RunEvent(str(event.get("event_type") or "run.event"), run_id, payload, sequence=sequence, source="journal")
            run = self.get_run(run_id)
            status = str(run.get("status") or "")
            if status in TERMINAL_RUN_STATUSES:
                yield RunEvent("run_completed", run_id, {"run": cast(JsonObject, run)}, source="synthetic")
                return
            if time.monotonic() - started >= timeout_seconds:
                yield RunEvent("run_stream_timeout", run_id, {"run": cast(JsonObject, run)}, source="synthetic")
                return
            time.sleep(poll_interval_seconds)

    def stream_run_events(self, run_id: str, *, poll_interval_seconds: float = 1.0, timeout_seconds: float = 600.0) -> Iterator[RunEvent]:
        started = time.monotonic()
        seen_event_keys: set[str] = set()
        last_sequence = 0
        yield RunEvent("run_started", run_id, cast(JsonObject, self.get_run(run_id)))
        while True:
            event_page = self.run_events(run_id, after_sequence=last_sequence)
            for event in cast(list[JsonValue], event_page.get("items", []) if isinstance(event_page, dict) else []):
                if not isinstance(event, dict):
                    continue
                last_sequence = max(last_sequence, int(event.get("sequence") or 0))
                event_type = str(event.get("event_type") or "run.event")
                yield RunEvent(event_type, run_id, cast(JsonObject, event), sequence=last_sequence)
            timeline = self.timeline(run_id)
            for item in cast(list[JsonValue], timeline.get("items", []) if isinstance(timeline, dict) else []):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or item.get("created_at") or json.dumps(item, sort_keys=True, ensure_ascii=False))
                if key in seen_event_keys:
                    continue
                seen_event_keys.add(key)
                yield RunEvent(self._event_type_from_timeline(cast(JsonObject, item)), run_id, cast(JsonObject, item), source="timeline")
            gate = self.gate(run_id)
            yield RunEvent("gate_changed", run_id, gate)
            yield RunEvent("run_state_changed", run_id, self.run_state(run_id))
            run = self.get_run(run_id)
            if str(run.get("status") or "") in TERMINAL_RUN_STATUSES:
                yield RunEvent("run_completed", run_id, {"run": cast(JsonObject, run), "gate": gate})
                return
            if time.monotonic() - started >= timeout_seconds:
                yield RunEvent("run_stream_timeout", run_id, {"run": cast(JsonObject, run), "gate": gate})
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

    def _headers(self, *, headers: Mapping[str, str] | None = None, idempotency_key: str | None = None) -> dict[str, str]:
        merged = {"Content-Type": "application/json", **dict(self.default_headers or {}), **dict(headers or {})}
        if idempotency_key:
            merged["Idempotency-Key"] = idempotency_key
        return merged

    def _json_value(
        self,
        method: str,
        path: str,
        payload: JsonObject | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> JsonValue:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers=self._headers(headers=headers, idempotency_key=idempotency_key),
        )
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            content = response.read().decode("utf-8")
            return cast(JsonValue, json.loads(content)) if content else {}

    def _json_object(
        self,
        method: str,
        path: str,
        payload: JsonObject | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        value = self._json_value(method, path, payload, headers=headers, idempotency_key=idempotency_key)
        if not isinstance(value, dict):
            raise TypeError(f"Expected JSON object from {path}.")
        return cast(JsonObject, value)
