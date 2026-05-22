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
    def build(
        *,
        repo_root: Path,
        template_dir: Path,
        workspace_root: Path | None = None,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        targets = ProjectInstructionBundle._normalize_targets(paths or [])
        sources = []
        seen: set[Path] = set()
        candidates: list[tuple[Path, Path, str]] = []
        candidates.append((repo_root / "AGENTS.md", repo_root, "repo"))
        candidates.extend((path, template_dir, "template") for path in ProjectInstructionBundle._agents_files(template_dir))
        if workspace_root is not None:
            candidates.extend((path, workspace_root, "workspace") for path in ProjectInstructionBundle._agents_files(workspace_root))
        candidates.append((template_dir / "docs" / "agent-guidelines.md", template_dir, "template_docs"))
        for path, scope_root, layer in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            text = _read_text(path)
            if not text:
                continue
            scope_dir = ProjectInstructionBundle._scope_dir(path, scope_root=scope_root)
            rules = ProjectInstructionBundle._rules(text)
            applicable = ProjectInstructionBundle._applies(scope_dir, targets) if targets else True
            sources.append(
                {
                    "path": ProjectInstructionBundle._source_path(path, repo_root=repo_root, template_dir=template_dir),
                    "layer": layer,
                    "scope": scope_dir,
                    "depth": 0 if scope_dir == "." else len(Path(scope_dir).parts),
                    "precedence": ProjectInstructionBundle._precedence(layer, scope_dir),
                    "applicable": applicable,
                    "matched_paths": ProjectInstructionBundle._matched_paths(scope_dir, targets),
                    "title": ProjectInstructionBundle._title(text, path.stem),
                    "content": text,
                    "summary": ProjectInstructionBundle._summary(text),
                    "rules": rules,
                }
            )
        sources.sort(key=lambda source: int(source.get("precedence", 0) or 0))
        applicable_sources = [source for source in sources if source.get("applicable")]
        active_rules = ProjectInstructionBundle._active_rules(applicable_sources)
        return {
            "schema": "grounded.project_instructions.v1",
            "status": "available" if sources else "missing",
            "precedence": [
                "lower number loads first",
                "repo root AGENTS.md",
                "template root/nested AGENTS.md",
                "workspace root/nested AGENTS.md",
                "deeper scope overrides parent scope for matching files",
            ],
            "targets": targets,
            "sources": sources,
            "applicable_sources": [
                {
                    "path": source.get("path"),
                    "layer": source.get("layer"),
                    "scope": source.get("scope"),
                    "precedence": source.get("precedence"),
                    "matched_paths": source.get("matched_paths"),
                }
                for source in applicable_sources
            ],
            "active_rules": active_rules,
            "conflicts": ProjectInstructionBundle._conflicts(active_rules),
            "created_at": _now(),
        }

    @staticmethod
    def compact_summary(bundle: dict[str, Any], *, limit: int = 1800) -> str:
        lines = ["Project instruction summary:"]
        active_rules = [rule for rule in bundle.get("active_rules") or [] if isinstance(rule, dict)]
        if active_rules:
            lines.append("Active AGENTS.md rules for current files:")
            for rule in active_rules[:10]:
                lines.append(f"- {rule.get('source_path')} ({rule.get('scope')}): {rule.get('text')}")
        conflicts = [item for item in bundle.get("conflicts") or [] if isinstance(item, dict)]
        if conflicts:
            lines.append("Instruction conflicts to resolve by precedence:")
            for item in conflicts[:4]:
                winner = item.get("winner") or {}
                shadowed = item.get("shadowed") or {}
                lines.append(f"- {item.get('rule_key')}: use {winner.get('source_path')} over {shadowed.get('source_path')}")
        sources = bundle.get("applicable_sources") or bundle.get("sources") or []
        by_path = {str(source.get("path")): source for source in bundle.get("sources") or [] if isinstance(source, dict)}
        for source_ref in sources:
            if not isinstance(source_ref, dict):
                continue
            source = by_path.get(str(source_ref.get("path"))) or source_ref
            if not isinstance(source, dict):
                continue
            scope = source.get("scope") or "."
            applies = "applicable" if source.get("applicable", True) else "available"
            lines.append(f"- {source.get('path')} [{applies}; scope={scope}; precedence={source.get('precedence')}]: {source.get('summary')}")
        return "\n".join(lines)[:limit]

    @staticmethod
    def _source_path(path: Path, *, repo_root: Path, template_dir: Path) -> str:
        if path == template_dir / "AGENTS.md":
            return "AGENTS.md"
        return str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)

    @staticmethod
    def _agents_files(root: Path) -> list[Path]:
        if not root.exists():
            return []
        ignored = {"node_modules", ".git", "__pycache__", ".venv", "dist", "build"}
        return [
            path
            for path in sorted(root.rglob("AGENTS.md"))
            if not any(part in ignored for part in path.parts)
        ]

    @staticmethod
    def _scope_dir(path: Path, *, scope_root: Path) -> str:
        try:
            rel = path.parent.relative_to(scope_root)
        except ValueError:
            return "."
        value = rel.as_posix()
        return "." if value == "." else value

    @staticmethod
    def _normalize_targets(paths: list[str]) -> list[str]:
        targets = []
        for raw in paths:
            value = str(raw or "").strip().replace("\\", "/").lstrip("/")
            if value and value not in targets:
                targets.append(value)
        return targets[:40]

    @staticmethod
    def _applies(scope: str, targets: list[str]) -> bool:
        if scope in {"", "."}:
            return True
        prefix = scope.rstrip("/") + "/"
        return any(target == scope or target.startswith(prefix) for target in targets)

    @staticmethod
    def _matched_paths(scope: str, targets: list[str]) -> list[str]:
        if not targets:
            return []
        if scope in {"", "."}:
            return targets[:12]
        prefix = scope.rstrip("/") + "/"
        return [target for target in targets if target == scope or target.startswith(prefix)][:12]

    @staticmethod
    def _precedence(layer: str, scope: str) -> int:
        base = {"repo": 100, "template_docs": 180, "template": 200, "workspace": 300}.get(layer, 100)
        depth = 0 if scope in {"", "."} else len(Path(scope).parts)
        return base + depth

    @staticmethod
    def _rules(text: str) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        current_heading = "general"
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                current_heading = stripped.lstrip("#").strip().lower() or "general"
                continue
            if not stripped.startswith(("-", "*")):
                continue
            body = stripped.lstrip("-* ").strip()
            if len(body) < 4:
                continue
            rules.append({"text": body[:500], "section": current_heading, "key": ProjectInstructionBundle._rule_key(body, current_heading)})
        return rules[:80]

    @staticmethod
    def _rule_key(text: str, section: str) -> str:
        lowered = re.sub(r"[^a-z0-9а-яё ]+", " ", text.lower())
        words = [word for word in lowered.split() if len(word) > 3 and word not in {"must", "should", "never", "always", "prefer", "keep", "using", "when", "with", "that", "this"}]
        return f"{section}:{'-'.join(words[:4])}" if words else section

    @staticmethod
    def _active_rules(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for source in sources:
            for rule in source.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                items.append(
                    {
                        **rule,
                        "source_path": source.get("path"),
                        "layer": source.get("layer"),
                        "scope": source.get("scope"),
                        "precedence": source.get("precedence"),
                    }
                )
        return sorted(items, key=lambda item: int(item.get("precedence", 0) or 0), reverse=True)

    @staticmethod
    def _conflicts(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            key = str(rule.get("key") or "")
            if not key:
                continue
            by_key.setdefault(key, []).append(rule)
        conflicts = []
        for key, grouped in by_key.items():
            unique_texts = {str(item.get("text") or "").strip().lower() for item in grouped}
            if len(grouped) < 2 or len(unique_texts) < 2:
                continue
            ordered = sorted(grouped, key=lambda item: int(item.get("precedence", 0) or 0), reverse=True)
            conflicts.append({"rule_key": key, "winner": ordered[0], "shadowed": ordered[-1], "candidates": ordered[:4]})
        return conflicts[:20]

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
        {"id": "skillify", "name": "/skillify", "kind": "workflow", "description": "Turn a successful product run into a reusable runtime SKILL.md draft.", "requires": ["run"], "workflow": "skillify_successful_run"},
        {"id": "simplify", "name": "/simplify", "kind": "workflow", "description": "After green checks, review changed files for reuse, selector stability, JS simplicity, and state consistency.", "requires": ["run"], "workflow": "post_green_simplify"},
        {"id": "debug-run", "name": "/debug-run", "kind": "workflow", "description": "Read preview/API/check/agent trace evidence for a run and emit a concrete repair packet.", "requires": ["run"], "workflow": "debug_run"},
        {"id": "stuck-run", "name": "/stuck-run", "kind": "workflow", "description": "Diagnose a stalled or repeatedly failing run and produce the next repair packet.", "requires": ["run"], "workflow": "stuck_run"},
        {"id": "doctor-workspace", "name": "/doctor-workspace", "kind": "workflow", "description": "Inspect workspace preview/API/platform logs and latest run evidence, then emit a workspace repair packet.", "requires": ["workspace"], "workflow": "doctor_workspace"},
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
        "/debug": "debug-run",
        "debug": "debug-run",
        "/stuck": "stuck-run",
        "stuck": "stuck-run",
        "/doctor": "doctor-workspace",
        "doctor": "doctor-workspace",
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
            "skillify": {"type": "execute_workflow", "workflow": "skillify_successful_run", "tab": "memory"},
            "simplify": {"type": "execute_workflow", "workflow": "post_green_simplify", "tab": "review"},
            "debug-run": {"type": "execute_workflow", "workflow": "debug_run", "tab": "trace"},
            "stuck-run": {"type": "execute_workflow", "workflow": "stuck_run", "tab": "trace"},
            "doctor-workspace": {"type": "execute_workflow", "workflow": "doctor_workspace", "tab": "doctor"},
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
            "skillify": "Generate a reusable SKILL.md draft from the selected successful run.",
            "simplify": "Review changed files after green checks and create safe simplification tasks.",
            "debug-run": "Diagnose this run from preview/API/check/agent trace evidence and emit a repair packet.",
            "stuck-run": "Find why this run is stuck or repeating and emit the next repair packet.",
            "doctor-workspace": "Inspect workspace runtime, preview/API logs, and latest run evidence; emit a workspace repair packet.",
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


class SubagentForkContract:
    """Hard ownership and tool policy for quality-mode subagents."""

    TOOL_ALLOWLISTS: dict[str, list[str]] = {
        "planner": ["read_files", "search_files", "semantic_scan", "inspect_project"],
        "backend": ["read_files", "search_files", "semantic_scan", "patch_files", "run_command", "run_checks"],
        "frontend-role-ui": ["read_files", "search_files", "semantic_scan", "patch_files", "browser_verify"],
        "tests": ["read_files", "search_files", "patch_files", "run_command", "run_checks"],
        "verifier": ["read_files", "search_files", "semantic_scan", "run_command", "run_checks", "browser_verify"],
        "polish": ["read_files", "search_files", "patch_files", "browser_verify"],
        "repair": ["read_files", "search_files", "semantic_scan", "patch_files", "run_command", "run_checks", "browser_verify"],
    }

    @classmethod
    def tool_allowlist_for_worker(cls, worker_id: str) -> list[str]:
        for lane in cls.build().get("lanes") or []:
            if isinstance(lane, dict) and worker_id in set(str(item) for item in lane.get("worker_ids") or []):
                return [str(item) for item in lane.get("tool_allowlist") or []]
        return []

    @classmethod
    def build(cls, *, implementation_plan: dict[str, Any] | None = None, generation_mode: str | None = None) -> dict[str, Any]:
        implementation_plan = implementation_plan if isinstance(implementation_plan, dict) else {}
        lanes = [
            cls._lane(
                lane_id="planner",
                worker_ids=["planner_worker"],
                branch_role="planner",
                stage="plan_contract",
                ownership="read-only product plan, acceptance contract, task ledger, and worker slicing",
                allowed_paths=["AGENTS.md", "docs", "miniapp/app/generated", "miniapp/tests", "miniapp/app"],
                forbidden_paths=["data", "runtime", ".env"],
                writes=False,
                dependencies=[],
                proof=["implementation_plan_ready", "product_task_ledger_ready"],
            ),
            cls._lane(
                lane_id="backend",
                worker_ids=["backend_api_worker"],
                branch_role="writer",
                stage="backend_contract",
                ownership="backend API, schemas, persistence, and shared role state",
                allowed_paths=["miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/app/generated"],
                forbidden_paths=["miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/tests"],
                writes=True,
                dependencies=["planner"],
                proof=["api_workflow_smoke", "lsp_static_diagnostics"],
            ),
            cls._lane(
                lane_id="frontend-role-ui",
                worker_ids=["client_surface_worker", "specialist_surface_worker", "manager_surface_worker"],
                branch_role="writer",
                stage="role_ui_and_tests",
                ownership="role-specific UI surfaces; each worker owns only its static/<role> tree",
                allowed_paths=["miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/app/static/shared"],
                forbidden_paths=["miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/tests"],
                writes=True,
                dependencies=["planner", "backend"],
                proof=["browser_flow_smoke:<role>", "mobile_layout:<role>"],
                child_lanes=[
                    {"worker_id": "client_surface_worker", "owned_paths": ["miniapp/app/static/client"], "role": "client"},
                    {"worker_id": "specialist_surface_worker", "owned_paths": ["miniapp/app/static/specialist"], "role": "specialist"},
                    {"worker_id": "manager_surface_worker", "owned_paths": ["miniapp/app/static/manager"], "role": "manager"},
                ],
            ),
            cls._lane(
                lane_id="tests",
                worker_ids=["test_verifier_worker"],
                branch_role="writer",
                stage="test_materialization",
                ownership="generated acceptance tests and smoke checks",
                allowed_paths=["miniapp/tests"],
                forbidden_paths=["miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/app/routes"],
                writes=True,
                dependencies=["planner", "backend", "frontend-role-ui"],
                proof=["generated_acceptance_tests", "api_workflow_smoke", "browser_flow_smoke"],
            ),
            cls._lane(
                lane_id="verifier",
                worker_ids=["verifier_worker", "mobile_polish_worker"],
                branch_role="verifier",
                stage="guardian_verifier",
                ownership="read-only browser proof, mobile layout proof, and blocker findings",
                allowed_paths=["miniapp/app", "miniapp/tests", "reports", "browser_proof"],
                forbidden_paths=["runtime", "data", ".env"],
                writes=False,
                dependencies=["backend", "frontend-role-ui", "tests"],
                proof=["all_checks_green", "browser_proof", "mobile_layout"],
            ),
            cls._lane(
                lane_id="polish",
                worker_ids=["polish_worker"],
                branch_role="writer",
                stage="visual_polish_after_green",
                ownership="targeted visual polish inside role UI/static shared assets only",
                allowed_paths=["miniapp/app/static/client", "miniapp/app/static/specialist", "miniapp/app/static/manager", "miniapp/app/static/shared"],
                forbidden_paths=["miniapp/app/routes", "miniapp/app/schemas.py", "miniapp/app/db.py", "miniapp/tests"],
                writes=True,
                dependencies=["verifier"],
                proof=["mobile_layout", "browser_flow_smoke"],
            ),
            cls._lane(
                lane_id="repair",
                worker_ids=["repair_worker"],
                branch_role="repair",
                stage="targeted_repair",
                ownership="only the owner scope implicated by failure signature or repair packet",
                allowed_paths=["miniapp/app", "miniapp/tests"],
                forbidden_paths=["runtime", "data", ".env", ".github"],
                writes=True,
                dependencies=["verifier"],
                proof=["latest_failed_check_passes", "repair_packet_resolved"],
            ),
        ]
        return {
            "schema": "grounded.subagent_fork_contract.v1",
            "status": "ready",
            "generation_mode": str(generation_mode or ""),
            "principle": "hard ownership scopes, scoped tool allowlists, isolated forks, raw proof before merge",
            "lanes": lanes,
            "execution_order": ["planner", "backend", "frontend-role-ui", "tests", "verifier", "polish", "repair"],
            "quality_mode_policy": {
                "parallelizable_lanes": [["frontend-role-ui", "tests"], ["polish"]],
                "serial_gates": ["planner", "backend", "verifier"],
                "merge_gate": "accept only owned diffs with required proof; forbidden or overlapping paths become repair packets",
                "same_owner_repair": "continue the worker that owns the failing paths; do not let a broad coordinator rewrite unrelated surfaces",
            },
            "ownership_matrix": cls.ownership_matrix(lanes),
            "conflict_policy": cls.conflict_policy(lanes),
            "plan_bindings": cls._plan_bindings(implementation_plan),
        }

    @classmethod
    def _lane(
        cls,
        *,
        lane_id: str,
        worker_ids: list[str],
        branch_role: str,
        stage: str,
        ownership: str,
        allowed_paths: list[str],
        forbidden_paths: list[str],
        writes: bool,
        dependencies: list[str],
        proof: list[str],
        child_lanes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "lane_id": lane_id,
            "worker_ids": worker_ids,
            "branch_role": branch_role,
            "stage": stage,
            "ownership": ownership,
            "tool_allowlist": list(cls.TOOL_ALLOWLISTS.get(lane_id, [])),
            "file_scope": {
                "allowed_paths": allowed_paths,
                "forbidden_paths": forbidden_paths,
                "exclusive_write": writes,
            },
            "writes": writes,
            "dependencies": dependencies,
            "required_proof": proof,
            "child_lanes": child_lanes or [],
        }

    @staticmethod
    def ownership_matrix(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        matrix: list[dict[str, Any]] = []
        for lane in lanes:
            scope = lane.get("file_scope") if isinstance(lane.get("file_scope"), dict) else {}
            for path in scope.get("allowed_paths") or []:
                matrix.append(
                    {
                        "lane_id": lane.get("lane_id"),
                        "worker_ids": lane.get("worker_ids") or [],
                        "path_prefix": path,
                        "exclusive_write": bool(scope.get("exclusive_write")),
                        "tool_allowlist": lane.get("tool_allowlist") or [],
                    }
                )
        return matrix

    @staticmethod
    def conflict_policy(lanes: list[dict[str, Any]]) -> dict[str, Any]:
        writers = [lane for lane in lanes if bool(lane.get("writes"))]
        overlaps: list[dict[str, Any]] = []
        for index, left in enumerate(writers):
            left_paths = [str(item).rstrip("/") for item in ((left.get("file_scope") or {}).get("allowed_paths") or [])]
            for right in writers[index + 1 :]:
                right_paths = [str(item).rstrip("/") for item in ((right.get("file_scope") or {}).get("allowed_paths") or [])]
                shared = [
                    {"left": a, "right": b}
                    for a in left_paths
                    for b in right_paths
                    if a == b or a.startswith(b + "/") or b.startswith(a + "/")
                ]
                if shared:
                    overlaps.append({"left_lane": left.get("lane_id"), "right_lane": right.get("lane_id"), "overlaps": shared})
        return {
            "status": "passed" if not overlaps else "needs_serial_gate",
            "overlaps": overlaps,
            "rule": "overlapping writer lanes cannot run in parallel unless a serial gate narrows the repair packet or path slice",
        }

    @staticmethod
    def _plan_bindings(implementation_plan: dict[str, Any]) -> dict[str, Any]:
        ledger = implementation_plan.get("product_task_ledger") if isinstance(implementation_plan.get("product_task_ledger"), list) else []
        bindings: dict[str, list[str]] = {
            "backend": [],
            "frontend-role-ui": [],
            "tests": [],
            "verifier": [],
        }
        for item in ledger:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            kind = str(item.get("kind") or "").lower()
            role = str(item.get("role") or "").lower()
            if kind == "backend":
                bindings["backend"].append(item_id)
            if role in {"client", "specialist", "manager"}:
                bindings["frontend-role-ui"].append(item_id)
            if kind == "proof":
                bindings["tests"].append(item_id)
                bindings["verifier"].append(item_id)
        return {key: value[:12] for key, value in bindings.items() if value}


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


class VisualRegressionGenerator:
    @classmethod
    def build(cls, *, run: Any, artifacts: dict[str, Any], browser_proof: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
        proof = browser_proof if isinstance(browser_proof, dict) else {}
        baseline_payload = baseline if isinstance(baseline, dict) else {}
        browser_check = cls._browser_check(artifacts)
        diagnostics = browser_check.get("diagnostics") if isinstance(browser_check.get("diagnostics"), dict) else {}
        steps = cls._first_list(
            proof.get("steps"),
            (proof.get("playwright_scenario") or {}).get("steps") if isinstance(proof.get("playwright_scenario"), dict) else None,
            diagnostics.get("steps"),
            diagnostics.get("ui_steps"),
        )
        mobile_layout = cls._first_dict(proof.get("mobile_layout"), diagnostics.get("mobile_layout"))
        dom_snapshots = [dict(item) for item in cls._first_list(proof.get("dom_snapshots"), diagnostics.get("dom_snapshots")) if isinstance(item, dict)][:80]
        layout_reports = cls._first_list(proof.get("layout_reports"), diagnostics.get("layout_reports"))
        visual_diffs = cls._visual_diffs(proof=proof, diagnostics=diagnostics, steps=steps)
        mobile_screenshots = cls._mobile_viewport_screenshots(proof=proof, diagnostics=diagnostics, steps=steps)
        role_snapshots = cls._role_page_snapshots(steps=steps, screenshots=mobile_screenshots)
        overflow_overlap = cls._overflow_overlap(mobile_layout=mobile_layout, layout_reports=layout_reports)
        runtime_errors = cls._runtime_errors(proof=proof, diagnostics=diagnostics)
        baseline_comparison = cls._baseline_comparison(current_role_snapshots=role_snapshots, baseline=baseline_payload)
        changed_files = cls._changed_files(run=run, artifacts=artifacts)
        issues = cls._issues(
            mobile_screenshots=mobile_screenshots,
            role_snapshots=role_snapshots,
            dom_snapshots=dom_snapshots,
            visual_diffs=visual_diffs,
            overflow_overlap=overflow_overlap,
            runtime_errors=runtime_errors,
            baseline_comparison=baseline_comparison,
            run=run,
        )
        blocking = any(str(item.get("severity") or "") == "high" for item in issues)
        status = "failed" if blocking else "incomplete" if issues else "passed"
        return {
            "schema": "grounded.visual_regression.v1",
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "blocking": blocking,
            "mobile_viewports": cls._mobile_viewports(proof=proof, diagnostics=diagnostics, mobile_layout=mobile_layout, steps=steps),
            "mobile_viewport_screenshots": mobile_screenshots,
            "role_page_snapshots": role_snapshots,
            "dom_state_snapshots": dom_snapshots,
            "overflow_overlap": overflow_overlap,
            "runtime_errors": runtime_errors,
            "baseline": baseline_comparison,
            "visual_diffs": visual_diffs,
            "changed_files": changed_files,
            "issues": issues,
            "artifact_refs": {
                "run_artifacts": f"run_artifacts:{run.run_id}",
                "browser_proof": run.browser_proof_ref,
                "visual_regression": f"visual_regression:{run.run_id}",
                "baseline_visual_regression": baseline_comparison.get("baseline_ref"),
            },
            "created_at": _now(),
        }

    @staticmethod
    def blocking_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for item in report.get("issues") or []:
            if not isinstance(item, dict) or item.get("severity") != "high":
                continue
            issues.append(
                {
                    "kind": "visual_regression",
                    "check": str(item.get("kind") or "visual_regression"),
                    "details": str(item.get("message") or "Generated app visual regression failed."),
                    "blocking": True,
                    "evidence": item,
                }
            )
        return issues

    @staticmethod
    def _browser_check(artifacts: dict[str, Any]) -> dict[str, Any]:
        for item in artifacts.get("check_results") or []:
            if isinstance(item, dict) and item.get("name") == "browser_flow_smoke":
                return item
        return {}

    @staticmethod
    def _first_dict(*values: Any) -> dict[str, Any]:
        for value in values:
            if isinstance(value, dict) and value:
                return dict(value)
        return {}

    @staticmethod
    def _first_list(*values: Any) -> list[Any]:
        for value in values:
            if isinstance(value, list) and value:
                return list(value)
        return []

    @classmethod
    def _mobile_viewports(cls, *, proof: dict[str, Any], diagnostics: dict[str, Any], mobile_layout: dict[str, Any], steps: list[Any]) -> list[dict[str, Any]]:
        raw: list[Any] = []
        raw.extend(cls._as_list(proof.get("viewports") or mobile_layout.get("viewports")))
        for value in (proof.get("mobile_viewport"), diagnostics.get("mobile_viewport"), mobile_layout.get("viewport")):
            if value:
                raw.append(value)
        for step in steps:
            if isinstance(step, dict) and step.get("mobile_viewport"):
                raw.append(step.get("mobile_viewport"))
        viewports: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in raw or [{"width": 390, "height": 844}]:
            viewport = cls._viewport_dict(value)
            key = f"{viewport.get('width')}x{viewport.get('height')}"
            if key in seen:
                continue
            seen.add(key)
            viewports.append(viewport)
        return viewports[:8]

    @classmethod
    def _mobile_viewport_screenshots(cls, *, proof: dict[str, Any], diagnostics: dict[str, Any], steps: list[Any]) -> list[dict[str, Any]]:
        screenshots: list[dict[str, Any]] = []

        def add(path: Any, *, route: Any = "", role: Any = "", phase: str = "after", viewport: Any = None, action: Any = "") -> None:
            text = str(path or "").strip()
            if not text:
                return
            screenshots.append(
                {
                    "path": text,
                    "route": str(route or ""),
                    "role": str(role or ""),
                    "phase": phase,
                    "action": str(action or ""),
                    "mobile_viewport": cls._viewport_dict(viewport or proof.get("mobile_viewport") or diagnostics.get("mobile_viewport") or {"width": 390, "height": 844}),
                }
            )

        for step in steps:
            if not isinstance(step, dict):
                continue
            add(step.get("screenshot_before"), route=step.get("route"), role=step.get("role"), phase="before", viewport=step.get("mobile_viewport"), action=step.get("action"))
            add(step.get("screenshot_after"), route=step.get("route"), role=step.get("role"), phase="after", viewport=step.get("mobile_viewport"), action=step.get("action"))
            add(step.get("screenshot"), route=step.get("route"), role=step.get("role"), phase="snapshot", viewport=step.get("mobile_viewport"), action=step.get("action"))
        for path in cls._as_list(proof.get("screenshots") or diagnostics.get("screenshots")):
            add(path, phase="snapshot")
        raw_role_screenshots = cls._first_role_screenshots(proof=proof, diagnostics=diagnostics)
        if isinstance(raw_role_screenshots, dict):
            raw_role_screenshots = [{"role": role, "path": path, "route": f"/{role}"} for role, path in raw_role_screenshots.items()]
        for item in raw_role_screenshots:
            if isinstance(item, dict):
                add(
                    item.get("path") or item.get("screenshot") or item.get("image_path"),
                    route=item.get("route"),
                    role=item.get("role"),
                    phase=str(item.get("phase") or "snapshot"),
                    viewport=item.get("mobile_viewport"),
                )
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in screenshots:
            key = f"{item.get('path')}:{item.get('phase')}:{item.get('route')}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:80]

    @classmethod
    def _role_page_snapshots(cls, *, steps: list[Any], screenshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for item in screenshots:
            role = str(item.get("role") or cls._role_from_route(str(item.get("route") or "")) or "")
            route = str(item.get("route") or (f"/{role}" if role else ""))
            if not role and not route:
                continue
            key = f"{role}:{route}:{item.get('phase')}"
            by_key.setdefault(
                key,
                {
                    "role": role,
                    "route": route,
                    "phase": item.get("phase"),
                    "screenshot": item.get("path"),
                    "mobile_viewport": item.get("mobile_viewport"),
                },
            )
        for step in steps:
            if not isinstance(step, dict):
                continue
            route = str(step.get("route") or "")
            role = str(step.get("role") or cls._role_from_route(route) or "")
            screenshot = step.get("screenshot_after") or step.get("screenshot")
            if role and screenshot:
                key = f"{role}:{route}:after"
                by_key.setdefault(key, {"role": role, "route": route, "phase": "after", "screenshot": screenshot, "mobile_viewport": cls._viewport_dict(step.get("mobile_viewport"))})
        return list(by_key.values())[:80]

    @classmethod
    def _visual_diffs(cls, *, proof: dict[str, Any], diagnostics: dict[str, Any], steps: list[Any]) -> list[dict[str, Any]]:
        explicit = cls._first_list(proof.get("visual_diffs"), diagnostics.get("visual_diffs"))
        if explicit:
            return [item for item in explicit if isinstance(item, dict)][:80]
        diffs: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            before = step.get("screenshot_before")
            after = step.get("screenshot_after")
            if before or after:
                diffs.append(
                    {
                        "action": step.get("action") or step.get("step"),
                        "route": step.get("route"),
                        "role": step.get("role"),
                        "screenshot_before": before,
                        "screenshot_after": after,
                        "diff_kind": "before_after_refs",
                        "changed": bool(before and after and before != after),
                        "mobile_viewport": cls._viewport_dict(step.get("mobile_viewport")),
                    }
                )
        return diffs[:80]

    @staticmethod
    def _runtime_errors(*, proof: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, Any]:
        console = [str(item) for item in VisualRegressionGenerator._as_list(proof.get("console_errors") or diagnostics.get("console_errors")) if str(item).strip()]
        page = [str(item) for item in VisualRegressionGenerator._as_list(proof.get("page_errors") or diagnostics.get("page_errors")) if str(item).strip()]
        network = [str(item) for item in VisualRegressionGenerator._as_list(proof.get("network_errors") or diagnostics.get("network_errors")) if str(item).strip()]
        visible = [str(item) for item in VisualRegressionGenerator._as_list(proof.get("visible_errors") or diagnostics.get("visible_errors")) if str(item).strip()]
        return {
            "status": "failed" if console or page or network or visible else "passed",
            "console_errors": console[:20],
            "page_errors": page[:20],
            "network_errors": network[:20],
            "visible_errors": visible[:20],
        }

    @classmethod
    def _first_role_screenshots(cls, *, proof: dict[str, Any], diagnostics: dict[str, Any]) -> list[Any]:
        for value in (proof.get("role_page_screenshots"), proof.get("role_screenshots"), diagnostics.get("role_page_screenshots"), diagnostics.get("role_screenshots")):
            if isinstance(value, dict) and value:
                return [{"role": role, "path": path, "route": f"/{role}"} for role, path in value.items()]
            if isinstance(value, list) and value:
                return list(value)
        return []

    @classmethod
    def _baseline_comparison(cls, *, current_role_snapshots: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
        baseline_snapshots = [dict(item) for item in baseline.get("role_page_snapshots") or [] if isinstance(item, dict)]
        baseline_ref = (baseline.get("artifact_refs") or {}).get("visual_regression") if isinstance(baseline.get("artifact_refs"), dict) else None
        current_keys = {cls._snapshot_key(item) for item in current_role_snapshots if cls._snapshot_key(item)}
        baseline_keys = {cls._snapshot_key(item) for item in baseline_snapshots if cls._snapshot_key(item)}
        missing = sorted(key for key in baseline_keys if key not in current_keys)
        new = sorted(key for key in current_keys if key not in baseline_keys)
        return {
            "status": "not_recorded" if not baseline else "regressed" if missing else "matched",
            "baseline_ref": baseline_ref,
            "baseline_run_id": baseline.get("run_id"),
            "current_snapshot_count": len(current_role_snapshots),
            "baseline_snapshot_count": len(baseline_snapshots),
            "missing_role_snapshots": missing,
            "new_role_snapshots": new,
        }

    @staticmethod
    def _snapshot_key(item: dict[str, Any]) -> str:
        role = str(item.get("role") or "").strip()
        route = str(item.get("route") or "").strip()
        return f"{role}:{route}" if role or route else ""

    @classmethod
    def _overflow_overlap(cls, *, mobile_layout: dict[str, Any], layout_reports: list[Any]) -> dict[str, Any]:
        normalized_reports = [dict(item) for item in layout_reports if isinstance(item, dict)]
        overflow_items = [item for item in normalized_reports if item.get("overflow") or item.get("horizontal_overflow")]
        overlap_items = [item for item in normalized_reports if item.get("overlaps") or item.get("critical_overlap")]
        horizontal = bool(mobile_layout.get("horizontal_overflow") or overflow_items)
        overlap = bool(mobile_layout.get("critical_overlap") or mobile_layout.get("overlap") or overlap_items)
        status = "failed" if str(mobile_layout.get("status") or "").lower() == "failed" or horizontal or overlap else "passed" if mobile_layout or normalized_reports else "not_recorded"
        return {
            "status": status,
            "horizontal_overflow": horizontal,
            "critical_overlap": overlap,
            "mobile_layout": mobile_layout,
            "layout_reports": normalized_reports[:40],
            "overflow_items": overflow_items[:20],
            "overlap_items": overlap_items[:20],
        }

    @classmethod
    def _issues(
        cls,
        *,
        mobile_screenshots: list[dict[str, Any]],
        role_snapshots: list[dict[str, Any]],
        dom_snapshots: list[Any],
        visual_diffs: list[dict[str, Any]],
        overflow_overlap: dict[str, Any],
        runtime_errors: dict[str, Any],
        baseline_comparison: dict[str, Any],
        run: Any,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if overflow_overlap.get("status") == "failed":
            issues.append({"kind": "overflow_overlap", "severity": "high", "message": "Mobile viewport proof found horizontal overflow or overlapping critical UI.", "evidence": overflow_overlap})
        if overflow_overlap.get("horizontal_overflow"):
            issues.append({"kind": "horizontal_overflow", "severity": "high", "message": "Mobile viewport has horizontal overflow.", "evidence": overflow_overlap})
        if runtime_errors.get("status") == "failed":
            issues.append({"kind": "js_error", "severity": "high", "message": "Browser proof recorded JavaScript, network, or visible runtime errors.", "evidence": runtime_errors})
        empty_dom = [
            item
            for item in dom_snapshots
            if isinstance(item, dict)
            and (
                item.get("empty") is True
                or item.get("is_empty") is True
                or str(item.get("status") or "").lower() in {"empty", "blank"}
                or (item.get("text_length") is not None and int(item.get("text_length") or 0) <= 0)
            )
        ]
        if empty_dom:
            issues.append({"kind": "empty_screen", "severity": "high", "message": "Browser proof found an empty or blank screen.", "evidence": {"dom_snapshots": empty_dom[:8]}})
        if baseline_comparison.get("status") == "regressed":
            issues.append({"kind": "layout_regression", "severity": "high", "message": "Current browser proof is missing role screenshots that existed in the previous successful run.", "evidence": baseline_comparison})
        if not mobile_screenshots:
            issues.append({"kind": "missing_mobile_viewport_screenshots", "severity": "medium", "message": "No mobile viewport screenshots were recorded for generated app visual proof."})
        if not role_snapshots:
            issues.append({"kind": "missing_role_page_snapshots", "severity": "medium", "message": "No role page screenshots were linked to client/specialist/manager routes."})
        if not dom_snapshots:
            issues.append({"kind": "missing_dom_state_snapshots", "severity": "medium", "message": "No DOM state snapshots were recorded after workflow actions."})
        mode = str(getattr(run, "mode", "") or getattr(run, "intent", "") or "").lower()
        if mode in {"edit", "fix"} and not visual_diffs:
            issues.append({"kind": "missing_before_after_visual_diff", "severity": "medium", "message": "Edit/fix run has no before/after visual diff screenshots."})
        return issues

    @staticmethod
    def _changed_files(*, run: Any, artifacts: dict[str, Any]) -> list[str]:
        paths = list(getattr(run, "touched_files", []) or [])
        for line in str(artifacts.get("diff") or "").splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    paths.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
        return list(dict.fromkeys(str(path) for path in paths if str(path).strip()))[:80]

    @staticmethod
    def _viewport_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            width = value.get("width")
            height = value.get("height")
            return {"width": int(width or 390), "height": int(height or 844)}
        text = str(value or "").lower()
        match = re.search(r"(\d{3,4})\s*x\s*(\d{3,4})", text)
        if match:
            return {"width": int(match.group(1)), "height": int(match.group(2))}
        return {"width": 390, "height": 844}

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    @staticmethod
    def _role_from_route(route: str) -> str:
        text = str(route or "").strip().lower()
        for role in ROLE_ORDER:
            if text == role or text.startswith(f"/{role}"):
                return role
        return ""


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
