from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.main import create_app
from app.models.common import GenerationMode
from app.models.domain import RunCheckResult, WorkspaceRecord
from app.services.check_runner import CheckRunner
from app.services.workflow_acceptance import build_acceptance_contract
from app.services.workspace.preview_service import PreviewService
from app.validators.build_validator import BuildValidator
from app.validators.connectivity_validator import ConnectivityValidator


def _write_role_root(source_dir: Path, role: str, *, app_title: str = "Aurora Shop") -> None:
    role_dir = source_dir / "miniapp/app/static" / role
    role_dir.mkdir(parents=True, exist_ok=True)
    role_dir.joinpath("index.html").write_text(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>{app_title} {role}</title>
    <link rel="stylesheet" href="/static/shared/base.css" />
    <link rel="stylesheet" href="/static/{role}/styles.css" />
  </head>
  <body>
    <main class="page-shell">
      <h1>{app_title}</h1>
      <nav>
        <a href="/{role}/profile">Profile</a>
        <a href="/{role}/catalog">Catalog</a>
      </nav>
      <p>{app_title} connected {role} workspace with shared catalog, orders, and role-specific actions.</p>
      {"<form id='record-form'><button type='submit'>Create order</button></form>" if role == "client" else ""}
      {"<button data-status-action>Confirm order</button><button data-status-action>Done</button>" if role == "specialist" else ""}
      {"<section class='metric-card'>Dashboard total count</section><button data-manager-action>Review order</button>" if role == "manager" else ""}
    </main>
    <script src="/static/preview_bridge.js" defer></script>
    <script src="/static/{role}/app.js" defer></script>
  </body>
</html>
""",
        encoding="utf-8",
    )
    role_js = {
        "client": "window.setupPreviewBridge?.('client');\nfetch('/api/orders', { method: 'POST', body: JSON.stringify({ title: 'User order' }) });\n",
        "specialist": "window.setupPreviewBridge?.('specialist');\nfetch('/api/orders/1', { method: 'PATCH', body: JSON.stringify({ status: 'confirmed' }) });\nconst queue = 'confirm status queue';\n",
        "manager": "window.setupPreviewBridge?.('manager');\nfetch('/api/orders/1', { method: 'PATCH', body: JSON.stringify({ status: 'reviewed' }) });\nconst dashboard = 'metric dashboard review';\n",
    }[role]
    role_dir.joinpath("app.js").write_text(role_js, encoding="utf-8")
    role_dir.joinpath("styles.css").write_text(
        f".page-shell.{role}-app {{ color: #172033; background: #ffffff; }}\n"
        ".record-card { border: 1px solid #d8dee8; }\n"
        ".metric-card { padding: 12px; }\n",
        encoding="utf-8",
    )


def _write_role_child(source_dir: Path, role: str, slug: str, *, app_title: str = "Aurora Shop") -> None:
    page_dir = source_dir / "miniapp/app/static" / role / slug
    page_dir.mkdir(parents=True, exist_ok=True)
    page_dir.joinpath("index.html").write_text(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>{app_title} {role} {slug}</title>
    <link rel="stylesheet" href="/static/shared/base.css" />
    <link rel="stylesheet" href="/static/{role}/styles.css" />
  </head>
  <body>
    <main class="page-shell">
      <h1>{app_title}</h1>
      <p>{app_title} {role} {slug} page for shared products, orders, and role-specific actions.</p>
    </main>
    <script src="/static/preview_bridge.js" defer></script>
    <script src="/static/{role}/app.js" defer></script>
  </body>
</html>
""",
        encoding="utf-8",
    )


def _write_generated_test_placeholders(source_dir: Path) -> None:
    tests_dir = source_dir / "miniapp/tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("test_generated_app.py").write_text("import unittest\n\nclass GeneratedAppTest(unittest.TestCase):\n    pass\n", encoding="utf-8")
    tests_dir.joinpath("generated_app.test.mjs").write_text("import test from 'node:test';\n\ntest('placeholder', () => {});\n", encoding="utf-8")


def _write_multipage_role_surfaces(source_dir: Path) -> None:
    manifest = {"roles": {}, "shared": {}}
    for role in ("client", "specialist", "manager"):
        _write_role_root(source_dir, role)
        _write_role_child(source_dir, role, "profile")
        _write_role_child(source_dir, role, "catalog")
        manifest["roles"][role] = {
            "routes": {
                f"/{role}": f"static/{role}/index.html",
                f"/{role}/profile": f"static/{role}/profile/index.html",
                f"/{role}/catalog": f"static/{role}/catalog/index.html",
            }
        }
    manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_template_shell_passes_platform_build_validator(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Validator Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)

    issues = BuildValidator().validate(source_dir)

    assert issues == []


def test_build_validator_flags_missing_dom_id(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="DOM Contract Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    script = source_dir / "miniapp/app/static/client/app.js"
    script.write_text(script.read_text(encoding="utf-8") + "\ndocument.getElementById('missing-target');\n", encoding="utf-8")

    issues = BuildValidator().validate(source_dir)

    assert any(issue.code == "build.page_script_dom_contract" for issue in issues)


def test_build_validator_accepts_shared_role_script_ids_on_child_pages(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Shared DOM Contract Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    client_dir = source_dir / "miniapp/app/static/client"
    orders_dir = client_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    client_dir.joinpath("index.html").write_text(
        """<!doctype html><html><head><link rel="stylesheet" href="/static/shared/base.css" /></head>
<body><main class="page-shell" id="client-root"></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>""",
        encoding="utf-8",
    )
    orders_dir.joinpath("index.html").write_text(
        """<!doctype html><html><head><link rel="stylesheet" href="/static/shared/base.css" /></head>
<body><main class="page-shell" id="order-form"></main><script src="/static/preview_bridge.js" defer></script><script src="/static/client/app.js" defer></script></body></html>""",
        encoding="utf-8",
    )
    client_dir.joinpath("app.js").write_text(
        "document.getElementById('client-root');\ndocument.getElementById('order-form');\n",
        encoding="utf-8",
    )
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "roles": {
                    "client": {
                        "routes": {
                            "/client": "static/client/index.html",
                            "/client/orders": "static/client/orders/index.html",
                        }
                    }
                },
                "shared": {},
            }
        ),
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.page_script_dom_contract" for issue in issues)


def test_build_validator_flags_duplicate_runtime_route(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Route Contract Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    duplicate = source_dir / "miniapp/app/routes/duplicate_health.py"
    duplicate.write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n@router.get('/health')\ndef health():\n    return {'ok': True}\n",
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert any(issue.code == "build.duplicate_runtime_route" for issue in issues)


def test_build_validator_accepts_manifest_static_paths_without_workspace_prefix(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Manifest Static Path Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        '{"roles":{"client":{"pages":[{"route_path":"/client","file_path":"static/client/index.html"}]}},"shared":{}}',
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.missing_static_page" for issue in issues)


def test_build_validator_accepts_compact_manifest_routes_map_for_child_pages(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Manifest Routes Map Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    _write_role_child(source_dir, "client", "catalog")
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "routes": {
                    "/client": "static/client/index.html",
                    "/client/catalog": "static/client/catalog/index.html",
                },
                "shared": {},
            }
        ),
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.missing_static_asset" for issue in issues)
    assert not any(issue.location == "miniapp/app/static/client/catalog/styles.css" for issue in issues)


def test_build_validator_accepts_slash_role_shorthand_manifest(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Manifest Role Shorthand Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    _write_role_child(source_dir, "client", "catalog")
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "roles": {
                    "/client": {
                        "catalog": "static/client/catalog/index.html",
                    }
                },
                "shared": {},
            }
        ),
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.missing_static_asset" for issue in issues)
    assert not any(issue.location == "miniapp/app/static/client/catalog/styles.css" for issue in issues)


def test_build_validator_accepts_roles_as_direct_route_map(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Manifest Direct Roles Map Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    _write_role_child(source_dir, "client", "menu")
    manifest = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "roles": {
                    "/client/menu": "static/client/menu/index.html",
                },
                "shared": {},
            }
        ),
        encoding="utf-8",
    )

    issues = BuildValidator().validate(source_dir)

    assert not any(issue.code == "build.missing_static_asset" for issue in issues)
    assert not any(issue.location == "miniapp/app/static/client/menu/styles.css" for issue in issues)


def test_template_role_page_resolver_accepts_compact_routes_map(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_route_pages = repo_root / "runtime/templates/base-miniapp/miniapp/app/routes/role_pages.py"
    route_pages_path = tmp_path / "miniapp/app/routes/role_pages.py"
    route_pages_path.parent.mkdir(parents=True)
    route_pages_path.write_text(source_route_pages.read_text(encoding="utf-8"), encoding="utf-8")
    page_path = tmp_path / "miniapp/app/static/client/profile/index.html"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("<main class=\"page-shell\">Profile</main>", encoding="utf-8")
    manifest_path = tmp_path / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"routes": {"/client/profile": "static/client/profile/index.html"}}),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("role_pages_under_test", route_pages_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.resolve_role_page("client", "/client/profile") == page_path
    try:
        module.resolve_role_page("client", "/client/missing")
    except KeyError:
        pass
    else:
        raise AssertionError("Missing compact route should raise KeyError")


def test_template_role_page_resolver_accepts_slash_role_shorthand_manifest(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_route_pages = repo_root / "runtime/templates/base-miniapp/miniapp/app/routes/role_pages.py"
    route_pages_path = tmp_path / "miniapp/app/routes/role_pages.py"
    route_pages_path.parent.mkdir(parents=True)
    route_pages_path.write_text(source_route_pages.read_text(encoding="utf-8"), encoding="utf-8")
    page_path = tmp_path / "miniapp/app/static/client/catalog/index.html"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("<main class=\"page-shell\">Catalog</main>", encoding="utf-8")
    manifest_path = tmp_path / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"roles": {"/client": {"catalog": "static/client/catalog/index.html"}}}),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("role_pages_under_test", route_pages_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.resolve_role_page("client", "/client/catalog") == page_path


def test_template_role_page_resolver_accepts_roles_as_direct_route_map(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    source_route_pages = repo_root / "runtime/templates/base-miniapp/miniapp/app/routes/role_pages.py"
    route_pages_path = tmp_path / "miniapp/app/routes/role_pages.py"
    route_pages_path.parent.mkdir(parents=True)
    route_pages_path.write_text(source_route_pages.read_text(encoding="utf-8"), encoding="utf-8")
    page_path = tmp_path / "miniapp/app/static/client/menu/index.html"
    page_path.parent.mkdir(parents=True)
    page_path.write_text("<main class=\"page-shell\">Menu</main>", encoding="utf-8")
    manifest_path = tmp_path / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"roles": {"/client/menu": "static/client/menu/index.html"}}),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("role_pages_under_test", route_pages_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.resolve_role_page("client", "/client/menu") == page_path


def test_static_check_rejects_typescript_syntax_inside_plain_js(tmp_path: Path) -> None:
    backend_dir = tmp_path / "miniapp"
    script = backend_dir / "app/static/client/app.js"
    script.parent.mkdir(parents=True)
    script.write_text("const button = (event.target as HTMLElement).closest('button');\n", encoding="utf-8")

    runner = object.__new__(CheckRunner)
    result = runner._run_static_js_syntax_check(backend_dir)

    assert result.status == "failed"
    assert "Static JavaScript syntax check failed" in "\n".join(result.logs)
    assert result.diagnostics["static_js_syntax_error"]["file_path"] == "miniapp/app/static/client/app.js"
    assert result.diagnostics["static_js_syntax_error"]["line"] == 1
    assert "node --check passes" in result.diagnostics["static_js_syntax_error"]["required_action"]


def test_static_check_installs_miniapp_requirements_before_import_smoke(
    monkeypatch,
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "source" / "miniapp"
    (backend_dir / "app").mkdir(parents=True)
    (backend_dir / "app" / "main.py").write_text("from app.db import Base\n", encoding="utf-8")
    (backend_dir / "requirements.txt").write_text("sqlalchemy==2.0.43\n", encoding="utf-8")
    runner = object.__new__(CheckRunner)
    calls: list[str] = []

    monkeypatch.setattr(
        runner,
        "_run_backend_compile",
        lambda _backend_dir: RunCheckResult(name="changed_files_static", status="passed", logs=["compile"]),
    )
    monkeypatch.setattr(
        runner,
        "_run_static_js_syntax_check",
        lambda _backend_dir: RunCheckResult(name="changed_files_static", status="passed", logs=["js"]),
    )

    def fake_install(_backend_dir, *, result_name="generated_app_python_tests", purpose="Generated Python dependency"):
        calls.append(f"install:{result_name}:{purpose}")
        return None

    def fake_import(_backend_dir):
        calls.append("import")
        return RunCheckResult(name="changed_files_static", status="passed", logs=["import"])

    monkeypatch.setattr(runner, "_install_python_requirements", fake_install)
    monkeypatch.setattr(runner, "_run_backend_import_smoke", fake_import)

    result = runner._static_check(source_dir=tmp_path / "source", changed_files=["miniapp/app/static/client/index.html"])

    assert result.status == "passed"
    assert calls == ["install:changed_files_static:Backend import-smoke dependency", "import"]


def test_dom_contract_accepts_role_app_js_ids_declared_on_child_pages(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    role_dir = source_dir / "miniapp/app/static/client"
    child_dir = role_dir / "orders"
    child_dir.mkdir(parents=True)
    role_dir.joinpath("index.html").write_text("<main id='client-root'></main>", encoding="utf-8")
    child_dir.joinpath("index.html").write_text("<main id='order-form'></main>", encoding="utf-8")
    role_dir.joinpath("app.js").write_text(
        "document.getElementById('order-form');\n"
        "document.querySelector('#client-root');\n",
        encoding="utf-8",
    )

    issues = CheckRunner._dom_contract_issues(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js"],
    )

    assert issues == []


def test_dom_contract_rejects_unguarded_shared_role_script_on_child_page(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    role_dir = source_dir / "miniapp/app/static/client"
    catalog_dir = role_dir / "catalog"
    orders_dir = role_dir / "orders"
    catalog_dir.mkdir(parents=True)
    orders_dir.mkdir(parents=True)
    catalog_dir.joinpath("index.html").write_text(
        "<main><section id='catalog-list'></section><p id='catalog-empty'></p></main><script src='/static/client/app.js'></script>",
        encoding="utf-8",
    )
    orders_dir.joinpath("index.html").write_text(
        "<main><span id='cart-count'></span></main><script src='/static/client/app.js'></script>",
        encoding="utf-8",
    )
    role_dir.joinpath("app.js").write_text(
        "const catalogList = document.getElementById('catalog-list');\n"
        "const cartCount = document.getElementById('cart-count');\n"
        "function renderCatalog() { catalogList.innerHTML = ''; }\n"
        "function renderCart() { cartCount.textContent = '0'; }\n"
        "window.addEventListener('DOMContentLoaded', () => { renderCatalog(); renderCart(); });\n",
        encoding="utf-8",
    )

    issues = CheckRunner._dom_contract_issues(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js"],
    )

    assert any(issue.code == "platform.unchecked_page_dom_id" for issue in issues)
    assert any("cart-count" in issue.message or "catalog-list" in issue.message for issue in issues)


def test_dom_contract_accepts_guarded_shared_role_script_on_child_pages(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    role_dir = source_dir / "miniapp/app/static/client"
    catalog_dir = role_dir / "catalog"
    orders_dir = role_dir / "orders"
    catalog_dir.mkdir(parents=True)
    orders_dir.mkdir(parents=True)
    catalog_dir.joinpath("index.html").write_text(
        "<main><section id='catalog-list'></section><p id='catalog-empty'></p></main><script src='/static/client/app.js'></script>",
        encoding="utf-8",
    )
    orders_dir.joinpath("index.html").write_text(
        "<main><span id='cart-count'></span><span id='status-badge'></span></main><script src='/static/client/app.js'></script>",
        encoding="utf-8",
    )
    role_dir.joinpath("app.js").write_text(
        "const catalogList = document.getElementById('catalog-list');\n"
        "const catalogEmpty = document.getElementById('catalog-empty');\n"
        "const cartCount = document.getElementById('cart-count');\n"
        "const badges = document.querySelectorAll('#status-badge');\n"
        "function renderCatalog() {\n"
        "  if (!catalogList || !catalogEmpty) return;\n"
        "  catalogEmpty.hidden = false;\n"
        "  catalogList.innerHTML = '';\n"
        "}\n"
        "function renderCart() { cartCount && (cartCount.textContent = '0'); }\n"
        "function renderBadges() { badges.forEach((badge) => { badge.textContent = 'ok'; }); }\n"
        "window.addEventListener('DOMContentLoaded', () => { renderCatalog(); renderCart(); });\n",
        encoding="utf-8",
    )

    issues = CheckRunner._dom_contract_issues(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js"],
    )

    assert issues == []


def test_agentic_platform_invariants_reject_neutral_role_template(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Neutral Role Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/specialist/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.neutral_role_template" in "\n".join(result.logs)
    assert "specialist" in result.diagnostics["role_coverage"]


def test_focused_css_edit_check_profile_skips_generated_tests_and_preview(tmp_path: Path) -> None:
    class FakeValidationSuite:
        def validate_build(self, workspace_path: Path):
            del workspace_path
            return []

        def validate_connectivity(self, workspace_path: Path):
            del workspace_path
            return []

    css_path = tmp_path / "miniapp/app/static/client/styles.css"
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text(".page-shell { color: #1f2937; background: #ffffff; }\n", encoding="utf-8")
    runner = CheckRunner(FakeValidationSuite(), object())  # type: ignore[arg-type]

    execution = runner.run(
        workspace_id="ws",
        run_id="run",
        source_dir=tmp_path,
        changed_files=["miniapp/app/static/client/styles.css"],
        scope_mode="agentic",
        check_profile="focused_edit",
        intent="edit",
        generation_mode=GenerationMode.FAST,
    )

    results = {result.name: result for result in execution.results}
    assert results["schema_validators"].status == "skipped"
    assert results["connectivity_validators"].status == "skipped"
    assert results["changed_files_static"].status == "passed"
    assert results["platform_invariants"].status == "passed"
    assert results["platform_invariants"].diagnostics["generated_tests"]["status"] == "skipped"
    assert results["generated_app_python_tests"].status == "skipped"
    assert "focused CSS-only visual edit" in results["generated_app_python_tests"].details
    assert results["generated_app_js_tests"].status == "skipped"
    assert results["preview_boot_smoke"].status == "skipped"
    assert results["preview_connectivity_smoke"].status == "skipped"
    assert results["browser_flow_smoke"].status == "skipped"


def _write_commerce_flow_source(source_dir: Path, *, handled_cart: bool) -> None:
    client_dir = source_dir / "miniapp/app/static/client"
    specialist_dir = source_dir / "miniapp/app/static/specialist"
    manager_dir = source_dir / "miniapp/app/static/manager"
    for directory in (client_dir, specialist_dir, manager_dir):
        directory.mkdir(parents=True, exist_ok=True)
    client_dir.joinpath("index.html").write_text(
        "<button data-action='add-to-cart'>В корзину</button><button id='checkout'>Оформить заказ</button>",
        encoding="utf-8",
    )
    client_js = (
        "const cart = [];\n"
        "fetch('/api/products');\n"
        "document.addEventListener('click', (event) => { if (event.target.matches('[data-action=\"add-to-cart\"]')) { cart.push('product'); } });\n"
        "fetch('/api/orders', { method: 'POST', body: JSON.stringify({ items: cart }) });\n"
        if handled_cart
        else "const cart = [];\nfetch('/api/products');\nfetch('/api/orders', { method: 'POST', body: JSON.stringify({ items: cart }) });\n"
    )
    client_dir.joinpath("app.js").write_text(client_js, encoding="utf-8")
    specialist_dir.joinpath("index.html").write_text("<form id='product-form'></form><section id='orders'></section>", encoding="utf-8")
    specialist_dir.joinpath("app.js").write_text(
        "fetch('/api/products', { method: 'POST', body: JSON.stringify({ name: 'test' }) });\n"
        "fetch('/api/orders');\n"
        "fetch('/api/orders/1', { method: 'PATCH', body: JSON.stringify({ status: 'packed' }) });\n",
        encoding="utf-8",
    )
    manager_dir.joinpath("index.html").write_text("<section>Dashboard summary total count</section>", encoding="utf-8")
    manager_dir.joinpath("app.js").write_text(
        "fetch('/api/orders');\nfetch('/api/orders/1', { method: 'PATCH', body: JSON.stringify({ status: 'reviewed' }) });\n",
        encoding="utf-8",
    )
    routes_dir = source_dir / "miniapp/app/routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    routes_dir.joinpath("store.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/products')\n"
        "def products(): return []\n"
        "@router.post('/api/products')\n"
        "def create_product(payload: dict): return payload\n"
        "@router.patch('/api/products/{product_id}')\n"
        "def patch_product(product_id: str, payload: dict): return payload\n"
        "@router.get('/api/orders')\n"
        "def orders(): return []\n"
        "@router.post('/api/orders')\n"
        "def create_order(payload: dict): return payload\n"
        "@router.patch('/api/orders/{order_id}')\n"
        "def patch_order(order_id: str, payload: dict): return payload\n",
        encoding="utf-8",
    )
    tests_dir = source_dir / "miniapp/tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.joinpath("test_generated_app.py").write_text(
        "from fastapi.testclient import TestClient\n# GET POST PATCH products orders cart\n",
        encoding="utf-8",
    )
    tests_dir.joinpath("generated_app.test.mjs").write_text(
        "import test from 'node:test';\n// products orders cart GET POST PATCH\n",
        encoding="utf-8",
    )


def test_frontend_interaction_static_smoke_rejects_unhandled_cart_button(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_commerce_flow_source(source_dir, handled_cart=False)
    contract = build_acceptance_contract(
        prompt="Интернет-магазин: специалист добавляет товар, клиент кладет в корзину и оформляет заказ",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
    )
    runner = object.__new__(CheckRunner)

    result = runner._frontend_interaction_static_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js"],
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
    )

    assert result.status == "failed"
    assert "platform.workflow_add_to_cart_unhandled" in "\n".join(result.logs)


def test_frontend_interaction_static_smoke_accepts_complete_commerce_flow(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_commerce_flow_source(source_dir, handled_cart=True)
    contract = build_acceptance_contract(
        prompt="Интернет-магазин: специалист добавляет товар, клиент кладет в корзину и оформляет заказ",
        intent="create",
        generation_mode=GenerationMode.BALANCED,
    )
    runner = object.__new__(CheckRunner)

    result = runner._frontend_interaction_static_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js"],
        intent="create",
        generation_mode=GenerationMode.BALANCED,
        acceptance_contract=contract,
    )

    assert result.status == "passed"


def test_fast_gate_runs_generated_tests_for_create_workflow(monkeypatch, tmp_path: Path) -> None:
    class FakeValidationSuite:
        def validate_build(self, workspace_path: Path):
            del workspace_path
            return []

        def validate_connectivity(self, workspace_path: Path):
            del workspace_path
            return []

    runner = CheckRunner(FakeValidationSuite(), object())  # type: ignore[arg-type]
    calls: list[str] = []
    monkeypatch.setattr(
        runner,
        "_static_check",
        lambda source_dir, changed_files: RunCheckResult(name="changed_files_static", status="passed", logs=["static"]),
    )
    monkeypatch.setattr(
        runner,
        "_platform_invariants_smoke",
        lambda **kwargs: RunCheckResult(name="platform_invariants", status="passed", logs=["platform"]),
    )
    monkeypatch.setattr(
        runner,
        "_frontend_interaction_static_smoke",
        lambda **kwargs: RunCheckResult(name="frontend_interaction_static_smoke", status="passed", logs=["flow"]),
    )

    def fake_python(_backend_dir, *, require_present=False):
        calls.append(f"python:{require_present}")
        return RunCheckResult(name="generated_app_python_tests", status="passed", logs=["python"])

    def fake_js(_backend_dir, *, require_present=False):
        calls.append(f"js:{require_present}")
        return RunCheckResult(name="generated_app_js_tests", status="passed", logs=["js"])

    monkeypatch.setattr(runner, "_run_python_app_tests", fake_python)
    monkeypatch.setattr(runner, "_run_js_app_tests", fake_js)

    execution = runner.run(
        workspace_id="ws",
        run_id="run",
        source_dir=tmp_path,
        changed_files=["miniapp/app/static/client/app.js"],
        scope_mode="agentic",
        check_profile="fast_gate",
        intent="create",
        generation_mode=GenerationMode.FAST,
        acceptance_contract={"required": True, "features": {"status_update": True}, "flows": []},
    )

    results = {result.name: result for result in execution.results}
    assert calls == ["python:True", "js:True"]
    assert results["generated_app_python_tests"].status == "passed"
    assert results["generated_app_js_tests"].status == "passed"
    assert results["preview_boot_smoke"].status == "skipped"


def test_agentic_platform_invariants_reject_single_page_role_surfaces(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    for role in ("client", "specialist", "manager"):
        _write_role_root(source_dir, role)
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.single_page_role_surface" in "\n".join(result.logs)
    assert result.diagnostics["multipage_coverage"]["client"]["route_count"] == 1


def test_agentic_platform_invariants_reject_placeholder_role_css(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    (source_dir / "miniapp/app/static/specialist/styles.css").write_text(
        "/* Generated specialist page styles can replace this file. */\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/specialist/styles.css"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.placeholder_role_css" in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_role_css_that_collapses_shell_spacing(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    (source_dir / "miniapp/app/static/client/styles.css").write_text(
        ".page-shell { padding-top: 32px; color: #172033; }\n"
        ".record-card { border: 1px solid #d8dee8; }\n"
        ".metric-card { padding: 12px; }\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/styles.css"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.role_css_collapses_shell_safe_top_spacing" in "\n".join(result.logs)


def test_agentic_platform_invariants_allow_inner_spacing_inside_shell(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    (source_dir / "miniapp/app/static/client/styles.css").write_text(
        ".page-shell .hero-panel { padding: 32px; color: #172033; }\n"
        ".record-card { border: 1px solid #d8dee8; }\n"
        ".metric-card { padding: 12px; }\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/styles.css"],
        scope_mode="agentic",
    )

    assert "platform.role_css_collapses_shell_safe_top_spacing" not in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_cross_role_navigation(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    client_html = source_dir / "miniapp/app/static/client/index.html"
    client_html.write_text(
        client_html.read_text(encoding="utf-8").replace("</nav>", '<a href="/specialist">Specialist app</a></nav>'),
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.cross_role_navigation" in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_technical_role_copy(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    client_html = source_dir / "miniapp/app/static/client/index.html"
    client_html.write_text(
        client_html.read_text(encoding="utf-8").replace("<h1>Aurora Shop</h1>", "<p>Client app</p><h1>Aurora Shop</h1><p>source request</p>"),
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.technical_role_copy" in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_identical_role_apps(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    manifest = {"roles": {}, "shared": {}}
    identical_html = """<!doctype html>
<html><head><link rel="stylesheet" href="/static/client/styles.css" /></head>
<body><main class="page-shell role-app"><h1>Aurora Shop</h1><form id="record-form"></form><button data-status-action>Confirm status</button><section class="metric-card">Dashboard total count</section><button data-manager-action>Review</button></main><script src="/static/preview_bridge.js" defer></script><script src="app.js" defer></script></body></html>
"""
    identical_js = "fetch('/api/orders', { method: 'POST' });\nfetch('/api/orders/1', { method: 'PATCH' });\nconst dashboard = 'metric dashboard review status queue';\n"
    identical_css = ".page-shell { color: #172033; }\n.record-card { border: 1px solid #ddd; }\n.metric-card { padding: 12px; }\n"
    for role in ("client", "specialist", "manager"):
        role_dir = source_dir / "miniapp/app/static" / role
        role_dir.mkdir(parents=True, exist_ok=True)
        role_dir.joinpath("index.html").write_text(identical_html.replace("/static/client/", f"/static/{role}/"), encoding="utf-8")
        role_dir.joinpath("app.js").write_text(identical_js, encoding="utf-8")
        role_dir.joinpath("styles.css").write_text(identical_css, encoding="utf-8")
        _write_role_child(source_dir, role, "profile")
        _write_role_child(source_dir, role, "catalog")
        manifest["roles"][role] = {
            "routes": {
                f"/{role}": f"static/{role}/index.html",
                f"/{role}/profile": f"static/{role}/profile/index.html",
                f"/{role}/catalog": f"static/{role}/catalog/index.html",
            }
        }
    manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.identical_role_surfaces" in "\n".join(result.logs)


def test_agentic_platform_invariants_reject_roles_without_actions(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    (source_dir / "miniapp/app/static/specialist/app.js").write_text(
        "window.setupPreviewBridge?.('specialist');\nconst queue = 'read only queue';\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/specialist/app.js"],
        scope_mode="agentic",
    )

    assert result.status == "failed"
    assert "platform.missing_role_workflow_actions" in "\n".join(result.logs)


def test_fast_agentic_platform_invariants_accept_one_child_page_per_role(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    manifest = {"roles": {}, "shared": {}}
    for role in ("client", "specialist", "manager"):
        _write_role_root(source_dir, role)
        _write_role_child(source_dir, role, "work")
        manifest["roles"][role] = {
            "routes": {
                f"/{role}": f"static/{role}/index.html",
                f"/{role}/work": f"static/{role}/work/index.html",
            }
        }
    manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html"],
        scope_mode="agentic",
        generation_mode=GenerationMode.FAST,
    )

    assert result.status == "passed"
    assert result.diagnostics["multipage_coverage"]["client"]["route_count"] == 2


def test_agentic_platform_invariants_accept_multipage_role_surfaces(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    manifest = {"roles": {}, "shared": {}}
    for role in ("client", "specialist", "manager"):
        _write_role_root(source_dir, role)
        _write_role_child(source_dir, role, "profile")
        _write_role_child(source_dir, role, "catalog")
        manifest["roles"][role] = {
            "routes": {
                f"/{role}": f"static/{role}/index.html",
                f"/{role}/profile": f"static/{role}/profile/index.html",
                f"/{role}/catalog": f"static/{role}/catalog/index.html",
            }
        }
    manifest_path = source_dir / "miniapp/app/generated/route_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html", "miniapp/app/generated/route_manifest.json"],
        scope_mode="agentic",
    )

    assert result.status == "passed"
    assert result.diagnostics["multipage_coverage"]["client"]["route_count"] == 3
    assert "/specialist/profile" in CheckRunner._root_preview_routes(source_dir)


def test_balanced_agentic_platform_invariants_reject_shallow_design_depth(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html", "miniapp/app/generated/route_manifest.json"],
        scope_mode="agentic",
        generation_mode=GenerationMode.BALANCED,
    )

    assert result.status == "failed"
    assert "platform.insufficient_mode_design_depth" in "\n".join(result.logs)


def test_quality_agentic_platform_invariants_require_three_child_pages_per_role(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    quality_css = (
        ".page-shell.quality-app { color: #172033; }\n"
        ".dashboard-grid { display: grid; }\n"
        ".metric-card { padding: 12px; }\n"
        ".status-badge { border: 1px solid #ccd3df; }\n"
        ".empty-state { color: #607086; }\n"
        ".loading-state { opacity: .72; }\n"
        ".success-state { color: #147a44; }\n"
        ".error-state { color: #a33131; }\n"
        ".workflow-form { display: grid; }\n"
        ".input-field { min-height: 40px; }\n"
        ".action-button { min-height: 40px; }\n"
        ".list-panel { display: grid; }\n"
        ".action-button:focus-visible { outline: 2px solid #2459d6; }\n"
        "@media (max-width: 640px) { .dashboard-grid { grid-template-columns: 1fr; } }\n"
    )
    for role in ("client", "specialist", "manager"):
        (source_dir / "miniapp/app/static" / role / "styles.css").write_text(quality_css, encoding="utf-8")
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html", "miniapp/app/generated/route_manifest.json"],
        scope_mode="agentic",
        generation_mode=GenerationMode.QUALITY,
    )

    assert result.status == "failed"
    assert "platform.single_page_role_surface" in "\n".join(result.logs)
    assert result.diagnostics["multipage_coverage"]["client"]["required_route_count"] == 4


def test_agentic_create_invariants_require_persistent_post_api(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/index.html", "miniapp/app/generated/route_manifest.json"],
        scope_mode="agentic",
        intent="create",
    )

    assert result.status == "failed"
    joined_logs = "\n".join(result.logs)
    assert "platform.missing_create_get_api" in joined_logs
    assert "platform.missing_create_post_api" in joined_logs
    assert "platform.missing_create_update_api" in joined_logs
    assert result.diagnostics["api_contract"]["frontend_post_refs"] == ["/api/orders"]
    assert result.diagnostics["api_contract"]["frontend_update_refs"] == ["/api/orders/1"]


def test_agentic_create_invariants_do_not_block_on_raw_api_method_evidence(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    routes_dir = source_dir / "miniapp/app/routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    routes_dir.joinpath("orders.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/orders')\n\n"
        "@router.get('')\n"
        "def list_orders():\n"
        "    return []\n\n"
        "@router.post('')\n"
        "def create_order(payload: dict):\n"
        "    return payload\n\n"
        "@router.patch('/{order_id}/status')\n"
        "def update_order_status(order_id: int, payload: dict):\n"
        "    return payload\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "const ordersEndpoint = '/api/orders';\n"
        "const createOptions = { method: 'POST', body: JSON.stringify({ title: 'Order' }) };\n"
        "submitToBackend(ordersEndpoint, createOptions);\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/static/specialist/app.js").write_text(
        "const statusEndpoint = '/api/orders/1/status';\n"
        "const updateOptions = { method: 'PATCH', body: JSON.stringify({ status: 'ready' }) };\n"
        "submitToBackend(statusEndpoint, updateOptions);\nconst queue = 'confirm status queue';\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js", "miniapp/app/static/specialist/app.js", "miniapp/app/routes/orders.py"],
        scope_mode="agentic",
        intent="create",
    )

    assert result.status == "passed"
    logs = "\n".join(result.logs)
    assert "platform.frontend_missing_post_api" in logs
    assert "\"blocking\": false" in logs
    assert result.diagnostics["api_contract"]["frontend_raw_methods"] == ["PATCH", "POST"]


def test_agentic_create_invariants_reject_preloaded_business_data(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    _write_multipage_role_surfaces(source_dir)
    _write_generated_test_placeholders(source_dir)
    routes_dir = source_dir / "miniapp/app/routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    routes_dir.joinpath("orders.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/orders')\n\n"
        "@router.get('')\n"
        "def list_orders():\n"
        "    return []\n\n"
        "@router.post('')\n"
        "def create_order(payload: dict):\n"
        "    return payload\n",
        encoding="utf-8",
    )
    client_app = source_dir / "miniapp/app/static/client/app.js"
    client_app.write_text(
        "const seedRecords = [{ id: 1, title: 'Preloaded order' }];\n"
        "fetch('/api/orders', { method: 'POST', body: JSON.stringify({ title: 'User order' }) });\n",
        encoding="utf-8",
    )
    runner = object.__new__(CheckRunner)

    result = runner._platform_invariants_smoke(
        source_dir=source_dir,
        changed_files=["miniapp/app/static/client/app.js", "miniapp/app/routes/orders.py"],
        scope_mode="agentic",
        intent="create",
    )

    assert result.status == "failed"
    assert "platform.preloaded_business_data" in "\n".join(result.logs)
    assert result.diagnostics["preloaded_data_findings"][0]["file_path"] == "miniapp/app/static/client/app.js"


def test_agentic_generated_app_tests_are_required(tmp_path: Path) -> None:
    runner = object.__new__(CheckRunner)
    backend_dir = tmp_path / "miniapp"
    (backend_dir / "app").mkdir(parents=True)

    python_result = runner._run_python_app_tests(backend_dir, require_present=True)
    js_result = runner._run_js_app_tests(backend_dir, require_present=True)

    assert python_result.status == "failed"
    assert js_result.status == "failed"
    assert python_result.diagnostics["missing_test_file"] == "tests/test_generated_app.py"
    assert js_result.diagnostics["missing_test_file"] == "tests/generated_app.test.mjs"


def test_generated_js_test_double_miniapp_path_is_diagnostic() -> None:
    logs = [
        "Error: ENOENT: no such file or directory",
        "/tmp/workspace/source/miniapp/miniapp/app/static/client/index.html",
    ]

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(logs)

    assert diagnostics["js_test_path_root"]["problem"] == "generated_js_test_prefixed_miniapp_twice"
    assert "cwd=miniapp" in diagnostics["js_test_path_root"]["expected_root"]


def test_generated_tests_explain_server_html_vs_js_rendered_content() -> None:
    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "AssertionError: 'Waterproof jacket' not found in '<!doctype html>...'",
            "assert(html.includes(\"Packing cubes\"))",
            'TypeError [ERR_INVALID_ARG_TYPE]: The "paths[0]" argument must be of type string. Received an instance of URL',
        ]
    )

    assert diagnostics["server_rendered_html_assertion"]["problem"] == "test_asserts_js_rendered_text_in_server_html"
    assert "TestClient sees HTML before browser JavaScript runs" in diagnostics["server_rendered_html_assertion"]["expected_scope"]
    assert diagnostics["static_html_assertion"]["problem"] == "js_test_asserts_dynamic_text_only_in_html"
    assert diagnostics["js_test_url_path_api"]["problem"] == "generated_js_test_passed_url_to_path_api"
    assert "fileURLToPath" in diagnostics["js_test_url_path_api"]["expected_path_api"]


def test_generated_js_test_failure_reports_assertion_source(tmp_path: Path) -> None:
    test_file = tmp_path / "generated_app.test.mjs"
    test_file.write_text(
        "\n".join(
            [
                'import { test } from "node:test";',
                'import assert from "node:assert";',
                'test("insights page mentions smart adoption trends", () => {',
                '  assert(insightsHtml.includes("voice-first devices"));',
                "});",
            ]
        ),
        encoding="utf-8",
    )

    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "✖ insights page mentions smart adoption trends",
            "at TestContext.<anonymous> (file:///workspace/miniapp/tests/generated_app.test.mjs:4:3)",
        ],
        test_file=test_file,
    )

    assert diagnostics["failing_test_location"]["line"] == 4
    assert diagnostics["assertion_source"]["source"] == 'assert(insightsHtml.includes("voice-first devices"));'
    assert diagnostics["expected_literal"] == "voice-first devices"


def test_generated_post_persistence_failure_is_diagnostic() -> None:
    diagnostics = CheckRunner._extract_generated_app_test_diagnostics(
        [
            "FAIL: test_create_order_persists (test_generated_app.GeneratedAppTest.test_create_order_persists)",
            "POST /api/orders record did not persist. Payload: {'order_id': 'ord-1'}",
        ]
    )

    assert diagnostics["post_persistence_failure"]["path"] == "/api/orders"
    assert diagnostics["post_persistence_failure"]["resource_slug"] == "orders"


def test_preview_ready_probes_all_role_roots(monkeypatch) -> None:
    probed: list[str] = []
    service = object.__new__(PreviewService)

    monkeypatch.setattr(PreviewService, "_probe_base_urls", staticmethod(lambda _url: ["http://preview.local"]))
    monkeypatch.setattr(PreviewService, "_probe_http", staticmethod(lambda url: probed.append(url) or True))

    assert service._http_preview_ready("http://preview.local")
    assert probed == [
        "http://preview.local/health",
        "http://preview.local/client",
        "http://preview.local/specialist",
        "http://preview.local/manager",
    ]


def test_connectivity_validator_accepts_api_routes_declared_inside_role_module(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "fetch('/api/products');\nfetch('/api/orders', { method: 'POST' });\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/client.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/client')\n"
        "api = APIRouter(prefix='/api')\n\n"
        "@api.get('/products')\n"
        "def products():\n"
        "    return []\n\n"
        "@api.post('/orders')\n"
        "def orders():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_accepts_base_api_constant_when_child_routes_exist(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="API Base Constant Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "const API_BASE = '/api/study-sprint';\nfetch(`${API_BASE}/topics`);\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/study_sprint.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/study-sprint')\n\n"
        "@router.get('/topics')\n"
        "def topics():\n"
        "    return []\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_flags_missing_nested_api_route(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Nested API Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "fetch(`/api/client/bookings/${bookingId}`, { method: 'DELETE' });\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/studio.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/api/client/bookings')\n"
        "def bookings():\n"
        "    return []\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert any(
        issue.code == "connectivity.missing_backend_route"
        and "/api/client/bookings/{param}" in issue.message
        for issue in issues
    )


def test_connectivity_validator_flags_post_when_only_get_route_exists(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="POST Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "fetch('/api/orders', { method: 'POST', body: JSON.stringify({ title: 'Order' }) });\n",
        encoding="utf-8",
    )
    (source_dir / "miniapp/app/routes/orders.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/api/orders')\n"
        "def orders():\n"
        "    return []\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert any(
        issue.code == "connectivity.missing_backend_route"
        and "POST /api/orders" in issue.message
        for issue in issues
    )


def test_connectivity_validator_ignores_unused_api_string_literals(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Unused API Literal Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    (source_dir / "miniapp/app/static/client/app.js").write_text(
        "const helpText = 'Документация упоминает /api/not-a-real-call только как текст';\n",
        encoding="utf-8",
    )

    issues = ConnectivityValidator().validate(source_dir)

    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)


def test_connectivity_validator_detects_methods_inside_fetch_wrappers(tmp_path: Path) -> None:
    app = create_app(repo_root=Path(__file__).resolve().parents[3], data_dir=tmp_path / "data")
    workspace = app.state.container.workspace_service.create_workspace(
        WorkspaceRecord(name="Fetch Wrapper Connectivity Workspace", path=str(tmp_path / "workspace"))
    )
    app.state.container.workspace_service.clone_template(workspace.workspace_id)
    source_dir = app.state.container.workspace_service.source_dir(workspace.workspace_id)
    script = (
        "await fetchJSON('/api/orders', { method: 'POST', body: JSON.stringify({ title: 'Order' }) });\n"
        "await fetchJSON(`/api/orders/${orderId}/status`, { method: 'PATCH', body: JSON.stringify({ status }) });\n"
    )
    (source_dir / "miniapp/app/static/specialist/app.js").write_text(script, encoding="utf-8")
    (source_dir / "miniapp/app/routes/orders.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api/orders')\n\n"
        "@router.post('')\n"
        "def create_order(payload: dict):\n"
        "    return payload\n\n"
        "@router.patch('/{order_id}/status')\n"
        "def update_order_status(order_id: int, payload: dict):\n"
        "    return payload\n",
        encoding="utf-8",
    )

    refs = ConnectivityValidator._extract_api_refs(script)
    runner_refs = CheckRunner._extract_frontend_api_refs(script)
    issues = ConnectivityValidator().validate(source_dir)

    assert ("POST", "/api/orders") in refs
    assert ("PATCH", "/api/orders/{param}/status") in refs
    assert ("PATCH", "/api/orders/{param}/status") in runner_refs
    assert not any(issue.code == "connectivity.missing_backend_route" for issue in issues)
