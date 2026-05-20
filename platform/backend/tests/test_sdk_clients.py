from __future__ import annotations

from typing import Any

from sdk.python.grounded_client import GroundedClient, JsonObject


def test_python_sdk_builds_idempotency_headers() -> None:
    client = GroundedClient(default_headers={"Authorization": "Bearer test"})

    headers = client._headers(idempotency_key="run-create-1", headers={"X-Trace": "sdk"})

    assert headers["Authorization"] == "Bearer test"
    assert headers["X-Trace"] == "sdk"
    assert headers["Idempotency-Key"] == "run-create-1"


def test_python_sdk_mutating_methods_pass_idempotency_key() -> None:
    class RecordingClient(GroundedClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str, JsonObject | None, str | None]] = []

        def _json_object(
            self,
            method: str,
            path: str,
            payload: JsonObject | None = None,
            *,
            headers: dict[str, str] | None = None,
            idempotency_key: str | None = None,
        ) -> JsonObject:
            del headers
            self.calls.append((method, path, payload, idempotency_key))
            return {"run_id": "run_1", "status": "pending"}

    client = RecordingClient()

    client.create_run("ws_1", {"prompt": "build"}, idempotency_key="idem-run")
    client.review_fix("run_1", idempotency_key="idem-fix")
    client.create_webhook({"url": "https://example.com/hook", "events": ["run.completed"]}, idempotency_key="idem-hook")

    assert client.calls[0] == ("POST", "/workspaces/ws_1/runs", {"prompt": "build"}, "idem-run")
    assert client.calls[1] == ("POST", "/runs/run_1/review/fix", None, "idem-fix")
    assert client.calls[2][0:2] == ("POST", "/webhooks")
    assert client.calls[2][3] == "idem-hook"


def test_python_sdk_stream_run_uses_v2_journal_and_optional_payloads() -> None:
    class FakeStreamingClient(GroundedClient):
        def __init__(self) -> None:
            super().__init__()
            self.run_reads = 0
            self.event_reads = 0

        def get_run(self, run_id: str) -> JsonObject:
            self.run_reads += 1
            return {"run_id": run_id, "status": "running" if self.run_reads == 1 else "completed"}

        def run_events_v2(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> JsonObject:
            del limit
            self.event_reads += 1
            assert run_id == "run_1"
            assert after_sequence == 0
            return {
                "scope": "run",
                "run_id": run_id,
                "next_sequence": 1,
                "items": [
                    {
                        "event_id": "evt_1",
                        "run_id": run_id,
                        "sequence": 1,
                        "event_type": "tool.completed",
                        "payload_ref": "payload_1",
                    }
                ],
            }

        def event_payload(self, payload_ref: str) -> JsonObject:
            return {"payload_ref": payload_ref, "payload": {"ok": True}}

    events = list(FakeStreamingClient().stream_run("run_1", poll_interval_seconds=0, include_payloads=True))

    assert [event.type for event in events] == ["run_started", "tool.completed", "run_completed"]
    assert events[1].sequence == 1
    assert events[1].source == "journal"
    assert isinstance(events[1].payload["payload"], dict)
    assert events[1].payload["payload"]["payload_ref"] == "payload_1"
