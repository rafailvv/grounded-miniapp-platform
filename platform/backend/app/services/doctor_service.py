from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from app.ai.openai_client import OpenAIClient
from app.core.config import Settings
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.exec_policy_service import ExecPolicyService
from app.services.run_protocol import RunProtocolService


DOCTOR_SCHEMA = "grounded.doctor_report.v2"
CHECK_SCHEMA = "grounded.doctor_check.v2"


class DoctorService:
    """Bounded environment/workspace diagnostics for platform support and repair planning."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: StateStore,
        openai_client: OpenAIClient,
        exec_policy_service: ExecPolicyService,
        event_journal_service: EventJournalService | None = None,
        run_protocol_service: RunProtocolService | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.openai_client = openai_client
        self.exec_policy_service = exec_policy_service
        self.event_journal_service = event_journal_service
        self.run_protocol_service = run_protocol_service

    def global_report(
        self,
        *,
        scope: str = "quick",
        workspace_id: str | None = None,
        run_id: str | None = None,
        preview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_scope = "full" if str(scope or "").lower() == "full" else "quick"
        checks = self._collect_checks(scope=resolved_scope, preview=preview)
        return self._persist_report(
            scope=resolved_scope,
            workspace_id=workspace_id,
            run_id=run_id,
            checks=checks,
            key="doctor:last",
            prefix="doctor",
            report_scope="global",
        )

    def workspace_report(
        self,
        *,
        workspace_id: str,
        run_id: str | None = None,
        scope: str = "quick",
        preview: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_scope = "full" if str(scope or "").lower() == "full" else "quick"
        return self._persist_report(
            scope=resolved_scope,
            workspace_id=workspace_id,
            run_id=run_id,
            checks=self._collect_checks(scope=resolved_scope, preview=preview),
            key=f"doctor_workspace:{workspace_id}:environment",
            prefix=f"doctor_workspace:{workspace_id}",
            report_scope="workspace",
        )

    def _collect_checks(self, *, scope: str, preview: dict[str, Any] | None) -> list[dict[str, Any]]:
        resolved_scope = "full" if str(scope or "").lower() == "full" else "quick"
        browser_probe = self._playwright_browsers_check if resolved_scope == "full" else self._browser_availability_check
        checks = [
            self._run_check("runtime", self._python_version_check),
            self._run_check("runtime", self._python_deps_check),
            self._run_check("runtime", self._node_version_check),
            self._run_check("runtime", self._npm_version_check),
            self._run_check("runtime", self._package_managers_check),
            self._run_check("preview", self._docker_binary_check),
            self._run_check("preview", self._compose_check),
            self._run_check("preview", self._docker_daemon_check if resolved_scope == "full" else self._docker_daemon_quick_check),
            self._run_check("browser", self._playwright_check),
            self._run_check("browser", browser_probe),
            self._run_check("models", self._openai_check),
            self._run_check("models", self._model_access_check),
            self._run_check("storage", lambda: self._writable_check("data_dir", self.settings.data_dir, "storage.write_permission")),
            self._run_check("storage", self._writable_dirs_check),
            self._run_check("storage", self._db_writable_check),
            self._run_check("storage", self._disk_space_check),
            self._run_check("templates", self._template_check),
            self._run_check("templates", self._template_hash_check),
            self._run_check("templates", self._template_manifest_check),
            self._run_check("ports", self._port_check),
            self._run_check("ports", self._preview_port_range_check),
            self._run_check("backend", self._backend_routes_check),
            self._run_check("backend", self._backend_imports_check),
            self._run_check("backend", self._stale_backend_check),
            self._run_check("preview", self._preview_runtime_check),
            self._run_check("preview", lambda: self._workspace_preview_check(preview or {})),
            self._run_check("preview", self._preview_container_check if resolved_scope == "full" else self._preview_container_quick_check),
            self._run_check("frontend", self._frontend_config_check),
            self._run_check("tests", self._test_command_check),
            self._run_check("policy", self._runtime_policy_files_check),
            self._run_check("policy", self.exec_policy_service.doctor_check),
            self._run_check("env", self._env_vars_check),
        ]
        if resolved_scope == "full":
            checks.insert(10, self._run_check("browser", self._browser_availability_check))
        return checks

    def _run_check(self, category: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            payload = dict(fn() or {})
        except Exception as exc:
            payload = self._check(
                getattr(fn, "__name__", "doctor_check"),
                False,
                f"{exc.__class__.__name__}: {exc}",
                required=True,
                fix_hint="Inspect the failing doctor check implementation and runtime environment.",
                repair_recipe_id="doctor.check_failed",
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload.setdefault("schema", CHECK_SCHEMA)
        payload.setdefault("category", category)
        payload.setdefault("required", True)
        payload.setdefault("details", "")
        payload.setdefault("command", None)
        payload.setdefault("evidence", {})
        payload.setdefault("fix_hint", self._default_fix_hint(payload))
        payload.setdefault("repair_recipe_id", self._default_repair_recipe(payload))
        payload["duration_ms"] = duration_ms
        return payload

    def _check(
        self,
        name: str,
        ok: bool,
        details: str = "",
        command: str | None = None,
        *,
        required: bool = True,
        evidence: dict[str, Any] | None = None,
        fix_hint: str | None = None,
        repair_recipe_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": CHECK_SCHEMA,
            "name": name,
            "status": "passed" if ok else "failed",
            "details": details,
            "command": command,
            "required": required,
            "evidence": self._redact(evidence or {}),
            "fix_hint": fix_hint or "",
            "repair_recipe_id": repair_recipe_id or "",
        }

    def _persist_report(
        self,
        *,
        scope: str,
        report_scope: str,
        workspace_id: str | None,
        run_id: str | None,
        checks: list[dict[str, Any]],
        key: str,
        prefix: str,
    ) -> dict[str, Any]:
        summary = self._doctor_summary(checks)
        blocking = list(summary.get("required_failed") or [])
        warnings = list(summary.get("warnings") or [])
        payload = {
            "schema": DOCTOR_SCHEMA,
            "legacy_schema": "grounded.doctor_health_panel.v1",
            "status": "passed" if not blocking else "failed",
            "scope": scope,
            "report_scope": report_scope,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "checks": checks,
            "sections": self._doctor_sections(checks),
            "summary": summary,
            "blocking_checks": blocking,
            "warnings": warnings,
            "repair_packet": self._repair_packet(workspace_id=workspace_id, run_id=run_id, checks=checks),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "next_sequence": self._next_sequence(prefix),
        }
        self.store.upsert("reports", key, payload)
        self.store.upsert("reports", f"{prefix}:{payload['next_sequence']}", payload)
        if run_id:
            self._append_run_event(run_id, payload)
        return payload

    def _append_run_event(self, run_id: str, payload: dict[str, Any]) -> None:
        if self.run_protocol_service is None:
            return
        try:
            self.run_protocol_service.append_event(
                run_id,
                "doctor.checked",
                payload,
                summary=f"Doctor checked: {payload.get('status')}",
                source_ref=f"doctor:{payload.get('next_sequence')}",
                idempotency_key=f"doctor.checked:{run_id}:{payload.get('next_sequence')}",
            )
        except Exception:
            return

    def _next_sequence(self, prefix: str) -> int:
        existing = [
            key
            for key, _payload in self.store.items("reports")
            if str(key).startswith(f"{prefix}:") and str(key).removeprefix(f"{prefix}:").isdigit()
        ]
        return len(existing) + 1

    def _python_version_check(self) -> dict[str, Any]:
        version = sys.version_info
        required_major, required_minor = self._required_python_version()
        ok = (version.major, version.minor) >= (required_major, required_minor)
        details = f"python={version.major}.{version.minor}.{version.micro}; executable={Path(sys.executable)}; required>={required_major}.{required_minor}"
        return self._check(
            "python",
            ok,
            details,
            str(Path(sys.executable)),
            evidence={"executable": str(Path(sys.executable)), "version": f"{version.major}.{version.minor}.{version.micro}"},
            fix_hint=f"Use Python >= {required_major}.{required_minor} for the backend runtime.",
            repair_recipe_id="doctor.runtime.python",
        )

    def _python_deps_check(self) -> dict[str, Any]:
        required = ["fastapi", "pydantic", "uvicorn", "sqlalchemy"]
        optional = ["playwright", "pytest"]
        missing_required = [name for name in required if importlib.util.find_spec(name) is None]
        missing_optional = [name for name in optional if importlib.util.find_spec(name) is None]
        details = f"required_present={len(required) - len(missing_required)}/{len(required)}; missing_required={missing_required}; missing_optional={missing_optional}"
        return self._check(
            "python_deps",
            not missing_required,
            details,
            required=True,
            evidence={"missing_required": missing_required, "missing_optional": missing_optional},
            fix_hint="Install backend Python dependencies in the active environment.",
            repair_recipe_id="doctor.runtime.python_deps",
        )

    def _required_python_version(self) -> tuple[int, int]:
        pyproject = self.settings.repo_root / "platform" / "backend" / "pyproject.toml"
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return (3, 11)
        match = re.search(r'requires-python\s*=\s*"[^"]*>=\s*(\d+)\.(\d+)', text)
        return (int(match.group(1)), int(match.group(2))) if match else (3, 11)

    def _node_version_check(self) -> dict[str, Any]:
        return self._versioned_binary_check("node", ["node", "--version"], minimum_major=18, required=True)

    def _npm_version_check(self) -> dict[str, Any]:
        return self._versioned_binary_check("npm", ["npm", "--version"], minimum_major=9, required=True)

    def _versioned_binary_check(self, name: str, command: list[str], *, minimum_major: int, required: bool) -> dict[str, Any]:
        path = shutil.which(command[0])
        if not path:
            return self._check(name, False, f"{name} not found; required>={minimum_major}", " ".join(command), required=required, repair_recipe_id=f"doctor.runtime.{name}")
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        except Exception as exc:
            return self._check(name, False, f"{path}; version check failed: {exc}", " ".join(command), required=required, repair_recipe_id=f"doctor.runtime.{name}")
        version_text = (result.stdout or result.stderr).strip()
        major = self._parse_major_version(version_text)
        ok = result.returncode == 0 and major is not None and major >= minimum_major
        details = f"path={path}; version={version_text or 'unknown'}; required>={minimum_major}"
        return self._check(
            name,
            ok,
            details,
            " ".join(command),
            required=required,
            evidence={"path": path, "version": version_text, "minimum_major": minimum_major},
            fix_hint=f"Install or select {name} >= {minimum_major}.",
            repair_recipe_id=f"doctor.runtime.{name}",
        )

    @staticmethod
    def _parse_major_version(value: str) -> int | None:
        match = re.search(r"(\d+)", value or "")
        return int(match.group(1)) if match else None

    def _package_managers_check(self) -> dict[str, Any]:
        names = ["npm", "pnpm", "yarn", "bun"]
        found = {name: shutil.which(name) for name in names}
        ok = bool(found.get("npm"))
        return self._check(
            "package_managers",
            ok,
            json.dumps({name: bool(path) for name, path in found.items()}, sort_keys=True),
            "which npm pnpm yarn bun",
            evidence={"found": found},
            fix_hint="At minimum npm must be available; pnpm/yarn/bun are optional.",
            repair_recipe_id="doctor.runtime.package_managers",
        )

    def _docker_binary_check(self) -> dict[str, Any]:
        path = shutil.which("docker")
        return self._check("docker", bool(path), path or "docker not found", "docker", required=False, evidence={"path": path}, fix_hint="Install Docker CLI if docker preview/runtime is needed.", repair_recipe_id="doctor.preview.docker")

    def _compose_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("docker_compose", False, "docker not found", "docker compose version", required=False, repair_recipe_id="doctor.preview.docker_compose")
        try:
            result = subprocess.run([docker, "compose", "version"], text=True, capture_output=True, timeout=5)
            return self._check("docker_compose", result.returncode == 0, (result.stdout or result.stderr).strip(), "docker compose version", required=False, repair_recipe_id="doctor.preview.docker_compose")
        except Exception as exc:
            return self._check("docker_compose", False, str(exc), "docker compose version", required=False, repair_recipe_id="doctor.preview.docker_compose")

    def _docker_daemon_quick_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        return self._check("docker_daemon", bool(docker), "docker CLI present; daemon probe skipped in quick scope" if docker else "docker CLI not found", "docker info", required=False, evidence={"probe": "skipped_quick"}, repair_recipe_id="doctor.preview.docker_daemon")

    def _docker_daemon_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("docker_daemon", False, "docker CLI not found", "docker info --format '{{.ServerVersion}}'", required=False, repair_recipe_id="doctor.preview.docker_daemon")
        try:
            result = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], text=True, capture_output=True, timeout=5)
        except Exception as exc:
            return self._check("docker_daemon", False, str(exc), "docker info --format '{{.ServerVersion}}'", required=False, repair_recipe_id="doctor.preview.docker_daemon")
        output = (result.stdout or result.stderr).strip()
        return self._check("docker_daemon", result.returncode == 0, output or "docker daemon did not respond", "docker info --format '{{.ServerVersion}}'", required=False, evidence={"server_version": output}, repair_recipe_id="doctor.preview.docker_daemon")

    def _playwright_check(self) -> dict[str, Any]:
        try:
            import playwright  # noqa: F401
            return self._check("playwright", True, "Python package import succeeded", "python -c 'import playwright'", required=False, repair_recipe_id="doctor.browser.playwright")
        except Exception as exc:
            return self._check("playwright", False, str(exc), "python -c 'import playwright'", required=False, fix_hint="Install Playwright Python package in the backend environment.", repair_recipe_id="doctor.browser.playwright")

    def _playwright_browsers_check(self) -> dict[str, Any]:
        if importlib.util.find_spec("playwright") is None:
            return self._check("playwright_browsers", False, "playwright package unavailable", "python -m playwright install", required=False, repair_recipe_id="doctor.browser.playwright_browsers")
        roots = self._playwright_browser_roots()
        installed = self._installed_browser_dirs(roots)
        if installed:
            return self._check("playwright_browsers", True, f"installed={', '.join(installed[:8])}; roots={', '.join(str(path) for path in roots)}", "python -m playwright install", required=False, evidence={"roots": [str(path) for path in roots], "installed": installed}, repair_recipe_id="doctor.browser.playwright_browsers")
        try:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run"], text=True, capture_output=True, timeout=10)
            output = (result.stdout or result.stderr).strip()
        except Exception as exc:
            output = str(exc)
        return self._check("playwright_browsers", False, f"no browser cache found; dry_run={output[:400]}", "python -m playwright install --dry-run", required=False, evidence={"roots": [str(path) for path in roots]}, fix_hint="Run Playwright browser installation outside generated agent commands.", repair_recipe_id="doctor.browser.playwright_browsers")

    def _playwright_browser_roots(self) -> list[Path]:
        configured = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        roots: list[Path] = []
        if configured and configured not in {"0", "false", "False"}:
            roots.append(Path(configured).expanduser())
        roots.extend([Path.home() / "Library" / "Caches" / "ms-playwright", Path.home() / ".cache" / "ms-playwright", self.settings.data_dir / ".cache" / "ms-playwright"])
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                unique.append(root)
                seen.add(key)
        return unique

    @staticmethod
    def _installed_browser_dirs(roots: list[Path]) -> list[str]:
        installed: list[str] = []
        for root in roots:
            try:
                if root.exists():
                    installed.extend(sorted(child.name for child in root.iterdir() if child.is_dir() and any(marker in child.name.lower() for marker in ("chromium", "firefox", "webkit"))))
            except OSError:
                continue
        return installed

    def _browser_availability_check(self) -> dict[str, Any]:
        package = importlib.util.find_spec("playwright") is not None
        roots = self._playwright_browser_roots()
        installed = self._installed_browser_dirs(roots)
        ok = package and bool(installed)
        details = f"playwright_package={package}; browser_cache_roots={[str(path) for path in roots]}; installed={installed[:8]}"
        return self._check("browser_availability", ok, details, "python -m playwright install --dry-run", required=False, evidence={"package": package, "roots": [str(path) for path in roots], "installed": installed}, repair_recipe_id="doctor.browser.availability")

    def _openai_check(self) -> dict[str, Any]:
        config = self.openai_client.configuration()
        return self._check("openai", bool(config.get("enabled")), "configured" if config.get("enabled") else "not configured", required=False, evidence={"enabled": bool(config.get("enabled"))}, repair_recipe_id="doctor.env.openai")

    def _model_access_check(self) -> dict[str, Any]:
        manager = getattr(self.openai_client, "model_manager", None)
        if manager is None:
            return self._check("model_access", False, "model manager unavailable", required=False, repair_recipe_id="doctor.models.access")
        try:
            status = manager.status()
            route = manager.select(role="agent_turn", model_profile=status.default_coding_profile, generation_mode="balanced")
        except Exception as exc:
            return self._check("model_access", False, f"model manager failed: {exc}", required=False, repair_recipe_id="doctor.models.access")
        provider = status.providers.get(route.selected_provider)
        provider_status = provider.status if provider is not None else "unknown"
        details = f"enabled={status.enabled}; provider={route.selected_provider}:{provider_status}; selected={route.selected_model}; profile={route.model_profile}; fallback={route.fallback_enabled}"
        return self._check("model_access", bool(status.enabled and route.status == "ready"), details, required=False, evidence={"enabled": status.enabled, "provider": route.selected_provider, "provider_status": provider_status, "selected_model": route.selected_model}, repair_recipe_id="doctor.models.access")

    def _writable_check(self, name: str, path: Path, repair_recipe_id: str = "doctor.storage.write_permission") -> dict[str, Any]:
        return self._check(name, os.access(path, os.W_OK), str(path), required=True, evidence={"path": str(path), "writable": os.access(path, os.W_OK)}, fix_hint=f"Ensure {path} exists and is writable.", repair_recipe_id=repair_recipe_id)

    def _writable_dirs_check(self) -> dict[str, Any]:
        paths = [self.settings.data_dir, self.settings.workspaces_dir, self.settings.exports_dir, self.settings.host_data_dir, self.settings.data_dir / ".sandbox" / "tmp", self.settings.data_dir / ".sandbox" / "home"]
        failed: list[str] = []
        passed: list[str] = []
        for path in paths:
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".doctor-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                passed.append(str(path))
            except Exception as exc:
                failed.append(f"{path}: {exc}")
        details = f"writable={len(passed)}/{len(paths)}; " + ("failed: " + "; ".join(failed) if failed else ", ".join(passed[:6]))
        return self._check("writable_dirs", not failed, details, required=True, evidence={"passed": passed, "failed": failed}, repair_recipe_id="doctor.storage.write_permission")

    def _disk_space_check(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.settings.data_dir)
        except Exception as exc:
            return self._check("disk_space", False, str(exc), required=True, repair_recipe_id="doctor.storage.disk_space")
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        ok = free_gb >= 1.0
        details = f"free={free_gb:.1f}GB total={total_gb:.1f}GB path={self.settings.data_dir}; required>=1GB"
        return self._check("disk_space", ok, details, required=True, evidence={"free_gb": round(free_gb, 2), "total_gb": round(total_gb, 2)}, repair_recipe_id="doctor.storage.disk_space")

    def _db_writable_check(self) -> dict[str, Any]:
        path = self.settings.data_dir / ".doctor-db-write.sqlite3"
        try:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS doctor_probe (id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO doctor_probe(value) VALUES (?)", ("ok",))
                row = conn.execute("SELECT value FROM doctor_probe ORDER BY id DESC LIMIT 1").fetchone()
                conn.commit()
            finally:
                conn.close()
            path.unlink(missing_ok=True)
            return self._check("db_writable", bool(row and row[0] == "ok"), f"path={path}; write_probe=ok", required=True, evidence={"path": str(path)}, repair_recipe_id="doctor.storage.db_writable")
        except Exception as exc:
            return self._check("db_writable", False, f"path={path}; error={exc}", required=True, repair_recipe_id="doctor.storage.db_writable")

    def _template_check(self) -> dict[str, Any]:
        required = [self.settings.template_dir / "miniapp" / "app" / "main.py", self.settings.template_dir / "docker" / "docker-compose.yml"]
        missing = [str(path) for path in required if not path.exists()]
        return self._check("template_integrity", not missing, "missing: " + ", ".join(missing) if missing else str(self.settings.template_dir), required=True, evidence={"missing": missing}, repair_recipe_id="doctor.template.integrity")

    def _template_hash_check(self) -> dict[str, Any]:
        root = self.settings.template_dir
        if not root.exists():
            return self._check("template_hash", False, f"missing template dir: {root}", required=True, repair_recipe_id="doctor.template.hash")
        digest = hashlib.sha256()
        file_count = 0
        ignored_dirs = {".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        try:
            paths = sorted(path for path in root.rglob("*") if path.is_file() and not any(part in ignored_dirs for part in path.relative_to(root).parts))
            for path in paths:
                rel = path.relative_to(root).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
                file_count += 1
        except Exception as exc:
            return self._check("template_hash", False, f"hash failed: {exc}", required=True, repair_recipe_id="doctor.template.hash")
        return self._check("template_hash", file_count > 0, f"sha256={digest.hexdigest()}; files={file_count}; root={root}", required=True, evidence={"sha256": digest.hexdigest(), "files": file_count}, repair_recipe_id="doctor.template.hash")

    def _template_manifest_check(self) -> dict[str, Any]:
        root = self.settings.template_dir
        required = [root / "miniapp" / "app" / "routes", root / "miniapp" / "app" / "static", root / "miniapp" / "app" / "main.py"]
        missing = [str(path) for path in required if not path.exists()]
        return self._check("template_manifest", not missing, "ok" if not missing else "missing: " + ", ".join(missing), required=True, evidence={"missing": missing}, repair_recipe_id="doctor.template.manifest")

    def _port_check(self) -> dict[str, Any]:
        port = int(self.settings.preview_port_base)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            result = sock.connect_ex(("127.0.0.1", port))
            return self._check("preview_port_base", result != 0, f"port {port} {'available' if result != 0 else 'in use'}", required=False, evidence={"port": port, "in_use": result == 0}, repair_recipe_id="doctor.preview.port")
        finally:
            sock.close()

    def _preview_port_range_check(self) -> dict[str, Any]:
        base = int(self.settings.preview_port_base)
        ports = list(range(base, base + 8))
        available: list[int] = []
        in_use: list[int] = []
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                result = sock.connect_ex(("127.0.0.1", port))
                (available if result != 0 else in_use).append(port)
            finally:
                sock.close()
        details = f"available={available}; in_use={in_use}; range={base}-{base + 7}"
        return self._check("preview_port_range", bool(available), details, required=False, evidence={"available": available, "in_use": in_use, "range": [base, base + 7]}, repair_recipe_id="doctor.preview.port_range")

    def _backend_routes_check(self) -> dict[str, Any]:
        route_file = self.settings.repo_root / "platform" / "backend" / "app" / "api" / "routes_workbench.py"
        if not route_file.exists():
            return self._check("backend_routes", False, f"missing {route_file}", required=True, repair_recipe_id="doctor.backend.routes")
        text = route_file.read_text(encoding="utf-8", errors="ignore")
        required_routes = ["/doctor", "/runs/{run_id}/timeline", "/runs/{run_id}/approvals", "/workspaces/{workspace_id}/files/search"]
        missing = [route for route in required_routes if route not in text]
        return self._check("backend_routes", not missing, "registered" if not missing else "missing: " + ", ".join(missing), required=True, evidence={"missing": missing}, repair_recipe_id="doctor.backend.routes")

    def _backend_imports_check(self) -> dict[str, Any]:
        modules = ["app.main", "app.api.routes_workbench", "app.modules.workspace_code_agent_runtime.runtime"]
        failed: list[str] = []
        for module in modules:
            try:
                importlib.import_module(module)
            except Exception as exc:
                failed.append(f"{module}: {exc.__class__.__name__}: {exc}")
        return self._check("backend_imports", not failed, "ok" if not failed else "; ".join(failed), required=True, evidence={"failed": failed}, repair_recipe_id="doctor.backend.imports")

    def _stale_backend_check(self) -> dict[str, Any]:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=1.0)
            conn.request("GET", "/doctor")
            response = conn.getresponse()
            body = response.read(240).decode("utf-8", errors="ignore")
            conn.close()
            ok = response.status < 500
            details = f"127.0.0.1:8000 returned {response.status}; {body[:120]}"
            return self._check("stale_backend_port_8000", ok, details, "GET http://127.0.0.1:8000/doctor", required=False, evidence={"status_code": response.status}, repair_recipe_id="doctor.backend.stale_port")
        except Exception as exc:
            return self._check("stale_backend_port_8000", True, f"no conflicting backend detected ({exc})", required=False, evidence={"conflict": False}, repair_recipe_id="doctor.backend.stale_port")

    def _preview_runtime_check(self) -> dict[str, Any]:
        workspace_root = self.settings.workspaces_dir
        runtime_dir = self.settings.runtime_dir
        checks = {
            "workspace_root_exists": workspace_root.exists(),
            "runtime_dir_exists": runtime_dir.exists(),
            "workspace_root_writable": os.access(workspace_root, os.W_OK) if workspace_root.exists() else False,
            "runtime_dir_writable": os.access(runtime_dir, os.W_OK) if runtime_dir.exists() else False,
        }
        return self._check("preview_runtime", all(checks.values()), json.dumps(checks, ensure_ascii=False, sort_keys=True), required=True, evidence=checks, repair_recipe_id="doctor.preview.runtime")

    def _workspace_preview_check(self, preview: dict[str, Any]) -> dict[str, Any]:
        if not preview:
            return self._check("workspace_preview", True, "no workspace preview record", required=False, evidence={"present": False}, repair_recipe_id="doctor.preview.workspace")
        status = str(preview.get("status") or "")
        ok = status not in {"error"}
        details = f"status={status}; stage={preview.get('stage')}; runtime={preview.get('runtime_mode')}; last_error={preview.get('last_error') or ''}"
        required = status == "error"
        return self._check("workspace_preview", ok, details, required=required, evidence={"status": status, "stage": preview.get("stage"), "runtime_mode": preview.get("runtime_mode"), "last_error": preview.get("last_error"), "proxy_port": preview.get("proxy_port")}, repair_recipe_id="doctor.preview.workspace")

    def _preview_container_quick_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        return self._check("preview_containers", True, "docker container probe skipped in quick scope" if docker else "docker not available; skipped", "docker ps", required=False, evidence={"probe": "skipped_quick"}, repair_recipe_id="doctor.preview.containers")

    def _preview_container_check(self) -> dict[str, Any]:
        docker = shutil.which("docker")
        if not docker:
            return self._check("preview_containers", True, "docker not available; skipped", "docker ps", required=False, repair_recipe_id="doctor.preview.containers")
        try:
            result = subprocess.run([docker, "ps", "--format", "{{.Names}}"], text=True, capture_output=True, timeout=5)
            names = [line for line in result.stdout.splitlines() if "grounded" in line or "miniapp" in line or "preview" in line]
            return self._check("preview_containers", result.returncode == 0, ", ".join(names[:12]) if names else "no matching preview containers", "docker ps --format '{{.Names}}'", required=False, evidence={"names": names[:12]}, repair_recipe_id="doctor.preview.containers")
        except Exception as exc:
            return self._check("preview_containers", False, str(exc), "docker ps --format '{{.Names}}'", required=False, repair_recipe_id="doctor.preview.containers")

    def _frontend_config_check(self) -> dict[str, Any]:
        candidates = [self.settings.template_dir / "miniapp" / "package.json", self.settings.repo_root / "miniapp" / "package.json"]
        existing = [str(path) for path in candidates if path.exists()]
        return self._check("frontend_config", bool(existing), "package configs: " + ", ".join(existing) if existing else "no frontend package.json found", required=False, evidence={"existing": existing}, repair_recipe_id="doctor.frontend.config")

    def _test_command_check(self) -> dict[str, Any]:
        exists = (self.settings.repo_root / "platform" / "backend" / "tests").exists()
        return self._check("platform_tests", exists, "pytest platform/backend/tests", required=True, repair_recipe_id="doctor.tests.platform")

    def _runtime_policy_files_check(self) -> dict[str, Any]:
        policy_dir = self.settings.runtime_dir / "policies"
        files = [policy_dir / "agent_exec_policy.json", policy_dir / "agent_hooks.json"]
        missing: list[str] = []
        invalid: list[str] = []
        loaded: list[str] = []
        for path in files:
            if not path.exists():
                missing.append(str(path))
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                invalid.append(f"{path}: {exc}")
                continue
            loaded.append(str(path))
        ok = not invalid and not any(path.endswith("agent_exec_policy.json") for path in missing)
        return self._check("runtime_policy_files", ok, f"loaded={loaded}; missing_optional={missing}; invalid={invalid}", required=True, evidence={"loaded": loaded, "missing_optional": missing, "invalid": invalid}, repair_recipe_id="doctor.policy.runtime_files")

    def _env_vars_check(self) -> dict[str, Any]:
        names = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "PLAYWRIGHT_BROWSERS_PATH", "PREVIEW_RUNTIME_MODE", "PLATFORM_BOOTSTRAP_STARTER_WORKSPACE"]
        present = {name: bool(os.getenv(name)) for name in names}
        details = "; ".join(f"{name}={'present' if value else 'missing'}" for name, value in present.items())
        return self._check("env_vars", True, details, required=False, evidence={"present": present}, fix_hint="Configure missing optional env vars only when the corresponding integration is needed.", repair_recipe_id="doctor.env.vars")

    @staticmethod
    def _doctor_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        required_failed: list[str] = []
        warnings: list[str] = []
        for check in checks:
            status = str(check.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            if check.get("required") and status != "passed":
                required_failed.append(str(check.get("name") or "unknown"))
            elif status != "passed":
                warnings.append(str(check.get("name") or "unknown"))
        return {"total": len(checks), "by_status": dict(sorted(counts.items())), "required_failed": required_failed, "warnings": warnings}

    @staticmethod
    def _doctor_sections(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups = [
            ("python", "Python/deps", {"python", "python_deps"}),
            ("node", "Node/npm", {"node", "npm", "package_managers"}),
            ("browser", "Browser/Playwright", {"playwright", "playwright_browsers", "browser_availability"}),
            ("backend", "Backend imports/API", {"backend_routes", "backend_imports", "stale_backend_process"}),
            ("frontend", "Frontend", {"frontend_config"}),
            ("preview", "Preview runtime", {"docker", "docker_compose", "docker_daemon", "preview_runtime", "workspace_preview", "preview_containers"}),
            ("ports", "Ports", {"preview_port_base", "backend_port_conflicts", "preview_port_range"}),
            ("storage", "Storage/DB", {"data_dir", "writable_dirs", "db_writable", "disk_space"}),
            ("templates", "Template integrity", {"template_integrity", "template_hash", "template_manifest"}),
            ("policy", "Policy config", {"runtime_policy_files", "exec_policy"}),
            ("models", "Model access", {"openai_config", "model_access"}),
            ("tests", "Test commands", {"platform_tests"}),
            ("env", "Environment variables", {"env_vars"}),
        ]
        sections: list[dict[str, Any]] = []
        for key, title, names in groups:
            items = [item for item in checks if item.get("name") in names]
            if not items:
                continue
            required_failed = [item for item in items if item.get("required") and item.get("status") != "passed"]
            failed_optional = [item for item in items if not item.get("required") and item.get("status") != "passed"]
            status = "failed" if required_failed else "warning" if failed_optional else "passed"
            sections.append({"key": key, "title": title, "status": status, "checks": [item.get("name") for item in items]})
        return sections

    def _repair_packet(self, *, workspace_id: str | None, run_id: str | None, checks: list[dict[str, Any]]) -> dict[str, Any]:
        failed = [item for item in checks if item.get("status") != "passed" and item.get("required")]
        if not failed:
            failed = [item for item in checks if item.get("status") != "passed"]
        if not failed:
            return {}
        first = failed[0]
        category = str(first.get("category") or "doctor")
        owner = "repair_worker"
        if category in {"backend", "preview", "ports"}:
            owner = "backend_api_worker"
        elif category in {"browser"}:
            owner = "test_verifier_worker"
        elif category in {"templates", "storage", "env", "runtime", "policy", "models"}:
            owner = "platform_runtime"
        target_files = ["miniapp/app/main.py"] if owner == "backend_api_worker" else []
        return {
            "schema": "grounded.repair_packet.v2",
            "source": "doctor",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "failure_class": f"doctor.{category}",
            "failure_signature": f"doctor.{first.get('name')}.{first.get('status')}",
            "issue_code": str(first.get("name") or "doctor_check_failed"),
            "severity": "high" if first.get("required") else "medium",
            "summary": str(first.get("details") or first.get("name") or "Doctor check failed."),
            "instruction": str(first.get("fix_hint") or "Fix the failing environment check, then rerun doctor."),
            "target_files": target_files,
            "owner": owner,
            "required_next_tool": "read_files" if target_files else "inspect_environment",
            "suggested_tool_after_read": "run_doctor",
            "proof_required": [f"doctor check {first.get('name')} passes"],
            "evidence": {"check": first, "blocking_checks": [item.get("name") for item in failed]},
        }

    @staticmethod
    def _default_fix_hint(check: dict[str, Any]) -> str:
        if check.get("fix_hint"):
            return str(check.get("fix_hint"))
        return f"Resolve doctor check `{check.get('name')}` and rerun doctor."

    @staticmethod
    def _default_repair_recipe(check: dict[str, Any]) -> str:
        if check.get("repair_recipe_id"):
            return str(check.get("repair_recipe_id"))
        return f"doctor.{check.get('category') or 'general'}.{check.get('name') or 'check'}"

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._redact_secret_value(str(key), cls._redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_]*token[A-Za-z0-9_]*=[^\s]+|[A-Za-z0-9_]*secret[A-Za-z0-9_]*=[^\s]+)", "[redacted]", value, flags=re.I)
        return value

    @staticmethod
    def _redact_secret_value(key: str, value: Any) -> Any:
        if re.search(r"(key|token|secret|password)", key, re.I) and value not in {False, True, None}:
            return "[redacted]"
        return value
