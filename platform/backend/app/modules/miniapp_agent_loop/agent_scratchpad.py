from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentScratchpad:
    """Run-scoped scratchpad used by the coordinator and repair loop."""

    run_id: str
    plan_markdown: str = ""
    route_ui_manifest: dict[str, Any] = field(default_factory=dict)
    worker_notes: list[dict[str, Any]] = field(default_factory=list)
    failed_fixes: list[dict[str, Any]] = field(default_factory=list)
    compact_boundaries: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def _plan_to_markdown(plan: dict[str, Any], todo: list[dict[str, Any]]) -> str:
        title = str(plan.get("summary") or plan.get("product_goal") or "Implementation plan").strip()
        lines = [f"# {title}", "", "## Todo"]
        for item in todo:
            status = str(item.get("status") or "pending")
            text = str(item.get("task") or item.get("step") or "").strip()
            if text:
                lines.append(f"- [{status}] {text}")
        roles = plan.get("roles") if isinstance(plan.get("roles"), list) else []
        if roles:
            lines.extend(["", "## Roles", *[f"- {role}" for role in roles]])
        entities = plan.get("primary_entities") if isinstance(plan.get("primary_entities"), list) else []
        if entities:
            lines.extend(["", "## State", *[f"- {entity}" for entity in entities]])
        return "\n".join(lines).strip() + "\n"

    def set_plan(self, plan: dict[str, Any], todo: list[dict[str, Any]]) -> None:
        self.plan_markdown = self._plan_to_markdown(plan, todo)

    def set_route_ui_manifest(self, manifest: dict[str, Any]) -> None:
        self.route_ui_manifest = dict(manifest or {})

    def append_worker_note(self, worker_id: str, summary: str, payload: dict[str, Any] | None = None) -> None:
        self.worker_notes.append(
            {
                "created_at": _now(),
                "worker_id": worker_id,
                "summary": str(summary or "")[:800],
                "payload": payload or {},
            }
        )

    def append_failed_fix(self, signature: str, summary: str, payload: dict[str, Any] | None = None) -> None:
        self.failed_fixes.append(
            {
                "created_at": _now(),
                "signature": str(signature or "")[:240],
                "summary": str(summary or "")[:800],
                "payload": payload or {},
            }
        )

    def set_next_action(self, *, action: str, reason: str = "", payload: dict[str, Any] | None = None) -> None:
        self.next_action = {
            "updated_at": _now(),
            "action": str(action or "")[:800],
            "reason": str(reason or "")[:800],
            "payload": payload or {},
        }

    def record_compact_boundary(
        self,
        *,
        plan: dict[str, Any],
        diff_summary: str | None,
        failed_signatures: list[str],
        next_action: str,
    ) -> dict[str, Any]:
        item = {
            "created_at": _now(),
            "plan_summary": str(plan.get("summary") or plan.get("product_goal") or "")[:600],
            "diff_summary": str(diff_summary or "")[:1200],
            "failed_signatures": list(dict.fromkeys(str(item) for item in failed_signatures if str(item).strip()))[-12:],
            "next_action": str(next_action or "")[:800],
        }
        self.compact_boundaries.append(item)
        self.set_next_action(action=next_action, reason="compact_boundary", payload=item)
        return item

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "files": {
                "plan.md": self.plan_markdown,
                "route_ui_manifest.json": self.route_ui_manifest,
                "worker_notes.jsonl": list(self.worker_notes),
                "failed_fixes.jsonl": list(self.failed_fixes),
                "next_action.json": dict(self.next_action),
            },
            "compact_boundaries": list(self.compact_boundaries),
        }
