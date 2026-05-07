from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from app.models.sandbox import SandboxExecutionPlan
from app.modules.miniapp_agent_loop.agent_command_policy import CommandPolicyDecision
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
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        yield_time_ms: int = 1000,
        process_id: str | None = None,
        execution_plan: SandboxExecutionPlan | None = None,
    ) -> AgentCommandResult:
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc).isoformat()
        resolved_process_id = str(process_id or f"proc_{uuid4().hex[:12]}")
        stdout_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
        stderr_buffer = HeadTailOutputBuffer(max_chars=max_output_chars)
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
        sandbox_summary = self._sandbox_summary(draft_source=draft_source, cwd=cwd, decision=decision, execution_plan=plan)

        def emit(payload: dict[str, Any]) -> None:
            if progress_callback is not None:
                progress_callback(payload)

        if not decision.allowed:
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
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
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error=decision.reason,
            )

        if not decision.argv:
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=[],
                resolved_argv=[],
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
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error="Command policy allowed the command but produced no argv.",
            )

        if not self._cwd_inside_workspace(draft_source, cwd):
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="blocked_by_sandbox",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error=f"Command cwd escapes workspace: {cwd}",
            )
        escaped_arg = self._first_workspace_escaping_arg(draft_source, cwd, list(decision.argv))
        if escaped_arg:
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="blocked_by_sandbox",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error=f"Command argument escapes workspace allowlist: {escaped_arg}",
            )
        if not plan.allowed:
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
                cwd=str(cwd),
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=None,
                semantic_status="blocked_by_sandbox",
                success=False,
                timed_out=False,
                timeout_seconds=timeout_seconds,
                stdout=stdout_buffer.snapshot(),
                stderr=stderr_buffer.snapshot(),
                output_delta_count=0,
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error=plan.reason,
            )

        env = self._sandbox_env(cwd)
        if not cwd.exists():
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
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
                policy_decision={**policy_payload, "sandbox": sandbox_summary},
                error=f"Command cwd does not exist: {cwd}",
            )
        emit({"status": "started", "process_id": resolved_process_id, "command": command, "argv": list(decision.argv), "resolved_argv": exec_argv, "cwd": str(cwd), "sandbox": sandbox_summary})
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
            return AgentCommandResult(
                command=command,
                process_id=resolved_process_id,
                argv=list(decision.argv),
                resolved_argv=exec_argv,
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
                "output_delta_count": 0,
                "sandbox": sandbox_summary,
                "network_mode": sandbox_summary["network_mode"],
            }

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
                    with self._lock:
                        meta = self._active_meta.get(resolved_process_id)
                        if meta is not None:
                            meta[stream_name] = buffer.snapshot()
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
                self._terminate_process_tree(process, kill=False)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._terminate_process_tree(process, kill=True)
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
        semantic_status, success = AgentCommandSemantics.classify(
            argv=list(decision.argv),
            exit_code=exit_code,
            timed_out=timed_out,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        emit(
            {
                "status": "completed",
                "process_id": resolved_process_id,
                "elapsed_ms": duration_ms,
                "exit_code": exit_code,
                "semantic_status": semantic_status,
                "success": success,
                "sandbox": sandbox_summary,
            }
        )
        with self._lock:
            meta = self._active_meta.get(resolved_process_id, {})
            meta.update(
                {
                    "status": "completed",
                    "exit_code": exit_code,
                    "semantic_status": semantic_status,
                    "success": success,
                    "duration_ms": duration_ms,
                    "stdout": stdout_buffer.snapshot(),
                    "stderr": stderr_buffer.snapshot(),
                    "output_delta_count": output_delta_count,
                    "sandbox": sandbox_summary,
                }
            )
            self._active.pop(resolved_process_id, None)
            self._active_meta[resolved_process_id] = meta
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
            policy_decision={**policy_payload, "sandbox": sandbox_summary},
            error=f"Command timed out after {timeout_seconds}s." if timed_out else None,
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
        text = str(payload.get("excerpt") or "")
        sliced = text[slice(start, end)]
        return {
            "process_id": process_id,
            "stream": stream_name,
            "start": start,
            "end": end,
            "content": sliced,
            "total_chars": payload.get("total_chars", len(text)),
            "omitted_chars": payload.get("omitted_chars", 0),
            "status": meta.get("status", "unknown"),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = [item for item in self._active_meta.values() if str(item.get("status") or "") == "running"]
            return {
                "active_processes": active,
                "processes": list(self._active_meta.values()),
                "active_count": len(self._active),
            }

    def _sandbox_env(self, cwd: Path) -> dict[str, str]:
        tmp_dir = cwd / ".sandbox" / "tmp"
        home_dir = cwd / ".sandbox" / "home"
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
        return base

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
            "cwd": str(cwd),
            "network_mode": execution_plan.network.mode,
            "env_isolated": True,
            "resource_limits": dict(self.resource_limits),
            "process_group_kill": os.name == "posix",
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
            "normalized_command": decision.normalized_command,
            "argv": list(decision.argv),
            "resolved_argv": list(decision.resolved_argv or decision.argv),
            "matched_prefix": list(decision.matched_prefix),
            "cwd_policy": decision.cwd_policy,
            "shell_parse": decision.parse_tree,
            "blocked_syntax": decision.blocked_syntax,
            "network_policy": decision.network_policy,
        }
