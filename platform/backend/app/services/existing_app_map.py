from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re
from typing import Any

from app.models.improve_mode import ExistingAppMapReport, ImproveModeReport, ImproveSlicePlan
from app.repositories.state_store import StateStore
from app.services.event_journal import EventJournalService
from app.services.generation_enhancements import MagicDocsBuilder
from app.services.lsp_context import LspContextService
from app.services.workspace.service import WorkspaceService
from app.validators.static_analysis import ROLE_ORDER, extract_declared_routes, extract_frontend_api_refs, normalize_api_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(path: Path, *, limit: int = 80000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


class ExistingAppMapService:
    def __init__(
        self,
        *,
        store: StateStore,
        workspace_service: WorkspaceService,
        lsp_context_service: LspContextService | None = None,
        event_journal_service: EventJournalService | None = None,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.lsp_context_service = lsp_context_service
        self.event_journal_service = event_journal_service

    def prepare_improve_run(self, *, workspace_id: str, run_id: str, prompt: str) -> dict[str, Any]:
        self._event(workspace_id, run_id, "improve.map.started", {"status": "started", "prompt": prompt[:400]})
        app_map = self.build_map(workspace_id=workspace_id, run_id=run_id)
        self._event(workspace_id, run_id, "improve.map.created", self._event_payload(app_map), source_ref=app_map.existing_app_map_ref)
        slice_plan = self.plan_slice(workspace_id=workspace_id, run_id=run_id, prompt=prompt, app_map=app_map)
        self._event(
            workspace_id,
            run_id,
            "improve.slice.blocked" if slice_plan.blocked_reasons else "improve.slice.planned",
            self._event_payload(slice_plan),
            source_ref=slice_plan.improve_slice_ref,
        )
        report = self.report(workspace_id=workspace_id, run_id=run_id)
        return {
            "map": app_map.model_dump(mode="json", by_alias=True),
            "slice": slice_plan.model_dump(mode="json", by_alias=True),
            "report": report,
        }

    def build_map(self, *, workspace_id: str, run_id: str) -> ExistingAppMapReport:
        workspace = self.workspace_service.get_workspace(workspace_id)
        source_dir = self._source_dir(workspace_id, run_id)
        route_graph_ref: str | None = None
        if self.lsp_context_service is not None:
            try:
                route_graph = self.lsp_context_service.route_graph(workspace_id=workspace_id, run_id=run_id, persist=True)
                route_graph_ref = str(route_graph.get("route_graph_ref") or "") or None
            except Exception:
                route_graph_ref = None
        ref = f"existing_app_map:{workspace_id}:{run_id}"
        prompt_contract_refs = self._prompt_contract_refs(workspace_id)
        known_proof_refs = self._known_proof_refs(workspace_id)
        magic_doc = MagicDocsBuilder.build(
            workspace=workspace,
            memory=self.store.get("reports", f"workspace_memory:{workspace_id}") or {},
            runs=[],
            source_dir=source_dir,
        )
        payload = ExistingAppMapReport(
            workspace_id=workspace_id,
            run_id=run_id,
            source_dir=str(source_dir),
            existing_app_map_ref=ref,
            role_pages=self._role_pages(source_dir),
            route_manifest=self._route_manifest(source_dir),
            api_endpoints=self._api_endpoints(source_dir),
            frontend_api_calls=self._frontend_api_calls(source_dir),
            persistence_models=self._persistence_models(source_dir),
            tests=self._files(source_dir / "miniapp" / "tests", suffixes={".py", ".js", ".mjs", ".ts"}),
            docs=self._files(source_dir / "docs", suffixes={".md", ".mdx", ".txt"}),
            known_proof_refs=known_proof_refs,
            prompt_contract_refs=prompt_contract_refs,
            lsp_route_graph_ref=route_graph_ref,
            created_at=_now(),
            next_sequence=self._next_sequence(ref),
        )
        data = payload.model_dump(mode="json", by_alias=True)
        data["magic_doc"] = {"path": magic_doc.get("path"), "content_excerpt": str(magic_doc.get("content") or "")[:2400]}
        self.store.upsert("reports", ref, data)
        return payload

    def plan_slice(self, *, workspace_id: str, run_id: str, prompt: str, app_map: ExistingAppMapReport | dict[str, Any] | None = None) -> ImproveSlicePlan:
        if app_map is None:
            app_map = self.read_map(workspace_id=workspace_id, run_id=run_id)
        map_payload = app_map.model_dump(mode="json", by_alias=True) if isinstance(app_map, ExistingAppMapReport) else dict(app_map)
        ref = f"improve_slice:{workspace_id}:{run_id}"
        prompt_l = prompt.lower()
        requested_roles = [role for role in ROLE_ORDER if role in prompt_l]
        if not requested_roles:
            requested_roles = [
                str(page.get("role"))
                for page in map_payload.get("role_pages") or []
                if str(page.get("role") or "") in ROLE_ORDER and str(page.get("role") or "") in prompt_l
            ]
        role_files = [
            str(page.get("file"))
            for page in map_payload.get("role_pages") or []
            if str(page.get("file") or "").strip() and (not requested_roles or page.get("role") in requested_roles)
        ]
        role_files.extend(self._role_support_files(workspace_id, run_id, requested_roles or list(ROLE_ORDER), prompt_l))
        api_files = self._api_files_for_prompt(workspace_id, run_id, prompt_l)
        test_files = [str(path) for path in map_payload.get("tests") or []][:6]
        connected_files = list(dict.fromkeys([*role_files, *api_files, *test_files]))
        if not connected_files:
            connected_files = self._fallback_connected_files(map_payload)
        protected_files = sorted(
            set(self._all_map_files(map_payload)) - set(connected_files)
        )[:200]
        blocked_reasons: list[str] = []
        if not (map_payload.get("role_pages") or map_payload.get("api_endpoints") or map_payload.get("frontend_api_calls")):
            blocked_reasons.append("existing_app_map_empty")
        required_proof = ["lsp_static_diagnostics", "api_workflow_smoke", "browser_flow_smoke"]
        if requested_roles:
            required_proof.append("role_scope_smoke")
        impact = self._impact(prompt_l)
        payload = ImproveSlicePlan(
            status="blocked" if blocked_reasons else "planned",
            workspace_id=workspace_id,
            run_id=run_id,
            improve_slice_ref=ref,
            existing_app_map_ref=str(map_payload.get("existing_app_map_ref") or f"existing_app_map:{workspace_id}:{run_id}"),
            requested_improvement=prompt.strip(),
            connected_files=connected_files[:40],
            protected_files=protected_files,
            allowed_roles=list(dict.fromkeys(requested_roles)),
            expected_behavioral_impact=impact,
            required_proof=required_proof,
            risk_level="high" if api_files and role_files else "medium" if api_files else "low",
            blocked_reasons=blocked_reasons,
            created_at=_now(),
            next_sequence=self._next_sequence(ref),
        )
        self.store.upsert("reports", ref, payload.model_dump(mode="json", by_alias=True))
        return payload

    def read_map(self, *, workspace_id: str, run_id: str) -> dict[str, Any]:
        ref = f"existing_app_map:{workspace_id}:{run_id}"
        payload = self.store.get("reports", ref)
        if isinstance(payload, dict):
            return payload
        return self.build_map(workspace_id=workspace_id, run_id=run_id).model_dump(mode="json", by_alias=True)

    def read_slice(self, *, workspace_id: str, run_id: str, prompt: str = "") -> dict[str, Any]:
        ref = f"improve_slice:{workspace_id}:{run_id}"
        payload = self.store.get("reports", ref)
        if isinstance(payload, dict):
            return payload
        return self.plan_slice(workspace_id=workspace_id, run_id=run_id, prompt=prompt).model_dump(mode="json", by_alias=True)

    def report(self, *, workspace_id: str, run_id: str) -> dict[str, Any]:
        map_payload = self.read_map(workspace_id=workspace_id, run_id=run_id)
        slice_payload = self.read_slice(workspace_id=workspace_id, run_id=run_id)
        ref = f"improve_mode:{workspace_id}:{run_id}"
        payload = ImproveModeReport(
            status="blocked" if (slice_payload.get("blocked_reasons") or []) else "ready",
            workspace_id=workspace_id,
            run_id=run_id,
            existing_app_map_ref=str(map_payload.get("existing_app_map_ref") or f"existing_app_map:{workspace_id}:{run_id}"),
            improve_slice_ref=str(slice_payload.get("improve_slice_ref") or f"improve_slice:{workspace_id}:{run_id}"),
            map=map_payload,
            slice=slice_payload,
            context_refs={
                "existing_app_map": map_payload.get("existing_app_map_ref"),
                "improve_slice": slice_payload.get("improve_slice_ref"),
                "lsp_route_graph": map_payload.get("lsp_route_graph_ref"),
                "prompt_contracts": map_payload.get("prompt_contract_refs") or [],
            },
            run_refs={"run": run_id},
            proof_refs=map_payload.get("known_proof_refs") or {},
            created_at=_now(),
            next_sequence=self._next_sequence(ref),
        ).model_dump(mode="json", by_alias=True)
        self.store.upsert("reports", ref, payload)
        return payload

    def _source_dir(self, workspace_id: str, run_id: str) -> Path:
        if self.workspace_service.draft_exists(workspace_id, run_id):
            return self.workspace_service.draft_source_dir(workspace_id, run_id)
        return self.workspace_service.source_dir(workspace_id)

    def _role_pages(self, source_dir: Path) -> list[dict[str, Any]]:
        root = source_dir / "miniapp" / "app" / "static"
        pages: list[dict[str, Any]] = []
        for html in sorted(root.rglob("index.html")) if root.exists() else []:
            try:
                rel = html.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            role = parts[0] if parts else ""
            if role not in ROLE_ORDER:
                continue
            route_tail = "/".join(parts[1:-1])
            route = f"/{role}" + (f"/{route_tail}" if route_tail else "")
            pages.append({"role": role, "route": route, "file": f"miniapp/app/static/{rel.as_posix()}", "title": self._html_title(html)})
        return pages

    def _route_manifest(self, source_dir: Path) -> dict[str, Any]:
        path = source_dir / "miniapp" / "app" / "static" / "route_manifest.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {"status": "invalid_json", "path": "miniapp/app/static/route_manifest.json"}

    def _api_endpoints(self, source_dir: Path) -> list[dict[str, Any]]:
        endpoints = []
        for method, path in sorted(extract_declared_routes(source_dir / "miniapp" / "app" / "routes", api_only=True)):
            endpoints.append({"method": method, "path": path, "resource": self._resource_from_path(path)})
        return endpoints

    def _frontend_api_calls(self, source_dir: Path) -> list[dict[str, Any]]:
        root = source_dir / "miniapp" / "app" / "static"
        calls: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*.js")) if root.exists() else []:
            rel = path.relative_to(source_dir).as_posix()
            for method, api_path in sorted(extract_frontend_api_refs(_read_text(path))):
                calls.append({"method": method, "path": api_path, "file": rel, "resource": self._resource_from_path(api_path)})
        return calls

    def _persistence_models(self, source_dir: Path) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        for path in sorted((source_dir / "miniapp" / "app").rglob("*.py")) if (source_dir / "miniapp" / "app").exists() else []:
            text = _read_text(path)
            if "mapped_column" not in text and "Column(" not in text and "__tablename__" not in text:
                continue
            rel = path.relative_to(source_dir).as_posix()
            for match in re.finditer(r"class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*(?:Base|DeclarativeBase)[^)]*\):", text):
                models.append({"name": match.group("name"), "file": rel})
            for table in re.finditer(r"__tablename__\s*=\s*['\"](?P<table>[^'\"]+)['\"]", text):
                if not any(item.get("table") == table.group("table") and item.get("file") == rel for item in models):
                    models.append({"table": table.group("table"), "file": rel})
        return models

    def _files(self, root: Path, *, suffixes: set[str]) -> list[str]:
        if not root.exists():
            return []
        if root.name == "tests" and root.parent.name == "miniapp":
            source_dir = root.parent.parent
        else:
            source_dir = root.parent
        files: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                try:
                    files.append(path.relative_to(source_dir).as_posix())
                except ValueError:
                    files.append(path.as_posix())
        return files[:80]

    def _prompt_contract_refs(self, workspace_id: str) -> list[str]:
        refs: list[str] = []
        for key, payload in self.store.items("reports"):
            if str(key).startswith(f"prompt_contract:{workspace_id}:") and isinstance(payload, dict):
                refs.append(str(key))
        return sorted(refs)[-8:]

    def _known_proof_refs(self, workspace_id: str) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        for key, payload in self.store.items("runs"):
            if not isinstance(payload, dict) or payload.get("workspace_id") != workspace_id:
                continue
            for field in ("browser_proof_ref", "browser_replay_proof_ref", "guardian_gate_ref", "draft_gate_ref", "lsp_context_ref"):
                value = payload.get(field)
                if value:
                    refs.setdefault(field, []).append(value)
        return {key: value[-5:] for key, value in refs.items()}

    def _role_support_files(self, workspace_id: str, run_id: str, roles: list[str], prompt_l: str) -> list[str]:
        source_dir = self._source_dir(workspace_id, run_id)
        files: list[str] = []
        for role in roles:
            root = source_dir / "miniapp" / "app" / "static" / role
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".js", ".css", ".html"}:
                    files.append(path.relative_to(source_dir).as_posix())
        if any(token in prompt_l for token in ("style", "visual", "mobile", "ui", "polish", "spacing", "layout")):
            shared = source_dir / "miniapp" / "app" / "static" / "shared"
            for path in sorted(shared.rglob("*.css")) if shared.exists() else []:
                files.append(path.relative_to(source_dir).as_posix())
        return files

    def _api_files_for_prompt(self, workspace_id: str, run_id: str, prompt_l: str) -> list[str]:
        if not any(token in prompt_l for token in ("api", "status", "save", "persist", "field", "form", "workflow", "flow", "data", "order", "request")):
            return []
        source_dir = self._source_dir(workspace_id, run_id)
        files = []
        routes = source_dir / "miniapp" / "app" / "routes"
        for path in sorted(routes.glob("*.py")) if routes.exists() else []:
            if path.name != "__init__.py":
                files.append(path.relative_to(source_dir).as_posix())
        for rel in ("miniapp/app/schemas.py", "miniapp/app/db.py"):
            if (source_dir / rel).exists():
                files.append(rel)
        return files

    def _fallback_connected_files(self, map_payload: dict[str, Any]) -> list[str]:
        files = [str(page.get("file")) for page in map_payload.get("role_pages") or [] if page.get("file")]
        files.extend(str(item.get("file")) for item in map_payload.get("frontend_api_calls") or [] if item.get("file"))
        files.extend(str(item.get("file")) for item in map_payload.get("persistence_models") or [] if item.get("file"))
        files.extend(str(path) for path in map_payload.get("tests") or [])
        return list(dict.fromkeys(files))[:12]

    def _all_map_files(self, map_payload: dict[str, Any]) -> list[str]:
        files = [str(page.get("file")) for page in map_payload.get("role_pages") or [] if page.get("file")]
        files.extend(str(item.get("file")) for item in map_payload.get("frontend_api_calls") or [] if item.get("file"))
        files.extend(str(item.get("file")) for item in map_payload.get("persistence_models") or [] if item.get("file"))
        files.extend(str(path) for path in map_payload.get("tests") or [])
        files.extend(str(path) for path in map_payload.get("docs") or [])
        return list(dict.fromkeys(files))

    @staticmethod
    def _impact(prompt_l: str) -> list[str]:
        impact: list[str] = []
        if any(token in prompt_l for token in ("ui", "visual", "style", "mobile", "layout", "copy")):
            impact.append("role_ui")
        if any(token in prompt_l for token in ("api", "save", "persist", "status", "field", "data")):
            impact.append("api_persistence")
        if any(token in prompt_l for token in ("test", "proof", "check")):
            impact.append("tests_proof")
        return impact or ["focused_behavior"]

    @staticmethod
    def _html_title(path: Path) -> str:
        text = _read_text(path, limit=12000)
        match = re.search(r"<title[^>]*>(?P<title>.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        return re.sub(r"\s+", " ", match.group("title")).strip() if match else ""

    @staticmethod
    def _resource_from_path(path: str) -> str:
        parts = [part for part in normalize_api_path(path).split("/") if part and not part.startswith("{")]
        return parts[1] if len(parts) > 1 and parts[0] == "api" else ""

    @staticmethod
    def _event_payload(model: Any) -> dict[str, Any]:
        payload = model.model_dump(mode="json", by_alias=True) if hasattr(model, "model_dump") else dict(model)
        return {
            "status": payload.get("status"),
            "existing_app_map_ref": payload.get("existing_app_map_ref"),
            "improve_slice_ref": payload.get("improve_slice_ref"),
            "connected_files": payload.get("connected_files", [])[:24],
            "blocked_reasons": payload.get("blocked_reasons", []),
        }

    def _next_sequence(self, ref: str) -> int:
        current = self.store.get("reports", ref)
        if isinstance(current, dict):
            return int(current.get("next_sequence") or 0) + 1
        return 1

    def _event(self, workspace_id: str, run_id: str, event_type: str, payload: dict[str, Any], *, source_ref: str | None = None) -> None:
        if self.event_journal_service is None:
            return
        self.event_journal_service.append_run(
            workspace_id=workspace_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            actor="system",
            summary=event_type.replace(".", " "),
            source_ref=source_ref,
            idempotency_key=f"{event_type}:{workspace_id}:{run_id}:{source_ref or 'inline'}",
        )
