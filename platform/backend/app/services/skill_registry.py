from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import re
from typing import Any

from app.models.skills import (
    SkillDefinition,
    SkillDependency,
    SkillFrontmatter,
    SkillInvocationPolicy,
    SkillRegistryManifest,
    SkillScope,
    SkillSelection,
    SkillValidationIssue,
)


ROLE_ORDER = ("client", "specialist", "manager")
SCOPE_PRECEDENCE = {"system": 0, "repo": 1, "plugin": 2, "user": 3}
EFFORT_ORDER = {"": 0, "none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


class SkillRegistryService:
    """Scoped, typed loader for repo/system/plugin/user skill packs."""

    _cache: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        *,
        runtime_dir: Path,
        repo_root: Path,
        data_dir: Path | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.repo_root = repo_root
        self.data_dir = data_dir or repo_root / "data"

    @staticmethod
    def system_builtin() -> dict[str, dict[str, Any]]:
        items = [
            {
                "id": "state-workflow",
                "name": "State workflow",
                "activation": "create_or_behavior_edit",
                "invocationPolicy": "auto",
                "constraints": ["Persist only the shared records and state transitions implied by the prompt."],
                "validation_hints": ["Prompt-derived persisted workflow exists."],
            },
            {
                "id": "role-surfaces",
                "name": "Role surfaces",
                "activation": "create_or_role_edit",
                "invocationPolicy": "auto",
                "constraints": ["Role pages share connected state while exposing distinct actions."],
                "validation_hints": ["Each role has a distinct action surface."],
            },
            {
                "id": "route-manifest",
                "name": "Route manifest",
                "activation": "route_change",
                "invocationPolicy": "auto",
                "constraints": ["Route metadata must match real static pages."],
                "validation_hints": ["Every role page is routeable."],
            },
            {
                "id": "mobile-shell",
                "name": "Mobile shell",
                "activation": "visual_or_quality",
                "invocationPolicy": "auto",
                "constraints": ["Mobile-first role pages for Telegram widths."],
                "validation_hints": ["No horizontal overflow on mobile."],
            },
            {
                "id": "preview-profile",
                "name": "Preview profile",
                "activation": "preview_or_platform",
                "invocationPolicy": "auto",
                "constraints": ["Preview profile selects the right host shell."],
                "validation_hints": ["Preview profile supports configured mock surface."],
            },
        ]
        return {str(item["id"]): item for item in items}

    def prefetch(self, *, force: bool = False) -> dict[str, Any]:
        signature = self._signature()
        cache_key = f"{self.runtime_dir.resolve()}::{self.data_dir.resolve()}"
        cached = self._cache.get(cache_key)
        if cached and not force and cached.get("signature") == signature:
            return {**cached, "cache": {"status": "hit", "signature": signature}, "created_at": _now()}
        items, issues = self._load_all()
        resolved = self._resolved_items(items)
        scopes: dict[str, int] = {}
        for item in items:
            scopes[item.scope] = scopes.get(item.scope, 0) + 1
        payload = {
            "schema": "grounded.skill_prefetch.v2",
            "status": "ready",
            "signature": signature,
            "items": [item.model_dump(mode="json") for item in resolved],
            "all_items": [item.model_dump(mode="json") for item in items],
            "manifest": self._manifest(signature=signature, scopes=scopes, issues=issues, cache={"status": "loaded", "signature": signature}).model_dump(mode="json", by_alias=True),
            "scopes": scopes,
            "validation_issues": [issue.model_dump(mode="json") for issue in issues],
            "cache": {"status": "loaded", "signature": signature},
            "created_at": _now(),
        }
        self._cache[cache_key] = payload
        return payload

    def manifest(self) -> dict[str, Any]:
        payload = self.prefetch()
        return dict(payload.get("manifest") or {})

    def search_for_context(
        self,
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
        prefetch = self.prefetch()
        return self.search_items_for_context(
            list(prefetch.get("items") or []),
            prompt=prompt,
            intent=intent,
            generation_mode=generation_mode,
            paths=paths,
            failure_class=failure_class,
            max_skills=max_skills,
            max_body_chars=max_body_chars,
            max_total_body_chars=max_total_body_chars,
            validation_issues=list(prefetch.get("validation_issues") or []),
            prefetch={k: v for k, v in prefetch.items() if k not in {"items", "all_items"}},
        )

    @classmethod
    def search_items_for_context(
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
        validation_issues: list[dict[str, Any]] | None = None,
        prefetch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        budget = cls.activation_budget(
            generation_mode=generation_mode,
            max_skills=max_skills,
            max_body_chars=max_body_chars,
            max_total_body_chars=max_total_body_chars,
        )
        explicit_mentions = cls.explicit_mentions(prompt, skills)
        haystack = " ".join([str(prompt or ""), str(intent or ""), str(generation_mode or ""), str(failure_class or ""), " ".join(paths or [])]).lower()
        by_lookup = cls._lookup_map(skills)
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for skill in skills:
            if not bool(skill.get("enabled", True)):
                skipped.append({"id": skill.get("id"), "scoped_id": skill.get("scoped_id"), "reason": "disabled"})
                continue
            policy = str(skill.get("invocationPolicy") or "explicit")
            score = 0
            reasons: list[str] = []
            skill_id = str(skill.get("id") or "")
            scoped_id = str(skill.get("scoped_id") or skill_id)
            explicit = skill_id in explicit_mentions or scoped_id in explicit_mentions
            if explicit:
                score += 100
                reasons.append("explicit_mention")
            if policy == "always":
                score += 20
                reasons.append("always")
            if policy == "disabled":
                skipped.append({"id": skill_id, "scoped_id": scoped_id, "reason": "disabled"})
                continue
            if policy == "explicit" and not explicit:
                skipped.append({"id": skill_id, "scoped_id": scoped_id, "reason": "explicit_only", "source": skill.get("source")})
                continue
            if policy in {"auto", "always"} or explicit:
                phrase_hits = [
                    phrase
                    for phrase in [str(item).lower() for item in cls.list_field(skill.get("whenToUse") or skill.get("activation"))]
                    if cls.phrase_matches(phrase, haystack)
                ]
                if phrase_hits:
                    score += min(9, len(phrase_hits) * 3)
                    reasons.append("whenToUse")
                for path_pattern in skill.get("paths") or []:
                    if any(cls.path_matches(str(path_pattern), str(path)) for path in paths or []):
                        score += 3
                        reasons.append("paths")
                        break
                if failure_class and cls.failure_matches(skill, failure_class):
                    score += 4
                    reasons.append("failure_class")
                if any(validation and validation.lower() in haystack for validation in [str(item) for item in skill.get("validation") or skill.get("validation_hints") or []]):
                    score += 2
                    reasons.append("validation")
                if skill_id in {"telegram-miniapp-product"} and ("telegram" in haystack or str(intent or "") == "create"):
                    score += 2
                    reasons.append("platform")
                if skill_id in {"fastapi-persistence"} and any(token in haystack for token in ("api", "persist", "state", "sqlite", "backend")):
                    score += 2
                    reasons.append("persistence")
                if skill_id in {"repair-failed-generation"} and failure_class:
                    score += 3
                    reasons.append("failure")
                if skill_id in {"mobile-ui-polish"} and (str(generation_mode or "").lower() == "quality" or "layout" in haystack or "overflow" in haystack):
                    score += 2
                    reasons.append("quality")
                if skill_id in {"empty-error-loading-states"} and str(generation_mode or "").lower() == "quality":
                    score += 4
                    reasons.append("quality_states")
                if skill_id in {"browser-acceptance-proof"} and (str(intent or "").lower() == "create" or "browser" in haystack or "final gate" in haystack):
                    score += 2
                    reasons.append("proof")
            if score:
                candidates.append({**skill, "activation_reason": ", ".join(dict.fromkeys(reasons)), "activation_score": score, "explicit": explicit})
            else:
                skipped.append({"id": skill_id, "scoped_id": scoped_id, "reason": "no intent/path/failure match", "source": skill.get("source")})
        selected = cls._select_with_budget(candidates, skipped, budget)
        selected, dependency_issues = cls._with_dependencies(selected, by_lookup, budget)
        selected = cls._annotate_effective_policy(selected)
        used_body_chars = sum(min(len(str(item.get("body") or "")), int(item.get("body_budget_chars") or budget["max_body_chars"])) for item in selected)
        return {
            "schema": "grounded.skill_search.v2",
            "status": "ready",
            "selected": selected,
            "skipped": skipped[:80],
            "explicit_mentions": sorted(explicit_mentions),
            "budget": {**budget, "used_body_chars": used_body_chars},
            "effective": cls.effective_policy(selected),
            "validation_issues": [*(validation_issues or []), *dependency_issues],
            "prefetch": prefetch or {},
            "created_at": _now(),
        }

    @staticmethod
    def compact_context(skills: list[dict[str, Any]], *, body_limit: int = 800) -> str:
        if not skills:
            return ""
        lines = ["Active runtime skills:"]
        for skill in skills:
            label = skill.get("scoped_id") or skill.get("id")
            lines.append(f"- {label}: {skill.get('activation_reason') or skill.get('activation')}")
            constraints = "; ".join(str(item) for item in (skill.get("constraints") or [])[:4])
            validation = "; ".join(str(item) for item in (skill.get("validation_hints") or skill.get("validation") or [])[:4])
            allowed_tools = ", ".join(str(item) for item in (skill.get("allowedTools") or [])[:8])
            if constraints:
                lines.append(f"  Rules: {constraints}")
            if validation:
                lines.append(f"  Validation: {validation}")
            if allowed_tools:
                lines.append(f"  Tool hint: {allowed_tools}")
            body = str(skill.get("body") or "").strip()
            if body:
                limit = min(int(skill.get("body_budget_chars") or body_limit), body_limit)
                lines.append(f"  Body excerpt: {body[:limit]}")
        return "\n".join(lines)

    @classmethod
    def usage_telemetry(cls, *, selected: list[dict[str, Any]], check_results: list[dict[str, Any]], run_status: str) -> dict[str, Any]:
        by_name = {str(item.get("name") or ""): str(item.get("status") or "") for item in check_results if isinstance(item, dict)}
        items: list[dict[str, Any]] = []
        for skill in selected:
            validation = [str(item) for item in skill.get("validation") or skill.get("validation_hints") or []]
            passed = [name for name in validation if by_name.get(name) == "passed"]
            failed = [name for name in validation if by_name.get(name) == "failed"]
            outcome = "helped" if passed and not failed else "not_helped" if failed else "neutral_completed" if run_status == "completed" else "unknown"
            items.append(
                {
                    "skill_id": skill.get("id"),
                    "scoped_id": skill.get("scoped_id"),
                    "scope": skill.get("scope"),
                    "source": skill.get("source"),
                    "dependencies": skill.get("dependencies") or [],
                    "invocationPolicy": skill.get("invocationPolicy"),
                    "allowedTools": skill.get("allowedTools") or [],
                    "model": skill.get("model") or "",
                    "effort": skill.get("effort") or "",
                    "activation_reason": skill.get("activation_reason"),
                    "activation_score": skill.get("activation_score"),
                    "validation": validation,
                    "passed": passed,
                    "failed": failed,
                    "outcome": outcome,
                }
            )
        return {"schema": "grounded.skill_usage_telemetry.v2", "status": run_status, "items": items, "created_at": _now()}

    @classmethod
    def effective_policy(cls, selected: list[dict[str, Any]]) -> dict[str, Any]:
        allowed_tools = sorted({str(tool) for skill in selected for tool in (skill.get("allowedTools") or []) if str(tool).strip()})
        effort = ""
        for skill in selected:
            value = str(skill.get("effort") or "").lower()
            if EFFORT_ORDER.get(value, 0) > EFFORT_ORDER.get(effort, 0):
                effort = value
        explicit_models = [str(item.get("model") or "") for item in selected if item.get("explicit") and str(item.get("model") or "").strip()]
        auto_models = [str(item.get("model") or "") for item in selected if not item.get("explicit") and str(item.get("model") or "").strip()]
        return {
            "allowedTools": allowed_tools,
            "model": (explicit_models or auto_models or [""])[0],
            "effort": effort,
        }

    @classmethod
    def explicit_mentions(cls, prompt: str, skills: list[dict[str, Any]]) -> set[str]:
        text = str(prompt or "")
        mentions = set(re.findall(r"[@$]([A-Za-z0-9_:-]+)", text))
        normalized = {item.lower() for item in mentions}
        result: set[str] = set()
        for skill in skills:
            skill_id = str(skill.get("id") or "")
            scoped_id = str(skill.get("scoped_id") or "")
            names = {skill_id.lower(), scoped_id.lower(), str(skill.get("name") or "").lower().replace(" ", "-")}
            result.update(names & normalized)
            if normalized & names:
                result.add(skill_id)
                if scoped_id:
                    result.add(scoped_id)
        return result

    @staticmethod
    def activation_budget(*, generation_mode: str | None, max_skills: int | None, max_body_chars: int | None, max_total_body_chars: int | None) -> dict[str, int]:
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
    def list_field(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
        return []

    @staticmethod
    def phrase_matches(phrase: str, haystack: str) -> bool:
        phrase = str(phrase or "").strip().lower()
        if not phrase:
            return False
        if phrase in haystack:
            return True
        tokens = [token for token in re.split(r"[^a-z0-9_-]+", phrase) if len(token) >= 3]
        return bool(tokens and all(token in haystack for token in tokens[:4]))

    @staticmethod
    def path_matches(pattern: str, path: str) -> bool:
        pattern = str(pattern or "").strip().replace("\\", "/")
        path = str(path or "").strip().replace("\\", "/")
        if not pattern or not path:
            return False
        return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*").rstrip("/"))

    @staticmethod
    def failure_matches(skill: dict[str, Any], failure_class: str) -> bool:
        failure = str(failure_class or "").lower()
        if not failure:
            return False
        return any(str(item).lower() in failure or failure in str(item).lower() for item in [*(skill.get("whenToUse") or []), *(skill.get("validation") or [])])

    def _load_all(self) -> tuple[list[SkillDefinition], list[SkillValidationIssue]]:
        items: list[SkillDefinition] = []
        issues: list[SkillValidationIssue] = []
        for item in self.system_builtin().values():
            items.append(self._system_skill(item))
        repo_root = self.runtime_dir / "skills"
        repo_items, repo_issues = self._load_skill_root(repo_root, scope="repo")
        items.extend(repo_items)
        issues.extend(repo_issues)
        for root in [self.runtime_dir / "plugins", self.data_dir / "plugins"]:
            plugin_items, plugin_issues = self._load_plugin_skills(root)
            items.extend(plugin_items)
            issues.extend(plugin_issues)
        user_items, user_issues = self._load_skill_root(self.data_dir / "skills", scope="user")
        items.extend(user_items)
        issues.extend(user_issues)
        dependency_issues = self._dependency_validation_issues(items)
        issues.extend(dependency_issues)
        return items, issues

    def _system_skill(self, raw: dict[str, Any]) -> SkillDefinition:
        skill_id = str(raw.get("id") or "")
        return SkillDefinition(
            id=skill_id,
            scoped_id=f"system:{skill_id}",
            scope="system",
            name=str(raw.get("name") or skill_id),
            source="system",
            activation=str(raw.get("activation") or "builtin"),
            invocationPolicy=str(raw.get("invocationPolicy") or "auto"),
            constraints=[str(item) for item in raw.get("constraints") or []],
            validation_hints=[str(item) for item in raw.get("validation_hints") or []],
            metadata={key: value for key, value in raw.items() if key not in {"id", "name", "activation", "constraints", "validation_hints"}},
        )

    def _load_skill_root(self, root: Path, *, scope: SkillScope, plugin_id: str | None = None) -> tuple[list[SkillDefinition], list[SkillValidationIssue]]:
        if not root.exists():
            return [], []
        items: list[SkillDefinition] = []
        issues: list[SkillValidationIssue] = []
        for path in sorted(root.glob("*/SKILL.md")):
            item, item_issues = self._load_skill_file(path, scope=scope, plugin_id=plugin_id)
            if item is not None:
                items.append(item)
            issues.extend(item_issues)
        return items, issues

    def _load_plugin_skills(self, root: Path) -> tuple[list[SkillDefinition], list[SkillValidationIssue]]:
        if not root.exists():
            return [], []
        items: list[SkillDefinition] = []
        issues: list[SkillValidationIssue] = []
        for path in sorted(root.rglob("skills/*/SKILL.md")):
            plugin_id = self._nearest_plugin_id(path)
            item, item_issues = self._load_skill_file(path, scope="plugin", plugin_id=plugin_id)
            if item is not None:
                items.append(item)
            issues.extend(item_issues)
        return items, issues

    def _load_skill_file(self, path: Path, *, scope: SkillScope, plugin_id: str | None = None) -> tuple[SkillDefinition | None, list[SkillValidationIssue]]:
        text = _read_text(path)
        if not text:
            return None, [SkillValidationIssue(code="skill_empty", message="Skill file is empty or unreadable.", scope=scope, source=str(path))]
        frontmatter_raw, body = self.frontmatter(text)
        rules, acceptance = self.sections(text)
        dependencies = self.dependencies(frontmatter_raw.get("dependencies"))
        invocation_policy = self.invocation_policy(frontmatter_raw)
        skill_id = path.parent.name
        scoped_id = f"{scope}:{skill_id}"
        source = str(path.relative_to(self.repo_root)) if path.is_relative_to(self.repo_root) else str(path)
        try:
            frontmatter = SkillFrontmatter(
                description=str(frontmatter_raw.get("description") or "").strip() or None,
                whenToUse=self.list_field(frontmatter_raw.get("whenToUse") or frontmatter_raw.get("when_to_use")),
                paths=self.list_field(frontmatter_raw.get("paths")),
                allowedTools=self.list_field(frontmatter_raw.get("allowedTools") or frontmatter_raw.get("allowed_tools")),
                model=str(frontmatter_raw.get("model") or "").strip(),
                effort=str(frontmatter_raw.get("effort") or "").strip(),
                validation=self.list_field(frontmatter_raw.get("validation")),
                dependencies=dependencies,
                invocationPolicy=invocation_policy,
            )
            item = SkillDefinition(
                id=skill_id,
                scoped_id=scoped_id,
                scope=scope,
                name=(frontmatter.description or self.title(body, skill_id)).removeprefix("MAGIC DOC: ").strip(),
                source=source,
                activation="frontmatter_match" if frontmatter_raw else "skill_match",
                invocationPolicy=frontmatter.invocationPolicy or "explicit",
                whenToUse=frontmatter.whenToUse,
                paths=frontmatter.paths,
                allowedTools=frontmatter.allowedTools,
                model=frontmatter.model,
                effort=frontmatter.effort,
                validation=frontmatter.validation,
                validation_hints=acceptance[:8],
                dependencies=frontmatter.dependencies,
                constraints=rules[:8],
                body=body or text,
                frontmatter=frontmatter_raw,
                plugin_id=plugin_id,
                mtime_ns=path.stat().st_mtime_ns if path.exists() else 0,
                enabled=invocation_policy != "disabled",
            )
            return item, []
        except Exception as exc:
            return None, [SkillValidationIssue(code="skill_invalid", message=str(exc), skill_id=skill_id, scoped_id=scoped_id, scope=scope, source=source)]

    @staticmethod
    def frontmatter(text: str) -> tuple[dict[str, Any], str]:
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
    def sections(text: str) -> tuple[list[str], list[str]]:
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
    def title(text: str, default_title: str) -> str:
        for line in text.splitlines():
            if line.startswith("#"):
                return line.lstrip("#").strip() or default_title
        return default_title

    @staticmethod
    def dependencies(value: Any) -> list[SkillDependency]:
        if not value:
            return []
        raw_items = value if isinstance(value, list) else [value]
        deps: list[SkillDependency] = []
        for item in raw_items:
            if isinstance(item, dict):
                dep_id = str(item.get("id") or item.get("skill") or "").strip()
                if dep_id:
                    deps.append(SkillDependency(id=dep_id, optional=bool(item.get("optional"))))
            elif str(item or "").strip():
                deps.append(SkillDependency(id=str(item).strip()))
        return deps

    @staticmethod
    def invocation_policy(frontmatter: dict[str, Any]) -> SkillInvocationPolicy:
        raw = str(frontmatter.get("invocationPolicy") or frontmatter.get("invocation_policy") or "").strip().lower()
        if raw in {"always", "auto", "explicit", "disabled"}:
            return raw  # type: ignore[return-value]
        if frontmatter.get("whenToUse") or frontmatter.get("when_to_use") or frontmatter.get("paths"):
            return "auto"
        return "explicit"

    def _nearest_plugin_id(self, path: Path) -> str | None:
        for parent in path.parents:
            manifest = parent / "plugin.json"
            if manifest.exists():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    return None
                return str(payload.get("id") or "") or None
        return None

    def _signature(self) -> str:
        parts: list[str] = []
        for root in [self.runtime_dir / "skills", self.runtime_dir / "plugins", self.data_dir / "plugins", self.data_dir / "skills"]:
            if not root.exists():
                parts.append(f"{root}:missing")
                continue
            for path in sorted(root.rglob("SKILL.md")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
            for path in sorted(root.rglob("plugin.json")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return str(hash(tuple(parts)))

    def _manifest(self, *, signature: str, scopes: dict[str, int], issues: list[SkillValidationIssue], cache: dict[str, Any]) -> SkillRegistryManifest:
        return SkillRegistryManifest(
            roots={
                "system": "builtin",
                "repo": str(self.runtime_dir / "skills"),
                "plugin_runtime": str(self.runtime_dir / "plugins"),
                "plugin_user": str(self.data_dir / "plugins"),
                "user": str(self.data_dir / "skills"),
            },
            scopes=scopes,
            signature=signature,
            cache=cache,
            validation_issues=issues,
            created_at=_now(),
        )

    def _resolved_items(self, items: list[SkillDefinition]) -> list[SkillDefinition]:
        by_id: dict[str, SkillDefinition] = {}
        for item in sorted(items, key=lambda skill: (SCOPE_PRECEDENCE.get(skill.scope, 99), skill.id, skill.scoped_id)):
            by_id.setdefault(item.id, item)
        return list(by_id.values())

    def _dependency_validation_issues(self, items: list[SkillDefinition]) -> list[SkillValidationIssue]:
        lookup = self._definition_lookup_map(items)
        issues: list[SkillValidationIssue] = []
        for item in items:
            for dep in item.dependencies:
                if dep.id not in lookup and not dep.optional:
                    issues.append(SkillValidationIssue(code="dependency_missing", message=f"Missing dependency {dep.id}.", skill_id=item.id, scoped_id=item.scoped_id, scope=item.scope, source=item.source))
            if self._has_dependency_cycle(item, lookup, []):
                issues.append(SkillValidationIssue(code="dependency_cycle", message="Skill dependency cycle detected.", skill_id=item.id, scoped_id=item.scoped_id, scope=item.scope, source=item.source))
        return issues

    @classmethod
    def _lookup_map(cls, skills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for item in skills:
            skill_id = str(item.get("id") or "")
            scoped_id = str(item.get("scoped_id") or "")
            if skill_id:
                lookup.setdefault(skill_id, item)
            if scoped_id:
                lookup[scoped_id] = item
        return lookup

    @staticmethod
    def _definition_lookup_map(items: list[SkillDefinition]) -> dict[str, SkillDefinition]:
        lookup: dict[str, SkillDefinition] = {}
        for item in items:
            lookup.setdefault(item.id, item)
            lookup[item.scoped_id] = item
        return lookup

    def _has_dependency_cycle(self, item: SkillDefinition, lookup: dict[str, SkillDefinition], stack: list[str]) -> bool:
        if item.scoped_id in stack:
            return True
        for dep in item.dependencies:
            nested = lookup.get(dep.id)
            if nested is not None and self._has_dependency_cycle(nested, lookup, [*stack, item.scoped_id]):
                return True
        return False

    @classmethod
    def _select_with_budget(cls, candidates: list[dict[str, Any]], skipped: list[dict[str, Any]], budget: dict[str, int]) -> list[dict[str, Any]]:
        candidates.sort(key=lambda item: (-int(item.get("activation_score") or 0), SCOPE_PRECEDENCE.get(str(item.get("scope") or ""), 99), str(item.get("id") or "")))
        selected: list[dict[str, Any]] = []
        used_body_chars = 0
        conflict_groups: set[str] = set()
        for item in candidates:
            group = cls.conflict_group(item)
            body_chars = min(len(str(item.get("body") or "")), int(budget["max_body_chars"]))
            if len(selected) >= int(budget["max_skills"]):
                skipped.append({"id": item.get("id"), "scoped_id": item.get("scoped_id"), "reason": "activation_budget_exceeded", "activation_score": item.get("activation_score")})
                continue
            if used_body_chars + body_chars > int(budget["max_total_body_chars"]) and not item.get("explicit"):
                skipped.append({"id": item.get("id"), "scoped_id": item.get("scoped_id"), "reason": "body_budget_exceeded", "activation_score": item.get("activation_score")})
                continue
            if group and group in conflict_groups and not item.get("explicit"):
                skipped.append({"id": item.get("id"), "scoped_id": item.get("scoped_id"), "reason": f"conflict_group:{group}", "activation_score": item.get("activation_score")})
                continue
            selected.append({**item, "body_budget_chars": int(budget["max_body_chars"])})
            used_body_chars += body_chars
            if group:
                conflict_groups.add(group)
        return selected

    @classmethod
    def _with_dependencies(cls, selected: list[dict[str, Any]], lookup: dict[str, dict[str, Any]], budget: dict[str, int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        result = list(selected)
        seen = {str(item.get("scoped_id") or item.get("id")) for item in result}
        issues: list[dict[str, Any]] = []

        def add_dependencies(item: dict[str, Any], stack: list[str]) -> None:
            for dep in item.get("dependencies") or []:
                dep_id = str(dep.get("id") if isinstance(dep, dict) else dep)
                optional = bool(dep.get("optional")) if isinstance(dep, dict) else False
                if dep_id in stack:
                    issues.append({"code": "dependency_cycle", "message": f"Dependency cycle at {dep_id}.", "skill_id": item.get("id"), "scoped_id": item.get("scoped_id")})
                    continue
                nested = lookup.get(dep_id)
                if nested is None:
                    if not optional:
                        issues.append({"code": "dependency_missing", "message": f"Missing dependency {dep_id}.", "skill_id": item.get("id"), "scoped_id": item.get("scoped_id")})
                    continue
                nested_key = str(nested.get("scoped_id") or nested.get("id"))
                if nested_key not in seen and len(result) < int(budget["max_skills"]):
                    seen.add(nested_key)
                    result.append({**nested, "activation_reason": f"dependency:{item.get('id')}", "activation_score": max(1, int(item.get("activation_score") or 1) - 1), "dependency": True, "body_budget_chars": int(budget["max_body_chars"])})
                    add_dependencies(nested, [*stack, dep_id])

        for item in list(result):
            add_dependencies(item, [str(item.get("scoped_id") or item.get("id"))])
        return result, issues

    @staticmethod
    def _annotate_effective_policy(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        effective = SkillRegistryService.effective_policy(selected)
        return [{**item, "effective_policy": effective} for item in selected]

    @staticmethod
    def conflict_group(skill: dict[str, Any]) -> str:
        skill_id = str(skill.get("id") or "")
        validation = " ".join(str(item).lower() for item in skill.get("validation") or skill.get("validation_hints") or [])
        paths = " ".join(str(item).lower() for item in skill.get("paths") or [])
        if "mobile" in validation or "static/**/styles" in paths or skill_id == "mobile-ui-polish":
            return "mobile_polish"
        if skill_id == "fastapi-persistence" or "api" in validation or "backend" in validation:
            return "persistence"
        if "repair" in skill_id or "failing_check" in validation:
            return "repair"
        return ""
