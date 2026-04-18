from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models.domain import FixScopeEntry
from app.modules.miniapp_agent_loop.fix_types import FixTurnContext

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixContextRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    def current_diff_summary(self, workspace_id: str, run_id: str) -> str | None:
        diff_report = self.service.store.get("reports", f"candidate_diff:{workspace_id}") or {}
        diff_text = str(diff_report.get("diff") or "")
        if not diff_text and self.service.workspace_service.draft_exists(workspace_id, run_id):
            diff_text = self.service.workspace_service.diff(workspace_id, run_id=run_id)
        summary = self.diff_summary(diff_text)
        return None if summary == "No diff recorded." else summary

    def collect_file_contexts(
        self,
        workspace_id: str,
        run_id: str,
        scope_entries: list[FixScopeEntry],
        *,
        fix_turn: FixTurnContext | None = None,
        budget_override: int | None = None,
        full_files: bool = False,
    ) -> dict[str, str]:
        contexts: dict[str, str] = {}
        budget = budget_override or self.service.MAX_CONTEXT_CHARS
        for entry in scope_entries:
            if budget <= 0:
                break
            if not self.file_exists(workspace_id, run_id, entry.file_path):
                continue
            target_path = self.service.workspace_service.draft_source_dir(workspace_id, run_id) / entry.file_path
            if target_path.is_dir():
                continue
            content = self.service.workspace_service.read_file(workspace_id, entry.file_path, run_id=run_id)
            excerpt = content if full_files else content[: min(len(content), min(4000, budget))]
            if len(excerpt) > budget:
                excerpt = excerpt[:budget]
            contexts[entry.file_path] = excerpt
            budget -= len(excerpt)
        for support_path in self.service._repair_support_files(fix_turn):
            if budget <= 0 or support_path in contexts or not self.file_exists(workspace_id, run_id, support_path):
                continue
            content = self.service.workspace_service.read_file(workspace_id, support_path, run_id=run_id)
            excerpt = content if full_files else content[: min(len(content), min(2500, budget))]
            if len(excerpt) > budget:
                excerpt = excerpt[:budget]
            contexts[support_path] = excerpt
            budget -= len(excerpt)
        return contexts

    def merge_additional_context_paths(
        self,
        workspace_id: str,
        run_id: str,
        contexts: dict[str, str],
        additional_paths: list[str],
        *,
        budget_override: int | None = None,
    ) -> dict[str, str]:
        if not additional_paths:
            return contexts
        merged = dict(contexts)
        budget = budget_override or self.service.MAX_CONTEXT_CHARS_EXPANDED
        used = sum(len(content) for content in merged.values())
        remaining = max(0, budget - used)
        for path in additional_paths:
            if remaining <= 0 or path in merged or not self.file_exists(workspace_id, run_id, path):
                continue
            target_path = self.service.workspace_service.draft_source_dir(workspace_id, run_id) / path
            if target_path.is_dir():
                continue
            content = self.service.workspace_service.read_file(workspace_id, path, run_id=run_id)
            excerpt = content[:remaining]
            if not excerpt:
                continue
            merged[path] = excerpt
            remaining -= len(excerpt)
        return merged

    @staticmethod
    def looks_like_context_refusal(diagnosis: str) -> bool:
        lowered = diagnosis.lower().replace("’", "'").replace("‘", "'")
        markers = (
            "can't inspect",
            "cannot inspect",
            "can't access",
            "cannot access",
            "can't edit the workspace files",
            "cannot edit the workspace files",
            "without access to the actual file contents",
            "unable to inspect",
            "unable to access the file",
            "need to inspect",
            "need the current contents",
            "need current contents",
            "need the current route wiring",
            "need current route wiring",
            "need to review the current",
            "need the full file",
            "need full file",
            "current file excerpts were insufficient",
            "insufficient file excerpts",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def planned_target_paths(llm_result: dict[str, Any]) -> list[str]:
        raw_targets = []
        tool_requests = llm_result.get("tool_requests") or []
        if isinstance(tool_requests, list):
            for item in tool_requests:
                if not isinstance(item, dict):
                    continue
                if str(item.get("tool") or "").strip().lower() != "read_files":
                    continue
                raw_targets = item.get("targets") or []
                if raw_targets:
                    break
        if not isinstance(raw_targets, list):
            return []
        normalized: list[str] = []
        for item in raw_targets:
            target = str(item or "").strip().lstrip("./")
            if not target or target in normalized:
                continue
            normalized.append(target)
        return normalized[:12]

    def resolve_frontend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        normalized = module_path.replace("@/", "miniapp/app/static/")
        candidates = [normalized]
        if "." not in Path(normalized).name:
            candidates.extend([f"{normalized}.html", f"{normalized}.css", f"{normalized}.js"])
        for candidate in candidates:
            if self.file_exists(workspace_id, run_id, candidate):
                return candidate
        return None

    def resolve_backend_module(self, workspace_id: str, run_id: str, module_path: str) -> str | None:
        normalized = f"miniapp/{module_path.replace('.', '/')}.py"
        return normalized if self.file_exists(workspace_id, run_id, normalized) else None

    def file_exists(self, workspace_id: str, run_id: str, relative_path: str) -> bool:
        return (self.service.workspace_service.draft_source_dir(workspace_id, run_id) / relative_path).exists()

    @staticmethod
    def diff_summary(diff_text: str) -> str:
        files = re.findall(r"^diff --git a/.+ b/(.+)$", diff_text, flags=re.MULTILINE)
        if not files:
            return "No diff recorded."
        return f"Updated {len(files)} file(s): {', '.join(files[:5])}"
