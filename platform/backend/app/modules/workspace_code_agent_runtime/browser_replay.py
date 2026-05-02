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
        return {
            "check": "browser_flow_smoke",
            "failed_step": diagnostics.get("failed_step"),
            "failed_role": diagnostics.get("failed_role"),
            "failed_route": diagnostics.get("failed_route"),
            "failed_selector": diagnostics.get("failed_selector"),
            "action": diagnostics.get("action"),
            "created_marker": diagnostics.get("created_marker") or diagnostics.get("created_state_marker"),
            "updated_marker": diagnostics.get("updated_marker") or diagnostics.get("updated_state_marker"),
            "console_errors": list(diagnostics.get("console_errors") or [])[-8:],
            "visible_errors": list(diagnostics.get("visible_errors") or [])[-8:],
            "api_before": diagnostics.get("api_before"),
            "api_after": diagnostics.get("api_after"),
            "screenshots": list(diagnostics.get("screenshots") or [])[-4:],
            "mobile_layout": diagnostics.get("mobile_layout"),
            "logs": list(browser.logs or [])[-8:],
            "next_action": "repair this exact browser step first, then rerun the full browser proof",
        }

    @staticmethod
    def should_rerun_step_first(packet: dict[str, Any] | None) -> bool:
        if not packet:
            return False
        return bool(packet.get("failed_step") or packet.get("failed_selector") or packet.get("failed_route"))
