from __future__ import annotations

import json
import re
from typing import Any

from app.models.browser_replay_proof import BrowserReplayProofReport, BrowserReplayScenarioReport
from app.modules.workspace_code_agent_runtime.browser_replay import BrowserProofReplay
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService


class BrowserReplayProofService:
    """Build durable replayable browser proof artifacts from captured diagnostics."""

    def __init__(self, store: StateStore, *, event_journal_service: EventJournalService | None = None) -> None:
        self.store = store
        self.event_journal_service = event_journal_service

    @staticmethod
    def proof_ref(workspace_id: str, run_id: str) -> str:
        return f"browser_replay_proof:{workspace_id}:{run_id}"

    @staticmethod
    def scenario_ref(workspace_id: str, run_id: str, scenario_id: str) -> str:
        return f"browser_replay_scenario:{workspace_id}:{run_id}:{scenario_id}"

    def build(self, *, workspace_id: str, run_id: str, browser_proof: dict[str, Any], failed_packet: dict[str, Any] | None = None) -> BrowserReplayProofReport:
        scenarios = self._scenario_reports(workspace_id=workspace_id, run_id=run_id, browser_proof=browser_proof, failed_packet=failed_packet)
        scenario_refs: list[str] = []
        playwright_refs: list[str] = []
        for scenario in scenarios:
            ref = self.scenario_ref(workspace_id, run_id, scenario.scenario_id)
            scenario.playwright_spec_ref = f"{ref}:playwright_spec"
            scenario_refs.append(ref)
            playwright_refs.append(scenario.playwright_spec_ref)
            self.store.upsert("reports", ref, scenario.model_dump(mode="json", by_alias=True))
        latest_failed = failed_packet or {}
        report = BrowserReplayProofReport(
            workspace_id=workspace_id,
            run_id=run_id,
            status="ready" if scenarios else "empty",
            replay_proof_ref=self.proof_ref(workspace_id, run_id),
            scenario_refs=scenario_refs,
            scenarios=scenarios,
            playwright_spec_refs=playwright_refs,
            failed_replay_packet_ref=f"browser_replay:{workspace_id}:{run_id}:latest" if latest_failed else None,
            latest_failed_step=latest_failed,
            artifact_refs={
                "browser_proof": browser_proof.get("artifact_refs", {}).get("browser_proof") if isinstance(browser_proof.get("artifact_refs"), dict) else None,
                "normalized_browser_proof": f"browser_proof:{run_id}",
                "failed_replay_packet": f"browser_replay:{workspace_id}:{run_id}:latest" if latest_failed else None,
            },
        )
        self.store.upsert("reports", report.replay_proof_ref, report.model_dump(mode="json", by_alias=True))
        self._append_event(workspace_id, run_id, report)
        return report

    def get(self, *, workspace_id: str, run_id: str) -> dict[str, Any] | None:
        payload = self.store.get("reports", self.proof_ref(workspace_id, run_id))
        return dict(payload) if isinstance(payload, dict) else None

    def scenario(self, *, workspace_id: str, run_id: str, scenario_id: str) -> dict[str, Any] | None:
        payload = self.store.get("reports", self.scenario_ref(workspace_id, run_id, scenario_id))
        return dict(payload) if isinstance(payload, dict) else None

    def _scenario_reports(self, *, workspace_id: str, run_id: str, browser_proof: dict[str, Any], failed_packet: dict[str, Any] | None) -> list[BrowserReplayScenarioReport]:
        proof = browser_proof if isinstance(browser_proof, dict) else {}
        raw_scenarios = [item for item in proof.get("scenarios") or [] if isinstance(item, dict)]
        steps = [item for item in proof.get("steps") or [] if isinstance(item, dict)]
        playwright = proof.get("playwright_scenario") if isinstance(proof.get("playwright_scenario"), dict) else {}
        if not raw_scenarios and (steps or playwright):
            raw_scenarios = [{"scenario_id": "browser_step_1", "steps": steps or playwright.get("steps") or [], "status": proof.get("status") or "unknown"}]
        reports: list[BrowserReplayScenarioReport] = []
        for index, raw in enumerate(raw_scenarios, start=1):
            scenario_id = self._scenario_id(raw, index)
            scenario_steps = [item for item in raw.get("steps") or steps or playwright.get("steps") or [] if isinstance(item, dict)]
            if not scenario_steps and raw.get("route"):
                scenario_steps = [raw]
            viewport = self._viewport(raw, proof, playwright)
            scenario = {"schema": "grounded.browser_playwright_scenario.v1", "mobile_viewport": viewport, "steps": scenario_steps}
            script_json = BrowserProofReplay.replayable_script(scenario=scenario, fallback_viewport=viewport)
            spec = self._playwright_spec(scenario_id=scenario_id, steps=script_json.get("steps") or [], viewport=viewport)
            reports.append(
                BrowserReplayScenarioReport(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    status=self._status(raw, proof),
                    role=str(raw.get("role") or (scenario_steps[0].get("role") if scenario_steps else "") or "") or None,
                    route=str(raw.get("route") or (scenario_steps[0].get("route") if scenario_steps else "") or "") or None,
                    viewport=viewport if isinstance(viewport, dict) else {},
                    steps=list(script_json.get("steps") or []),
                    playwright_spec=spec,
                    screenshot_refs=self._screenshots(raw, proof, scenario_steps),
                    dom_snapshot_refs=self._dom_snapshots(raw, proof),
                    console_logs=self._logs(proof, "console"),
                    network_logs=self._logs(proof, "network"),
                    failed_step_context=proof.get("failed_step_context") if isinstance(proof.get("failed_step_context"), dict) else {},
                    replay_command_hint=f"npx playwright test replay/{scenario_id}.spec.ts",
                )
            )
        if failed_packet:
            scenario_id = "failed_step"
            step = failed_packet.get("failed_step_context") if isinstance(failed_packet.get("failed_step_context"), dict) else {}
            replay_plan = failed_packet.get("replay_plan") if isinstance(failed_packet.get("replay_plan"), dict) else {}
            route = str(failed_packet.get("failed_route") or replay_plan.get("route") or step.get("route") or "")
            selector = str(failed_packet.get("failed_selector") or failed_packet.get("dom_selector") or replay_plan.get("selector") or step.get("selector") or "")
            viewport = failed_packet.get("mobile_viewport") if isinstance(failed_packet.get("mobile_viewport"), dict) else replay_plan.get("mobile_viewport") if isinstance(replay_plan.get("mobile_viewport"), dict) else {}
            steps = [{"index": 1, "role": failed_packet.get("failed_role") or step.get("role"), "route": route, "action": failed_packet.get("action") or failed_packet.get("failed_step") or step.get("action"), "selector": selector, "input": step.get("input"), "expect": step.get("expect"), "mobile_viewport": viewport, "screenshot_before": failed_packet.get("screenshot_before"), "screenshot_after": failed_packet.get("screenshot_after")}]
            reports.append(
                BrowserReplayScenarioReport(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    scenario_id=scenario_id,
                    status="failed",
                    role=str(failed_packet.get("failed_role") or step.get("role") or "") or None,
                    route=route or None,
                    viewport=viewport,
                    steps=steps,
                    playwright_spec=self._playwright_spec(scenario_id=scenario_id, steps=steps, viewport=viewport),
                    screenshot_refs=self._dedupe([failed_packet.get("screenshot_before"), failed_packet.get("screenshot_after"), *list(failed_packet.get("screenshots") or [])]),
                    dom_snapshot_refs=[],
                    console_logs=self._dedupe([*list(failed_packet.get("console_logs") or []), *list(failed_packet.get("console_errors") or [])])[-40:],
                    network_logs=self._dedupe([*list(failed_packet.get("network_logs") or []), *list(failed_packet.get("network_errors") or [])])[-40:],
                    failed_step_context=step,
                    replay_command_hint="npx playwright test replay/failed_step.spec.ts",
                )
            )
        return reports[:50]

    @staticmethod
    def _scenario_id(raw: dict[str, Any], index: int) -> str:
        base = str(raw.get("scenario_id") or raw.get("flow_id") or raw.get("id") or f"scenario_{index}")
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_") or f"scenario_{index}"

    @staticmethod
    def _status(raw: dict[str, Any], proof: dict[str, Any]) -> str:
        status = str(raw.get("status") or proof.get("status") or "unknown").lower()
        return status if status in {"passed", "failed", "partial", "unknown"} else "unknown"

    @staticmethod
    def _viewport(raw: dict[str, Any], proof: dict[str, Any], playwright: dict[str, Any]) -> dict[str, Any]:
        for value in (raw.get("mobile_viewport"), raw.get("viewport"), playwright.get("mobile_viewport"), proof.get("mobile_viewport")):
            if isinstance(value, dict):
                return value
        return {}

    @classmethod
    def _screenshots(cls, raw: dict[str, Any], proof: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
        values: list[Any] = [raw.get("screenshot"), raw.get("screenshot_before"), raw.get("screenshot_after"), proof.get("screenshot_before"), proof.get("screenshot_after"), *list(proof.get("screenshots") or [])]
        for step in steps:
            values.extend([step.get("screenshot"), step.get("screenshot_before"), step.get("screenshot_after")])
        return cls._dedupe(values)

    @staticmethod
    def _dom_snapshots(raw: dict[str, Any], proof: dict[str, Any]) -> list[dict[str, Any]]:
        snapshots = []
        for item in [*(proof.get("dom_snapshots") or []), *(raw.get("dom_snapshots") or [])]:
            if isinstance(item, dict):
                snapshots.append(item)
        return snapshots[:40]

    @classmethod
    def _logs(cls, proof: dict[str, Any], kind: str) -> list[str]:
        keys = ("console_logs", "console_errors") if kind == "console" else ("network_logs", "network_errors")
        values: list[Any] = []
        for key in keys:
            values.extend(list(proof.get(key) or []))
        return cls._dedupe(values)[-80:]

    @staticmethod
    def _dedupe(values: list[Any]) -> list[str]:
        items: list[str] = []
        for value in values:
            if value is None:
                continue
            text = str(value)
            if text and text not in items:
                items.append(text)
        return items

    @staticmethod
    def _playwright_spec(*, scenario_id: str, steps: list[dict[str, Any]], viewport: dict[str, Any]) -> str:
        width = int(viewport.get("width") or 390) if isinstance(viewport, dict) else 390
        height = int(viewport.get("height") or 844) if isinstance(viewport, dict) else 844
        lines = [
            "import { test, expect } from '@playwright/test';",
            "",
            f"test('replay {scenario_id}', async ({{ page }}) => {{",
            f"  await page.setViewportSize({{ width: {width}, height: {height} }});",
        ]
        for step in steps:
            route = json.dumps(str(step.get("route") or "/"))
            selector = str(step.get("selector") or "")
            action = str(step.get("action") or "")
            value = step.get("input")
            expect_value = step.get("expect")
            lines.append(f"  await page.goto({route});")
            if selector and action:
                locator = f"page.locator({json.dumps(selector)})"
                lowered = action.lower()
                if value is not None:
                    lines.append(f"  await {locator}.fill({json.dumps(str(value))});")
                elif any(token in lowered for token in ("click", "submit", "tap", "press")):
                    lines.append(f"  await {locator}.click();")
                else:
                    lines.append(f"  await expect({locator}).toBeVisible();")
            if expect_value:
                lines.append(f"  await expect(page.getByText({json.dumps(str(expect_value))}).first()).toBeVisible();")
        lines.extend(["});", ""])
        return "\n".join(lines)

    def _append_event(self, workspace_id: str, run_id: str, report: BrowserReplayProofReport) -> None:
        if self.event_journal_service is None:
            return
        self.event_journal_service.append_run(
            workspace_id=workspace_id,
            run_id=run_id,
            event_type="browser.replay_proof.created",
            payload={"browser_replay_proof_ref": report.replay_proof_ref, "scenario_refs": report.scenario_refs, "status": report.status},
            actor="system",
            summary="Browser replay proof created.",
            source_ref=report.replay_proof_ref,
            idempotency_key=f"browser.replay_proof.created:{run_id}:{len(report.scenario_refs)}",
        )
