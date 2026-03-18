from __future__ import annotations

import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app

ROLE_PREFIX = {
    "client": "Client",
    "specialist": "Specialist",
    "manager": "Manager",
}


def _install_llm_stub(app) -> None:
    openrouter = app.state.container.openrouter_client
    openrouter.api_key = "test-key"

    def fake_generate_structured(*, role: str, schema_name: str, schema: dict, system_prompt: str, user_prompt: str) -> dict:
        del role, schema, system_prompt
        payload = json.loads(user_prompt)
        if schema_name == "grounded_spec_outline_v1":
            return {
                "model": "openai/gpt-5-mini",
                "payload": {
                    "product_goal": str(payload.get("prompt") or "Generated mini-app"),
                    "roles": [
                        {"role": "client", "responsibility": "Browse and place orders.", "primary_actions": ["Browse", "Order", "Track"]},
                        {"role": "specialist", "responsibility": "Process incoming work.", "primary_actions": ["Review", "Pack", "Resolve"]},
                        {"role": "manager", "responsibility": "Oversee operations.", "primary_actions": ["Inspect", "Prioritize", "Supervise"]},
                    ],
                    "entities": ["Order", "Catalog", "Status"],
                    "flows": [{"name": "Primary flow", "goal": "Move through the main app journey.", "roles": ["client", "specialist", "manager"]}],
                    "api_needs": ["Read records", "Update statuses"],
                    "risks": ["Provider instability"],
                },
            }
        if schema_name == "grounded_spec_v1":
            prompt = str(payload.get("prompt") or "Generated mini-app")
            return {"model": "openai/gpt-5-mini", "payload": _grounded_spec_payload(prompt)}
        if schema_name == "role_contract_v1":
            return {"model": "openai/gpt-5-mini", "payload": _role_contract_payload(payload.get("role_scope") or [])}
        if schema_name == "page_graph_v2":
            return {
                "model": "openai/gpt-5-mini",
                "payload": _page_graph_payload(
                    role_scope=payload.get("role_scope") or [],
                    scope_mode=str(payload.get("scope_mode") or "app_surface_build"),
                ),
            }
        if schema_name.startswith("page_file_v1_"):
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _page_file_payload(payload)}
        if schema_name == "composition_bundle_v1":
            return {"model": "openai/gpt-5.1-codex-mini", "payload": _composition_payload(payload)}
        raise AssertionError(f"Unexpected schema name: {schema_name}")

    openrouter.generate_structured = fake_generate_structured


def _grounded_spec_payload(prompt: str) -> dict:
    return {
        "metadata": {
            "workspace_id": "ws_stub",
            "conversation_id": "conv_stub",
            "prompt_turn_id": "turn_stub",
            "template_revision_id": "rev_stub",
        },
        "target_platform": "telegram_mini_app",
        "preview_profile": "telegram_mock",
        "product_goal": prompt,
        "actors": [
            {
                "actor_id": "actor_client",
                "name": "Shopper",
                "role": "client",
                "description": "Primary customer flow.",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "domain_entities": [
            {
                "entity_id": "entity_order",
                "name": "Order",
                "description": "Customer order and processing state.",
                "attributes": [{"name": "status", "type": "string", "required": True}],
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "user_flows": [
            {
                "flow_id": "flow_primary",
                "name": "Primary flow",
                "goal": "Move through the main app journey.",
                "steps": [{"step_id": "step_1", "order": 1, "actor_id": "actor_client", "action": "Open the app"}],
                "acceptance_criteria": ["The generated app exposes real routes and pages."],
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "ui_requirements": [
            {
                "req_id": "ui_1",
                "category": "navigation",
                "description": "Provide real routed pages.",
                "priority": "must",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "explicit"}],
            }
        ],
        "api_requirements": [],
        "persistence_requirements": [],
        "integration_requirements": [],
        "security_requirements": [
            {
                "security_req_id": "sec_1",
                "category": "telegram_initdata",
                "rule": "Validate Telegram init data.",
                "severity": "critical",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "platform_constraints": [
            {
                "constraint_id": "platform_1",
                "category": "sdk",
                "rule": "Use Telegram Mini App compatible runtime behavior.",
                "severity": "critical",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "non_functional_requirements": [
            {
                "nfr_id": "nfr_1",
                "category": "usability",
                "description": "Avoid template-like placeholder screens.",
                "priority": "must",
                "evidence": [{"doc_ref_id": "prompt-source", "evidence_type": "derived"}],
            }
        ],
        "assumptions": [
            {
                "assumption_id": "assume_1",
                "text": "Use stubbed LLM responses in tests.",
                "status": "active",
                "rationale": "The backend tests verify the orchestration flow without calling a real provider.",
            }
        ],
        "unknowns": [],
        "contradictions": [],
        "doc_refs": [],
    }


def _role_contract_payload(role_scope: list[str]) -> dict:
    templates = {
        "client": {
            "responsibility": "Explore the offering, open detail pages, submit a request or order, and track progress.",
            "entry_goal": "Help the user start from a clear landing screen and move into the core journey.",
            "primary_jobs": ["Browse", "Open details", "Submit", "Track"],
            "key_entities": ["Order", "Request", "Catalog"],
            "ui_style_notes": ["Use welcoming entry language", "Make primary actions obvious"],
            "success_states": ["Action submitted", "Status visible"],
            "must_differ_from": ["specialist", "manager"],
        },
        "specialist": {
            "responsibility": "Handle incoming work items, progress them, and resolve blocked cases.",
            "entry_goal": "Surface the active queue and the next task to process.",
            "primary_jobs": ["Review queue", "Open work item", "Change status", "Resolve blockers"],
            "key_entities": ["Queue", "Task", "Issue"],
            "ui_style_notes": ["Keep workspace dense but readable", "Expose action context quickly"],
            "success_states": ["Work item updated", "Queue pressure reduced"],
            "must_differ_from": ["client", "manager"],
        },
        "manager": {
            "responsibility": "See overall health, supervise operations, and inspect items that need intervention.",
            "entry_goal": "Give an overview first, then routes into management surfaces.",
            "primary_jobs": ["Review dashboard", "Inspect activity", "Watch workload", "Intervene"],
            "key_entities": ["Metrics", "Team", "Operations"],
            "ui_style_notes": ["Keep overview concise", "Avoid operational clutter on the landing screen"],
            "success_states": ["Attention points visible", "Operational trends readable"],
            "must_differ_from": ["client", "specialist"],
        },
    }
    return {
        "app_title": "Booking workspace",
        "app_summary": "A routed booking and operations mini app with separate customer, specialist, and manager flows.",
        "shared_entities": ["Booking", "Status", "Team load"],
        "shared_logic": ["Routing", "Role-aware navigation", "Action feedback"],
        "roles": [{"role": role, **templates[role]} for role in role_scope],
    }


def _page_graph_payload(*, role_scope: list[str], scope_mode: str) -> dict:
    if scope_mode == "minimal_patch":
        pages = [
            {
                "role": "client",
                "entry_path": "/",
                "landing_page_id": "client_home",
                "routes_file": "frontend/src/roles/client/ClientRoutes.tsx",
                "pages": [
                    _page(
                        "client_home",
                        "/",
                        "Home",
                        "ClientHomePage",
                        "frontend/src/roles/client/pages/ClientHomePage.tsx",
                        "Client workspace",
                        "A real role landing page with actions instead of placeholder metrics.",
                    ),
                    _page(
                        "client_profile",
                        "/profile",
                        "Profile",
                        "ClientProfilePage",
                        "frontend/src/roles/client/pages/ClientProfile/ClientProfilePage.tsx",
                        "Profile",
                        "Profile editing page.",
                    ),
                ],
            }
        ]
        return {
            "summary": "Refine the existing client flow without touching unrelated roles.",
            "flow_mode": "multi_page",
            "files_to_read": [
                "frontend/src/roles/client/pages/ClientHomePage.tsx",
                "frontend/src/shared/ui/templates/RoleProfileEditorPage.tsx",
            ],
            "target_files": ["frontend/src/roles/client/pages/ClientHomePage.tsx"],
            "shared_files": [],
            "backend_targets": [],
            "page_graph": {
                "app_title": "Booking workspace",
                "summary": "Refine the client landing surface while preserving the rest of the workspace.",
                "flow_mode": "multi_page",
                "roles": pages,
            },
        }

    templates = {
        "client": [
            _page("client_home", "/", "Home", "ClientHomePage", "frontend/src/roles/client/pages/generated/ClientHomePage.tsx", "Booking home", "Entry page with clear shopper actions."),
            _page("client_catalog", "/catalog", "Catalog", "ClientCatalogPage", "frontend/src/roles/client/pages/generated/ClientCatalogPage.tsx", "Catalog", "Browse services or products."),
            _page("client_detail", "/details/:recordId", "Detail", "ClientDetailPage", "frontend/src/roles/client/pages/generated/ClientDetailPage.tsx", "Detail", "Open an item and inspect its details."),
        ],
        "specialist": [
            _page("specialist_home", "/", "Desk", "SpecialistHomePage", "frontend/src/roles/specialist/pages/generated/SpecialistHomePage.tsx", "Operations desk", "Entry page for active work."),
            _page("specialist_queue", "/queue", "Queue", "SpecialistQueuePage", "frontend/src/roles/specialist/pages/generated/SpecialistQueuePage.tsx", "Queue", "List of active items."),
            _page("specialist_detail", "/queue/:recordId", "Task", "SpecialistDetailPage", "frontend/src/roles/specialist/pages/generated/SpecialistDetailPage.tsx", "Task workspace", "Detailed work item page."),
        ],
        "manager": [
            _page("manager_home", "/", "Overview", "ManagerHomePage", "frontend/src/roles/manager/pages/generated/ManagerHomePage.tsx", "Operations overview", "Landing page with attention points."),
            _page("manager_dashboard", "/dashboard", "Dashboard", "ManagerDashboardPage", "frontend/src/roles/manager/pages/generated/ManagerDashboardPage.tsx", "Dashboard", "Overall status and trends."),
            _page("manager_team", "/team", "Team", "ManagerTeamPage", "frontend/src/roles/manager/pages/generated/ManagerTeamPage.tsx", "Team load", "Supervision of current team load."),
        ],
    }
    roles = []
    target_files = ["frontend/src/shared/generated/appChrome.tsx", "frontend/src/shared/generated/appState.tsx"]
    for role in role_scope:
        role_pages = templates[role]
        roles.append(
            {
                "role": role,
                "entry_path": "/",
                "landing_page_id": role_pages[0]["page_id"],
                "routes_file": f"frontend/src/roles/{role}/{ROLE_PREFIX[role]}Routes.tsx",
                "pages": role_pages,
            }
        )
        target_files.append(f"frontend/src/roles/{role}/{ROLE_PREFIX[role]}Routes.tsx")
        target_files.extend(page["file_path"] for page in role_pages)
    return {
        "summary": "Create a routed booking workspace with custom role-specific pages.",
        "flow_mode": "multi_page",
        "files_to_read": [
            "frontend/src/shared/ui/templates/RoleProfileEditorPage.tsx",
            "frontend/src/shared/ui/generated/GeneratedRoleScreen.module.css",
        ],
        "target_files": target_files,
        "shared_files": [
            "frontend/src/shared/generated/appChrome.tsx",
            "frontend/src/shared/generated/appState.tsx",
        ],
        "backend_targets": [],
        "page_graph": {
            "app_title": "Booking workspace",
            "summary": "A routed booking mini app with separate client, specialist, and manager page trees.",
            "flow_mode": "multi_page",
            "roles": roles,
        },
    }


def _page(page_id: str, route_path: str, navigation_label: str, component_name: str, file_path: str, title: str, description: str) -> dict:
    return {
        "page_id": page_id,
        "route_path": route_path,
        "navigation_label": navigation_label,
        "component_name": component_name,
        "file_path": file_path,
        "title": title,
        "description": description,
        "purpose": description,
        "page_kind": "workspace",
        "primary_actions": ["Open", "Continue"],
        "data_dependencies": ["records"],
        "loading_state": "Loading content…",
        "empty_state": "No items yet.",
        "error_state": "Something went wrong.",
    }


def _page_file_payload(payload: dict) -> dict:
    page = payload["page"]
    role = payload["role"]
    component_name = page["component_name"]
    title = page["title"]
    description = page["description"]
    home_link = "/" if page["route_path"] != "/" else "/profile"
    home_label = "Profile" if page["route_path"] == "/" else "Back home"
    content = f"""import {{ Link }} from 'react-router-dom';

export function {component_name}(): JSX.Element {{
  return (
    <section>
      <header>
        <span>{role}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </header>
      <div>
        <p>Real routed page content for {component_name}.</p>
        <Link to="{home_link}">{home_label}</Link>
      </div>
    </section>
  );
}}
"""
    return {
        "assistant_message": f"Generated {component_name}.",
        "operations": [
            {
                "file_path": page["file_path"],
                "operation": "replace",
                "content": content,
                "reason": f"Implement the {page['title']} page.",
            }
        ],
    }


def _composition_payload(payload: dict) -> dict:
    page_graph = payload["page_graph"]
    target_files = set(payload["target_files"])
    operations: list[dict] = []
    if "frontend/src/shared/generated/appChrome.tsx" in target_files:
        operations.append(
            {
                "file_path": "frontend/src/shared/generated/appChrome.tsx",
                "operation": "replace",
                "content": "export function GeneratedPageFrame(props: { title: string; body: string }): JSX.Element { return <section><h1>{props.title}</h1><p>{props.body}</p></section>; }\n",
                "reason": "Provide a minimal shared frame for generated pages.",
            }
        )
    if "frontend/src/shared/generated/appState.tsx" in target_files:
        operations.append(
            {
                "file_path": "frontend/src/shared/generated/appState.tsx",
                "operation": "replace",
                "content": "export function useGeneratedAppState(): { loading: boolean } { return { loading: false }; }\n",
                "reason": "Provide generated shared state helpers.",
            }
        )
    for role, role_payload in (page_graph.get("roles") or {}).items():
        routes_file = role_payload["routes_file"]
        if routes_file not in target_files:
            continue
        operations.append(
            {
                "file_path": routes_file,
                "operation": "replace",
                "content": _routes_file_source(role, role_payload["pages"]),
                "reason": f"Wire real routed pages for {role}.",
            }
        )
    return {"assistant_message": "Composed shared files and routes.", "operations": operations}


def _routes_file_source(role: str, pages: list[dict]) -> str:
    imports = ["import { Navigate, Route, Routes } from 'react-router-dom';", "import { AppShell } from '@/app/layout/AppShell';"]
    route_lines = []
    for page in pages:
        import_path = page["file_path"].replace("frontend/src/", "@/").removesuffix(".tsx")
        imports.append(f"import {{ {page['component_name']} }} from '{import_path}';")
        route_path = page["route_path"]
        if route_path == "/":
            route_lines.append('        <Route index element={{<{component} />}} />'.format(component=page["component_name"]))
        else:
            relative_path = route_path.lstrip("/")
            route_lines.append(
                '        <Route path="{path}" element={{<{component} />}} />'.format(
                    path=relative_path,
                    component=page["component_name"],
                )
            )
    imports_block = "\n".join(imports)
    return f"""{imports_block}

export function {ROLE_PREFIX[role]}Routes(): JSX.Element {{
  return (
    <Routes>
      <Route element={{<AppShell />}}>
{chr(10).join(route_lines)}
        <Route path="*" element={{<Navigate to="/" replace />}} />
      </Route>
    </Routes>
  );
}}
"""


def test_generation_pipeline_smoke(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Smoke Workspace",
            "description": "End-to-end smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    document = client.post(
        f"/workspaces/{workspace_id}/documents",
        json={
            "file_name": "requirements.md",
            "file_path": "docs/requirements.md",
            "source_type": "project_doc",
            "content": "Booking form with name, phone, date and comment fields.",
        },
    ).json()
    index_response = client.post(f"/documents/{document['document_id']}/index")
    assert index_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Create a consultation booking mini-app with name phone date and comment fields.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client", "specialist", "manager"],
            "model_profile": "openai_code_fast",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "basic",
        },
    )
    assert run_response.status_code == 200
    run = run_response.json()

    final_run = run
    for _ in range(90):
        current = client.get(f"/runs/{run['run_id']}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["status"] == "awaiting_approval"
    assert final_run["apply_status"] == "awaiting_approval"
    assert final_run["draft_ready"] is True

    spec_payload = client.get(f"/workspaces/{workspace_id}/spec/current").json()
    validation_payload = client.get(f"/workspaces/{workspace_id}/validation/current").json()
    preview_payload = client.get(f"/workspaces/{workspace_id}/preview/url").json()
    file_tree = client.get(f"/workspaces/{workspace_id}/files/tree").json()
    draft_tree = client.get(f"/workspaces/{workspace_id}/files/tree?run_id={run['run_id']}").json()
    draft_diff = client.get(f"/workspaces/{workspace_id}/diff?run_id={run['run_id']}").json()["diff"]

    assert spec_payload["product_goal"].startswith("Create a consultation booking")
    assert validation_payload["blocking"] is False
    assert preview_payload["url"] is None or preview_payload["url"].startswith("http://localhost:")
    if preview_payload["url"] is None:
        assert preview_payload["role_urls"] == {}
    else:
        assert preview_payload["role_urls"]["client"].startswith(preview_payload["url"])
    assert all(item["path"] != "backend/app/generated/runtime_manifest.json" for item in file_tree)
    assert any(item["path"] == "artifacts/grounded_spec.json" for item in draft_tree)
    assert any(item["path"] == "artifacts/generated_app_graph.json" for item in draft_tree)
    assert "ClientRoutes.tsx" in draft_diff
    assert "generated_app_graph.json" in draft_diff

    preview_response = client.get(f"/preview/{workspace_id}?role=manager&run_id={run['run_id']}")
    assert preview_response.status_code == 200
    assert "booking" in preview_response.text.lower()

    approve_response = client.post(f"/runs/{run['run_id']}/approve")
    assert approve_response.status_code == 200
    approved_run = approve_response.json()
    assert approved_run["status"] == "completed"
    assert approved_run["apply_status"] == "applied"
    assert approved_run["draft_status"] == "approved"

    save_response = client.post(
        f"/workspaces/{workspace_id}/files/save",
        json={
            "relative_path": "docs/manual-note.md",
            "content": "# Manual edit\n\nThis file was added in the smoke test.\n",
        },
    )
    assert save_response.status_code == 200

    diff_response = client.get(f"/workspaces/{workspace_id}/diff")
    assert diff_response.status_code == 200
    assert "manual-note.md" in diff_response.json()["diff"]

    zip_export = client.post(f"/workspaces/{workspace_id}/export/zip").json()
    patch_export = client.post(f"/workspaces/{workspace_id}/export/git-patch").json()
    assert Path(zip_export["file_path"]).exists()
    assert Path(patch_export["file_path"]).exists()

    events_response = client.get(f"/jobs/{final_run['linked_job_id']}/events")
    assert events_response.status_code == 200
    assert "job_completed" in events_response.text


def test_quality_mode_blocks_without_openrouter(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Quality Workspace",
            "description": "Quality mode should block without OpenRouter",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Build a high-quality role-aware consultation workflow mini-app.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "quality",
        },
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "blocked"
    assert payload["failure_reason"]


def test_run_api_exposes_artifacts_and_links_job(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Run Workspace",
            "description": "Run API smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    run_response = client.post(
        f"/workspaces/{workspace_id}/runs",
        json={
            "prompt": "Refine the existing mini-app with a stronger AI workspace framing and preserved role previews.",
            "intent": "auto",
            "apply_strategy": "manual_approve",
            "target_role_scope": ["client"],
            "model_profile": "openai_code_fast",
            "generation_mode": "basic",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["status"] in {"pending", "running", "awaiting_approval"}

    final_run = run_payload
    for _ in range(90):
        current = client.get(f"/runs/{run_payload['run_id']}")
        assert current.status_code == 200
        final_run = current.json()
        if final_run["status"] in {"awaiting_approval", "blocked", "failed"}:
            break
        time.sleep(0.2)

    assert final_run["linked_job_id"]
    assert final_run["status"] == "awaiting_approval"
    assert final_run["apply_status"] == "awaiting_approval"
    assert final_run["draft_status"] == "ready"
    assert final_run["draft_ready"] is True

    list_response = client.get(f"/workspaces/{workspace_id}/runs")
    assert list_response.status_code == 200
    listed_runs = list_response.json()
    assert listed_runs[0]["run_id"] == final_run["run_id"]

    artifact_response = client.get(f"/runs/{final_run['run_id']}/artifacts")
    assert artifact_response.status_code == 200
    artifacts = artifact_response.json()
    assert artifacts["run"]["run_id"] == final_run["run_id"]
    assert artifacts["job"]["job_id"] == final_run["linked_job_id"]
    assert artifacts["code_change_plan"]["summary"]
    assert isinstance(artifacts["code_change_plan"]["targets"], list)
    assert artifacts["page_graph"]["page_graph"]["roles"]["client"]["pages"]
    assert artifacts["role_contract"]["role_contract"]["roles"]["client"]["responsibility"]
    assert isinstance(artifacts["iterations"], list)
    assert artifacts["candidate_diff"]
    assert "validation" in artifacts
    assert "trace" in artifacts

    iterations_response = client.get(f"/runs/{final_run['run_id']}/iterations")
    assert iterations_response.status_code == 200
    iterations = iterations_response.json()
    assert iterations
    assert iterations[0]["role_scope"] == ["client"]

    discard_response = client.post(f"/runs/{final_run['run_id']}/discard")
    assert discard_response.status_code == 200
    discarded_run = discard_response.json()
    assert discarded_run["draft_status"] == "discarded"


def test_generate_endpoint_acts_as_compatibility_shim(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    _install_llm_stub(app)
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Compat Workspace",
            "description": "Generate endpoint compatibility smoke test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]
    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    job_response = client.post(
        f"/workspaces/{workspace_id}/generate",
        json={
            "prompt": "Create a booking mini-app draft through the compatibility endpoint.",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
            "generation_mode": "basic",
        },
    )
    assert job_response.status_code == 200
    payload = job_response.json()
    assert payload["status"] == "completed"
    assert payload["summary"]
    file_tree = client.get(f"/workspaces/{workspace_id}/files/tree").json()
    assert all(item["path"] != "backend/app/generated/runtime_manifest.json" for item in file_tree)


def test_clone_template_boots_preview_automatically(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    app = create_app(repo_root=repo_root, data_dir=tmp_path / "data")
    client = TestClient(app)

    workspace = client.post(
        "/workspaces",
        json={
            "name": "Preview Workspace",
            "description": "Preview should boot after cloning",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    workspace_id = workspace["workspace_id"]

    clone_response = client.post(f"/workspaces/{workspace_id}/clone-template")
    assert clone_response.status_code == 200

    preview_payload = None
    for _ in range(20):
        response = client.get(f"/workspaces/{workspace_id}/preview/url")
        assert response.status_code == 200
        preview_payload = response.json()
        if preview_payload["status"] in {"running", "error"}:
            break
        time.sleep(0.1)

    assert preview_payload is not None
    assert preview_payload["status"] in {"running", "error"}
