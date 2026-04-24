from __future__ import annotations

import re
from typing import Any

from app.models.artifacts import ValidationIssue
from app.models.domain import RunCheckResult
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import ROLE_ORDER


class ServiceStrategyMixins:
    _EXPLICIT_MICRO_PATCH_MARKERS = (
        "css only",
        "styles only",
        "styling only",
        "visual only",
        "layout only",
        "avatar only",
        "logo only",
        "image only",
        "spacing only",
        "padding only",
        "margin only",
    )
    _VISUAL_ROLE_PATCH_MARKERS = (
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
        "avatar",
        "photo",
        "logo",
        "image",
        "background",
        "full-screen",
        "full screen",
        "stretched",
        "properly sized",
        "aligned",
        "proportions",
        "text",
        "copy",
        "labels",
        "loading text",
        "action text",
        "reads naturally",
        "natural text",
        "natural copy",
        "stray numeric",
        "numeric suffix",
        "clean up",
    )
    _UI_FLOW_ROLE_PATCH_MARKERS = (
        "separate page",
        "separate details page",
        "details page",
        "detail page",
        "dedicated page",
        "dedicated detail page",
        "dedicated details page",
        "open a separate page",
        "opens a separate page",
        "open on a separate page",
        "clicks on a request",
        "click on a request",
        "clicks on",
        "click on",
        "from the list",
        "instead of keeping everything only in the list",
        "show full information",
        "full information about",
        "open the details",
        "open details",
        "details screen",
        "detail screen",
    )
    _CONTRACT_ROLE_PATCH_CORE_MARKERS = (
        "real saved action",
        "real persisted",
        "persist to the real backend",
        "persisted to the backend",
        "shared db state",
        "shared state",
        "status is correctly reflected",
        "must be able to reject",
        "must be able to approve",
        "save action",
        "approve/reject",
        "approve or reject",
        "reject controls",
        "approve controls",
        "decisions must persist",
        "existing api",
    )
    _CONTRACT_ROLE_PATCH_ACTION_MARKERS = (
        "status must change",
        "status must update",
        "reject the request",
        "approve the request",
        "record when",
        "across the app",
        "across the whole app",
        "across specialist",
        "across manager",
        "across client",
    )
    _CONTRACT_ROLE_PATCH_MARKERS = (
        "real saved action",
        "real persisted",
        "persist to the real backend",
        "persisted to the backend",
        "shared db state",
        "shared state",
        "status must change",
        "status must update",
        "across the app",
        "across the whole app",
        "across specialist",
        "across manager",
        "across client",
        "backend",
        "database",
        "schema",
        "persist",
        "persistence",
        "api",
        "endpoint",
        "route",
        "record when",
        "reject the request",
        "approve the request",
        "must be able to reject",
        "must be able to approve",
        "save action",
        "status is correctly reflected",
    )
    _INTERACTION_PATCH_TRIGGER_MARKERS = (
        "button",
        "buttons",
        "click",
        "clicks",
        "clicked",
        "tap",
        "taps",
        "pressed",
        "press",
        "pressing",
        "when i click",
        "when the user clicks",
        "when the client clicks",
        "when the specialist clicks",
        "when the manager clicks",
        "when i press",
        "when pressing",
        "on click",
        "при нажатии",
        "по нажатию",
        "когда нажимаю",
        "нажимаю на кнопку",
        "кнопк",
    )
    _INTERACTION_PATCH_RESULT_MARKERS = (
        "should change",
        "should update",
        "should open",
        "should save",
        "should trigger",
        "should submit",
        "should become",
        "must change",
        "must update",
        "must open",
        "must save",
        "does not change",
        "doesn't change",
        "stays",
        "stays the same",
        "remains",
        "instead of",
        "не меняется",
        "не изменяется",
        "остается",
        "остаётся",
        "должен меняться",
        "должна меняться",
        "должно меняться",
        "должен открывать",
        "должна открывать",
        "должно открывать",
        "должен вызывать",
        "должна вызывать",
        "должно вызывать",
        "должен отправлять",
        "должна отправлять",
        "должно отправлять",
    )
    _INTERACTION_PATCH_BROAD_MARKERS = (
        "separate page",
        "details page",
        "detail page",
        "dedicated page",
        "details screen",
        "detail screen",
        "new page",
        "new screen",
        "across the app",
        "across the whole app",
        "across specialist",
        "across manager",
        "across client",
        "all roles",
    )

    @staticmethod
    def _matching_marker_count(lowered: str, markers: tuple[str, ...]) -> int:
        return sum(1 for marker in markers if marker in lowered)

    @staticmethod
    def _looks_like_explicit_micro_patch(lowered: str) -> bool:
        return any(marker in lowered for marker in ServiceStrategyMixins._EXPLICIT_MICRO_PATCH_MARKERS)

    @staticmethod
    def _looks_like_role_flow_expansion_request(lowered: str) -> bool:
        explicit_phrases = (
            "open a separate page",
            "opens a separate page",
            "open on a separate page",
            "separate details page",
            "dedicated details page",
            "full information about",
        )
        if any(marker in lowered for marker in explicit_phrases):
            return True
        return ServiceStrategyMixins._matching_marker_count(lowered, ServiceStrategyMixins._UI_FLOW_ROLE_PATCH_MARKERS) >= 2

    @staticmethod
    def _looks_like_visual_role_patch_request(lowered: str) -> bool:
        if not lowered.strip():
            return False
        visual_hits = sum(1 for marker in ServiceStrategyMixins._VISUAL_ROLE_PATCH_MARKERS if marker in lowered)
        if visual_hits == 0:
            return False
        normalized = ServiceStrategyMixins._without_preservation_only_contract_language(lowered)
        return not any(marker in normalized for marker in ServiceStrategyMixins._CONTRACT_ROLE_PATCH_MARKERS)

    @staticmethod
    def _without_preservation_only_contract_language(lowered: str) -> str:
        return re.sub(
            r"\b(?:keep|keeping|preserve|preserving|leave|leaving)\b[^.?!]*(?:api|routes?|backend|behavior|logic|roles?|structure)[^.?!]*\b(?:intact|unchanged|as\s+is|same)\b",
            "",
            lowered,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _looks_like_contract_role_patch_request(lowered: str) -> bool:
        if any(marker in lowered for marker in ServiceStrategyMixins._CONTRACT_ROLE_PATCH_CORE_MARKERS):
            return True
        support_hits = ServiceStrategyMixins._matching_marker_count(lowered, ServiceStrategyMixins._CONTRACT_ROLE_PATCH_MARKERS)
        action_hits = ServiceStrategyMixins._matching_marker_count(lowered, ServiceStrategyMixins._CONTRACT_ROLE_PATCH_ACTION_MARKERS)
        return action_hits >= 1 and support_hits >= 2

    @staticmethod
    def _looks_like_narrow_interaction_patch_request(lowered: str) -> bool:
        if not lowered.strip():
            return False
        if ServiceStrategyMixins._looks_like_role_flow_expansion_request(lowered):
            return False
        if any(marker in lowered for marker in ServiceStrategyMixins._INTERACTION_PATCH_BROAD_MARKERS):
            return False
        trigger_hits = ServiceStrategyMixins._matching_marker_count(lowered, ServiceStrategyMixins._INTERACTION_PATCH_TRIGGER_MARKERS)
        result_hits = ServiceStrategyMixins._matching_marker_count(lowered, ServiceStrategyMixins._INTERACTION_PATCH_RESULT_MARKERS)
        if trigger_hits == 0:
            return False
        if result_hits >= 1:
            return True
        return bool(
            re.search(
                r"\b(?:button|click|tap|press|handler|action)\b.*\b(?:status|open|save|submit|change|update|trigger|select)\b",
                lowered,
            )
            or re.search(
                r"(?:кнопк|нажати|нажимаю).*(?:статус|открыва|сохраня|отправля|меня|обновля)",
                lowered,
            )
        )

    @staticmethod
    def _role_only_patch_kind(*, prompt: str, role_scope: list[str], intent: str) -> str | None:
        if len(role_scope) != 1:
            return None
        if intent not in {"edit", "refine", "role_only_change"}:
            return None
        lowered = str(prompt or "").lower()
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return None
        if ServiceStrategyMixins._looks_like_visual_role_patch_request(lowered):
            return "visual_patch"
        if ServiceStrategyMixins._looks_like_narrow_interaction_patch_request(lowered):
            return "interaction_patch"
        if ServiceStrategyMixins._looks_like_contract_role_patch_request(lowered):
            return "contract_patch"
        if ServiceStrategyMixins._looks_like_role_flow_expansion_request(lowered):
            return "ui_flow_patch"
        return "role_patch"

    @staticmethod
    def _scope_mode(intent: str, prompt: str, role_scope: list[str]) -> str:
        lowered = prompt.lower()
        role_patch_kind = ServiceStrategyMixins._role_only_patch_kind(prompt=prompt, role_scope=role_scope, intent=intent)
        if ServiceStrategyMixins._looks_like_fix_request(lowered):
            return "minimal_patch"
        if role_patch_kind == "visual_patch":
            return "minimal_patch"
        if role_patch_kind == "interaction_patch":
            return "minimal_patch"
        if ServiceStrategyMixins._looks_like_explicit_micro_patch(lowered):
            return "minimal_patch"
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return "whole_file_build"
        if intent == "create":
            return "whole_file_build"
        if role_patch_kind in {"ui_flow_patch", "contract_patch"}:
            return "whole_file_build"
        if len(role_scope) == 1 and intent in {"edit", "refine", "role_only_change"}:
            return "whole_file_build"
        whole_file_markers = ("full implementation", "whole file", "whole files", "generate by files", "generate files", "new app surface", "catalog", "storefront", "checkout", "workspace", "dashboard", "workflow", "multi-page", "multi role", "multi-role", "refactor")
        if len(role_scope) > 1 or any(marker in lowered for marker in whole_file_markers):
            return "whole_file_build"
        return "minimal_patch"

    @staticmethod
    def _strategy_reason(intent: str, prompt: str, role_scope: list[str], *, require_multi_page: bool) -> str:
        lowered = prompt.lower()
        role_patch_kind = ServiceStrategyMixins._role_only_patch_kind(prompt=prompt, role_scope=role_scope, intent=intent)
        if intent == "create":
            return "Intent=create requires a whole-file build for the primary app surface."
        if role_patch_kind == "visual_patch":
            return "The request is a single-role visual patch, so generation stays in minimal patch mode."
        if role_patch_kind == "interaction_patch":
            return "The request is a narrow single-role interaction fix, so generation keeps the existing surface and changes only the smallest in-place behavior."
        if role_patch_kind == "ui_flow_patch":
            return "The request expands an existing single-role UI flow, so generation uses bounded role regeneration instead of a micro-patch."
        if role_patch_kind == "contract_patch":
            return "The request changes a single-role contract-backed behavior, so generation uses bounded role regeneration with related runtime files when needed."
        if role_patch_kind == "role_patch":
            return "The request changes one existing role surface, so generation stays bounded to that role bundle instead of reworking the whole app."
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return "The request describes a new workflow-heavy app surface, so generation uses whole-file bundles."
        if len(role_scope) > 1:
            return "Multiple roles are in scope, so generation uses whole-file bundles instead of local patches."
        if require_multi_page:
            return "The request implies multiple pages or flows, so generation uses whole-file bundles."
        if len(role_scope) == 1 and intent in {"edit", "refine", "role_only_change"}:
            return "A single-role change should stay bounded to that role and feature family, but not be forced into a micro-patch."
        if any(marker in lowered for marker in ("catalog", "storefront", "checkout", "workspace", "dashboard", "full implementation", "refactor")):
            return "The request introduces a new app surface, so generation uses whole-file bundles."
        return "The request is narrow enough to stay in minimal patch mode."

    @staticmethod
    def _requires_multi_page(prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], intent: str) -> bool:
        role_patch_kind = ServiceStrategyMixins._role_only_patch_kind(prompt=prompt, role_scope=role_scope, intent=intent)
        if intent == "create":
            return True
        lowered = prompt.lower()
        if ServiceStrategyMixins._looks_like_fix_request(lowered):
            return False
        if role_patch_kind == "visual_patch":
            return False
        if role_patch_kind == "ui_flow_patch":
            return True
        if role_patch_kind == "role_patch":
            return False
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return True
        if intent in {"edit", "refine", "role_only_change"}:
            return any(marker in lowered for marker in ("new page", "new pages", "add page", "add pages", "multi-page", "catalog", "checkout", "dashboard", "workflow", "workspace"))
        if len(role_scope) > 1 or len(grounded_spec.user_flows) > 1 or len(grounded_spec.domain_entities) > 1:
            return True
        multi_markers = ("page", "pages", "browse", "detail", "cart", "checkout", "track", "queue", "dashboard", "management", "workspace", "catalog")
        return any(marker in lowered for marker in multi_markers)

    @staticmethod
    def _classify_execution_class(*, prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], intent: str) -> str:
        lowered = prompt.lower()
        lifecycle_markers = ("create", "submit", "assign", "update", "comment", "track", "filter", "schedule", "approve", "review", "manage")
        lifecycle_hits = sum(1 for marker in lifecycle_markers if marker in lowered)
        entity_count = len(grounded_spec.domain_entities)
        flow_count = len(grounded_spec.user_flows)
        api_count = len(grounded_spec.api_requirements)
        persistence_count = len(grounded_spec.persistence_requirements)
        role_handoff = len(role_scope) > 1 and flow_count > 1
        dashboard_signal = any(marker in lowered for marker in ("dashboard", "overview", "workload", "monitor", "triage", "queue", "status", "statuses"))
        if persistence_count >= 3 or (entity_count >= 4 and api_count >= 4):
            return "data_crud_app"
        if role_handoff and (dashboard_signal or api_count >= 3):
            return "workflow_dashboard_app"
        if role_handoff or flow_count > 1 or lifecycle_hits >= 3 or api_count > 0 or persistence_count > 0:
            return "entity_workflow_app"
        if intent == "create" and entity_count > 1 and lifecycle_hits >= 2:
            return "entity_workflow_app"
        return "entity_workflow_app"

    @staticmethod
    def _is_business_page(role: str, page: dict[str, Any]) -> bool:
        file_path = str(page.get("file_path") or "")
        route_path = str(page.get("route_path") or "")
        return file_path not in {f"miniapp/app/static/{role}/index.html", f"miniapp/app/static/{role}/profile/index.html"} and route_path not in {f"/{role}", f"/{role}/profile", "/", "/profile"}

    @staticmethod
    def _is_role_root_page(role: str, page: dict[str, Any]) -> bool:
        route_path = str(page.get("route_path") or "").strip()
        file_path = str(page.get("file_path") or "").strip()
        page_kind = str(page.get("page_kind") or "").strip().lower()
        return bool(page.get("is_entry") or route_path in {"/", f"/{role}"} or file_path == f"miniapp/app/static/{role}/index.html" or page_kind in {"home", "dashboard", "landing"})

    @staticmethod
    def _first_paint_required_sections(role: str) -> list[str]:
        section_map = {
            "client": ["profile_card", "primary_actions", "live_section_shell", "empty_state_guidance"],
            "specialist": ["profile_card", "operational_actions", "live_section_shell", "empty_state_guidance"],
            "manager": ["profile_card", "oversight_summary", "live_section_shell", "empty_state_guidance"],
        }
        return list(section_map.get(role, ["primary_actions", "live_section_shell", "empty_state_guidance"]))

    def _first_paint_contract_for_page(self, *, role: str, page: dict[str, Any], grounded_spec: GroundedSpecModel) -> dict[str, Any]:
        dependency_count = len(page.get("data_dependencies") or [])
        product_goal = str(getattr(grounded_spec, "product_goal", "") or "").strip()
        return {
            "content_first_required": self._is_role_root_page(role, page) and dependency_count > 0,
            "loading_policy": "do_not_force_dedicated_loading_blocks_on_role_roots" if dependency_count > 0 else "secondary_only",
            "required_surface_sections": self._first_paint_required_sections(role),
            "empty_state_policy": "show honest business empty states instead of pseudo-records",
            "product_goal": product_goal,
        }

    @staticmethod
    def _looks_like_fix_request(prompt: str) -> bool:
        fix_markers = ("fix", "bug", "error", "failed", "failure", "exception", "traceback", "stacktrace", "stack trace", "build failed", "preview failed", "docker", "npm run build", "exit code", "исправ", "ошиб", "не работает", "слом", "падает", "сбой")
        return any(marker in prompt for marker in fix_markers)

    @staticmethod
    def _looks_like_create_surface_request(prompt: str, role_scope: list[str]) -> bool:
        if ServiceStrategyMixins._looks_like_fix_request(prompt):
            return False
        creation_markers = ("create ", "build ", "generate ", "make ", "new mini app", "new app", "from scratch", "application should", "app should")
        workflow_markers = ("mini app", "mini-app", "multi-page", "multi page", "multi-role", "multi role", "role-based", "role based", "storefront", "catalog", "checkout", "cart", "order", "orders", "workspace", "dashboard", "customer-facing", "customer side")
        role_mentions = sum(1 for role in ROLE_ORDER if role in prompt)
        has_creation_signal = any(marker in prompt for marker in creation_markers)
        has_workflow_signal = any(marker in prompt for marker in workflow_markers)
        if has_creation_signal and (has_workflow_signal or len(role_scope) > 1 or role_mentions > 1):
            return True
        if len(role_scope) > 1 and has_workflow_signal and any(marker in prompt for marker in ("should", "support", "application", "app")):
            return True
        return False

    @staticmethod
    def _effective_prompt(request: Any) -> str:
        if request.mode != "fix" or request.error_context is None:
            return request.prompt
        segments = [
            request.prompt.strip(),
            "Repair only the reported error. Keep the diff minimal and preserve existing behavior.",
            f"Error source: {request.error_context.source or 'unknown'}",
            f"Failing target: {request.error_context.failing_target or 'unknown'}",
            request.error_context.raw_error.strip(),
        ]
        return "\n\n".join(segment for segment in segments if segment)

    @staticmethod
    def _failure_class_from_error_context(error_context: Any) -> str | None:
        if not error_context:
            return None
        source = str(getattr(error_context, "source", "") or "").lower()
        raw_error = str(getattr(error_context, "raw_error", "") or "").lower()
        text = f"{source}\n{raw_error}"
        if any(marker in text for marker in ("preview failed", "docker preview", "permission denied", "docker daemon")):
            return "preview_startup"
        if any(marker in text for marker in ("traceback", "importerror", "modulenotfounderror", "literal is not defined")):
            return "backend_startup"
        if any(marker in text for marker in ("npm run build", "ts230", "vite", "typescript", "jsx", "next/link")):
            return "frontend_build"
        if any(marker in text for marker in ("401", "403", "authorization", "permissions denied")):
            return "runtime_permission"
        if any(marker in text for marker in ("payload", "schema", "validationerror", "does not return", "unexpected key")):
            return "api_contract_mismatch"
        if source:
            return source
        return "runtime_failure"

    @staticmethod
    def _root_cause_summary(error_context: Any) -> str | None:
        if not error_context:
            return None
        source = str(getattr(error_context, "source", "") or "runtime")
        failing_target = str(getattr(error_context, "failing_target", "") or "current build")
        raw_error = str(getattr(error_context, "raw_error", "") or "").strip()
        first_line = raw_error.splitlines()[0].strip() if raw_error else ""
        if not first_line:
            return f"Fix run requested for {source} issue in {failing_target}."
        return f"{source} issue in {failing_target}: {first_line[:220]}"

    @staticmethod
    def _summarize_failed_checks(build_issues: list[ValidationIssue], preview_issue: ValidationIssue | None) -> str | None:
        issues = [issue.message for issue in build_issues[:3]]
        if preview_issue is not None:
            issues.append(preview_issue.message)
        issues = [issue.strip() for issue in issues if issue and issue.strip()]
        return "; ".join(issues[:4]) if issues else None

    @staticmethod
    def _build_fix_handoff(*, prompt: str, failure_reason: str, failure_class: str | None, issues: list[ValidationIssue], mode: str | None = None) -> dict[str, Any]:
        check_results = [
            RunCheckResult(
                name=issue.code,
                status="failed" if issue.blocking else "passed",
                details=issue.message,
                logs=[
                    f"location={issue.location or 'unknown'}",
                    f"severity={issue.severity or 'unknown'}",
                ],
            ).model_dump(mode="json")
            for issue in issues
        ]
        return {
            "prompt": prompt,
            "mode": mode or "fix",
            "failure_reason": failure_reason,
            "failure_class": failure_class,
            "check_results": check_results,
        }
