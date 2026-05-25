from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from app.models.domain import RunRecord


ROLE_ORDER = ("client", "specialist", "manager")
DEFAULT_ALLOWED_TOOLS = ("read_files", "search_files", "apply_patch_to_draft", "write_file", "run_checks", "browser_verify")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "generated-skill"


class SkillifyService:
    """Turns a successful product run into a reusable runtime SKILL.md draft."""

    SCHEMA = "grounded.skillify.v1"

    def __init__(self, *, data_dir: Path) -> None:
        self.data_dir = data_dir

    def build(
        self,
        *,
        run: RunRecord,
        artifacts: dict[str, Any] | None = None,
        skill_id: str | None = None,
        title: str | None = None,
        write: bool = False,
        scope: str = "user",
    ) -> dict[str, Any]:
        if run.status != "completed" or run.apply_status != "applied":
            raise ValueError("Skillify requires a successful run with status=completed and apply_status=applied.")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        inferred_title = title or self._title_from_run(run)
        resolved_id = _slug(skill_id or inferred_title)
        evidence = self._evidence(run, artifacts=artifacts)
        content = self._content(run=run, skill_id=resolved_id, title=inferred_title, evidence=evidence)
        target_path = self._target_path(resolved_id, scope=scope)
        write_status = "preview"
        if write:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            write_status = "written"
        return {
            "schema": self.SCHEMA,
            "status": "ready",
            "write_status": write_status,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "skill_id": resolved_id,
            "title": inferred_title,
            "scope": scope,
            "target_path": str(target_path),
            "content": content,
            "evidence": evidence,
            "warnings": self._warnings(run=run, evidence=evidence),
            "created_at": _now(),
        }

    def _target_path(self, skill_id: str, *, scope: str) -> Path:
        if scope != "user":
            raise ValueError("Skillify currently writes only user-scoped skills.")
        return self.data_dir / "skills" / skill_id / "SKILL.md"

    @staticmethod
    def _title_from_run(run: RunRecord) -> str:
        contract = run.acceptance_contract if isinstance(run.acceptance_contract, dict) else {}
        product = str(contract.get("product") or contract.get("domain") or "").strip()
        if product:
            return product[:80]
        prompt = " ".join(str(run.prompt or "").split())
        prompt = re.sub(r"^(create|build|make|generate|создай|сделай|сгенерируй)\s+", "", prompt, flags=re.IGNORECASE)
        return (prompt.split(".")[0] or "Generated product workflow")[:80].strip()

    @classmethod
    def _evidence(cls, run: RunRecord, *, artifacts: dict[str, Any]) -> dict[str, Any]:
        contract = run.acceptance_contract if isinstance(run.acceptance_contract, dict) else {}
        plan = run.implementation_plan if isinstance(run.implementation_plan, dict) else {}
        checks = artifacts.get("check_results") or artifacts.get("items") or []
        if not isinstance(checks, list):
            checks = []
        flows = [item for item in contract.get("flows") or [] if isinstance(item, dict)]
        ledger = [item for item in plan.get("product_task_ledger") or [] if isinstance(item, dict)]
        paths = sorted(
            {
                *[str(path) for path in run.touched_files or [] if str(path).strip()],
                *[
                    str(path)
                    for item in ledger
                    for path in (item.get("owned_paths") or [])
                    if str(path).strip()
                ],
            }
        )
        return {
            "prompt": run.prompt,
            "roles": list(run.target_role_scope or ROLE_ORDER),
            "flows": flows[:8],
            "ledger": ledger[:12],
            "paths": paths[:80],
            "checks": [
                {"name": item.get("name"), "status": item.get("status"), "details": item.get("details")}
                for item in checks
                if isinstance(item, dict)
            ][:20],
            "acceptance_required": bool(contract.get("required")),
            "generation_mode": str(run.generation_mode),
        }

    @classmethod
    def _content(cls, *, run: RunRecord, skill_id: str, title: str, evidence: dict[str, Any]) -> str:
        when_to_use = cls._when_to_use(title=title, prompt=run.prompt, flows=evidence.get("flows") or [])
        validation = cls._validation(evidence)
        rules = cls._rules(evidence)
        acceptance = cls._acceptance(evidence)
        frontmatter = [
            "---",
            f"description: {cls._yaml_scalar(title + ' workflow pack generated from a successful run.')}",
            "whenToUse:",
            *[f"  - {cls._yaml_scalar(item)}" for item in when_to_use],
            "paths:",
            "  - miniapp/app/static/**",
            "  - miniapp/app/routes/**",
            "  - miniapp/tests/**",
            "allowedTools:",
            *[f"  - {tool}" for tool in DEFAULT_ALLOWED_TOOLS],
            "model: default",
            "effort: high",
            "validation:",
            *[f"  - {cls._yaml_scalar(item)}" for item in validation],
            "---",
        ]
        body = [
            f"# {title}",
            "",
            "Generated by Skillify from a successful run. Use this skill when a future prompt matches the same product workflow pattern.",
            "",
            "## Rules",
            "",
            *[f"- {item}" for item in rules],
            "",
            "## Acceptance",
            "",
            *[f"- {item}" for item in acceptance],
            "",
            "## Source Evidence",
            "",
            f"- Source run: `{run.run_id}`",
            f"- Skill id: `{skill_id}`",
            f"- Prompt: {run.prompt.strip()}",
        ]
        return "\n".join(frontmatter + body).strip() + "\n"

    @classmethod
    def _when_to_use(cls, *, title: str, prompt: str, flows: list[dict[str, Any]]) -> list[str]:
        phrases = [_slug(title).replace("-", " "), _slug(title)]
        for flow in flows:
            for key in ("name", "title", "id"):
                value = str(flow.get(key) or "").strip()
                if value:
                    phrases.append(value)
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9]{4,}", prompt)[:10]:
            phrases.append(token.lower())
        return list(dict.fromkeys([item for item in phrases if item]))[:14]

    @staticmethod
    def _validation(evidence: dict[str, Any]) -> list[str]:
        checks = {str(item.get("name")) for item in evidence.get("checks") or [] if isinstance(item, dict) and item.get("status") == "passed"}
        defaults = ["persisted_workflow", "role_coverage", "browser_flow_smoke"]
        return list(dict.fromkeys([*checks, *defaults]))[:10]

    @classmethod
    def _rules(cls, evidence: dict[str, Any]) -> list[str]:
        rules = [
            "Derive entities, fields, labels, and routes from the user's prompt; do not hard-code the original generated product data.",
            "Preserve the role split: client creates or consumes the core workflow, specialist operates the work queue, and manager sees operational control and metrics when relevant.",
            "Persist prompt-owned state through FastAPI routes and read the same state back in role UIs.",
            "Keep Telegram mobile screens compact, routeable, and free of raw implementation labels.",
            "Include empty, loading, and error states for the main persisted workflow.",
        ]
        for item in evidence.get("ledger") or []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or item.get("description") or item.get("intent") or "").strip()
            role = str(item.get("role") or "").strip()
            if content:
                rules.append(f"{role.capitalize() if role else 'Role'} surface: {content}")
        return list(dict.fromkeys(rules))[:12]

    @staticmethod
    def _acceptance(evidence: dict[str, Any]) -> list[str]:
        acceptance = [
            "API proof creates or updates the core persisted record and reads it back.",
            "Browser proof completes the primary client workflow and verifies the result appears in the relevant specialist or manager surface.",
            "Role coverage proves client, specialist, and manager surfaces are distinct and routeable.",
            "Generated Python and JavaScript tests cover the saved workflow and UI wiring.",
            "Mobile proof has no horizontal overflow, clipped controls, or unreadable Russian-length labels.",
        ]
        for flow in evidence.get("flows") or []:
            if isinstance(flow, dict):
                name = str(flow.get("name") or flow.get("title") or flow.get("id") or "").strip()
                if name:
                    acceptance.append(f"Flow `{name}` is covered end to end.")
        return list(dict.fromkeys(acceptance))[:12]

    @staticmethod
    def _warnings(*, run: RunRecord, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if not evidence.get("flows") and not evidence.get("ledger"):
            warnings.append({"code": "low_specificity", "message": "Run has no acceptance flows or product task ledger; generated skill may be generic."})
        if not evidence.get("checks"):
            warnings.append({"code": "missing_check_evidence", "message": "No check result artifact was found for this run."})
        if run.generation_mode in {"fast", "basic"}:
            warnings.append({"code": "low_generation_mode", "message": "Skill was generated from a fast/basic run; review before reusing automatically."})
        return warnings

    @staticmethod
    def _yaml_scalar(value: object) -> str:
        text = str(value or "").strip().replace('"', '\\"')
        if not text:
            return '""'
        if re.search(r"[:#\[\]{}]|^\s|\s$", text):
            return f'"{text}"'
        return text
