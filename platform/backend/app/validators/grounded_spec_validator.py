from __future__ import annotations

from app.models.artifacts import GroundedSpecValidatorResult, ValidationIssue
from app.models.grounded_spec import GroundedSpecModel


class GroundedSpecValidator:
    def validate(self, spec: GroundedSpecModel) -> GroundedSpecValidatorResult:
        issues: list[ValidationIssue] = []

        if len(spec.product_goal.strip()) < 10:
            issues.append(
                ValidationIssue(
                    code="spec.product_goal",
                    message="product_goal must be meaningfully populated.",
                    severity="critical",
                    location="product_goal",
                )
            )
        if not spec.actors:
            issues.append(
                ValidationIssue(
                    code="spec.actors",
                    message="At least one actor is required.",
                    severity="critical",
                    location="actors",
                )
            )
        if not spec.user_flows:
            issues.append(
                ValidationIssue(
                    code="spec.user_flows",
                    message="At least one user flow is required.",
                    severity="critical",
                    location="user_flows",
                )
            )
        if not spec.platform_constraints:
            issues.append(
                ValidationIssue(
                    code="spec.platform_constraints",
                    message="At least one platform constraint is required.",
                    severity="critical",
                    location="platform_constraints",
                )
            )
        for api_req in spec.api_requirements:
            if not api_req.method or not api_req.path:
                issues.append(
                    ValidationIssue(
                        code="spec.api_requirements.incomplete",
                        message=f"API requirement {api_req.api_req_id} must include method and path.",
                        severity="critical",
                        location=f"api_requirements.{api_req.api_req_id}",
                    )
                )
        if any(item.severity == "critical" for item in spec.contradictions):
            issues.append(
                ValidationIssue(
                    code="spec.contradictions.critical",
                    message="Critical contradictions block code generation.",
                    severity="critical",
                    location="contradictions",
                )
            )
        if any(item.impact == "high" for item in spec.unknowns):
            issues.append(
                ValidationIssue(
                    code="spec.unknowns.high_impact",
                    message="High-impact unknowns remain unresolved; generation may continue with assumptions or require later clarification.",
                    severity="high",
                    location="unknowns",
                    blocking=False,
                )
            )

        blocking = any(issue.blocking for issue in issues)
        return GroundedSpecValidatorResult(valid=not issues, blocking=blocking, issues=issues)
