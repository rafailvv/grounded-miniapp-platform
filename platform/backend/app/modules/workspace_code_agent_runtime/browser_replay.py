from __future__ import annotations

from typing import Any

from app.models.domain import CheckExecutionRecord


class BrowserProofReplay:
    """Builds exact single-step browser repair packets from the last proof."""

    @staticmethod
    def failed_step_packet(execution: CheckExecutionRecord | None) -> dict[str, Any] | None:
        if execution is None:
            return None
        browser = next((result for result in execution.results if result.name == "browser_flow_smoke"), None)
        if browser is None or browser.status != "failed":
            return None
        diagnostics = dict(browser.diagnostics or {})
        scenario = BrowserProofReplay._scenario(diagnostics)
        failed_step = diagnostics.get("failed_step")
        failed_context = BrowserProofReplay._failed_step_context(diagnostics, scenario)
        dom_selector = diagnostics.get("dom_selector") or diagnostics.get("failed_selector") or failed_context.get("selector")
        screenshot_before = diagnostics.get("screenshot_before") or failed_context.get("screenshot_before")
        screenshot_after = diagnostics.get("screenshot_after") or failed_context.get("screenshot_after") or (list(diagnostics.get("screenshots") or [])[-1] if diagnostics.get("screenshots") else None)
        console_logs = list(diagnostics.get("console_logs") or diagnostics.get("console_errors") or [])[-20:]
        network_logs = list(diagnostics.get("network_logs") or diagnostics.get("network_errors") or [])[-20:]
        mobile_layout = diagnostics.get("mobile_layout") if isinstance(diagnostics.get("mobile_layout"), dict) else {}
        mobile_viewport = diagnostics.get("mobile_viewport") or mobile_layout.get("viewport") or mobile_layout.get("viewports") or {}
        replay_plan = BrowserProofReplay.replay_plan(
            failed_step=str(failed_step or ""),
            failed_route=str(diagnostics.get("failed_route") or failed_context.get("route") or ""),
            failed_selector=str(dom_selector or ""),
            scenario=scenario,
            mobile_viewport=mobile_viewport,
        )
        replayable_script = BrowserProofReplay.replayable_script(scenario=scenario, fallback_viewport=mobile_viewport)
        return {
            "schema": "grounded.browser_replay_packet.v2",
            "check": "browser_flow_smoke",
            "failed_step": failed_step,
            "failed_role": diagnostics.get("failed_role"),
            "failed_route": diagnostics.get("failed_route") or failed_context.get("route"),
            "failed_selector": diagnostics.get("failed_selector") or dom_selector,
            "dom_selector": dom_selector,
            "action": diagnostics.get("action"),
            "persisted_marker": diagnostics.get("persisted_marker") or diagnostics.get("persisted_state_marker") or diagnostics.get("created_marker") or diagnostics.get("created_state_marker"),
            "update_marker": diagnostics.get("update_marker") or diagnostics.get("update_state_marker") or diagnostics.get("updated_marker") or diagnostics.get("updated_state_marker"),
            "created_marker": diagnostics.get("created_marker") or diagnostics.get("created_state_marker"),
            "updated_marker": diagnostics.get("updated_marker") or diagnostics.get("updated_state_marker"),
            "console_errors": list(diagnostics.get("console_errors") or [])[-8:],
            "console_logs": console_logs,
            "network_errors": list(diagnostics.get("network_errors") or [])[-8:],
            "network_logs": network_logs,
            "visible_errors": list(diagnostics.get("visible_errors") or [])[-8:],
            "api_before": diagnostics.get("api_before"),
            "api_after": diagnostics.get("api_after"),
            "screenshots": list(diagnostics.get("screenshots") or [])[-4:],
            "screenshot_before": screenshot_before,
            "screenshot_after": screenshot_after,
            "mobile_layout": diagnostics.get("mobile_layout"),
            "mobile_viewport": mobile_viewport,
            "playwright_scenario": scenario,
            "replayable_script": replayable_script,
            "replayable_scripts": [replayable_script] if replayable_script else [],
            "failed_step_context": failed_context,
            "replay_plan": replay_plan,
            "repair_order": [
                "reproduce_failed_step",
                "patch_smallest_connected_slice",
                "rerun_failed_step",
                "rerun_full_browser_proof",
            ],
            "logs": list(browser.logs or [])[-8:],
            "next_action": "reproduce this exact Playwright failed step first, repair it, rerun the same step, then rerun the full browser proof",
        }

    @staticmethod
    def replayable_script(*, scenario: dict[str, Any], fallback_viewport: Any = None) -> dict[str, Any]:
        steps = [item for item in scenario.get("steps") or [] if isinstance(item, dict)] if isinstance(scenario, dict) else []
        script_steps: list[dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            route = str(step.get("route") or step.get("url") or step.get("path") or "")
            selector = str(step.get("selector") or step.get("dom_selector") or "")
            action = str(step.get("action") or step.get("step") or f"step_{index}")
            script_steps.append(
                {
                    "index": index,
                    "role": step.get("role"),
                    "route": route,
                    "action": action,
                    "selector": selector,
                    "input": step.get("input") or step.get("value") or step.get("text"),
                    "expect": step.get("expect") or step.get("assertion") or step.get("expected"),
                    "mobile_viewport": step.get("mobile_viewport") or fallback_viewport or scenario.get("mobile_viewport") or {},
                    "screenshot_before": step.get("screenshot_before"),
                    "screenshot_after": step.get("screenshot_after") or step.get("screenshot"),
                }
            )
        return {
            "schema": "grounded.browser_replay_script.v1",
            "mode": "playwright_step_replay",
            "steps": script_steps,
            "step_count": len(script_steps),
            "mobile_viewport": fallback_viewport or scenario.get("mobile_viewport") or {},
            "instructions": [
                "Open each step route in order.",
                "Apply selector/action/input exactly when present.",
                "Capture console, network, DOM, layout, and screenshot evidence after every step.",
            ],
        }

    @staticmethod
    def should_rerun_step_first(packet: dict[str, Any] | None) -> bool:
        if not packet:
            return False
        replay_plan = packet.get("replay_plan") if isinstance(packet.get("replay_plan"), dict) else {}
        return bool(replay_plan.get("first_action") == "reproduce_failed_step" or packet.get("failed_step") or packet.get("failed_selector") or packet.get("failed_route"))

    @staticmethod
    def replay_plan(*, failed_step: str, failed_route: str, failed_selector: str, scenario: dict[str, Any], mobile_viewport: Any) -> dict[str, Any]:
        steps = [item for item in scenario.get("steps") or [] if isinstance(item, dict)] if isinstance(scenario, dict) else []
        selected: dict[str, Any] = {}
        for item in reversed(steps):
            if failed_step and item.get("action") == failed_step:
                selected = item
                break
        if not selected and steps:
            selected = steps[-1]
        return {
            "schema": "grounded.browser_replay_plan.v1",
            "first_action": "reproduce_failed_step",
            "mode": "single_step_then_full_proof",
            "failed_step": failed_step,
            "route": failed_route or selected.get("route") or "",
            "selector": failed_selector or selected.get("selector") or "",
            "mobile_viewport": mobile_viewport or selected.get("mobile_viewport") or {},
            "scenario_step": selected,
            "instructions": [
                "Open the failed route at the recorded mobile viewport.",
                "Use the recorded selector/action from scenario_step.",
                "Verify console and network logs before changing code.",
                "After patching, rerun this same step before the full browser proof.",
            ],
        }

    @staticmethod
    def _scenario(diagnostics: dict[str, Any]) -> dict[str, Any]:
        scenario = diagnostics.get("playwright_scenario")
        if isinstance(scenario, dict):
            return scenario
        return {
            "schema": "grounded.browser_playwright_scenario.v1",
            "mobile_viewport": diagnostics.get("mobile_viewport") or {},
            "steps": list(diagnostics.get("ui_steps") or diagnostics.get("steps") or []),
        }

    @staticmethod
    def _failed_step_context(diagnostics: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
        context = diagnostics.get("failed_step_context")
        if isinstance(context, dict) and context:
            return context
        failed_step = str(diagnostics.get("failed_step") or "")
        for item in reversed([entry for entry in scenario.get("steps") or [] if isinstance(entry, dict)]):
            if failed_step and item.get("action") == failed_step:
                return item
        steps = [entry for entry in scenario.get("steps") or [] if isinstance(entry, dict)]
        return steps[-1] if steps else {}
