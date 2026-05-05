from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EDIT_FAILURE_CLASS = "generation.invalid_edit_operation"


def _clean_path(path: object) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_protected_target(path: str) -> bool:
    normalized = _clean_path(path)
    protected_exact = {
        "platform-state.json",
        "miniapp/app/routes/role_pages.py",
        "miniapp/app/routes/role_routes.py",
        "miniapp/app/generated/miniapp_contract.json",
        "miniapp/app/generated/route_manifest.json",
        "miniapp/app/generated/contract_validator.json",
    }
    protected_parts = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    parts = set(normalized.split("/"))
    return normalized in protected_exact or bool(parts.intersection(protected_parts))


def _default_target_files(file_path: str, evidence: dict[str, Any] | None = None) -> list[str]:
    paths: list[str] = []
    normalized = _clean_path(file_path)
    if normalized and not _is_protected_target(normalized):
        paths.append(normalized)
    if isinstance(evidence, dict):
        for key in ("target_files", "allowed_alternative_files", "paths", "changed_files"):
            value = evidence.get(key)
            if isinstance(value, list):
                for item in value:
                    path = _clean_path(item)
                    if path and not _is_protected_target(path) and path not in paths:
                        paths.append(path)
    return paths


def _severity_for_code(code: str) -> str:
    if code in {"unsafe_path", "protected_delete", "protected_path", "patch_conflict"}:
        return "critical"
    if code in {"stale_file", "file_not_read", "missing_patch_diff", "invalid_patch_diff", "duplicate_path"}:
        return "high"
    return "medium"


def _required_tool_for_code(code: str) -> str:
    if code in {"file_not_read", "stale_file", "old_string_not_found", "multiple_matches", "patch_conflict"}:
        return "read_files"
    if code in {"duplicate_path", "patch_envelope_in_content", "invalid_patch_diff", "invalid_patch_envelope_position"}:
        return "write_file"
    return "read_files"


def _suggested_tool_after_read(code: str) -> str:
    if code in {"duplicate_path", "patch_envelope_in_content", "protected_path", "protected_delete"}:
        return "write_file"
    return "apply_patch_to_draft_or_write_file"


def _forbidden_once_for_code(code: str) -> list[str]:
    if code in {"invalid_patch_diff", "missing_patch_diff", "patch_envelope_in_content", "patch_conflict"}:
        return ["apply_patch_to_draft"]
    return []


@dataclass(frozen=True)
class EditFailurePacket:
    code: str
    message: str
    failure_class: str = EDIT_FAILURE_CLASS
    failure_signature: str = ""
    severity: str = "high"
    retryable: bool = True
    deterministic: bool = True
    file_path: str = ""
    target_files: list[str] = field(default_factory=list)
    required_next_tool: str = "read_files"
    suggested_tool_after_read: str = "apply_patch_to_draft_or_write_file"
    forbidden_tools_once: list[str] = field(default_factory=list)
    forbidden_target_files: list[str] = field(default_factory=list)
    repair_recipe_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    repeated_count: int = 0

    @classmethod
    def from_edit_issue(
        cls,
        *,
        code: str,
        message: str,
        file_path: object = "",
        evidence: dict[str, Any] | None = None,
        attempt: int = 0,
        repeated_count: int = 0,
    ) -> "EditFailurePacket":
        normalized_path = _clean_path(file_path)
        target_files = _default_target_files(normalized_path, evidence)
        return cls(
            code=str(code or "invalid_edit_operation"),
            message=str(message or "Invalid edit operation."),
            failure_signature=f"{EDIT_FAILURE_CLASS}:{code or 'invalid_edit_operation'}",
            severity=_severity_for_code(str(code or "")),
            retryable=str(code or "") not in {"protected_delete", "protected_path"},
            deterministic=True,
            file_path=normalized_path,
            target_files=target_files,
            required_next_tool=_required_tool_for_code(str(code or "")),
            suggested_tool_after_read=_suggested_tool_after_read(str(code or "")),
            forbidden_tools_once=_forbidden_once_for_code(str(code or "")),
            forbidden_target_files=[
                _clean_path(item)
                for item in ((evidence or {}).get("forbidden_target_files") or (evidence or {}).get("blocked_files") or [])
                if _clean_path(item)
            ],
            repair_recipe_id=f"edit.{code or 'invalid_edit_operation'}",
            evidence=dict(evidence or {}),
            attempt=int(attempt or 0),
            repeated_count=int(repeated_count or 0),
        )

    def with_repeated_count(self, repeated_count: int) -> "EditFailurePacket":
        return EditFailurePacket(
            code=self.code,
            message=self.message,
            failure_class=self.failure_class,
            failure_signature=self.failure_signature,
            severity=self.severity,
            retryable=self.retryable,
            deterministic=self.deterministic,
            file_path=self.file_path,
            target_files=list(self.target_files),
            required_next_tool=self.required_next_tool,
            suggested_tool_after_read=self.suggested_tool_after_read,
            forbidden_tools_once=list(self.forbidden_tools_once),
            forbidden_target_files=list(self.forbidden_target_files),
            repair_recipe_id=self.repair_recipe_id,
            evidence=dict(self.evidence),
            attempt=self.attempt,
            repeated_count=int(repeated_count or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "failure_class": self.failure_class,
            "failure_signature": self.failure_signature,
            "severity": self.severity,
            "retryable": self.retryable,
            "deterministic": self.deterministic,
            "file_path": self.file_path,
            "target_files": list(self.target_files),
            "required_next_tool": self.required_next_tool,
            "suggested_tool_after_read": self.suggested_tool_after_read,
            "forbidden_tools_once": list(self.forbidden_tools_once),
            "forbidden_target_files": list(self.forbidden_target_files),
            "repair_recipe_id": self.repair_recipe_id,
            "evidence": dict(self.evidence),
            "attempt": self.attempt,
            "repeated_count": self.repeated_count,
        }


@dataclass(frozen=True)
class RepairTransitionDecision:
    active: bool = False
    reason: str = ""
    forced_tool_names: list[str] = field(default_factory=list)
    forced_targets: list[str] = field(default_factory=list)
    forbidden_tools_once: list[str] = field(default_factory=list)
    context_mode: str = "minimal"
    repair_focus: str = ""
    next_forced_action: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "forced_tool_names": list(self.forced_tool_names),
            "forced_targets": list(self.forced_targets),
            "forbidden_tools_once": list(self.forbidden_tools_once),
            "context_mode": self.context_mode,
            "repair_focus": self.repair_focus,
            "next_forced_action": dict(self.next_forced_action),
        }


class RepairTransitionPolicy:
    IMMEDIATE_FORCE_CODES = {
        "protected_path",
        "protected_delete",
    }
    FORCE_CODES = {
        "protected_path",
        "protected_delete",
        "file_not_read",
        "stale_file",
        "old_string_not_found",
        "multiple_matches",
        "invalid_patch_diff",
        "missing_patch_diff",
        "patch_envelope_in_content",
        "invalid_patch_envelope_position",
        "duplicate_path",
        "patch_conflict",
    }

    @classmethod
    def decide(
        cls,
        *,
        repair_packets: list[dict[str, Any]],
        repeated_failure_signatures: dict[str, int],
        latest_files_read: list[str],
    ) -> RepairTransitionDecision:
        packet = cls._first_forced_packet(repair_packets, repeated_failure_signatures)
        if not packet:
            return RepairTransitionDecision()
        targets = cls._targets(packet)
        read_set = {_clean_path(path) for path in latest_files_read}
        all_read = bool(targets) and all(target in read_set for target in targets)
        signature = str(packet.get("failure_signature") or packet.get("signature") or packet.get("code") or "")
        repeated_count = max(
            int(packet.get("repeated_count") or 0),
            int(repeated_failure_signatures.get(signature, 0) or 0),
        )
        if all_read:
            forbidden_once = {str(item) for item in packet.get("forbidden_tools_once") or []}
            if str(packet.get("code") or "") in {"patch_conflict", "protected_path", "protected_delete"} or "apply_patch_to_draft" in forbidden_once:
                forced_tools = ["write_file"]
            else:
                forced_tools = ["write_file", "apply_patch_to_draft"]
            phase = "write_after_forced_read"
            focus = (
                "The failing files were read after repeated edit failure. Patch only these target files now; "
                "use write_file if patch context is ambiguous."
            )
        else:
            forced_tools = ["read_files"]
            phase = "read_required_after_repeated_edit_failure"
            focus = "Read the exact target files before trying another edit. Do not write in this turn."
        next_action = {
            "phase": phase,
            "failure_signature": signature,
            "code": str(packet.get("code") or packet.get("issue_code") or ""),
            "target_files": targets,
            "required_next_tool": forced_tools[0],
            "allowed_tools": forced_tools,
            "repeated_count": repeated_count,
            "forbidden_target_files": [str(item) for item in packet.get("forbidden_target_files") or []],
        }
        return RepairTransitionDecision(
            active=True,
            reason=phase,
            forced_tool_names=forced_tools,
            forced_targets=targets,
            forbidden_tools_once=[str(item) for item in packet.get("forbidden_tools_once") or []],
            context_mode="expanded",
            repair_focus=focus,
            next_forced_action=next_action,
        )

    @classmethod
    def _first_forced_packet(
        cls,
        repair_packets: list[dict[str, Any]],
        repeated_failure_signatures: dict[str, int],
    ) -> dict[str, Any] | None:
        for packet in repair_packets:
            if not isinstance(packet, dict):
                continue
            code = str(packet.get("code") or packet.get("issue_code") or "")
            if code not in cls.FORCE_CODES:
                continue
            signature = str(packet.get("failure_signature") or packet.get("signature") or code)
            repeated_count = max(
                int(packet.get("repeated_count") or 0),
                int(repeated_failure_signatures.get(signature, 0) or 0),
            )
            if code in cls.IMMEDIATE_FORCE_CODES and cls._targets(packet) and repeated_count >= 1:
                return packet
            if repeated_count >= 2:
                return packet
        return None

    @staticmethod
    def _targets(packet: dict[str, Any]) -> list[str]:
        targets: list[str] = []
        for item in packet.get("target_files") or []:
            path = _clean_path(item)
            if path and not _is_protected_target(path) and path not in targets:
                targets.append(path)
        for item in packet.get("allowed_alternative_files") or []:
            path = _clean_path(item)
            if path and not _is_protected_target(path) and path not in targets:
                targets.append(path)
        path = _clean_path(packet.get("file_path") or "")
        if path and not _is_protected_target(path) and path not in targets:
            targets.append(path)
        return targets[:8]
