from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


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
                    "path": str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path),
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
    def _title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip() or fallback
        return fallback

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

    @staticmethod
    def builtin() -> dict[str, dict[str, Any]]:
        items = [
            {
                "id": "state-workflow",
                "name": "State workflow",
                "activation": "create_or_behavior_edit",
                "constraints": ["Persist only the shared records and state transitions implied by the prompt."],
                "validation_hints": ["Prompt-derived persisted workflow exists."],
            },
            {
                "id": "role-surfaces",
                "name": "Role surfaces",
                "activation": "create_or_role_edit",
                "constraints": ["Role pages share connected state while exposing distinct actions."],
                "validation_hints": ["Each role has a distinct action surface."],
            },
            {
                "id": "route-manifest",
                "name": "Route manifest",
                "activation": "route_change",
                "constraints": ["Route metadata must match real static pages."],
                "validation_hints": ["Every role page is routeable."],
            },
            {
                "id": "mobile-shell",
                "name": "Mobile shell",
                "activation": "visual_or_quality",
                "constraints": ["Mobile-first role pages for Telegram widths."],
                "validation_hints": ["No horizontal overflow on mobile."],
            },
            {
                "id": "preview-profile",
                "name": "Preview profile",
                "activation": "preview_or_platform",
                "constraints": ["Preview profile selects the right host shell."],
                "validation_hints": ["Preview profile supports configured mock surface."],
            },
        ]
        return {str(item["id"]): item for item in items}

    @staticmethod
    def load_from_runtime(runtime_dir: Path, repo_root: Path) -> list[dict[str, Any]]:
        root = runtime_dir / "skills"
        if not root.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(root.glob("*/SKILL.md")):
            text = _read_text(path)
            if not text:
                continue
            skill_id = path.parent.name
            frontmatter, body = SkillPackCatalog._frontmatter(text)
            rules, acceptance = SkillPackCatalog._sections(text)
            items.append(
                {
                    "id": skill_id,
                    "name": str(frontmatter.get("description") or ProjectInstructionBundle._title(body, skill_id)).removeprefix("MAGIC DOC: ").strip(),
                    "source": str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path),
                    "activation": "frontmatter_match" if frontmatter else "skill_match",
                    "whenToUse": SkillPackCatalog._frontmatter_list(frontmatter.get("whenToUse") or frontmatter.get("when_to_use")),
                    "paths": SkillPackCatalog._frontmatter_list(frontmatter.get("paths")),
                    "allowedTools": SkillPackCatalog._frontmatter_list(frontmatter.get("allowedTools") or frontmatter.get("allowed_tools")),
                    "model": str(frontmatter.get("model") or "").strip(),
                    "effort": str(frontmatter.get("effort") or "").strip(),
                    "validation": SkillPackCatalog._frontmatter_list(frontmatter.get("validation")),
                    "frontmatter": frontmatter,
                    "constraints": rules[:8],
                    "validation_hints": acceptance[:8],
                    "body": body or text,
                }
            )
        return items

    @staticmethod
    def select_for_context(
        skills: list[dict[str, Any]],
        *,
        prompt: str = "",
        intent: str | None = None,
        generation_mode: str | None = None,
        paths: list[str] | None = None,
        failure_class: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        haystack = " ".join(
            [
                str(prompt or ""),
                str(intent or ""),
                str(generation_mode or ""),
                str(failure_class or ""),
                " ".join(paths or []),
            ]
        ).lower()
        selected: list[dict[str, Any]] = []
        for skill in skills:
            score = 0
            reasons: list[str] = []
            when_to_use = str(skill.get("whenToUse") or skill.get("activation") or "").lower()
            if when_to_use and any(token for token in re.split(r"[^a-z0-9_-]+", when_to_use) if token and token in haystack):
                score += 2
                reasons.append("whenToUse")
            for path_pattern in skill.get("paths") or []:
                prefix = str(path_pattern).rstrip("*")
                if prefix and any(str(path).startswith(prefix) for path in paths or []):
                    score += 2
                    reasons.append("paths")
            skill_id = str(skill.get("id") or "")
            if skill_id in {"telegram-miniapp-product"} and ("telegram" in haystack or str(intent or "") == "create"):
                score += 2
                reasons.append("platform")
            if skill_id in {"fastapi-persistence"} and any(token in haystack for token in ("api", "persist", "state", "sqlite", "backend")):
                score += 2
                reasons.append("persistence")
            if skill_id in {"repair-failed-generation"} and failure_class:
                score += 3
                reasons.append("failure")
            if skill_id in {"mobile-ui-polish"} and str(generation_mode or "").lower() == "quality":
                score += 2
                reasons.append("quality")
            if skill_id in {"browser-acceptance-proof"} and str(intent or "").lower() == "create":
                score += 2
                reasons.append("proof")
            if score:
                selected.append({**skill, "activation_reason": ", ".join(dict.fromkeys(reasons)), "activation_score": score})
        selected.sort(key=lambda item: (-int(item.get("activation_score") or 0), str(item.get("id") or "")))
        return selected[:limit]

    @staticmethod
    def compact_context(skills: list[dict[str, Any]], *, body_limit: int = 800) -> str:
        if not skills:
            return ""
        lines = ["Active runtime skills:"]
        for skill in skills:
            lines.append(f"- {skill.get('id')}: {skill.get('activation_reason') or skill.get('activation')}")
            constraints = "; ".join(str(item) for item in (skill.get("constraints") or [])[:4])
            validation = "; ".join(str(item) for item in (skill.get("validation_hints") or skill.get("validation") or [])[:4])
            if constraints:
                lines.append(f"  Rules: {constraints}")
            if validation:
                lines.append(f"  Validation: {validation}")
            body = str(skill.get("body") or "").strip()
            if body:
                lines.append(f"  Body excerpt: {body[:body_limit]}")
        return "\n".join(lines)

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
        {"id": "generate", "name": "/generate", "kind": "run", "description": "Create or extend the current product from the prompt.", "requires": ["workspace", "prompt"]},
        {"id": "fix", "name": "/fix", "kind": "run", "description": "Repair a selected failing run using its failure context.", "requires": ["workspace", "run"]},
        {"id": "polish", "name": "/polish", "kind": "run", "description": "Run a quality visual pass without changing product semantics.", "requires": ["workspace"]},
        {"id": "review", "name": "/review", "kind": "analysis", "description": "Inspect a run for bugs, missing proof, and risky paths.", "requires": ["run"]},
        {"id": "add-page", "name": "/add-page", "kind": "run", "description": "Add a prompt-derived routeable page to one or more roles.", "requires": ["workspace", "prompt"]},
        {"id": "add-role-flow", "name": "/add-role-flow", "kind": "run", "description": "Add a connected workflow across role surfaces.", "requires": ["workspace", "prompt"]},
        {"id": "doctor", "name": "/doctor", "kind": "diagnostic", "description": "Run platform diagnostics.", "requires": []},
        {"id": "memory", "name": "/memory", "kind": "knowledge", "description": "View or save workspace memory.", "requires": ["workspace"]},
        {"id": "rollback", "name": "/rollback", "kind": "safety", "description": "Rollback the selected applied run.", "requires": ["run"]},
        {"id": "acceptance", "name": "/acceptance", "kind": "analysis", "description": "Show generated acceptance scenarios.", "requires": ["run"]},
        {"id": "visual-qa", "name": "/visual-qa", "kind": "analysis", "description": "Show static and browser-derived visual QA.", "requires": ["run"]},
        {"id": "docs", "name": "/docs", "kind": "knowledge", "description": "Regenerate the workspace Magic Doc.", "requires": ["workspace"]},
    )

    @classmethod
    def list(cls) -> dict[str, Any]:
        return {"schema": "grounded.slash_commands.v1", "items": list(cls.COMMANDS)}

    @classmethod
    def resolve(cls, command_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        command = next((item for item in cls.COMMANDS if item["id"] == command_id or item["name"] == command_id), None)
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

    @staticmethod
    def _ui_action(command_id: str) -> dict[str, Any]:
        mapping = {
            "generate": {"type": "submit_composer"},
            "fix": {"type": "start_fix_run"},
            "review": {"type": "open_tab", "tab": "review"},
            "doctor": {"type": "open_tab", "tab": "doctor"},
            "memory": {"type": "open_tab", "tab": "memory"},
            "acceptance": {"type": "open_report", "report": "acceptance_scenarios"},
            "visual-qa": {"type": "open_report", "report": "visual_qa"},
            "docs": {"type": "open_report", "report": "magic_doc"},
            "rollback": {"type": "run_action", "action": "rollback"},
        }
        return mapping.get(command_id, {"type": "submit_composer_with_prompt"})

    @staticmethod
    def _prompt_template(command_id: str, payload: dict[str, Any]) -> str:
        detail = str(payload.get("prompt") or payload.get("detail") or "").strip()
        templates = {
            "polish": "Polish the current app visually. Preserve existing behavior and tests.",
            "add-page": f"Add a routeable page: {detail}".strip(),
            "add-role-flow": f"Add a connected role workflow: {detail}".strip(),
            "fix": "Analyze the selected run failure and apply the smallest safe fix.",
        }
        return templates.get(command_id, detail)


class WorkerRoleCatalog:
    @staticmethod
    def roles() -> dict[str, Any]:
        items = [
            {
                "worker_id": "planner",
                "purpose": "Prompt contract, acceptance scenarios, file ownership plan.",
                "allowed_paths": ["README.md", "docs/**", "miniapp/tests/**"],
                "handoff": "Produces implementation plan and scenario list before edits.",
            },
            {
                "worker_id": "backend_api",
                "purpose": "FastAPI routes, schemas, persistence, shared state.",
                "allowed_paths": ["miniapp/app/**/*.py", "miniapp/requirements.txt"],
                "handoff": "Owns API/persistence failures.",
            },
            {
                "worker_id": "client_ui",
                "purpose": "Client role HTML/CSS/JS and client child pages.",
                "allowed_paths": ["miniapp/app/static/client/**"],
                "handoff": "Owns client selector and layout failures.",
            },
            {
                "worker_id": "specialist_ui",
                "purpose": "Specialist role HTML/CSS/JS and child pages.",
                "allowed_paths": ["miniapp/app/static/specialist/**"],
                "handoff": "Owns specialist selector and layout failures.",
            },
            {
                "worker_id": "manager_ui",
                "purpose": "Manager role HTML/CSS/JS and child pages.",
                "allowed_paths": ["miniapp/app/static/manager/**"],
                "handoff": "Owns manager selector and layout failures.",
            },
            {
                "worker_id": "generated_tests",
                "purpose": "Generated Python and JS acceptance tests.",
                "allowed_paths": ["miniapp/tests/**"],
                "handoff": "Owns stale or brittle generated tests.",
            },
            {
                "worker_id": "verifier",
                "purpose": "Independent final review, checks, browser proof, visual QA.",
                "allowed_paths": [],
                "handoff": "Read-only unless a repair run is started from findings.",
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
            flows = AcceptanceScenarioGenerator._fallback_flows(run)
        scenarios = []
        for index, flow in enumerate(flows, start=1):
            flow_id = str(flow.get("id") or flow.get("name") or f"flow-{index}")
            roles = [
                role for role in (flow.get("roles") or run.target_role_scope or ROLE_ORDER)
                if str(role) in ROLE_ORDER
            ] or list(ROLE_ORDER)
            scenarios.append(
                {
                    "scenario_id": _slug(flow_id),
                    "title": str(flow.get("title") or flow.get("description") or flow_id).strip()[:120],
                    "roles": roles,
                    "steps": AcceptanceScenarioGenerator._steps_for_flow(flow, roles),
                    "proof": {
                        "api_check": "api_workflow_smoke",
                        "browser_check": "browser_flow_smoke",
                        "source_check": "frontend_interaction_static_smoke",
                    },
                    "status": AcceptanceScenarioGenerator._scenario_status(artifacts),
                }
            )
        return {
            "schema": "grounded.acceptance_scenarios.v1",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": "planned" if scenarios else "empty",
            "items": scenarios[:8],
            "source": "acceptance_contract" if (run.acceptance_contract or {}).get("flows") else "fallback",
            "created_at": _now(),
        }

    @staticmethod
    def _fallback_flows(run: Any) -> list[dict[str, Any]]:
        prompt = str(run.prompt or "Generated product workflow")
        return [
            {
                "id": "persisted-workflow",
                "title": prompt[:120],
                "description": "Prompt-derived persisted workflow across role surfaces.",
                "roles": list(run.target_role_scope or ROLE_ORDER),
            }
        ]

    @staticmethod
    def _steps_for_flow(flow: dict[str, Any], roles: list[str]) -> list[dict[str, Any]]:
        steps = [
            {"kind": "open_role", "role": roles[0], "expectation": "Primary role surface loads."},
            {"kind": "create_or_update", "role": roles[0], "expectation": "User-provided state is persisted through the API."},
        ]
        if len(roles) > 1:
            steps.append({"kind": "consume_state", "role": roles[-1], "expectation": "Another role can see or act on the shared state."})
        steps.append({"kind": "mobile_layout", "role": "all", "expectation": "No horizontal overflow or critical overlap."})
        custom_steps = [item for item in flow.get("steps") or [] if isinstance(item, dict)]
        return custom_steps[:6] or steps

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
        for item in timeline:
            kind = str(item.get("kind") or "unknown")
            phase_counts[kind] = phase_counts.get(kind, 0) + 1
            if str(item.get("status") or "").lower() in {"failed", "blocked", "conflict"}:
                blockers.append({"kind": kind, "title": item.get("title"), "payload": item.get("payload")})
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
            {"id": "skills_runtime_v1", "status": "current", "description": "Runtime skill packs are discovered from runtime/skills/*/SKILL.md."},
            {"id": "slash_commands_v1", "status": "current", "description": "Workbench slash commands expose stable ids and UI action hints."},
            {"id": "acceptance_scenarios_v1", "status": "current", "description": "Run acceptance scenarios are generated from acceptance contracts and proof status."},
            {"id": "visual_qa_v1", "status": "current", "description": "Static visual QA combines source checks with browser mobile diagnostics."},
            {"id": "trace_reducer_v1", "status": "current", "description": "Run timeline and tool events reduce into blockers, quality signals, and next action."},
            {"id": "magic_docs_v1", "status": "current", "description": "Workspace architecture docs can be regenerated from memory, routes, APIs, and runs."},
        ]
