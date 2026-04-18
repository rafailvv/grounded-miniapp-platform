from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.models.domain import FixScopeEntry, RunCheckResult
from app.services.check_runner import CheckRunner

if TYPE_CHECKING:
    from app.services.fix_orchestrator import FixOrchestrator


class FixClassificationRuntime:
    def __init__(self, service: "FixOrchestrator") -> None:
        self.service = service

    @staticmethod
    def prefer_failure_class(existing: str | None, candidate: str | None) -> str | None:
        if not candidate:
            return existing
        if not existing:
            return candidate
        priority = {
            "runtime_manifest_route_missing": 100,
            "db_dependency_export_missing": 95,
            "backend_framework_mismatch": 94,
            "loading_first_root_surface": 92,
            "frontend_link_route_mismatch": 90,
            "router_not_registered": 88,
            "api_endpoint_missing": 86,
            "frontend_compile/type/import": 80,
            "backend_startup/import/schema": 78,
            "preview_runtime/docker_orchestration": 72,
            "route_api_contract_mismatch": 68,
            "runtime_preview_boot": 40,
            "build/runtime": 10,
        }
        existing_rank = priority.get(existing, 50)
        candidate_rank = priority.get(candidate, 50)
        return candidate if candidate_rank >= existing_rank else existing

    def specialized_failure_class(
        self,
        *,
        workspace_id: str,
        run_id: str,
        results: list[RunCheckResult],
        combined_text: str,
        implicated_files: list[str],
    ) -> str | None:
        lowered = combined_text.lower()
        issue_codes = {issue.code for issue in CheckRunner.failing_issues(results)}
        if {"build.loading_first_root_surface", "build.root_page_missing_business_surface"} & issue_codes:
            return "loading_first_root_surface"
        if (
            ("no module named 'flask'" in lowered or 'no module named "flask"' in lowered or "from flask import" in lowered)
            and any(path.startswith("miniapp/app/routes/") and path.endswith(".py") for path in implicated_files)
        ):
            return "backend_framework_mismatch"
        if "/api/runtime/" in lowered and "manifest" in lowered and ("404" in lowered or "not found" in lowered):
            return "runtime_manifest_route_missing"
        if ("cannot import name 'get_db'" in lowered or 'cannot import name "get_db"' in lowered or "import get_db" in lowered) and any(
            path.endswith(("/db.py", "/schemas.py", "/main.py")) for path in implicated_files
        ):
            return "db_dependency_export_missing"
        if "not declared in route_manifest.json" in lowered or ("/specialist/" in lowered and "404" in lowered):
            return "frontend_link_route_mismatch"
        missing_backend_routes = [
            issue
            for issue in CheckRunner.failing_issues(results)
            if issue.code == "connectivity.missing_backend_route"
        ]
        if missing_backend_routes:
            for issue in missing_backend_routes:
                location = str(issue.location or "")
                if location.startswith("miniapp/app/routes/") and (
                    self.service.workspace_service.draft_source_dir(workspace_id, run_id) / location
                ).exists():
                    return "router_not_registered"
            return "api_endpoint_missing"
        return None

    def implicated_files(
        self,
        workspace_id: str,
        run_id: str,
        text: str,
        existing_scope: list[FixScopeEntry],
    ) -> list[str]:
        candidates: list[str] = []
        for match in re.findall(r"((?:miniapp|docker)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)", text):
            candidates.append(match)
        for match in re.findall(r"(static/[A-Za-z0-9_./-]+\.(?:html|css|js))", text):
            candidates.append(f"miniapp/app/{match}")
        for module in re.findall(r"\"(@/[A-Za-z0-9_./-]+)\"", text):
            resolved = self.service._resolve_frontend_module(workspace_id, run_id, module)
            if resolved:
                candidates.append(resolved)
        for module in re.findall(r"'(app(?:\.[A-Za-z0-9_]+)+)'", text):
            resolved = self.service._resolve_backend_module(workspace_id, run_id, module)
            if resolved:
                candidates.append(resolved)
        for line in text.splitlines():
            if "cannot import name" in line.lower():
                backend_match = re.search(r"from '([^']+)'", line)
                if backend_match:
                    resolved = self.service._resolve_backend_module(workspace_id, run_id, backend_match.group(1))
                    if resolved:
                        candidates.append(resolved)
        candidates.extend(self.test_failure_implicated_paths(text))
        for entry in existing_scope:
            candidates.append(entry.file_path)
        unique: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip().lstrip("./")
            if not normalized or normalized in unique:
                continue
            if self.service._file_exists(workspace_id, run_id, normalized) or self.service._allow_missing_scope_path(normalized):
                unique.append(normalized)
        return unique[:24]

    @staticmethod
    def root_cause_summary(results: list[RunCheckResult], preview_details: dict[str, str], raw_error: str) -> str:
        for result in results:
            if result.status == "failed":
                line = next((item.strip() for item in result.logs if item.strip()), result.details or "")
                if line:
                    return line
        preview_error = str(preview_details.get("last_error") or "").strip()
        if preview_error:
            return preview_error
        raw = raw_error.strip()
        return raw.splitlines()[0] if raw else "Fix mode detected an unresolved build or runtime failure."

    @staticmethod
    def augment_failure_evidence_from_test_results(base_text: str, results: list[RunCheckResult]) -> str:
        markers: list[str] = []
        for result in results:
            if result.status != "failed":
                continue
            haystack = "\n".join([result.details or "", *result.logs]).lower()
            if result.name == "generated_app_python_tests":
                if "sessionlocal" in haystack:
                    markers.append("runtime.startup.missing_sessionlocal")
                if "roleprofilerecord" in haystack:
                    markers.append("runtime.startup.missing_role_profile_record")
                if "cannot import name" in haystack or "importerror" in haystack:
                    markers.append("runtime.startup.import_drift")
            if result.name == "generated_app_js_tests":
                if "not declared in route_manifest.json" in haystack:
                    markers.append("route_manifest.missing_declared_route")
                if "/profile" in haystack:
                    markers.append("route_manifest.missing_profile_route")
        if not markers:
            return base_text
        return "\n".join([base_text, *markers])

    def test_failure_implicated_paths(self, text: str) -> list[str]:
        lowered = text.lower()
        candidates: list[str] = []
        if "sessionlocal" in lowered:
            candidates.extend(["miniapp/app/main.py", "miniapp/app/db.py", "miniapp/tests/test_generated_app.py"])
        if "roleprofilerecord" in lowered:
            candidates.extend(
                [
                    "miniapp/app/db.py",
                    "miniapp/app/routes/profiles.py",
                    "miniapp/app/schemas.py",
                    "miniapp/tests/test_generated_app.py",
                ]
            )
        if "route_manifest.json" in lowered or "not declared in route_manifest.json" in lowered:
            candidates.extend(["miniapp/app/generated/route_manifest.json", "miniapp/tests/generated_app.test.mjs"])
        route_refs = re.findall(r"Route\s+([/A-Za-z0-9_{}:-]+)\s+referenced", text)
        route_refs.extend(match for _, match in re.findall(r"(['\"])(/[^'\"]+)\1", text))
        normalized_routes: list[str] = []
        for route in route_refs:
            route = str(route).strip()
            if not route.startswith("/") or route.startswith("/api/") or route in normalized_routes:
                continue
            normalized_routes.append(route)
        for route in normalized_routes:
            candidates.extend(self.page_triplet_candidates_for_route(route))
            role = route.strip("/").split("/", 1)[0]
            if role in {"client", "specialist", "manager"}:
                candidates.append(f"miniapp/app/routes/{role}.py")
        return candidates

    @staticmethod
    def page_triplet_candidates_for_route(route_path: str) -> list[str]:
        route = str(route_path or "").strip()
        if not route.startswith("/"):
            return []
        segments = [segment for segment in route.strip("/").split("/") if segment]
        if not segments:
            return []
        role = segments[0]
        if role not in {"client", "specialist", "manager"}:
            return []
        page_segments = segments[1:]
        if not page_segments:
            base = f"miniapp/app/static/{role}"
            return [f"{base}/index.html", f"{base}/styles.css", f"{base}/app.js"]
        folder = "_".join(segment.replace("-", "_") for segment in page_segments)
        base = f"miniapp/app/static/{role}/{folder}"
        return [f"{base}/index.html", f"{base}/styles.css", f"{base}/app.js"]

    @staticmethod
    def failure_signature(failure_class: str, root_cause_summary: str) -> str:
        normalized = re.sub(r"\s+", " ", f"{failure_class}:{root_cause_summary}".strip().lower())
        normalized = re.sub(r"\bline \d+\b", "line", normalized)
        normalized = re.sub(r"\(\d+,\d+\)", "(loc)", normalized)
        return normalized[:220]

    @staticmethod
    def error_excerpt(results: list[RunCheckResult], preview_details: dict[str, Any], raw_error: str) -> str:
        excerpt_lines: list[str] = []
        for result in results:
            if result.status == "failed":
                excerpt_lines.extend(result.logs[:12])
        if not excerpt_lines and preview_details.get("logs"):
            excerpt_lines.extend(preview_details.get("logs", [])[-12:])
        if not excerpt_lines and raw_error.strip():
            excerpt_lines = raw_error.strip().splitlines()[:12]
        return "\n".join(excerpt_lines[:12])

    @staticmethod
    def first_failing_command(results: list[RunCheckResult]) -> str | None:
        for result in results:
            if result.status == "failed" and result.command:
                return result.command
        return None

    @staticmethod
    def first_failing_exit_code(results: list[RunCheckResult]) -> int | None:
        for result in results:
            if result.status == "failed" and result.exit_code is not None:
                return result.exit_code
        return None

    @staticmethod
    def classify_failure_text(text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ("npm is not available", "docker compose is not available", "tooling is unavailable", "was not found on path")):
            return "tooling/platform_misconfiguration"
        if any(marker in lowered for marker in ("could not be opened in preview", "returned unusable preview content", "preview route smoke", "connection refused")):
            return "runtime_preview_boot"
        if any(
            marker in lowered
            for marker in (
                "has no exported member",
                "typescript",
                "argument of type",
                "cannot find module",
                "vite build",
                "static miniapp validation failed",
                "is not defined",
                "undefined leaves invalid state",
                "ts230",
                ".ts:",
                ".tsx:",
                ".js:",
            )
        ):
            return "frontend_compile/type/import"
        if any(marker in lowered for marker in ("traceback", "importerror", "modulenotfounderror", "cannot import name", "py_compile failed", "pydantic")):
            return "backend_startup/import/schema"
        if any(marker in lowered for marker in ("docker preview", "container ", "dependency failed to start", "health probe", "preview rebuild failed")):
            return "preview_runtime/docker_orchestration"
        if any(marker in lowered for marker in ("401", "403", "permission denied")):
            return "runtime_permission_mismatch"
        if any(marker in lowered for marker in ("fetch(", "/api/", "response status", "payload", "contract")):
            return "route_api_contract_mismatch"
        return "build/runtime"
