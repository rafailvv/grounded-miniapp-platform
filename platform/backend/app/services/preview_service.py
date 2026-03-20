from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.core.config import Settings
from app.models.domain import PreviewRecord
from app.repositories.state_store import StateStore
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.workspace_log_service import WorkspaceLogService
from app.services.workspace_service import WorkspaceService

ROLE_ORDER = ("client", "specialist", "manager")
STARTING_STALE_AFTER_SEC = 45
PREVIEW_HTTP_PROBE_TIMEOUT_SEC = 1.5


class PreviewService:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        workspace_service: WorkspaceService,
        runtime_manager: PreviewRuntimeManager,
        workspace_log_service: WorkspaceLogService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service
        self.runtime_manager = runtime_manager
        self.workspace_log_service = workspace_log_service

    def _append_log(self, preview: PreviewRecord, message: str) -> None:
        preview.logs.append(message)
        preview.logs = preview.logs[-240:]
        self.workspace_log_service.append(preview.workspace_id, source="preview", message=message)

    def _persist(self, preview: PreviewRecord) -> None:
        preview.updated_at = datetime.now(timezone.utc)
        self.store.upsert("previews", preview.workspace_id, preview.model_dump(mode="json"))

    def start(self, workspace_id: str, source_dir: Path | None = None, draft_run_id: str | None = None) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        source_dir = source_dir or self.workspace_service.source_dir(workspace_id)
        runtime_mode = self.runtime_manager.preferred_mode()
        preview.runtime_mode = runtime_mode
        preview.draft_run_id = draft_run_id
        preview.status = "starting"
        preview.stage = "starting"
        preview.progress_percent = max(preview.progress_percent, 8)
        preview.last_error = None
        self._persist(preview)
        started_at = datetime.now(timezone.utc)
        try:
            self._append_log(preview, f"Preview start requested. mode={runtime_mode}.")
            proxy_port = self._select_proxy_port(workspace_id, preview, source_dir)
            self._append_log(preview, f"Selected preview port {proxy_port}.")
            preview.progress_percent = max(preview.progress_percent, 24)
            self._persist(preview)
            project_name, logs = self.runtime_manager.start(workspace_id, source_dir, proxy_port)
            preview.proxy_port = proxy_port
            preview.project_name = project_name
            preview.url = self.runtime_manager.preview_url(proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(proxy_port)
            preview.logs.extend(logs or [f"Docker preview started on port {proxy_port}."])
            preview.logs = preview.logs[-240:]
            preview.status = "running"
            preview.stage = "running"
            preview.progress_percent = 100
            preview.started_at = preview.started_at or datetime.now(timezone.utc)
            preview.last_error = None
            preview.latency_breakdown["last_start_ms"] = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            self._append_log(preview, f"Preview runtime is healthy at {preview.url}.")
        except Exception as exc:
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.status = "error"
            preview.stage = "error"
            preview.progress_percent = 100
            preview.last_error = str(exc)
            self._append_log(preview, f"Docker preview failed: {exc}")
        self._persist(preview)
        return preview

    def ensure_started(self, workspace_id: str, *, force_rebuild: bool = False) -> PreviewRecord:
        preview = self._reconcile_runtime_state(self._get_or_create(workspace_id), workspace_id)
        if preview.status == "running" and preview.url and not force_rebuild:
            return preview
        if (
            preview.status == "starting"
            and preview.stage in {"starting", "rebuilding", "health_check"}
            and not self._is_stale_starting_preview(preview)
        ):
            return preview
        if preview.status == "starting" and preview.stage in {"starting", "rebuilding", "health_check"}:
            self._append_log(preview, "Stale preview bootstrap detected. Restarting preview ensure flow.")

        preview.status = "starting"
        preview.stage = "rebuilding" if force_rebuild else "starting"
        preview.progress_percent = 6
        preview.last_error = None
        self._append_log(preview, f"Preview ensure requested. force_rebuild={force_rebuild}.")
        self._persist(preview)

        worker = threading.Thread(
            target=self._ensure_worker,
            args=(workspace_id, force_rebuild),
            daemon=True,
        )
        worker.start()
        return preview

    def rebuild(
        self,
        workspace_id: str,
        source_dir: Path | None = None,
        draft_run_id: str | None = None,
    ) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        source_dir = source_dir or self.workspace_service.source_dir(workspace_id)
        runtime_mode = self.runtime_manager.preferred_mode()
        preview.runtime_mode = runtime_mode
        preview.draft_run_id = draft_run_id
        preview.status = "starting"
        preview.stage = "rebuilding"
        preview.progress_percent = max(preview.progress_percent, 12)
        preview.last_error = None
        self._persist(preview)
        started_at = datetime.now(timezone.utc)
        try:
            self._append_log(preview, f"Preview rebuild requested. mode={runtime_mode}.")
            proxy_port = self._select_proxy_port(workspace_id, preview, source_dir)
            self._append_log(preview, f"Using preview port {proxy_port} for rebuild.")
            preview.progress_percent = max(preview.progress_percent, 28)
            self._persist(preview)
            logs = self.runtime_manager.rebuild(workspace_id, source_dir, proxy_port)
            preview.proxy_port = proxy_port
            preview.project_name = self.runtime_manager.project_name(workspace_id)
            preview.url = self.runtime_manager.preview_url(proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(proxy_port)
            preview.logs.extend(logs or ["Docker preview rebuilt."])
            preview.logs = preview.logs[-240:]
            preview.status = "running"
            preview.stage = "running"
            preview.progress_percent = 100
            preview.started_at = preview.started_at or datetime.now(timezone.utc)
            preview.last_error = None
            preview.latency_breakdown["last_rebuild_ms"] = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            self._append_log(preview, f"Preview rebuild completed and runtime is healthy at {preview.url}.")
        except Exception as exc:
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.status = "error"
            preview.stage = "error"
            preview.progress_percent = 100
            preview.last_error = str(exc)
            self._append_log(preview, f"Docker preview rebuild failed: {exc}")
        self._persist(preview)
        return preview

    def rebuild_async(
        self,
        workspace_id: str,
        source_dir: Path | None = None,
        draft_run_id: str | None = None,
        on_complete: Callable[[PreviewRecord], None] | None = None,
    ) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        preview.runtime_mode = self.runtime_manager.preferred_mode()
        preview.draft_run_id = draft_run_id
        preview.status = "starting"
        preview.stage = "rebuilding"
        preview.progress_percent = 10
        preview.last_error = None
        self._append_log(preview, "Queued asynchronous preview rebuild.")
        self._persist(preview)

        worker = threading.Thread(
            target=self._rebuild_worker,
            args=(workspace_id, source_dir, draft_run_id, on_complete),
            daemon=True,
        )
        worker.start()
        return preview

    def _select_proxy_port(self, workspace_id: str, preview: PreviewRecord, source_dir: Path) -> int:
        existing_port = preview.proxy_port
        reserved_ports = self._reserved_preview_ports(workspace_id)
        if existing_port is not None and self._port_belongs_to_workspace(workspace_id, source_dir, existing_port):
            return existing_port
        if existing_port is not None and existing_port not in reserved_ports and self.runtime_manager.port_free(existing_port):
            return existing_port
        return self.runtime_manager.allocate_port(workspace_id, reserved_ports=reserved_ports)

    def _ensure_worker(self, workspace_id: str, force_rebuild: bool) -> None:
        preview = self._reconcile_runtime_state(self._get_or_create(workspace_id), workspace_id)
        should_rebuild = force_rebuild or bool(preview.project_name or preview.proxy_port)
        self._append_log(preview, f"Ensure worker started. rebuild={should_rebuild}.")
        preview.stage = "rebuilding" if should_rebuild else "starting"
        preview.progress_percent = max(preview.progress_percent, 14)
        self._persist(preview)
        if should_rebuild:
            self.rebuild(workspace_id)
            return
        self.start(workspace_id)

    def _rebuild_worker(
        self,
        workspace_id: str,
        source_dir: Path | None,
        draft_run_id: str | None,
        on_complete: Callable[[PreviewRecord], None] | None,
    ) -> None:
        preview = self.rebuild(workspace_id, source_dir=source_dir, draft_run_id=draft_run_id)
        if on_complete is not None:
            on_complete(preview)

    def reset(self, workspace_id: str) -> PreviewRecord:
        preview = self._reconcile_runtime_state(self._get_or_create(workspace_id), workspace_id)
        if preview.runtime_mode == "docker":
            try:
                self._append_log(preview, "Preview reset requested for docker runtime.")
                logs = self.runtime_manager.reset(workspace_id, self._runtime_source_dir(workspace_id, preview), preview.proxy_port)
                preview.logs.extend(logs or ["Docker preview stopped."])
                preview.logs = preview.logs[-240:]
            except Exception as exc:
                self._append_log(preview, f"Preview reset failed: {exc}")
                preview.status = "error"
            else:
                preview.status = "stopped"
                preview.stage = "idle"
                preview.progress_percent = 0
                preview.url = None
                preview.frontend_url = None
                preview.backend_url = None
                preview.proxy_port = None
                preview.project_name = None
                preview.last_error = None
                self._append_log(preview, "Preview runtime stopped.")
        else:
            preview.status = "stopped"
            preview.stage = "idle"
            preview.progress_percent = 0
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.last_error = None
            self._append_log(preview, "No external preview session to reset.")
        preview.draft_run_id = None
        self._persist(preview)
        return preview

    def get(self, workspace_id: str) -> PreviewRecord:
        payload = self.store.get("previews", workspace_id)
        if not payload:
            return self._get_or_create(workspace_id)
        preview = PreviewRecord.model_validate(payload)
        preview = self._fast_restore_preview_from_http(preview)
        if preview.status != "running" or not preview.url:
            preview = self._reconcile_runtime_state(preview, workspace_id)
            preview = self._fast_restore_preview_from_http(preview)
        preview = self._gate_public_readiness(preview)
        if preview.runtime_mode == "inline":
            preview.runtime_mode = "docker"
            preview.status = "error"
            preview.stage = "error"
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.progress_percent = 100
            self._append_log(preview, "Legacy inline preview was disabled. Start the docker runtime preview.")
        return preview

    def peek(self, workspace_id: str) -> PreviewRecord:
        payload = self.store.get("previews", workspace_id)
        if not payload:
            return self._get_or_create(workspace_id)
        preview = PreviewRecord.model_validate(payload)
        if preview.runtime_mode == "docker" and preview.proxy_port is not None and not preview.url and self._has_ready_runtime_log(preview):
            preview.url = self.runtime_manager.preview_url(preview.proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(preview.proxy_port)
            preview.status = "running"
            preview.stage = "running"
            preview.progress_percent = 100
            preview.last_error = None
            self._persist(preview)
        return preview

    def role_urls(self, workspace_id: str) -> dict[str, str]:
        preview = self.get(workspace_id)
        if not preview.url:
            return {}
        return {role: f"{preview.url}/{role}" for role in ROLE_ORDER}

    @staticmethod
    def role_urls_from_preview(preview: PreviewRecord) -> dict[str, str]:
        if not preview.url:
            return {}
        return {role: f"{preview.url}/{role}" for role in ROLE_ORDER}

    def _fast_restore_preview_from_http(self, preview: PreviewRecord) -> PreviewRecord:
        if preview.runtime_mode != "docker" or preview.proxy_port is None:
            return preview
        runtime_url = self.runtime_manager.preview_url(preview.proxy_port)
        if not self._http_preview_ready(runtime_url):
            return preview
        if preview.status == "running" and preview.url == runtime_url:
            return preview
        preview.url = runtime_url
        preview.frontend_url = runtime_url
        preview.backend_url = self.runtime_manager.backend_url(preview.proxy_port)
        preview.status = "running"
        preview.stage = "running"
        preview.progress_percent = 100
        preview.last_error = None
        self._persist(preview)
        return preview

    @staticmethod
    def _has_ready_runtime_log(preview: PreviewRecord) -> bool:
        ready_markers = (
            "Preview runtime is healthy at ",
            "Preview runtime is already running. Restored preview state from docker.",
            "Preview rebuild completed and runtime is healthy at ",
        )
        return any(any(marker in line for marker in ready_markers) for line in preview.logs[-40:])

    def _gate_public_readiness(self, preview: PreviewRecord) -> PreviewRecord:
        if preview.runtime_mode != "docker" or preview.status != "running" or not preview.url:
            return preview
        if self._http_preview_ready(preview.url):
            return preview
        preview.url = None
        preview.status = "starting"
        preview.stage = "health_check"
        preview.progress_percent = min(99, max(preview.progress_percent, 92))
        preview.frontend_url = None
        preview.backend_url = None
        preview.last_error = None
        self._persist(preview)
        return preview

    def _http_preview_ready(self, preview_url: str) -> bool:
        probe_paths = ("/health", "/client")
        candidate_bases = self._probe_base_urls(preview_url)
        for base_url in candidate_bases:
            if all(self._probe_http(f"{base_url}{probe_path}") for probe_path in probe_paths):
                return True
        return False

    @staticmethod
    def _probe_base_urls(preview_url: str) -> list[str]:
        parsed = urlparse(preview_url)
        if not parsed.scheme or not parsed.netloc:
            return [preview_url.rstrip("/")]
        bases: list[str] = []
        if parsed.port is not None:
            for host in ("host.docker.internal", "127.0.0.1", "localhost"):
                bases.append(f"{parsed.scheme}://{host}:{parsed.port}")
        bases.append(f"{parsed.scheme}://{parsed.netloc}")
        unique: list[str] = []
        for base in bases:
            normalized = base.rstrip("/")
            if normalized not in unique:
                unique.append(normalized)
        return unique

    @staticmethod
    def _probe_http(url: str) -> bool:
        request = Request(url, headers={"Cache-Control": "no-cache"})
        try:
            with urlopen(request, timeout=PREVIEW_HTTP_PROBE_TIMEOUT_SEC) as response:
                return 200 <= int(response.status) < 400
        except (URLError, OSError, ValueError):
            return False

    def render_html(self, workspace_id: str, source_dir: Path, role: str = "client") -> str:
        payload = self._preview_payload_from_source(source_dir)
        selected_role = role if role in ROLE_ORDER else "client"
        role_payload = payload.get("roles", {}).get(selected_role, {})
        title = role_payload.get("title", f"{selected_role.title()} preview")
        description = role_payload.get("description", "Preview not generated yet")
        feature_text = role_payload.get("feature_text", "Role experience is not generated yet.")
        metrics = role_payload.get("metrics", [])
        pages = role_payload.get("pages", [])
        primary_action = role_payload.get("primary_action_label", "Open role")
        secondary_action = role_payload.get("secondary_action_label", "Open profile")
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f7efe2;
        --panel: #fffaf2;
        --ink: #1a1a1a;
        --accent: #005f73;
        --accent-2: #e76f51;
        --border: rgba(0, 0, 0, 0.08);
      }}
      body {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        background: radial-gradient(circle at top, #fff7e9 0%, #f2e7d5 45%, #e6dbc9 100%);
        color: var(--ink);
      }}
      .shell {{
        max-width: 420px;
        margin: 24px auto;
        padding: 16px;
      }}
      .phone {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 28px;
        min-height: 720px;
        box-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
        overflow: hidden;
      }}
      .header {{
        padding: 20px 20px 8px;
        border-bottom: 1px solid var(--border);
      }}
      .screen {{
        padding: 18px 20px 28px;
      }}
      .field {{
        display: grid;
        gap: 6px;
        margin-bottom: 14px;
      }}
      input, textarea {{
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 12px;
        font: inherit;
      }}
      button {{
        border: 0;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--accent), #0a9396);
        color: white;
        padding: 12px 16px;
        font: inherit;
        cursor: pointer;
      }}
      .helper {{
        color: #5b5b5b;
        font-size: 14px;
      }}
      .badge {{
        display: inline-flex;
        background: rgba(231, 111, 81, 0.12);
        color: var(--accent-2);
        border-radius: 999px;
        padding: 4px 10px;
        font-size: 12px;
        margin-bottom: 12px;
      }}
    </style>
  </head>
      <body>
        <div class="shell">
          <div class="phone">
            <div class="header">
              <div class="badge">{selected_role.title()} role</div>
              <h1 style="margin:0;">{title}</h1>
              <p class="helper" style="margin:8px 0 0;">{description}</p>
            </div>
            <div id="app" class="screen"></div>
          </div>
        </div>
        <script>
          const config = {json.dumps(payload)};
          const role = {json.dumps(selected_role)};
          const rolePayload = (config.roles || {{}})[role] || {{}};
          const app = document.getElementById("app");
          const state = {{ profileOpen: false, selectedPageId: ((rolePayload.pages || [])[0] || {{}}).page_id || '' }};
          const metrics = rolePayload.metrics || [];
          const pages = rolePayload.pages || [];

          function render() {{
            const selectedPage = pages.find((page) => page.page_id === state.selectedPageId) || pages[0] || null;
            const metricsHtml = metrics.map((metric) => `
              <div style="border:1px solid var(--border); border-radius:16px; padding:12px;">
                <div class="helper">${{metric.label}}</div>
                <strong style="font-size:24px;">${{metric.value}}</strong>
              </div>
            `).join("");
            const pageTabs = pages.map((page) => `
              <button
                data-page-id="${{page.page_id}}"
                style="background:${{selectedPage && selectedPage.page_id === page.page_id ? 'linear-gradient(135deg, var(--accent), #0a9396)' : 'white'}}; color:${{selectedPage && selectedPage.page_id === page.page_id ? 'white' : 'var(--ink)'}}; border:1px solid var(--border); margin:0 8px 8px 0;"
              >
                ${{page.title}}
              </button>
            `).join("");
            const pageDetails = selectedPage ? `
              <div style="display:grid; gap:12px; margin-top:16px;">
                <div style="border:1px solid var(--border); border-radius:16px; padding:14px;">
                  <strong>${{selectedPage.title}}</strong>
                  <p class="helper" style="margin:8px 0 0;">${{selectedPage.description || ''}}</p>
                  <div class="helper" style="margin-top:8px;">Route: ${{selectedPage.route_path}}</div>
                </div>
              </div>
            ` : '';
            const profile = rolePayload.profile || {{}};
            const profileHtml = state.profileOpen ? `
              <div style="display:grid; gap:10px; margin-top:16px;">
                <label class="field"><span>Имя</span><input value="${{profile.first_name || ''}}" /></label>
                <label class="field"><span>Email</span><input value="${{profile.email || ''}}" /></label>
                <label class="field"><span>Телефон</span><input value="${{profile.phone || ''}}" /></label>
              </div>
            ` : '';
            app.innerHTML = `
              <h2 style="margin-top:0;">${{rolePayload.title || role}}</h2>
              <p class="helper">{feature_text}</p>
              ${{pages.length ? `<div style="margin:14px 0 6px;">${{pageTabs}}</div>` : ''}}
              <div style="display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:10px; margin:16px 0;">
                ${{metricsHtml}}
              </div>
              ${{pageDetails}}
              <button data-action="primary">{primary_action}</button>
              <button data-action="secondary" style="margin-left:8px; background: linear-gradient(135deg, var(--accent-2), #f4a261);">{secondary_action}</button>
              ${{profileHtml}}
            `;
            bindHandlers();
          }}

          function bindHandlers() {{
            const secondary = app.querySelector('[data-action="secondary"]');
            if (secondary) {{
              secondary.addEventListener('click', () => {{
                state.profileOpen = !state.profileOpen;
                render();
              }});
            }}
            app.querySelectorAll('[data-page-id]').forEach((button) => {{
              button.addEventListener('click', () => {{
                state.selectedPageId = button.getAttribute('data-page-id') || '';
                render();
              }});
            }});
          }}
          render();
        </script>
      </body>
</html>"""

    @staticmethod
    def _preview_payload_from_source(source_dir: Path) -> dict[str, object]:
        def empty_profile() -> dict[str, str]:
            return {
                "first_name": "",
                "email": "",
                "phone": "",
            }

        generated_graph_path = source_dir / "artifacts" / "generated_app_graph.json"
        if generated_graph_path.exists():
            graph = json.loads(generated_graph_path.read_text(encoding="utf-8"))
            roles: dict[str, dict[str, object]] = {}
            for role in ROLE_ORDER:
                role_payload = (graph.get("roles") or {}).get(role) or {}
                pages = role_payload.get("pages") or []
                roles[role] = {
                    "title": str(graph.get("app_title") or role.title()),
                    "description": str(graph.get("summary") or ""),
                    "feature_text": str(graph.get("summary") or ""),
                    "primary_action_label": pages[1]["title"] if len(pages) > 1 else "Open role",
                    "secondary_action_label": "Profile",
                    "metrics": [
                        {"metric_id": "pages", "label": "Pages", "value": str(len(pages))},
                        {"metric_id": "routes", "label": "Routes", "value": str(len(pages))},
                    ],
                    "pages": pages,
                    "profile": empty_profile(),
                }
            return {"roles": roles}

        grounded_spec_path = source_dir / "artifacts" / "grounded_spec.json"
        if grounded_spec_path.exists():
            spec = json.loads(grounded_spec_path.read_text(encoding="utf-8"))
            goal = str(spec.get("product_goal") or "Generated mini-app preview")
            roles = {}
            for role in ROLE_ORDER:
                roles[role] = {
                    "title": f"{role.title()} workspace",
                    "description": goal,
                    "feature_text": goal,
                    "primary_action_label": "Open role",
                    "secondary_action_label": "Profile",
                    "metrics": [
                        {"metric_id": "scope", "label": "Flows", "value": str(len(spec.get("user_flows", [])))},
                        {"metric_id": "docs", "label": "Sources", "value": str(len(spec.get("doc_refs", [])))},
                    ],
                    "profile": empty_profile(),
                }
            return {"roles": roles}

        return {
            "roles": {
                role: {
                    "title": f"{role.title()} workspace",
                    "description": "Minimal base mini-app preview.",
                    "feature_text": "Profile and home are ready to extend.",
                    "primary_action_label": "Open role",
                    "secondary_action_label": "Profile",
                    "metrics": [],
                    "profile": empty_profile(),
                }
                for role in ROLE_ORDER
            }
        }

    def _get_or_create(self, workspace_id: str) -> PreviewRecord:
        payload = self.store.get("previews", workspace_id)
        if payload:
            return PreviewRecord.model_validate(payload)
        preview = PreviewRecord(workspace_id=workspace_id)
        self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    def _runtime_source_dir(self, workspace_id: str, preview: PreviewRecord) -> Path:
        if preview.draft_run_id and self.workspace_service.draft_exists(workspace_id, preview.draft_run_id):
            return self.workspace_service.draft_source_dir(workspace_id, preview.draft_run_id)
        return self.workspace_service.source_dir(workspace_id)

    def _reserved_preview_ports(self, workspace_id: str) -> set[int]:
        reserved: set[int] = set()
        for payload in self.store.list("previews"):
            try:
                item = PreviewRecord.model_validate(payload)
            except Exception:
                continue
            if item.workspace_id == workspace_id or item.proxy_port is None:
                continue
            if item.runtime_mode != "docker":
                continue
            if item.status not in {"starting", "running"}:
                continue
            reserved.add(item.proxy_port)
        return reserved

    def _port_belongs_to_workspace(self, workspace_id: str, source_dir: Path, port: int) -> bool:
        containers = self.runtime_manager.inspect_containers(workspace_id, source_dir, port)
        for container in containers:
            if (container.get("state") or "") != "running":
                continue
            published_port = container.get("published_port")
            if isinstance(published_port, str) and published_port.isdigit() and int(published_port) == port:
                return True
        return False

    def _is_stale_starting_preview(self, preview: PreviewRecord) -> bool:
        age_seconds = (datetime.now(timezone.utc) - preview.updated_at).total_seconds()
        return age_seconds >= STARTING_STALE_AFTER_SEC

    def _reconcile_runtime_state(self, preview: PreviewRecord, workspace_id: str) -> PreviewRecord:
        if preview.runtime_mode != "docker":
            return preview

        source_dir: Path | None
        try:
            source_dir = self._runtime_source_dir(workspace_id, preview)
        except KeyError:
            source_dir = None

        runtime_present = False
        runtime_running = False
        restored_proxy_port: int | None = None
        if source_dir is not None:
            containers = self.runtime_manager.inspect_containers(workspace_id, source_dir, preview.proxy_port)
            runtime_present = any((container.get("state") or "") != "missing" for container in containers)
            runtime_running = any((container.get("state") or "") == "running" for container in containers)
            if runtime_running:
                for container in containers:
                    if (container.get("state") or "") != "running":
                        continue
                    published_port = container.get("published_port")
                    if isinstance(published_port, str) and published_port.isdigit():
                        restored_proxy_port = int(published_port)
                        break

        changed = False
        if not runtime_present and (
            preview.project_name
            or preview.proxy_port is not None
            or preview.url
            or preview.status in {"running", "starting", "error"}
        ):
            preview.status = "stopped"
            preview.stage = "idle"
            preview.progress_percent = 0
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.last_error = None
            self._append_log(preview, "Preview runtime was not found. Resetting stale preview state.")
            changed = True
        elif runtime_running and (preview.proxy_port is not None or restored_proxy_port is not None) and (
            preview.status != "running" or not preview.url
        ):
            preview.proxy_port = preview.proxy_port or restored_proxy_port
            preview.project_name = preview.project_name or self.runtime_manager.project_name(workspace_id)
            preview.url = self.runtime_manager.preview_url(preview.proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(preview.proxy_port)
            preview.status = "running"
            preview.stage = "running"
            preview.progress_percent = 100
            preview.last_error = None
            self._append_log(preview, "Preview runtime is already running. Restored preview state from docker.")
            changed = True
        elif runtime_present and not runtime_running and preview.status == "running":
            preview.status = "error"
            preview.stage = "error"
            preview.progress_percent = 100
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.last_error = "Preview containers exist but none are running."
            self._append_log(preview, "Preview containers exist but none are running. Marking preview as error.")
            changed = True

        if changed:
            self._persist(preview)
        return preview
