from __future__ import annotations

from pathlib import Path

from app.services.check_runner import CheckRunner


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


def test_frontend_api_refs_are_generic() -> None:
    refs = CheckRunner._extract_frontend_api_refs(
        "await fetchJSON('/api/entities', { method: 'POST' });\n"
        "await fetchJSON(`/api/entities/${entityId}/state`, { method: 'PATCH' });\n"
    )

    assert ("POST", "/api/entities") in refs
    assert ("PATCH", "/api/entities/{param}/state") in refs


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
