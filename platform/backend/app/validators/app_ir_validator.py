from __future__ import annotations

from app.models.app_ir import AppIRModel
from app.models.artifacts import AppIRValidatorResult, ValidationIssue


class AppIRValidator:
    def validate(self, ir: AppIRModel) -> AppIRValidatorResult:
        issues: list[ValidationIssue] = []

        screen_ids = [screen.screen_id for screen in ir.screens]
        if ir.entry_screen_id not in screen_ids:
            issues.append(
                ValidationIssue(
                    code="ir.entry_screen_id",
                    message="entry_screen_id must reference an existing screen.",
                    severity="critical",
                    location="entry_screen_id",
                )
            )

        self._check_uniqueness("screen_id", screen_ids, issues)
        self._check_uniqueness("variable_id", [item.variable_id for item in ir.variables], issues)
        self._check_uniqueness(
            "component_id",
            [component.component_id for screen in ir.screens for component in screen.components],
            issues,
        )
        self._check_uniqueness(
            "action_id",
            [action.action_id for screen in ir.screens for action in screen.actions],
            issues,
        )
        self._check_uniqueness("transition_id", [item.transition_id for item in ir.transitions], issues)
        self._check_uniqueness("integration_id", [item.integration_id for item in ir.integrations], issues)

        variable_ids = {item.variable_id for item in ir.variables}
        for screen in ir.screens:
            for component in screen.components:
                if component.binding_variable_id not in variable_ids:
                    issues.append(
                        ValidationIssue(
                            code="ir.binding_variable_id",
                            message=f"Component {component.component_id} references a missing variable.",
                            severity="critical",
                            location=f"screens.{screen.screen_id}.components.{component.component_id}",
                        )
                    )

        integration_ids = {item.integration_id for item in ir.integrations}
        for screen in ir.screens:
            for action in screen.actions:
                if action.integration_id and action.integration_id not in integration_ids:
                    issues.append(
                        ValidationIssue(
                            code="ir.integration_ref",
                            message=f"Action {action.action_id} references a missing integration.",
                            severity="critical",
                            location=f"screens.{screen.screen_id}.actions.{action.action_id}",
                        )
                    )

        for transition in ir.transitions:
            if transition.from_screen_id not in screen_ids or transition.to_screen_id not in screen_ids:
                issues.append(
                    ValidationIssue(
                        code="ir.transition_ref",
                        message=f"Transition {transition.transition_id} references a missing screen.",
                        severity="critical",
                        location=f"transitions.{transition.transition_id}",
                    )
                )

        route_roles: set[str] = set()
        for group in ir.route_groups:
            route_roles.add(group.role)
            route_paths: set[str] = set()
            referenced_screen_ids = {route.screen_id for route in group.routes}
            if not group.routes:
                issues.append(
                    ValidationIssue(
                        code="ir.route_groups.empty",
                        message=f"Route group {group.role} must contain at least one route.",
                        severity="critical",
                        location=f"route_groups.{group.role}",
                    )
                )
            if not any(route.is_entry for route in group.routes):
                issues.append(
                    ValidationIssue(
                        code="ir.route_groups.entry",
                        message=f"Route group {group.role} must define one entry route.",
                        severity="critical",
                        location=f"route_groups.{group.role}",
                    )
                )
            for route in group.routes:
                if route.path in route_paths:
                    issues.append(
                        ValidationIssue(
                            code="ir.route_groups.duplicate_path",
                            message=f"Duplicate route path {route.path} in role {group.role}.",
                            severity="critical",
                            location=f"route_groups.{group.role}",
                        )
                    )
                route_paths.add(route.path)
                if route.screen_id not in screen_ids:
                    issues.append(
                        ValidationIssue(
                            code="ir.route_groups.screen_ref",
                            message=f"Route {route.route_id} references a missing screen.",
                            severity="critical",
                            location=f"route_groups.{group.role}.{route.route_id}",
                        )
                    )
            if group.entry_path not in route_paths:
                issues.append(
                    ValidationIssue(
                        code="ir.route_groups.entry_path",
                        message=f"entry_path {group.entry_path} must match one of the generated routes for {group.role}.",
                        severity="critical",
                        location=f"route_groups.{group.role}",
                    )
                )
            if not referenced_screen_ids:
                issues.append(
                    ValidationIssue(
                        code="ir.route_groups.screens",
                        message=f"Route group {group.role} does not reference any screens.",
                        severity="critical",
                        location=f"route_groups.{group.role}",
                    )
                )

        if route_roles != {"client", "specialist", "manager"}:
            issues.append(
                ValidationIssue(
                    code="ir.route_groups.roles",
                    message="AppIR must include route groups for client, specialist, and manager.",
                    severity="critical",
                    location="route_groups",
                )
            )

        source_ids = {source.source_id for source in ir.screen_data_sources}
        self._check_uniqueness("screen_data_source", list(source_ids), issues)
        for source in ir.screen_data_sources:
            if source.screen_id not in screen_ids:
                issues.append(
                    ValidationIssue(
                        code="ir.screen_data_sources.screen_ref",
                        message=f"Screen data source {source.source_id} references a missing screen.",
                        severity="critical",
                        location=f"screen_data_sources.{source.source_id}",
                    )
                )

        for action_group in ir.role_action_groups:
            for action_id in action_group.action_ids:
                if action_id not in {action.action_id for screen in ir.screens for action in screen.actions}:
                    issues.append(
                        ValidationIssue(
                            code="ir.role_action_groups.action_ref",
                            message=f"Role action group {action_group.role} references a missing action {action_id}.",
                            severity="critical",
                            location=f"role_action_groups.{action_group.role}",
                        )
                    )

        if ir.auth_model.mode == "telegram_session" and not ir.auth_model.telegram_initdata_validation_required:
            issues.append(
                ValidationIssue(
                    code="ir.auth_model.telegram",
                    message="telegram_session requires server-side initData validation.",
                    severity="critical",
                    location="auth_model",
                )
            )

        for variable in ir.variables:
            if variable.trust_level == "trusted" and variable.source == "user_input":
                issues.append(
                    ValidationIssue(
                        code="ir.trusted_user_input",
                        message=f"Variable {variable.variable_id} cannot be trusted when sourced from user_input.",
                        severity="critical",
                        location=f"variables.{variable.variable_id}",
                    )
                )

        for terminal_screen_id in ir.terminal_screen_ids:
            if terminal_screen_id not in screen_ids:
                issues.append(
                    ValidationIssue(
                        code="ir.terminal_screen_ids",
                        message=f"Terminal screen {terminal_screen_id} does not exist.",
                        severity="critical",
                        location="terminal_screen_ids",
                    )
                )

        for pii_var in ir.security.pii_variables:
            if pii_var not in variable_ids:
                issues.append(
                    ValidationIssue(
                        code="ir.security.pii_variables",
                        message=f"PII variable {pii_var} does not exist.",
                        severity="critical",
                        location="security.pii_variables",
                    )
                )

        if any(question.blocking for question in ir.open_questions):
            issues.append(
                ValidationIssue(
                    code="ir.open_questions.blocking",
                    message="Blocking open questions prevent controlled compilation.",
                    severity="high",
                    location="open_questions",
                )
            )

        blocking = any(issue.blocking for issue in issues)
        return AppIRValidatorResult(valid=not issues, blocking=blocking, issues=issues)

    @staticmethod
    def _check_uniqueness(label: str, values: list[str], issues: list[ValidationIssue]) -> None:
        seen: set[str] = set()
        duplicates = {item for item in values if item in seen or seen.add(item)}
        for duplicate in duplicates:
            issues.append(
                ValidationIssue(
                    code=f"ir.{label}.duplicate",
                    message=f"{label} must be unique: {duplicate}",
                    severity="critical",
                    location=label,
                )
            )
