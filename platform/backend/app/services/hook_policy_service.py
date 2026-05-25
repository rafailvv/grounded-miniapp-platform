from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatch
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.models.hooks import (
    HookAction,
    HookCondition,
    HookContext,
    HookContextItem,
    HookEvaluation,
    HookPolicy,
    HookRule,
    HookValidationIssue,
)
from app.repositories.state_store import StateStore


SUPPORTED_HOOKS = [
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "before_apply",
    "after_checks",
    "on_check_failed",
]
SUPPORTED_ACTIONS = ["block", "add_context", "tag"]
SUPPORTED_CONDITIONS = [
    "hook",
    "tool",
    "canonical_tool",
    "risk",
    "mode",
    "path_globs",
    "changed_files",
    "check_names",
    "check_statuses",
    "worker_id",
    "run_intent",
    "generation_mode",
]

MAX_RULES_PER_POLICY = 80
MAX_ACTIONS_PER_RULE = 8
MAX_CONTEXT_CHARS = 2000
MAX_REASON_CHARS = 500
MAX_METADATA_CHARS = 4000
SECRET_KEY_RE = re.compile(r"(^|_)(api_key|access_token|refresh_token|private_key|client_secret|password|secret)($|_)", re.I)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
)


class HookPolicyService:
    """Declarative lifecycle hook policy loader and evaluator."""

    def __init__(self, store: StateStore, project_policy_path: Path | None = None) -> None:
        self.store = store
        self.project_policy_path = project_policy_path
        self._builtin_policy = HookPolicy(
            policy_id="builtin",
            source="builtin",
            rules=[
                HookRule(
                    rule_id="builtin.pre_tool_use.forbidden_risk",
                    source="builtin",
                    description="Block model-facing tools already marked as forbidden.",
                    conditions=HookCondition(hook="pre_tool_use", risk="forbidden"),
                    actions=[HookAction(action="block", reason="Tool risk is forbidden by internal hook policy.")],
                    priority=1000,
                ),
                HookRule(
                    rule_id="builtin.on_check_failed.repair_context",
                    source="builtin",
                    description="Add focused repair context after failed checks.",
                    conditions=HookCondition(hook="on_check_failed"),
                    actions=[
                        HookAction(
                            action="add_context",
                            text="Repair must start from the failing check evidence and named repair packet before broad edits.",
                            priority=100,
                            target="repair_turn",
                        )
                    ],
                    priority=100,
                ),
            ],
        )

    @property
    def caps(self) -> dict[str, int]:
        return {
            "max_rules_per_policy": MAX_RULES_PER_POLICY,
            "max_actions_per_rule": MAX_ACTIONS_PER_RULE,
            "max_context_chars": MAX_CONTEXT_CHARS,
            "max_reason_chars": MAX_REASON_CHARS,
            "max_metadata_chars": MAX_METADATA_CHARS,
        }

    def manifest(self) -> dict[str, Any]:
        project_policy, project_issues = self.project_policy()
        return {
            "schema": "grounded.hook_policy_manifest.v1",
            "supported_hooks": SUPPORTED_HOOKS,
            "supported_actions": SUPPORTED_ACTIONS,
            "supported_conditions": SUPPORTED_CONDITIONS,
            "caps": self.caps,
            "builtin_policy": self._builtin_policy.model_dump(mode="json", by_alias=True),
            "project_policy_path": str(self.project_policy_path) if self.project_policy_path else None,
            "project_policy": project_policy.model_dump(mode="json", by_alias=True),
            "validation_issues": [issue.model_dump(mode="json") for issue in project_issues],
            "policy_schema": HookPolicy.model_json_schema(),
            "evaluation_schema": HookEvaluation.model_json_schema(),
        }

    def workspace_policy_report(self, workspace_id: str) -> dict[str, Any]:
        policy, issues = self.workspace_policy(workspace_id)
        return {
            "schema": "grounded.workspace_hook_policy.v1",
            "workspace_id": workspace_id,
            "policy": policy.model_dump(mode="json", by_alias=True),
            "validation_issues": [issue.model_dump(mode="json") for issue in issues],
        }

    def update_workspace_policy(self, workspace_id: str, raw_policy: dict[str, Any]) -> dict[str, Any]:
        policy, issues = self._policy_from_raw(raw_policy, source="workspace", policy_id=f"workspace:{workspace_id}")
        payload = {
            "schema": "grounded.workspace_hook_policy.v1",
            "workspace_id": workspace_id,
            "policy": policy.model_dump(mode="json", by_alias=True),
            "validation_issues": [issue.model_dump(mode="json") for issue in issues],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", self.workspace_policy_ref(workspace_id), payload)
        return payload

    def evaluate(
        self,
        context: HookContext,
    ) -> HookEvaluation:
        policies, issues = self._policies_for_context(context)
        matched_rules: list[dict[str, Any]] = []
        contexts: list[HookContextItem] = []
        tags: dict[str, Any] = {}
        should_block = False
        block_reason: str | None = None
        for policy in policies:
            if not policy.enabled:
                continue
            for rule in sorted(policy.rules, key=lambda item: item.priority, reverse=True):
                if not rule.enabled or not self._matches(rule.conditions, context):
                    continue
                matched_rules.append(
                    {
                        "rule_id": rule.rule_id,
                        "source": rule.source,
                        "priority": rule.priority,
                        "actions": [action.kind for action in rule.actions],
                    }
                )
                for action in rule.actions:
                    if action.kind == "block":
                        should_block = True
                        if not block_reason:
                            block_reason = (action.reason or f"Hook rule {rule.rule_id} blocked this action.")[:MAX_REASON_CHARS]
                    elif action.kind == "add_context" and action.text:
                        contexts.append(
                            HookContextItem(
                                text=action.text[:MAX_CONTEXT_CHARS],
                                priority=action.priority,
                                target=action.target,
                                source=rule.source,
                                source_rule_id=rule.rule_id,
                                metadata=dict(action.metadata or {}),
                            )
                        )
                    elif action.kind == "tag":
                        tags[rule.rule_id] = dict(action.metadata or {})
        contexts = sorted(contexts, key=lambda item: item.priority, reverse=True)[:20]
        return HookEvaluation(
            trace_id=f"hook_eval_{uuid4().hex}",
            hook=context.hook,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            should_block=should_block,
            block_reason=block_reason,
            added_contexts=contexts,
            tags=tags,
            matched_rules=matched_rules,
            validation_issues=issues,
        )

    def workspace_policy_ref(self, workspace_id: str) -> str:
        return f"workspace_hooks:{workspace_id}"

    def project_policy(self) -> tuple[HookPolicy, list[HookValidationIssue]]:
        if self.project_policy_path is None or not self.project_policy_path.exists():
            return HookPolicy(policy_id="project", source="project", rules=[]), []
        try:
            raw = json.loads(self.project_policy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return HookPolicy(policy_id="project", source="project", rules=[]), [
                HookValidationIssue(source="project", code="policy_read_failed", message=str(exc), blocking=False)
            ]
        return self._policy_from_raw(raw, source="project", policy_id="project")

    def workspace_policy(self, workspace_id: str | None) -> tuple[HookPolicy, list[HookValidationIssue]]:
        if not workspace_id:
            return HookPolicy(policy_id="workspace", source="workspace", rules=[]), []
        record = self.store.get("reports", self.workspace_policy_ref(workspace_id)) or {}
        raw = record.get("policy") if isinstance(record.get("policy"), dict) else record
        return self._policy_from_raw(raw or {}, source="workspace", policy_id=f"workspace:{workspace_id}")

    def _policies_for_context(self, context: HookContext) -> tuple[list[HookPolicy], list[HookValidationIssue]]:
        project_policy, project_issues = self.project_policy()
        workspace_policy, workspace_issues = self.workspace_policy(context.workspace_id)
        return [self._builtin_policy, project_policy, workspace_policy], [*project_issues, *workspace_issues]

    def _policy_from_raw(self, raw_policy: dict[str, Any], *, source: str, policy_id: str) -> tuple[HookPolicy, list[HookValidationIssue]]:
        issues: list[HookValidationIssue] = []
        if not isinstance(raw_policy, dict):
            return HookPolicy(policy_id=policy_id, source=source, rules=[]), [
                HookValidationIssue(source=source, code="policy_invalid", message="Hook policy must be an object.")
            ]
        raw_rules = raw_policy.get("rules") if isinstance(raw_policy.get("rules"), list) else []
        if "rules" in raw_policy and not isinstance(raw_policy.get("rules"), list):
            issues.append(HookValidationIssue(source=source, code="rules_invalid", message="Hook policy rules must be a list."))
        if len(raw_rules) > MAX_RULES_PER_POLICY:
            issues.append(
                HookValidationIssue(
                    source=source,
                    code="rules_cap_exceeded",
                    message=f"Only the first {MAX_RULES_PER_POLICY} hook rules are evaluated.",
                )
            )
        rules: list[HookRule] = []
        for index, raw_rule in enumerate(raw_rules[:MAX_RULES_PER_POLICY]):
            rule, rule_issues = self._rule_from_raw(raw_rule, source=source, index=index)
            issues.extend(rule_issues)
            if rule is not None:
                rules.append(rule)
        try:
            policy = HookPolicy(
                policy_id=str(raw_policy.get("policy_id") or policy_id),
                source=source,
                enabled=bool(raw_policy.get("enabled", True)),
                rules=rules,
                metadata=raw_policy.get("metadata") if isinstance(raw_policy.get("metadata"), dict) else {},
            )
        except ValidationError as exc:
            issues.append(HookValidationIssue(source=source, code="policy_invalid", message=str(exc)))
            policy = HookPolicy(policy_id=policy_id, source=source, rules=rules)
        return policy, issues

    def _rule_from_raw(self, raw_rule: Any, *, source: str, index: int) -> tuple[HookRule | None, list[HookValidationIssue]]:
        issues: list[HookValidationIssue] = []
        if not isinstance(raw_rule, dict):
            return None, [HookValidationIssue(source=source, code="rule_invalid", message="Hook rule must be an object.", path=f"rules[{index}]")]
        normalized = dict(raw_rule)
        normalized["source"] = source
        normalized.setdefault("rule_id", f"{source}.rule_{index + 1}")
        normalized.setdefault("conditions", {})
        actions = normalized.get("actions")
        if actions is None and isinstance(normalized.get("action"), dict):
            actions = [normalized["action"]]
        normalized["actions"] = actions if isinstance(actions, list) else []
        if len(normalized["actions"]) > MAX_ACTIONS_PER_RULE:
            issues.append(
                HookValidationIssue(
                    source=source,
                    rule_id=str(normalized["rule_id"]),
                    code="actions_cap_exceeded",
                    message=f"Only the first {MAX_ACTIONS_PER_RULE} actions are evaluated.",
                    path=f"rules[{index}].actions",
                )
            )
            normalized["actions"] = normalized["actions"][:MAX_ACTIONS_PER_RULE]
        try:
            rule = HookRule.model_validate(normalized)
        except ValidationError as exc:
            return None, [
                HookValidationIssue(
                    source=source,
                    rule_id=str(normalized.get("rule_id") or f"{source}.rule_{index + 1}"),
                    code="rule_invalid",
                    message=str(exc),
                    path=f"rules[{index}]",
                )
            ]
        rule_issues = self._validate_rule(rule)
        if rule_issues:
            if any(issue.code == "secret_like_content" for issue in rule_issues):
                rule = rule.model_copy(
                    update={
                        "actions": [
                            action.model_copy(
                                update={
                                    "reason": "Invalid hook rule disabled." if action.kind == "block" else None,
                                    "text": None,
                                    "metadata": {},
                                }
                            )
                            for action in rule.actions
                        ]
                    }
                )
            rule = rule.model_copy(update={"enabled": False})
            issues.extend(rule_issues)
        return rule, issues

    def _validate_rule(self, rule: HookRule) -> list[HookValidationIssue]:
        issues: list[HookValidationIssue] = []
        if not rule.actions:
            issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="actions_missing", message="Hook rule must define at least one action."))
        for action in rule.actions:
            if action.kind == "block" and not (action.reason or "").strip():
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="block_reason_missing", message="Block actions require a reason."))
            if action.kind == "add_context" and not (action.text or "").strip():
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="context_missing", message="add_context actions require text."))
            if action.reason and len(action.reason) > MAX_REASON_CHARS:
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="reason_too_large", message="Block reason exceeds hook cap."))
            if action.text and len(action.text) > MAX_CONTEXT_CHARS:
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="context_too_large", message="Hook context exceeds hook cap."))
            metadata_size = len(json.dumps(action.metadata or {}, ensure_ascii=False, default=str))
            if metadata_size > MAX_METADATA_CHARS:
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="metadata_too_large", message="Hook action metadata exceeds hook cap."))
            secret_path = self._secret_path(action.model_dump(mode="json"))
            if secret_path:
                issues.append(HookValidationIssue(source=rule.source, rule_id=rule.rule_id, code="secret_like_content", message=f"Secret-like hook content rejected at {secret_path}."))
        return issues

    def _matches(self, condition: HookCondition, context: HookContext) -> bool:
        payload = context.payload or {}
        if condition.hook is not None and not self._matches_value(context.hook, condition.hook):
            return False
        if condition.tool is not None and not self._matches_value(str(payload.get("model_tool") or payload.get("tool") or ""), condition.tool):
            return False
        if condition.canonical_tool is not None and not self._matches_value(str(payload.get("tool") or payload.get("canonical_tool") or ""), condition.canonical_tool):
            return False
        if condition.risk is not None and not self._matches_value(str(payload.get("risk") or ""), condition.risk):
            return False
        if condition.mode is not None and not self._matches_value(str(payload.get("mode") or ""), condition.mode):
            return False
        if condition.worker_id is not None and not self._matches_value(str(payload.get("worker_id") or ""), condition.worker_id):
            return False
        if condition.run_intent is not None and not self._matches_value(str(payload.get("intent") or payload.get("run_intent") or ""), condition.run_intent):
            return False
        if condition.generation_mode is not None and not self._matches_value(str(payload.get("generation_mode") or ""), condition.generation_mode):
            return False
        paths = self._payload_paths(payload)
        if condition.path_globs and not any(self._path_matches(path, condition.path_globs) for path in paths):
            return False
        if condition.changed_files and not any(self._path_matches(path, condition.changed_files) for path in paths):
            return False
        check_names, check_statuses = self._payload_checks(payload)
        if condition.check_names and not any(self._matches_value(name, condition.check_names) for name in check_names):
            return False
        if condition.check_statuses and not any(self._matches_value(status, condition.check_statuses) for status in check_statuses):
            return False
        return True

    def _matches_value(self, actual: str, expected: str | list[str]) -> bool:
        normalized = actual.strip().lower()
        candidates = [expected] if isinstance(expected, str) else expected
        return any(normalized == str(candidate).strip().lower() for candidate in candidates)

    def _path_matches(self, path: str, globs: list[str]) -> bool:
        normalized = str(path).replace("\\", "/")
        return any(fnmatch(normalized, pattern.replace("\\", "/")) for pattern in globs)

    def _payload_paths(self, payload: dict[str, Any]) -> list[str]:
        values: list[Any] = []
        for key in ("paths", "changed_files", "files", "targets"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                values.extend(candidate)
            elif isinstance(candidate, str):
                values.append(candidate)
        input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        for key in ("file_path", "paths", "changed_files", "files", "targets"):
            candidate = input_payload.get(key)
            if isinstance(candidate, list):
                values.extend(candidate)
            elif isinstance(candidate, str):
                values.append(candidate)
        return [str(value).replace("\\", "/") for value in values if str(value or "").strip()]

    def _payload_checks(self, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
        checks = payload.get("checks") or payload.get("failed_checks") or []
        names: list[str] = []
        statuses: list[str] = []
        if isinstance(checks, list):
            for item in checks:
                if isinstance(item, dict):
                    names.append(str(item.get("name") or ""))
                    statuses.append(str(item.get("status") or ""))
                else:
                    names.append(str(item or ""))
        return [item for item in names if item], [item for item in statuses if item]

    def _secret_path(self, value: Any, *, path: str = "$") -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key)
                if SECRET_KEY_RE.search(re.sub(r"[^a-z0-9]+", "_", key_text.lower()).strip("_")):
                    return f"{path}.{key_text}"
                nested_path = self._secret_path(nested, path=f"{path}.{key_text}")
                if nested_path:
                    return nested_path
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                nested_path = self._secret_path(nested, path=f"{path}[{index}]")
                if nested_path:
                    return nested_path
        elif isinstance(value, str):
            for pattern in SECRET_VALUE_PATTERNS:
                if pattern.search(value):
                    return path
        return None
