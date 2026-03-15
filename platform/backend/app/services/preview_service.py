from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.models.domain import PreviewRecord
from app.repositories.state_store import StateStore
from app.services.runtime_manager import PreviewRuntimeManager
from app.services.workspace_service import WorkspaceService

ROLE_ORDER = ("client", "specialist", "manager")


class PreviewService:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        workspace_service: WorkspaceService,
        runtime_manager: PreviewRuntimeManager,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace_service = workspace_service
        self.runtime_manager = runtime_manager

    def start(self, workspace_id: str) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)
        runtime_mode = self.runtime_manager.preferred_mode()
        preview.runtime_mode = runtime_mode
        preview.updated_at = datetime.now(timezone.utc)
        try:
            proxy_port = preview.proxy_port or self.runtime_manager.allocate_port(workspace_id)
            project_name, logs = self.runtime_manager.start(workspace_id, source_dir, proxy_port)
            preview.proxy_port = proxy_port
            preview.project_name = project_name
            preview.url = self.runtime_manager.preview_url(proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(proxy_port)
            preview.logs.extend(logs or [f"Docker preview started on port {proxy_port}."])
            preview.status = "running"
            preview.started_at = preview.started_at or datetime.now(timezone.utc)
        except Exception as exc:
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.status = "error"
            preview.logs.append(f"Docker preview failed: {exc}")
        self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    def rebuild(self, workspace_id: str) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        source_dir = self.workspace_service.source_dir(workspace_id)
        runtime_mode = self.runtime_manager.preferred_mode()
        preview.runtime_mode = runtime_mode
        preview.updated_at = datetime.now(timezone.utc)
        try:
            proxy_port = preview.proxy_port or self.runtime_manager.allocate_port(workspace_id)
            logs = self.runtime_manager.rebuild(workspace_id, source_dir, proxy_port)
            preview.proxy_port = proxy_port
            preview.project_name = self.runtime_manager.project_name(workspace_id)
            preview.url = self.runtime_manager.preview_url(proxy_port)
            preview.frontend_url = preview.url
            preview.backend_url = self.runtime_manager.backend_url(proxy_port)
            preview.logs.extend(logs or ["Docker preview rebuilt."])
            preview.status = "running"
            preview.started_at = preview.started_at or datetime.now(timezone.utc)
        except Exception as exc:
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.status = "error"
            preview.logs.append(f"Docker preview rebuild failed: {exc}")
        self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    def reset(self, workspace_id: str) -> PreviewRecord:
        preview = self._get_or_create(workspace_id)
        if preview.runtime_mode == "docker":
            try:
                logs = self.runtime_manager.reset(workspace_id, self.workspace_service.source_dir(workspace_id), preview.proxy_port)
                preview.logs.extend(logs or ["Docker preview stopped."])
            except Exception as exc:
                preview.logs.append(f"Preview reset failed: {exc}")
                preview.status = "error"
            else:
                preview.status = "stopped"
        else:
            preview.status = "stopped"
            preview.logs.append("No external preview session to reset.")
        preview.updated_at = datetime.now(timezone.utc)
        self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    def get(self, workspace_id: str) -> PreviewRecord:
        payload = self.store.get("previews", workspace_id)
        if not payload:
            return self._get_or_create(workspace_id)
        preview = PreviewRecord.model_validate(payload)
        if preview.runtime_mode == "inline":
            preview.runtime_mode = "docker"
            preview.status = "error"
            preview.url = None
            preview.frontend_url = None
            preview.backend_url = None
            preview.proxy_port = None
            preview.project_name = None
            preview.logs.append("Legacy inline preview was disabled. Start the docker runtime preview.")
        if preview.runtime_mode == "docker" and preview.proxy_port is not None:
            try:
                preview.logs = self.runtime_manager.collect_logs(
                    workspace_id,
                    self.workspace_service.source_dir(workspace_id),
                    preview.proxy_port,
                )
            except Exception as exc:
                preview.logs.append(f"Failed to collect runtime logs: {exc}")
            self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview

    def role_urls(self, workspace_id: str) -> dict[str, str]:
        preview = self.get(workspace_id)
        if not preview.url:
            return {}
        return {role: f"{preview.url}?role={role}" for role in ROLE_ORDER}

    def render_html(self, workspace_id: str, source_dir: Path, role: str = "client") -> str:
        role_seed_path = source_dir / "backend" / "app" / "generated" / "role_seed.json"
        payload = {"roles": {}}
        if role_seed_path.exists():
            payload = json.loads(role_seed_path.read_text(encoding="utf-8"))
        selected_role = role if role in ROLE_ORDER else "client"
        role_payload = payload.get("roles", {}).get(selected_role, {})
        title = role_payload.get("title", f"{selected_role.title()} preview")
        description = role_payload.get("description", "Preview not generated yet")
        feature_text = role_payload.get("feature_text", "Role experience is not generated yet.")
        metrics = role_payload.get("metrics", [])
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
          const state = {{ profileOpen: false }};
          const metrics = rolePayload.metrics || [];

          function render() {{
            const metricsHtml = metrics.map((metric) => `
              <div style="border:1px solid var(--border); border-radius:16px; padding:12px;">
                <div class="helper">${{metric.label}}</div>
                <strong style="font-size:24px;">${{metric.value}}</strong>
              </div>
            `).join("");
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
              <div style="display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:10px; margin:16px 0;">
                ${{metricsHtml}}
              </div>
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
          }}
          render();
        </script>
      </body>
</html>"""

    def _get_or_create(self, workspace_id: str) -> PreviewRecord:
        payload = self.store.get("previews", workspace_id)
        if payload:
            return PreviewRecord.model_validate(payload)
        preview = PreviewRecord(workspace_id=workspace_id)
        self.store.upsert("previews", workspace_id, preview.model_dump(mode="json"))
        return preview
