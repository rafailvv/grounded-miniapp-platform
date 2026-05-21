from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SECTION_ORDER = (
    ("current_state", "Current State"),
    ("task_specification", "Task Specification"),
    ("files_and_functions", "Files and Functions"),
    ("workflow", "Workflow"),
    ("errors_and_corrections", "Errors & Corrections"),
    ("learnings", "Learnings"),
    ("worklog", "Worklog"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, *, limit: int = 360) -> str:
    return " ".join(str(value or "").split())[:limit]


class SessionMemorySections:
    """Stable sectioned memory for continuing product generation work."""

    SCHEMA = "grounded.session_memory.v1"

    @classmethod
    def build(cls, *, workspace_id: str, memory: dict[str, Any] | None = None, runs: list[Any] | None = None) -> dict[str, Any]:
        memory = memory if isinstance(memory, dict) else {}
        runs = list(runs or [])
        latest = runs[0] if runs else None
        sections = {
            "current_state": cls._current_state(latest),
            "task_specification": cls._task_specification(latest, memory),
            "files_and_functions": cls._files_and_functions(latest, runs),
            "workflow": cls._workflow(memory, latest),
            "errors_and_corrections": cls._errors_and_corrections(memory, runs),
            "learnings": cls._learnings(memory),
            "worklog": cls._worklog(runs),
        }
        rendered_sections = [
            {"id": key, "title": title, "items": sections[key]}
            for key, title in SECTION_ORDER
        ]
        text_lines = ["Session memory (always loaded; update after successful or failed runs):"]
        for section in rendered_sections:
            text_lines.append(f"# {section['title']}")
            items = [item for item in section["items"] if isinstance(item, dict)]
            if not items:
                text_lines.append("- No stable notes yet.")
                continue
            for item in items[:6]:
                text_lines.append(f"- {item.get('text')}")
        counts = {section["id"]: len(section["items"]) for section in rendered_sections}
        return {
            "schema": cls.SCHEMA,
            "workspace_id": workspace_id,
            "status": "ready" if any(counts.values()) else "empty",
            "sections": rendered_sections,
            "counts": counts,
            "text": "\n".join(text_lines),
            "source_refs": {"workspace_memory": f"workspace_memory:{workspace_id}"},
            "generated_at": _now(),
        }

    @staticmethod
    def compact_text(session_memory: dict[str, Any] | None, *, limit: int = 2400) -> str:
        if not isinstance(session_memory, dict):
            return ""
        text = str(session_memory.get("text") or "").strip()
        if len(text) <= limit:
            return text
        head = max(1, limit - 80)
        return f"{text[:head]}\n...[session memory truncated]..."

    @classmethod
    def _current_state(cls, latest: Any) -> list[dict[str, Any]]:
        if latest is None:
            return []
        items = [
            cls._item(
                f"Latest run `{latest.run_id}` is `{latest.status}` with apply status `{latest.apply_status}` at stage `{latest.current_stage}`.",
                source="latest_run",
                ref=latest.run_id,
            )
        ]
        if getattr(latest, "summary", None):
            items.append(cls._item(_text(latest.summary), source="latest_run_summary", ref=latest.run_id))
        for issue in list(getattr(latest, "remaining_issues", []) or [])[:4]:
            if isinstance(issue, dict):
                items.append(cls._item(_text(issue.get("details") or issue.get("message") or issue), source="remaining_issue", ref=latest.run_id))
        return items

    @classmethod
    def _task_specification(cls, latest: Any, memory: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if latest is not None:
            items.append(cls._item(_text(getattr(latest, "prompt", "")), source="latest_prompt", ref=latest.run_id))
            contract = latest.acceptance_contract if isinstance(getattr(latest, "acceptance_contract", None), dict) else {}
            flows = [flow for flow in contract.get("flows") or [] if isinstance(flow, dict)]
            for flow in flows[:5]:
                label = flow.get("name") or flow.get("title") or flow.get("id") or "flow"
                roles = ", ".join(str(role) for role in flow.get("roles") or [])
                api_paths = ", ".join(str(path) for path in flow.get("api_paths") or [])
                detail = " / ".join(part for part in [roles, api_paths] if part)
                items.append(cls._item(f"{label}: {detail}" if detail else str(label), source="acceptance_flow", ref=latest.run_id))
        for item in cls._memory_items(memory, kinds={"product_decision"})[:4]:
            items.append(cls._item(_text(item.get("text")), source="workspace_memory", ref=item.get("memory_id")))
        return cls._dedupe(items)

    @classmethod
    def _files_and_functions(cls, latest: Any, runs: list[Any]) -> list[dict[str, Any]]:
        paths: list[str] = []
        for run in ([latest] if latest is not None else []) + runs[:3]:
            for path in getattr(run, "touched_files", []) or []:
                if str(path).strip():
                    paths.append(str(path))
            plan = getattr(run, "implementation_plan", {}) if isinstance(getattr(run, "implementation_plan", {}), dict) else {}
            for ledger in plan.get("product_task_ledger") or []:
                if isinstance(ledger, dict):
                    paths.extend(str(path) for path in ledger.get("owned_paths") or [] if str(path).strip())
        return [cls._item(path, source="touched_or_owned_path") for path in list(dict.fromkeys(paths))[:18]]

    @classmethod
    def _workflow(cls, memory: dict[str, Any], latest: Any) -> list[dict[str, Any]]:
        items = [cls._item(_text(item.get("text")), source="workspace_memory", ref=item.get("memory_id")) for item in cls._memory_items(memory, kinds={"reusable_workflow", "working_pattern"})[:6]]
        if latest is not None:
            checks = getattr(latest, "checks_summary", None)
            if checks is not None:
                items.append(cls._item("Run product checks, generated tests, browser proof, then apply only after the gate is green.", source="platform_workflow", ref=latest.run_id))
        return cls._dedupe(items)

    @classmethod
    def _errors_and_corrections(cls, memory: dict[str, Any], runs: list[Any]) -> list[dict[str, Any]]:
        items = [cls._item(_text(item.get("text")), source="workspace_memory", ref=item.get("memory_id")) for item in cls._memory_items(memory, kinds={"failure_shield", "failure_signature", "avoidance"})[:8]]
        for run in runs[:5]:
            if getattr(run, "failure_signature", None) or getattr(run, "failure_reason", None):
                items.append(cls._item(_text(f"{run.failure_signature or run.failure_class}: {run.failure_reason or run.root_cause_summary}"), source="run_failure", ref=run.run_id))
        return cls._dedupe(items)

    @classmethod
    def _learnings(cls, memory: dict[str, Any]) -> list[dict[str, Any]]:
        kinds = {"preference", "working_pattern", "product_decision", "avoidance"}
        return cls._dedupe([cls._item(_text(item.get("text")), source="workspace_memory", ref=item.get("memory_id")) for item in cls._memory_items(memory, kinds=kinds)[:10]])

    @classmethod
    def _worklog(cls, runs: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for run in runs[:8]:
            changed = ", ".join(str(path) for path in list(getattr(run, "touched_files", []) or [])[:4])
            suffix = f"; files: {changed}" if changed else ""
            items.append(cls._item(f"`{run.run_id}` {run.status}/{run.apply_status}: {_text(run.prompt, limit=140)}{suffix}", source="run_history", ref=run.run_id))
        return items

    @staticmethod
    def _memory_items(memory: dict[str, Any], *, kinds: set[str]) -> list[dict[str, Any]]:
        items = []
        for item in memory.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("status", "active") != "active":
                continue
            if str(item.get("kind") or "") in kinds:
                items.append(item)
        return items

    @staticmethod
    def _item(text: object, *, source: str, ref: object | None = None) -> dict[str, Any]:
        return {"text": _text(text), "source": source, "ref": ref}

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            text = str(item.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(item)
        return result
