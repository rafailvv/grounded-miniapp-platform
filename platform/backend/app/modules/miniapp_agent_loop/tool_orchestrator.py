from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable

from app.services.tool_protocol import structured_tool_error


GOVERNANCE_RETRY_ERROR_CODES = {
    "sandbox_preflight_blocked",
    "sandbox_denied",
    "blocked_by_sandbox",
    "policy_not_started",
}


@dataclass(frozen=True)
class ToolOrchestrationRequest:
    request: Any
    decision: Any
    protocol_input: dict[str, Any]
    workspace_id: str
    run_id: str


@dataclass(frozen=True)
class ToolAttemptResult:
    attempt: int
    status: str
    duration_ms: int
    error_code: str | None = None
    failure_class: str | None = None
    failure_signature: str | None = None
    retryable: bool = False
    governance_retry: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "failure_class": self.failure_class,
            "failure_signature": self.failure_signature,
            "retryable": self.retryable,
            "governance_retry": self.governance_retry,
        }


@dataclass(frozen=True)
class ToolOrchestrationResult:
    router_result: Any
    attempts: list[ToolAttemptResult] = field(default_factory=list)

    def attempt_payloads(self) -> list[dict[str, Any]]:
        return [attempt.as_dict() for attempt in self.attempts]


class ToolOrchestrator:
    """Central policy/trace wrapper around model-facing tool handlers.

    ToolRouter remains responsible for normalization, batching, and concrete
    handler implementations. This layer owns the common per-tool lifecycle:
    validation/approval hooks -> attempt -> governance retry metadata -> trace.
    """

    def __init__(
        self,
        *,
        emit_activity: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.emit_activity = emit_activity

    def run(
        self,
        orchestration_request: ToolOrchestrationRequest,
        *,
        schema_error: Callable[[], dict[str, Any] | None],
        pre_hook_error: Callable[[], dict[str, Any] | None],
        post_hook: Callable[[Any, bool], Any],
        execute: Callable[[], Any],
        failed_result: Callable[[dict[str, Any]], Any],
    ) -> ToolOrchestrationResult:
        request = orchestration_request.request
        decision = orchestration_request.decision
        attempts: list[ToolAttemptResult] = []
        max_attempts = self._max_attempts(decision)

        self._emit(
            "tool_orchestration_started",
            "Tool orchestration started",
            self._event_payload(orchestration_request, status="started", attempt=0, max_attempts=max_attempts),
        )

        validation_error = schema_error() if getattr(decision, "allowed", False) else None
        if validation_error is not None:
            result = failed_result(validation_error)
            attempts.append(self._attempt_from_result(result, attempt=1, duration_ms=0))
            result = self._finalize(orchestration_request, result, attempts)
            return ToolOrchestrationResult(router_result=result, attempts=attempts)

        if not getattr(decision, "allowed", False):
            result = failed_result(
                structured_tool_error(
                    code="tool_not_allowed",
                    message=getattr(decision, "reason", "Tool is not allowed."),
                    details={"tool": getattr(request, "tool", ""), "mode": orchestration_request.protocol_input.get("mode")},
                )
            )
            attempts.append(self._attempt_from_result(result, attempt=1, duration_ms=0))
            result = self._finalize(orchestration_request, result, attempts)
            return ToolOrchestrationResult(router_result=result, attempts=attempts)

        hook_error = pre_hook_error()
        if hook_error is not None:
            result = failed_result(hook_error)
            post_outcome = post_hook(result, True)
            self._attach_hook_context(result, post_outcome)
            attempts.append(self._attempt_from_result(result, attempt=1, duration_ms=0))
            result = self._finalize(orchestration_request, result, attempts)
            return ToolOrchestrationResult(router_result=result, attempts=attempts)

        final_result = None
        for attempt in range(1, max_attempts + 1):
            self._emit(
                "tool_attempt_started",
                "Tool attempt started",
                self._event_payload(orchestration_request, status="started", attempt=attempt, max_attempts=max_attempts),
            )
            started = time.perf_counter()
            try:
                result = execute()
            except Exception as exc:  # pragma: no cover - defensive boundary for arbitrary handlers.
                result = failed_result(
                    structured_tool_error(
                        code="tool_execution_error",
                        message=str(exc),
                        retryable=True,
                        details={"error_class": exc.__class__.__name__},
                    )
                )
            duration_ms = int((time.perf_counter() - started) * 1000)
            attempt_result = self._attempt_from_result(result, attempt=attempt, duration_ms=duration_ms)
            attempts.append(attempt_result)
            self._emit(
                "tool_attempt_completed",
                "Tool attempt completed",
                {
                    **self._event_payload(orchestration_request, status=attempt_result.status, attempt=attempt, max_attempts=max_attempts),
                    "duration_ms": duration_ms,
                    "error_code": attempt_result.error_code,
                    "failure_class": attempt_result.failure_class,
                    "failure_signature": attempt_result.failure_signature,
                },
            )
            final_result = result
            if not self._should_retry_governance(orchestration_request, attempt_result, attempt=attempt, max_attempts=max_attempts):
                break

        assert final_result is not None
        failed = str((getattr(final_result, "envelope", {}) or {}).get("status") or "") == "failed"
        post_outcome = post_hook(final_result, failed)
        self._attach_hook_context(final_result, post_outcome)
        final_result = self._finalize(orchestration_request, final_result, attempts)
        return ToolOrchestrationResult(router_result=final_result, attempts=attempts)

    @staticmethod
    def approval_payload(decision: Any, override: dict[str, Any] | None = None) -> dict[str, Any]:
        if override is not None:
            payload = dict(override)
            payload.setdefault("class", getattr(decision, "approval_class", "none"))
            payload.setdefault("policy", getattr(decision, "reason", ""))
            return payload
        approval_class = str(getattr(decision, "approval_class", "none") or "none")
        if approval_class == "policy":
            return {"required": False, "status": "policy_checked", "class": "policy", "policy": getattr(decision, "reason", "")}
        if approval_class == "human":
            return {"required": True, "status": "pending", "class": "human", "policy": getattr(decision, "reason", "")}
        if approval_class == "forbidden":
            return {"required": True, "status": "rejected", "class": "forbidden", "policy": getattr(decision, "reason", "")}
        return {"required": False, "status": "not_required", "class": "none"}

    def _finalize(self, orchestration_request: ToolOrchestrationRequest, result: Any, attempts: list[ToolAttemptResult]) -> Any:
        attempt_payloads = [attempt.as_dict() for attempt in attempts]
        envelope = getattr(result, "envelope", None)
        if isinstance(envelope, dict):
            retry = dict(envelope.get("retry") or {})
            retry["attempt"] = len(attempts)
            retry.setdefault("max_attempts", self._max_attempts(orchestration_request.decision))
            retry["attempts"] = attempt_payloads
            retry["governance_retry_only"] = True
            envelope["retry"] = retry
            envelope["orchestration"] = {
                "schema": "grounded.tool_orchestration.v1",
                "attempt_count": len(attempts),
                "attempts": attempt_payloads,
            }
            result_summary = envelope.get("result_summary")
            if isinstance(result_summary, dict):
                result_summary["orchestration"] = {
                    "attempt_count": len(attempts),
                    "last_status": attempts[-1].status if attempts else envelope.get("status"),
                }
            model_result = getattr(result, "model_result", None)
            if isinstance(model_result, dict):
                model_result["orchestration_attempts"] = attempt_payloads
                model_result["envelope"] = envelope
                model_result["tool_envelope"] = envelope
        final_status = str(envelope.get("status") if isinstance(envelope, dict) else (attempts[-1].status if attempts else "completed"))
        self._emit(
            "tool_orchestration_completed",
            "Tool orchestration completed",
            {
                **self._event_payload(
                    orchestration_request,
                    status=final_status,
                    attempt=len(attempts),
                    max_attempts=self._max_attempts(orchestration_request.decision),
                ),
                "attempt_count": len(attempts),
                "failure_class": envelope.get("failure_class") if isinstance(envelope, dict) else None,
                "failure_signature": envelope.get("failure_signature") if isinstance(envelope, dict) else None,
            },
        )
        return result

    @staticmethod
    def _attach_hook_context(result: Any, post_outcome: Any) -> None:
        if post_outcome is None or not getattr(post_outcome, "additional_contexts", None):
            return
        model_result = getattr(result, "model_result", None)
        if isinstance(model_result, dict):
            model_result["hook_contexts"] = list(post_outcome.additional_contexts)
            if hasattr(post_outcome, "as_dict"):
                model_result["hook_evaluation"] = post_outcome.as_dict()

    @staticmethod
    def _max_attempts(decision: Any) -> int:
        retry_policy = getattr(decision, "retry_policy", {}) if decision is not None else {}
        if not isinstance(retry_policy, dict):
            return 1
        try:
            return max(1, int(retry_policy.get("max_attempts") or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _attempt_from_result(result: Any, *, attempt: int, duration_ms: int) -> ToolAttemptResult:
        envelope = getattr(result, "envelope", None)
        if not isinstance(envelope, dict):
            return ToolAttemptResult(attempt=attempt, status="unknown", duration_ms=duration_ms)
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        status = str(envelope.get("status") or "completed")
        error_code = str(error.get("code") or "") or None
        retry = envelope.get("retry") if isinstance(envelope.get("retry"), dict) else {}
        return ToolAttemptResult(
            attempt=attempt,
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
            failure_class=str(envelope.get("failure_class") or error_code or "") or None,
            failure_signature=str(envelope.get("failure_signature") or "") or None,
            retryable=bool(error.get("retryable") or retry.get("retryable")),
            governance_retry=error_code in GOVERNANCE_RETRY_ERROR_CODES,
        )

    @staticmethod
    def _should_retry_governance(
        orchestration_request: ToolOrchestrationRequest,
        attempt_result: ToolAttemptResult,
        *,
        attempt: int,
        max_attempts: int,
    ) -> bool:
        if attempt >= max_attempts or not attempt_result.governance_retry:
            return False
        decision = orchestration_request.decision
        request = orchestration_request.request
        if bool(getattr(decision, "deferred", False)):
            return True
        if str(getattr(request, "canonical_tool", "")) == "shell.exec" and attempt_result.error_code == "policy_not_started":
            return True
        return False

    def _event_payload(
        self,
        orchestration_request: ToolOrchestrationRequest,
        *,
        status: str,
        attempt: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        request = orchestration_request.request
        decision = orchestration_request.decision
        return {
            "workspace_id": orchestration_request.workspace_id,
            "run_id": orchestration_request.run_id,
            "tool_use_id": getattr(request, "tool_call_id", ""),
            "tool": getattr(request, "tool", ""),
            "canonical_tool": getattr(request, "canonical_tool", ""),
            "approval_class": getattr(decision, "approval_class", ""),
            "sandbox_profile": getattr(decision, "sandbox_profile", ""),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "status": status,
        }

    def _emit(self, kind: str, label: str, details: dict[str, Any]) -> None:
        if self.emit_activity is not None:
            self.emit_activity(kind, label, details)
