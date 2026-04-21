from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from app.models.artifacts import ValidationIssue
from app.models.domain import RunCheckResult


class GenerationPreflightValidation:
    @staticmethod
    def _normalized_imported_schema_names(content: str) -> set[str]:
        imported_names: set[str] = set()
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+\((.*?)\)", content, flags=re.DOTALL):
            parts = [part.strip() for part in match.group(1).replace("\n", " ").split(",") if part.strip()]
            for part in parts:
                imported_names.add(part.split(" as ", 1)[0].strip())
        for match in re.finditer(r"from\s+app\.schemas\s+import\s+([A-Za-z0-9_, ]+)", content):
            parts = [part.strip() for part in match.group(1).split(",") if part.strip()]
            for part in parts:
                imported_names.add(part.split(" as ", 1)[0].strip())
        return imported_names

    @classmethod
    def preflight_generation_issues(
        cls,
        *,
        draft_root: Path,
        changed_files: list[str],
        page_graph: dict[str, Any],
        role_scope: list[str],
        normalize_local_route_ref: Any,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        issues.extend(cls.preflight_backend_syntax_issues(draft_root, changed_files))
        issues.extend(cls.preflight_frontend_syntax_issues(draft_root, changed_files))
        issues.extend(cls.preflight_profile_schema_issues(draft_root))
        issues.extend(cls.preflight_route_schema_issues(draft_root))
        issues.extend(cls.preflight_route_manifest_link_issues(draft_root, page_graph, role_scope, normalize_local_route_ref=normalize_local_route_ref))
        deduped: list[ValidationIssue] = []
        seen: set[tuple[str, str]] = set()
        for issue in issues:
            key = (issue.code, issue.location)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped

    @classmethod
    def _preflight_generation_issues(cls, *args: Any, **kwargs: Any) -> list[ValidationIssue]:
        return cls.preflight_generation_issues(*args, **kwargs)

    @staticmethod
    def preflight_backend_syntax_issues(draft_root: Path, changed_files: list[str]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for file_path in changed_files:
            if not file_path.startswith("miniapp/app/") or not file_path.endswith(".py"):
                continue
            absolute = draft_root / file_path
            if not absolute.exists():
                continue
            try:
                compile(absolute.read_text(encoding="utf-8"), file_path, "exec")
            except SyntaxError as exc:
                issues.append(ValidationIssue(code="preflight.python_syntax_error", message=f"{file_path} has invalid Python syntax before full checks: {exc.msg}.", severity="high", location=file_path, blocking=True))
        return issues

    _preflight_backend_syntax_issues = preflight_backend_syntax_issues

    @staticmethod
    def preflight_frontend_syntax_issues(draft_root: Path, changed_files: list[str]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        js_files = [file_path for file_path in changed_files if file_path.startswith("miniapp/app/static/") and file_path.endswith(".js")]
        if not js_files:
            return issues
        node_bin = shutil.which("node")
        if not node_bin:
            return issues
        for file_path in js_files:
            absolute = draft_root / file_path
            if not absolute.exists():
                continue
            completed = subprocess.run([node_bin, "--check", str(absolute)], capture_output=True, text=True)
            if completed.returncode == 0:
                continue
            logs = (completed.stderr or completed.stdout or "").strip().splitlines()
            message = logs[0] if logs else f"{file_path} has invalid JavaScript syntax before full checks."
            issues.append(ValidationIssue(code="preflight.javascript_syntax_error", message=f"{file_path} has invalid JavaScript syntax before full checks: {message}", severity="high", location=file_path, blocking=True))
        return issues

    _preflight_frontend_syntax_issues = preflight_frontend_syntax_issues

    @staticmethod
    def preflight_profile_schema_issues(draft_root: Path) -> list[ValidationIssue]:
        profiles_path = draft_root / "miniapp/app/routes/profiles.py"
        schemas_path = draft_root / "miniapp/app/schemas.py"
        if not profiles_path.exists() or not schemas_path.exists():
            return []
        profiles_content = profiles_path.read_text(encoding="utf-8")
        schemas_content = schemas_path.read_text(encoding="utf-8")
        issues: list[ValidationIssue] = []
        if "from app.schemas import" in profiles_content and "RoleProfile" in profiles_content and "class RoleProfile" not in schemas_content:
            issues.append(ValidationIssue(code="preflight.profile_schema_contract", message="routes/profiles.py imports RoleProfile, but schemas.py does not define it.", severity="high", location="miniapp/app/schemas.py", blocking=True))
        if "from app.schemas import" in profiles_content and "AppRole" in profiles_content and "AppRole =" not in schemas_content:
            issues.append(ValidationIssue(code="preflight.profile_schema_contract", message="routes/profiles.py imports AppRole, but schemas.py does not define it.", severity="high", location="miniapp/app/schemas.py", blocking=True))
        return issues

    _preflight_profile_schema_issues = preflight_profile_schema_issues

    @staticmethod
    def preflight_route_schema_issues(draft_root: Path) -> list[ValidationIssue]:
        routes_dir = draft_root / "miniapp/app/routes"
        schemas_path = draft_root / "miniapp/app/schemas.py"
        if not routes_dir.exists() or not schemas_path.exists():
            return []
        schemas_content = schemas_path.read_text(encoding="utf-8")
        issues: list[ValidationIssue] = []
        for route_file in routes_dir.glob("*.py"):
            content = route_file.read_text(encoding="utf-8")
            imported_names = GenerationPreflightValidation._normalized_imported_schema_names(content)
            missing = sorted(name for name in imported_names if f"class {name}" not in schemas_content and f"{name} =" not in schemas_content)
            if missing:
                issues.append(ValidationIssue(code="preflight.route_schema_contract", message=f"{route_file.name} imports schemas that do not exist in schemas.py: {', '.join(missing)}.", severity="high", location="miniapp/app/schemas.py", blocking=True))
        return issues

    _preflight_route_schema_issues = preflight_route_schema_issues

    @classmethod
    def preflight_route_manifest_link_issues(
        cls,
        draft_root: Path,
        page_graph: dict[str, Any],
        role_scope: list[str],
        *,
        normalize_local_route_ref: Any | None = None,
    ) -> list[ValidationIssue]:
        manifest_path = draft_root / "miniapp/app/generated/route_manifest.json"
        if not manifest_path.exists():
            return []
        try:
            route_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        normalize_local_route_ref = normalize_local_route_ref or cls._normalize_local_route_ref
        declared_routes: set[str] = set()
        for role in role_scope:
            for page in (((route_manifest.get("roles") or {}).get(role) or {}).get("pages") or []):
                if isinstance(page, dict):
                    route_path = str(page.get("route_path") or "").strip()
                    if route_path:
                        declared_routes.add(normalize_local_route_ref(route_path))
        issues: list[ValidationIssue] = []
        href_pattern = re.compile(r"""href=["']([^"']+)["']""")
        for role in role_scope:
            for page in (((page_graph.get("roles") or {}).get(role) or {}).get("pages") or []):
                if not isinstance(page, dict):
                    continue
                file_path = str(page.get("file_path") or "")
                if not file_path.endswith(".html"):
                    continue
                absolute = draft_root / file_path
                if not absolute.exists():
                    continue
                content = absolute.read_text(encoding="utf-8")
                for route_ref in href_pattern.findall(content):
                    route_ref = str(route_ref).strip()
                    if not route_ref.startswith("/") or route_ref.startswith("/api/") or route_ref.startswith("/static/"):
                        continue
                    normalized_route_ref = normalize_local_route_ref(route_ref)
                    if normalized_route_ref not in declared_routes:
                        issues.append(ValidationIssue(code="preflight.route_manifest_link_mismatch", message=f"{file_path} references {route_ref}, but route_manifest.json does not declare it.", severity="high", location=file_path, blocking=True))
        return issues

    _preflight_route_manifest_link_issues = preflight_route_manifest_link_issues

    @staticmethod
    def preflight_check_results(issues: list[ValidationIssue]) -> list[RunCheckResult]:
        syntax_logs = [json.dumps(issue.model_dump(mode="json"), ensure_ascii=False) for issue in issues if issue.code == "preflight.python_syntax_error"]
        contract_logs = [json.dumps(issue.model_dump(mode="json"), ensure_ascii=False) for issue in issues if issue.code != "preflight.python_syntax_error"]
        results: list[RunCheckResult] = []
        if contract_logs:
            results.append(RunCheckResult(name="connectivity_validators", status="failed", details="Preflight contract checks failed before running the full check pipeline.", command="preflight.contract_checks", exit_code=1, logs=contract_logs))
        else:
            results.append(RunCheckResult(name="connectivity_validators", status="passed", details="Preflight contract checks passed.", command="preflight.contract_checks", exit_code=0, logs=[]))
        if syntax_logs:
            results.append(RunCheckResult(name="changed_files_static", status="failed", details="Preflight backend syntax checks failed before running the full check pipeline.", command="preflight.python_syntax_checks", exit_code=1, logs=syntax_logs))
        else:
            results.append(RunCheckResult(name="changed_files_static", status="passed", details="Preflight backend syntax checks passed.", command="preflight.python_syntax_checks", exit_code=0, logs=[]))
        results.extend(
            [
                RunCheckResult(name="generated_app_python_tests", status="skipped", details="Generated Python app tests skipped because preflight issues already failed.", command="preflight.skip", exit_code=None, logs=[]),
                RunCheckResult(name="generated_app_js_tests", status="skipped", details="Generated JS app tests skipped because preflight issues already failed.", command="preflight.skip", exit_code=None, logs=[]),
                RunCheckResult(name="preview_boot_smoke", status="skipped", details="Preview smoke skipped because preflight issues already failed.", command="preflight.skip", exit_code=None, logs=[]),
                RunCheckResult(name="preview_connectivity_smoke", status="skipped", details="Preview connectivity smoke skipped because preflight issues already failed.", command="preflight.skip", exit_code=None, logs=[]),
            ]
        )
        return results

    _preflight_check_results = preflight_check_results

    @staticmethod
    def _normalize_local_route_ref(route_ref: str) -> str:
        normalized = str(route_ref or "").strip()
        if not normalized:
            return normalized
        normalized = re.sub(r"\$\{[^/]+\}", "sample", normalized)
        normalized = re.sub(r"\{[^/]+\}", "sample", normalized)
        normalized = re.sub(r":[^/]+", "sample", normalized)
        if normalized != "/" and normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        for role in ("client", "specialist", "manager"):
            if normalized == f"/{role}/root":
                return f"/{role}"
        return normalized
