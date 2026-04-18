from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.modules.miniapp_agent_loop.fix_prompt_builder import FixPromptBuilder
from app.modules.miniapp_agent_loop.fix_types import FixPromptContext, FixTurnContext

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixPromptRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    @staticmethod
    def read_only_surfaces() -> list[str]:
        return FixPromptBuilder.read_only_surfaces()

    @staticmethod
    def expected_contract_snapshot(fix_turn: FixTurnContext) -> dict[str, Any]:
        return FixPromptBuilder.expected_contract_snapshot(fix_turn)

    @staticmethod
    def repair_context_mode(fix_turn: FixTurnContext, repeated_signature_without_progress: int) -> str:
        return FixPromptBuilder.repair_context_mode(fix_turn, repeated_signature_without_progress)

    @staticmethod
    def needs_full_context_first(fix_turn: FixTurnContext) -> bool:
        return FixPromptBuilder.needs_full_context_first(fix_turn)

    @staticmethod
    def previous_attempt_summary(fix_turn: FixTurnContext) -> str | None:
        return FixPromptBuilder.previous_attempt_summary(fix_turn)

    @staticmethod
    def normalized_critical_issues(
        results,
        fix_turn: FixTurnContext | None = None,
    ) -> list[dict[str, Any]]:
        return FixPromptBuilder.normalized_critical_issues(
            results,
            failure_class=fix_turn.failure_class if fix_turn is not None else None,
        )

    def repair_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["patch_ready", "tool_request", "no_progress", "fatal_invalid_response"],
                },
                "diagnosis": {"type": "string"},
                "tool_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tool": {"type": "string", "enum": ["list_files", "read_files", "run_checks", "search_files", "run_command"]},
                            "mode": {"type": "string", "enum": ["exact", "final"]},
                            "targets": {"type": "array", "items": {"type": "string"}},
                            "pattern": {"type": "string"},
                            "command": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["tool", "targets", "reason"],
                    },
                },
                "expected_verification": {"type": "string"},
                "rationale_by_file": {"type": "object", "additionalProperties": {"type": "string"}},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "file_path": {"type": "string"},
                            "operation": {"type": "string", "enum": ["create", "replace", "delete"]},
                            "content": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["file_path", "operation", "reason"],
                    },
                },
            },
            "required": ["diagnosis", "tool_requests", "expected_verification", "rationale_by_file", "operations"],
        }

    def repair_system_prompt(self) -> str:
        return self.service.fix_prompt_builder.repair_system_prompt()

    def repair_user_prompt(
        self,
        repair_packet: FixPromptContext,
        *,
        repair_feedback: str | None = None,
    ) -> str:
        return self.service.fix_prompt_builder.repair_user_prompt(repair_packet, repair_feedback=repair_feedback)

    @staticmethod
    def prompt_cache_key(repair_packet: FixPromptContext) -> str:
        return FixPromptBuilder.prompt_cache_key(repair_packet)
