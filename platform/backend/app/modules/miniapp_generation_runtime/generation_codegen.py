from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from app.models.common import GenerationMode
from app.models.domain import DraftFileOperation
from app.models.grounded_spec import GroundedSpecModel
from app.services.workspace.service import json_dumps

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner

logger = logging.getLogger(__name__)


class MiniappGenerationCodegen(MiniappGenerationRuntimeOwner):
    _PROVIDER_BUDGET_ERROR_MARKERS = (
        "requires more credits",
        "fewer max_tokens",
        "can only afford",
    )

    @classmethod
    def _is_provider_budget_error(cls, error_message: str) -> bool:
        lowered = str(error_message or "").lower()
        return any(marker in lowered for marker in cls._PROVIDER_BUDGET_ERROR_MARKERS)

    @staticmethod
    def _humanize_static_slug(value: str) -> str:
        text = re.sub(r"[_-]+", " ", str(value or "").strip()).strip()
        return " ".join(part.capitalize() for part in text.split()) or "Workspace"

    @staticmethod
    def _safe_js_payload(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _role_ui_target_parts(target: str) -> tuple[str, str, str] | None:
        normalized = str(target or "").strip().replace("\\", "/")
        match = re.fullmatch(
            r"miniapp/app/static/(?P<role>client|specialist|manager)(?:/(?P<slug>[^/]+))?/(?P<name>index\.html|styles\.css|app\.js)",
            normalized,
        )
        if not match:
            return None
        return match.group("role"), match.group("slug") or "root", match.group("name")

    def _role_ui_page_title(
        self,
        *,
        role: str,
        slug: str,
        entity_contract: dict[str, Any],
    ) -> str:
        plural_label = str(entity_contract.get("plural_label") or "Records").strip() or "Records"
        if slug == "root":
            return f"{self._humanize_static_slug(role)} {plural_label}"
        if slug == "profile":
            return f"{self._humanize_static_slug(role)} Profile"
        return self._humanize_static_slug(slug)

    def _role_ui_fallback_html(
        self,
        *,
        role: str,
        slug: str,
        file_path: str,
        entity_contract: dict[str, Any],
    ) -> str:
        title = self._role_ui_page_title(role=role, slug=slug, entity_contract=entity_contract)
        role_label = self._humanize_static_slug(role)
        style_path = "/" + file_path.replace("miniapp/app/", "").replace("index.html", "styles.css")
        script_path = "/" + file_path.replace("miniapp/app/", "").replace("index.html", "app.js")
        entity_label = str(entity_contract.get("singular_label") or "record").strip() or "record"
        plural_label = str(entity_contract.get("plural_label") or "records").strip() or "records"
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
    <link rel="stylesheet" href="/static/shared/base.css" />
    <link rel="stylesheet" href="{style_path}" />
  </head>
  <body data-role="{role}" data-page="{slug}">
    <main class="page-shell">
      <section class="workflow-page">
        <header class="workflow-hero">
          <button class="back-button" id="back-button" type="button" hidden>Back</button>
          <div class="identity-row">
            <div class="avatar-wrap" id="profile-avatar" aria-hidden="true"></div>
            <div>
              <span class="eyebrow">{role_label}</span>
              <h1>{title}</h1>
              <p id="profile-name" class="profile-name">{role_label}</p>
            </div>
          </div>
          <p class="hero-copy">Create, review, and update persisted {plural_label} without seeded demo data.</p>
        </header>

        <section class="workflow-grid">
          <form id="record-form" class="record-card" autocomplete="off">
            <span class="eyebrow">New {entity_label}</span>
            <label>
              Title
              <input id="record-title" name="title" type="text" placeholder="Short title" required />
            </label>
            <label>
              Details
              <textarea id="record-details" name="details" rows="4" placeholder="Important details"></textarea>
            </label>
            <label>
              Status
              <select id="status-select" name="status"></select>
            </label>
            <button class="primary-action" id="create-button" type="submit">Create</button>
          </form>

          <section class="record-card record-list-card" aria-live="polite">
            <div class="list-header">
              <div>
                <span class="eyebrow">Live {plural_label}</span>
                <h2>Shared state</h2>
              </div>
              <button class="secondary-action" id="reload-button" type="button">Reload</button>
            </div>
            <p id="empty-state" class="empty-state">No records yet.</p>
            <div id="record-list" class="record-list"></div>
          </section>
        </section>
      </section>
    </main>
    <script src="/static/preview_bridge.js" defer></script>
    <script src="{script_path}" defer></script>
  </body>
</html>
"""

    def _role_ui_fallback_css(self) -> str:
        return """:root {
  --workflow-bg: #eef5f1;
  --workflow-ink: #17231d;
  --workflow-muted: #617064;
  --workflow-card: #ffffff;
  --workflow-accent: #1f7a4d;
  --workflow-soft: #dcece3;
  --workflow-border: rgba(23, 35, 29, 0.14);
}

body {
  background:
    radial-gradient(circle at 18% 12%, rgba(31, 122, 77, 0.18), transparent 32%),
    linear-gradient(145deg, #f8fbf7 0%, var(--workflow-bg) 100%);
  color: var(--workflow-ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
}

button,
input,
select,
textarea {
  font: inherit;
}

.workflow-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.workflow-hero,
.record-card {
  border: 1px solid var(--workflow-border);
  border-radius: 28px;
  background: rgba(255, 255, 255, 0.88);
  box-shadow: 0 22px 60px rgba(43, 74, 55, 0.14);
}

.workflow-hero {
  padding: 22px;
}

.identity-row,
.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.identity-row {
  justify-content: flex-start;
}

.avatar-wrap {
  width: 58px;
  height: 58px;
  overflow: hidden;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--workflow-soft);
  flex: 0 0 auto;
}

.avatar,
.avatar-fallback {
  width: 100%;
  height: 100%;
}

.avatar {
  object-fit: cover;
}

.avatar-fallback {
  display: grid;
  place-items: center;
  font-weight: 800;
  color: var(--workflow-accent);
}

.eyebrow {
  color: var(--workflow-accent);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}

h1,
h2,
p {
  margin: 0;
}

h1 {
  font-size: clamp(26px, 7vw, 42px);
  line-height: 0.98;
  margin-top: 6px;
}

h2 {
  font-size: 22px;
}

.profile-name,
.hero-copy,
.empty-state,
.record-meta {
  color: var(--workflow-muted);
}

.hero-copy {
  margin-top: 16px;
  line-height: 1.55;
}

.workflow-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
  gap: 16px;
}

.record-card {
  padding: 18px;
}

.record-form,
.record-list-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: var(--workflow-muted);
  font-size: 13px;
  font-weight: 700;
}

input,
select,
textarea {
  width: 100%;
  border: 1px solid var(--workflow-border);
  border-radius: 16px;
  padding: 12px 14px;
  background: #fbfdfa;
  color: var(--workflow-ink);
}

.primary-action,
.secondary-action,
.back-button {
  border: 0;
  border-radius: 999px;
  cursor: pointer;
  font-weight: 800;
}

.primary-action {
  padding: 13px 18px;
  background: var(--workflow-accent);
  color: #ffffff;
}

.secondary-action,
.back-button {
  padding: 10px 14px;
  background: var(--workflow-soft);
  color: var(--workflow-accent);
}

.back-button {
  margin-bottom: 14px;
}

.record-list {
  display: grid;
  gap: 10px;
}

.record-item {
  border: 1px solid var(--workflow-border);
  border-radius: 18px;
  padding: 14px;
  background: #fbfdfa;
  display: grid;
  gap: 10px;
}

.record-title-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.status-pill {
  border-radius: 999px;
  background: var(--workflow-soft);
  color: var(--workflow-accent);
  padding: 5px 9px;
  font-size: 12px;
  font-weight: 800;
}

@media (max-width: 720px) {
  .workflow-grid {
    grid-template-columns: 1fr;
  }

  .identity-row,
  .list-header,
  .record-title-row {
    align-items: flex-start;
    flex-direction: column;
  }
}
"""

    def _role_ui_fallback_js(
        self,
        *,
        role: str,
        slug: str,
        entity_contract: dict[str, Any],
    ) -> str:
        api_path = str(entity_contract.get("api_path") or "").strip() or "/api/records"
        plural_label = str(entity_contract.get("plural_label") or "records").strip() or "records"
        singular_label = str(entity_contract.get("singular_label") or "record").strip() or "record"
        status_values = [
            str(value).strip()
            for value in list(entity_contract.get("status_literals") or [])
            if str(value).strip()
        ] or ["pending", "in_progress", "completed"]
        key_fields = [
            field
            for field in list(entity_contract.get("key_fields") or [])
            if isinstance(field, dict) and str(field.get("name") or "").strip()
        ][:8]
        entity_payload = {
            "apiPath": api_path,
            "pluralLabel": plural_label,
            "singularLabel": singular_label,
            "statuses": status_values,
            "keyFields": key_fields,
        }
        return f"""const ROLE = {self._safe_js_payload(role)};
const PAGE = {self._safe_js_payload(slug)};
const ENTITY = {self._safe_js_payload(entity_payload)};

window.setupPreviewBridge?.(ROLE);

const elements = {{
  avatar: document.getElementById("profile-avatar"),
  profileName: document.getElementById("profile-name"),
  backButton: document.getElementById("back-button"),
  form: document.getElementById("record-form"),
  title: document.getElementById("record-title"),
  details: document.getElementById("record-details"),
  status: document.getElementById("status-select"),
  reload: document.getElementById("reload-button"),
  empty: document.getElementById("empty-state"),
  list: document.getElementById("record-list"),
}};

init();

function init() {{
  populateStatuses();
  setupBackButton();
  bindEvents();
  loadProfile();
  loadRecords();
}}

function setupBackButton() {{
  if (!elements.backButton || PAGE === "root") {{
    return;
  }}
  elements.backButton.hidden = false;
  elements.backButton.addEventListener("click", () => {{
    if (window.history.length > 1) {{
      window.history.back();
      return;
    }}
    window.location.assign(`/${{ROLE}}`);
  }});
}}

function bindEvents() {{
  elements.reload?.addEventListener("click", loadRecords);
  elements.form?.addEventListener("submit", async (event) => {{
    event.preventDefault();
    const payload = buildCreatePayload();
    const response = await fetch(ENTITY.apiPath, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(payload),
    }});
    if (!response.ok) {{
      renderError(`Create failed with ${{response.status}}`);
      return;
    }}
    elements.form.reset();
    populateStatuses();
    await loadRecords();
  }});
}}

function populateStatuses() {{
  if (!elements.status) {{
    return;
  }}
  elements.status.innerHTML = ENTITY.statuses
    .map((status) => `<option value="${{escapeHtml(status)}}">${{formatText(status)}}</option>`)
    .join("");
}}

async function loadProfile() {{
  try {{
    const response = await fetch(`/api/profiles/${{ROLE}}`);
    if (!response.ok) {{
      return;
    }}
    const profile = await response.json();
    const name = getDisplayName(profile, `${{formatText(ROLE)}} profile`);
    if (elements.profileName) {{
      elements.profileName.textContent = name;
    }}
    if (elements.avatar) {{
      elements.avatar.innerHTML = renderAvatar(profile.photo_url, getInitials(name));
    }}
  }} catch (error) {{
    console.warn("Profile load failed", error);
  }}
}}

async function loadRecords() {{
  try {{
    const response = await fetch(ENTITY.apiPath);
    if (!response.ok) {{
      renderError(`Load failed with ${{response.status}}`);
      return;
    }}
    const payload = await response.json();
    renderRecords(extractItems(payload));
  }} catch (error) {{
    renderError("Load failed");
  }}
}}

function buildCreatePayload() {{
  const title = elements.title?.value?.trim() || `New ${{ENTITY.singularLabel}}`;
  const details = elements.details?.value?.trim() || "";
  const status = elements.status?.value || ENTITY.statuses[0] || "pending";
  const payload = {{title, details, status}};
  for (const field of ENTITY.keyFields) {{
    const name = String(field.name || "").trim();
    if (!name || name in payload || name === "id" || name.endsWith("_id")) {{
      continue;
    }}
    payload[name] = sampleValueForField(name, title, details, status);
  }}
  return payload;
}}

function sampleValueForField(name, title, details, status) {{
  const lowered = name.toLowerCase();
  if (lowered.includes("status") || lowered === "state") {{
    return status;
  }}
  if (lowered.includes("detail") || lowered.includes("reason") || lowered.includes("note")) {{
    return details || title;
  }}
  if (lowered.includes("date") || lowered.includes("time") || lowered.endsWith("_at")) {{
    return new Date().toISOString();
  }}
  if (lowered.includes("title") || lowered.includes("name") || lowered.includes("label")) {{
    return title;
  }}
  return title;
}}

function extractItems(payload) {{
  if (Array.isArray(payload)) {{
    return payload;
  }}
  if (!payload || typeof payload !== "object") {{
    return [];
  }}
  for (const key of ["items", "records", "results", "data", "rows"]) {{
    if (Array.isArray(payload[key])) {{
      return payload[key];
    }}
  }}
  return [];
}}

function renderRecords(records) {{
  if (!elements.list || !elements.empty) {{
    return;
  }}
  elements.empty.hidden = records.length > 0;
  elements.list.innerHTML = records.map(renderRecord).join("");
  elements.list.querySelectorAll("[data-action='advance']").forEach((button) => {{
    button.addEventListener("click", () => updateRecord(button.dataset.id));
  }});
}}

function renderRecord(record) {{
  const id = recordId(record);
  const status = String(record.status || record.state || ENTITY.statuses[0] || "pending");
  const title = String(record.title || record.name || record.label || id || ENTITY.singularLabel);
  const details = String(record.details || record.description || record.reason || "");
  return `<article class="record-item">
    <div class="record-title-row">
      <strong>${{escapeHtml(title)}}</strong>
      <span class="status-pill">${{escapeHtml(formatText(status))}}</span>
    </div>
    <p class="record-meta">${{escapeHtml(details || "No extra details")}}</p>
    <button class="secondary-action" type="button" data-action="advance" data-id="${{escapeHtml(id)}}">Update status</button>
  </article>`;
}}

async function updateRecord(id) {{
  if (!id) {{
    return;
  }}
  const nextStatus = ENTITY.statuses[1] || ENTITY.statuses[0] || "updated";
  const response = await fetch(`${{ENTITY.apiPath}}/${{encodeURIComponent(id)}}`, {{
    method: "PATCH",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{status: nextStatus, details: "Updated from role workspace"}}),
  }});
  if (!response.ok) {{
    renderError(`Update failed with ${{response.status}}`);
    return;
  }}
  await loadRecords();
}}

function recordId(record) {{
  for (const key of ["id", "record_id", "item_id", "request_id"]) {{
    if (record && record[key]) {{
      return String(record[key]);
    }}
  }}
  for (const [key, value] of Object.entries(record || {{}})) {{
    if (key.endsWith("_id") && value) {{
      return String(value);
    }}
  }}
  return "";
}}

function renderError(message) {{
  if (elements.empty) {{
    elements.empty.hidden = false;
    elements.empty.textContent = message;
  }}
}}

function getDisplayName(profile, fallback) {{
  const fullName = `${{profile.first_name || ""}} ${{profile.last_name || ""}}`.trim();
  return fullName || fallback;
}}

function getInitials(name) {{
  return String(name || "U")
    .split(/\\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("") || "U";
}}

function renderAvatar(photoUrl, fallbackText) {{
  if (photoUrl) {{
    return `<img class="avatar" src="${{escapeHtml(photoUrl)}}" alt="" />`;
  }}
  return `<div class="avatar-fallback">${{escapeHtml(fallbackText)}}</div>`;
}}

function formatText(value) {{
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
}}

function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\\"": "&quot;",
    "'": "&#39;",
  }}[char]));
}}
"""

    def _whole_file_role_ui_fallback_result(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        cluster_name: str,
        cluster_targets: list[str],
        entity_contract: dict[str, Any],
        timeout_seconds: int = 0,
    ) -> dict[str, Any] | None:
        if not (cluster_name.startswith("role_") and "_ui_" in cluster_name):
            return None
        target_parts = [self._role_ui_target_parts(target) for target in cluster_targets]
        if not target_parts or any(parts is None for parts in target_parts):
            return None
        operations: list[DraftFileOperation] = []
        for target, parts in zip(cluster_targets, target_parts, strict=False):
            assert parts is not None
            role, slug, name = parts
            if slug == "profile":
                existing = self.workspace_service.try_read_text_file(workspace_id, target, run_id=draft_run_id)
                if existing is not None:
                    continue
            if name == "index.html":
                content = self._role_ui_fallback_html(
                    role=role,
                    slug=slug,
                    file_path=target,
                    entity_contract=entity_contract,
                )
            elif name == "styles.css":
                content = self._role_ui_fallback_css()
            else:
                content = self._role_ui_fallback_js(
                    role=role,
                    slug=slug,
                    entity_contract=entity_contract,
                )
            operations.append(
                DraftFileOperation(
                    file_path=target,
                    operation="replace",
                    content=content,
                    reason=(
                        "Provider-budget fallback: materialize a neutral DB-backed role page from the "
                        "extracted entity contract instead of blocking the run."
                    ),
                )
            )
        return {
            "cluster_name": cluster_name,
            "target_files": list(cluster_targets),
            "assistant_message": (
                f"{cluster_name} could not be generated because the LLM provider had insufficient output budget, "
                "so neutral DB-backed role pages were materialized from the entity contract."
            ),
            "operations": operations,
            "duration_ms": int(timeout_seconds * 1000),
            "fallback_used": True,
        }

    def _whole_file_batch_timeout_seconds(
        self,
        batch: list[dict[str, Any]],
        *,
        generation_mode: GenerationMode,
    ) -> int:
        is_ui_batch = any(
            str(cluster.get("cluster_name") or "").startswith("role_")
            or str(cluster.get("cluster_name") or "") == "backend_support"
            for cluster in batch
        )
        default_timeout = int(
            getattr(self, "WHOLE_FILE_UI_CLUSTER_TIMEOUT_SECONDS", self.WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS)
            if is_ui_batch
            else self.WHOLE_FILE_CLUSTER_TIMEOUT_SECONDS
        )
        return default_timeout

    def _whole_file_timeout_fallback_result(
        self,
        *,
        cluster_name: str,
        cluster_targets: list[str],
        timeout_seconds: int = 0,
    ) -> dict[str, Any] | None:
        operations: list[DraftFileOperation] = []
        for target in cluster_targets:
            deterministic_source = self.generation_contract_routes._deterministic_route_source_for_path(target)
            if deterministic_source is None:
                return None
            operations.append(
                DraftFileOperation(
                    file_path=target,
                    operation="replace",
                    content=deterministic_source,
                    reason=(
                        "Whole-file generation timeout fallback: restore this canonical route module "
                        "from the deterministic contract source instead of blocking the entire run."
                    ),
                )
            )
        return {
            "cluster_name": cluster_name,
            "target_files": list(cluster_targets),
            "assistant_message": (
                f"{cluster_name} timed out during whole-file generation, so the canonical deterministic "
                "route contract was applied instead."
            ),
            "operations": operations,
            "duration_ms": int(timeout_seconds * 1000),
            "fallback_used": True,
        }

    def _whole_file_static_reuse_fallback_result(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        cluster_name: str,
        cluster_targets: list[str],
        timeout_seconds: int,
    ) -> dict[str, Any] | None:
        if not cluster_targets or not all(str(target).startswith("miniapp/app/static/") for target in cluster_targets):
            return None
        for target in cluster_targets:
            content = self.workspace_service.try_read_text_file(workspace_id, target, run_id=draft_run_id)
            if content is None:
                return None
        return {
            "cluster_name": cluster_name,
            "target_files": list(cluster_targets),
            "assistant_message": (
                f"{cluster_name} timed out during whole-file generation, so the existing static page surface "
                "was kept and the run continued into exact checks."
            ),
            "operations": [],
            "duration_ms": int(timeout_seconds * 1000),
            "fallback_used": True,
        }

    def _whole_file_error_fallback_result(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        cluster_name: str,
        cluster_targets: list[str],
        error_message: str,
        entity_contract: dict[str, Any],
    ) -> dict[str, Any] | None:
        lowered = str(error_message or "").lower()
        if self._is_provider_budget_error(error_message) and (
            cluster_name == "backend_support"
            or cluster_name == "backend_routes"
            or cluster_name.startswith("backend_route_")
        ):
            fallback = self._whole_file_timeout_fallback_result(
                cluster_name=cluster_name,
                cluster_targets=cluster_targets,
            )
            if fallback is not None:
                fallback["assistant_message"] = (
                    f"{cluster_name} could not be generated because the LLM provider had insufficient output budget, "
                    "so the deterministic backend contract was applied instead."
                )
                return fallback
        if self._is_provider_budget_error(error_message):
            fallback = self._whole_file_role_ui_fallback_result(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                cluster_name=cluster_name,
                cluster_targets=cluster_targets,
                entity_contract=entity_contract,
            )
            if fallback is not None:
                return fallback
        if not any(
            marker in lowered
            for marker in (
                "returned no file operations",
                "exhausted the tool-request budget",
                "requested files that were already present in the current context",
                "repeated identical tool requests",
            )
        ):
            return None
        fallback = self._whole_file_timeout_fallback_result(
            cluster_name=cluster_name,
            cluster_targets=cluster_targets,
        )
        if fallback is None:
            return None
        fallback["assistant_message"] = (
            f"{cluster_name} failed during whole-file generation, so the canonical deterministic "
            "route contract was applied instead."
        )
        return fallback

    def _resolve_code_edits(
        self,
        *,
        workspace_id: str,
        draft_run_id: str,
        prompt: str,
        grounded_spec: GroundedSpecModel,
        entity_contract: dict[str, Any],
        role_scope: list[str],
        file_contexts: dict[str, str],
        target_files: list[str],
        role_contract: dict[str, Any],
        page_graph: dict[str, Any],
        intent: str,
        scope_mode: str,
        generation_mode: GenerationMode,
        creative_direction: dict[str, Any],
        visual_only_patch: bool = False,
    ) -> dict[str, Any]:
        if scope_mode == "whole_file_build":
            return self._resolve_whole_file_code_edits(
                workspace_id=workspace_id,
                draft_run_id=draft_run_id,
                prompt=prompt,
                grounded_spec=grounded_spec,
                entity_contract=entity_contract,
                role_scope=role_scope,
                file_contexts=file_contexts,
                target_files=target_files,
                role_contract=role_contract,
                page_graph=page_graph,
                intent=intent,
                scope_mode=scope_mode,
                generation_mode=generation_mode,
                creative_direction=creative_direction,
            )
        target_set = set(target_files)
        page_operations: list[DraftFileOperation] = []
        page_messages: list[str] = []
        generated_page_sources: dict[str, str] = {}
        generated_backend_sources: dict[str, str] = {}
        trace_payloads: dict[str, dict[str, Any]] = {}
        latency_breakdown: dict[str, int] = {}
        draft_source = self.workspace_service.draft_source_dir(workspace_id, draft_run_id)
        workspace_tree = (
            self.workspace_service.file_tree(workspace_id, run_id=draft_run_id)
            if self.workspace_service.draft_exists(workspace_id, draft_run_id)
            else []
        )
        selected_pages = self._selected_pages_for_edit(page_graph, target_set)
        resolve_page_file_edit = getattr(self.service, "_resolve_page_file_edit", self._resolve_page_file_edit)
        resolve_page_file_edits_async = getattr(self.service, "_resolve_page_file_edits_async", self._resolve_page_file_edits_async)
        if len(selected_pages) <= 1:
            ordered_page_results = [
                resolve_page_file_edit(
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    entity_contract=entity_contract,
                    role=role,
                    page=page,
                    page_graph=page_graph,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    intent=intent,
                    file_contexts=file_contexts,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                    workspace_id=workspace_id,
                    draft_run_id=draft_run_id,
                    workspace_tree=workspace_tree,
                    draft_source=draft_source,
                )
                for role, page in selected_pages
            ]
        else:
            ordered_page_results = asyncio.run(
                resolve_page_file_edits_async(
                    selected_pages=selected_pages,
                    prompt=prompt,
                    grounded_spec=grounded_spec,
                    entity_contract=entity_contract,
                    page_graph=page_graph,
                    role_contract=role_contract,
                    scope_mode=scope_mode,
                    intent=intent,
                    file_contexts=file_contexts,
                    generation_mode=generation_mode,
                    creative_direction=creative_direction,
                    workspace_id=workspace_id,
                    draft_run_id=draft_run_id,
                    workspace_tree=workspace_tree,
                    draft_source=draft_source,
                )
            )
            if any("error" in result and (result.get("retryable") or self._is_recoverable_page_error_message(str(result.get("error") or ""))) for result in ordered_page_results):
                for index, page_result in enumerate(ordered_page_results):
                    if "error" not in page_result:
                        continue
                    if not (page_result.get("retryable") or self._is_recoverable_page_error_message(str(page_result.get("error") or ""))):
                        continue
                    role, page = selected_pages[index]
                    ordered_page_results[index] = resolve_page_file_edit(
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        entity_contract=entity_contract,
                        role=role,
                        page=page,
                        page_graph=page_graph,
                        role_contract=role_contract,
                        scope_mode=scope_mode,
                        intent=intent,
                        file_contexts=file_contexts,
                        generation_mode=GenerationMode.FAST,
                        creative_direction=creative_direction,
                        recovery_mode="serial_recovery_retry",
                        workspace_id=workspace_id,
                        draft_run_id=draft_run_id,
                        workspace_tree=workspace_tree,
                        draft_source=draft_source,
                    )
        for page_result in ordered_page_results:
            if "error" in page_result:
                return page_result
            raw_operations = list(page_result.get("operations") or [])
            if not raw_operations and page_result.get("operation") is not None:
                raw_operations = [page_result["operation"]]
            for raw_operation in raw_operations:
                operation = raw_operation if isinstance(raw_operation, DraftFileOperation) else DraftFileOperation.model_validate(raw_operation)
                page_operations.append(operation)
                if operation.content is not None:
                    generated_page_sources[operation.file_path] = operation.content
            page_messages.append(str(page_result.get("assistant_message") or "").strip())
        effective_target_files = list(target_files)
        backend_targets = [] if visual_only_patch else self._backend_composition_targets(target_files, selected_pages)
        backend_contract_gap_targets = (
            []
            if visual_only_patch
            else self._detect_missing_backend_contract_targets(
                generated_page_sources=generated_page_sources,
                current_target_files=effective_target_files,
                backend_targets=backend_targets,
                entity_contract=entity_contract,
            )
        )
        static_contract_gap_targets = self._detect_missing_static_asset_targets(
            generated_page_sources=generated_page_sources,
            current_target_files=effective_target_files,
            page_graph=page_graph,
        )
        contract_gap_targets = list(dict.fromkeys([*backend_contract_gap_targets, *static_contract_gap_targets]))
        if contract_gap_targets:
            effective_target_files = list(dict.fromkeys([*effective_target_files, *contract_gap_targets]))
            backend_targets = list(dict.fromkeys([*backend_targets, *backend_contract_gap_targets]))
            for file_path in contract_gap_targets:
                if file_path in file_contexts:
                    continue
                try:
                    content = self.workspace_service.try_read_text_file(workspace_id, file_path, run_id=draft_run_id)
                except FileNotFoundError:
                    continue
                if content is not None:
                    file_contexts[file_path] = content
        composition_clusters: list[tuple[str, str, list[str]]] = []
        if backend_targets:
            composition_clusters.append(("composition_backend", "miniapp", backend_targets))
        if composition_clusters:
            async def resolve_clusters() -> list[dict[str, Any]]:
                tasks = [
                    asyncio.to_thread(
                        self._timed_composition_cluster,
                        cluster_name=cluster_name,
                        prompt=prompt,
                        grounded_spec=grounded_spec,
                        entity_contract=entity_contract,
                        role_scope=role_scope,
                        role_contract=role_contract,
                        page_graph=page_graph,
                        scope_mode=scope_mode,
                        intent=intent,
                        stage_name=stage_name,
                        target_files=cluster_targets,
                        file_contexts=file_contexts,
                        generated_page_sources=generated_page_sources,
                        generated_support_sources={},
                        generation_mode=generation_mode,
                        creative_direction=creative_direction,
                        workspace_id=workspace_id,
                        draft_run_id=draft_run_id,
                        workspace_tree=workspace_tree,
                        draft_source=draft_source,
                    )
                    for cluster_name, stage_name, cluster_targets in composition_clusters
                ]
                return await asyncio.gather(*tasks)
            composition_results = asyncio.run(resolve_clusters())
        else:
            composition_results = []
        for result in composition_results:
            if "error" in result:
                return result
            cluster_name = str(result["cluster_name"])
            duration_ms = int(result["duration_ms"])
            latency_breakdown[cluster_name] = duration_ms
            trace_payloads[cluster_name] = {
                "message": f"{cluster_name.replace('_', ' ').capitalize()} completed.",
                "payload": {
                    "duration_ms": duration_ms,
                    "target_files": result["target_files"],
                    "operation_count": len(result["operations"]),
                },
            }
            if cluster_name == "composition_backend":
                for operation in result["operations"]:
                    if operation.content is not None:
                        generated_backend_sources[operation.file_path] = operation.content
            if str(result.get("assistant_message") or "").strip():
                page_messages.append(str(result["assistant_message"]).strip())
        page_graph = self.generation_targeting.sanitize_page_graph_role_entries(page_graph)
        operations = self._dedupe_operations(
            [
                DraftFileOperation(file_path="artifacts/generated_app_graph.json", operation="replace", content=json_dumps(page_graph), reason="Persist the LLM-generated page graph for validation, preview, and run artifacts."),
                DraftFileOperation(file_path="artifacts/page_graph_verification.json", operation="replace", content=json_dumps(self._build_page_graph_verification_report(page_graph, role_scope)), reason="Persist structural verification for the planned page graph and route tree."),
                *page_operations,
                *[operation for result in composition_results for operation in result["operations"]],
            ]
        )
        assistant_parts = [message for message in page_messages if message]
        assistant_message = " ".join(assistant_parts).strip() or f"Generated {len(page_operations)} page files and composed the miniapp for a {scope_mode} run."
        if "composition_backend" in latency_breakdown:
            latency_breakdown["composition_backend_ms"] = latency_breakdown["composition_backend"]
        return {
            "assistant_message": assistant_message,
            "operations": operations,
            "planner_contract_gap_targets": contract_gap_targets,
            "effective_target_files": effective_target_files,
            "effective_backend_targets": backend_targets,
            "latency_breakdown": latency_breakdown,
            "trace_payloads": trace_payloads,
        }

    def _resolve_whole_file_code_edits(self, **kwargs: Any) -> dict[str, Any]:
        target_files = kwargs["target_files"]
        clusters = self._build_generation_clusters(target_files)
        if not clusters:
            return {"error": "Whole-file generation requires at least one canonical target file."}
        total_target_files = max(1, sum(len(list(cluster["target_files"])) for cluster in clusters))
        completed_target_files = 0
        results: list[dict[str, Any]] = []
        workspace_tree = (
            self.workspace_service.file_tree(kwargs["workspace_id"], run_id=kwargs["draft_run_id"])
            if self.workspace_service.draft_exists(kwargs["workspace_id"], kwargs["draft_run_id"])
            else []
        )
        draft_source = self.workspace_service.draft_source_dir(kwargs["workspace_id"], kwargs["draft_run_id"])
        for batch in self._group_generation_clusters_for_execution(clusters):
            batch_timeout_seconds = self._whole_file_batch_timeout_seconds(
                batch,
                generation_mode=kwargs["generation_mode"],
            )
            self._sync_generation_batch_started(
                linked_run_id=kwargs["draft_run_id"],
                completed_target_files=completed_target_files,
                total_target_files=total_target_files,
                batch=batch,
            )
            immediate_results: list[dict[str, Any]] = []
            pending_batch: list[dict[str, Any]] = []
            for cluster in batch:
                cluster_name = str(cluster["cluster_name"])
                cluster_targets = list(cluster["target_files"])
                if cluster_name == "shared_static":
                    reuse_result = self._whole_file_static_reuse_fallback_result(
                        workspace_id=kwargs["workspace_id"],
                        draft_run_id=kwargs["draft_run_id"],
                        cluster_name=cluster_name,
                        cluster_targets=cluster_targets,
                        timeout_seconds=0,
                    )
                    if reuse_result is not None:
                        immediate_results.append(reuse_result)
                        continue
                pending_batch.append(cluster)
            if not pending_batch:
                for cluster_result in immediate_results:
                    cluster_name = str(cluster_result["cluster_name"])
                    cluster_target_count = len(list(cluster_result.get("target_files") or []))
                    results.append(cluster_result)
                    completed_target_files += cluster_target_count
                    self._sync_generation_cluster_progress(
                        linked_run_id=kwargs["draft_run_id"],
                        completed_target_files=completed_target_files,
                        total_target_files=total_target_files,
                        cluster_name=cluster_name,
                    )
                continue
            executor = ThreadPoolExecutor(max_workers=len(pending_batch), thread_name_prefix=f"whole-file-batch-{self._whole_file_parallel_group(str(pending_batch[0]['cluster_name']))}")
            future_map: dict[Any, tuple[str, int, list[str]]] = {}
            for cluster in pending_batch:
                cluster_name = str(cluster["cluster_name"])
                cluster_targets = list(cluster["target_files"])
                cluster_target_count = len(cluster_targets)
                logger.info("whole_file_cluster_started workspace_id=%s draft_run_id=%s cluster=%s targets=%s", kwargs["workspace_id"], kwargs["draft_run_id"], cluster_name, cluster_target_count)
                future = self._submit_with_context(
                    executor,
                    self._timed_whole_file_cluster,
                    cluster_name=cluster_name,
                    cluster_targets=cluster_targets,
                    prompt=kwargs["prompt"],
                    grounded_spec=kwargs["grounded_spec"],
                    entity_contract=kwargs.get("entity_contract") or {},
                    role_scope=kwargs["role_scope"],
                    role_contract=kwargs["role_contract"],
                    page_graph=kwargs["page_graph"],
                    scope_mode=kwargs["scope_mode"],
                    intent=kwargs["intent"],
                    file_contexts=kwargs["file_contexts"],
                    generation_mode=kwargs["generation_mode"],
                    creative_direction=kwargs["creative_direction"],
                    workspace_id=kwargs["workspace_id"],
                    draft_run_id=kwargs["draft_run_id"],
                    workspace_tree=workspace_tree,
                    draft_source=draft_source,
                )
                future_map[future] = (cluster_name, cluster_target_count, cluster_targets)
            done, not_done = wait(future_map.keys(), timeout=batch_timeout_seconds, return_when=ALL_COMPLETED)
            pending_fallback_results: list[dict[str, Any]] = []
            if not_done:
                executor.shutdown(wait=False, cancel_futures=True)
                unresolved_pending: list[str] = []
                for future in not_done:
                    cluster_name, _, cluster_targets = future_map[future]
                    fallback_result = self._whole_file_timeout_fallback_result(
                        cluster_name=cluster_name,
                        cluster_targets=cluster_targets,
                        timeout_seconds=batch_timeout_seconds,
                    )
                    if fallback_result is None:
                        fallback_result = self._whole_file_role_ui_fallback_result(
                            workspace_id=kwargs["workspace_id"],
                            draft_run_id=kwargs["draft_run_id"],
                            cluster_name=cluster_name,
                            cluster_targets=cluster_targets,
                            entity_contract=kwargs.get("entity_contract") or {},
                            timeout_seconds=batch_timeout_seconds,
                        )
                    if fallback_result is None:
                        fallback_result = self._whole_file_static_reuse_fallback_result(
                            workspace_id=kwargs["workspace_id"],
                            draft_run_id=kwargs["draft_run_id"],
                            cluster_name=cluster_name,
                            cluster_targets=cluster_targets,
                            timeout_seconds=batch_timeout_seconds,
                        )
                    if fallback_result is None:
                        unresolved_pending.append(cluster_name)
                        continue
                    pending_fallback_results.append(fallback_result)
                    self.workspace_log_service.append(
                        kwargs["workspace_id"],
                        source="generation.cluster_timeout_fallback",
                        message="Applied deterministic timeout fallback for an unresolved whole-file generation cluster.",
                        payload={"draft_run_id": kwargs["draft_run_id"], "cluster_name": cluster_name, "file_paths": cluster_targets},
                    )
                if unresolved_pending:
                    pending_names = ", ".join(unresolved_pending)
                    return {"error": f"Whole-file generation timed out while waiting for cluster result: {pending_names}"}
            try:
                for future in done:
                    cluster_name, cluster_target_count, cluster_targets = future_map[future]
                    cluster_result = future.result()
                    if "error" in cluster_result:
                        fallback_result = self._whole_file_error_fallback_result(
                            workspace_id=kwargs["workspace_id"],
                            draft_run_id=kwargs["draft_run_id"],
                            cluster_name=cluster_name,
                            cluster_targets=cluster_targets,
                            error_message=str(cluster_result["error"]),
                            entity_contract=kwargs.get("entity_contract") or {},
                        )
                        if fallback_result is None:
                            return {"error": str(cluster_result["error"])}
                        cluster_result = fallback_result
                        self.workspace_log_service.append(
                            kwargs["workspace_id"],
                            source="generation.cluster_error_fallback",
                            message="Applied deterministic fallback for a canonical route cluster after a whole-file generation error.",
                            payload={
                                "draft_run_id": kwargs["draft_run_id"],
                                "cluster_name": cluster_name,
                                "file_paths": cluster_targets,
                                "error": str(cluster_result.get("assistant_message") or ""),
                            },
                        )
                    cluster_operations = [DraftFileOperation.model_validate(item) for item in cluster_result.get("operations", [])]
                    if cluster_operations:
                        deduped_cluster_operations = self._dedupe_operations(cluster_operations)
                        self.workspace_service.apply_draft_operations(
                            kwargs["workspace_id"],
                            kwargs["draft_run_id"],
                            deduped_cluster_operations,
                        )
                        for operation in deduped_cluster_operations:
                            if operation.content is not None:
                                kwargs["file_contexts"][operation.file_path] = operation.content
                        self.workspace_log_service.append(
                            kwargs["workspace_id"],
                            source="generation.cluster_persisted",
                            message="Persisted completed code cluster into the draft workspace.",
                            payload={"draft_run_id": kwargs["draft_run_id"], "cluster_name": cluster_name, "file_paths": [operation.file_path for operation in deduped_cluster_operations]},
                        )
                    logger.info("whole_file_cluster_completed workspace_id=%s draft_run_id=%s cluster=%s duration_ms=%s", kwargs["workspace_id"], kwargs["draft_run_id"], cluster_name, cluster_result.get("duration_ms"))
                    results.append(cluster_result)
                    completed_target_files += cluster_target_count
                    self._sync_generation_cluster_progress(
                        linked_run_id=kwargs["draft_run_id"],
                        completed_target_files=completed_target_files,
                        total_target_files=total_target_files,
                        cluster_name=cluster_name,
                    )
                for cluster_result in immediate_results:
                    cluster_name = str(cluster_result["cluster_name"])
                    cluster_target_count = len(list(cluster_result.get("target_files") or []))
                    results.append(cluster_result)
                    completed_target_files += cluster_target_count
                    self._sync_generation_cluster_progress(
                        linked_run_id=kwargs["draft_run_id"],
                        completed_target_files=completed_target_files,
                        total_target_files=total_target_files,
                        cluster_name=cluster_name,
                    )
                for cluster_result in pending_fallback_results:
                    cluster_name = str(cluster_result["cluster_name"])
                    cluster_target_count = len(list(cluster_result.get("target_files") or []))
                    cluster_operations = [DraftFileOperation.model_validate(item) for item in cluster_result.get("operations", [])]
                    if cluster_operations:
                        deduped_cluster_operations = self._dedupe_operations(cluster_operations)
                        self.workspace_service.apply_draft_operations(
                            kwargs["workspace_id"],
                            kwargs["draft_run_id"],
                            deduped_cluster_operations,
                        )
                        for operation in deduped_cluster_operations:
                            if operation.content is not None:
                                kwargs["file_contexts"][operation.file_path] = operation.content
                        self.workspace_log_service.append(
                            kwargs["workspace_id"],
                            source="generation.cluster_persisted",
                            message="Persisted completed code cluster into the draft workspace.",
                            payload={"draft_run_id": kwargs["draft_run_id"], "cluster_name": cluster_name, "file_paths": [operation.file_path for operation in deduped_cluster_operations], "fallback_used": True},
                        )
                    results.append(cluster_result)
                    completed_target_files += cluster_target_count
                    self._sync_generation_cluster_progress(
                        linked_run_id=kwargs["draft_run_id"],
                        completed_target_files=completed_target_files,
                        total_target_files=total_target_files,
                        cluster_name=cluster_name,
                    )
            except Exception as exc:
                executor.shutdown(wait=False, cancel_futures=True)
                return {"error": f"Whole-file cluster failed: {exc}"}
            else:
                executor.shutdown(wait=False, cancel_futures=False)
        page_graph = self.generation_targeting.sanitize_page_graph_role_entries(kwargs["page_graph"])
        operations: list[DraftFileOperation] = [
            DraftFileOperation(file_path="artifacts/generated_app_graph.json", operation="replace", content=json_dumps(page_graph), reason="Persist the planned page graph for validation, preview, and run artifacts."),
            DraftFileOperation(file_path="artifacts/page_graph_verification.json", operation="replace", content=json_dumps(self._build_page_graph_verification_report(page_graph, kwargs["role_scope"])), reason="Persist structural verification for the planned page graph and route tree."),
        ]
        messages: list[str] = []
        latency_breakdown: dict[str, int] = {}
        trace_payloads: dict[str, dict[str, Any]] = {}
        for result in results:
            if "error" in result:
                return result
            operations.extend(result["operations"])
            if str(result.get("assistant_message") or "").strip():
                messages.append(str(result["assistant_message"]).strip())
            latency_breakdown[result["cluster_name"]] = int(result["duration_ms"])
            trace_payloads[result["cluster_name"]] = {
                "message": f"{result['cluster_name'].replace('_', ' ').capitalize()} completed.",
                "payload": {"duration_ms": result["duration_ms"], "target_files": result["target_files"], "operation_count": len(result["operations"]), "write_strategy": "whole_file_build"},
            }
        if any(key.startswith("frontend_") for key in latency_breakdown):
            latency_breakdown["whole_file_frontend_ms"] = sum(value for key, value in latency_breakdown.items() if key.startswith("frontend_"))
        if "backend_core" in latency_breakdown:
            latency_breakdown["whole_file_backend_ms"] = latency_breakdown["backend_core"]
        return {
            "assistant_message": " ".join(messages).strip() or f"Generated {len(target_files)} files using whole-file bundle generation.",
            "operations": self._dedupe_operations(operations),
            "planner_contract_gap_targets": [],
            "effective_target_files": list(target_files),
            "latency_breakdown": latency_breakdown,
            "trace_payloads": trace_payloads,
        }

    async def _resolve_page_file_edits_async(self, **kwargs: Any) -> list[dict[str, Any]]:
        selected_pages = kwargs["selected_pages"]
        semaphore = asyncio.Semaphore(min(self._page_edit_parallelism(scope_mode=kwargs["scope_mode"], generation_mode=kwargs["generation_mode"]), len(selected_pages)))

        async def run_one(role: str, page: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await asyncio.to_thread(
                    self._resolve_page_file_edit,
                    prompt=kwargs["prompt"],
                    grounded_spec=kwargs["grounded_spec"],
                    entity_contract=kwargs.get("entity_contract") or {},
                    role=role,
                    page=page,
                    page_graph=kwargs["page_graph"],
                    role_contract=kwargs["role_contract"],
                    scope_mode=kwargs["scope_mode"],
                    intent=kwargs["intent"],
                    file_contexts=kwargs["file_contexts"],
                    generation_mode=kwargs["generation_mode"],
                    creative_direction=kwargs["creative_direction"],
                    workspace_id=kwargs.get("workspace_id"),
                    draft_run_id=kwargs.get("draft_run_id"),
                    workspace_tree=kwargs.get("workspace_tree"),
                    draft_source=kwargs.get("draft_source"),
                )
        return list(await asyncio.gather(*[run_one(role, page) for role, page in selected_pages]))
