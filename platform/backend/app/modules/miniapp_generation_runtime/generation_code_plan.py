from __future__ import annotations

import json
import re
import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, wait
from pathlib import Path
from typing import Any

from app.models.common import GenerationMode
from app.models.grounded_spec import GroundedSpecModel

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner


class MiniappGenerationCodePlan(MiniappGenerationRuntimeOwner):
    _VISUAL_PATCH_MARKERS = (
        "style",
        "styles",
        "styling",
        "polish",
        "polished",
        "visual",
        "look",
        "looks",
        "spacing",
        "hierarchy",
        "readable",
        "readability",
        "clear labels",
        "consistent with the rest of the app",
        "plain",
        "unfinished",
        "layout",
        "css",
        "padding",
        "margin",
        "design",
        "emphasis",
    )
    _FUNCTIONAL_PATCH_MARKERS = (
        "backend",
        "database",
        "schema",
        "persist",
        "persistence",
        "api",
        "endpoint",
        "route",
        "real saved",
        "save action",
        "create ",
        "update ",
        "delete ",
        "reject ",
        "approve ",
        "return to list",
        "new page",
        "separate page",
        "dedicated page",
        "record when",
        "status must",
    )
    _PAGE_FOCUS_STOPWORDS = {
        "a",
        "an",
        "and",
        "app",
        "booking",
        "bookings",
        "button",
        "buttons",
        "clear",
        "consistent",
        "current",
        "details",
        "detail",
        "existing",
        "flow",
        "for",
        "from",
        "hierarchy",
        "in",
        "it",
        "labels",
        "look",
        "looks",
        "of",
        "page",
        "polish",
        "polished",
        "proper",
        "readable",
        "rest",
        "specialist",
        "spacing",
        "status",
        "style",
        "styles",
        "styling",
        "the",
        "this",
        "to",
        "well",
        "with",
        "workflow",
    }

    def _resolve_code_plan(
        self,
        *,
        workspace_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        intent: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        scope_mode = self._scope_mode(intent, prompt, role_scope)
        require_multi_page = self._requires_multi_page(prompt, grounded_spec, role_scope, intent)
        strategy_reason = self._strategy_reason(intent, prompt, role_scope, require_multi_page=require_multi_page)
        workspace_tree = self.workspace_service.file_tree(workspace_id)
        try:
            payload = self._generate_code_plan_sections_with_timeout(
                timeout_seconds=float(self.CODE_PLAN_TOTAL_TIMEOUT_SECONDS),
                workspace_id=workspace_id,
                prompt=prompt,
                grounded_spec=grounded_spec,
                doc_refs=doc_refs,
                role_scope=role_scope,
                role_contract=role_contract,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
            normalized = self._normalize_model_payload(payload["payload"])
            planned = self._normalize_page_plan(
                normalized,
                role_scope=role_scope,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
                workspace_tree=workspace_tree,
            )
            planned["workspace_id"] = workspace_id
            focused_role = self._focused_minimal_patch_role(prompt=prompt, role_scope=role_scope)
            if scope_mode == "minimal_patch" and focused_role:
                planned = self._prune_minimal_patch_plan_to_focused_role(
                    planned,
                    prompt=prompt,
                    focused_role=focused_role,
                    role_scope=role_scope,
                )
            plan_gate_issues = self._page_graph_gate_issues(
                planned["page_graph"],
                role_scope,
                scope_mode=scope_mode,
                require_multi_page=require_multi_page,
            )
            planned["write_strategy"] = scope_mode
            planned["strategy_reason"] = strategy_reason
            planned["model"] = payload["model"]
            planned["plan_gate_issues"] = plan_gate_issues
            return planned
        except Exception as exc:
            self._append_trace(
                workspace_id,
                "code_plan_advisory_failed",
                "Advisory code plan failed; generation will continue from prompt, template affordances, and tool exploration.",
                {"error": str(exc)},
            )
            return {
                "summary": "",
                "flow_mode": "multi_page" if require_multi_page else "single_page",
                "files_to_read": [],
                "target_files": [],
                "shared_files": [],
                "backend_targets": [],
                "generation_clusters": [],
                "active_role_scope": [],
                "execution_plan": {},
                "planner_contract_enrichment": {"proactive_backend_targets": []},
                "page_graph": {"roles": {}},
                "scope_mode": scope_mode,
                "require_multi_page": require_multi_page,
                "write_strategy": scope_mode,
                "strategy_reason": strategy_reason,
                "model": "code-plan-unavailable",
                "plan_gate_issues": [],
                "workspace_id": workspace_id,
                "error": f"Page graph planning failed: {exc}",
            }

    @staticmethod
    def _focused_minimal_patch_role(*, prompt: str, role_scope: list[str]) -> str | None:
        lowered = str(prompt or "").lower()
        if len(role_scope) <= 1:
            return role_scope[0] if role_scope else None
        if not any(marker in lowered for marker in ("page", "detail", "details", "screen", "view", "route", "workflow")):
            return None
        scores: dict[str, int] = {}
        for role in role_scope:
            normalized = str(role or "").strip().lower()
            if not normalized:
                continue
            score = lowered.count(normalized)
            for pattern in (
                rf"\bfor the {normalized}\b",
                rf"\b{normalized} workflow\b",
                rf"\b{normalized} flow\b",
                rf"\b{normalized} can\b",
                rf"\b{normalized} should\b",
                rf"\b{normalized} must\b",
                rf"\b{normalized} opens?\b",
                rf"\bopen(?:s)? .*?\b{normalized}\b",
                rf"\b{normalized}\b.*?\b(detail|details|page|screen|view|route)\b",
                rf"\b(detail|details|page|screen|view|route)\b.*?\b{normalized}\b",
            ):
                if re.search(pattern, lowered):
                    score += 2
            scores[normalized] = score
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) < 2:
            return ranked[0][0] if ranked and ranked[0][1] >= 3 else None
        top_role, top_score = ranked[0]
        next_score = ranked[1][1]
        if top_score >= 4 and top_score >= next_score + 2:
            return top_role
        return None

    def _prune_minimal_patch_plan_to_focused_role(
        self,
        planned: dict[str, Any],
        *,
        prompt: str,
        focused_role: str,
        role_scope: list[str],
    ) -> dict[str, Any]:
        page_graph = dict(planned.get("page_graph") or {})
        roles = dict(page_graph.get("roles") or {})
        focused_payload = roles.get(focused_role) if isinstance(roles.get(focused_role), dict) else {}
        focused_page_targets: set[str] = set()
        focused_pages: list[dict[str, Any]] = []
        for page in (focused_payload.get("pages") or []):
            if not isinstance(page, dict):
                continue
            focused_pages.append(page)
            for key in ("file_path", "style_path", "script_path"):
                path = page.get(key)
                if isinstance(path, str) and path.startswith("miniapp/app/static/"):
                    focused_page_targets.add(path)

        visual_only_patch = self._looks_like_visual_page_patch(prompt=prompt)
        if visual_only_patch:
            source_role_pages = self._source_role_pages_for_focus(workspace_id=planned.get("workspace_id"), role=focused_role)
            for page in source_role_pages:
                if not isinstance(page, dict):
                    continue
                page_signature = (
                    str(page.get("page_id") or "").strip(),
                    str(page.get("route_path") or "").strip(),
                    str(page.get("file_path") or "").strip(),
                )
                if not any(
                    (
                        str(existing.get("page_id") or "").strip(),
                        str(existing.get("route_path") or "").strip(),
                        str(existing.get("file_path") or "").strip(),
                    )
                    == page_signature
                    for existing in focused_pages
                ):
                    focused_pages.append(page)
                for key in ("file_path", "style_path", "script_path"):
                    path = page.get(key)
                    if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/"):
                        focused_page_targets.add(path)

        if not focused_page_targets:
            return planned

        selected_page_targets = focused_page_targets
        if visual_only_patch:
            prompt_tokens = self._prompt_focus_tokens(prompt)
            rootish_prompt_tokens = {"main", "home", "landing", "dashboard", "root", "header", "top"}
            profile_prompt_tokens = {"profile", "settings", "account"}
            if rootish_prompt_tokens & prompt_tokens and not (profile_prompt_tokens & prompt_tokens):
                root_page_targets = {
                    str(path)
                    for page in focused_pages
                    if str(page.get("page_kind") or "").lower() in {"role_root", "landing"}
                    or str(page.get("route_path") or "").strip().lower() in {"/", f"/{focused_role}"}
                    for path in (page.get("file_path"), page.get("style_path"), page.get("script_path"))
                    if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/")
                }
                selected_page_targets = root_page_targets or self._select_focused_visual_page_targets(
                    prompt=prompt,
                    focused_role=focused_role,
                    pages=focused_pages,
                )
            else:
                selected_page_targets = self._select_focused_visual_page_targets(
                    prompt=prompt,
                    focused_role=focused_role,
                    pages=focused_pages,
                )

        non_focused_route_files = {
            str(role_payload.get("routes_file"))
            for role, role_payload in roles.items()
            if role != focused_role
            and isinstance(role_payload, dict)
            and isinstance(role_payload.get("routes_file"), str)
        }
        pruned_target_files = [
            path
            for path in (planned.get("target_files") or [])
            if not (
                isinstance(path, str)
                and (
                    (path.startswith("miniapp/app/static/") and path not in selected_page_targets and not path.startswith("miniapp/app/static/shared/"))
                    or path in non_focused_route_files
                )
            )
        ]
        pruned_backend_targets = [
            path
            for path in (planned.get("backend_targets") or [])
            if path not in non_focused_route_files
        ]
        if visual_only_patch:
            pruned_target_files = [
                path
                for path in pruned_target_files
                if path in selected_page_targets or path.startswith("miniapp/app/static/shared/")
            ]
            pruned_target_files = list(
                dict.fromkeys(
                    [
                        *pruned_target_files,
                        *sorted(selected_page_targets),
                    ]
                )
            )
            pruned_backend_targets = []

        filtered_roles = dict(roles)
        if visual_only_patch and focused_pages:
            filtered_roles[focused_role] = {
                **focused_payload,
                "pages": [
                    page
                    for page in focused_pages
                    if any(
                        isinstance(page.get(key), str) and page.get(key) in selected_page_targets
                        for key in ("file_path", "style_path", "script_path")
                    )
                ],
            }
            page_graph["roles"] = filtered_roles

        pruned_target_files = self._sanitize_planner_target_files(
            target_files=pruned_target_files,
            backend_targets=pruned_backend_targets,
            page_graph=page_graph,
        )
        target_set = set(pruned_target_files)
        pruned_backend_targets = [path for path in pruned_backend_targets if path in target_set]
        pruned_shared_files = [path for path in (planned.get("shared_files") or []) if path in target_set]
        pruned_files_to_read = list(planned.get("files_to_read") or [])
        if visual_only_patch:
            role_support_targets: set[str] = set()
            for page in focused_pages:
                for key in ("file_path", "style_path", "script_path"):
                    path = page.get(key)
                    if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/"):
                        role_support_targets.add(path)
            allowed_read_targets = set(pruned_target_files) | role_support_targets
            pruned_files_to_read = [
                path
                for path in pruned_files_to_read
                if isinstance(path, str)
                and (
                    path in allowed_read_targets
                    or path.startswith("miniapp/app/static/shared/")
                )
            ]
        generation_clusters = self._build_generation_clusters(pruned_target_files)
        execution_plan = self._build_execution_plan(
            role_scope=role_scope,
            roles=page_graph.get("roles") or roles,
            shared_files=pruned_shared_files,
            backend_targets=pruned_backend_targets,
            target_files=pruned_target_files,
            generation_clusters=generation_clusters,
        )
        return {
            **planned,
            "target_files": pruned_target_files,
            "backend_targets": pruned_backend_targets,
            "shared_files": pruned_shared_files,
            "files_to_read": pruned_files_to_read,
            "generation_clusters": generation_clusters,
            "active_role_scope": execution_plan["active_role_scope"],
            "execution_plan": execution_plan,
            "visual_only_patch": visual_only_patch,
            "suppress_role_route_targets": visual_only_patch,
        }

    def _source_role_pages_for_focus(self, *, workspace_id: str | None, role: str) -> list[dict[str, Any]]:
        if not workspace_id:
            return []
        try:
            workspace = self.workspace_service.get_workspace(workspace_id)
        except Exception:
            return []
        artifacts_path = Path(workspace.path) / "source" / "artifacts" / "generated_app_graph.json"
        if not artifacts_path.exists():
            return []
        try:
            payload = json.loads(artifacts_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        roles = payload.get("roles") or {}
        role_payload = roles.get(role) if isinstance(roles, dict) else None
        pages = role_payload.get("pages") if isinstance(role_payload, dict) else None
        if not isinstance(pages, list):
            return []
        return [page for page in pages if isinstance(page, dict)]

    @classmethod
    def _looks_like_visual_page_patch(cls, *, prompt: str) -> bool:
        lowered = str(prompt or "").lower()
        if not lowered.strip():
            return False
        visual_hits = sum(1 for marker in cls._VISUAL_PATCH_MARKERS if marker in lowered)
        if visual_hits == 0:
            return False
        return not any(marker in lowered for marker in cls._FUNCTIONAL_PATCH_MARKERS)

    @classmethod
    def _select_focused_visual_page_targets(
        cls,
        *,
        prompt: str,
        focused_role: str,
        pages: list[dict[str, Any]],
    ) -> set[str]:
        if len(pages) <= 1:
            return {
                str(path)
                for page in pages
                for path in (page.get("file_path"), page.get("style_path"), page.get("script_path"))
                if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/")
            }
        prompt_tokens = cls._prompt_focus_tokens(prompt)
        rootish_prompt_tokens = {"main", "home", "landing", "dashboard", "root", "header", "top"}
        profile_prompt_tokens = {"profile", "settings", "account"}
        cardish_prompt_tokens = {"card", "header", "top", "container", "avatar", "photo", "image", "logo"}
        if rootish_prompt_tokens & prompt_tokens and (
            not (profile_prompt_tokens & prompt_tokens)
            or cardish_prompt_tokens & prompt_tokens
        ):
            root_targets = {
                str(path)
                for page in pages
                if str(page.get("page_kind") or "").lower() in {"role_root", "landing"}
                or str(page.get("route_path") or "").strip().lower() in {"/", f"/{focused_role}"}
                for path in (page.get("file_path"), page.get("style_path"), page.get("script_path"))
                if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/")
            }
            if root_targets:
                return root_targets
        best_score = 0
        best_targets: set[str] = set()
        for page in pages:
            score = cls._score_page_focus(prompt_tokens=prompt_tokens, focused_role=focused_role, page=page)
            page_targets = {
                str(path)
                for path in (page.get("file_path"), page.get("style_path"), page.get("script_path"))
                if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/")
            }
            if not page_targets:
                continue
            if score > best_score:
                best_score = score
                best_targets = set(page_targets)
            elif score and score == best_score:
                best_targets.update(page_targets)
        if best_targets:
            return best_targets
        return {
            str(path)
            for page in pages
            for path in (page.get("file_path"), page.get("style_path"), page.get("script_path"))
            if isinstance(path, str) and path.startswith(f"miniapp/app/static/{focused_role}/")
        }

    @classmethod
    def _prompt_focus_tokens(cls, prompt: str) -> set[str]:
        tokens: set[str] = set()
        for raw in re.findall(r"[a-z0-9]+", str(prompt or "").lower()):
            if len(raw) < 3 or raw in cls._PAGE_FOCUS_STOPWORDS:
                continue
            tokens.add(raw)
            if raw.endswith("s") and len(raw) > 4:
                tokens.add(raw[:-1])
        return tokens

    @classmethod
    def _score_page_focus(
        cls,
        *,
        prompt_tokens: set[str],
        focused_role: str,
        page: dict[str, Any],
    ) -> int:
        score = 0
        page_kind = str(page.get("page_kind") or "").lower()
        page_tokens = cls._prompt_focus_tokens(
            " ".join(
                [
                    str(page.get("page_id") or ""),
                    str(page.get("route_path") or ""),
                    str(page.get("title") or ""),
                    str(page.get("navigation_label") or ""),
                    str(page.get("purpose") or ""),
                    str(page.get("description") or ""),
                    str(page.get("file_path") or ""),
                ]
            )
        )
        overlap = prompt_tokens & page_tokens
        score += len(overlap) * 2
        route_path = str(page.get("route_path") or "").strip().lower()
        rootish_prompt_tokens = {"main", "home", "landing", "dashboard", "root", "header", "top"}
        profile_prompt_tokens = {"profile", "settings", "account"}
        avatar_prompt_tokens = {"avatar", "photo", "image", "logo", "picture"}
        detail_tokens = {"detail", "details", "booking", "bookings"}
        if {"detail", "details"} & prompt_tokens and (
            page_kind in {"detail", "feature"}
            or {"detail", "details"} & page_tokens
            or any(token in page_tokens for token in {"id", focused_role})
        ):
            score += 4
        if detail_tokens & prompt_tokens and detail_tokens & page_tokens:
            score += 2
        if avatar_prompt_tokens & prompt_tokens and avatar_prompt_tokens & page_tokens:
            score += 3
        cardish_prompt_tokens = {"card", "header", "top", "container"}
        if rootish_prompt_tokens & prompt_tokens:
            if page_kind in {"role_root", "landing"} or route_path in {"/", f"/{focused_role}"}:
                score += 6
                if avatar_prompt_tokens & prompt_tokens:
                    score += 4
                if cardish_prompt_tokens & prompt_tokens:
                    score += 3
            if page_kind in {"role_profile", "profile"} and not (
                (profile_prompt_tokens & prompt_tokens) and not (cardish_prompt_tokens & prompt_tokens)
            ):
                score -= 3
        if profile_prompt_tokens & prompt_tokens and page_kind in {"role_profile", "profile"}:
            score += 5
        return score

    def _generate_code_plan_sections_with_timeout(
        self,
        *,
        timeout_seconds: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="code-plan-total")
        future = self._submit_with_context(executor, self._generate_code_plan_sections, **kwargs)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"Timed out waiting for code plan generation after {int(timeout_seconds)}s."
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    def _generate_code_plan_sections(
        self,
        *,
        workspace_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        doc_refs: list[Any],
        role_scope: list[str],
        role_contract: dict[str, Any],
        scope_mode: str,
        require_multi_page: bool,
        workspace_tree: list[dict[str, str]],
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
    ) -> dict[str, Any]:
        sections_started = time.perf_counter()
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="code-plan")
        futures = {
            "graph": self._submit_with_context(
                executor,
                self._generate_structured_with_retry,
                role="code_plan",
                schema_name="page_graph_structure_v1",
                schema=self._code_plan_partial_schema(["summary", "flow_mode", "page_graph"]),
                system_prompt=self._code_plan_section_system_prompt("Page graph and route structure"),
                user_prompt=self._code_plan_section_user_prompt(
                    section_id="graph",
                    section_title="Page graph and route structure",
                    section_contract=[
                        "Return the real page graph, role routes, page purposes, primary actions, and handoff paths.",
                        "Keep role surfaces distinct and multi-page when required.",
                        "Do not decide final file-read lists in this section.",
                    ],
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    require_multi_page=require_multi_page,
                    workspace_tree=workspace_tree,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                ),
            ),
            "targeting": self._submit_with_context(
                executor,
                self._generate_structured_with_retry,
                role="code_plan",
                schema_name="page_graph_targeting_v1",
                schema=self._code_plan_partial_schema(["files_to_read", "target_files", "shared_files", "backend_targets"]),
                system_prompt=self._code_plan_section_system_prompt("File targeting and read set"),
                user_prompt=self._code_plan_section_user_prompt(
                    section_id="targeting",
                    section_title="File targeting and read set",
                    section_contract=[
                        "Return only read-set and file-target lists.",
                        "Target files must stay minimal for minimal_patch requests.",
                        "Use the page graph implied by the request and role contract, but do not re-emit full page definitions.",
                    ],
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    doc_refs=doc_refs,
                    role_scope=role_scope,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    require_multi_page=require_multi_page,
                    workspace_tree=workspace_tree,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                ),
            ),
        }
        section_timeout = float(self.CODE_PLAN_SECTION_TIMEOUT_SECONDS)
        completed, pending = wait(set(futures.values()), timeout=section_timeout, return_when=ALL_COMPLETED)
        section_payloads: dict[str, dict[str, Any]] = {}
        section_errors: dict[str, str] = {}
        try:
            for section_name, future in futures.items():
                if future in pending:
                    section_errors[section_name] = "timeout"
                    continue
                try:
                    section_payloads[section_name] = future.result()
                except Exception as exc:
                    section_errors[section_name] = str(exc)
            if pending:
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=False, cancel_futures=False)
        finally:
            if pending:
                for future in pending:
                    future.cancel()

        if section_errors:
            graph_payload = section_payloads.get("graph")
            if graph_payload is None:
                raise RuntimeError(
                    "Code plan generation returned incomplete sections without a valid agent response: "
                    f"{section_errors}"
                )
            merged_payload = self._normalize_model_payload(graph_payload["payload"])
            targeting_payload = section_payloads.get("targeting")
            if targeting_payload is not None:
                merged_payload.update(self._normalize_model_payload(targeting_payload["payload"]))
            else:
                merged_payload.setdefault("files_to_read", [])
                merged_payload.setdefault("target_files", [])
                merged_payload.setdefault("shared_files", [])
                merged_payload.setdefault("backend_targets", [])
            winning_model = str(
                (targeting_payload or {}).get("model")
                or graph_payload.get("model")
                or "code-plan-partial"
            )
            self._append_trace(
                workspace_id,
                "code_plan_sections_partial_merge",
                "Code plan section timed out; merged successful sections without planner-shaped substitution.",
                {
                    "duration_ms": int((time.perf_counter() - sections_started) * 1000),
                    "section_errors": section_errors,
                    "section_payloads": sorted(section_payloads.keys()),
                },
            )
            return {
                "model": winning_model,
                "payload": merged_payload,
                "response_mode": "code_plan_sections_partial_merge",
            }

        self._append_trace(
            workspace_id,
            "code_plan_sections_parallel",
            "Code plan graph and targeting sections completed in parallel.",
            {
                "duration_ms": int((time.perf_counter() - sections_started) * 1000),
                "sections": ["graph", "targeting"],
            },
        )
        graph_payload_normalized = self._normalize_model_payload(section_payloads["graph"]["payload"])
        targeting_payload_normalized = self._normalize_model_payload(section_payloads["targeting"]["payload"])
        merged_payload = {**graph_payload_normalized, **targeting_payload_normalized}
        return {
            "model": (
                section_payloads.get("targeting", {}).get("model")
                or section_payloads.get("graph", {}).get("model")
                or "code-plan-sections"
            ),
            "payload": merged_payload,
            "response_mode": "code_plan_sections",
        }
