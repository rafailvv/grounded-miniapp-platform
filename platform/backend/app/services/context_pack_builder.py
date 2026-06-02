from __future__ import annotations

import hashlib
from typing import Iterable
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import CodeChunkRecord, ContextPack, WorkspaceRecord
from app.services.code_index_service import CodeIndexService
from app.services.engine.mode_profiles import ModeProfiles
from app.services.generation_enhancements import ProjectInstructionBundle, SkillPackCatalog
from app.services.memory_pipeline import WorkspaceMemoryPipeline
from app.services.session_memory import SessionMemorySections
from app.services.skill_registry import SkillRegistryService
from app.services.workspace.service import WorkspaceService


class ContextPackBuilder:
    def __init__(
        self,
        code_index_service: CodeIndexService,
        workspace_service: WorkspaceService,
        context_budget_manager: Any | None = None,
        prompt_state_manager: Any | None = None,
    ) -> None:
        self.code_index_service = code_index_service
        self.workspace_service = workspace_service
        self.context_budget_manager = context_budget_manager
        self.prompt_state_manager = prompt_state_manager

    def build(
        self,
        *,
        workspace: WorkspaceRecord,
        prompt: str,
        model_profile: str | None,
        generation_mode: GenerationMode = GenerationMode.BALANCED,
        active_paths: list[str] | None = None,
        target_files: list[str] | None = None,
        execution_class: str | None = None,
        run_id: str | None = None,
        intent: str | None = None,
    ) -> ContextPack:
        budget = (
            self.context_budget_manager.build_budget(
                generation_mode=generation_mode,
                target_file_count=len(target_files or []),
                run_mode="generate",
            )
            if self.context_budget_manager is not None
            else {}
        )
        if (
            generation_mode == GenerationMode.FAST
            and str(intent or "").strip().lower() in {"edit", "refine", "role_only_change"}
            and int(budget.get("recent_diff_chars", 0) or 0) <= 0
        ):
            budget["recent_diff_chars"] = 1500
        code_limit, doc_limit = self._retrieval_limits(generation_mode, budget=budget)
        preferred_paths = self._preferred_anchor_paths(target_files=target_files or [])
        retrieval_active_paths = list(dict.fromkeys([*preferred_paths, *(active_paths or []), *(target_files or [])]))
        retrieval = self.code_index_service.retrieve(
            workspace_id=workspace.workspace_id,
            prompt=prompt,
            code_limit=code_limit,
            doc_limit=doc_limit,
            active_paths=retrieval_active_paths,
            recent_paths=self._recent_paths(workspace),
            budget=budget,
        )
        targeted_files: dict[str, str] = {}
        file_targets = list(target_files or [])
        if generation_mode == GenerationMode.FAST:
            file_targets = file_targets[: max(1, int(budget.get("targeted_file_limit", 5) or 5))]
        for file_path in file_targets:
            try:
                content = self.workspace_service.try_read_text_file(workspace.workspace_id, file_path, run_id=run_id)
            except FileNotFoundError:
                continue
            if content is None:
                continue
            targeted_files[file_path] = content
        stable_prefix = self.stable_prefix(workspace, model_profile)
        retrieval_stats = dict(retrieval["stats"])  # type: ignore[arg-type]
        mode_profile = ModeProfiles.resolve(generation_mode).to_dict()
        prompt_fingerprint = (
            self.prompt_state_manager.fingerprint(
                prompt=prompt,
                stable_prefix=stable_prefix,
                cache_key=self.prompt_cache_key(workspace, model_profile, stable_prefix),
            ).to_dict()
            if self.prompt_state_manager is not None
            else {}
        )
        retrieval_stats["anchor_report"] = {
            "execution_class": execution_class or "shell_app",
            "preferred_anchor_paths": preferred_paths,
            "retrieval_active_paths": retrieval_active_paths[:24],
            "selected_code_paths": [chunk["path"] for chunk in retrieval["code"]],  # type: ignore[index]
            "target_file_sample": file_targets[:12],
        }
        retrieval_stats["budget"] = budget
        retrieval_stats["mode_profile"] = mode_profile
        retrieval_stats["prompt_fingerprint"] = prompt_fingerprint
        workspace_summary = self._workspace_summary(
            workspace,
            prompt=prompt,
            intent=intent,
            generation_mode=generation_mode,
            paths=retrieval_active_paths,
        )
        memory_trace = self._last_memory_retrieval(workspace.workspace_id)
        if memory_trace:
            retrieval_stats["memory_retrieval"] = {
                "schema": memory_trace.get("schema"),
                "status": memory_trace.get("status"),
                "selected_count": (memory_trace.get("stats") or {}).get("selected_count"),
                "selected_ids": [item.get("memory_id") for item in memory_trace.get("items") or [] if isinstance(item, dict)],
                "reasons": [hit.get("selection_reason") for hit in memory_trace.get("hits") or [] if isinstance(hit, dict)],
            }
        return ContextPack(
            workspace_id=workspace.workspace_id,
            revision_id=workspace.current_revision_id,
            prompt=prompt,
            system_prefix=stable_prefix,
            workspace_summary=workspace_summary,
            current_task=prompt.strip(),
            recent_diff=self._build_recent_diff(
                workspace=workspace,
                run_id=run_id,
                generation_mode=generation_mode,
                execution_class=execution_class,
                intent=intent,
                target_files=file_targets,
                active_paths=retrieval_active_paths,
                budget=budget,
            ),
            code_chunks=[CodeChunkRecord.model_validate(item) for item in retrieval["code"]],  # type: ignore[index]
            doc_chunks=[CodeChunkRecord.model_validate(item) for item in retrieval["docs"]],  # type: ignore[index]
            targeted_files=targeted_files,
            prompt_cache_key=self.prompt_cache_key(workspace, model_profile, stable_prefix),
            retrieval_stats=retrieval_stats,
        )

    def _workspace_summary(
        self,
        workspace: WorkspaceRecord,
        *,
        prompt: str = "",
        intent: str | None = None,
        generation_mode: GenerationMode | str | None = None,
        paths: list[str] | None = None,
    ) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        parts = [
            f"Workspace {workspace.name}. Target platform: {platform}. "
            f"Template cloned: {workspace.template_cloned}. Current revision: {workspace.current_revision_id or 'none'}."
        ]
        memory = self._workspace_memory_summary(workspace.workspace_id, prompt=prompt, paths=paths or [])
        if memory:
            parts.append(memory)
        instruction_summary = self._project_instruction_summary(workspace=workspace, paths=paths or [])
        if instruction_summary:
            parts.append(instruction_summary)
        skill_summary = self._runtime_skill_summary(
            prompt=prompt,
            intent=intent,
            generation_mode=generation_mode,
            paths=paths or [],
        )
        if skill_summary:
            parts.append(skill_summary)
        return "\n".join(parts)

    def _workspace_memory_summary(self, workspace_id: str, *, prompt: str = "", paths: list[str] | None = None) -> str:
        store = getattr(self.code_index_service, "store", None)
        if store is None:
            return ""
        payload = store.get("reports", f"workspace_memory:{workspace_id}") or {}
        session_memory = store.get("reports", f"session_memory:{workspace_id}")
        if not isinstance(session_memory, dict):
            session_memory = SessionMemorySections.build(workspace_id=workspace_id, memory=payload, runs=[])
            store.upsert("reports", f"session_memory:{workspace_id}", session_memory)
        summary = WorkspaceMemoryPipeline.summary(workspace_id, payload, prompt=prompt, paths=paths or [], top_k=10)
        retrieval = WorkspaceMemoryPipeline.retrieve(
            workspace_id,
            payload,
            prompt=prompt,
            paths=paths or [],
            top_k=10,
        )
        store.upsert("reports", f"memory_retrieval:last:{workspace_id}", retrieval)
        items = [item for item in retrieval.get("items") or [] if isinstance(item, dict)]
        summary_text = str(summary.get("text") or "").strip()
        session_text = SessionMemorySections.compact_text(session_memory, limit=2400)
        if not items and not summary_text and not session_text:
            return ""
        lines = ["Workspace memory:"]
        if session_text:
            lines.append(session_text)
        if summary_text:
            lines.append(summary_text)
        else:
            lines.append("Workspace memory summary (always loaded; retrieve details on demand):")
        repeated_stats = WorkspaceMemoryPipeline.repeated_failure_stats(payload.get("items") or [])
        repeated_items = [item for item in repeated_stats.get("items") or [] if isinstance(item, dict)]
        if repeated_items:
            lines.append("Repeated failure refs:")
            for item in repeated_items[:3]:
                lines.append(
                    f"- {item.get('failure_signature') or item.get('key')}: count={item.get('count')}; "
                    f"check={item.get('check_name') or item.get('failure_class') or 'unknown'}"
                )
        hits_by_id = {
            str((hit.get("item") or {}).get("memory_id") or ""): hit
            for hit in retrieval.get("hits") or []
            if isinstance(hit, dict) and isinstance(hit.get("item"), dict)
        }
        if items:
            lines.append("Relevant memory details:")
        for item in items:
            text = str(item.get("text") or "").strip()
            if text:
                hit = hits_by_id.get(str(item.get("memory_id") or ""))
                reasons = ", ".join(str(reason) for reason in ((hit or {}).get("selection_reason") or [])[:3])
                suffix = f" [selected: {reasons}]" if reasons else ""
                lines.append(f"- {item.get('kind')}: {text[:220]}{suffix}")
        return "\n".join(lines)

    def _last_memory_retrieval(self, workspace_id: str) -> dict[str, Any]:
        store = getattr(self.code_index_service, "store", None)
        if store is None:
            return {}
        payload = store.get("reports", f"memory_retrieval:last:{workspace_id}") or {}
        return payload if isinstance(payload, dict) else {}

    def _runtime_skill_summary(
        self,
        *,
        prompt: str,
        intent: str | None,
        generation_mode: GenerationMode | str | None,
        paths: list[str],
    ) -> str:
        settings = getattr(self.code_index_service, "settings", None)
        if settings is None:
            return ""
        registry = SkillRegistryService(runtime_dir=settings.runtime_dir, repo_root=settings.repo_root, data_dir=settings.data_dir)
        prefetch = registry.prefetch()
        search = registry.search_for_context(
            prompt=prompt,
            intent=intent,
            generation_mode=str(getattr(generation_mode, "value", generation_mode) or ""),
            paths=paths,
        )
        selected = list(search.get("selected") or [])
        store = getattr(self.code_index_service, "store", None)
        if store is not None:
            store.upsert(
                "reports",
                "skill_prefetch:last",
                {
                    "prefetch": {k: v for k, v in prefetch.items() if k != "items"},
                    "search": {k: v for k, v in search.items() if k != "selected"},
                    "selected_ids": [item.get("id") for item in selected],
                },
            )
        return SkillPackCatalog.compact_context(selected, body_limit=500)

    def skill_search_for_context(
        self,
        *,
        prompt: str,
        intent: str | None,
        generation_mode: GenerationMode | str | None,
        paths: list[str],
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        settings = getattr(self.code_index_service, "settings", None)
        if settings is None:
            return {"schema": "grounded.skill_search.v1", "status": "settings_unavailable", "selected": [], "skipped": []}
        registry = SkillRegistryService(runtime_dir=settings.runtime_dir, repo_root=settings.repo_root, data_dir=settings.data_dir)
        prefetch = registry.prefetch()
        search = registry.search_for_context(
            prompt=prompt,
            intent=intent,
            generation_mode=str(getattr(generation_mode, "value", generation_mode) or ""),
            paths=paths,
            failure_class=failure_class,
        )
        return {
            **search,
            "prefetch": {k: v for k, v in prefetch.items() if k != "items"},
        }

    def _project_instruction_summary(self, *, workspace: WorkspaceRecord | None = None, paths: list[str] | None = None) -> str:
        settings = getattr(self.code_index_service, "settings", None)
        if settings is None:
            return ""
        workspace_root = self.workspace_service.source_dir(workspace.workspace_id) if workspace is not None else None
        bundle = ProjectInstructionBundle.build(repo_root=settings.repo_root, template_dir=settings.template_dir, workspace_root=workspace_root, paths=paths or [])
        return ProjectInstructionBundle.compact_summary(bundle, limit=1400)

    @staticmethod
    def stable_prefix(workspace: WorkspaceRecord, model_profile: str | None) -> str:
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        profile_label = str(model_profile or "mode-default")
        return (
            "You are editing a grounded mini-app workspace. "
            "Prefer bounded coherent feature changes over overly narrow micro-patches, preserve role separation, and keep generated artifacts consistent. "
            f"Model profile: {profile_label}. Target platform: {platform}. "
            "Defer non-essential file reads. Use retrieved chunks before widening context."
        )

    @classmethod
    def prompt_cache_key(cls, workspace: WorkspaceRecord, model_profile: str | None, stable_prefix: str | None = None) -> str:
        stable_prefix = stable_prefix if stable_prefix is not None else cls.stable_prefix(workspace, model_profile)
        platform = getattr(workspace.target_platform, "value", workspace.target_platform)
        material = f"{workspace.workspace_id}:{platform}:{str(model_profile or 'mode-default')}:{stable_prefix}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _recent_paths(workspace: WorkspaceRecord) -> list[str]:
        return [revision.message.split(": ", 1)[-1] for revision in workspace.revisions[-5:] if ": " in revision.message]

    @staticmethod
    def _retrieval_limits(generation_mode: GenerationMode, budget: dict[str, Any] | None = None) -> tuple[int, int]:
        budget = budget or {}
        if generation_mode == GenerationMode.FAST:
            return max(1, int(budget.get("retrieval_chunks", 2) or 2)), 0
        if generation_mode == GenerationMode.BALANCED:
            return 6, 4
        return 7, 5

    def _build_recent_diff(
        self,
        *,
        workspace: WorkspaceRecord,
        run_id: str | None,
        generation_mode: GenerationMode,
        execution_class: str | None,
        intent: str | None,
        target_files: list[str],
        active_paths: list[str],
        budget: dict[str, Any],
    ) -> str:
        if not workspace.template_cloned or not run_id:
            return ""
        diff_budget = max(0, int(budget.get("recent_diff_chars", 0) or 0))
        if diff_budget <= 0:
            return ""
        diff_text = self.workspace_service.diff(workspace.workspace_id, run_id=run_id)
        if not diff_text.strip():
            return ""
        filtered_diff = diff_text
        if generation_mode == GenerationMode.FAST:
            if str(intent or "").strip().lower() not in {"edit", "refine", "role_only_change"}:
                return ""
            focus_paths = list(
                dict.fromkeys(
                    [
                        *list(target_files or [])[:4],
                        *list(active_paths or [])[:2],
                    ]
                )
            )
            filtered_diff = self._filter_diff_to_paths(diff_text, focus_paths) if focus_paths else ""
        if not filtered_diff.strip():
            return ""
        return filtered_diff[:diff_budget]

    @staticmethod
    def _filter_diff_to_paths(diff_text: str, paths: Iterable[str]) -> str:
        normalized_paths = {
            str(path).strip().replace("\\", "/")
            for path in paths
            if str(path).strip()
        }
        if not normalized_paths:
            return ""
        kept_sections: list[str] = []
        current_lines: list[str] = []
        current_path: str | None = None
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                if current_lines and current_path in normalized_paths:
                    kept_sections.append("\n".join(current_lines))
                current_lines = [line]
                current_path = None
                if " b/" in line:
                    current_path = line.split(" b/", 1)[1].strip()
                    if "/" in current_path:
                        current_path = current_path.split("/", 1)[1]
                continue
            if current_lines:
                current_lines.append(line)
        if current_lines and current_path in normalized_paths:
            kept_sections.append("\n".join(current_lines))
        return "\n".join(section for section in kept_sections if section).strip()

    @staticmethod
    def _preferred_anchor_paths(*, target_files: list[str]) -> list[str]:
        anchors: list[str] = []
        anchors.extend(path for path in target_files if path.endswith("/index.html") or path.endswith("/profile.html"))
        anchors.extend(path for path in target_files if path.startswith("miniapp/app/routes/"))
        return list(dict.fromkeys(path for path in anchors if path))
