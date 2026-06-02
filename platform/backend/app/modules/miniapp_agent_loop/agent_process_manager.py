from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from app.models.sandbox import SandboxExecutionPlan, SandboxKillDiagnostics, SandboxLogCapture, SandboxLogStreamCapture
from app.modules.miniapp_agent_loop.agent_command_policy import CommandPolicyDecision
from app.services.command_canonicalizer import CommandCanonicalizer
from app.services.sandbox_service import SandboxService


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

DEFAULT_RESOURCE_LIMITS = {
    "cpu_seconds": int(os.getenv("AGENT_EXEC_CPU_SECONDS", "120")),
    "address_space_bytes": int(os.getenv("AGENT_EXEC_ADDRESS_SPACE_BYTES", str(1024 * 1024 * 1024))),
    "file_size_bytes": int(os.getenv("AGENT_EXEC_FILE_SIZE_BYTES", str(128 * 1024 * 1024))),
    "open_files": int(os.getenv("AGENT_EXEC_OPEN_FILES", "256")),
}

COMPLETED_PROCESS_RETENTION = int(os.getenv("AGENT_EXEC_PROCESS_RETENTION", "128"))


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


@dataclass
class BoundedOutputSpool:
    max_chars: int = int(os.getenv("AGENT_EXEC_FULL_OUTPUT_CHARS", "2000000"))
    total_chars: int = 0
    truncated_full: bool = False
    _parts: list[str] = field(default_factory=list)
    _sha256: Any = field(default_factory=hashlib.sha256)

    def append(self, text: str) -> None:
        if not text:
            return
        self.total_chars += len(text)
        self._sha256.update(text.encode("utf-8", errors="replace"))
        current = sum(len(part) for part in self._parts)
        remaining = max(0, self.max_chars - current)
        if remaining:
            self._parts.append(text[:remaining])
        if len(text) > remaining:
            self.truncated_full = True

    @property
    def content(self) -> str:
        return "".join(self._parts)

    @property
    def sha256(self) -> str:
        return self._sha256.hexdigest()


@dataclass(frozen=True)
class AgentCommandResult:
    process_id: str
    command: str
    argv: list[str]
    resolved_argv: list[str]
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
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    output_artifacts: list[dict[str, Any]] = field(default_factory=list)
    command_canonical: dict[str, Any] = field(default_factory=dict)
    execution_classification: dict[str, Any] = field(default_factory=dict)
    sandbox_boundary: dict[str, Any] = field(default_factory=dict)
    environment_snapshot: dict[str, Any] = field(default_factory=dict)
    log_capture: dict[str, Any] = field(default_factory=dict)
    killed_diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "tool": "run_command",
            "process_id": self.process_id,
            "command": self.command,
            "argv": self.argv,
            "resolved_argv": self.resolved_argv,
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
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "output_artifacts": list(self.output_artifacts),
            "command_canonical": dict(self.command_canonical),
            "execution_classification": dict(self.execution_classification),
            "output_delta_count": self.output_delta_count,
            "policy_decision": self.policy_decision,
        }
        if self.sandbox_boundary:
            payload["sandbox_boundary"] = self.sandbox_boundary
        if self.environment_snapshot:
            payload["environment_snapshot"] = self.environment_snapshot
        if self.log_capture:
            payload["log_capture"] = self.log_capture
        if self.killed_diagnostics:
            payload["killed_diagnostics"] = self.killed_diagnostics
        if self.error:
            payload["error"] = self.error
        return payload


class AgentCommandSemantics:
    """Map diagnostic command exit codes to agent-readable status."""

    @staticmethod
    def classify(
        *,
        command: str,
        argv: list[str],
        exit_code: int | None,
        timed_out: bool,
        stdout: str = "",
        stderr: str = "",
        workspace_id: str | None = None,
        semantic_status: str | None = None,
    ) -> tuple[str, bool, dict[str, Any], dict[str, Any]]:
        canonical, classification = CommandCanonicalizer.classify_execution(
            command=command,
            argv=argv,
            workspace_id=workspace_id,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            semantic_status=semantic_status,
        )
        return (
            str(classification.get("semantic_status") or "unknown_failure"),
            bool(classification.get("success")),
            canonical,
            classification,
        )


class AgentProcessManager:
    """Safe streaming process runner for agent diagnostic commands."""

    def __init__(
        self,
        *,
        deterministic_env: dict[str, str] | None = None,
        resource_limits: dict[str, int] | None = None,
        sandbox_service: SandboxService | None = None,
    ) -> None:
        self.deterministic_env = dict(deterministic_env or DEFAULT_AGENT_ENV)
        self.resource_limits = dict(resource_limits or DEFAULT_RESOURCE_LIMITS)
        self.sandbox_service = sandbox_service or SandboxService()
        self._active: dict[str, subprocess.Popen[str]] = {}
        self._active_meta: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def run(
        self,
        *,
        draft_source: Path,
        command: str,
        decision: CommandPolicyDecision,
        timeout_seconds: int,
        max_output_chars: int,
        workspace_id: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        yield_time_ms: int = 1000,
        process_id: str | None = None,
        execution_plan: SandboxExecutionPlan | None = None,
        output_artifact_writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> AgentCommandResult:
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        resolved_process_id = str(process_id or f"proc_{uuid4().hex[:12]}")
        stdout_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
        stderr_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
        stdout_spool = BoundedOutputSpool()
        stderr_spool = BoundedOutputSpool()
        output_delta_count = 0
        cwd = draft_source / "miniapp" if decision.cwd_policy == "miniapp" else draft_source
        base_exec_argv = list(decision.resolved_argv or decision.argv)
        plan = execution_plan or self.sandbox_service.build_execution_plan(
            root=draft_source,
            cwd=cwd,
            argv=base_exec_argv,
            profile="analysis_readonly",
            network_mode="blocked",
            write_roots=[cwd / ".sandbox" / "tmp", cwd / ".sandbox" / "home"],
        )
        exec_argv = list(plan.wrapped_argv or base_exec_argv)
        policy_payload = self._policy_payload(decision)
        command_canonical = CommandCanonicalizer.canonicalize(
            command,
            argv=list(decision.argv),
            workspace_id=workspace_id,
            status_taxonomy="not_started",
        )
        execution_classification = {
            "schema": "grounded.command_execution_classification.v1",
            "command_family": command_canonical.get("command_family"),
            "status_taxonomy": "not_started",
            "semantic_status": "not_started",
            "success": False,
            "retry_recipe_id": command_canonical.get("retry_recipe_id"),
            "failure_hint": "",
        }
        sandbox_summary = self._sandbox_summary(draft_source=draft_source, cwd=cwd, decision=decision, execution_plan=plan)
        planned_env, tmp_dir, home_dir = self._sandbox_env(cwd, create_dirs=False)
        environment_snapshot_model = self.sandbox_service.environment_snapshot(
            process_id=resolved_process_id,
            root=draft_source,
            cwd=cwd,
            argv=list(decision.argv),
            resolved_argv=base_exec_argv,
            wrapped_argv=exec_argv,
            env=planned_env,
            tmp_dir=tmp_dir,
            home_dir=home_dir,
            resource_limits=dict(self.resource_limits),
            execution_plan=plan,
        )
        environment_snapshot = environment_snapshot_model.model_dump(mode="json", by_alias=True)
        sandbox_boundary = self.sandbox_service.runtime_boundary(
            process_id=resolved_process_id,
            root=draft_source,
            cwd=cwd,
            execution_plan=plan,
            timeout_seconds=timeout_seconds,
            resource_limits=dict(self.resource_limits),
            environment=environment_snapshot_model,
            max_excerpt_chars=max_output_chars,
            full_spool_max_chars=stdout_spool.max_chars,
        ).model_dump(mode="json", by_alias=True)
        log_capture = self.sandbox_service.empty_log_capture(
            max_excerpt_chars=max_output_chars,
            full_spool_max_chars=stdout_spool.max_chars,
        ).model_dump(mode="json", by_alias=True)

        def policy_with_runtime() -> dict[str, Any]:
            return {
                **policy_payload,
                "sandbox": sandbox_summary,
                "sandbox_boundary": sandbox_boundary,
                "environment_snapshot": environment_snapshot,
            }

        def early_result(*, semantic_status: str, error: str, kill_reason: str = "not_started") -> AgentCommandResult:
            nonlocal command_canonical, execution_classification
            status_override = (
                "policy_blocked"
                if semantic_status == "blocked_by_policy"
                else "sandbox_blocked"
                if semantic_status == "blocked_by_sandbox"
                else semantic_status
            )
            command_canonical, execution_classification = CommandCanonicalizer.classify_execution(
                command=command,
                argv=list(decision.argv),
                workspace_id=workspace_id,
                exit_code=None,
                timed_out=False,
                semantic_status=status_override,
            )
            diagnostics = self._kill_diagnostics(
                reason=kill_reason,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                exit_code=None,
                detail=error,
            )
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status=semantic_status,
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision=policy_with_runtime(),
                error=error,
                sandbox_boundary=sandbox_boundary,
                environment_snapshot=environment_snapshot,
                log_capture=log_capture,
                killed_diagnostics=diagnostics,
                command_canonical=command_canonical,
                execution_classification=execution_classification,
            )

        def emit(payload: dict[str, Any]) -> None:
            if progress_callback is not None:
                progress_callback(payload)

        if not decision.allowed:
            return early_result(semantic_status="blocked_by_policy", error=decision.reason, kill_reason="policy_blocked")

        if not decision.argv:
            return early_result(semantic_status="not_started", error="Command policy allowed the command but produced no argv.")

        if not self._cwd_inside_workspace(draft_source, cwd):
            return early_result(semantic_status="blocked_by_sandbox", error=f"Command cwd escapes workspace: {cwd}", kill_reason="sandbox_blocked")
        escaped_arg = self._first_workspace_escaping_arg(draft_source, cwd, list(decision.argv))
        if escaped_arg:
            return early_result(semantic_status="blocked_by_sandbox", error=f"Command argument escapes workspace allowlist: {escaped_arg}", kill_reason="sandbox_blocked")
        if not plan.allowed:
            return early_result(semantic_status="blocked_by_sandbox", error=plan.reason, kill_reason="sandbox_blocked")

        if not cwd.exists():
            return early_result(semantic_status="not_started", error=f"Command cwd does not exist: {cwd}")
        env, tmp_dir, home_dir = self._sandbox_env(cwd, create_dirs=True)
        environment_snapshot_model = self.sandbox_service.environment_snapshot(
            process_id=resolved_process_id,
            root=draft_source,
            cwd=cwd,
            argv=list(decision.argv),
            resolved_argv=base_exec_argv,
            wrapped_argv=exec_argv,
            env=env,
            tmp_dir=tmp_dir,
            home_dir=home_dir,
            resource_limits=dict(self.resource_limits),
            execution_plan=plan,
        )
        environment_snapshot = environment_snapshot_model.model_dump(mode="json", by_alias=True)
        sandbox_boundary = self.sandbox_service.runtime_boundary(
            process_id=resolved_process_id,
            root=draft_source,
            cwd=cwd,
            execution_plan=plan,
            timeout_seconds=timeout_seconds,
            resource_limits=dict(self.resource_limits),
            environment=environment_snapshot_model,
            max_excerpt_chars=max_output_chars,
            full_spool_max_chars=stdout_spool.max_chars,
        ).model_dump(mode="json", by_alias=True)
        try:
            process = subprocess.Popen(
                exec_argv,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
                start_new_session=True,
                preexec_fn=self._preexec_fn(),
            )
        except OSError as exc:
            return early_result(semantic_status="not_started", error=str(exc), kill_reason="startup_failed")
        environment_snapshot_model = self.sandbox_service.environment_snapshot(
            process_id=resolved_process_id,
            host_pid=process.pid,
            root=draft_source,
            cwd=cwd,
            argv=list(decision.argv),
            resolved_argv=base_exec_argv,
            wrapped_argv=exec_argv,
            env=env,
            tmp_dir=tmp_dir,
            home_dir=home_dir,
            resource_limits=dict(self.resource_limits),
            execution_plan=plan,
        )
        environment_snapshot = environment_snapshot_model.model_dump(mode="json", by_alias=True)
        sandbox_boundary = self.sandbox_service.runtime_boundary(
            process_id=resolved_process_id,
            root=draft_source,
            cwd=cwd,
            execution_plan=plan,
            timeout_seconds=timeout_seconds,
            resource_limits=dict(self.resource_limits),
            environment=environment_snapshot_model,
            max_excerpt_chars=max_output_chars,
            full_spool_max_chars=stdout_spool.max_chars,
        ).model_dump(mode="json", by_alias=True)
        emit(
            {
                "status": "started",
                "process_id": resolved_process_id,
                "command": command,
                "argv": list(decision.argv),
                "resolved_argv": exec_argv,
                "cwd": str(cwd),
                "command_canonical": command_canonical,
                "execution_classification": execution_classification,
                "sandbox": sandbox_summary,
                "sandbox_boundary": sandbox_boundary,
                "environment_snapshot": environment_snapshot,
                "log_capture": log_capture,
            }
        )
        with self._lock:
            self._active[resolved_process_id] = process
            self._active_meta[resolved_process_id] = {
                "process_id": resolved_process_id,
                "command": command,
                "argv": list(decision.argv),
                "resolved_argv": exec_argv,
                "cwd": str(cwd),
                "started_at": started_at,
                "status": "running",
                "stdout": stdout_buffer.snapshot(),
                "stderr": stderr_buffer.snapshot(),
                "stdout_content": "",
                "stderr_content": "",
                "output_delta_count": 0,
                "command_canonical": command_canonical,
                "execution_classification": execution_classification,
                "sandbox": sandbox_summary,
                "sandbox_boundary": sandbox_boundary,
                "environment_snapshot": environment_snapshot,
                "log_capture": log_capture,
                "network_mode": sandbox_summary["network_mode"],
            }

        lock = threading.Lock()

        def consume(stream: Any, buffer: HeadTailOutputBuffer, spool: BoundedOutputSpool, stream_name: str) -> None:
            nonlocal output_delta_count
            try:
                for chunk in iter(stream.readline, ""):
                    if not chunk:
                        break
                    with lock:
                        buffer.append(chunk)
                        spool.append(chunk)
                        output_delta_count += 1
                    with self._lock:
                        meta = self._active_meta.get(resolved_process_id)
                        if meta is not None:
                            meta[stream_name] = buffer.snapshot()
                            meta[f"{stream_name}_content"] = spool.content
                            meta["output_delta_count"] = output_delta_count
                    emit(
                        {
                            "status": "output_delta",
                            "process_id": resolved_process_id,
                            "stream": stream_name,
                            "chars": len(chunk),
                            "text": chunk[-4000:],
                            "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        }
                    )
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threads = [
            threading.Thread(target=consume, args=(process.stdout, stdout_buffer, stdout_spool, "stdout"), daemon=True)
            if process.stdout is not None
            else None,
            threading.Thread(target=consume, args=(process.stderr, stderr_buffer, stderr_spool, "stderr"), daemon=True)
            if process.stderr is not None
            else None,
        ]
        for thread in threads:
            if thread is not None:
                thread.start()

        timed_out = False
        forced_kill = False
        last_heartbeat = started
        while process.poll() is None:
            elapsed = time.perf_counter() - started
            if timeout_seconds > 0 and elapsed >= timeout_seconds:
                timed_out = True
                self._terminate_process_tree(process, kill=False)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    forced_kill = True
                    self._terminate_process_tree(process, kill=True)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                break
            if (time.perf_counter() - last_heartbeat) * 1000 >= max(250, yield_time_ms):
                last_heartbeat = time.perf_counter()
                emit(
                    {
                        "status": "heartbeat",
                        "process_id": resolved_process_id,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "output_delta_count": output_delta_count,
                    }
                )
            time.sleep(0.05)

        for thread in threads:
            if thread is not None:
                thread.join(timeout=1)

        exit_code = process.returncode
        with self._lock:
            terminate_requested = bool((self._active_meta.get(resolved_process_id) or {}).get("terminate_requested_at"))
        killed_diagnostics = self._kill_diagnostics(
            reason="timeout" if timed_out else "manual_terminate" if terminate_requested else "signal" if isinstance(exit_code, int) and exit_code < 0 else "none",
            timed_out=timed_out,
            timeout_seconds=timeout_seconds,
            exit_code=exit_code,
            process_group=os.name == "posix",
            requested_signal="SIGTERM" if timed_out or terminate_requested else None,
            final_signal="SIGKILL" if forced_kill else None,
            grace_seconds=2 if timed_out else None,
            detail=(
                f"Command exceeded timeout of {timeout_seconds}s."
                if timed_out
                else "Terminate requested by caller."
                if terminate_requested
                else None
            ),
        )
        semantic_status, success, command_canonical, execution_classification = AgentCommandSemantics.classify(
            command=command,
            argv=list(decision.argv),
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout_buffer.excerpt(),
            stderr=stderr_buffer.excerpt(),
            workspace_id=workspace_id,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout_artifact = self._write_output_artifact(
            writer=output_artifact_writer,
            stream="stdout",
            command=command,
            process_id=resolved_process_id,
            content=stdout_spool.content,
            head_tail=stdout_buffer.snapshot(),
            sha256=stdout_spool.sha256 if stdout_spool.total_chars else None,
            total_chars=stdout_spool.total_chars,
            truncated_full=stdout_spool.truncated_full,
            exit_code=exit_code,
            semantic_status=semantic_status,
            metadata={"command_canonical": command_canonical, "execution_classification": execution_classification},
        )
        stderr_artifact = self._write_output_artifact(
            writer=output_artifact_writer,
            stream="stderr",
            command=command,
            process_id=resolved_process_id,
            content=stderr_spool.content,
            head_tail=stderr_buffer.snapshot(),
            sha256=stderr_spool.sha256 if stderr_spool.total_chars else None,
            total_chars=stderr_spool.total_chars,
            truncated_full=stderr_spool.truncated_full,
            exit_code=exit_code,
            semantic_status=semantic_status,
            metadata={"command_canonical": command_canonical, "execution_classification": execution_classification},
        )
        output_artifacts = [item for item in (stdout_artifact, stderr_artifact) if isinstance(item, dict)]
        log_capture = self._log_capture(
            stdout_buffer=stdout_buffer,
            stderr_buffer=stderr_buffer,
            stdout_spool=stdout_spool,
            stderr_spool=stderr_spool,
            stdout_artifact=stdout_artifact,
            stderr_artifact=stderr_artifact,
            max_output_chars=max_output_chars,
            output_delta_count=output_delta_count,
        )
        emit(
            {
                "status": "completed",
                "process_id": resolved_process_id,
                "elapsed_ms": duration_ms,
                "exit_code": exit_code,
                "semantic_status": semantic_status,
                "command_canonical": command_canonical,
                "execution_classification": execution_classification,
                "success": success,
                "sandbox": sandbox_summary,
                "sandbox_boundary": sandbox_boundary,
                "environment_snapshot": environment_snapshot,
                "log_capture": log_capture,
                "killed_diagnostics": killed_diagnostics,
                "stdout_ref": (stdout_artifact or {}).get("ref"),
                "stderr_ref": (stderr_artifact or {}).get("ref"),
            }
        )
        with self._lock:
            meta = self._active_meta.get(resolved_process_id, {})
            meta.update(
                {
                    "status": "completed",
                    "exit_code": exit_code,
                    "semantic_status": semantic_status,
                    "command_canonical": command_canonical,
                    "execution_classification": execution_classification,
                    "success": success,
                    "duration_ms": duration_ms,
                    "stdout": stdout_buffer.snapshot(),
                    "stderr": stderr_buffer.snapshot(),
                    "stdout_content": stdout_spool.content,
                    "stderr_content": stderr_spool.content,
                    "stdout_ref": (stdout_artifact or {}).get("ref"),
                    "stderr_ref": (stderr_artifact or {}).get("ref"),
                    "output_artifacts": output_artifacts,
                    "output_delta_count": output_delta_count,
                    "sandbox": sandbox_summary,
                    "sandbox_boundary": sandbox_boundary,
                    "environment_snapshot": environment_snapshot,
                    "log_capture": log_capture,
                    "killed_diagnostics": killed_diagnostics,
                }
            )
            self._active.pop(resolved_process_id, None)
            self._active_meta[resolved_process_id] = meta
            self._prune_completed_locked()
        return AgentCommandResult(
            command=command,
            process_id=resolved_process_id,
            argv=list(decision.argv),
            resolved_argv=exec_argv,
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
            policy_decision=policy_with_runtime(),
            error=f"Command timed out after {timeout_seconds}s." if timed_out else None,
            stdout_ref=(stdout_artifact or {}).get("ref"),
            stderr_ref=(stderr_artifact or {}).get("ref"),
            stdout_sha256=stdout_spool.sha256 if stdout_spool.total_chars else None,
            stderr_sha256=stderr_spool.sha256 if stderr_spool.total_chars else None,
            output_artifacts=output_artifacts,
            command_canonical=command_canonical,
            execution_classification=execution_classification,
            sandbox_boundary=sandbox_boundary,
            environment_snapshot=environment_snapshot,
            log_capture=log_capture,
            killed_diagnostics=killed_diagnostics,
        )

    def write_stdin(self, process_id: str, data: str) -> bool:
        with self._lock:
            process = self._active.get(process_id)
        if process is None or process.stdin is None:
            return False
        process.stdin.write(data)
        process.stdin.flush()
        with self._lock:
            meta = self._active_meta.get(process_id)
            if meta is not None:
                meta["last_stdin_write_at"] = datetime.now(timezone.utc).isoformat()
                meta["stdin_write_count"] = int(meta.get("stdin_write_count") or 0) + 1
        return True

    def terminate(self, process_id: str) -> bool:
        with self._lock:
            process = self._active.get(process_id)
        if process is None:
            return False
        self._terminate_process_tree(process, kill=False)
        with self._lock:
            meta = self._active_meta.get(process_id)
            if meta is not None:
                meta["terminate_requested_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def resize(self, process_id: str, *, cols: int | None = None, rows: int | None = None) -> bool:
        with self._lock:
            meta = self._active_meta.get(process_id)
            if meta is None:
                return False
            meta["terminal_size"] = {
                "cols": max(1, int(cols or 80)),
                "rows": max(1, int(rows or 24)),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pty_backed": False,
            }
        return True

    def read_output(self, process_id: str, *, stream: str = "stdout", start: int | None = None, end: int | None = None) -> dict[str, Any]:
        stream_name = stream if stream in {"stdout", "stderr"} else "stdout"
        with self._lock:
            meta = dict(self._active_meta.get(process_id) or {})
        payload = meta.get(stream_name) if isinstance(meta.get(stream_name), dict) else {}
        text = str(meta.get(f"{stream_name}_content") or payload.get("excerpt") or "")
        normalized_start = max(0, int(start or 0))
        normalized_end = int(end) if end is not None else None
        sliced = text[slice(normalized_start, normalized_end)]
        next_start = normalized_start + len(sliced)
        return {
            "process_id": process_id,
            "stream": stream_name,
            "start": normalized_start,
            "end": normalized_end,
            "next_start": next_start,
            "content": sliced,
            "total_chars": payload.get("total_chars", len(text)),
            "buffered_chars": len(text),
            "omitted_chars": payload.get("omitted_chars", 0),
            "status": meta.get("status", "unknown"),
            "artifact_ref": meta.get(f"{stream_name}_ref"),
        }

    def _prune_completed_locked(self) -> None:
        completed = [
            item
            for item in self._active_meta.values()
            if str(item.get("status") or "") != "running"
        ]
        overflow = len(completed) - max(1, COMPLETED_PROCESS_RETENTION)
        if overflow <= 0:
            return
        completed.sort(key=lambda item: str(item.get("started_at") or ""))
        for item in completed[:overflow]:
            process_id = str(item.get("process_id") or "")
            if process_id and process_id not in self._active:
                self._active_meta.pop(process_id, None)

    @staticmethod
    def _write_output_artifact(
        *,
        writer: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
        stream: str,
        command: str,
        process_id: str,
        content: str,
        head_tail: dict[str, Any],
        sha256: str | None,
        total_chars: int,
        truncated_full: bool,
        exit_code: int | None,
        semantic_status: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if writer is None or not content:
            return None
        try:
            return writer(
                {
                    "stream": stream,
                    "command": command,
                    "process_id": process_id,
                    "content": content,
                    "head_tail": {**head_tail, "sha256": sha256, "truncated_full": truncated_full},
                    "sha256": sha256,
                    "total_chars": total_chars,
                    "truncated_full": truncated_full,
                    "exit_code": exit_code,
                    "semantic_status": semantic_status,
                    "metadata": dict(metadata or {}),
                }
            )
        except Exception:
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = [item for item in self._active_meta.values() if str(item.get("status") or "") == "running"]
            return {
                "active_processes": active,
                "processes": list(self._active_meta.values()),
                "active_count": len(self._active),
            }

    def _sandbox_env(self, cwd: Path, *, create_dirs: bool = True) -> tuple[dict[str, str], Path, Path]:
        tmp_dir = cwd / ".sandbox" / "tmp"
        home_dir = cwd / ".sandbox" / "home"
        if create_dirs:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            home_dir.mkdir(parents=True, exist_ok=True)
        base: dict[str, str] = {
            "PATH": os.defpath,
            "HOME": str(home_dir),
            "USER": "agent",
            "SHELL": "/bin/false",
        }
        base.update(self.deterministic_env)
        base.update(
            {
                "TMPDIR": str(tmp_dir),
                "TEMP": str(tmp_dir),
                "TMP": str(tmp_dir),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(tmp_dir / "pycache"),
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "socks5://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9",
                "https_proxy": "http://127.0.0.1:9",
                "all_proxy": "socks5://127.0.0.1:9",
                "NO_PROXY": "",
                "no_proxy": "",
            }
        )
        return base, tmp_dir, home_dir

    def _log_capture(
        self,
        *,
        stdout_buffer: HeadTailOutputBuffer,
        stderr_buffer: HeadTailOutputBuffer,
        stdout_spool: BoundedOutputSpool,
        stderr_spool: BoundedOutputSpool,
        stdout_artifact: dict[str, Any] | None,
        stderr_artifact: dict[str, Any] | None,
        max_output_chars: int,
        output_delta_count: int,
    ) -> dict[str, Any]:
        def stream_capture(
            *,
            stream: str,
            buffer: HeadTailOutputBuffer,
            spool: BoundedOutputSpool,
            artifact: dict[str, Any] | None,
        ) -> SandboxLogStreamCapture:
            return SandboxLogStreamCapture(
                stream=stream,  # type: ignore[arg-type]
                head_chars=len(buffer._head),
                tail_chars=len(buffer._tail),
                total_chars=buffer.total_chars,
                omitted_chars=buffer.omitted_chars,
                chunk_count=buffer.chunk_count,
                sha256=spool.sha256 if spool.total_chars else None,
                artifact_ref=(artifact or {}).get("ref"),
                truncated_excerpt=bool(buffer.omitted_chars),
                truncated_full=spool.truncated_full,
            )

        return SandboxLogCapture(
            max_excerpt_chars=max_output_chars,
            full_spool_max_chars=stdout_spool.max_chars,
            output_delta_count=output_delta_count,
            stdout=stream_capture(stream="stdout", buffer=stdout_buffer, spool=stdout_spool, artifact=stdout_artifact),
            stderr=stream_capture(stream="stderr", buffer=stderr_buffer, spool=stderr_spool, artifact=stderr_artifact),
        ).model_dump(mode="json", by_alias=True)

    @staticmethod
    def _kill_diagnostics(
        *,
        reason: str,
        timed_out: bool,
        timeout_seconds: int,
        exit_code: int | None,
        detail: str | None = None,
        process_group: bool | None = None,
        requested_signal: str | None = None,
        final_signal: str | None = None,
        grace_seconds: float | None = None,
    ) -> dict[str, Any]:
        return_signal = None
        if isinstance(exit_code, int) and exit_code < 0:
            try:
                return_signal = signal.Signals(abs(exit_code)).name
            except ValueError:
                return_signal = f"SIG{abs(exit_code)}"
        killed = reason in {"timeout", "manual_terminate", "signal"} or bool(return_signal)
        return SandboxKillDiagnostics(
            killed=killed,
            reason=reason if reason in {"none", "timeout", "manual_terminate", "signal", "startup_failed", "sandbox_blocked", "policy_blocked", "not_started"} else "not_started",  # type: ignore[arg-type]
            timed_out=timed_out,
            timeout_seconds=timeout_seconds,
            grace_seconds=grace_seconds,
            process_group=os.name == "posix" if process_group is None else process_group,
            requested_signal=requested_signal,
            final_signal=final_signal,
            exit_code=exit_code,
            return_signal=return_signal,
            detail=detail,
            terminated_at=datetime.now(timezone.utc).isoformat() if killed or reason != "none" else None,
        ).model_dump(mode="json", by_alias=True)

    def _preexec_fn(self):
        if os.name != "posix":
            return None
        limits = dict(self.resource_limits)

        def apply_limits() -> None:
            try:
                os.umask(0o077)
            except Exception:
                pass
            try:
                import resource

                if limits.get("cpu_seconds"):
                    resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"]))
                if limits.get("address_space_bytes") and hasattr(resource, "RLIMIT_AS"):
                    resource.setrlimit(resource.RLIMIT_AS, (limits["address_space_bytes"], limits["address_space_bytes"]))
                if limits.get("file_size_bytes"):
                    resource.setrlimit(resource.RLIMIT_FSIZE, (limits["file_size_bytes"], limits["file_size_bytes"]))
                if limits.get("open_files"):
                    resource.setrlimit(resource.RLIMIT_NOFILE, (limits["open_files"], limits["open_files"]))
            except Exception:
                pass

        return apply_limits

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str], *, kill: bool) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL if kill else signal.SIGTERM)
                return
            except Exception:
                pass
        try:
            if kill:
                process.kill()
            else:
                process.terminate()
        except Exception:
            pass

    @staticmethod
    def _cwd_inside_workspace(workspace_root: Path, cwd: Path) -> bool:
        try:
            cwd.resolve().relative_to(workspace_root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _first_workspace_escaping_arg(workspace_root: Path, cwd: Path, argv: list[str]) -> str | None:
        root = workspace_root.resolve()
        for arg in argv[1:]:
            text = str(arg or "")
            if not text or text.startswith("-"):
                continue
            if not text.startswith("/") and "/" not in text and not text.startswith("."):
                continue
            candidate = Path(text) if text.startswith("/") else cwd / text
            if not candidate.exists() and not text.startswith("/"):
                continue
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                return text
        return None

    def _sandbox_summary(self, *, draft_source: Path, cwd: Path, decision: CommandPolicyDecision, execution_plan: SandboxExecutionPlan) -> dict[str, Any]:
        return {
            "mode": "workspace_process_group",
            "profile": execution_plan.profile,
            "provider": execution_plan.provider,
            "enforcement": execution_plan.enforcement,
            "fs_allowlist": [str(draft_source.resolve())],
            "filesystem_allowlist": self.sandbox_service.filesystem_allowlist(root=draft_source, cwd=cwd, execution_plan=execution_plan).model_dump(mode="json", by_alias=True),
            "cwd": str(cwd),
            "network_mode": execution_plan.network.mode,
            "env_isolated": True,
            "resource_limits": dict(self.resource_limits),
            "process_group_kill": os.name == "posix",
            "runtime_boundary_schema": "grounded.sandbox.runtime_boundary.v1",
            "environment_snapshot_schema": "grounded.sandbox.environment_snapshot.v1",
            "log_capture_schema": "grounded.sandbox.log_capture.v1",
            "kill_diagnostics_schema": "grounded.sandbox.kill_diagnostics.v1",
            "cwd_policy": decision.cwd_policy,
            "resolved_argv": list(decision.resolved_argv or decision.argv),
            "wrapped_argv": list(execution_plan.wrapped_argv),
            "read_roots": list(execution_plan.read_roots),
            "write_roots": list(execution_plan.write_roots),
            "shell_parse": decision.parse_tree,
            "network_policy": decision.network_policy,
            "violations": [item.model_dump(mode="json") for item in execution_plan.violations],
        }

    @staticmethod
    def _policy_payload(decision: CommandPolicyDecision) -> dict[str, Any]:
        return {
            "action": decision.action,
            "reason": decision.reason,
            "safety_class": decision.safety_class,
            "normalized_command": decision.normalized_command,
            "argv": list(decision.argv),
            "resolved_argv": list(decision.resolved_argv or decision.argv),
            "matched_prefix": list(decision.matched_prefix),
            "cwd_policy": decision.cwd_policy,
            "shell_parse": decision.parse_tree,
            "blocked_syntax": decision.blocked_syntax,
            "network_policy": decision.network_policy,
        }
