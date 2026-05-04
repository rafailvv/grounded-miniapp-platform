from __future__ import annotations

from pathlib import Path

from app.services.check_runner import CheckRunner
from app.validators.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator


def test_form_wiring_accepts_form_property_reads(tmp_path: Path) -> None:
    js_path = tmp_path / "app.js"
    html = (
        "<form id='manager-action-form'>"
        "<select name='entity_id'></select>"
        "<textarea name='note'></textarea>"
        "<button type='submit'>Save</button>"
        "</form>"
    )
    js = (
        "const managerForm = document.querySelector('#manager-action-form');\n"
        "managerForm?.addEventListener('submit', (event) => {\n"
        "  event.preventDefault();\n"
        "  const entityId = managerForm.entity_id?.value;\n"
        "  const note = managerForm.note?.value?.trim();\n"
        "  fetch('/api/entities/actions', { method: 'POST', body: JSON.stringify({ entity_id: entityId, note }) });\n"
        "});\n"
    )

    issues = CheckRunner._form_wiring_issues("miniapp/app/static/manager/index.html", js_path, html, js)

    assert not any(issue.code == "platform.workflow_form_field_not_submitted" for issue in issues)


def test_form_wiring_rejects_path_id_read_from_unrelated_object(tmp_path: Path) -> None:
    js_path = tmp_path / "app.js"
    html = (
        "<form id='specialist-action-form'>"
        "<input name='id' type='number'>"
        "<select name='state'></select>"
        "<button type='submit'>Save</button>"
        "</form>"
    )
    js = (
        "const form = document.querySelector('#specialist-action-form');\n"
        "form.addEventListener('submit', (event) => {\n"
        "  event.preventDefault();\n"
        "  const formData = new FormData(form);\n"
        "  const entityId = formData.get('entity_id');\n"
        "  fetch(`/api/entities/${entityId}`, { method: 'PATCH', body: JSON.stringify({ state: formData.get('state') }) });\n"
        "});\n"
        "function render(item) { return item.id; }\n"
    )

    issues = CheckRunner._form_wiring_issues("miniapp/app/static/specialist/index.html", js_path, html, js)

    assert any(issue.code == "platform.workflow_form_field_not_submitted" and "id" in issue.message for issue in issues)


def test_form_wiring_accepts_change_only_filter_forms(tmp_path: Path) -> None:
    js_path = tmp_path / "app.js"
    html = (
        "<form id='filter-form'>"
        "<select name='state' id='state-filter'><option value='all'>All</option></select>"
        "</form>"
    )
    js = (
        "const form = document.getElementById('filter-form');\n"
        "const filter = document.getElementById('state-filter');\n"
        "form?.addEventListener('change', () => { renderFiltered(filter.value); });\n"
    )

    issues = CheckRunner._form_wiring_issues("miniapp/app/static/specialist/queue/index.html", js_path, html, js)

    assert not any(issue.code == "platform.workflow_form_without_submit_handler" for issue in issues)


def test_frontend_api_refs_are_generic() -> None:
    refs = CheckRunner._extract_frontend_api_refs(
        "await fetchJSON('/api/entities', { method: 'POST' });\n"
        "await fetchJSON(`/api/entities/${entityId}/state`, { method: 'PATCH' });\n"
    )

    assert ("POST", "/api/entities") in refs
    assert ("PATCH", "/api/entities/{param}/state") in refs


def test_connectivity_accepts_api_root_router_prefix(tmp_path: Path) -> None:
    routes_dir = tmp_path / "miniapp/app/routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api')\n"
        "@router.get('')\n"
        "def list_items(): return []\n"
        "@router.post('')\n"
        "def create_item(): return {}\n",
        encoding="utf-8",
    )
    client_dir = tmp_path / "miniapp/app/static/client"
    client_dir.mkdir(parents=True)
    (client_dir / "app.js").write_text(
        "const API_BASE = '/api';\n"
        "fetch(API_BASE);\n"
        "fetch(API_BASE, { method: 'POST', body: '{}' });\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(tmp_path)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_preview_url_candidates_allow_container_to_reach_host_preview() -> None:
    candidates = CheckRunner._preview_base_url_candidates("http://localhost:16544")

    assert candidates[0] == "http://host.docker.internal:16544"
    assert "http://127.0.0.1:16544" in candidates
    assert "http://localhost:16544" in candidates


def test_generated_js_html_includes_failure_is_reported_generically() -> None:
    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "AssertionError [ERR_ASSERTION]: The expression evaluated to a falsy value:",
            "  assert.ok(html.includes('id=\"client-main-form\"'))",
            "at TestContext.<anonymous> (file:///workspace/miniapp/tests/generated_app.test.mjs:20:10)",
        ]
    )

    issue = diagnostics["js_test_missing_generated_source_token"]
    assert issue["problem"] == "generated_js_test_requires_token_on_wrong_page"
    assert issue["token"] == 'id="client-main-form"'


def _write_role_surface(root: Path, role: str, *, child_pages: int) -> None:
    role_dir = root / "miniapp/app/static" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    html = (
        "<main class='page-shell'><section class='page'>"
        "<h1>Shared workspace</h1><form id='main-form'><button type='submit'>Save</button></form>"
        "<button id='refresh-button'>Refresh</button><div class='dashboard summary metric count'></div>"
        "</section></main>"
    )
    js = (
        "document.querySelector('#main-form')?.addEventListener('submit', event => {"
        "event.preventDefault(); fetch('/api/entities', { method: 'POST', body: '{}' }); });"
        "document.querySelector('#refresh-button')?.addEventListener('click', () => {"
        "fetch('/api/entities/1', { method: 'PATCH', body: '{}' }); });"
        "const status = 'update progress action save review';"
    )
    css = ".page{display:block}.card{padding:12px}.button{min-height:44px}.list{min-width:0}\n"
    (role_dir / "index.html").write_text(html, encoding="utf-8")
    (role_dir / "app.js").write_text(js, encoding="utf-8")
    (role_dir / "styles.css").write_text(css, encoding="utf-8")
    for index in range(child_pages):
        child_dir = role_dir / f"step-{index + 1}"
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "index.html").write_text(html, encoding="utf-8")


def test_route_manifest_allows_pages_and_routes_for_same_static_file(tmp_path: Path) -> None:
    role_dir = tmp_path / "miniapp/app/static/client/detail"
    role_dir.mkdir(parents=True)
    (role_dir / "index.html").write_text(
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="/static/shared/base.css">'
        '<script src="/static/preview_bridge.js" defer></script>'
        "</head><body><main class='page-shell'>Detail</main></body></html>",
        encoding="utf-8",
    )
    shared_dir = tmp_path / "miniapp/app/static/shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "base.css").write_text(".page-shell{padding:24px;min-width:0}", encoding="utf-8")
    (tmp_path / "miniapp/app/static/preview_bridge.js").write_text("window.__bridge=true;", encoding="utf-8")
    generated_dir = tmp_path / "miniapp/app/generated"
    generated_dir.mkdir(parents=True)
    (generated_dir / "route_manifest.json").write_text(
        """
        {
          "roles": {
            "client": {
              "pages": [
                {"route_path": "/client/detail", "file_path": "static/client/detail/index.html"}
              ],
              "routes": {
                "/client/detail": "static/client/detail/index.html"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )

    issues = BuildValidator().validate(tmp_path)

    assert not any(issue.code == "build.duplicate_static_route" for issue in issues)


def test_route_manifest_rejects_same_route_to_different_static_files(tmp_path: Path) -> None:
    for slug in ("detail", "other"):
        page_dir = tmp_path / "miniapp/app/static/client" / slug
        page_dir.mkdir(parents=True)
        (page_dir / "index.html").write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="/static/shared/base.css">'
            '<script src="/static/preview_bridge.js" defer></script>'
            "</head><body><main class='page-shell'>Page</main></body></html>",
            encoding="utf-8",
        )
    shared_dir = tmp_path / "miniapp/app/static/shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "base.css").write_text(".page-shell{padding:24px;min-width:0}", encoding="utf-8")
    (tmp_path / "miniapp/app/static/preview_bridge.js").write_text("window.__bridge=true;", encoding="utf-8")
    generated_dir = tmp_path / "miniapp/app/generated"
    generated_dir.mkdir(parents=True)
    (generated_dir / "route_manifest.json").write_text(
        """
        {
          "roles": {
            "client": {
              "pages": [
                {"route_path": "/client/detail", "file_path": "static/client/detail/index.html"}
              ],
              "routes": {
                "/client/detail": "static/client/other/index.html"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )

    issues = BuildValidator().validate(tmp_path)

    assert any(issue.code == "build.duplicate_static_route" for issue in issues)
