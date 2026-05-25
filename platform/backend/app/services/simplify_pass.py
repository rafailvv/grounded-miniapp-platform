from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import re
from typing import Any


class SimplifyPass:
    """Post-green simplification audit for changed miniapp files."""

    SCHEMA = "grounded.simplify_pass.v1"

    @classmethod
    def build(
        cls,
        *,
        run: Any,
        source_dir: Path,
        gate: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate = gate if isinstance(gate, dict) else {}
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        changed_files = cls._changed_files(run=run, artifacts=artifacts)
        files = cls._read_files(source_dir, changed_files)
        findings: list[dict[str, Any]] = []
        findings.extend(cls._duplicate_function_findings(files))
        findings.extend(cls._selector_findings(files))
        findings.extend(cls._js_complexity_findings(files))
        findings.extend(cls._state_consistency_findings(files))
        findings.extend(cls._reuse_findings(files))
        safe_tasks = cls._safe_tasks(findings)
        green = cls._is_green(run, gate)
        status = "ready" if green else "blocked_until_green"
        if green and findings:
            status = "needs_simplify"
        if green and not findings:
            status = "clean"
        return {
            "schema": cls.SCHEMA,
            "run_id": run.run_id,
            "workspace_id": run.workspace_id,
            "status": status,
            "green_required": True,
            "green_status": {
                "is_green": green,
                "run_status": run.status,
                "apply_status": run.apply_status,
                "gate_status": gate.get("status"),
                "gate_blocking": gate.get("blocking"),
            },
            "changed_files": changed_files,
            "reviewed_files": [item["path"] for item in files],
            "summary": {
                "finding_count": len(findings),
                "safe_task_count": len(safe_tasks),
                "categories": dict(Counter(str(item.get("category") or "unknown") for item in findings)),
            },
            "findings": findings[:80],
            "safe_refactor_tasks": safe_tasks[:40],
            "completion_gate": {
                "allowed_to_apply_automatically": False,
                "reason": "Simplify pass is an audit/repair-task generator; source edits still need scoped patch + checks.",
            },
        }

    @staticmethod
    def _changed_files(*, run: Any, artifacts: dict[str, Any]) -> list[str]:
        paths = [str(path).strip().replace("\\", "/") for path in list(getattr(run, "touched_files", []) or []) if str(path).strip()]
        for key in ("changed_files", "touched_files"):
            value = artifacts.get(key)
            if isinstance(value, list):
                paths.extend(str(path).strip().replace("\\", "/") for path in value if str(path).strip())
        if not paths and isinstance(artifacts.get("diff"), str):
            for line in str(artifacts.get("diff") or "").splitlines():
                if line.startswith("diff --git ") and " b/" in line:
                    paths.append(line.split(" b/", 1)[1].strip())
        return [path for path in dict.fromkeys(paths) if path.startswith("miniapp/")][:80]

    @staticmethod
    def _read_files(source_dir: Path, changed_files: list[str]) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for relative in changed_files:
            if not relative.endswith((".js", ".mjs", ".html", ".css", ".py")):
                continue
            path = source_dir / relative
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files.append({"path": relative, "text": text[:120000], "line_count": text.count("\n") + 1})
        return files

    @staticmethod
    def _duplicate_function_findings(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        signatures: dict[str, list[str]] = defaultdict(list)
        for item in files:
            if not str(item["path"]).endswith((".js", ".mjs")):
                continue
            for match in re.finditer(r"\b(?:function\s+([A-Za-z0-9_]+)|const\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)", item["text"]):
                name = match.group(1) or match.group(2)
                if name:
                    signatures[name].append(str(item["path"]))
        findings = []
        for name, paths in signatures.items():
            unique_paths = list(dict.fromkeys(paths))
            if len(paths) > 1:
                findings.append(
                    {
                        "category": "reuse",
                        "severity": "medium",
                        "title": f"Repeated JS helper `{name}`",
                        "details": "Repeated helpers increase repair surface and selector/state drift risk.",
                        "paths": unique_paths,
                        "evidence": {"symbol": name, "occurrences": len(paths)},
                    }
                )
        return findings

    @staticmethod
    def _selector_findings(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings = []
        for item in files:
            path = str(item["path"])
            if not path.endswith((".js", ".mjs")):
                continue
            text = str(item["text"])
            selectors = re.findall(r"(?:querySelector|getElementById)\((['\"])(.*?)\1\)", text)
            selector_counts = Counter(value for _, value in selectors)
            repeated = [selector for selector, count in selector_counts.items() if count >= 4]
            if repeated:
                findings.append(
                    {
                        "category": "selectors",
                        "severity": "medium",
                        "title": "Repeated DOM selectors should be centralized",
                        "details": "Centralize repeated selectors or guarded DOM lookup helpers to reduce brittle UI repairs.",
                        "paths": [path],
                        "evidence": {"selectors": repeated[:12]},
                    }
                )
            if "querySelector" in text and "?." not in text and "if (" not in text[: max(1, text.find("querySelector") + 400)]:
                findings.append(
                    {
                        "category": "selectors",
                        "severity": "low",
                        "title": "DOM lookups may need stable guards",
                        "details": "Changed JS uses selectors; verify optional route/page DOM is guarded before binding handlers.",
                        "paths": [path],
                        "evidence": {"selector_count": len(selectors)},
                    }
                )
        return findings

    @staticmethod
    def _js_complexity_findings(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings = []
        for item in files:
            path = str(item["path"])
            text = str(item["text"])
            if not path.endswith((".js", ".mjs")):
                continue
            fetch_count = len(re.findall(r"\bfetch\s*\(", text))
            listener_count = len(re.findall(r"\.addEventListener\s*\(", text))
            if int(item["line_count"]) > 420 or fetch_count >= 8 or listener_count >= 16:
                findings.append(
                    {
                        "category": "efficiency",
                        "severity": "medium",
                        "title": "Large role JS file needs structure pass",
                        "details": "Split repeated API/render/event-binding logic into local helpers without changing behavior.",
                        "paths": [path],
                        "evidence": {"line_count": item["line_count"], "fetch_count": fetch_count, "listener_count": listener_count},
                    }
                )
        return findings

    @staticmethod
    def _state_consistency_findings(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keys: dict[str, set[str]] = defaultdict(set)
        api_paths: dict[str, set[str]] = defaultdict(set)
        for item in files:
            path = str(item["path"])
            text = str(item["text"])
            for key in re.findall(r"localStorage\.(?:getItem|setItem|removeItem)\((['\"])(.*?)\1", text):
                keys[key[1]].add(path)
            for api in re.findall(r"['\"](/api/[A-Za-z0-9_./-]+)['\"]", text):
                api_paths[api].add(path)
        findings = []
        for key, paths in keys.items():
            if len(paths) >= 2:
                findings.append(
                    {
                        "category": "state_consistency",
                        "severity": "medium",
                        "title": f"Shared localStorage key `{key}` appears across changed files",
                        "details": "Confirm a single source of truth and avoid role-specific drift for persisted client state.",
                        "paths": sorted(paths),
                        "evidence": {"storage_key": key},
                    }
                )
        for api, paths in api_paths.items():
            if len(paths) >= 3:
                findings.append(
                    {
                        "category": "state_consistency",
                        "severity": "low",
                        "title": f"API path `{api}` is called from several changed files",
                        "details": "Consider a shared API helper only if payload shape and error handling are identical.",
                        "paths": sorted(paths),
                        "evidence": {"api_path": api},
                    }
                )
        return findings

    @staticmethod
    def _reuse_findings(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        class_counts: Counter[str] = Counter()
        paths_by_class: dict[str, set[str]] = defaultdict(set)
        for item in files:
            path = str(item["path"])
            if not path.endswith((".html", ".js", ".mjs")):
                continue
            for class_attr in re.findall(r"class=['\"]([^'\"]+)['\"]", str(item["text"])):
                normalized = " ".join(sorted(class_attr.split()))
                if normalized:
                    class_counts[normalized] += 1
                    paths_by_class[normalized].add(path)
        repeated = [name for name, count in class_counts.items() if count >= 8 and len(paths_by_class[name]) >= 2]
        if not repeated:
            return []
        return [
            {
                "category": "reuse",
                "severity": "low",
                "title": "Repeated UI class combinations across changed files",
                "details": "If markup repeats, move stable card/list/form styling to shared CSS instead of duplicating per role.",
                "paths": sorted(set().union(*(paths_by_class[name] for name in repeated[:6]))),
                "evidence": {"class_groups": repeated[:6]},
            }
        ]

    @staticmethod
    def _safe_tasks(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tasks = []
        for index, finding in enumerate(findings, start=1):
            category = str(finding.get("category") or "quality")
            tasks.append(
                {
                    "task_id": f"simplify.{category}.{index}",
                    "category": category,
                    "title": finding.get("title"),
                    "paths": finding.get("paths") or [],
                    "proof_required": ["run existing green checks again", "browser proof unchanged for touched role surfaces"],
                    "autofix_safe": False,
                }
            )
        return tasks

    @staticmethod
    def _is_green(run: Any, gate: dict[str, Any]) -> bool:
        if gate:
            return str(gate.get("status") or "").lower() in {"passed", "green"} and not bool(gate.get("blocking"))
        return str(getattr(run, "status", "")) == "completed" and str(getattr(run, "apply_status", "")) == "applied"
