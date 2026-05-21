from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from app.models.sandbox import (
    ApplySafetyItem,
    ApplySafetyReport,
    SandboxEnvironmentSnapshot,
    SandboxExecutionPlan,
    SandboxFilesystemAllowlist,
    SandboxLogCapture,
    SandboxLogStreamCapture,
    SandboxNetworkDecision,
    SandboxOperation,
    SandboxPathDecision,
    SandboxRuntimeBoundary,
    SandboxRuntimeManifest,
    SandboxProfile,
    SandboxViolation,
)


class SandboxViolationError(ValueError):
    def __init__(self, decision: SandboxPathDecision | ApplySafetyReport | SandboxExecutionPlan) -> None:
        self.decision = decision
        if isinstance(decision, SandboxPathDecision):
            message = decision.reason
        elif isinstance(decision, SandboxExecutionPlan):
            message = decision.reason
        else:
            message = "; ".join(item.message for item in decision.violations) or "Sandbox preflight failed."
        super().__init__(message)


class SandboxService:
    """Filesystem and process sandbox enforcement shared by agent/runtime paths."""

    IGNORED_PARTS = {
        ".git",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".vite",
        ".cache",
        ".sandbox",
    }
    IGNORED_NAMES = {".DS_Store", "vite.config.js", "vite.config.d.ts"}
    IGNORED_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo", ".db", ".sqlite", ".sqlite3")
    GENERATED_PREFIX = "miniapp/app/generated/"
    CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
    WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

    def __init__(self, *, strict_network: bool = True) -> None:
        self.strict_network = strict_network

    def manifest(self) -> dict[str, object]:
        provider = self.network_provider()
        enforcement = "hard" if provider in {"sandbox-exec", "unshare"} else "unavailable"
        return SandboxRuntimeManifest(
            profiles={
                "analysis_readonly": {"writes": "none", "network": "blocked", "description": "Read-only diagnostics and workspace inspection."},
                "agent_draft_write": {"writes": "draft_workspace", "network": "blocked", "description": "Guarded agent writes into draft source only."},
                "source_apply_gate": {"writes": "source_workspace", "network": "blocked", "description": "Validated draft/patch apply into source."},
                "developer_bypass": {"writes": "workspace", "network": "allowed", "description": "Human/dev mode; path safety remains enforced."},
            },
            provider=provider,
            enforcement=enforcement,  # type: ignore[arg-type]
            execution_boundary={
                "mode": "workspace_process_group",
                "isolated_generated_app_workspace": "workspace source or per-run draft source",
                "filesystem_allowlist": "read roots + explicit write roots from SandboxExecutionPlan",
                "environment": "deterministic minimal env with isolated HOME/TMPDIR under .sandbox",
                "process_group_kill": os.name == "posix",
            },
            path_safety={
                "path_traversal": "blocked",
                "symlink_ancestors": "blocked",
                "symlink_writes": "blocked",
                "hardlink_writes": "blocked",
            },
            network_policy={
                "default": "blocked",
                "provider": provider,
                "enforcement": enforcement,
                "strict_network": self.strict_network,
                "fail_closed_without_provider": self.strict_network,
            },
            process_timeout={
                "required": True,
                "termination": ["SIGTERM process group", "SIGKILL process group after grace"],
                "grace_seconds": 2,
            },
            log_capture={
                "stdout": "head/tail excerpt + bounded full spool + sha256 + optional artifact ref",
                "stderr": "head/tail excerpt + bounded full spool + sha256 + optional artifact ref",
                "streaming": "output_delta events include bounded tail chunks",
            },
            killed_process_diagnostics={
                "fields": ["reason", "timed_out", "timeout_seconds", "requested_signal", "final_signal", "exit_code", "return_signal"],
                "reasons": ["timeout", "manual_terminate", "signal", "startup_failed", "sandbox_blocked", "policy_blocked"],
            },
            preview_lifecycle={
                "start": "allocated port, runtime mode, health probe, logs",
                "reset": "stop local process or docker compose down with stale resource cleanup",
                "destroy": "workspace deletion destroys preview containers/networks/volumes and clears preview state",
            },
            reproducibility={
                "environment_snapshot": "cwd, argv, wrapped argv, env keys/hash, resource limits, provider, enforcement",
                "workspace_snapshot": "isolated source/draft root with path safety and ignored runtime artifacts",
            },
        ).model_dump(mode="json", by_alias=True)

    def network_provider(self) -> str:
        if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
            return "sandbox-exec"
        if platform.system() == "Linux" and shutil.which("unshare"):
            return "unshare"
        return "none"

    def resolve_path(
        self,
        root: Path,
        relative_path: str | Path,
        *,
        operation: SandboxOperation,
        profile: SandboxProfile,
        allow_generated: bool = False,
    ) -> SandboxPathDecision:
        raw = str(relative_path or "")
        violations: list[SandboxViolation] = []
        normalized = self._normalize_relative_path(raw, violations)
        root_resolved = root.resolve(strict=False)
        target = root_resolved / normalized if normalized else root_resolved
        resolved = target.resolve(strict=False)

        if normalized:
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                violations.append(self._violation("path_escape", "Path resolves outside the sandbox root.", normalized))
        elif operation in {"write", "delete", "copy", "apply"}:
            violations.append(self._violation("empty_path", "Write/apply operations require a file path.", raw))

        if normalized and self._is_ignored_path(PurePosixPath(normalized), allow_generated=allow_generated):
            violations.append(self._violation("ignored_path", "Path targets an ignored/generated workspace area.", normalized))

        ancestor_violation = self._first_symlink_ancestor(root_resolved, normalized)
        if ancestor_violation is not None:
            violations.append(ancestor_violation)

        exists = target.exists()
        is_symlink = target.is_symlink()
        is_file = target.is_file() if exists else False
        is_dir = target.is_dir() if exists else False
        hardlink_count = 0
        is_hardlink = False
        sha256: str | None = None
        snapshot: dict[str, object] = self.path_snapshot(target)
        if exists or is_symlink:
            try:
                st = target.lstat()
                hardlink_count = int(getattr(st, "st_nlink", 0) or 0)
                is_hardlink = stat.S_ISREG(st.st_mode) and hardlink_count > 1
            except OSError:
                violations.append(self._violation("stat_failed", "Path could not be inspected safely.", normalized))
        if exists and is_file and not is_symlink:
            sha256 = self._sha256_file(target)

        if is_symlink:
            if operation == "read":
                try:
                    target.resolve(strict=True).relative_to(root_resolved)
                except (OSError, ValueError):
                    violations.append(self._violation("symlink_escape", "Symlink target escapes the sandbox root.", normalized))
            else:
                violations.append(self._violation("symlink_write", "Writes/apply through symlinks are blocked.", normalized))
        if operation in {"write", "delete", "copy", "apply"} and is_hardlink:
            violations.append(self._violation("hardlink_write", "Writes/apply to hardlinked files are blocked.", normalized, {"nlink": hardlink_count}))
        if profile == "analysis_readonly" and operation in {"write", "delete", "copy", "apply"}:
            violations.append(self._violation("readonly_profile", "Profile does not allow filesystem writes.", normalized))

        allowed = not any(item.blocking for item in violations)
        return SandboxPathDecision(
            profile=profile,
            operation=operation,
            path=raw,
            normalized_path=normalized,
            root=str(root_resolved),
            absolute_path=str(target),
            resolved_path=str(resolved),
            allowed=allowed,
            reason="Path is allowed by sandbox." if allowed else "; ".join(item.message for item in violations if item.blocking),
            exists=exists,
            is_file=is_file,
            is_dir=is_dir,
            is_symlink=is_symlink,
            is_hardlink=is_hardlink,
            hardlink_count=hardlink_count,
            sha256=sha256,
            snapshot=snapshot,
            violations=violations,
        )

    def preflight_apply(
        self,
        root: Path,
        paths: Iterable[str | Path],
        *,
        profile: SandboxProfile,
        operation: SandboxOperation = "apply",
        allow_generated: bool = False,
        operation_ids: dict[str, str] | None = None,
    ) -> ApplySafetyReport:
        items: list[ApplySafetyItem] = []
        violations: list[SandboxViolation] = []
        for raw_path in list(dict.fromkeys(str(path or "") for path in paths)):
            decision = self.resolve_path(root, raw_path, operation=operation, profile=profile, allow_generated=allow_generated)
            if not decision.allowed:
                violations.extend(decision.violations)
            items.append(
                ApplySafetyItem(
                    operation_id=(operation_ids or {}).get(decision.normalized_path),
                    op=operation,
                    path=decision.normalized_path,
                    decision=decision,
                    snapshot=decision.snapshot,
                )
            )
        return ApplySafetyReport(
            status="blocked" if violations else "passed",
            profile=profile,
            root=str(root.resolve(strict=False)),
            items=items,
            violations=violations,
        )

    def validate_snapshot(self, path: Path, snapshot: dict[str, object]) -> bool:
        current = self.path_snapshot(path)
        keys = ("exists", "inode", "device", "mtime_ns", "size", "sha256", "is_symlink", "hardlink_count")
        return all(current.get(key) == snapshot.get(key) for key in keys)

    def safe_write_text(
        self,
        root: Path,
        relative_path: str | Path,
        content: str,
        *,
        profile: SandboxProfile,
        snapshot: dict[str, object] | None = None,
        allow_generated: bool = False,
    ) -> None:
        self.safe_write_bytes(
            root,
            relative_path,
            str(content or "").encode("utf-8"),
            profile=profile,
            snapshot=snapshot,
            allow_generated=allow_generated,
        )

    def safe_write_bytes(
        self,
        root: Path,
        relative_path: str | Path,
        content: bytes,
        *,
        profile: SandboxProfile,
        snapshot: dict[str, object] | None = None,
        allow_generated: bool = False,
    ) -> None:
        decision = self.resolve_path(root, relative_path, operation="write", profile=profile, allow_generated=allow_generated)
        if not decision.allowed:
            raise SandboxViolationError(decision)
        target = Path(decision.absolute_path)
        if snapshot is not None and not self.validate_snapshot(target, snapshot):
            raise ValueError("Sandbox preflight is stale.")
        target.parent.mkdir(parents=True, exist_ok=True)
        parent_decision = self.resolve_path(root, target.parent.relative_to(Path(decision.root)), operation="write", profile=profile, allow_generated=True)
        if not parent_decision.allowed:
            raise SandboxViolationError(parent_decision)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def safe_delete_path(
        self,
        root: Path,
        relative_path: str | Path,
        *,
        profile: SandboxProfile,
        snapshot: dict[str, object] | None = None,
        allow_generated: bool = False,
    ) -> None:
        decision = self.resolve_path(root, relative_path, operation="delete", profile=profile, allow_generated=allow_generated)
        if not decision.allowed:
            raise SandboxViolationError(decision)
        target = Path(decision.absolute_path)
        if snapshot is not None and not self.validate_snapshot(target, snapshot):
            raise ValueError("Sandbox preflight is stale.")
        if not target.exists() and not target.is_symlink():
            return
        if target.is_symlink():
            raise SandboxViolationError(decision)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    def safe_copy_file(
        self,
        source_root: Path,
        destination_root: Path,
        relative_path: str | Path,
        *,
        profile: SandboxProfile,
        destination_snapshot: dict[str, object] | None = None,
        allow_generated: bool = False,
    ) -> None:
        source = self.resolve_path(source_root, relative_path, operation="apply", profile=profile, allow_generated=allow_generated)
        if not source.allowed or source.is_symlink or not source.is_file:
            raise SandboxViolationError(source)
        data = Path(source.absolute_path).read_bytes()
        self.safe_write_bytes(
            destination_root,
            relative_path,
            data,
            profile=profile,
            snapshot=destination_snapshot,
            allow_generated=allow_generated,
        )

    def safe_copy_tree(
        self,
        source_root: Path,
        destination_root: Path,
        *,
        profile: SandboxProfile,
        allow_generated: bool = False,
    ) -> None:
        destination_root.mkdir(parents=True, exist_ok=True)
        for source_path in sorted(source_root.rglob("*")):
            relative = source_path.relative_to(source_root)
            if self._is_ignored_path(PurePosixPath(relative.as_posix()), allow_generated=allow_generated):
                continue
            decision = self.resolve_path(source_root, relative, operation="apply", profile=profile, allow_generated=allow_generated)
            if not decision.allowed:
                raise SandboxViolationError(decision)
            destination = destination_root / relative
            if source_path.is_symlink():
                raise SandboxViolationError(decision)
            if source_path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if source_path.is_file():
                self.safe_copy_file(source_root, destination_root, relative, profile=profile, allow_generated=allow_generated)

    def safe_replace_workspace_from_draft(self, source_root: Path, draft_root: Path) -> None:
        for child in sorted(source_root.iterdir(), key=lambda item: item.name):
            if child.name == ".git":
                continue
            if self._is_ignored_path(PurePosixPath(child.name), allow_generated=True):
                continue
            decision = self.resolve_path(source_root, child.name, operation="delete", profile="source_apply_gate", allow_generated=True)
            if not decision.allowed:
                raise SandboxViolationError(decision)
        for draft_path in sorted(draft_root.rglob("*")):
            relative = draft_path.relative_to(draft_root)
            if self._is_ignored_path(PurePosixPath(relative.as_posix()), allow_generated=True):
                continue
            decision = self.resolve_path(draft_root, relative, operation="apply", profile="source_apply_gate", allow_generated=True)
            if not decision.allowed:
                raise SandboxViolationError(decision)
        for child in sorted(source_root.iterdir(), key=lambda item: item.name):
            if child.name == ".git":
                continue
            if self._is_ignored_path(PurePosixPath(child.name), allow_generated=True):
                continue
            self.safe_delete_path(source_root, child.name, profile="source_apply_gate", allow_generated=True)
        self.safe_copy_tree(draft_root, source_root, profile="source_apply_gate", allow_generated=True)

    def build_execution_plan(
        self,
        *,
        root: Path,
        cwd: Path,
        argv: list[str],
        profile: SandboxProfile = "analysis_readonly",
        network_mode: str = "blocked",
        write_roots: list[Path] | None = None,
    ) -> SandboxExecutionPlan:
        violations: list[SandboxViolation] = []
        root_resolved = root.resolve(strict=False)
        cwd_resolved = cwd.resolve(strict=False)
        try:
            cwd_resolved.relative_to(root_resolved)
        except ValueError:
            violations.append(self._violation("cwd_escape", "Command cwd escapes sandbox root.", str(cwd)))
        for arg in argv[1:]:
            text = str(arg or "")
            if not text or text.startswith("-"):
                continue
            if not text.startswith("/") and "/" not in text and not text.startswith("."):
                continue
            candidate = Path(text) if text.startswith("/") else cwd_resolved / text
            if not candidate.exists() and not text.startswith("/"):
                continue
            try:
                candidate.resolve(strict=False).relative_to(root_resolved)
            except ValueError:
                violations.append(self._violation("arg_escape", "Command argument escapes sandbox root.", text))
                break
        provider = self.network_provider() if network_mode == "blocked" and profile != "developer_bypass" else "none"
        enforcement = "hard" if provider in {"sandbox-exec", "unshare"} else ("policy_only" if profile == "developer_bypass" else "unavailable")
        network = SandboxNetworkDecision(
            mode="allowed" if profile == "developer_bypass" else "blocked",
            allowed=profile == "developer_bypass",
            provider=provider,
            enforcement=enforcement,
            reason="Network blocked by OS sandbox." if enforcement == "hard" else "No hard network provider available.",
        )
        if profile != "developer_bypass" and network_mode == "blocked" and enforcement != "hard" and self.strict_network:
            violations.append(self._violation("network_provider_unavailable", "Hard network isolation provider is unavailable."))
        write_root_paths = [path.resolve(strict=False) for path in (write_roots or [])]
        read_roots = self._execution_read_roots(root_resolved, argv)
        wrapped_argv = list(argv)
        if not violations:
            if provider == "sandbox-exec":
                wrapped_argv = ["sandbox-exec", "-p", self._macos_profile(read_roots, write_root_paths), "--", *argv]
            elif provider == "unshare":
                wrapped_argv = ["unshare", "-n", "--", *argv]
        allowed = not any(item.blocking for item in violations)
        return SandboxExecutionPlan(
            profile=profile,
            provider=provider,
            enforcement=enforcement,
            cwd=str(cwd_resolved),
            argv=list(argv),
            wrapped_argv=wrapped_argv,
            read_roots=[str(path) for path in read_roots],
            write_roots=[str(path) for path in write_root_paths],
            network=network,
            allowed=allowed,
            reason="Execution allowed by sandbox." if allowed else "; ".join(item.message for item in violations),
            violations=violations,
        )

    def filesystem_allowlist(self, *, root: Path, cwd: Path, execution_plan: SandboxExecutionPlan) -> SandboxFilesystemAllowlist:
        allowed_operations: list[SandboxOperation]
        if execution_plan.profile == "analysis_readonly":
            allowed_operations = ["read", "exec"]
        elif execution_plan.profile == "developer_bypass":
            allowed_operations = ["read", "write", "delete", "copy", "apply", "exec"]
        else:
            allowed_operations = ["read", "write", "copy", "apply", "exec"]
        return SandboxFilesystemAllowlist(
            root=str(root.resolve(strict=False)),
            cwd=str(cwd.resolve(strict=False)),
            read_roots=list(execution_plan.read_roots),
            write_roots=list(execution_plan.write_roots),
            denied_parts=sorted(self.IGNORED_PARTS),
            denied_names=sorted(self.IGNORED_NAMES),
            denied_suffixes=list(self.IGNORED_SUFFIXES),
            generated_prefix=self.GENERATED_PREFIX,
            path_safety={
                "path_traversal": "blocked",
                "symlink_ancestors": "blocked",
                "symlink_writes": "blocked",
                "hardlink_writes": "blocked",
                "absolute_paths": "blocked for workspace file operations; execution args must resolve inside root",
            },
            allowed_operations=allowed_operations,
        )

    def environment_snapshot(
        self,
        *,
        process_id: str,
        root: Path,
        cwd: Path,
        argv: list[str],
        resolved_argv: list[str],
        wrapped_argv: list[str],
        env: dict[str, str],
        tmp_dir: Path,
        home_dir: Path,
        resource_limits: dict[str, int],
        execution_plan: SandboxExecutionPlan,
        host_pid: int | None = None,
    ) -> SandboxEnvironmentSnapshot:
        env_keys = sorted(str(key) for key in env)
        env_material = {key: env.get(key, "") for key in env_keys}
        env_sha256 = hashlib.sha256(json.dumps(env_material, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="replace")).hexdigest()
        base_payload = {
            "process_id": process_id,
            "host_pid": host_pid,
            "workspace_root": str(root.resolve(strict=False)),
            "isolated_workspace": str(root.resolve(strict=False)),
            "cwd": str(cwd.resolve(strict=False)),
            "argv": list(argv),
            "resolved_argv": list(resolved_argv),
            "wrapped_argv": list(wrapped_argv),
            "env_keys": env_keys,
            "env_sha256": env_sha256,
            "tmp_dir": str(tmp_dir),
            "home_dir": str(home_dir),
            "resource_limits": dict(resource_limits),
            "os_name": platform.system() or os.name,
            "profile": execution_plan.profile,
            "provider": execution_plan.provider,
            "enforcement": execution_plan.enforcement,
            "network_mode": execution_plan.network.mode,
        }
        snapshot_sha256 = hashlib.sha256(json.dumps(base_payload, sort_keys=True, separators=(",", ":")).encode("utf-8", errors="replace")).hexdigest()
        return SandboxEnvironmentSnapshot(
            **base_payload,
            created_at=datetime.now(timezone.utc).isoformat(),
            snapshot_sha256=snapshot_sha256,
        )

    def runtime_boundary(
        self,
        *,
        process_id: str,
        root: Path,
        cwd: Path,
        execution_plan: SandboxExecutionPlan,
        timeout_seconds: int,
        resource_limits: dict[str, int],
        environment: SandboxEnvironmentSnapshot,
        max_excerpt_chars: int,
        full_spool_max_chars: int,
    ) -> SandboxRuntimeBoundary:
        return SandboxRuntimeBoundary(
            process_id=process_id,
            profile=execution_plan.profile,
            provider=execution_plan.provider,
            enforcement=execution_plan.enforcement,
            workspace_root=str(root.resolve(strict=False)),
            isolated_workspace=str(root.resolve(strict=False)),
            cwd=str(cwd.resolve(strict=False)),
            filesystem=self.filesystem_allowlist(root=root, cwd=cwd, execution_plan=execution_plan),
            network=execution_plan.network,
            timeout_seconds=timeout_seconds,
            resource_limits=dict(resource_limits),
            process_group_kill=os.name == "posix",
            environment=environment,
            log_capture_policy={
                "max_excerpt_chars": max_excerpt_chars,
                "full_spool_max_chars": full_spool_max_chars,
                "streams": ["stdout", "stderr"],
                "captures_sha256": True,
                "artifact_refs": "stored when an output artifact writer is provided",
            },
            violations=list(execution_plan.violations),
        )

    @staticmethod
    def empty_log_capture(*, max_excerpt_chars: int, full_spool_max_chars: int) -> SandboxLogCapture:
        return SandboxLogCapture(
            max_excerpt_chars=max_excerpt_chars,
            full_spool_max_chars=full_spool_max_chars,
            stdout=SandboxLogStreamCapture(stream="stdout"),
            stderr=SandboxLogStreamCapture(stream="stderr"),
        )

    def path_snapshot(self, path: Path) -> dict[str, object]:
        try:
            st = path.lstat()
        except OSError:
            return {
                "exists": False,
                "inode": None,
                "device": None,
                "mtime_ns": None,
                "size": None,
                "sha256": None,
                "is_symlink": False,
                "hardlink_count": 0,
            }
        is_regular = stat.S_ISREG(st.st_mode)
        return {
            "exists": True,
            "inode": int(st.st_ino),
            "device": int(st.st_dev),
            "mtime_ns": int(st.st_mtime_ns),
            "size": int(st.st_size),
            "sha256": self._sha256_file(path) if is_regular and not path.is_symlink() else None,
            "is_symlink": stat.S_ISLNK(st.st_mode),
            "hardlink_count": int(st.st_nlink),
        }

    def _normalize_relative_path(self, raw: str, violations: list[SandboxViolation]) -> str:
        path = str(raw or "").strip()
        if "\\" in path:
            violations.append(self._violation("backslash_path", "Backslash path normalization is blocked.", raw))
        if self.CONTROL_CHARS.search(path):
            violations.append(self._violation("control_char_path", "Control characters are blocked in paths.", raw))
        if path.startswith("~") or path.startswith("/") or self.WINDOWS_DRIVE.match(path):
            violations.append(self._violation("absolute_path", "Absolute and home-relative paths are blocked.", raw))
        while path.startswith("./"):
            path = path[2:]
        normalized = path.strip("/")
        parts = PurePosixPath(normalized).parts
        if any(part in {"..", ""} for part in parts):
            violations.append(self._violation("path_traversal", "Parent-directory traversal is blocked.", raw))
        return normalized

    def _first_symlink_ancestor(self, root: Path, normalized_path: str) -> SandboxViolation | None:
        current = root
        parts = PurePosixPath(normalized_path).parts
        for part in parts[:-1]:
            current = current / part
            if not current.exists() and not current.is_symlink():
                return None
            try:
                if current.lstat() and current.is_symlink():
                    return self._violation("symlink_ancestor", "Symlink ancestors are blocked.", current.as_posix())
            except OSError:
                return self._violation("stat_failed", "Path ancestor could not be inspected safely.", current.as_posix())
        return None

    def _is_ignored_path(self, path: PurePosixPath, *, allow_generated: bool = False) -> bool:
        text = path.as_posix().strip("/")
        if any(part in self.IGNORED_PARTS for part in path.parts):
            return True
        if path.name in self.IGNORED_NAMES:
            return True
        if path.name.endswith(self.IGNORED_SUFFIXES):
            return True
        return bool((text == self.GENERATED_PREFIX.rstrip("/") or text.startswith(self.GENERATED_PREFIX)) and not allow_generated)

    def _execution_read_roots(self, root: Path, argv: list[str]) -> list[Path]:
        roots = [
            root,
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/System"),
            Path("/Library"),
            Path("/opt"),
            Path("/Applications"),
            Path("/private/var/db/timezone"),
            Path("/etc"),
            Path("/dev/null"),
            Path("/dev/urandom"),
            Path("/dev/random"),
        ]
        if argv:
            executable = Path(argv[0])
            if executable.is_absolute():
                roots.append(executable)
        return list(dict.fromkeys(path.resolve(strict=False) for path in roots if path.exists() or str(path).startswith("/dev/")))

    @staticmethod
    def _macos_profile(read_roots: list[Path], write_roots: list[Path]) -> str:
        def literal(path: Path) -> str:
            return f'(literal "{str(path)}")'

        def subpath(path: Path) -> str:
            return f'(subpath "{str(path)}")'

        read_filters = "\n  ".join(subpath(path) if path.is_dir() else literal(path) for path in read_roots)
        write_filters = "\n  ".join([*(subpath(path) for path in write_roots), literal(Path("/dev/null"))])
        return "\n".join(
            [
                "(version 1)",
                "(deny default)",
                '(import "bsd.sb")',
                "(allow process*)",
                "(allow mach-lookup)",
                "(allow sysctl-read)",
                f"(allow file-read*\n  {read_filters})",
                f"(allow file-write*\n  {write_filters})",
            ]
        )

    @staticmethod
    def _sha256_file(path: Path) -> str | None:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _violation(code: str, message: str, path: str | None = None, details: dict[str, object] | None = None) -> SandboxViolation:
        return SandboxViolation(code=code, message=message, path=path, details=dict(details or {}))
