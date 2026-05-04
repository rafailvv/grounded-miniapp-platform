from __future__ import annotations

import re

from app.models.domain import DraftAction
from app.modules.miniapp_agent_loop.repair_packets import EditFailurePacket
from app.modules.miniapp_agent_loop.types import AgentTurnPlan


class AgentEditValidator:
    PATCH_ENVELOPE_MARKERS = ("*** Begin Patch", "*** End Patch", "*** Add File:", "*** Delete File:", "*** Update File:")
    MAX_FILE_CHANGE_COUNT = 80
    MAX_SINGLE_CONTENT_CHARS = 260_000
    MAX_TOTAL_CONTENT_CHARS = 900_000
    PROTECTED_PATHS = (
        ".git/",
        "node_modules/",
        "__pycache__/",
        ".venv/",
        "venv/",
        "platform-state.json",
        "miniapp/app/routes/role_pages.py",
        "miniapp/app/routes/role_routes.py",
        "miniapp/app/routes/generated_contract.py",
        "miniapp/app/generated/miniapp_contract.json",
        "miniapp/app/generated/api_client.js",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/generated/contract_validator.json",
        "miniapp/tests/test_generated_app.py",
        "miniapp/tests/generated_app.test.mjs",
    )

    @staticmethod
    def normalize_plan(plan: AgentTurnPlan) -> AgentTurnPlan:
        if plan.outcome == "changes_ready" and not plan.file_changes:
            plan.outcome = "no_op"
        if plan.outcome == "changes_ready":
            issue = AgentEditValidator._first_invalid_file_change(plan.file_changes)
            if issue:
                code, message = issue
                packet = AgentEditValidator.repair_packet_for_issue(
                    code=code,
                    message=message,
                    file_changes=plan.file_changes,
                )
                plan.outcome = "fatal_invalid_response"
                plan.diagnosis = message
                plan.failure_class = "generation.invalid_edit_operation"
                plan.failure_signature = f"generation.invalid_edit_operation:{code}"
                plan.root_cause_summary = message
                plan.fix_targets = list(packet.get("target_files") or [])
                plan.metadata = {
                    **dict(plan.metadata or {}),
                    "repair_packets": [packet],
                    "invalid_edit_code": code,
                }
                plan.file_changes = []
        return plan

    @classmethod
    def repair_packet_for_issue(
        cls,
        *,
        code: str,
        message: str,
        file_changes: list[DraftAction],
        attempt: int = 0,
        repeated_count: int = 0,
        evidence: dict[str, object] | None = None,
    ) -> dict[str, object]:
        file_path = cls._best_issue_path(code, file_changes)
        payload = {
            "operation_count": len(file_changes),
            "operations": [
                {
                    "file_path": cls._strip_leading_dot_slash(getattr(item, "file_path", "")),
                    "operation": str(getattr(item, "operation", "") or ""),
                    "has_content": bool(getattr(item, "content", None)),
                    "has_diff": bool(getattr(item, "diff", None)),
                }
                for item in file_changes[:8]
            ],
        }
        if evidence:
            payload.update(evidence)
        return EditFailurePacket.from_edit_issue(
            code=code,
            message=message,
            file_path=file_path,
            evidence=payload,
            attempt=attempt,
            repeated_count=repeated_count,
        ).as_dict()

    @classmethod
    def repair_packet_for_apply_conflict(
        cls,
        *,
        conflict_reason: str,
        file_changes: list[DraftAction],
        attempt: int = 0,
        repeated_count: int = 0,
    ) -> dict[str, object]:
        return cls.repair_packet_for_issue(
            code="patch_conflict",
            message=conflict_reason or "Patch did not apply to the current draft.",
            file_changes=file_changes,
            attempt=attempt,
            repeated_count=repeated_count,
            evidence={"conflict_reason": conflict_reason or ""},
        )

    @classmethod
    def repair_packet_for_read_state(
        cls,
        *,
        status: str,
        file_path: str,
        message: str,
        attempt: int = 0,
        repeated_count: int = 0,
        evidence: dict[str, object] | None = None,
    ) -> dict[str, object]:
        code = "stale_file" if status == "stale" else "file_not_read" if status == "unread" else status
        return EditFailurePacket.from_edit_issue(
            code=code,
            message=message,
            file_path=file_path,
            evidence=evidence or {"freshness": status},
            attempt=attempt,
            repeated_count=repeated_count,
        ).as_dict()

    @classmethod
    def _best_issue_path(cls, code: str, file_changes: list[DraftAction]) -> str:
        seen: set[str] = set()
        for operation in file_changes:
            raw_path = cls._strip_leading_dot_slash(getattr(operation, "file_path", ""))
            normalized = cls._normalize_path(raw_path)
            path = normalized or raw_path
            if code == "duplicate_path":
                if path in seen:
                    return path
                seen.add(path)
                continue
            if path:
                return path
        return ""

    @classmethod
    def _first_invalid_file_change(cls, file_changes: list[DraftAction]) -> tuple[str, str] | None:
        if len(file_changes) > cls.MAX_FILE_CHANGE_COUNT:
            return (
                "too_many_file_changes",
                f"Turn returned {len(file_changes)} file changes; split the edit into smaller patch-first tool calls.",
            )
        total_chars = 0
        seen_paths: set[str] = set()
        for operation in file_changes:
            normalized_path = cls._normalize_path(getattr(operation, "file_path", ""))
            if not normalized_path:
                return ("unsafe_path", "Every file change must target a relative path inside miniapp/.")
            if normalized_path in seen_paths:
                return ("duplicate_path", f"{normalized_path} was edited more than once in the same turn; merge it into one operation.")
            seen_paths.add(normalized_path)
            op = str(getattr(operation, "operation", "") or "").strip().lower()
            if op not in {"create", "replace", "delete", "patch"}:
                return ("unknown_operation", f"{normalized_path} uses unsupported operation '{op}'.")
            content = getattr(operation, "content", None)
            diff = getattr(operation, "diff", None)
            if op in {"create", "replace"}:
                text = "" if content is None else str(content)
                if not text:
                    return ("missing_content", f"{normalized_path} {op} operation must include file content.")
                if cls._role_page_neutral_template_issue(normalized_path, text):
                    return (
                        "neutral_role_template_write",
                        f"{normalized_path} writes the platform neutral starter page back into a generated role app. Patch the prompt-derived workflow UI instead.",
                    )
                if any(marker in text for marker in cls.PATCH_ENVELOPE_MARKERS):
                    return (
                        "patch_envelope_in_content",
                        f"{normalized_path} was returned as {op} content containing patch envelope markers. Return raw file content, or use operation='patch' with a diff.",
                    )
                issue = cls._validate_size(normalized_path, text)
                if issue:
                    return issue
                total_chars += len(text)
            elif op == "patch":
                patch_text = str(diff or content or "")
                if not patch_text.strip():
                    return ("missing_patch_diff", f"{normalized_path} patch operation must include a unified diff.")
                if cls._role_page_neutral_template_issue(normalized_path, patch_text):
                    return (
                        "neutral_role_template_patch",
                        f"{normalized_path} patch would restore neutral starter role content. Patch the prompt-derived workflow UI instead.",
                    )
                if patch_text.lstrip().startswith("*** Begin Patch"):
                    issue = cls._validate_size(normalized_path, patch_text)
                    if issue:
                        return issue
                    total_chars += len(patch_text)
                    continue
                if any(marker in patch_text for marker in cls.PATCH_ENVELOPE_MARKERS):
                    return (
                        "invalid_patch_envelope_position",
                        f"{normalized_path} patch operation contains patch envelope markers outside a Codex-style update patch.",
                    )
                if not cls._looks_like_unified_diff(patch_text):
                    return (
                        "invalid_patch_diff",
                        f"{normalized_path} patch operation must include unified-diff hunks with @@ context and +/- lines.",
                    )
                issue = cls._validate_size(normalized_path, patch_text)
                if issue:
                    return issue
                total_chars += len(patch_text)
            elif op == "delete" and cls._is_protected_path(normalized_path):
                return ("protected_delete", f"{normalized_path} is protected and cannot be deleted by the agent loop.")
        if total_chars > cls.MAX_TOTAL_CONTENT_CHARS:
            return (
                "oversized_turn",
                f"Turn returned {total_chars} characters of edits; use compact patch-first changes and continue in another turn.",
            )
        return None

    @classmethod
    def _normalize_path(cls, raw_path: object) -> str:
        path = cls._strip_leading_dot_slash(raw_path)
        if not path or path.startswith("/") or path.startswith("~"):
            return ""
        if "\x00" in path or ".." in path.split("/"):
            return ""
        if not path.startswith("miniapp/"):
            return ""
        if cls._is_protected_path(path):
            return ""
        return path

    @classmethod
    def _is_protected_path(cls, path: str) -> bool:
        normalized = cls._strip_leading_dot_slash(path)
        parts = set(normalized.split("/"))
        protected_dirs = {item.strip("/") for item in cls.PROTECTED_PATHS if item.endswith("/")}
        if parts.intersection(protected_dirs):
            return True
        return any(not item.endswith("/") and (normalized == item or normalized.endswith(f"/{item}")) for item in cls.PROTECTED_PATHS)

    @staticmethod
    def _strip_leading_dot_slash(raw_path: object) -> str:
        path = str(raw_path or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path

    @classmethod
    def _validate_size(cls, path: str, text: str) -> tuple[str, str] | None:
        if len(text) > cls.MAX_SINGLE_CONTENT_CHARS:
            return (
                "oversized_file_operation",
                f"{path} operation is {len(text)} characters; split it into smaller files or patch hunks.",
            )
        return None

    @staticmethod
    def _role_page_neutral_template_issue(path: str, text: str) -> bool:
        normalized = str(path or "").replace("\\", "/")
        if not re.fullmatch(r"miniapp/app/static/(client|specialist|manager)/index\.html", normalized):
            return False
        lowered = str(text or "").lower()
        markers = (
            "neutral starter",
            "should be replaced by the generated app",
            "client surface",
            "specialist surface",
            "manager surface",
            "preview entry",
        )
        return sum(1 for marker in markers if marker in lowered) >= 2

    @staticmethod
    def _looks_like_unified_diff(diff: str) -> bool:
        text = str(diff or "")
        if "@@" not in text:
            return False
        has_removed_or_context = bool(re.search(r"(?m)^[- ][^\n]*", text))
        has_added_or_context = bool(re.search(r"(?m)^[+ ][^\n]*", text))
        return has_removed_or_context and has_added_or_context
