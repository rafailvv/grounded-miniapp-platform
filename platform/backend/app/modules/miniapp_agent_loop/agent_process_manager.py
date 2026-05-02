from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable

from app.modules.miniapp_agent_loop.agent_command_policy import CommandPolicyDecision


DEFAULT_AGENT_ENV = {
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "PYTHONIOENCODING": "utf-8",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
}


@dataclass
class HeadTailOutputBuffer:
    max_chars: int
    head_chars: int = field(init=False)
    tail_chars: int = field(init=False)
    total_chars: int = 0
    chunk_count: int = 0
    _head: str = ""
    _tail: str = ""

    def __post_init__(self) -> None:
        budget = max(20, int(self.max_chars or 6000))
        self.head_chars = max(1, budget // 2)
        self.tail_chars = max(1, budget - self.head_chars)

    def append(self, text: str) -> None:
        if not text:
            return
        self.chunk_count += 1
        self.total_chars += len(text)
        if len(self._head) < self.head_chars:
            remaining = self.head_chars - len(self._head)
            self._head += text[:remaining]
        self._tail = (self._tail + text)[-self.tail_chars :]

    @property
    def omitted_chars(self) -> int:
        return max(0, self.total_chars - len(self._head) - len(self._tail))

    def excerpt(self) -> str:
        if self.omitted_chars <= 0:
            for overlap in range(min(len(self._head), len(self._tail)), -1, -1):
                if self._head.endswith(self._tail[:overlap]):
                    return self._head + self._tail[overlap:]
            return self._head + self._tail
        return f"{self._head}\n...[omitted {self.omitted_chars} chars]...\n{self._tail}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "head": self._head,
            "tail": self._tail,
            "total_chars": self.total_chars,
            "omitted_chars": self.omitted_chars,
            "chunk_count": self.chunk_count,
            "excerpt": self.excerpt(),
        }


@dataclass(frozen=True)
class AgentCommandResult:
    command: str
    argv: list[str]
    cwd: str
    started_at: str
    duration_ms: int
    exit_code: int | None
    semantic_status: str
    success: bool
    timed_out: bool
    timeout_seconds: int
    stdout: dict[str, Any]
    stderr: dict[str, Any]
    output_delta_count: int
    policy_decision: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "tool": "run_command",
            "command": self.command,
            "argv": self.argv,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "semantic_status": self.semantic_status,
            "success": self.success,
            "timed_out": self.timed_out,
            "timeout_seconds": self.timeout_seconds,
            "stdout": self.stdout.get("excerpt", ""),
            "stderr": self.stderr.get("excerpt", ""),
            "stdout_head": self.stdout.get("head", ""),
            "stdout_tail": self.stdout.get("tail", ""),
            "stderr_head": self.stderr.get("head", ""),
            "stderr_tail": self.stderr.get("tail", ""),
            "stdout_omitted_chars": self.stdout.get("omitted_chars", 0),
            "stderr_omitted_chars": self.stderr.get("omitted_chars", 0),
            "stdout_truncated": bool(self.stdout.get("omitted_chars", 0)),
            "stderr_truncated": bool(self.stderr.get("omitted_chars", 0)),
            "output_delta_count": self.output_delta_count,
            "policy_decision": self.policy_decision,
        }
        if self.error:
            payload["error"] = self.error
        return payload


class AgentCommandSemantics:
    """Map diagnostic command exit codes to agent-readable status."""

    @staticmethod
    def classify(*, argv: list[str], exit_code: int | None, timed_out: bool) -> tuple[str, bool]:
        if timed_out:
            return "timeout", False
        if exit_code is None:
            return "not_started", False
        executable = Path(argv[0]).name.lower() if argv else ""
        if exit_code == 0:
            return "passed", True
        if executable == "rg" and exit_code == 1:
            return "no_matches", True
        if executable == "diff" and exit_code == 1:
            return "differences_found", True
        return "failed", False


class AgentProcessManager:
    """Safe streaming process runner for agent diagnostic commands."""

    def __init__(self, *, deterministic_env: dict[str, str] | None = None) -> None:
        self.deterministic_env = dict(deterministic_env or DEFAULT_AGENT_ENV)

    def run(
        self,
        *,
        draft_source: Path,
        command: str,
        decision: CommandPolicyDecision,
        timeout_seconds: int,
        max_output_chars: int,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        yield_time_ms: int = 1000,
    ) -> AgentCommandResult:
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        stdout_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
        stderr_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
        output_delta_count = 0
        cwd = draft_source / "miniapp" if decision.cwd_policy == "miniapp" else draft_source
        policy_payload = self._policy_payload(decision)

        def emit(payload: dict[str, Any]) -> None:
            if progress_callback is not None:
                progress_callback(payload)

        if not decision.allowed:
            return AgentCommandResult(
                command=command,
                argv=list(decision.argv),
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="blocked_by_policy",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision=policy_payload,
                error=decision.reason,
            )

        if not decision.argv:
            return AgentCommandResult(
                command=command,
                argv=[],
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="not_started",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision=policy_payload,
                error="Command policy allowed the command but produced no argv.",
            )

        env = os.environ.copy()
        env.update(self.deterministic_env)
        if not cwd.exists():
            return AgentCommandResult(
                command=command,
                argv=list(decision.argv),
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="not_started",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision=policy_payload,
                error=f"Command cwd does not exist: {cwd}",
            )
        emit({"status": "started", "command": command, "argv": list(decision.argv), "cwd": str(cwd)})
        try:
            process = subprocess.Popen(
                list(decision.argv),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )
        except OSError as exc:
            return AgentCommandResult(
                command=command,
                argv=list(decision.argv),
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="not_started",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision=policy_payload,
                error=str(exc),
            )

        lock = threading.Lock()

        def consume(stream: Any, buffer: HeadTailOutputBuffer, stream_name: str) -> None:
            nonlocal output_delta_count
            try:
                for chunk in iter(stream.readline, ""):
                    if not chunk:
                        break
                    with lock:
                        buffer.append(chunk)
                        output_delta_count += 1
                    emit(
                        {
                            "status": "output_delta",
                            "stream": stream_name,
                            "chars": len(chunk),
                            "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        }
                    )
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threads = [
            threading.Thread(target=consume, args=(process.stdout, stdout_buffer, "stdout"), daemon=True)
            if process.stdout is not None
            else None,
            threading.Thread(target=consume, args=(process.stderr, stderr_buffer, "stderr"), daemon=True)
            if process.stderr is not None
            else None,
        ]
        for thread in threads:
            if thread is not None:
                thread.start()

        timed_out = False
        last_heartbeat = started
        while process.poll() is None:
            elapsed = time.perf_counter() - started
            if elapsed >= timeout_seconds:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            if (time.perf_counter() - last_heartbeat) * 1000 >= max(250, yield_time_ms):
                last_heartbeat = time.perf_counter()
                emit(
                    {
                        "status": "heartbeat",
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "output_delta_count": output_delta_count,
                    }
                )
            time.sleep(0.05)

        for thread in threads:
            if thread is not None:
                thread.join(timeout=1)

        exit_code = process.returncode
        semantic_status, success = AgentCommandSemantics.classify(
            argv=list(decision.argv),
            exit_code=exit_code,
            timed_out=timed_out,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        emit(
            {
                "status": "completed",
                "elapsed_ms": duration_ms,
                "exit_code": exit_code,
                "semantic_status": semantic_status,
                "success": success,
            }
        )
        return AgentCommandResult(
            command=command,
            argv=list(decision.argv),
            cwd=str(cwd),
            started_at=started_at,
            duration_ms=duration_ms,
            exit_code=exit_code,
            semantic_status=semantic_status,
            success=success,
            timed_out=timed_out,
            timeout_seconds=timeout_seconds,
            stdout=stdout_buffer.snapshot(),
            stderr=stderr_buffer.snapshot(),
            output_delta_count=output_delta_count,
            policy_decision=policy_payload,
            error=f"Command timed out after {timeout_seconds}s." if timed_out else None,
        )

    @staticmethod
    def _policy_payload(decision: CommandPolicyDecision) -> dict[str, Any]:
        return {
            "action": decision.action,
            "reason": decision.reason,
            "normalized_command": decision.normalized_command,
            "argv": list(decision.argv),
            "matched_prefix": list(decision.matched_prefix),
            "cwd_policy": decision.cwd_policy,
        }
