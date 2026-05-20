from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import re
from typing import Any

from app.services.skill_registry import SkillRegistryService


ROLE_ORDER = ("client", "specialist", "manager")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "item"


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


class ProjectInstructionBundle:
    """Loads repo and generated-workspace guidance as a stable agent contract."""

    @staticmethod
    def build(*, repo_root: Path, template_dir: Path) -> dict[str, Any]:
        sources = []
        for path in (repo_root / "AGENTS.md", template_dir / "AGENTS.md", template_dir / "docs" / "agent-guidelines.md"):
            text = _read_text(path)
            if not text:
                continue
            sources.append(
                {
                    "path": ProjectInstructionBundle._source_path(path, repo_root=repo_root, template_dir=template_dir),
                    "title": ProjectInstructionBundle._title(text, path.stem),
                    "content": text,
                    "summary": ProjectInstructionBundle._summary(text),
                }
            )
        return {
            "schema": "grounded.project_instructions.v1",
            "status": "available" if sources else "missing",
            "precedence": ["workspace AGENTS.md", "template AGENTS.md", "template docs/agent-guidelines.md"],
            "sources": sources,
            "created_at": _now(),
        }

    @staticmethod
    def compact_summary(bundle: dict[str, Any], *, limit: int = 1800) -> str:
        lines = ["Project instruction summary:"]
        for source in bundle.get("sources") or []:
            if not isinstance(source, dict):
                continue
            lines.append(f"- {source.get('path')}: {source.get('summary')}")
        return "\n".join(lines)[:limit]

    @staticmethod
    def _source_path(path: Path, *, repo_root: Path, template_dir: Path) -> str:
        if path == template_dir / "AGENTS.md":
            return "AGENTS.md"
        return str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)

    @staticmethod
    def _title(text: str, default_title: str) -> str:
        for line in text.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip() or default_title
        return default_title

    @staticmethod
    def _summary(text: str) -> str:
        bullets = [
            line.strip("- ").strip()
            for line in text.splitlines()
            if line.strip().startswith("-") and len(line.strip()) > 4
        ][:8]
        return "; ".join(bullets)[:900]


class SkillPackCatalog:
    """Skill discovery layer inspired by Codex/Claude, scoped to this generator."""

    _runtime_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def builtin() -> dict[str, dict[str, Any]]:
        return SkillRegistryService.system_builtin()

    @classmethod
    def load_from_runtime(cls, runtime_dir: Path, repo_root: Path) -> list[dict[str, Any]]:
        return list(cls.prefetch(runtime_dir, repo_root).get("items") or [])

    @classmethod
    def prefetch(cls, runtime_dir: Path, repo_root: Path, *, force: bool = False) -> dict[str, Any]:
        return SkillRegistryService(runtime_dir=runtime_dir, repo_root=repo_root, data_dir=repo_root / "data").prefetch(force=force)

    @classmethod
    def search_for_context(
        cls,
        skills: list[dict[str, Any]],
        *,
        prompt: str = "",
        intent: str | None = None,
        generation_mode: str | None = None,
        paths: list[str] | None = None,
        failure_class: str | None = None,
        max_skills: int | None = None,
        max_body_chars: int | None = None,
        max_total_body_chars: int | None = None,
    ) -> dict[str, Any]:
        return SkillRegistryService.search_items_for_context(
            skills,
            prompt=prompt,
            intent=intent,
            generation_mode=generation_mode,
            paths=paths,
            failure_class=failure_class,
            max_skills=max_skills,
            max_body_chars=max_body_chars,
            max_total_body_chars=max_total_body_chars,
        )

    @classmethod
    def select_for_context(
        cls,
        skills: list[dict[str, Any]],
        *,
        prompt: str = "",
        intent: str | None = None,
        generation_mode: str | None = None,
        paths: list[str] | None = None,
        failure_class: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return list(
            cls.search_for_context(
                skills,
                prompt=prompt,
                intent=intent,
                generation_mode=generation_mode,
                paths=paths,
                failure_class=failure_class,
                max_skills=limit,
            ).get("selected")
            or []
        )

    @staticmethod
    def compact_context(skills: list[dict[str, Any]], *, body_limit: int = 800) -> str:
        return SkillRegistryService.compact_context(skills, body_limit=body_limit)

    @staticmethod
    def explicit_mentions(prompt: str, skills: list[dict[str, Any]]) -> set[str]:
        return SkillRegistryService.explicit_mentions(prompt, skills)

    @staticmethod
    def usage_telemetry(
        *,
        selected: list[dict[str, Any]],
        check_results: list[dict[str, Any]],
        run_status: str,
    ) -> dict[str, Any]:
        return SkillRegistryService.usage_telemetry(selected=selected, check_results=check_results, run_status=run_status)

    @staticmethod
    def _runtime_signature(root: Path) -> str:
        parts = []
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return str(hash(tuple(parts)))

    @staticmethod
    def _activation_budget(
        *,
        generation_mode: str | None,
        max_skills: int | None,
        max_body_chars: int | None,
        max_total_body_chars: int | None,
    ) -> dict[str, int]:
        mode = str(generation_mode or "").lower()
        defaults = {
            "fast": {"max_skills": 2, "max_body_chars": 350, "max_total_body_chars": 900},
            "balanced": {"max_skills": 4, "max_body_chars": 550, "max_total_body_chars": 2600},
            "quality": {"max_skills": 5, "max_body_chars": 800, "max_total_body_chars": 5200},
        }.get(mode, {"max_skills": 4, "max_body_chars": 550, "max_total_body_chars": 2600})
        return {
            "max_skills": max(1, int(max_skills or defaults["max_skills"])),
            "max_body_chars": max(120, int(max_body_chars or defaults["max_body_chars"])),
            "max_total_body_chars": max(240, int(max_total_body_chars or defaults["max_total_body_chars"])),
        }

    @staticmethod
    def _phrase_matches(phrase: str, haystack: str) -> bool:
        phrase = str(phrase or "").strip().lower()
        if not phrase:
            return False
        if phrase in haystack:
            return True
        tokens = [token for token in re.split(r"[^a-z0-9_-]+", phrase) if len(token) >= 3]
        return bool(tokens and all(token in haystack for token in tokens[:4]))

    @staticmethod
    def _path_matches(pattern: str, path: str) -> bool:
        pattern = str(pattern or "").strip().replace("\\", "/")
        path = str(path or "").strip().replace("\\", "/")
        if not pattern or not path:
            return False
        return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*").rstrip("/"))

    @staticmethod
    def _failure_matches(skill: dict[str, Any], failure_class: str) -> bool:
        failure = str(failure_class or "").lower()
        if not failure:
            return False
        return any(str(item).lower() in failure or failure in str(item).lower() for item in [*(skill.get("whenToUse") or []), *(skill.get("validation") or [])])

    @staticmethod
    def _conflict_group(skill: dict[str, Any]) -> str:
        skill_id = str(skill.get("id") or "")
        validation = " ".join(str(item).lower() for item in skill.get("validation") or skill.get("validation_hints") or [])
        paths = " ".join(str(item).lower() for item in skill.get("paths") or [])
        if "mobile" in validation or "static/**/styles" in paths or skill_id == "mobile-ui-polish":
            return "mobile_polish"
        if "api" in validation or "routes" in paths or skill_id == "fastapi-persistence":
            return "persistence"
        if "repair" in skill_id or "failing_check" in validation:
            return "repair"
        return ""

    @staticmethod
    def _sections(text: str) -> tuple[list[str], list[str]]:
        rules: list[str] = []
        acceptance: list[str] = []
        current: list[str] | None = None
        for line in text.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("## rules"):
                current = rules
                continue
            if lowered.startswith("## acceptance"):
                current = acceptance
                continue
            if lowered.startswith("## "):
                current = None
                continue
            if current is not None and line.strip().startswith("-"):
                current.append(line.strip("- ").strip())
        return rules, acceptance

    @staticmethod
    def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        data: dict[str, Any] = {}
        index = 1
        while index < len(lines):
            line = lines[index]
            if line.strip() == "---":
                return data, "\n".join(lines[index + 1 :]).lstrip()
            if ":" in line and not line.startswith((" ", "\t", "-")):
                key, raw_value = line.split(":", 1)
                key = key.strip()
                value = raw_value.strip().strip("\"'")
                if value:
                    data[key] = value
                else:
                    values: list[str] = []
                    cursor = index + 1
                    while cursor < len(lines):
                        child = lines[cursor].lstrip()
                        if not child.startswith("- "):
                            break
                        values.append(child[2:].strip().strip("\"'"))
                        cursor += 1
                    if values:
                        data[key] = values
                        index = cursor - 1
                    else:
                        data[key] = ""
            index += 1
        return {}, text

    @staticmethod
    def _frontmatter_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
        return []


class SlashCommandCatalog:
    """Workbench slash-command contract."""

    COMMANDS: tuple[dict[str, Any], ...] = (
        {"id": "generate", "name": "/generate", "kind": "workflow", "description": "Create a product app from the prompt and run production acceptance.", "requires": ["workspace", "prompt"], "workflow": "create_run"},
        {"id": "fix", "name": "/fix", "kind": "workflow", "description": "Repair the latest blocked or failed run from its active repair packet.", "requires": ["workspace", "run"], "workflow": "repair_latest_failure"},
        {"id": "polish", "name": "/polish", "kind": "workflow", "description": "Improve UI polish while preserving product semantics and acceptance proof.", "requires": ["workspace"], "workflow": "ui_polish_run"},
        {"id": "add-flow", "name": "/add-flow", "kind": "workflow", "description": "Add a new end-to-end scenario across API, persistence, UI, roles, mobile, and tests.", "requires": ["workspace", "prompt"], "workflow": "add_product_flow"},
        {"id": "review", "name": "/review", "kind": "workflow", "description": "Find product risks, proof gaps, stale tests, and apply blockers for a run.", "requires": ["run"], "workflow": "risk_review"},
        {"id": "acceptance", "name": "/acceptance", "kind": "workflow", "description": "Run the acceptance proof loop and refresh readiness evidence.", "requires": ["run"], "workflow": "acceptance_proof"},
        {"id": "deploy", "name": "/deploy", "kind": "workflow", "description": "Prepare deploy artifacts only after the production gate is green.", "requires": ["workspace"], "workflow": "deploy_bundle"},
        {"id": "babysit-pr", "name": "/babysit-pr", "kind": "workflow", "description": "Watch exported app PR CI, reviews, flaky failures, and next repair/push actions.", "requires": ["workspace"], "workflow": "pr_ci_babysitter"},
        {"id": "docs", "name": "/docs", "kind": "workflow", "description": "Regenerate and write product architecture documentation.", "requires": ["workspace"], "workflow": "product_architecture_docs"},
    )
    ALIASES: dict[str, str] = {
        "/add-page": "add-flow",
        "add-page": "add-flow",
        "/add-role-flow": "add-flow",
        "add-role-flow": "add-flow",
        "/visual-qa": "acceptance",
        "visual-qa": "acceptance",
        "/watch-pr": "babysit-pr",
        "watch-pr": "babysit-pr",
    }

    @classmethod
    def list(cls) -> dict[str, Any]:
        return {"schema": "grounded.slash_commands.v1", "items": list(cls.COMMANDS)}

    @classmethod
    def resolve(cls, command_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = cls.normalize_id(command_id)
        command = next((item for item in cls.COMMANDS if item["id"] == normalized or item["name"] == normalized), None)
        if command is None:
            raise KeyError(f"Slash command not found: {command_id}")
        payload = payload or {}
        return {
            "schema": "grounded.slash_command_resolution.v1",
            "command": command,
            "prompt_template": cls._prompt_template(str(command["id"]), payload),
            "ui_action": cls._ui_action(str(command["id"])),
            "created_at": _now(),
        }

    @classmethod
    def normalize_id(cls, command_id: str) -> str:
        raw = str(command_id or "").strip()
        lowered = raw.lower()
        if lowered in cls.ALIASES:
            return cls.ALIASES[lowered]
        return lowered.removeprefix("/")

    @staticmethod
    def _ui_action(command_id: str) -> dict[str, Any]:
        mapping = {
            "generate": {"type": "execute_workflow", "workflow": "create_run"},
            "fix": {"type": "execute_workflow", "workflow": "repair_latest_failure"},
            "polish": {"type": "execute_workflow", "workflow": "ui_polish_run"},
            "add-flow": {"type": "execute_workflow", "workflow": "add_product_flow"},
            "review": {"type": "execute_workflow", "workflow": "risk_review", "tab": "review"},
            "acceptance": {"type": "execute_workflow", "workflow": "acceptance_proof", "tab": "checks"},
            "deploy": {"type": "execute_workflow", "workflow": "deploy_bundle"},
            "docs": {"type": "execute_workflow", "workflow": "product_architecture_docs"},
        }
        return mapping.get(SlashCommandCatalog.normalize_id(command_id), {"type": "execute_workflow"})

    @staticmethod
    def _prompt_template(command_id: str, payload: dict[str, Any]) -> str:
        detail = str(payload.get("prompt") or payload.get("detail") or "").strip()
        templates = {
            "polish": "Polish the current app visually. Preserve existing behavior and tests.",
            "add-flow": f"Add a connected product flow: {detail}".strip(),
            "fix": "Analyze the selected run failure and apply the smallest safe fix.",
            "acceptance": "Run the full production acceptance proof loop and report exact blockers.",
            "deploy": "Prepare deploy artifacts after the production readiness gate is green.",
            "docs": "Update product architecture documentation from current routes, APIs, memory, and runs.",
        }
        return templates.get(SlashCommandCatalog.normalize_id(command_id), detail)


class WorkerRoleCatalog:
    @staticmethod
    def roles() -> dict[str, Any]:
        items = [
            {
                "worker_id": "backend_api_worker",
                "alias_ids": [],
                "purpose": "FastAPI routes, schemas, persistence, shared state.",
                "allowed_paths": ["miniapp/app/**/*.py", "miniapp/requirements.txt"],
                "handoff": "Owns API/persistence failures.",
            },
            {
                "worker_id": "client_surface_worker",
                "alias_ids": [],
                "purpose": "Client role HTML/CSS/JS and client child pages.",
                "allowed_paths": ["miniapp/app/static/client/**"],
                "handoff": "Owns client selector and layout failures.",
            },
            {
                "worker_id": "specialist_surface_worker",
                "alias_ids": [],
                "purpose": "Specialist role HTML/CSS/JS and child pages.",
                "allowed_paths": ["miniapp/app/static/specialist/**"],
                "handoff": "Owns specialist selector and layout failures.",
            },
            {
                "worker_id": "manager_surface_worker",
                "alias_ids": [],
                "purpose": "Manager role HTML/CSS/JS and child pages.",
                "allowed_paths": ["miniapp/app/static/manager/**"],
                "handoff": "Owns manager selector and layout failures.",
            },
            {
                "worker_id": "test_verifier_worker",
                "alias_ids": [],
                "purpose": "Generated Python and JS acceptance tests.",
                "allowed_paths": ["miniapp/tests/**"],
                "handoff": "Owns stale or brittle generated tests.",
            },
            {
                "worker_id": "mobile_polish_worker",
                "alias_ids": [],
                "purpose": "Independent mobile polish and visual QA after green workflow.",
                "allowed_paths": ["miniapp/app/static/**"],
                "handoff": "Owns mobile overflow, spacing, and role-surface polish failures.",
            },
            {
                "worker_id": "repair_worker",
                "alias_ids": [],
                "purpose": "Focused owned repair from failure signature or merge decision.",
                "allowed_paths": ["miniapp/app/**", "miniapp/tests/**"],
                "handoff": "Runs only from an explicit repair packet.",
            },
        ]
        return {"schema": "grounded.worker_roles.v1", "items": items}


class AcceptanceScenarioGenerator:
    @staticmethod
    def build(run: Any, artifacts: dict[str, Any]) -> dict[str, Any]:
        flows = [
            item for item in (run.acceptance_contract or {}).get("flows", [])
            if isinstance(item, dict)
        ]
        if not flows:
            return {
                "schema": "grounded.acceptance_scenarios.v1",
                "run_id": run.run_id,
                "workspace_id": run.workspace_id,
                "status": "blocked_contract_missing",
                "items": [],
                "source": "acceptance_contract_missing",
                "blocking": True,
                "message": "Acceptance scenarios require a prompt-derived acceptance/product contract; no platform-invented workflow was generated.",
                "created_at": _now(),
            }
        scenarios = []
        for index, flow in enumerate(flows, start=1):
            flow_id = str(flow.get("id") or flow.get("name") or f"flow-{index}")
            roles = [
                role for role in (flow.get("roles") or run.target_role_scope or ROLE_ORDER)
                if str(role) in ROLE_ORDER
            ] or list(ROLE_ORDER)
            steps = AcceptanceScenarioGenerator._steps_for_flow(flow, roles)
            scenarios.append(
                {
                    "scenario_id": _slug(flow_id),
                    "title": str(flow.get("title") or flow.get("description") or flow_id).strip()[:120],
                    "roles": roles,
                    "steps": steps,
                    "proof": {
                        "api_check": "api_workflow_smoke",
                        "browser_check": "browser_flow_smoke",
                        "source_check": "frontend_interaction_static_smoke",
                    },
                    "status": AcceptanceScenarioGenerator._scenario_status(artifacts) if steps else "blocked_contract_steps_missing",
                    "blocking": not bool(steps),
                }
            )
        overall_status = "blocked_contract_steps_missing" if any(not item.get("steps") for item in scenarios) else "planned"
        return {
            "schema": "grounded.acceptance_scenarios.v1",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": overall_status if scenarios else "empty",
            "items": scenarios[:8],
            "source": "acceptance_contract",
            "created_at": _now(),
        }

    @staticmethod
    def _steps_for_flow(flow: dict[str, Any], roles: list[str]) -> list[dict[str, Any]]:
        custom_steps = [item for item in flow.get("steps") or [] if isinstance(item, dict)]
        return custom_steps[:8]

    @staticmethod
    def _scenario_status(artifacts: dict[str, Any]) -> str:
        checks = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in artifacts.get("check_results") or []
            if isinstance(item, dict)
        }
        if checks.get("api_workflow_smoke") == "passed" and checks.get("browser_flow_smoke") == "passed":
            return "proved"
        if checks:
            return "partially_proved"
        return "planned"


class VisualQAGenerator:
    @staticmethod
    def build(*, run: Any, artifacts: dict[str, Any], source_dir: Path) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        for html_path in sorted((source_dir / "miniapp" / "app" / "static").rglob("*.html"))[:80]:
            rel = html_path.relative_to(source_dir).as_posix()
            text = _read_text(html_path)
            if "<meta name=\"viewport\"" not in text and "name='viewport'" not in text:
                issues.append({"kind": "missing_viewport", "path": rel, "severity": "medium", "message": "HTML page has no viewport meta tag."})
            if "/static/preview_bridge.js" not in text:
                issues.append({"kind": "missing_preview_bridge", "path": rel, "severity": "high", "message": "Route page is missing preview bridge script."})
        for css_path in sorted((source_dir / "miniapp" / "app" / "static").rglob("*.css"))[:80]:
            rel = css_path.relative_to(source_dir).as_posix()
            text = _read_text(css_path, limit=40000)
            fixed = []
            for line_no, line in enumerate(text.splitlines(), start=1):
                if re.search(r"\b(?:width|min-width|max-width)\s*:\s*(?:[5-9]\d{2,}|[1-9]\d{3,})px\b", line):
                    fixed.append({"line": line_no, "text": line.strip()[:180]})
            if fixed:
                issues.append({"kind": "fixed_width_risk", "path": rel, "severity": "medium", "message": "Large fixed width may break Telegram mobile view.", "evidence": fixed[:8]})
            checks.append(
                {
                    "path": rel,
                    "has_mobile_media": bool(re.search(r"@media[^{]+max-width\s*:\s*(?:4[0-9]{2}|3[0-9]{2})px", text, re.I)),
                    "has_overflow_guard": "overflow-x" in text.lower() or "min-width: 0" in text.lower(),
                }
            )
        browser_mobile = VisualQAGenerator._browser_mobile_report(run, artifacts)
        if isinstance(browser_mobile, dict) and browser_mobile.get("status") == "failed":
            issues.append({"kind": "browser_mobile_layout_failed", "severity": "high", "message": "Browser proof reported mobile layout failure.", "evidence": browser_mobile})
        status = "failed" if any(item.get("severity") == "high" for item in issues) else "needs_review" if issues else "passed"
        return {
            "schema": "grounded.visual_qa.v1",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "issues": issues,
            "checks": checks,
            "browser_mobile_layout": browser_mobile,
            "viewports": [{"width": 360}, {"width": 390}, {"width": 430}],
            "created_at": _now(),
        }

    @staticmethod
    def _browser_mobile_report(run: Any, artifacts: dict[str, Any]) -> dict[str, Any]:
        browser = {}
        for item in artifacts.get("check_results") or []:
            if isinstance(item, dict) and item.get("name") == "browser_flow_smoke":
                browser = item
                break
        diagnostics = browser.get("diagnostics") if isinstance(browser.get("diagnostics"), dict) else {}
        mobile = run.mobile_layout_report or diagnostics.get("mobile_layout") or {}
        return dict(mobile) if isinstance(mobile, dict) else {}


class TraceReducer:
    @staticmethod
    def build(*, run: Any, timeline: list[dict[str, Any]], tool_events: list[dict[str, Any]], artifacts: dict[str, Any]) -> dict[str, Any]:
        phase_counts: dict[str, int] = {}
        blockers: list[dict[str, Any]] = []
        repair_events: list[dict[str, Any]] = []
        for item in timeline:
            kind = str(item.get("kind") or "unknown")
            phase_counts[kind] = phase_counts.get(kind, 0) + 1
            if str(item.get("status") or "").lower() in {"failed", "blocked", "conflict"}:
                blockers.append({"kind": kind, "title": item.get("title"), "payload": item.get("payload")})
            if "repair" in kind or "repair" in str(item.get("title") or "").lower():
                repair_events.append({"kind": kind, "title": item.get("title"), "payload": item.get("payload")})
        changed_files = list(run.touched_files or [])
        if not changed_files:
            changed_files = TraceReducer._paths_from_diff(str(artifacts.get("diff") or ""))
        quality = {
            "has_diff": bool(str(artifacts.get("diff") or "").strip() or changed_files),
            "has_checks": bool(artifacts.get("check_results")),
            "has_browser_proof": bool(artifacts.get("browser_flow_proof") or artifacts.get("browser_proof_steps") or run.browser_flow_proof),
            "has_review": bool(artifacts.get("review") or run.verifier_review_ref),
            "tool_event_count": len(tool_events),
        }
        return {
            "schema": "grounded.trace_reducer.v1",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": "blocked" if blockers else "summarized",
            "phase_counts": phase_counts,
            "blockers": blockers[:20],
            "changed_files": changed_files[:80],
            "quality_signals": quality,
            "next_action": TraceReducer._next_action(run, blockers, quality),
            "last_failed_attempt": blockers[-1] if blockers else {},
            "repeated_action": TraceReducer._repeated_action(tool_events),
            "next_best_repair_case": repair_events[-1] if repair_events else (blockers[-1] if blockers else {}),
            "stale_diff": TraceReducer._stale_diff(blockers),
            "created_at": _now(),
        }

    @staticmethod
    def _paths_from_diff(diff: str) -> list[str]:
        paths = []
        for line in diff.splitlines():
            if line.startswith("diff --git ") and " b/" in line:
                paths.append(line.split(" b/", 1)[1].strip())
        return list(dict.fromkeys(paths))

    @staticmethod
    def _next_action(run: Any, blockers: list[dict[str, Any]], quality: dict[str, Any]) -> dict[str, Any]:
        if blockers:
            return {"action": "repair", "reason": "Trace contains failed or blocked phase.", "target": blockers[0].get("kind")}
        if run.status not in {"completed", "awaiting_approval"}:
            return {"action": "continue_run", "reason": "Run is not terminal."}
        if not quality.get("has_browser_proof"):
            return {"action": "browser_verify", "reason": "No browser proof is recorded."}
        return {"action": "none", "reason": "Trace has no blocking phase."}

    @staticmethod
    def _repeated_action(tool_events: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for event in tool_events:
            key = str(event.get("tool") or event.get("tool_call_id") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
        repeated = [key for key, count in counts.items() if count > 1]
        return {"status": "repeated", "tools": repeated[:8]} if repeated else {}

    @staticmethod
    def _stale_diff(blockers: list[dict[str, Any]]) -> dict[str, Any]:
        latest = blockers[-1] if blockers else {}
        text = json.dumps(latest, ensure_ascii=False, default=str).lower()
        return {"status": "suspected", "event": latest} if "stale" in text or "old_string_not_found" in text else {}


class MagicDocsBuilder:
    @staticmethod
    def build(*, workspace: Any, memory: dict[str, Any], runs: list[Any], source_dir: Path) -> dict[str, Any]:
        latest_runs = sorted(runs, key=lambda item: getattr(item, "updated_at", getattr(item, "created_at", "")), reverse=True)[:5]
        routes = MagicDocsBuilder._routes(source_dir)
        api_refs = MagicDocsBuilder._api_refs(source_dir)
        lines = [
            "# MAGIC DOC: Product Architecture",
            "",
            "## Workspace",
            f"- Name: {workspace.name}",
            f"- Target platform: {getattr(workspace.target_platform, 'value', workspace.target_platform)}",
            f"- Current revision: {workspace.current_revision_id or 'none'}",
            "",
            "## Product Memory",
        ]
        memory_items = [item for item in memory.get("items") or [] if isinstance(item, dict)]
        lines.extend([f"- {item.get('kind')}: {item.get('text')}" for item in memory_items[-12:]] or ["- No stored memory yet."])
        lines.extend(["", "## Routes", *([f"- {route}" for route in routes[:40]] or ["- No route pages detected."])])
        lines.extend(["", "## API References", *([f"- {ref}" for ref in api_refs[:40]] or ["- No API refs detected."])])
        lines.extend(["", "## Recent Runs"])
        lines.extend(
            [
                f"- {run.run_id}: {run.status}; {run.summary or run.failure_reason or run.prompt[:120]}"
                for run in latest_runs
            ]
            or ["- No runs yet."]
        )
        content = "\n".join(lines).strip() + "\n"
        return {
            "schema": "grounded.magic_doc.v1",
            "workspace_id": workspace.workspace_id,
            "path": "docs/product-architecture.md",
            "content": content,
            "updated_at": _now(),
        }

    @staticmethod
    def _routes(source_dir: Path) -> list[str]:
        root = source_dir / "miniapp" / "app" / "static"
        routes: list[str] = []
        if not root.exists():
            return routes
        for path in sorted(root.rglob("index.html")):
            try:
                rel = path.relative_to(root).parent.as_posix()
            except ValueError:
                continue
            routes.append("/" + rel if rel != "." else "/")
        return routes

    @staticmethod
    def _api_refs(source_dir: Path) -> list[str]:
        refs: set[str] = set()
        for path in sorted((source_dir / "miniapp" / "app").rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".py", ".js", ".mjs"}:
                continue
            text = _read_text(path, limit=40000)
            refs.update(re.findall(r"['\"](/api/[^'\"\s)]+)['\"]", text))
        return sorted(refs)


class ConfigMigrationCatalog:
    @staticmethod
    def items() -> list[dict[str, Any]]:
        return [
            {"id": "state_store_v2", "status": "current", "description": "State store supports sharded runtime reports and additive run fields."},
            {"id": "workspace_memory_v1", "status": "current", "description": "Workspace memory is stored as a report with secret scanning and stale reference checks."},
            {"id": "skills_runtime_v2", "status": "current", "description": "Scoped skill packs are discovered from system, repo, plugin, and user roots."},
            {"id": "slash_commands_v1", "status": "current", "description": "Workbench slash commands expose stable ids and UI action hints."},
            {"id": "acceptance_scenarios_v1", "status": "current", "description": "Run acceptance scenarios are generated from acceptance contracts and proof status."},
            {"id": "visual_qa_v1", "status": "current", "description": "Static visual QA combines source checks with browser mobile diagnostics."},
            {"id": "trace_reducer_v1", "status": "current", "description": "Run timeline and tool events reduce into blockers, quality signals, and next action."},
            {"id": "magic_docs_v1", "status": "current", "description": "Workspace architecture docs can be regenerated from memory, routes, APIs, and runs."},
        ]
