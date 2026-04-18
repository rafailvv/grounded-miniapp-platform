from __future__ import annotations

from typing import Any

from app.models.domain import RunCheckResult, ValidationIssue
from app.models.grounded_spec import GroundedSpecModel
from app.services.miniapp_generation.constants import ROLE_ORDER, WORKFLOW_HEAVY_MARKERS


class ServiceStrategyMixins:
    @staticmethod
    def _scope_mode(intent: str, prompt: str, role_scope: list[str]) -> str:
        lowered = prompt.lower()
        if ServiceStrategyMixins._looks_like_fix_request(lowered):
            return "minimal_patch"
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return "whole_file_build"
        if intent in {"edit", "refine", "role_only_change"}:
            return "minimal_patch"
        if any(marker in lowered for marker in ("only ", "just ", "точечно", "только ", "without touching", "do not touch anything else")):
            return "minimal_patch"
        if len(role_scope) == 1 and any(marker in lowered for marker in ("change", "update", "fix", "refine", "polish")):
            return "minimal_patch"
        if intent == "create":
            return "whole_file_build"
        whole_file_markers = ("full implementation", "whole file", "whole files", "generate by files", "generate files", "new app surface", "catalog", "storefront", "checkout", "workspace", "dashboard", "workflow", "multi-page", "multi role", "multi-role", "refactor")
        if len(role_scope) > 1 or any(marker in lowered for marker in whole_file_markers):
            return "whole_file_build"
        return "minimal_patch"

    @staticmethod
    def _strategy_reason(intent: str, prompt: str, role_scope: list[str], *, require_multi_page: bool) -> str:
        lowered = prompt.lower()
        if intent == "create":
            return "Intent=create requires a whole-file build for the primary app surface."
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return "The request describes a new workflow-heavy app surface, so generation uses whole-file bundles."
        if len(role_scope) > 1:
            return "Multiple roles are in scope, so generation uses whole-file bundles instead of local patches."
        if require_multi_page:
            return "The request implies multiple pages or flows, so generation uses whole-file bundles."
        if any(marker in lowered for marker in ("catalog", "storefront", "checkout", "workspace", "dashboard", "full implementation", "refactor")):
            return "The request introduces a new app surface, so generation uses whole-file bundles."
        return "The request is narrow enough to stay in minimal patch mode."

    @staticmethod
    def _requires_multi_page(prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], intent: str) -> bool:
        if intent == "create":
            return True
        lowered = prompt.lower()
        if ServiceStrategyMixins._looks_like_fix_request(lowered):
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
    def _requires_business_pages(prompt: str, grounded_spec: GroundedSpecModel, role_scope: list[str], intent: str) -> bool:
        lowered = prompt.lower()
        if ServiceStrategyMixins._looks_like_fix_request(lowered):
            return False
        if ServiceStrategyMixins._looks_like_create_surface_request(lowered, role_scope):
            return True
        if any(marker in lowered for marker in WORKFLOW_HEAVY_MARKERS):
            return True
        if intent == "create" and len(role_scope) > 1:
            return True
        if len(grounded_spec.user_flows) > 1:
            return True
        if len(grounded_spec.api_requirements) > 0 or len(grounded_spec.persistence_requirements) > 0:
            return True
        return False

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
    def _planning_retry_prompt(prompt: str) -> str:
        corrective = (
            "Planning correction: expand into a realistic route/page graph that matches the workflow. "
            "Do not collapse the app into role landing pages. "
            "Use dashboard, workbench, workspace, and profile only as structural references when the prompt does not imply a better structure. "
            "Differentiate the roles through page purpose, actions, and handoffs instead of mirrored wording. "
            "Role root pages must feel complete on first render without fake records: render real sections, actions, and honest empty states immediately. "
            "Do not make loading or error UI the primary visible surface on first render."
        )
        return f"{prompt.rstrip()}\n\n{corrective}"

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
            "client": ["profile_card", "summary_metrics", "primary_actions", "requests_preview"],
            "specialist": ["profile_card", "summary_metrics", "queue_preview", "availability_preview", "conflict_preview"],
            "manager": ["profile_card", "oversight_metrics", "availability_preview", "conflict_preview", "approval_preview"],
        }
        return list(section_map.get(role, ["summary_metrics", "primary_actions"]))

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
            RunCheckResult(name=issue.code, passed=not issue.blocking, details=issue.message, metadata={"location": issue.location, "severity": issue.severity}).model_dump(mode="json")
            for issue in issues
        ]
        return {
            "prompt": prompt,
            "mode": mode or "fix",
            "failure_reason": failure_reason,
            "failure_class": failure_class,
            "check_results": check_results,
        }
