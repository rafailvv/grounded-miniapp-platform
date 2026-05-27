from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class PatchGrammarValidator:
    """Strict, deterministic validator for generated patch envelopes."""

    HUNK_HEADER_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@(?:\s.*)?$")

    @classmethod
    def validate_operations(cls, patch_actions: list[Any]) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for operation in patch_actions:
            report = cls.validate_operation(operation)
            operations.append(report)
            issues.extend(report.get("issues") or [])
        patch_sha256 = cls.patch_sha256(patch_actions)
        return {
            "schema": "grounded.patch_validation.v1",
            "status": "passed" if not any(item.get("blocking", True) for item in issues) else "failed",
            "patch_sha256": patch_sha256,
            "operation_count": len(patch_actions),
            "operations": operations,
            "issues": issues,
            "deterministic": True,
            "grammar": {
                "unified_diff": "diff headers plus line-numbered @@ -old,+new @@ hunks",
                "codex_update_patch": "*** Begin Patch / *** Update File: <path> / @@ hunks / *** End Patch",
                "line_free_hunk": "@@ followed by +/-/space lines for one explicitly named file",
            },
        }

    @classmethod
    def validate_operation(cls, operation: Any) -> dict[str, Any]:
        operation_id = str(getattr(operation, "operation_id", "") or "")
        op = str(getattr(operation, "op", "") or "")
        file_path = cls._normalize_path(str(getattr(operation, "file_path", "") or ""))
        content = getattr(operation, "content", None)
        diff = str(getattr(operation, "diff", "") or "")
        issues: list[dict[str, Any]] = []
        if not file_path:
            issues.append(cls._issue("path_escape", "Patch path must stay within workspace.", operation_id=operation_id))
        if op in {"create", "update"} and content is None:
            issues.append(cls._issue("missing_content", f"Patch operation {operation_id} is missing content.", operation_id=operation_id, path=file_path))
        if op == "delete" and (content is not None or diff.strip()):
            issues.append(cls._issue("delete_with_payload", f"Delete operation {operation_id} must not include content or diff.", operation_id=operation_id, path=file_path))
        diff_kind = "none"
        paths: list[str] = []
        hunk_count = 0
        if op == "patch":
            if not diff.strip():
                issues.append(cls._issue("missing_diff", f"Patch operation {operation_id} is missing a diff.", operation_id=operation_id, path=file_path))
            else:
                parsed = cls.validate_diff(diff, expected_path=file_path, operation_id=operation_id)
                diff_kind = str(parsed.get("diff_kind") or "unknown")
                paths = [str(path) for path in parsed.get("paths") or []]
                hunk_count = int(parsed.get("hunk_count") or 0)
                issues.extend(parsed.get("issues") or [])
        return {
            "operation_id": operation_id,
            "op": op,
            "file_path": file_path,
            "status": "passed" if not any(item.get("blocking", True) for item in issues) else "failed",
            "diff_kind": diff_kind,
            "paths": paths,
            "hunk_count": hunk_count,
            "patch_sha256": cls.patch_sha256([operation]),
            "issues": issues,
        }

    @classmethod
    def validate_diff(cls, diff_text: str, *, expected_path: str, operation_id: str = "") -> dict[str, Any]:
        text = str(diff_text or "")
        issues: list[dict[str, Any]] = []
        if text.lstrip().startswith("*** Begin Patch"):
            parsed = cls._validate_codex_update_patch(text, expected_path=expected_path, operation_id=operation_id)
        elif cls._looks_line_free_hunk(text):
            parsed = cls._validate_line_free_hunk(text, expected_path=expected_path, operation_id=operation_id)
        else:
            parsed = cls._validate_unified_diff(text, expected_path=expected_path, operation_id=operation_id)
        issues.extend(parsed.get("issues") or [])
        if not parsed.get("paths"):
            issues.append(cls._issue("missing_patch_path", "Patch diff did not contain a target path.", operation_id=operation_id, path=expected_path))
        for path in parsed.get("paths") or []:
            if path != expected_path:
                issues.append(cls._issue("path_mismatch", f"Patch operation {operation_id} touched {path} outside {expected_path}.", operation_id=operation_id, path=path, evidence={"expected_path": expected_path}))
        if int(parsed.get("hunk_count") or 0) <= 0:
            issues.append(cls._issue("missing_hunk", "Patch diff must contain at least one hunk.", operation_id=operation_id, path=expected_path))
        return {**parsed, "issues": issues, "status": "passed" if not issues else "failed"}

    @classmethod
    def conflict_packet(cls, *, workspace_id: str, run_id: str | None, validation_report: dict[str, Any], conflict_reason: str | None = None) -> dict[str, Any]:
        issues = [item for item in validation_report.get("issues") or [] if isinstance(item, dict)]
        primary = issues[0] if issues else {}
        patch_sha256 = str(validation_report.get("patch_sha256") or "")
        return {
            "schema": "grounded.patch_conflict_packet.v1",
            "code": primary.get("code") or "patch_apply_conflict",
            "message": conflict_reason or primary.get("message") or "Patch could not be applied.",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "patch_sha256": patch_sha256,
            "issues": issues[:12],
            "operation_count": validation_report.get("operation_count"),
            "repair_hint": cls._repair_hint(str(primary.get("code") or "")),
            "forbidden_repeat_action": {"type": "same_patch_sha256", "sha256": patch_sha256} if patch_sha256 else None,
        }

    @classmethod
    def patch_sha256(cls, patch_actions: list[Any]) -> str:
        normalized = []
        for operation in patch_actions:
            normalized.append(
                {
                    "operation_id": str(getattr(operation, "operation_id", "") or ""),
                    "op": str(getattr(operation, "op", "") or ""),
                    "file_path": cls._normalize_path(str(getattr(operation, "file_path", "") or "")),
                    "content": getattr(operation, "content", None),
                    "diff": str(getattr(operation, "diff", "") or ""),
                    "precondition": getattr(operation, "precondition", None) or None,
                }
            )
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _validate_unified_diff(cls, text: str, *, expected_path: str, operation_id: str) -> dict[str, Any]:
        lines = text.splitlines()
        paths = cls._paths_from_unified_diff(lines)
        hunk_count = 0
        issues: list[dict[str, Any]] = []
        for line_no, line in enumerate(lines, start=1):
            if line.startswith("@@"):
                if not cls.HUNK_HEADER_RE.match(line):
                    issues.append(cls._issue("malformed_hunk_header", f"Malformed unified diff hunk header at line {line_no}.", operation_id=operation_id, path=expected_path, evidence={"line": line_no}))
                hunk_count += 1
                continue
            if hunk_count and line and not line.startswith((" ", "+", "-", "\\")):
                issues.append(cls._issue("malformed_hunk_line", f"Unified diff hunk line {line_no} must start with space, '+', '-', or '\\'.", operation_id=operation_id, path=expected_path, evidence={"line": line_no}))
        if not paths:
            issues.append(cls._issue("malformed_unified_diff", "Unified diff must include ---/+++ or diff --git file headers.", operation_id=operation_id, path=expected_path))
        return {"diff_kind": "unified_diff", "paths": paths, "hunk_count": hunk_count, "issues": issues}

    @classmethod
    def _validate_codex_update_patch(cls, text: str, *, expected_path: str, operation_id: str) -> dict[str, Any]:
        lines = text.splitlines()
        issues: list[dict[str, Any]] = []
        if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
            issues.append(cls._issue("malformed_codex_patch", "Codex patch must start with *** Begin Patch and end with *** End Patch.", operation_id=operation_id, path=expected_path))
        paths: list[str] = []
        hunk_count = 0
        for line in lines:
            if line.startswith("*** Update File: "):
                paths.append(cls._normalize_path(line.split(":", 1)[1]))
            elif line.startswith("*** Add File: ") or line.startswith("*** Delete File: "):
                issues.append(cls._issue("invalid_partial_edit", "Patch op accepts only Codex Update File hunks; use create/delete operations for file adds or deletes.", operation_id=operation_id, path=expected_path))
            elif line.startswith("@@"):
                hunk_count += 1
            elif line.startswith("***") and line not in {"*** Begin Patch", "*** End Patch"}:
                continue
        if len(paths) != 1:
            issues.append(cls._issue("malformed_codex_patch", "Codex update patch must target exactly one file.", operation_id=operation_id, path=expected_path))
        return {"diff_kind": "codex_update_patch", "paths": list(dict.fromkeys(paths)), "hunk_count": hunk_count, "issues": issues}

    @classmethod
    def _validate_line_free_hunk(cls, text: str, *, expected_path: str, operation_id: str) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        hunk_count = 0
        has_change = False
        in_hunk = False
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if line.startswith(("--- ", "+++ ", "diff --git ")):
                issues.append(cls._issue("invalid_partial_edit", "Line-free hunk patch must not include file headers.", operation_id=operation_id, path=expected_path, evidence={"line": line_no}))
                continue
            if line.startswith("@@"):
                in_hunk = True
                hunk_count += 1
                continue
            if not in_hunk:
                issues.append(cls._issue("invalid_partial_edit", "Line-free hunk patch must begin with @@.", operation_id=operation_id, path=expected_path, evidence={"line": line_no}))
                continue
            if line[0] not in {" ", "+", "-"}:
                issues.append(cls._issue("malformed_hunk_line", "Line-free hunk body lines must start with space, '+', or '-'.", operation_id=operation_id, path=expected_path, evidence={"line": line_no}))
            if line.startswith(("+", "-")):
                has_change = True
        if not has_change:
            issues.append(cls._issue("empty_patch", "Patch hunk must include at least one addition or deletion.", operation_id=operation_id, path=expected_path))
        return {"diff_kind": "line_free_hunk", "paths": [expected_path] if expected_path else [], "hunk_count": hunk_count, "issues": issues}

    @classmethod
    def _paths_from_unified_diff(cls, lines: list[str]) -> list[str]:
        paths: list[str] = []
        for line in lines:
            if line.startswith("diff --git "):
                for part in line.split()[2:4]:
                    candidate = cls._strip_diff_prefix(part)
                    if candidate:
                        paths.append(candidate)
                continue
            if line.startswith("--- ") or line.startswith("+++ "):
                candidate = line[4:].strip().split("\t", 1)[0].strip().strip('"')
                candidate = cls._strip_diff_prefix(candidate)
                if candidate:
                    paths.append(candidate)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _looks_line_free_hunk(text: str) -> bool:
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("--- ", "+++ ", "diff --git ")):
                return False
            return stripped.startswith("@@") and PatchGrammarValidator.HUNK_HEADER_RE.match(stripped) is None
        return False

    @classmethod
    def _normalize_path(cls, value: str) -> str:
        path = str(value or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path.startswith(("source/", "draft/")):
            path = path.split("/", 1)[1]
        if not path or path.startswith("/") or path.startswith("~") or ".." in Path(path).parts or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
            return ""
        return path

    @classmethod
    def _strip_diff_prefix(cls, value: str) -> str:
        candidate = str(value or "").strip().strip('"')
        if candidate in {"/dev/null", "dev/null"}:
            return ""
        return cls._normalize_path(candidate)

    @staticmethod
    def _issue(code: str, message: str, *, operation_id: str = "", path: str = "", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "severity": "high",
            "operation_id": operation_id,
            "path": path,
            "blocking": True,
            "evidence": evidence or {},
        }

    @staticmethod
    def _repair_hint(code: str) -> str:
        mapping = {
            "path_escape": "Use a relative workspace path without '..', '~', or absolute prefixes.",
            "missing_diff": "Send a complete unified diff or a supported Codex update patch.",
            "missing_patch_path": "Include file headers or use apply_patch_to_draft with the exact file_path.",
            "path_mismatch": "Split edits so each patch operation touches only its declared file_path.",
            "malformed_hunk_header": "Use numbered unified diff hunk headers such as @@ -12,3 +12,4 @@.",
            "invalid_partial_edit": "Use create/update/delete operations for whole-file changes and patch operations only for focused hunks.",
            "empty_patch": "Include at least one changed line.",
        }
        return mapping.get(code, "Read the current file, regenerate a smaller patch against that exact content, and do not repeat the same patch hash.")
